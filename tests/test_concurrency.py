"""并发安全测试：高并发下绝不能超额占用。

场景：
1. 单一授权 max_count=5，20 个并发申请（同 region/channel），
   恰好 5 个成功、15 个 quota_exhausted，used_count == 5。
2. 多素材批量申请（每项引用不同授权）混合并发，验证行锁 + 咨询锁
   不会让任何一个授权超过额度。
3. 并发撤销与新申请交错，最终 used_count 必须恒等于「未撤销的通过申请数」。
4. 同一申请并发撤销两次，恰一次成功。
5. 改挂与撤销并发，归属正确、释放不重复不串号。
6. 改挂与新申请并发，替代授权绝不超额、账实相符。
7. 两个并发改挂串行化，次数不重复搬移。
8. 授权启用/停用（PATCH）与通道写事务共用同一把通道咨询锁：
   锁被持有时 PATCH 必须阻塞，锁内提交后新事务只能看到变更前/后的完整状态。
9. 停用与「撤销触发的候补成交」并发：结果必须等价于某一串行顺序，
   停用提交后候补不得再按已停用授权成交。
10. 停用与额度转拨并发：转拨不得在状态变更中间通过，
    结果必须等价于某一串行顺序且两端额度账实相符。

运行：
    DATABASE_URL=postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439 \
        python3 -m tests.test_concurrency
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://licensing@/licensing?host=/tmp&port=5439",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402
from app.services import _bucket_lock_id  # noqa: E402

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


def scenario_patch_shares_channel_lock() -> None:
    print("== 场景8：授权启用/停用与通道写事务共用同一把通道咨询锁 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c8", "name": "状态变更锁"}
    ).json()["id"]
    aid = client.post(
        "/api/v1/authorizations",
        json={
            "material_id": mid,
            "region": "CN",
            "channel": "patchlock",
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
            "max_count": 3,
        },
    ).json()["id"]
    lock_id = _bucket_lock_id("CN", "patchlock")

    def patch(status: str):
        c = TestClient(app)
        return c.patch(
            f"/api/v1/authorizations/{aid}", json={"status": status}
        )

    # 8a) 另一事务持有通道咨询锁期间，PATCH 必须阻塞（不得绕过锁直接提交）；
    #     阻塞期间外部读到的仍是变更前的完整状态；锁释放后 PATCH 才提交。
    holder = SessionLocal()
    holder.execute(
        text("SELECT pg_advisory_xact_lock(:lk)"), {"lk": lock_id}
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(patch, "inactive")
        time.sleep(1.5)
        assert not fut.done(), "通道咨询锁被持有时 PATCH 必须等待，不得直接提交"
        probe = SessionLocal()
        try:
            observed = probe.execute(
                select(models.Authorization.status).where(
                    models.Authorization.id == aid
                )
            ).scalar_one()
        finally:
            probe.close()
        assert observed == "active", (
            f"PATCH 阻塞期间外部不得看到中间态，实际 {observed}"
        )
        holder.rollback()  # 释放通道咨询锁
        r = fut.result(timeout=15)
    holder.close()
    assert r.status_code == 200 and r.json()["status"] == "inactive", r.text
    assert (
        client.get(f"/api/v1/authorizations/{aid}").json()["status"]
        == "inactive"
    )
    print("  PASS  8a 通道锁持有期间 PATCH 阻塞，释放后提交为 inactive")

    # 8b) PATCH 与新发行申请在同一把通道锁后排队：二者都被阻塞，锁释放后
    #     按某一串行顺序完成——申请要么看到启用后的完整状态（201），
    #     要么看到启用前的完整状态（422 authorization_inactive）。
    holder = SessionLocal()
    holder.execute(
        text("SELECT pg_advisory_xact_lock(:lk)"), {"lk": lock_id}
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_patch = pool.submit(patch, "active")
        f_dist = pool.submit(
            lambda: TestClient(app).post(
                "/api/v1/distributions",
                json={
                    "material_ids": [mid],
                    "region": "CN",
                    "channel": "patchlock",
                    "occur_at": NOW.isoformat(),
                },
            )
        )
        time.sleep(1.5)
        assert not f_patch.done() and not f_dist.done(), (
            "PATCH 与新申请都应在通道锁后排队"
        )
        holder.rollback()
        rp = f_patch.result(timeout=15)
        rd = f_dist.result(timeout=15)
    holder.close()
    assert rp.status_code == 200 and rp.json()["status"] == "active", rp.text
    used = client.get(f"/api/v1/authorizations/{aid}").json()["used_count"]
    if rd.status_code == 201:
        # 串行顺序：启用先提交，新申请看到启用后的完整状态。
        assert used == 1, f"申请成功后 used_count 应为 1，实际 {used}"
    else:
        # 串行顺序：新申请先进入，看到的仍是停用状态。
        assert rd.status_code == 422, rd.text
        assert (
            rd.json()["error"]["details"][0]["reason"]
            == "authorization_inactive"
        ), rd.text
        assert used == 0, f"申请被拒绝不得占额，实际 used_count={used}"
    print(
        f"  PASS  8b PATCH 与新申请同锁排队，申请结果={rd.status_code}"
        "（等价于某一串行顺序）"
    )


def scenario_disable_vs_waitlist_fulfill() -> None:
    print("== 场景9：停用与撤销触发的候补成交并发，结果等价于某一串行顺序 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c9", "name": "停用候补竞态"}
    ).json()["id"]

    fulfilled = failed = 0
    for round_no in range(10):
        channel = f"wl{round_no}"
        aid = client.post(
            "/api/v1/authorizations",
            json={
                "material_id": mid,
                "region": "CN",
                "channel": channel,
                "starts_at": (NOW - timedelta(days=1)).isoformat(),
                "ends_at": (NOW + timedelta(days=1)).isoformat(),
                "max_count": 1,
            },
        ).json()["id"]
        # 占满唯一额度，随后候补入队。
        rd = client.post(
            "/api/v1/distributions",
            json={
                "material_ids": [mid],
                "region": "CN",
                "channel": channel,
                "occur_at": NOW.isoformat(),
            },
        )
        assert rd.status_code == 201, rd.text
        did = rd.json()["id"]
        rw = client.post(
            "/api/v1/waitlists",
            json={
                "material_ids": [mid],
                "region": "CN",
                "channel": channel,
                "occur_at": NOW.isoformat(),
                "ttl_minutes": 60,
            },
        )
        assert rw.status_code == 201 and rw.json()["status"] == "pending", rw.text
        wid = rw.json()["id"]

        # 并发：停用授权 vs 撤销申请（撤销释放额度会触发候补成交判定）。
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_patch = pool.submit(
                lambda: TestClient(app).patch(
                    f"/api/v1/authorizations/{aid}",
                    json={"status": "inactive"},
                )
            )
            f_revoke = pool.submit(
                lambda: TestClient(app).post(
                    f"/api/v1/distributions/{did}/revoke"
                )
            )
            rp = f_patch.result()
            rr = f_revoke.result()
        assert rp.status_code == 200 and rp.json()["status"] == "inactive"
        assert rr.status_code == 200, rr.text

        w = client.get(f"/api/v1/waitlists/{wid}").json()
        auth = client.get(f"/api/v1/authorizations/{aid}").json()
        if w["status"] == "fulfilled":
            # 串行顺序：撤销先成交（授权当时仍启用），停用随后生效；
            # 成交占用完整保留，停用不影响已通过的申请。
            fulfilled += 1
            assert auth["used_count"] == 1, auth
            d = client.get(f"/api/v1/distributions/{w['distribution_id']}").json()
            assert d["status"] == "approved"
        else:
            # 串行顺序：停用先生效，撤销释放时授权已不再匹配，
            # 候补标记 failed，绝不按已停用授权生成占用。
            assert w["status"] == "failed", w
            assert w["failure_reason"] == "authorization_inactive", w
            failed += 1
            assert auth["used_count"] == 0, auth
        assert auth["status"] == "inactive"
    print(
        f"  PASS  10 轮：成交后停用={fulfilled} 次、停用后失配={failed} 次，"
        "均等价于某一串行顺序，无「停用后仍生成占用」"
    )


def scenario_disable_vs_transfer() -> None:
    print("== 场景10：停用与额度转拨并发，转拨不得在状态变更中间通过 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c10", "name": "停用转拨竞态"}
    ).json()["id"]

    moved = blocked = 0
    for round_no in range(10):
        channel = f"tr{round_no}"
        common = {
            "material_id": mid,
            "region": "CN",
            "channel": channel,
            "starts_at": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=1)).isoformat(),
        }
        src = client.post(
            "/api/v1/authorizations", json={**common, "max_count": 10}
        ).json()["id"]
        tgt = client.post(
            "/api/v1/authorizations", json={**common, "max_count": 5}
        ).json()["id"]
        op_no = f"OP-PATCH-TR-{round_no}"
        payload = {
            "source_authorization_id": src,
            "target_authorization_id": tgt,
            "count": 3,
            "operation_no": op_no,
        }

        def delayed_patch():
            # 奇数轮稍作延迟，让转拨先取得通道锁，覆盖两种串行顺序。
            if round_no % 2 == 1:
                time.sleep(0.3)
            return TestClient(app).patch(
                f"/api/v1/authorizations/{src}", json={"status": "inactive"}
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_patch = pool.submit(delayed_patch)
            f_tr = pool.submit(
                lambda: TestClient(app).post(
                    "/api/v1/authorizations/transfer", json=payload
                )
            )
            rp = f_patch.result()
            rt = f_tr.result()

        assert rp.status_code == 200 and rp.json()["status"] == "inactive"
        s = client.get(f"/api/v1/authorizations/{src}").json()
        t = client.get(f"/api/v1/authorizations/{tgt}").json()
        if rt.status_code == 200:
            # 串行顺序：转拨先完成（两端当时均启用），停用随后生效。
            moved += 1
            assert (s["max_count"], t["max_count"]) == (7, 8), (s, t)
            # 幂等重放不受随后的停用影响：同号同参返回首次结果。
            replay = client.post(
                "/api/v1/authorizations/transfer", json=payload
            )
            assert replay.status_code == 200, replay.text
            assert replay.json() == rt.json(), (replay.json(), rt.json())
        else:
            # 串行顺序：停用先生效，转拨在锁内读到完整新状态而整笔拒绝，
            # 两端额度均不变。
            assert rt.status_code == 422, rt.text
            err = rt.json()["error"]
            assert err["code"] == "quota_transfer_rejected", rt.text
            assert (
                err["details"][0]["reason"] == "authorization_inactive"
            ), rt.text
            assert (s["max_count"], t["max_count"]) == (10, 5), (s, t)
            blocked += 1
        # 账实相符：转拨只动 max_count，两端占用/预留不变，不变量保持。
        assert s["used_count"] == 0 and t["used_count"] == 0
        assert s["reserved_count"] == 0 and t["reserved_count"] == 0
        assert s["status"] == "inactive" and t["status"] == "active"
    assert moved > 0 and blocked > 0, (
        f"两种串行顺序都应被覆盖：moved={moved} blocked={blocked}"
    )
    print(
        f"  PASS  10 轮：转拨先于停用={moved} 次、停用先于转拨={blocked} 次，"
        "无中间态、额度账实相符"
    )


def scenario_disable_vs_migrate() -> None:
    print("== 场景11：启用/停用与改挂并发，状态翻转与次数搬移不交错 ==")
    client = TestClient(app)
    mid = client.post(
        "/api/v1/materials", json={"code": "c11", "name": "状态改挂竞态"}
    ).json()["id"]

    for round_no in range(6):
        channel = f"mg{round_no}"
        common = {
            "material_id": mid,
            "region": "CN",
            "channel": channel,
            "starts_at": (NOW - timedelta(days=2)).isoformat(),
            "ends_at": (NOW + timedelta(days=2)).isoformat(),
        }
        src = client.post(
            "/api/v1/authorizations", json={**common, "max_count": 5}
        ).json()["id"]
        tgt = client.post(
            "/api/v1/authorizations", json={**common, "max_count": 50}
        ).json()["id"]
        for _ in range(2):
            r = client.post(
                "/api/v1/distributions",
                json={
                    "material_ids": [mid],
                    "region": "CN",
                    "channel": channel,
                    "occur_at": NOW.isoformat(),
                },
            )
            assert r.status_code == 201, r.text

        # 并发：PATCH 翻转源授权状态（启用）vs 改挂（成功会把源置为停用）。
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_patch = pool.submit(
                lambda: TestClient(app).patch(
                    f"/api/v1/authorizations/{src}", json={"status": "active"}
                )
            )
            f_mg = pool.submit(
                lambda: TestClient(app).post(
                    "/api/v1/authorizations/migrate",
                    json={
                        "source_authorization_id": src,
                        "replacement_authorization_id": tgt,
                    },
                )
            )
            rp = f_patch.result()
            rm = f_mg.result()

        assert rp.status_code == 200, rp.text
        assert rm.status_code == 200, rm.text
        assert rm.json()["migrated_count"] == 2, rm.text
        s = client.get(f"/api/v1/authorizations/{src}").json()
        t = client.get(f"/api/v1/authorizations/{tgt}").json()
        # 次数搬移完整：源迁出归零、目标承接 2 条，绝不部分搬移。
        assert s["used_count"] == 0 and t["used_count"] == 2, (s, t)
        # 最终状态恰为某一串行顺序的结果：改挂后启用（active）
        # 或启用后改挂（inactive），不得出现中间态。
        assert s["status"] in ("active", "inactive"), s
        # 账实相符：目标 used_count 等于归属它的有效明细数。
        db = SessionLocal()
        try:
            valid = db.scalar(
                select(func.count())
                .select_from(models.DistributionItem)
                .join(
                    models.Distribution,
                    models.DistributionItem.distribution_id
                    == models.Distribution.id,
                )
                .where(
                    models.DistributionItem.authorization_id == tgt,
                    models.Distribution.status == "approved",
                )
            )
        finally:
            db.close()
        assert t["used_count"] == valid, (t["used_count"], valid)
    print("  PASS  6 轮：状态翻转与改挂串行，次数完整搬移、账实相符")


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
        scenario_patch_shares_channel_lock()
        scenario_disable_vs_waitlist_fulfill()
        scenario_disable_vs_transfer()
        scenario_disable_vs_migrate()
    print("\n全部并发场景通过：未发生超额、账实相符。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
