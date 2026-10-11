"""Ask a domain's mail server whether addresses exist, without sending any email.

For each domain: connect to its best MX host on port 25, say EHLO, give a MAIL FROM, then
ask RCPT TO for each candidate and read the reply code. One made-up address is also tried:
if the server accepts it, the domain accepts everything ("catch-all") and accepted
candidates are downgraded to accept_all. The session ends with QUIT; no DATA is sent.
"""

import secrets
import smtplib
import socket
import ssl
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from hunterly.finder.domains import MXLookupError, mx_hosts

VALID, INVALID, ACCEPT_ALL, UNKNOWN = "valid", "invalid", "accept_all", "unknown"

# Words in a 5xx reply that mean "you are blocked", not "no such mailbox".
_POLICY_HINTS = ("5.7.", "block", "spamhaus", "policy", "not permitted", "reputation",
                 "rbl", "blacklist", "listed")


class _TLSFailed(Exception):
    """STARTTLS broke the session; retry the host without it."""


# Last time a mail server refused us for policy reasons (blocklisted IP), for the dashboard.
last_policy_block: tuple[float, str] | None = None


@dataclass
class AddressResult:
    address: str
    status: str
    code: int | None = None
    message: str = ""


@dataclass
class DomainReport:
    domain: str
    mx_host: str | None
    catch_all: bool | None
    results: list[AddressResult] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DomainReport":
        results = [AddressResult(**r) for r in data.get("results", [])]
        return cls(data["domain"], data.get("mx_host"), data.get("catch_all"), results,
                   data.get("error"))


def classify(code: int | None, message: str = "") -> str:
    if code is None:
        return UNKNOWN
    if code in (250, 251):
        return VALID
    if 500 <= code < 600:
        lowered = message.lower()
        # "No such user" in enhanced codes, incl. Microsoft 365's 5.4.1 recipient rejection.
        if "5.1." in lowered or ("5.4.1" in lowered and "recipient address rejected" in lowered):
            return INVALID
        if code in (550, 551, 553) and not any(h in lowered for h in _POLICY_HINTS):
            return INVALID
        return UNKNOWN
    return UNKNOWN  # 4xx: greylisting or a temporary failure


def _text(msg: bytes | str) -> str:
    return msg.decode(errors="replace") if isinstance(msg, bytes) else str(msg)


class RateLimiter:
    """At most `limit` checks per domain in any rolling hour (in memory)."""

    def __init__(self, limit: int, window: float = 3600.0):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def take(self, domain: str, count: int) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[domain]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) + count > self.limit:
                return False
            hits.extend([now] * count)
            return True


def verify_domain(
    domain: str,
    addresses: list[str],
    *,
    helo_host: str,
    mail_from: str,
    timeout: float = 10.0,
    mx_lookup: Callable[[str], list[str]] = mx_hosts,
    smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
) -> DomainReport:
    try:
        hosts = mx_lookup(domain)
    except MXLookupError as exc:
        results = [AddressResult(a, UNKNOWN, None, str(exc)) for a in addresses]
        return DomainReport(domain, None, None, results, error=str(exc))
    if not hosts:
        results = [AddressResult(a, INVALID, None, "domain has no mail server") for a in addresses]
        return DomainReport(domain, None, None, results, error="no MX record")

    last_error = ""
    for host in hosts[:2]:
        try:
            try:
                return _probe(host, domain, addresses, helo_host, mail_from, timeout,
                              smtp_factory, use_tls=True)
            except _TLSFailed:
                return _probe(host, domain, addresses, helo_host, mail_from, timeout,
                              smtp_factory, use_tls=False)
        except (OSError, smtplib.SMTPException) as exc:  # socket.timeout is an OSError
            last_error = f"{host}: {type(exc).__name__}: {exc}"

    hint = " (outbound port 25 may be blocked on this network)"
    results = [AddressResult(a, UNKNOWN, None, last_error) for a in addresses]
    return DomainReport(domain, None, None, results, error=last_error + hint)


def _opportunistic_tls() -> ssl.SSLContext:
    # Like mail servers do between themselves: encrypt if possible, don't insist on a valid cert.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _probe(host, domain, addresses, helo_host, mail_from, timeout, smtp_factory,
           use_tls: bool = True) -> DomainReport:
    with smtp_factory(host, 25, local_hostname=helo_host, timeout=timeout) as smtp:
        smtp.ehlo_or_helo_if_needed()
        if use_tls and smtp.has_extn("starttls"):
            try:
                smtp.starttls(context=_opportunistic_tls())
                smtp.ehlo()
            except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
                raise _TLSFailed(str(exc)) from exc
        code, msg = smtp.mail(mail_from)
        if code >= 400:
            raise smtplib.SMTPSenderRefused(code, msg, mail_from)

        results: list[AddressResult] = []
        for i, address in enumerate(addresses):
            try:
                code, msg = smtp.rcpt(address)
            except smtplib.SMTPServerDisconnected as exc:
                for rest in addresses[i:]:
                    results.append(AddressResult(rest, UNKNOWN, None, f"disconnected: {exc}"))
                return DomainReport(domain, host, None, results, error="server disconnected")
            text = _text(msg)
            results.append(AddressResult(address, classify(code, text), code, text[:200]))
            if code and code >= 500 and any(h in text.lower() for h in _POLICY_HINTS):
                global last_policy_block
                last_policy_block = (time.monotonic(), host)
                for rest in addresses[i + 1:]:  # no point asking again; it will refuse all
                    results.append(AddressResult(rest, UNKNOWN, code, text[:200]))
                return DomainReport(domain, host, None, results, error=text[:300])

        probe = f"hunterly-check-{secrets.token_hex(5)}@{domain}"
        try:
            pcode, pmsg = smtp.rcpt(probe)
            verdict = classify(pcode, _text(pmsg))
            catch_all = True if verdict == VALID else False if verdict == INVALID else None
        except smtplib.SMTPServerDisconnected:
            catch_all = None
        if catch_all:
            for r in results:
                if r.status == VALID:
                    r.status = ACCEPT_ALL
        return DomainReport(domain, host, catch_all, results)


_port25_cache: tuple[float, bool] | None = None


def port25_status(max_age: float = 3600.0) -> bool:
    """port25_reachable(), remembered for an hour so pages stay fast."""
    global _port25_cache
    now = time.monotonic()
    if _port25_cache is None or now - _port25_cache[0] > max_age:
        _port25_cache = (now, port25_reachable(timeout=4.0))
    return _port25_cache[1]


def port25_reachable(host: str = "gmail-smtp-in.l.google.com", timeout: float = 8.0) -> bool:
    """True if this machine can open an outbound connection on port 25."""
    try:
        with socket.create_connection((host, 25), timeout=timeout):
            return True
    except OSError:
        return False
