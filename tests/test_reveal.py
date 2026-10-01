"""Host reveal payload: correct answers per type and the per-option
distribution, computed from the answers that were actually scored.

Run from the project root:   venv\\Scripts\\python -m unittest discover tests -v
"""

import unittest

from _support import Game, question, run


class HostRevealDistribution(unittest.TestCase):

    def test_mc_counts_and_answered(self):
        g = Game([question("mc", "abcd", correct="1")]).add("a", "b", "c", "d", "e").start()
        g.open_answers()
        for nick, idx in [("a", 1), ("b", 0), ("c", 1), ("d", 3)]:
            run(g.mgr.handle_answer(g.code, g.pid(nick), 0, idx, 0))
        r = g.reveal()
        self.assertEqual(r["correct_index"], 1)
        self.assertEqual(r["distribution"], {"answered": 4, "players": 5,
                                             "counts": [1, 2, 0, 1]})

    def test_ms_reveals_every_correct_option(self):
        g = Game([question("ms", "abcd", correct="[0, 2]")]).add("a", "b", "c").start()
        g.open_answers()
        for nick, sel in [("a", [0, 2]), ("b", [0, 1])]:
            run(g.mgr.handle_selection(g.code, g.pid(nick), 0, sel))
            run(g.mgr.handle_confirm(g.code, g.pid(nick), 0))
        # c selected but never confirmed: still scored, so still counted
        run(g.mgr.handle_selection(g.code, g.pid("c"), 0, [2]))
        r = g.reveal()
        self.assertEqual(r["correct_indices"], [0, 2])
        self.assertEqual(r["distribution"]["counts"], [2, 1, 2, 0])
        self.assertEqual(r["distribution"]["answered"], 3)
        self.assertEqual(g.player("c").question_results[0]["points"], 50)

    def test_order_full_and_in_place(self):
        g = Game([question("order", ["first", "second", "third"])])
        g.add("a", "b", "c").start()
        q = g.ws("a").last("question")
        shuffled = q["options"]
        right = [shuffled.index(o) for o in ["first", "second", "third"]]
        swapped = [right[1], right[0], right[2]]
        g.open_answers()
        run(g.mgr.handle_order_update(g.code, g.pid("a"), 0, right))
        run(g.mgr.handle_order_update(g.code, g.pid("b"), 0, swapped))
        r = g.reveal()
        self.assertEqual(r["correct_options"], ["first", "second", "third"])
        d = r["distribution"]
        self.assertEqual((d["answered"], d["players"], d["full_correct"]), (2, 3, 1))
        self.assertEqual(d["in_place"], [1, 1, 2])
        self.assertNotIn("counts", d)

    def test_poll_has_counts_but_nothing_correct(self):
        g = Game([question("poll", "abc", points=0)]).add("a", "b").start()
        g.open_answers()
        run(g.mgr.handle_answer(g.code, g.pid("a"), 0, 2, 0))
        run(g.mgr.handle_answer(g.code, g.pid("b"), 0, 2, 0))
        r = g.reveal()
        self.assertEqual(r["correct_index"], -1)
        self.assertEqual(r["distribution"]["counts"], [0, 0, 2])

    def test_host_rejoin_during_reveal_repaints_same_numbers(self):
        g = Game([question("mc", "ab", correct="0")]).add("a", "b").start()
        g.open_answers()
        run(g.mgr.handle_answer(g.code, g.pid("a"), 0, 0, 0))
        first = g.reveal()
        rejoin = run(g.mgr.rejoin_host(g.code, g.host))
        self.assertEqual(rejoin["state"], "reveal")
        self.assertEqual(rejoin["reveal"]["distribution"], first["distribution"])
        self.assertEqual(rejoin["reveal"]["leaderboard"], first["leaderboard"])

    def test_player_reveal_unchanged(self):
        """The phones' personal reveal must not grow host-only fields."""
        g = Game([question("ms", "abc", correct="[0]")]).add("a").start()
        g.open_answers()
        run(g.mgr.handle_selection(g.code, g.pid("a"), 0, [0]))
        run(g.mgr.handle_confirm(g.code, g.pid("a"), 0))
        g.reveal()
        mine = g.ws("a").last("reveal")
        for host_only in ("distribution", "correct_indices", "correct_options"):
            self.assertNotIn(host_only, mine)


if __name__ == "__main__":
    unittest.main()
