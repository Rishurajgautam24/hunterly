"""The verifier service for the cloud VM (where outbound port 25 is open).

Run on the VM:   QL_VERIFIER_KEY=... QL_SMTP_HELO_HOST=vm.example.com uv run ql-verifier
Reach it from the Mac through an SSH tunnel (ssh -L 8766:127.0.0.1:8766 vm) and set
QL_VERIFIER_URL=http://127.0.0.1:8766 and the same QL_VERIFIER_KEY in the backend's .env.
"""

import os
import secrets

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from ql.config import get_settings
from ql.finder.domains import normalize_domain
from ql.finder.smtp_check import RateLimiter, verify_domain

app = FastAPI(title="QL verifier", docs_url=None, redoc_url=None)
_limiter: RateLimiter | None = None


class VerifyRequest(BaseModel):
    domain: str
    addresses: list[str] = Field(min_length=1, max_length=10)


def require_key(authorization: str = Header(default="")) -> None:
    expected = os.environ.get("QL_VERIFIER_KEY", "")
    given = authorization.removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="bad or missing key")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/verify", dependencies=[Depends(require_key)])
def verify(req: VerifyRequest) -> dict:
    global _limiter
    settings = get_settings()
    domain = normalize_domain(req.domain)
    if not domain or any(not a.lower().endswith("@" + domain) for a in req.addresses):
        raise HTTPException(status_code=422, detail="addresses must belong to the domain")
    if _limiter is None:
        _limiter = RateLimiter(settings.smtp_checks_per_domain_per_hour)
    if not _limiter.take(domain, len(req.addresses) + 1):
        raise HTTPException(status_code=429, detail="hourly limit for this domain reached")
    report = verify_domain(domain, req.addresses, helo_host=settings.smtp_helo_host,
                           mail_from=settings.mail_from, timeout=settings.smtp_timeout)
    return report.to_dict()


def main() -> None:
    if not os.environ.get("QL_VERIFIER_KEY"):
        raise SystemExit("Set QL_VERIFIER_KEY before starting the verifier.")
    host = os.environ.get("QL_VERIFIER_HOST", "127.0.0.1")
    port = int(os.environ.get("QL_VERIFIER_PORT", "8766"))
    uvicorn.run(app, host=host, port=port)
