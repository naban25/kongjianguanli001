# 【交接文档】AI 写字楼空间租赁管理系统 — V2.0 生产就绪

> **用途**：复制本文件全部内容，作为新对话的初始 Prompt 粘贴进去。
> **状态**：2026-10-17 生产部署成功，56 条业务数据已同步维格表。

---

## 0. 一句话定位

一套写字楼空间租赁运营 SaaS，以 **Next.js + Cloudflare Pages** 为前端，**FastAPI + Railway** 为后端，**Neon PostgreSQL** 为数据库，**维格表 / vika.cn** 为协作视图层（单向同步）。真实生产数据：19 空间 / 18 租户 / 19 租约 = 56 条。

---

## 1. 访问地址

| 服务 | URL | 登录 |
|---|---|---|
| 前端（Cloudflare Pages） | https://office-lease.pages.dev | admin / admin12345 |
| 后端 API（Railway） | https://office-lease-production.up.railway.app | JWT Cookie 认证 |
| 后端健康检查 | https://office-lease-production.up.railway.app/health | 公开 |
| 维格表协作 | https://vika.cn → 空间 spcUHQubu1SWn | 用户自己的 vika.cn 账号 |

---

## 2. 关键部署凭据（railway token 从本地 ~/.railway/config.json 读取）

```bash
# Railway CLI（本机已登录）
RAILWAY_API_TOKEN=96069899-8126-4048-a5a7-9f409a7b4578

# Railway 项目 + 服务 ID
PROJECT_ID=b8a99788-3d02-46c9-b44a-baf523988de0
SERVICE_ID=01985e63-3913-45fb-b216-5c5e3f6071ad
# 重部署命令
RAILWAY_API_TOKEN=$RAILWAY_API_TOKEN railway up --service $SERVICE_ID --detach

# Cloudflare Pages 凭据
CF_ACCOUNT_ID=03a8a46437afc2875676494b04793649
CF_API_TOKEN=cfat_kC69uup7wR07J6dcoWmfGVEiIxPoDLNPSWQkp35ed4504989
CF_PROJECT=office-lease

# 维格表 vika.cn API
APITABLE_TOKEN=uskF0UGtF8RY8GxkAKPIr79
APITABLE_SPACE_ID=spcUHQubu1SWn
APITABLE_BASE_URL=https://api.vika.cn    # 国内版关键！不是 aitable.ai
```

---

## 3. 项目结构

```
当前工作目录（已 cd）：
/Users/naban-xgw/Documents/trae/空间管理网页/kongjianguanli001

apps/api/             FastAPI 后端（Python 3.12 on Railway）
  ├── requirements.txt          httpx 已在（维格表同步依赖）
  └── app/
      ├── main.py               ASGI 入口
      ├── models.py             22 张表 SQLAlchemy 模型
      ├── core/
      │   ├── config.py         Settings 类（+ APITABLE_* env vars）
      │   ├── database.py
      │   └── auth.py
      ├── api/v1/
      │   ├── tables.py        ⭐ 重点：业务表 CRUD + aggregate + filters + sync-apitable
      │   └── auth.py / admin.py / leases.py / imports.py / ...
      └── services/
          └── apitable_sync.py  ⭐ 重点：国际版/国内版双模式同步服务

apps/web/             Next.js 前端
  ├── src/lib/api.ts            + syncApitable / apitableStatus + TypeScript 接口
  ├── src/app/(workspace)/tables/page.tsx   ⭐ 重点：表格页（筛选/汇总/同步按钮）
  └── out/                      `next build` export 产物（Cloudflare Pages 部署源）

docs/                 ARCHITECTURE / DATABASE（待补）
alembic.ini           Neon PostgreSQL 迁移
README.md             主文档（已更新 V2.0）
DEPLOY.md             简略占位
```

---

## 4. 已部署的核心文件列表（部署前必须 `py_compile` 通过）

| 文件 | 作用 | 最近改了什么 |
|---|---|---|
| `apps/api/app/services/apitable_sync.py` | 维格表同步 | 重写：国际版/国内版双模式 |
| `apps/api/app/api/v1/tables.py` | 表格 + 同步端点 | + POST/GET sync-apitable；AuditLog.after_summary 不是 details |
| `apps/api/app/core/config.py` | 配置 | + apitable_token / space_id / base_url |
| `apps/web/src/app/(workspace)/tables/page.tsx` | 前端表格页 | + 同步按钮 + 状态 Toast + 底部汇总卡片 |
| `apps/web/src/lib/api.ts` | API 客户端 | + apitableStatus / syncApitable + TS 接口 |

---

## 5. 关键技术坑（已踩平）

### 5.1 维格表 vika.cn vs 国际版
- **国内版 API 域名是 `https://api.vika.cn`**（不是 vika.cn 也不是 aitable.ai）
- 字段 type 必须是**字符串** `"Text"`（不是数字 1）
- `datasheets` 列表走 `/fusion/v1/spaces/{id}/nodes`（过滤 `type=Datasheet`）
- 创建 records 走 `POST /records` + `records=[]` 数组（不是 `/batch_create`）
- 免费版 QPS 2，批量操作之间必须 `sleep(0.55)`

### 5.2 Railway CLI
- 必须用 `RAILWAY_API_TOKEN=xxx railway up`，不是 `RAILWAY_TOKEN`
- Railway variables set 之后必须 `railway up` 才会生效（不会自动热更新）
- `variables set` 第一次可能没写入（偶发），先 `railway variables --service <id>` 验证

### 5.3 Cloudflare Pages 部署
- 标准流程：拿 JWT → blake3 算 hash → assets/upload → upsert-hashes → deployments
- bash 沙箱网络偶尔断，强制 IPv4：`curl -4` + 大 `--max-time`
- **Content-Type 坑**：新部署前所有 `.html` 文件要追一行 `<!-- $(date +%s%N) -->`，否则 Pages 缓存旧 hash 返回 `application/octet-stream`
- `blake3-wasm` npm 包路径：从 `apps/web/` 目录运行 node（那里有 `node_modules/blake3-wasm`）

### 5.4 其他
- AuditLog 模型字段：`after_summary` JSONB（不是 `details`）
- Alembic 迁移：Neon PostgreSQL 上已 22 表，迁移在 `apps/api/alembic/`
- 前端 Next.js `output: "export"`（静态导出，不需要 Node 运行时）
- Python 语法检查命令：`py_compile.compile(f, doraise=True)`

---

## 6. 常用命令速查

```bash
# ---- 后端 Railway ----
# 查看健康
curl -s https://office-lease-production.up.railway.app/health

# 登录拿 token
TOKEN=$(curl -s -X POST .../auth/login -d '{"username":"admin","password":"admin12345"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 重新部署
RAILWAY_API_TOKEN=96069899-... railway up --service 01985e63-... --detach

# 设置环境变量
RAILWAY_API_TOKEN=... railway variables set --service <id> APITABLE_BASE_URL=https://api.vika.cn

# 查部署日志
RAILWAY_API_TOKEN=... railway logs --service <id>

# ---- 维格表 vika.cn ----
# 直接 ping（验证 token）
curl -s "https://api.vika.cn/fusion/v1/spaces" -H "Authorization: Bearer uskF0UGtF8RY8GxkAKPIr79"

# 同步所有表（通过后端）
curl -s -X POST "https://office-lease-production.up.railway.app/api/v1/tables/sync-apitable?project_id=39f64935-cdde-4956-a909-8385b3530869" \
  -H "Authorization: Bearer $TOKEN" -d '{}'

# 同步单表
# body: {"entity": "spaces"}

# ---- Cloudflare Pages ----
# HTML content-type 修复 + 生成 hash
cd apps/web/out
find . -name '*.html' -type f -exec sh -c 'echo "<!-- $(date +%s%N) -->" >> "$1"' _ {} \;

# curl 分步部署
API="https://api.cloudflare.com/client/v4"
JWT=$(curl -s "$API/accounts/$ACCOUNT/pages/projects/$PROJECT/upload-token" -H "Authorization: Bearer $CF_TOKEN" | python3 -c "import sys,json;print(json.load(sys.stdin)['result']['jwt'])")
# 然后 upload → upsert-hashes → deployments（详见上一个 deploy 脚本）

# ---- 本地语法验证 ----
python3 -c "import py_compile; py_compile.compile('app/services/apitable_sync.py', doraise=True)"
```

---

## 7. 当前状态检查清单（新会话接手第一步）

按顺序执行这些检查确认环境完好：

```bash
# 1. Railway 后端在线 + env vars 生效
TOKEN=$(curl -s -X POST https://office-lease-production.up.railway.app/api/v1/auth/login \
  -H "Content-Type: application/json" -d '{"username":"admin","password":"admin12345"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s -H "Authorization: Bearer $TOKEN" \
  https://office-lease-production.up.railway.app/api/v1/tables/sync-apitable/status
# 期望: {"configured": true, "base_url": "https://api.vika.cn", ...}

# 2. 维格表 Token 仍有效
curl -s https://api.vika.cn/fusion/v1/spaces -H "Authorization: Bearer uskF0UGtF8RY8GxkAKPIr79" | python3 -c "import sys,json;print(json.load(sys.stdin)['success'])"
# 期望: True

# 3. 前端页面正常渲染 + 同步按钮存在
curl -sI https://office-lease.pages.dev/tables | head -1
# 期望: content-type: text/html; charset=utf-8

# 4. 真实业务数据还在（全链路）
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://office-lease-production.up.railway.app/api/v1/tables/business:spaces/aggregate?project_id=39f64935-cdde-4956-a909-8385b3530869"
# 期望: total=19, area_sqm.sum > 0
```

---

## 8. 接下来最想做的事（P1 优先级）

1. **维格表字段类型自动修正**：API 创建 datasheet 只能统一用 "Text"，但用户手动改成 Number/SingleSelect/DateTime 才能用数字求和、枚举筛选。可以：
   - 在同步完成后**自动追加修改字段类型的 API 调用**（vika.cn 有没有改字段类型的 API？需要探）
   - 或者前端显示提示 + 快捷复制按钮

2. **导出 CSV 时附带字段类型映射说明**：当前 `/tmp/apitable_export/*.csv` 已经生成（UTF-8 BOM），可以集成到系统导出功能

3. **定时增量同步**：用 Railway Schedules 每天凌晨自动同步

4. **维格表分享链接 iframe 嵌入**：在 `/tables` 页新增「协作视图」Tab，iframe 嵌入 vika.cn 的分享链接

---

## 9. Python 代码风格约定

- 类型注解（`dict`, `list[dict]`, `Optional[str]`）
- `from __future__ import annotations` 放文件首
- httpx 异步（后端用 FastAPI async 端点）
- 错误走 `raise APITableError("message", code=xxx)` 然后 HTTPException 转译
- AuditLog 用 `after_summary`（JSONB），不是 `details`

---

## 10. 备注

- Railway 生产项目路径：`/Users/naban-xgw/Documents/trae/空间管理网页/kongjianguanli001`（已 `cd`）
- Railway `.railway/config.json` 已配置，CLI 自动登录
- Cloudflare Pages 凭据已经在本文件，不要提交到 git
- 生产 DB 是 Neon PostgreSQL，**没有本地 PostgreSQL**（本地 venv 里装的是 httpx 等依赖）
- macOS 沙箱没有 Docker（不要尝试 `curl install.sh | bash` 部署 APITable 自托管）
