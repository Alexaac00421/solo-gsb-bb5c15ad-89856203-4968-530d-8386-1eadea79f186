"""候补队列功能与并发安全测试（真实 PostgreSQL + TestClient）。

覆盖需求：
- 提交候补：全部可承接则直接成交；每项均有匹配授权但额度不足才入队；
  缺少匹配授权则逐项拒绝（不占额、不入队）。
- 撤销、预留取消/到期、改期、改挂释放额度后按受理顺序重算队首：
  可整体承接则原子占额转一笔申请；额度仍不足停止本轮；
  授权已不再匹配则标记失败并继续后项。
- 新直接申请须先处理可成交候补，不得抢走其额度。
- 查询返回候补状态、失败原因及生成的申请编号；待处理候补可取消；
  已成交申请沿用现有撤销规则。
- 并发释放与受理不重复生成申请、不超额，账实相符。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_waitlist
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy import update as sa_update

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=CST)
OCT_START = "2026-10-01T00:00:00+08:00"
OCT_END = "2026-11-01T00:00:00+08:00"
OCCUR = "2026-10-15T12:00:00+08:00"

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


def new_client():
    return TestClient(app)


def make_material(client, code):
    return client.post(
        "/api/v1/materials", json={"code": code, "name": code}
    ).json()["id"]


def make_auth(client, mid, count, region="CN", channel="web",
              starts=OCT_START, ends=OCT_END):
    return client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": region,
            "channel": channel,
            "starts_at": starts,
            "ends_at": ends,
            "max_count": count,
        },
    ).json()["id"]


def apply(client, mids, region="CN", channel="web", occur=OCCUR):
    return client.post(
        "/api/v1/distributions",
        json={
            "material_ids": mids,
            "region": region,
            "channel": channel,
            "occur_at": occur,
        },
    )


def waitlist(client, mids, region="CN", channel="web", occur=OCCUR,
             ttl=60):
    return client.post(
        "/api/v1/waitlists",
        json={
            "material_ids": mids,
            "region": region,
            "channel": channel,
            "occur_at": occur,
            "ttl_minutes": ttl,
        },
    )


def get_auth(client, aid):
    return client.get(f"/api/v1/authorizations/{aid}").json()


def get_wait(client, wid):
    return client.get(f"/api/v1/waitlists/{wid}")


def force_expire(reservation_id):
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


def set_waitlist_expiry(waitlist_id, at_sql):
    """直接改写库内候补到期时刻（at_sql 为相对 now 的 SQL，如 now()、
    now() + interval '1 minute'），用于确定性验证有效期边界。"""
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


def books(auth_id):
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
        ok = (
            auth.used_count == approved_items
            and auth.reserved_count == pending_items
            and auth.used_count + auth.reserved_count <= auth.max_count
            and auth.used_count >= 0
            and auth.reserved_count >= 0
        )
        return ok, auth.used_count, auth.reserved_count
    finally:
        db.close()


def main() -> int:
    global failed
    Base.metadata.drop_all(bind=engine)
    init_db()
    c = new_client()

    print("== 1. 全部可承接：候补直接成交 ==")
    m1 = make_material(c, "wl-m1")
    m2 = make_material(c, "wl-m2")
    a1 = make_auth(c, m1, 2)
    a2 = make_auth(c, m2, 1)

    r = waitlist(c, [m1, m2])
    check("直接成交 201", r.status_code == 201, r.text)
    body = r.json()
    check("状态 fulfilled", body["status"] == "fulfilled", body)
    check("生成申请编号", body["distribution_id"] is not None, body)
    check("内嵌申请 approved",
          body["distribution"]["status"] == "approved", body)
    check("内嵌申请两项明细", len(body["distribution"]["items"]) == 2, body)
    check("失败原因为空", body["failure_reason"] is None, body)
    check("fulfilled_at 已记录", body["fulfilled_at"] is not None, body)
    d1 = body["distribution_id"]
    check("申请可查询",
          c.get(f"/api/v1/distributions/{d1}").status_code == 200)
    check("授权1 used=1", get_auth(c, a1)["used_count"] == 1)
    check("授权2 used=1", get_auth(c, a2)["used_count"] == 1)
    wid_immediate = body["id"]

    print("== 2. 每项均有匹配授权但额度不足：入队不占额 ==")
    # m2 额度 1 已用完；m1 还有余量 → 整体入队
    r = waitlist(c, [m1, m2])
    check("入队 201", r.status_code == 201, r.text)
    body = r.json()
    check("状态 pending", body["status"] == "pending", body)
    check("无申请编号", body["distribution_id"] is None, body)
    check("候补素材清单两项", len(body["items"]) == 2, body)
    w1 = body["id"]
    check("入队不占额：授权1 used 仍为 1", get_auth(c, a1)["used_count"] == 1)
    check("入队不占额：授权2 used 仍为 1", get_auth(c, a2)["used_count"] == 1)

    print("== 3. 缺少匹配授权：逐项拒绝 ==")
    r = waitlist(c, [m1, m2], channel="tv")
    check("无匹配 422", r.status_code == 422, r.text)
    err = r.json()["error"]
    check("错误码 waitlist_rejected", err["code"] == "waitlist_rejected", err)
    check("逐项原因两条", len(err["details"]) == 2, err)
    check("原因 no_matching_authorization",
          all(d["reason"] == "no_matching_authorization"
              for d in err["details"]), err)

    # 混合：m1 可承接、m2 无匹配（渠道 tv 只有 m1 授权）→ 整体拒绝
    a1_tv = make_auth(c, m1, 5, channel="tv")
    r = waitlist(c, [m1, m2], channel="tv")
    err = r.json()["error"]
    check("混合场景整体拒绝", r.status_code == 422
          and err["code"] == "waitlist_rejected", r.text)
    check("混合场景仅列失败项",
          len(err["details"]) == 1
          and err["details"][0]["material_id"] == m2
          and err["details"][0]["reason"] == "no_matching_authorization",
          err)

    # 匹配授权均已停用 → authorization_inactive → 拒绝（不入队）
    m3 = make_material(c, "wl-m3")
    a3 = make_auth(c, m3, 1)
    c.patch(f"/api/v1/authorizations/{a3}", json={"status": "inactive"})
    r = waitlist(c, [m3])
    err = r.json()["error"]
    check("授权停用 422",
          r.status_code == 422 and err["code"] == "waitlist_rejected", r.text)
    check("原因 authorization_inactive",
          err["details"][0]["reason"] == "authorization_inactive", err)
    c.patch(f"/api/v1/authorizations/{a3}", json={"status": "active"})

    # 额度不足 + 无匹配 混合：整体拒绝
    r = waitlist(c, [m2, m3], region="US")
    check("US 全不匹配 422", r.status_code == 422, r.text)

    print("== 4. 参数校验 ==")
    r = waitlist(c, [m1, m1])
    check("重复素材 422",
          r.status_code == 422
          and r.json()["error"]["code"] == "duplicate_material_items", r.text)
    r = waitlist(c, [m1, 99999])
    check("未知素材 422",
          r.status_code == 422
          and r.json()["error"]["code"] == "unknown_material", r.text)
    r = c.post("/api/v1/waitlists", json={
        "material_ids": [m1], "region": "CN", "channel": "web",
        "occur_at": "2026-10-15T12:00:00", "ttl_minutes": 30})
    check("无时区 422", r.status_code == 422, r.text)
    r = c.post("/api/v1/waitlists", json={
        "material_ids": [m1], "region": "CN", "channel": "web",
        "occur_at": "2026-10-15T12:00:00+08:00"})
    check("缺少 ttl_minutes 422", r.status_code == 422, r.text)
    for bad_ttl in (0, -5, 1441, 9999):
        r = c.post("/api/v1/waitlists", json={
            "material_ids": [m1], "region": "CN", "channel": "web",
            "occur_at": "2026-10-15T12:00:00+08:00",
            "ttl_minutes": bad_ttl})
        check(f"ttl 超范围 {bad_ttl} 422", r.status_code == 422, r.text)
    # ttl 超范围 / 缺字段即使缺匹配授权，也因请求体校验先被拒（不入队）
    r = c.post("/api/v1/waitlists", json={
        "material_ids": [m1], "region": "ZZ", "channel": "nope",
        "occur_at": "2026-10-15T12:00:00+08:00", "ttl_minutes": 0})
    check("字段非法先 422（不进入核验）", r.status_code == 422, r.text)
    r = get_wait(c, 99999)
    check("未知候补 404",
          r.status_code == 404 and r.json()["error"]["code"] == "not_found",
          r.text)

    print("== 5. 撤销释放额度：队首自动成交 ==")
    # w1 = [m1, m2] 等待 m2 的额度；撤销 m2 上的申请 d1（d1 含 m1+m2）
    r = c.post(f"/api/v1/distributions/{d1}/revoke")
    check("撤销 200", r.status_code == 200, r.text)
    body = get_wait(c, w1).json()
    check("队首已成交", body["status"] == "fulfilled", body)
    check("成交生成新申请", body["distribution_id"] is not None
          and body["distribution_id"] != d1, body)
    check("授权1 被重新占用", get_auth(c, a1)["used_count"] == 1)
    check("授权2 被重新占用", get_auth(c, a2)["used_count"] == 1)
    d_w1 = body["distribution_id"]
    check("成交申请可查询且 approved",
          c.get(f"/api/v1/distributions/{d_w1}").json()["status"]
          == "approved")

    print("== 6. 已成交申请沿用现有撤销规则 ==")
    r = c.post(f"/api/v1/distributions/{d_w1}/revoke")
    check("成交申请可撤销", r.status_code == 200, r.text)
    r = c.post(f"/api/v1/distributions/{d_w1}/revoke")
    check("重复撤销 409",
          r.status_code == 409
          and r.json()["error"]["code"] == "already_revoked", r.text)

    print("== 7. FIFO：按受理顺序成交 ==")
    # 独立素材与授权（额度 1），避免受前面用例占用影响
    mf = make_material(c, "wl-fifo")
    af = make_auth(c, mf, 1)
    r = apply(c, [mf])
    check("直接申请占满额度", r.status_code == 201, r.text)
    d_m1 = r.json()["id"]
    r1 = waitlist(c, [mf])
    r2 = waitlist(c, [mf])
    w_fifo1, w_fifo2 = r1.json()["id"], r2.json()["id"]
    check("两笔均入队", r1.json()["status"] == "pending"
          and r2.json()["status"] == "pending",
          f'{r1.json()["status"]} {r2.json()["status"]}')
    c.post(f"/api/v1/distributions/{d_m1}/revoke")
    check("队首先成交", get_wait(c, w_fifo1).json()["status"] == "fulfilled")
    check("次笔仍等待", get_wait(c, w_fifo2).json()["status"] == "pending")
    # 再释放一次（撤销队首生成的申请）→ 次笔成交
    d_fifo1 = get_wait(c, w_fifo1).json()["distribution_id"]
    c.post(f"/api/v1/distributions/{d_fifo1}/revoke")
    check("次笔随后成交", get_wait(c, w_fifo2).json()["status"] == "fulfilled")

    print("== 8. 队首额度不足停止本轮（不跳过队首） ==")
    # 状态：a1（m1，额度 2）空闲；a2（m2，额度 1）被 d_m2 占满
    r = apply(c, [m2])
    d_m2 = r.json()["id"]
    # w_head = [m1, m2]：m2 满 → 入队；w_tail = [m2]：m2 满 → 入队
    w_head = waitlist(c, [m1, m2]).json()["id"]
    w_tail = waitlist(c, [m2]).json()["id"]
    check("队首入队", get_wait(c, w_head).json()["status"] == "pending")
    check("次笔入队", get_wait(c, w_tail).json()["status"] == "pending")
    # 释放 m1 的额度（队首不需要更多 m1 也无法整体承接）：
    # 先占满 a1 再撤销一笔 → 队列重算仍停在队首，a1 释放的额度不被占用
    d_a1x = apply(c, [m1]).json()["id"]
    d_a1y = apply(c, [m1]).json()["id"]
    c.post(f"/api/v1/distributions/{d_a1x}/revoke")
    check("释放非瓶颈额度：队首仍等待",
          get_wait(c, w_head).json()["status"] == "pending")
    check("释放非瓶颈额度：次笔仍等待",
          get_wait(c, w_tail).json()["status"] == "pending")
    check("释放的 a1 额度未被候补占用", get_auth(c, a1)["used_count"] == 1)
    # 撤销 m2 占用 → 队首整体可承接（m1 也有余量）→ 成交；随后额度尽，次笔等待
    c.post(f"/api/v1/distributions/{d_m2}/revoke")
    check("队首整体成交", get_wait(c, w_head).json()["status"] == "fulfilled")
    check("次笔仍等待（额度已尽）",
          get_wait(c, w_tail).json()["status"] == "pending")
    c.post(f"/api/v1/distributions/{d_a1y}/revoke")  # 清理 a1 占用

    print("== 9. 预留取消/到期释放：队首成交 ==")
    # m2 的 a2 额度 1 当前空闲（d_m2 已撤销、w_head 用的是 a1+a2？）
    # w_head 成交占用了 a1 与 a2：a2 used=1 → w_tail 仍在等 a2。
    r = c.post("/api/v1/reservations", json={
        "material_ids": [m2], "region": "CN", "channel": "web",
        "occur_at": OCCUR, "ttl_minutes": 30})
    check("a2 满时预留被拒", r.status_code == 422, r.text)
    # 撤销 w_head 的申请释放 a2，w_tail 成交
    d_head = get_wait(c, w_head).json()["distribution_id"]
    c.post(f"/api/v1/distributions/{d_head}/revoke")
    check("w_tail 成交", get_wait(c, w_tail).json()["status"] == "fulfilled")

    # 预留取消释放：新建素材 m4 额度 1，预留占额 → 候补入队 → 取消预留 → 成交
    m4 = make_material(c, "wl-m4")
    a4 = make_auth(c, m4, 1)
    r = c.post("/api/v1/reservations", json={
        "material_ids": [m4], "region": "CN", "channel": "web",
        "occur_at": OCCUR, "ttl_minutes": 30})
    resv1 = r.json()["id"]
    check("预留占额成功", r.status_code == 201, r.text)
    w_r1 = waitlist(c, [m4]).json()["id"]
    check("预留占额下候补入队",
          get_wait(c, w_r1).json()["status"] == "pending")
    c.post(f"/api/v1/reservations/{resv1}/cancel")
    check("取消预留后队首成交",
          get_wait(c, w_r1).json()["status"] == "fulfilled")

    # 预留到期释放：预留占额 → 候补入队 → 强制到期 → 查询触发结算 → 成交
    d_wr1 = get_wait(c, w_r1).json()["distribution_id"]
    c.post(f"/api/v1/distributions/{d_wr1}/revoke")  # 释放 a4 供本场景使用
    r = c.post("/api/v1/reservations", json={
        "material_ids": [m4], "region": "CN", "channel": "web",
        "occur_at": OCCUR, "ttl_minutes": 30})
    resv2 = r.json()["id"]
    check("到期场景预留占额成功", r.status_code == 201, r.text)
    w_r2 = waitlist(c, [m4]).json()["id"]
    check("到期前候补等待", get_wait(c, w_r2).json()["status"] == "pending")
    force_expire(resv2)
    body = get_wait(c, w_r2).json()  # 查询触发通道结算 → 释放 → 成交
    check("预留到期后队首成交", body["status"] == "fulfilled", body)
    ok, used, reserved = books(a4)
    check("a4 账实相符", ok, f"used={used} reserved={reserved}")

    print("== 10. 新直接申请不得抢走可成交候补的额度 ==")
    m5 = make_material(c, "wl-m5")
    a5 = make_auth(c, m5, 1)
    # 预留占满唯一额度；候补入队；预留到期但尚无通道写事务结算
    r = c.post("/api/v1/reservations", json={
        "material_ids": [m5], "region": "CN", "channel": "web",
        "occur_at": OCCUR, "ttl_minutes": 30})
    resv3 = r.json()["id"]
    w_steal = waitlist(c, [m5]).json()["id"]
    force_expire(resv3)
    # 新直接申请：须先结算到期、成交候补，自己因额度已占而被拒
    r = apply(c, [m5])
    check("直接申请被拒（额度归候补）",
          r.status_code == 422
          and r.json()["error"]["code"] == "distribution_rejected", r.text)
    check("候补已成交", get_wait(c, w_steal).json()["status"] == "fulfilled")
    check("a5 used=1（候补的申请）", get_auth(c, a5)["used_count"] == 1)
    ok, used, reserved = books(a5)
    check("a5 账实相符", ok, f"used={used} reserved={reserved}")

    print("== 11. 改期释放额度：队首成交 ==")
    m6 = make_material(c, "wl-m6")
    a6_oct = make_auth(c, m6, 1, ends="2026-11-01T00:00:00+08:00")
    a6_nov = make_auth(c, m6, 1,
                       starts="2026-11-01T00:00:00+08:00",
                       ends="2026-12-01T00:00:00+08:00")
    r = apply(c, [m6])  # 占用 10 月授权
    d_m6 = r.json()["id"]
    w_m6 = waitlist(c, [m6]).json()["id"]
    check("改期前候补等待", get_wait(c, w_m6).json()["status"] == "pending")
    r = c.post(f"/api/v1/distributions/{d_m6}/reschedule",
               json={"occur_at": "2026-11-10T12:00:00+08:00"})
    check("改期成功", r.status_code == 200, r.text)
    check("改期释放后队首成交",
          get_wait(c, w_m6).json()["status"] == "fulfilled")
    check("10 月授权被候补占用", get_auth(c, a6_oct)["used_count"] == 1)
    check("11 月授权被改期占用", get_auth(c, a6_nov)["used_count"] == 1)

    print("== 12. 改挂：授权不再匹配 → 队首标记失败并继续后项 ==")
    m7 = make_material(c, "wl-m7")
    m8 = make_material(c, "wl-m8")
    a7 = make_auth(c, m7, 1)
    a8 = make_auth(c, m8, 1)
    d7 = apply(c, [m7]).json()["id"]   # 占满 a7（发行时刻 10-15）
    d8 = apply(c, [m8]).json()["id"]   # 占满 a8
    # 队首：等 a7，发行时刻 10-20（替代授权不覆盖该时刻）
    w_fail = waitlist(c, [m7], occur="2026-10-20T12:00:00+08:00").json()["id"]
    w_next = waitlist(c, [m8]).json()["id"]    # 次笔：等 a8
    # 替代授权只覆盖 d7 的发行时刻（10-15），不覆盖 w_fail 的时刻（10-20）
    a7_new = make_auth(c, m7, 5,
                       starts="2026-10-15T00:00:00+08:00",
                       ends="2026-10-16T00:00:00+08:00")
    r = c.post("/api/v1/authorizations/migrate",
               json={"source_authorization_id": a7,
                     "replacement_authorization_id": a7_new})
    check("改挂成功", r.status_code == 200, r.text)
    body = get_wait(c, w_fail).json()
    check("队首标记失败", body["status"] == "failed", body)
    check("失败原因 authorization_inactive",
          body["failure_reason"] == "authorization_inactive", body)
    check("失败无申请编号", body["distribution_id"] is None, body)
    check("failed_at 已记录", body["failed_at"] is not None, body)
    check("次笔未受影响仍等待",
          get_wait(c, w_next).json()["status"] == "pending")
    # 释放 a8 → 次笔成交（队首失败不阻塞后续）
    c.post(f"/api/v1/distributions/{d8}/revoke")
    check("队首失败后后项可成交",
          get_wait(c, w_next).json()["status"] == "fulfilled")

    print("== 13. 取消候补 ==")
    m9 = make_material(c, "wl-m9")
    a9 = make_auth(c, m9, 1)
    d9 = apply(c, [m9]).json()["id"]
    w_c1 = waitlist(c, [m9]).json()["id"]
    r = c.post(f"/api/v1/waitlists/{w_c1}/cancel")
    check("取消 200", r.status_code == 200, r.text)
    check("状态 cancelled", r.json()["status"] == "cancelled", r.json())
    check("cancelled_at 已记录", r.json()["cancelled_at"] is not None)
    r = c.post(f"/api/v1/waitlists/{w_c1}/cancel")
    check("重复取消 409",
          r.status_code == 409
          and r.json()["error"]["code"] == "waitlist_already_cancelled",
          r.text)
    # 已成交不可取消
    c.post(f"/api/v1/distributions/{d9}/revoke")  # 触发队列（无 pending）
    w_c2 = waitlist(c, [m9]).json()["id"]  # 直接成交
    check("空闲时直接成交", get_wait(c, w_c2).json()["status"] == "fulfilled")
    r = c.post(f"/api/v1/waitlists/{w_c2}/cancel")
    check("已成交取消 409",
          r.status_code == 409
          and r.json()["error"]["code"] == "waitlist_already_fulfilled",
          r.text)
    # 已失败不可取消
    r = c.post(f"/api/v1/waitlists/{w_fail}/cancel")
    check("已失败取消 409",
          r.status_code == 409
          and r.json()["error"]["code"] == "waitlist_already_failed",
          r.text)
    r = c.post("/api/v1/waitlists/99999/cancel")
    check("取消未知候补 404", r.status_code == 404, r.text)

    print("== 14. 查询与列表过滤 ==")
    r = c.get("/api/v1/waitlists")
    check("列表 200", r.status_code == 200, r.text)
    total = len(r.json())
    r = c.get("/api/v1/waitlists", params={"status": "pending"})
    check("过滤 pending", all(w["status"] == "pending" for w in r.json()))
    r = c.get("/api/v1/waitlists", params={"status": "fulfilled"})
    check("过滤 fulfilled",
          all(w["status"] == "fulfilled" for w in r.json())
          and len(r.json()) >= 1)
    r = c.get("/api/v1/waitlists", params={"status": "failed"})
    check("过滤 failed 含失败原因",
          any(w["id"] == w_fail
              and w["failure_reason"] == "authorization_inactive"
              for w in r.json()), r.text)
    r = c.get("/api/v1/waitlists", params={"status": "cancelled"})
    check("过滤 cancelled",
          any(w["id"] == w_c1 for w in r.json()), r.text)
    r = c.get("/api/v1/waitlists", params={"status": "expired"})
    check("过滤 expired 200", r.status_code == 200, r.text)
    check("过滤 expired 只含失效项",
          all(w["status"] == "expired" for w in r.json()), r.text)
    n = sum(len(c.get("/api/v1/waitlists",
                      params={"status": s}).json())
            for s in ("pending", "fulfilled", "failed", "cancelled",
                      "expired"))
    check("五态合计等于总数", n == total, f"{n} != {total}")
    r = c.get("/api/v1/waitlists", params={"status": "bogus"})
    check("非法过滤值 422",
          r.status_code == 422
          and r.json()["error"]["code"] == "invalid_waitlist_status",
          r.text)

    print("== 15. 候补成交沿用重叠授权选择顺序 ==")
    m10 = make_material(c, "wl-m10")
    a10_late = make_auth(c, m10, 1, ends="2026-11-15T00:00:00+08:00")
    a10_early = make_auth(c, m10, 1, ends="2026-11-01T00:00:00+08:00")
    d10a = apply(c, [m10]).json()["id"]   # 占 a10_early（到期早优先）
    d10b = apply(c, [m10]).json()["id"]   # 占 a10_late
    w_ord = waitlist(c, [m10]).json()["id"]
    c.post(f"/api/v1/distributions/{d10a}/revoke")
    c.post(f"/api/v1/distributions/{d10b}/revoke")
    body = get_wait(c, w_ord).json()
    check("释放后成交", body["status"] == "fulfilled", body)
    hit = body["distribution"]["items"][0]["authorization"]["id"]
    check("命中到期早的授权", hit == a10_early,
          f"hit={hit} early={a10_early} late={a10_late}")

    print("== 16. 多素材候补整体成交（部分释放不成交） ==")
    m11 = make_material(c, "wl-m11")
    m12 = make_material(c, "wl-m12")
    a11 = make_auth(c, m11, 1)
    a12 = make_auth(c, m12, 1)
    d11 = apply(c, [m11]).json()["id"]
    d12 = apply(c, [m12]).json()["id"]
    w_multi = waitlist(c, [m11, m12]).json()["id"]
    c.post(f"/api/v1/distributions/{d11}/revoke")
    check("仅释放一项仍等待",
          get_wait(c, w_multi).json()["status"] == "pending")
    check("释放的额度未被候补占用", get_auth(c, a11)["used_count"] == 0)
    c.post(f"/api/v1/distributions/{d12}/revoke")
    body = get_wait(c, w_multi).json()
    check("全部释放后整体成交", body["status"] == "fulfilled", body)
    check("成交申请含两项",
          len(body["distribution"]["items"]) == 2, body)
    ok11, _, _ = books(a11)
    ok12, _, _ = books(a12)
    check("a11/a12 账实相符", ok11 and ok12)

    print("== 17. 并发：多候补 + 并发释放，不重复成交不超额 ==")
    m13 = make_material(c, "wl-m13")
    a13 = make_auth(c, m13, 3)
    holders = [apply(c, [m13]).json()["id"] for _ in range(3)]

    def submit_wait(i):
        cc = new_client()
        return cc.post("/api/v1/waitlists", json={
            "material_ids": [m13], "region": "CN", "channel": "web",
            "occur_at": OCCUR, "ttl_minutes": 60}).json()["id"]

    with ThreadPoolExecutor(max_workers=10) as ex:
        wids = list(ex.map(submit_wait, range(10)))
    pend = c.get("/api/v1/waitlists", params={"status": "pending"}).json()
    check("10 笔候补全部入队",
          len([w for w in pend if w["id"] in wids]) == 10,
          f"{len(pend)}")

    def revoke_one(did):
        cc = new_client()
        return cc.post(f"/api/v1/distributions/{did}/revoke").status_code

    with ThreadPoolExecutor(max_workers=3) as ex:
        codes = list(ex.map(revoke_one, holders))
    check("三笔撤销全部成功", codes == [200, 200, 200], codes)

    fulfilled = [w for w in
                 c.get("/api/v1/waitlists", params={"status": "fulfilled"}
                       ).json() if w["id"] in wids]
    check("恰 3 笔候补成交", len(fulfilled) == 3,
          f"fulfilled={len(fulfilled)}")
    check("成交按受理顺序（前 3 笔）",
          sorted(w["id"] for w in fulfilled) == sorted(wids)[:3],
          [w["id"] for w in fulfilled])
    dist_ids = [w["distribution_id"] for w in fulfilled]
    check("申请编号不重复", len(set(dist_ids)) == 3, dist_ids)
    still = [w for w in
             c.get("/api/v1/waitlists", params={"status": "pending"}
                   ).json() if w["id"] in wids]
    check("其余 7 笔仍等待", len(still) == 7, f"{len(still)}")
    ok, used, reserved = books(a13)
    check("a13 账实相符且未超额", ok and used == 3,
          f"used={used} reserved={reserved}")

    print("== 18. 并发：直接申请与候补受理混合，不超额 ==")
    m14 = make_material(c, "wl-m14")
    a14 = make_auth(c, m14, 5)

    def direct(i):
        return new_client().post("/api/v1/distributions", json={
            "material_ids": [m14], "region": "CN", "channel": "web",
            "occur_at": OCCUR}).status_code

    def wait(i):
        return new_client().post("/api/v1/waitlists", json={
            "material_ids": [m14], "region": "CN", "channel": "web",
            "occur_at": OCCUR, "ttl_minutes": 60}).json()["status"]

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(direct, i) for i in range(10)]
        futs += [ex.submit(wait, i) for i in range(10)]
        results = [f.result() for f in as_completed(futs)]
    direct_ok = sum(1 for x in results if x == 201)
    wait_fulfilled = sum(1 for x in results if x == "fulfilled")
    wait_pending = sum(1 for x in results if x == "pending")
    check("成交总数恰为额度 5", direct_ok + wait_fulfilled == 5,
          f"direct={direct_ok} wait_fulfilled={wait_fulfilled}")
    check("其余候补均入队等待", wait_pending == 10 - wait_fulfilled,
          f"pending={wait_pending}")
    ok, used, reserved = books(a14)
    check("a14 账实相符且 used=5", ok and used == 5, f"used={used}")

    print("== 19. 并发：重复取消同一候补，恰一次生效 ==")
    m15 = make_material(c, "wl-m15")
    a15 = make_auth(c, m15, 1)
    apply(c, [m15])
    w_dup = waitlist(c, [m15]).json()["id"]

    def cancel_w(i):
        return new_client().post(
            f"/api/v1/waitlists/{w_dup}/cancel").status_code

    with ThreadPoolExecutor(max_workers=6) as ex:
        codes = list(ex.map(cancel_w, range(6)))
    check("取消恰一次 200", codes.count(200) == 1, codes)
    check("其余均 409", codes.count(409) == 5, codes)
    check("终态 cancelled",
          get_wait(c, w_dup).json()["status"] == "cancelled")

    print("== 20. 候补不占用额度：pending 期间额度可被统计验证 ==")
    ok, used, reserved = books(a15)
    check("a15 used=1 且无预留占用", ok and used == 1 and reserved == 0,
          f"used={used} reserved={reserved}")

    print("== 21. 通道隔离：他通道释放不触发本通道队列 ==")
    # 独立地区，避免受前面用例在同通道遗留的候补 backlog 影响（严格 FIFO）
    m16 = make_material(c, "wl-m16")
    a16_cn = make_auth(c, m16, 1, region="ISO1")
    a16_us = make_auth(c, m16, 1, region="ISO2")
    d16_cn = apply(c, [m16], region="ISO1").json()["id"]
    d16_us = apply(c, [m16], region="ISO2").json()["id"]
    w16 = waitlist(c, [m16], region="ISO1").json()["id"]  # ISO1 满 → 入队
    check("候补入队", get_wait(c, w16).json()["status"] == "pending")
    c.post(f"/api/v1/distributions/{d16_us}/revoke")     # 释放 ISO2 通道
    check("他通道释放不成交", get_wait(c, w16).json()["status"] == "pending")
    c.post(f"/api/v1/distributions/{d16_cn}/revoke")     # 释放 ISO1 通道
    check("本通道释放成交", get_wait(c, w16).json()["status"] == "fulfilled")

    print("== 22. 授权停用后队列重算标记失败 ==")
    m17 = make_material(c, "wl-m17")
    a17 = make_auth(c, m17, 1, region="ISO3")
    d17 = apply(c, [m17], region="ISO3").json()["id"]
    # 长有效期：本用例验证「授权停用」失败路径，排除到期失效干扰。
    w17 = waitlist(c, [m17], region="ISO3", ttl=1440).json()["id"]
    check("候补入队", get_wait(c, w17).json()["status"] == "pending")
    c.patch(f"/api/v1/authorizations/{a17}", json={"status": "inactive"})
    check("停用后未触发重算仍等待",
          get_wait(c, w17).json()["status"] == "pending")
    c.post(f"/api/v1/distributions/{d17}/revoke")  # 撤销触发队列重算
    body = get_wait(c, w17).json()
    check("重算标记 failed", body["status"] == "failed", body)
    check("失败原因 authorization_inactive",
          body["failure_reason"] == "authorization_inactive", body)

    print("== 23. 有效期字段：受理时记录 ttl/expires_at ==")
    mt = make_material(c, "wl-ttl")
    at = make_auth(c, mt, 1, region="ISOT")

    # 入队路径
    dt = apply(c, [mt], region="ISOT").json()["id"]
    r = waitlist(c, [mt], region="ISOT", ttl=90)
    check("入队 201", r.status_code == 201, r.text)
    body = r.json()
    check("回显 ttl_minutes=90", body["ttl_minutes"] == 90, body)
    check("回显 expires_at", body["expires_at"] is not None, body)
    check("pending 无失效时刻", body["expired_at"] is None, body)
    check("pending 无失效原因", body["expiry_reason"] is None, body)
    created = datetime.fromisoformat(body["created_at"])
    expires = datetime.fromisoformat(body["expires_at"])
    delta = (expires - created).total_seconds()
    check("expires_at ≈ created_at + 90min",
          abs(delta - 90 * 60) <= 5, f"delta={delta}")
    wt = body["id"]

    # ttl 边界 1 与 1440 可受理（额度满 → 入队）
    check("ttl=1 可受理",
          waitlist(c, [mt], region="ISOT", ttl=1).status_code == 201)
    check("ttl=1440 可受理",
          waitlist(c, [mt], region="ISOT", ttl=1440).status_code == 201)

    # 直接成交路径同样记录有效期：先撤销 dt，wt（队首）成交，其后两笔
    # 仍排队（额度仅 1）；再撤销 wt 生成的申请会成交下一笔，故另取一个
    # 空闲通道验证「直接成交」回显。
    c.post(f"/api/v1/distributions/{dt}/revoke")  # 触发队列（wt 成交）
    check("wt 在有效期内成交", get_wait(c, wt).json()["status"] == "fulfilled")

    mt2 = make_material(c, "wl-ttl2")
    make_auth(c, mt2, 1, region="ISOT2")
    r = waitlist(c, [mt2], region="ISOT2", ttl=30)
    body = r.json()
    check("空闲直接成交 201", r.status_code == 201, r.text)
    check("直接成交 fulfilled", body["status"] == "fulfilled", body)
    check("直接成交也回显 ttl", body["ttl_minutes"] == 30, body)
    check("直接成交也回显 expires_at",
          body["expires_at"] is not None, body)
    check("直接成交无失效信息",
          body["expired_at"] is None and body["expiry_reason"] is None, body)

    print("== 24. 队首过期：标记 expired、释放位置、后续按序成交 ==")
    me = make_material(c, "wl-exp1")
    ae = make_auth(c, me, 1, region="ISOE1")
    de = apply(c, [me], region="ISOE1").json()["id"]
    we_head = waitlist(c, [me], region="ISOE1", ttl=1).json()["id"]
    check("队首排队",
          get_wait(c, we_head).json()["status"] == "pending")
    # 恰在到期时刻：左闭右开即失效
    set_waitlist_expiry(we_head, "now()")
    # 释放唯一额度触发本通道队列：队首已过期 → 标记 expired 并释放位置，
    # 释放出的额度不会被死候补占用。
    c.post(f"/api/v1/distributions/{de}/revoke")
    body = get_wait(c, we_head).json()
    check("队首标记 expired", body["status"] == "expired", body)
    check("失效原因 ttl_expired",
          body["expiry_reason"] == "ttl_expired", body)
    check("失效时刻已记录", body["expired_at"] is not None, body)
    check("失效无申请编号", body["distribution_id"] is None, body)
    check("失效无 failure_reason", body["failure_reason"] is None, body)
    check("有效期仍可查询",
          body["ttl_minutes"] == 1 and body["expires_at"] is not None, body)
    check("过期候补不占额 ae used=0", get_auth(c, ae)["used_count"] == 0)
    ok_e, used_e, _ = books(ae)
    check("ae 账实相符", ok_e and used_e == 0, f"used={used_e}")

    # 过期队首释放位置后，其后的有效候补可按序成交
    me2 = make_material(c, "wl-exp2")
    ae2 = make_auth(c, me2, 1, region="ISOE2")
    de2 = apply(c, [me2], region="ISOE2").json()["id"]
    we_old = waitlist(c, [me2], region="ISOE2", ttl=1).json()["id"]
    we_next = waitlist(c, [me2], region="ISOE2", ttl=1440).json()["id"]
    set_waitlist_expiry(we_old, "now() - interval '1 second'")
    set_waitlist_expiry(we_next, "now() + interval '10 minutes'")
    c.post(f"/api/v1/distributions/{de2}/revoke")
    check("过期队首 expired",
          get_wait(c, we_old).json()["status"] == "expired")
    body = get_wait(c, we_next).json()
    check("其后有效候补按序成交", body["status"] == "fulfilled", body)
    check("成交申请编号", body["distribution_id"] is not None, body)
    check("ae2 被有效候补占用 used=1",
          get_auth(c, ae2)["used_count"] == 1)

    print("== 25. 有效期内正常成交（未过期不受影响） ==")
    mv = make_material(c, "wl-valid")
    av = make_auth(c, mv, 1, region="ISOV")
    dv = apply(c, [mv], region="ISOV").json()["id"]
    wv = waitlist(c, [mv], region="ISOV", ttl=60).json()["id"]
    # expires_at 设为未来 10 分钟（仍有效），释放额度应正常成交而非失效
    set_waitlist_expiry(wv, "now() + interval '10 minutes'")
    c.post(f"/api/v1/distributions/{dv}/revoke")
    body = get_wait(c, wv).json()
    check("有效期内释放 → 成交而非过期",
          body["status"] == "fulfilled", body)
    check("成交有申请编号", body["distribution_id"] is not None, body)
    check("成交无失效信息", body["expired_at"] is None, body)

    print("== 26. 过期查询与取消语义 ==")
    mx = make_material(c, "wl-x")
    ax = make_auth(c, mx, 1, region="ISOX")
    dx = apply(c, [mx], region="ISOX").json()["id"]
    wx = waitlist(c, [mx], region="ISOX", ttl=5).json()["id"]
    set_waitlist_expiry(wx, "now()")
    # 单条查询触发通道惰性失效（无需等待额度释放事件）
    body = get_wait(c, wx).json()
    check("查询即惰性标记 expired", body["status"] == "expired", body)
    check("查询返回失效时刻", body["expired_at"] is not None, body)
    check("查询返回失效原因", body["expiry_reason"] == "ttl_expired", body)
    # 取消已过期候补 → 409 waitlist_already_expired
    r = c.post(f"/api/v1/waitlists/{wx}/cancel")
    check("取消过期候补 409",
          r.status_code == 409
          and r.json()["error"]["code"] == "waitlist_already_expired",
          r.text)
    # 即便随后释放额度，过期候补也不成交
    c.post(f"/api/v1/distributions/{dx}/revoke")
    check("释放后仍 expired",
          get_wait(c, wx).json()["status"] == "expired")
    check("过期后额度未被它占用", get_auth(c, ax)["used_count"] == 0)

    # 列表按 expired 过滤可查到（已落库 + 派生两种都覆盖）
    my = make_material(c, "wl-y")
    ay = make_auth(c, my, 1, region="ISOY")
    apply(c, [my], region="ISOY")
    wy = waitlist(c, [my], region="ISOY", ttl=3).json()["id"]
    set_waitlist_expiry(wy, "now()")
    # 不触发任何写事务，直接列表：库内仍 pending，按 expired 派生呈现
    rows = c.get("/api/v1/waitlists", params={"status": "expired"}).json()
    check("列表派生 expired 含 wy",
          any(w["id"] == wy and w["status"] == "expired"
              and w["expiry_reason"] == "ttl_expired" for w in rows),
          str([(w["id"], w["status"]) for w in rows]))
    rows_pending = c.get(
        "/api/v1/waitlists", params={"status": "pending"}).json()
    check("派生过期候补不计入 pending",
          all(w["id"] != wy for w in rows_pending))
    # 派生过期候补取消时落库为 expired 并返回 409
    r = c.post(f"/api/v1/waitlists/{wy}/cancel")
    check("派生过期候补取消亦 409",
          r.status_code == 409
          and r.json()["error"]["code"] == "waitlist_already_expired",
          r.text)
    check("取消后已落库 expired",
          get_wait(c, wy).json()["status"] == "expired")

    print("== 27. 队首过期后，新直接申请可用释放出的额度（不被死候补阻塞） ==")
    mn = make_material(c, "wl-next")
    an = make_auth(c, mn, 1, region="ISON")
    dn = apply(c, [mn], region="ISON").json()["id"]
    wn = waitlist(c, [mn], region="ISON", ttl=1).json()["id"]
    set_waitlist_expiry(wn, "now()")
    # 新直接申请：先处理队列（过期候补失效），再判定自身 → 可用额度通过
    r = c.post(f"/api/v1/distributions/{dn}/revoke")
    check("撤销成功", r.status_code == 200, r.text)
    r = apply(c, [mn], region="ISON")
    check("过期候补不阻塞新直接申请", r.status_code == 201, r.text)
    check("候补终态 expired",
          get_wait(c, wn).json()["status"] == "expired")
    check("授权被新申请占用 used=1", get_auth(c, an)["used_count"] == 1)

    print("== 28. 多笔过期一次性清理，仍按受理顺序成交有效候补 ==")
    mg = make_material(c, "wl-gap")
    ag = make_auth(c, mg, 3, region="ISOG")
    holders_g = [apply(c, [mg], region="ISOG").json()["id"] for _ in range(3)]
    wg1 = waitlist(c, [mg], region="ISOG", ttl=1).json()["id"]
    wg2 = waitlist(c, [mg], region="ISOG", ttl=1).json()["id"]
    wg3 = waitlist(c, [mg], region="ISOG", ttl=1440).json()["id"]
    wg4 = waitlist(c, [mg], region="ISOG", ttl=1440).json()["id"]
    # 前两笔过期，后两笔有效；一次撤销释放 1 个额度
    set_waitlist_expiry(wg1, "now()")
    set_waitlist_expiry(wg2, "now() - interval '1 minute'")
    c.post(f"/api/v1/distributions/{holders_g[0]}/revoke")
    check("wg1 expired", get_wait(c, wg1).json()["status"] == "expired")
    check("wg2 expired", get_wait(c, wg2).json()["status"] == "expired")
    check("wg3（第一笔有效）成交",
          get_wait(c, wg3).json()["status"] == "fulfilled")
    check("wg4 仍等待（严格 FIFO，额度已尽）",
          get_wait(c, wg4).json()["status"] == "pending")
    c.post(f"/api/v1/distributions/{holders_g[1]}/revoke")
    check("再释放后 wg4 成交",
          get_wait(c, wg4).json()["status"] == "fulfilled")
    ok_g, used_g, resv_g = books(ag)
    check("ag 账实相符：2 笔有效候补成交、1 笔存量占用",
          ok_g and used_g == 3, f"used={used_g} resv={resv_g}")

    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
