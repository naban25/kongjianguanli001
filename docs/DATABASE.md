# DATABASE

## 连接

```ini
DATABASE_URL = postgresql://app:change_me@localhost:5432/office_lease
```

- 后端账号 `app`，按最小权限原则只授予本项目所需的读写权限
- 连接池 `pool_size=10, max_overflow=20, pool_pre_ping=True`

## 迁移

```bash
alembic revision --autogenerate -m "<message>"   # 自动生成
alembic upgrade head                               # 应用
alembic downgrade -1                              # 回滚
```

迁移文件位于 `apps/api/alembic/versions/`，**提交到版本库**。

## 表清单（22）

### 组织 / 项目 / 楼宇
| 表 | 说明 |
|----|------|
| `organizations` | 顶层组织 |
| `projects` | 项目 / 楼宇 · 关联 organization |
| `buildings` | 楼宇 · 关联 project |

### 空间 / 租户 / 合同 / 费用 / 收款
| 表 | 说明 |
|----|------|
| `spaces` | 可出租空间（UUID 主键，空间编号在项目内唯一） |
| `tenants` | 租户 |
| `leases` | 租赁合同 |
| `charges` | 应收费用台账 |
| `payments` | 收款记录 |
| `payment_allocations` | 收款与费用的分配 |

### 用户 / RBAC
| 表 | 说明 |
|----|------|
| `app_users` | 系统用户（密码 bcrypt 哈希） |
| `user_project_roles` | 用户-项目-角色（唯一约束） |

### 导入管线
| 表 | 说明 |
|----|------|
| `import_jobs` | 批次元数据（状态机：uploaded→…→completed） |
| `import_sheets` | 批次中的工作表 |
| `import_rows` | 每行原始 JSON（`raw_values` JSONB，不可变） |
| `field_mappings` | 字段映射建议 + 用户确认 |

### 数据质量 / 审计 / AI / 导出
| 表 | 说明 |
|----|------|
| `data_quality_issues` | 缺失/重复/格式/关联/业务/状态 冲突 |
| `audit_logs` | 所有关键操作留痕 |
| `ai_conversations` | AI 对话会话 |
| `ai_messages` | 对话消息（用户/AI/工具） |
| `ai_tool_runs` | 每次工具调用的执行元数据 |
| `export_jobs` | 异步导出任务 |

## 核心约束

- 全部主键使用 PostgreSQL `UUID`（不可预测，避免顺序 ID 泄露）
- 金额字段一律 `NUMERIC(14, 2)`，**禁止浮点数**
- 日期：纯租赁日期用 `DATE`，操作时间用 `TIMESTAMPTZ`
- 外键关联明确；常用筛选字段（`project_id`、`status`、`due_date` 等）有索引
- 关键业务修改一律写 `audit_logs`，记录前值摘要
- 所有业务表必须带 `organization_id` + `project_id`（多租户隔离）
- 软删除优先（状态变更），避免不可恢复的硬删除
- SQLAlchemy naming convention 保证 Alembic 可生成稳定的 constraint 名称

## 数据范围权限（全部后端强制）

```python
def require_project_scope(project_id: str, user: AppUser, db: Session) -> bool:
    if user.is_system_admin:                # 系统管理员看全部
        return True
    return db.query(UserProjectRole)         # 其他人必须在 user_project_roles 中
        .filter(UserProjectRole.user_id == user.id,
                UserProjectRole.project_id == project_id)
        .first() is not None
```

所有 API 路由 / Agent 工具 / 导出任务 **共用此函数**。

## Demo 种子数据

通过 `apps/api/scripts/seed.py` 初始化：

| 实体 | 数量 | 备注 |
|------|------|------|
| Organization | 1 | Demo Organization |
| Project | 1 | Demo Tower A (DEMO-P01) |
| Building | 1 | Tower A |
| Space | 15 | A01~A05，3 层 × 5 间 |
| Tenant | 3 | Acme Inc. / Tech Startup / Creative Studio |
| Lease | 3 | 活跃租约（租金 ¥15k/月） |
| Admin User | 1 | admin / admin12345 |

实际业务演示请通过 Excel 导入管线（规格 §6）导入真实台账。
