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
import json
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
        migrate_schema(engine)                        # existing DBs catch up (idempotent)
        _engine = engine
        return _engine


def backend_name() -> Optional[str]:
    """'postgresql', 'sqlite', or None before the first connection attempt."""
    if _engine is None:
        get_engine()
    return _backend


def sqlite_path() -> Path:
    return SQLITE_PATH


def _injection_mode_from_action(action) -> Optional[str]:
    """Recover a *recorded* injection mode from ``real_mesh_action``.

    Only what the injector actually wrote down counts: the explicit
    ``injection_mode`` key (T7's local-simulation branch) or a
    ``CHAOS_MESH_*`` action name (its real-CR branch). An action that
    discloses neither stays ``None`` — the mode is never guessed.
    """
    if isinstance(action, str):
        try:
            action = json.loads(action)
        except (TypeError, ValueError):
            return None
    if not isinstance(action, dict):
        return None
    explicit = action.get("injection_mode")
    if explicit:
        return str(explicit)
    if str(action.get("action", "")).startswith("CHAOS_MESH"):
        return "chaos_mesh"
    return None


def migrate_schema(engine: Engine) -> int:
    """Bring tables that already exist up to the current models.

    ``Base.metadata.create_all()`` only creates *missing tables* — it never
    adds a column to a table that is already there. Track T7's
    ``injection_mode`` disclosure was therefore dropped from every ledger row
    persisted before the column existed (``save_ground_truth`` whitelists
    model fields), so a restart erased the honesty contract's mode from the
    ledger. This adds the column and backfills rows from the recorded
    ``real_mesh_action``, leaving rows it cannot classify as ``NULL``.

    Idempotent; returns how many rows were backfilled.
    """
    from sqlalchemy import inspect

    inspector = inspect(engine)
    if "ground_truth" not in inspector.get_table_names():
        return 0

    columns = {c["name"] for c in inspector.get_columns("ground_truth")}
    if "injection_mode" not in columns:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE ground_truth ADD COLUMN injection_mode VARCHAR(30)"
            ))
        logger.info("db migration: added ground_truth.injection_mode")

    backfilled = 0
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT experiment_id, real_mesh_action FROM ground_truth "
            "WHERE injection_mode IS NULL"
        )).fetchall()
        for experiment_id, action in rows:
            mode = _injection_mode_from_action(action)
            if mode:
                conn.execute(
                    text("UPDATE ground_truth SET injection_mode = :mode "
                         "WHERE experiment_id = :eid"),
                    {"mode": mode, "eid": experiment_id},
                )
                backfilled += 1
    if backfilled:
        logger.info("db migration: backfilled injection_mode on %d row(s)", backfilled)
    return backfilled


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
