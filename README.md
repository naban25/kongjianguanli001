# AI 写字楼空间租赁管理系统

一套**已生产部署**的写字楼空间租赁运营系统。以 Excel/CSV 导入为入口，以多维表格为工作界面，以受控 AI Agent 为自然语言查询入口，以 **APITable / 维格表** 云版为协作视图层。

## 文档导航

| 文档 | 内容 |
|---|---|
| [README.md](./README.md) | 本文档 — 项目总览与快速开始 |
| [DEPLOYMENT.md](./DEPLOYMENT.md) | **部署链路**：Cloudflare Pages / Railway / 子模块同步流程 / 排障 |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 系统架构、模块划分、数据模型 |
| [TESTING.md](./TESTING.md) | 测试策略、运行命令、CI 矩阵 |

## 仓库结构

主仓是 git submodules wrapper，**真正部署的是两个子仓**：

```
kongjianguanli001/           ← 主仓（文档 + wrapper）
├── apps/web/                ← 子仓 naban25/kongjianguanli-web → Cloudflare Pages
├── apps/api/                ← 子仓 naban25/kongjianguanli-api → Railway
└── scripts/                 ← 一键推送 + 配置检查
```

**推送流程**：参见 [DEPLOYMENT.md § 3](./DEPLOYMENT.md#3-一键推送推荐) 与 `scripts/push-all.sh`。

> **规格版本：V2.0 · 生产部署已连通 · 56 条真实业务数据**
>
> | 环境 | 地址 | 说明 |
> |---|---|---|
> | 生产前端 | https://office-lease.pages.dev | Cloudflare Pages + Next.js export |
> | 生产后端 | https://office-lease-production.up.railway.app | Railway + FastAPI + Neon PostgreSQL |
> | 维格表协作 | https://vika.cn 空间 `spcUHQubu1SWn` | 5 张 datasheet 已同步 |
> | 登录账号 | `admin` / `admin12345` | |

---

## 架构总览

```
┌──────────────────────────────────────────────────────────┐
│                     用户浏览器                              │
└────────────────────┬─────────────────────────────────────┘
                     │
        ┌────────────┴────────────┐
        ▼                         ▼
┌─────────────────────┐  ┌────────────────────────┐
│  Next.js 前端        │  │  vika.cn 维格表协作层    │
│  Cloudflare Pages   │  │  (只读视图，数据同步)     │
│  office-lease       │  │                         │
│  .pages.dev         │  │                         │
└──────────┬──────────┘  └────────────────────────┘
           │ REST API
           ▼
┌─────────────────────────────────┐
│  FastAPI 后端 Railway            │
│  office-lease-production         │
│  up.railway.app                  │
└──────────┬──────────────────────┘
           │
    ┌──────┴──────┐
    ▼              ▼
┌──────────────┐  ┌─────────────────────┐
│ Neon PostgreSQL│  │ vika.cn API          │
│ (托管云数据库)  │  │ (数据同步)           │
└──────────────┘  └─────────────────────┘
```

**数据源单一原则**：Neon PostgreSQL 是唯一主数据源；维格表是只读协作层，通过 `POST /api/v1/tables/sync-apitable` 触发单向全量同步。

---

## 业务数据（生产库）

| 业务表 | 记录数 | 关键字段 |
|---|---|---|
| 空间表 spaces | 19 | 空间编号、楼层、面积(㎡)、工位数、空间类型、状态 |
| 租户表 tenants | 18 | 租户名称、租户类型、统一社会信用代码、联系人、联系电话、行业、状态 |
| 租约表 leases | 19 | 合同编号、租户名称、空间编号、签约日期、起租日期、到期日期、月租金、押金、付款周期、状态 |
| 费用表 charges | 0 | 费用类型、计费起止、到期日、应收金额、状态 |
| 收款表 payments | 0 | 收款日期、收款金额、付款方式、流水号、状态 |

---

## 已实现功能

### 核心功能（生产可用）

| 模块 | 端点/入口 | 说明 |
|---|---|---|
| 认证 | `POST /api/v1/auth/login` | JWT + HttpOnly Cookie 双通道 |
| RBAC | `system_admin` / `project_admin` / `operator` | 全局 + 项目级数据范围 |
| 多维表格 | `GET /api/v1/tables/business:{entity}/records` | 5 张业务表统一 CRUD |
| **列级筛选** | `?filters=field:op:value` | 8 种操作符 eq/ne/gt/gte/lt/lte/like/in |
| **底部汇总** | `GET /api/v1/tables/{ref}/aggregate` | SUM/AVG/COUNT/MIN/MAX |
| **新增记录** | `POST /api/v1/tables/{ref}/records` | 必填字段校验 + enum 转换 |
| 审计日志 | `audit_logs` JSONB `after_summary` | 登录/导入/同步等留痕 |
| 出租率/应收款 | `GET /api/v1/metrics/occupancy` | 数量/面积/工位三种口径 |
| Excel/CSV 导入 | `/api/v1/imports/*` | 上传→解析→映射→预览→确认 |
| **维格表同步** | `POST /api/v1/tables/sync-apitable` | Neon → vika.cn 全量单向同步 |
| AI Agent | `POST /api/v1/assistant/conversations` | 规则型 Intent Router + 受控工具 |

### 前端增强（本次实现）

- **列可见性切换**：工具栏「列」按钮，系统字段默认隐藏
- **列级筛选面板**：点击列头漏斗图标，枚举列有快速筛选 chips
- **底部汇总面板**：表格下方蓝渐变卡片，所有数值/货币列 SUM + AVG
- **同步到维格表按钮**：工具栏中间，`configured=true` 时自动出现
- **同步结果 Toast**：工具栏下方显示每张表写入条数

---

## 部署架构

### 后端 Railway

- **项目**：Railway 项目 `b8a99788-3d02-46c9-b44a-baf523988de0`
- **服务**：`01985e63-3913-45fb-b216-5c5e3f6071ad`
- **部署命令**：
  ```bash
  RAILWAY_API_TOKEN=xxx railway up \
    --service 01985e63-3913-45fb-b216-5c5e3f6071ad --detach
  ```
- **当前环境变量**：
  | Key | Value | 说明 |
  |---|---|---|
  | `DATABASE_URL` | Neon PostgreSQL 连接串 | 托管云数据库 |
  | `SESSION_SECRET` | 随机值 | 32+ 字符 |
  | `JWT_SECRET_KEY` | 随机值 | 32+ 字符 |
  | `LLM_PROVIDER` | `nvidia` | NVIDIA Free Endpoint |
  | `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | NVIDIA 配置 | AI Agent 用 |
  | `APITABLE_TOKEN` | `uskF0UGtF8RY8GxkAKPIr79` | 维格表 API Token |
  | `APITABLE_SPACE_ID` | `spcUHQubu1SWn` | 维格表空间 ID |
  | `APITABLE_BASE_URL` | `https://api.vika.cn` | 国内版 |

### 前端 Cloudflare Pages

- **Account ID**：`03a8a46437afc2875676494b04793649`
- **Project**：`office-lease`
- **部署方式**：Next.js `output: "export"` → curl 直传 Pages API
- **构建命令**：
  ```bash
  cd apps/web && npx next build
  ```
- **API**：`https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/pages/projects/{PROJECT}`
- **Content-Type 坑**：新部署前给所有 `.html` 追一行注释改 hash（否则 Pages 缓存旧 octet-stream）

### 数据库 Neon PostgreSQL

- **迁移**：Alembic，已 22 表
- **连接**：Railway 环境变量 `DATABASE_URL`

---

## 目录结构

```
kongjianguanli001/
├── apps/
│   ├── api/                               # FastAPI 后端
│   │   ├── app/
│   │   │   ├── main.py
│   │   │   ├── models.py                  # 22 张表 SQLAlchemy
│   │   │   ├── core/
│   │   │   │   ├── config.py              # Settings（+ apitable_* env vars）
│   │   │   │   ├── database.py             # engine / session maker
│   │   │   │   ├── auth.py                 # JWT + RBAC helpers
│   │   │   │   └── logging_setup.py
│   │   │   ├── api/v1/
│   │   │   │   ├── tables.py              # 多维表格 + sync-apitable 端点
│   │   │   │   ├── auth.py / admin.py / imports.py / leases.py / ...
│   │   │   └── services/
│   │   │       ├── apitable_sync.py        # ⭐ 新增：vika.cn 同步（国际版/国内版双模式）
│   │   │       ├── import_pipeline.py
│   │   │       └── storage.py
│   │   └── requirements.txt
│   └── web/                               # Next.js 前端
│       ├── src/
│       │   ├── app/(workspace)/tables/page.tsx   # ⭐ 增强：筛选/汇总/同步
│       │   ├── lib/api.ts                # + syncApitable / apitableStatus
│       │   └── ...
│       └── out/                           # next export 产物（部署源）
├── docs/
│   ├── ARCHITECTURE.md
│   └── DATABASE.md
├── alembic.ini
└── README.md                              # 本文件
```

---

## API 概要（生产）

```
# 认证
POST   /api/v1/auth/login
POST   /api/v1/auth/logout
GET    /api/v1/auth/me

# 业务表统一入口
GET    /api/v1/tables?project_id=…
GET    /api/v1/tables/{ref}/records?project_id=…&page=1&page_size=200&filters=status:eq:active
POST   /api/v1/tables/{ref}/records?project_id=…
PATCH  /api/v1/tables/{ref}/records/{rid}?project_id=…
DELETE /api/v1/tables/{ref}/records/{rid}?project_id=…
GET    /api/v1/tables/{ref}/aggregate?project_id=…        # SUM/AVG/COUNT 数值列
GET    /api/v1/tables/{ref}/schema?project_id=…           # COLUMN_META

# 维格表同步
GET    /api/v1/tables/sync-apitable/status                 # 配置状态
POST   /api/v1/tables/sync-apitable?project_id=…          # 全量同步 5 张表
POST   /api/v1/tables/sync-apitable?project_id=…           # 单表同步
       └─ body: {"entity": "spaces"}

# 其他
GET    /api/v1/spaces /leases /metrics /imports /assistant /audit-logs
...
```

---

## 维格表同步：国际版 vs 国内版

| 差异点 | 国际版 `apitable.com` | 国内版 `vika.cn` |
|---|---|---|
| API base | `https://aitable.ai` | **`https://api.vika.cn`** |
| 字段 type | 数字常量（1/2/3/5...） | **字符串**（`"Text"` 等） |
| 国际版 | 需正确 property 格式 | **统一用 `"Text"`**（避免坑） |
| datasheet 列表 | `/fusion/v1/spaces/{id}/datasheets` | **`/fusion/v1/spaces/{id}/nodes`**（过滤 `type=Datasheet`） |
| 创建 records | `POST /batch_create` | **`POST /records`**（records=[] 数组） |
| 删除 records | `recordIds=a,b,c` query | 同左 |
| QPS 免费版 | 2 | 2 |

**自动检测**：代码根据 `APITABLE_BASE_URL` 是否含 `vika.cn` 自动选国内版/国际版逻辑。
国内版 Token 以 `usk` 开头，Space ID 以 `spc` 开头。

---

## 未实现 / 后续迭代

| 优先级 | 功能 | 说明 |
|---|---|---|
| P1 | 维格表字段类型自动修正 | API 创建只能用 Text，需要手动改 Number/SingleSelect/DateTime |
| P1 | 同步按钮国际化 | 当前只有中文 |
| P2 | 同步增量优化 | 每次全量清空重写 → 只同步 `updated_at` 变化的行 |
| P2 | 定时自动同步 | Railway cron 触发 / sync-apitable |
| P3 | 租户/费用/收款独立 CRUD 路由 | 模型已存在，路由待补 |
| P3 | 真实 LLM 接入 NVIDIA | 配置已有，规则降级 |
| P3 | Docker/docker-compose 本地部署 | 生产用 Railway，本地待补 |

---

## 测试 / 验收清单（V2.0）

- [x] Railway 后端健康检查 `/health`
- [x] Cloudflare Pages 前端可访问（content-type: text/html）
- [x] 管理员 `admin / admin12345` 登录
- [x] 多维表格 5 张业务表数据可见
- [x] 列级筛选：`filters=status:eq:active`
- [x] 底部汇总：aggregate 返回所有数值列 SUM/AVG
- [x] 新增记录：POST /records 完整 CRUD
- [x] 维格表 Token 验证：GET /fusion/v1/spaces 200
- [x] 维格表全量同步 5 张表 → 56 条 records written
- [x] 前端同步按钮出现 + 状态 Toast
