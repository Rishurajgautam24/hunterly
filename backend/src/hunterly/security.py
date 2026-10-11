"""A per-install secret used as a CSRF token on the web forms."""

import secrets

from fastapi import HTTPException

from hunterly.config import get_settings

_secret: str | None = None


def install_secret() -> str:
    global _secret
    if _secret is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        path = settings.data_dir / "secret.txt"
        if not path.exists():
            path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            path.chmod(0o600)  # owner-only on macOS/Linux; Windows only honours the read-only bit
        _secret = path.read_text(encoding="utf-8").strip()
    return _secret


def reset_secret() -> None:
    global _secret
    _secret = None


def check_csrf(token: str) -> None:
    if not token or not secrets.compare_digest(token, install_secret()):
        raise HTTPException(status_code=403, detail="Form expired or invalid; reload the page.")
