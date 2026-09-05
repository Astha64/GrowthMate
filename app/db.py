"""
Database engine/session setup for GrowthMate.

Reads DATABASE_URL from the environment (see .env.example / §11). Sets up a
SQLAlchemy engine and a scoped session factory. matches LLD §2 / file map §10.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./growthmate.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    """Create all tables. Idempotent — safe to call on every boot."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency yielding a session that is closed afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
