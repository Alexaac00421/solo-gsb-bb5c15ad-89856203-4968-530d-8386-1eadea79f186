"""ORM 模型。

授权时段为带时区的 **左闭右开** 区间 ``[starts_at, ends_at)``，
核验时使用 ``starts_at <= occur_at < ends_at``。
"""
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    DateTime,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    authorizations: Mapped[list["Authorization"]] = relationship(
        back_populates="material"
    )


class Authorization(Base):
    __tablename__ = "authorizations"
    __table_args__ = (
        CheckConstraint("starts_at < ends_at", name="ck_auth_valid_period"),
        CheckConstraint(
            "used_count >= 0 AND used_count <= max_count",
            name="ck_auth_quota_invariant",
        ),
        # 未到期（待确认）预留占用的额度与正式占用共享 max_count：
        # used_count 仅统计已通过申请（语义保持兼容），reserved_count
        # 统计未到期待确认预留；二者之和不得超过 max_count。
        CheckConstraint(
            "reserved_count >= 0 "
            "AND used_count + reserved_count <= max_count",
            name="ck_auth_reserved_invariant",
        ),
        Index(
            "ix_auth_match",
            "material_id",
            "region",
            "channel",
            "starts_at",
            "ends_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False
    )
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ends_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    max_count: Mapped[int] = mapped_column(Integer, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 未到期（待确认）预留占用的次数；到期/取消后归还，确认时转入 used_count。
    reserved_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # active：可用于新申请；inactive：已停用，仅影响新申请。
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    material: Mapped["Material"] = relationship(back_populates="authorizations")

    @property
    def remaining(self) -> int:
        """正式可占用余量（语义保持兼容：不含预留）。"""
        return self.max_count - self.used_count

    @property
    def reservable_remaining(self) -> int:
        """可预留余量：总额度扣除正式占用与未到期预留后的剩余。"""
        return self.max_count - self.used_count - self.reserved_count


class Distribution(Base):
    """一次发行申请：整体通过（approved）或整体不通过（不落库）。"""

    __tablename__ = "distributions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('approved', 'revoked')", name="ck_dist_status"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    occur_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="approved", nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    items: Mapped[list["DistributionItem"]] = relationship(
        back_populates="distribution",
        cascade="all, delete-orphan",
        order_by="DistributionItem.id",
    )


class DistributionItem(Base):
    """申请中每个素材所占用的授权（占用时快照授权维度信息）。"""

    __tablename__ = "distribution_items"
    __table_args__ = (
        Index("ix_dist_item_auth", "authorization_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    distribution_id: Mapped[int] = mapped_column(
        ForeignKey("distributions.id", ondelete="CASCADE"), nullable=False
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False
    )
    authorization_id: Mapped[int] = mapped_column(
        ForeignKey("authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)

    distribution: Mapped["Distribution"] = relationship(back_populates="items")
    material: Mapped["Material"] = relationship()
    authorization: Mapped["Authorization"] = relationship()


class Reservation(Base):
    """一次额度预留：全部素材同时核验通过才生成预留编号并逐项占用预留额度。

    生命周期状态：

    - ``pending``   待确认（未到期；逐项占用 ``reserved_count``）
    - ``confirmed`` 已确认（已原子转为一笔 approved 发行申请）
    - ``cancelled`` 已取消（预留额度已释放，仅一次）
    - ``expired``   已过期（到期未确认，预留额度已释放）

    到期时刻（``expires_at``）的判定为左闭右开：``now < expires_at``
    才允许确认；恰在到期时刻确认视为逾期。到期释放采用惰性清理：
    任一通道写事务在咨询锁内首先把已过期待确认预留标记为 expired 并归还
    额度，因此「到期即释放」对后续判定立即可见。
    """

    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled', 'expired')",
            name="ck_resv_status",
        ),
        CheckConstraint(
            "(status = 'confirmed') = (distribution_id IS NOT NULL)",
            name="ck_resv_distribution",
        ),
        # 仅终态记录发生时刻；取消/过期互斥后各自只写一次。
        CheckConstraint(
            "ttl_minutes >= 1 AND ttl_minutes <= 30",
            name="ck_resv_ttl_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    occur_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ttl_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )
    # 确认成功后指向由本预留生成的那笔 approved 申请；重复确认据此返回同一笔。
    distribution_id: Mapped[int | None] = mapped_column(
        ForeignKey("distributions.id", ondelete="RESTRICT"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    items: Mapped[list["ReservationItem"]] = relationship(
        back_populates="reservation",
        cascade="all, delete-orphan",
        order_by="ReservationItem.id",
    )
    distribution: Mapped["Distribution | None"] = relationship()


class ReservationItem(Base):
    """预留中每个素材所命中的授权（预留创建时确定，确认时转为同一授权）。"""

    __tablename__ = "reservation_items"
    __table_args__ = (
        Index("ix_resv_item_auth", "authorization_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reservation_id: Mapped[int] = mapped_column(
        ForeignKey("reservations.id", ondelete="CASCADE"), nullable=False
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False
    )
    authorization_id: Mapped[int] = mapped_column(
        ForeignKey("authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)

    reservation: Mapped["Reservation"] = relationship(back_populates="items")
    material: Mapped["Material"] = relationship()
    authorization: Mapped["Authorization"] = relationship()


class Waitlist(Base):
    """一次候补申请：每项均有匹配授权但额度不足时进入本通道候补队列。

    生命周期状态：

    - ``pending``   待成交（队列中按受理顺序等待，**不占任何额度**；
      仅在申请有效期 ``expires_at`` 之前等待成交）
    - ``fulfilled`` 已成交（额度释放后重算队首时整体可承接，已原子占额
      并转为一笔 approved 发行申请）
    - ``failed``    已失败（重算时授权已不再匹配——无匹配授权或匹配授权
      均已停用，终态，``failure_reason`` 记录原因）
    - ``cancelled`` 已取消（待成交时被取消，终态，仅生效一次）
    - ``expired``   已失效（处理队首时已过申请有效期，终态；
      ``expired_at`` 记录失效时刻，``expiry_reason`` 记录失效原因）

    候补按受理顺序（``id`` 升序）处理：撤销、预留取消/到期、改期、改挂
    释放额度后重算队首——已过有效期的队首先标记 ``expired`` 并释放队列
    位置（再按原顺序尝试后续候补）；可整体承接则原子占额转申请并继续
    下一队首；额度仍不足则停止本轮；授权已不再匹配则标记失败并继续后项。
    """

    __tablename__ = "waitlists"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'fulfilled', 'failed', 'cancelled', "
            "'expired')",
            name="ck_wait_status",
        ),
        CheckConstraint(
            "(status = 'fulfilled') = (distribution_id IS NOT NULL)",
            name="ck_wait_distribution",
        ),
        # 申请有效期（分钟）固定 1～1440；与到期时刻同生共死。
        CheckConstraint(
            "ttl_minutes IS NULL OR "
            "(ttl_minutes >= 1 AND ttl_minutes <= 1440)",
            name="ck_wait_ttl_range",
        ),
        CheckConstraint(
            "(ttl_minutes IS NULL) = (expires_at IS NULL)",
            name="ck_wait_ttl_consistency",
        ),
        # expired 终态必须记录失效时刻与原因；其余状态二者均为空。
        CheckConstraint(
            "status = 'expired' OR expiry_reason IS NULL",
            name="ck_wait_expiry_reason",
        ),
        Index("ix_wait_queue", "region", "channel", "status", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    occur_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 申请有效期（分钟，1～1440）与到期时刻（受理时刻 + ttl_minutes）。
    # 候补仅在有效期内等待成交；两列可空仅为兼容有效期功能上线前的旧记录。
    ttl_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )
    # 终态失败原因（重算时授权已不再匹配）：no_matching_authorization /
    # authorization_inactive；仅 failed 状态有值。
    failure_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # 失效原因；仅 expired 状态有值（当前固定为 ttl_expired）。
    expiry_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # 成交后指向由本候补生成的那笔 approved 申请。
    distribution_id: Mapped[int | None] = mapped_column(
        ForeignKey("distributions.id", ondelete="RESTRICT"), nullable=True
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    items: Mapped[list["WaitlistItem"]] = relationship(
        back_populates="waitlist",
        cascade="all, delete-orphan",
        order_by="WaitlistItem.id",
    )
    distribution: Mapped["Distribution | None"] = relationship()


class WaitlistItem(Base):
    """候补中的素材清单（成交时按既有匹配规则重新选择授权，故不固化授权）。"""

    __tablename__ = "waitlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    waitlist_id: Mapped[int] = mapped_column(
        ForeignKey("waitlists.id", ondelete="CASCADE"), nullable=False
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False
    )
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)

    waitlist: Mapped["Waitlist"] = relationship(back_populates="items")
    material: Mapped["Material"] = relationship()


class ChannelFreeze(Base):
    """通道（region+channel）新额度发放冻结状态。

    每个通道至多一行（唯一约束）。冻结/恢复与该通道全部发行类写事务
    共用同一把通道咨询锁，因此同通道操作严格串行，不存在「冻结与发行
    交错」的中间态。

    - ``status = frozen``：新的直接发行、额度预留与候补受理一律拒绝
      （不占额、不入队）；既有预留仍可确认/取消，已通过申请仍可撤销/
      改期，改挂仍可进行，但这些操作在冻结期间释放的额度**不得使候补
      成交**；到期预留与候补仍按各自原有效期惰性结算，冻结不延长。
    - ``status = active``（恢复）：先按原受理顺序处理本通道仍有效的
      候补，再允许新请求占额。

    幂等：重复同态操作（已冻结再冻结、已恢复再恢复）不改写任何字段
    （原因与 ``changed_at`` 保持不变）。冻结时必须填写原因，原因随
    冻结记录保留；恢复后行保留，``reason`` 仍为末次冻结原因，
    ``changed_at`` 为最近一次状态变更（冻结/恢复）时刻，
    ``frozen_at`` / ``resumed_at`` 记录各自末次时刻。
    """

    __tablename__ = "channel_freezes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('frozen', 'active')", name="ck_freeze_status"
        ),
        # 冻结必须有原因；active 行（恢复后保留的历史行）原因为空。
        CheckConstraint(
            "status = 'active' OR reason IS NOT NULL",
            name="ck_freeze_reason",
        ),
        Index("ux_channel_freeze", "region", "channel", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="frozen", nullable=False
    )
    # 末次冻结原因；恢复后保留，active 新行（理论上不会出现）为空。
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 最近一次状态变更（冻结/恢复）时刻；重复同态操作不刷新。
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    frozen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class QuotaTransfer(Base):
    """一次授权间额度转拨：同一素材/地区/渠道下，把源授权未占用的
    可发行次数（``max_count`` 的一部分）转拨到目标授权。

    - 仅两端授权均启用且通道未冻结时受理；两端授权时段可以不同；
    - 既有申请与预留的归属不变，仅原子调整两端 ``max_count``；
    - ``operation_no`` 全局唯一（幂等键）：相同操作号 + 相同参数的
      重复请求返回首次转拨结果（重放不受随后的通道冻结/授权停用影响）；
      相同操作号 + 不同参数为冲突（409）；
    - 快照列记录首次转拨完成（含同事务候补重算）后两端的额度与占用，
      重放时原样返回，不随后续业务变化。
    """

    __tablename__ = "quota_transfers"
    __table_args__ = (
        CheckConstraint("count > 0", name="ck_transfer_count_positive"),
        Index("ux_quota_transfer_operation", "operation_no", unique=True),
        Index(
            "ix_transfer_auths",
            "source_authorization_id",
            "target_authorization_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 幂等键：运营侧操作号，全局唯一。
    operation_no: Mapped[str] = mapped_column(String(64), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    source_authorization_id: Mapped[int] = mapped_column(
        ForeignKey("authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    target_authorization_id: Mapped[int] = mapped_column(
        ForeignKey("authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    # 首次转拨完成后的两端额度与占用快照（重放原样返回）。
    source_max_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_used_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_reserved_count: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    target_max_count: Mapped[int] = mapped_column(Integer, nullable=False)
    target_used_count: Mapped[int] = mapped_column(Integer, nullable=False)
    target_reserved_count: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
