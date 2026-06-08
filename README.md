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
| `ADMIN_PASSWORD` | Password for the `/admin` panel | `admin123` |
| `SECRET_KEY` | Session signing key — **change in production** | `quizlab-secret-change-me` |
| `DATABASE_URL` | SQLAlchemy database URL | `sqlite:///./quizlab.db` |

## How to Create a Quiz

1. Go to `/admin/login` and enter your admin password.
2. Click **+ New Quiz** on the dashboard.
3. Enter a quiz name and optional course tag (e.g. `ADM-3083`).
4. Add questions one by one using the form on the right, or bulk-import via CSV.
5. For each question you can set:
   - Question type: **Multiple Choice** (A–D) or **True/False**
   - Time limit: 10 / 20 / 30 seconds
   - Points: 100 / 200 / 500
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

## CSV Import Format

Download the template from the admin panel (↓ Template button) or use this structure:

| Column | Description | Valid Values |
|---|---|---|
| `question` | Question text | Any text |
| `option_a` | Answer option A | Any text |
| `option_b` | Answer option B | Any text |
| `option_c` | Answer option C (omit for True/False) | Any text or blank |
| `option_d` | Answer option D (omit for True/False) | Any text or blank |
| `correct` | Correct answer letter | `A`, `B`, `C`, or `D` |
| `time_limit` | Seconds per question | `10`, `20`, or `30` |
| `points` | Points awarded for correct answer | `100`, `200`, or `500` |
| `image_url` | Optional image URL | URL or blank |

**Example:**
```csv
question,option_a,option_b,option_c,option_d,correct,time_limit,points,image_url
What is 2+2?,3,4,5,6,B,20,100,
The sky is blue,True,False,,,A,10,200,
```

## Deploy to Render.com

1. Push your code to a GitHub repository.
2. Go to [render.com](https://render.com) and click **New → Web Service**.
3. Connect your GitHub repo.
4. Render will detect `render.yaml` automatically. Review the settings:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Set the `ADMIN_PASSWORD` environment variable in the Render dashboard (or let it auto-generate one and copy it from the logs).
6. Click **Deploy**.

The `render.yaml` includes a 1 GB persistent disk mounted at `/data` for the SQLite database and uploaded images. Make sure the disk is attached before the first deploy.

> **Note:** After deploy, find your auto-generated `ADMIN_PASSWORD` in **Environment → Secret Files** or the deploy logs.

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
