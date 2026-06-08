import sys
import os
from sqlmodel import SQLModel, create_engine, Session, text

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./quizlab.db")

_is_postgres = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

if _is_postgres:
    engine = create_engine(DATABASE_URL, echo=False)
else:
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


def create_db_and_tables():
    from sqlalchemy import inspect as _inspect

    inspector = _inspect(engine)
    tables = inspector.get_table_names()

    if 'question' not in tables:
        # Fresh install — create everything
        SQLModel.metadata.create_all(engine)
        return

    # Check if schema is new (has options_json column)
    q_cols = [c['name'] for c in inspector.get_columns('question')]
    if 'options_json' in q_cols:
        # Schema is current, just ensure quiz table is up to date
        SQLModel.metadata.create_all(engine)  # no-op for existing tables
        _ensure_quiz_columns()
        return

    # Old schema detected — warn and try to proceed
    print("WARNING: QuizLab database schema is outdated.", file=sys.stderr)
    if _is_postgres:
        print("Drop and recreate the public schema in Supabase to apply the new schema.", file=sys.stderr)
    else:
        print("Delete quizlab.db and restart to apply the new schema.", file=sys.stderr)
    print("All existing quizzes will need to be re-created.", file=sys.stderr)
    SQLModel.metadata.create_all(engine)
    _ensure_quiz_columns()


def _ensure_quiz_columns():
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE quiz ADD COLUMN read_time INTEGER DEFAULT 5"))
            conn.commit()
        except Exception:
            pass  # column already exists


def get_session():
    with Session(engine) as session:
        yield session
