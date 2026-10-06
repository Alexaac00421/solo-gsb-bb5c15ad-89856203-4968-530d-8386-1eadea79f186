"""FastAPI 应用入口与 HTTP 路由。"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import schemas, services
from app.database import get_db, init_db
from app.errors import APIError


@asynccontextmanager
async def lifespan(app: FastAPI):
    # docker compose up 后自动建表，无需手工迁移。
    init_db()
    yield


app = FastAPI(
    title="素材发行授权核验 API",
    version="1.7.0",
    description=(
        "登记素材与授权（地区/渠道/带时区左闭右开时段/可发行次数），"
        "对发行申请做整体核验、原子占用，支持查询、一次性撤销、"
        "已通过申请的改期（原子重选授权）、"
        "授权退役时将未撤销申请一次性改挂到替代授权；"
        "支持 1～30 分钟有效期的发行额度预留（待确认/确认/取消/过期）；"
        "支持额度不足时的通道候补队列（按受理顺序成交/失败/取消，"
        "候补可设 1～1440 分钟申请有效期，到期处理队首时标记 expired 并释放位置）；"
        "支持按地区+渠道临时冻结/恢复新额度发放（冻结期间拒绝新的直接"
        "发行、额度预留与候补受理；恢复时先按原受理顺序处理仍有效候补）；"
        "并支持同一素材/地区/渠道下两条授权间的未占用次数转拨"
        "（操作号幂等，转拨后同事务重算候补）。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details or [],
            }
        },
    )


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


# ------------------------------------------------------------------ 素材
@app.post(
    "/api/v1/materials",
    response_model=schemas.MaterialOut,
    status_code=201,
    tags=["materials"],
)
def create_material(
    body: schemas.MaterialCreate, db: Session = Depends(get_db)
):
    return services.create_material(db, body)


@app.get(
    "/api/v1/materials/{material_id}",
    response_model=schemas.MaterialOut,
    tags=["materials"],
)
def get_material(material_id: int, db: Session = Depends(get_db)):
    return services.get_material(db, material_id)


# ------------------------------------------------------------------ 授权
@app.post(
    "/api/v1/authorizations",
    response_model=schemas.AuthorizationOut,
    status_code=201,
    tags=["authorizations"],
)
def create_authorization(
    body: schemas.AuthorizationCreate, db: Session = Depends(get_db)
):
    return services.create_authorization(db, body)


@app.get(
    "/api/v1/authorizations/{authorization_id}",
    response_model=schemas.AuthorizationOut,
    tags=["authorizations"],
)
def get_authorization(authorization_id: int, db: Session = Depends(get_db)):
    return services.get_authorization(db, authorization_id)


@app.patch(
    "/api/v1/authorizations/{authorization_id}",
    response_model=schemas.AuthorizationOut,
    tags=["authorizations"],
)
def update_authorization(
    authorization_id: int,
    body: schemas.AuthorizationUpdate,
    db: Session = Depends(get_db),
):
    return services.set_authorization_status(
        db, authorization_id, body.status
    )


@app.post(
    "/api/v1/authorizations/migrate",
    response_model=schemas.AuthorizationMigrationOut,
    tags=["authorizations"],
    summary="授权退役改挂：原授权承载的全部未撤销申请一次性改挂到替代授权",
)
def migrate_authorization(
    body: schemas.AuthorizationMigrationRequest,
    db: Session = Depends(get_db),
):
    return services.migrate_authorization(db, body)


@app.post(
    "/api/v1/authorizations/transfer",
    response_model=schemas.QuotaTransferOut,
    tags=["authorizations"],
    summary=(
        "授权间额度转拨：同一素材/地区/渠道下把源授权未占用次数转拨到"
        "目标授权（操作号幂等，转拨后同事务重算候补）"
    ),
)
def transfer_authorization_quota(
    body: schemas.QuotaTransferCreate, db: Session = Depends(get_db)
):
    return services.transfer_quota(db, body)


# ------------------------------------------------------------------ 发行
@app.post(
    "/api/v1/distributions",
    response_model=schemas.DistributionOut,
    status_code=201,
    tags=["distributions"],
    summary="提交发行申请：整体核验并原子占用次数",
)
def create_distribution(
    body: schemas.DistributionCreate, db: Session = Depends(get_db)
):
    return services.create_distribution(db, body)


@app.get(
    "/api/v1/distributions/{distribution_id}",
    response_model=schemas.DistributionOut,
    tags=["distributions"],
)
def get_distribution(distribution_id: int, db: Session = Depends(get_db)):
    return services.get_distribution(db, distribution_id)


@app.post(
    "/api/v1/distributions/{distribution_id}/revoke",
    response_model=schemas.DistributionOut,
    tags=["distributions"],
    summary="撤销发行申请（仅可撤销一次，释放占用次数）",
)
def revoke_distribution(
    distribution_id: int, db: Session = Depends(get_db)
):
    return services.revoke_distribution(db, distribution_id)


@app.post(
    "/api/v1/distributions/{distribution_id}/reschedule",
    response_model=schemas.DistributionOut,
    tags=["distributions"],
    summary="改期：以新的带时区发行时刻调整已通过申请的档期（原子重选授权）",
)
def reschedule_distribution(
    distribution_id: int,
    body: schemas.DistributionReschedule,
    db: Session = Depends(get_db),
):
    return services.reschedule_distribution(db, distribution_id, body)


# ------------------------------------------------------------------ 预留
@app.post(
    "/api/v1/reservations",
    response_model=schemas.ReservationOut,
    status_code=201,
    tags=["reservations"],
    summary="预留发行额度：全部素材同时通过才生成预留编号并逐项占额",
)
def create_reservation(
    body: schemas.ReservationCreate, db: Session = Depends(get_db)
):
    return services.create_reservation(db, body)


@app.get(
    "/api/v1/reservations",
    response_model=list[schemas.ReservationOut],
    tags=["reservations"],
    summary="查询预留列表（可按 status 过滤：待确认/已确认/已取消/已过期）",
)
def list_reservations(
    status: str | None = None, db: Session = Depends(get_db)
):
    return services.list_reservations(db, status)


@app.get(
    "/api/v1/reservations/{reservation_id}",
    response_model=schemas.ReservationOut,
    tags=["reservations"],
)
def get_reservation(reservation_id: int, db: Session = Depends(get_db)):
    return services.get_reservation(db, reservation_id)


@app.post(
    "/api/v1/reservations/{reservation_id}/confirm",
    response_model=schemas.ReservationOut,
    tags=["reservations"],
    summary="到期前确认：原子转为一笔已通过申请（重复确认返回同一笔）",
)
def confirm_reservation(
    reservation_id: int, db: Session = Depends(get_db)
):
    return services.confirm_reservation(db, reservation_id)


@app.post(
    "/api/v1/reservations/{reservation_id}/cancel",
    response_model=schemas.ReservationOut,
    tags=["reservations"],
    summary="取消预留（与确认互斥，仅生效一次，释放预留额度）",
)
def cancel_reservation(
    reservation_id: int, db: Session = Depends(get_db)
):
    return services.cancel_reservation(db, reservation_id)


# ------------------------------------------------------------------ 候补
@app.post(
    "/api/v1/waitlists",
    response_model=schemas.WaitlistOut,
    status_code=201,
    tags=["waitlists"],
    summary=(
        "提交候补申请（含 1～1440 分钟申请有效期）：全部可承接则直接成交；"
        "每项均有匹配授权但额度不足则入本通道候补队列（仅在有效期内等待）；"
        "缺少匹配授权或有效期缺失/超范围则拒绝"
    ),
)
def create_waitlist(
    body: schemas.WaitlistCreate, db: Session = Depends(get_db)
):
    return services.create_waitlist(db, body)


@app.get(
    "/api/v1/waitlists",
    response_model=list[schemas.WaitlistOut],
    tags=["waitlists"],
    summary="查询候补列表（可按 status 过滤：待成交/已成交/已失败/已取消/已失效）",
)
def list_waitlists(
    status: str | None = None, db: Session = Depends(get_db)
):
    return services.list_waitlists(db, status)


@app.get(
    "/api/v1/waitlists/{waitlist_id}",
    response_model=schemas.WaitlistOut,
    tags=["waitlists"],
    summary="查询候补（状态、有效期/失效信息、失败原因及成交生成的申请编号）",
)
def get_waitlist(waitlist_id: int, db: Session = Depends(get_db)):
    return services.get_waitlist(db, waitlist_id)


@app.post(
    "/api/v1/waitlists/{waitlist_id}/cancel",
    response_model=schemas.WaitlistOut,
    tags=["waitlists"],
    summary="取消待成交候补（仅 pending 可取消；已成交的请撤销其申请）",
)
def cancel_waitlist(waitlist_id: int, db: Session = Depends(get_db)):
    return services.cancel_waitlist(db, waitlist_id)


# ----------------------------------------------------------- 通道额度冻结
@app.put(
    "/api/v1/channel-freezes",
    response_model=schemas.ChannelFreezeOut,
    tags=["channel-freezes"],
    summary=(
        "按地区+渠道冻结/恢复新额度发放：冻结须填写原因；"
        "恢复时先按原受理顺序处理本通道仍有效的候补，再允许新请求占额"
    ),
)
def set_channel_freeze(
    body: schemas.ChannelFreezeRequest, db: Session = Depends(get_db)
):
    return services.set_channel_freeze(db, body)


@app.get(
    "/api/v1/channel-freezes",
    response_model=schemas.ChannelFreezeOut,
    tags=["channel-freezes"],
    summary="查询通道冻结状态：当前状态、末次原因与变更时刻",
)
def get_channel_freeze(
    region: str, channel: str, db: Session = Depends(get_db)
):
    return services.get_channel_freeze_state(db, region, channel)
