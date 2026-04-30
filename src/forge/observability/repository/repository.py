"""SQLAlchemy-backed read repository for the analytics API."""

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.sql import Executable

from forge.observability.config import get_settings


class Repository:
    """Read repository backed by any SQLAlchemy-supported database."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def query(self, stmt: Executable) -> list[dict]:
        with self._engine.connect() as conn:
            result = conn.execute(stmt)
            return [dict(row._mapping) for row in result]

    def query_one(self, stmt: Executable) -> dict | None:
        rows = self.query(stmt)
        return rows[0] if rows else None


@lru_cache(maxsize=1)
def _get_engine() -> Engine:
    return create_engine(get_settings().datastore_dsn)


def get_repository() -> Repository:
    return Repository(_get_engine())
