# 素材发行授权核验 HTTP API

为内容团队构建的素材发行授权核验服务：登记素材及其发行授权（**地区、渠道、带时区的左闭右开时段、可发行次数**），对携带多个素材的发行申请做**整体核验、原子占用**，支持已通过申请的查询、**一次性撤销（释放占用）**、**改期（更换发行时刻并原子重选授权）**、授权退役时的**一次性改挂**，支持发行前的**额度预留**（1～30 分钟有效期，待确认/确认/取消/过期全生命周期），以及额度不足时的**通道候补队列**（候补可设 **1～1440 分钟申请有效期**，按受理顺序成交/失败/取消/**到期失效**，新直接申请不抢候补额度）；支持发行团队**按地区 + 渠道临时冻结 / 恢复新额度发放**（冻结须填原因；冻结期拒绝一切新占额，既有操作不受影响且释放额度不成交候补；恢复时先按原受理顺序清理仍有效候补再放行新请求）；并支持运营在**同一素材/地区/渠道的两条授权间转拨未占用次数**（操作号幂等：同号同参重放返回首次结果、异参冲突；先结算到期预留并按原顺序处理候补上再判定可转出量，成功后同事务重算候补）。

- 技术栈：Python 3.11 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Pydantic v2
- 并发安全：数据库行锁 + 通道咨询锁 + 条件更新三重保障，**高并发下绝不超额、不重复释放、候补不重复成交、转拨不重复执行**
- 一键启动：`docker compose up`，自动等待数据库就绪并初始化表结构（旧库启动时幂等补齐预留字段与候补表，无需手工迁移）

---

## 1. 快速开始（Docker Compose）

```bash
# 可选：复制环境变量配置并按需修改
cp .env.example .env

# 构建并一键启动（自动建表初始化）
docker compose up --build
```

启动后：

| 服务 | 地址 |
| --- | --- |
| API 服务 | <http://localhost:8000> |
| 交互式文档 Swagger UI | <http://localhost:8000/docs> |
| ReDoc 文档 | <http://localhost:8000/redoc> |
| 健康检查 | `GET http://localhost:8000/health` |
| PostgreSQL | `localhost:5432`（宿主机映射，容器内为 `db:5432`） |

停止：

```bash
docker compose down          # 停止，保留数据卷
docker compose down -v       # 停止并删除数据库数据卷（清空全部数据）
```

---

## 2. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_USER` | `licensing` | 数据库用户名 |
| `POSTGRES_PASSWORD` | `licensing` | 数据库密码 |
| `POSTGRES_DB` | `licensing` | 数据库名 |
| `DB_HOST_PORT` | `5432` | PostgreSQL 映射到宿主机的端口 |
| `API_HOST_PORT` | `8000` | API 映射到宿主机的端口 |
| `DATABASE_URL` | `postgresql+psycopg://licensing:licensing@db:5432/licensing` | 应用侧数据库连接串（compose 中由上面三个变量拼装） |
| `DB_WAIT_TIMEOUT` | `30` | 启动时等待数据库就绪的最长秒数 |
| `DB_POOL_SIZE` | `10` | 连接池常驻连接数 |
| `DB_MAX_OVERFLOW` | `20` | 连接池溢出连接数 |
| `RESERVATION_TTL_MIN_MINUTES` | `1` | 预留有效期下限（分钟），仅可在 1～30 区间内收窄 |
| `RESERVATION_TTL_MAX_MINUTES` | `30` | 预留有效期上限（分钟），仅可在 1～30 区间内收窄 |

数据库表在应用启动事件中通过 `Base.metadata.create_all` 幂等创建，**无需手工执行迁移**。
预留功能新增的 `authorizations.reserved_count` 列与 `ck_auth_reserved_invariant`
约束、候补功能新增的 `waitlists` / `waitlist_items` 表、通道冻结功能
新增的 `channel_freezes` 表（每通道唯一行），以及额度转拨功能新增的
`quota_transfers` 表（操作号全局唯一），也会在启动时
对旧版本库**幂等补齐**（列/约束/表已存在则跳过）。

### 不用 Docker 的本地运行方式

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg://用户:密码@主机:5432/licensing"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 3. 接口一览

基准路径：`/api/v1`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/materials` | 登记素材（业务编号唯一） |
| `GET` | `/materials/{id}` | 查询素材 |
| `POST` | `/authorizations` | 为素材登记一条授权 |
| `GET` | `/authorizations/{id}` | 查询授权（含已用/剩余/预留次数） |
| `PATCH` | `/authorizations/{id}` | 启用/停用授权（仅影响新申请与新预留） |
| `POST` | `/authorizations/migrate` | **授权退役：原授权未撤销申请一次性改挂到替代授权** |
| `POST` | `/authorizations/transfer` | **授权间额度转拨：同一素材/地区/渠道下源授权未占用次数转拨到目标授权（操作号幂等）** |
| `POST` | `/distributions` | **提交发行申请：整体核验并原子占用** |
| `GET` | `/distributions/{id}` | 查询发行申请（含各项命中授权） |
| `POST` | `/distributions/{id}/revoke` | **撤销申请（仅可一次），释放占用次数** |
| `POST` | `/distributions/{id}/reschedule` | **改期：以新的带时区发行时刻调整档期，原子重选各项授权** |
| `POST` | `/reservations` | **预留发行额度：全部素材同时通过才生成预留编号并逐项占额** |
| `GET` | `/reservations` | 查询预留列表（`?status=pending/confirmed/cancelled/expired` 过滤） |
| `GET` | `/reservations/{id}` | 查询单个预留（读取时惰性结算到期状态） |
| `POST` | `/reservations/{id}/confirm` | **到期前确认：原子转为一笔已通过申请**（重复确认返回同一笔） |
| `POST` | `/reservations/{id}/cancel` | **取消预留**（与确认互斥，仅生效一次，释放预留额度） |
| `POST` | `/waitlists` | **提交候补申请（含 1～1440 分钟申请有效期）：可承接则直接成交；每项均有匹配授权但额度不足则入队** |
| `GET` | `/waitlists` | 查询候补列表（`?status=pending/fulfilled/failed/cancelled/expired` 过滤） |
| `GET` | `/waitlists/{id}` | 查询候补（状态、有效期/失效信息、失败原因及生成的申请编号） |
| `POST` | `/waitlists/{id}/cancel` | **取消待成交候补**（仅 `pending` 可取消；已成交的请撤销其申请；已失效返回 409） |
| `PUT` | `/channel-freezes` | **按地区+渠道冻结/恢复新额度发放**（冻结须填 `reason`） |
| `GET` | `/channel-freezes?region=&channel=` | **查询通道冻结状态**（当前状态、末次原因、变更时刻） |

### 核验与占用规则

1. 匹配维度：素材、`region`（地区）、`channel`（渠道）、发行时刻。
2. 时段语义：**左闭右开** `[starts_at, ends_at)`，命中条件
   `starts_at <= occur_at < ends_at`；所有时间必须携带时区（如 `+08:00`）。
3. 次数：授权含 `max_count` 可发行次数；每次整体通过的申请对命中授权占用 1 次。
   **未到期的待确认预留同样占额**（见下节），故新申请的可通过条件为
   `used_count + reserved_count < max_count`。
4. **重叠授权选择**：当一个素材存在多条匹配授权时，按
   **到期时间 `ends_at` 升序、到期相同再按授权编号 `id` 升序**，
   跳过已停用/无余量者，选择第一条可用授权（预留沿用同一顺序）。
5. **整体判定**：申请中的每个素材都必须有「匹配、启用、有余量」的授权，
   申请才整体通过并落库占用；**任一项不满足则整体拒绝、列明每项原因、不扣减任何次数**。
6. **停用语义**：停用的授权不参与新申请匹配，但**不影响已通过的申请**；
   已通过申请仍可查询、可撤销。
7. **撤销**：已通过申请仅可成功撤销一次；撤销后逐项释放占用次数，
   申请记录保留且状态变为 `revoked`，仍可查询。重复撤销返回 `409`。

### 发行申请改期规则

运营用**申请编号 + 新的带时区发行时刻**（`POST /distributions/{id}/reschedule`，
请求体 `{"occur_at": "..."}`）调整一笔**已通过（`approved`）**申请的档期；
**地区、渠道与素材清单不变**，仅更换发行时刻：

1. **同一瞬间幂等**：新时刻与当前时刻为同一瞬间（仅时区表示不同也算，
   如 `2026-10-15T12:00:00+08:00` 与 `2026-10-15T04:00:00Z`）时，
   **返回原申请、不变更任何次数**（该空操作不要求授权仍启用）。
2. **逐项重选**：否则按既有**左闭右开**匹配
   （`starts_at <= occur_at < ends_at`）与**重叠授权顺序**
   （到期早优先、其次编号小）为每个素材重选授权；**余量判定扣除本申请
   原占用**（该授权 `used_count` 先减回本申请占的 1 次，故 `max_count=1`
   的授权也能自我承接），**仍计入未到期预留**（`reserved_count` 正常
   占额），且**目标授权必须启用**。
3. **原子更新**：全部项都可承接才在一个事务内更新申请 `occur_at`、
   逐项明细归属与相关授权次数（归属不变的授权次数不动；归属变化的
   「原授权 `-1`、新授权 `+1`」），返回更新后的申请；**任一项失败则
   整体拒绝**（`reschedule_rejected`，逐项列明原因），原申请时刻、
   明细归属与所有授权次数均不变。
4. **已撤销申请拒绝改期**（409 `distribution_revoked`，即使新时刻为
   同一瞬间）；未知申请编号返回 404。
5. **预留确认生成的申请同样可改期**：预留的原始 `occur_at` 与确认记录
   （`confirmed_at`、关联申请编号）保留不变；预留查询（单条/列表）内嵌的
   申请展示改期后的**最新数据**。改期后的申请仍适用既有约定：可查询、
   可撤销（撤销释放改期后归属的授权）、可参与授权改挂。

### 授权退役改挂规则

授权需要退役时，运营用**原授权编号 + 替代授权编号**，把原授权承载的
**全部未撤销（`approved`）发行申请一次性改挂**到替代授权：

1. 两端授权编号必须**不同**，且**素材、地区、渠道完全一致**。
2. 替代授权必须处于 **`active` 启用**状态；其时段（左闭右开）必须
   **覆盖每条待迁移申请的发行时刻**；且**剩余次数足够一次性承接
   全部迁入占用**。
3. **任一条件不满足则整体拒绝**：不改动任何申请归属，原/替代两端
   次数与状态均不变（原授权不会被停用）。
4. **成功后**：原授权置为 `inactive`（停用），其占用次数按迁移条数
   迁出（归零的部分即迁移条数）；替代授权按迁移条数增加占用；
   返回 `migrated_count` 与两端授权（含各自剩余次数）。
5. **已撤销申请不迁移**：`revoked` 申请的明细仍归属原授权、状态不变。
   原授权若已无未撤销申请，迁移数为 `0`，原授权仍会被停用（幂等，
   可重复调用）。
6. 改挂后的申请在查询接口中显示为**替代授权**；仍可通过原
   `POST /distributions/{id}/revoke` 接口撤销，**撤销只释放替代授权
   一次**，原授权次数不受影响。
7. **预留阻断改挂**：原授权存在**未到期的待确认预留**时，改挂请求
   **整体拒绝**（`pending_reservations`，不改动任何归属、次数、状态）；
   待这些预留确认/取消/到期后再改挂即可。替代授权承接迁入占用时，
   其容量还要扣除自身未到期预留（`used + reserved + n <= max`）。

### 授权间额度转拨规则

运营需要在**同一素材、地区、渠道**的两条不同授权间调拨未占用的发行
次数时，用 `POST /authorizations/transfer`，请求体携带
**源授权编号、目标授权编号、正整数转拨次数与操作号**：

```json
{
  "source_authorization_id": 1,
  "target_authorization_id": 2,
  "count": 3,
  "operation_no": "OP-20261006-0001"
}
```

1. **受理条件**：两端授权**均启用（`active`）**且**通道未冻结**
   （冻结期间一律 422 `channel_frozen`，不重算、不改额）；
   两端**授权时段可以不同**；**既有申请与预留的归属不变**，转拨仅
   原子调整两端 `max_count`（源 `-count`、目标 `+count`），
   `used_count` / `reserved_count` 不因转拨本身变化。
2. **先结算再判定**：受理后先在通道咨询锁内**结算本通道到期预留**
   （归还 `reserved_count`）并**按原受理顺序处理候补**，再以源授权
   **`max_count - used_count - reserved_count`** 判定可转出量。
3. **整笔拒绝**（422 `quota_transfer_rejected`，两端额度均不变）：
   可转出量**不足**（`insufficient_quota`）、**转出后源额度非正**
   （`source_quota_not_positive`，要求 `max_count - count >= 1`）、
   源目标编号相同（`same_authorization`）、素材/地区/渠道**维度不符**
   （`dimension_mismatch`）、任一端未启用（`authorization_inactive`）；
   授权编号不存在返回 404 `not_found`。
4. **成功后同事务重算候补**：目标新增余量**优先供仍有效的队首**，
   过期或失配（授权不再匹配）的候补依既有规则出队
   （`expired` / `failed`）；快照在重算之后落库，故响应中的占用数
   可能已包含被新增余量成交的候补。
5. **响应**：操作号、转拨次数、首次转拨完成时刻，以及**两端转拨后的
   额度与占用快照**（`max_count` / `used_count` / `reserved_count`
   及派生余量 `remaining` / `reservable_remaining`）。
6. **操作号幂等**：`operation_no` 全局唯一。**相同操作号 + 相同参数**
   的重复请求**返回首次转拨结果**（200，快照原样返回，不重复转拨），
   且**重放不受随后的通道冻结或授权停用影响**；**相同操作号 + 不同
   参数**（源/目标/次数任一不同）返回 **409 `quota_transfer_conflict`**。
   仅成功转拨会落库操作号；被拒绝的请求不占用操作号，修正参数后可
   用同一操作号重新发起。
7. **并发安全**：转拨与本通道全部发行类写事务共用同一把通道咨询锁，
   两端授权按 id 升序行锁，额度搬移使用条件更新
   （`max - used - reserved >= count` 且 `max - count >= 1`）并断言
   影响行数；并发转拨**绝不超额**，并发同操作号**绝不重复转拨**
   （唯一约束兜底，跨通道同号亦然）。

### 额度预留规则

发行方可用**素材编号列表、地区、渠道、带时区发行时刻 `occur_at` 与
有效期 `ttl_minutes`（1～30 分钟）**预留发行额度：

1. **整体预留**：沿用现有授权匹配（素材/地区/渠道/左闭右开时段）与
   重叠授权选择顺序（到期早优先、其次编号小）。**所有素材同时通过**
   （匹配、启用、且 `used + reserved < max`）才生成预留编号、逐项记录
   命中授权、到期时刻（受理时刻 + `ttl_minutes`）与命中授权的当前
   **可预留余量** `reservable_remaining`；**任一素材失败则整体拒绝、
   逐项列明原因、不占任何额度**。
2. **额度模型（兼容）**：授权的 `used_count` / `remaining` 仍只统计
   已通过申请的**正式占用**，语义与旧版本完全一致；新增
   `reserved_count`（未到期待确认预留占用）与
   `reservable_remaining = max_count - used_count - reserved_count`。
   恒有 `used_count + reserved_count <= max_count`。**未到期预留同时
   参与新发行申请与其他预留的余量判定**，因此并发提交与预留合计绝不超额。
3. **到期与释放**：到期时刻为左闭右开语义——`now < expires_at` 才允许
   确认，**恰在到期时刻确认视为逾期**。到期释放采用**惰性结算**：任一
   通道写事务（提交/预留/确认/取消/撤销/改挂）与预留查询都会先在通道
   锁内把本通道已过期待确认预留标记为 `expired` 并逐项归还预留额度，
   因此预留**到期即释放**，对随后的判定立即可见，无需后台定时任务。
4. **确认** `POST /reservations/{id}/confirm`：到期前确认把预留**原子
   转为一笔已通过（`approved`）发行申请**（逐项 `reserved_count - 1`、
   `used_count + 1`，占用总量不变），预留记为 `confirmed` 并关联新申请
   编号。**重复确认返回同一笔申请**（幂等 200，不重复占用）。生成的
   申请可在既有 `/distributions` 接口查询、用 `/revoke` 撤销。
   **授权停用不影响已有预留的确认**：确认不重新匹配授权、不要求授权
   仍启用（停用只影响新申请/新预留的匹配）。
5. **取消** `POST /reservations/{id}/cancel`：取消与确认**互斥**。
   取消逐项释放预留额度、预留记为 `cancelled`，**仅生效一次**；
   **取消或过期之后的确认一律拒绝（409）**；对已取消预留重复取消
   返回 409 且**不再释放额度**；对已确认预留取消返回 409；
   对已过期预留取消返回 409（额度已由到期结算释放，不重复释放）。
6. **查询区分四态**：`pending`（待确认）、`confirmed`（已确认）、
   `cancelled`（已取消）、`expired`（已过期）。列表
   `GET /reservations?status=...` 按状态过滤；单条查询
   `GET /reservations/{id}` 会先做通道到期结算，逾期者落库为 `expired`。
7. 预留的地区/渠道/发行时刻在创建时确定；确认生成的申请**归属预留时
   命中的同一授权**，发行时刻沿用预留的 `occur_at`。

### 候补规则

发行团队可用与直接申请相同的字段（**素材编号列表、地区、渠道、带时区
发行时刻**）**外加申请有效期 `ttl_minutes`（1～1440 分钟，到期时刻
= 受理时刻 + `ttl_minutes`）**提交候补申请 `POST /waitlists`；系统先
沿用现有匹配及重叠授权选择规则逐项判定：

1. **全部可承接 → 直接成交**：与直接申请一致地原子占额并生成一笔
   `approved` 申请，候补记录为 `fulfilled` 并关联 `distribution_id`
   （响应内嵌该申请，同时回显 `ttl_minutes` 与 `expires_at`）。
2. **每项均有匹配授权但额度不足 → 入队**：仅当每个素材都命中「匹配、
   启用」的授权、纯粹因 `used + reserved = max` 无法承接时，才进入
   **本通道（region+channel）候补队列**，记录为 `pending` 并保存
   `ttl_minutes` / `expires_at`。**候补在队列中不占任何额度**，且
   **仅在申请有效期内等待成交**。
3. **缺少匹配授权 → 逐项拒绝**：任一项无匹配授权
   （`no_matching_authorization`）或匹配授权均已停用
   （`authorization_inactive`）时，整体拒绝（422 `waitlist_rejected`，
   `details` 逐项列明原因），**不入队、不占额**。
4. **字段校验先于业务核验**：`ttl_minutes` 缺失、非整数或不在
   **1～1440** 区间（以及无时区、重复/未知素材等既有非法字段）一律
   422 请求体校验拒绝，**不进入核验、不入队**。

**申请有效期与到期失效（左闭右开）**：到期判定为
`now < expires_at` 才可成交——**恰在到期时刻处理即视为已失效**。
过期状态采用**惰性结算**，不依赖后台定时任务：

- 任一通道写事务（提交/预留/确认/取消/撤销/改期/改挂/候补受理）在
  通道咨询锁内、处理队首之前，先把本通道 `expires_at <= now` 的
  待成交候补统一标记为 **`expired`**，记录**失效时刻** `expired_at`
  与**失效原因** `expiry_reason`（当前固定为 `ttl_expired`）；
- **处理队首时，已过期候补被标记 `expired`、释放其队列位置，再按
  原受理顺序尝试后续候补**；过期候补**不占任何额度**，其让开的额度
  可被后续有效候补或新直接申请使用；
- 单条查询 `GET /waitlists/{id}` 也会在通道锁内做一次有效期结算，
  逾期者立即落库为 `expired`；列表 `GET /waitlists` 为只读视图，
  对库内仍 `pending` 但已过有效期的候补按 `expired` **派生呈现**
  （不产生写事务）；
- 对已失效候补执行取消返回 **409 `waitlist_already_expired`**；
  有效期功能上线前的历史候补无 `expires_at`，不按有效期失效，
  其状态/成交语义保持兼容。

**队列处理（按受理顺序，`id` 升序）**：撤销申请、预留取消、预留到期
（惰性结算）、改期搬移、授权改挂**释放额度后**，以及**新直接申请/
新候补受理前**，都会在通道咨询锁内（先统一失效过期候补，再）重算队首：

- 队首**可整体承接**：原子占额并转为一笔 `approved` 申请
  （`fulfilled`，关联 `distribution_id`），继续处理下一队首；
- 队首**仍匹配但额度不足**：**停止本轮**（严格按受理顺序，不跳过
  队首看后续候补），队首留在队列等待下次释放；
- 队首**授权已不再匹配**（任一项无匹配授权或匹配授权均已停用，
  例如授权被改挂停用）：标记 `failed` 并记录 `failure_reason`，
  继续处理后续候补。

由此保证：**新直接申请永远不会抢走可成交候补的额度**——直接申请在
自身判定前先处理队列，可成交的候补已先占额，直接申请按剩余额度判定
（接口与返回与旧版本完全兼容）。候补成交沿用与直接申请一致的余量
口径（未到期预留同样占额）与重叠授权选择顺序（到期早优先、其次
编号小）。

**查询与取消**：

- `GET /waitlists/{id}` 返回候补状态、申请有效期（`ttl_minutes` /
  `expires_at`）、失效信息（`expired` 时的 `expired_at` 与
  `expiry_reason`）、`failure_reason`（失败原因）、`distribution_id`
  （成交生成的申请编号，有则**内嵌完整申请**）；`GET /waitlists`
  支持 `?status=pending/fulfilled/failed/cancelled/expired` 过滤。
- `POST /waitlists/{id}/cancel`：**仅 `pending` 可取消**（仅生效
  一次，重复取消 409）；已成交（409 `waitlist_already_fulfilled`，
  如需释放额度请撤销其申请）、已失败（409 `waitlist_already_failed`）、
  **已失效（409 `waitlist_already_expired`）**为终态不可取消。
- 候补成交生成的申请是普通的 `approved` 申请：**沿用现有撤销规则**
  （`POST /distributions/{id}/revoke`，仅可一次），撤销释放额度后
  同样会触发本通道队首重算；也可查询、改期、参与授权改挂。

### 通道额度冻结 / 恢复规则

发行团队可临时冻结某个**通道（`region` + `channel`）**的新额度发放，
用于合规检查、渠道异常处置等场景。通道以「该地区+渠道下存在授权」
为存在判据（授权被停用不影响通道归属）。

**接口**：

- `PUT /api/v1/channel-freezes`：请求体
  `{"region": "CN", "channel": "web", "action": "freeze",
  "reason": "合规检查临时冻结"}`；`action` 仅可 `freeze` / `resume`。
  `freeze` 必须携带**非空** `reason`（恢复时无需也不读取原因）。
  冻结与恢复均与该通道的发行类事务共用同一把**通道咨询锁**，同通道
  严格串行，其他通道完全不受影响。
- `GET /api/v1/channel-freezes?region=CN&channel=web`：返回
  `status`（`frozen` / `active`）、末次冻结 `reason`、最近一次变更
  时刻 `changed_at`、末次冻结/恢复时刻 `frozen_at` / `resumed_at`。
  从未冻结的通道返回 `status=active`、`reason=null`、`changed_at=null`。

**冻结期间（`frozen`）的语义**：

1. **新的直接发行（`POST /distributions`）、额度预留
   （`POST /reservations`）与候补受理（`POST /waitlists`，含直接成交
   路径）一律明确拒绝**：422 `channel_frozen`（响应 `details` 带回
   `region`/`channel`/`reason`），**不占用、不预留任何额度，候补也不入队**。
2. **既有业务不受影响**：已存在的待确认预留仍可**确认 / 取消**；
   已通过申请仍可**撤销 / 改期**；授权仍可**退役改挂**；待成交候补
   仍可**取消**。
3. **冻结期间释放的额度不得使候补成交**：上述任何操作（撤销、预留
   取消/到期、改期、改挂）释放的额度都**保留在通道内**，待成交候补
   保持 `pending`，不会因释放而翻转成 `fulfilled`。
4. **冻结不延长任何有效期**：到期预留仍按原 `expires_at` 惰性结算为
   `expired` 并归还预留额度（逾期确认/取消照常 409）；到期候补仍按
   原申请有效期在队首处理时标记 `expired`（记录失效时刻与
   `ttl_expired`）并释放队列位置。
5. 冻结期间**不做候补失败/成交判定**：例如授权在冻结期间被停用，
   候补齐备留在 `pending`，待恢复处理队首时再按既有规则判定
   `failed` 或重新选择替代授权成交。

**恢复（`resume`）的语义**：

1. 恢复在同一事务、同一把通道咨询锁内依次完成：①冻结期间到期的预留
   按原有效期结算释放；②冻结期间到期的候补统一标记 `expired` 并释放
   位置；③按**原受理顺序（id 升序）**处理本通道仍有效的候补——
   可整体承接者原子占额转申请（`fulfilled`）、授权不再匹配者标记
   `failed` 并继续后项、队首仍匹配但额度不足则停止本轮。
2. **候补处理先于一切恢复后的新请求占额**：恢复事务提交前，新的
   直接发行/预留/候补受理仍在通道锁后排队；恢复提交后它们进入时又会
   先重算队首。因此恢复释放的额度必然优先归仍有效的候补所有，新请求
   不会插队。
3. **幂等**：对已冻结通道再次 `freeze`（即使携带不同原因）、对已
   `active` 通道（含从未冻结）再次 `resume`，均为幂等空操作（200），
   **不改动 `reason` 与 `changed_at`**；冻结→恢复→再次冻结时才更新
   为新原因与新变更时刻。恢复后历史行保留，`reason` 仍展示末次冻结
   原因，`resumed_at` 记录末次恢复时刻。

**报错**：通道下不存在任何授权（冻结/恢复/查询均然）→ 422
`channel_not_found`；冻结缺少非空 `reason`、`action` 非法、缺少
`region`/`channel` → 422 请求体校验错误。冻结仅作用于指定的
`region`+`channel`，其他通道的发行、预留、候补照常受理与成交。

### 错误响应格式

所有业务错误统一为：

```json
{
  "error": {
    "code": "distribution_rejected",
    "message": "授权核验未通过，未占用任何发行次数",
    "details": [
      {
        "item_index": 1,
        "material_id": 8,
        "reason": "quota_exhausted",
        "message": "匹配授权（id=4）可发行次数已用尽：3/3"
      }
    ]
  }
}
```

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 422 | 请求体校验错误（FastAPI 默认格式） | 非法时段（`starts_at >= ends_at`）、时间无时区、次数非正整数、预留 `ttl_minutes` 不在 1～30、候补 `ttl_minutes` 缺失或不在 1～1440、冻结缺少非空 `reason`、冻结/恢复 `action` 非 `freeze`/`resume` 等 |
| 409 | `duplicate_material` | 素材业务编号已存在 |
| 422 | `unknown_material` | 登记授权/发行申请/预留引用了未知素材编号 |
| 422 | `duplicate_material_items` | 同一发行申请/预留中携带重复素材 |
| 422 | `distribution_rejected` | 核验未通过；`details` 按项列明原因（见下） |
| 422 | `reschedule_rejected` | 改期核验未通过；`details` 按项列明原因（见下），原记录与次数均不变 |
| 422 | `reservation_rejected` | 预留核验未通过；`details` 按项列明原因（见下），不占任何额度 |
| 422 | `migration_rejected` | 授权改挂核验未通过；`details` 列明原因（见下），归属与次数均不变 |
| 422 | `waitlist_rejected` | 候补核验未通过（存在缺少匹配授权的项）；`details` 逐项列明原因，不入队 |
| 409 | `already_revoked` | 对已撤销申请重复撤销 |
| 409 | `distribution_revoked` | 对已撤销申请改期 |
| 409 | `reservation_already_confirmed` | 对已确认预留执行取消 |
| 409 | `reservation_already_cancelled` | 重复取消已取消预留（不再释放额度） |
| 409 | `reservation_cancelled` | 已取消预留之后再确认 |
| 409 | `reservation_expired` | 到期时刻（含）之后确认，或对已过期预留取消 |
| 422 | `invalid_reservation_status` | 预留列表使用了不支持的状态过滤值 |
| 409 | `waitlist_already_fulfilled` | 对已成交候补执行取消（如需释放额度请撤销其申请） |
| 409 | `waitlist_already_cancelled` | 重复取消已取消候补 |
| 409 | `waitlist_already_failed` | 对已失败（授权不再匹配）候补执行取消 |
| 409 | `waitlist_already_expired` | 对已超过申请有效期失效（`expired`）的候补执行取消 |
| 422 | `invalid_waitlist_status` | 候补列表使用了不支持的状态过滤值 |
| 422 | `channel_not_found` | 冻结/恢复/查询的通道下不存在任何授权（region+channel 无授权） |
| 422 | `channel_frozen` | 通道冻结期间提交新的直接发行 / 额度预留 / 候补受理 / 额度转拨；明确拒绝、不占额、不入队，`details` 携带冻结原因 |
| 422 | `quota_transfer_rejected` | 额度转拨核验未通过；`details` 列明原因（见下），两端额度均不变 |
| 409 | `quota_transfer_conflict` | 相同操作号携带不同参数（源/目标/次数任一不同）的转拨请求 |
| 404 | `not_found` | 路径中的素材/授权/申请/预留/候补编号不存在（改挂时原/替代授权任一不存在；转拨时源/目标授权任一不存在） |

`distribution_rejected` / `reschedule_rejected` / `reservation_rejected`
/ `waitlist_rejected` 的逐项原因 `reason`：

- `no_matching_authorization`：没有同时匹配地区、渠道与发行时刻的授权
- `authorization_inactive`：匹配的授权均已停用（停用只影响新申请/新预留/改期重选/候补受理与成交）
- `quota_exhausted`：匹配授权的可发行/可预留次数已用尽（正式占用 + 未到期预留）

候补的终态失败原因 `failure_reason`（重算队首时授权已不再匹配）：
`no_matching_authorization` 或 `authorization_inactive`。

候补的终态失效原因 `expiry_reason`（处理队首时已过申请有效期）：
当前固定为 `ttl_expired`，同时记录失效时刻 `expired_at`。

`migration_rejected` 的原因 `reason`（`details` 中附带两端授权编号）：

- `same_authorization`：原授权与替代授权编号相同
- `dimension_mismatch`：两端素材、地区或渠道不一致
- `replacement_inactive`：替代授权未启用
- `period_not_covered`：替代授权时段未覆盖某些申请的发行时刻
- `insufficient_quota`：替代授权余量（扣除其未到期预留）不足以一次性承接全部迁入占用
- `pending_reservations`：原授权存在未到期的待确认预留，改挂整体拒绝

`quota_transfer_rejected` 的原因 `reason`（`details` 中附带两端授权编号）：

- `same_authorization`：源授权与目标授权编号相同
- `dimension_mismatch`：两端素材、地区或渠道不一致
- `authorization_inactive`：源或目标授权未处于启用状态
- `insufficient_quota`：源授权可转出量（`max_count - used_count - reserved_count`）不足
- `source_quota_not_positive`：转出后源授权额度将非正（要求 `max_count - count >= 1`）

---

## 4. 调用示例（curl）

```bash
# 1) 登记两个素材
curl -s -X POST http://localhost:8000/api/v1/materials \
  -H 'Content-Type: application/json' \
  -d '{"code": "VIDEO-0001", "name": "秋季宣传片"}'

curl -s -X POST http://localhost:8000/api/v1/materials \
  -H 'Content-Type: application/json' \
  -d '{"code": "IMG-0007", "name": "封面图"}'

# 2) 登记授权：中国大陆 / web 渠道 / 10 月（左闭右开，带时区）/ 3 次
curl -s -X POST http://localhost:8000/api/v1/authorizations \
  -H 'Content-Type: application/json' \
  -d '{
    "material_id": 1,
    "region": "CN",
    "channel": "web",
    "starts_at": "2026-10-01T00:00:00+08:00",
    "ends_at":   "2026-11-01T00:00:00+08:00",
    "max_count": 3
  }'

curl -s -X POST http://localhost:8000/api/v1/authorizations \
  -H 'Content-Type: application/json' \
  -d '{
    "material_id": 2,
    "region": "CN",
    "channel": "web",
    "starts_at": "2026-10-01T00:00:00+08:00",
    "ends_at":   "2026-11-01T00:00:00+08:00",
    "max_count": 3
  }'

# 3) 提交发行申请（两个素材同时通过才占用）
curl -s -X POST http://localhost:8000/api/v1/distributions \
  -H 'Content-Type: application/json' \
  -d '{
    "material_ids": [1, 2],
    "region": "CN",
    "channel": "web",
    "occur_at": "2026-10-04T15:30:00+08:00"
  }'
# 201：返回申请 id 及每个素材命中的授权（含剩余次数）

# 4) 查询已通过的申请
curl -s http://localhost:8000/api/v1/distributions/1

# 5) 撤销（仅可一次），释放占用次数
curl -s -X POST http://localhost:8000/api/v1/distributions/1/revoke
# 再撤销一次 -> 409 already_revoked

# 6) 停用授权（仅影响之后的新申请）
curl -s -X PATCH http://localhost:8000/api/v1/authorizations/1 \
  -H 'Content-Type: application/json' -d '{"status": "inactive"}'
curl -s -X PATCH http://localhost:8000/api/v1/authorizations/1 \
  -H 'Content-Type: application/json' -d '{"status": "active"}'

# 7) 授权退役改挂：把授权 1 承载的全部未撤销申请一次性改挂到授权 3
curl -s -X POST http://localhost:8000/api/v1/authorizations/migrate \
  -H 'Content-Type: application/json' \
  -d '{"source_authorization_id": 1, "replacement_authorization_id": 3}'
# 200：{"migrated_count": 2,
#       "source_authorization":      {... "used_count": 0, "remaining": 3, "status": "inactive"},
#       "replacement_authorization": {... "used_count": 2, "remaining": 8, "status": "active"}}

# 改挂后查询申请显示替代授权；仍可用原接口撤销，撤销只释放替代授权一次
curl -s http://localhost:8000/api/v1/distributions/1
curl -s -X POST http://localhost:8000/api/v1/distributions/1/revoke
```

### 改期操作示例（curl）

```bash
# 前提：申请 1 为 approved（地区 CN / 渠道 web / 素材清单不变）
# 13) 改期：把申请 1 调整到新的发行时刻（带时区）
curl -s -X POST http://localhost:8000/api/v1/distributions/1/reschedule \
  -H 'Content-Type: application/json' \
  -d '{"occur_at": "2026-10-20T09:00:00+08:00"}'
# 200：返回更新后的申请——occur_at 已变；每个素材按左闭右开匹配与
#      重叠授权顺序（到期早优先、其次编号小）重选授权，归属变化的项
#      「原授权 used_count-1、新授权 +1」已原子搬移，归属不变则次数不动。
#      余量判定已扣除本申请原占用，未到期预留仍占额，目标授权须启用。

# 14) 同一瞬间幂等：与当前时刻相同的瞬间（时区表示可不同）
curl -s -X POST http://localhost:8000/api/v1/distributions/1/reschedule \
  -H 'Content-Type: application/json' \
  -d '{"occur_at": "2026-10-20T01:00:00Z"}'   # 即 09:00+08:00
# 200：返回原申请，任何授权次数均不变

# 15) 失败与拒绝
# 任一项不可承接（无匹配/目标停用/余量不足）
#   -> 422 reschedule_rejected，details 逐项列明原因，原记录与次数均不变
# 已撤销的申请改期（即使同一瞬间）
#   -> 409 distribution_revoked
# 申请编号不存在 -> 404 not_found；occur_at 缺时区 -> 422

# 16) 由预留确认生成的申请同样可改期；预留的原始 occur_at 与确认记录保留，
#     预留查询内嵌的申请展示改期后的最新数据
curl -s -X POST http://localhost:8000/api/v1/distributions/2/reschedule \
  -H 'Content-Type: application/json' \
  -d '{"occur_at": "2026-10-21T10:00:00+08:00"}'
curl -s http://localhost:8000/api/v1/reservations/1
# reservation.occur_at 仍为预留原始时刻；distribution.occur_at 为改期后时刻
```

### 预留操作示例（curl）

```bash
# 8) 预留发行额度：素材 [1,2] / CN / web / 指定发行时刻 / 15 分钟有效
curl -s -X POST http://localhost:8000/api/v1/reservations \
  -H 'Content-Type: application/json' \
  -d '{
    "material_ids": [1, 2],
    "region": "CN",
    "channel": "web",
    "occur_at": "2026-10-04T15:30:00+08:00",
    "ttl_minutes": 15
  }'
# 201：返回预留 id、status=pending、expires_at（受理时刻+15min）、
#      逐项命中授权（含各自 reservable_remaining）；任一素材失败则
#      422 reservation_rejected 且整体不占额。

# 9) 查询预留（单条查询会先结算到期：逾期者返回 status=expired）
curl -s http://localhost:8000/api/v1/reservations/1

# 列表 + 按状态过滤：pending / confirmed / cancelled / expired
curl -s "http://localhost:8000/api/v1/reservations?status=pending"

# 10) 到期前确认：原子转为一笔 approved 申请；重复确认返回同一笔申请
curl -s -X POST http://localhost:8000/api/v1/reservations/1/confirm
# 200：status=confirmed，distribution_id=<新申请id>，并内嵌该申请；
#      授权 used_count+1、reserved_count-1（占用总量不变）。
# 逾时确认              -> 409 reservation_expired
# 取消后再确认          -> 409 reservation_cancelled
# 再调用一次 confirm    -> 200（幂等，同一个 distribution_id）

# 11) 取消预留（与确认互斥，仅生效一次，释放预留额度）
curl -s -X POST http://localhost:8000/api/v1/reservations/1/cancel
# 200：status=cancelled，reserved_count 逐项归还；
# 重复取消              -> 409 reservation_already_cancelled（不再释放）
# 已确认后取消          -> 409 reservation_already_confirmed
# 已过期后取消          -> 409 reservation_expired（额度已由到期结算释放）

# 12) 确认生成的申请仍是一笔普通申请，可用既有接口撤销（释放正式占用）
curl -s -X POST http://localhost:8000/api/v1/distributions/1/revoke
```

### 候补操作示例（curl）

```bash
# 前提：授权 1（素材 1 / CN / web / max_count=1）的唯一额度已被申请 1 占用

# 17) 提交候补：沿用素材/地区/渠道/带时区发行时刻，并带申请有效期
#     ttl_minutes（1～1440 分钟）。额度不足但每项均有匹配授权
#     -> 入队（201，status=pending）
curl -s -X POST http://localhost:8000/api/v1/waitlists \
  -H 'Content-Type: application/json' \
  -d '{
    "material_ids": [1],
    "region": "CN",
    "channel": "web",
    "occur_at": "2026-10-04T15:30:00+08:00",
    "ttl_minutes": 30
  }'
# 201：{"id": 1, "status": "pending", "ttl_minutes": 30,
#       "expires_at": "2026-10-04T07:55:00Z", "distribution_id": null,
#       "failure_reason": null, "expiry_reason": null, "expired_at": null,
#       "items": [{"item_index": 0, "material_id": 1}], ...}
# 若全部可承接则直接成交：status=fulfilled 且随附 distribution_id 与内嵌申请
#     （仍回显 ttl_minutes/expires_at）；
# 若任一项缺少匹配授权 -> 422 waitlist_rejected（details 逐项列明原因，不入队）；
# ttl_minutes 缺失或不在 1～1440 -> 422 请求体校验错误（同样不入队）。

# 18) 撤销占用额度的申请 -> 释放额度，队首候补原子占额自动成交
#     （须在候补有效期内；已过期的队首会先标记 expired 并释放位置）
curl -s -X POST http://localhost:8000/api/v1/distributions/1/revoke
curl -s http://localhost:8000/api/v1/waitlists/1
# {"id": 1, "status": "fulfilled", "distribution_id": 2, "failure_reason": null,
#  "fulfilled_at": "...", "distribution": {... "status": "approved" ...}, ...}

# 19) 成交后的申请沿用现有规则：可查询、可撤销（撤销又会触发队首重算）
curl -s http://localhost:8000/api/v1/distributions/2
curl -s -X POST http://localhost:8000/api/v1/distributions/2/revoke

# 19b) 到期失效：处理队首时已过有效期（expires_at <= now，左闭右开）
#      的候补标记 expired、记录失效时刻与原因并释放队列位置；
#      单条查询也会惰性结算（无需等待额度释放事件）。
curl -s http://localhost:8000/api/v1/waitlists/3
# {"id": 3, "status": "expired", "ttl_minutes": 5,
#  "expires_at": "2026-10-04T07:35:00Z", "expired_at": "2026-10-04T07:40:02Z",
#  "expiry_reason": "ttl_expired", "distribution_id": null, ...}
# 对已失效候补取消 -> 409 waitlist_already_expired
curl -s -X POST http://localhost:8000/api/v1/waitlists/3/cancel

# 20) 待成交候补可取消（仅 pending 可取消，仅生效一次）
curl -s -X POST http://localhost:8000/api/v1/waitlists \
  -H 'Content-Type: application/json' \
  -d '{"material_ids": [1], "region": "CN", "channel": "web",
       "occur_at": "2026-10-04T15:30:00+08:00", "ttl_minutes": 60}'  # 额度又已满 -> pending
curl -s -X POST http://localhost:8000/api/v1/waitlists/4/cancel
# 200：status=cancelled；重复取消 -> 409 waitlist_already_cancelled
# 已成交后取消 -> 409 waitlist_already_fulfilled（请改用撤销申请）
# 授权改挂停用导致不再匹配 -> 重算时 status=failed、failure_reason 记录原因，
# 对 failed 取消 -> 409 waitlist_already_failed
# 超过申请有效期 -> status=expired，对其取消 -> 409 waitlist_already_expired

# 21) 列表与状态过滤（含 expired）
curl -s "http://localhost:8000/api/v1/waitlists?status=pending"
curl -s "http://localhost:8000/api/v1/waitlists?status=fulfilled"
curl -s "http://localhost:8000/api/v1/waitlists?status=expired"
# 非法过滤值 -> 422 invalid_waitlist_status
```

### 通道额度冻结 / 恢复操作示例（curl）

```bash
# 22) 冻结通道 CN/web 的新额度发放（freeze 必须填写原因）
curl -s -X PUT http://localhost:8000/api/v1/channel-freezes \
  -H 'Content-Type: application/json' \
  -d '{"region": "CN", "channel": "web",
       "action": "freeze", "reason": "发行合规检查临时冻结"}'
# 200：{"region": "CN", "channel": "web", "status": "frozen",
#       "reason": "发行合规检查临时冻结",
#       "changed_at": "2026-10-06T03:00:00Z",
#       "frozen_at":  "2026-10-06T03:00:00Z", "resumed_at": null}

# 23) 查询当前状态、原因与变更时刻
curl -s "http://localhost:8000/api/v1/channel-freezes?region=CN&channel=web"

# 24) 冻结期间：新直接发行 / 新预留 / 候补受理全部明确拒绝（不占额、不入队）
curl -s -X POST http://localhost:8000/api/v1/distributions \
  -H 'Content-Type: application/json' \
  -d '{"material_ids": [1, 2], "region": "CN", "channel": "web",
       "occur_at": "2026-10-06T15:30:00+08:00"}'
# 422 channel_frozen：
# {"error": {"code": "channel_frozen",
#   "message": "通道已冻结新额度发放（region='CN', channel='web'），
#               新的直接发行、额度预留与候补受理均被拒绝；冻结原因：发行合规检查临时冻结",
#   "details": [{"region": "CN", "channel": "web",
#                "reason": "发行合规检查临时冻结"}]}}
# POST /reservations、POST /waitlists 同理返回 422 channel_frozen。

# 25) 冻结期间既有操作仍允许；释放的额度不会使候补成交
curl -s -X POST http://localhost:8000/api/v1/reservations/1/confirm   # 既有预留确认
curl -s -X POST http://localhost:8000/api/v1/reservations/2/cancel    # 既有预留取消
curl -s -X POST http://localhost:8000/api/v1/distributions/3/revoke   # 已通过申请撤销
curl -s -X POST http://localhost:8000/api/v1/distributions/4/reschedule \
  -H 'Content-Type: application/json' -d '{"occur_at": "2026-10-20T09:00:00+08:00"}'
curl -s -X POST http://localhost:8000/api/v1/waitlists/5/cancel       # 候补取消
curl -s -X POST http://localhost:8000/api/v1/authorizations/migrate \
  -H 'Content-Type: application/json' \
  -d '{"source_authorization_id": 1, "replacement_authorization_id": 3}'  # 授权改挂
# 即使撤销/取消/到期/改期/改挂释放了额度，候补仍保持 pending；
# 到期预留与候补仍按各自原有效期结算（expired），冻结不延长有效期。

# 26) 重复冻结：幂等，原因与 changed_at 均不变化（即使携带新原因）
curl -s -X PUT http://localhost:8000/api/v1/channel-freezes \
  -H 'Content-Type: application/json' \
  -d '{"region": "CN", "channel": "web", "action": "freeze", "reason": "另一个原因"}'
# 200，reason/changed_at 与 22) 完全相同

# 27) 恢复：同一事务内先结算到期预留/候补，再按原受理顺序处理仍有效候补，
#     然后才允许新请求占额
curl -s -X PUT http://localhost:8000/api/v1/channel-freezes \
  -H 'Content-Type: application/json' \
  -d '{"region": "CN", "channel": "web", "action": "resume"}'
# 200：{"status": "active", "reason": "发行合规检查临时冻结",
#       "changed_at": "2026-10-06T04:00:00Z",
#       "frozen_at":  "2026-10-06T03:00:00Z",
#       "resumed_at": "2026-10-06T04:00:00Z"}
# 冻结期间释放的额度在此刻按原受理顺序优先归仍有效的候补：
curl -s http://localhost:8000/api/v1/waitlists/6
# {"id": 6, "status": "fulfilled", "distribution_id": 7, ...}
# 队首已过期 -> expired 释放位置；授权不再匹配 -> failed 继续后项；
# 队首仍匹配但额度不足 -> 停止，新请求同样排在其后。
# 重复 resume（含从未冻结的通道）-> 200 幂等，changed_at 不变。

# 28) 报错：无授权通道 / 缺原因 / 非法 action
curl -s -X PUT http://localhost:8000/api/v1/channel-freezes \
  -H 'Content-Type: application/json' \
  -d '{"region": "ZZ", "channel": "x", "action": "freeze", "reason": "r"}'
# 422 channel_not_found（resume 与 GET 查询同理）
curl -s -X PUT http://localhost:8000/api/v1/channel-freezes \
  -H 'Content-Type: application/json' \
  -d '{"region": "CN", "channel": "web", "action": "freeze"}'
# 422 请求体校验错误：冻结必须填写原因（reason 为非空字符串）
curl -s -X PUT http://localhost:8000/api/v1/channel-freezes \
  -H 'Content-Type: application/json' \
  -d '{"region": "CN", "channel": "web", "action": "pause", "reason": "r"}'
# 422 请求体校验错误：action 仅可 freeze/resume
# 其他通道（如 CN/app、US/web）不受 CN/web 冻结影响，照常受理与成交。
```

### 授权间额度转拨操作示例（curl）

```bash
# 29) 前提：授权 1 与授权 2 同素材、同地区、同渠道（时段可以不同），
#     均处于启用状态且通道未冻结。
#     把授权 1 的 3 次未占用额度转拨到授权 2（操作号全局唯一）
curl -s -X POST http://localhost:8000/api/v1/authorizations/transfer \
  -H 'Content-Type: application/json' \
  -d '{
    "source_authorization_id": 1,
    "target_authorization_id": 2,
    "count": 3,
    "operation_no": "OP-20261006-0001"
  }'
# 200：{
#   "operation_no": "OP-20261006-0001",
#   "count": 3,
#   "created_at": "2026-10-06T05:00:00Z",
#   "source_authorization": {"authorization_id": 1, "max_count": 7,
#     "used_count": 2, "reserved_count": 1,
#     "remaining": 5, "reservable_remaining": 4},
#   "target_authorization": {"authorization_id": 2, "max_count": 8,
#     "used_count": 0, "reserved_count": 0,
#     "remaining": 8, "reservable_remaining": 8}
# }
# 说明：仅两端 max_count 原子调整（源 -3、目标 +3）；既有申请与预留的
#       归属不变（used_count/reserved_count 不因转拨本身变化）。

# 30) 幂等重放：相同操作号 + 相同参数 -> 返回首次转拨结果（不重复转拨），
#     即使随后通道被冻结、授权被停用，重放仍返回首次结果
curl -s -X POST http://localhost:8000/api/v1/authorizations/transfer \
  -H 'Content-Type: application/json' \
  -d '{
    "source_authorization_id": 1,
    "target_authorization_id": 2,
    "count": 3,
    "operation_no": "OP-20261006-0001"
  }'
# 200：响应与 29) 完全一致

# 31) 相同操作号 + 不同参数（源/目标/次数任一不同）-> 409 冲突
curl -s -X POST http://localhost:8000/api/v1/authorizations/transfer \
  -H 'Content-Type: application/json' \
  -d '{
    "source_authorization_id": 1,
    "target_authorization_id": 2,
    "count": 5,
    "operation_no": "OP-20261006-0001"
  }'
# 409 quota_transfer_conflict

# 32) 整笔拒绝（两端额度均不变）：
#   可转出量不足（max-used-reserved < count）-> 422 insufficient_quota
#   转出后源额度非正（max-count < 1）        -> 422 source_quota_not_positive
#   源目标编号相同                           -> 422 same_authorization
#   素材/地区/渠道不一致                     -> 422 dimension_mismatch
#   任一端未启用                             -> 422 authorization_inactive
#   通道冻结中                               -> 422 channel_frozen
#   授权编号不存在                           -> 404 not_found
#   次数非正整数 / 操作号缺失或为空           -> 422 请求体校验错误
curl -s -X POST http://localhost:8000/api/v1/authorizations/transfer \
  -H 'Content-Type: application/json' \
  -d '{
    "source_authorization_id": 1,
    "target_authorization_id": 2,
    "count": 99,
    "operation_no": "OP-20261006-0002"
  }'
# 422 quota_transfer_rejected：
# {"error": {"code": "quota_transfer_rejected",
#   "message": "额度转拨核验未通过，两端授权额度均未变更",
#   "details": [{"reason": "insufficient_quota",
#     "message": "源授权（id=1）可转出量不足：请求 99 次，可转出 4 次
#                 （max_count 7 - used_count 2 - reserved_count 1）",
#     "source_authorization_id": 1, "target_authorization_id": 2}]}}
# 被拒绝的请求不占用操作号：修正参数后可用同一操作号重新发起。
```

候补成交响应示例（注意 `distribution_id`、内嵌的完整申请，
以及申请有效期 `ttl_minutes` / `expires_at`）：

```json
{
  "id": 1,
  "region": "CN",
  "channel": "web",
  "occur_at": "2026-10-04T15:30:00+08:00",
  "ttl_minutes": 30,
  "expires_at": "2026-10-04T08:01:00Z",
  "status": "fulfilled",
  "failure_reason": null,
  "expiry_reason": null,
  "distribution_id": 2,
  "created_at": "2026-10-04T07:31:00Z",
  "fulfilled_at": "2026-10-04T07:40:00Z",
  "failed_at": null,
  "cancelled_at": null,
  "expired_at": null,
  "items": [{"item_index": 0, "material_id": 1}],
  "distribution": {
    "id": 2,
    "region": "CN",
    "channel": "web",
    "occur_at": "2026-10-04T15:30:00+08:00",
    "status": "approved",
    "created_at": "2026-10-04T07:40:00Z",
    "revoked_at": null,
    "items": [
      {
        "item_index": 0,
        "material_id": 1,
        "authorization": {"id": 1, "max_count": 1, "used_count": 1, "remaining": 0}
      }
    ]
  }
}
```

候补到期失效响应示例（`status=expired`，返回有效期、失效时刻与原因；
无成交申请）：

```json
{
  "id": 3,
  "region": "CN",
  "channel": "web",
  "occur_at": "2026-10-04T15:30:00+08:00",
  "ttl_minutes": 5,
  "expires_at": "2026-10-04T07:35:00Z",
  "status": "expired",
  "failure_reason": null,
  "expiry_reason": "ttl_expired",
  "distribution_id": null,
  "created_at": "2026-10-04T07:30:00Z",
  "fulfilled_at": null,
  "failed_at": null,
  "cancelled_at": null,
  "expired_at": "2026-10-04T07:40:02Z",
  "items": [{"item_index": 0, "material_id": 1}],
  "distribution": null
}
```

预留成功响应示例（注意授权对象新增的 `reserved_count` 与
`reservable_remaining`；正式占用字段 `used_count`/`remaining` 语义不变）：

```json
{
  "id": 1,
  "region": "CN",
  "channel": "web",
  "occur_at": "2026-10-04T15:30:00+08:00",
  "ttl_minutes": 15,
  "expires_at": "2026-10-04T07:45:00Z",
  "status": "pending",
  "created_at": "2026-10-04T07:30:00Z",
  "confirmed_at": null,
  "cancelled_at": null,
  "expired_at": null,
  "distribution_id": null,
  "distribution": null,
  "items": [
    {
      "item_index": 0,
      "material_id": 1,
      "authorization": {
        "id": 1, "material_id": 1, "region": "CN", "channel": "web",
        "starts_at": "2026-10-01T00:00:00+08:00",
        "ends_at": "2026-11-01T00:00:00+08:00",
        "max_count": 3,
        "used_count": 0,
        "remaining": 3,
        "reserved_count": 1,
        "reservable_remaining": 2,
        "status": "active",
        "created_at": "2026-10-01T00:00:00Z"
      }
    }
  ]
}
```

成功的发行申请响应示例：

```json
{
  "id": 1,
  "region": "CN",
  "channel": "web",
  "occur_at": "2026-10-04T15:30:00+08:00",
  "status": "approved",
  "created_at": "2026-10-04T07:30:00Z",
  "revoked_at": null,
  "items": [
    {
      "item_index": 0,
      "material_id": 1,
      "authorization": {
        "id": 1, "material_id": 1, "region": "CN", "channel": "web",
        "starts_at": "2026-10-01T00:00:00+08:00",
        "ends_at": "2026-11-01T00:00:00+08:00",
        "max_count": 3, "used_count": 1, "remaining": 2,
        "status": "active",
        "created_at": "2026-10-04T07:00:00Z"
      }
    }
  ]
}
```

---

## 5. 并发控制设计

所有写事务在单个数据库事务内完成，并遵守统一的全局加锁顺序，
**从根上杜绝跨事务死锁**：

```
通道咨询锁 pg_advisory_xact_lock(region+channel)
        → 通道冻结状态判定（frozen 闸门）
        → 到期预留惰性结算（reservations / authorizations）
        → 候补有效期失效结算（expires_at <= now 的 pending → expired）
        → 候补队列处理（waitlists，受理顺序逐队首；冻结通道仅失效不成交）
        → 冻结/恢复行 / 预留/申请/候补/转拨行锁（id 升序）
        → 授权行锁（authorizations，id 升序）
        → 明细行锁（distribution/reservation/waitlist_items，id 升序）
```

1. **通道咨询锁 `pg_advisory_xact_lock`**：按 `region+channel` 分桶，
   同一通道内的提交、预留、确认、取消、撤销、改期、改挂、候补受理与
   候补成交、**额度转拨**，以及**通道冻结 / 恢复**由此串行判定，消除
   「各自读到有余量、先后扣减导致超额」以及「确认/取消/改期/改挂/
   成交/转拨交错读到一半旧状态」的竞态；事务提交/回滚时自动释放
   （同事务内重入同把锁安全）。授权行只会被同通道事务竞争（匹配维度
   即 region+channel，改挂与转拨也要求两端通道一致），因此不同通道
   不争用同一授权行，跨桶不可能成等待环。冻结 / 恢复是**通道级状态
   翻转**（`channel_freezes` 每通道唯一行），在同一把锁内完成：冻结
   事务一旦提交，随后取得该锁的新发行/新预留/候补受理/转拨全部在
   业务判定前读到 `frozen` 而拒绝；恢复事务在同一事务/同一把锁内先
   结算到期、再按原受理顺序处理候补，**提交后才放行新请求**，故恢复
   释放的额度必然先归仍有效的候补。
2. **到期惰性结算**：拿到通道咨询锁后、业务判定之前，事务先把本通道
   `expires_at <= now` 的待确认预留 `FOR UPDATE` 锁定，再把这些预留
   涉及的授权与业务候选授权合并、按 id 升序一次性行锁，随后条件归还
   `reserved_count` 并把预留置为 `expired`。因此到期预留对随后的提交/
   预留余量判定立即可见（**到期即释放**），且到期释放与确认/取消在同
   通道内天然互斥，不会重复释放。无需后台定时任务。
3. **候补有效期失效与队列处理**：到期结算之后、重算队首之前，先把
   本通道 `expires_at <= now`（左闭右开）的待成交候补 `FOR UPDATE`
   锁定并统一标记为 `expired`（记录 `expired_at` 与 `ttl_expired`
   原因），它们不占额度、立即释放队列位置。随后在撤销、预留取消、
   改期搬移、改挂释放额度后，以及新直接申请/新候补受理前，在通道锁内
   按受理顺序（`id` 升序）`FOR UPDATE` 逐队首重算：可整体承接则条件
   占额并原子生成一笔 `approved` 申请（同一事务内状态翻转
   `pending → fulfilled`，**不会重复生成申请**）；额度仍不足则停止
   本轮；授权已不再匹配则标记 `failed` 并继续后项。新直接申请因此
   永远不会抢走可成交候补的额度，过期候补也不会阻塞后续候补/新申请。
   单条候补查询在通道锁内只做「有效期失效结算」（不重算成交/失败），
   使逾期者立即呈现；列表查询则只读派生 `expired` 状态。
4. **行锁 `SELECT ... FOR UPDATE`**：预留/申请/候补行锁串行化对同一
   对象的并发操作（确认与取消恰为一方 200、一方 409；并发双重确认
   均 200 但只生成一笔申请；并发双重取消恰为一次 200、一次 409；
   并发撤销与改期，撤销必先于改期生效；并发取消同一候补恰一次
   生效）；授权行始终按 `id` 全局升序加锁；改挂、撤销与改期还锁定
   相关**明细行**，固定「明细 → 授权」归属。
5. **条件更新**：正式占用 `used+reserved < max`、预留占用
   `used+reserved < max`、确认转账（`reserved >= 1` 再
   `used+reserved < max`）、取消/到期释放 `reserved >= n`、
   改挂迁入 `used+reserved <= max-n` / 迁出 `used >= n`、
   撤销释放 `used >= 1`、改期搬移「原授权 `used >= 1` 再新授权
   `used+reserved < max`」、候补成交占额 `used+reserved < max`、
   转拨源端 `max-used-reserved >= count` 且 `max-count >= 1`，
   全部断言影响行数，作为超额/重复释放的最后防线。
6. **转拨幂等**：`quota_transfers.operation_no` 全局唯一。受理前
   （无锁）与通道锁内各做一次同号预检，同号同参直接返回首次落库的
   快照（重放不受随后的冻结/停用影响），同号异参 409；并发同号在
   通道锁内串行化，跨通道同号由唯一约束兜底（失败方回滚后按既有
   记录重放/判冲突），绝不重复转拨。

核验/预留/改期/改挂不通过即 `ROLLBACK`，不产生任何次数或归属变化。任意时刻
都有不变量：

- `used_count` 恒等于归属该授权且处于 `approved` 状态的占用明细数；
- `reserved_count` 恒等于归属该授权且未到期待确认的预留明细数；
- `1 <= max_count`、`0 <= used_count`、`0 <= reserved_count`、
  `used_count + reserved_count <= max_count`（转拨只调整 `max_count`
  且源端保留至少 1 次，不破坏上述不变量）。

---

## 6. 测试

测试套件基于真实 PostgreSQL（非模拟），通过 FastAPI TestClient 发起 HTTP 请求：

- `tests/test_api.py`：43 项端到端用例，覆盖左闭右开边界、地区/渠道匹配、
  次数耗尽（含预留占额）、撤销释放、停用语义、整体拒绝原子性、重复素材、
  未知编号、重复撤销、重叠授权选择顺序等。
- `tests/test_migrate.py`：63 项授权退役改挂用例，覆盖正常改挂与次数搬移、
  查询显示替代授权、改挂后撤销只释放替代授权一次、已撤销不迁移、幂等空迁移、
  多素材批量、两端相同/维度不一致/替代停用/时段不覆盖（含左闭右开边界）/
  余量不足/**原授权有未到期预留（pending_reservations）**六类拒绝的原子性、
  404/422 等。
- `tests/test_reservation_api.py`：77 项预留端到端用例，覆盖整体预留与逐项
  原因、可预留余量、到期左闭右开与到期即释放、确认转申请/重复确认同一笔、
  取消与确认互斥/重复取消不重复释放、四态查询与过滤、停用不影响确认、
  改挂阻断、重叠选择顺序、TTL 1～30 边界等。
- `tests/test_reschedule.py`：99 项改期端到端用例，覆盖归属不变次数不动、
  归属搬移「原授权 -1、新授权 +1」、同一瞬间幂等（含授权停用下的空操作）、
  余量扣除本申请原占用（max=1 自我承接）、未到期预留仍占额、目标须启用、
  失败整体拒绝的原子性（无匹配/停用/满员/多素材部分失败）、已撤销 409、
  未知 404、左闭右开边界、预留确认申请改期（预留记录保留、关联申请展示
  最新）、改期后撤销/改挂约定，以及并发重复改期、改期与撤销竞争、
  改期与新申请竞争单槽（绝不超额、账实相符）。
- `tests/test_concurrency.py`：既有并发安全用例（20/30 并发抢占、多素材批量、
  撤销与新申请交错、并发双重撤销、**改挂与撤销并发、改挂与提交并发、
  并发重复改挂**），断言绝不超额、不重复释放且账实相符。
- `tests/test_reservations.py`：预留并发安全用例（预留与直接申请混合抢占、
  并发双重确认、确认与取消竞争、并发双重取消、确认与申请竞争容量、
  **改挂与新预留并发**、到期释放与逾期确认/新申请竞争、多素材并发预留），
  断言绝不超额、不重复释放且
  `used_count`/`reserved_count` 与有效明细账实相符。
- `tests/test_waitlist.py`：候补端到端与并发用例，覆盖直接成交/
  入队不占额/缺匹配逐项拒绝、撤销/预留取消/预留到期/改期/改挂触发的
  队首成交、FIFO 与队首阻塞（停止本轮）、授权不再匹配标记失败并继续
  后项、新直接申请不抢可成交候补额度、取消与状态查询过滤、成交申请
  沿用撤销规则、重叠授权选择顺序，以及并发受理+并发释放不重复成交、
  直接申请与候补混合并发不超额、并发重复取消恰一次生效；**另覆盖
  申请有效期 `ttl_minutes`（1～1440）字段回显与缺失/超范围 422、
  到期左闭右开边界、队首过期标记 `expired`（失效时刻/`ttl_expired`
  原因）并释放位置、其后有效候补按序成交、有效期内正常成交、查询惰性
  失效与列表派生 expired、取消过期 409、过期不占额不阻塞新申请、
  多笔过期一次性清理**，以及账实相符。
- `tests/test_channel_freeze.py`：111 项通道额度冻结/恢复端到端与并发
  用例，覆盖冻结/恢复/查询生命周期与幂等（重复同态不改原因/变更时刻、
  冻结→恢复→再冻结更新）、无授权通道/缺原因/非法 action 报错、冻结期
  新直接发行/新预留/候补受理全部拒绝且不占额不入队、冻结期既有预留
  确认/取消与申请撤销/改期/改挂/候补取消仍可用且释放额度不成交候补、
  冻结不延长预留/候补有效期（到期即 expired 且不成交、逾期操作 409）、
  恢复时按原受理顺序成交/失效释放/失败继续/队首不足停止、恢复与新申请
  并发时候补严格优先且绝不超额、冻结与发行并发串行、其他通道不受影响。
- `tests/test_transfer.py`：73 项授权间额度转拨端到端与并发用例，覆盖
  字段校验（正整数次数/操作号）、时段不同可转拨、快照与查询一致、
  既有申请与预留归属不变、可转出量边界（max-used-reserved）、不足/
  源额度非正/源目标相同/维度不符/未启用/冻结整笔拒绝且额度不变、
  到期预留先结算再判定、候补先按原顺序处理再判定、转拨后同事务重算
  候补（新增余量供队首、过期/失配出队）、幂等重放返回首次结果、
  异参 409、重放不受随后冻结/停用影响、并发不超额、并发同号同参
  不重复转拨、并发同号异参恰一笔生效，以及全量账实相符。

```bash
pip install -r requirements.txt httpx
# 指向任意可用 PostgreSQL 16
export DATABASE_URL="postgresql+psycopg://licensing@localhost:5432/licensing"
python3 -m tests.test_api
python3 -m tests.test_migrate
python3 -m tests.test_reservation_api
python3 -m tests.test_reschedule
python3 -m tests.test_concurrency
python3 -m tests.test_reservations
python3 -m tests.test_waitlist
python3 -m tests.test_channel_freeze
python3 -m tests.test_transfer
```

> 测试会先 `DROP` 再重建全部表，请勿指向含生产数据的数据库。

## 7. 项目结构

```
.
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── requirements.txt
├── README.md
├── app/
│   ├── main.py        # FastAPI 路由与异常处理
│   ├── config.py      # 环境变量配置（含预留 TTL 1～30 范围）
│   ├── database.py    # 引擎/会话/启动建表（旧库幂等补齐 reserved_count 与候补表）
│   ├── models.py      # Material / Authorization / Distribution(+Item) /
│   │                  # Reservation(+Item) / Waitlist(+Item) / ChannelFreeze / QuotaTransfer
│   ├── schemas.py     # 请求响应模型与时段/时区/TTL/冻结原因/转拨参数校验
│   ├── errors.py      # 业务错误码（预留四态、候补状态、通道冻结 channel_frozen/channel_not_found、
│   │                  # 转拨 quota_transfer_rejected/quota_transfer_conflict）
│   └── services.py    # 核验占用 / 预留 / 确认 / 取消 / 到期结算 /
│                      # 撤销 / 改期 / 改挂 / 候补受理与队列成交 /
│                      # 通道冻结恢复 / 授权间额度转拨（核心事务逻辑）
└── tests/
    ├── test_api.py
    ├── test_migrate.py
    ├── test_reservation_api.py
    ├── test_reschedule.py
    ├── test_reservations.py
    ├── test_waitlist.py
    ├── test_channel_freeze.py
    ├── test_transfer.py
    └── test_concurrency.py
```
