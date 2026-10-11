import pytest

from hunterly import db, security
from hunterly.config import get_settings
from hunterly.finder import verifier


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own data folder and database, and no real Ollama."""
    monkeypatch.setenv("HUNTERLY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HUNTERLY_OLLAMA_URL", "http://127.0.0.1:9")  # nothing listens here
    get_settings.cache_clear()
    db.reset_engine()
    security.reset_secret()
    verifier._limiter = None
    yield
    db.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def session():
    from sqlmodel import Session

    with Session(db.get_engine()) as s:
        yield s
