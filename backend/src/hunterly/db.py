"""SQLite engine and session helpers."""

from collections.abc import Iterator

from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from hunterly.config import get_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{settings.db_path}", connect_args={"check_same_thread": False}
        )
        from hunterly import models  # noqa: F401  (register tables)

        SQLModel.metadata.create_all(_engine)
        _add_missing_columns(_engine)
    return _engine


def _add_missing_columns(engine: Engine) -> None:
    """Tiny migration: add columns introduced after a database was created (all nullable)."""
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            have = {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
            for column in table.columns:
                if column.name not in have:
                    kind = column.type.compile(dialect=engine.dialect)
                    conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {kind}')


def reset_engine() -> None:
    """Forget the cached engine (used by tests after changing HUNTERLY_DATA_DIR)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
