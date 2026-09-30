import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.db import Base, SessionLocal, engine
from app.routers import auth, pages
from app.seed import ensure_seed_data
from sqlalchemy import text


def _migrate_schema() -> None:
    """轻量迁移：给已存在的 dip_lots 补登记人/登记时间列（create_all 不补列）。

    历史浸染笔回填给 admin，登记时间取浸染时间；回填后这些笔对染缸工即「他人笔」。
    """
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE dip_lots ADD COLUMN IF NOT EXISTS created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL"))
        conn.execute(text("ALTER TABLE dip_lots ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ"))
        conn.execute(
            text(
                "UPDATE dip_lots SET created_by_id = u.id "
                "FROM users u WHERE dip_lots.created_by_id IS NULL AND u.is_superuser = true"
            )
        )
        conn.execute(
            text('UPDATE dip_lots SET created_at = "dippedAt" WHERE dip_lots.created_at IS NULL')
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _migrate_schema()
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
