from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import DipLot, Vat, Workshop
from app.services.delete_rules import (
    DELETE_DENIALS,
    DELETE_MATRIX,
    LOT as TARGET_LOT,
    VAT as TARGET_VAT,
    WORKSHOP as TARGET_WORKSHOP,
    DeleteError,
    is_today,
    perform_delete,
    role_of,
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


def _vat_payload(vat: Vat, current_user_id: int) -> dict:
    lots = sorted(vat.lots, key=lambda x: (x.dippedAt, x.id))
    chronological = lots
    latest = lots[-1] if lots else None
    recent = list(reversed(lots[-8:]))  # 展开区展示近几笔
    now = datetime.now().astimezone()
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
        "spark": _spark_points(chronological),
        "lotCount": len(lots),
        "recentLots": [
            {
                "id": l.id,
                "dippedAt": l.dippedAt.strftime("%Y-%m-%d %H:%M"),
                "clothMeters": float(l.clothMeters),
                "redoxMv": float(l.redoxMv) if l.redoxMv is not None else None,
                # 删除矩阵所需事实：是否本人登记 / 是否当日登记
                "createdById": l.created_by_id,
                "own": l.created_by_id == current_user_id,
                "createdToday": is_today(l.created_at, now),
            }
            for l in recent
        ],
    }


def _bay_context(
    request: Request,
    db: Session,
    user,
    workshop_id: Optional[int] = None,
    selected_vat: Optional[int] = None,
    error: Optional[str] = None,
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
    return {
        "request": request,
        "user": user,
        "workshops": [
            {
                "id": w.id,
                "name": w.name,
                "region": w.region,
                # chip 数量：删除工坊后据服务端重渲染结果复算
                "vatCount": vat_counts.get(w.id, 0),
                "empty": vat_counts.get(w.id, 0) == 0,
            }
            for w in workshops
        ],
        "vats": [_vat_payload(v, user.id) for v in vats],
        "filter_workshop": workshop_id,
        "selected_vat": selected_vat,
        "error": error,
        "status_labels": STATUS_LABELS,
        "active": "bay",
        # 删除矩阵、中文拒绝文案与当前角色：前后端共用同一份事实源
        "delete_matrix": DELETE_MATRIX,
        "delete_denials": DELETE_DENIALS,
        "current_role": role_of(user),
        "current_user_id": user.id,
    }


@router.get("/", response_class=HTMLResponse)
async def bay(
    request: Request,
    workshop: Optional[int] = None,
    vat: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "bay.html", _bay_context(request, db, user, workshop, vat))


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
            dippedAt=datetime.fromisoformat(dippedAt),
            clothMeters=Decimal(clothMeters),
            redoxMv=Decimal(redoxMv) if redoxMv.strip() else None,
            created_by_id=user.id,
            created_at=datetime.now().astimezone(),
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


# 三条删除入口（浸染笔 / 染缸 / 工坊）共用同一个 helper、同一份删除矩阵。
# 拒绝（403）或已不存在（404）都带着中文说明重新渲染还原台：不清会话、不跳登录页，还原台仍可打开。
async def _handle_delete(
    request: Request,
    db: Session,
    user,
    target: str,
    pk: int,
    workshop: str,
):
    ws = int(workshop) if workshop.strip() else None
    try:
        result = perform_delete(db, target, pk, user)
    except DeleteError as exc:
        # exc.status_code: 403 矩阵拒绝 / 404 已被并发删除；会话保持不变，还原台照常渲染
        ctx_ws = exc.context.get("workshop")
        render_ws = ctx_ws if ctx_ws is not None else ws
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, render_ws, exc.context.get("vat"), exc.message),
            status_code=exc.status_code,
        )
    dest_ws = ws
    if result["workshop"] is not None:
        dest_ws = result["workshop"]
    location = "/"
    params = []
    if target == TARGET_LOT and result.get("vat"):
        params.append(f"vat={result['vat']}")
    if dest_ws is not None and db.get(Workshop, dest_ws) is not None:
        params.append(f"workshop={dest_ws}")
    if params:
        location += "?" + "&".join(params)
    return RedirectResponse(location, status_code=303)


@router.post("/bay/lots/{pk}/delete", response_class=HTMLResponse)
async def bay_delete_lot(
    pk: int,
    request: Request,
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return await _handle_delete(request, db, user, TARGET_LOT, pk, workshop)


@router.post("/bay/vats/{pk}/delete", response_class=HTMLResponse)
async def bay_delete_vat(
    pk: int,
    request: Request,
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return await _handle_delete(request, db, user, TARGET_VAT, pk, workshop)


@router.post("/bay/workshops/{pk}/delete", response_class=HTMLResponse)
async def bay_delete_workshop(
    pk: int,
    request: Request,
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return await _handle_delete(request, db, user, TARGET_WORKSHOP, pk, workshop)


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
