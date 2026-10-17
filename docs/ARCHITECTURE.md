# ARCHITECTURE

## 总体

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend (Next.js + Tailwind)     apps/web                     │
│  · 登录 / 工作台 / 空间 / 租赁 / 导入 / 多维表格 / AI 助手      │
│  · 通过 API Client 调用后端                                       │
└─────────────────────────┬───────────────────────────────────────┘
                          │ HTTP / JSON (Cookie + Bearer)
┌─────────────────────────▼───────────────────────────────────────┐
│  Backend (FastAPI + SQLAlchemy)    apps/api                      │
│                                                                  │
│  main.py                   ASGI 入口 · CORS · 中间件 · 异常处理     │
│  core/config.py            Pydantic Settings · 环境变量            │
│  core/database.py          引擎 · SessionLocal · naming convention │
│  core/auth.py              bcrypt · python-jose · CurrentUser 依赖  │
│                                                                  │
│  api/v1/                   所有 REST 路由 · 全部返回结构化错误       │
│  services/storage.py       本地私有存储 · SHA-256 · 随机 key       │
│  services/import_pipeline  xlsx/csv 解析 · 类型推断 · 规则型映射     │
│                                                                  │
│  Agent (规则型)              Intent Router → 受控 Tool             │
│  LLMProvider (抽象)          未配置时自动降级                     │
└─────────────────────────┬───────────────────────────────────────┘
                          │ psycopg3
┌─────────────────────────▼───────────────────────────────────────┐
│  PostgreSQL 16           office_lease                            │
│  22 表 · NUMERIC 金额 · JSONB 原始数据 · UUID 主键 · 迁移由 Alembic │
└─────────────────────────────────────────────────────────────────┘
```

## 模块边界

| 层 | 职责 | 不做 |
|----|------|------|
| FastAPI 路由 | 参数校验、权限依赖注入、返回统一格式 | 直接拼接 SQL；包含业务规则 |
| Services    | 导入管线、文件存储、LLM 抽象 | 不处理 HTTP 协议 |
| Models      | ORM 映射 | 业务逻辑、指标计算 |
| Metrics     | 出租率、应收款汇总 | 不接收任意参数化的业务表达式 |

## 数据流（导入）

1. `POST /api/v1/imports` (multipart)
2. `services/storage.save_uploaded_file` → 随机 key + SHA-256
3. `services/import_pipeline.start_import_job` → openpyxl / csv 解析
4. 每个 Sheet 创建 `ImportSheet` + 逐行 `ImportRow`（保留 `raw_values` JSONB）
5. 规则型字段映射 → `FieldMapping` 表（`confidence`、`mapping_method`）
6. 用户确认（PATCH mapping / POST preview）
7. `POST /imports/{id}/confirm` → `ImportJob.status=completed`
8. 原始数据 **永不** 被业务实体覆盖

## 数据流（Agent 查询）

1. `POST /assistant/conversations/{id}/messages`
2. 后端重新认证 + 项目范围校验
3. 规则型 Intent Router（关键词匹配）
4. 调用受控工具（`_tool_get_due_leases`、`_tool_search_spaces` …）
5. 每个工具返回结构化 JSON（`data` + `metric_definitions` + `warnings`）
6. 保存 `AIConversation` / `AIMessage` / `AIToolRun`
7. LLM 未配置 → 规则型降级，不伪造回答

## 技术选型

- **后端框架**：FastAPI 0.143 · 自动 OpenAPI · 类型校验
- **ORM**：SQLAlchemy 2.1 (async-ready, 同步模式)
- **数据库**：PostgreSQL 16 · JSONB / NUMERIC / UUID · Alembic 1.20 迁移
- **认证**：bcrypt 哈希 + python-jose (HS256) JWT · HttpOnly Cookie + Bearer 双模式
- **Excel**：openpyxl 3.1 (read_only=True) · CSV 多编码 fallback
- **Python**：3.14 (已验证 3.12/3.13 也兼容)
- **前端**：Next.js 15 (App Router) · Tailwind · 已初始化

## 未采用的替代及理由

- **SQLite**（V1）：PostgreSQL 原生 JSONB、NUMERIC、UUID 必需，已改用 PostgreSQL。
- **passlib**：与 bcrypt 5.x 不兼容，改为直接使用 bcrypt + python-jose。
- **Pandas**：大文件一次性载入内存风险高，openpyxl read_only=True 已足够。
- **NocoDB/Baserow**：依赖许可证、数据库版本、安全边界未完全验证，当前保持自研后端 + 后续评估嵌入。

## 关键安全决策

- 权限 **全部** 在后端校验；前端隐藏仅作体验优化
- 导出、Agent 工具、表格记录查询共用同一套 `require_project_scope`
- 敏感操作（登录、导入、修改）写 `audit_logs`
- 密钥通过 `.env` / 环境变量 · 日志打印前脱敏
- CSV / Excel 单元格永不作为代码执行
