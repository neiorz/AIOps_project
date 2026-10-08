"""
Database engine / session management (Track T2).

Backend selection happens ONCE, on first use:

  1. try ``settings.POSTGRES_URL`` (rewritten to the psycopg 3 dialect,
     because SQLAlchemy routes bare ``postgresql://`` to psycopg2, which is
     not installed here);
  2. if the server cannot be reached, fall back to a local SQLite file.

The choice is logged loudly. A silent fallback would hide a broken Postgres
for the whole life of the process, which is exactly the class of "quiet
no-op" this codebase refuses to ship.

Nothing here blocks on import: ``from app.db.session import SessionLocal``
must succeed even with no database present at all (PHASE_PLAN T2 step 1).
"""
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger(__name__)

#: Local fallback file. gitignored — it is runtime state, not source.
SQLITE_PATH: Path = Path(settings.BASE_DIR) / "app" / "db" / "aiops_ground_truth.db"

_engine: Optional[Engine] = None
_backend: Optional[str] = None
_lock = threading.Lock()


def _as_psycopg(url: str) -> str:
    """Force the psycopg 3 dialect (psycopg2 is not installed)."""
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    return url


def _try_postgres() -> Engine:
    engine = create_engine(
        _as_psycopg(settings.POSTGRES_URL),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 4},
    )
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return engine


def _make_sqlite() -> Engine:
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{SQLITE_PATH}",
        connect_args={"check_same_thread": False},
    )
    # WAL keeps readers unblocked while a chaos experiment writes a row.
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):  # pragma: no cover - driver callback
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


def get_engine() -> Engine:
    """Return the process-wide engine, choosing the backend once."""
    global _engine, _backend
    if _engine is not None:
        return _engine

    with _lock:
        if _engine is not None:
            return _engine
        try:
            engine = _try_postgres()
            _backend = "postgresql"
            logger.info(
                "DB backend: PostgreSQL (%s)", settings.POSTGRES_URL.split("@")[-1]
            )
        except Exception as exc:                      # noqa: BLE001 - any driver error
            reason = str(exc).strip().splitlines()[0][:160]
            logger.warning(
                "DB backend: PostgreSQL unreachable (%s) -> falling back to SQLite "
                "at %s. Set POSTGRES_URL to a reachable server to use Postgres.",
                reason,
                SQLITE_PATH,
            )
            engine = _make_sqlite()
            _backend = "sqlite"

        from app.db.models import Base

        Base.metadata.create_all(engine)              # fresh DB auto-creates tables
        _engine = engine
        return _engine


def backend_name() -> Optional[str]:
    """'postgresql', 'sqlite', or None before the first connection attempt."""
    if _engine is None:
        get_engine()
    return _backend


def sqlite_path() -> Path:
    return SQLITE_PATH


def _new_session() -> Session:
    """expire_on_commit=False keeps rows readable after the block commits."""
    return Session(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Commit/rollback-managed session for scripts and repository calls."""
    session = _new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency — yields a session and always closes it."""
    session = _new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_for_tests() -> None:
    """Drop the cached engine so a test can force a different backend."""
    global _engine, _backend
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _backend = None
