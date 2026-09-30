"""删除权限矩阵（浸染笔 / 染缸 / 工坊 × 染缸工 / 主管）。

矩阵是唯一事实源：
- 后端 ``perform_delete`` 按它鉴权并在行锁内执行删除；
- ``DELETE_MATRIX`` / ``DELETE_DENIALS`` 直接 JSON 下发，前端 vatBay 的
  ``canDelete`` / ``denyReason`` 跑同一份矩阵与同一份中文文案。

三条删除入口（笔 / 缸 / 坊）一律走这里，不得各写各的判断。

规则一览：

    对象       染缸工(worker)          主管(supervisor)
    浸染笔     own_today 当日自建笔    forbidden 不代删单笔
    染缸       forbidden               empty_only 空缸
    工坊       forbidden               empty_only 空坊
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import DipLot, User, Vat, Workshop

# 删除对象
LOT = "lot"
VAT = "vat"
WORKSHOP = "workshop"
TARGETS = (LOT, VAT, WORKSHOP)

# 角色
ROLE_WORKER = "worker"
ROLE_SUPERVISOR = "supervisor"

# 规则（谓词）
RULE_OWN_TODAY = "own_today"    # 仅本人当日自建
RULE_EMPTY_ONLY = "empty_only"  # 仅空对象
RULE_FORBIDDEN = "forbidden"    # 该角色对该对象无删除权

# 删除矩阵：DELETE_MATRIX[对象][角色] -> 规则名
DELETE_MATRIX: dict[str, dict[str, str]] = {
    LOT: {ROLE_WORKER: RULE_OWN_TODAY, ROLE_SUPERVISOR: RULE_FORBIDDEN},
    VAT: {ROLE_WORKER: RULE_FORBIDDEN, ROLE_SUPERVISOR: RULE_EMPTY_ONLY},
    WORKSHOP: {ROLE_WORKER: RULE_FORBIDDEN, ROLE_SUPERVISOR: RULE_EMPTY_ONLY},
}

# 拒绝时的中文说明：前端禁用按钮的提示与后端拦截报错共用同一份文案
DELETE_DENIALS: dict[str, dict[str, str]] = {
    LOT: {
        "not_owner": "该浸染笔由他人登记：染缸工只能删除本人当日登记的浸染笔。",
        "not_today": "该浸染笔不是当日登记：隔日记录已入账，不可删除。",
        "supervisor": "浸染笔只许登记人当日自删，主管不代删单笔记录。",
    },
    VAT: {
        "worker": "染缸只许主管删除，染缸工无权删缸。",
        "not_empty": "该染缸仍有浸染记录，须先清空全部浸染后才能删缸。",
    },
    WORKSHOP: {
        "worker": "工坊只许主管删除，染缸工无权删坊。",
        "not_empty": "该工坊下仍有染缸，须先清空全部染缸后才能删坊。",
    },
}

MISSING_MESSAGES = {
    LOT: "该浸染笔已不存在或已被删除。",
    VAT: "该染缸已不存在或已被删除。",
    WORKSHOP: "该工坊已不存在或已被删除。",
}


class DeleteError(Exception):
    """矩阵拒绝（403）或目标已不存在（404）。message 始终为可直接展示的中文。"""

    def __init__(self, message: str, status_code: int = 403, context: Optional[dict] = None):
        self.message = message
        self.status_code = status_code
        # 被拒目标的所属缸/坊，供调用方重渲染还原台时保持展开位置
        self.context = context or {}
        super().__init__(message)


def role_of(user: User) -> str:
    return ROLE_SUPERVISOR if user.is_superuser else ROLE_WORKER


def is_today(value: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """当日 = 登记时间的本地日历日与今天相同（与登录用户同处一个工作日）。"""
    if value is None:
        return False
    now = now or datetime.now().astimezone()
    local = value.astimezone(now.tzinfo) if value.tzinfo else value
    return local.date() == now.date()


def lot_facts(lot: DipLot, user: User, now: Optional[datetime] = None) -> dict:
    return {
        "own": lot.created_by_id == user.id,
        "today": is_today(lot.created_at, now),
    }


def _rule_allows(rule: str, facts: dict) -> bool:
    if rule == RULE_OWN_TODAY:
        return bool(facts.get("own") and facts.get("today"))
    if rule == RULE_EMPTY_ONLY:
        return bool(facts.get("empty"))
    return False  # forbidden / 未知规则一律拒绝


def denial_reason(target: str, role: str, facts: dict) -> Optional[str]:
    """按矩阵给出拒绝的中文原因；允许时返回 None。"""
    rule = DELETE_MATRIX[target][role]
    if rule == RULE_FORBIDDEN:
        return DELETE_DENIALS[LOT]["supervisor"] if target == LOT else DELETE_DENIALS[target]["worker"]
    if not _rule_allows(rule, facts):
        if rule == RULE_OWN_TODAY:
            return (
                DELETE_DENIALS[LOT]["not_owner"]
                if not facts.get("own")
                else DELETE_DENIALS[LOT]["not_today"]
            )
        if rule == RULE_EMPTY_ONLY:
            return DELETE_DENIALS[target]["not_empty"]
    return None


def perform_delete(db: Session, target: str, pk: int, user: User) -> dict:
    """按矩阵鉴权并在数据库行锁内执行删除。

    成功返回重定向提示 ``{"vat": vat_id|None, "workshop": workshop_id|None}``；
    矩阵拒绝抛 ``DeleteError(403)``，目标已不存在/并发已删抛 ``DeleteError(404)``。
    会话（session cookie）由调用方持有，本函数绝不清会话。
    """
    if target not in DELETE_MATRIX:
        raise DeleteError("未知的删除对象。", 400)
    role = role_of(user)
    now = datetime.now().astimezone()

    if target == LOT:
        # 行锁：两请求并发删同一笔时，后到者在锁释放后读到空行，得到 404 中文说明
        lot = db.query(DipLot).filter(DipLot.id == pk).with_for_update().first()
        if lot is None:
            db.rollback()
            raise DeleteError(MISSING_MESSAGES[LOT], 404)
        reason = denial_reason(LOT, role, lot_facts(lot, user, now))
        if reason:
            vat_id = lot.vat_id
            vat = db.query(Vat).filter(Vat.id == vat_id).first()
            ctx = {"vat": vat_id, "workshop": vat.workshop_id if vat else None}
            db.rollback()
            raise DeleteError(reason, 403, ctx)
        vat_id = lot.vat_id
        vat = db.query(Vat).filter(Vat.id == vat_id).first()
        workshop_id = vat.workshop_id if vat else None
        db.delete(lot)
        db.commit()
        return {"vat": vat_id, "workshop": workshop_id}

    if target == VAT:
        vat = db.query(Vat).filter(Vat.id == pk).with_for_update().first()
        if vat is None:
            db.rollback()
            raise DeleteError(MISSING_MESSAGES[VAT], 404)
        # 锁内行数复核：仍挂浸染的缸先拒，绝不级联吞掉浸染笔
        facts = {"empty": db.query(DipLot).filter(DipLot.vat_id == pk).count() == 0}
        reason = denial_reason(VAT, role, facts)
        if reason:
            ctx = {"vat": pk, "workshop": vat.workshop_id}
            db.rollback()
            raise DeleteError(reason, 403, ctx)
        workshop_id = vat.workshop_id
        db.delete(vat)
        db.commit()
        return {"vat": None, "workshop": workshop_id}

    workshop = db.query(Workshop).filter(Workshop.id == pk).with_for_update().first()
    if workshop is None:
        db.rollback()
        raise DeleteError(MISSING_MESSAGES[WORKSHOP], 404)
    # 锁内复核：两主管并发删同一空坊，后到者要么见缸（刚被新建）被拒，
    # 要么坊已被先到者删除 -> 404 中文说明，至多一笔成功
    facts = {"empty": db.query(Vat).filter(Vat.workshop_id == pk).count() == 0}
    reason = denial_reason(WORKSHOP, role, facts)
    if reason:
        db.rollback()
        raise DeleteError(reason, 403, {"vat": None, "workshop": pk})
    db.delete(workshop)
    db.commit()
    return {"vat": None, "workshop": None}
