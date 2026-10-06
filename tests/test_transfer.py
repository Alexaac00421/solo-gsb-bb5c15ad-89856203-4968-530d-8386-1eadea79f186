"""授权间额度转拨功能与并发安全测试（真实 PostgreSQL + TestClient）。

覆盖需求：
- 同一素材/地区/渠道下两条不同授权间转拨未占用次数；授权时段可以不同；
  既有申请与预留的归属不变，仅原子调整两端 max_count。
- 受理条件：两端均启用且通道未冻结；先结算到期预留并按原顺序处理候补，
  再以 max_count-used_count-reserved_count 判定可转出量；不足、转出后
  源额度非正、授权不存在、维度不符均整笔拒绝且两端额度不变。
- 成功后同事务重算候补：新增余量优先供仍有效队首，过期/失配者出队。
- 幂等：相同操作号+相同参数返回首次转拨结果；异参 409；重放不受随后
  冻结/停用影响；并发不超额、不重复转拨。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_transfer
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
NOV_START = "2026-11-01T00:00:00+08:00"
NOV_END = "2026-12-01T00:00:00+08:00"
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


# ------------------------------------------------------------- 构造辅助
def make_material(c, code):
    return c.post(
        "/api/v1/materials", json={"code": code, "name": code}
    ).json()["id"]


def make_auth(c, mid, count, region="CN", channel="web",
              starts=OCT_START, ends=OCT_END):
    return c.post(
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


def transfer(c, src, tgt, count, op):
    return c.post(
        "/api/v1/authorizations/transfer",
        json={
            "source_authorization_id": src,
            "target_authorization_id": tgt,
            "count": count,
            "operation_no": op,
        },
    )


def get_auth(c, aid):
    return c.get(f"/api/v1/authorizations/{aid}").json()


def freeze(c, region, channel, reason="合规检查临时冻结"):
    return c.put(
        "/api/v1/channel-freezes",
        json={
            "region": region,
            "channel": channel,
            "action": "freeze",
            "reason": reason,
        },
    )


def resume(c, region, channel):
    return c.put(
        "/api/v1/channel-freezes",
        json={"region": region, "channel": channel, "action": "resume"},
    )


def force_expire_reservation(reservation_id):
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
    """直接改写库内候补到期时刻（at_sql 为相对 now 的 SQL）。"""
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


def books():
    """全量账实核对：每个授权 used/reserved 与有效明细一致且不超额。"""
    db = SessionLocal()
    try:
        auths = db.query(models.Authorization).all()
        bad = []
        for a in auths:
            used = db.query(models.DistributionItem).join(
                models.Distribution,
                models.DistributionItem.distribution_id
                == models.Distribution.id,
            ).filter(
                models.DistributionItem.authorization_id == a.id,
                models.Distribution.status == "approved",
            ).count()
            reserved = db.query(models.ReservationItem).join(
                models.Reservation,
                models.ReservationItem.reservation_id
                == models.Reservation.id,
            ).filter(
                models.ReservationItem.authorization_id == a.id,
                models.Reservation.status == "pending",
            ).count()
            if (
                a.used_count != used
                or a.reserved_count != reserved
                or a.used_count + a.reserved_count > a.max_count
                or a.max_count < 1
            ):
                bad.append(
                    (a.id, a.max_count, a.used_count, used,
                     a.reserved_count, reserved)
                )
        return bad
    finally:
        db.close()


def main() -> int:
    global failed
    Base.metadata.drop_all(bind=engine)
    init_db()

    with TestClient(app) as c:
        print("== 1. 字段校验（正整数次数 / 操作号 / 源目标相同） ==")
        m0 = make_material(c, "tf-m0")
        a0 = make_auth(c, m0, 10)
        b0 = make_auth(c, m0, 5)
        for body, name in [
            ({"source_authorization_id": a0, "target_authorization_id": b0,
              "count": 0, "operation_no": "OP-V0"}, "count=0 -> 422"),
            ({"source_authorization_id": a0, "target_authorization_id": b0,
              "count": -3, "operation_no": "OP-V1"}, "count<0 -> 422"),
            ({"source_authorization_id": a0, "target_authorization_id": b0,
              "count": 1, "operation_no": ""}, "空操作号 -> 422"),
            ({"source_authorization_id": a0, "target_authorization_id": b0,
              "count": 1, "operation_no": "   "}, "空白操作号 -> 422"),
            ({"source_authorization_id": a0, "target_authorization_id": b0,
              "count": 1}, "缺操作号 -> 422"),
            ({"source_authorization_id": a0, "target_authorization_id": b0,
              "count": 1.5, "operation_no": "OP-V2"}, "非整数次数 -> 422"),
        ]:
            r = c.post("/api/v1/authorizations/transfer", json=body)
            check(name, r.status_code == 422, r.text)
        r = transfer(c, a0, a0, 1, "OP-V3")
        check(
            "源目标相同 -> 422 same_authorization",
            r.status_code == 422
            and r.json()["error"]["code"] == "quota_transfer_rejected"
            and r.json()["error"]["details"][0]["reason"]
            == "same_authorization",
            r.text,
        )
        check(
            "校验失败不改额度",
            get_auth(c, a0)["max_count"] == 10
            and get_auth(c, b0)["max_count"] == 5,
        )

        print("== 2. 基本转拨：时段可不同 / 快照 / 归属不变 ==")
        m1 = make_material(c, "tf-m1")
        # 源授权 10 月档，目标授权 11 月档：时段不同也可转拨。
        sa = make_auth(c, m1, 10, starts=OCT_START, ends=OCT_END)
        ta = make_auth(c, m1, 5, starts=NOV_START, ends=NOV_END)
        d1 = apply(c, [m1]).json()["id"]
        d2 = apply(c, [m1]).json()["id"]
        rv1 = reserve(c, [m1]).json()["id"]
        check(
            "转拨前源占用 2/10 + 预留 1",
            get_auth(c, sa)["used_count"] == 2
            and get_auth(c, sa)["reserved_count"] == 1,
        )
        r = transfer(c, sa, ta, 3, "OP-B1")
        check("转拨 200", r.status_code == 200, r.text)
        body = r.json()
        check(
            "响应含操作号与转拨次数",
            body["operation_no"] == "OP-B1" and body["count"] == 3,
            body,
        )
        check("响应含完成时刻", body["created_at"] is not None, body)
        s_snap, t_snap = (
            body["source_authorization"],
            body["target_authorization"],
        )
        check(
            "源快照 7/10 占用 2 预留 1",
            s_snap["authorization_id"] == sa
            and s_snap["max_count"] == 7
            and s_snap["used_count"] == 2
            and s_snap["reserved_count"] == 1
            and s_snap["remaining"] == 5
            and s_snap["reservable_remaining"] == 4,
            s_snap,
        )
        check(
            "目标快照 8/8 占用 0",
            t_snap["authorization_id"] == ta
            and t_snap["max_count"] == 8
            and t_snap["used_count"] == 0
            and t_snap["reserved_count"] == 0
            and t_snap["remaining"] == 8
            and t_snap["reservable_remaining"] == 8,
            t_snap,
        )
        ga, gb = get_auth(c, sa), get_auth(c, ta)
        check(
            "快照与查询一致",
            ga["max_count"] == 7 and ga["used_count"] == 2
            and ga["reserved_count"] == 1
            and gb["max_count"] == 8 and gb["used_count"] == 0,
            f"{ga}/{gb}",
        )
        dist = c.get(f"/api/v1/distributions/{d1}").json()
        check(
            "既有申请归属不变",
            dist["items"][0]["authorization"]["id"] == sa,
            dist,
        )
        resv = c.get(f"/api/v1/reservations/{rv1}").json()
        check(
            "既有预留归属与状态不变",
            resv["status"] == "pending"
            and resv["items"][0]["authorization"]["id"] == sa,
            resv,
        )
        check(
            "既有申请仍可撤销且只影响源占用",
            c.post(f"/api/v1/distributions/{d2}/revoke").status_code == 200
            and get_auth(c, sa)["used_count"] == 1,
        )

        print("== 3. 可转出量 = max-used-reserved（不足整笔拒绝） ==")
        m2 = make_material(c, "tf-m2")
        sc = make_auth(c, m2, 5)
        tc = make_auth(c, m2, 4)
        apply(c, [m2])
        apply(c, [m2])
        reserve(c, [m2])
        # used=2 reserved=1 -> 可转出 2；请求 3 不足。
        r = transfer(c, sc, tc, 3, "OP-C1")
        check(
            "不足 -> 422 insufficient_quota",
            r.status_code == 422
            and r.json()["error"]["code"] == "quota_transfer_rejected"
            and r.json()["error"]["details"][0]["reason"]
            == "insufficient_quota",
            r.text,
        )
        check(
            "不足拒绝后两端额度不变",
            get_auth(c, sc)["max_count"] == 5
            and get_auth(c, tc)["max_count"] == 4,
        )
        r = transfer(c, sc, tc, 2, "OP-C2")
        check(
            "恰好可转出量 -> 200",
            r.status_code == 200
            and r.json()["source_authorization"]["max_count"] == 3
            and r.json()["target_authorization"]["max_count"] == 6,
            r.text,
        )

        print("== 4. 转出后源额度非正 -> 整笔拒绝 ==")
        m3 = make_material(c, "tf-m3")
        sd = make_auth(c, m3, 4)
        td = make_auth(c, m3, 4)
        r = transfer(c, sd, td, 4, "OP-D1")
        check(
            "全部转出（源归零）-> 422 source_quota_not_positive",
            r.status_code == 422
            and r.json()["error"]["details"][0]["reason"]
            == "source_quota_not_positive",
            r.text,
        )
        check(
            "拒绝后两端额度不变",
            get_auth(c, sd)["max_count"] == 4
            and get_auth(c, td)["max_count"] == 4,
        )
        r = transfer(c, sd, td, 3, "OP-D2")
        check(
            "源保留 1 -> 200",
            r.status_code == 200
            and r.json()["source_authorization"]["max_count"] == 1,
            r.text,
        )

        print("== 5. 授权不存在 / 维度不符 ==")
        r = transfer(c, 999999, td, 1, "OP-E1")
        check("源不存在 -> 404", r.status_code == 404, r.text)
        r = transfer(c, sd, 999999, 1, "OP-E2")
        check("目标不存在 -> 404", r.status_code == 404, r.text)
        m3b = make_material(c, "tf-m3b")
        other_mat = make_auth(c, m3b, 5)
        other_region = make_auth(c, m3, 5, region="US")
        other_channel = make_auth(c, m3, 5, channel="app")
        for tgt, name in [
            (other_mat, "素材不符"),
            (other_region, "地区不符"),
            (other_channel, "渠道不符"),
        ]:
            r = transfer(c, sd, tgt, 1, f"OP-E-{tgt}")
            check(
                f"{name} -> 422 dimension_mismatch",
                r.status_code == 422
                and r.json()["error"]["details"][0]["reason"]
                == "dimension_mismatch",
                r.text,
            )

        print("== 6. 两端均须启用 ==")
        m4 = make_material(c, "tf-m4")
        se = make_auth(c, m4, 5)
        te = make_auth(c, m4, 5)
        c.patch(f"/api/v1/authorizations/{se}", json={"status": "inactive"})
        r = transfer(c, se, te, 1, "OP-F1")
        check(
            "源停用 -> 422 authorization_inactive",
            r.status_code == 422
            and r.json()["error"]["details"][0]["reason"]
            == "authorization_inactive",
            r.text,
        )
        c.patch(f"/api/v1/authorizations/{se}", json={"status": "active"})
        c.patch(f"/api/v1/authorizations/{te}", json={"status": "inactive"})
        r = transfer(c, se, te, 1, "OP-F2")
        check(
            "目标停用 -> 422 authorization_inactive",
            r.status_code == 422
            and r.json()["error"]["details"][0]["reason"]
            == "authorization_inactive",
            r.text,
        )
        c.patch(f"/api/v1/authorizations/{te}", json={"status": "active"})
        check(
            "停用拒绝后两端额度不变",
            get_auth(c, se)["max_count"] == 5
            and get_auth(c, te)["max_count"] == 5,
        )

        print("== 7. 通道冻结期间不受理（独立通道 FR/web） ==")
        m5 = make_material(c, "tf-m5")
        sf = make_auth(c, m5, 6, region="FR", channel="web")
        tf_ = make_auth(c, m5, 4, region="FR", channel="web")
        check("冻结成功", freeze(c, "FR", "web").status_code == 200)
        r = transfer(c, sf, tf_, 2, "OP-G1")
        check(
            "冻结中转拨 -> 422 channel_frozen",
            r.status_code == 422
            and r.json()["error"]["code"] == "channel_frozen",
            r.text,
        )
        check(
            "冻结拒绝后两端额度不变",
            get_auth(c, sf)["max_count"] == 6
            and get_auth(c, tf_)["max_count"] == 4,
        )
        check("恢复成功", resume(c, "FR", "web").status_code == 200)
        r = transfer(c, sf, tf_, 2, "OP-G1")
        check(
            "恢复后同操作号可正常受理",
            r.status_code == 200
            and r.json()["source_authorization"]["max_count"] == 4,
            r.text,
        )

        print("== 8. 幂等：同号同参重放 / 异参冲突 ==")
        m6 = make_material(c, "tf-m6")
        sh = make_auth(c, m6, 8)
        th = make_auth(c, m6, 5)
        th2 = make_auth(c, m6, 5)
        r1 = transfer(c, sh, th, 2, "OP-H1")
        check("首次转拨 200", r1.status_code == 200, r1.text)
        r2 = transfer(c, sh, th, 2, "OP-H1")
        check(
            "同号同参重放 -> 200 且与首次完全一致",
            r2.status_code == 200 and r2.json() == r1.json(),
            f"{r1.text} vs {r2.text}",
        )
        check(
            "重放不重复转拨",
            get_auth(c, sh)["max_count"] == 6
            and get_auth(c, th)["max_count"] == 7,
        )
        for body, name in [
            ({"source_authorization_id": sh, "target_authorization_id": th,
              "count": 3, "operation_no": "OP-H1"}, "异次数"),
            ({"source_authorization_id": sh, "target_authorization_id": th2,
              "count": 2, "operation_no": "OP-H1"}, "异目标"),
            ({"source_authorization_id": th, "target_authorization_id": sh,
              "count": 2, "operation_no": "OP-H1"}, "异源"),
        ]:
            r = c.post("/api/v1/authorizations/transfer", json=body)
            check(
                f"同号{name} -> 409 quota_transfer_conflict",
                r.status_code == 409
                and r.json()["error"]["code"] == "quota_transfer_conflict",
                r.text,
            )
        check(
            "冲突请求不改变额度",
            get_auth(c, sh)["max_count"] == 6
            and get_auth(c, th)["max_count"] == 7
            and get_auth(c, th2)["max_count"] == 5,
        )

        print("== 9. 重放不受随后冻结/停用影响（独立通道 IC/web） ==")
        m7 = make_material(c, "tf-m7")
        si = make_auth(c, m7, 6, region="IC", channel="web")
        ti = make_auth(c, m7, 4, region="IC", channel="web")
        r1 = transfer(c, si, ti, 2, "OP-I1")
        check("首次转拨 200", r1.status_code == 200, r1.text)
        freeze(c, "IC", "web")
        c.patch(f"/api/v1/authorizations/{si}", json={"status": "inactive"})
        c.patch(f"/api/v1/authorizations/{ti}", json={"status": "inactive"})
        r2 = transfer(c, si, ti, 2, "OP-I1")
        check(
            "冻结+停用后同参重放仍返回首次结果",
            r2.status_code == 200 and r2.json() == r1.json(),
            r2.text,
        )
        r3 = transfer(c, si, ti, 1, "OP-I1")
        check(
            "冻结+停用后异参仍 409",
            r3.status_code == 409
            and r3.json()["error"]["code"] == "quota_transfer_conflict",
            r3.text,
        )
        resume(c, "IC", "web")
        c.patch(f"/api/v1/authorizations/{si}", json={"status": "active"})
        c.patch(f"/api/v1/authorizations/{ti}", json={"status": "active"})

        print("== 10. 先结算到期预留再判定可转出量（独立通道 JD/web） ==")
        m8 = make_material(c, "tf-m8")
        sj = make_auth(c, m8, 6, region="JD", channel="web")
        tj = make_auth(c, m8, 4, region="JD", channel="web")
        rvj = reserve(c, [m8], ttl=15, region="JD", channel="web").json()["id"]
        check(
            "预留占额后可转出量为 5-1=4+1",
            get_auth(c, sj)["reserved_count"] == 1,
        )
        force_expire_reservation(rvj)
        # 到期预留先结算归还 -> 可转出量回升为 6，转 5 成功（源余 1）。
        r = transfer(c, sj, tj, 5, "OP-J1")
        check(
            "到期预留结算后转拨 200",
            r.status_code == 200
            and r.json()["source_authorization"]["max_count"] == 1
            and r.json()["source_authorization"]["reserved_count"] == 0,
            r.text,
        )
        check(
            "预留已惰性结算为 expired",
            c.get(f"/api/v1/reservations/{rvj}").json()["status"]
            == "expired",
        )

        print("== 11. 先按原顺序处理候补再判定（独立通道 KA/web） ==")
        m9 = make_material(c, "tf-m9")
        # 源授权 10 月档、目标授权 11 月档：候补发行时刻仅命中源授权。
        sk = make_auth(c, m9, 3, region="KA", channel="web",
                       starts=OCT_START, ends=OCT_END)
        tk = make_auth(c, m9, 4, region="KA", channel="web",
                       starts=NOV_START, ends=NOV_END)
        apply(c, [m9], region="KA", channel="web")
        apply(c, [m9], region="KA", channel="web")
        rvk = reserve(c, [m9], region="KA", channel="web").json()["id"]
        wk = waitlist(c, [m9], region="KA", channel="web").json()["id"]
        check(
            "满额后候补入队",
            c.get(f"/api/v1/waitlists/{wk}").json()["status"] == "pending",
        )
        force_expire_reservation(rvk)
        # 结算归还预留(1) -> 队首候补成交占用 -> 可转出量 0，转 2 不足。
        r = transfer(c, sk, tk, 2, "OP-K1")
        check(
            "候补成交占用后不足 -> 422",
            r.status_code == 422
            and r.json()["error"]["details"][0]["reason"]
            == "insufficient_quota",
            r.text,
        )
        wk_body = c.get(f"/api/v1/waitlists/{wk}").json()
        check(
            "候补已按原顺序成交",
            wk_body["status"] == "fulfilled"
            and wk_body["distribution_id"] is not None,
            wk_body,
        )
        check(
            "两端额度未被部分修改",
            get_auth(c, sk)["max_count"] == 3
            and get_auth(c, sk)["used_count"] == 3
            and get_auth(c, tk)["max_count"] == 4
            and get_auth(c, tk)["used_count"] == 0,
        )

        print("== 12. 转拨后同事务重算候补：新增余量供队首（LB/web） ==")
        m10 = make_material(c, "tf-m10")
        # 目标 10 月档（候补仅命中目标）、源 11 月档：时段不同也可转拨。
        tl = make_auth(c, m10, 1, region="LB", channel="web",
                       starts=OCT_START, ends=OCT_END)
        sl = make_auth(c, m10, 5, region="LB", channel="web",
                       starts=NOV_START, ends=NOV_END)
        apply(c, [m10], region="LB", channel="web")  # 占满目标唯一额度
        wl = waitlist(c, [m10], region="LB", channel="web").json()["id"]
        check(
            "目标满额候补入队",
            c.get(f"/api/v1/waitlists/{wl}").json()["status"] == "pending",
        )
        r = transfer(c, sl, tl, 2, "OP-L1")
        check("转拨 200", r.status_code == 200, r.text)
        t_snap = r.json()["target_authorization"]
        check(
            "目标快照含候补成交占用：max=3 used=2",
            t_snap["max_count"] == 3 and t_snap["used_count"] == 2,
            t_snap,
        )
        wl_body = c.get(f"/api/v1/waitlists/{wl}").json()
        check(
            "队首候补已成交",
            wl_body["status"] == "fulfilled"
            and wl_body["distribution_id"] is not None,
            wl_body,
        )

        print("== 13. 过期候补依规则出队不成交（MC/web） ==")
        m11 = make_material(c, "tf-m11")
        tm = make_auth(c, m11, 1, region="MC", channel="web",
                       starts=OCT_START, ends=OCT_END)
        sm = make_auth(c, m11, 5, region="MC", channel="web",
                       starts=NOV_START, ends=NOV_END)
        apply(c, [m11], region="MC", channel="web")
        wm = waitlist(c, [m11], region="MC", channel="web").json()["id"]
        set_waitlist_expiry(wm, "now()")
        r = transfer(c, sm, tm, 1, "OP-M1")
        check("转拨 200", r.status_code == 200, r.text)
        wm_body = c.get(f"/api/v1/waitlists/{wm}").json()
        check(
            "过期候补标记 expired 不成交",
            wm_body["status"] == "expired"
            and wm_body["expiry_reason"] == "ttl_expired",
            wm_body,
        )
        check(
            "目标额度未被过期候补消耗",
            get_auth(c, tm)["max_count"] == 2
            and get_auth(c, tm)["used_count"] == 1,
        )

        print("== 14. 失配候补依规则出队（ND/web） ==")
        m12 = make_material(c, "tf-m12")
        m12b = make_material(c, "tf-m12b")
        an = make_auth(c, m12b, 1, region="ND", channel="web")
        apply(c, [m12b], region="ND", channel="web")
        wn = waitlist(c, [m12b], region="ND", channel="web").json()["id"]
        c.patch(f"/api/v1/authorizations/{an}", json={"status": "inactive"})
        sn = make_auth(c, m12, 5, region="ND", channel="web")
        tn = make_auth(c, m12, 5, region="ND", channel="web")
        r = transfer(c, sn, tn, 2, "OP-N1")
        check("转拨 200", r.status_code == 200, r.text)
        wn_body = c.get(f"/api/v1/waitlists/{wn}").json()
        check(
            "失配候补标记 failed",
            wn_body["status"] == "failed"
            and wn_body["failure_reason"] == "authorization_inactive",
            wn_body,
        )

        print("== 15. 并发转拨不超额（OC/web，10 并发抢 10 可转出量） ==")
        m13 = make_material(c, "tf-m13")
        so = make_auth(c, m13, 10, region="OC", channel="web")
        to = make_auth(c, m13, 5, region="OC", channel="web")

        def one_transfer(i):
            cc = TestClient(app)
            return transfer(cc, so, to, 3, f"OP-O{i}").status_code

        with ThreadPoolExecutor(max_workers=10) as pool:
            codes = list(pool.map(one_transfer, range(10)))
        ok = sum(1 for x in codes if x == 200)
        rej = sum(1 for x in codes if x == 422)
        # 每笔转 3 且源须保留 >=1：至多 3 笔成功（10 -> 1）。
        check("并发恰 3 笔成功", ok == 3, f"codes={codes}")
        check("其余 7 笔整笔拒绝", rej == 7, f"codes={codes}")
        check(
            "并发后两端额度：源 1 目标 14",
            get_auth(c, so)["max_count"] == 1
            and get_auth(c, to)["max_count"] == 14,
        )

        print("== 16. 并发同号同参不重复转拨（PC/web） ==")
        m14 = make_material(c, "tf-m14")
        sp = make_auth(c, m14, 6, region="PC", channel="web")
        tp = make_auth(c, m14, 5, region="PC", channel="web")

        def same_transfer(_):
            cc = TestClient(app)
            r = transfer(cc, sp, tp, 2, "OP-P-SAME")
            return r.status_code, r.text

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(same_transfer, range(8)))
        codes = [x[0] for x in results]
        bodies = {x[1] for x in results}
        check("并发同号全部 200", all(x == 200 for x in codes), f"{codes}")
        check("并发同号返回同一首次结果", len(bodies) == 1, f"{bodies}")
        check(
            "额度仅转拨一次",
            get_auth(c, sp)["max_count"] == 4
            and get_auth(c, tp)["max_count"] == 7,
        )

        print("== 17. 并发同号异参恰一笔生效（QC/web） ==")
        m15 = make_material(c, "tf-m15")
        sq = make_auth(c, m15, 9, region="QC", channel="web")
        tq = make_auth(c, m15, 5, region="QC", channel="web")
        counts = [1, 2, 3, 4, 5, 6]

        def conflict_transfer(i):
            cc = TestClient(app)
            r = transfer(cc, sq, tq, counts[i], "OP-Q-SAME")
            return r.status_code, r.json()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(conflict_transfer, range(6)))
        oks = [b for code, b in results if code == 200]
        conflicts = [
            b for code, b in results
            if code == 409 and b["error"]["code"] == "quota_transfer_conflict"
        ]
        check("同号异参恰一笔 200", len(oks) == 1, f"{results}")
        check("其余均为 409 冲突", len(conflicts) == 5, f"{results}")
        winner = oks[0]["count"]
        check(
            "额度仅按胜出一笔转拨",
            get_auth(c, sq)["max_count"] == 9 - winner
            and get_auth(c, tq)["max_count"] == 5 + winner,
            f"winner={winner}",
        )

        print("== 18. 全量账实相符 ==")
        bad = books()
        check("used/reserved 与有效明细一致且不超额", not bad, bad)

    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
