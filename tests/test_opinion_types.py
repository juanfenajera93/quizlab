"""Phase G opinion types: open-ended, brainstorm, scale. No right answer,
0 points, no live counter; live games, homework, review, host reveal and
the CSV spec.

Run from the project root:   venv\\Scripts\\python -m unittest discover tests -v
"""

import asyncio
import json
import unittest
from unittest import mock

from _support import FakeWS, Game, gm, question, run, saved_quiz

import qtypes
import question_spec as spec

OPEN = question("open", (), points=0, time_limit=60)
BRAIN = question("brainstorm", (), correct=json.dumps({"ideas": 2}), points=0, time_limit=90)
SCALE = question("scale", ("Nada", "Algo", "Mucho"), correct=json.dumps({"max": 5}),
                 points=0, time_limit=20)


class Grouping(unittest.TestCase):

    def test_groups_by_shared_words_ignoring_case_accents_stopwords(self):
        ideas = ["Sensores en semáforos", "sensor de tráfico en los semáforos",
                 "Datos de Google Maps", "google maps", "Cámaras de tránsito",
                 "las cámaras", "Waze"]
        groups = qtypes.group_ideas(ideas)
        named = {g["name"]: sorted(g["ideas"]) for g in groups}
        self.assertEqual(named, {"Sensores": [0, 1], "Google": [2, 3],
                                 "Cámaras": [4, 5], "Otras ideas": [6]})
        self.assertEqual(groups[-1]["name"], "Otras ideas")

    def test_prefix_stem_matches_word_families(self):
        groups = qtypes.group_ideas(["más práctica", "practicar más", "Prácticas guiadas"])
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(groups[0]["ideas"]), [0, 1, 2])

    def test_stopword_only_ideas_group_only_when_identical(self):
        groups = qtypes.group_ideas(["no sé", "No  SÉ", "y tú?"])
        self.assertIn([0, 1], [sorted(g["ideas"]) for g in groups])

    def test_no_shared_words_means_one_pool(self):
        self.assertEqual(qtypes.group_ideas(["uno", "dos", "tres"]),
                         [{"name": "Ideas", "ideas": [0, 1, 2]}])
        self.assertEqual(qtypes.group_ideas([]), [])

    def test_deterministic(self):
        ideas = ["datos abiertos", "datos del gobierno", "apps", "apps de mapas"]
        self.assertEqual(qtypes.group_ideas(ideas), qtypes.group_ideas(list(ideas)))


class NoPointsNoCounter(unittest.TestCase):

    def test_meta_and_spec(self):
        for code in ("open", "brainstorm", "scale"):
            m = qtypes.kind_meta()[code]
            self.assertFalse(m["scored"], code)
            self.assertTrue(m["never_scores"], code)
            self.assertEqual(m["fixed_points"], 0, code)

    def test_game_gives_zero_and_leaves_streaks_alone(self):
        qs = [question("mc", "ab", correct="0"), dict(OPEN, points=500), SCALE]
        g = Game(qs, scoring_mode="accuracy", streak=True).add("a").start()
        g.open_answers()
        run(g.mgr.handle_answer(g.code, g.pid("a"), 0, 0, 0))
        g.reveal(); g.next(); g.open_answers()
        run(g.mgr.handle_submit(g.code, g.pid("a"), 1, "Una opinión"))
        self.assertEqual([a["question_id"] for a in g.ws("a").of_type("answer_ack")], [0])
        r = g.reveal()
        mine = g.ws("a").last("reveal")
        self.assertEqual(mine["points_earned"], 0)
        self.assertTrue(mine["no_points"])
        self.assertEqual(g.player("a").streak, 1)        # untouched
        self.assertEqual(g.player("a").score, 100)
        self.assertEqual(r["cards"], ["Una opinión"])


class OpenEnded(unittest.TestCase):

    def test_card_wall_is_anonymous_and_capped(self):
        g = Game([OPEN]).add("ana", "beto", "caro").start()
        g.open_answers()
        run(g.mgr.handle_submit(g.code, g.pid("ana"), 0, "  Me  gustó   el ejemplo  "))
        run(g.mgr.handle_submit(g.code, g.pid("beto"), 0, "x" * 400))
        run(g.mgr.handle_submit(g.code, g.pid("ana"), 0, "cambio"))       # locked
        host = g.reveal()
        self.assertEqual(host["cards"], ["Me gustó el ejemplo", "x" * 250])
        self.assertNotIn("ana", json.dumps(host["cards"]))
        self.assertEqual(host["distribution"]["answered"], 2)
        self.assertEqual(g.ws("ana").last("reveal")["your_text"], "✓")
        self.assertEqual(g.ws("caro").last("reveal")["your_text"], "")
        run(g.mgr.end_game(g.code))
        review = g.ws("ana").last("game_end")["review"][0]
        self.assertEqual((review["your_answer"], review["scored"]),
                         ("Me gustó el ejemplo", False))


class Brainstorm(unittest.TestCase):

    def test_several_ideas_until_the_limit(self):
        g = Game([BRAIN]).add("a", "b").start()
        self.assertEqual(g.ws("a").last("question")["max_ideas"], 2)
        g.open_answers()
        run(g.mgr.handle_submit(g.code, g.pid("a"), 0, "datos abiertos"))
        self.assertNotIn(0, g.player("a").confirmed)
        # rejoin mid-question: still open, previous ideas come back
        rj = run(g.mgr.rejoin_player(g.code, g.pid("a"), "a", FakeWS()))
        self.assertFalse(rj["already_answered"])
        self.assertEqual(rj["question"]["previous"], ["datos abiertos"])
        run(g.mgr.handle_submit(g.code, g.pid("a"), 0, "datos del gobierno"))
        run(g.mgr.handle_submit(g.code, g.pid("a"), 0, "una tercera"))     # over the limit
        self.assertEqual(g.player("a").answers[0], ["datos abiertos", "datos del gobierno"])
        self.assertIn(0, g.player("a").confirmed)
        rj = run(g.mgr.rejoin_player(g.code, g.pid("a"), "a", FakeWS()))
        self.assertTrue(rj["already_answered"])
        run(g.mgr.handle_submit(g.code, g.pid("b"), 0, "apps de mapas"))
        host = g.reveal()
        self.assertEqual(host["ideas"], ["datos abiertos", "datos del gobierno", "apps de mapas"])
        self.assertEqual(host["auto_groups"][0], {"name": "Datos", "ideas": [0, 1]})
        self.assertIsNone(host["saved_view"])

    def test_manual_groups_are_saved_validated_and_survive_rejoin_and_restart(self):
        qs = [BRAIN]
        g = Game(qs, quiz_id=saved_quiz(qs)).add("a", "b").start()
        g.open_answers()
        run(g.mgr.handle_submit(g.code, g.pid("a"), 0, "uno"))
        run(g.mgr.handle_submit(g.code, g.pid("b"), 0, "dos"))
        # not revealed yet: ignored
        run(g.mgr.set_host_view(g.code, 0, {"mode": "manual", "groups": []}))
        self.assertEqual(g.session.host_views, {})
        g.reveal()
        view = {"mode": "manual", "groups": [{"name": "Números", "ideas": [0, 1, "x", -3]},
                                             {"name": "", "ideas": []}]}
        run(g.mgr.set_host_view(g.code, 0, view))
        stored = {"mode": "manual", "groups": [{"name": "Números", "ideas": [0, 1]},
                                               {"name": "", "ideas": []}]}
        self.assertEqual(g.session.host_views[0], stored)
        run(g.mgr.set_host_view(g.code, 0, {"mode": "weird"}))           # rejected
        self.assertEqual(g.session.host_views[0], stored)
        self.assertEqual(run(g.mgr.rejoin_host(g.code, FakeWS()))["reveal"]["saved_view"], stored)
        fresh = gm.GameManager()
        fresh.rehydrate_live()
        self.assertEqual(fresh.get_session(g.code).host_views[0], stored)
        run(g.mgr.end_game(g.code))
        stats = g.session.analytics_data["question_stats"][0]
        self.assertEqual(json.loads(stats["answers_json"]), {"Números": 2})


class Scale(unittest.TestCase):

    def test_distribution_and_average(self):
        g = Game([SCALE]).add("a", "b", "c", "d").start()
        q = g.ws("a").last("question")
        self.assertEqual((q["scale_max"], q["options"]), (5, ["Nada", "Algo", "Mucho"]))
        g.open_answers()
        for nick, v in (("a", 5), ("b", 4), ("c", 4), ("d", 9)):
            run(g.mgr.handle_submit(g.code, g.pid(nick), 0, v))
        run(g.mgr.handle_submit(g.code, g.pid("d"), 0, 2.5))
        run(g.mgr.handle_submit(g.code, g.pid("d"), 0, True))
        self.assertNotIn(0, g.player("d").answers)
        d = g.reveal()["distribution"]
        self.assertEqual(d["scale_counts"], [0, 0, 0, 2, 1])
        self.assertEqual((d["average"], d["max"], d["answered"]), (4.33, 5, 3))
        run(g.mgr.end_game(g.code))
        self.assertEqual(g.ws("a").last("game_end")["review"][0]["your_answer"], "5 / 5")

    def test_one_to_ten(self):
        q10 = dict(SCALE, correct_json=json.dumps({"max": 10}))
        k = qtypes.kind_of(q10)
        self.assertEqual(k.normalize_submit(q10, 10, None), (10, True))
        self.assertIsNone(k.normalize_submit(SCALE, 10, None))


class HomeworkAndSpec(unittest.TestCase):

    def test_homework(self):
        import main
        from database import engine
        from models import Assignment
        from sqlmodel import Session
        quiz_id = saved_quiz([OPEN, BRAIN, SCALE])
        code = f"G{quiz_id:07d}"
        with Session(engine) as db:
            db.add(Assignment(quiz_id=quiz_id, code=code))
            db.commit()
        req = mock.Mock()

        async def body():
            return {"nickname": "ana", "answers": ["Mi opinión", ["idea 1", "", "idea 2", "idea 3"], 3]}
        req.json = body
        with Session(engine) as db:
            out = json.loads(asyncio.run(main.api_assignment_submit(code, req, db)).body)
        self.assertEqual((out["score"], out["correct_count"]), (0, 0))
        self.assertEqual([r["your_answer"] for r in out["review"]],
                         ["Mi opinión", "idea 1 · idea 2", "3 / 5"])
        self.assertFalse(any(r["scored"] for r in out["review"]))
        with Session(engine) as db:
            qs = json.loads(asyncio.run(main.api_assignment_questions(code, db)).body)["questions"]
        self.assertEqual((qs[1]["max_ideas"], qs[2]["scale_max"]), (2, 5))

    def test_settings_cells(self):
        bs = spec.QUESTION_TYPES["brainstorm"]
        self.assertEqual(spec.parse_settings_cell(bs, ""), {"ideas": 3})
        self.assertEqual(spec.parse_settings_cell(bs, " ideas = 5 "), {"ideas": 5})
        sc = spec.QUESTION_TYPES["scale"]
        self.assertEqual(spec.parse_settings_cell(sc, "max=10"), {"max": 10})
        for cell in ("ideas=0", "ideas=11", "max=7", "foo=1", "5"):
            with self.assertRaises(ValueError, msg=cell):
                spec.parse_settings_cell(bs if "ideas" in cell else sc, cell)
        self.assertEqual(spec.encode_settings_cell({"max": 10}), "max=10")

    def test_scale_needs_three_labels(self):
        header = ";".join(spec.COLUMN_NAMES)
        r = spec.parse_csv((header + "\nQ;scale;Bajo;Alto;;;;;max=10;20;0;\n"
                            "Q2;scale;;;;;;;;20;0;").encode())
        self.assertEqual(len(r.errors), 1)
        self.assertEqual(r.questions[0]["options"], list(spec.QUESTION_TYPES["scale"].default_options))


if __name__ == "__main__":
    unittest.main()
