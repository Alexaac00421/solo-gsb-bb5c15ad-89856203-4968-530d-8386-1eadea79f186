"""核心业务逻辑：登记 / 核验占用 / 查询 / 撤销 / 改期 / 授权退役改挂 / 额度预留 / 候补 / 通道冻结 / 授权间额度转拨。

并发安全
========
所有通道写事务遵守统一的全局加锁顺序，杜绝跨事务死锁：

    **通道咨询锁（region+channel）→ 冻结状态闸门 → 到期预留结算 →
    候补队列处理（冻结通道仅失效不成交）→
    冻结/恢复/预留/申请/候补/转拨行 → 授权行（id 升序）→ 明细行**

通道咨询锁按 ``region+channel`` 分桶：同一通道内的提交、预留、确认、
取消、撤销、改期、改挂、候补受理与候补成交、额度转拨，以及冻结/恢复
全部串行判定。授权行只可能被同通道事务竞争（授权匹配维度即
region+channel，改挂与转拨也要求两端通道一致），因此不同通道的事务
不会争用同一授权行，跨桶不可能形成等待环；同事务内对同一批授权一律
按 id 升序 ``SELECT ... FOR UPDATE``。

通道冻结（region+channel）
===========================
``channel_freezes`` 每通道至多一行。冻结期间新的直接发行、额度预留与
候补受理在业务判定前一律拒绝（``channel_frozen``，不占额、不入队）；
既有预留确认/取消、申请撤销/改期、授权改挂、候补取消仍可进行，但这些
操作释放的额度不得使候补成交（队列只做有效期失效结算，不做成交/失败
判定）。到期预留与候补仍按原有效期惰性结算，冻结不延长。恢复时在同一
事务、同一把通道咨询锁内先结算到期、再按原受理顺序处理仍有效的候补，
提交后才放行新请求，故恢复释放的额度必然先归候补。重复同态操作
（已冻结再 freeze、已恢复再 resume）幂等，不改动原因与变更时刻。

到期释放（惰性结算）
====================
预留不依赖后台定时任务：任一通道写事务在咨询锁内、业务判定之前，首先
把该通道 ``expires_at <= now`` 的待确认预留原子标记为 ``expired`` 并
逐项归还 ``reserved_count``。因此预留一旦到期，对随后的提交/预留/余量
判定立即可见（「到期即释放」）。预留查询（单条）同样先做通道结算，
故逾期确认/取消必被拒绝。

候补队列
========
候补申请沿用既有匹配与重叠授权选择规则：全部可承接则直接成交（生成
approved 申请）；每项均有匹配授权但额度不足才入本通道队列（不占额）；
任一项缺少匹配授权则整体逐项拒绝。候补携带申请有效期
``ttl_minutes``（1～1440 分钟）与到期时刻 ``expires_at``（受理时刻
+ ttl_minutes），仅在有效期内等待成交。撤销、预留取消/到期、改期、
改挂在通道锁内释放额度后，以及新直接申请/新候补受理前，都会先把
已过有效期（左闭右开：``expires_at <= now``）的待成交候补标记为
``expired``（记录失效时刻与 ``ttl_expired`` 原因）并释放队列位置，
再按受理顺序重算队首：可整体承接则原子占额转为一笔 approved 申请并
继续下一队首；额度仍不足则停止本轮；授权已不再匹配则标记失败并继续
后项。因此新直接申请永远不会抢走可成交候补的额度，过期候补也不会
阻塞后续候补。

额度模型
========
- ``used_count``：已通过（未撤销）发行申请的正式占用，语义与历史版本
  完全一致，``remaining = max_count - used_count`` 保持兼容。
- ``reserved_count``：未到期待确认预留占用；确认时逐项转为正式占用
  （reserved-1、used+1），取消/到期时逐项归还。
- 不变量：``used_count >= 0``、``reserved_count >= 0``、
  ``used_count + reserved_count <= max_count``。

所有扣减/归还均采用带条件的 UPDATE 并断言影响行数，作为绝不超额、
绝不重复释放的最后防线。
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from zlib import crc32

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import models
from app.errors import (
    AlreadyRevoked,
    ChannelFrozen,
    ChannelNotFound,
    DistributionRejected,
    DistributionRevoked,
    DuplicateMaterialItems,
    InvalidReservationStatus,
    InvalidWaitlistStatus,
    MigrationRejected,
    MaterialCodeTaken,
    RescheduleRejected,
    ReservationAlreadyCancelled,
    ReservationAlreadyConfirmed,
    ReservationCancelled,
    ReservationExpired,
    ReservationRejected,
    ResourceNotFound,
    TransferOperationConflict,
    TransferRejected,
    UnknownMaterial,
    WaitlistAlreadyCancelled,
    WaitlistAlreadyExpired,
    WaitlistAlreadyFailed,
    WaitlistAlreadyFulfilled,
    WaitlistNotPending,
    WaitlistRejected,
)
from app.schemas import (
    AuthorizationCreate,
    AuthorizationMigrationRequest,
    ChannelFreezeRequest,
    DistributionCreate,
    DistributionReschedule,
    MaterialCreate,
    QuotaTransferCreate,
    ReservationCreate,
    WaitlistCreate,
)


# ---------------------------------------------------------------- 登记素材
def create_material(db: Session, data: MaterialCreate) -> models.Material:
    material = models.Material(code=data.code, name=data.name)
    db.add(material)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise MaterialCodeTaken(data.code)
    db.commit()
    db.refresh(material)
    return material


def get_material(db: Session, material_id: int) -> models.Material:
    material = db.get(models.Material, material_id)
    if material is None:
        raise ResourceNotFound("素材", material_id)
    return material


# ------------------------------------------------------------- 登记授权
def create_authorization(
    db: Session, data: AuthorizationCreate
) -> models.Authorization:
    if db.get(models.Material, data.material_id) is None:
        raise UnknownMaterial([data.material_id])

    auth = models.Authorization(
        material_id=data.material_id,
        region=data.region,
        channel=data.channel,
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        max_count=data.max_count,
    )
    db.add(auth)
    db.commit()
    db.refresh(auth)
    return auth


def get_authorization(db: Session, authorization_id: int) -> models.Authorization:
    """查询授权（含已用/剩余/预留）。

    查询前在该授权所在通道内做一次到期结算并锁定授权行，因此返回的
    ``reserved_count`` / ``reservable_remaining`` 是扣除已到期预留后的
    准确值；``used_count``/``remaining`` 的正式占用语义不变。
    """
    auth = db.get(models.Authorization, authorization_id)
    if auth is None:
        raise ResourceNotFound("授权", authorization_id)
    try:
        _channel_lock(db, auth.region, auth.channel)
        now = utcnow()
        frozen_row = _channel_is_frozen(db, auth.region, auth.channel)
        _settle_expired(
            db,
            now,
            region=auth.region,
            channel=auth.channel,
            extra_auth_ids=[authorization_id],
            frozen=frozen_row is not None,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(auth)
    return auth


def set_authorization_status(
    db: Session, authorization_id: int, status: str
) -> models.Authorization:
    auth = db.get(models.Authorization, authorization_id)
    if auth is None:
        raise ResourceNotFound("授权", authorization_id)
    auth.status = status
    db.commit()
    db.refresh(auth)
    return auth


# ----------------------------------------------------------- 并发基础设施
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 候补超过申请有效期被标记 expired 时记录的失效原因。
WAITLIST_EXPIRY_TTL = "ttl_expired"


def _expire_due_waitlists(
    db: Session,
    region: str,
    channel: str,
    now: datetime,
) -> int:
    """在通道咨询锁内把本通道已过申请有效期的待成交候补标记为 ``expired``。

    左闭右开：``expires_at <= now`` 即失效（恰在到期时刻处理也算过期）。
    失效的候补不占任何额度，仅推进状态并释放队列位置；队首处理循环随后
    按原受理顺序自然越过它们。旧记录 ``expires_at`` 为空（有效期功能
    上线前的候补），永不按有效期失效。返回本轮失效条数。
    """
    due = list(
        db.scalars(
            select(models.Waitlist)
            .where(
                models.Waitlist.region == region,
                models.Waitlist.channel == channel,
                models.Waitlist.status == "pending",
                models.Waitlist.expires_at.is_not(None),
                models.Waitlist.expires_at <= now,
            )
            .order_by(models.Waitlist.id.asc())
            .with_for_update()
        )
    )
    for waitlist in due:
        waitlist.status = "expired"
        waitlist.expiry_reason = WAITLIST_EXPIRY_TTL
        waitlist.expired_at = now
    if due:
        db.flush()
    return len(due)


def _bucket_lock_id(region: str, channel: str) -> int:
    """region+channel → 单个有符号 32 位整数（pg_advisory_xact_lock 参数）。"""
    raw = crc32(f"{region}\x1f{channel}".encode("utf-8"))
    return raw - 0x100000000 if raw >= 0x80000000 else raw


def _channel_lock(db: Session, region: str, channel: str) -> None:
    """第一加锁点：通道事务级咨询锁（同事务可重入，提交/回滚自动释放）。"""
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lk)"),
        {"lk": _bucket_lock_id(region, channel)},
    )


def _channel_has_authorization(
    db: Session, region: str, channel: str
) -> bool:
    """通道（region+channel）下是否存在任何授权（含已停用）。

    冻结/恢复/查询以「通道存在授权」为通道存在的判据；停用仅影响新
    申请匹配，不改变通道归属。
    """
    return db.scalar(
        select(models.Authorization.id)
        .where(
            models.Authorization.region == region,
            models.Authorization.channel == channel,
        )
        .limit(1)
    ) is not None


def _get_freeze_row(
    db: Session, region: str, channel: str
) -> models.ChannelFreeze | None:
    """读取通道冻结行（调用方须已持有通道咨询锁；无行=从未冻结）。"""
    return db.scalar(
        select(models.ChannelFreeze).where(
            models.ChannelFreeze.region == region,
            models.ChannelFreeze.channel == channel,
        )
    )


def _channel_is_frozen(
    db: Session, region: str, channel: str
) -> models.ChannelFreeze | None:
    """通道处于冻结中则返回冻结行，否则返回 None。"""
    row = _get_freeze_row(db, region, channel)
    return row if row is not None and row.status == "frozen" else None


def _ensure_channel_exists(
    db: Session, region: str, channel: str
) -> None:
    """不存在任何授权的通道：明确报错（冻结/恢复/查询共用）。"""
    if not _channel_has_authorization(db, region, channel):
        raise ChannelNotFound(region, channel)


def _freeze_state_out(
    region: str,
    channel: str,
    row: models.ChannelFreeze | None,
) -> models.ChannelFreeze:
    """构造对外返回的冻结状态对象；从未冻结的通道呈现 active 空状态。"""
    if row is not None:
        return row
    return models.ChannelFreeze(
        region=region,
        channel=channel,
        status="active",
        reason=None,
        changed_at=None,
        frozen_at=None,
        resumed_at=None,
    )


def _validate_material_ids(db: Session, material_ids: list[int]) -> None:
    """重复素材 / 未知编号校验（与申请、预留共用）。"""
    counts = Counter(material_ids)
    duplicated = sorted(mid for mid, n in counts.items() if n > 1)
    if duplicated:
        raise DuplicateMaterialItems(duplicated)

    found_ids = set(
        db.scalars(
            select(models.Material.id).where(
                models.Material.id.in_(material_ids)
            )
        )
    )
    unknown = [mid for mid in material_ids if mid not in found_ids]
    if unknown:
        raise UnknownMaterial(sorted(unknown))


def _lock_authorizations(
    db: Session, auth_ids: list[int]
) -> list[models.Authorization]:
    """按 id 升序行锁授权并以最新行值刷新（调用方须已持有通道咨询锁）。

    ``populate_existing``：调用方若在取锁前已不加锁读过同一授权行，
    此处用锁定后的最新行值刷新其属性，保证随后的余量判定基于行锁
    保护下的真实计数。
    """
    if not auth_ids:
        return []
    return list(
        db.scalars(
            select(models.Authorization)
            .where(models.Authorization.id.in_(auth_ids))
            .order_by(models.Authorization.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


def _settle_expired(
    db: Session,
    now: datetime,
    region: str | None = None,
    channel: str | None = None,
    extra_auth_ids: list[int] | None = None,
    *,
    frozen: bool = False,
) -> list[models.Authorization]:
    """在已持有通道咨询锁的前提下，结算到期未确认预留并返回锁定的授权行。

    - 锁定本通道（或全局，列表场景不调用本函数）所有 ``expires_at <= now``
      的待确认预留行（FOR UPDATE）；
    - 把这些预留涉及的授权 id 与 ``extra_auth_ids``（业务候选授权）合并，
      按 id 升序一次性 ``FOR UPDATE`` 锁定并读取最新行；
    - 逐项条件归还 ``reserved_count``，预留置为 expired；
    - 到期释放额度后按受理顺序重算本通道候补队首（成交会消耗刚释放的
      额度），随后重新锁定并返回最新授权行；**通道冻结期间例外**
      （``frozen=True``）：到期预留仍按原有效期结算释放，但**不触发候补
      成交**——冻结期间释放的额度不得使候补成交；
    - 调用方随后在同一事务内基于返回的授权行做余量判定。
    """
    resv_stmt = (
        select(models.Reservation)
        .where(
            models.Reservation.status == "pending",
            models.Reservation.expires_at <= now,
        )
        .order_by(models.Reservation.id.asc())
        .with_for_update()
    )
    if region is not None:
        resv_stmt = resv_stmt.where(
            models.Reservation.region == region,
            models.Reservation.channel == channel,
        )
    expiring = list(db.scalars(resv_stmt))

    auth_counts: Counter[int] = Counter()
    for resv in expiring:
        items = list(
            db.scalars(
                select(models.ReservationItem)
                .where(
                    models.ReservationItem.reservation_id == resv.id
                )
                .order_by(models.ReservationItem.id.asc())
            )
        )
        auth_counts.update(item.authorization_id for item in items)

    lock_ids = sorted(set(auth_counts) | set(extra_auth_ids or []))
    locked = _lock_authorizations(db, lock_ids)

    # 条件归还：每个授权按到期预留条数一次性扣减 reserved_count。
    for auth_id, n in auth_counts.items():
        result = db.execute(
            update(models.Authorization)
            .where(
                models.Authorization.id == auth_id,
                models.Authorization.reserved_count >= n,
            )
            .values(
                reserved_count=models.Authorization.reserved_count - n
            )
        )
        if result.rowcount != 1:
            db.rollback()
            raise RuntimeError(
                f"到期释放失败：授权 {auth_id} reserved_count 不足 {n}"
            )

    if expiring:
        db.execute(
            update(models.Reservation)
            .where(
                models.Reservation.id.in_([r.id for r in expiring]),
                models.Reservation.status == "pending",
            )
            .values(status="expired", expired_at=now)
        )
        db.flush()
        # 上面的 Core UPDATE 绕过了 ORM；使本事务后续属性读取拿到新值。
        db.expire_all()
        if region is not None:
            # 到期释放额度后，先重算本通道候补队首（成交会消耗刚释放的
            # 额度，失败/失效标记会推进队列）。冻结期间仅做有效期失效
            # 结算，不做成交/失败判定（释放的额度不得使候补成交）。
            _process_waitlist(
                db,
                region,
                channel,
                now,
                allow_fulfill=not frozen,
            )
        # 候补成交可能消耗了本批授权额度：重新锁定读取最新计数后返回。
        locked = _lock_authorizations(db, lock_ids)
    return locked


def _evaluate(
    material_ids: list[int],
    locked_rows: list[models.Authorization],
    *,
    region: str,
    channel: str,
    occur: datetime,
    for_reservation: bool,
    used_discount: dict[int, int] | None = None,
) -> tuple[dict[int, models.Authorization], list[dict]]:
    """沿用既有授权匹配与重叠选择顺序做逐项判定。

    选择顺序：匹配（素材/地区/渠道/左闭右开时段）→ 仅启用 →
    ``(ends_at 升序, id 升序)`` → 第一条有余量者。余量对正式申请按
    ``used + reserved < max``（未到期预留占额），预留申请同理（预留同样
    不得超额）。``used_discount``（授权 id → 扣减次数）用于改期场景：
    判定余量时先扣除本申请在该授权上的原占用，未到期预留仍全额计入。
    返回 ``(逐项命中授权, 失败原因列表)``。
    """
    discount = used_discount or {}
    grouped: dict[int, list[models.Authorization]] = {
        mid: [] for mid in material_ids
    }
    for auth in locked_rows:
        if (
            auth.region == region
            and auth.channel == channel
            and auth.starts_at <= occur < auth.ends_at
            and auth.material_id in grouped
        ):
            grouped[auth.material_id].append(auth)

    reasons: list[dict] = []
    chosen: dict[int, models.Authorization] = {}
    for index, mid in enumerate(material_ids):
        candidates = sorted(grouped[mid], key=lambda a: (a.ends_at, a.id))
        if not candidates:
            reasons.append(
                {
                    "item_index": index,
                    "material_id": mid,
                    "reason": "no_matching_authorization",
                    "message": "不存在同时匹配地区、渠道与发行时刻的授权",
                }
            )
            continue

        active = [a for a in candidates if a.status == "active"]
        if not active:
            reasons.append(
                {
                    "item_index": index,
                    "material_id": mid,
                    "reason": "authorization_inactive",
                    "message": "匹配的授权均已停用（停用仅影响新申请）",
                }
            )
            continue

        with_quota = next(
            (
                a
                for a in active
                if a.used_count - discount.get(a.id, 0) + a.reserved_count
                < a.max_count
            ),
            None,
        )
        if with_quota is None:
            detail = active[0]
            if for_reservation:
                message = (
                    f"匹配授权（id={detail.id}）可预留额度已用尽："
                    f"正式占用 {detail.used_count}、未到期预留 "
                    f"{detail.reserved_count}/{detail.max_count}"
                )
            else:
                message = (
                    f"匹配授权（id={detail.id}）可发行次数已用尽："
                    f"{detail.used_count}/{detail.max_count}"
                    + (
                        f"（另有未到期预留 {detail.reserved_count}）"
                        if detail.reserved_count
                        else ""
                    )
                )
            reasons.append(
                {
                    "item_index": index,
                    "material_id": mid,
                    "reason": "quota_exhausted",
                    "message": message,
                }
            )
            continue
        chosen[mid] = with_quota
    return chosen, reasons


def _candidate_auth_ids(
    db: Session,
    material_ids: list[int],
    region: str,
    channel: str,
    occur: datetime,
) -> list[int]:
    """不加锁预读候选授权 id（用于合并统一的 id 升序行锁集合）。"""
    return list(
        db.scalars(
            select(models.Authorization.id)
            .where(
                models.Authorization.region == region,
                models.Authorization.channel == channel,
                # 左闭右开：[starts_at, ends_at)
                models.Authorization.starts_at <= occur,
                models.Authorization.ends_at > occur,
                models.Authorization.material_id.in_(material_ids),
            )
        )
    )


# ------------------------------------------------------------- 候补队列
def _process_waitlist(
    db: Session,
    region: str,
    channel: str,
    now: datetime | None = None,
    *,
    allow_fulfill: bool = True,
) -> int:
    """在通道咨询锁内按受理顺序处理本通道候补队列，返回本轮成交笔数。

    ``allow_fulfill=False``（通道冻结期间）：仍先把已过申请有效期的
    待成交候补标记为 ``expired``（冻结不延长候补有效期、到期即失效），
    但**不做任何成交/失败判定**——冻结期间释放的额度不得使候补成交；
    仍有效的候补齐备留在队首，恢复事务先按原受理顺序统一处理。

    处理开始先统一标记本通道已过申请有效期（``expires_at <= now``，
    左闭右开）的待成交候补为 ``expired``（记录失效时刻与
    ``ttl_expired`` 原因），它们不占额度、随即释放队列位置；随后每轮
    只看队首（``id`` 最小的仍有效 pending 候补）：

    - 队首**可整体承接**：原子占额并转为一笔 approved 申请
      （``fulfilled``），继续处理下一队首；
    - 队首**授权已不再匹配**（任一项无匹配授权或匹配授权均已停用）：
      标记 ``failed`` 并记录失败原因，继续处理后续候补；
    - 队首**仍匹配但额度不足**：停止本轮，队首留在队列等待下次释放
      （严格按受理顺序，不跳过队首看后续候补）。
    """
    now = now or utcnow()
    # 先统一失效已过申请有效期的候补（队首优先、按原顺序释放位置）。
    # 冻结期间同样执行：候补仍按原有效期结算，冻结不延长有效期。
    _expire_due_waitlists(db, region, channel, now)
    if not allow_fulfill:
        return 0

    fulfilled = 0
    while True:
        head = db.scalar(
            select(models.Waitlist)
            .where(
                models.Waitlist.region == region,
                models.Waitlist.channel == channel,
                models.Waitlist.status == "pending",
            )
            .order_by(models.Waitlist.id.asc())
            .limit(1)
            .with_for_update()
        )
        if head is None:
            return fulfilled

        # 处理队首：已过申请有效期则先失效并释放位置，再按原顺序尝试
        # 后续候补（左闭右开：expires_at == now 即过期）。
        if head.expires_at is not None and head.expires_at <= now:
            head.status = "expired"
            head.expiry_reason = WAITLIST_EXPIRY_TTL
            head.expired_at = now
            db.flush()
            continue

        material_ids = list(
            db.scalars(
                select(models.WaitlistItem.material_id)
                .where(models.WaitlistItem.waitlist_id == head.id)
                .order_by(models.WaitlistItem.item_index.asc())
            )
        )
        candidate_ids = _candidate_auth_ids(
            db, material_ids, region, channel, head.occur_at
        )
        locked_rows = _lock_authorizations(db, candidate_ids)
        chosen, reasons = _evaluate(
            material_ids,
            locked_rows,
            region=region,
            channel=channel,
            occur=head.occur_at,
            for_reservation=False,
        )

        if reasons:
            blocking = next(
                (r for r in reasons if r["reason"] != "quota_exhausted"),
                None,
            )
            if blocking is not None:
                # 授权已不再匹配：终态失败，继续处理后续候补。
                head.status = "failed"
                head.failure_reason = blocking["reason"]
                head.failed_at = utcnow()
                db.flush()
                continue
            # 额度仍不足：停止本轮。
            return fulfilled

        # 整体可承接：条件占额（护栏断言），生成一笔 approved 申请。
        for auth in chosen.values():
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth.id,
                    models.Authorization.used_count
                    + models.Authorization.reserved_count
                    < models.Authorization.max_count,
                )
                .values(used_count=models.Authorization.used_count + 1)
            )
            if result.rowcount != 1:
                db.rollback()
                raise RuntimeError(
                    f"候补成交失败：授权 {auth.id} 余量不足"
                )

        distribution = models.Distribution(
            region=region,
            channel=channel,
            occur_at=head.occur_at,
            status="approved",
        )
        db.add(distribution)
        db.flush()
        for index, mid in enumerate(material_ids):
            db.add(
                models.DistributionItem(
                    distribution_id=distribution.id,
                    material_id=mid,
                    authorization_id=chosen[mid].id,
                    item_index=index,
                )
            )
        head.status = "fulfilled"
        head.distribution_id = distribution.id
        head.fulfilled_at = utcnow()
        db.flush()
        fulfilled += 1


# ------------------------------------------------------- 发行核验与占用
def create_distribution(
    db: Session, data: DistributionCreate
) -> models.Distribution:
    # 1) 重复素材 / 未知编号（与预留同一套校验）。
    _validate_material_ids(db, list(data.material_ids))
    material_ids = list(data.material_ids)

    try:
        occur = data.occur_at
        # 2) 通道咨询锁：同 region+channel 的提交/预留/确认/取消/撤销/改挂
        #    全部串行判定，第一加锁点，杜绝跨事务死锁。
        _channel_lock(db, data.region, data.channel)
        now = utcnow()

        # 2b) 通道冻结期间，新的直接发行明确拒绝（不占额）。
        freeze_row = _channel_is_frozen(db, data.region, data.channel)
        if freeze_row is not None:
            db.rollback()
            raise ChannelFrozen(
                data.region, data.channel, freeze_row.reason
            )

        # 3) 预读候选授权 → 与本通道到期预留涉及授权合并后按 id 升序锁定，
        #    同时完成到期结算（到期预留立即释放、参与本次余量判定）。
        candidate_ids = _candidate_auth_ids(
            db, material_ids, data.region, data.channel, occur
        )
        locked_rows = _settle_expired(
            db,
            now,
            region=data.region,
            channel=data.channel,
            extra_auth_ids=candidate_ids,
        )

        # 3b) 新直接申请须先处理可成交候补，不得抢走其额度：按受理顺序
        #     重算队首后再做本次判定；候补成交消耗了额度时重读最新计数。
        if _process_waitlist(db, data.region, data.channel, now):
            locked_rows = _lock_authorizations(db, candidate_ids)

        # 4) 逐项判定（未到期预留参与余量：used + reserved < max）。
        chosen, reasons = _evaluate(
            material_ids,
            locked_rows,
            region=data.region,
            channel=data.channel,
            occur=occur,
            for_reservation=False,
        )
        if reasons:
            db.rollback()
            raise DistributionRejected(reasons)

        # 5) 条件占用正式次数（含预留占用护栏），断言影响行数。
        for auth in chosen.values():
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth.id,
                    models.Authorization.used_count
                    + models.Authorization.reserved_count
                    < models.Authorization.max_count,
                )
                .values(used_count=models.Authorization.used_count + 1)
            )
            if result.rowcount != 1:
                db.rollback()
                raise DistributionRejected(
                    [
                        {
                            "material_id": auth.material_id,
                            "reason": "quota_exhausted",
                            "message": (
                                f"授权 id={auth.id} 余量不足，"
                                "并发占用冲突，本次申请整体拒绝"
                            ),
                        }
                    ]
                )

        # 6) 落库申请及逐项占用记录。
        distribution = models.Distribution(
            region=data.region,
            channel=data.channel,
            occur_at=data.occur_at,
            status="approved",
        )
        db.add(distribution)
        db.flush()
        for index, mid in enumerate(material_ids):
            db.add(
                models.DistributionItem(
                    distribution_id=distribution.id,
                    material_id=mid,
                    authorization_id=chosen[mid].id,
                    item_index=index,
                )
            )
        db.commit()
    except (ChannelFrozen, DistributionRejected):
        raise
    except Exception:
        db.rollback()
        raise

    return get_distribution(db, distribution_id=distribution.id)


# ------------------------------------------------------------- 查询/撤销
def get_distribution(db: Session, distribution_id: int) -> models.Distribution:
    distribution = db.get(
        models.Distribution,
        distribution_id,
        options=[
            selectinload(models.Distribution.items).selectinload(
                models.DistributionItem.material
            ),
            selectinload(models.Distribution.items).selectinload(
                models.DistributionItem.authorization
            ),
        ],
    )
    if distribution is None:
        raise ResourceNotFound("发行申请", distribution_id)
    return distribution


def revoke_distribution(
    db: Session, distribution_id: int
) -> models.Distribution:
    try:
        # 先读取申请的通道维度（不加锁），以便第一步即获取通道咨询锁。
        head = db.execute(
            select(
                models.Distribution.region,
                models.Distribution.channel,
                models.Distribution.status,
            ).where(models.Distribution.id == distribution_id)
        ).first()
        if head is None:
            raise ResourceNotFound("发行申请", distribution_id)
        region, channel, _status = head

        # 第一加锁点：该申请所在通道的咨询锁。与进行中的提交/预留/确认/
        # 取消/改挂事务互斥，同时杜绝跨事务死锁（一律先取咨询锁）。
        _channel_lock(db, region, channel)
        # 撤销前先结算本通道到期预留（归还 reserved_count），使通道总额度
        # 视图一致；撤销本身只释放正式占用 used_count。冻结期间到期预留
        # 照常结算，但结算与本次撤销释放的额度均不得使候补成交。
        frozen_row = _channel_is_frozen(db, region, channel)
        _settle_expired(
            db,
            utcnow(),
            region=region,
            channel=channel,
            frozen=frozen_row is not None,
        )

        # 再锁定申请行，串行化对同一申请的并发撤销，复查状态，
        # 保证后续判定基于最新已提交数据（并发双重撤销恰为一次
        # 200、一次 409）。
        distribution = db.scalar(
            select(models.Distribution)
            .where(models.Distribution.id == distribution_id)
            .with_for_update()
        )
        if distribution.status == "revoked":
            raise AlreadyRevoked(distribution_id)

        # 锁定本申请的全部占用明细行：与改挂事务的明细归属更新互斥，
        # 固定「明细 → 授权」的当前归属。
        items = list(
            db.scalars(
                select(models.DistributionItem)
                .where(
                    models.DistributionItem.distribution_id
                    == distribution_id
                )
                .order_by(models.DistributionItem.id.asc())
                .with_for_update()
            )
        )
        auth_ids = sorted({item.authorization_id for item in items})

        # 按授权 id 升序加锁，与核验/改挂/预留事务保持同一行锁顺序。
        if auth_ids:
            db.execute(
                select(models.Authorization.id)
                .where(models.Authorization.id.in_(auth_ids))
                .order_by(models.Authorization.id.asc())
                .with_for_update()
            )

        # 同一授权在本申请中至多出现一次（素材已去重），逐项释放。
        # 条件 ``used_count >= 1`` 作为不重复释放/账实相符的最后防线。
        for auth_id in auth_ids:
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth_id,
                    models.Authorization.used_count >= 1,
                )
                .values(
                    used_count=models.Authorization.used_count - 1
                )
            )
            if result.rowcount != 1:
                db.rollback()
                raise RuntimeError(
                    f"撤销释放失败：授权 {auth_id} used_count 异常"
                )

        distribution.status = "revoked"
        distribution.revoked_at = datetime.now(timezone.utc)
        db.flush()

        # 撤销释放额度后，按受理顺序重算本通道候补队首；冻结期间仅结算
        # 候补到期失效，不成交（释放的额度保留至恢复时，恢复事务先按
        # 原顺序处理仍有效候补）。
        _process_waitlist(
            db, region, channel, allow_fulfill=frozen_row is None
        )
        db.commit()
    except (ResourceNotFound, AlreadyRevoked):
        raise
    except Exception:
        db.rollback()
        raise

    return get_distribution(db, distribution_id=distribution_id)


# --------------------------------------------------------- 发行申请改期
def reschedule_distribution(
    db: Session, distribution_id: int, data: DistributionReschedule
) -> models.Distribution:
    """运营改期：以新的带时区发行时刻调整一笔已通过申请的档期。

    - 地区、渠道、素材清单不变，仅更换 ``occur_at``；
    - 新时刻与当前时刻为**同一瞬间**（仅时区表示不同也算）：幂等返回
      原申请，不变更任何次数；
    - 否则按既有左闭右开匹配与重叠授权顺序（到期早优先、其次编号小）
      逐项重选授权；余量判定**扣除本申请原占用**（该授权
      ``used_count`` 先减回本申请占的 1 次），**仍计入未到期预留**，且
      目标授权必须启用；
    - 全部项可承接才原子更新申请时刻、逐项明细归属与相关授权次数
      （归属不变的授权次数不动；归属变化的「原授权 -1、新授权 +1」），
      返回更新后的申请；任一项失败整体拒绝并逐项列明原因，原记录与
      次数均不变；
    - 已撤销申请拒绝改期（409）；由确认预留生成的申请同样可改期，
      预留原始时刻与确认记录保留，预留查询关联申请展示最新数据。
    """
    # 不加锁读取通道维度（不可变），仅用于确定咨询锁桶。
    head = db.execute(
        select(
            models.Distribution.region,
            models.Distribution.channel,
        ).where(models.Distribution.id == distribution_id)
    ).first()
    if head is None:
        raise ResourceNotFound("发行申请", distribution_id)
    region, channel = head

    try:
        # 第一加锁点：通道咨询锁。与提交/预留/确认/取消/撤销/改挂互斥。
        _channel_lock(db, region, channel)
        frozen_row = _channel_is_frozen(db, region, channel)

        # 锁定申请行，复查状态：已撤销申请拒绝改期（即使新时刻为同一瞬间）。
        distribution = db.scalar(
            select(models.Distribution)
            .where(models.Distribution.id == distribution_id)
            .with_for_update()
        )
        if distribution.status == "revoked":
            raise DistributionRevoked(distribution_id)

        # 同一瞬间：幂等空操作，返回原申请且不变动任何次数。
        if data.occur_at == distribution.occur_at:
            db.rollback()
            return get_distribution(db, distribution_id)

        # 锁定本申请明细行（与撤销/改挂互斥），确定素材清单与原归属授权。
        items = list(
            db.scalars(
                select(models.DistributionItem)
                .where(
                    models.DistributionItem.distribution_id
                    == distribution_id
                )
                .order_by(models.DistributionItem.item_index.asc())
                .with_for_update()
            )
        )
        material_ids = [item.material_id for item in items]
        own_auth_ids = {item.authorization_id for item in items}

        # 新时刻候选授权与原归属授权合并，随本通道到期结算一次性按 id
        # 升序行锁（到期预留立即释放并参与本次余量判定；冻结期间到期
        # 预留照常结算，但不触发候补成交）。
        candidate_ids = _candidate_auth_ids(
            db, material_ids, region, channel, data.occur_at
        )
        locked_rows = _settle_expired(
            db,
            utcnow(),
            region=region,
            channel=channel,
            extra_auth_ids=sorted(set(candidate_ids) | own_auth_ids),
            frozen=frozen_row is not None,
        )

        # 逐项重选：余量扣除本申请原占用，仍计入未到期预留。
        chosen, reasons = _evaluate(
            material_ids,
            locked_rows,
            region=region,
            channel=channel,
            occur=data.occur_at,
            for_reservation=False,
            used_discount={auth_id: 1 for auth_id in own_auth_ids},
        )
        if reasons:
            db.rollback()
            raise RescheduleRejected(reasons)

        # 原子搬移：归属变化的项逐条「原授权条件 -1、新授权条件 +1」，
        # 归属不变的项不动次数。素材在申请内唯一 ⇒ 每个授权在一次改期中
        # 至多作为一端出现一次，不会既加又减。
        for item in items:
            new_auth = chosen[item.material_id]
            old_auth_id = item.authorization_id
            if new_auth.id == old_auth_id:
                continue
            dec = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == old_auth_id,
                    models.Authorization.used_count >= 1,
                )
                .values(used_count=models.Authorization.used_count - 1)
            )
            if dec.rowcount != 1:
                db.rollback()
                raise RuntimeError(
                    f"改期迁出失败：授权 {old_auth_id} used_count 异常"
                )
            inc = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == new_auth.id,
                    models.Authorization.used_count
                    + models.Authorization.reserved_count
                    < models.Authorization.max_count,
                )
                .values(used_count=models.Authorization.used_count + 1)
            )
            if inc.rowcount != 1:
                db.rollback()
                raise RuntimeError(
                    f"改期迁入失败：授权 {new_auth.id} 余量不足"
                )
            item.authorization_id = new_auth.id

        distribution.occur_at = data.occur_at
        db.flush()

        # 改期搬移可能释放了原授权额度：按受理顺序重算本通道候补队首；
        # 冻结期间仅结算候补到期失效，不成交。
        _process_waitlist(
            db, region, channel, allow_fulfill=frozen_row is None
        )
        db.commit()
    except (ResourceNotFound, DistributionRevoked, RescheduleRejected):
        raise
    except Exception:
        db.rollback()
        raise

    return get_distribution(db, distribution_id=distribution_id)


# =============================================================== 额度预留
def create_reservation(db: Session, data: ReservationCreate) -> models.Reservation:
    """全部素材同时通过才生成预留编号并逐项占用预留额度；任一失败整体拒绝。"""
    material_ids = list(data.material_ids)
    _validate_material_ids(db, material_ids)

    now = utcnow()
    expires_at = now + timedelta(minutes=data.ttl_minutes)

    try:
        _channel_lock(db, data.region, data.channel)

        # 通道冻结期间，新的额度预留明确拒绝（不占额）。
        freeze_row = _channel_is_frozen(db, data.region, data.channel)
        if freeze_row is not None:
            db.rollback()
            raise ChannelFrozen(data.region, data.channel, freeze_row.reason)

        candidate_ids = _candidate_auth_ids(
            db, material_ids, data.region, data.channel, data.occur_at
        )
        locked_rows = _settle_expired(
            db,
            now,
            region=data.region,
            channel=data.channel,
            extra_auth_ids=candidate_ids,
        )

        # 沿用既有授权匹配与重叠选择顺序；未到期预留同样占额。
        chosen, reasons = _evaluate(
            material_ids,
            locked_rows,
            region=data.region,
            channel=data.channel,
            occur=data.occur_at,
            for_reservation=True,
        )
        if reasons:
            db.rollback()
            raise ReservationRejected(reasons)

        reservation = models.Reservation(
            region=data.region,
            channel=data.channel,
            occur_at=data.occur_at,
            expires_at=expires_at,
            ttl_minutes=data.ttl_minutes,
            status="pending",
        )
        db.add(reservation)
        db.flush()

        # 逐项条件预留：used + reserved < max 护栏，断言影响行数。
        # 素材已去重，同一授权在一次预留中至多被选中一次。
        for index, mid in enumerate(material_ids):
            auth = chosen[mid]
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth.id,
                    models.Authorization.used_count
                    + models.Authorization.reserved_count
                    < models.Authorization.max_count,
                )
                .values(
                    reserved_count=models.Authorization.reserved_count + 1
                )
            )
            if result.rowcount != 1:
                db.rollback()
                raise ReservationRejected(
                    [
                        {
                            "item_index": index,
                            "material_id": mid,
                            "reason": "quota_exhausted",
                            "message": (
                                f"授权 id={auth.id} 可预留余量不足，"
                                "并发预留冲突，本次预留整体拒绝"
                            ),
                        }
                    ]
                )
            db.add(
                models.ReservationItem(
                    reservation_id=reservation.id,
                    material_id=mid,
                    authorization_id=auth.id,
                    item_index=index,
                )
            )
        db.commit()
    except (ChannelFrozen, ReservationRejected):
        raise
    except Exception:
        db.rollback()
        raise

    return _load_reservation(db, reservation.id)


def list_reservations(
    db: Session, status: str | None
) -> list[models.Reservation]:
    """查询预留列表，可按状态过滤。

    列表为只读视图：库内仍 pending 但已过到期时刻的预留按 **expired**
    呈现（写路径与单条查询会惰性结算落库；此处仅在响应中派生状态，
    不产生写事务）。
    """
    if status is not None and status not in (
        "pending",
        "confirmed",
        "cancelled",
        "expired",
    ):
        raise InvalidReservationStatus(status)

    now = utcnow()
    rows = list(
        db.scalars(
            select(models.Reservation)
            .options(
                selectinload(models.Reservation.items).selectinload(
                    models.ReservationItem.material
                ),
                selectinload(models.Reservation.items).selectinload(
                    models.ReservationItem.authorization
                ),
                selectinload(models.Reservation.distribution)
                .selectinload(models.Distribution.items)
                .selectinload(models.DistributionItem.material),
                selectinload(models.Reservation.distribution)
                .selectinload(models.Distribution.items)
                .selectinload(models.DistributionItem.authorization),
            )
            .order_by(models.Reservation.id.asc())
        )
    )

    result: list[models.Reservation] = []
    for resv in rows:
        effective = resv.status
        if resv.status == "pending" and resv.expires_at <= now:
            effective = "expired"
        if status is None or effective == status:
            # 分离后覆盖派生状态，避免把瞬时状态标记为脏对象。
            db.expunge(resv)
            object.__setattr__(resv, "status", effective)
            result.append(resv)
    return result


def _load_reservation(
    db: Session, reservation_id: int, *, for_update: bool = False
) -> models.Reservation:
    # 确认事务内把同事务新建的 distribution 赋给本预留后，关系缓存可能为
    # None；一律使会话已加载状态过期，保证下面 selectinload 真正载入新关系。
    db.expire_all()
    stmt = (
        select(models.Reservation)
        .where(models.Reservation.id == reservation_id)
        .options(
            selectinload(models.Reservation.items).selectinload(
                models.ReservationItem.material
            ),
            selectinload(models.Reservation.items).selectinload(
                models.ReservationItem.authorization
            ),
            selectinload(models.Reservation.distribution)
            .selectinload(models.Distribution.items)
            .selectinload(models.DistributionItem.material),
            selectinload(models.Reservation.distribution)
            .selectinload(models.Distribution.items)
            .selectinload(models.DistributionItem.authorization),
        )
    )
    if for_update:
        stmt = stmt.with_for_update()
    resv = db.scalar(stmt)
    if resv is None:
        raise ResourceNotFound("预留", reservation_id)
    return resv


def get_reservation(db: Session, reservation_id: int) -> models.Reservation:
    """查询预留；先在通道内结算到期预留，逾期者落库为 expired。"""
    head = db.execute(
        select(
            models.Reservation.region,
            models.Reservation.channel,
        ).where(models.Reservation.id == reservation_id)
    ).first()
    if head is None:
        raise ResourceNotFound("预留", reservation_id)
    region, channel = head
    try:
        _channel_lock(db, region, channel)
        frozen_row = _channel_is_frozen(db, region, channel)
        _settle_expired(
            db,
            utcnow(),
            region=region,
            channel=channel,
            frozen=frozen_row is not None,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _load_reservation(db, reservation_id)


def confirm_reservation(
    db: Session, reservation_id: int
) -> models.Reservation:
    """到期前确认：把预留原子转为一笔 approved 申请。

    - 重复确认返回同一笔申请（幂等，200）；
    - 已取消 / 已过期（含恰在到期时刻确认，视为逾期）→ 409；
    - 授权停用不影响确认：确认不重新匹配授权、不要求启用，只把预留槽位
      转为同一授权上的正式占用。
    """
    head = db.execute(
        select(
            models.Reservation.region,
            models.Reservation.channel,
        ).where(models.Reservation.id == reservation_id)
    ).first()
    if head is None:
        raise ResourceNotFound("预留", reservation_id)
    region, channel = head

    try:
        _channel_lock(db, region, channel)
        frozen_row = _channel_is_frozen(db, region, channel)

        # 本预留涉及的授权行必须纳入统一行锁集合（条件转账需要）。
        item_auth_ids = list(
            db.scalars(
                select(models.ReservationItem.authorization_id)
                .join(
                    models.Reservation,
                    models.ReservationItem.reservation_id
                    == models.Reservation.id,
                )
                .where(models.Reservation.id == reservation_id)
            )
        )
        _settle_expired(
            db,
            utcnow(),
            region=region,
            channel=channel,
            extra_auth_ids=item_auth_ids,
            frozen=frozen_row is not None,
        )

        resv = _load_reservation(db, reservation_id, for_update=True)

        if resv.status == "confirmed":
            # 幂等：重复确认返回同一申请，不重复占用。
            db.rollback()
            return _load_reservation(db, reservation_id)
        if resv.status == "cancelled":
            db.rollback()
            raise ReservationCancelled(reservation_id)
        if resv.status == "expired":
            db.rollback()
            raise ReservationExpired(reservation_id)

        # pending：通道结算已保证 now < expires_at（否则已置 expired）。
        now = utcnow()
        auth_ids = sorted(
            {item.authorization_id for item in resv.items}
        )

        # 生成一笔 approved 申请及逐项明细，归属预留时命中的同一授权。
        distribution = models.Distribution(
            region=resv.region,
            channel=resv.channel,
            occur_at=resv.occur_at,
            status="approved",
        )
        db.add(distribution)
        db.flush()
        for item in resv.items:
            db.add(
                models.DistributionItem(
                    distribution_id=distribution.id,
                    material_id=item.material_id,
                    authorization_id=item.authorization_id,
                    item_index=item.item_index,
                )
            )

        # 原子转账：reserved_count - 1（条件 >=1）紧接 used_count + 1
        # （条件 used + reserved < max，即槽位确实来自本预留），
        # 占用总量不变，绝不超额、不重复。
        for auth_id in auth_ids:
            dec = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth_id,
                    models.Authorization.reserved_count >= 1,
                )
                .values(
                    reserved_count=models.Authorization.reserved_count - 1
                )
            )
            if dec.rowcount != 1:
                db.rollback()
                raise RuntimeError(
                    f"确认预留失败：授权 {auth_id} reserved_count 异常"
                )
            inc = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth_id,
                    models.Authorization.used_count
                    + models.Authorization.reserved_count
                    < models.Authorization.max_count,
                )
                .values(used_count=models.Authorization.used_count + 1)
            )
            if inc.rowcount != 1:
                db.rollback()
                raise RuntimeError(
                    f"确认预留失败：授权 {auth_id} 正式余量不足"
                )

        resv.status = "confirmed"
        resv.distribution_id = distribution.id
        resv.confirmed_at = now
        db.commit()
    except (
        ResourceNotFound,
        ReservationCancelled,
        ReservationExpired,
    ):
        raise
    except Exception:
        db.rollback()
        raise

    return _load_reservation(db, reservation_id)


def cancel_reservation(db: Session, reservation_id: int) -> models.Reservation:
    """取消预留（与确认互斥）：逐项释放预留额度，仅生效一次。

    - 重复取消返回 409，不再释放额度；
    - 已确认 → 409（不可取消）；已过期 → 409（额度已由到期结算释放）。
    """
    head = db.execute(
        select(
            models.Reservation.region,
            models.Reservation.channel,
        ).where(models.Reservation.id == reservation_id)
    ).first()
    if head is None:
        raise ResourceNotFound("预留", reservation_id)
    region, channel = head

    try:
        _channel_lock(db, region, channel)
        frozen_row = _channel_is_frozen(db, region, channel)

        item_auth_ids = list(
            db.scalars(
                select(models.ReservationItem.authorization_id)
                .join(
                    models.Reservation,
                    models.ReservationItem.reservation_id
                    == models.Reservation.id,
                )
                .where(models.Reservation.id == reservation_id)
            )
        )
        _settle_expired(
            db,
            utcnow(),
            region=region,
            channel=channel,
            extra_auth_ids=item_auth_ids,
            frozen=frozen_row is not None,
        )

        resv = _load_reservation(db, reservation_id, for_update=True)

        if resv.status == "cancelled":
            db.rollback()
            raise ReservationAlreadyCancelled(reservation_id)
        if resv.status == "confirmed":
            db.rollback()
            raise ReservationAlreadyConfirmed(reservation_id)
        if resv.status == "expired":
            db.rollback()
            raise ReservationExpired(reservation_id, action="取消")

        auth_ids = sorted(
            {item.authorization_id for item in resv.items}
        )
        for auth_id in auth_ids:
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth_id,
                    models.Authorization.reserved_count >= 1,
                )
                .values(
                    reserved_count=models.Authorization.reserved_count - 1
                )
            )
            if result.rowcount != 1:
                db.rollback()
                raise RuntimeError(
                    f"取消预留失败：授权 {auth_id} reserved_count 异常"
                )

        resv.status = "cancelled"
        resv.cancelled_at = utcnow()
        db.flush()

        # 取消预留释放额度后，按受理顺序重算本通道候补队首；冻结期间
        # 仅结算候补到期失效，不成交（恢复时再按原顺序统一处理）。
        _process_waitlist(
            db, region, channel, allow_fulfill=frozen_row is None
        )
        db.commit()
    except (
        ResourceNotFound,
        ReservationAlreadyCancelled,
        ReservationAlreadyConfirmed,
        ReservationExpired,
    ):
        raise
    except Exception:
        db.rollback()
        raise

    return _load_reservation(db, reservation_id)


# --------------------------------------------------------- 授权退役改挂
def migrate_authorization(
    db: Session, data: AuthorizationMigrationRequest
) -> dict:
    """原授权退役：把其承载的全部 **未撤销** 申请明细一次性改挂到替代授权。

    前置条件（任一不满足则整体拒绝，申请归属与两端次数均不变）：

    - 两端编号不同且授权均存在；
    - 素材、地区、渠道三个维度完全一致；
    - 替代授权为启用状态（active）；
    - **原授权不存在未到期的待确认预留**（预留仍指向原授权，停用会使
      确认语义与余量账目不清晰，故整体拒绝，原因 ``pending_reservations``；
      待预留确认/取消/到期后再改挂即可）；
    - 替代授权时段左闭右开覆盖每条待迁移申请的 ``occur_at``；
    - 替代授权余量（扣除其自身未到期预留后）足够一次性承接全部迁入占用。

    并发：单事务内「咨询锁 → 到期结算 → 授权行锁 → 申请行锁 → 明细行锁」，
    与提交/预留/确认/取消/撤销遵循同一加锁顺序；次数搬移全部使用条件更新
    并断言影响行数，绝不超额或重复释放。
    """
    source_id = data.source_authorization_id
    target_id = data.replacement_authorization_id

    if source_id == target_id:
        raise MigrationRejected(
            [
                {
                    "reason": "same_authorization",
                    "message": "原授权与替代授权必须为不同编号",
                    "source_authorization_id": source_id,
                    "replacement_authorization_id": target_id,
                }
            ]
        )

    def _reject(reason: str, message: str) -> MigrationRejected:
        return MigrationRejected(
            [
                {
                    "reason": reason,
                    "message": message,
                    "source_authorization_id": source_id,
                    "replacement_authorization_id": target_id,
                }
            ]
        )

    # 不加锁读取，仅用于存在性/维度判断与确定咨询锁桶；
    # 维度（material_id/region/channel）不可变，无需锁保护。
    source = db.get(models.Authorization, source_id)
    if source is None:
        raise ResourceNotFound("授权", source_id)
    target = db.get(models.Authorization, target_id)
    if target is None:
        raise ResourceNotFound("授权", target_id)

    try:
        # 1) 维度校验先行，不一致直接拒绝，不触碰任何锁。
        if source.region != target.region or source.channel != target.channel:
            raise _reject(
                "dimension_mismatch",
                "原授权与替代授权的地区、渠道不一致，不可改挂",
            )
        if source.material_id != target.material_id:
            raise _reject(
                "dimension_mismatch",
                "原授权与替代授权的素材不一致，不可改挂",
            )

        # 2) 第一加锁点：通道咨询锁；随后立即结算本通道到期预留
        #    （归还 reserved_count），再锁定两端授权行。冻结期间改挂仍
        #    允许，但到期结算与迁出释放的额度均不得使候补成交。
        _channel_lock(db, source.region, source.channel)
        frozen_row = _channel_is_frozen(db, source.region, source.channel)
        locked_rows = _settle_expired(
            db,
            utcnow(),
            region=source.region,
            channel=source.channel,
            extra_auth_ids=[source_id, target_id],
            frozen=frozen_row is not None,
        )
        by_id = {a.id: a for a in locked_rows}
        source = by_id.get(source_id, source)
        target = by_id.get(target_id, target)

        # 3) 替代授权必须启用。
        if target.status != "active":
            raise _reject(
                "replacement_inactive",
                f"替代授权（id={target_id}）未处于启用状态",
            )

        # 4) 原授权存在未到期（待确认）预留时整体拒绝。
        pending_resv_ids = list(
            db.scalars(
                select(models.Reservation.id)
                .join(
                    models.ReservationItem,
                    models.ReservationItem.reservation_id
                    == models.Reservation.id,
                )
                .where(
                    models.Reservation.status == "pending",
                    models.ReservationItem.authorization_id == source_id,
                )
            )
        )
        if pending_resv_ids:
            raise _reject(
                "pending_reservations",
                f"原授权（id={source_id}）存在 {len(pending_resv_ids)} 条"
                "未到期的待确认预留，请待其确认/取消/到期后再改挂",
            )

        # 5) 锁定原授权承载的全部未撤销申请的申请行（id 升序）。
        dist_rows = list(
            db.scalars(
                select(models.Distribution)
                .join(
                    models.DistributionItem,
                    models.DistributionItem.distribution_id
                    == models.Distribution.id,
                )
                .where(
                    models.DistributionItem.authorization_id == source_id,
                    models.Distribution.status == "approved",
                )
                .order_by(models.Distribution.id.asc())
                .with_for_update()
            )
        )
        dist_ids = [d.id for d in dist_rows]

        # 6) 锁定这些申请下归属原授权的明细行（FOR UPDATE）。
        items: list[models.DistributionItem] = []
        occur_times: list[datetime] = []
        if dist_ids:
            items = list(
                db.scalars(
                    select(models.DistributionItem)
                    .where(
                        models.DistributionItem.distribution_id.in_(
                            dist_ids
                        ),
                        models.DistributionItem.authorization_id
                        == source_id,
                    )
                    .order_by(models.DistributionItem.id.asc())
                    .with_for_update()
                )
            )
            occur_map = {d.id: d.occur_at for d in dist_rows}
            occur_times = [occur_map[it.distribution_id] for it in items]

        n = len(items)

        # 7) 逐条校验替代授权时段覆盖发行时刻（左闭右开）。
        uncovered = sorted(
            {
                it.id
                for it, occur_at in zip(items, occur_times)
                if not (target.starts_at <= occur_at < target.ends_at)
            }
        )
        if uncovered:
            raise _reject(
                "period_not_covered",
                f"替代授权（id={target_id}）时段未覆盖 {len(uncovered)} 条"
                "申请的发行时刻（需满足 starts_at <= occur_at < ends_at）",
            )

        # 8) 余量必须足够一次性承接全部迁入占用（扣除替代授权自身的
        #    未到期预留，预留与正式占用共享总额度）。
        if (
            target.used_count + target.reserved_count + n
            > target.max_count
        ):
            raise _reject(
                "insufficient_quota",
                f"替代授权（id={target_id}）余量不足：待迁入 {n} 条，"
                f"正式占用 {target.used_count}、未到期预留 "
                f"{target.reserved_count}/{target.max_count}",
            )

        # 9) 一次性搬移：明细归属改替代授权；原授权条件减 n；
        #    替代授权条件加 n（含预留护栏）；原授权停用。
        if n:
            moved = db.execute(
                update(models.DistributionItem)
                .where(
                    models.DistributionItem.id.in_([it.id for it in items]),
                    models.DistributionItem.authorization_id == source_id,
                )
                .values(authorization_id=target_id)
            )
            if moved.rowcount != n:
                db.rollback()
                raise RuntimeError(
                    f"改挂明细数异常：期望 {n}，实际 {moved.rowcount}"
                )

            dec = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == source_id,
                    models.Authorization.used_count >= n,
                )
                .values(
                    used_count=models.Authorization.used_count - n
                )
            )
            if dec.rowcount != 1:
                db.rollback()
                raise RuntimeError(
                    f"原授权 {source_id} 迁出次数异常（used_count < {n}）"
                )

            inc = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == target_id,
                    models.Authorization.used_count
                    + models.Authorization.reserved_count
                    <= models.Authorization.max_count - n,
                )
                .values(
                    used_count=models.Authorization.used_count + n
                )
            )
            if inc.rowcount != 1:
                db.rollback()
                raise RuntimeError(
                    f"替代授权 {target_id} 余量不足，迁入 {n} 条失败"
                )

        source.status = "inactive"
        db.flush()

        # 改挂后原授权停用、占用迁出：按受理顺序重算本通道候补队首
        # （仍匹配但额度释放的候补可成交；匹配授权已停用的候补标记失败）。
        # 冻结期间仅结算候补到期失效、不成交：释放的额度保留至恢复，
        # 恢复时仍有效的候补按原受理顺序优先处理（授权已不再匹配者
        # 恢复时标记 failed）。
        _process_waitlist(
            db,
            source.region,
            source.channel,
            allow_fulfill=frozen_row is None,
        )
        db.commit()
    except MigrationRejected:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    db.refresh(source)
    db.refresh(target)
    return {
        "migrated_count": n,
        "source_authorization": source,
        "replacement_authorization": target,
    }


# --------------------------------------------------------- 授权间额度转拨
def _transfer_snapshot(
    authorization_id: int,
    max_count: int,
    used_count: int,
    reserved_count: int,
) -> dict:
    """一端授权的额度与占用快照（含派生余量）。"""
    return {
        "authorization_id": authorization_id,
        "max_count": max_count,
        "used_count": used_count,
        "reserved_count": reserved_count,
        "remaining": max_count - used_count,
        "reservable_remaining": max_count - used_count - reserved_count,
    }


def _transfer_out(record: models.QuotaTransfer) -> dict:
    """由转拨记录构造响应：操作号、转拨次数与两端转拨后快照。

    重放（相同操作号 + 相同参数）原样返回首次落库的快照，不随后续
    冻结/停用/新业务变化。
    """
    return {
        "operation_no": record.operation_no,
        "count": record.count,
        "created_at": record.created_at,
        "source_authorization": _transfer_snapshot(
            record.source_authorization_id,
            record.source_max_count,
            record.source_used_count,
            record.source_reserved_count,
        ),
        "target_authorization": _transfer_snapshot(
            record.target_authorization_id,
            record.target_max_count,
            record.target_used_count,
            record.target_reserved_count,
        ),
    }


def transfer_quota(db: Session, data: QuotaTransferCreate) -> dict:
    """同一素材/地区/渠道下，把源授权未占用的可发行次数转拨到目标授权。

    - 请求携带操作号 ``operation_no``（幂等键，全局唯一）：相同操作号 +
      相同参数的重复请求返回首次转拨结果（重放不受随后的通道冻结或授权
      停用影响）；相同操作号 + 不同参数返回 409
      ``quota_transfer_conflict``；
    - 仅两端授权均启用且通道未冻结时受理；两端授权时段可以不同；
      既有申请与预留的归属不变，仅原子调整两端 ``max_count``；
    - 受理后先在通道锁内结算本通道到期预留并按原顺序处理候补，再以
      源授权 ``max_count - used_count - reserved_count`` 判定可转出量；
      不足、转出后源额度非正（``max_count - count < 1``）、授权不存在
      或维度不符（素材/地区/渠道）则整笔拒绝，两端额度均不变；
    - 成功后在同一事务内重算本通道候补：目标新增余量优先供仍有效的
      队首，过期或失配者依既有规则出队；
    - 响应与落库记录均含首次转拨完成（含同事务候补重算）后的两端
      额度与占用快照。
    """
    source_id = data.source_authorization_id
    target_id = data.target_authorization_id
    count = data.count
    operation_no = data.operation_no

    def _reject(reason: str, message: str) -> TransferRejected:
        return TransferRejected(
            [
                {
                    "reason": reason,
                    "message": message,
                    "source_authorization_id": source_id,
                    "target_authorization_id": target_id,
                }
            ]
        )

    def _replay_or_conflict(record: models.QuotaTransfer) -> dict:
        """相同操作号：参数一致 → 返回首次转拨结果；否则 409 冲突。"""
        if (
            record.source_authorization_id == source_id
            and record.target_authorization_id == target_id
            and record.count == count
        ):
            return _transfer_out(record)
        raise TransferOperationConflict(operation_no)

    # 1) 源目标相同：参数级拒绝（无需任何锁）。
    if source_id == target_id:
        raise _reject(
            "same_authorization", "源授权与目标授权必须为不同编号"
        )

    # 2) 幂等预检（不加锁）：相同操作号已落库 → 同参重放返回首次结果，
    #    异参冲突。预检先于一切业务判定，故重放不受随后的冻结/停用影响。
    existing = db.scalar(
        select(models.QuotaTransfer).where(
            models.QuotaTransfer.operation_no == operation_no
        )
    )
    if existing is not None:
        return _replay_or_conflict(existing)

    # 3) 存在性与维度校验（不加锁；授权维度不可变，无需锁保护）。
    source = db.get(models.Authorization, source_id)
    if source is None:
        raise ResourceNotFound("授权", source_id)
    target = db.get(models.Authorization, target_id)
    if target is None:
        raise ResourceNotFound("授权", target_id)
    if source.region != target.region or source.channel != target.channel:
        raise _reject(
            "dimension_mismatch",
            "源授权与目标授权的地区、渠道不一致，不可转拨",
        )
    if source.material_id != target.material_id:
        raise _reject(
            "dimension_mismatch",
            "源授权与目标授权的素材不一致，不可转拨",
        )

    region, channel = source.region, source.channel
    try:
        # 4) 第一加锁点：通道咨询锁（与本通道全部发行类写事务串行）。
        _channel_lock(db, region, channel)
        now = utcnow()

        # 4b) 锁内复查操作号：等待锁期间同号事务可能已提交。
        existing = db.scalar(
            select(models.QuotaTransfer).where(
                models.QuotaTransfer.operation_no == operation_no
            )
        )
        if existing is not None:
            db.rollback()
            return _replay_or_conflict(existing)

        # 5) 冻结闸门：通道冻结期间不受理转拨（不重算、不改额）。
        freeze_row = _channel_is_frozen(db, region, channel)
        if freeze_row is not None:
            db.rollback()
            raise ChannelFrozen(region, channel, freeze_row.reason)

        # 6) 先结算本通道到期预留并按原顺序处理候补（到期归还的预留与
        #    候补成交的占用都计入随后的可转出量判定），再锁定两端授权行。
        locked_rows = _settle_expired(
            db,
            now,
            region=region,
            channel=channel,
            extra_auth_ids=[source_id, target_id],
        )
        if _process_waitlist(db, region, channel, now):
            locked_rows = _lock_authorizations(db, [source_id, target_id])
        by_id = {a.id: a for a in locked_rows}
        source = by_id[source_id]
        target = by_id[target_id]

        # 7) 两端均须启用。
        if source.status != "active" or target.status != "active":
            raise _reject(
                "authorization_inactive",
                f"源授权（id={source_id}，{source.status}）与目标授权"
                f"（id={target_id}，{target.status}）均须处于启用状态",
            )

        # 8) 可转出量 = max_count - used_count - reserved_count；
        #    不足或转出后源额度非正 → 整笔拒绝，两端额度不变。
        transferable = (
            source.max_count - source.used_count - source.reserved_count
        )
        if count > transferable:
            raise _reject(
                "insufficient_quota",
                f"源授权（id={source_id}）可转出量不足：请求 {count} 次，"
                f"可转出 {transferable} 次（max_count {source.max_count} - "
                f"used_count {source.used_count} - "
                f"reserved_count {source.reserved_count}）",
            )
        if source.max_count - count < 1:
            raise _reject(
                "source_quota_not_positive",
                f"转出后源授权（id={source_id}）额度须保持为正："
                f"max_count {source.max_count} - {count} 应 >= 1",
            )

        # 9) 原子更新两端 max_count：条件更新 + 断言影响行数，作为
        #    绝不超额转拨的最后防线（与既有占额/释放口径一致）。
        dec = db.execute(
            update(models.Authorization)
            .where(
                models.Authorization.id == source_id,
                models.Authorization.max_count
                - models.Authorization.used_count
                - models.Authorization.reserved_count
                >= count,
                models.Authorization.max_count - count >= 1,
            )
            .values(max_count=models.Authorization.max_count - count)
        )
        if dec.rowcount != 1:
            db.rollback()
            raise RuntimeError(
                f"转拨失败：源授权 {source_id} 可转出量不足 {count}"
            )
        inc = db.execute(
            update(models.Authorization)
            .where(models.Authorization.id == target_id)
            .values(max_count=models.Authorization.max_count + count)
        )
        if inc.rowcount != 1:
            db.rollback()
            raise RuntimeError(f"转拨失败：目标授权 {target_id} 更新异常")

        # 10) 同事务重算本通道候补：目标新增余量优先供仍有效的队首，
        #     过期或失配者依既有规则出队（成交会消耗两端计数）。
        _process_waitlist(db, region, channel, now)

        # 11) 重锁两端读取转拨后（含候补重算）最新计数，落库转拨记录
        #     与两端快照；操作号唯一约束兜底并发同号（跨通道或预检间隙）。
        locked_rows = _lock_authorizations(db, [source_id, target_id])
        by_id = {a.id: a for a in locked_rows}
        source = by_id[source_id]
        target = by_id[target_id]
        transfer = models.QuotaTransfer(
            operation_no=operation_no,
            region=region,
            channel=channel,
            source_authorization_id=source_id,
            target_authorization_id=target_id,
            count=count,
            source_max_count=source.max_count,
            source_used_count=source.used_count,
            source_reserved_count=source.reserved_count,
            target_max_count=target.max_count,
            target_used_count=target.used_count,
            target_reserved_count=target.reserved_count,
        )
        db.add(transfer)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(models.QuotaTransfer).where(
                    models.QuotaTransfer.operation_no == operation_no
                )
            )
            if existing is not None:
                return _replay_or_conflict(existing)
            raise
    except (ChannelFrozen, TransferRejected):
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    return _transfer_out(transfer)


# ================================================================== 候补
def create_waitlist(db: Session, data: WaitlistCreate) -> models.Waitlist:
    """提交候补申请：沿用既有匹配与重叠授权选择规则。

    - 全部可承接：直接原子占额生成一笔 approved 申请，候补记为
      ``fulfilled`` 并关联申请编号（仍记录申请有效期与到期时刻）；
    - 每项均有匹配授权但额度不足：进入本通道候补队列（``pending``，
      按受理顺序等待，不占任何额度；仅在申请有效期 ``expires_at``
      之内等待成交，到期处理队首时标记 ``expired`` 并释放位置）；
    - 任一项缺少匹配授权（无匹配或匹配授权均已停用）：整体逐项拒绝，
      不入队、不占额。
    """
    material_ids = list(data.material_ids)
    _validate_material_ids(db, material_ids)

    try:
        _channel_lock(db, data.region, data.channel)
        now = utcnow()
        expires_at = now + timedelta(minutes=data.ttl_minutes)

        # 通道冻结期间，候补受理明确拒绝：不成交、不入队、不占额。
        # 到期预留/候补的有效期结算由恢复事务与冻结期间允许的写操作
        # （确认/取消/撤销/改期/改挂）在同一把通道锁内完成。
        freeze_row = _channel_is_frozen(db, data.region, data.channel)
        if freeze_row is not None:
            db.rollback()
            raise ChannelFrozen(
                data.region, data.channel, freeze_row.reason
            )

        candidate_ids = _candidate_auth_ids(
            db, material_ids, data.region, data.channel, data.occur_at
        )
        locked_rows = _settle_expired(
            db,
            now,
            region=data.region,
            channel=data.channel,
            extra_auth_ids=candidate_ids,
        )

        # 既有候补按受理顺序优先：先重算队首，新候补不得插队抢额。
        if _process_waitlist(db, data.region, data.channel, now):
            locked_rows = _lock_authorizations(db, candidate_ids)

        chosen, reasons = _evaluate(
            material_ids,
            locked_rows,
            region=data.region,
            channel=data.channel,
            occur=data.occur_at,
            for_reservation=False,
        )

        if reasons:
            # 任一项缺少匹配授权（无匹配/均已停用）→ 整体逐项拒绝；
            # 仅当每项均有匹配授权、纯粹额度不足时才入队候补。
            if any(r["reason"] != "quota_exhausted" for r in reasons):
                db.rollback()
                raise WaitlistRejected(reasons)
            waitlist = models.Waitlist(
                region=data.region,
                channel=data.channel,
                occur_at=data.occur_at,
                ttl_minutes=data.ttl_minutes,
                expires_at=expires_at,
                status="pending",
            )
            db.add(waitlist)
            db.flush()
            for index, mid in enumerate(material_ids):
                db.add(
                    models.WaitlistItem(
                        waitlist_id=waitlist.id,
                        material_id=mid,
                        item_index=index,
                    )
                )
            db.commit()
            return _load_waitlist(db, waitlist.id)

        # 全部可承接：与直接申请一致地条件占额并生成 approved 申请。
        for auth in chosen.values():
            result = db.execute(
                update(models.Authorization)
                .where(
                    models.Authorization.id == auth.id,
                    models.Authorization.used_count
                    + models.Authorization.reserved_count
                    < models.Authorization.max_count,
                )
                .values(used_count=models.Authorization.used_count + 1)
            )
            if result.rowcount != 1:
                db.rollback()
                raise WaitlistRejected(
                    [
                        {
                            "material_id": auth.material_id,
                            "reason": "quota_exhausted",
                            "message": (
                                f"授权 id={auth.id} 余量不足，"
                                "并发占用冲突，本次候补整体拒绝"
                            ),
                        }
                    ]
                )

        distribution = models.Distribution(
            region=data.region,
            channel=data.channel,
            occur_at=data.occur_at,
            status="approved",
        )
        db.add(distribution)
        db.flush()
        for index, mid in enumerate(material_ids):
            db.add(
                models.DistributionItem(
                    distribution_id=distribution.id,
                    material_id=mid,
                    authorization_id=chosen[mid].id,
                    item_index=index,
                )
            )
        waitlist = models.Waitlist(
            region=data.region,
            channel=data.channel,
            occur_at=data.occur_at,
            ttl_minutes=data.ttl_minutes,
            expires_at=expires_at,
            status="fulfilled",
            distribution_id=distribution.id,
            fulfilled_at=now,
        )
        db.add(waitlist)
        db.flush()
        db.commit()
    except (ChannelFrozen, WaitlistRejected):
        raise
    except Exception:
        db.rollback()
        raise

    return _load_waitlist(db, waitlist.id)


def _load_waitlist(
    db: Session, waitlist_id: int, *, for_update: bool = False
) -> models.Waitlist:
    # 成交事务内把同事务新建的 distribution 赋给本候补后，关系缓存可能为
    # None；一律使会话已加载状态过期，保证下面 selectinload 真正载入新关系。
    db.expire_all()
    stmt = (
        select(models.Waitlist)
        .where(models.Waitlist.id == waitlist_id)
        .options(
            selectinload(models.Waitlist.items).selectinload(
                models.WaitlistItem.material
            ),
            selectinload(models.Waitlist.distribution)
            .selectinload(models.Distribution.items)
            .selectinload(models.DistributionItem.material),
            selectinload(models.Waitlist.distribution)
            .selectinload(models.Distribution.items)
            .selectinload(models.DistributionItem.authorization),
        )
    )
    if for_update:
        stmt = stmt.with_for_update()
    waitlist = db.scalar(stmt)
    if waitlist is None:
        raise ResourceNotFound("候补", waitlist_id)
    return waitlist


def get_waitlist(db: Session, waitlist_id: int) -> models.Waitlist:
    """查询候补；先在通道内结算到期预留（可能顺带成交本通道候补）。"""
    head = db.execute(
        select(
            models.Waitlist.region,
            models.Waitlist.channel,
        ).where(models.Waitlist.id == waitlist_id)
    ).first()
    if head is None:
        raise ResourceNotFound("候补", waitlist_id)
    region, channel = head
    try:
        now = utcnow()
        _channel_lock(db, region, channel)
        frozen_row = _channel_is_frozen(db, region, channel)
        _settle_expired(
            db,
            now,
            region=region,
            channel=channel,
            frozen=frozen_row is not None,
        )
        # 读路径只做申请有效期的惰性失效（不重算成交/失败）：使已过期
        # 候补立即呈现（冻结期间同样失效——冻结不延长有效期）；授权停用
        # 等「不再匹配」仍按既有约定等到额度释放类写事务或恢复事务处理
        # 队首时才标记 failed。
        _expire_due_waitlists(db, region, channel, now)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _load_waitlist(db, waitlist_id)


def list_waitlists(
    db: Session, status: str | None
) -> list[models.Waitlist]:
    """查询候补列表（按受理顺序），可按状态过滤。

    列表为只读视图：库内仍 pending 但已过申请有效期（``expires_at``
    非空且 ``<= now``）的候补按 **expired** 呈现（写路径与单条查询会
    在通道锁内惰性落库；此处仅在响应中派生状态，不产生写事务）。
    """
    if status is not None and status not in (
        "pending",
        "fulfilled",
        "failed",
        "cancelled",
        "expired",
    ):
        raise InvalidWaitlistStatus(status)

    now = utcnow()
    rows = list(
        db.scalars(
            select(models.Waitlist)
            .options(
                selectinload(models.Waitlist.items).selectinload(
                    models.WaitlistItem.material
                ),
                selectinload(models.Waitlist.distribution)
                .selectinload(models.Distribution.items)
                .selectinload(models.DistributionItem.material),
                selectinload(models.Waitlist.distribution)
                .selectinload(models.Distribution.items)
                .selectinload(models.DistributionItem.authorization),
            )
            .order_by(models.Waitlist.id.asc())
        )
    )

    result: list[models.Waitlist] = []
    for waitlist in rows:
        effective = waitlist.status
        if (
            waitlist.status == "pending"
            and waitlist.expires_at is not None
            and waitlist.expires_at <= now
        ):
            effective = "expired"
        if status is None or effective == status:
            # 分离后覆盖派生状态，避免把瞬时状态标记为脏对象。
            db.expunge(waitlist)
            object.__setattr__(waitlist, "status", effective)
            object.__setattr__(
                waitlist, "expiry_reason", WAITLIST_EXPIRY_TTL
            )
            object.__setattr__(waitlist, "expired_at", now)
            result.append(waitlist)
    return result


def cancel_waitlist(db: Session, waitlist_id: int) -> models.Waitlist:
    """取消待成交候补（仅 pending 可取消，仅生效一次）。

    候补在队列中不占任何额度，取消无需归还；已成交的候补不可取消，
    其生成的申请沿用既有撤销规则（``POST /distributions/{id}/revoke``）。
    """
    head = db.execute(
        select(
            models.Waitlist.region,
            models.Waitlist.channel,
        ).where(models.Waitlist.id == waitlist_id)
    ).first()
    if head is None:
        raise ResourceNotFound("候补", waitlist_id)
    region, channel = head

    try:
        now = utcnow()
        _channel_lock(db, region, channel)
        frozen_row = _channel_is_frozen(db, region, channel)
        frozen = frozen_row is not None
        _settle_expired(
            db, now, region=region, channel=channel, frozen=frozen
        )
        # 先处理队列：已过申请有效期的候补落库为 expired，不允许再取消。
        # 这是独立的惰性结算（非冻结时含可能的成交；冻结时仅到期失效），
        # 须先提交持久化，再在新事务中复查目标候补状态——否则随后冲突
        # 分支的 rollback 会把失效/成交一并回滚。
        _process_waitlist(
            db, region, channel, now, allow_fulfill=not frozen
        )
        db.commit()

        waitlist = _load_waitlist(db, waitlist_id, for_update=True)
        if waitlist.status == "fulfilled":
            db.rollback()
            raise WaitlistAlreadyFulfilled(
                waitlist_id, waitlist.distribution_id
            )
        if waitlist.status == "cancelled":
            db.rollback()
            raise WaitlistAlreadyCancelled(waitlist_id)
        if waitlist.status == "failed":
            db.rollback()
            raise WaitlistAlreadyFailed(waitlist_id)
        if waitlist.status == "expired":
            db.rollback()
            raise WaitlistAlreadyExpired(waitlist_id)

        waitlist.status = "cancelled"
        waitlist.cancelled_at = utcnow()
        db.flush()

        # 队首可能因本次取消而变化：按受理顺序重算本通道候补队首；
        # 冻结期间仅结算到期失效，不成交。
        _process_waitlist(db, region, channel, allow_fulfill=not frozen)
        db.commit()
    except (
        ResourceNotFound,
        WaitlistNotPending,
    ):
        raise
    except Exception:
        db.rollback()
        raise

    return _load_waitlist(db, waitlist_id)


# ============================================================ 通道额度冻结
def set_channel_freeze(
    db: Session, data: ChannelFreezeRequest
) -> models.ChannelFreeze:
    """按地区 + 渠道冻结 / 恢复新额度发放（与通道发行事务共用咨询锁）。

    冻结（``action=freeze``，须有原因）：
      - 通道下必须存在至少一条授权（含已停用），否则 422
        ``channel_not_found``；
      - 重复冻结（已 frozen）：**幂等空操作**，原因、``changed_at`` 均不
        改动，直接返回当前状态。

    恢复（``action=resume``）：
      - 未冻结通道（含从未冻结）：幂等空操作，返回当前状态；
      - 否则在同一事务/同一把通道咨询锁内：先把状态翻转为 active，再
        ①惰性结算本通道到期预留（冻结不延长预留有效期，到期释放额度），
        ②按**原受理顺序**处理仍有效的候补（过期者先按原有效期标记
        expired 并释放位置；可整体承接者原子成交；授权不再匹配者标记
        failed；队首额度不足则停止），随后本通道新请求即可占额。
        翻转状态先于队列处理，故队列处理期间本通道在语义上已恢复。

    冻结/恢复与并发发行同通道串行（同一把咨询锁），其他通道不受影响。
    """
    region, channel = data.region, data.channel
    try:
        _channel_lock(db, region, channel)
        now = utcnow()

        row = _get_freeze_row(db, region, channel)

        if data.action == "freeze":
            if row is not None and row.status == "frozen":
                # 重复同态：不改动原因与变更时刻。
                db.commit()
                return row
            # 通道存在性在锁内判定（与并发授权登记串行意义上取最新视图）。
            _ensure_channel_exists(db, region, channel)
            if row is None:
                row = models.ChannelFreeze(
                    region=region,
                    channel=channel,
                    status="frozen",
                    reason=data.reason,
                    changed_at=now,
                    frozen_at=now,
                    resumed_at=None,
                )
                db.add(row)
            else:
                row.status = "frozen"
                row.reason = data.reason
                row.changed_at = now
                row.frozen_at = now
            db.commit()

        else:  # resume
            if row is None or row.status == "active":
                # 重复同态（含从未冻结）：不改动变更时刻。通道不存在同样
                # 明确报错——查询/操作须有明确对象。
                if row is None:
                    _ensure_channel_exists(db, region, channel)
                db.commit()
                return _freeze_state_out(region, channel, row)

            # 先翻转状态：随后的到期结算与队列处理均按「已恢复」进行
            # （本事务仍持有通道咨询锁，外部观察不到中间态）。
            row.status = "active"
            row.changed_at = now
            row.resumed_at = now
            db.flush()

            # 冻结期间到期的预留按原有效期结算释放；冻结期间到期的候补
            # 在 _process_waitlist 内统一标记 expired；随后按原受理顺序
            # 处理仍有效的候补，先于恢复后允许的任何新请求占额。
            _settle_expired(db, now, region=region, channel=channel)
            _process_waitlist(db, region, channel, now)
            db.commit()
    except ChannelNotFound:
        raise
    except Exception:
        db.rollback()
        raise

    db.refresh(row)
    return row


def get_channel_freeze_state(
    db: Session, region: str, channel: str
) -> models.ChannelFreeze:
    """查询通道当前冻结状态、末次原因与变更时刻。

    通道下不存在任何授权时 422 ``channel_not_found``；从未冻结的通道
    返回 ``status=active``、``reason=null``、``changed_at=null``。
    查询在通道咨询锁内顺带做一次到期/失效惰性结算（冻结期间不成交），
    与预留/候补单条查询的读语义保持一致，但不改变冻结状态本身。
    """
    try:
        _channel_lock(db, region, channel)
        _ensure_channel_exists(db, region, channel)
        now = utcnow()
        frozen_row = _channel_is_frozen(db, region, channel)
        _settle_expired(
            db,
            now,
            region=region,
            channel=channel,
            frozen=frozen_row is not None,
        )
        _expire_due_waitlists(db, region, channel, now)
        row = _get_freeze_row(db, region, channel)
        db.commit()
    except ChannelNotFound:
        raise
    except Exception:
        db.rollback()
        raise
    return _freeze_state_out(region, channel, row)
