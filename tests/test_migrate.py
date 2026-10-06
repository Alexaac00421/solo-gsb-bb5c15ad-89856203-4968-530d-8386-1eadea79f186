"""授权退役改挂端到端测试（真实 PostgreSQL + TestClient）。

覆盖：
- 正常改挂：次数搬移、原授权停用、查询显示替代授权、撤销只释放替代授权一次；
- 拒绝原因：两端相同、维度不一致、替代授权停用、时段不覆盖、余量不足；
- 原子性：任一条件不满足则归属与两端次数均不变；
- 已撤销申请不迁移；无可迁移申请时迁移数为 0（原授权仍停用）；
- 重复改挂为幂等空操作；多素材批量申请整体改挂；
- 404 与 422 校验。

每个场景使用独立 region 通道，避免重叠授权选择相互干扰。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_migrate
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439",
)

from app.database import Base, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=CST)

passed = 0
failed = 0


def check(name: str, cond: bool, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    init_db()

    with TestClient(app) as c:
        def new_material(code):
            return c.post(
                "/api/v1/materials", json={"code": code, "name": code}
            ).json()["id"]

        m1 = new_material("m1")
        m2 = new_material("m2")
        m3 = new_material("m3")

        def new_auth(mid, count, region, channel="web",
                     starts=NOW - timedelta(days=1),
                     ends=NOW + timedelta(days=1)):
            return c.post(
                "/api/v1/authorizations",
                json={
                    "material_id": mid,
                    "region": region,
                    "channel": channel,
                    "starts_at": starts.isoformat(),
                    "ends_at": ends.isoformat(),
                    "max_count": count,
                },
            ).json()["id"]

        def apply(mids, region, channel="web", occur=NOW):
            return c.post(
                "/api/v1/distributions",
                json={
                    "material_ids": list(mids),
                    "region": region,
                    "channel": channel,
                    "occur_at": occur.isoformat(),
                },
            )

        def migrate(src, dst):
            return c.post(
                "/api/v1/authorizations/migrate",
                json={
                    "source_authorization_id": src,
                    "replacement_authorization_id": dst,
                },
            )

        def used(aid):
            return c.get(f"/api/v1/authorizations/{aid}").json()["used_count"]

        def status(aid):
            return c.get(f"/api/v1/authorizations/{aid}").json()["status"]

        print("== 正常改挂：次数搬移 + 查询 + 撤销只释放替代授权 ==")
        R = "R1"
        a1 = new_auth(m1, 3, R)
        b1 = new_auth(m1, 10, R)
        d1 = apply([m1], R).json()["id"]
        d2 = apply([m1], R).json()["id"]
        r = migrate(a1, b1)
        check("migrate 200", r.status_code == 200, r.text)
        body = r.json()
        check("migrated_count=2", body["migrated_count"] == 2, r.text)
        check(
            "source remaining=3 used=0 inactive",
            body["source_authorization"]["used_count"] == 0
            and body["source_authorization"]["remaining"] == 3
            and body["source_authorization"]["status"] == "inactive",
            r.text,
        )
        check(
            "replacement used=2 remaining=8",
            body["replacement_authorization"]["used_count"] == 2
            and body["replacement_authorization"]["remaining"] == 8,
            r.text,
        )
        g1 = c.get(f"/api/v1/distributions/{d1}").json()
        g2 = c.get(f"/api/v1/distributions/{d2}").json()
        check(
            "queries show replacement auth",
            g1["items"][0]["authorization"]["id"] == b1
            and g2["items"][0]["authorization"]["id"] == b1,
        )
        check("still approved", g1["status"] == "approved")

        # 撤销改挂后的申请：只释放替代授权一次
        rv = c.post(f"/api/v1/distributions/{d1}/revoke")
        check("revoke migrated 200", rv.status_code == 200, rv.text)
        check("replacement released once (2->1)", used(b1) == 1)
        check("source untouched (0)", used(a1) == 0)
        check(
            "double revoke 409",
            c.post(f"/api/v1/distributions/{d1}/revoke").status_code == 409,
        )

        print("== 重复改挂为幂等空操作 ==")
        r2 = migrate(a1, b1)
        check("re-migrate 200", r2.status_code == 200, r2.text)
        check("re-migrate count=0", r2.json()["migrated_count"] == 0, r2.text)
        check(
            "counts unchanged", used(b1) == 1 and used(a1) == 0
        )

        print("== 已撤销申请不迁移 ==")
        R = "R2"
        a2 = new_auth(m2, 2, R)
        b2 = new_auth(m2, 10, R)
        keep = apply([m2], R).json()["id"]
        gone = apply([m2], R).json()["id"]
        c.post(f"/api/v1/distributions/{gone}/revoke")
        r = migrate(a2, b2)
        check(
            "only approved migrated (1)",
            r.status_code == 200 and r.json()["migrated_count"] == 1,
            r.text,
        )
        gg = c.get(f"/api/v1/distributions/{gone}").json()
        check(
            "revoked query keeps old auth + revoked",
            gg["items"][0]["authorization"]["id"] == a2
            and gg["status"] == "revoked",
        )
        check(
            "approved query shows replacement",
            c.get(f"/api/v1/distributions/{keep}").json()["items"][0][
                "authorization"
            ]["id"]
            == b2,
        )
        check("source freed approved only (0)", used(a2) == 0)
        check("target gained 1", used(b2) == 1)

        print("== 多素材批量申请整体改挂 ==")
        R = "R3"
        ba = new_auth(m1, 2, R)
        bb = new_auth(m2, 2, R)
        ta = new_auth(m1, 5, R)
        tb = new_auth(m2, 5, R)
        bd = apply([m1, m2], R).json()
        check("batch distribution approved", "id" in bd, bd)
        # 两条源授权退役到各自替代授权
        check("migrate ba->ta", migrate(ba, ta).status_code == 200)
        check("migrate bb->tb", migrate(bb, tb).status_code == 200)
        bg = c.get(f"/api/v1/distributions/{bd['id']}").json()
        ids = {
            it["material_id"]: it["authorization"]["id"] for it in bg["items"]
        }
        check(
            "batch items all repointed",
            ids[m1] == ta and ids[m2] == tb,
            str(ids),
        )
        check("used ta=1 tb=1", used(ta) == 1 and used(tb) == 1)
        # 撤销批量申请：两端各释放一次
        rv = c.post(f"/api/v1/distributions/{bd['id']}/revoke")
        check("batch revoke 200", rv.status_code == 200, rv.text)
        check(
            "both replacements released once",
            used(ta) == 0 and used(tb) == 0,
        )

        print("== 拒绝：两端相同 ==")
        R = "R4"
        x = new_auth(m1, 1, R)
        r = migrate(x, x)
        check("same 422", r.status_code == 422, r.text)
        check(
            "same reason",
            r.json()["error"]["details"][0]["reason"]
            == "same_authorization",
            r.text,
        )

        print("== 拒绝：维度不一致（地区/渠道/素材） ==")
        R = "R5"
        s = new_auth(m1, 3, R)
        t_region = new_auth(m1, 3, "OTHER")
        t_channel = new_auth(m1, 3, R, channel="app")
        t_material = new_auth(m2, 3, R)
        for t, label in [
            (t_region, "region"),
            (t_channel, "channel"),
            (t_material, "material"),
        ]:
            r = migrate(s, t)
            check(
                f"dimension mismatch {label} 422",
                r.status_code == 422
                and r.json()["error"]["details"][0]["reason"]
                == "dimension_mismatch",
                r.text,
            )
        check("source not deactivated on mismatch", status(s) == "active")

        print("== 拒绝：替代授权停用（归属与次数不变） ==")
        R = "R6"
        s = new_auth(m1, 3, R)
        apply([m1], R)  # s 占用 1
        t = new_auth(m1, 3, R)
        c.patch(f"/api/v1/authorizations/{t}", json={"status": "inactive"})
        r = migrate(s, t)
        check(
            "inactive target 422",
            r.status_code == 422
            and r.json()["error"]["details"][0]["reason"]
            == "replacement_inactive",
            r.text,
        )
        check("source used unchanged (1)", used(s) == 1)
        check("source remains active", status(s) == "active")
        check("target used unchanged (0)", used(t) == 0)
        # 重新启用后可迁移
        c.patch(f"/api/v1/authorizations/{t}", json={"status": "active"})
        r = migrate(s, t)
        check("migrate after re-activate 200", r.status_code == 200, r.text)
        check(
            "counts moved (s=0, t=1)",
            r.json()["migrated_count"] == 1 and used(s) == 0 and used(t) == 1,
        )

        print("== 拒绝：替代授权时段不覆盖发行时刻 ==")
        R = "R7"
        s = new_auth(m1, 3, R)
        dd = apply([m1], R, occur=NOW).json()["id"]
        # 替代授权在 NOW 之后才开始
        t = new_auth(
            m1, 3, R,
            starts=NOW + timedelta(days=2), ends=NOW + timedelta(days=5),
        )
        r = migrate(s, t)
        check(
            "period not covered 422",
            r.status_code == 422
            and r.json()["error"]["details"][0]["reason"]
            == "period_not_covered",
            r.text,
        )
        check(
            "item still on source",
            c.get(f"/api/v1/distributions/{dd}").json()["items"][0][
                "authorization"
            ]["id"]
            == s,
        )
        check("counts unchanged (s=1 t=0)", used(s) == 1 and used(t) == 0)
        check("source remains active", status(s) == "active")

        # 左闭右开边界：替代授权恰好 [NOW, ...) 左边界命中应通过，
        # 右边界（ends_at == occur_at）应拒绝。
        R = "R7B"
        s2 = new_auth(m1, 3, R)
        apply([m1], R, occur=NOW).json()["id"]
        t_left = new_auth(m1, 3, R, starts=NOW, ends=NOW + timedelta(days=2))
        check(
            "left-closed coverage accepted",
            migrate(s2, t_left).status_code == 200,
        )
        check("left-bound migrated 1", used(t_left) == 1 and used(s2) == 0)

        R = "R7C"
        s3 = new_auth(
            m1, 3, R,
            starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=3),
        )
        apply([m1], R, occur=NOW + timedelta(days=1))
        # 替代授权在申请之后才登记，右端点恰为发行时刻：右开，不覆盖
        t_right = new_auth(
            m1, 3, R, starts=NOW, ends=NOW + timedelta(days=1)
        )
        r = migrate(s3, t_right)
        check(
            "right-open coverage rejected",
            r.status_code == 422
            and r.json()["error"]["details"][0]["reason"]
            == "period_not_covered",
            r.text,
        )
        check("s3 still active & holding 1", status(s3) == "active" and used(s3) == 1)

        print("== 拒绝：替代授权余量不足（原子性） ==")
        R = "R8"
        s = new_auth(m2, 5, R)
        apply([m2], R)
        apply([m2], R)
        apply([m2], R)  # 源授权 3 条未撤销占用
        t = new_auth(m2, 2, R)  # 仅 2 余量 < 3
        r = migrate(s, t)
        check(
            "insufficient_quota 422",
            r.status_code == 422
            and r.json()["error"]["details"][0]["reason"]
            == "insufficient_quota",
            r.text,
        )
        check("source used unchanged (3)", used(s) == 3)
        check("target used unchanged (0)", used(t) == 0)
        check("source still active", status(s) == "active")

        # 停用容量不足的旧替代授权，避免后续新申请落到它上面
        c.patch(f"/api/v1/authorizations/{t}", json={"status": "inactive"})

        # 恰好等量（3 余量承接 3 占用）应成功且替代授权满载
        t2 = new_auth(m2, 3, R)
        r = migrate(s, t2)
        check("exact-fit quota 200", r.status_code == 200, r.text)
        check(
            "migrated 3; source 0/inactive; target 3/full",
            r.json()["migrated_count"] == 3
            and used(s) == 0 and status(s) == "inactive"
            and used(t2) == 3,
            r.text,
        )
        rn = apply([m2], R)
        check(
            "exhausted after migration",
            rn.status_code == 422
            and rn.json()["error"]["details"][0]["reason"]
            == "quota_exhausted",
            rn.text,
        )

        print("== 零迁移：原授权本无未撤销申请（仅已撤销） ==")
        R = "R9"
        s = new_auth(m3, 2, R)
        t = new_auth(m3, 5, R)
        dz = apply([m3], R).json()["id"]
        c.post(f"/api/v1/distributions/{dz}/revoke")
        r = migrate(s, t)
        check("zero migration 200", r.status_code == 200, r.text)
        check("migrated_count 0", r.json()["migrated_count"] == 0)
        check(
            "source deactivated even with zero migration",
            status(s) == "inactive" and used(s) == 0,
        )
        check("target untouched", used(t) == 0 and status(t) == "active")

        print("== 改挂后撤销释放，替代授权可再承接新申请 ==")
        # R8 中 t2 满载，撤销一条改挂过来的申请后应可重新申请
        # 找到 R8 下迁入 t2 的某条申请：最初 3 个 R8 申请
        # 直接复用 R9 链路更简单：登记新链路
        R = "R10"
        s = new_auth(m3, 2, R)
        t = new_auth(m3, 2, R)
        drel = apply([m3], R).json()["id"]
        migrate(s, t)
        check(
            "revoke after migration frees replacement",
            c.post(f"/api/v1/distributions/{drel}/revoke").status_code == 200
            and used(t) == 0,
        )
        rn = apply([m3], R)
        check("re-apply after revoke ok", rn.status_code == 201, rn.text)
        check(
            "re-apply lands on replacement",
            rn.json()["items"][0]["authorization"]["id"] == t,
            rn.text,
        )

        print("== 404 / 422 ==")
        check("missing source 404", migrate(999999, t).status_code == 404)
        check("missing replacement 404", migrate(s, 999999).status_code == 404)
        bad = c.post(
            "/api/v1/authorizations/migrate",
            json={"source_authorization_id": 0,
                  "replacement_authorization_id": 1},
        )
        check("non-positive id 422", bad.status_code == 422, bad.text)
        bad2 = c.post(
            "/api/v1/authorizations/migrate",
            json={"source_authorization_id": 1},
        )
        check("missing field 422", bad2.status_code == 422, bad2.text)

    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
