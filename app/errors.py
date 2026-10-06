"""领域异常：统一转换为带错误码的 JSON 响应。"""


class APIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: list[dict] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class ResourceNotFound(APIError):
    """路径编号不存在。"""

    def __init__(self, resource: str, identifier: object):
        super().__init__(
            status_code=404,
            code="not_found",
            message=f"{resource}不存在：{identifier}",
        )


class MaterialCodeTaken(APIError):
    def __init__(self, code: str):
        super().__init__(
            status_code=409,
            code="duplicate_material",
            message=f"素材编号已存在：{code}",
        )


class UnknownMaterial(APIError):
    """请求体内引用了未知素材编号。"""

    def __init__(self, unknown_ids: list[int]):
        super().__init__(
            status_code=422,
            code="unknown_material",
            message=f"未知素材编号：{', '.join(map(str, unknown_ids))}",
            details=[{"material_id": mid} for mid in unknown_ids],
        )


class DuplicateMaterialItems(APIError):
    def __init__(self, duplicated_ids: list[int]):
        super().__init__(
            status_code=422,
            code="duplicate_material_items",
            message=f"申请中素材编号重复：{', '.join(map(str, duplicated_ids))}",
            details=[{"material_id": mid} for mid in duplicated_ids],
        )


class DistributionRejected(APIError):
    """授权核验未通过：整体拒绝、不占用任何次数。"""

    def __init__(self, reasons: list[dict]):
        super().__init__(
            status_code=422,
            code="distribution_rejected",
            message="授权核验未通过，未占用任何发行次数",
            details=reasons,
        )


class AlreadyRevoked(APIError):
    def __init__(self, distribution_id: int):
        super().__init__(
            status_code=409,
            code="already_revoked",
            message=f"发行申请已撤销，不可重复撤销：{distribution_id}",
        )


class DistributionRevoked(APIError):
    """已撤销的申请不可改期。"""

    def __init__(self, distribution_id: int):
        super().__init__(
            status_code=409,
            code="distribution_revoked",
            message=f"发行申请已撤销，不可改期：{distribution_id}",
        )


class RescheduleRejected(APIError):
    """改期核验未通过：整体拒绝，原申请时刻、明细归属与授权次数均不变。"""

    def __init__(self, reasons: list[dict]):
        super().__init__(
            status_code=422,
            code="reschedule_rejected",
            message="改期核验未通过，原申请发行时刻、明细归属与授权次数均未变更",
            details=reasons,
        )


class MigrationRejected(APIError):
    """授权退役改挂核验未通过：整体拒绝，归属与次数均不变。"""

    def __init__(self, reasons: list[dict]):
        super().__init__(
            status_code=422,
            code="migration_rejected",
            message="授权改挂核验未通过，未变更任何申请归属与次数",
            details=reasons,
        )


class ReservationRejected(APIError):
    """预留核验未通过：整体拒绝、不占用任何（预留）额度。"""

    def __init__(self, reasons: list[dict]):
        super().__init__(
            status_code=422,
            code="reservation_rejected",
            message="预留核验未通过，未占用任何预留额度",
            details=reasons,
        )


class ReservationNotPending(APIError):
    """对预留执行确认/取消时的状态冲突基类（409）。"""

    def __init__(self, code: str, message: str):
        super().__init__(status_code=409, code=code, message=message)


class ReservationAlreadyConfirmed(ReservationNotPending):
    """预留已确认：重复确认幂等由路由处理；对已确认预留取消则冲突。"""

    def __init__(self, reservation_id: int):
        super().__init__(
            code="reservation_already_confirmed",
            message=f"预留已确认，不可取消：{reservation_id}",
        )


class ReservationAlreadyCancelled(ReservationNotPending):
    """重复取消：409 且不再释放任何额度。"""

    def __init__(self, reservation_id: int):
        super().__init__(
            code="reservation_already_cancelled",
            message=f"预留已取消，重复取消不再释放额度：{reservation_id}",
        )


class ReservationCancelled(ReservationNotPending):
    def __init__(self, reservation_id: int, action: str = "确认"):
        super().__init__(
            code="reservation_cancelled",
            message=f"预留已取消，{action}被拒绝：{reservation_id}",
        )


class ReservationExpired(ReservationNotPending):
    """到期时刻（含）之后确认视为逾期；取消已过期预留同样拒绝（额度已释放）。"""

    def __init__(self, reservation_id: int, action: str = "确认"):
        super().__init__(
            code="reservation_expired",
            message=f"预留已过期，{action}被拒绝：{reservation_id}",
        )


class InvalidReservationStatus(APIError):
    """列表查询使用了不支持的状态过滤值。"""

    def __init__(self, value: str):
        super().__init__(
            status_code=422,
            code="invalid_reservation_status",
            message=(
                f"不支持的预留状态：{value}；"
                "可选 pending/confirmed/cancelled/expired"
            ),
        )


# ------------------------------------------------------------------ 候补
class WaitlistRejected(APIError):
    """候补核验未通过：存在缺少匹配授权的项，整体拒绝、不入队、不占额。"""

    def __init__(self, reasons: list[dict]):
        super().__init__(
            status_code=422,
            code="waitlist_rejected",
            message="候补核验未通过：存在缺少匹配授权的素材，未进入候补队列",
            details=reasons,
        )


class WaitlistNotPending(APIError):
    """对候补执行取消时的状态冲突基类（409）。"""

    def __init__(self, code: str, message: str):
        super().__init__(status_code=409, code=code, message=message)


class WaitlistAlreadyFulfilled(WaitlistNotPending):
    """候补已成交：不可取消，如需释放额度请撤销其生成的申请。"""

    def __init__(self, waitlist_id: int, distribution_id: int | None):
        super().__init__(
            code="waitlist_already_fulfilled",
            message=(
                f"候补已成交并生成发行申请（id={distribution_id}），"
                f"不可取消；如需释放额度请撤销该申请：{waitlist_id}"
            ),
        )


class WaitlistAlreadyCancelled(WaitlistNotPending):
    """重复取消候补：409。"""

    def __init__(self, waitlist_id: int):
        super().__init__(
            code="waitlist_already_cancelled",
            message=f"候补已取消，不可重复取消：{waitlist_id}",
        )


class WaitlistAlreadyFailed(WaitlistNotPending):
    """候补已失败（终态）：不可取消。"""

    def __init__(self, waitlist_id: int):
        super().__init__(
            code="waitlist_already_failed",
            message=f"候补已失败（授权不再匹配），不可取消：{waitlist_id}",
        )


class WaitlistAlreadyExpired(WaitlistNotPending):
    """候补已过申请有效期（终态）：不可取消。"""

    def __init__(self, waitlist_id: int):
        super().__init__(
            code="waitlist_already_expired",
            message=f"候补已超过申请有效期失效，不可取消：{waitlist_id}",
        )


class InvalidWaitlistStatus(APIError):
    """候补列表查询使用了不支持的状态过滤值。"""

    def __init__(self, value: str):
        super().__init__(
            status_code=422,
            code="invalid_waitlist_status",
            message=(
                f"不支持的候补状态：{value}；"
                "可选 pending/fulfilled/failed/cancelled/expired"
            ),
        )


# ------------------------------------------------------------- 通道额度冻结
class ChannelNotFound(APIError):
    """通道（region+channel）下不存在任何授权：无冻结/恢复/查询对象。"""

    def __init__(self, region: str, channel: str):
        super().__init__(
            status_code=422,
            code="channel_not_found",
            message=(
                "通道不存在：该地区与渠道下尚无任何授权，"
                f"region={region!r}, channel={channel!r}"
            ),
            details=[{"region": region, "channel": channel}],
        )


class ChannelFrozen(APIError):
    """冻结期间提交新的直接发行 / 额度预留 / 候补受理：明确拒绝且不占额。"""

    def __init__(self, region: str, channel: str, reason: str | None):
        super().__init__(
            status_code=422,
            code="channel_frozen",
            message=(
                f"通道已冻结新额度发放（region={region!r}, "
                f"channel={channel!r}），新的直接发行、额度预留与候补受理"
                f"均被拒绝；冻结原因：{reason or '（未记录）'}"
            ),
            details=[
                {
                    "region": region,
                    "channel": channel,
                    "reason": reason,
                }
            ],
        )


# ------------------------------------------------------------- 授权额度转拨
class TransferRejected(APIError):
    """额度转拨核验未通过：整笔拒绝，两端授权额度均不变。"""

    def __init__(self, reasons: list[dict]):
        super().__init__(
            status_code=422,
            code="quota_transfer_rejected",
            message="额度转拨核验未通过，两端授权额度均未变更",
            details=reasons,
        )


class TransferOperationConflict(APIError):
    """相同操作号携带不同参数：与首次转拨请求冲突（幂等键冲突）。"""

    def __init__(self, operation_no: str):
        super().__init__(
            status_code=409,
            code="quota_transfer_conflict",
            message=(
                f"操作号 {operation_no!r} 已用于一笔不同参数的转拨；"
                "相同操作号必须以相同参数重放"
            ),
        )
