"""Server-side question clock: the read phase and the deadline are enforced
on the server, and the server reveals by itself if the host goes silent.

Run from the project root:   venv\\Scripts\\python -m unittest discover tests -v
"""

import asyncio
import unittest
from unittest import mock

from _support import Game, gm, question, run, saved_quiz


class AnswerWindow(unittest.TestCase):

    def test_read_phase_answers_are_rejected(self):
        g = Game([question("mc", "ab", correct="0")], read_time=5).add("a").start()
        run(g.mgr.handle_answer(g.code, g.pid("a"), 0, 0, 0))
        self.assertNotIn(0, g.player("a").answers)
        self.assertEqual(g.ws("a").last("answer_rejected")["reason"], "read_phase")

    def test_late_answers_are_rejected(self):
        g = Game([question("mc", "ab", correct="0", time_limit=10)]).add("a").start()
        g.open_answers(elapsed=10 + gm.ANSWER_GRACE_SECONDS + 0.5)
        run(g.mgr.handle_answer(g.code, g.pid("a"), 0, 0, 0))
        self.assertNotIn(0, g.player("a").answers)
        self.assertEqual(g.ws("a").last("answer_rejected")["reason"], "time_up")

    def test_rejection_covers_every_answer_kind(self):
        qs = [question("ms", "abc", correct="[0]", time_limit=5),
              question("order", "abc", time_limit=5),
              question("wordcloud", (), points=0, time_limit=5)]
        g = Game(qs).add("a").start()
        late = 5 + gm.ANSWER_GRACE_SECONDS + 1
        g.open_answers(elapsed=late)
        run(g.mgr.handle_selection(g.code, g.pid("a"), 0, [0]))
        run(g.mgr.handle_confirm(g.code, g.pid("a"), 0))
        self.assertNotIn(0, g.player("a").selections)
        self.assertNotIn(0, g.player("a").answers)
        g.reveal(); g.next(); g.open_answers(elapsed=late)
        run(g.mgr.handle_order_update(g.code, g.pid("a"), 1, [0, 1, 2]))
        self.assertNotIn(1, g.player("a").answers)
        g.reveal(); g.next(); g.open_answers(elapsed=late)
        run(g.mgr.handle_wordcloud_answer(g.code, g.pid("a"), 2, "hola"))
        self.assertNotIn(2, g.player("a").answers)
        self.assertEqual(len(g.ws("a").of_type("answer_rejected")), 3)

    def test_grace_window_accepts_but_caps_time(self):
        g = Game([question("mc", "ab", correct="0", time_limit=10, points=100)])
        g.add("a").start()
        g.open_answers(elapsed=10.6)          # past the deadline, inside grace
        run(g.mgr.handle_answer(g.code, g.pid("a"), 0, 0, 0))
        self.assertEqual(g.player("a").answer_times[0], 10.0)
        g.reveal()
        self.assertEqual(g.player("a").question_results[0]["points"], 50)  # floor

    def test_on_time_answer_unchanged(self):
        g = Game([question("mc", "ab", correct="0", time_limit=20, points=200)])
        g.add("a").start()
        g.open_answers(elapsed=5.0)
        run(g.mgr.handle_answer(g.code, g.pid("a"), 0, 0, 0))
        self.assertIsNone(g.ws("a").last("answer_rejected"))
        g.reveal()
        self.assertEqual(g.player("a").question_results[0]["points"],
                         gm.speed_points(200, 20, g.player("a").answer_times[0]))


@mock.patch.object(gm, "AUTO_REVEAL_GRACE_SECONDS", 0.05)
class AutoReveal(unittest.TestCase):

    def test_server_reveals_when_host_is_gone(self):
        g = Game([question("mc", "ab", correct="0", time_limit=1)]).add("a")

        async def scenario():
            await g.mgr.start_game(g.code)
            self.assertIsNotNone(g.session.deadline_task)
            g.session.host_websocket = None       # host tab closed
            await g.mgr.handle_answer(g.code, g.pid("a"), 0, 0, 0)
            await asyncio.sleep(1.3)
        run(scenario())
        self.assertEqual(g.session.state, "reveal")
        self.assertTrue(g.ws("a").last("reveal")["is_correct"])

    def test_host_reveal_cancels_the_timer(self):
        g = Game([question("mc", "ab", correct="0", time_limit=1)]).add("a")

        async def scenario():
            await g.mgr.start_game(g.code)
            task = g.session.deadline_task
            await g.mgr.reveal_answer(g.code)
            await asyncio.sleep(0)
            return task
        task = run(scenario())
        self.assertTrue(task.cancelled())
        self.assertEqual(len(g.host.of_type("reveal")), 1)

    def test_restart_mid_question_resumes_the_clock(self):
        qs = [question("mc", "ab", correct="0", time_limit=1)]
        g = Game(qs, quiz_id=saved_quiz(qs)).add("a")

        async def scenario():
            await g.mgr.start_game(g.code)
            g.mgr._cancel_auto_reveal(g.session)
            fresh = gm.GameManager()                    # "new process"
            fresh.rehydrate_live()
            restored = fresh.get_session(g.code)
            self.assertIsNotNone(restored.deadline_task)
            await asyncio.sleep(1.3)
            return restored
        restored = run(scenario())
        self.assertEqual(restored.state, "reveal")


class DeadlineSettle(unittest.TestCase):
    """Real AUTO_REVEAL_GRACE_SECONDS here: the settle must finish first."""

    def test_buzzer_answer_lands_before_deadline_reveal(self):
        """The host's ring hits 0 and it asks for the reveal; a phone's
        auto-submit arriving a moment later is still scored."""
        g = Game([question("mc", "ab", correct="0", time_limit=1)]).add("a")

        async def scenario():
            await g.mgr.start_game(g.code)
            await asyncio.sleep(1.0)
            reveal = asyncio.create_task(g.mgr.reveal_answer(g.code, at_deadline=True))
            await asyncio.sleep(0.3)
            await g.mgr.handle_answer(g.code, g.pid("a"), 0, 0, 0)
            await reveal
        run(scenario())
        self.assertTrue(g.player("a").question_results[0]["answered"])
        self.assertTrue(g.player("a").question_results[0]["correct"])


if __name__ == "__main__":
    unittest.main()
