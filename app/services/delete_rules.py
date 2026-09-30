"""删除权限矩阵 —— 浸染笔 / 染缸 / 工坊三类删除的唯一事实来源。

三条删除入口（前端按钮显隐与后端执行）都必须查这张矩阵，不得各写各的判断。
矩阵可 :func:`matrix_json` 序列化给浏览器，前端用同一份规则决定按钮是否出现。

规则（题面）：
- 浸染笔（dip_lot）：染缸工只能删「自己今天登记」的笔；删他人的或非当日的一律拒绝。
  主管无此限制。
- 染缸（vat）：仅主管可删；缸下仍有浸染笔时先拒绝。
- 工坊（workshop）：仅主管可删；坊下仍有染缸时先拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DipLot, User, Vat, Workshop

ROLE_SUPERVISOR = "supervisor"
ROLE_WORKER = "worker"

TARGET_LOT = "dip_lot"
TARGET_VAT = "vat"
TARGET_WORKSHOP = "workshop"

TargetKind = Literal["dip_lot", "vat", "workshop"]
RoleKind = Literal["supervisor", "worker"]

# worker 取值含义：
#   deny       —— 该角色一律不允许
#   own_today  —— 仅允许删除自己当日创建的笔
# supervisor 取值含义：
#   allow       —— 无条件允许（仍受非空校验之外的约束；浸染笔不做非空校验）
#   allow_empty —— 允许，但仅当没有子项（缸无浸染笔 / 坊无染缸）
DELETE_MATRIX: dict[TargetKind, dict[str, str]] = {
    TARGET_LOT: {"supervisor": "allow", "worker": "own_today"},
    TARGET_VAT: {"supervisor": "allow_empty", "worker": "deny"},
    TARGET_WORKSHOP: {"supervisor": "allow_empty", "worker": "deny"},
}

# 矩阵的中文展示名，随矩阵一起下发前端
TARGET_LABELS: dict[TargetKind, str] = {
    TARGET_LOT: "浸染笔",
    TARGET_VAT: "染缸",
    TARGET_WORKSHOP: "工坊",
}


class DeleteRuleError(Exception):
    """删除被矩阵规则或完整性约束拒绝。message 为可直接展示的中文说明。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class DeleteNotFoundError(Exception):
    """目标不存在或已被他人删除（并发场景）。message 为中文说明。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def role_of(user: User) -> RoleKind:
    """账号到矩阵角色的唯一映射：超管即主管，其余为染缸工。"""
    return ROLE_SUPERVISOR if getattr(user, "is_superuser", False) else ROLE_WORKER


def matrix_json(user: User) -> dict:
    """下发给前端的同一份矩阵（含当前用户角色与中文标签）。"""
    return {
        "role": role_of(user),
        "rules": DELETE_MATRIX,
        "labels": TARGET_LABELS,
    }


def is_same_local_day(moment: datetime, now: datetime) -> bool:
    """浸染笔是否属于「当日」：按业务本地日历日比较（朴素时间按本地处理）。"""
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    return moment.date() == now.date()


@dataclass
class DeleteDecision:
    allowed: bool
    reason: Optional[str] = None


def can_delete_lot(lot: DipLot, user: User, now: Optional[datetime] = None) -> DeleteDecision:
    """矩阵在「浸染笔」上的判定，供后端执行与（需要时的）复用。"""
    role = role_of(user)
    rule = DELETE_MATRIX[TARGET_LOT][role]
    if rule == "allow":
        return DeleteDecision(True)
    # worker / own_today
    owner_id = lot.created_by_id
    if owner_id is None or owner_id != user.id:
        return DeleteDecision(False, "染缸工只能删除自己登记的浸染笔，他人的笔不可删除。")
    if not is_same_local_day(lot.dippedAt, now or datetime.now(timezone.utc)):
        return DeleteDecision(False, "染缸工只能删除当日登记的浸染笔，非当日的笔不可删除。")
    return DeleteDecision(True)


def _children_exist(db: Session, kind: TargetKind, pk: int) -> bool:
    if kind == TARGET_VAT:
        stmt = (
            select(func.count())
            .select_from(DipLot)
            .where(DipLot.vat_id == pk)
        )
    elif kind == TARGET_WORKSHOP:
        stmt = (
            select(func.count())
            .select_from(Vat)
            .where(Vat.workshop_id == pk)
        )
    else:
        return False
    return (db.scalar(stmt) or 0) > 0


def _get_locked(db: Session, kind: TargetKind, pk: int):
    """按主键取行并 `FOR UPDATE` 锁定，保证两位主管并发删同一空坊时只成一笔。"""
    model = {
        TARGET_LOT: DipLot,
        TARGET_VAT: Vat,
        TARGET_WORKSHOP: Workshop,
    }[kind]
    return db.execute(select(model).where(model.id == pk).with_for_update()).scalar_one_or_none()


def evaluate(db: Session, kind: TargetKind, pk: int, user: User,
             now: Optional[datetime] = None) -> DeleteDecision:
    """读侧判定：取目标（不锁）并按矩阵给出结论，供展示/预检复用。"""
    model = {
        TARGET_LOT: DipLot,
        TARGET_VAT: Vat,
        TARGET_WORKSHOP: Workshop,
    }[kind]
    obj = db.get(model, pk)
    if obj is None:
        return DeleteDecision(False, "该条记录已不存在或已被删除。")
    role = role_of(user)
    rule = DELETE_MATRIX[kind][role]
    if rule == "deny":
        return DeleteDecision(False, f"{TARGET_LABELS[kind]}仅主管可删除。")
    if kind == TARGET_LOT:
        return can_delete_lot(obj, user, now)
    # allow_empty：vat / workshop
    if _children_exist(db, kind, pk):
        if kind == TARGET_VAT:
            return DeleteDecision(False, "该染缸下仍有浸染记录，请先清空浸染笔后再删除染缸。")
        return DeleteDecision(False, "该工坊下仍有染缸，请先清空染缸后再删除工坊。")
    return DeleteDecision(True)


def perform_delete(db: Session, kind: TargetKind, pk: int, user: User,
                   now: Optional[datetime] = None):
    """三条删除入口共用的唯一执行路径：矩阵判定 → 行锁 → 非空校验 → 删除。

    - 目标不存在 / 并发下已被他人删掉：抛 :class:`DeleteNotFoundError`（中文说明）。
    - 规则或完整性不满足：抛 :class:`DeleteRuleError`（中文说明），调用方不得清会话。
    - 通过则在当前事务内删除并提交。
    """
    obj = _get_locked(db, kind, pk)
    if obj is None:
        raise DeleteNotFoundError(f"该{TARGET_LABELS[kind]}已不存在或已被删除。")

    role = role_of(user)
    rule = DELETE_MATRIX[kind][role]

    if rule == "deny":
        raise DeleteRuleError(f"{TARGET_LABELS[kind]}仅主管可删除。")

    if kind == TARGET_LOT:
        decision = can_delete_lot(obj, user, now)
        if not decision.allowed:
            raise DeleteRuleError(decision.reason)
    elif rule == "allow_empty" and _children_exist(db, kind, pk):
        if kind == TARGET_VAT:
            raise DeleteRuleError("该染缸下仍有浸染记录，请先清空浸染笔后再删除染缸。")
        raise DeleteRuleError("该工坊下仍有染缸，请先清空染缸后再删除工坊。")

    db.delete(obj)
    db.commit()
    return obj
