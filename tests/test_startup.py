"""Startup: admin/secret settings, and database migrations that are
idempotent, logged step by step, and fail with a clear error instead of
hanging.

The Postgres tests run only when QUIZLAB_TEST_PG_URL points at a scratch
database they may freely drop tables in, e.g.
    set QUIZLAB_TEST_PG_URL=postgresql://postgres@127.0.0.1:54329/quizlab_test

Run from the project root:   venv\\Scripts\\python -m unittest discover tests -v
"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import _support  # noqa: F401  (test DATABASE_URL, sys.path)
import database
from sqlalchemy import create_engine, inspect, text

PG_URL = os.environ.get("QUIZLAB_TEST_PG_URL")

# The schema as it was before the column migrations existed.
OLD_SCHEMA = [
    "CREATE TABLE quiz (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, "
    "course_tag VARCHAR, created_at TIMESTAMP, last_played TIMESTAMP)",
    "CREATE TABLE question (id INTEGER PRIMARY KEY, quiz_id INTEGER NOT NULL "
    "REFERENCES quiz(id), position INTEGER, text VARCHAR NOT NULL, "
    "question_type VARCHAR, options_json VARCHAR, correct_json VARCHAR, "
    "time_limit INTEGER, points INTEGER, image_url VARCHAR)",
    "INSERT INTO quiz (id, name) VALUES (1, 'old quiz')",
]


def _columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


class AuthSettings(unittest.TestCase):

    def test_production_requires_both(self):
        import main
        with self.assertRaises(RuntimeError) as ctx:
            main._auth_settings(True, {})
        self.assertIn("ADMIN_PASSWORD and SECRET_KEY", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx:
            main._auth_settings(True, {"ADMIN_PASSWORD": "x"})
        self.assertIn("SECRET_KEY", str(ctx.exception))
        self.assertEqual(main._auth_settings(True, {"ADMIN_PASSWORD": "p",
                                                    "SECRET_KEY": "s"}), ("p", "s"))

    def test_local_sqlite_fallback_is_not_admin123(self):
        import main
        password, secret = main._auth_settings(False, {})
        self.assertEqual(password, "admin")
        self.assertTrue(secret)
        self.assertNotIn("admin123", Path(main.__file__).read_text(encoding="utf-8"))


class _MigrationCases:
    """Shared by the SQLite and Postgres variants: self.engine is a fresh,
    empty database."""

    def _old_db(self):
        with self.engine.begin() as c:
            for stmt in OLD_SCHEMA:
                c.execute(text(stmt))

    def test_old_schema_is_upgraded_and_rerun_is_a_noop(self):
        self._old_db()
        with self.assertLogs("quizlab.startup", "INFO") as logs:
            database.create_db_and_tables(self.engine)
        self.assertLessEqual({"read_time", "scoring_mode", "streak_bonus"},
                             _columns(self.engine, "quiz"))
        self.assertIn("student_id", _columns(self.engine, "sessionresult"))
        joined = "\n".join(logs.output)
        for step in ("connecting to", "connected", "creating tables",
                     "migration: ALTER TABLE quiz ADD COLUMN read_time",
                     "database ready"):
            self.assertIn(step, joined)
        with self.engine.connect() as c:
            row = c.execute(text("SELECT name, read_time, scoring_mode FROM quiz")).one()
        self.assertEqual(tuple(row), ("old quiz", 5, "speed"))
        with self.assertLogs("quizlab.startup", "INFO") as logs:
            database.create_db_and_tables(self.engine)
        self.assertNotIn("migration:", "\n".join(logs.output))

    def test_fresh_database(self):
        database.create_db_and_tables(self.engine)
        self.assertIn("livesession", inspect(self.engine).get_table_names())


class SqliteMigrations(_MigrationCases, unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quizlab_mig_")
        self.engine = create_engine(f"sqlite:///{Path(self.tmp, 'm.db').as_posix()}")

    def tearDown(self):
        self.engine.dispose()

    def test_unreachable_database_fails_with_clear_error(self):
        bad = create_engine("sqlite:///" + Path(self.tmp, "no", "such", "dir", "x.db").as_posix())
        with self.assertRaises(database.StartupDatabaseError) as ctx:
            database.create_db_and_tables(bad)
        self.assertIn("could not connect", str(ctx.exception))


@unittest.skipUnless(PG_URL, "QUIZLAB_TEST_PG_URL not set")
class PostgresMigrations(_MigrationCases, unittest.TestCase):

    def setUp(self):
        self.engine = create_engine(PG_URL)
        with self.engine.begin() as c:
            c.execute(text("DROP SCHEMA public CASCADE"))
            c.execute(text("CREATE SCHEMA public"))

    def tearDown(self):
        self.engine.dispose()

    def test_blocked_migration_times_out_with_clear_error(self):
        self._old_db()
        holder = self.engine.connect()
        tx = holder.begin()
        holder.execute(text("LOCK TABLE quiz IN ACCESS EXCLUSIVE MODE"))
        try:
            t0 = time.monotonic()
            with self.assertRaises(database.StartupDatabaseError) as ctx:
                database.create_db_and_tables(self.engine)
            elapsed = time.monotonic() - t0
        finally:
            tx.rollback()
            holder.close()
        self.assertLess(elapsed, 15)
        self.assertIn("lock", str(ctx.exception).lower())
        self.assertIn("nothing was changed", str(ctx.exception))
        # All or nothing: no table was created by the failed attempt
        self.assertNotIn("livesession", inspect(self.engine).get_table_names())
        # And it succeeds once the lock is gone
        database.create_db_and_tables(self.engine)
        self.assertIn("read_time", _columns(self.engine, "quiz"))

    def test_connect_timeout(self):
        # 10.255.255.1 is unroutable: the connect would hang without a timeout
        bad = create_engine("postgresql://u:p@10.255.255.1:5432/x",
                            connect_args={"connect_timeout": 2})
        t0 = time.monotonic()
        with self.assertRaises(database.StartupDatabaseError):
            database.create_db_and_tables(bad)
        self.assertLess(time.monotonic() - t0, 10)


if __name__ == "__main__":
    unittest.main()
