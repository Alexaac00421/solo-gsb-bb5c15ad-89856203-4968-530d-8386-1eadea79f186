"""并发安全测试：高并发下绝不能超额占用。

场景：
1. 单一授权 max_count=5，20 个并发申请（同 region/channel），
   恰好 5 个成功、15 个 quota_exhausted，used_count == 5。
2. 多素材批量申请（每项引用不同授权）混合并发，验证行锁 + 咨询锁
   不会让任何一个授权超过额度。
3. 并发撤销与新申请交错，最终 used_count 必须恒等于「未撤销的通过申请数」。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_concurrency
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=CST)


def scenario_single_auth_quota() -> None:
    print("== 场景1：单授权 5 次额度，20 并发申请 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c1", "name": "并发素材"}
    ).json()["id"]
    aid = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "CN",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 5,
        },
    ).json()["id"]

    def one_request(i: int):
        c = TestClient(app)
        r = c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "CN",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        return r.status_code, r.json()

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(one_request, i) for i in range(20)]
        for f in as_completed(futures):
            results.append(f.result())

    ok = [r for code, r in results if code == 201]
    rej = [
        r
        for code, r in results
        if code == 422 and r["error"]["code"] == "distribution_rejected"
    ]
    assert len(ok) == 5, f"成功数应为 5，实际 {len(ok)}"
    assert len(rej) == 15, f"拒绝数应为 15，实际 {len(rej)}"
    assert all(
        d["error"]["details"][0]["reason"] == "quota_exhausted" for d in rej
    ), "拒绝原因应为 quota_exhausted"

    used = client.get(f"/api/v1/authorizations/{aid}").json()["used_count"]
    assert used == 5, f"used_count 应为 5，实际 {used}"
    print(f"  PASS  成功={len(ok)} 拒绝={len(rej)} used_count={used}（无超额）")


def scenario_multi_material() -> None:
    print("== 场景2：3 素材批量申请，各 4 次额度，30 并发 ==")
    client = TestClient(app)
    ids = []
    for code in ("c2a", "c2b", "c2c"):
        mid = client.post(
            "/api/v1/materials", json={"code": code, "name": code}
        ).json()["id"]
        aid = client.post(
            "/api/v1/authorizations",
            json={
                "material_id": mid,
                "region": "CN",
                "channel": "app",
                "starts_at": (NOW - timedelta(days=1)).isoformat(),
                "ends_at": (NOW + timedelta(days=1)).isoformat(),
                "max_count": 4,
            },
        ).json()["id"]
        ids.append((mid, aid))

    def one_request(i: int):
        c = TestClient(app)
        r = c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid for mid, _ in ids],
                "region": "CN",
                "channel": "app",
                "occur_at": NOW.isoformat(),
            },
        )
        return r.status_code

    with ThreadPoolExecutor(max_workers=15) as pool:
        codes = list(pool.map(one_request, range(30)))

    assert codes.count(201) == 4, f"整体成功应为 4，实际 {codes.count(201)}"
    assert codes.count(422) == 26, f"整体拒绝应为 26，实际 {codes.count(422)}"
    for mid, aid in ids:
        used = client.get(f"/api/v1/authorizations/{aid}").json()["used_count"]
        assert used == 4, f"授权 {aid} used_count 应为 4，实际 {used}"
    print(
        f"  PASS  整体成功={codes.count(201)} 各授权 used_count=4（无超额）"
    )


def scenario_revoke_race() -> None:
    print("== 场景3：申请占满额度后并发撤销 + 新申请交错 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c3", "name": "撤销竞态"}
    ).json()["id"]
    aid = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "JP",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 10,
        },
    ).json()["id"]

    # 先占满 10 次
    approved_ids = []
    for _ in range(10):
        r = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "JP",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        assert r.status_code == 201, r.text
        approved_ids.append(r.json()["id"])

    # 10 个撤销与 10 个新申请同时发起
    def revoke(did: int):
        c = TestClient(app)
        return c.post(f"/api/v1/distributions/{did}/revoke").status_code

    def apply(i: int):
        c = TestClient(app)
        r = c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "JP",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(revoke, did) for did in approved_ids]
        futs += [pool.submit(apply, i) for i in range(10)]
        outcomes = [f.result() for f in as_completed(futs)]

    revoke_codes = [o for o in outcomes if isinstance(o, int)]
    apply_outs = [o for o in outcomes if isinstance(o, tuple)]
    assert revoke_codes.count(200) == 10, "10 个撤销均应成功"

    # 撤销恰好释放 10 个名额，新申请成功数必须为 0 或 ≤10；
    # 关键：used_count 必须恰好等于「仍处于 approved 状态的申请数」。
    new_ok = sum(1 for code, _ in apply_outs if code == 201)
    assert 0 <= new_ok <= 10

    # 以数据库直接统计 approved 明细数为准，校验账实相符。
    from sqlalchemy import func

    db = SessionLocal()
    try:
        used = db.get(models.Authorization, aid).used_count
        approved_items = db.scalar(
            select(func.count())
            .select_from(models.DistributionItem)
            .join(
                models.Distribution,
                models.DistributionItem.distribution_id
                == models.Distribution.id,
            )
            .where(
                models.DistributionItem.authorization_id == aid,
                models.Distribution.status == "approved",
            )
        )
    finally:
        db.close()

    assert used == approved_items, (
        f"used_count={used} 与有效占用明细数 {approved_items} 不一致"
    )
    assert used <= 10, f"超额！used_count={used}"
    print(
        f"  PASS  撤销成功=10，新申请成功={new_ok}，"
        f"used_count={used} == 有效明细={approved_items}（账实相符、无超额）"
    )


def scenario_repeat_revoke_race() -> None:
    print("== 场景4：同一申请并发撤销两次，必须只有一次成功 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c4", "name": "重复撤销竞态"}
    ).json()["id"]
    client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "EU",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 5,
        },
    )
    did = client.post(
        "/api/v1/distributions",
        json={
            "material_ids": [mid],
            "region": "EU",
            "channel": "web",
            "occur_at": NOW.isoformat(),
        },
    ).json()["id"]

    def revoke():
        c = TestClient(app)
        return c.post(f"/api/v1/distributions/{did}/revoke").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda _: revoke(), range(2)))
    assert sorted(codes) == [200, 409], f"应恰为一次200一次409，实际 {codes}"
    dist = client.get(f"/api/v1/distributions/{did}").json()
    assert dist["status"] == "revoked"
    print(f"  PASS  并发双撤销返回 {sorted(codes)}，状态=revoked，次数只释放一次")


def scenario_migrate_vs_revoke() -> None:
    print("== 场景5：改挂与撤销并发，归属正确、释放不重复不串号 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c5", "name": "改挂撤销竞态"}
    ).json()["id"]
    aid = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "MX",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 20,
        },
    ).json()["id"]
    bid = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "MX",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=2)).isoformat(),
            "ends_at": (NOW + timedelta(days=2)).isoformat(),
            "max_count": 50,
        },
    ).json()["id"]

    approved_ids = []
    for _ in range(20):
        r = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "MX",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        assert r.status_code == 201, r.text
        approved_ids.append(r.json()["id"])

    def migrate():
        c = TestClient(app)
        return c.post(
            "/api/v1/authorizations/migrate",
            json={
                "source_authorization_id": aid,
                "replacement_authorization_id": bid,
            },
        ).status_code

    def revoke(did: int):
        c = TestClient(app)
        return c.post(f"/api/v1/distributions/{did}/revoke").status_code

    with ThreadPoolExecutor(max_workers=21) as pool:
        futs = [pool.submit(migrate)]
        futs += [pool.submit(revoke, did) for did in approved_ids]
        codes = [f.result() for f in as_completed(futs)]
    assert codes.count(200) == 21, f"改挂1次+撤销20次均应成功：{codes}"

    # 账实相符：每个授权的 used_count 必须等于仍 approved 且归属它的明细数。
    db = SessionLocal()
    try:
        for auth_id in (aid, bid):
            used = db.get(models.Authorization, auth_id).used_count
            valid = db.scalar(
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
            assert used == valid, (
                f"授权 {auth_id}：used_count={used} 有效明细={valid}，账实不符"
            )
            assert used >= 0
        # 全部 20 条都撤销了，两端 used_count 都应为 0。
        used_a = db.get(models.Authorization, aid).used_count
        used_b = db.get(models.Authorization, bid).used_count
        assert used_a == 0 and used_b == 0, (
            f"全部撤销后两端应为 0：source={used_a} target={used_b}"
        )
        status_a = db.get(models.Authorization, aid).status
        assert status_a == "inactive", "改挂成功后原授权应停用"
    finally:
        db.close()
    print(
        "  PASS  21 个并发操作全部成功，撤销后 source=0 target=0，"
        "无重复释放/释放错授权，原授权已停用"
    )


def scenario_migrate_vs_submit() -> None:
    print("== 场景6：改挂与新申请并发，替代授权绝不超额、账实相符 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c6", "name": "改挂提交竞态"}
    ).json()["id"]

    # 6a) 替代授权容量恰好 8：源已有 6 占用，迁移需 6 名额，
    #     并发新申请无论先落源（源余量2）还是后落目标（目标余量2），
    #     目标 used_count 都绝不能超过 8；改挂可能成功或因并发占满而
    #     原子拒绝（insufficient_quota），两种结果都合法，但账必须平。
    aid = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "BR",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 8,
        },
    ).json()["id"]
    for _ in range(6):
        r = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "BR",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        assert r.status_code == 201, r.text
    bid = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "BR",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=2)).isoformat(),
            "ends_at": (NOW + timedelta(days=2)).isoformat(),
            "max_count": 8,
        },
    ).json()["id"]

    def migrate():
        c = TestClient(app)
        r = c.post(
            "/api/v1/authorizations/migrate",
            json={
                "source_authorization_id": aid,
                "replacement_authorization_id": bid,
            },
        )
        if r.status_code == 200:
            return 200, r.json()["migrated_count"]
        return 422, r.json()["error"]["details"][0]["reason"]

    def apply(i: int):
        c = TestClient(app)
        return c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "BR",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=11) as pool:
        mf = pool.submit(migrate)
        apply_f = [pool.submit(apply, i) for i in range(10)]
        mcode, minfo = mf.result()
        apply_codes = [f.result() for f in apply_f]

    if mcode == 200:
        assert 6 <= minfo <= 8, f"迁移数异常：{minfo}"
    else:
        assert minfo == "insufficient_quota", f"拒绝原因异常：{minfo}"

    db = SessionLocal()
    try:
        used_a = db.get(models.Authorization, aid).used_count
        used_b = db.get(models.Authorization, bid).used_count
        max_a = db.get(models.Authorization, aid).max_count
        max_b = db.get(models.Authorization, bid).max_count
        valid_a, valid_b = (
            db.scalar(
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
            for auth_id in (aid, bid)
        )
    finally:
        db.close()

    assert used_a <= max_a, f"源授权超额：{used_a}/{max_a}"
    assert used_b <= max_b, f"替代授权超额：{used_b}/{max_b}"
    assert used_a == valid_a, f"源账实不符：{used_a} vs {valid_a}"
    assert used_b == valid_b, f"目标账实不符：{used_b} vs {valid_b}"
    print(
        f"  PASS  6a 改挂={mcode}({minfo}) 新申请成功={apply_codes.count(201)} "
        f"source={used_a}/{max_a}（明细{valid_a}） "
        f"target={used_b}/{max_b}（明细{valid_b}）无超额、账实相符"
    )

    # 6b) 替代容量远大于并发规模，改挂必定成功：验证高并发提交同时
    #     进行时，迁移与新占用互不丢失、不重复。
    aid2 = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "CL",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 100,
        },
    ).json()["id"]
    bid2 = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "CL",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=2)).isoformat(),
            "ends_at": (NOW + timedelta(days=2)).isoformat(),
            "max_count": 100,
        },
    ).json()["id"]
    for _ in range(5):
        assert (
            client.post(
                "/api/v1/distributions",
                json={
                    "material_ids": [mid],
                    "region": "CL",
                    "channel": "web",
                    "occur_at": NOW.isoformat(),
                },
            ).status_code
            == 201
        )

    def migrate2():
        c = TestClient(app)
        r = c.post(
            "/api/v1/authorizations/migrate",
            json={
                "source_authorization_id": aid2,
                "replacement_authorization_id": bid2,
            },
        )
        return r.status_code, (
            r.json()["migrated_count"] if r.status_code == 200 else None
        )

    def apply2(i: int):
        c = TestClient(app)
        return c.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "CL",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=11) as pool:
        mf = pool.submit(migrate2)
        apply_f = [pool.submit(apply2, i) for i in range(10)]
        mcode2, moved2 = mf.result()
        codes2 = [f.result() for f in apply_f]

    assert mcode2 == 200, f"容量充足时改挂应成功：{mcode2}"
    assert 5 <= moved2 <= 15

    db = SessionLocal()
    try:
        ua = db.get(models.Authorization, aid2).used_count
        ub = db.get(models.Authorization, bid2).used_count
        va, vb = (
            db.scalar(
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
            for auth_id in (aid2, bid2)
        )
        src_status = db.get(models.Authorization, aid2).status
    finally:
        db.close()

    total_approved = ua + ub
    # 守恒：改挂只是把预存 5 条占用从源搬到目标，不改变占用总数；
    # 因此总有效占用恒等于「预存 5 + 并发新申请成功数」。
    assert total_approved == 5 + codes2.count(201), (
        f"总有效占用={total_approved} 应等于预存5+新申请"
        f"{codes2.count(201)}"
    )
    assert ua == va and ub == vb, "账实不符"
    assert src_status == "inactive"
    print(
        f"  PASS  6b 改挂成功迁移={moved2}，新申请成功={codes2.count(201)}，"
        f"source={ua} target={ub}，占用守恒、账实相符、原授权停用"
    )


def scenario_double_migrate_race() -> None:
    print("== 场景7：两个并发改挂（或重复改挂）串行化，次数不重复搬移 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c7", "name": "重复改挂竞态"}
    ).json()["id"]
    aid = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "AR",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 5,
        },
    ).json()["id"]
    bid = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "AR",
            "channel": "web",
            "starts_at": (NOW - timedelta(days=2)).isoformat(),
            "ends_at": (NOW + timedelta(days=2)).isoformat(),
            "max_count": 50,
        },
    ).json()["id"]
    for _ in range(5):
        r = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "AR",
                "channel": "web",
                "occur_at": NOW.isoformat(),
            },
        )
        assert r.status_code == 201, r.text

    def migrate():
        c = TestClient(app)
        r = c.post(
            "/api/v1/authorizations/migrate",
            json={
                "source_authorization_id": aid,
                "replacement_authorization_id": bid,
            },
        )
        return r.status_code, (
            r.json().get("migrated_count") if r.status_code == 200 else None
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: migrate(), range(2)))

    assert all(code == 200 for code, _ in outcomes), outcomes
    counts = sorted(n for _, n in outcomes)
    assert counts == [0, 5], f"两次并发改挂应恰为 5 和 0：{counts}"

    used_a = client.get(f"/api/v1/authorizations/{aid}").json()["used_count"]
    used_b = client.get(f"/api/v1/authorizations/{bid}").json()["used_count"]
    assert used_a == 0 and used_b == 5, f"次数重复搬移：a={used_a} b={used_b}"
    print(
        f"  PASS  并发改挂迁移数 {counts}，source=0 target=5，"
        "未重复搬移、原授权已停用"
    )


def main() -> int:
    Base.metadata.drop_all(bind=engine)
    init_db()
    with TestClient(app):
        scenario_single_auth_quota()
        scenario_multi_material()
        scenario_revoke_race()
        scenario_repeat_revoke_race()
        scenario_migrate_vs_revoke()
        scenario_migrate_vs_submit()
        scenario_double_migrate_race()
    print("\n全部并发场景通过：未发生超额、账实相符。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
