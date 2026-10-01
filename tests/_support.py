"""Shared helpers for tests that drive GameManager without real sockets.

Importing this module points DATABASE_URL at a throwaway SQLite file (unless
one is already set) so game_manager's live-state persistence never touches
quizlab.db, and creates the tables.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "DATABASE_URL" not in os.environ:
    _TMP = tempfile.mkdtemp(prefix="quizlab_test_")
    os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP, 'unit.db').as_posix()}"

from database import create_db_and_tables  # noqa: E402
import game_manager as gm  # noqa: E402

create_db_and_tables()


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)

    async def close(self):
        pass

    def of_type(self, t):
        return [m for m in self.sent if m.get("type") == t]

    def last(self, t):
        msgs = self.of_type(t)
        return msgs[-1] if msgs else None


def run(coro):
    return asyncio.run(coro)


def question(q_type, options=(), correct="", time_limit=20, points=100, **extra):
    return {"text": f"{q_type} question", "question_type": q_type,
            "options": list(options), "correct_json": correct,
            "time_limit": time_limit, "points": points, "image_url": "", **extra}


def saved_quiz(questions, read_time=0, scoring_mode="speed"):
    """Insert a Quiz + Questions (needed when a test rehydrates from the DB)
    and return the quiz id."""
    import json
    from sqlmodel import Session
    from models import Question, Quiz
    with Session(gm.engine) as db:
        quiz = Quiz(name="t", read_time=read_time, scoring_mode=scoring_mode)
        db.add(quiz)
        db.commit()
        db.refresh(quiz)
        for i, q in enumerate(questions):
            db.add(Question(quiz_id=quiz.id, position=i, text=q["text"],
                            question_type=q["question_type"],
                            options_json=json.dumps(q["options"]),
                            correct_json=q["correct_json"],
                            time_limit=q["time_limit"], points=q["points"]))
        db.commit()
        return quiz.id


class Game:
    """A GameManager room with a fake host socket and named fake players."""

    def __init__(self, questions, scoring_mode="speed", streak=False, read_time=0,
                 quiz_id=999999):
        self.mgr = gm.GameManager()
        self.host = FakeWS()
        quiz = {"id": quiz_id, "name": "t", "read_time": read_time,
                "scoring_mode": scoring_mode, "streak_bonus": streak,
                "questions": questions}
        self.code = self.mgr.create_session(quiz, self.host)
        self.session = self.mgr.get_session(self.code)
        self.players = {}     # nick -> (player_id, FakeWS)

    def add(self, *nicks):
        for nick in nicks:
            ws = FakeWS()
            pid = run(self.mgr.add_player(self.code, nick, ws))
            self.players[nick] = (pid, ws)
        return self

    def pid(self, nick):
        return self.players[nick][0]

    def ws(self, nick):
        return self.players[nick][1]

    def player(self, nick):
        return self.session.players[self.pid(nick)]

    def start(self):
        run(self.mgr.start_game(self.code))
        return self

    def open_answers(self, elapsed=1.0):
        """Pretend the answer phase started `elapsed` seconds ago."""
        self.session.answer_phase_start_time = gm.time.time() - elapsed

    def reveal(self):
        run(self.mgr.reveal_answer(self.code))
        return self.host.last("reveal")

    def next(self):
        run(self.mgr.next_question(self.code))
