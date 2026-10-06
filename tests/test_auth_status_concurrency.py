"""授权启用/停用（PATCH）与同通道写事务共用通道咨询锁的并发安全测试。

修复点：授权状态变更过去直接改状态并提交，未获取 region+channel 的
通道咨询锁，可能与同通道的发行、预留、候补成交、改挂、额度转拨
交错读到对方的中间态（如候补已按启用授权选定后才停用仍生成占用、
转拨从状态变更的缝隙中通过），违反「停用授权不得承接新业务」。

本测试以真实 PostgreSQL 验证：
1. PATCH 启用/停用与同通道其它写事务严格串行：通道咨询锁被占时
   PATCH 必须等待锁释放（而不是直接提交）；
2. PATCH 停用提交后，新写事务只能看到停用后的完整状态——同通道
   新发行/新预留整体 422 authorization_inactive、转拨 422
   authorization_inactive，绝无停用后仍新增占用；既有预留可照常确认；
3. 停用在锁内立即按既有契约重算候补：匹配授权均停用的队首在停用
   提交后即为 failed 终态；
4. 通道冻结期间仍可启用/停用，但只做到期/有效期结算，不做候补成交/
   失败判定，失配候补在恢复事务（同一把锁）内才标记 failed；
5. 全程账实相符、授权额度不超额。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_auth_status_concurrency
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439",
)

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app import models, services  # noqa: E402

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=CST)


def _make_auth(client, mid, region, channel="web", max_count=10,
              starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1)):
    r = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": region,
            "channel": channel,
            "starts_at": starts.isoformat(),
            "ends_at": ends.isoformat(),
            "max_count": max_count,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_material(client, code):
    r = client.post("/api/v1/materials", json={"code": code, "name": code})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _occur():
    return NOW.isoformat()


def scenario_status_change_serialized_on_channel_lock() -> None:
    print("== 场景1：通道锁被占时，PATCH 启用/停用必须等待 ==")
    client = TestClient(app)
    mid = _make_material(client, "as-lock-mat")
    region = "LCK1"
    aid = _make_auth(client, mid, region)

    hold_done = threading.Event()
    patch_started = threading.Event()
    outcome = {}

    def hold_channel_lock():
        # 独立事务持有该通道的 xact 咨询锁，持有期间不提交。
        db = SessionLocal()
        try:
            db.execute(
                text("SELECT pg_advisory_xact_lock(:lk)"),
                {"lk": services._bucket_lock_id(region, "web")},
            )
            patch_started.set()
            hold_done.wait(5)
            db.commit()
        finally:
            db.close()

    def patch_status():
        patch_started.wait(5)
        c = TestClient(app)
        outcome["code"] = c.patch(
            f"/api/v1/authorizations/{aid}",
            json={"status": "inactive"},
        ).status_code

    holder = threading.Thread(target=hold_channel_lock)
    holder.start()
    assert patch_started.wait(5), "持锁线程未就绪"

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(patch_status)
        # 给 PATCH 足够时间拿到（或错误地绕过）通道锁：持锁期间
        # 它必须被阻塞在 pg_advisory_xact_lock 上。
        if fut.done():
            raise AssertionError(
                "PATCH 在通道咨询锁被占期间直接提交，未与同通道事务串行"
            )
        import time as _time

        _time.sleep(0.3)
        if fut.done():
            raise AssertionError(
                "PATCH 未等待通道咨询锁即提交（状态变更未与同通道事务串行）"
            )
        hold_done.set()
        holder.join(5)
        fut.result(5)
        assert outcome.get("code") == 200, outcome

    # 提交后新读事务必须看到停用后的完整状态。
    body = client.get(f"/api/v1/authorizations/{aid}").json()
    assert body["status"] == "inactive", body
    print("  PASS  PATCH 等待通道咨询锁，提交后状态完整可见")


def scenario_deactivation_blocks_all_new_business() -> None:
    print("== 场景2：停用提交后新发行/新预留/转拨均被原子拒绝 ==")
    client = TestClient(app)

    # --- 2a) 单授权通道：停用后新发行/新预留一律拒绝 ---
    mid = _make_material(client, "as-newbiz-mat")
    region = "LCK2"
    aid = _make_auth(client, mid, region, max_count=10)
    # 既有已通过申请与待确认预留：停用不应影响它们。
    d0 = client.post(
        "/api/v1/distributions",
        json={"material_ids": [mid], "region": region, "channel": "web",
              "occur_at": _occur()},
    ).json()["id"]
    r0 = client.post(
        "/api/v1/reservations",
        json={"material_ids": [mid], "region": region, "channel": "web",
              "occur_at": _occur(), "ttl_minutes": 30},
    ).json()["id"]

    rp = client.patch(f"/api/v1/authorizations/{aid}",
                     json={"status": "inactive"})
    assert rp.status_code == 200, rp.text

    rd = client.post(
        "/api/v1/distributions",
        json={"material_ids": [mid], "region": region, "channel": "web",
              "occur_at": _occur()},
    )
    assert rd.status_code == 422, rd.text
    assert rd.json()["error"]["details"][0]["reason"] == (
        "authorization_inactive"
    ), rd.text

    rr = client.post(
        "/api/v1/reservations",
        json={"material_ids": [mid], "region": region, "channel": "web",
              "occur_at": _occur(), "ttl_minutes": 30},
    )
    assert rr.status_code == 422, rr.text
    assert rr.json()["error"]["details"][0]["reason"] == (
        "authorization_inactive"
    ), rr.text

    # 既有预留确认不受停用影响：确认仍 200（不重新匹配授权）。
    rc = client.post(f"/api/v1/reservations/{r0}/confirm")
    assert rc.status_code == 200, rc.text
    assert rc.json()["status"] == "confirmed", rc.text

    # 账实相符：停用后没有产生任何新占用。
    a = client.get(f"/api/v1/authorizations/{aid}").json()
    # 既有 1 笔直接发行 + 1 笔预留确认 = 2 正式占用；无遗留预留。
    assert a["used_count"] == 2 and a["reserved_count"] == 0, a
    assert a["used_count"] + a["reserved_count"] <= a["max_count"], a
    # 既有申请仍可撤销（停用不影响已通过申请）。
    assert client.post(
        f"/api/v1/distributions/{d0}/revoke"
    ).status_code == 200

    # --- 2b) 转拨通道：源授权停用 → 422，两端额度不变 ---
    rt_region = "LCK2T"
    sid = _make_auth(client, mid, rt_region, max_count=10)
    tid = _make_auth(
        client, mid, rt_region, max_count=10,
        starts=NOW - timedelta(days=2), ends=NOW + timedelta(days=2),
    )
    assert client.patch(f"/api/v1/authorizations/{sid}",
                     json={"status": "inactive"}).status_code == 200
    rt = client.post(
        "/api/v1/authorizations/transfer",
        json={"source_authorization_id": sid,
              "target_authorization_id": tid, "count": 1,
              "operation_no": "as-newbiz-transfer-1"},
    )
    assert rt.status_code == 422, rt.text
    assert rt.json()["error"]["details"][0]["reason"] == (
        "authorization_inactive"
    ), rt.text
    s = client.get(f"/api/v1/authorizations/{sid}").json()
    t = client.get(f"/api/v1/authorizations/{tid}").json()
    assert s["max_count"] == 10 and t["max_count"] == 10, (s, t)
    print("  PASS  停用后新发行/新预留/转拨均 422，既有预留确认、申请撤销正常")


def scenario_deactivate_fails_matched_waitlist_in_lock() -> None:
    print("== 场景3：停用在锁内重算候补，匹配授权停用的队首立即 failed ==")
    client = TestClient(app)
    mid = _make_material(client, "as-wait-mat")
    region = "LCK3"
    aid = _make_auth(client, mid, region, max_count=1)
    # 占满唯一授权 → 随后候补入队。
    assert client.post(
        "/api/v1/distributions",
        json={"material_ids": [mid], "region": region, "channel": "web",
              "occur_at": _occur()},
    ).status_code == 201
    w = client.post(
        "/api/v1/waitlists",
        json={"material_ids": [mid], "region": region, "channel": "web",
              "occur_at": _occur(), "ttl_minutes": 1440},
    )
    wid = w.json()["id"]
    assert w.status_code == 201 and w.json()["status"] == "pending", w.text

    # 停用与候补处理共用同一把锁：提交后队首即完整 failed 终态。
    assert client.patch(f"/api/v1/authorizations/{aid}",
                       json={"status": "inactive"}).status_code == 200
    body = client.get(f"/api/v1/waitlists/{wid}").json()
    assert body["status"] == "failed", body
    assert body["failure_reason"] == "authorization_inactive", body
    print("  PASS  停用提交后候补立即 failed（authorization_inactive）")


def scenario_frozen_channel_defers_waitlist_failure_to_resume() -> None:
    print("== 场景4：冻结通道内停用不做候补失败判定，恢复时才 failed ==")
    client = TestClient(app)
    mid = _make_material(client, "as-frozen-mat")
    region = "LCK4"
    aid = _make_auth(client, mid, region, max_count=1)
    assert client.post(
        "/api/v1/distributions",
        json={"material_ids": [mid], "region": region, "channel": "web",
              "occur_at": _occur()},
    ).status_code == 201
    w = client.post(
        "/api/v1/waitlists",
        json={"material_ids": [mid], "region": region, "channel": "web",
              "occur_at": _occur(), "ttl_minutes": 1440},
    )
    wid = w.json()["id"]
    assert w.json()["status"] == "pending", w.text

    # 冻结通道（PATCH 停用并非新额度发放，冻结期间仍允许）。
    fr = client.put(
        "/api/v1/channel-freezes",
        json={"region": region, "channel": "web",
              "action": "freeze", "reason": "检修"},
    )
    assert fr.status_code == 200 and fr.json()["status"] == "frozen", fr.text

    # 冻结期间停用授权：PATCH 仍 200，但遵守冻结契约——只结算有效期、
    # 不做候补成交/失败判定，故候补仍 pending。
    assert client.patch(f"/api/v1/authorizations/{aid}",
                     json={"status": "inactive"}).status_code == 200
    body = client.get(f"/api/v1/waitlists/{wid}").json()
    assert body["status"] == "pending", body

    # 恢复事务在同一把通道锁内按原受理顺序处理队首：授权已停用 → failed。
    rs = client.put(
        "/api/v1/channel-freezes",
        json={"region": region, "channel": "web", "action": "resume"},
    )
    assert rs.status_code == 200, rs.text
    body = client.get(f"/api/v1/waitlists/{wid}").json()
    assert body["status"] == "failed", body
    assert body["failure_reason"] == "authorization_inactive", body
    print("  PASS  冻结期停用不判失败，恢复事务内同锁标记 failed")


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    init_db()
    with TestClient(app):
        scenario_status_change_serialized_on_channel_lock()
        scenario_deactivation_blocks_all_new_business()
        scenario_deactivate_fails_matched_waitlist_in_lock()
        scenario_frozen_channel_defers_waitlist_failure_to_resume()
    print("\n全部授权启停并发场景通过：状态变更与同通道写事务串行，"
          "停用授权不承接任何新业务，账实相符。")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
