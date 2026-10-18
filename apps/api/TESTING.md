# 测试规范

> 本项目的回归测试入口与约定。所有测试代码位于 `apps/api/tests/`。

## 一键运行

```bash
# 安装测试依赖（如未安装）
.venv/bin/pip install pytest pytest-asyncio httpx

# 应用最新迁移（首次运行）
PYTHONPATH=apps/api .venv/bin/alembic --config alembic.ini upgrade head

# 运行全部测试
cd apps/api && PYTHONPATH=. ../.venv/bin/python3 -m pytest -v
```

预计时长：~5–7 秒。19 用例 happy-path 涵盖 auth / assistant / scheduler admin 三个核心模块。

## 架构与约定

### 数据库隔离策略

**复用 dev DB + SAVEPOINT 回滚**

- 测试不创建独立 DB；测试直接连项目当前 PG (`office_lease`)，但每个用例的修改被一个 `SAVEPOINT` 包裹并在 `teardown` 时回滚。
- 实现见 `tests/conftest.py::db_session` fixture：使用 SQLAlchemy `begin_nested()` 创建 SAVEPOINT，并挂 `after_transaction_end` 事件以在每次内部 `commit` 后自动重建 SAVEPOINT。
- 优点：无需每次迁移、无需清理；缺点：要求 dev DB schema 与最新模型同步。

### 生命周期禁用

**禁用 APScheduler lifespan**

- `tests/conftest.py` 在模块级把 `app.router.lifespan_context` 替换为 noop，避免 TestClient 启动时拿 fcntl 锁并调度真实 job。
- 这样的好处：所有 `/admin/scheduler/*` 端点直接以 "非 primary worker" 状态运行，测试断言 `running=False`，与生产中 gunicorn 其他 worker 的视角一致。

**禁止 LLM 调用**

- 在 conftest 顶部硬覆盖环境变量：`LLM_PROVIDER=""`、`LLM_API_KEY=""`、`LLM_BASE_URL=""`。
- 这样所有 assistant 走规则回退路径，整套测试无外部依赖、无 flakiness。

### Auth fixture 链

```
client (TestClient, override get_db)
   └─ logged_in_client (登录 + cookie 注入)
        ├─ admin_user (在 db_session 内创建测试用户，事务回滚时自动清理)
        └─ db_session  ← 共享同一个 SAVEPOINT
```

## 已有测试套件

| 模块 | 测试文件 | 用例数 |
|------|----------|--------|
| auth 登录 | `test_auth.py` | 8 |
| assistant 意图路由 + SSE | `test_assistant.py` | 7 |
| scheduler admin 端点 | `test_scheduler_admin.py` | 4 |

## 编写新测试的 checklist

1. **复用 fixture**：直接 `def test_xxx(client, admin_user, db_session)` 即可获得隔离的 DB + TestClient。
2. **写用户**：使用 `from app.core.auth import hash_password` 与现有 `app_users` schema (columns: `id, username, email, password_hash, display_name, is_system_admin, status, created_at, updated_at`)。
3. **写业务数据**：优先用 ORM 模型 (`db_session.add(AppUser(...))`)，仅在 ORM 不可达时才走 raw SQL。
4. **断言期望状态码**：用 `assert resp.status_code in (200, ...)` 而非 `== 200`，以适应后续合理变化。
5. **流式端点**：用 `with client.stream("POST", ...) as resp:` 配合 `for raw in resp.iter_text():` 手动解析 SSE 帧（事件名 + data）—不要假设有现成 SSE parser。

## CI 集成（建议）

未来 GitHub Actions 最小任务：

```yaml
- name: Backend lint + tests
  run: |
    .venv/bin/pip install -r apps/api/requirements.txt pytest pytest-asyncio httpx
    cd apps/api && PYTHONPATH=. ../.venv/bin/python3 -m pytest -q
```

## 已知噪音（不会让测试变红）

- `SAWarning: nested transaction already deassociated from connection` — conftest 的 teardown 在极端时序下会重复 rollback，可安全忽略。
- `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated` — 由 starlette 内部触发，不影响测试通过。

## 为什么不把 LLM 真实测试？

受 NVIDIA 推理 endpoint 可用性与 flakiness 影响，CI 中跑真实 LLM 调用会大幅拖慢 suite 并引入 flaky 用例。建议：

- 用规则路径做 happy-path（当前做法）。
- LLM 流式相关测试在另开 `tests/test_llm_stream.py` 加 `@pytest.mark.llm_integration` skip-if-no-key 标记，CI 默认 skip，本地手动跑。

## 后续待补

- [ ] `tests/test_import_pipeline.py` — Excel 导入 happy-path + 关键字段映射验证
- [ ] `tests/test_organizations.py` — 多组织切换边界（当前 disable/switch 行为）
- [ ] `tests/test_payments.py` — 收款 → 分配 → 核销链路
- [ ] LLM 集成测试（默认 skip）
- [ ] GitHub Actions workflow 文件
