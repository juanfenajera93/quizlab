import asyncio
import csv
import io
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import qrcode
from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.middleware.sessions import SessionMiddleware

from database import create_db_and_tables, engine, get_session, _is_postgres
from game_manager import game_manager
from models import Question, Quiz, QuizSession, SessionResult, QuestionStat

load_dotenv()

app = FastAPI(title="QuizLab")

SECRET_KEY = os.getenv("SECRET_KEY", "quizlab-secret-change-me")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
async def on_startup():
    create_db_and_tables()
    if _is_postgres:
        import logging
        logging.warning(
            "QuizLab is running in PostgreSQL mode. "
            "Image uploads are saved to the local filesystem and will NOT persist "
            "between Render deploys. Migrate uploads to Supabase Storage for persistence."
        )
    asyncio.create_task(_cleanup_loop())
    asyncio.create_task(_heartbeat_loop())


async def _cleanup_loop():
    while True:
        await asyncio.sleep(600)
        game_manager.cleanup_old_sessions()


async def _heartbeat_loop():
    while True:
        await asyncio.sleep(25)
        await game_manager.ping_all_players()


async def _persist_session(room_code: str):
    gs = game_manager.get_session(room_code)
    if not gs or gs.analytics_persisted or not gs.analytics_data:
        return
    try:
        with Session(engine) as db:
            data = gs.analytics_data
            quiz_id = gs.quiz_data.get("id")
            if not quiz_id:
                return
            qs = QuizSession(
                quiz_id=quiz_id,
                room_code=data["room_code"],
                student_count=data["student_count"],
            )
            db.add(qs)
            db.commit()
            db.refresh(qs)
            for entry in data["leaderboard"]:
                db.add(SessionResult(
                    session_id=qs.id,
                    nickname=entry["nickname"],
                    score=entry["score"],
                    rank=entry["rank"],
                ))
            db.commit()
            for stat in data["question_stats"]:
                db.add(QuestionStat(
                    session_id=qs.id,
                    question_index=stat["question_index"],
                    question_text=stat["question_text"],
                    question_type=stat["question_type"],
                    correct_count=stat["correct_count"],
                    total_answers=stat["total_answers"],
                    avg_time_seconds=stat["avg_time_seconds"],
                    answers_json=stat["answers_json"],
                ))
            db.commit()
            gs.analytics_persisted = True
    except Exception as exc:
        import logging
        logging.error(f"Failed to persist session {room_code}: {exc}")


def _require_admin(request: Request):
    if not request.session.get("admin"):
        raise HTTPException(status_code=302, headers={"Location": "/admin/login"})


# ─── Root ────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return RedirectResponse("/admin")


# ─── Admin auth ──────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_root(request: Request):
    if request.session.get("admin"):
        return RedirectResponse("/admin/dashboard")
    return RedirectResponse("/admin/login")


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        request.session["admin"] = True
        return RedirectResponse("/admin/dashboard", status_code=303)
    return templates.TemplateResponse(
        "admin_login.html", {"request": request, "error": "Incorrect password"}
    )


@app.get("/admin/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login")


# ─── Admin dashboard ─────────────────────────────────────────────────────────

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: Session = Depends(get_session)):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    quizzes = db.exec(select(Quiz).order_by(Quiz.created_at.desc())).all()
    quizzes_data = []
    for quiz in quizzes:
        count = len(db.exec(select(Question).where(Question.quiz_id == quiz.id)).all())
        play_count = len(db.exec(select(QuizSession).where(QuizSession.quiz_id == quiz.id)).all())
        quizzes_data.append(
            {
                "id": quiz.id,
                "name": quiz.name,
                "course_tag": quiz.course_tag,
                "question_count": count,
                "last_played": quiz.last_played,
                "play_count": play_count,
            }
        )
    groups_dict: dict = {}
    for q in quizzes_data:
        tag = q["course_tag"] or "Sin materia"
        if tag not in groups_dict:
            groups_dict[tag] = []
        groups_dict[tag].append(q)
    sorted_tags = sorted(t for t in groups_dict if t != "Sin materia")
    if "Sin materia" in groups_dict:
        sorted_tags.append("Sin materia")
    quiz_groups = [{"tag": t, "quizzes": groups_dict[t]} for t in sorted_tags]
    return templates.TemplateResponse(
        "admin_dashboard.html", {
            "request": request,
            "quiz_groups": quiz_groups,
            "quiz_count": len(quizzes_data),
        }
    )


# ─── Quiz editor ─────────────────────────────────────────────────────────────

@app.get("/admin/quiz/new", response_class=HTMLResponse)
async def new_quiz_page(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    return templates.TemplateResponse(
        "admin_quiz_editor.html",
        {"request": request, "quiz": None, "questions_json": "[]", "read_time": 5},
    )


@app.get("/admin/quiz/{quiz_id}/edit", response_class=HTMLResponse)
async def edit_quiz_page(
    request: Request, quiz_id: int, db: Session = Depends(get_session)
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    quiz = db.get(Quiz, quiz_id)
    if not quiz:
        raise HTTPException(status_code=404)
    questions = db.exec(
        select(Question).where(Question.quiz_id == quiz_id).order_by(Question.position)
    ).all()

    questions_json = json.dumps(
        [
            {
                "text": q.text,
                "question_type": q.question_type,
                "options": json.loads(q.options_json) if q.options_json else [],
                "correct_json": q.correct_json,
                "time_limit": q.time_limit,
                "points": q.points,
                "image_url": q.image_url or "",
            }
            for q in questions
        ]
    )
    return templates.TemplateResponse(
        "admin_quiz_editor.html",
        {"request": request, "quiz": quiz, "questions_json": questions_json,
         "read_time": quiz.read_time},
    )


@app.post("/admin/quiz/save")
async def save_quiz(request: Request, db: Session = Depends(get_session)):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401)
    data = await request.json()

    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Quiz name is required"}, status_code=400)

    course_tag = data.get("course_tag", "").strip() or None
    quiz_id = data.get("quiz_id")
    questions_data = data.get("questions", [])

    read_time = int(data.get("read_time", 5))
    read_time = max(0, min(60, read_time))

    if quiz_id:
        quiz = db.get(Quiz, int(quiz_id))
        if not quiz:
            raise HTTPException(status_code=404)
        quiz.name = name
        quiz.course_tag = course_tag
        quiz.read_time = read_time
        for q in db.exec(select(Question).where(Question.quiz_id == quiz.id)).all():
            db.delete(q)
        db.commit()
    else:
        quiz = Quiz(name=name, course_tag=course_tag, read_time=read_time)
        db.add(quiz)
        db.commit()
        db.refresh(quiz)

    for i, qd in enumerate(questions_data):
        q = Question(
            quiz_id=quiz.id,
            position=i,
            text=qd.get("text", "").strip(),
            question_type=qd.get("question_type", "mc"),
            options_json=json.dumps(qd.get("options", [])),
            correct_json=str(qd.get("correct_json", "")),
            time_limit=int(qd.get("time_limit", 20)),
            points=int(qd.get("points", 100)),
            image_url=qd.get("image_url") or None,
        )
        db.add(q)
    db.commit()
    return JSONResponse({"quiz_id": quiz.id})


@app.delete("/admin/quiz/{quiz_id}")
async def delete_quiz(
    request: Request, quiz_id: int, db: Session = Depends(get_session)
):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401)
    quiz = db.get(Quiz, quiz_id)
    if not quiz:
        raise HTTPException(status_code=404)
    for q in db.exec(select(Question).where(Question.quiz_id == quiz_id)).all():
        db.delete(q)
    db.delete(quiz)
    db.commit()
    return JSONResponse({"ok": True})


# ─── Session history ─────────────────────────────────────────────────────────

@app.get("/admin/quiz/{quiz_id}/history", response_class=HTMLResponse)
async def quiz_history(
    request: Request, quiz_id: int, db: Session = Depends(get_session)
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    quiz = db.get(Quiz, quiz_id)
    if not quiz:
        raise HTTPException(status_code=404)
    quiz_sessions = db.exec(
        select(QuizSession).where(QuizSession.quiz_id == quiz_id).order_by(QuizSession.played_at.desc())
    ).all()
    sessions_data = []
    for qs in quiz_sessions:
        top3 = db.exec(
            select(SessionResult)
            .where(SessionResult.session_id == qs.id)
            .order_by(SessionResult.rank)
        ).all()[:3]
        sessions_data.append({
            "id": qs.id,
            "played_at": qs.played_at,
            "student_count": qs.student_count,
            "room_code": qs.room_code,
            "top3": [{"nickname": r.nickname, "score": r.score, "rank": r.rank} for r in top3],
        })
    return templates.TemplateResponse(
        "admin_quiz_history.html",
        {"request": request, "quiz": quiz, "sessions": sessions_data},
    )


@app.get("/admin/session/{session_id}", response_class=HTMLResponse)
async def session_detail(
    request: Request, session_id: int, db: Session = Depends(get_session)
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    qs = db.get(QuizSession, session_id)
    if not qs:
        raise HTTPException(status_code=404)
    quiz = db.get(Quiz, qs.quiz_id)
    results = db.exec(
        select(SessionResult)
        .where(SessionResult.session_id == session_id)
        .order_by(SessionResult.rank)
    ).all()
    stats = db.exec(
        select(QuestionStat)
        .where(QuestionStat.session_id == session_id)
        .order_by(QuestionStat.question_index)
    ).all()
    stats_data = []
    for stat in stats:
        try:
            answers_raw = json.loads(stat.answers_json)
        except Exception:
            answers_raw = []
        total_ans = stat.total_answers
        distribution = []
        if stat.question_type in ("mc", "tf", "ms", "poll") and isinstance(answers_raw, list):
            for i, cnt in enumerate(answers_raw):
                pct = round(cnt / total_ans * 100, 1) if total_ans > 0 else 0.0
                distribution.append({"label": chr(65 + i), "count": cnt, "pct": pct})
        elif stat.question_type == "wordcloud" and isinstance(answers_raw, dict):
            sorted_words = sorted(answers_raw.items(), key=lambda x: x[1], reverse=True)[:5]
            distribution = [{"word": w, "count": c} for w, c in sorted_words]
        stats_data.append({
            "question_index": stat.question_index,
            "question_text": stat.question_text[:60] + ("…" if len(stat.question_text) > 60 else ""),
            "question_type": stat.question_type,
            "correct_count": stat.correct_count,
            "total_answers": total_ans,
            "avg_time_seconds": stat.avg_time_seconds,
            "distribution": distribution,
        })
    return templates.TemplateResponse(
        "admin_session_detail.html",
        {
            "request": request,
            "session": qs,
            "quiz": quiz,
            "results": results,
            "stats": stats_data,
        },
    )


# ─── Image upload ─────────────────────────────────────────────────────────────
# TODO: migrate uploads to Supabase Storage for persistence in production

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


@app.post("/admin/upload-image")
async def upload_image(request: Request, file: UploadFile = File(...)):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401)
    if file.content_type not in ALLOWED_TYPES:
        return JSONResponse({"error": "Unsupported file type"}, status_code=400)
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        return JSONResponse({"error": "File too large (max 10 MB)"}, status_code=400)
    ext = (file.filename or "img").rsplit(".", 1)[-1].lower()
    filename = f"{uuid.uuid4()}.{ext}"
    (UPLOAD_DIR / filename).write_bytes(content)
    return JSONResponse({"url": f"/uploads/{filename}"})


# ─── CSV template & import ───────────────────────────────────────────────────

@app.get("/admin/csv-template")
async def csv_template(request: Request):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401)
    lines = [
        "question,type,option_1,option_2,option_3,option_4,option_5,option_6,correct,time_limit,points,image_url",
        "¿Cuánto es 2 + 2?,mc,3,4,5,6,,,B,20,100,",
        "¿El cielo es azul?,tf,Verdadero,Falso,,,,,A,10,200,",
        "Selecciona los números pares,ms,1,2,3,4,,,\"B,D\",30,200,",
        "¿Cuál es tu color favorito?,poll,Rojo,Azul,Verde,,,,,20,100,",
        "Ordena: menor a mayor,order,3,1,4,2,,,,30,300,",
    ]
    return Response(
        content="\n".join(lines),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=quizlab_template.csv"},
    )


@app.post("/admin/import-csv")
async def import_csv(request: Request, file: UploadFile = File(...)):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401)
    raw = await file.read()
    try:
        text_content = raw.decode("utf-8-sig")
    except Exception:
        return JSONResponse({"error": "Could not decode file as UTF-8"}, status_code=400)

    reader = csv.DictReader(io.StringIO(text_content))
    fieldnames = set(reader.fieldnames or [])

    # Detect format: new (option_1) vs old (option_a)
    is_new_format = "option_1" in fieldnames
    is_old_format = "option_a" in fieldnames

    if not is_new_format and not is_old_format:
        # Neither format — require at least question column
        if "question" not in fieldnames:
            return JSONResponse({"error": "Missing 'question' column"}, status_code=400)

    letter_to_idx = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5}
    questions, errors = [], []

    for i, row in enumerate(reader, start=2):
        q_text = (row.get("question") or "").strip()
        if not q_text:
            errors.append(f"Row {i}: empty question text, skipped")
            continue

        # Determine type
        q_type = (row.get("type") or "mc").strip().lower()
        if q_type not in ("mc", "tf", "ms", "poll", "order", "wordcloud"):
            q_type = "mc"

        # Collect options
        if is_new_format or "option_1" in fieldnames:
            opts = []
            for k in range(1, 7):
                v = (row.get(f"option_{k}") or "").strip()
                if v:
                    opts.append(v)
        else:
            # Old format: option_a .. option_d
            opts = []
            for letter in ("a", "b", "c", "d"):
                v = (row.get(f"option_{letter}") or "").strip()
                if v:
                    opts.append(v)

        if q_type == "tf" and len(opts) < 2:
            opts = ["Verdadero", "Falso"]

        if q_type == "wordcloud":
            opts = []

        if q_type not in ("poll", "order", "wordcloud") and len(opts) < 2:
            errors.append(f"Row {i}: need at least 2 options, skipped")
            continue

        # Parse correct field
        correct_str = (row.get("correct") or "").strip()
        correct_json = ""

        if q_type in ("mc", "tf"):
            correct_upper = correct_str.upper()
            if correct_upper in letter_to_idx:
                correct_json = str(letter_to_idx[correct_upper])
            elif correct_str.isdigit():
                correct_json = correct_str
            else:
                errors.append(f"Row {i}: invalid 'correct' value '{correct_str}', defaulting to A")
                correct_json = "0"

        elif q_type == "ms":
            # Expect "A,C" or "B,D" etc.
            parts = [p.strip().upper() for p in correct_str.replace(";", ",").split(",") if p.strip()]
            indices = []
            for p in parts:
                if p in letter_to_idx:
                    indices.append(letter_to_idx[p])
                elif p.isdigit():
                    indices.append(int(p))
            correct_json = json.dumps(sorted(set(indices))) if indices else "[]"

        elif q_type == "poll":
            correct_json = ""

        elif q_type == "wordcloud":
            correct_json = ""

        elif q_type == "order":
            # The input order is the correct order; correct_json = [0,1,...,n-1]
            correct_json = json.dumps(list(range(len(opts))))

        # Time limit
        try:
            tl = int(row.get("time_limit") or 20)
            tl = max(5, min(120, tl))
        except (ValueError, TypeError):
            tl = 20

        # Points
        try:
            default_pts = 0 if q_type == "wordcloud" else 100
            pts = int(row.get("points") or default_pts)
            pts = max(0, pts)
        except (ValueError, TypeError):
            pts = 0 if q_type == "wordcloud" else 100

        image_url = (row.get("image_url") or "").strip() or None

        questions.append({
            "text": q_text,
            "question_type": q_type,
            "options": opts,
            "correct_json": correct_json,
            "time_limit": tl,
            "points": pts,
            "image_url": image_url,
        })

    if errors and not questions:
        return JSONResponse({"error": "\n".join(errors)}, status_code=400)
    return JSONResponse({"questions": questions, "errors": errors})


# ─── Host ────────────────────────────────────────────────────────────────────

@app.get("/admin/host/{quiz_id}", response_class=HTMLResponse)
async def host_launch(
    request: Request, quiz_id: int, db: Session = Depends(get_session)
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    quiz = db.get(Quiz, quiz_id)
    if not quiz:
        raise HTTPException(status_code=404)
    quiz.last_played = datetime.utcnow()
    db.add(quiz)
    db.commit()
    return templates.TemplateResponse(
        "host.html",
        {"request": request, "quiz_id": quiz_id, "quiz_name": quiz.name},
    )


@app.post("/admin/quiz/end/{room_code}")
async def end_quiz(room_code: str, request: Request):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401)
    session = game_manager.get_session(room_code)
    leaderboard = session.get_leaderboard() if session else []
    questions_answered = (session.current_question_index + 1) if session else 0
    total_questions = len(session.questions) if session else 0
    await game_manager.end_session(room_code)
    return JSONResponse({
        "ok": True,
        "leaderboard": leaderboard,
        "questions_answered": questions_answered,
        "total_questions": total_questions,
    })


@app.get("/qr/{room_code}")
async def qr_code(room_code: str, request: Request):
    base = str(request.base_url).rstrip("/")
    url = f"{base}/play?room={room_code}"
    qr = qrcode.QRCode(version=1, box_size=10, border=4,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


# ─── Player ──────────────────────────────────────────────────────────────────

@app.get("/play", response_class=HTMLResponse)
async def player_page(request: Request, room: str = ""):
    return templates.TemplateResponse(
        "player.html", {"request": request, "room_code": room.upper()}
    )


# ─── WebSocket: Host ─────────────────────────────────────────────────────────

@app.websocket("/ws/host/{quiz_id}")
async def ws_host(websocket: WebSocket, quiz_id: int, db: Session = Depends(get_session)):
    await websocket.accept()
    quiz = db.get(Quiz, quiz_id)
    if not quiz:
        await websocket.close(code=4004)
        return

    questions = db.exec(
        select(Question).where(Question.quiz_id == quiz_id).order_by(Question.position)
    ).all()

    if not questions:
        await websocket.send_json({"type": "error", "message": "Quiz has no questions"})
        await websocket.close()
        return

    quiz_data = {
        "id": quiz.id,
        "name": quiz.name,
        "read_time": quiz.read_time,
        "questions": [
            {
                "text": q.text,
                "question_type": q.question_type,
                "options": json.loads(q.options_json) if q.options_json else [],
                "correct_json": q.correct_json,
                "time_limit": q.time_limit,
                "points": q.points,
                "image_url": q.image_url,
            }
            for q in questions
        ],
    }

    room_code = game_manager.create_session(quiz_data, websocket)

    await websocket.send_json(
        {
            "type": "session_created",
            "room_code": room_code,
            "quiz_name": quiz.name,
            "question_count": len(questions),
        }
    )

    try:
        while True:
            data = await websocket.receive_json()
            t = data.get("type")
            if t == "start_game":
                await game_manager.start_game(room_code)
            elif t == "reveal":
                await game_manager.reveal_answer(room_code)
            elif t == "next_question":
                await game_manager.next_question(room_code)
            elif t == "end_game":
                await game_manager.end_game(room_code)
            _s = game_manager.get_session(room_code)
            if _s and _s.state == "ended" and not _s.analytics_persisted:
                await _persist_session(room_code)
    except WebSocketDisconnect:
        session = game_manager.get_session(room_code)
        if session:
            session.host_websocket = None


# ─── WebSocket: Player ───────────────────────────────────────────────────────

@app.websocket("/ws/player")
async def ws_player(websocket: WebSocket):
    await websocket.accept()
    player_id: str | None = None
    room_code: str | None = None

    try:
        while True:
            data = await websocket.receive_json()
            t = data.get("type")

            if t == "join":
                rc = data.get("room_code", "").strip().upper()
                nickname = data.get("nickname", "").strip()

                session = game_manager.get_session(rc)
                if not session:
                    await websocket.send_json({"type": "error", "message": "Room not found"})
                    continue
                if session.state != "lobby":
                    await websocket.send_json({"type": "error", "message": "Game already in progress"})
                    continue
                if not nickname:
                    await websocket.send_json({"type": "error", "message": "Nickname is required"})
                    continue
                taken = any(
                    p.nickname.lower() == nickname.lower()
                    for p in session.players.values()
                    if p.connected
                )
                if taken:
                    await websocket.send_json({"type": "error", "message": "Nickname already taken"})
                    continue

                room_code = rc
                player_id = await game_manager.add_player(rc, nickname, websocket)
                player_list = session.connected_player_list()
                await websocket.send_json(
                    {
                        "type": "joined",
                        "player_id": player_id,
                        "player_list": player_list,
                        "room_code": rc,
                    }
                )

            elif t == "answer" and player_id and room_code:
                await game_manager.handle_answer(
                    room_code,
                    player_id,
                    int(data.get("question_id", -1)),
                    int(data.get("answer_index", -1)),
                    float(data.get("client_timestamp", 0)),
                )

            elif t == "selection" and player_id and room_code:
                await game_manager.handle_selection(
                    room_code,
                    player_id,
                    int(data.get("question_id", -1)),
                    data.get("selections", []),
                )

            elif t == "confirm" and player_id and room_code:
                await game_manager.handle_confirm(
                    room_code,
                    player_id,
                    int(data.get("question_id", -1)),
                )

            elif t == "order_update" and player_id and room_code:
                await game_manager.handle_order_update(
                    room_code,
                    player_id,
                    int(data.get("question_id", -1)),
                    data.get("ordering", []),
                )

            elif t == "wordcloud_answer" and player_id and room_code:
                try:
                    wc_qid = int(data.get("question_id", -1))
                except (ValueError, TypeError):
                    wc_qid = -1
                await game_manager.handle_wordcloud_answer(
                    room_code, player_id, wc_qid, data.get("text", "")
                )

            elif t == "reaction" and player_id and room_code:
                await game_manager.broadcast_reaction(
                    room_code, player_id, data.get("emoji", "")
                )

            elif t == "rejoin":
                rc = data.get("room_code", "").strip().upper()
                pid = data.get("player_id", "").strip()
                nickname = data.get("nickname", "").strip()
                result = await game_manager.rejoin_player(rc, pid, nickname, websocket)
                if result["ok"]:
                    room_code = rc
                    player_id = result["player_id"]
                    await websocket.send_json({
                        "type": "rejoined",
                        "state": result["state"],
                        "score": result["score"],
                        "question_index": result["question_index"],
                    })
                else:
                    await websocket.send_json({"type": "error", "message": result["reason"]})

    except WebSocketDisconnect:
        if player_id and room_code:
            await game_manager.remove_player(room_code, player_id)
    except Exception:
        if player_id and room_code:
            await game_manager.remove_player(room_code, player_id)
