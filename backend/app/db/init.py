"""
Database bootstrap (Track T2, step 5).

alembic is not available in this environment (and downloading it is not an
option), so schema creation is plain DDL driven straight off the ORM models:
``Base.metadata.create_all()`` is idempotent, so it is safe to run on every
startup and on a completely fresh database.

    python -m app.db.init          # create tables + report what exists
    python -m app.db.init --drop   # start over (local dev only)
"""
import argparse
import sys

from sqlalchemy import inspect, text

from app.db.models import Base, GroundTruth, Incident
from app.db.session import backend_name, get_engine, migrate_schema, session_scope


def init_db() -> list:
    """Create any missing tables. Returns the table names afterwards."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    migrate_schema(engine)          # create_all never alters existing tables
    return sorted(inspect(engine).get_table_names())


def _drop_db() -> None:
    engine = get_engine()
    Base.metadata.drop_all(engine)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create Track T2 tables.")
    parser.add_argument("--drop", action="store_true",
                        help="drop all tables first (local dev only)")
    args = parser.parse_args(argv)

    if args.drop:
        _drop_db()
        print("  dropped all tables")

    tables = init_db()
    engine = get_engine()

    print(f"  backend : {backend_name()}")
    print(f"  tables  : {', '.join(tables) or '(none)'}")

    for model in (GroundTruth, Incident):
        with session_scope() as session:
            n = session.execute(
                text(f"SELECT COUNT(*) FROM {model.__tablename__}")
            ).scalar_one()
        print(f"  {model.__tablename__:<14}: {n} row(s)")

    expected = {"ground_truth", "incidents"}
    missing = expected - set(tables)
    if missing:
        print(f"  MISSING TABLES: {sorted(missing)}", file=sys.stderr)
        return 1
    print("  OK — schema ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
