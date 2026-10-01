"""Golden snapshot of every question type's server-side behavior.

One deterministic game (fake clock, seeded shuffles) that plays every
question type with a correct, a partial/wrong and a silent player, and
records everything the server says: each question, ack, reveal (players
and host), the end-of-game review, the analytics stats, the session-detail
distribution and homework (Tarea) grading for the same questions.

tests/golden/question_types.json was written by the code *before* the
question-type registry refactor (Phase E); this test proves the refactor
changed nothing. Regenerate deliberately with QUIZLAB_WRITE_GOLDEN=1 only
when a behavior change is intended (new types append new sections).

Run from the project root:   venv\\Scripts\\python -m unittest discover tests -v
"""

import asyncio
import json
import os
import random
import unittest
from pathlib import Path
from unittest import mock

from _support import Game, gm, question, saved_quiz

GOLDEN = Path(__file__).resolve().parent / "golden" / "question_types.json"

# (question, {nick: [(handler, args...)]}) — handlers are GameManager methods
ROUNDS = [
    (question("mc", ["Media", "Mediana", "Moda", "Rango"], correct="1", points=200),
     {"ana": [("answer", 1)], "beto": [("answer", 2)]}),
    (question("tf", ["Verdadero", "Falso"], correct="1", time_limit=15),
     {"ana": [("answer", 1)], "beto": [("answer", 0)]}),
    (question("ms", ["Histograma", "Caja", "Pastel", "Densidad"], correct="[0, 1, 3]",
              points=300, time_limit=30),
     {"ana": [("select", [0, 1, 3]), ("confirm",)],
      "beto": [("select", [0, 3])]}),                       # never confirmed
    (question("poll", ["Excel", "Python", "R"], points=50),
     {"ana": [("answer", 1)], "beto": [("answer", 1)]}),
    (question("order", ["uno", "dos", "tres", "cuatro"], correct="[0, 1, 2, 3]",
              points=400, time_limit=45),
     {"ana": [("order", "right")], "beto": [("order", "swap")]}),
    (question("wordcloud", [], points=0, time_limit=30),
     {"ana": [("word", "Duplicados")], "beto": [("word", "  duplicados ")]}),
    (question("mc", ["a", "b"], correct="0", points=0),     # unscored mc
     {"ana": [("answer", 0)]}),
]

HOMEWORK_ANSWERS = {
    "ana": [1, 1, [0, 1, 3], 1, [0, 1, 2, 3], "Duplicados", 0],
    "beto": [2, 0, [0, 3], 1, [1, 0, 2, 3], "", None],
}


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def _normalize(obj, names):
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    for raw, nice in names.items():
        text = text.replace(raw, nice)
    return json.loads(text)


async def _play(g, clock):
    for qi, (q, plan) in enumerate(ROUNDS):
        if qi:
            await g.mgr.next_question(g.code)
        else:
            await g.mgr.start_game(g.code)
        for step, (nick, actions) in enumerate(sorted(plan.items())):
            clock.now += 2.5 + step
            pid = g.pid(nick)
            for action, *args in actions:
                if action == "answer":
                    await g.mgr.handle_answer(g.code, pid, qi, args[0], 0)
                elif action == "select":
                    await g.mgr.handle_selection(g.code, pid, qi, args[0])
                elif action == "confirm":
                    await g.mgr.handle_confirm(g.code, pid, qi)
                elif action == "order":
                    shuffled = g.ws(nick).last("question")["options"]
                    right = [shuffled.index(o) for o in q["options"]]
                    if args[0] == "swap":
                        right[0], right[1] = right[1], right[0]
                    await g.mgr.handle_order_update(g.code, pid, qi, right)
                elif action == "word":
                    await g.mgr.handle_wordcloud_answer(g.code, pid, qi, args[0])
        clock.now += 1
        await g.mgr.reveal_answer(g.code)
    await g.mgr.end_game(g.code)


def _session_detail_stats(g):
    import main
    from database import engine
    from models import QuizSession
    from sqlmodel import Session, select
    main.game_manager.sessions[g.code] = g.session
    try:
        asyncio.run(main._persist_session(g.code))
    finally:
        main.game_manager.sessions.pop(g.code, None)
    with Session(engine) as db:
        qs = db.exec(select(QuizSession).where(QuizSession.room_code == g.code)
                     .order_by(QuizSession.id.desc())).first()
        req = mock.Mock(session={"admin": True})
        resp = asyncio.run(main.session_detail(req, qs.id, db))
    return resp.context["stats"]


def _homework():
    import main
    from database import engine
    from models import Assignment
    from sqlmodel import Session
    quiz_id = saved_quiz([q for q, _ in ROUNDS])
    code = f"GOLD{quiz_id:04d}"[-8:]
    with Session(engine) as db:
        db.add(Assignment(quiz_id=quiz_id, code=code))
        db.commit()
    out = {}
    for nick, answers in HOMEWORK_ANSWERS.items():
        req = mock.Mock()

        async def body(answers=answers, nick=nick):
            return {"nickname": nick, "answers": answers}
        req.json = body
        with Session(engine) as db:
            resp = asyncio.run(main.api_assignment_submit(code, req, db))
        out[nick] = json.loads(resp.body)
        questions = json.loads(asyncio.run(
            main.api_assignment_questions(code, Session(engine))).body)
    out["questions_api"] = questions
    return out


def capture() -> dict:
    clock = Clock()
    random.seed(20261001)
    with mock.patch("time.time", clock):
        g = Game([q for q, _ in ROUNDS], scoring_mode="speed", streak=True)
        g.add("ana", "beto", "caro")
        asyncio.run(_play(g, clock))
        names = {g.pid(n): f"<{n}>" for n in g.players}
        names[g.code] = "<ROOM>"
        result = {
            "host": g.host.sent,
            "players": {n: g.ws(n).sent for n in g.players},
            "analytics": g.session.analytics_data,
            "session_detail": _session_detail_stats(g),
        }
    result["homework"] = _homework()
    return _normalize(result, names)


class GoldenQuestionTypes(unittest.TestCase):
    maxDiff = None

    def test_matches_golden(self):
        got = capture()
        if os.environ.get("QUIZLAB_WRITE_GOLDEN") == "1" or not GOLDEN.exists():
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(json.dumps(got, ensure_ascii=False, indent=1,
                                         sort_keys=True), encoding="utf-8")
            self.skipTest(f"golden written to {GOLDEN}")
        want = json.loads(GOLDEN.read_text(encoding="utf-8"))
        # Fields added to the session-detail rows after the golden was taken
        # (view, scored) are allowed; everything the golden recorded must
        # still match exactly.
        got["session_detail"] = [{k: g[k] for k in w if k in g}
                                 for g, w in zip(got["session_detail"],
                                                 want["session_detail"])]
        for section in want:
            self.assertEqual(got.get(section), want[section], section)


if __name__ == "__main__":
    unittest.main()
