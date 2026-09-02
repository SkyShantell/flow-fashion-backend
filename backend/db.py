from __future__ import annotations

import os
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import declarative_base, sessionmaker


def _database_url() -> str:
    raw = os.getenv("DATABASE_URL", "sqlite:///./flow_phase1.db").strip()
    if raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql+psycopg2://", 1)
    elif raw.startswith("postgresql://") and "+" not in raw.split("://", 1)[0]:
        raw = raw.replace("postgresql://", "postgresql+psycopg2://", 1)
    return raw


DATABASE_URL = _database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
    connect_args={"check_same_thread": False} if IS_SQLITE else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
Base = declarative_base()


def _add_missing_columns() -> None:
    """Tiny idempotent migration for V5 production-setting fields.

    Existing Railway databases were created before these columns existed;
    SQLAlchemy create_all does not alter an existing table.
    """
    if not IS_SQLITE:
        statements = [
            "ALTER TABLE batches ADD COLUMN IF NOT EXISTS mode VARCHAR(80) DEFAULT 'fashion_tryon'",
            "ALTER TABLE batches ADD COLUMN IF NOT EXISTS scene_pool JSON",
            "ALTER TABLE batches ADD COLUMN IF NOT EXISTS motion_pool JSON",
            "ALTER TABLE product_jobs ADD COLUMN IF NOT EXISTS scene_override VARCHAR(120)",
            "ALTER TABLE product_jobs ADD COLUMN IF NOT EXISTS motion_style_override VARCHAR(80)",
            "ALTER TABLE product_jobs ADD COLUMN IF NOT EXISTS editorial_shots JSON",
        ]
        with engine.begin() as conn:
            for sql in statements:
                conn.exec_driver_sql(sql)
        return

    inspector = inspect(engine)
    additions = {
        "batches": {
            "mode": "VARCHAR(80) DEFAULT 'fashion_tryon'",
            "scene_pool": "JSON",
            "motion_pool": "JSON",
        },
        "product_jobs": {
            "scene_override": "VARCHAR(120)",
            "motion_style_override": "VARCHAR(80)",
            "editorial_shots": "JSON",
        },
    }
    with engine.begin() as conn:
        for table, columns in additions.items():
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, col_type in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


def init_db() -> None:
    from backend import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


@contextmanager
def session_scope():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
