import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from starlette.middleware.sessions import SessionMiddleware

from app.db import Base, SessionLocal, engine
from app.routers import auth, pages
from app.seed import ensure_seed_data


def _ensure_schema() -> None:
    """create_all 建新表；对已存在的旧库幂等补齐后续新增列。"""
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    lot_cols = {col["name"] for col in inspector.get_columns("dip_lots")}
    if "created_by_id" not in lot_cols:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE dip_lots ADD COLUMN created_by_id INTEGER "
                    "REFERENCES users(id) ON DELETE SET NULL"
                )
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_schema()
    db = SessionLocal()
    try:
        ensure_seed_data(db)
    finally:
        db.close()
    yield


app = FastAPI(title="IndigoVat 染缸还原台", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "dev-indigovat-session-secret"),
    session_cookie="indigovat_session",
    same_site="lax",
    https_only=False,
)

static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(auth.router)
app.include_router(pages.router)
