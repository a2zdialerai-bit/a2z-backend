from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlmodel import Session, SQLModel, create_engine

from config import settings


def _normalize_db_url(url: str) -> str:
    """Ensure postgres URLs use the psycopg3 driver prefix expected by SQLAlchemy."""
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _sqlite_connect_args(database_url: str) -> dict:
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


_db_url = _normalize_db_url(settings.database_url)

engine = create_engine(
    _db_url,
    echo=False,
    connect_args=_sqlite_connect_args(_db_url),
    pool_pre_ping=True,
)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    # Idempotent column migrations
    from sqlalchemy import text
    with engine.connect() as conn:
        for sql in [
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS preferred_voice_id VARCHAR(255)",
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS preferred_voice_gender VARCHAR(50)",
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS is_admin_workspace BOOLEAN DEFAULT FALSE",
            "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS is_admin_campaign BOOLEAN DEFAULT FALSE",
            "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS marketplace_listings_count INTEGER DEFAULT 0",
            "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS marketplace_revenue_cents INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_attempts INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP",
            "ALTER TABLE agentvoiceclone ADD COLUMN IF NOT EXISTS is_shared BOOLEAN DEFAULT FALSE",
            "ALTER TABLE agentvoiceclone ADD COLUMN IF NOT EXISTS display_name_public VARCHAR(255)",
            "ALTER TABLE agentvoiceclone ADD COLUMN IF NOT EXISTS royalty_rate_cents_per_min INTEGER DEFAULT 1",
            "ALTER TABLE agentvoiceclone ADD COLUMN IF NOT EXISTS total_minutes_used INTEGER DEFAULT 0",
            "ALTER TABLE agentvoiceclone ADD COLUMN IF NOT EXISTS total_royalties_earned_cents INTEGER DEFAULT 0",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS days_expired INTEGER",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_list_price VARCHAR(60)",
        ]:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()