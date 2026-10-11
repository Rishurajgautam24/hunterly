"""Hunter.io as a backup when the mail server can't be asked directly (port 25 blocked,
greylisting, catch-all domains). Free plan: 50 credits a month; the finder only charges
when it returns an address."""

from dataclasses import dataclass

import httpx

API = "https://api.hunter.io/v2"
_STATUS = {"valid": "valid", "invalid": "invalid", "accept_all": "accept_all",
           "disposable": "invalid", "webmail": "unknown", "unknown": "unknown"}


class HunterError(Exception):
    pass


@dataclass
class HunterResult:
    email: str
    status: str
    score: int | None


def _get(path: str, params: dict, key: str) -> dict:
    try:
        resp = httpx.get(f"{API}/{path}", params={**params, "api_key": key}, timeout=25)
    except httpx.HTTPError as exc:
        raise HunterError(f"Hunter unreachable: {exc}") from exc
    if resp.status_code == 401:
        raise HunterError("Hunter rejected the API key")
    if resp.status_code == 429:
        raise HunterError("Hunter credits used up for this month")
    if resp.status_code == 403:
        raise HunterError("Hunter rate limit; try again in a minute")
    if resp.status_code >= 400:
        raise HunterError(f"Hunter error {resp.status_code}")
    return resp.json().get("data") or {}


def find(domain: str, first: str, last: str, key: str) -> HunterResult | None:
    params = {"domain": domain, "first_name": first, "last_name": last} if last else \
        {"domain": domain, "full_name": first}
    data = _get("email-finder", params, key)
    if not data.get("email"):
        return None
    status = (data.get("verification") or {}).get("status") or "unknown"
    return HunterResult(data["email"].lower(), _STATUS.get(status, "unknown"), data.get("score"))


def verify(address: str, key: str) -> HunterResult:
    data = _get("email-verifier", {"email": address}, key)
    return HunterResult(address, _STATUS.get(data.get("status", "unknown"), "unknown"), data.get("score"))
