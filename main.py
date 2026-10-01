import asyncio
import csv
import hmac
import io
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

import qrcode
from PIL import Image, UnidentifiedImageError
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

import question_spec
from database import (
    StartupDatabaseError,
    create_db_and_tables,
    engine,
    get_session,
    _is_postgres,
)
from game_manager import (
    TEAM_NAMES,
    build_quiz_data,
    game_manager,
    is_nickname_allowed,
    _score_answer,
)
from models import (
    Assignment,
    AssignmentResult,
    ClassGroup,
    Question,
    Quiz,
    QuizSession,
    SessionResult,
    QuestionStat,
    Student,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("quizlab")

app = FastAPI(title="QuizLab")


def _auth_settings(is_postgres: bool, environ=os.environ):
    """(ADMIN_PASSWORD, SECRET_KEY). In production (Postgres DATABASE_URL)
    both must be set: a missing password used to fall back to a well-known
    default, and a missing SECRET_KEY to a public one that lets anyone forge
    the admin session cookie. Only local SQLite development gets fallbacks."""
    password = environ.get("ADMIN_PASSWORD", "")
    secret = environ.get("SECRET_KEY", "")
    if is_postgres:
        missing = [name for name, value in (("ADMIN_PASSWORD", password),
                                            ("SECRET_KEY", secret)) if not value]
        if missing:
            raise RuntimeError(
                f"{' and '.join(missing)} not set. DATABASE_URL points at "
                f"Postgres (production), where QuizLab refuses to start without "
                f"{'it' if len(missing) == 1 else 'them'}. Set "
                f"{'it' if len(missing) == 1 else 'them'} in the Render "
                f"dashboard (Environment).")
        return password, secret
    if not password:
        logger.warning("ADMIN_PASSWORD not set: local SQLite development, using "
                       "the password 'admin'. Never deploy like this.")
        password = "admin"
    if not secret:
        secret = "quizlab-local-dev-only"
    return password, secret


ADMIN_PASSWORD, SECRET_KEY = _auth_settings(_is_postgres)
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
async def on_startup():
    logger.info("startup: QuizLab starting")
    try:
        create_db_and_tables()
    except StartupDatabaseError as exc:
        logger.critical("startup FAILED: %s", exc)
        raise
    if not _supabase_enabled():
        logger.warning(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY not set: image uploads are saved to "
            "the local uploads/ folder, which does NOT persist on Render. "
            "Set both variables to store images in Supabase Storage."
        )
    # A restart (Render free-tier idle spin-down, a deploy, a crash) would
    # otherwise wipe every in-progress room. Restore whatever was still live
    # last time the process ran so students and the host can rejoin the same
    # room code instead of getting room_not_found.
    logger.info("startup: restoring live sessions")
    game_manager.rehydrate_live()
    asyncio.create_task(_cleanup_loop())
    asyncio.create_task(_heartbeat_loop())
    logger.info("startup: ready")


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
                class_id=data.get("class_id"),
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
                    student_id=entry.get("student_id"),
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
            logger.info("room %s: session persisted to history (session_id=%s, %d players)",
                        room_code, qs.id, data["student_count"])
    except Exception:
        logger.exception("room %s: failed to persist session to history", room_code)


def _rejoined_message(result: dict) -> dict:
    """Shape the rejoin_player result into the "rejoined" client message."""
    msg = {
        "type": "rejoined",
        "player_id": result["player_id"],
        "state": result["state"],
        "score": result["score"],
        "question_index": result["question_index"],
    }
    for key in ("question", "phase", "read_time_remaining",
                "answer_time_remaining", "already_answered", "answer_ack",
                "reveal", "team", "team_name", "streak", "player_list"):
        if key in result:
            msg[key] = result[key]
    return msg


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
    if hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
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
        {"request": request, "quiz": None, "questions_json": "[]", "read_time": 5,
         **_points_limits()},
    )


def _points_limits() -> dict:
    return {"points_min": question_spec.POINTS_MIN,
            "points_max": question_spec.POINTS_MAX}


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
         "read_time": quiz.read_time, **_points_limits()},
    )


def _validate_points(raw):
    """(points, None) if raw is a whole number within question_spec's limits,
    else (None, error message)."""
    lo, hi = question_spec.POINTS_MIN, question_spec.POINTS_MAX
    msg = f"points must be a whole number from {lo} to {hi}."
    if isinstance(raw, bool):
        return None, msg
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    if isinstance(raw, str):
        try:
            raw = int(raw.strip())
        except ValueError:
            return None, msg
    if not isinstance(raw, int) or not lo <= raw <= hi:
        return None, msg
    return raw, None


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

    scoring_mode = data.get("scoring_mode", "speed")
    if scoring_mode not in ("speed", "accuracy"):
        scoring_mode = "speed"
    streak_bonus = bool(data.get("streak_bonus", False))

    # Validate points before touching the DB: the update path deletes the old
    # questions first, so a bad value must not leave the quiz half-saved.
    points_list = []
    for i, qd in enumerate(questions_data):
        pts, err = _validate_points(qd.get("points", 100))
        if err:
            return JSONResponse({"error": f"Question {i + 1}: {err}"},
                                status_code=400)
        qtype = question_spec.QUESTION_TYPES.get(qd.get("question_type", "mc"))
        if qtype and qtype.fixed_points is not None:
            pts = qtype.fixed_points
        points_list.append(pts)

    if quiz_id:
        quiz = db.get(Quiz, int(quiz_id))
        if not quiz:
            raise HTTPException(status_code=404)
        quiz.name = name
        quiz.course_tag = course_tag
        quiz.read_time = read_time
        quiz.scoring_mode = scoring_mode
        quiz.streak_bonus = streak_bonus
        for q in db.exec(select(Question).where(Question.quiz_id == quiz.id)).all():
            db.delete(q)
        db.commit()
    else:
        quiz = Quiz(name=name, course_tag=course_tag, read_time=read_time,
                    scoring_mode=scoring_mode, streak_bonus=streak_bonus)
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
            points=points_list[i],
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


# ─── Classes & students ──────────────────────────────────────────────────────

@app.get("/admin/classes", response_class=HTMLResponse)
async def classes_page(request: Request, db: Session = Depends(get_session)):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    classes = db.exec(select(ClassGroup).order_by(ClassGroup.name)).all()
    classes_data = []
    for cg in classes:
        students = db.exec(select(Student).where(Student.class_id == cg.id)).all()
        sessions = db.exec(select(QuizSession).where(QuizSession.class_id == cg.id)).all()
        classes_data.append({
            "id": cg.id, "name": cg.name,
            "student_count": len(students),
            "session_count": len(sessions),
        })
    return templates.TemplateResponse(
        "admin_classes.html", {"request": request, "classes": classes_data}
    )


@app.post("/admin/classes")
async def create_class(request: Request, name: str = Form(...),
                       db: Session = Depends(get_session)):
    _require_admin(request)
    name = name.strip()
    if name:
        db.add(ClassGroup(name=name))
        db.commit()
    return RedirectResponse("/admin/classes", status_code=303)


@app.post("/admin/classes/{class_id}/delete")
async def delete_class(request: Request, class_id: int,
                       db: Session = Depends(get_session)):
    _require_admin(request)
    cg = db.get(ClassGroup, class_id)
    if cg:
        for s in db.exec(select(Student).where(Student.class_id == class_id)).all():
            db.delete(s)
        for qs in db.exec(select(QuizSession).where(QuizSession.class_id == class_id)).all():
            qs.class_id = None
            db.add(qs)
        db.delete(cg)
        db.commit()
    return RedirectResponse("/admin/classes", status_code=303)


@app.get("/admin/class/{class_id}", response_class=HTMLResponse)
async def class_detail(request: Request, class_id: int,
                       db: Session = Depends(get_session)):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    cg = db.get(ClassGroup, class_id)
    if not cg:
        raise HTTPException(status_code=404)
    students = db.exec(
        select(Student).where(Student.class_id == class_id).order_by(Student.name)
    ).all()
    students_data = []
    for s in students:
        results = db.exec(
            select(SessionResult).where(SessionResult.student_id == s.id)
        ).all()
        scores = [r.score for r in results]
        students_data.append({
            "id": s.id, "name": s.name,
            "sessions_played": len(results),
            "avg_score": round(sum(scores) / len(scores)) if scores else None,
            "best_score": max(scores) if scores else None,
        })
    return templates.TemplateResponse(
        "admin_class_detail.html",
        {"request": request, "class_group": cg, "students": students_data},
    )


@app.post("/admin/class/{class_id}/students")
async def add_students(request: Request, class_id: int, names: str = Form(...),
                       db: Session = Depends(get_session)):
    _require_admin(request)
    cg = db.get(ClassGroup, class_id)
    if not cg:
        raise HTTPException(status_code=404)
    existing = {
        s.name.lower()
        for s in db.exec(select(Student).where(Student.class_id == class_id)).all()
    }
    for line in names.splitlines():
        name = line.strip()[:40]
        if name and name.lower() not in existing:
            db.add(Student(class_id=class_id, name=name))
            existing.add(name.lower())
    db.commit()
    return RedirectResponse(f"/admin/class/{class_id}", status_code=303)


@app.post("/admin/student/{student_id}/delete")
async def delete_student(request: Request, student_id: int,
                         db: Session = Depends(get_session)):
    _require_admin(request)
    s = db.get(Student, student_id)
    class_id = s.class_id if s else None
    if s:
        db.delete(s)
        db.commit()
    return RedirectResponse(f"/admin/class/{class_id}" if class_id else "/admin/classes",
                            status_code=303)


@app.get("/admin/student/{student_id}", response_class=HTMLResponse)
async def student_progress(request: Request, student_id: int,
                           db: Session = Depends(get_session)):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    student = db.get(Student, student_id)
    if not student:
        raise HTTPException(status_code=404)
    cg = db.get(ClassGroup, student.class_id)
    results = db.exec(
        select(SessionResult).where(SessionResult.student_id == student_id)
    ).all()
    history = []
    for r in results:
        qs = db.get(QuizSession, r.session_id)
        if not qs:
            continue
        quiz = db.get(Quiz, qs.quiz_id)
        history.append({
            "played_at": qs.played_at,
            "quiz_name": quiz.name if quiz else "—",
            "score": r.score,
            "rank": r.rank,
            "student_count": qs.student_count,
            "session_id": qs.id,
        })
    history.sort(key=lambda h: h["played_at"])
    scores = [h["score"] for h in history]
    max_score = max(scores) if scores else 1
    for h in history:
        h["bar_pct"] = round(h["score"] / max_score * 100) if max_score > 0 else 0
    summary = {
        "sessions": len(history),
        "avg_score": round(sum(scores) / len(scores)) if scores else 0,
        "best_score": max(scores) if scores else 0,
        "avg_rank": round(sum(h["rank"] for h in history) / len(history), 1) if history else 0,
    }
    return templates.TemplateResponse(
        "admin_student.html",
        {"request": request, "student": student, "class_group": cg,
         "history": history, "summary": summary},
    )


@app.get("/admin/api/classes")
async def api_classes(request: Request, db: Session = Depends(get_session)):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401)
    classes = db.exec(select(ClassGroup).order_by(ClassGroup.name)).all()
    return JSONResponse([
        {
            "id": cg.id,
            "name": cg.name,
            "student_count": len(db.exec(
                select(Student).where(Student.class_id == cg.id)).all()),
        }
        for cg in classes
    ])


# ─── CSV export ──────────────────────────────────────────────────────────────

@app.get("/admin/session/{session_id}/export.csv")
async def export_session_csv(request: Request, session_id: int,
                             db: Session = Depends(get_session)):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401)
    qs = db.get(QuizSession, session_id)
    if not qs:
        raise HTTPException(status_code=404)
    quiz = db.get(Quiz, qs.quiz_id)
    results = db.exec(
        select(SessionResult).where(SessionResult.session_id == session_id)
        .order_by(SessionResult.rank)
    ).all()
    stats = db.exec(
        select(QuestionStat).where(QuestionStat.session_id == session_id)
        .order_by(QuestionStat.question_index)
    ).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Quiz", quiz.name if quiz else "", "Sala", qs.room_code,
                     "Fecha", qs.played_at.strftime("%Y-%m-%d %H:%M")])
    writer.writerow([])
    writer.writerow(["rank", "nombre", "puntos"])
    for r in results:
        writer.writerow([r.rank, r.nickname, r.score])
    writer.writerow([])
    writer.writerow(["pregunta", "tipo", "respondieron", "correctos",
                     "pct_correcto", "tiempo_promedio_s"])
    for s in stats:
        pct = round(s.correct_count / s.total_answers * 100) if s.total_answers else ""
        writer.writerow([s.question_text, s.question_type, s.total_answers,
                         s.correct_count, pct, s.avg_time_seconds])
    filename = f"quizlab_sesion_{session_id}.csv"
    return Response(
        content="\ufeff" + buf.getvalue(),  # BOM so Excel opens UTF-8 correctly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ─── Assignments (async / homework mode) ─────────────────────────────────────

def _generate_assignment_code(db: Session) -> str:
    import random as _random
    import string as _string
    while True:
        code = "".join(_random.choices(_string.ascii_uppercase + _string.digits, k=8))
        if not db.exec(select(Assignment).where(Assignment.code == code)).first():
            return code


@app.get("/admin/quiz/{quiz_id}/assignments", response_class=HTMLResponse)
async def quiz_assignments(request: Request, quiz_id: int,
                           db: Session = Depends(get_session)):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    quiz = db.get(Quiz, quiz_id)
    if not quiz:
        raise HTTPException(status_code=404)
    assignments = db.exec(
        select(Assignment).where(Assignment.quiz_id == quiz_id)
        .order_by(Assignment.created_at.desc())
    ).all()
    rows = []
    for a in assignments:
        subs = db.exec(select(AssignmentResult)
                       .where(AssignmentResult.assignment_id == a.id)).all()
        cg = db.get(ClassGroup, a.class_id) if a.class_id else None
        rows.append({
            "id": a.id, "code": a.code, "deadline": a.deadline,
            "created_at": a.created_at,
            "class_name": cg.name if cg else None,
            "submissions": len(subs),
            "closed": bool(a.deadline and a.deadline < datetime.utcnow()),
        })
    classes = db.exec(select(ClassGroup).order_by(ClassGroup.name)).all()
    return templates.TemplateResponse(
        "admin_assignments.html",
        {"request": request, "quiz": quiz, "assignments": rows, "classes": classes},
    )


@app.post("/admin/quiz/{quiz_id}/assignments")
async def create_assignment(request: Request, quiz_id: int,
                            deadline: str = Form(""),
                            class_id: str = Form(""),
                            db: Session = Depends(get_session)):
    _require_admin(request)
    quiz = db.get(Quiz, quiz_id)
    if not quiz:
        raise HTTPException(status_code=404)
    dl = None
    if deadline.strip():
        try:
            dl = datetime.fromisoformat(deadline.strip())
        except ValueError:
            dl = None
    cid = int(class_id) if class_id.strip().isdigit() else None
    a = Assignment(quiz_id=quiz_id, class_id=cid, deadline=dl,
                   code=_generate_assignment_code(db))
    db.add(a)
    db.commit()
    logger.info("assignment %s created (quiz_id=%s, class_id=%s, deadline=%s)",
                a.code, quiz_id, cid, dl)
    return RedirectResponse(f"/admin/quiz/{quiz_id}/assignments", status_code=303)


@app.post("/admin/assignment/{assignment_id}/delete")
async def delete_assignment(request: Request, assignment_id: int,
                            db: Session = Depends(get_session)):
    _require_admin(request)
    a = db.get(Assignment, assignment_id)
    quiz_id = a.quiz_id if a else None
    if a:
        for r in db.exec(select(AssignmentResult)
                         .where(AssignmentResult.assignment_id == assignment_id)).all():
            db.delete(r)
        db.delete(a)
        db.commit()
    return RedirectResponse(
        f"/admin/quiz/{quiz_id}/assignments" if quiz_id else "/admin/dashboard",
        status_code=303)


@app.get("/admin/assignment/{assignment_id}", response_class=HTMLResponse)
async def assignment_detail(request: Request, assignment_id: int,
                            db: Session = Depends(get_session)):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/login")
    a = db.get(Assignment, assignment_id)
    if not a:
        raise HTTPException(status_code=404)
    quiz = db.get(Quiz, a.quiz_id)
    cg = db.get(ClassGroup, a.class_id) if a.class_id else None
    subs = db.exec(
        select(AssignmentResult).where(AssignmentResult.assignment_id == assignment_id)
        .order_by(AssignmentResult.score.desc())
    ).all()
    return templates.TemplateResponse(
        "admin_assignment_detail.html",
        {"request": request, "assignment": a, "quiz": quiz, "class_group": cg,
         "submissions": subs,
         "closed": bool(a.deadline and a.deadline < datetime.utcnow())},
    )


@app.get("/assignment/{code}", response_class=HTMLResponse)
async def assignment_page(request: Request, code: str):
    return templates.TemplateResponse(
        "assignment.html", {"request": request, "code": code.upper()}
    )


def _get_open_assignment(code: str, db: Session):
    a = db.exec(select(Assignment).where(Assignment.code == code.upper())).first()
    if not a:
        return None, "not_found"
    if a.deadline and a.deadline < datetime.utcnow():
        return a, "closed"
    return a, None


@app.get("/api/assignment/{code}")
async def api_assignment_info(code: str, db: Session = Depends(get_session)):
    a, err = _get_open_assignment(code, db)
    if err == "not_found":
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    quiz = db.get(Quiz, a.quiz_id)
    questions = db.exec(
        select(Question).where(Question.quiz_id == a.quiz_id)).all()
    submitted = {
        r.nickname.lower()
        for r in db.exec(select(AssignmentResult)
                         .where(AssignmentResult.assignment_id == a.id)).all()
    }
    roster_names = None
    if a.class_id:
        roster_names = [
            s.name for s in db.exec(
                select(Student).where(Student.class_id == a.class_id)).all()
            if s.name.lower() not in submitted
        ]
    return JSONResponse({
        "ok": True,
        "quiz_name": quiz.name if quiz else "",
        "question_count": len(questions),
        "deadline": a.deadline.isoformat() if a.deadline else None,
        "closed": err == "closed",
        "has_roster": a.class_id is not None,
        "roster_names": roster_names,
    })


@app.get("/api/assignment/{code}/questions")
async def api_assignment_questions(code: str, db: Session = Depends(get_session)):
    a, err = _get_open_assignment(code, db)
    if err:
        return JSONResponse({"ok": False, "error": err},
                            status_code=404 if err == "not_found" else 403)
    questions = db.exec(
        select(Question).where(Question.quiz_id == a.quiz_id)
        .order_by(Question.position)
    ).all()
    return JSONResponse({
        "ok": True,
        "questions": [
            {
                "index": i,
                "text": q.text,
                "question_type": q.question_type,
                "options": json.loads(q.options_json) if q.options_json else [],
                "image_url": q.image_url or "",
                "points": q.points,
            }
            for i, q in enumerate(questions)
        ],
    })


@app.post("/api/assignment/{code}/submit")
async def api_assignment_submit(code: str, request: Request,
                                db: Session = Depends(get_session)):
    a, err = _get_open_assignment(code, db)
    if err:
        return JSONResponse({"ok": False, "error": err},
                            status_code=404 if err == "not_found" else 403)
    data = await request.json()
    nickname = str(data.get("nickname", "")).strip()[:40]
    answers = data.get("answers", [])
    if not nickname:
        return JSONResponse({"ok": False, "error": "nickname_required"}, status_code=400)
    if not is_nickname_allowed(nickname):
        return JSONResponse({"ok": False, "error": "nickname_not_allowed"}, status_code=400)

    student_id = None
    if a.class_id:
        student = next(
            (s for s in db.exec(
                select(Student).where(Student.class_id == a.class_id)).all()
             if s.name.lower() == nickname.lower()),
            None,
        )
        if not student:
            return JSONResponse({"ok": False, "error": "pick_from_roster"}, status_code=400)
        student_id = student.id

    already = next(
        (r for r in db.exec(select(AssignmentResult)
                            .where(AssignmentResult.assignment_id == a.id)).all()
         if r.nickname.lower() == nickname.lower()),
        None,
    )
    if already:
        return JSONResponse({"ok": False, "error": "already_submitted"}, status_code=409)

    questions = db.exec(
        select(Question).where(Question.quiz_id == a.quiz_id)
        .order_by(Question.position)
    ).all()

    # Homework is self-paced: accuracy scoring, no speed pressure
    score = 0
    correct_count = 0
    review = []
    for i, q in enumerate(questions):
        ans = answers[i] if i < len(answers) else None
        options = json.loads(q.options_json) if q.options_json else []
        if q.question_type == "order":
            # Client submits original-array indices in chosen order;
            # the correct ordering is simply 0..n-1
            correct_json = json.dumps(list(range(len(options))))
        else:
            correct_json = q.correct_json
        pts, is_correct = _score_answer(
            q.question_type, correct_json,
            ans if ans is not None else -1,
            0.0, q.time_limit, q.points, "accuracy")
        score += pts
        scored = q.question_type not in ("poll", "wordcloud")
        if is_correct and scored:
            correct_count += 1

        your_display = "—"
        correct_display = ""
        if q.question_type in ("mc", "tf", "poll"):
            if isinstance(ans, int) and 0 <= ans < len(options):
                your_display = str(options[ans])
            if q.question_type != "poll":
                try:
                    ci = int(q.correct_json) if q.correct_json != "" else 0
                except (ValueError, TypeError):
                    ci = 0
                if 0 <= ci < len(options):
                    correct_display = str(options[ci])
        elif q.question_type == "ms":
            if isinstance(ans, list):
                your_display = ", ".join(
                    str(options[j]) for j in ans
                    if isinstance(j, int) and 0 <= j < len(options)) or "—"
            try:
                correct_display = ", ".join(
                    str(options[j]) for j in json.loads(q.correct_json)
                    if isinstance(j, int) and 0 <= j < len(options))
            except Exception:
                correct_display = ""
        elif q.question_type == "order":
            if isinstance(ans, list):
                your_display = " → ".join(
                    str(options[j]) for j in ans
                    if isinstance(j, int) and 0 <= j < len(options)) or "—"
            correct_display = " → ".join(str(o) for o in options)
        elif q.question_type == "wordcloud":
            if isinstance(ans, str) and ans.strip():
                your_display = ans.strip()[:50]

        review.append({
            "index": i,
            "text": q.text,
            "question_type": q.question_type,
            "your_answer": your_display,
            "correct_answer": correct_display,
            "answered": ans is not None and ans != -1 and ans != "",
            "correct": is_correct,
            "points": pts,
            "scored": scored,
        })

    db.add(AssignmentResult(
        assignment_id=a.id,
        student_id=student_id,
        nickname=nickname,
        score=score,
        correct_count=correct_count,
        total_questions=len(questions),
        answers_json=json.dumps(review, ensure_ascii=False),
    ))
    db.commit()
    logger.info("assignment %s: submission by %r (score=%d, %d/%d correct)",
                a.code, nickname, score, correct_count, len(questions))
    return JSONResponse({
        "ok": True,
        "score": score,
        "correct_count": correct_count,
        "total": len(questions),
        "review": review,
    })


# ─── Image upload ─────────────────────────────────────────────────────────────
# Files go to a public Supabase Storage bucket when SUPABASE_URL and
# SUPABASE_SERVICE_KEY are set (server-side only, never sent to the browser);
# otherwise they fall back to the local uploads/ folder for development.

MAX_IMAGE_BYTES = 10 * 1024 * 1024
SUPABASE_BUCKET = "question-images"

# Pillow format name -> (MIME type, file extension)
_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "GIF": ("image/gif", "gif"),
    "WEBP": ("image/webp", "webp"),
}


def _supabase_enabled() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY"))


def _detect_image(content: bytes):
    """Return (mime, ext) from the actual bytes, or None if the content is not
    a decodable jpeg/png/gif/webp. The declared content type and the original
    filename are never trusted."""
    try:
        with Image.open(io.BytesIO(content)) as img:
            fmt = img.format
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError,
            Image.DecompressionBombError):
        return None
    return _IMAGE_FORMATS.get(fmt)


def _upload_to_supabase(filename: str, content: bytes, mime: str) -> str:
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    req = urllib.request.Request(
        f"{base}/storage/v1/object/{SUPABASE_BUCKET}/{filename}",
        data=content,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "apikey": key,
            "Content-Type": mime,
            "Cache-Control": "max-age=31536000",
        },
    )
    with urllib.request.urlopen(req, timeout=30):
        pass
    return f"{base}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"


@app.post("/admin/upload-image")
async def upload_image(request: Request, file: UploadFile = File(...)):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401)
    content = await file.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        return JSONResponse({"error": "File too large (max 10 MB)"}, status_code=400)
    detected = _detect_image(content)
    if not detected:
        return JSONResponse(
            {"error": "Unsupported or corrupt image (use JPG, PNG, GIF or WebP)"},
            status_code=400)
    mime, ext = detected
    filename = f"{uuid.uuid4()}.{ext}"

    if _supabase_enabled():
        try:
            url = await asyncio.to_thread(_upload_to_supabase, filename, content, mime)
        except (urllib.error.URLError, OSError) as exc:
            logger.error("Supabase upload failed: %s", exc)
            return JSONResponse({"error": "Image storage unavailable, try again"},
                                status_code=502)
        return JSONResponse({"url": url})

    (UPLOAD_DIR / filename).write_bytes(content)
    return JSONResponse({"url": f"/uploads/{filename}"})


# ─── CSV template, AI prompt & import ───────────────────────────────────────
# All three are generated from question_spec.py (single source of truth for
# columns, question types and limits).

@app.get("/admin/csv-template")
async def csv_template(request: Request):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401)
    return Response(
        content=question_spec.build_template_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=quizlab_template.csv"},
    )


@app.get("/admin/ai-prompt")
async def ai_prompt(request: Request):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401)
    return Response(
        content=question_spec.build_ai_prompt().encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=quizlab_ai_prompt.md"},
    )


@app.post("/admin/import-csv")
async def import_csv(request: Request, file: UploadFile = File(...)):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401)
    result = question_spec.parse_csv(await file.read())
    return JSONResponse(result.as_dict())


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

    quiz_data = build_quiz_data(quiz, questions)

    # The first client message declares intent: "create_session" starts a new
    # room (the old implicit behavior), "host_rejoin" re-attaches to a live one.
    room_code: str | None = None

    try:
        while True:
            data = await websocket.receive_json()
            t = data.get("type")

            if t == "create_session" and room_code is None:
                room_code = game_manager.create_session(quiz_data, websocket)
                logger.info("room %s created (quiz_id=%s, %r, %d questions)",
                            room_code, quiz.id, quiz.name, len(questions))
                await websocket.send_json({
                    "type": "session_created",
                    "room_code": room_code,
                    "quiz_name": quiz.name,
                    "question_count": len(questions),
                })

            elif t == "host_rejoin" and room_code is None:
                rc = data.get("room_code", "").strip().upper()
                result = await game_manager.rejoin_host(rc, websocket)
                if result.get("ok"):
                    room_code = result["room_code"]
                    del result["ok"]
                    result["type"] = "host_rejoined"
                    await websocket.send_json(result)
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": result.get("reason", "room_not_found"),
                    })

            elif t == "start_game" and room_code:
                await game_manager.start_game(room_code)
            elif t == "reveal" and room_code:
                await game_manager.reveal_answer(
                    room_code, at_deadline=bool(data.get("at_deadline")))
            elif t == "next_question" and room_code:
                await game_manager.next_question(room_code)
            elif t == "end_game" and room_code:
                await game_manager.end_game(room_code)

            elif t == "set_teams" and room_code:
                await game_manager.set_teams(room_code, data.get("count", 0))

            elif t == "lock_room" and room_code:
                locked = game_manager.set_locked(room_code, data.get("locked", False))
                await websocket.send_json({"type": "room_locked", "locked": locked})

            elif t == "kick_player" and room_code:
                await game_manager.kick_player(room_code, data.get("player_id", ""))

            elif t == "set_class" and room_code:
                class_id = data.get("class_id")
                roster = None
                class_name = None
                if class_id:
                    cg = db.get(ClassGroup, int(class_id))
                    if cg:
                        class_name = cg.name
                        roster = [
                            {"student_id": s.id, "name": s.name}
                            for s in db.exec(
                                select(Student).where(Student.class_id == cg.id)
                            ).all()
                        ]
                    else:
                        class_id = None
                ok = game_manager.set_class(
                    room_code, int(class_id) if class_id else None,
                    class_name, roster)
                await websocket.send_json({
                    "type": "class_set",
                    "ok": bool(ok),
                    "class_id": class_id,
                    "class_name": class_name,
                    "roster_size": len(roster or []),
                })

            if room_code:
                _s = game_manager.get_session(room_code)
                if _s and _s.state == "ended" and not _s.analytics_persisted:
                    await _persist_session(room_code)
    except WebSocketDisconnect:
        if room_code:
            session = game_manager.get_session(room_code)
            # Only detach if this socket is still the active one — a stale
            # close after a host rejoin must not sever the new connection.
            if session and session.host_websocket is websocket:
                session.host_websocket = None
                logger.info("room %s: host websocket disconnected", room_code)


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

            # Any inbound message proves the socket is alive end-to-end, not
            # just that our last outbound send didn't throw. See
            # PLAYER_TIMEOUT_SECONDS in game_manager.py.
            if player_id and room_code:
                game_manager.touch_player(room_code, player_id)

            if t == "room_info":
                # Pre-join probe: lets the client show the roster picker (or a
                # locked-room message) before attempting to join
                rc = data.get("room_code", "").strip().upper()
                session = game_manager.get_session(rc)
                if not session or session.state == "ended":
                    await websocket.send_json({"type": "room_info", "exists": False})
                    continue
                await websocket.send_json({
                    "type": "room_info",
                    "exists": True,
                    "state": session.state,
                    "locked": session.locked,
                    "has_roster": bool(session.roster),
                    "roster_names": session.available_roster_names(),
                    "class_name": session.class_name,
                })

            elif t == "join":
                rc = data.get("room_code", "").strip().upper()
                nickname = data.get("nickname", "").strip()

                session = game_manager.get_session(rc)
                if not session:
                    await websocket.send_json({"type": "error", "message": "Room not found"})
                    continue
                if not nickname:
                    await websocket.send_json({"type": "error", "message": "Nickname is required"})
                    continue
                # A student whose browser lost its stored identity (tab
                # killed, new tab from a re-scanned QR) comes back through
                # "join" with the same name. If that seat exists and its
                # socket is gone, re-attach them instead of refusing —
                # this works mid-game too, where a fresh join is rejected.
                orphan = next(
                    (p for p in session.players.values()
                     if p.nickname.lower() == nickname.lower()
                     and not p.connected),
                    None,
                )
                if orphan is not None and session.state != "ended":
                    result = await game_manager.rejoin_player(
                        rc, orphan.player_id, nickname, websocket)
                    if result["ok"]:
                        room_code = rc
                        player_id = result["player_id"]
                        await websocket.send_json(_rejoined_message(result))
                        await game_manager.notify_player_list(session)
                        continue
                if session.state != "lobby":
                    await websocket.send_json({"type": "error", "message": "Game already in progress"})
                    continue
                if session.locked:
                    await websocket.send_json({"type": "error", "message": "room_locked"})
                    continue
                if not is_nickname_allowed(nickname):
                    await websocket.send_json({"type": "error", "message": "nickname_not_allowed"})
                    continue
                if session.roster and nickname.lower() not in (
                    r["name"].lower() for r in session.roster
                ):
                    await websocket.send_json({
                        "type": "error",
                        "message": "pick_from_roster",
                        "roster_names": session.available_roster_names(),
                    })
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
                player = session.players.get(player_id) if player_id else None
                player_list = session.player_list()
                joined_msg = {
                    "type": "joined",
                    "player_id": player_id,
                    "player_list": player_list,
                    "room_code": rc,
                    "team": player.team if player else None,
                    "team_count": session.team_count,
                }
                if player and player.team is not None:
                    joined_msg["team_name"] = TEAM_NAMES[player.team % len(TEAM_NAMES)]
                await websocket.send_json(joined_msg)

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
                    await websocket.send_json(_rejoined_message(result))
                    # Host sees the chip light up again; phones in the lobby
                    # get the refreshed list.
                    session = game_manager.get_session(rc)
                    if session:
                        await game_manager.notify_player_list(session)
                else:
                    await websocket.send_json({"type": "error", "message": result["reason"]})

            elif t == "ping":
                # Client-side liveness probe (sent after a phone wakes up):
                # any reply proves the socket is still alive end-to-end.
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        if player_id and room_code:
            await game_manager.remove_player(room_code, player_id, websocket)
    except Exception:
        logger.exception("room %s: player websocket error (player_id=%s)",
                         room_code, player_id)
        if player_id and room_code:
            await game_manager.remove_player(room_code, player_id, websocket)
