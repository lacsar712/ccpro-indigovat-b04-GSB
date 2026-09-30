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


def _get_or_create_user(db: Session, username: str, is_superuser: bool) -> User:
    user = db.query(User).filter_by(username=username).first()
    if user is None:
        user = User(
            username=username,
            password_hash=hash_password("123456"),
            is_superuser=is_superuser,
        )
        db.add(user)
        db.flush()
    return user


def _get_or_create_workshop(db: Session, name: str, region: str, notes: str) -> Workshop:
    ws = db.query(Workshop).filter_by(name=name).first()
    if ws is None:
        ws = Workshop(name=name, region=region, notes=notes)
        db.add(ws)
        db.flush()
    return ws


def _get_or_create_vat(
    db: Session, workshop_id: int, code: str, dye_type: str, volume: str, status: str
) -> Vat:
    vat = db.query(Vat).filter_by(workshop_id=workshop_id, code=code).first()
    if vat is None:
        vat = Vat(
            workshop_id=workshop_id,
            code=code,
            dyeType=dye_type,
            volumeL=Decimal(volume),
            status=status,
        )
        db.add(vat)
        db.flush()
    return vat


def ensure_seed_data(db: Session) -> None:
    """幂等种子：账号 + 蓝靛湾/清水江样例缸位、电位序列与删除演示数据。

    删除演示覆盖：
    - 染缸工「当日自建笔」（可删）与「非当日自建笔」（不可删）
    - 「他人笔」（主管登记，染缸工不可删）
    - 「有浸染缸」（主管不可直接删）与「空缸」（主管可删）
    - 「空坊」云雾山三号坊（无染缸，主管可删）
    """
    admin = _get_or_create_user(db, "admin", is_superuser=True)
    worker = _get_or_create_user(db, "worker", is_superuser=False)
    db.commit()

    w1 = _get_or_create_workshop(db, "蓝靛湾一号坊", "黔东南", "晨露还原较快")
    w2 = _get_or_create_workshop(db, "清水江二号坊", "黔南", "缸体较深，保温好")
    # 空坊：无任何染缸，供主管演示删除工坊
    w3 = _get_or_create_workshop(db, "云雾山三号坊", "黔中", "新建坊，尚未布缸")
    db.flush()

    v1 = _get_or_create_vat(db, w1.id, "V-01", "土靛", "800.00", Vat.STATUS_REDUCING)
    v2 = _get_or_create_vat(db, w1.id, "V-02", "合成靛", "600.00", Vat.STATUS_IDLE)
    v3 = _get_or_create_vat(db, w2.id, "V-11", "土靛", "900.00", Vat.STATUS_REDUCING)
    v4 = _get_or_create_vat(db, w2.id, "V-12", "板蓝根靛", "750.00", Vat.STATUS_READY)
    # 空缸：无浸染笔，供主管演示删除染缸
    v5 = _get_or_create_vat(db, w1.id, "V-03", "土靛", "500.00", Vat.STATUS_IDLE)
    db.flush()

    # 旧数据（新增 created_by 列之前的笔）一律记到主管名下，即染缸工眼中的「他人笔」
    db.query(DipLot).filter(DipLot.created_by_id.is_(None)).update(
        {DipLot.created_by_id: admin.id}, synchronize_session=False
    )

    now = datetime.now(timezone.utc)

    def seed_series(vat: Vat, series, owner_id: int) -> None:
        """series: (hours_ago, meters, redox or None)；仅在该缸尚无笔时播种，保持幂等。"""
        if db.query(DipLot).filter_by(vat_id=vat.id).first() is not None:
            return
        for hours, meters, redox in series:
            db.add(
                DipLot(
                    vat_id=vat.id,
                    created_by_id=owner_id,
                    dippedAt=now - timedelta(hours=hours),
                    clothMeters=Decimal(meters),
                    redoxMv=Decimal(redox) if redox is not None else None,
                )
            )
        db.flush()

    # 这些主管登记的历史笔，对染缸工而言都是「他人笔」
    seed_series(
        v1,
        [
            (36, "18.00", "-410.00"),
            (28, "22.50", "-455.00"),
            (20, "30.00", "-490.00"),
            (12, "40.00", "-510.00"),
            (8, "45.00", "-520.00"),
        ],
        admin.id,
    )
    seed_series(
        v2,
        [
            (6, "8.00", None),
            (1, "12.00", None),
        ],
        admin.id,
    )
    seed_series(
        v3,
        [
            (40, "25.00", "-390.00"),
            (30, "35.00", "-430.00"),
            (22, "48.00", "-460.00"),
            (14, "60.00", "-480.00"),
        ],
        admin.id,
    )
    seed_series(
        v4,
        [
            (48, "20.00", "-420.00"),
            (32, "28.00", "-470.00"),
            (20, "33.00", "-505.00"),
            (10, "38.50", "-530.00"),
        ],
        admin.id,
    )

    # 染缸工演示笔：一笔「当日自建」（可由本人删除）、一笔「非当日自建」（本人也不可删）
    if not db.query(DipLot).filter_by(created_by_id=worker.id).first():
        db.add_all(
            [
                DipLot(
                    vat_id=v2.id,
                    created_by_id=worker.id,
                    dippedAt=now - timedelta(minutes=20),
                    clothMeters=Decimal("15.00"),
                    redoxMv=Decimal("-505.00"),
                ),
                DipLot(
                    vat_id=v2.id,
                    created_by_id=worker.id,
                    dippedAt=now - timedelta(days=3),
                    clothMeters=Decimal("9.50"),
                    redoxMv=None,
                ),
            ]
        )

    db.commit()
