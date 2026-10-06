"""请求 / 响应模型。

所有时间字段均强制要求带时区信息（如 ``2026-10-01T00:00:00+08:00``），
服务端统一按 UTC 存储、按原始语义比较，彻底避免夏令时 / 时区歧义。
"""
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.config import settings


def _ensure_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field_name} 必须携带时区信息，例如 +08:00")
    return value


class MaterialCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, description="素材业务编号")
    name: str = Field(min_length=1, max_length=255, description="素材名称")

    @field_validator("code", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空串")
        return v


class MaterialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    created_at: datetime


class AuthorizationCreate(BaseModel):
    material_id: int = Field(gt=0)
    region: str = Field(min_length=1, max_length=64)
    channel: str = Field(min_length=1, max_length=64)
    # 左闭右开：[starts_at, ends_at)
    starts_at: datetime
    ends_at: datetime
    max_count: int = Field(gt=0, description="可发行次数（正整数）")

    @field_validator("region", "channel")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空串")
        return v

    @field_validator("starts_at")
    @classmethod
    def _starts_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v, "starts_at")

    @field_validator("ends_at")
    @classmethod
    def _ends_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v, "ends_at")

    @model_validator(mode="after")
    def _validate_period(self) -> "AuthorizationCreate":
        if self.starts_at >= self.ends_at:
            raise ValueError(
                "非法时段：左闭右开要求 starts_at 严格早于 ends_at"
            )
        return self


class AuthorizationUpdate(BaseModel):
    status: Literal["active", "inactive"] = Field(
        description="active=启用，inactive=停用（仅影响新申请）"
    )


class AuthorizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    material_id: int
    region: str
    channel: str
    starts_at: datetime
    ends_at: datetime
    max_count: int
    used_count: int
    remaining: int
    # 未到期（待确认）预留占用的额度；可预留余量 = max - used - reserved。
    reserved_count: int
    reservable_remaining: int
    status: str
    created_at: datetime


class DistributionCreate(BaseModel):
    material_ids: list[int] = Field(
        min_length=1, description="本次申请发行的素材编号（数据库自增 id）"
    )
    region: str = Field(min_length=1, max_length=64)
    channel: str = Field(min_length=1, max_length=64)
    occur_at: datetime = Field(description="发行时刻（带时区）")

    @field_validator("material_ids")
    @classmethod
    def _positive_ids(cls, v: list[int]) -> list[int]:
        if any(mid <= 0 for mid in v):
            raise ValueError("素材编号必须为正整数")
        return v

    @field_validator("region", "channel")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空串")
        return v

    @field_validator("occur_at")
    @classmethod
    def _occur_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v, "occur_at")


class DistributionItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_index: int
    material_id: int
    authorization: AuthorizationOut


class DistributionReschedule(BaseModel):
    """发行申请改期：仅更换发行时刻，地区/渠道/素材清单不变。"""

    occur_at: datetime = Field(description="新的发行时刻（带时区）")

    @field_validator("occur_at")
    @classmethod
    def _occur_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v, "occur_at")


# ------------------------------------------------------- 授权退役改挂
class AuthorizationMigrationRequest(BaseModel):
    """原授权退役：将其承载的全部未撤销申请一次性改挂到替代授权。"""

    source_authorization_id: int = Field(
        gt=0, description="原授权编号（退役方）"
    )
    replacement_authorization_id: int = Field(
        gt=0, description="替代授权编号（承接方，须与原授权不同）"
    )


class AuthorizationMigrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    migrated_count: int = Field(description="实际改挂的未撤销申请明细条数")
    source_authorization: AuthorizationOut = Field(
        description="原授权（成功后已停用，次数已迁出）"
    )
    replacement_authorization: AuthorizationOut = Field(
        description="替代授权（已承接全部迁入占用）"
    )


class DistributionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    region: str
    channel: str
    occur_at: datetime
    status: Literal["approved", "revoked"]
    created_at: datetime
    revoked_at: datetime | None = None
    items: list[DistributionItemOut]


# ------------------------------------------------------------- 额度预留
class ReservationCreate(BaseModel):
    material_ids: list[int] = Field(
        min_length=1, description="本次预留涉及的素材编号（数据库自增 id）"
    )
    region: str = Field(min_length=1, max_length=64)
    channel: str = Field(min_length=1, max_length=64)
    occur_at: datetime = Field(description="发行时刻（带时区）")
    ttl_minutes: int = Field(
        ge=1,
        le=30,
        description=(
            "预留有效期（分钟），范围 "
            f"{settings.reservation_ttl_min_minutes}～"
            f"{settings.reservation_ttl_max_minutes}；"
            "到期时刻 = 受理时刻 + ttl_minutes"
        ),
    )

    @field_validator("material_ids")
    @classmethod
    def _positive_ids(cls, v: list[int]) -> list[int]:
        if any(mid <= 0 for mid in v):
            raise ValueError("素材编号必须为正整数")
        return v

    @field_validator("region", "channel")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空串")
        return v

    @field_validator("occur_at")
    @classmethod
    def _occur_aware(cls, v: datetime) -> datetime:
        return _ensure_aware(v, "occur_at")

    @model_validator(mode="after")
    def _validate_ttl(self) -> "ReservationCreate":
        lo, hi = (
            settings.reservation_ttl_min_minutes,
            settings.reservation_ttl_max_minutes,
        )
        if not (lo <= self.ttl_minutes <= hi):
            raise ValueError(
                f"预留有效期必须在 {lo}～{hi} 分钟之间，"
                f"收到 {self.ttl_minutes}"
            )
        return self


class ReservationItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_index: int
    material_id: int
    authorization: AuthorizationOut


class ReservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    region: str
    channel: str
    occur_at: datetime
    ttl_minutes: int
    expires_at: datetime = Field(description="到期时刻（带时区）；左闭右开，到期时刻确认视为逾期")
    # pending=待确认 confirmed=已确认 cancelled=已取消 expired=已过期。
    # 读取时惰性结算：库内仍为 pending 但已过到期时刻的预留返回 expired。
    status: Literal["pending", "confirmed", "cancelled", "expired"]
    created_at: datetime
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None
    expired_at: datetime | None = None
    distribution_id: int | None = Field(
        default=None, description="确认后生成的发行申请编号（重复确认返回同一笔）"
    )
    # 仅在已确认（status=confirmed）时随附完整申请。
    distribution: DistributionOut | None = None
    items: list[ReservationItemOut] = Field(
        description="逐项命中授权（含确认时的逐项可预留余量快照）"
    )


# ----------------------------------------------------------------- 候补
class WaitlistCreate(DistributionCreate):
    """候补申请：沿用直接发行申请字段（素材清单/地区/渠道/带时区发行时刻），
    另加申请有效期 ``ttl_minutes``（1～1440 分钟，到期时刻 = 受理时刻 +
    ttl_minutes）。

    - 全部可承接：直接生成已通过申请（status=fulfilled，随附申请编号）；
    - 每项均有匹配授权但额度不足：进入本通道候补队列（status=pending），
      仅在有效期内等待成交，处理队首时已过期则标记 expired 并让出位置；
    - 任一项缺少匹配授权或 ``ttl_minutes`` 缺失/超范围：422 拒绝，不入队。
    """

    ttl_minutes: int = Field(
        ge=1,
        le=1440,
        description=(
            "候补申请有效期（分钟），范围 1～1440；"
            "到期时刻 = 受理时刻 + ttl_minutes，"
            "候补仅在有效期内等待成交（左闭右开，到期时刻即失效）"
        ),
    )


class WaitlistItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_index: int
    material_id: int


class WaitlistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    region: str
    channel: str
    occur_at: datetime
    # 申请有效期（分钟，1～1440）与到期时刻；pending 仅在到期时刻前可成交。
    ttl_minutes: int | None = Field(
        default=None, description="候补申请有效期（分钟），范围 1～1440"
    )
    expires_at: datetime | None = Field(
        default=None,
        description="申请失效时刻（受理时刻 + ttl_minutes，带时区）；左闭右开，到达该时刻即失效",
    )
    # pending=待成交 fulfilled=已成交 failed=已失败（授权不再匹配）
    # cancelled=已取消 expired=已失效（超过申请有效期）。
    status: Literal[
        "pending", "fulfilled", "failed", "cancelled", "expired"
    ]
    failure_reason: str | None = Field(
        default=None,
        description="终态失败原因：no_matching_authorization / authorization_inactive",
    )
    expiry_reason: str | None = Field(
        default=None,
        description="失效原因（status=expired）：当前固定为 ttl_expired",
    )
    distribution_id: int | None = Field(
        default=None, description="成交后生成的发行申请编号"
    )
    created_at: datetime
    fulfilled_at: datetime | None = None
    failed_at: datetime | None = None
    cancelled_at: datetime | None = None
    expired_at: datetime | None = Field(
        default=None, description="失效时刻（处理队首时被标记为 expired 的时刻）"
    )
    items: list[WaitlistItemOut] = Field(description="候补素材清单")
    # 仅在已成交（status=fulfilled）时随附完整申请。
    distribution: DistributionOut | None = None


# ----------------------------------------------------------- 通道额度冻结
class ChannelFreezeRequest(BaseModel):
    """按地区 + 渠道临时冻结 / 恢复新额度发放。

    - ``action=freeze``：必须携带非空 ``reason``；
    - ``action=resume``：恢复时先按原受理顺序处理本通道仍有效的候补，
      再允许新请求占额；``reason`` 无需提供（提供也忽略）；
    - 重复同态操作（已冻结再冻结、未冻结再恢复）不改动原因与变更时刻。
    """

    region: str = Field(min_length=1, max_length=64)
    channel: str = Field(min_length=1, max_length=64)
    action: Literal["freeze", "resume"] = Field(
        description="freeze=冻结新额度发放；resume=恢复（先清理有效候补）"
    )
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="冻结原因（action=freeze 时必填非空；恢复时忽略）",
    )

    @field_validator("region", "channel")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空串")
        return v

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @model_validator(mode="after")
    def _validate_reason(self) -> "ChannelFreezeRequest":
        if self.action == "freeze" and not self.reason:
            raise ValueError("冻结必须填写原因（reason 为非空字符串）")
        return self


class ChannelFreezeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    region: str
    channel: str
    # frozen=冻结中（拒绝新直接发行/新预留/候补受理）；active=正常发放。
    status: Literal["frozen", "active"]
    # 末次冻结原因（恢复后仍保留；从未冻结的通道查询时为 null）。
    reason: str | None = None
    # 最近一次状态变更（冻结/恢复）时刻；重复同态操作不刷新。
    changed_at: datetime | None = Field(
        default=None, description="最近一次冻结/恢复变更时刻（带时区）"
    )
    frozen_at: datetime | None = None
    resumed_at: datetime | None = None


# ----------------------------------------------------------- 授权额度转拨
class QuotaTransferCreate(BaseModel):
    """授权间额度转拨：同一素材/地区/渠道下，源授权 → 目标授权。

    ``operation_no`` 为幂等键：相同操作号 + 相同参数的重复请求返回首次
    转拨结果；相同操作号 + 不同参数返回 409 ``quota_transfer_conflict``。
    """

    source_authorization_id: int = Field(
        gt=0, description="源授权编号（转出方）"
    )
    target_authorization_id: int = Field(
        gt=0, description="目标授权编号（转入方）"
    )
    count: int = Field(gt=0, description="转拨次数（正整数）")
    operation_no: str = Field(
        min_length=1,
        max_length=64,
        description="操作号（幂等键，全局唯一）",
    )

    @field_validator("operation_no")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空串")
        return v


class QuotaTransferAuthSnapshot(BaseModel):
    """转拨后一端授权的额度与占用快照。"""

    authorization_id: int
    max_count: int
    used_count: int
    reserved_count: int
    remaining: int = Field(
        description="正式可占用余量：max_count - used_count"
    )
    reservable_remaining: int = Field(
        description="可预留余量：max_count - used_count - reserved_count"
    )


class QuotaTransferOut(BaseModel):
    operation_no: str
    count: int
    source_authorization: QuotaTransferAuthSnapshot = Field(
        description="源授权（转出方）转拨后的额度与占用快照"
    )
    target_authorization: QuotaTransferAuthSnapshot = Field(
        description="目标授权（转入方）转拨后的额度与占用快照"
    )
    created_at: datetime = Field(description="首次转拨完成时刻（带时区）")
