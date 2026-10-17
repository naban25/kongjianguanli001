"""SQLAlchemy engine, session factory and declarative base."""
from __future__ import annotations

import logging
from typing import Generator

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
_connect_args = settings.db_connect_args()

engine = create_engine(settings.database_url, **_connect_args)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Naming convention for Alembic migrations (constraint names, indexes etc.)
naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=naming_convention)


class Base(DeclarativeBase):
    metadata = metadata


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a DB session and closes it after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_db_connection() -> tuple[bool, str]:
    """Quick health-check ping against the database."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "ok"
    except SQLAlchemyError as exc:
        logger.warning("DB health check failed: %s", exc)
        return False, str(exc)
