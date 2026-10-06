"""额度预留端到端功能测试（真实 PostgreSQL + TestClient）。

覆盖需求要点：
- 全部素材同时通过才生成预留编号、逐项授权、到期时刻与可预留余量；
  任一失败整体拒绝、并列明原因、不占额；
- 未到期预留参与新申请及其他预留的余量判定；
- 到期左闭右开（到期时刻确认视为逾期），到期即释放；
- 到期前确认原子转为一笔 approved 申请，重复确认返回同一申请；
- 取消与确认互斥；取消/过期后的确认拒绝；重复取消不再释放；
- 查询区分 pending/confirmed/cancelled/expired 四态与状态过滤；
- 授权停用不影响已有预留的确认；
- 原授权存在未到期待确认预留时改挂整体拒绝；
- 预留逐项命中授权沿用重叠选择顺序；可预留余量随占用变化；
- TTL 1～30 分钟边界、重复素材/未知编号/404/409 等。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_reservation_api
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import update as sa_update
from sqlalchemy import func

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439",
)

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 12, 10, 12, 0, tzinfo=CST)

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def force_expire(reservation_id: int) -> None:
    """直接把库内到期时刻改为当前时刻（左闭右开：now == expires_at 即逾期）。"""
    db = SessionLocal()
    try:
        db.execute(
            sa_update(models.Reservation)
            .where(models.Reservation.id == reservation_id)
            .values(expires_at=func.now())
        )
        db.commit()
    finally:
        db.close()


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    init_db()
    c = TestClient(app)

    print("== 登记素材与授权 ==")
    m1 = c.post("/api/v1/materials", json={"code": "p1", "name": "素材1"}).json()["id"]
    m2 = c.post("/api/v1/materials", json={"code": "p2", "name": "素材2"}).json()["id"]
    m3 = c.post("/api/v1/materials", json={"code": "p3", "name": "素材3"}).json()["id"]
    auth1 = c.post(
        "/api/v1/authorizations",
        json={
            "material_id": m1, "region": "CN", "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 3,
        },
    ).json()["id"]
    auth2 = c.post(
        "/api/v1/authorizations",
        json={
            "material_id": m2, "region": "CN", "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 3,
        },
    ).json()["id"]
    auth3 = c.post(
        "/api/v1/authorizations",
        json={
            "material_id": m3, "region": "CN", "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 1,
        },
    ).json()["id"]

    print("== 创建预留：成功返回编号/逐项授权/到期时刻/可预留余量 ==")
    r = c.post(
        "/api/v1/reservations",
        json={
            "material_ids": [m1, m2],
            "region": "CN",
            "channel": "web",
            "occur_at": NOW.isoformat(),
            "ttl_minutes": 15,
        },
    )
    check("reserve 201", r.status_code == 201, r.text)
    body = r.json()
    rid = body["id"]
    check("status pending", body["status"] == "pending", body)
    check("ttl echoed", body["ttl_minutes"] == 15)
    check(
        "expires_at ~= now+15min",
        abs(
            (
                datetime.fromisoformat(body["expires_at"])
                - datetime.fromisoformat(body["created_at"])
            ).total_seconds()
            - 900
        )
        < 5,
        body["expires_at"],
    )
    check("2 items", len(body["items"]) == 2)
    ids = {it["material_id"]: it["authorization"]["id"] for it in body["items"]}
    check("item auth m1", ids.get(m1) == auth1, ids)
    check("item auth m2", ids.get(m2) == auth2, ids)
    check(
        "item reservable remaining 2",
        body["items"][0]["authorization"]["reservable_remaining"] == 2,
        body["items"],
    )
    check("distribution absent before confirm", body["distribution"] is None)
    check("distribution_id null", body["distribution_id"] is None)

    print("== 未到期预留参与新申请与其他预留的余量判定 ==")
    g1 = c.get(f"/api/v1/authorizations/{auth1}").json()
    check("auth1 used 0", g1["used_count"] == 0)
    check("auth1 reserved 1", g1["reserved_count"] == 1)
    check("auth1 remaining 3（兼容）", g1["remaining"] == 3)
    check("auth1 reservable 2", g1["reservable_remaining"] == 2)

    # auth3 容量 1：直接申请先占掉，则预留 m3 失败
    rd = c.post(
        "/api/v1/distributions",
        json={"material_ids": [m3], "region": "CN", "channel": "web",
              "occur_at": NOW.isoformat()},
    )
    check("direct m3 approved", rd.status_code == 201, rd.text)

    print("== 整体拒绝：任一素材不足则不占任何额度并列明原因 ==")
    before1 = c.get(f"/api/v1/authorizations/{auth1}").json()["reserved_count"]
    before2 = c.get(f"/api/v1/authorizations/{auth2}").json()["reserved_count"]
    rr = c.post(
        "/api/v1/reservations",
        json={
            "material_ids": [m1, m2, m3],
            "region": "CN",
            "channel": "web",
            "occur_at": NOW.isoformat(),
            "ttl_minutes": 10,
        },
    )
    check("mixed reserve 422", rr.status_code == 422, rr.text)
    err = rr.json()["error"]
    check("code reservation_rejected", err["code"] == "reservation_rejected")
    check("only m3 reason", len(err["details"]) == 1, err["details"])
    check(
        "m3 quota_exhausted",
        err["details"][0]["reason"] == "quota_exhausted"
        and err["details"][0]["material_id"] == m3,
        err["details"],
    )
    after1 = c.get(f"/api/v1/authorizations/{auth1}").json()["reserved_count"]
    after2 = c.get(f"/api/v1/authorizations/{auth2}").json()["reserved_count"]
    check("no partial reserve", before1 == after1 and before2 == after2)

    # 直接申请 m1 也要把预留计入余量：auth1 used0+resv1，仍可占（剩2）
    rd1 = c.post(
        "/api/v1/distributions",
        json={"material_ids": [m1], "region": "CN", "channel": "web",
              "occur_at": NOW.isoformat()},
    )
    check("direct m1 approved with reserve held", rd1.status_code == 201, rd1.text)
    g1 = c.get(f"/api/v1/authorizations/{auth1}").json()
    check("auth1 used1 resv1", g1["used_count"] == 1 and g1["reserved_count"] == 1)
    # 再来两次直接申请：第一次成功（剩 1 个可预留余量），第二次失败
    rd2 = c.post(
        "/api/v1/distributions",
        json={"material_ids": [m1], "region": "CN", "channel": "web",
              "occur_at": NOW.isoformat()},
    )
    check("direct m1 again approved", rd2.status_code == 201, rd2.text)
    rd3 = c.post(
        "/api/v1/distributions",
        json={"material_ids": [m1], "region": "CN", "channel": "web",
              "occur_at": NOW.isoformat()},
    )
    check(
        "direct m1 blocked by reservation",
        rd3.status_code == 422
        and rd3.json()["error"]["details"][0]["reason"] == "quota_exhausted",
        rd3.text,
    )
    g1 = c.get(f"/api/v1/authorizations/{auth1}").json()
    check("auth1 used2 resv1 full", g1["used_count"] == 2 and g1["reserved_count"] == 1)

    print("== 确认：原子转为 approved 申请，重复确认返回同一笔 ==")
    cf = c.post(f"/api/v1/reservations/{rid}/confirm")
    check("confirm 200", cf.status_code == 200, cf.text)
    cf_body = cf.json()
    check("confirmed", cf_body["status"] == "confirmed")
    check("confirmed_at set", cf_body["confirmed_at"] is not None)
    did = cf_body["distribution_id"]
    check("distribution id present", isinstance(did, int))
    check("distribution embedded", cf_body["distribution"] is not None)
    check(
        "distribution approved + items",
        cf_body["distribution"]["status"] == "approved"
        and len(cf_body["distribution"]["items"]) == 2,
    )
    # 同一申请可在既有发行接口查询
    gd = c.get(f"/api/v1/distributions/{did}")
    check("get generated distribution", gd.status_code == 200 and gd.json()["id"] == did)
    g1 = c.get(f"/api/v1/authorizations/{auth1}").json()
    g2 = c.get(f"/api/v1/authorizations/{auth2}").json()
    # auth1: used 3(2 direct + 1 confirm), reserved 0; auth2: used1 reserved0
    check("auth1 after confirm used3/resv0", g1["used_count"] == 3 and g1["reserved_count"] == 0, g1)
    check("auth2 after confirm used1/resv0", g2["used_count"] == 1 and g2["reserved_count"] == 0)

    cf2 = c.post(f"/api/v1/reservations/{rid}/confirm")
    check("duplicate confirm 200", cf2.status_code == 200, cf2.text)
    check(
        "duplicate confirm same application",
        cf2.json()["distribution_id"] == did,
    )
    g1b = c.get(f"/api/v1/authorizations/{auth1}").json()
    check("no double consume", g1b["used_count"] == 3 and g1b["reserved_count"] == 0)

    # 确认后取消被拒绝
    cx = c.post(f"/api/v1/reservations/{rid}/cancel")
    check("cancel confirmed 409", cx.status_code == 409 and
          cx.json()["error"]["code"] == "reservation_already_confirmed", cx.text)

    # 确认生成的申请可通过既有接口撤销
    rv = c.post(f"/api/v1/distributions/{did}/revoke")
    check("revoke generated distribution", rv.status_code == 200)
    g1c = c.get(f"/api/v1/authorizations/{auth1}").json()
    g2c = c.get(f"/api/v1/authorizations/{auth2}").json()
    check("after revoke auth1 used2", g1c["used_count"] == 2)
    check("after revoke auth2 used0", g2c["used_count"] == 0)

    print("== 取消：释放预留、与确认互斥、重复取消不再释放 ==")
    rc = c.post(
        "/api/v1/reservations",
        json={"material_ids": [m1], "region": "CN", "channel": "web",
              "occur_at": NOW.isoformat(), "ttl_minutes": 10},
    )
    check("new reserve for cancel (slots: auth1 used2)", rc.status_code == 201, rc.text)
    ridc = rc.json()["id"]
    x1 = c.post(f"/api/v1/reservations/{ridc}/cancel")
    check("cancel 200", x1.status_code == 200 and x1.json()["status"] == "cancelled")
    check("cancelled_at set", x1.json()["cancelled_at"] is not None)
    g1d = c.get(f"/api/v1/authorizations/{auth1}").json()
    check("reserve released after cancel", g1d["used_count"] == 2 and g1d["reserved_count"] == 0)
    x2 = c.post(f"/api/v1/reservations/{ridc}/cancel")
    check("repeat cancel 409 no re-release", x2.status_code == 409 and
          x2.json()["error"]["code"] == "reservation_already_cancelled", x2.text)
    x3 = c.post(f"/api/v1/reservations/{ridc}/confirm")
    check("confirm after cancel 409", x3.status_code == 409 and
          x3.json()["error"]["code"] == "reservation_cancelled", x3.text)
    g1e = c.get(f"/api/v1/authorizations/{auth1}").json()
    check("still zero reserved", g1e["reserved_count"] == 0)

    print("== 过期：到期时刻确认视为逾期，到期即释放，过期后确认/取消均拒绝 ==")
    re_ = c.post(
        "/api/v1/reservations",
        json={"material_ids": [m2], "region": "CN", "channel": "web",
              "occur_at": NOW.isoformat(), "ttl_minutes": 1},
    )
    ride = re_.json()["id"]
    g2before = c.get(f"/api/v1/authorizations/{auth2}").json()
    check("reserved before expiry", g2before["reserved_count"] == 1 and
          g2before["used_count"] == 0)
    force_expire(ride)
    late = c.post(f"/api/v1/reservations/{ride}/confirm")
    check("confirm at expiry 409 expired", late.status_code == 409 and
          late.json()["error"]["code"] == "reservation_expired", late.text)
    g2after = c.get(f"/api/v1/authorizations/{auth2}").json()
    check("expired releases quota", g2after["reserved_count"] == 0 and
          g2after["reservable_remaining"] == 3)
    ge = c.get(f"/api/v1/reservations/{ride}")
    check("status expired", ge.json()["status"] == "expired" and
          ge.json()["expired_at"] is not None, ge.text)
    ce = c.post(f"/api/v1/reservations/{ride}/cancel")
    check("cancel expired 409", ce.status_code == 409 and
          ce.json()["error"]["code"] == "reservation_expired", ce.text)
    late2 = c.post(f"/api/v1/reservations/{ride}/confirm")
    check("re-confirm expired 409", late2.status_code == 409)
    # 释放后新预留可立即成功（到期即释放参与余量判定）
    rn = c.post(
        "/api/v1/reservations",
        json={"material_ids": [m2], "region": "CN", "channel": "web",
              "occur_at": NOW.isoformat(), "ttl_minutes": 10},
    )
    check("reserve after expiry release", rn.status_code == 201, rn.text)
    c.post(f"/api/v1/reservations/{rn.json()['id']}/cancel")

    print("== 授权停用不影响已有预留的确认 ==")
    # auth2 当前 used0/resv0（上面已取消）；建预留后停用再确认
    rh = c.post(
        "/api/v1/reservations",
        json={"material_ids": [m2], "region": "CN", "channel": "web",
              "occur_at": NOW.isoformat(), "ttl_minutes": 10},
    ).json()["id"]
    c.patch(f"/api/v1/authorizations/{auth2}", json={"status": "inactive"})
    hf = c.post(f"/api/v1/reservations/{rh}/confirm")
    check("confirm despite inactive", hf.status_code == 200 and
          hf.json()["status"] == "confirmed", hf.text)
    # 新预留匹配不到启用授权
    hn = c.post(
        "/api/v1/reservations",
        json={"material_ids": [m2], "region": "CN", "channel": "web",
              "occur_at": NOW.isoformat(), "ttl_minutes": 10},
    )
    check(
        "new reserve on inactive rejected",
        hn.status_code == 422
        and hn.json()["error"]["details"][0]["reason"] == "authorization_inactive",
        hn.text,
    )
    c.patch(f"/api/v1/authorizations/{auth2}", json={"status": "active"})

    print("== 查询：区分四种状态并支持过滤 ==")
    # 确保 pending 态至少有一条（前面的预留均已终态）。
    pending_rid = c.post(
        "/api/v1/reservations",
        json={"material_ids": [m2], "region": "CN", "channel": "web",
              "occur_at": NOW.isoformat(), "ttl_minutes": 20},
    ).json()["id"]
    check("fresh pending visible",
          c.get(f"/api/v1/reservations/{pending_rid}").json()["status"] == "pending")
    allres = c.get("/api/v1/reservations").json()
    statuses = sorted(x["status"] for x in allres)
    for st in ("pending", "confirmed", "cancelled", "expired"):
        n_list = len(c.get(f"/api/v1/reservations?status={st}").json())
        n_direct = statuses.count(st)
        check(f"filter {st} ({n_list})", n_list == n_direct and n_list >= 1)
    bad = c.get("/api/v1/reservations?status=wat")
    check("invalid status 422", bad.status_code == 422 and
          bad.json()["error"]["code"] == "invalid_reservation_status")
    check("unknown reservation 404",
          c.get("/api/v1/reservations/999999").status_code == 404)
    check("confirm unknown 404",
          c.post("/api/v1/reservations/999999/confirm").status_code == 404)
    check("cancel unknown 404",
          c.post("/api/v1/reservations/999999/cancel").status_code == 404)
    # 收尾：取消该 pending，避免影响后续通道余量。
    c.post(f"/api/v1/reservations/{pending_rid}/cancel")

    print("== 改挂：原授权存在未到期待确认预留时整体拒绝 ==")
    ms = c.post("/api/v1/materials", json={"code": "ps", "name": "改挂素材"}).json()["id"]
    sa = c.post(
        "/api/v1/authorizations",
        json={"material_id": ms, "region": "CN", "channel": "app",
              "starts_at": (NOW - timedelta(days=1)).isoformat(),
              "ends_at": (NOW + timedelta(days=1)).isoformat(),
              "max_count": 10},
    ).json()["id"]
    ta = c.post(
        "/api/v1/authorizations",
        json={"material_id": ms, "region": "CN", "channel": "app",
              "starts_at": (NOW - timedelta(days=2)).isoformat(),
              "ends_at": (NOW + timedelta(days=2)).isoformat(),
              "max_count": 100},
    ).json()["id"]
    # 先放一条正式申请
    c.post(
        "/api/v1/distributions",
        json={"material_ids": [ms], "region": "CN", "channel": "app",
              "occur_at": NOW.isoformat()},
    )
    pr = c.post(
        "/api/v1/reservations",
        json={"material_ids": [ms], "region": "CN", "channel": "app",
              "occur_at": NOW.isoformat(), "ttl_minutes": 10},
    ).json()["id"]
    mg = c.post(
        "/api/v1/authorizations/migrate",
        json={"source_authorization_id": sa, "replacement_authorization_id": ta},
    )
    check("migrate rejected with pending resv", mg.status_code == 422 and
          mg.json()["error"]["details"][0]["reason"] == "pending_reservations", mg.text)
    gs = c.get(f"/api/v1/authorizations/{sa}").json()
    check("source untouched active/reserved", gs["status"] == "active" and
          gs["reserved_count"] == 1 and gs["used_count"] == 1)
    # 预留到期后改挂成功（到期释放 + 无待确认预留）
    force_expire(pr)
    mg2 = c.post(
        "/api/v1/authorizations/migrate",
        json={"source_authorization_id": sa, "replacement_authorization_id": ta},
    )
    check("migrate after expiry ok", mg2.status_code == 200 and
          mg2.json()["migrated_count"] == 1 and
          mg2.json()["source_authorization"]["status"] == "inactive", mg2.text)

    print("== 重叠授权选择顺序沿用，预留余量逐项选择第一条可用 ==")
    mo = c.post("/api/v1/materials", json={"code": "po", "name": "选择素材"}).json()["id"]
    e1 = c.post(
        "/api/v1/authorizations",
        json={"material_id": mo, "region": "CN", "channel": "tv",
              "starts_at": (NOW - timedelta(hours=2)).isoformat(),
              "ends_at": (NOW + timedelta(hours=1)).isoformat(),
              "max_count": 1},
    ).json()["id"]
    e2 = c.post(
        "/api/v1/authorizations",
        json={"material_id": mo, "region": "CN", "channel": "tv",
              "starts_at": (NOW - timedelta(hours=2)).isoformat(),
              "ends_at": (NOW + timedelta(hours=3)).isoformat(),
              "max_count": 10},
    ).json()["id"]
    # 第一条（到期最早）容量被预留占满后，下一条预留应落到 e2
    c.post(
        "/api/v1/reservations",
        json={"material_ids": [mo], "region": "CN", "channel": "tv",
              "occur_at": NOW.isoformat(), "ttl_minutes": 10},
    )
    rs2 = c.post(
        "/api/v1/reservations",
        json={"material_ids": [mo], "region": "CN", "channel": "tv",
              "occur_at": NOW.isoformat(), "ttl_minutes": 10},
    )
    check("second reserve chooses later auth",
          rs2.status_code == 201 and
          rs2.json()["items"][0]["authorization"]["id"] == e2, rs2.text)

    print("== 参数校验 ==")
    for ttl, ok in ((1, True), (30, True), (0, False), (31, False)):
        v = c.post(
            "/api/v1/reservations",
            json={"material_ids": [m2], "region": "CN", "channel": "web",
                  "occur_at": NOW.isoformat(), "ttl_minutes": ttl},
        )
        check(f"ttl {ttl} -> {'201' if ok else '422'}",
              (v.status_code == 201) == ok, v.text)
        if ok:
            c.post(f"/api/v1/reservations/{v.json()['id']}/cancel")
    na = c.post(
        "/api/v1/reservations",
        json={"material_ids": [m2], "region": "CN", "channel": "web",
              "occur_at": "2026-12-10T12:00:00", "ttl_minutes": 5},
    )
    check("naive occur 422", na.status_code == 422)
    dm = c.post(
        "/api/v1/reservations",
        json={"material_ids": [m1, m1], "region": "CN", "channel": "web",
              "occur_at": NOW.isoformat(), "ttl_minutes": 5},
    )
    check("duplicate items 422", dm.status_code == 422 and
          dm.json()["error"]["code"] == "duplicate_material_items")
    um = c.post(
        "/api/v1/reservations",
        json={"material_ids": [888888], "region": "CN", "channel": "web",
              "occur_at": NOW.isoformat(), "ttl_minutes": 5},
    )
    check("unknown material 422", um.status_code == 422 and
          um.json()["error"]["code"] == "unknown_material")

    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
