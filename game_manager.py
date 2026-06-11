import json
import logging
import math
import random
import string
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from fastapi import WebSocket

logger = logging.getLogger("quizlab.game")

# Nickname profanity filter (es/en). Matched as substrings after normalizing
# accents and common leetspeak, so "P3nd3jo" is caught too.
_BANNED_SUBSTRINGS = (
    "pendej", "puta", "puto", "verga", "mierda", "chinga", "culero", "culo",
    "pinche", "cabron", "mamon", "joto", "marica", "zorra", "perra", "polla",
    "cojone", "gilipolla", "coño", "cono",
    "fuck", "shit", "bitch", "cunt", "dick", "cock", "pussy", "whore",
    "slut", "nigg", "fag", "retard", "asshole", "penis", "vagina",
)

_LEET_MAP = str.maketrans("013457@$", "oieastas")


def _normalize_for_filter(text: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.translate(_LEET_MAP)
    return "".join(c for c in text if c.isalnum())


def is_nickname_allowed(nickname: str) -> bool:
    norm = _normalize_for_filter(nickname)
    return not any(bad in norm for bad in _BANNED_SUBSTRINGS)


TEAM_NAMES = ["Equipo Lima", "Equipo Fuego", "Equipo Violeta", "Equipo Azul"]


class Player:
    def __init__(self, player_id: str, nickname: str, websocket: WebSocket,
                 room_code: str = ""):
        self.player_id = player_id
        self.nickname = nickname
        self.websocket = websocket
        self.room_code = room_code
        self.score = 0
        self.answers: Dict[int, Any] = {}        # final locked answers
        self.selections: Dict[int, Any] = {}     # live selections (ms/order)
        self.confirmed: set = set()              # locked question ids
        self.answer_times: Dict[int, float] = {}
        self.connected = True
        self.streak = 0                          # consecutive fully-correct answers
        self.team: Optional[int] = None          # team index when team mode is on
        self.student_id: Optional[int] = None    # roster identity when class attached
        # Scoring outcome per question, written once at reveal so rejoins and
        # the end-of-game review never re-derive (or re-randomize) points:
        # {qi: {"points", "correct", "answered", "streak_after"}}
        self.question_results: Dict[int, dict] = {}


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
        # Question indices already revealed (guards against double-scoring)
        self.revealed_questions: set = set()
        # Room hygiene: locked rooms reject new joins
        self.locked = False
        # Team mode: number of teams (None/0 = individual play)
        self.team_count: Optional[int] = None
        # Class roster: [{"student_id", "name"}] — when set, players must pick
        # their name from this list and get a stable student identity
        self.roster: Optional[List[dict]] = None
        self.class_id: Optional[int] = None
        self.class_name: Optional[str] = None

    @property
    def scoring_mode(self) -> str:
        return self.quiz_data.get("scoring_mode", "speed")

    @property
    def streak_bonus_enabled(self) -> bool:
        return bool(self.quiz_data.get("streak_bonus", False))

    def get_team_leaderboard(self) -> List[dict]:
        if not self.team_count:
            return []
        totals = [0] * self.team_count
        members = [0] * self.team_count
        for p in self.players.values():
            if p.team is not None and 0 <= p.team < self.team_count:
                totals[p.team] += p.score
                members[p.team] += 1
        teams = [
            {"team": i, "name": TEAM_NAMES[i % len(TEAM_NAMES)],
             "score": totals[i], "members": members[i]}
            for i in range(self.team_count)
        ]
        teams.sort(key=lambda t: t["score"], reverse=True)
        for i, t in enumerate(teams):
            t["rank"] = i + 1
        return teams

    def available_roster_names(self) -> List[str]:
        """Roster names not yet claimed by a connected player."""
        if not self.roster:
            return []
        claimed = {p.nickname.lower() for p in self.players.values()}
        return [r["name"] for r in self.roster if r["name"].lower() not in claimed]

    def touch(self):
        """Mark the session active so the cleanup loop doesn't collect it."""
        self.last_activity = datetime.utcnow()

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
                    "team": p.team,
                    "student_id": p.student_id,
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
            {"nickname": p.nickname, "player_id": p.player_id, "team": p.team}
            for p in self.players.values()
            if p.connected
        ]


def _score_answer(q_type, correct_json, player_answer, time_taken, time_limit,
                  base_points, scoring_mode="speed"):
    """Returns (points_earned, is_correct)"""
    min_pts = math.floor(base_points * 0.5)

    def speed_bonus(tt):
        if scoring_mode == "accuracy":
            # Accuracy mode: full points for a correct answer, no time pressure
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
        player = Player(player_id, nickname, websocket, session.room_code)
        if session.roster:
            entry = next(
                (r for r in session.roster
                 if r["name"].lower() == nickname.lower()),
                None,
            )
            if entry:
                player.student_id = entry["student_id"]
        if session.team_count:
            # Round-robin onto the currently smallest team
            sizes = [0] * session.team_count
            for p in session.players.values():
                if p.team is not None and 0 <= p.team < session.team_count:
                    sizes[p.team] += 1
            player.team = sizes.index(min(sizes))
        session.players[player_id] = player
        session.touch()
        logger.info("room %s: player joined %r (%s, team=%s, student_id=%s)",
                    session.room_code, nickname, player_id,
                    player.team, player.student_id)

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

    async def set_teams(self, room_code: str, count: int):
        """Lobby-only: turn team mode on (2-4 teams) or off (0). Reassigns
        everyone already in the room round-robin."""
        session = self.get_session(room_code)
        if not session or session.state != "lobby":
            return
        count = max(0, min(4, int(count)))
        session.team_count = count if count >= 2 else None
        session.touch()
        players = list(session.players.values())
        for i, p in enumerate(players):
            p.team = (i % count) if session.team_count else None
        logger.info("room %s: team mode set to %s",
                    session.room_code, session.team_count or "off")
        for p in players:
            await self._send_to_player(p, {
                "type": "team_update",
                "team": p.team,
                "team_name": (TEAM_NAMES[p.team % len(TEAM_NAMES)]
                              if p.team is not None else None),
                "team_count": session.team_count,
            })
        await self._send_host(session, {
            "type": "player_update",
            "count": len(session.connected_player_list()),
            "players": session.connected_player_list(),
            "team_count": session.team_count,
        })

    def set_class(self, room_code: str, class_id: Optional[int],
                  class_name: Optional[str], roster: Optional[List[dict]]):
        """Attach (or detach) a class roster to a lobby session."""
        session = self.get_session(room_code)
        if not session or session.state != "lobby":
            return False
        session.class_id = class_id
        session.class_name = class_name
        session.roster = roster
        session.touch()
        logger.info("room %s: class %s attached (%d roster names)",
                    session.room_code, class_name or "none",
                    len(roster or []))
        return True

    def set_locked(self, room_code: str, locked: bool) -> bool:
        session = self.get_session(room_code)
        if not session:
            return False
        session.locked = bool(locked)
        session.touch()
        logger.info("room %s: room %s", session.room_code,
                    "locked" if session.locked else "unlocked")
        return session.locked

    async def kick_player(self, room_code: str, player_id: str):
        session = self.get_session(room_code)
        if not session:
            return
        player = session.players.pop(player_id, None)
        if not player:
            return
        session.touch()
        logger.info("room %s: player %r kicked by host",
                    session.room_code, player.nickname)
        try:
            await player.websocket.send_json({"type": "kicked"})
        except Exception:
            pass
        try:
            await player.websocket.close()
        except Exception:
            pass
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
        if q_type == "order" and len(options) > 1:
            shuffled_indices = list(range(len(options)))
            random.shuffle(shuffled_indices)
            # correct_ordering: the sequence of shuffled-array indices that gives original order
            # i.e., where should shuffled[i] go? → argsort of shuffled_indices
            argsort = [0] * len(shuffled_indices)
            for orig_pos, shuf_pos in enumerate(shuffled_indices):
                argsort[shuf_pos] = orig_pos
            # Player submits: send_options in their chosen order as indices into send_options
            # Correct submission = the permutation that restores original order
            # Since send_options[i] = options[shuffled_indices[i]],
            # the correct order of send_options is argsort of shuffled_indices
            session.order_correct[qi] = argsort
        elif q_type == "order":
            session.order_correct[qi] = list(range(len(options)))

        # _question_payload reconstructs the shuffled order from order_correct,
        # so the live message and the rejoin message share one shape
        msg = self._question_payload(session)
        session.touch()
        logger.info("room %s: question %d/%d started (%s)",
                    session.room_code, qi + 1, len(session.questions), q_type)
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

        session.touch()
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

        session.touch()
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

        session.touch()
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

        session.touch()
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

        qi = session.current_question_index
        session.touch()

        if qi in session.revealed_questions:
            # Duplicate reveal (double-click, second host tab, replay): never
            # re-score. Resend the already-computed payload to the host only.
            logger.warning("room %s: duplicate reveal for question %d blocked",
                           session.room_code, qi + 1)
            await self._send_host(session, self._build_host_reveal(session))
            return
        session.revealed_questions.add(qi)

        session.state = "reveal"
        q_type = q.get("question_type", "mc")
        correct_json = q.get("correct_json", "")
        time_limit = q.get("time_limit", 20)
        base_points = q.get("points", 100)

        # For order type: use the shuffled-correct ordering stored in session
        if q_type == "order":
            correct_ordering = session.order_correct.get(qi, [])
            scoring_correct_json = json.dumps(correct_ordering)
        else:
            scoring_correct_json = correct_json

        # Score all players, recording the outcome so rejoins and the
        # end-of-game review reuse it instead of re-deriving points
        streak_counts = q_type in ("mc", "tf", "ms", "order")
        for player in session.players.values():
            answer = player.answers.get(qi)
            if answer is None:
                # For order: use selections as final answer
                answer = player.selections.get(qi)
            if answer is None:
                if streak_counts:
                    player.streak = 0
                player.question_results[qi] = {
                    "points": 0, "correct": False, "answered": False,
                    "streak_after": player.streak,
                }
                continue
            time_taken = player.answer_times.get(qi, time_limit)
            pts, is_correct = _score_answer(
                q_type, scoring_correct_json, answer, time_taken, time_limit,
                base_points, session.scoring_mode)
            if streak_counts:
                if is_correct:
                    player.streak += 1
                    if session.streak_bonus_enabled and player.streak >= 2:
                        # +10% of base points per consecutive correct, capped at +50%
                        pts += math.floor(
                            base_points * 0.1 * min(player.streak - 1, 5))
                else:
                    player.streak = 0
            player.score += pts
            player.question_results[qi] = {
                "points": pts, "correct": is_correct, "answered": True,
                "streak_after": player.streak,
            }

        leaderboard = session.get_leaderboard()

        for player in session.players.values():
            reveal_msg = self._build_player_reveal(session, player, leaderboard)
            await self._send_to_player(player, reveal_msg)

        await self._send_host(session, self._build_host_reveal(session))
        logger.info("room %s: question %d revealed (%s)",
                    session.room_code, qi + 1, q_type)

    async def next_question(self, room_code: str):
        session = self.get_session(room_code)
        if not session:
            return
        session.current_question_index += 1
        if session.current_question_index >= len(session.questions):
            await self.end_game(room_code)
        else:
            await self._send_question(session)

    def _build_review(self, session: GameSession, player: Player) -> List[dict]:
        """Per-question review for one player: what they answered, what was
        right, and whether they got it — shown on their final screen."""
        review = []
        for qi, q in enumerate(session.questions):
            if qi not in session.revealed_questions:
                continue  # skipped questions were never scored
            q_type = q.get("question_type", "mc")
            options = q.get("options", [])
            correct_json = q.get("correct_json", "")
            result = player.question_results.get(qi, {})
            answered = result.get("answered", False)
            ans = player.answers.get(qi)
            if ans is None:
                ans = player.selections.get(qi)

            your_display = "—"
            correct_display = ""
            if q_type in ("mc", "tf", "poll"):
                if answered and isinstance(ans, int) and 0 <= ans < len(options):
                    your_display = str(options[ans])
                if q_type != "poll":
                    try:
                        ci = int(correct_json) if correct_json != "" else 0
                    except (ValueError, TypeError):
                        ci = 0
                    if 0 <= ci < len(options):
                        correct_display = str(options[ci])
            elif q_type == "ms":
                if answered and isinstance(ans, list):
                    your_display = ", ".join(
                        str(options[i]) for i in ans
                        if isinstance(i, int) and 0 <= i < len(options)
                    ) or "—"
                try:
                    correct_display = ", ".join(
                        str(options[i]) for i in json.loads(correct_json)
                        if isinstance(i, int) and 0 <= i < len(options)
                    )
                except Exception:
                    correct_display = ""
            elif q_type == "order":
                # Player ordering indexes into the shuffled list they saw
                perm = session.order_correct.get(qi)
                send_options = options[:]
                if perm and len(perm) == len(options):
                    send_options = [""] * len(options)
                    for orig_idx, shuf_pos in enumerate(perm):
                        send_options[shuf_pos] = options[orig_idx]
                if answered and isinstance(ans, list):
                    your_display = " → ".join(
                        str(send_options[i]) for i in ans
                        if isinstance(i, int) and 0 <= i < len(send_options)
                    ) or "—"
                correct_display = " → ".join(str(o) for o in options)
            elif q_type == "wordcloud":
                if answered and isinstance(ans, str):
                    your_display = ans

            review.append({
                "index": qi,
                "text": q.get("text", ""),
                "question_type": q_type,
                "your_answer": your_display,
                "correct_answer": correct_display,
                "answered": answered,
                "correct": result.get("correct", False),
                "points": result.get("points", 0),
                "scored": q_type not in ("poll", "wordcloud"),
            })
        return review

    async def end_game(self, room_code: str):
        session = self.get_session(room_code)
        if not session:
            return
        session.analytics_data = self.compile_analytics(session)
        session.state = "ended"
        session.touch()
        leaderboard = session.get_leaderboard()
        teams = session.get_team_leaderboard() if session.team_count else None
        logger.info("room %s: game ended (%d players, %d/%d questions)",
                    session.room_code, len(session.players),
                    session.current_question_index + 1, len(session.questions))
        host_msg: dict = {"type": "game_end", "leaderboard": leaderboard}
        if teams:
            host_msg["teams"] = teams
        for player in session.players.values():
            player_msg = dict(host_msg)
            player_msg["review"] = self._build_review(session, player)
            if teams is not None:
                player_msg["your_team"] = player.team
            await self._send_to_player(player, player_msg)
        await self._send_host(session, host_msg)

    async def remove_player(self, room_code: str, player_id: str,
                            websocket: Optional[WebSocket] = None):
        session = self.get_session(room_code)
        if not session:
            return
        player = session.players.get(player_id)
        if player:
            if websocket is not None and player.websocket is not websocket:
                # A stale socket closed after the player already rejoined on a
                # new one — don't mark the live connection disconnected.
                return
            if player.connected:
                logger.info("room %s: player %r disconnected (ws closed)",
                            session.room_code, player.nickname)
            player.connected = False
        player_list = session.connected_player_list()
        await self._send_host(session, {
            "type": "player_update",
            "count": len(player_list),
            "players": player_list,
        })

    async def _send_to_player(self, player: Player, message: dict) -> bool:
        """Send to a player regardless of the `connected` flag.

        `connected` tracks deliverability for UI (lobby counts), but must never
        silently block game-state delivery: a player marked disconnected by one
        failed send may still have a receivable socket, or the flag may simply
        be stale. On failure, close the server side of the socket so the
        browser's onclose fires and its reconnect+rejoin cycle starts.
        """
        try:
            await player.websocket.send_json(message)
            if not player.connected:
                logger.info("room %s: player %r reconnected (send ok)",
                            player.room_code, player.nickname)
            player.connected = True
            return True
        except Exception:
            if player.connected:
                logger.info("room %s: player %r marked disconnected (send failed)",
                            player.room_code, player.nickname)
            player.connected = False
            try:
                await player.websocket.close()
            except Exception:
                pass
            return False

    async def _broadcast_players(self, session: GameSession, message: dict):
        for player in session.players.values():
            await self._send_to_player(player, message)

    async def _send_host(self, session: GameSession, message: dict):
        if session.host_websocket:
            try:
                await session.host_websocket.send_json(message)
            except Exception:
                pass

    def _build_player_reveal(self, session: GameSession, player: Player,
                             leaderboard: Optional[List[dict]] = None) -> dict:
        """Build the per-player reveal message. Assumes scores are already
        applied for the current question (i.e. reveal_answer has run)."""
        q = session.current_question or {}
        qi = session.current_question_index
        q_type = q.get("question_type", "mc")
        correct_json = q.get("correct_json", "")
        time_limit = q.get("time_limit", 20)
        base_points = q.get("points", 100)

        if q_type == "order":
            scoring_correct_json = json.dumps(session.order_correct.get(qi, []))
        else:
            scoring_correct_json = correct_json

        correct_index = -1
        if q_type in ("mc", "tf"):
            try:
                correct_index = int(correct_json) if correct_json != "" else 0
            except (ValueError, TypeError):
                correct_index = 0

        if leaderboard is None:
            leaderboard = session.get_leaderboard()

        answer = player.answers.get(qi)
        if answer is None:
            answer = player.selections.get(qi)

        # Prefer the outcome recorded at reveal time (includes streak bonus);
        # recompute only if this player was somehow never scored
        stored = player.question_results.get(qi)
        if stored is not None:
            pts_earned, is_correct = stored["points"], stored["correct"]
        else:
            time_taken = player.answer_times.get(qi, time_limit)
            pts_earned, is_correct = _score_answer(
                q_type, scoring_correct_json, answer if answer is not None else -1,
                time_taken, time_limit, base_points, session.scoring_mode
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
            "streak": player.streak,
        }
        if q_type == "wordcloud":
            reveal_msg["your_text"] = player.answers.get(qi, "")
        if session.team_count:
            reveal_msg["teams"] = session.get_team_leaderboard()
            reveal_msg["your_team"] = player.team
        return reveal_msg

    def _build_host_reveal(self, session: GameSession) -> dict:
        """Build the host-side reveal message. Assumes scores are already
        applied for the current question (i.e. reveal_answer has run)."""
        q = session.current_question or {}
        qi = session.current_question_index
        q_type = q.get("question_type", "mc")
        correct_json = q.get("correct_json", "")

        if q_type == "order":
            scoring_correct_json = json.dumps(session.order_correct.get(qi, []))
        else:
            scoring_correct_json = correct_json

        correct_index = -1
        if q_type in ("mc", "tf"):
            try:
                correct_index = int(correct_json) if correct_json != "" else 0
            except (ValueError, TypeError):
                correct_index = 0

        host_reveal: dict = {
            "type": "reveal",
            "question_type": q_type,
            "correct_index": correct_index,
            "correct_json": scoring_correct_json,
            "leaderboard": session.get_leaderboard()[:5],
        }
        if session.team_count:
            host_reveal["teams"] = session.get_team_leaderboard()
        if q_type == "wordcloud":
            freq_dict: Dict[str, int] = {}
            for txt in session.wordcloud_answers.get(qi, {}).values():
                key = txt.lower().strip()
                if key:
                    freq_dict[key] = freq_dict.get(key, 0) + 1
            host_reveal["words"] = freq_dict
        return host_reveal

    def _phase_info(self, session: GameSession) -> dict:
        """Current phase and remaining time for the active question."""
        q = session.current_question or {}
        time_limit = q.get("time_limit", 20)
        now = time.time()
        phase = "answering"
        read_remaining = 0.0
        answer_remaining = float(time_limit)
        if session.answer_phase_start_time:
            if now < session.answer_phase_start_time:
                phase = "reading"
                read_remaining = session.answer_phase_start_time - now
            else:
                answer_remaining = max(
                    0.0, time_limit - (now - session.answer_phase_start_time)
                )
        return {
            "phase": phase,
            "read_time_remaining": round(read_remaining, 1),
            "answer_time_remaining": round(answer_remaining, 1),
        }

    def _question_payload(self, session: GameSession) -> Optional[dict]:
        """Build the same message shape as _send_question, reconstructing the
        shuffled option order for "order" questions from order_correct."""
        q = session.current_question
        if not q:
            return None
        qi = session.current_question_index
        q_type = q.get("question_type", "mc")
        options = q.get("options", [])

        send_options = options[:]
        if q_type == "order":
            perm = session.order_correct.get(qi)
            # perm[original_index] = position in the shuffled list sent to players
            if perm and len(perm) == len(options):
                send_options = [""] * len(options)
                for orig_idx, shuf_pos in enumerate(perm):
                    send_options[shuf_pos] = options[orig_idx]

        return {
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
        session.touch()
        logger.info("room %s: player rejoined %r (%s, state=%s)",
                    session.room_code, player.nickname, player.player_id,
                    session.state)
        result = {
            "ok": True,
            "player_id": player.player_id,
            "state": session.state,
            "score": player.score,
            "question_index": session.current_question_index,
            "team": player.team,
            "team_name": (TEAM_NAMES[player.team % len(TEAM_NAMES)]
                          if player.team is not None else None),
            "streak": player.streak,
        }

        qi = session.current_question_index
        if session.state == "question":
            payload = self._question_payload(session)
            if payload:
                result.update(self._phase_info(session))
                result.update({
                    "question": payload,
                    "already_answered": qi in player.confirmed or qi in player.answers,
                })
        elif session.state == "reveal":
            result["reveal"] = self._build_player_reveal(session, player)

        return result

    async def rejoin_host(self, room_code: str, websocket: WebSocket) -> dict:
        """Re-attach a host websocket to a live session and return a full
        host-state-sync payload, mirroring rejoin_player."""
        session = self.get_session(room_code)
        if not session or session.state == "ended":
            return {"ok": False, "reason": "room_not_found"}

        session.host_websocket = websocket
        session.touch()
        logger.info("room %s: host rejoined (state=%s)",
                    session.room_code, session.state)

        result: dict = {
            "ok": True,
            "room_code": session.room_code,
            "quiz_name": session.quiz_data.get("name", ""),
            "question_count": len(session.questions),
            "state": session.state,
            "question_index": session.current_question_index,
            "player_list": session.connected_player_list(),
            "leaderboard": session.get_leaderboard(),
            "locked": session.locked,
            "team_count": session.team_count,
            "class_id": session.class_id,
            "class_name": session.class_name,
        }
        if session.team_count:
            result["teams"] = session.get_team_leaderboard()

        if session.state in ("question", "reveal"):
            payload = self._question_payload(session)
            if payload:
                qi = session.current_question_index
                result["question"] = payload
                result["answer_counts"] = session.answer_counts
                if session.state == "question":
                    result.update(self._phase_info(session))
                    if payload["question_type"] == "wordcloud":
                        # live word feed (reactions are transient — no state)
                        result["words"] = list(
                            session.wordcloud_answers.get(qi, {}).values()
                        )
                else:
                    result["reveal"] = self._build_host_reveal(session)

        return result

    async def end_session(self, room_code: str):
        session = self.get_session(room_code)
        if not session:
            return
        session.state = "ended"
        session.touch()
        logger.info("room %s: session stopped by host at question %d/%d",
                    session.room_code, session.current_question_index + 1,
                    len(session.questions))
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
                    await self._send_to_player(player, {"type": "ping"})
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
            "class_id": session.class_id,
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

        session.touch()
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
        session.touch()
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
            s = self.sessions[code]
            logger.info("room %s: garbage-collected (inactive since %s, state=%s)",
                        code, s.last_activity.isoformat(timespec="seconds"),
                        s.state)
            del self.sessions[code]


game_manager = GameManager()
