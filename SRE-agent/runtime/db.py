from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from runtime.models import Base
from runtime.settings import get_settings

_ENGINE = None
_SESSION_FACTORY = None
_INITIALIZED = False


def _build_engine():
    settings = get_settings()
    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    return create_engine(settings.database_url, future=True, pool_pre_ping=True, connect_args=connect_args)


def get_engine():
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = _build_engine()
    return _ENGINE


def get_session_factory():
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)
    return _SESSION_FACTORY


def init_db() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    Base.metadata.create_all(bind=get_engine())
    _INITIALIZED = True


@contextmanager
def session_scope() -> Iterator[Session]:
    init_db()
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_db_state() -> None:
    global _ENGINE, _SESSION_FACTORY, _INITIALIZED
    _ENGINE = None
    _SESSION_FACTORY = None
    _INITIALIZED = False
