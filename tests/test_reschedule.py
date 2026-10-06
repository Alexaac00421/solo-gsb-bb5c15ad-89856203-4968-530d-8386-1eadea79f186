"""发行申请改期端到端功能测试（真实 PostgreSQL + TestClient）。

覆盖需求要点：
- 改期成功：仅更换发行时刻，地区/渠道/素材清单不变；归属不变时次数不动，
  归属变化时「原授权 -1、新授权 +1」原子搬移；
- 新时刻与当前时刻为同一瞬间（仅时区表示不同也算）：幂等返回原申请、
  不变动任何次数，且不要求授权仍启用；
- 重选沿用左闭右开匹配与重叠授权顺序（到期早优先、其次编号小）；
- 余量判定扣除本申请原占用（max=1 可自我承接），仍计入未到期预留；
- 目标授权须启用；任一项失败整体拒绝、逐项列明原因、原记录与次数不变；
- 未知编号 404、已撤销 409（即使同一瞬间）、无时区/缺字段 422；
- 预留确认生成的申请可改期：预留原始时刻与确认记录保留，
  预留查询关联申请展示最新数据；改期后撤销/改挂约定不变；
- 并发：重复改期结果一致、改期与撤销竞争、改期与新申请竞争单槽，
  绝不超额、账实相符。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_reschedule
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439",
)

from app.database import Base, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402

CST = timezone(timedelta(hours=8))

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


def iso(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=CST).isoformat()


def at(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=CST)


def parse(s):
    return datetime.fromisoformat(s)


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    init_db()
    c = TestClient(app)

    def material(code):
        r = c.post("/api/v1/materials", json={"code": code, "name": code})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def auth(mid, start, end, max_count):
        r = c.post(
            "/api/v1/authorizations",
            json={
                "material_id": mid, "region": "CN", "channel": "web",
                "starts_at": start, "ends_at": end, "max_count": max_count,
            },
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def apply(mids, occur):
        return c.post(
            "/api/v1/distributions",
            json={"material_ids": mids, "region": "CN", "channel": "web",
                  "occur_at": occur},
        )

    def reschedule(did, occur):
        return c.post(
            f"/api/v1/distributions/{did}/reschedule",
            json={"occur_at": occur},
        )

    def auth_used(aid):
        return c.get(f"/api/v1/authorizations/{aid}").json()["used_count"]

    def dist(did):
        r = c.get(f"/api/v1/distributions/{did}")
        assert r.status_code == 200, r.text
        return r.json()

    print("== A. 基本改期与同一瞬间幂等 ==")
    m1 = material("rs-m1")
    a1 = auth(m1, iso(2026, 10, 1), iso(2026, 11, 1), 2)
    d1 = apply([m1], iso(2026, 10, 10, 12)).json()["id"]
    check("初始占用 used=1", auth_used(a1) == 1)

    r = reschedule(d1, iso(2026, 10, 15, 12))
    check("改期 200", r.status_code == 200, r.text)
    body = r.json()
    check("occur_at 已更新",
          parse(body["occur_at"]) == at(2026, 10, 15, 12), body)
    check("归属未变", body["items"][0]["authorization"]["id"] == a1, body)
    check("归属不变次数不动", auth_used(a1) == 1)

    # 同一瞬间（仅时区表示不同）：幂等返回原申请，不变动次数
    r = reschedule(d1, "2026-10-15T04:00:00+00:00")
    check("同一瞬间 200", r.status_code == 200, r.text)
    check("同一瞬间 occur 不变",
          parse(r.json()["occur_at"]) == at(2026, 10, 15, 12), r.json())
    check("同一瞬间次数不变", auth_used(a1) == 1)

    # 同一瞬间幂等不要求授权仍启用
    c.patch(f"/api/v1/authorizations/{a1}", json={"status": "inactive"})
    r = reschedule(d1, iso(2026, 10, 15, 12))
    check("授权停用时同一瞬间仍幂等 200", r.status_code == 200, r.text)
    check("授权停用时次数不变", auth_used(a1) == 1)
    c.patch(f"/api/v1/authorizations/{a1}", json={"status": "active"})

    got = dist(d1)
    check("查询展示改期后时刻",
          parse(got["occur_at"]) == at(2026, 10, 15, 12), got)

    print("== B. 改期换授权与重叠选择顺序 ==")
    m2 = material("rs-m2")
    b1 = auth(m2, iso(2026, 10, 1), iso(2026, 10, 20), 1)
    b2 = auth(m2, iso(2026, 10, 1), iso(2026, 11, 20), 1)
    d2 = apply([m2], iso(2026, 10, 10, 12)).json()["id"]
    check("初始命中到期早者 b1",
          dist(d2)["items"][0]["authorization"]["id"] == b1)

    r = reschedule(d2, iso(2026, 10, 25, 12))
    check("改期换授权 200", r.status_code == 200, r.text)
    check("归属搬到 b2", r.json()["items"][0]["authorization"]["id"] == b2,
          r.json())
    check("原授权 -1", auth_used(b1) == 0)
    check("新授权 +1", auth_used(b2) == 1)

    r = reschedule(d2, iso(2026, 10, 12, 12))
    check("改回重叠区 200", r.status_code == 200, r.text)
    check("重叠顺序选到期早者 b1",
          r.json()["items"][0]["authorization"]["id"] == b1, r.json())
    check("b1 恢复 1", auth_used(b1) == 1)
    check("b2 归零", auth_used(b2) == 0)

    print("== C. 余量扣除本申请原占用（max=1 自我承接） ==")
    m3 = material("rs-m3")
    c1 = auth(m3, iso(2026, 10, 1), iso(2026, 11, 1), 1)
    d3 = apply([m3], iso(2026, 10, 10, 12)).json()["id"]
    check("c1 已满 1/1", auth_used(c1) == 1)
    r = reschedule(d3, iso(2026, 10, 12, 12))
    check("自我承接 200", r.status_code == 200, r.text)
    check("自我承接次数不变", auth_used(c1) == 1)

    print("== D. 失败整体拒绝：逐项原因、原记录与次数不变 ==")
    d4 = apply([m1], iso(2026, 10, 10, 12)).json()["id"]
    check("a1 已满 2/2", auth_used(a1) == 2)

    r = reschedule(d4, iso(2027, 1, 10, 12))
    check("无匹配 422", r.status_code == 422, r.text)
    err = r.json()["error"]
    check("无匹配 code", err["code"] == "reschedule_rejected", err)
    check("无匹配 reason",
          err["details"][0]["reason"] == "no_matching_authorization", err)
    check("无匹配含素材编号", err["details"][0]["material_id"] == m1, err)
    check("失败后时刻不变",
          parse(dist(d4)["occur_at"]) == at(2026, 10, 10, 12))
    check("失败后次数不变", auth_used(a1) == 2)

    # 目标授权须启用
    a1b = auth(m1, iso(2026, 11, 1), iso(2026, 12, 1), 1)
    c.patch(f"/api/v1/authorizations/{a1b}", json={"status": "inactive"})
    r = reschedule(d4, iso(2026, 11, 10, 12))
    check("目标停用 422", r.status_code == 422, r.text)
    check("目标停用 reason",
          r.json()["error"]["details"][0]["reason"] == "authorization_inactive",
          r.text)
    c.patch(f"/api/v1/authorizations/{a1b}", json={"status": "active"})

    # 目标余量不足（被他人占满，本申请折扣不适用）
    dx = apply([m1], iso(2026, 11, 10, 12))
    check("占位申请 201", dx.status_code == 201, dx.text)
    r = reschedule(d4, iso(2026, 11, 15, 12))
    check("目标满员 422", r.status_code == 422, r.text)
    check("目标满员 reason",
          r.json()["error"]["details"][0]["reason"] == "quota_exhausted",
          r.text)
    check("满员后归属不变", dist(d4)["items"][0]["authorization"]["id"] == a1)
    check("满员后 a1 次数不变", auth_used(a1) == 2)
    check("满员后 a1b 次数不变", auth_used(a1b) == 1)

    # 多素材：一项可承接一项不可 → 整体拒绝，归属与次数均不变
    m4 = material("rs-m4")
    m5 = material("rs-m5")
    a4a = auth(m4, iso(2026, 10, 1), iso(2026, 11, 1), 1)
    a4b = auth(m4, iso(2026, 11, 1), iso(2026, 12, 1), 1)
    a5a = auth(m5, iso(2026, 10, 1), iso(2026, 11, 1), 1)
    d5 = apply([m4, m5], iso(2026, 10, 10, 12)).json()["id"]
    r = reschedule(d5, iso(2026, 11, 10, 12))
    check("多素材整体 422", r.status_code == 422, r.text)
    errs = r.json()["error"]["details"]
    check("仅失败项列原因",
          len(errs) == 1 and errs[0]["item_index"] == 1
          and errs[0]["material_id"] == m5
          and errs[0]["reason"] == "no_matching_authorization", errs)
    got = dist(d5)
    check("多素材时刻不变",
          parse(got["occur_at"]) == at(2026, 10, 10, 12), got)
    check("多素材归属不变",
          [it["authorization"]["id"] for it in got["items"]] == [a4a, a5a],
          got)
    check("a4a 次数不变", auth_used(a4a) == 1)
    check("a4b 未承接", auth_used(a4b) == 0)
    check("a5a 次数不变", auth_used(a5a) == 1)

    print("== E. 未到期预留仍计入余量 ==")
    m6 = material("rs-m6")
    f1 = auth(m6, iso(2026, 10, 1), iso(2026, 11, 1), 2)
    d6 = apply([m6], iso(2026, 10, 10, 12)).json()["id"]
    rr = c.post(
        "/api/v1/reservations",
        json={"material_ids": [m6], "region": "CN", "channel": "web",
              "occur_at": iso(2026, 10, 20, 12), "ttl_minutes": 30},
    )
    check("预留 201", rr.status_code == 201, rr.text)
    # 自身占用折扣 + 未到期预留占额：used(1)-1+reserved(1) = 1 < 2 → 可承接
    r = reschedule(d6, iso(2026, 10, 20, 12))
    check("折扣与预留并存 200", r.status_code == 200, r.text)
    check("同授权次数不动", auth_used(f1) == 1)
    c.post(f"/api/v1/reservations/{rr.json()['id']}/cancel")

    # 目标授权被未到期预留占满 → quota_exhausted；取消预留后可承接
    m7 = material("rs-m7")
    e1 = auth(m7, iso(2026, 10, 1), iso(2026, 10, 15), 1)
    e2 = auth(m7, iso(2026, 10, 15), iso(2026, 11, 1), 1)
    d7 = apply([m7], iso(2026, 10, 10, 12)).json()["id"]
    rr = c.post(
        "/api/v1/reservations",
        json={"material_ids": [m7], "region": "CN", "channel": "web",
              "occur_at": iso(2026, 10, 20, 12), "ttl_minutes": 30},
    )
    check("占满预留 201", rr.status_code == 201, rr.text)
    r = reschedule(d7, iso(2026, 10, 20, 12))
    check("预留占满 422", r.status_code == 422, r.text)
    check("预留占满 reason",
          r.json()["error"]["details"][0]["reason"] == "quota_exhausted",
          r.text)
    check("预留占满归属不变", dist(d7)["items"][0]["authorization"]["id"] == e1)
    c.post(f"/api/v1/reservations/{rr.json()['id']}/cancel")
    r = reschedule(d7, iso(2026, 10, 20, 12))
    check("预留释放后 200", r.status_code == 200, r.text)
    check("搬至 e2", r.json()["items"][0]["authorization"]["id"] == e2)
    check("e1 释放", auth_used(e1) == 0)
    check("e2 承接", auth_used(e2) == 1)

    print("== F. 已撤销 / 未知 / 参数校验 ==")
    c.post(f"/api/v1/distributions/{d3}/revoke")
    r = reschedule(d3, iso(2026, 10, 13, 12))
    check("已撤销改期 409", r.status_code == 409, r.text)
    check("已撤销 code",
          r.json()["error"]["code"] == "distribution_revoked", r.text)
    r = reschedule(d3, dist(d3)["occur_at"])
    check("已撤销同一瞬间仍 409", r.status_code == 409, r.text)
    r = reschedule(999999, iso(2026, 10, 13, 12))
    check("未知申请 404", r.status_code == 404, r.text)
    check("未知申请 code", r.json()["error"]["code"] == "not_found", r.text)
    r = reschedule(d1, "2026-10-16T12:00:00")
    check("无时区 422", r.status_code == 422, r.text)
    r = c.post(f"/api/v1/distributions/{d1}/reschedule", json={})
    check("缺字段 422", r.status_code == 422, r.text)

    print("== G. 预留确认生成的申请可改期，预留记录保留 ==")
    m9 = material("rs-m9")
    g1 = auth(m9, iso(2026, 10, 1), iso(2026, 10, 15), 2)
    g2 = auth(m9, iso(2026, 10, 15), iso(2026, 12, 1), 2)
    rr = c.post(
        "/api/v1/reservations",
        json={"material_ids": [m9], "region": "CN", "channel": "web",
              "occur_at": iso(2026, 10, 10, 12), "ttl_minutes": 30},
    )
    rid = rr.json()["id"]
    cf = c.post(f"/api/v1/reservations/{rid}/confirm")
    check("确认 200", cf.status_code == 200, cf.text)
    d10 = cf.json()["distribution_id"]
    check("确认归属 g1", dist(d10)["items"][0]["authorization"]["id"] == g1)

    r = reschedule(d10, iso(2026, 10, 20, 12))
    check("预留申请改期 200", r.status_code == 200, r.text)
    check("改期后归属 g2", r.json()["items"][0]["authorization"]["id"] == g2,
          r.json())
    check("g1 释放", auth_used(g1) == 0)
    check("g2 承接", auth_used(g2) == 1)

    got = c.get(f"/api/v1/reservations/{rid}").json()
    check("预留状态仍 confirmed", got["status"] == "confirmed", got)
    check("预留原始时刻保留",
          parse(got["occur_at"]) == at(2026, 10, 10, 12), got)
    check("确认记录保留", got["confirmed_at"] is not None, got)
    check("关联申请展示最新时刻",
          parse(got["distribution"]["occur_at"]) == at(2026, 10, 20, 12), got)
    check("关联申请展示最新归属",
          got["distribution"]["items"][0]["authorization"]["id"] == g2, got)

    c.post(f"/api/v1/distributions/{d10}/revoke")
    check("改期后撤销释放新授权 g2", auth_used(g2) == 0)

    print("== H. 左闭右开边界 ==")
    m10 = material("rs-m10")
    h1 = auth(m10, iso(2026, 10, 1), iso(2026, 10, 10), 1)
    h2 = auth(m10, iso(2026, 10, 10), iso(2026, 10, 20), 1)
    d11 = apply([m10], iso(2026, 10, 5, 12)).json()["id"]
    r = reschedule(d11, iso(2026, 10, 10))
    check("边界改期 200", r.status_code == 200, r.text)
    check("occur==ends 离开旧授权、occur==starts 进入新授权",
          r.json()["items"][0]["authorization"]["id"] == h2, r.json())
    check("h1 释放", auth_used(h1) == 0)
    check("h2 承接", auth_used(h2) == 1)
    r = reschedule(d11, iso(2026, 10, 20))
    check("右端点外 422", r.status_code == 422, r.text)
    check("右端点外 reason",
          r.json()["error"]["details"][0]["reason"]
          == "no_matching_authorization", r.text)

    print("== I. 改期后改挂约定不变 ==")
    h3 = auth(m10, iso(2026, 10, 10), iso(2026, 10, 20), 1)
    r = c.post(
        "/api/v1/authorizations/migrate",
        json={"source_authorization_id": h2, "replacement_authorization_id": h3},
    )
    check("改挂 200 且迁移 1 条",
          r.status_code == 200 and r.json()["migrated_count"] == 1, r.text)
    check("改挂后归属 h3", dist(d11)["items"][0]["authorization"]["id"] == h3)
    check("h2 归零", auth_used(h2) == 0)
    check("h3 承接", auth_used(h3) == 1)

    print("== J. 并发安全 ==")
    # J1：同一申请 10 并发改期到同一新时刻 → 全部 200，账实相符
    mj = material("rs-mj")
    j1 = auth(mj, iso(2026, 10, 1), iso(2026, 10, 15), 1)
    j2 = auth(mj, iso(2026, 10, 15), iso(2026, 11, 1), 1)
    dj = apply([mj], iso(2026, 10, 10, 12)).json()["id"]

    def rs_same(_):
        cc = TestClient(app)
        return cc.post(
            f"/api/v1/distributions/{dj}/reschedule",
            json={"occur_at": iso(2026, 10, 20, 12)},
        ).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(rs_same, range(10)))
    check("并发重复改期全部 200", all(s == 200 for s in codes), codes)
    check("j1 归零", auth_used(j1) == 0)
    check("j2 唯一占用", auth_used(j2) == 1)
    check("最终归属 j2", dist(dj)["items"][0]["authorization"]["id"] == j2)

    # J2：改期与撤销竞争 → 撤销必 200；改期 200 或 409；终态一致
    mk = material("rs-mk")
    k1 = auth(mk, iso(2026, 10, 1), iso(2026, 10, 15), 1)
    k2 = auth(mk, iso(2026, 10, 15), iso(2026, 11, 1), 1)
    dk = apply([mk], iso(2026, 10, 10, 12)).json()["id"]

    def do_rs():
        cc = TestClient(app)
        return cc.post(
            f"/api/v1/distributions/{dk}/reschedule",
            json={"occur_at": iso(2026, 10, 20, 12)},
        ).status_code

    def do_rv():
        cc = TestClient(app)
        return cc.post(f"/api/v1/distributions/{dk}/revoke").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_rs = pool.submit(do_rs)
        f_rv = pool.submit(do_rv)
        rs_code, rv_code = f_rs.result(), f_rv.result()
    check("撤销 200", rv_code == 200, (rs_code, rv_code))
    check("改期 200 或 409", rs_code in (200, 409), rs_code)
    check("终态 revoked", dist(dk)["status"] == "revoked")
    check("k1 无残留", auth_used(k1) == 0)
    check("k2 无残留", auth_used(k2) == 0)

    # J3：改期与新申请竞争单槽目标授权 → 恰一方成功，绝不超额
    ml = material("rs-ml")
    l1 = auth(ml, iso(2026, 10, 1), iso(2026, 10, 15), 1)
    l2 = auth(ml, iso(2026, 10, 15), iso(2026, 11, 1), 1)
    dl = apply([ml], iso(2026, 10, 10, 12)).json()["id"]

    def t_rs():
        cc = TestClient(app)
        return cc.post(
            f"/api/v1/distributions/{dl}/reschedule",
            json={"occur_at": iso(2026, 10, 20, 12)},
        ).status_code

    def t_new():
        cc = TestClient(app)
        return cc.post(
            "/api/v1/distributions",
            json={"material_ids": [ml], "region": "CN", "channel": "web",
                  "occur_at": iso(2026, 10, 20, 12)},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_a = pool.submit(t_rs)
        f_b = pool.submit(t_new)
        s_rs, s_new = f_a.result(), f_b.result()
    check("恰一方成功", (s_rs == 200) != (s_new == 201), (s_rs, s_new))
    check("l2 绝不超额", auth_used(l2) == 1)
    if s_rs == 200:
        check("改期胜出：l1 释放", auth_used(l1) == 0)
        check("改期胜出：时刻已变",
              parse(dist(dl)["occur_at"]) == at(2026, 10, 20, 12))
        check("改期胜出：新申请 422", s_new == 422, s_new)
    else:
        check("新申请胜出：改期 422", s_rs == 422, s_rs)
        check("新申请胜出：l1 仍在", auth_used(l1) == 1)
        check("新申请胜出：时刻未变",
              parse(dist(dl)["occur_at"]) == at(2026, 10, 10, 12))

    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
