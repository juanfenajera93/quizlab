"""Timezones: aware UTC everywhere, deadlines typed and shown in Ecuador
time, legacy naive rows read as UTC, and an idempotent timestamptz
migration on Postgres (runs when QUIZLAB_TEST_PG_URL is set, see
test_startup.py).

Run from the project root:   venv\\Scripts\\python -m unittest discover tests -v
"""

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import _support  # noqa: F401
import database
import timeutil
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, select

UTC = timezone.utc
PG_URL = os.environ.get("QUIZLAB_TEST_PG_URL")


class TimeUtil(unittest.TestCase):

    def test_deadline_typed_in_ecuador_is_stored_as_utc(self):
        self.assertEqual(timeutil.parse_local("2026-10-01T23:59"),
                         datetime(2026, 10, 2, 4, 59, tzinfo=UTC))
        self.assertEqual(timeutil.parse_local("2026-10-01T23:59:00+00:00"),
                         datetime(2026, 10, 1, 23, 59, tzinfo=UTC))
        self.assertIsNone(timeutil.parse_local("  "))
        with self.assertRaises(ValueError):
            timeutil.parse_local("mañana")

    def test_display_in_ecuador(self):
        stored = datetime(2026, 10, 2, 4, 59, tzinfo=UTC)
        self.assertEqual(timeutil.format_local(stored, "%Y-%m-%d %H:%M"), "2026-10-01 23:59")
        naive_legacy = datetime(2026, 10, 2, 4, 59)          # naive = UTC
        self.assertEqual(timeutil.format_local(naive_legacy, "%H:%M"), "23:59")
        self.assertEqual(timeutil.format_local(None), "")
        self.assertIn("America/Guayaquil", timeutil.timezone_label())
        self.assertIn("UTC-05:00", timeutil.timezone_label())

    def test_utc_now_is_aware(self):
        self.assertEqual(timeutil.utc_now().utcoffset().total_seconds(), 0)

    def test_no_naive_now_left_in_the_app(self):
        root = Path(__file__).resolve().parent.parent
        for name in ("main.py", "game_manager.py", "models.py", "database.py"):
            src = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("utcnow(", src, name)
            self.assertNotIn("datetime.now()", src, name)


class AssignmentDeadline(unittest.TestCase):

    def _assignment(self, deadline_typed):
        from models import Assignment, Quiz
        with Session(database.engine) as db:
            quiz = Quiz(name="hw")
            db.add(quiz)
            db.commit()
            db.refresh(quiz)
            a = Assignment(quiz_id=quiz.id, code=f"TZ{quiz.id:06d}",
                           deadline=timeutil.parse_local(deadline_typed))
            db.add(a)
            db.commit()
            return a.code

    def _info(self, code, now):
        import main
        with mock.patch.object(main, "utc_now", return_value=now), \
                Session(database.engine) as db:
            resp = asyncio.run(main.api_assignment_info(code, db))
        return json.loads(resp.body)

    def test_2359_closes_at_2359_ecuador_not_1859(self):
        code = self._assignment("2026-10-01T23:59")
        # 19:00 in Ecuador (00:00 UTC Oct 2): the old code had closed it at 18:59
        info = self._info(code, datetime(2026, 10, 2, 0, 0, tzinfo=UTC))
        self.assertFalse(info["closed"])
        self.assertEqual(info["deadline_display"], "01/10/2026 23:59")
        self.assertEqual(info["deadline"], "2026-10-02T04:59:00+00:00")
        # 23:58:59 Ecuador: open; 23:59 Ecuador: closed
        self.assertFalse(self._info(code, datetime(2026, 10, 2, 4, 58, 59, tzinfo=UTC))["closed"])
        self.assertTrue(self._info(code, datetime(2026, 10, 2, 4, 59, tzinfo=UTC))["closed"])

    def test_values_come_back_aware_utc(self):
        from models import Assignment
        code = self._assignment("2026-12-24T08:00")
        with Session(database.engine) as db:
            a = db.exec(select(Assignment).where(Assignment.code == code)).one()
        self.assertEqual(a.deadline, datetime(2026, 12, 24, 13, 0, tzinfo=UTC))
        self.assertEqual(a.created_at.tzinfo, UTC)


class LegacyNaiveRowsSqlite(unittest.TestCase):

    def test_naive_rows_read_as_utc(self):
        from models import Assignment, Quiz
        eng = create_engine("sqlite:///" + Path(tempfile.mkdtemp(), "l.db").as_posix())
        database.create_db_and_tables(eng)
        with eng.begin() as c:   # what the old code wrote: naive UTC text
            c.execute(text("INSERT INTO quiz (id, name, read_time, scoring_mode, "
                           "streak_bonus, created_at) VALUES "
                           "(1, 'q', 5, 'speed', 0, '2026-09-01 12:00:00.000000')"))
            c.execute(text("INSERT INTO assignment (id, quiz_id, code, deadline, created_at) "
                           "VALUES (1, 1, 'OLD', '2026-10-02 04:59:00.000000', "
                           "'2026-09-01 12:00:00.000000')"))
        with Session(eng) as db:
            a = db.get(Assignment, 1)
            q = db.get(Quiz, 1)
        self.assertEqual(a.deadline, datetime(2026, 10, 2, 4, 59, tzinfo=UTC))
        self.assertEqual(q.created_at, datetime(2026, 9, 1, 12, 0, tzinfo=UTC))
        eng.dispose()


@unittest.skipUnless(PG_URL, "QUIZLAB_TEST_PG_URL not set")
class PostgresTimestamptz(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine(PG_URL)
        with self.engine.begin() as c:
            c.execute(text("DROP SCHEMA public CASCADE"))
            c.execute(text("CREATE SCHEMA public"))

    def tearDown(self):
        self.engine.dispose()

    def _types(self):
        return {(r[0], r[1]): r[2] for r in self.engine.connect().execute(text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND data_type LIKE 'timestamp%'"))}

    def test_naive_columns_become_timestamptz_keeping_the_instant(self):
        from models import Assignment
        # A database as the previous version left it: naive timestamps
        with self.engine.begin() as c:
            c.execute(text("SET TIME ZONE 'America/Guayaquil'"))   # must not matter
            c.execute(text("CREATE TABLE quiz (id SERIAL PRIMARY KEY, name VARCHAR NOT NULL, "
                           "course_tag VARCHAR, read_time INTEGER, scoring_mode VARCHAR, "
                           "streak_bonus BOOLEAN, created_at TIMESTAMP, last_played TIMESTAMP)"))
            c.execute(text("CREATE TABLE classgroup (id SERIAL PRIMARY KEY, name VARCHAR NOT NULL, "
                           "created_at TIMESTAMP)"))
            c.execute(text("CREATE TABLE assignment (id SERIAL PRIMARY KEY, quiz_id INTEGER "
                           "REFERENCES quiz(id), class_id INTEGER REFERENCES classgroup(id), "
                           "code VARCHAR NOT NULL, deadline TIMESTAMP, created_at TIMESTAMP)"))
            c.execute(text("INSERT INTO quiz (id, name, created_at) VALUES (1, 'q', '2026-09-01 12:00')"))
            c.execute(text("INSERT INTO assignment (quiz_id, code, deadline, created_at) "
                           "VALUES (1, 'OLD', '2026-10-02 04:59', '2026-09-01 12:00')"))
        with self.assertLogs("quizlab.startup", "INFO") as logs:
            database.create_db_and_tables(self.engine)
        self.assertIn('ALTER TABLE "assignment" ALTER COLUMN "deadline" TYPE TIMESTAMP '
                      'WITH TIME ZONE', "\n".join(logs.output))
        types = self._types()
        self.assertTrue(types, "no timestamp columns found")
        self.assertEqual(set(types.values()), {"timestamp with time zone"}, types)
        with Session(self.engine) as db:
            a = db.exec(select(Assignment).where(Assignment.code == "OLD")).one()
        self.assertEqual(a.deadline, datetime(2026, 10, 2, 4, 59, tzinfo=UTC))
        # Idempotent: a second startup changes nothing
        with self.assertLogs("quizlab.startup", "INFO") as logs:
            database.create_db_and_tables(self.engine)
        self.assertNotIn("ALTER", "\n".join(logs.output))
        with Session(self.engine) as db:
            a = db.exec(select(Assignment).where(Assignment.code == "OLD")).one()
        self.assertEqual(a.deadline, datetime(2026, 10, 2, 4, 59, tzinfo=UTC))

    def test_fresh_database_is_timestamptz(self):
        database.create_db_and_tables(self.engine)
        self.assertEqual(set(self._types().values()), {"timestamp with time zone"})


if __name__ == "__main__":
    unittest.main()
