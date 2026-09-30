from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import DipLot, Vat, Workshop
from app.services import delete_rules
from app.services.delete_rules import (
    TARGET_LOT,
    TARGET_VAT,
    TARGET_WORKSHOP,
    DeleteNotFoundError,
    DeleteRuleError,
)
from app.services.vat_rules import VatRuleError, validate_vat_status_change

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _tojson(value):
    return markupsafe.Markup(json.dumps(value, ensure_ascii=False))


templates.env.filters["tojson"] = _tojson

STATUS_LABELS = {
    Vat.STATUS_IDLE: "闲置",
    Vat.STATUS_REDUCING: "还原中",
    Vat.STATUS_READY: "可染色",
}


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _need_login(request: Request, db: Session):
    return get_current_user(request, db)


def _spark_points(lots: list[DipLot], width: int = 72, height: int = 28) -> list[dict]:
    """把 redox 序列压成 sparkline 坐标（无有效读数则空）。"""
    vals = [float(l.redoxMv) for l in lots if l.redoxMv is not None]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = 0 if n == 1 else round(i * (width - 1) / (n - 1), 2)
        y = round(height - 1 - ((v - lo) / span) * (height - 1), 2)
        pts.append({"x": x, "y": y})
    return pts


def _vat_payload(vat: Vat) -> dict:
    lots = sorted(vat.lots, key=lambda x: (x.dippedAt, x.id))
    chronological = lots
    latest = lots[-1] if lots else None
    recent = list(reversed(lots[-8:]))  # 展开区展示近几笔
    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "volumeL": float(vat.volumeL),
        "status": vat.status,
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "lastRedox": float(latest.redoxMv) if latest and latest.redoxMv is not None else None,
        "lastMeters": float(latest.clothMeters) if latest else None,
        "lastDippedAt": latest.dippedAt.strftime("%Y-%m-%d %H:%M") if latest else None,
        # 缸下浸染笔总数：删除染缸前的「是否为空」由前端读矩阵 + 该计数判定
        "lotCount": len(lots),
        "spark": _spark_points(chronological),
        "recentLots": [
            {
                "id": l.id,
                "dippedAt": l.dippedAt.strftime("%Y-%m-%d %H:%M"),
                # 带时区的 ISO 时刻，前端据此换算本地「当日」
                "dippedIso": l.dippedAt.isoformat(),
                "clothMeters": float(l.clothMeters),
                "redoxMv": float(l.redoxMv) if l.redoxMv is not None else None,
                "createdById": l.created_by_id,
            }
            for l in recent
        ],
    }


def _workshop_payload(workshop: Workshop, vat_counts: dict[int, int]) -> dict:
    return {
        "id": workshop.id,
        "name": workshop.name,
        "region": workshop.region,
        "vatCount": vat_counts.get(workshop.id, 0),
    }


def _bay_context(
    request: Request,
    db: Session,
    user,
    workshop_id: Optional[int] = None,
    selected_vat: Optional[int] = None,
    error: Optional[str] = None,
    flash_msg: Optional[str] = None,
    flash_err: Optional[str] = None,
):
    # 始终下发全部缸位；工坊仅作前端 chip 筛选，避免切回「全部」时缺数据
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    vats = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .order_by(Vat.code)
        .all()
    )
    vat_counts: dict[int, int] = {}
    for v in vats:
        vat_counts[v.workshop_id] = vat_counts.get(v.workshop_id, 0) + 1

    matrix = delete_rules.matrix_json(user)
    return {
        "request": request,
        "user": user,
        "workshops": [_workshop_payload(w, vat_counts) for w in workshops],
        "vats": [_vat_payload(v) for v in vats],
        "filter_workshop": workshop_id,
        "selected_vat": selected_vat,
        "error": error,
        "flash_msg": flash_msg,
        "flash_err": flash_err,
        "status_labels": STATUS_LABELS,
        "active": "bay",
        # 删除权限矩阵：前后端共用的唯一事实来源，前端据此决定三个删除入口显隐
        "delete_matrix": matrix,
        "me": {"id": user.id, "username": user.username},
    }


def _back_url(
    workshop: Optional[int],
    vat: Optional[int],
    flash_msg: Optional[str] = None,
    flash_err: Optional[str] = None,
) -> str:
    params = []
    if workshop:
        params.append(f"workshop={workshop}")
    if vat:
        params.append(f"vat={vat}")
    if flash_msg:
        params.append(f"del_msg={quote(flash_msg)}")
    if flash_err:
        params.append(f"del_err={quote(flash_err)}")
    return "/?" + "&".join(params) if params else "/"


@router.get("/", response_class=HTMLResponse)
async def bay(
    request: Request,
    workshop: Optional[int] = None,
    vat: Optional[int] = None,
    del_msg: Optional[str] = None,
    del_err: Optional[str] = None,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, workshop, vat, None, del_msg, del_err),
    )


@router.post("/bay/vats/{pk}/status", response_class=HTMLResponse)
async def bay_vat_status(
    pk: int,
    request: Request,
    status: str = Form(...),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .filter(Vat.id == pk)
        .first()
    )
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        latest = item.latest_lot()
        validate_vat_status_change(item, status, latest)
        item.status = status
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


@router.post("/bay/vats/{pk}/lots", response_class=HTMLResponse)
async def bay_log_lot(
    pk: int,
    request: Request,
    dippedAt: str = Form(...),
    clothMeters: str = Form(...),
    redoxMv: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = db.get(Vat, pk)
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        lot = DipLot(
            vat_id=pk,
            created_by_id=user.id,
            dippedAt=datetime.fromisoformat(dippedAt),
            clothMeters=Decimal(clothMeters),
            redoxMv=Decimal(redoxMv) if redoxMv.strip() else None,
        )
        db.add(lot)
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except (ValueError, InvalidOperation) as exc:
        error = f"浸染记录无效：{exc}"
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


# ---------------------------------------------------------------------------
# 三条删除入口：浸染笔 / 染缸 / 工坊。
# 全部走 delete_rules.perform_delete 这同一条判定+执行路径，不各自重写规则。
# 删除被规则拒绝（DeleteRuleError）时只回显中文、保留会话，还原台照常可打开。
# ---------------------------------------------------------------------------


@router.post("/bay/lots/{pk}/delete")
async def bay_delete_lot(
    pk: int,
    request: Request,
    workshop: str = Form(""),
    vat: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    ws = int(workshop) if workshop.strip() else None
    back_vat = int(vat) if vat.strip() else None
    try:
        delete_rules.perform_delete(db, TARGET_LOT, pk, user)
        return RedirectResponse(
            _back_url(ws, back_vat, flash_msg="该浸染笔已删除。"),
            status_code=303,
        )
    except DeleteRuleError as exc:
        db.rollback()
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, ws, back_vat, flash_err=exc.message),
            status_code=403,
        )
    except DeleteNotFoundError as exc:
        db.rollback()
        return RedirectResponse(_back_url(ws, back_vat, flash_err=exc.message), status_code=303)


@router.post("/bay/vats/{pk}/delete")
async def bay_delete_vat(
    pk: int,
    request: Request,
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    ws = int(workshop) if workshop.strip() else None
    try:
        delete_rules.perform_delete(db, TARGET_VAT, pk, user)
        return RedirectResponse(
            _back_url(ws, None, flash_msg="染缸已删除，缸位条与工坊缸数已更新。"),
            status_code=303,
        )
    except DeleteRuleError as exc:
        db.rollback()
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, ws, pk, flash_err=exc.message),
            status_code=403,
        )
    except DeleteNotFoundError as exc:
        db.rollback()
        return RedirectResponse(_back_url(ws, None, flash_err=exc.message), status_code=303)


@router.post("/bay/workshops/{pk}/delete")
async def bay_delete_workshop(
    pk: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        delete_rules.perform_delete(db, TARGET_WORKSHOP, pk, user)
        return RedirectResponse(
            _back_url(None, None, flash_msg="工坊已删除，工坊筛选已更新。"),
            status_code=303,
        )
    except DeleteRuleError as exc:
        db.rollback()
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, None, None, flash_err=exc.message),
            status_code=403,
        )
    except DeleteNotFoundError as exc:
        db.rollback()
        return RedirectResponse(_back_url(None, None, flash_err=exc.message), status_code=303)


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
