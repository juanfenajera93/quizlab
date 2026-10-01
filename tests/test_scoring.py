"""Scoring transparency tests: the formula is unchanged, the reveal breakdown
adds up, the answer ack matches what reveal awards, points are validated on
save, and static/js/scoring.js computes the same numbers as the server.

Run from the project root:   venv\\Scripts\\python -m unittest discover tests -v
"""

import asyncio
import itertools
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Same as test_csv_roundtrip: keep game_manager's engine off quizlab.db.
if "DATABASE_URL" not in os.environ:
    _TMP = tempfile.mkdtemp(prefix="quizlab_test_")
    os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP, 'unit.db').as_posix()}"

import question_spec as spec  # noqa: E402
from database import create_db_and_tables  # noqa: E402
import game_manager as gm  # noqa: E402


def _old_score_answer(q_type, correct_json, player_answer, time_taken,
                      time_limit, base_points, scoring_mode="speed"):
    """The scoring function as it was before this branch, verbatim."""
    min_pts = math.floor(base_points * 0.5)

    def speed_bonus(tt):
        if scoring_mode == "accuracy":
            return base_points
        time_remaining = max(0.0, time_limit - tt)
        pts = math.floor(base_points * (time_remaining / time_limit)) if time_limit > 0 else 0
        return max(min_pts, pts)

    if q_type in ("mc", "tf"):
        try:
            correct_idx = int(correct_json) if correct_json != "" else 0
        except (ValueError, TypeError):
            correct_idx = 0
        if player_answer == correct_idx:
            return speed_bonus(time_taken), True
        return 0, False
    elif q_type == "ms":
        try:
            correct_indices = set(json.loads(correct_json))
        except Exception:
            return 0, False
        if not isinstance(player_answer, list):
            return 0, False
        selected = set(player_answer)
        all_indices = set(range(100))
        if selected & (all_indices - correct_indices):
            return 0, False
        overlap = len(selected & correct_indices)
        if overlap == len(correct_indices):
            return speed_bonus(time_taken), True
        elif overlap > 0:
            return math.floor(base_points * (overlap / len(correct_indices))), False
        return 0, False
    elif q_type == "poll":
        if player_answer is not None and player_answer != -1:
            return base_points, True
        return 0, False
    elif q_type == "order":
        try:
            correct_order = json.loads(correct_json) if correct_json else []
        except Exception:
            return 0, False
        if not isinstance(player_answer, list) or not correct_order:
            return 0, False
        if len(player_answer) != len(correct_order):
            return 0, False
        matching = sum(1 for a, b in zip(player_answer, correct_order) if a == b)
        if matching == len(correct_order):
            return speed_bonus(time_taken), True
        return math.floor(base_points * (matching / len(correct_order))), False
    elif q_type == "wordcloud":
        if player_answer and isinstance(player_answer, str) and player_answer.strip():
            return base_points, True
        return 0, False
    return 0, False


CASES = {
    "mc": ("1", [1, 0, 2, -1, None]),
    "tf": ("0", [0, 1, -1]),
    "ms": ("[0, 2, 3]", [[0, 2, 3], [0, 2], [2], [0, 1], [], [3, 2, 0], -1]),
    "poll": ("", [0, 3, -1, None]),
    "order": ("[2, 0, 1, 3]", [[2, 0, 1, 3], [2, 0, 3, 1], [0, 1, 2, 3],
                               [2, 0, 1], -1]),
    "wordcloud": ("", ["hola", "  ", "", None]),
}
TIMES = [0.0, 0.01, 1.3, 4.999, 7.5, 9.99, 10.0, 14.2, 19.99, 20.0, 25.0, 60.0]
LIMITS = [5, 10, 20, 30, 45, 120]
BASES = [0, 1, 7, 100, 133, 200, 333, 500, 999, 1000]


class FormulaUnchanged(unittest.TestCase):

    def test_score_answer_matches_previous_implementation(self):
        for (q_type, (correct, answers)), tt, tl, base, mode in itertools.product(
                CASES.items(), TIMES, LIMITS, BASES, ("speed", "accuracy")):
            for ans in answers:
                args = (q_type, correct, ans, tt, tl, base, mode)
                self.assertEqual(gm._score_answer(*args), _old_score_answer(*args),
                                 args)

    def test_details_are_consistent(self):
        for (q_type, (correct, answers)), tt, tl, base, mode in itertools.product(
                CASES.items(), TIMES, LIMITS, BASES, ("speed", "accuracy")):
            for ans in answers:
                d = gm._score_details(q_type, correct, ans, tt, tl, base, mode)
                if d["kind"] == "speed":
                    self.assertEqual(mode, "speed")
                    self.assertEqual(d["points"], gm.speed_points(base, tl, tt))
                    self.assertGreaterEqual(d["speed_factor"], gm.SPEED_FLOOR)
                    # The shown factor explains the points (floor rounding)
                    self.assertLessEqual(
                        abs(d["points"] - base * d["speed_factor"]), 1 + base * 0.0005)
                elif d["kind"] == "partial":
                    self.assertIn(q_type, ("ms", "order"))
                    self.assertEqual(d["points"],
                                     math.floor(base * d["hits"] / d["parts"]))

    def test_streak_bonus_matches_previous_inline_formula(self):
        for base, streak in itertools.product(BASES, range(0, 10)):
            old = math.floor(base * 0.1 * min(streak - 1, 5)) if streak >= 2 else 0
            self.assertEqual(gm.streak_bonus(base, streak), old)


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)

    async def close(self):
        pass

    def of_type(self, t):
        return [m for m in self.sent if m.get("type") == t]


def _run(coro):
    return asyncio.run(coro)


class RevealBreakdownAndAck(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        create_db_and_tables()

    def _session(self, questions, scoring_mode="speed", streak=True):
        mgr = gm.GameManager()
        host = _FakeWS()
        quiz = {"id": 999999, "name": "t", "read_time": 0,
                "scoring_mode": scoring_mode, "streak_bonus": streak,
                "questions": questions}
        code = mgr.create_session(quiz, host)
        return mgr, mgr.get_session(code), code

    def _add(self, mgr, code, nick):
        ws = _FakeWS()
        pid = _run(mgr.add_player(code, nick, ws))
        return pid, ws

    def test_breakdown_adds_up_and_ack_matches_reveal(self):
        qs = [{"text": f"q{i}", "question_type": "mc", "options": ["a", "b"],
               "correct_json": "1", "time_limit": 20, "points": 200}
              for i in range(3)]
        mgr, session, code = self._session(qs)
        pid, ws = self._add(mgr, code, "ana")
        player = session.players[pid]
        _run(mgr.start_game(code))
        q_msg = ws.of_type("question")[-1]
        self.assertEqual(q_msg["points"], 200)
        self.assertEqual(q_msg["full_time_limit"], 20)
        self.assertEqual(q_msg["scoring_mode"], "speed")
        self.assertTrue(q_msg["streak_bonus"])
        self.assertLessEqual(abs(q_msg["answer_starts_in"]), 0.5)

        for qi, elapsed in enumerate([3.0, 5.5, 12.0]):
            # Pretend the answer phase started `elapsed` seconds ago
            session.answer_phase_start_time = gm.time.time() - elapsed
            _run(mgr.handle_answer(code, pid, qi, 1, 0))
            ack = ws.of_type("answer_ack")[-1]
            self.assertEqual(ack["question_id"], qi)
            _run(mgr.reveal_answer(code))
            reveal = ws.of_type("reveal")[-1]
            bd = reveal["breakdown"]
            self.assertEqual(bd["kind"], "speed")
            self.assertEqual(bd["base"], 200)
            self.assertEqual(ack["max_points"], bd["question_points"])
            self.assertEqual(ack["streak_bonus"], bd["streak_bonus"])
            self.assertEqual(bd["total"], bd["question_points"] + bd["streak_bonus"])
            self.assertEqual(reveal["points_earned"], bd["total"])
            self.assertEqual(bd["streak"], qi + 1)
            if qi < 2:
                _run(mgr.next_question(code))
        self.assertEqual(player.score, sum(
            r["points"] for r in player.question_results.values()))
        # streak 2 → +10%, streak 3 → +20%
        self.assertEqual(player.question_results[1]["breakdown"]["streak_bonus"], 20)
        self.assertEqual(player.question_results[2]["breakdown"]["streak_bonus"], 40)

    def test_partial_credit_and_unanswered(self):
        qs = [{"text": "ms", "question_type": "ms", "options": list("abcd"),
               "correct_json": "[0, 1, 2]", "time_limit": 30, "points": 300}]
        mgr, session, code = self._session(qs, scoring_mode="accuracy")
        p1, ws1 = self._add(mgr, code, "ana")
        p2, ws2 = self._add(mgr, code, "beto")
        _run(mgr.start_game(code))
        _run(mgr.handle_selection(code, p1, 0, [0, 2]))
        _run(mgr.handle_confirm(code, p1, 0))
        ack = ws1.of_type("answer_ack")[-1]
        self.assertEqual(ack["max_points"], 300)        # accuracy: fixed
        _run(mgr.reveal_answer(code))
        bd = ws1.of_type("reveal")[-1]["breakdown"]
        self.assertEqual((bd["kind"], bd["hits"], bd["parts"]), ("partial", 2, 3))
        self.assertEqual(bd["question_points"], 200)
        self.assertEqual(bd["streak_bonus"], 0)
        self.assertEqual(bd["total"], 200)
        bd2 = ws2.of_type("reveal")[-1]["breakdown"]
        self.assertEqual((bd2["kind"], bd2["total"]), ("none", 0))

    def test_no_ack_for_poll_wordcloud_or_zero_points(self):
        qs = [{"text": "p", "question_type": "poll", "options": ["a", "b"],
               "correct_json": "", "time_limit": 20, "points": 100},
              {"text": "z", "question_type": "mc", "options": ["a", "b"],
               "correct_json": "0", "time_limit": 20, "points": 0}]
        mgr, session, code = self._session(qs)
        pid, ws = self._add(mgr, code, "ana")
        _run(mgr.start_game(code))
        _run(mgr.handle_answer(code, pid, 0, 1, 0))
        _run(mgr.reveal_answer(code))
        _run(mgr.next_question(code))
        _run(mgr.handle_answer(code, pid, 1, 0, 0))
        self.assertEqual(ws.of_type("answer_ack"), [])

    def test_rejoin_carries_server_ack(self):
        qs = [{"text": "o", "question_type": "order", "options": list("abc"),
               "correct_json": "[0, 1, 2]", "time_limit": 20, "points": 100}]
        mgr, session, code = self._session(qs)
        pid, ws = self._add(mgr, code, "ana")
        _run(mgr.start_game(code))
        _run(mgr.handle_order_update(code, pid, 0, [0, 1, 2]))
        result = _run(mgr.rejoin_player(code, pid, "ana", _FakeWS()))
        self.assertTrue(result["already_answered"])
        self.assertEqual(result["answer_ack"]["max_points"],
                         ws.of_type("answer_ack")[-1]["max_points"])


class PointsValidation(unittest.TestCase):

    def test_validate_points(self):
        import main
        lo, hi = spec.POINTS_MIN, spec.POINTS_MAX
        for ok, expected in [(lo, lo), (hi, hi), (250, 250), ("300", 300),
                             (500.0, 500)]:
            self.assertEqual(main._validate_points(ok), (expected, None), ok)
        for bad in [hi + 1, lo - 1, 2.5, "abc", "", None, True, [100]]:
            pts, err = main._validate_points(bad)
            self.assertIsNone(pts, bad)
            self.assertIn(str(hi), err)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class JsParity(unittest.TestCase):
    """static/js/scoring.js must produce the server's numbers."""

    def test_speed_points_and_streak_bonus_match(self):
        grid = [(b, tl, tt, m) for b, tl, tt, m in itertools.product(
            BASES, LIMITS, TIMES + [0.123, 3.333, 17.77], ("speed", "accuracy"))]
        streaks = list(itertools.product(BASES, range(0, 10)))
        script = (
            "global.window = global; global.performance = {now: () => 0};"
            f"require({json.dumps(str(ROOT / 'static/js/scoring.js'))});"
            "const d = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
            "console.log(JSON.stringify({"
            "  speed: d.g.map(a => QLScore.speedPoints(a[0], a[1], a[2], a[3])),"
            "  streak: d.s.map(a => QLScore.streakBonus(a[0], a[1]))}));"
        )
        out = json.loads(subprocess.run(
            ["node", "-e", script], input=json.dumps({"g": grid, "s": streaks}),
            capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out["speed"], [gm.speed_points(*a) for a in grid])
        self.assertEqual(out["streak"], [gm.streak_bonus(*a) for a in streaks])


if __name__ == "__main__":
    unittest.main()
