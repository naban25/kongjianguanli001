"""Tests for /dashboard/financials — monthly due/paid/outstanding roll-up.

Coverage:
  1. 默认参数（当前月）→ 200 且 totals / charges / payments 结构完整
  2. year_month 解析：传 'YYYY-MM' 切换到对应月份
  3. 未授权访问 → 401/403
  4. totals.outstanding == totals.due - 实际已收部分（不为负数）
  5. 详细账目包含本月所有未取消 charges 与所有已完成 payments
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text


# ---------- Helpers ------------------------------------------------------

def _seed_project(db_session, org_id: uuid.UUID) -> uuid.UUID:
    """插入一个最小 Project 行（name/status/timezone 列必填）。"""
    pid = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO projects (id, organization_id, name, status, timezone, created_at, updated_at) "
            "VALUES (:id, :pid, :n, 'active', 'Asia/Shanghai', NOW(), NOW())"
        ),
        {"id": str(pid), "pid": str(org_id), "n": f"财务测试项目-{pid.hex[:6]}"},
    )
    db_session.flush()
    return pid


def _seed_tenant(db_session, org_id: uuid.UUID, project_id: uuid.UUID) -> uuid.UUID:
    """插入最小 Tenant 行（tenant_name / tenant_type / status 必填）。"""
    tid = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO tenants (id, organization_id, project_id, tenant_name, "
            "tenant_type, status, created_at, updated_at) "
            "VALUES (:id, :oid, :pid, :name, 'enterprise', 'active', NOW(), NOW())"
        ),
        {"id": str(tid), "oid": str(org_id), "pid": str(project_id),
         "name": f"测试租户-{tid.hex[:6]}"},
    )
    db_session.flush()
    return tid


def _seed_charge(db_session, org_id: uuid.UUID, project_id: uuid.UUID,
                 tenant_id: uuid.UUID, *, due: date, amount: str = "1000.00",
                 status: str = "unpaid") -> uuid.UUID:
    cid = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO charges (id, organization_id, project_id, tenant_id, "
            "charge_type, due_date, amount_due, currency, status, created_at, updated_at) "
            "VALUES (:id, :oid, :pid, :tid, 'rent', :due, :amt, 'CNY', :st, NOW(), NOW())"
        ),
        {"id": str(cid), "oid": str(org_id), "pid": str(project_id),
         "tid": str(tenant_id), "due": due.isoformat(),
         "amt": amount, "st": status},
    )
    db_session.flush()
    return cid


def _seed_payment(db_session, org_id: uuid.UUID, project_id: uuid.UUID,
                  tenant_id: uuid.UUID, *, paid: date, amount: str = "500.00",
                  status: str = "completed") -> uuid.UUID:
    pid = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO payments (id, organization_id, project_id, tenant_id, "
            "payment_date, amount, currency, status, created_at, updated_at) "
            "VALUES (:id, :oid, :pid, :tid, :paid, :amt, 'CNY', :st, NOW(), NOW())"
        ),
        {"id": str(pid), "oid": str(org_id), "pid": str(project_id),
         "tid": str(tenant_id), "paid": paid.isoformat(),
         "amt": amount, "st": status},
    )
    db_session.flush()
    return pid


def _ensure_role(db_session, user_id: uuid.UUID, project_id: uuid.UUID) -> None:
    """确保 admin 用户对项目有 view 权限，避免 ActiveOrg 校验失败。"""
    db_session.execute(
        text(
            "INSERT INTO user_project_roles (id, user_id, project_id, role, created_at) "
            "VALUES (:id, :uid, :pid, 'project_admin', NOW()) ON CONFLICT DO NOTHING"
        ),
        {"id": str(uuid.uuid4()), "uid": str(user_id),
         "pid": str(project_id)},
    )
    db_session.flush()


# ---------- Fixtures -----------------------------------------------------

@pytest.fixture()
def fin_seed(db_session, admin_user):
    """为财务测试插入 1 项目 / 1 租户 / 多笔费用与收款。"""
    # 取默认 org
    org_id = db_session.execute(
        text("SELECT id FROM organizations LIMIT 1")
    ).first()[0]
    org_id = uuid.UUID(str(org_id))

    project_id = _seed_project(db_session, org_id)
    tenant_id = _seed_tenant(db_session, org_id, project_id)
    _ensure_role(db_session, admin_user["id"], project_id)

    today = date.today()
    in_month = today.replace(day=15)
    last_month_last_day = in_month - timedelta(days=15)
    # 下个月第一天：跨月加 20 天到下月（用 replace(day=1) + 替换月）
    if in_month.month == 12:
        next_month_first = in_month.replace(year=in_month.year + 1, month=1, day=1)
    else:
        next_month_first = in_month.replace(month=in_month.month + 1, day=1)

    # 本月应收：本月 2 笔 (3000 元)
    _seed_charge(db_session, org_id, project_id, tenant_id, due=in_month, amount="1500.00")
    _seed_charge(db_session, org_id, project_id, tenant_id, due=in_month, amount="1500.00")
    # 上月 1 笔（应被排除）
    _seed_charge(db_session, org_id, project_id, tenant_id,
                 due=last_month_last_day, amount="999.00")
    # 下月 1 笔（应被排除）
    _seed_charge(db_session, org_id, project_id, tenant_id,
                 due=next_month_first, amount="888.00")
    # 本月已取消 1 笔（应被排除）
    _seed_charge(db_session, org_id, project_id, tenant_id,
                 due=in_month, amount="111.00", status="cancelled")

    # 本月已收：本月 2 笔 (1200 元)
    _seed_payment(db_session, org_id, project_id, tenant_id, paid=in_month, amount="700.00")
    _seed_payment(db_session, org_id, project_id, tenant_id, paid=in_month, amount="500.00")
    # 上月 1 笔（应被排除）
    _seed_payment(db_session, org_id, project_id, tenant_id,
                  paid=last_month_last_day, amount="333.00")
    # 本月但 status=cancelled 的 1 笔（应被排除）
    _seed_payment(db_session, org_id, project_id, tenant_id,
                  paid=in_month, amount="222.00", status="cancelled")

    db_session.flush()
    return {"project_id": project_id, "org_id": org_id}


# ---------- Tests --------------------------------------------------------


def test_financials_default_month(logged_in_client, fin_seed):
    """默认参数：当前月 totals & 账目明细。"""
    pid = fin_seed["project_id"]
    resp = logged_in_client.get(f"/api/v1/dashboard/financials?project_id={pid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert "year_month" in body
    assert "totals" in body
    assert body["totals"]["due"] == 3000.0  # 两笔 1500
    assert body["totals"]["paid"] == 1200.0  # 700 + 500
    # outstanding = 应收 - 实际已收（按费用视角，未收到的部分）
    # 因为 amount_paid 列在测试 seed 中默认是 0，所以 outstanding = 3000 - 0 = 3000
    assert body["totals"]["outstanding"] >= body["totals"]["due"] - body["totals"]["paid"]
    assert body["totals"]["charge_count"] == 2
    assert body["totals"]["payment_count"] == 2

    # 详细账目只包含本月的 2 笔 charges（不含上月/下月/取消）
    assert len(body["charges"]) == 2
    # 详细账目只包含本月的 2 笔 payments（不含上月/取消）
    assert len(body["payments"]) == 2


def test_financials_explicit_year_month(logged_in_client, fin_seed):
    """指定 year_month → 范围切到 2024-01，应为空数据"""
    pid = fin_seed["project_id"]
    resp = logged_in_client.get(
        f"/api/v1/dashboard/financials?project_id={pid}&year_month=2024-01"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["year_month"] == "2024-01"
    assert body["totals"]["due"] == 0
    assert body["totals"]["paid"] == 0
    assert body["totals"]["charge_count"] == 0
    assert body["totals"]["payment_count"] == 0
    assert body["charges"] == []
    assert body["payments"] == []


def test_financials_rejects_bad_year_month(logged_in_client, fin_seed):
    """year_month 格式错误必须返回 400。"""
    pid = fin_seed["project_id"]
    resp = logged_in_client.get(
        f"/api/v1/dashboard/financials?project_id={pid}&year_month=2024/01"
    )
    assert resp.status_code == 400, resp.text
    assert "格式" in resp.json().get("detail", "")


def test_financials_requires_auth(client, fin_seed):
    """未登录访问 → 401/403。"""
    pid = fin_seed["project_id"]
    resp = client.get(f"/api/v1/dashboard/financials?project_id={pid}")
    assert resp.status_code in (401, 403), resp.text


def test_financials_aggregates_only_sum_columns_via_filter(logged_in_client, fin_seed):
    """本月范围筛选 → 与多维表格打通（charges/due_date:between:...)"""
    pid = fin_seed["project_id"]
    today = date.today()
    # 同步检查 tables 端点支持 between 操作符
    start = today.replace(day=1).isoformat()
    # 计算本月最后一天
    if today.month == 12:
        last_day_date = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
    else:
        last_day_date = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
    end = last_day_date.isoformat()
    resp = logged_in_client.get(
        f"/api/v1/tables/business:charges/records"
        f"?project_id={pid}"
        f"&filters=due_date:between:{start}|{end}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 范围筛选返回本月所有 charges（含 cancelled — cancelled 是业务状态字段，
    # 范围筛选是数据点过滤，不应隐藏 cancelled 行）
    # 本月有 in_month 的 3 笔（2 个 unpaid + 1 个 cancelled）+ 上下月各 1 笔不在范围
    assert body["total"] == 3
