"""预留额度功能并发安全测试（真实 PostgreSQL + TestClient）。

覆盖需求中的硬约束：并发提交、预留、确认、取消、撤销和改挂
**不得超额或重复释放**，且账实相符：

    used_count == 归属该授权且 approved 的 distribution_items 数
    reserved_count == 归属该授权且未到期待确认的 reservation_items 数
    used_count + reserved_count <= max_count

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_reservations
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 12, 5, 12, 0, tzinfo=CST)


def new_client() -> TestClient:
    return TestClient(app)


def make_auth(client, code, count, region, ttl_days=1):
    mid = client.post(
        "/api/v1/materials", json={"code": code, "name": code}
    ).json()["id"]
    aid = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": region,
            "channel": "web",
            "starts_at": (NOW - timedelta(days=ttl_days)).isoformat(),
            "ends_at": (NOW + timedelta(days=ttl_days)).isoformat(),
            "max_count": count,
        },
    ).json()["id"]
    return mid, aid


def assert_books_balanced(auth_id, max_count):
    """账实相符：used/reserved 与有效明细数一致且不超额。"""
    db = SessionLocal()
    try:
        auth = db.get(models.Authorization, auth_id)
        approved_items = db.scalar(
            select(func.count())
            .select_from(models.DistributionItem)
            .join(
                models.Distribution,
                models.DistributionItem.distribution_id
                == models.Distribution.id,
            )
            .where(
                models.DistributionItem.authorization_id == auth_id,
                models.Distribution.status == "approved",
            )
        )
        pending_items = db.scalar(
            select(func.count())
            .select_from(models.ReservationItem)
            .join(
                models.Reservation,
                models.ReservationItem.reservation_id == models.Reservation.id,
            )
            .where(
                models.ReservationItem.authorization_id == auth_id,
                models.Reservation.status == "pending",
                models.Reservation.expires_at > func.now(),
            )
        )
        assert auth.used_count == approved_items, (
            f"授权{auth_id}: used={auth.used_count} 正式明细={approved_items}"
        )
        assert auth.reserved_count == pending_items, (
            f"授权{auth_id}: reserved={auth.reserved_count} 待确认明细={pending_items}"
        )
        assert auth.used_count + auth.reserved_count <= max_count, (
            f"授权{auth_id} 超额：{auth.used_count}+{auth.reserved_count}>{max_count}"
        )
        assert auth.used_count >= 0 and auth.reserved_count >= 0
        return auth.used_count, auth.reserved_count
    finally:
        db.close()


def scenario_reserve_vs_apply() -> None:
    print("== 场景1：10 个预留与 20 个直接申请并发，总额 10，绝不超额 ==")
    c = new_client()
    mid, aid = make_auth(c, "r1", 10, "CN")

    def reserve(i):
        cc = new_client()
        r = cc.post(
            "/api/v1/reservations",
            json={
                "material_ids": [mid],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
                "ttl_minutes": 20,
            },
        )
        return ("reserve", r.status_code)

    def apply(i):
        cc = new_client()
        r = cc.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        return ("apply", r.status_code)

    with ThreadPoolExecutor(max_workers=30) as pool:
        futs = [pool.submit(reserve, i) for i in range(10)]
        futs += [pool.submit(apply, i) for i in range(20)]
        outs = [f.result() for f in as_completed(futs)]

    ok_reserve = sum(
        1 for kind, code in outs if kind == "reserve" and code == 201
    )
    ok_apply = sum(1 for kind, code in outs if kind == "apply" and code == 201)
    assert ok_reserve + ok_apply == 10, (
        f"成功总数应为 10：预留 {ok_reserve} + 申请 {ok_apply}"
    )
    used, reserved = assert_books_balanced(aid, 10)
    assert used + reserved == 10
    print(
        f"  PASS  预留成功={ok_reserve} 申请成功={ok_apply} "
        f"used={used} reserved={reserved}（合计=10，无超额）"
    )


def scenario_confirm_race() -> None:
    print("== 场景2：同一预留并发确认两次，只生成一笔申请、只转账一次 ==")
    c = new_client()
    mid, aid = make_auth(c, "r2", 5, "JP")
    rid = c.post(
        "/api/v1/reservations",
        json={
            "material_ids": [mid],
            "region": "JP",
            "channel": "web",
            "occur_at": NOW.isoformat(),
            "ttl_minutes": 20,
        },
    ).json()["id"]

    def confirm():
        cc = new_client()
        r = cc.post(f"/api/v1/reservations/{rid}/confirm")
        return r.status_code, (
            r.json().get("distribution_id") if r.status_code == 200 else None
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        res = list(pool.map(lambda _: confirm(), range(2)))
    assert all(code == 200 for code, _ in res), res
    d1, d2 = (did for _, did in res)
    assert d1 == d2, f"重复确认应返回同一笔申请：{d1} != {d2}"
    used, reserved = assert_books_balanced(aid, 5)
    assert used == 1 and reserved == 0, (used, reserved)

    # 确认后的申请可用既有接口撤销
    rv = c.post(f"/api/v1/distributions/{d1}/revoke")
    assert rv.status_code == 200
    used, reserved = assert_books_balanced(aid, 5)
    assert used == 0 and reserved == 0
    print(
        f"  PASS  并发确认均 200 且同一申请 id={d1}；used=1,reserved=0；"
        "撤销后归零（不重复占用）"
    )


def scenario_confirm_vs_cancel() -> None:
    print("== 场景3：确认与取消并发，互斥且额度恰好变化一次 ==")
    c = new_client()
    mid, aid = make_auth(c, "r3", 5, "KR")
    rid = c.post(
        "/api/v1/reservations",
        json={
            "material_ids": [mid],
            "region": "KR",
            "channel": "web",
            "occur_at": NOW.isoformat(),
            "ttl_minutes": 20,
        },
    ).json()["id"]

    def confirm():
        cc = new_client()
        r = cc.post(f"/api/v1/reservations/{rid}/confirm")
        return ("confirm", r.status_code)

    def cancel():
        cc = new_client()
        r = cc.post(f"/api/v1/reservations/{rid}/cancel")
        return ("cancel", r.status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outs = list(pool.map(lambda fn: fn(), [confirm, cancel]))
    codes = {kind: code for kind, code in outs}
    assert sorted(codes.values()) == [200, 409], outs

    used, reserved = assert_books_balanced(aid, 5)
    if codes["confirm"] == 200:
        assert (used, reserved) == (1, 0)
        final = "confirmed"
    else:
        assert (used, reserved) == (0, 0)
        final = "cancelled"

    # 后续操作按终态处理：
    # - 已确认：重复确认幂等 200、再取消 409；
    # - 已取消：重复取消 409、再确认 409（取消后的确认须拒绝）。
    after_confirm = c.post(f"/api/v1/reservations/{rid}/confirm").status_code
    after_cancel = c.post(f"/api/v1/reservations/{rid}/cancel").status_code
    assert after_cancel == 409  # 不论原终态如何，取消必被拒绝且不释放
    if final == "confirmed":
        assert after_confirm == 200  # 重复确认幂等，返回同一申请
    else:
        assert after_confirm == 409  # 取消后确认须拒绝
    used2, reserved2 = assert_books_balanced(aid, 5)
    assert (used2, reserved2) == (used, reserved), "终态后重复操作改变了额度"
    print(f"  PASS  互斥终态={final}，used={used},reserved={reserved}；幂等/拒绝均不再变额")


def scenario_double_cancel_race() -> None:
    print("== 场景4：同一预留并发取消两次，仅一次成功、只释放一次 ==")
    c = new_client()
    mid, aid = make_auth(c, "r4", 5, "SG")
    rid = c.post(
        "/api/v1/reservations",
        json={
            "material_ids": [mid],
            "region": "SG",
            "channel": "web",
            "occur_at": NOW.isoformat(),
            "ttl_minutes": 20,
        },
    ).json()["id"]

    def cancel():
        cc = new_client()
        return cc.post(f"/api/v1/reservations/{rid}/cancel").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda _: cancel(), range(2)))
    assert sorted(codes) == [200, 409], codes
    used, reserved = assert_books_balanced(aid, 5)
    assert used == 0 and reserved == 0
    print(f"  PASS  并发取消 {sorted(codes)}，reserved 只释放一次（归零）")


def scenario_confirm_vs_apply_full() -> None:
    print("== 场景5：容量恰为 1 时，确认与直接申请并发，绝不超额 ==")
    c = new_client()
    mid, aid = make_auth(c, "r5", 1, "TH")
    rid = c.post(
        "/api/v1/reservations",
        json={
            "material_ids": [mid],
            "region": "TH",
            "channel": "web",
            "occur_at": NOW.isoformat(),
            "ttl_minutes": 20,
        },
    ).json()["id"]

    def confirm():
        cc = new_client()
        return cc.post(f"/api/v1/reservations/{rid}/confirm").status_code

    def apply():
        cc = new_client()
        return cc.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "TH",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outs = list(pool.map(lambda fn: fn(), [confirm, apply]))
    # 确认必成功；直接申请要么因额度被预留转正式占满而 422，
    # 要么（理论上）抢先——这里预留在前且确认先提交，预期 200+422。
    assert sorted(outs) == [200, 422], outs
    used, reserved = assert_books_balanced(aid, 1)
    assert (used, reserved) == (1, 0)
    print(f"  PASS  确认=200 直接申请={outs[1]}，used=1/reserved=0（容量1未超额）")


def scenario_migrate_vs_reservations() -> None:
    print("== 场景6：改挂与新预留并发，要么拒绝改挂，要么预留落替代授权，账平 ==")
    c = new_client()
    mid = c.post(
        "/api/v1/materials", json={"code": "r6", "name": "r6"}
    ).json()["id"]
    aid = c.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "MY",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 50,
        },
    ).json()["id"]
    bid = c.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "MY",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=2)).isoformat(),
            "ends_at": (NOW + timedelta(days=2)).isoformat(),
            "max_count": 100,
        },
    ).json()["id"]
    # 源授权已有 5 条正式占用
    for _ in range(5):
        assert (
            c.post(
                "/api/v1/distributions",
                json={
                    "material_ids": [mid],
                    "region": "MY",
                    "channel": "web",
                    "occur_at": NOW.isoformat(),
                },
            ).status_code
            == 201
        )

    def migrate():
        cc = new_client()
        r = cc.post(
            "/api/v1/authorizations/migrate",
            json={
                "source_authorization_id": aid,
                "replacement_authorization_id": bid,
            },
        )
        return r.status_code

    def reserve(i):
        cc = new_client()
        r = cc.post(
            "/api/v1/reservations",
            json={
                "material_ids": [mid],
                "region": "MY",
                "channel": "web",
                "occur_at": NOW.isoformat(),
                "ttl_minutes": 20,
            },
        )
        return r.status_code

    with ThreadPoolExecutor(max_workers=11) as pool:
        mf = pool.submit(migrate)
        rf = [pool.submit(reserve, i) for i in range(10)]
        mcode = mf.result()
        rcodes = [f.result() for f in rf]

    db = SessionLocal()
    try:
        a = db.get(models.Authorization, aid)
        b = db.get(models.Authorization, bid)
        if mcode == 200:
            # 改挂先提交：源已停用，10 个并发预留必须全部落替代授权。
            assert rcodes.count(201) == 10, rcodes
            assert a.status == "inactive"
            assert (b.used_count, b.reserved_count) == (5, 10), (
                b.used_count,
                b.reserved_count,
            )
            bad = db.scalar(
                select(func.count())
                .select_from(models.ReservationItem)
                .join(
                    models.Reservation,
                    models.ReservationItem.reservation_id
                    == models.Reservation.id,
                )
                .where(
                    models.Reservation.status == "pending",
                    models.ReservationItem.authorization_id == aid,
                )
            )
            assert bad == 0, f"仍有 {bad} 条待确认预留指向已停用源授权"
            print(
                "  PASS  改挂先成功：10 个并发预留全部落替代授权，"
                "target used=5 reserved=10，源无残留待确认预留"
            )
        else:
            # 并发预留先落在源授权：改挂须以 pending_reservations 整体拒绝，
            # 源保持 active、次数不变；所有预留仍归属源。
            assert mcode == 422, mcode
            assert a.status == "active"
            n_ok = rcodes.count(201)
            assert (a.used_count, a.reserved_count) == (5, n_ok), (
                a.used_count,
                a.reserved_count,
                n_ok,
            )
            assert b.used_count == 0 and b.reserved_count == 0
            on_target = db.scalar(
                select(func.count())
                .select_from(models.ReservationItem)
                .join(
                    models.Reservation,
                    models.ReservationItem.reservation_id
                    == models.Reservation.id,
                )
                .where(
                    models.Reservation.status == "pending",
                    models.ReservationItem.authorization_id == bid,
                )
            )
            assert on_target == 0, "改挂拒绝后不应有预留落到替代授权"
            print(
                f"  PASS  {n_ok} 个预留先落源授权，改挂整体拒绝"
                f"（pending_reservations），源 active、used=5 reserved={n_ok}，"
                "替代授权零占用"
            )
    finally:
        db.close()
    assert_books_balanced(aid, 50)
    assert_books_balanced(bid, 100)
    assert mcode in (200, 422)


def scenario_expiry_releases_under_pressure() -> None:
    print("== 场景7：到期预留被并发申请/确认竞争时，释放与拒绝一致、不重复释放 ==")
    c = new_client()
    mid, aid = make_auth(c, "r7", 1, "PH")
    r = c.post(
        "/api/v1/reservations",
        json={
            "material_ids": [mid],
            "region": "PH",
            "channel": "web",
            "occur_at": NOW.isoformat(),
            "ttl_minutes": 1,
        },
    ).json()
    rid = r["id"]
    # 直接置为到期时刻
    db = SessionLocal()
    try:
        db.execute(
            update(models.Reservation)
            .where(models.Reservation.id == rid)
            .values(expires_at=func.now())
        )
        db.commit()
    finally:
        db.close()

    def late_confirm():
        cc = new_client()
        return ("confirm", cc.post(f"/api/v1/reservations/{rid}/confirm").status_code)

    def late_cancel():
        cc = new_client()
        return ("cancel", cc.post(f"/api/v1/reservations/{rid}/cancel").status_code)

    def apply():
        cc = new_client()
        return (
            "apply",
            cc.post(
                "/api/v1/distributions",
                json={
                    "material_ids": [mid],
                    "region": "PH",
                    "channel": "web",
                    "occur_at": NOW.isoformat(),
                },
            ).status_code,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(late_confirm), pool.submit(late_cancel)]
        futs += [pool.submit(apply) for _ in range(4)]
        outs = [f.result() for f in as_completed(futs)]

    # 逾期确认/取消：确认必 409（reservation_expired）；
    # 取消要么与结算竞争得 200（到期未结算的瞬态，随后置 cancelled），
    # 要么 409（已过期/已取消）；二者都不允许出现重复释放。
    confirm_code = [code for k, code in outs if k == "confirm"][0]
    assert confirm_code == 409, outs
    apply_ok = sum(1 for k, code in outs if k == "apply" and code == 201)
    # 到期后只释放 1 个名额：恰好一个直接申请成功。
    assert apply_ok == 1, outs
    used, reserved = assert_books_balanced(aid, 1)
    assert (used, reserved) == (1, 0), (used, reserved)
    g = c.get(f"/api/v1/reservations/{rid}").json()
    assert g["status"] in ("expired", "cancelled")
    print(
        f"  PASS  逾期确认=409，到期释放的 1 名额恰好被 1 个申请取得；"
        f"预留终态={g['status']}，used=1/reserved=0"
    )


def scenario_multi_material_reservation() -> None:
    print("== 场景8：多素材预留，任一授权不足则整体不占额；并发下各授权不超额 ==")
    c = new_client()
    mids, aids = [], []
    for i, cap in enumerate((3, 3, 3)):
        mid, aid = make_auth(c, f"r8{i}", cap, "VN")
        mids.append(mid)
        aids.append(aid)

    def reserve(i):
        cc = new_client()
        r = cc.post(
            "/api/v1/reservations",
            json={
                "material_ids": mids,
                "region": "VN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
                "ttl_minutes": 20,
            },
        )
        return r.status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(reserve, range(10)))
    assert codes.count(201) == 3, codes
    assert codes.count(422) == 7
    for aid in aids:
        used, reserved = assert_books_balanced(aid, 3)
        assert (used, reserved) == (0, 3), (used, reserved)
    print("  PASS  10 个多素材预留恰 3 个整体成功，三个授权均 reserved=3（不超额、账平）")


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    init_db()
    with TestClient(app):
        scenario_reserve_vs_apply()
        scenario_confirm_race()
        scenario_confirm_vs_cancel()
        scenario_double_cancel_race()
        scenario_confirm_vs_apply_full()
        scenario_migrate_vs_reservations()
        scenario_expiry_releases_under_pressure()
        scenario_multi_material_reservation()
    print("\n全部预留并发场景通过：未超额、未重复释放、账实相符。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
