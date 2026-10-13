"""Database engine and session management.

SQLite by default so the editor runs with zero infrastructure; point
MAKMA_DATABASE_URL at Postgres for anything shared or production-bound.
"""
from __future__ import annotations

import logging

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import settings

log = logging.getLogger("makma.db")


class Base(DeclarativeBase):
    pass


def _make_engine():
    url = settings.database_url
    if url.startswith("sqlite"):
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30},
            poolclass=StaticPool if ":memory:" in url else None,
            future=True,
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(conn, _rec):  # pragma: no cover - driver glue
            cur = conn.cursor()
            # WAL lets the inline worker thread write while the API reads.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True, future=True)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from . import models  # noqa: F401  (registers mappers)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Add columns the models declare but an existing SQLite file lacks.

    This is not a migration system and does not pretend to be one: it only
    adds nullable-or-defaulted columns, and it will not rename, drop, change a
    type, or backfill. It exists so that pulling a change does not leave a
    running development database throwing OperationalError on every query.

    A deployment with real data needs a real migration tool. See
    docs/SECURITY.md, "Not implemented".
    """
    from sqlalchemy import inspect, text

    if not engine.url.get_backend_name().startswith("sqlite"):
        return

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present or column.primary_key:
                    continue
                ddl = _column_ddl(column)
                if ddl is None:
                    log.warning(
                        "column %s.%s cannot be added automatically; the "
                        "database needs a real migration",
                        table.name, column.name,
                    )
                    continue
                log.info("adding column %s.%s", table.name, column.name)
                connection.execute(
                    text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}")
                )


def _column_ddl(column) -> str | None:
    """The `ALTER TABLE ADD COLUMN` fragment, or None if it is not safe."""
    from sqlalchemy.schema import CreateColumn

    if not column.nullable and column.default is None and column.server_default is None:
        # SQLite cannot add a NOT NULL column without a default, and guessing
        # one would silently invent data.
        return None

    ddl = str(CreateColumn(column).compile(bind=engine))
    default = getattr(column.default, "arg", None)
    if default is not None and not callable(default) and "DEFAULT" not in ddl.upper():
        if isinstance(default, bool):
            literal = "1" if default else "0"
        elif isinstance(default, (int, float)):
            literal = str(default)
        else:
            escaped = str(default).replace("'", "''")
            literal = f"'{escaped}'"
        ddl = f"{ddl} DEFAULT {literal}"
    return ddl


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for workers and scripts."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
