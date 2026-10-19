# 部署链路（Deployment Pipeline）

本文档汇总「**AI 写字楼空间租赁管理系统**」从代码提交到生产部署的完整链路，
以及三个 Dashboard（Cloudflare Pages / Railway / GitHub）必要的配置与排障步骤。

> 一句话概览：**主仓只是 wrapper，子仓才是部署入口**。
> 任何代码改动都要推两次（子仓 + 本仓）。

---

## 1. 仓库结构

```
kongjianguanli001/        ← 主仓（记录 gitmodules + 文档，不部署）
├── apps/
│   ├── web/              ← 子仓 naban25/kongjianguanli-web → Cloudflare Pages
│   └── api/              ← 子仓 naban25/kongjianguanli-api → Railway
├── .github/workflows/    ← 主仓 CI：backend-tests.yml（lint + pytest）
├── scripts/              ← 推送 / 同步辅助脚本
└── *.md                  ← 项目文档（README / ARCHITECTURE / DEPLOYMENT / TESTING）
```

每个子仓都是**独立 git 仓库**，各自有自己的 GitHub Actions 工作流与 CI。

---

## 2. 三条部署链路

### 2.1 前端：apps/web → Cloudflare Pages

```
本地 → git push → naban25/kongjianguanli-web
   ↓ webhook
Cloudflare Pages:
   1. Clone 仓库
   2. Build command: cd apps/web && npm install && npm run build
   3. Output directory: apps/web/out
   4. Deploy
```

**前置门禁**：
- 子仓 `.github/workflows/build.yml` —— push / PR 自动跑 `npm run build`
- 本地 `.githooks/pre-push` —— main 分支 push 前本地先构建（参见 `apps/web/.githooks/README.md`）

**必要 Dashboard 配置（Cloudflare Pages → Settings → Environment variables）**：

| 变量 | 说明 | 示例 |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | 前端调用的后端根地址 | `https://api.<your-domain>.com` |

> ⚠ 静态导出模式下 `NEXT_PUBLIC_*` 必须 Dashboard 设置，
> 不能依赖 `.env.production`（被 .gitignore 忽略 + build context 不读）。

**Webhook 失效恢复**（已踩过）：
1. Dashboard → Deployments → "Create deployment" 手动触发
2. 或 Settings → Builds → "Retry latest deployment"

### 2.2 后端：apps/api → Railway

```
本地 → git push → naban25/kongjianguanli-api
   ↓ webhook
Railway:
   1. Wait for CI（GitHub Actions 后端测试全绿）
   2. Docker build（railway.json → Dockerfile）
   3. Deploy with health check at /health
```

**前置门禁**：
- 子仓 `.github/workflows/tests.yml` —— lint + pytest
- 主仓 `.github/workflows/backend-tests.yml` —— 主仓 pointer 变更时也跑后端测试

**必要 Dashboard 配置（Railway → Variables）**：

| 变量 | 是否必需 | 说明 |
|---|---|---|
| `DATABASE_URL` | ✅ | Neon PostgreSQL 连接串 |
| `LLM_PROVIDER` | ✅（即使是占位） | AI 模块开关；空字符串 = 禁用 LLM |
| `LLM_API_KEY` | 仅启用 LLM 时 | NVIDIA / OpenAI Key |
| `LLM_BASE_URL` | 仅自定义时 | 自定义 endpoint URL |
| `JWT_SECRET_KEY` | ✅ | JWT 签名密钥 |
| `RAILWAY_API_TOKEN` | 自动化部署时 | 部署 API 凭证（注意是 `_API_`）|
| `SCHEDULER_LOCK_PATH` | 否 | 默认 `/tmp/office_lease_scheduler.lock` |

**Webhook 偶发失效恢复**（已踩过）：
```bash
# 用 GraphQL API 重新连接 GitHub repo
mutation serviceConnect($id: String!, $input: ServiceConnectInput!) {
  serviceConnect(id: $id, input: $input) { id }
}
```
或 Dashboard → Settings → "Disconnect" → 重新连接 GitHub。

### 2.3 主仓：kongjianguanli001（同步无前后）

主仓**不直接部署**，只做两件事：
1. **管理 gitmodules**（apps/web / apps/api 的子模块声明）
2. **记录指针版本**（每次子仓更新后 bump pointer 到新 commit）

**何时需要 push 主仓**：
- 修改了根目录 `.md`、`.github/`、`scripts/`、`railway.json`、`.gitmodules`
- 子仓有新提交需要让主仓 pointer 反映

> ⚠ 如果只 push 子仓而不 bump 主仓 pointer，**主仓用户看到的依然是旧版本**。
> 历史教训：曾因漏 bump pointer 导致页面看不到更新。

---

## 3. 一键推送（推荐）

主仓根目录提供 `scripts/push-all.sh`，自动按顺序推三个仓并 bump 主仓指针：

```bash
# 在主仓根目录
scripts/push-all.sh "feat(tables): 新增按列类型筛选"
```

行为：
1. 检查三个仓的脏状态（有未提交则中止）
2. 子仓 apps/api 提交并 push（如果有变更）
3. 等 apps/api CI 全绿
4. 子仓 apps/web 提交并 push
5. 等 apps/web build workflow 全绿
6. 主仓 bump 子模块 pointer 并 push

详见 `scripts/push-all.sh` 顶部注释。

---

## 4. 子模块工作流（手动版）

如果 `push-all.sh` 不可用，按以下步骤：

```bash
# 1) 子仓：apps/api（后端）
cd apps/api
git add -A && git commit -m "feat: xxx"
git push origin main
# 观察 https://github.com/naban25/kongjianguanli-api/actions 直到 ✅
# 观察 Railway Dashboard 直到 "Deployed"

# 2) 子仓：apps/web（前端）
cd ../web
git add -A && git commit -m "feat: xxx"
git push origin main
# 观察 https://github.com/naban25/kongjianguanli-web/actions 直到 ✅
# 观察 Cloudflare Pages 直到 "Success"

# 3) 主仓：bump 指针
cd ../..
git add apps/api apps/web
git commit -m "chore(submodule): bump 子模块"
git push origin main
# 主仓的 CI（backend-tests）会再跑一次后端测试
```

---

## 5. 常见故障排查

| 现象 | 原因 | 修复 |
|---|---|---|
| Cloudflare Pages build "Success" 但线上是空页面 | Output dir 设成 `.next/` 而非 `out/` | Dashboard → Settings → Build → 改成 `apps/web/out` |
| Cloudflare Pages 405 / 404 静态资源 | 同上：部署了 `.next/` 缓存而非 `out/` | 同上 |
| Cloudflare Pages 报 `cannot use both "use client" and export function generateStaticParams()` | 客户端组件里同时导出 `generateStaticParams` | 拆出 server shell + client component |
| Cloudflare Pages 报 `dynamicParams: true cannot be used with output: export` | 静态导出不允许 `dynamicParams` | 删掉该行 |
| Cloudflare Pages 报 `Page missing generateStaticParams()` | `generateStaticParams() => []` 不被算有效 | 改为返回单个 dummy：`[{ orgId: "_" }]` |
| Cloudflare Pages 报 `useSearchParams should be wrapped in a suspense boundary` | `output: export` 下必须包 Suspense | 包 `<Suspense fallback={...}>` |
| Railway push 后不自动部署 | webhook 失效 | 用 `serviceConnect` GraphQL 重连 |
| 主仓看到的是旧版本 | 子模块 pointer 没 bump | 主仓 `git add apps/web apps/api && commit && push` |
| LLM 模块静默降级 | `.env` 里 `LLM_PROVIDER` 空 | 设一个占位值如 `nvidia` |

---

## 6. CI 矩阵

| 仓库 | Workflow | 触发 | 检查 |
|---|---|---|---|
| 主仓 | `backend-tests.yml` | push / PR to main | ruff + alembic upgrade + pytest |
| apps/web | `build.yml` | push / PR to main | npm ci + next lint + tsc + npm run build + artifact 上传 |
| apps/api | `tests.yml` | push / PR to main | ruff + alembic upgrade + pytest |

---

## 7. 配置审计

所有 Dashboard 必要的环境变量参见各子目录 `.env.example`：

- 主仓 `.env.example`
- `apps/web/.env.example`
- `apps/api/.env.example`

定期（建议每月）review Dashboard 实际配置与 `.env.example` 是否同步。

---

## 8. 相关文档

- [README.md](./README.md) — 项目总览与快速开始
- [ARCHITECTURE.md](./ARCHITECTURE.md) — 系统架构与模块划分
- [TESTING.md](./TESTING.md) — 测试策略与运行方式
- `apps/web/.githooks/README.md` — 本地 pre-push hook 使用说明
- `apps/api/.env.example` — 后端环境变量清单
- `apps/web/.env.example` — 前端环境变量清单