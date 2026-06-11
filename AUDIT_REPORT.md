# QuizLab — Audit Report

**Date:** 2026-06-10
**Scope:** Full review of visual consistency, code organization, WebSocket runtime behavior, and code quality across all sessions to date. No code was changed as part of this audit. Line numbers reflect the state of the repo after the Phase 1 disconnection fixes.

**Classification key:**
🟢 **Quick win** — small, low-risk, < ~1 hour · 🟡 **Worth doing soon** — meaningful payoff, needs a focused session · 🔵 **Longer-term refactor** — structural, plan deliberately

---

## 1. Visualización

**Overall: the design system is in good shape.** All tokens (palette, both themes) live in one place (`static/css/base.css:4-47`), the three-font system (Barlow Condensed for display, Syne Mono for codes/numbers, DM Sans for body) is loaded once in `templates/base.html:17` and applied consistently, and the light-mode lime-contrast fixes follow a single documented convention in all three CSS files. The newer features (reactions, word cloud, history pages) mostly inherit the language correctly. Findings below are deviations, not systemic problems.

### V1. 🟢 Duplicated timer-ring CSS
`.ring-track`, `.ring-progress`, `.ring-number` (+ `.urgent`) are defined identically in `static/css/base.css:272-300` ("shared by host + player") **and again** in `static/css/host.css:176-203`. The host copy is pure duplication — delete it. Any future tweak made in one file will silently miss the other.

### V2. 🟢 Session-detail table styles live inside a Jinja block — and the wrong one
`templates/admin_session_detail.html:116-136` defines the entire `.stat-table` / `.dist-*` component inside a `<style>` tag placed in `{% block extra_js %}`. It's the only page-level component not in `static/css/admin.css`, and putting CSS in the JS block is a trap for the next session. Move it to `admin.css` next to the other admin components.

### V3. 🟢 Medal colors hardcoded in four places
`#FFD700` / `#C0C0C0` / `#CD7F32` appear in `static/css/host.css:443-445` (`.lb-rank.topN`), `host.css:554-556` (`.final-lb-num.topN`), `templates/admin_session_detail.html:122-124` (`.stat-table tr.topN`), and as inline strings in `static/js/host.js` (`showStoppedLeaderboard`, ~line 530). Promote to `--gold` / `--silver` / `--bronze` tokens in `base.css` so the history pages and live screens can't drift.

### V4. 🟡 The "stopped quiz" overlay bypasses the design system entirely
`showStoppedLeaderboard()` in `static/js/host.js:523-567` builds a full-screen overlay from concatenated inline-style strings (hardcoded font sizes, spacing, colors) and re-implements `.final-lb-row` markup by hand. It mostly mimics the final screen, but it's the one screen that won't pick up any future CSS change. It should be a hidden section in `host.html` styled by `host.css`, populated by JS — like every other view.

### V5. 🟡 Two UI languages
The product has drifted from English to Spanish across sessions: older admin screens are English (`templates/admin_dashboard.html` "Your Quizzes", "+ New Quiz"; `admin_quiz_editor.html` "Save Quiz", "Add Question"; `host.html` "Players", "Start Game", "Room Code", "Live Answers"; `admin_login.html` entirely English), while everything newer is Spanish (player UI, "Historial", "Detener Quiz", "RESULTADOS PARCIALES", "Detalle de Sesión", word-cloud strings). Students only see Spanish, so the inconsistency is teacher-facing — but it's worth deciding on one language (presumably Spanish) and sweeping the admin/host templates once.

### V6. 🟢 Dead static markup in host.html
`templates/host.html:93-114` hardcodes four `.bar-row` elements, but `buildChart()` in `host.js:410-441` empties and rebuilds that container on every question. The static rows render only between page load and the first question. Replace with an empty container.

### V7. 🟢 Word-cloud input font deviates from the form convention
Every input gets DM Sans via the global rule in `base.css:180-196`, but `.wc-input` (`static/css/player.css:824-839`) overrides it with Barlow Condensed at 22px — a display font, in a text input, all-caps-looking while students type. If intentional (game-show feel), add a comment saying so; otherwise align it with the form convention.

---

## 2. Organización

### O1. 🔵 `main.py` is five modules in one file (~850 lines)
It currently contains: admin auth, dashboard, quiz CRUD, image upload, CSV import/export, history/analytics views, QR generation, the host WebSocket, the player WebSocket, and DB persistence (`_persist_session`, `main.py:81`). A natural split using FastAPI routers:

- `routers/admin.py` — auth, dashboard, quiz editor, upload, CSV
- `routers/history.py` — history + session-detail views (and their stat-shaping logic)
- `routers/ws.py` — both WebSocket endpoints
- `services/persistence.py` — `_persist_session`, paired with analytics (see O2)

Not urgent — the file is still navigable — but each session adds to it, and the WebSocket handlers in particular deserve their own home before the next realtime feature lands.

### O2. 🟡 `game_manager.py` mixes four concerns
Transport (send/close/broadcast), game-state machine, scoring (`_score_answer`, `game_manager.py:87`), and analytics compilation (`compile_analytics`, `game_manager.py:689`). The cheapest meaningful split: move `_score_answer` to `scoring.py` (pure function, trivially testable — the Phase 1 smoke test would have been simpler with it importable) and `compile_analytics` + `_persist_session` to `analytics.py` so the compile/persist pair lives together instead of straddling two files.

### O3. 🟡 ~650 lines of editor JavaScript inline in a template
`templates/admin_quiz_editor.html:192-843` holds all editor logic in a `<script>` block, unlike player/host which have proper files in `static/js/`. Only `questions_json` and `quiz.id` actually need Jinja; everything else can move to `static/js/editor.js` (with the data passed via `window.QUIZ_DATA`, same pattern as `window.QUIZ_ID` in `host.html:145`).

### O4. Dead code from earlier iterations — all 🟢

| What | Where | Note |
|---|---|---|
| `_require_admin()` | `main.py:125` | Defined, never used — meanwhile the same inline session check is repeated in ~12 endpoints (see C2) |
| `onStateSync()` + `'state_sync'` case | `static/js/player.js` (~lines 127, 658) | The server never sends `state_sync`; Phase 1 made `onRejoined` carry the payload instead. Either delete it or refactor `onRejoined` to delegate to it — don't keep two near-identical renderers |
| `answerCounts` variable | `static/js/host.js:14,129` | Assigned on every question, never read (`latestCounts` is the live one) |
| `client_timestamp` | `player.js` (`submitAnswer`) → `main.py:763` | Sent and accepted but never used in scoring; timing comes from `answer_phase_start_time` server-side |
| `.game-footer` CSS | `static/css/host.css:465-474` | No footer exists in `host.html` |
| Old CSV format support (`option_a`–`option_d`) | `main.py:466-500` | Backward-compat for a format only the pre-rewrite template produced; likely no such files exist anymore — confirm and delete |

### O5. 🟢 Repo hygiene — files tracked in git that shouldn't be
`git ls-files` shows: **`.env` (contains the real `SECRET_KEY`/`ADMIN_PASSWORD` — see C6)**, `__pycache__/*.pyc`, `quizlab.db` (the live dev database), and stray log files `uv_err.txt`, `uvicorn_err.txt`, `uvicorn_out.txt`. There are also **two** virtualenvs on disk (`venv/` and `.venv/`). Fix `.gitignore`, `git rm --cached` the offenders, delete one venv.

### O6. 🟢 Deprecated patterns
`@app.on_event("startup")` (`main.py:55`) is deprecated in current FastAPI — migrate to a lifespan handler. `datetime.utcnow()` (used throughout `game_manager.py` and `models.py`) is deprecated since Python 3.12 — `datetime.now(timezone.utc)`. Neither breaks today; both will eventually.

---

## 3. Funcionamiento

End-to-end flow reviewed: join → heartbeat → question → answer/selection/confirm → reveal → next → end → persist, plus rejoin, reactions, and word cloud. The Phase 1 fixes close the player-side delivery/rejoin holes. The remaining gaps, in order of severity:

### F1. 🟡 The host has no reconnection story at all (the mirror of Phase 1)
`host.js:37` — `onclose` just logs. There is no host heartbeat-recovery, no host rejoin protocol, and reloading the host page opens `/ws/host/{quiz_id}`, which **always creates a brand-new session and room code** (`main.py:677`). If the teacher's laptop drops Wi-Fi for 30 seconds mid-class, the session is orphaned: players stay connected to a room whose host can never return, and the only exit is "Detener Quiz"… which the host can't send either. Server-side the session survives (`host_websocket = None`, `main.py:704-706`), so the fix is a `host_rejoin` by room code symmetrical to the player one, plus reconnect logic in `host.js`. After today's incident, this is the most likely next classroom failure.

### F2. 🟢 Double reveal double-scores players — no server-side guard
`reveal_answer()` (`game_manager.py:389`) increments `player.score` for every call. The only protection is the client-side `revealSent` flag (`host.js:401-407`). Two host tabs, a duplicated message, or a reconnect replay would score every player twice. One line: `if session.state != "question": return` at the top. The same pattern applies to `start_game`/`next_question` (`game_manager.py:454`), which have no state guards either — a double-clicked "Next →" silently skips a question.

### F3. 🟡 Sessions stopped via "Detener Quiz" never reach history
The HTTP stop path (`main.py:599` → `end_session`, `game_manager.py:666`) sets `state = "ended"` but never calls `compile_analytics`, so `analytics_data` stays empty and `_persist_session` (which requires it, `main.py:83`) never fires. A 9-of-10-questions session stopped one early is invisible in the history pages, even though the host saw a partial leaderboard. If intentional, document it; if not, compile + persist in `end_session` too.

### F4. 🟡 Persistence is coupled to the host's WebSocket receive loop
`_persist_session` runs only inside `ws_host`'s message loop (`main.py:700-702`), i.e. only when the host's *next* inbound message is processed after state becomes `ended`. If the host socket dies at the wrong moment, analytics are never written, and `cleanup_old_sessions` (`game_manager.py:823`) erases the in-memory data two hours later. Move the persist call into `end_game` itself (via a callback or by passing the persist function into the manager) so it doesn't depend on host connectivity.

### F5. 🟢 A long quiet stretch can get a live session garbage-collected
`last_activity` is only touched on `add_player` (`game_manager.py:181`) and `rejoin_player` (`game_manager.py:628`) — never on questions, answers, or reveals. A session running longer than 2 hours with no new joins crosses the `cleanup_old_sessions` cutoff (`game_manager.py:823-828`) **while still being played**, and the next cleanup tick deletes it mid-game. Unlikely at 60 minutes, guaranteed at 130. Touch `last_activity` in `_send_question` (one line).

### F6. 🟢 Malformed client messages silently kill the player connection
In `ws_player`, casts like `int(data.get("question_id", -1))` (`main.py:761`) raise on garbage, land in the blanket `except Exception` (`main.py:829`), and the player is removed — with zero logging. The wordcloud handler already guards its cast (`main.py:790-792`); the others don't. Harden the casts and log the outer exception so the next "students dropped silently" incident leaves evidence.

### F7. 🟢 Word-cloud live feed double-escapes text
`onWordcloudUpdate` (`host.js:83`) sets `chip.textContent = escapeHtml(word)` — `textContent` doesn't parse HTML, so a student answering `A&B` displays as `A&amp;B` on the projector. Drop the `escapeHtml` (textContent is already safe). The reaction handler (`host.js:68`, `innerHTML`) uses it correctly.

### F8. 🟡 A student who loses sessionStorage is locked out for good
Joins are rejected outside the lobby (`main.py:730-732`), and rejoin requires the stored `player_id`/nickname. If a phone dies and the student returns on a borrowed device or a fresh browser, there is no path back in — they sit out the rest of class. Options: allow mid-game joins (score from 0), or let rejoin succeed by nickname alone when that nickname is currently disconnected (it already half-supports this — the nickname fallback in `rejoin_player`, `game_manager.py:616-621` — but the client never offers the option). Related minor edge: that nickname fallback takes the *first* case-insensitive match, so two students who ever shared a nickname can attach to each other's score.

### F9. 🔵 All game timing lives in the host's browser
The server records `answer_phase_start_time` but never enforces it: reveal happens only when the host's `setInterval` fires (`host.js:364-380`), and `handle_answer` accepts answers as long as `state == "question"` — indefinitely, if the host tab is throttled in the background (browsers throttle timers on inactive tabs). Speed bonus also trusts server-side elapsed time only, which is correct, but late answers after the visual "0" are still accepted until the reveal message arrives. A server-side `asyncio` timer per question (with the host able to reveal early) is the robust design; significant change, so plan it.

---

## 4. Código

### C1. 🟢 Logging is ad hoc and most failures are invisible
`import logging` happens inside functions (`main.py:59,121`); `game_manager` has none at all, so every swallowed exception (`_send_host`, `_send_to_player`, `ping_all_players`) is silent. Today's hour-long incident produced zero log lines. Set up one module-level logger per file and emit at least `warning` on send failures and `error` (with traceback) in the `ws_player` outer handler. This is the single cheapest change for diagnosing the next classroom problem.

### C2. 🟢 Repeated logic that wants extraction
- **Admin check ×12:** every admin endpoint repeats `if not request.session.get("admin")`. The unused `_require_admin` (`main.py:125`) already exists — finish the thought and apply it as a `Depends()` (one variant for HTML redirects, one returning 401 for JSON).
- **Answer-time recording ×4:** the identical `answer_phase_start_time` elapsed-time block appears in `handle_answer` (`game_manager.py:273-277`), `handle_confirm` (~342), `handle_order_update` (~369), and `handle_wordcloud_answer` (~799) → one `_record_answer_time(session, player, qid)` helper.
- **JS utilities ×3:** `escapeHtml` is copy-pasted in `player.js`, `host.js`, and `admin_quiz_editor.html`; `showToast` in dashboard and editor templates → `static/js/util.js`.

### C3. 🟢 N+1 queries in dashboard and history
- `admin_dashboard` (`main.py:176-177`) runs **two queries per quiz** and counts by loading every row into Python: `len(db.exec(select(Question)...).all())`. With 50 quizzes that's 100+ queries per page view. One `select(Question.quiz_id, func.count()).group_by(...)` each for questions and sessions replaces them all.
- `quiz_history` (`main.py:334-339`) fetches **all** `SessionResult` rows per session and slices `[:3]` in Python — add `.limit(3)`, or fetch all top-3s in one query.

Harmless at today's scale, but they're the queries that will degrade first on Render/Supabase, and the fix is mechanical.

### C4. 🟡 `delete_quiz` leaves orphaned history rows
`main.py:304-316` deletes the quiz and its questions but not its `QuizSession` / `SessionResult` / `QuestionStat` rows. Orphaned sessions become unreachable (history is only linked from the quiz card) yet still exist; `session_detail` half-handles it (`quiz` can be `None`). Either cascade the delete or deliberately keep history and show it under an "archived" label — currently it's accidental limbo.

### C5. 🟢 No architecture overview for future sessions
Each session re-derives the same map. Add a ~20-line comment at the top of `main.py` (or an `ARCHITECTURE.md`): the module roles, the in-memory vs. DB split (live sessions in `game_manager.sessions`, history in SQLite/Postgres), the WebSocket message catalog (client→server and server→client types for player and host), and the `correct_json` shape per question type (`mc`/`tf`: index as string; `ms`/`order`: JSON int array; `poll`/`wordcloud`: empty). The `models.py:22` comment currently says "see spec" — there is no spec in the repo.

### C6. 🟢⚠️ Secrets committed to git
`.env` is tracked and contains the real `SECRET_KEY` and `ADMIN_PASSWORD`. Untrack it (`git rm --cached .env`, add to `.gitignore` — `.env.example` already exists for the template) and **rotate both values**, since they're in the repo history. If the repo is private and local-only the urgency is lower, but do it before the repo ever gets pushed anywhere shared.

### C7. Minor code-quality notes (🟢 each, batch them)
- Type hints are decent in `game_manager.py`; `main.py` endpoints have none on returns — fine, low value, skip unless touching anyway.
- `database.py:45-51` migration-by-`try/ALTER/except pass` runs on every boot and hides real DB errors; acceptable at this scale, but log the exception instead of `pass`.
- `models.py` FK columns (`session_id`, `quiz_id`) have no indexes — irrelevant at classroom scale, free to add (`index=True`) whenever the schema next changes.
- `reveal_answer` recomputes `_score_answer` per player twice per reveal (once to score, once in `_build_player_reveal`) — harmless cost, but storing per-question results on the player would also let history record *who* got what right, which the analytics currently can't.

---

## Prioritized list (proposed)

> **Note:** this ordering is a recommendation based on classroom-failure risk and effort. The final prioritization decision belongs to the project owner.

| # | Finding | Class | Why this position |
|---|---------|-------|-------------------|
| 1 | **F1** Host reconnect/rejoin | 🟡 | The remaining single point of failure for a live class; exact sibling of today's incident |
| 2 | **F2** State guards on reveal/next/start | 🟢 | Prevents silent double-scoring and skipped questions; ~5 lines |
| 3 | **C6** Untrack `.env`, rotate secrets | 🟢 | Cheap, and the cost of *not* doing it compounds with every push |
| 4 | **C1 + F6** Real logging + harden player message parsing | 🟢 | Makes every future incident diagnosable; today's would have left evidence |
| 5 | **F5** Touch `last_activity` on question send | 🟢 | One line; prevents mid-game session deletion in long classes |
| 6 | **F3 + F4** Persist stopped sessions; decouple persistence from host socket | 🟡 | History silently loses real sessions today |
| 7 | **F7** Word-cloud double-escape | 🟢 | Visible cosmetic bug on the projector; one-line fix |
| 8 | **O4 + O5** Dead-code and repo-hygiene sweep | 🟢 | Removes the traps (dead `state_sync` path, stale files) before they confuse a future session |
| 9 | **C2 + C3** Extract repeated logic; fix N+1 queries | 🟢 | Mechanical; shrinks the surface for the bigger refactors below |
| 10 | **C5** Architecture overview + message catalog | 🟢 | Pays for itself in every subsequent Claude Code session |
| 11 | **V1–V3, V6, V7** CSS dedup, medal tokens, table styles into admin.css | 🟢 | Batchable in one visual-cleanup pass |
| 12 | **V5** One UI language (Spanish) for admin/host | 🟡 | Teacher-facing polish |
| 13 | **F8** Re-entry path for students without sessionStorage | 🟡 | Real classroom scenario, needs a product decision first |
| 14 | **V4** Rebuild stopped-quiz overlay as a styled view | 🟡 | Fold into the same session as #12 |
| 15 | **C4** Decide orphaned-history behavior on quiz delete | 🟡 | Needs the same product decision lens |
| 16 | **O2 + O3** Split scoring/analytics out of game_manager; editor JS to its own file | 🟡 | Best done before the next feature touches those files |
| 17 | **O1** Router split of main.py | 🔵 | Structural; do after #16 |
| 18 | **F9** Server-side question timer | 🔵 | The right long-term design; biggest behavioral change, design it deliberately |
| 19 | **O6, C7** Deprecation and minor-quality batch | 🟢/🔵 | Opportunistic, whenever nearby code is touched |
