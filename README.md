# QuizLab

A real-time classroom quiz platform — Kahoot-style. Professors host sessions from a projector; students join on their phones via QR code or room code.

## Local Setup

**Requirements:** Python 3.10+

```bash
# 1. Clone / enter the project directory
cd quizlab

# 2. Create and activate a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy the env file and set your admin password
cp .env.example .env
# Edit .env and set ADMIN_PASSWORD

# 5. Start the server
uvicorn main:app --reload
```

Then open [http://localhost:8000](http://localhost:8000) — you'll be redirected to the admin login.

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `ADMIN_PASSWORD` | Password for the `/admin` panel | **Required in production.** Local SQLite only: `admin` |
| `SECRET_KEY` | Session signing key | **Required in production.** Local SQLite only: a dev-only value |
| `DATABASE_URL` | SQLAlchemy database URL | `sqlite:///./quizlab.db` |
| `DB_CONNECT_TIMEOUT` | Seconds to wait for the database connection at startup | `10` |
| `MIGRATION_LOCK_TIMEOUT` | Postgres `lock_timeout` for startup migrations | `5s` |
| `MIGRATION_STATEMENT_TIMEOUT` | Postgres `statement_timeout` for startup migrations | `60s` |

"Production" means `DATABASE_URL` points at Postgres. There, QuizLab refuses
to start if `ADMIN_PASSWORD` or `SECRET_KEY` is missing, instead of falling
back to a well-known default. Startup logs every database step
(`startup: connecting…`, `connected`, `tables created`, `database ready`),
so a slow or blocked deploy shows where it stopped.

## How to Create a Quiz

1. Go to `/admin/login` and enter your admin password.
2. Click **+ New Quiz** on the dashboard.
3. Enter a quiz name and optional course tag (e.g. `ADM-3083`).
4. Add questions one by one using the form on the right, or bulk-import via CSV.
5. For each question you can set:
   - Question type: multiple choice, true/false, multiple select, poll, ordering or word cloud
   - Time limit: 10 / 20 / 30 seconds
   - Points: 100 / 200 / 500, or any custom value from 0 to 1000
   - An image (drag-and-drop upload or paste a URL)
6. Reorder questions by dragging the ⠿ handle.
7. Click **Save Quiz**.

## How to Host a Session

1. On the dashboard, click **Host** next to any quiz.
2. A QR code and 6-character room code are displayed.
3. Students scan the QR or go to `/play` and enter the room code + a nickname.
4. Click **Start Game** once at least one player has joined.
5. The game flows automatically — the timer triggers the answer reveal.
6. Click **Next →** to advance to the next question.
7. The final leaderboard shows at the end with confetti.

## CSV Import and AI-Generated Questions

In the quiz editor, below the question list:

- **Download CSV template**: header row plus one data-analytics example per
  question type (`mc`, `tf`, `ms`, `poll`, `order`, `wordcloud`). UTF-8 with
  BOM, **separated by semicolons (`;`)** so it opens straight into columns in
  Excel with Spanish regional settings.
- **Download AI prompt**: a Markdown file (`quizlab_ai_prompt.md`) to give any
  AI assistant together with your class material. It explains every column
  and type, how `correct` is encoded, point and time ranges, image URL rules,
  examples, and a self-check list. The AI answers with a CSV in the template
  format.
- **Import CSV**: accepts `;` or `,` separators (auto-detected from the header
  row) and UTF-8 or Windows-1252 (Excel's plain "CSV" save). Rows with errors
  are skipped and listed with their row number, column, value and reason; rows
  with warnings are imported with the adjustment shown. Imported questions are
  added to the editor; click **Save Quiz** to keep them.

Format summary (letters A-F refer to `option_1`..`option_6`):

| `type` | Options | `correct` |
|---|---|---|
| `mc` | 2-6 | one letter, e.g. `B` |
| `tf` | exactly 2 (blank = Verdadero/Falso) | `A` or `B` |
| `ms` | 2-6 | all correct letters, e.g. `A,C,D` |
| `poll` | 2-6 | blank |
| `order` | 2-6, written in the correct order | blank |
| `wordcloud` | none | blank (always 0 points) |

`time_limit`: whole seconds 5-120. `points`: whole number 0-1000.
`image_url`: blank or a direct public `https://` link to an image file. The
legacy `option_a`..`option_d` format (no `type` column) still imports as `mc`.

### Where the format is defined

`question_spec.py` is the single source of truth: `COLUMNS`,
`QUESTION_TYPES` (option limits, `correct` encoding, scoring text, time
guidance, default points and an example row per type) and the numeric limits.
The template (`build_template_csv`), the AI prompt (`build_ai_prompt`) and
the importer (`parse_csv`) are all generated from it.

To add a question type, add a `QuestionType` entry there; the template, AI
prompt and importer update automatically. The game engine
(`game_manager._score_answer`), player/host JS and the editor UI still need
their own support. `tests/test_csv_roundtrip.py` fails if a type's example
answer is not scored as correct by the game engine.

### Tests

```bash
venv\Scripts\python -m unittest discover tests -v
```

Covers the template -> import round trip (unit and end-to-end over HTTP with
a throwaway SQLite DB), both separators, both encodings, and row/column error
reporting.

## Deploy to Render.com

1. Push your code to a GitHub repository.
2. Go to [render.com](https://render.com) and click **New → Web Service**.
3. Connect your GitHub repo.
4. Render will detect `render.yaml` automatically. Review the settings:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Set the `ADMIN_PASSWORD` environment variable in the Render dashboard (or let it auto-generate one and copy it from the logs).
6. Click **Deploy**.

Question images are stored in a public Supabase Storage bucket named `question-images`. Set `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` in the Render environment (server-side only). Without them, uploads fall back to the local `uploads/` folder, which does **not** persist on Render.

> **Note:** After deploy, find your auto-generated `ADMIN_PASSWORD` in **Environment → Secret Files** or the deploy logs.

> **Free-tier spin-down risk:** Render's free plan spins the service down after ~15 minutes with no inbound HTTP traffic, which can restart the process mid-class (e.g. during a normal lecture gap between quiz questions). QuizLab persists live session/player state to the database and rehydrates it on startup, so a restart no longer loses scores or strands connected students — everyone's phone rejoins the same room code automatically. What this does **not** fix is the ~30-60s cold-start delay while a spun-down instance wakes back up: during that window the app is unreachable for everyone, host and students alike. For classes running live, upgrade to a paid Render plan to avoid spin-down entirely.

## WebSocket Protocol Reference

### Player → Server
```json
{ "type": "join",   "room_code": "ABC123", "nickname": "Juan" }
{ "type": "answer", "question_id": 2, "answer_index": 1, "client_timestamp": 1718000000000 }
```

### Server → Player
```json
{ "type": "joined",        "player_id": "uuid", "player_list": [...] }
{ "type": "player_update", "player_count": 5, "player_list": [...] }
{ "type": "game_start" }
{ "type": "question",      "id": 2, "text": "...", "options": [...], "time_limit": 20, ... }
{ "type": "reveal",        "correct_index": 1, "points_earned": 850, "rank": 2, "total_players": 8 }
{ "type": "game_end",      "leaderboard": [...] }
```

### Server → Host
```json
{ "type": "session_created", "room_code": "ABC123", "quiz_name": "...", "question_count": 8 }
{ "type": "player_update",   "count": 5, "players": [...] }
{ "type": "answer_counts",   "counts": [3, 1, 0, 2] }
{ "type": "reveal",          "correct_index": 1, "leaderboard": [...] }
{ "type": "game_end",        "leaderboard": [...] }
```
