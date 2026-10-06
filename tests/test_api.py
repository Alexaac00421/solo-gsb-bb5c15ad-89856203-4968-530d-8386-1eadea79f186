"""端到端功能测试（真实 PostgreSQL + TestClient）。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_api
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
    global failed
    Base.metadata.drop_all(bind=engine)
    init_db()

    with TestClient(app) as client:
        print("== 健康检查 ==")
        r = client.get("/health")
        check("health 200", r.status_code == 200, r.text)

        print("== 登记素材 ==")
        r = client.post("/api/v1/materials", json={"code": "m1", "name": "素材一"})
        check("create material m1", r.status_code == 201, r.text)
        m1 = r.json()["id"]
        r = client.post("/api/v1/materials", json={"code": "m2", "name": "素材二"})
        m2 = r.json()["id"]
        r = client.post("/api/v1/materials", json={"code": "m3", "name": "素材三"})
        m3 = r.json()["id"]
        r = client.post("/api/v1/materials", json={"code": "m1", "name": "重复"})
        check("duplicate material code 409", r.status_code == 409, r.text)
        check(
            "duplicate code error code",
            r.json()["error"]["code"] == "duplicate_material",
            r.text,
        )

        print("== 登记授权（非法参数） ==")
        bad = client.post(
            "/api/v1/authorizations",
            json={
                "material_id": m1,
                "region": "CN",
                "channel": "web",
                "starts_at": "2026-10-04T18:00:00+08:00",
                "ends_at": "2026-10-04T18:00:00+08:00",
                "max_count": 1,
            },
        )
        check("zero-length period 422", bad.status_code == 422, bad.text)
        bad2 = client.post(
            "/api/v1/authorizations",
            json={
                "material_id": m1,
                "region": "CN",
                "channel": "web",
                "starts_at": "2026-10-05T00:00:00+08:00",
                "ends_at": "2026-10-04T00:00:00+08:00",
                "max_count": 1,
            },
        )
        check("inverted period 422", bad2.status_code == 422, bad2.text)
        bad3 = client.post(
            "/api/v1/authorizations",
            json={
                "material_id": m1,
                "region": "CN",
                "channel": "web",
                "starts_at": "2026-10-04T00:00:00",  # 无时区
                "ends_at": "2026-10-05T00:00:00",
                "max_count": 1,
            },
        )
        check("naive datetime 422", bad3.status_code == 422, bad3.text)
        bad4 = client.post(
            "/api/v1/authorizations",
            json={
                "material_id": m1,
                "region": "CN",
                "channel": "web",
                "starts_at": "2026-10-04T00:00:00+08:00",
                "ends_at": "2026-10-05T00:00:00+08:00",
                "max_count": 0,
            },
        )
        check("max_count 0 -> 422", bad4.status_code == 422, bad4.text)
        bad5 = client.post(
            "/api/v1/authorizations",
            json={
                "material_id": 999999,
                "region": "CN",
                "channel": "web",
                "starts_at": "2026-10-04T00:00:00+08:00",
                "ends_at": "2026-10-05T00:00:00+08:00",
                "max_count": 1,
            },
        )
        check("unknown material -> 422", bad5.status_code == 422, bad5.text)
        check(
            "unknown material code",
            bad5.json()["error"]["code"] == "unknown_material",
            bad5.text,
        )

        print("== 登记授权（左闭右开边界） ==")
        # 边界授权：[NOW, NOW+1h)
        r = client.post(
            "/api/v1/authorizations",
            json={
                "material_id": m1,
                "region": "CN",
                "channel": "web",
                "starts_at": NOW.isoformat(),
                "ends_at": (NOW + timedelta(hours=1)).isoformat(),
                "max_count": 100,
            },
        )
        check("create boundary auth", r.status_code == 201, r.text)
        auth_edge = r.json()["id"]

        # 左闭：starts_at 时刻应命中
        rl = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m1],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        check("left-closed boundary approved", rl.status_code == 201, rl.text)
        # 右开：ends_at 时刻不应命中
        rr = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m1],
                "region": "CN",
                "channel": "web",
                "occur_at": (NOW + timedelta(hours=1)).isoformat(),
            },
        )
        check(
            "right-open boundary rejected",
            rr.status_code == 422
            and rr.json()["error"]["details"][0]["reason"]
            == "no_matching_authorization",
            rr.text,
        )
        # 地区不匹配 / 渠道不匹配
        for region, channel in [("US", "web"), ("CN", "app")]:
            rx = client.post(
                "/api/v1/distributions",
                json={
                    "material_ids": [m1],
                    "region": region,
                    "channel": channel,
                    "occur_at": (NOW + timedelta(minutes=30)).isoformat(),
                },
            )
            check(
                f"mismatch {region}/{channel} rejected",
                rx.status_code == 422
                and rx.json()["error"]["details"][0]["reason"]
                == "no_matching_authorization",
                rx.text,
            )

        print("== 次数耗尽 / 撤销释放 ==")
        r = client.post(
            "/api/v1/authorizations",
            json={
                "material_id": m2,
                "region": "CN",
                "channel": "web",
                "starts_at": (NOW - timedelta(days=1)).isoformat(),
                "ends_at": (NOW + timedelta(days=1)).isoformat(),
                "max_count": 2,
            },
        )
        auth2 = r.json()["id"]
        rd1 = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m2],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        check("quota first approved", rd1.status_code == 201, rd1.text)
        d1 = rd1.json()["id"]
        check(
            "used_count 1/2",
            client.get(f"/api/v1/authorizations/{auth2}").json()["used_count"]
            == 1,
        )
        # 占满最后一次
        d_full = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m2],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        ).json()["id"]
        rd2 = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m2],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        check(
            "quota exhausted rejected 422",
            rd2.status_code == 422
            and rd2.json()["error"]["details"][0]["reason"]
            == "quota_exhausted",
            rd2.text,
        )
        # 已通过申请可查
        rg = client.get(f"/api/v1/distributions/{d1}")
        check("get approved distribution", rg.status_code == 200, rg.text)
        check(
            "get returns authorization",
            rg.json()["items"][0]["authorization"]["id"] == auth2,
        )
        # 撤销一次并释放
        rv = client.post(f"/api/v1/distributions/{d1}/revoke")
        check("revoke 200", rv.status_code == 200, rv.text)
        check("status revoked", rv.json()["status"] == "revoked", rv.text)
        check(
            "quota released (2/2 -> 1/2)",
            client.get(f"/api/v1/authorizations/{auth2}").json()["used_count"]
            == 1,
        )
        rv2 = client.post(f"/api/v1/distributions/{d1}/revoke")
        check(
            "double revoke 409",
            rv2.status_code == 409
            and rv2.json()["error"]["code"] == "already_revoked",
            rv2.text,
        )
        # 释放后可再申请
        rd3 = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m2],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        check("approve after release", rd3.status_code == 201, rd3.text)
        # 已撤销申请仍可查
        check(
            "revoked distribution still queryable",
            client.get(f"/api/v1/distributions/{d1}").status_code == 200,
        )

        print("== 停用仅影响新申请 ==")
        # d_full 仍占用 1 次，当前已满；先撤销 rd3 让余量=1 但停用，
        # 验证「停用 + 有余量」也必须拒绝。
        client.post(f"/api/v1/distributions/{rd3.json()['id']}/revoke")
        rp = client.patch(
            f"/api/v1/authorizations/{auth2}", json={"status": "inactive"}
        )
        check("deactivate auth", rp.status_code == 200, rp.text)
        rd5 = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m2],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        check(
            "inactive with quota still rejected",
            rd5.status_code == 422
            and rd5.json()["error"]["details"][0]["reason"]
            == "authorization_inactive",
            rd5.text,
        )
        # 重新启用后可通过
        client.patch(f"/api/v1/authorizations/{auth2}", json={"status": "active"})
        rd6 = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m2],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        check("re-activate approved", rd6.status_code == 201, rd6.text)

        print("== 整体拒绝原子性（不扣次）+ 多项原因 ==")
        r = client.post(
            "/api/v1/authorizations",
            json={
                "material_id": m3,
                "region": "CN",
                "channel": "web",
                "starts_at": (NOW - timedelta(days=1)).isoformat(),
                "ends_at": (NOW + timedelta(days=1)).isoformat(),
                "max_count": 1,
            },
        )
        auth3 = r.json()["id"]
        before = client.get(f"/api/v1/authorizations/{auth3}").json()[
            "used_count"
        ]
        rmix = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m3, m2, 999998, 999999],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        # 未知编号优先整体拒绝
        check("unknown among items 422", rmix.status_code == 422, rmix.text)
        check(
            "unknown among items code",
            rmix.json()["error"]["code"] == "unknown_material",
            rmix.text,
        )
        after = client.get(f"/api/v1/authorizations/{auth3}").json()[
            "used_count"
        ]
        check("unknown reject no consume", before == after == 0, f"{before}/{after}")

        # m3 有余量、m2 已耗尽：混合申请应整体拒绝并列明每项原因
        rmix2 = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m3, m2],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        check("partial reject 422", rmix2.status_code == 422, rmix2.text)
        details = rmix2.json()["error"]["details"]
        check("one reason per failing item", len(details) == 1, rmix2.text)
        check(
            "reason is quota_exhausted",
            details[0]["reason"] == "quota_exhausted",
            rmix2.text,
        )
        check(
            "m3 not consumed on reject",
            client.get(f"/api/v1/authorizations/{auth3}").json()["used_count"]
            == 0,
            rmix2.text,
        )

        print("== 重复素材 / 未知路径编号 ==")
        rdup = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m1, m1],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        check(
            "duplicate material items 422",
            rdup.status_code == 422
            and rdup.json()["error"]["code"] == "duplicate_material_items",
            rdup.text,
        )
        check(
            "unknown distribution id 404",
            client.get("/api/v1/distributions/999999").status_code == 404,
        )
        check(
            "revoke unknown id 404",
            client.post("/api/v1/distributions/999999/revoke").status_code
            == 404,
        )
        check(
            "unknown material path 404",
            client.get("/api/v1/materials/999999").status_code == 404,
        )
        check(
            "unknown auth patch 404",
            client.patch(
                "/api/v1/authorizations/999999", json={"status": "active"}
            ).status_code
            == 404,
        )

        print("== 重叠授权选择规则（到期早优先，其次编号小） ==")
        # auth edge 余量充足；新增两个更晚到期授权，NOW+30min 时刻三重叠
        late1 = client.post(
            "/api/v1/authorizations",
            json={
                "material_id": m1,
                "region": "CN",
                "channel": "web",
                "starts_at": (NOW - timedelta(hours=2)).isoformat(),
                "ends_at": (NOW + timedelta(hours=5)).isoformat(),
                "max_count": 100,
            },
        ).json()["id"]
        late2 = client.post(
            "/api/v1/authorizations",
            json={
                "material_id": m1,
                "region": "CN",
                "channel": "web",
                "starts_at": (NOW - timedelta(hours=2)).isoformat(),
                "ends_at": (NOW + timedelta(hours=3)).isoformat(),
                "max_count": 100,
            },
        ).json()["id"]
        rsel = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m1],
                "region": "CN",
                "channel": "web",
                "occur_at": (NOW + timedelta(minutes=30)).isoformat(),
            },
        )
        chosen = rsel.json()["items"][0]["authorization"]["id"]
        # NOW+30min 仍在 edge 授权（+1h 到期）窗口内，edge 到期最早
        check(
            "earliest expiry chosen (edge)",
            chosen == auth_edge,
            f"chosen={chosen} expected={auth_edge}",
        )
        # edge 窗口外（+2h）：应选到期较早的 late2(+3h) 而非 late1(+5h)
        rsel2 = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [m1],
                "region": "CN",
                "channel": "web",
                "occur_at": (NOW + timedelta(hours=2)).isoformat(),
            },
        )
        chosen2 = rsel2.json()["items"][0]["authorization"]["id"]
        check(
            "next-earliest expiry chosen (late2)",
            chosen2 == late2,
            f"chosen={chosen2} expected={late2}",
        )

    print(f"\n结果：{passed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
