import logging
import os
import time

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import make_url
from sqlmodel import SQLModel, create_engine, Session
from sqlmodel.sql.sqltypes import UTCDateTime

logger = logging.getLogger("quizlab.startup")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./quizlab.db")

_is_postgres = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

# Startup must fail loudly instead of hanging (a deploy once sat on "Running
# uvicorn" for 10+ minutes with no output). Every limit can be raised from
# the environment without a code change.
#   DB_CONNECT_TIMEOUT          seconds to wait for the TCP/TLS connection
#   MIGRATION_LOCK_TIMEOUT      how long a schema change may wait for a lock
#                               held by another connection (old instance,
#                               open SQL editor transaction...)
#   MIGRATION_STATEMENT_TIMEOUT cap on any single migration statement
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))
MIGRATION_LOCK_TIMEOUT = os.getenv("MIGRATION_LOCK_TIMEOUT", "5s")
MIGRATION_STATEMENT_TIMEOUT = os.getenv("MIGRATION_STATEMENT_TIMEOUT", "60s")

if _is_postgres:
    engine = create_engine(
        DATABASE_URL, echo=False, pool_pre_ping=True,
        connect_args={"connect_timeout": DB_CONNECT_TIMEOUT,
                      "application_name": "quizlab"})
else:
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


class StartupDatabaseError(RuntimeError):
    """Raised (and shown in the deploy log) when the database step of startup
    cannot complete. The message says which step and what to check."""


def describe_database() -> str:
    """The database URL with the password masked, for logs."""
    try:
        return make_url(DATABASE_URL).render_as_string(hide_password=True)
    except Exception:
        return "postgres (unparseable URL)" if _is_postgres else "sqlite"


# Columns added after the initial schema: (table, column, SQLite type,
# Postgres type). Only columns that are actually missing are ALTERed, so a
# failure is a real error rather than "column already exists".
_COLUMN_MIGRATIONS = [
    ("quiz", "read_time", "INTEGER DEFAULT 5", "INTEGER DEFAULT 5"),
    ("quiz", "scoring_mode", "VARCHAR DEFAULT 'speed'", "VARCHAR DEFAULT 'speed'"),
    ("quiz", "streak_bonus", "BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
    ("quizsession", "class_id", "INTEGER", "INTEGER"),
    ("sessionresult", "student_id", "INTEGER", "INTEGER"),
]


def _step(name: str, t0: float):
    logger.info("startup: %s (%.1fs)", name, time.monotonic() - t0)


def create_db_and_tables(bind=None):
    """Connect, create missing tables and apply column migrations, in one
    transaction (Postgres DDL is transactional: all or nothing), with
    lock/statement timeouts so a blocked migration errors out quickly.
    `bind` defaults to the app engine (tests pass their own)."""
    bind = bind if bind is not None else engine
    pg = bind.dialect.name == "postgresql"
    t0 = time.monotonic()
    logger.info("startup: connecting to %s (timeout %ss)",
                describe_database() if bind is engine
                else bind.url.render_as_string(hide_password=True),
                DB_CONNECT_TIMEOUT)
    try:
        conn = bind.connect()
    except Exception as exc:
        raise StartupDatabaseError(
            f"could not connect to the database within {DB_CONNECT_TIMEOUT}s "
            f"({type(exc).__name__}: {exc}). Check DATABASE_URL. On Render + "
            f"Supabase use the pooler connection string (IPv4); the direct "
            f"db.<ref>.supabase.co host is IPv6-only.") from exc
    try:
        _step("connected", t0)
        with conn.begin():                  # commit on success, else roll back
            if pg:
                conn.exec_driver_sql(
                    f"SET LOCAL lock_timeout = '{MIGRATION_LOCK_TIMEOUT}'")
                conn.exec_driver_sql(
                    f"SET LOCAL statement_timeout = '{MIGRATION_STATEMENT_TIMEOUT}'")
            _migrate(conn, t0, pg)
    except StartupDatabaseError:
        raise
    except Exception as exc:
        hint = ""
        text = str(exc).lower()
        if "lock timeout" in text:
            hint = (f" A schema change waited more than {MIGRATION_LOCK_TIMEOUT} "
                    f"for a table lock held by another connection (the previous "
                    f"instance during a deploy, or an open transaction in the "
                    f"Supabase SQL editor). Check pg_stat_activity and retry.")
        elif "statement timeout" in text:
            hint = (f" A migration statement ran longer than "
                    f"{MIGRATION_STATEMENT_TIMEOUT}.")
        raise StartupDatabaseError(
            f"database migration failed, nothing was changed "
            f"({type(exc).__name__}: {exc}).{hint}") from exc
    finally:
        conn.close()
    _step("database ready", t0)


def _migrate(conn, t0, pg):
    inspector = sa_inspect(conn)
    tables = set(inspector.get_table_names())
    _step(f"found {len(tables)} tables", t0)

    if "question" in tables:
        q_cols = {c["name"] for c in inspector.get_columns("question")}
        if "options_json" not in q_cols:
            logger.warning("startup: QuizLab database schema is outdated (question "
                           "table has no options_json). %s All existing quizzes "
                           "will need to be re-created.",
                           "Drop and recreate the public schema in Supabase."
                           if pg else "Delete quizlab.db and restart.")

    missing = [t.name for t in SQLModel.metadata.sorted_tables if t.name not in tables]
    if missing:
        logger.info("startup: creating tables: %s", ", ".join(missing))
        SQLModel.metadata.create_all(conn)
        _step("tables created", t0)

    for table, column, sqlite_type, pg_type in _COLUMN_MIGRATIONS:
        if table in missing:
            continue        # just created with every column
        cols = {c["name"] for c in sa_inspect(conn).get_columns(table)}
        if column in cols:
            continue
        ddl = f"ALTER TABLE {table} ADD COLUMN {column} " + (
            pg_type if pg else sqlite_type)
        logger.info("startup: migration: %s", ddl)
        conn.exec_driver_sql(ddl)
    _step("column migrations checked", t0)

    if pg:
        _migrate_timestamps_to_timestamptz(conn)
        _step("timestamp columns checked", t0)


def _migrate_timestamps_to_timestamptz(conn):
    """Postgres: datetime columns created before the app stored aware UTC are
    `timestamp without time zone` holding naive UTC. Convert them to
    timestamptz, reading the existing values as UTC. Idempotent: only
    columns still without a time zone are touched; a rerun does nothing.
    (SQLite has no such type; UTCDateTime reads its naive values as UTC.)"""
    wanted = {(t.name, c.name) for t in SQLModel.metadata.sorted_tables
              for c in t.columns if isinstance(c.type, UTCDateTime)}
    naive = conn.exec_driver_sql(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND data_type = 'timestamp without time zone'").fetchall()
    for table, column in sorted(set(map(tuple, naive)) & wanted):
        ddl = (f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
               f'TYPE TIMESTAMP WITH TIME ZONE USING "{column}" '
               "AT TIME ZONE 'UTC'")
        logger.info("startup: migration: %s", ddl)
        conn.exec_driver_sql(ddl)


def get_session():
    with Session(engine) as session:
        yield session
