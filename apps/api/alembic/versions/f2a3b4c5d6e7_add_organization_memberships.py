"""add organization_memberships table

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-19 12:00:00.000000

说明
----
为了支持"每个组织下可包含多个项目、用户和角色配置"，新增用户-组织-角色关联表
``organization_memberships``。

迁移步骤：
1. 创建 organization_memberships 表（含唯一约束与外键）。
2. 数据回填：基于现有 ``user_project_roles`` → ``projects`` → ``projects.organization_id``
   的派生关系，把已分配项目的用户回填为该组织的 ``operator`` 成员。
3. 系统管理员（``is_system_admin=True``）默认拥有所有组织访问权，无需回填。

降级
----
删除 organization_memberships 表（与 UserProjectRole 关系独立）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'f2a3b4c5d6e7'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 创建 organization_memberships 表
    op.create_table(
        'organization_memberships',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        # role 默认 operator；不可为空
        sa.Column('role', sa.String(length=32), nullable=False, server_default='operator'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['user_id'], ['app_users.id'], name='fk_org_membership_user'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name='fk_org_membership_org'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'organization_id', name='uq_org_membership'),
    )
    op.create_index('ix_organization_memberships_user_id', 'organization_memberships', ['user_id'])
    op.create_index('ix_organization_memberships_organization_id', 'organization_memberships', ['organization_id'])

    # 2. 数据回填：基于 user_project_roles → projects → organization_id 的派生关系
    #    同一 (user_id, organization_id) 出现多次时，保留首条。
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            INSERT INTO organization_memberships (id, user_id, organization_id, role, created_at, updated_at)
            SELECT
                gen_random_uuid(),
                upr.user_id,
                p.organization_id,
                'operator' AS role,
                NOW(),
                NOW()
            FROM user_project_roles upr
            JOIN projects p ON p.id = upr.project_id
            ON CONFLICT (user_id, organization_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_index('ix_organization_memberships_organization_id', table_name='organization_memberships')
    op.drop_index('ix_organization_memberships_user_id', table_name='organization_memberships')
    op.drop_table('organization_memberships')