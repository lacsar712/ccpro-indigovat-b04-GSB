import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import DipLot, User, Vat, Workshop

_PWD_SALT = os.environ.get("PWD_SALT", "indigovat-dev-salt").encode("utf-8")


def hash_password(password: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), _PWD_SALT, 120000
    )
    return digest.hex()


def verify_password(plain: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(plain), hashed)


def _ensure_delete_demo(db: Session, admin: User, worker: User) -> None:
    """旧数据卷幂等补齐删除矩阵演示对象：空坊、空缸、三种删除场景浸染笔。"""
    now = datetime.now(timezone.utc)
    w3 = db.query(Workshop).filter_by(name="空置三号坊").first()
    if w3 is None:
        w3 = Workshop(name="空置三号坊", region="黔中", notes="尚无染缸，可删坊")
        db.add(w3)
        db.flush()

    v2 = db.query(Vat).filter_by(code="V-02").first()
    v19 = db.query(Vat).filter_by(code="V-19").first()
    if v19 is None:
        # 旧卷里 V-19 原属二号坊；空坊 w3 必须保持为空，故依次挂到 V-11、V-02，
        # 都没有时挂到任意已有染缸的工坊，绝不占用空坊
        host = db.query(Vat).filter(Vat.code == "V-11").first()
        if host is None and v2 is not None:
            host = v2
        if host is None:
            host = db.query(Vat).filter(Vat.workshop_id != w3.id).first()
        workshop_id = host.workshop_id if host else w3.id
        v19 = Vat(
            workshop_id=workshop_id,
            code="V-19",
            dyeType="土靛",
            volumeL=Decimal("500.00"),
            status=Vat.STATUS_IDLE,
        )
        db.add(v19)
        db.flush()

    if v2 is not None and worker is not None and admin is not None:
        # 用 created_at 标记幂等：同角色同时间窗的演示笔只补一次
        demo_owned_today = (
            db.query(DipLot)
            .filter(DipLot.vat_id == v2.id, DipLot.created_by_id == worker.id)
            .count()
        )
        if demo_owned_today == 0:
            db.add_all(
                [
                    # 染缸工自建当日笔：唯一可被染缸工删的笔
                    DipLot(
                        vat_id=v2.id,
                        dippedAt=now - timedelta(minutes=20),
                        clothMeters=Decimal("12.00"),
                        redoxMv=None,
                        created_by_id=worker.id,
                        created_at=now - timedelta(minutes=20),
                    ),
                    # 染缸工自建隔日笔：拒（非当日）
                    DipLot(
                        vat_id=v2.id,
                        dippedAt=now - timedelta(days=3),
                        clothMeters=Decimal("9.50"),
                        redoxMv=None,
                        created_by_id=worker.id,
                        created_at=now - timedelta(days=3),
                    ),
                    # 他人（主管）当日笔：拒（非本人）
                    DipLot(
                        vat_id=v2.id,
                        dippedAt=now - timedelta(hours=2),
                        clothMeters=Decimal("7.25"),
                        redoxMv=None,
                        created_by_id=admin.id,
                        created_at=now - timedelta(hours=2),
                    ),
                ]
            )
    db.commit()


def ensure_seed_data(db: Session) -> None:
    """幂等种子：账号 + 蓝靛湾/清水江样例缸位、电位序列与删除矩阵演示数据。

    删除矩阵演示覆盖：
    - 染缸工自建当日笔（可删）
    - 染缸工自建隔日笔（拒：非当日）
    - 他人（主管）登记笔（拒：非本人）
    - 仍挂浸染的染缸（主管删缸被拒）
    - 空染缸（主管可删）
    - 空工坊（主管可删，且可复现双主管并发）
    """
    if not db.query(User).filter_by(username="admin").first():
        db.add(
            User(
                username="admin",
                password_hash=hash_password("123456"),
                is_superuser=True,
            )
        )
    if not db.query(User).filter_by(username="worker").first():
        db.add(
            User(
                username="worker",
                password_hash=hash_password("123456"),
                is_superuser=False,
            )
        )
    db.commit()

    admin = db.query(User).filter_by(username="admin").first()
    worker = db.query(User).filter_by(username="worker").first()

    if db.query(Workshop).filter_by(name="蓝靛湾一号坊").first():
        # 旧数据卷：只幂等补齐删除矩阵演示对象，不重复灌历史序列
        _ensure_delete_demo(db, admin, worker)
        return

    w1 = Workshop(name="蓝靛湾一号坊", region="黔东南", notes="晨露还原较快")
    w2 = Workshop(name="清水江二号坊", region="黔南", notes="缸体较深，保温好")
    # 空工坊：主管可直接删除，用于演示空坊删除与并发删除
    w3 = Workshop(name="空置三号坊", region="黔中", notes="尚无染缸，可删坊")
    db.add_all([w1, w2, w3])
    db.flush()

    v1 = Vat(
        workshop_id=w1.id,
        code="V-01",
        dyeType="土靛",
        volumeL=Decimal("800.00"),
        status=Vat.STATUS_REDUCING,
    )
    v2 = Vat(
        workshop_id=w1.id,
        code="V-02",
        dyeType="合成靛",
        volumeL=Decimal("600.00"),
        status=Vat.STATUS_IDLE,
    )
    v3 = Vat(
        workshop_id=w2.id,
        code="V-11",
        dyeType="土靛",
        volumeL=Decimal("900.00"),
        status=Vat.STATUS_REDUCING,
    )
    v4 = Vat(
        workshop_id=w2.id,
        code="V-12",
        dyeType="板蓝根靛",
        volumeL=Decimal("750.00"),
        status=Vat.STATUS_READY,
    )
    # 空染缸：主管可直接删；与上面仍挂浸染的缸形成对照
    v5 = Vat(
        workshop_id=w2.id,
        code="V-19",
        dyeType="土靛",
        volumeL=Decimal("500.00"),
        status=Vat.STATUS_IDLE,
    )
    db.add_all([v1, v2, v3, v4, v5])
    db.flush()

    now = datetime.now(timezone.utc)

    def lot(vat_id, hours_ago, meters, redox, owner, created_at):
        return DipLot(
            vat_id=vat_id,
            dippedAt=now - timedelta(hours=hours_ago),
            clothMeters=Decimal(meters),
            redoxMv=Decimal(redox) if redox is not None else None,
            created_by_id=owner.id,
            created_at=created_at,
        )

    def series(vat_id, rows, owner):
        """rows: (hours_ago, meters, redox or None)；历史序列登记时间取浸染时间。"""
        return [
            lot(vat_id, h, m, r, owner, now - timedelta(hours=h))
            for h, m, r in rows
        ]

    # V-01：主管登记的历史电位序列（对染缸工即「他人笔」，且均非当日）
    db.add_all(
        series(
            v1.id,
            [
                (36, "18.00", "-410.00"),
                (28, "22.50", "-455.00"),
                (20, "30.00", "-490.00"),
                (12, "40.00", "-510.00"),
                (8, "45.00", "-520.00"),
            ],
            admin,
        )
    )

    # V-02 集中放置删除矩阵三种浸染笔情形
    db.add_all(
        [
            # 染缸工自建当日笔：唯一可被染缸工删除的笔
            lot(v2.id, 1, "12.00", None, worker, now - timedelta(minutes=20)),
            # 染缸工自建但隔日：拒（非当日）
            lot(v2.id, 72, "9.50", None, worker, now - timedelta(days=3)),
            # 他人（主管）当日登记：拒（非本人），即便就在今天也不行
            lot(v2.id, 2, "7.25", None, admin, now - timedelta(hours=2)),
        ]
    )

    db.add_all(
        series(
            v3.id,
            [
                (40, "25.00", "-390.00"),
                (30, "35.00", "-430.00"),
                (22, "48.00", "-460.00"),
                (14, "60.00", "-480.00"),
            ],
            admin,
        )
    )
    db.add_all(
        series(
            v4.id,
            [
                (48, "20.00", "-420.00"),
                (32, "28.00", "-470.00"),
                (20, "33.00", "-505.00"),
                (10, "38.50", "-530.00"),
            ],
            admin,
        )
    )
    # V-19 空缸不下任何浸染；三号坊不下任何染缸。
    db.commit()
