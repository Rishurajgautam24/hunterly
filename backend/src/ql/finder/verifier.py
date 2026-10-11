"""Run the mail-server check here, or on the verifier VM when QL_VERIFIER_URL is set."""

import httpx

from ql.config import get_settings
from ql.finder.smtp_check import UNKNOWN, AddressResult, DomainReport, RateLimiter, verify_domain

_limiter: RateLimiter | None = None


def _get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(get_settings().smtp_checks_per_domain_per_hour)
    return _limiter


def verify(domain: str, addresses: list[str]) -> DomainReport:
    settings = get_settings()
    if settings.verifier_url:
        return _remote(settings.verifier_url, settings.verifier_key, domain, addresses)

    # +1 for the catch-all probe address.
    if not _get_limiter().take(domain, len(addresses) + 1):
        msg = "hourly check limit for this domain reached; try again later"
        return DomainReport(domain, None, None, [AddressResult(a, UNKNOWN, None, msg)
                                                 for a in addresses], error=msg)
    return verify_domain(domain, addresses, helo_host=settings.smtp_helo_host,
                         mail_from=settings.mail_from, timeout=settings.smtp_timeout)


def _remote(url: str, key: str | None, domain: str, addresses: list[str]) -> DomainReport:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        resp = httpx.post(f"{url.rstrip('/')}/verify", json={"domain": domain,
                          "addresses": addresses}, headers=headers, timeout=90)
        resp.raise_for_status()
        return DomainReport.from_dict(resp.json())
    except httpx.HTTPError as exc:
        msg = f"verifier unreachable: {exc}"
        return DomainReport(domain, None, None, [AddressResult(a, UNKNOWN, None, msg)
                                                 for a in addresses], error=msg)
