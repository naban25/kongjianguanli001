"""Tests for business notification push service.

Coverage:
  1. notify_configured: 各通道配置正确识别
  2. notify_event: 无任何通道配置时静默跳过
  3. notify_event: 飞书 webhook 被调用（mock httpx）
  4. notify_event: 邮件配置缺 SMTP host 时跳过
  5. notify_event: Lark 调用失败不抛异常
  6. admin/notify/test 端点：未配置 → 400
  7. admin/notify/status 端点：返回各通道状态
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


# ---------- Fixtures ----------

@pytest.fixture
def settings_clear_notify(monkeypatch):
    """清空所有 notify_* 环境变量，确保测试隔离。"""
    for key in [
        "NOTIFY_LARK_WEBHOOK",
        "NOTIFY_SLACK_WEBHOOK",
        "NOTIFY_EMAIL_TO",
        "NOTIFY_SMTP_HOST",
        "NOTIFY_SMTP_USER",
        "NOTIFY_SMTP_PASSWORD",
        "NOTIFY_SMTP_FROM",
        "NOTIFY_SMTP_PORT",
    ]:
        monkeypatch.delenv(key, raising=False)
    from app.core.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings_with_lark(settings_clear_notify, monkeypatch):
    """仅开启 Lark webhook 的 settings。

    通过 monkeypatch 设置环境变量，触发 pydantic settings 重新加载，
    而不是直接构造 Settings 对象——因为 notify_event 内部用的是
    lru_cache 缓存的 get_settings()。
    """
    monkeypatch.setenv("NOTIFY_LARK_WEBHOOK", "https://open.feishu.cn/hook/test")
    from app.core.config import get_settings
    get_settings.cache_clear()
    return get_settings()


# ---------- 1. 配置检测 ----------

def test_notify_configured_no_channels(settings_clear_notify):
    """未配置任何通道 → notify_configured == False"""
    from app.core.config import get_settings
    assert get_settings().notify_configured is False


def test_notify_configured_lark_only():
    """只配 Lark → True"""
    from app.core.config import Settings
    s = Settings(notify_lark_webhook="https://open.feishu.cn/hook/x")
    assert s.notify_configured is True


def test_notify_configured_email_requires_smtp():
    """只配 email 但没 SMTP host → False"""
    from app.core.config import Settings
    s = Settings(notify_email_to="a@example.com,b@example.com", notify_smtp_host="")
    assert s.notify_configured is False


def test_notify_configured_email_full():
    """完整 email 配置 → True"""
    from app.core.config import Settings
    s = Settings(
        notify_email_to="a@example.com",
        notify_smtp_host="smtp.example.com",
        notify_smtp_from="noreply@example.com",
    )
    assert s.notify_configured is True


# ---------- 2. notify_event 无配置时跳过 ----------

@pytest.mark.asyncio
async def test_notify_event_silent_when_unconfigured(settings_clear_notify):
    """未配置 → 静默返回 0 通道"""
    from app.services.notify import notify_event
    result = await notify_event("test", "summary")
    assert result == {"sent": 0, "channels": []}


# ---------- 3. notify_event 调用 Lark ----------

@pytest.mark.asyncio
async def test_notify_event_calls_lark_webhook(settings_with_lark):
    """配 Lark → 飞书 webhook 被调用一次"""
    from app.services.notify import notify_event

    with patch("app.services.notify.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_response = AsyncMock()
        mock_response.raise_for_status = lambda: None
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = await notify_event("测试标题", "test summary", "info")

    assert result["sent"] == 1
    assert "lark" in result["channels"]
    # 校验 payload 内容
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "https://open.feishu.cn/hook/test"
    payload = call_args.kwargs["json"]
    assert payload["msg_type"] == "text"
    assert "测试标题" in payload["content"]["text"]
    assert "test summary" in payload["content"]["text"]


# ---------- 4. notify_event Lark 失败不抛异常 ----------

@pytest.mark.asyncio
async def test_notify_event_lark_failure_does_not_raise(settings_with_lark):
    """Lark 调用失败 → 不抛异常"""
    from app.services.notify import notify_event

    with patch("app.services.notify.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post.side_effect = RuntimeError("network down")
        mock_client_cls.return_value = mock_client

        # 不应该抛异常
        result = await notify_event("test", "summary")
    assert result["sent"] == 1  # 算作尝试了一次


# ---------- 5-6. Admin 端点 ----------

def test_notify_status_endpoint(logged_in_client, settings_clear_notify):
    """未配置 → 全部 false"""
    r = logged_in_client.get("/api/v1/admin/notify/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"lark": False, "slack": False, "email": False, "configured": False}


def test_notify_test_endpoint_no_config_returns_400(logged_in_client, settings_clear_notify):
    """未配置 → 触发测试返回 400"""
    r = logged_in_client.post(
        "/api/v1/admin/notify/test",
        json={"title": "测试", "summary": "测试内容", "level": "info"},
    )
    assert r.status_code == 400, r.text
    assert "未配置" in r.json()["detail"]


def test_notify_test_endpoint_requires_admin(client, settings_clear_notify):
    """未登录访问 → 401/403"""
    r = client.post(
        "/api/v1/admin/notify/test",
        json={"title": "test", "summary": "x", "level": "info"},
    )
    assert r.status_code in (401, 403), r.text


def test_notify_status_requires_admin(client, settings_clear_notify):
    """未登录访问 → 401/403"""
    r = client.get("/api/v1/admin/notify/status")
    assert r.status_code in (401, 403), r.text
