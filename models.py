from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class Quiz(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    course_tag: Optional[str] = None
    read_time: int = Field(default=5)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_played: Optional[datetime] = None


class Question(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    quiz_id: int = Field(foreign_key="quiz.id")
    position: int = Field(default=0)          # renamed from "order" (reserved word)
    text: str
    question_type: str = Field(default="mc")  # mc|tf|ms|poll|order|wordcloud
    options_json: str = Field(default='[]')   # JSON array of strings
    correct_json: str = Field(default='')     # see spec
    time_limit: int = Field(default=20)
    points: int = Field(default=100)
    image_url: Optional[str] = None


class QuizSession(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    quiz_id: int = Field(foreign_key="quiz.id")
    room_code: str
    played_at: datetime = Field(default_factory=datetime.utcnow)
    student_count: int = Field(default=0)


class SessionResult(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="quizsession.id")
    nickname: str
    score: int
    rank: int


class QuestionStat(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="quizsession.id")
    question_index: int
    question_text: str
    question_type: str
    correct_count: int = Field(default=0)
    total_answers: int = Field(default=0)
    avg_time_seconds: float = Field(default=0.0)
    answers_json: str = Field(default="[]")
