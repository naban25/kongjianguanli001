"""业务通知推送服务。

支持三类通道（任意组合，未配置则静默跳过）：
- 飞书自定义机器人 webhook（Lark）
- Slack incoming webhook
- 邮件（SMTP）

所有方法都是 fire-and-forget：失败只记录日志，不抛出异常，避免影响
调度任务主流程。

使用示例：

    from ..services.notify import notify_event
    await notify_event(
        title="本月对账完成",
        summary="共标记 12 条过期租约，3 条逾期费用。",
        level="info",
    )

注意：本模块仅做对外通知，业务内部事件仍由 AuditLog 写入数据库。
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx

from ..core.config import get_settings

logger = logging.getLogger(__name__)


# ============================================================
#  通道发送
# ============================================================

async def _send_lark(webhook: str, title: str, summary: str, level: str) -> None:
    """发送飞书自定义机器人消息（text 类型）。

    飞书 webhook 文档：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
    """
    # level → emoji 前缀
    prefix = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}.get(level, "📢")
    text = f"{prefix} **{title}**\n\n{summary}"
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(webhook, json=payload)
            r.raise_for_status()
    except Exception as e:
        logger.warning("[notify.lark] 飞书推送失败: %s", e)


async def _send_slack(webhook: str, title: str, summary: str, level: str) -> None:
    """发送 Slack incoming webhook 消息。"""
    emoji = {"info": ":information_source:", "warning": ":warning:", "error": ":x:"}.get(level, ":loudspeaker:")
    payload = {
        "text": f"{emoji} *{title}*\n{summary}",
        "mrkdwn": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(webhook, json=payload)
            r.raise_for_status()
    except Exception as e:
        logger.warning("[notify.slack] Slack 推送失败: %s", e)


async def _send_email(to_addrs: list[str], title: str, summary: str, level: str) -> None:
    """通过 SMTP 发送邮件通知。"""
    settings = get_settings()
    if not settings.notify_smtp_host or not settings.notify_smtp_from:
        return

    msg = EmailMessage()
    msg["Subject"] = f"[空间管理] {title}"
    msg["From"] = settings.notify_smtp_from
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(
        f"{title}\n\n"
        f"级别: {level}\n\n"
        f"{summary}\n\n"
        f"— 自动通知，请勿直接回复 —"
    )

    try:
        # SMTP 是阻塞 I/O，放到线程池里避免阻塞事件循环
        import asyncio
        await asyncio.to_thread(_smtp_send, settings, msg, to_addrs)
    except Exception as e:
        logger.warning("[notify.email] 邮件推送失败: %s", e)


def _smtp_send(settings: Any, msg: EmailMessage, to_addrs: list[str]) -> None:
    """同步发送 SMTP（在 to_thread 里执行）。"""
    with smtplib.SMTP(settings.notify_smtp_host, settings.notify_smtp_port, timeout=15) as smtp:
        smtp.starttls()
        if settings.notify_smtp_user:
            smtp.login(settings.notify_smtp_user, settings.notify_smtp_password)
        smtp.send_message(msg, from_addr=settings.notify_smtp_from, to_addrs=to_addrs)


# ============================================================
#  公开 API
# ============================================================

async def notify_event(title: str, summary: str, level: str = "info") -> dict[str, Any]:
    """对外发送业务事件通知。

    Args:
        title: 通知标题，例如「本月对账完成」
        summary: 通知正文，支持纯文本或简单 markdown
        level: info / warning / error，影响 emoji 样式

    Returns:
        各通道发送结果的简报，仅用于日志。
    """
    settings = get_settings()
    if not settings.notify_configured:
        logger.debug("[notify] 未配置任何通知通道，跳过推送 (title=%s)", title)
        return {"sent": 0, "channels": []}

    results = {"sent": 0, "channels": []}

    if settings.notify_lark_webhook:
        await _send_lark(settings.notify_lark_webhook, title, summary, level)
        results["sent"] += 1
        results["channels"].append("lark")

    if settings.notify_slack_webhook:
        await _send_slack(settings.notify_slack_webhook, title, summary, level)
        results["sent"] += 1
        results["channels"].append("slack")

    if settings.notify_email_to and settings.notify_smtp_host:
        addrs = [a.strip() for a in settings.notify_email_to.split(",") if a.strip()]
        if addrs:
            await _send_email(addrs, title, summary, level)
            results["sent"] += 1
            results["channels"].append("email")

    logger.info("[notify] 事件 %r 推送到 %d 个通道 (%s)", title, results["sent"], ",".join(results["channels"]))
    return results