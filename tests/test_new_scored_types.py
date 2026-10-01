"""Phase F scored types: short answer and pin on image, in live games
(generic "submit" message), homework, the review, the host reveal and the
CSV spec.

Run from the project root:   venv\\Scripts\\python -m unittest discover tests -v
"""

import asyncio
import json
import math
import unittest
from unittest import mock

from _support import Game, gm, question, run, saved_quiz

import qtypes
import question_spec as spec
from scoring import Outcome, speed_points

SHORT = question("short", (), correct=json.dumps(["Tamaño", "el tamaño de muestra"]),
                 points=200, time_limit=20)
# Image twice as tall as wide; one zone in the middle, radius 10% of width
PIN_CFG = {"zones": [{"x": 0.5, "y": 0.5, "r": 0.1}], "falloff": 1.0, "aspect": 2.0}
PIN = question("pin", (), correct=json.dumps(PIN_CFG), points=100, time_limit=20,
               image_url="https://example.org/img.png")


def score(q, answer, elapsed=0.0, mode="speed"):
    kind = qtypes.kind_of(q)
    return kind.score(kind.homework_key(q), answer,
                      Outcome(elapsed, q["time_limit"], q["points"], mode))


class ShortAnswerMatching(unittest.TestCase):

    def test_case_accents_and_spaces_do_not_matter(self):
        for typed in ("Tamaño", "tamano", " TAMAÑO ", "TaMaNo", "el  tamaño  de muestra",
                      "EL TAMANO DE MUESTRA"):
            self.assertTrue(score(SHORT, typed)["correct"], typed)
        for typed in ("tamaños", "taman", "", "   ", None, 3, ["Tamaño"]):
            self.assertFalse(score(SHORT, typed)["correct"], typed)

    def test_speed_formula(self):
        d = score(SHORT, "tamano", elapsed=5.0)
        self.assertEqual(d["kind"], "speed")
        self.assertEqual(d["points"], speed_points(200, 20, 5.0))
        self.assertEqual(score(SHORT, "tamano", elapsed=5.0, mode="accuracy")["points"], 200)

    def test_normalize_text(self):
        self.assertEqual(qtypes.normalize_text("  Ñandú   CORRE "), "nandu corre")


class ShortAnswerGame(unittest.TestCase):

    def test_submit_reveal_and_review(self):
        g = Game([SHORT]).add("a", "b", "c", "d").start()
        g.open_answers(4.0)
        run(g.mgr.handle_submit(g.code, g.pid("a"), 0, "  TAMAÑO "))
        run(g.mgr.handle_submit(g.code, g.pid("b"), 0, "tamano"))
        run(g.mgr.handle_submit(g.code, g.pid("c"), 0, "la media es lo que importa"))
        # locked after the first submission
        run(g.mgr.handle_submit(g.code, g.pid("a"), 0, "otra cosa"))
        self.assertEqual(g.player("a").answers[0], "TAMAÑO")
        self.assertEqual(g.player("c").answers[0], "la media es lo que i")   # 20 chars
        self.assertIsNotNone(g.ws("a").last("answer_ack"))
        host = g.reveal()
        self.assertEqual(host["accepted_answers"], ["Tamaño", "el tamaño de muestra"])
        d = host["distribution"]
        self.assertEqual((d["answered"], d["players"], d["correct_count"]), (3, 4, 2))
        self.assertEqual(d["top_answers"][0], {"text": "TAMAÑO", "count": 2, "accepted": True}
                         if d["top_answers"][0]["text"] == "TAMAÑO" else
                         {"text": "tamano", "count": 2, "accepted": True})
        self.assertEqual(d["top_answers"][1]["accepted"], False)
        mine = g.ws("a").last("reveal")
        self.assertTrue(mine["is_correct"])
        self.assertEqual(mine["your_answer"], "TAMAÑO")
        self.assertEqual(mine["breakdown"]["kind"], "speed")
        self.assertNotIn("accepted_answers", mine)
        self.assertEqual(g.player("a").streak, 1)
        run(g.mgr.end_game(g.code))
        review = g.ws("c").last("game_end")["review"][0]
        self.assertEqual(review["your_answer"], "la media es lo que i")
        self.assertEqual(review["correct_answer"], "Tamaño / el tamaño de muestra")
        self.assertFalse(review["correct"])
        self.assertTrue(review["scored"])
        silent = g.ws("d").last("game_end")["review"][0]
        self.assertEqual(silent["your_answer"], "—")

    def test_options_never_reach_the_phones(self):
        g = Game([SHORT]).add("a").start()
        q = g.ws("a").last("question")
        self.assertEqual(q["options"], [])
        self.assertNotIn("correct_json", q)

    def test_old_messages_do_not_answer_new_types(self):
        g = Game([SHORT]).add("a").start()
        g.open_answers()
        run(g.mgr.handle_answer(g.code, g.pid("a"), 0, 0, 0))
        run(g.mgr.handle_wordcloud_answer(g.code, g.pid("a"), 0, "tamaño"))
        self.assertNotIn(0, g.player("a").answers)

    def test_late_submit_is_rejected(self):
        g = Game([SHORT]).add("a").start()
        g.open_answers(20 + gm.ANSWER_GRACE_SECONDS + 1)
        run(g.mgr.handle_submit(g.code, g.pid("a"), 0, "tamaño"))
        self.assertNotIn(0, g.player("a").answers)
        self.assertEqual(g.ws("a").last("answer_rejected")["reason"], "time_up")


class PinScoring(unittest.TestCase):

    def test_inside_any_zone_is_full_speed_points(self):
        for p in ({"x": 0.5, "y": 0.5}, {"x": 0.59, "y": 0.5}, {"x": 0.5, "y": 0.549}):
            d = score(PIN, p, elapsed=10.0)
            self.assertEqual((d["kind"], d["correct"]), ("speed", True), p)
            self.assertEqual(d["points"], speed_points(100, 20, 10.0))

    def test_aspect_keeps_zones_circular(self):
        # 0.06 of the height = 0.12 of the width on a 2:1 tall image: outside
        self.assertFalse(score(PIN, {"x": 0.5, "y": 0.56})["correct"])

    def test_outside_falls_linearly_to_zero_at_falloff(self):
        # Edge at x = 0.6; falloff = one radius (0.1): 0 at x = 0.7
        for x, frac in ((0.625, 0.75), (0.65, 0.5), (0.675, 0.25), (0.7, 0.0), (0.9, 0.0)):
            d = score(PIN, {"x": x, "y": 0.5}, mode="accuracy")
            self.assertEqual(d["kind"], "near")
            self.assertFalse(d["correct"])
            self.assertAlmostEqual(d["proximity"], frac, places=3)
            self.assertEqual(d["points"], math.floor(100 * frac + 1e-9))
            self.assertAlmostEqual(d["distance"], (x - 0.6) / 0.1, places=2)

    def test_near_uses_the_speed_formula_and_shows_it(self):
        d = score(PIN, {"x": 0.65, "y": 0.5}, elapsed=10.0)
        self.assertEqual(d["full_points"], speed_points(100, 20, 10.0))
        self.assertEqual(d["points"], math.floor(d["full_points"] * 0.5))
        self.assertIn("speed_factor", d)

    def test_falloff_is_configurable(self):
        q = dict(PIN, correct_json=json.dumps(dict(PIN_CFG, falloff=2.0)))
        self.assertAlmostEqual(score(q, {"x": 0.7, "y": 0.5}, mode="accuracy")["proximity"], 0.5)
        q0 = dict(PIN, correct_json=json.dumps(dict(PIN_CFG, falloff=0)))
        self.assertEqual(score(q0, {"x": 0.61, "y": 0.5})["points"], 0)

    def test_nearest_zone_edge_counts(self):
        cfg = {"zones": [{"x": 0.2, "y": 0.2, "r": 0.05}, {"x": 0.8, "y": 0.2, "r": 0.1}],
               "falloff": 1.0, "aspect": 1.0}
        q = dict(PIN, correct_json=json.dumps(cfg))
        d = score(q, {"x": 0.65, "y": 0.2}, mode="accuracy")   # 0.05 from the big one
        self.assertAlmostEqual(d["distance"], 0.5)
        self.assertTrue(score(q, {"x": 0.21, "y": 0.2})["correct"])

    def test_bad_pins_score_nothing(self):
        for bad in (None, -1, "x", {"x": 2, "y": 0.5}, {"x": "a", "y": 1}, [0.5, 0.5]):
            self.assertEqual(score(PIN, bad)["points"], 0, bad)


class PinGame(unittest.TestCase):

    def test_reveal_shows_zones_and_every_pin(self):
        g = Game([PIN], streak=True).add("a", "b", "c").start()
        g.open_answers(2.0)
        run(g.mgr.handle_submit(g.code, g.pid("a"), 0, {"x": 0.5, "y": 0.5}))
        run(g.mgr.handle_submit(g.code, g.pid("b"), 0, {"x": 0.65, "y": 0.5}))
        run(g.mgr.handle_submit(g.code, g.pid("c"), 0, {"x": 5, "y": 0.5}))   # invalid
        self.assertNotIn(0, g.player("c").answers)
        host = g.reveal()
        self.assertEqual(host["zones"], PIN_CFG["zones"])
        self.assertEqual(host["aspect"], 2.0)
        self.assertEqual(host["distribution"]["pins"],
                         [{"x": 0.5, "y": 0.5, "inside": True},
                          {"x": 0.65, "y": 0.5, "inside": False}])
        self.assertEqual(host["distribution"]["inside"], 1)
        bd = g.ws("b").last("reveal")["breakdown"]
        self.assertEqual(bd["kind"], "near")
        self.assertEqual(bd["total"], bd["question_points"])
        self.assertAlmostEqual(bd["proximity"], 0.5)
        self.assertIn("full_points", bd)
        self.assertEqual(g.player("a").streak, 1)
        self.assertEqual(g.player("b").streak, 0)
        run(g.mgr.end_game(g.code))
        self.assertEqual(g.ws("a").last("game_end")["review"][0]["your_answer"],
                         "📍 dentro de la zona")
        self.assertEqual(g.ws("b").last("game_end")["review"][0]["your_answer"],
                         "📍 fuera, a 0,5 radios de la zona")


class Homework(unittest.TestCase):

    def test_graded_like_the_live_game(self):
        import main
        from database import engine
        from models import Assignment
        from sqlmodel import Session
        quiz_id = saved_quiz([SHORT, PIN])
        with Session(engine) as db:
            db.add(Assignment(quiz_id=quiz_id, code=f"F{quiz_id:07d}"))
            db.commit()
        req = mock.Mock()

        async def body():
            return {"nickname": "ana", "answers": [" TAMAÑO  ", {"x": 0.65, "y": 0.5}]}
        req.json = body
        with Session(engine) as db:
            out = json.loads(asyncio.run(main.api_assignment_submit(f"F{quiz_id:07d}", req, db)).body)
        self.assertEqual(out["score"], 200 + 50)            # accuracy mode
        self.assertEqual(out["correct_count"], 1)
        self.assertEqual(out["review"][0]["your_answer"], "TAMAÑO")
        self.assertEqual(out["review"][1]["your_answer"], "📍 fuera, a 0,5 radios de la zona")
        with Session(engine) as db:
            qs = json.loads(asyncio.run(main.api_assignment_questions(f"F{quiz_id:07d}", db)).body)
        self.assertEqual([q["options"] for q in qs["questions"]], [[], []])
        self.assertNotIn("correct_json", qs["questions"][0])


class SpecAndValidation(unittest.TestCase):

    def test_csv_cells(self):
        self.assertEqual(spec.parse_answers_cell(" Mediana |  | la mediana "),
                         ["Mediana", "la mediana"])
        cfg = spec.parse_zones_cell("50 17,5 5 | 20 30 2.5 | falloff=1.5 | aspect=0.75")
        self.assertEqual(cfg["zones"][1], {"x": 0.2, "y": 0.3, "r": 0.025})
        self.assertEqual((cfg["falloff"], cfg["aspect"]), (1.5, 0.75))
        self.assertEqual(spec.parse_zones_cell(spec.encode_zones_cell(cfg)), cfg)
        for bad in ("", "50 50", "150 50 5", "50 50 0", "50 50 5 | falloff=-1", "50 50 5 | size=3"):
            with self.assertRaises(ValueError, msg=bad):
                spec.parse_zones_cell(bad)

    def test_import_errors(self):
        header = ";".join(spec.COLUMN_NAMES)
        rows = [
            "Sin respuestas;short;;;;;;;;30;100;",
            "Muy larga;short;;;;;;;una respuesta demasiado larga;30;100;",
            "Sin imagen;pin;;;;;;;50 50 5;30;100;",
            "Bien;short;;;;;;;Mediana | la mediana;30;100;",
        ]
        r = spec.parse_csv(("\n".join([header] + rows)).encode())
        self.assertEqual(len(r.questions), 1)
        self.assertEqual([e.row for e in r.errors], [2, 3, 4])
        self.assertIn("20 characters", r.errors[1].message)
        self.assertIn("image", r.errors[2].message)

    def test_save_validation(self):
        k = qtypes.get_kind("pin")
        self.assertEqual(k.validate_question(PIN), [])
        self.assertTrue(k.validate_question(dict(PIN, image_url="")))
        self.assertTrue(k.validate_question(dict(PIN, correct_json=json.dumps(
            {"zones": [], "falloff": 1, "aspect": 1}))))
        self.assertTrue(k.validate_question(dict(PIN, correct_json=json.dumps(
            dict(PIN_CFG, aspect=None)))))
        s = qtypes.get_kind("short")
        self.assertEqual(s.validate_question(SHORT), [])
        self.assertTrue(s.validate_question(dict(SHORT, correct_json="[]")))


if __name__ == "__main__":
    unittest.main()
