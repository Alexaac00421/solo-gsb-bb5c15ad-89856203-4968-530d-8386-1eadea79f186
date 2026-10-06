"""通道额度冻结/恢复功能与并发安全测试（真实 PostgreSQL + TestClient）。

覆盖需求：
- 按地区+渠道冻结/恢复：冻结须填写原因；查询返回当前状态、原因与
  变更时刻；重复同态操作不改动原因与变更时刻。
- 不存在授权的通道、缺失冻结原因、非法状态（action）均明确报错。
- 冻结期间：新直接发行/新预留/候补受理明确拒绝且不占额；既有预留
  仍可确认/取消，已通过申请仍可撤销/改期，授权仍可改挂，候补仍可
  取消；这些操作释放的额度不得使候补成交。
- 到期预留与候补仍按原有效期结算（冻结不延长）：冻结期间到期即
  expired，且不成交。
- 恢复时先按原受理顺序处理本通道仍有效的候补（过期者释放位置、
  不再匹配者失败、可承接者成交、队首不足则停止），再允许新请求占额。
- 冻结/恢复只影响本通道，其他通道不受影响。
- 并发：冻结与并发发行同通道串行绝不超额；恢复与新申请并发时
  候补严格先于新请求成交。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_channel_freeze
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, text
from sqlalchemy import update as sa_update

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439",
)

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402

CST = timezone(timedelta(hours=8))
OCT_START = "2026-10-01T00:00:00+08:00"
OCT_END = "2026-11-01T00:00:00+08:00"
OCCUR = "2026-10-15T12:00:00+08:00"
OCCUR2 = "2026-10-20T12:00:00+08:00"

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


# ------------------------------------------------------------- 构造辅助
def make_material(c, code):
    return c.post(
        "/api/v1/materials", json={"code": code, "name": code}
    ).json()["id"]


def make_auth(c, mid, count, region="CN", channel="web",
              starts=OCT_START, ends=OCT_END, material_id=None):
    return c.post(
        "/api/v1/authorizations",
        json={
            "material_id": material_id or mid,
            "region": region,
            "channel": channel,
            "starts_at": starts,
            "ends_at": ends,
            "max_count": count,
        },
    ).json()["id"]


def apply(c, mids, region="CN", channel="web", occur=OCCUR):
    return c.post(
        "/api/v1/distributions",
        json={
            "material_ids": mids,
            "region": region,
            "channel": channel,
            "occur_at": occur,
        },
    )


def reserve(c, mids, ttl=15, region="CN", channel="web", occur=OCCUR):
    return c.post(
        "/api/v1/reservations",
        json={
            "material_ids": mids,
            "region": region,
            "channel": channel,
            "occur_at": occur,
            "ttl_minutes": ttl,
        },
    )


def waitlist(c, mids, ttl=120, region="CN", channel="web", occur=OCCUR):
    return c.post(
        "/api/v1/waitlists",
        json={
            "material_ids": mids,
            "region": region,
            "channel": channel,
            "occur_at": occur,
            "ttl_minutes": ttl,
        },
    )


def freeze(c, region, channel, reason="合规检查临时冻结"):
    return c.put(
        "/api/v1/channel-freezes",
        json={"region": region, "channel": channel,
              "action": "freeze", "reason": reason},
    )


def resume(c, region, channel):
    return c.put(
        "/api/v1/channel-freezes",
        json={"region": region, "channel": channel, "action": "resume"},
    )


def state(c, region, channel):
    return c.get(
        "/api/v1/channel-freezes",
        params={"region": region, "channel": channel},
    )


def auth_view(c, aid):
    return c.get(f"/api/v1/authorizations/{aid}").json()


def force_expire_reservation(reservation_id):
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


def set_waitlist_expiry(waitlist_id, at_sql):
    db = SessionLocal()
    try:
        db.execute(
            sa_update(models.Waitlist)
            .where(models.Waitlist.id == waitlist_id)
            .values(expires_at=text(at_sql))
        )
        db.commit()
    finally:
        db.close()


def scenario_errors():
    print("== 1. 参数与通道存在性校验 ==")
    c = TestClient(app)
    m1 = make_material(c, "f-m1")
    make_auth(c, m1, 2)

    # 不存在任何授权的通道
    r = freeze(c, "US", "app")
    check("freeze unknown channel 422", r.status_code == 422, r.text)
    check("freeze unknown channel code",
          r.json()["error"]["code"] == "channel_not_found", r.text)
    r = resume(c, "US", "app")
    check("resume unknown channel 422", r.status_code == 422, r.text)
    check("resume unknown channel code",
          r.json()["error"]["code"] == "channel_not_found", r.text)
    r = state(c, "US", "app")
    check("query unknown channel 422", r.status_code == 422, r.text)

    # 缺原因 / 空白原因
    r = c.put("/api/v1/channel-freezes",
              json={"region": "CN", "channel": "web", "action": "freeze"})
    check("freeze without reason 422", r.status_code == 422, r.text)
    r = c.put("/api/v1/channel-freezes",
              json={"region": "CN", "channel": "web",
                    "action": "freeze", "reason": "   "})
    check("freeze with blank reason 422", r.status_code == 422, r.text)

    # 非法 action / 缺 query 参数 / 缺地区渠道
    r = c.put("/api/v1/channel-freezes",
              json={"region": "CN", "channel": "web",
                    "action": "pause", "reason": "x"})
    check("illegal action 422", r.status_code == 422, r.text)
    r = c.get("/api/v1/channel-freezes", params={"region": "CN"})
    check("missing channel query 422", r.status_code == 422, r.text)
    r = c.put("/api/v1/channel-freezes",
              json={"channel": "web", "action": "resume"})
    check("missing region body 422", r.status_code == 422, r.text)

    # 通道唯一授权被停用，通道仍存在（停用不改变通道归属）
    a_only = make_auth(c, m1, 1, region="VN", channel="web")
    c.patch(f"/api/v1/authorizations/{a_only}",
            json={"status": "inactive"})
    r = freeze(c, "VN", "web")
    check("freeze channel with only inactive auth 200",
          r.status_code == 200 and r.json()["status"] == "frozen", r.text)
    r = state(c, "VN", "web")
    check("query frozen inactive-auth channel",
          r.status_code == 200 and r.json()["status"] == "frozen", r.text)
    resume(c, "VN", "web")


def scenario_freeze_lifecycle():
    print("== 2. 冻结/恢复生命周期与幂等 ==")
    c = TestClient(app)
    m = make_material(c, "f-life")
    make_auth(c, m, 3, region="EU", channel="pos")

    # 从未冻结 → active 空状态
    r = state(c, "EU", "pos")
    check("never frozen -> active", r.status_code == 200, r.text)
    b = r.json()
    check("never frozen status", b["status"] == "active", b)
    check("never frozen reason null", b["reason"] is None, b)
    check("never frozen changed_at null", b["changed_at"] is None, b)

    # 未冻结先恢复：幂等空操作
    r = resume(c, "EU", "pos")
    check("resume when active 200 idempotent", r.status_code == 200, r.text)
    check("resume idempotent changed_at null",
          r.json()["changed_at"] is None, r.text)

    # 冻结
    r = freeze(c, "EU", "pos", reason="风控核查")
    check("freeze 200", r.status_code == 200, r.text)
    b = r.json()
    check("frozen status", b["status"] == "frozen", b)
    check("frozen reason", b["reason"] == "风控核查", b)
    check("frozen changed_at", b["changed_at"] is not None, b)
    check("frozen frozen_at", b["frozen_at"] == b["changed_at"], b)
    check("frozen resumed_at null", b["resumed_at"] is None, b)
    first_changed = b["changed_at"]
    first_frozen_at = b["frozen_at"]

    # 查询
    r = state(c, "EU", "pos")
    b = r.json()
    check("query frozen", b["status"] == "frozen"
          and b["reason"] == "风控核查", b)

    # 重复冻结：原因与变更时刻均不变（即使携带新原因）
    r = freeze(c, "EU", "pos", reason="另一个原因")
    check("refreeze same state 200", r.status_code == 200, r.text)
    b = r.json()
    check("refreeze reason unchanged", b["reason"] == "风控核查", b)
    check("refreeze changed_at unchanged",
          b["changed_at"] == first_changed, b)
    check("refreeze frozen_at unchanged",
          b["frozen_at"] == first_frozen_at, b)

    # 恢复
    r = resume(c, "EU", "pos")
    check("resume 200", r.status_code == 200, r.text)
    b = r.json()
    check("resumed status", b["status"] == "active", b)
    check("resumed keeps reason", b["reason"] == "风控核查", b)
    check("resumed changed_at moved",
          b["changed_at"] != first_changed, b)
    check("resumed resumed_at",
          b["resumed_at"] == b["changed_at"], b)
    check("resumed keeps frozen_at", b["frozen_at"] == first_frozen_at, b)
    resumed_changed = b["changed_at"]

    # 重复恢复：变更时刻不变
    r = resume(c, "EU", "pos")
    check("re-resume 200 idempotent", r.status_code == 200, r.text)
    check("re-resume changed_at unchanged",
          r.json()["changed_at"] == resumed_changed, r.text)

    # 再次冻结：更新原因与变更时刻
    r = freeze(c, "EU", "pos", reason="二次冻结")
    b = r.json()
    check("refreeze after resume status", b["status"] == "frozen", b)
    check("refreeze after resume reason",
          b["reason"] == "二次冻结", b)
    check("refreeze after resume changed_at moved",
          b["changed_at"] != resumed_changed, b)
    # 收尾恢复，便于后续场景
    resume(c, "EU", "pos")


def scenario_block_new_issuance():
    print("== 3. 冻结期间拒绝新直接发行/新预留/候补受理且不占额 ==")
    c = TestClient(app)
    m1 = make_material(c, "f-blk1")
    m2 = make_material(c, "f-blk2")
    a1 = make_auth(c, m1, 2)
    a2 = make_auth(c, m2, 2)
    freeze(c, "CN", "web")

    # 新直接发行
    r = apply(c, [m1, m2])
    check("distribution rejected while frozen",
          r.status_code == 422, r.text)
    check("distribution channel_frozen code",
          r.json()["error"]["code"] == "channel_frozen", r.text)
    check("channel_frozen details carry reason",
          r.json()["error"]["details"][0]["reason"] == "合规检查临时冻结",
          r.text)

    # 新预留
    r = reserve(c, [m1, m2], ttl=10)
    check("reservation rejected while frozen",
          r.status_code == 422 and
          r.json()["error"]["code"] == "channel_frozen", r.text)

    # 候补受理（即便额度充足也不直接成交）
    r = waitlist(c, [m1, m2], ttl=60)
    check("waitlist rejected while frozen",
          r.status_code == 422 and
          r.json()["error"]["code"] == "channel_frozen", r.text)

    # 均不占额
    v1, v2 = auth_view(c, a1), auth_view(c, a2)
    check("no quota consumed by frozen rejects",
          v1["used_count"] == 0 and v1["reserved_count"] == 0
          and v2["used_count"] == 0 and v2["reserved_count"] == 0,
          f"{v1} {v2}")

    # 没有任何候补/预留/申请落库
    check("waitlist list empty", c.get("/api/v1/waitlists").json() == [])
    check("reservation list empty",
          c.get("/api/v1/reservations").json() == [])

    resume(c, "CN", "web")
    r = apply(c, [m1, m2])
    check("distribution works after resume", r.status_code == 201, r.text)


def scenario_existing_operations_during_freeze():
    print("== 4. 冻结期间既有操作仍可进行，且释放的额度不成交候补 ==")
    c = TestClient(app)
    m = make_material(c, "f-exist")
    a = make_auth(c, m, 3, region="JP", channel="web")

    # 冻结前：两笔申请占用 2 槽（used=2/3）
    d1 = apply(c, [m], region="JP", channel="web").json()["id"]
    d2 = apply(c, [m], region="JP", channel="web").json()["id"]

    # 撤销 d2 释放 1 槽 → 建既有预留（reserved=1，used=1）
    c.post(f"/api/v1/distributions/{d2}/revoke")
    rsv = reserve(c, [m], ttl=20, region="JP", channel="web")
    check("existing reservation created", rsv.status_code == 201, rsv.text)
    rsv_id = rsv.json()["id"]

    # 候补：used1+reserved1=2<3 → 直接成交 1 笔（used=2）
    w_direct = waitlist(c, [m], ttl=120, region="JP", channel="web")
    check("waitlist before freeze fulfilled directly",
          w_direct.status_code == 201
          and w_direct.json()["status"] == "fulfilled", w_direct.text)
    w_direct_dist = w_direct.json()["distribution_id"]
    # used2+reserved1=3 满 → 后续候补入队
    wq = waitlist(c, [m], ttl=120, region="JP", channel="web")
    check("waitlist queued before freeze",
          wq.status_code == 201 and wq.json()["status"] == "pending",
          wq.text)
    wq_id = wq.json()["id"]

    # ---- 冻结 ----
    freeze(c, "JP", "web")

    # 4.1 既有预留仍可确认（reserved→used，不释放净额；used=3）
    r = c.post(f"/api/v1/reservations/{rsv_id}/confirm")
    check("confirm reservation during freeze 200",
          r.status_code == 200 and r.json()["status"] == "confirmed",
          r.text)
    check("waitlist still pending after confirm during freeze",
          c.get(f"/api/v1/waitlists/{wq_id}").json()["status"] == "pending")

    # 确认生成的申请仍可撤销 → 释放 1 槽，但候补不得成交
    dist_from_rsv = r.json()["distribution_id"]
    r = c.post(f"/api/v1/distributions/{dist_from_rsv}/revoke")
    check("revoke confirmed-reservation distribution during freeze",
          r.status_code == 200, r.text)
    wb = c.get(f"/api/v1/waitlists/{wq_id}").json()
    check("released quota does NOT fulfill waitlist while frozen",
          wb["status"] == "pending", wb)
    check("reserved_count returned after revoke",
          auth_view(c, a)["reserved_count"] == 0)

    # 4.2 既有申请仍可撤销（d1 与冻结前直接成交的 w_direct），
    #     释放全部额度但候补仍不成交
    r = c.post(f"/api/v1/distributions/{d1}/revoke")
    check("revoke existing distribution d1 during freeze",
          r.status_code == 200, r.text)
    r = c.post(f"/api/v1/distributions/{w_direct_dist}/revoke")
    check("revoke pre-freeze fulfilled waitlist distribution during freeze",
          r.status_code == 200, r.text)
    check("waitlist still pending after all revokes during freeze",
          c.get(f"/api/v1/waitlists/{wq_id}").json()["status"] == "pending")

    # 此时 used=0,reserved=0,max=3；候补仍 pending（冻结中）
    v = auth_view(c, a)
    check("quota free but waitlist pending during freeze",
          v["used_count"] == 0 and v["reserved_count"] == 0
          and v["remaining"] == 3, v)

    # 4.3 候补仍可取消
    r = c.post(f"/api/v1/waitlists/{wq_id}/cancel")
    check("cancel waitlist during freeze",
          r.status_code == 200 and r.json()["status"] == "cancelled",
          r.text)

    # 4.4 冻结期「取消既有预留释放额度也不成交候补」：
    #     恢复后再造一个既有预留 + 候补，重新冻结验证取消路径。
    resume(c, "JP", "web")
    rsv2 = reserve(c, [m], ttl=20, region="JP", channel="web")
    check("reservation2 created after resume", rsv2.status_code == 201,
          rsv2.text)
    # 把正式余量全部占满（used+reserved=max），随后候补只能入队。
    # 直接申请的护栏是 used+reserved<max：按 max-used-reserved 补笔数。
    va = auth_view(c, a)
    free_slots = va["max_count"] - va["used_count"] - va["reserved_count"]
    for _ in range(free_slots):
        d_fill = apply(c, [m], region="JP", channel="web")
        check("fill slot before waitlist2",
              d_fill.status_code == 201, d_fill.text)
    wq2b = waitlist(c, [m], ttl=120, region="JP", channel="web")
    check("waitlist2 queued (reserved holds slot)",
          wq2b.status_code == 201 and wq2b.json()["status"] == "pending",
          wq2b.text)
    wq2b_id = wq2b.json()["id"]
    freeze(c, "JP", channel="web")
    r = c.post(f"/api/v1/reservations/{rsv2.json()['id']}/cancel")
    check("cancel existing reservation during freeze 200",
          r.status_code == 200 and r.json()["status"] == "cancelled",
          r.text)
    check("reservation cancel releases quota but no fulfillment",
          c.get(f"/api/v1/waitlists/{wq2b_id}").json()["status"]
          == "pending")
    resume(c, "JP", "web")
    check("waitlist2 fulfilled after resume",
          c.get(f"/api/v1/waitlists/{wq2b_id}").json()["status"]
          == "fulfilled")

    # 恢复后通道不再拒绝新请求（额度此时已用尽 → quota_exhausted，
    # 关键是错误不再是 channel_frozen）。
    r = apply(c, [m], region="JP", channel="web")
    check("new distribution after resume JP not frozen-rejected",
          r.status_code == 422
          and r.json()["error"]["code"] == "distribution_rejected",
          r.text)


def scenario_expiry_during_freeze():
    print("== 5. 冻结不延长有效期：到期预留/候补仍按原有效期结算 ==")
    c = TestClient(app)
    m = make_material(c, "f-ttl")
    a = make_auth(c, m, 1, region="KR", channel="web")

    # 先建既有预留（reserved=1，占满唯一槽）
    rsv = reserve(c, [m], ttl=20, region="KR", channel="web")
    rsv_id = rsv.json()["id"]
    check("reservation created", rsv.status_code == 201, rsv.text)
    # 槽满 → 两个候补按序入队
    wq1 = waitlist(c, [m], ttl=120, region="KR", channel="web")
    wq1_id = wq1.json()["id"]
    wq2 = waitlist(c, [m], ttl=120, region="KR", channel="web")
    wq2_id = wq2.json()["id"]
    check("queued1", wq1.json()["status"] == "pending", wq1.text)
    check("queued2", wq2.json()["status"] == "pending", wq2.text)

    freeze(c, "KR", "web")

    # 队首候补到期：读路径惰性失效，不成交、不占额
    set_waitlist_expiry(wq1_id, "now()")
    r = c.get(f"/api/v1/waitlists/{wq1_id}")
    check("waitlist expired during freeze (lazy read)",
          r.status_code == 200 and r.json()["status"] == "expired"
          and r.json()["expiry_reason"] == "ttl_expired", r.text)

    # 预留到期：单条查询结算为 expired、归还预留额度；有效候补不成交
    force_expire_reservation(rsv_id)
    r = c.get(f"/api/v1/reservations/{rsv_id}")
    check("reservation expired during freeze",
          r.status_code == 200 and r.json()["status"] == "expired",
          r.text)
    v = auth_view(c, a)
    check("expired reservation released reserved_count during freeze",
          v["reserved_count"] == 0 and v["used_count"] == 0
          and v["remaining"] == 1, v)
    check("valid waitlist still pending (no fulfillment during freeze)",
          c.get(f"/api/v1/waitlists/{wq2_id}").json()["status"] == "pending")

    # 逾期确认/取消预留仍被拒绝
    r = c.post(f"/api/v1/reservations/{rsv_id}/confirm")
    check("confirm expired reservation during freeze 409",
          r.status_code == 409 and
          r.json()["error"]["code"] == "reservation_expired", r.text)
    r = c.post(f"/api/v1/reservations/{rsv_id}/cancel")
    check("cancel expired reservation during freeze 409",
          r.status_code == 409 and
          r.json()["error"]["code"] == "reservation_expired", r.text)

    # 恢复：过期候补 wq1 已释放位置；有效候补 wq2 按原顺序成交
    r = resume(c, "KR", "web")
    check("resume KR 200", r.status_code == 200, r.text)
    wb = c.get(f"/api/v1/waitlists/{wq2_id}").json()
    check("valid waitlist fulfilled on resume (expired head skipped)",
          wb["status"] == "fulfilled" and wb["distribution_id"], wb)


def scenario_resume_fifo():
    print("== 6. 恢复时按原受理顺序处理候补，先于新请求占额 ==")
    c = TestClient(app)
    m = make_material(c, "f-fifo")
    a = make_auth(c, m, 2, region="SG", channel="web")

    # 占满 2 槽
    d1 = apply(c, [m], region="SG", channel="web").json()["id"]
    d2 = apply(c, [m], region="SG", channel="web").json()["id"]
    # 两个候补按序入队
    w1 = waitlist(c, [m], ttl=240, region="SG", channel="web").json()["id"]
    w2 = waitlist(c, [m], ttl=240, region="SG", channel="web").json()["id"]

    freeze(c, "SG", "web")
    # 冻结期间撤销两笔，释放 2 槽但不成交
    c.post(f"/api/v1/distributions/{d1}/revoke")
    c.post(f"/api/v1/distributions/{d2}/revoke")
    check("w1 pending during freeze",
          c.get(f"/api/v1/waitlists/{w1}").json()["status"] == "pending")
    check("w2 pending during freeze",
          c.get(f"/api/v1/waitlists/{w2}").json()["status"] == "pending")

    # 恢复瞬间同时打 8 个新直接申请：候补必须先成交占满 2 槽，
    # 新申请全部因额度不足被拒。
    results = []

    def do_resume():
        cc = TestClient(app)
        return resume(cc, "SG", "web").status_code

    def do_apply(i):
        cc = TestClient(app)
        return apply(cc, [m], region="SG", channel="web").status_code

    with ThreadPoolExecutor(max_workers=9) as pool:
        futs = [pool.submit(do_resume)]
        futs += [pool.submit(do_apply, i) for i in range(8)]
        for f in futs:
            results.append(f.result())

    check("resume status 200 under concurrency", 200 in results, results)
    w1b = c.get(f"/api/v1/waitlists/{w1}").json()
    w2b = c.get(f"/api/v1/waitlists/{w2}").json()
    check("w1 fulfilled", w1b["status"] == "fulfilled", w1b)
    check("w2 fulfilled", w2b["status"] == "fulfilled", w2b)
    # FIFO：w1 的成交申请编号不晚于 w2
    check("FIFO order kept",
          w1b["distribution_id"] < w2b["distribution_id"],
          f"{w1b['distribution_id']} {w2b['distribution_id']}")
    # 新申请无一成功（2 槽全被候补吃掉）
    check("no new distribution beat waitlists",
          201 not in results, results)
    v = auth_view(c, a)
    check("exactly 2 used after resume",
          v["used_count"] == 2 and v["remaining"] == 0, v)

    # 成交后的申请可正常撤销（通道已恢复 → 触发后续队列，此处已无候补）
    r = c.post(f"/api/v1/distributions/{w1b['distribution_id']}/revoke")
    check("revoke fulfilled distribution after resume",
          r.status_code == 200, r.text)


def scenario_resume_head_blocking_and_failed():
    print("== 7. 恢复时队首额度不足停止；授权不再匹配标记失败并继续 ==")
    c = TestClient(app)
    m = make_material(c, "f-head")
    a = make_auth(c, m, 1, region="TH", channel="web")

    # 占满唯一槽，排两个候补
    d1 = apply(c, [m], region="TH", channel="web").json()["id"]
    w1 = waitlist(c, [m], ttl=240, region="TH", channel="web").json()["id"]
    w2 = waitlist(c, [m], ttl=240, region="TH", channel="web").json()["id"]

    freeze(c, "TH", channel="web")
    # 冻结期间把授权停用：恢复时两个候补都应 failed（不再匹配）
    c.patch(f"/api/v1/authorizations/{a}", json={"status": "inactive"})
    r = resume(c, "TH", channel="web")
    check("resume with inactive auth 200", r.status_code == 200, r.text)
    w1b = c.get(f"/api/v1/waitlists/{w1}").json()
    w2b = c.get(f"/api/v1/waitlists/{w2}").json()
    check("w1 failed after resume (inactive)",
          w1b["status"] == "failed"
          and w1b["failure_reason"] == "authorization_inactive", w1b)
    check("w2 failed too (queue continues past failures)",
          w2b["status"] == "failed", w2b)

    # 队首额度不足停止本轮的场景
    c.patch(f"/api/v1/authorizations/{a}", json={"status": "active"})
    # 槽仍被 d1 占用 → 新候补 w3 入队（通道已恢复）
    w3 = waitlist(c, [m], ttl=240, region="TH", channel="web")
    check("w3 pending (quota full)",
          w3.status_code == 201 and w3.json()["status"] == "pending",
          w3.text)
    w3_id = w3.json()["id"]
    # 新直接申请先处理队首（仍不足）→ 自身也被拒
    r = apply(c, [m], region="TH", channel="web")
    check("new apply rejected behind blocked head",
          r.status_code == 422, r.text)
    check("w3 still pending",
          c.get(f"/api/v1/waitlists/{w3_id}").json()["status"] == "pending")


def scenario_reschedule_and_migrate_during_freeze():
    print("== 8. 冻结期间改期/改挂仍可进行；释放的额度恢复时才成交候补 ==")
    c = TestClient(app)
    m = make_material(c, "f-mig")
    # a1 max=1 承载申请；a2 先停用（候补排队时不会命中 a2，保证入队）
    a1 = make_auth(c, m, 1, region="AU", channel="web")
    a2 = make_auth(c, m, 5, region="AU", channel="web")
    c.patch(f"/api/v1/authorizations/{a2}", json={"status": "inactive"})

    d1 = apply(c, [m], region="AU", channel="web", occur=OCCUR)
    check("setup d1 approved", d1.status_code == 201, d1.text)
    d1_id = d1.json()["id"]
    check("d1 hit a1",
          d1.json()["items"][0]["authorization"]["id"] == a1, d1.text)

    # a1 满、a2 停用 → 候补纯额度不足入队
    wq = waitlist(c, [m], ttl=240, region="AU", channel="web")
    wq_id = wq.json()["id"]
    check("waitlist pending on full a1",
          wq.status_code == 201 and wq.json()["status"] == "pending",
          wq.text)

    freeze(c, "AU", "web")

    # 改期（同一瞬间幂等路径不受冻结影响）
    r = c.post(f"/api/v1/distributions/{d1_id}/reschedule",
               json={"occur_at": OCCUR})
    check("reschedule same instant during freeze 200",
          r.status_code == 200, r.text)

    # 冻结期间启用 a2，再把 a1 改挂到 a2：申请迁出、a1 停用，
    # a1 的占用被释放到 a2；冻结期间候补不得成交（也不判定失败）。
    c.patch(f"/api/v1/authorizations/{a2}", json={"status": "active"})
    r = c.post("/api/v1/authorizations/migrate",
               json={"source_authorization_id": a1,
                     "replacement_authorization_id": a2})
    check("migrate during freeze 200", r.status_code == 200, r.text)
    check("source inactive after migrate during freeze",
          r.json()["source_authorization"]["status"] == "inactive", r.text)
    check("waitlist NOT fulfilled by migrate during freeze",
          c.get(f"/api/v1/waitlists/{wq_id}").json()["status"] == "pending")

    r = resume(c, "AU", "web")
    check("resume AU 200", r.status_code == 200, r.text)
    wb = c.get(f"/api/v1/waitlists/{wq_id}").json()
    check("waitlist fulfilled on resume onto a2",
          wb["status"] == "fulfilled", wb)
    check("fulfilled onto replacement auth",
          wb["distribution"]["items"][0]["authorization"]["id"] == a2,
          wb)


def scenario_other_channel_unaffected():
    print("== 9. 其他通道不受影响 ==")
    c = TestClient(app)
    m = make_material(c, "f-other")
    a_cn = make_auth(c, m, 2, region="CN", channel="web")
    a_us = make_auth(c, m, 2, region="US", channel="web")
    a_cn_app = make_auth(c, m, 2, region="CN", channel="app")

    freeze(c, "CN", "web")

    r = apply(c, [m], region="US", channel="web")
    check("US/web distribution unaffected", r.status_code == 201, r.text)
    r = apply(c, [m], region="CN", channel="app")
    check("CN/app distribution unaffected", r.status_code == 201, r.text)
    r = reserve(c, [m], ttl=10, region="US", channel="web")
    check("US/web reservation unaffected", r.status_code == 201, r.text)
    r = waitlist(c, [m], ttl=60, region="CN", channel="app")
    check("CN/app waitlist unaffected (fulfilled)",
          r.status_code == 201 and r.json()["status"] == "fulfilled",
          r.text)

    # CN/web 仍拒绝
    r = apply(c, [m], region="CN", channel="web")
    check("CN/web still frozen",
          r.status_code == 422 and
          r.json()["error"]["code"] == "channel_frozen", r.text)

    # 恢复 CN/web 不影响其他通道状态查询
    resume(c, "CN", "web")
    r = apply(c, [m], region="CN", channel="web")
    check("CN/web works after resume", r.status_code == 201, r.text)
    v_us = auth_view(c, a_us)
    check("US quota independent", v_us["used_count"] == 1, v_us)


def scenario_freeze_concurrency():
    print("== 10. 冻结与并发发行同通道串行：绝不超额 ==")
    c = TestClient(app)
    m = make_material(c, "f-conc")
    a = make_auth(c, m, 3, region="DE", channel="web")

    def op(i):
        cc = TestClient(app)
        if i == 0:
            return ("freeze", freeze(cc, "DE", "web", reason="并发冻结").status_code)
        return ("apply", apply(cc, [m], region="DE", channel="web").status_code)

    with ThreadPoolExecutor(max_workers=12) as pool:
        outs = list(pool.map(op, range(12)))

    v = auth_view(c, a)
    approved = sum(1 for kind, s in outs if kind == "apply" and s == 201)
    rejected = [s for kind, s in outs if kind == "apply" and s != 201]
    check("freeze committed",
          any(kind == "freeze" and s == 200 for kind, s in outs), outs)
    check("never over quota", v["used_count"] <= 3, v)
    check("approved count equals used_count",
          approved == v["used_count"], f"{approved} vs {v}")
    check("rejected apply are 422",
          all(s == 422 for s in rejected), outs)

    # 冻结最终生效（freeze 在前则后续都拒绝；在后则已有占额）
    st = state(c, "DE", "web").json()
    check("final state frozen", st["status"] == "frozen", st)

    # 恢复后队列（冻结期间无候补可产生——候补受理也被拒）；直接验证新发行恢复
    resume(c, "DE", "web")
    r = apply(c, [m], region="DE", channel="web")
    # 若已有 3 笔则 422 quota；否则 201——关键是不再是 channel_frozen
    check("after resume not channel_frozen",
          r.status_code in (201, 422) and not (
              r.status_code == 422 and
              r.json().get("error", {}).get("code") == "channel_frozen"),
          r.text)


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    init_db()

    scenario_errors()
    scenario_freeze_lifecycle()
    scenario_block_new_issuance()
    scenario_existing_operations_during_freeze()
    scenario_expiry_during_freeze()
    scenario_resume_fifo()
    scenario_resume_head_blocking_and_failed()
    scenario_reschedule_and_migrate_during_freeze()
    scenario_other_channel_unaffected()
    scenario_freeze_concurrency()

    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
