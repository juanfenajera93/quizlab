import json
import math
import random
import string
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from fastapi import WebSocket


class Player:
    def __init__(self, player_id: str, nickname: str, websocket: WebSocket):
        self.player_id = player_id
        self.nickname = nickname
        self.websocket = websocket
        self.score = 0
        self.answers: Dict[int, Any] = {}        # final locked answers
        self.selections: Dict[int, Any] = {}     # live selections (ms/order)
        self.confirmed: set = set()              # locked question ids
        self.answer_times: Dict[int, float] = {}
        self.connected = True


class GameSession:
    def __init__(self, room_code: str, quiz_data: dict, host_websocket: WebSocket):
        self.room_code = room_code
        self.quiz_data = quiz_data
        self.host_websocket = host_websocket
        self.players: Dict[str, Player] = {}
        self.current_question_index = -1
        self.question_start_time: Optional[float] = None
        self.answer_phase_start_time: Optional[float] = None
        self.state = "lobby"
        self.answer_counts: List[int] = []
        self.last_activity = datetime.utcnow()
        self.created_at = datetime.utcnow()
        # For "order" type: store the correct ordering to expect from players
        self.order_correct: Dict[int, list] = {}
        # Analytics
        self.analytics_data: dict = {}
        self.analytics_persisted: bool = False
        # Word cloud answers: {question_index: {player_id: text}}
        self.wordcloud_answers: Dict[int, Dict[str, str]] = {}

    @property
    def questions(self) -> List[dict]:
        return self.quiz_data.get("questions", [])

    @property
    def current_question(self) -> Optional[dict]:
        idx = self.current_question_index
        qs = self.questions
        if 0 <= idx < len(qs):
            return qs[idx]
        return None

    @property
    def read_time(self) -> int:
        return self.quiz_data.get("read_time", 5)

    def get_leaderboard(self) -> List[dict]:
        ranked = sorted(
            [
                {
                    "nickname": p.nickname,
                    "score": p.score,
                    "player_id": p.player_id,
                }
                for p in self.players.values()
            ],
            key=lambda x: x["score"],
            reverse=True,
        )
        for i, entry in enumerate(ranked):
            entry["rank"] = i + 1
        return ranked

    def connected_player_list(self) -> List[dict]:
        return [
            {"nickname": p.nickname, "player_id": p.player_id}
            for p in self.players.values()
            if p.connected
        ]


def _score_answer(q_type, correct_json, player_answer, time_taken, time_limit, base_points):
    """Returns (points_earned, is_correct)"""
    min_pts = math.floor(base_points * 0.5)

    def speed_bonus(tt):
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
        # Any wrong selection → zero
        all_indices = set(range(100))
        if selected & (all_indices - correct_indices):
            return 0, False
        overlap = len(selected & correct_indices)
        if overlap == len(correct_indices):
            return speed_bonus(time_taken), True
        elif overlap > 0:
            pts = math.floor(base_points * (overlap / len(correct_indices)))
            return pts, False
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
        pts = math.floor(base_points * (matching / len(correct_order)))
        return pts, False

    elif q_type == "wordcloud":
        if player_answer and isinstance(player_answer, str) and player_answer.strip():
            return base_points, True
        return 0, False

    return 0, False


class GameManager:
    def __init__(self):
        self.sessions: Dict[str, GameSession] = {}

    def generate_room_code(self) -> str:
        chars = string.ascii_uppercase + string.digits
        while True:
            code = "".join(random.choices(chars, k=6))
            if code not in self.sessions:
                return code

    def create_session(self, quiz_data: dict, host_websocket: WebSocket) -> str:
        code = self.generate_room_code()
        self.sessions[code] = GameSession(code, quiz_data, host_websocket)
        return code

    def get_session(self, room_code: str) -> Optional[GameSession]:
        return self.sessions.get(room_code.upper())

    async def add_player(
        self, room_code: str, nickname: str, websocket: WebSocket
    ) -> Optional[str]:
        session = self.get_session(room_code)
        if not session or session.state == "ended":
            return None

        player_id = str(uuid.uuid4())
        session.players[player_id] = Player(player_id, nickname, websocket)
        session.last_activity = datetime.utcnow()

        player_list = session.connected_player_list()
        await self._broadcast_players(session, {
            "type": "player_update",
            "player_count": len(player_list),
            "player_list": player_list,
        })
        await self._send_host(session, {
            "type": "player_update",
            "count": len(player_list),
            "players": player_list,
        })
        return player_id

    async def start_game(self, room_code: str):
        session = self.get_session(room_code)
        if not session:
            return
        session.current_question_index = 0
        session.state = "question"
        await self._broadcast_players(session, {"type": "game_start"})
        await self._send_question(session)

    async def _send_question(self, session: GameSession):
        q = session.current_question
        if not q:
            return

        options = q.get("options", [])
        num_options = len(options)
        session.answer_counts = [0] * max(num_options, 1)

        now = time.time()
        session.question_start_time = now
        session.answer_phase_start_time = now + session.read_time
        session.state = "question"

        qi = session.current_question_index
        q_type = q.get("question_type", "mc")

        # For "order" type: shuffle the options, store correct ordering
        send_options = options[:]
        if q_type == "order" and len(options) > 1:
            shuffled_indices = list(range(len(options)))
            random.shuffle(shuffled_indices)
            send_options = [options[i] for i in shuffled_indices]
            # correct_ordering: the sequence of shuffled-array indices that gives original order
            # i.e., where should shuffled[i] go? → argsort of shuffled_indices
            argsort = [0] * len(shuffled_indices)
            for orig_pos, shuf_pos in enumerate(shuffled_indices):
                argsort[shuf_pos] = orig_pos
            # Player submits: send_options in their chosen order as indices into send_options
            # Correct submission = the permutation that restores original order
            # Simplest: correct_ordering = the sorted permutation
            # Player sends [idx0, idx1, ...] meaning "item at idx0 is position 0, etc."
            # We store: what the player should submit to get full score
            # Since send_options[i] = options[shuffled_indices[i]],
            # the correct order of send_options is argsort of shuffled_indices
            session.order_correct[qi] = argsort
        elif q_type == "order":
            session.order_correct[qi] = list(range(len(options)))

        msg = {
            "type": "question",
            "id": qi,
            "text": q["text"],
            "image_url": q.get("image_url") or "",
            "options": send_options,
            "time_limit": q.get("time_limit", 20),
            "read_time": session.read_time,
            "number": qi + 1,
            "total": len(session.questions),
            "question_type": q_type,
        }
        await self._broadcast_players(session, msg)
        await self._send_host(session, msg)

    async def handle_answer(
        self,
        room_code: str,
        player_id: str,
        question_id: int,
        answer_index: int,
        client_timestamp: float,
    ):
        """Handle mc/tf/poll answer (single index, auto-lock)."""
        session = self.get_session(room_code)
        if not session or session.state != "question":
            return
        player = session.players.get(player_id)
        if not player or question_id in player.answers:
            return
        if session.current_question_index != question_id:
            return

        q = session.current_question
        q_type = q.get("question_type", "mc") if q else "mc"

        # For ms and order: use separate handlers
        if q_type in ("ms", "order"):
            return

        player.answers[question_id] = answer_index
        player.confirmed.add(question_id)

        if session.answer_phase_start_time:
            elapsed = time.time() - session.answer_phase_start_time
            player.answer_times[question_id] = max(0.0, elapsed)
        else:
            player.answer_times[question_id] = 0.0

        if 0 <= answer_index < len(session.answer_counts):
            session.answer_counts[answer_index] += 1

        await self._send_host(session, {
            "type": "answer_counts",
            "counts": session.answer_counts,
        })

    async def handle_selection(
        self,
        room_code: str,
        player_id: str,
        question_id: int,
        selections: list,
    ):
        """Handle ms live selection update (not locked yet)."""
        session = self.get_session(room_code)
        if not session or session.state != "question":
            return
        player = session.players.get(player_id)
        if not player or question_id in player.confirmed:
            return
        if session.current_question_index != question_id:
            return

        player.selections[question_id] = selections

        # Rebuild answer_counts from all players' current selections
        num_opts = len(session.answer_counts)
        counts = [0] * num_opts
        for p in session.players.values():
            sel = p.selections.get(question_id) or p.answers.get(question_id)
            if isinstance(sel, list):
                for idx in sel:
                    if 0 <= idx < num_opts:
                        counts[idx] += 1
        session.answer_counts = counts

        await self._send_host(session, {
            "type": "answer_counts",
            "counts": session.answer_counts,
        })

    async def handle_confirm(
        self,
        room_code: str,
        player_id: str,
        question_id: int,
    ):
        """Lock ms answer."""
        session = self.get_session(room_code)
        if not session or session.state != "question":
            return
        player = session.players.get(player_id)
        if not player or question_id in player.confirmed:
            return
        if session.current_question_index != question_id:
            return

        sel = player.selections.get(question_id, [])
        player.answers[question_id] = sel
        player.confirmed.add(question_id)

        if session.answer_phase_start_time:
            elapsed = time.time() - session.answer_phase_start_time
            player.answer_times[question_id] = max(0.0, elapsed)
        else:
            player.answer_times[question_id] = 0.0

    async def handle_order_update(
        self,
        room_code: str,
        player_id: str,
        question_id: int,
        ordering: list,
    ):
        """Handle order type answer update (auto-locked at reveal)."""
        session = self.get_session(room_code)
        if not session or session.state != "question":
            return
        player = session.players.get(player_id)
        if not player:
            return
        if session.current_question_index != question_id:
            return

        player.selections[question_id] = ordering
        # Auto-update answers (will be used at reveal)
        player.answers[question_id] = ordering

        if session.answer_phase_start_time:
            elapsed = time.time() - session.answer_phase_start_time
            player.answer_times[question_id] = max(0.0, elapsed)
        else:
            player.answer_times[question_id] = 0.0

        # Count how many players have submitted an ordering
        num_opts = len(session.answer_counts)
        counts = [0] * max(num_opts, 1)
        submitted = sum(1 for p in session.players.values() if question_id in p.answers)
        # For order: just show how many submitted in first bar
        if counts:
            counts[0] = submitted
        session.answer_counts = counts

        await self._send_host(session, {
            "type": "answer_counts",
            "counts": session.answer_counts,
        })

    async def reveal_answer(self, room_code: str):
        session = self.get_session(room_code)
        if not session:
            return
        q = session.current_question
        if not q:
            return

        session.state = "reveal"
        q_type = q.get("question_type", "mc")
        correct_json = q.get("correct_json", "")
        time_limit = q.get("time_limit", 20)
        base_points = q.get("points", 100)

        qi = session.current_question_index

        # For order type: use the shuffled-correct ordering stored in session
        if q_type == "order":
            correct_ordering = session.order_correct.get(qi, [])
            scoring_correct_json = json.dumps(correct_ordering)
        else:
            scoring_correct_json = correct_json

        # Score all players
        for player in session.players.values():
            answer = player.answers.get(qi)
            if answer is None:
                # For order: use selections as final answer
                answer = player.selections.get(qi)
            if answer is None:
                continue
            time_taken = player.answer_times.get(qi, time_limit)
            pts, _ = _score_answer(q_type, scoring_correct_json, answer, time_taken, time_limit, base_points)
            player.score += pts

        leaderboard = session.get_leaderboard()

        # Determine correct_index for host display (mc/tf only)
        correct_index = -1
        if q_type in ("mc", "tf"):
            try:
                correct_index = int(correct_json) if correct_json != "" else 0
            except (ValueError, TypeError):
                correct_index = 0

        for player in session.players.values():
            if not player.connected:
                continue
            answer = player.answers.get(qi)
            if answer is None:
                answer = player.selections.get(qi)

            time_taken = player.answer_times.get(qi, time_limit)
            pts_earned, is_correct = _score_answer(
                q_type, scoring_correct_json, answer if answer is not None else -1,
                time_taken, time_limit, base_points
            )

            rank = next(
                (e["rank"] for e in leaderboard if e["player_id"] == player.player_id),
                len(leaderboard),
            )

            reveal_msg = {
                "type": "reveal",
                "question_type": q_type,
                "correct_json": scoring_correct_json,
                "correct_index": correct_index,
                "your_answer": answer if (answer is not None and not isinstance(answer, str)) else -1,
                "is_correct": is_correct,
                "points_earned": pts_earned,
                "total_score": player.score,
                "rank": rank,
                "total_players": len([p for p in session.players.values() if p.connected]),
                "leaderboard": leaderboard[:5],
                "no_points": base_points == 0 or q_type == "wordcloud",
            }
            if q_type == "wordcloud":
                reveal_msg["your_text"] = player.answers.get(qi, "")

            try:
                await player.websocket.send_json(reveal_msg)
            except Exception:
                player.connected = False

        host_reveal: dict = {
            "type": "reveal",
            "question_type": q_type,
            "correct_index": correct_index,
            "correct_json": scoring_correct_json,
            "leaderboard": leaderboard[:5],
        }
        if q_type == "wordcloud":
            freq_dict: Dict[str, int] = {}
            for txt in session.wordcloud_answers.get(qi, {}).values():
                key = txt.lower().strip()
                if key:
                    freq_dict[key] = freq_dict.get(key, 0) + 1
            host_reveal["words"] = freq_dict
        await self._send_host(session, host_reveal)

    async def next_question(self, room_code: str):
        session = self.get_session(room_code)
        if not session:
            return
        session.current_question_index += 1
        if session.current_question_index >= len(session.questions):
            await self.end_game(room_code)
        else:
            await self._send_question(session)

    async def end_game(self, room_code: str):
        session = self.get_session(room_code)
        if not session:
            return
        session.analytics_data = self.compile_analytics(session)
        session.state = "ended"
        leaderboard = session.get_leaderboard()
        msg = {"type": "game_end", "leaderboard": leaderboard}
        await self._broadcast_players(session, msg)
        await self._send_host(session, msg)

    async def remove_player(self, room_code: str, player_id: str):
        session = self.get_session(room_code)
        if not session:
            return
        player = session.players.get(player_id)
        if player:
            player.connected = False
        player_list = session.connected_player_list()
        await self._send_host(session, {
            "type": "player_update",
            "count": len(player_list),
            "players": player_list,
        })

    async def _broadcast_players(self, session: GameSession, message: dict):
        for player in session.players.values():
            if player.connected:
                try:
                    await player.websocket.send_json(message)
                except Exception:
                    player.connected = False

    async def _send_host(self, session: GameSession, message: dict):
        if session.host_websocket:
            try:
                await session.host_websocket.send_json(message)
            except Exception:
                pass

    async def rejoin_player(self, room_code: str, player_id: str, nickname: str,
                            websocket: WebSocket) -> dict:
        session = self.get_session(room_code)
        if not session or session.state == "ended":
            return {"ok": False, "reason": "room_not_found"}
        player = session.players.get(player_id)
        if not player:
            player = next(
                (p for p in session.players.values()
                 if p.nickname.lower() == nickname.lower()),
                None,
            )
        if not player:
            return {"ok": False, "reason": "player_not_found"}
        player.websocket = websocket
        player.connected = True
        session.last_activity = datetime.utcnow()
        return {
            "ok": True,
            "player_id": player.player_id,
            "state": session.state,
            "score": player.score,
            "question_index": session.current_question_index,
        }

    async def end_session(self, room_code: str):
        session = self.get_session(room_code)
        if not session:
            return
        session.state = "ended"
        await self._broadcast_players(session, {
            "type": "game_ended",
            "message": "El quiz ha sido detenido por el profesor.",
        })

    async def ping_all_players(self):
        for session in list(self.sessions.values()):
            if session.state == "ended":
                continue
            for player in session.players.values():
                if player.connected:
                    try:
                        await player.websocket.send_json({"type": "ping"})
                    except Exception:
                        player.connected = False
            if session.host_websocket:
                try:
                    await session.host_websocket.send_json({"type": "ping"})
                except Exception:
                    pass

    def compile_analytics(self, session: GameSession) -> dict:
        question_stats = []
        for qi, q in enumerate(session.questions):
            q_type = q.get("question_type", "mc")
            base_points = q.get("points", 100)
            correct_json = q.get("correct_json", "")
            time_limit = q.get("time_limit", 20)

            if q_type == "order":
                scoring_correct_json = json.dumps(session.order_correct.get(qi, []))
            else:
                scoring_correct_json = correct_json

            answered_players = [
                p for p in session.players.values()
                if qi in p.answers or qi in p.selections
            ]
            total_answers = len(answered_players)

            correct_count = 0
            time_sum = 0.0
            time_count = 0
            for p in answered_players:
                ans = p.answers.get(qi)
                if ans is None:
                    ans = p.selections.get(qi)
                tt = p.answer_times.get(qi, time_limit)
                _, is_correct = _score_answer(q_type, scoring_correct_json, ans, tt, time_limit, base_points)
                if is_correct:
                    correct_count += 1
                if qi in p.answer_times:
                    time_sum += p.answer_times[qi]
                    time_count += 1

            avg_time = round(time_sum / time_count, 2) if time_count > 0 else 0.0

            if q_type in ("mc", "tf", "poll"):
                opts = q.get("options", [])
                counts = [0] * max(len(opts), 1)
                for p in session.players.values():
                    ans = p.answers.get(qi)
                    if isinstance(ans, int) and 0 <= ans < len(counts):
                        counts[ans] += 1
                answers_json = json.dumps(counts)
            elif q_type == "ms":
                opts = q.get("options", [])
                counts = [0] * max(len(opts), 1)
                for p in session.players.values():
                    ans = p.answers.get(qi)
                    if isinstance(ans, list):
                        for idx in ans:
                            if isinstance(idx, int) and 0 <= idx < len(counts):
                                counts[idx] += 1
                answers_json = json.dumps(counts)
            elif q_type == "order":
                submitted = sum(1 for p in session.players.values() if qi in p.answers)
                answers_json = json.dumps([submitted])
            elif q_type == "wordcloud":
                freq: Dict[str, int] = {}
                for word in session.wordcloud_answers.get(qi, {}).values():
                    key = word.lower().strip()
                    if key:
                        freq[key] = freq.get(key, 0) + 1
                answers_json = json.dumps(freq)
            else:
                answers_json = "[]"

            question_stats.append({
                "question_index": qi,
                "question_text": q.get("text", ""),
                "question_type": q_type,
                "correct_count": correct_count,
                "total_answers": total_answers,
                "avg_time_seconds": avg_time,
                "answers_json": answers_json,
            })

        return {
            "leaderboard": session.get_leaderboard(),
            "student_count": len(session.players),
            "room_code": session.room_code,
            "question_stats": question_stats,
        }

    async def handle_wordcloud_answer(
        self,
        room_code: str,
        player_id: str,
        question_id: int,
        text: str,
    ):
        session = self.get_session(room_code)
        if not session or session.state != "question":
            return
        if session.current_question_index != question_id:
            return
        player = session.players.get(player_id)
        if not player or question_id in player.confirmed:
            return

        text = text.strip()[:50]
        if not text:
            return

        session.wordcloud_answers.setdefault(question_id, {})[player_id] = text
        player.answers[question_id] = text
        player.confirmed.add(question_id)

        if session.answer_phase_start_time:
            elapsed = time.time() - session.answer_phase_start_time
            player.answer_times[question_id] = max(0.0, elapsed)
        else:
            player.answer_times[question_id] = 0.0

        word_list = list(session.wordcloud_answers[question_id].values())
        await self._send_host(session, {
            "type": "wordcloud_update",
            "question_id": question_id,
            "words": word_list,
        })

    async def broadcast_reaction(self, room_code: str, player_id: str, emoji: str):
        session = self.get_session(room_code)
        if not session:
            return
        player = session.players.get(player_id)
        if not player:
            return
        await self._send_host(session, {
            "type": "reaction",
            "emoji": emoji,
            "nickname": player.nickname,
        })

    def cleanup_old_sessions(self):
        cutoff = datetime.utcnow() - timedelta(hours=2)
        to_remove = [
            code
            for code, s in self.sessions.items()
            if s.last_activity < cutoff
        ]
        for code in to_remove:
            del self.sessions[code]


game_manager = GameManager()
