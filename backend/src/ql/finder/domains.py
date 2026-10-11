"""Company domains: normalise, look up mail servers (MX), suggest likely domains."""

import re
import time

import dns.exception
import dns.resolver

from ql.finder.names import local_part

_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](-?[a-z0-9])*\.)+[a-z]{2,63}$")
_COMPANY_SUFFIXES = {
    "inc", "llc", "ltd", "limited", "pvt", "private", "plc", "gmbh", "corp", "corporation",
    "co", "company", "technologies", "technology", "solutions", "labs", "india", "group",
}
SUGGEST_TLDS = [".com", ".in", ".io", ".ai", ".co"]


def normalize_domain(value: str | None) -> str | None:
    """'https://www.Acme.com/about' -> 'acme.com'. Returns None if it isn't a domain."""
    if not value:
        return None
    text = value.strip().lower()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    text = re.sub(r"^[a-z]+://", "", text).split("/")[0].split(":")[0]
    text = text.removeprefix("www.")
    return text if _DOMAIN_RE.match(text) else None


class MXLookupError(Exception):
    """DNS itself failed (no network, timeout), so we can't say whether mail servers exist."""


_CANARY = "gmail.com"  # always has mail servers; used to spot a broken resolver
_canary: tuple[float, bool] | None = None


def _dns_sane(timeout: float) -> bool:
    global _canary
    now = time.monotonic()
    if _canary is None or now - _canary[0] > 600:
        try:
            ok = bool(_resolve_mx(_CANARY, timeout))
        except MXLookupError:
            ok = False
        _canary = (now, ok)
    return _canary[1]


def mx_hosts(domain: str, timeout: float = 5.0) -> list[str]:
    """Mail servers for a domain, best first. Empty if the domain has none.
    Raises MXLookupError if DNS could not be reached or answers nonsense."""
    hosts = _resolve_mx(domain, timeout)
    if not hosts and domain != _CANARY and not _dns_sane(timeout):
        raise MXLookupError("DNS looks broken here (no mail server found even for gmail.com)")
    return hosts


def _resolve_mx(domain: str, timeout: float) -> list[str]:
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = timeout
        answers = resolver.resolve(domain, "MX")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.resolver.NoNameservers, dns.exception.Timeout,
            dns.resolver.NoResolverConfiguration, OSError) as exc:
        raise MXLookupError(f"DNS lookup for {domain} failed: {type(exc).__name__}") from exc
    records = sorted(answers, key=lambda r: r.preference)
    hosts = [str(r.exchange).rstrip(".") for r in records]
    return [h for h in hosts if h]  # a "null MX" (".") means the domain takes no mail


def suggest_domains(company_name: str) -> list[str]:
    """Likely domains for a company name, e.g. 'Acme Technologies Pvt Ltd' -> acme.com, ..."""
    words = [local_part(w) for w in company_name.split()]
    words = [w for w in words if w]
    core = [w for w in words if w not in _COMPANY_SUFFIXES] or words
    if not core:
        return []
    stems = list(dict.fromkeys(["".join(core), core[0], "".join(words)]))
    return [stem + tld for stem in stems for tld in SUGGEST_TLDS][:10]


def has_mail_server(domain: str, timeout: float = 5.0) -> bool | None:
    """True / False, or None when DNS couldn't be reached."""
    try:
        return bool(mx_hosts(domain, timeout))
    except MXLookupError:
        return None
