"""LLM Provider — OpenAI-compatible chat completions via httpx.

Safe by design:
  - No SDK binding; any /v1/chat/completions endpoint works
  - 3-s retry on 5xx + exponential backoff
  - Timeout enforced at both httpx level and read level
  - All errors surface as LLMError so callers can fall back gracefully
  - NEVER exposes raw prompt/content in error messages (sanitised)
  - NEVER executes arbitrary SQL or tool calls outside the allowlist

Callers: Assistant intent router uses this for (a) intent disambiguation
when rule-based match is weak, and (b) natural-language summarization of
structured tool results.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterator, Optional

import httpx

from ..core.config import get_settings

settings = get_settings()


class LLMError(Exception):
    """Safe error — message never contains full prompt, only a short sanitised reason."""

    def __init__(self, reason: str, *, status_code: Optional[int] = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


def is_configured() -> bool:
    return settings.llm_configured


def _build_system_prompt(business_context: dict[str, Any]) -> str:
    """System prompt for the assistant LLM. Boundaries are explicit — no SQL, no free writes."""
    return f"""你是"AI写字楼空间租赁管理系统"的数据查询助手。你的任务：
1. 理解用户意图 → 从允许的工具列表中选最合适的1个
2. 提取查询参数（如到期天数、状态、日期范围）
3. 最终用中文总结工具返回的数据，给出业务洞见

⚠️ 严格边界：
- 只能调用系统提供的受控工具，禁止生成或执行任意 SQL
- 禁止修改数据库（所有工具都是只读的）
- 如果工具数据为空，如实告知"暂无数据"，禁止编造
- 回答必须简洁、结构化，优先给数字和关键事实

允许的工具：
- get_due_leases(days=N) → 未来 N 天内到期的租赁合同
- get_overdue_charges() → 当前逾期未缴费用
- get_business_metrics() → 出租率、活跃租约数、应收款汇总
- search_spaces(status?, search?) → 按状态/关键词搜索空间
- get_available_tables() → 列出当前项目各类数据表行数

业务上下文：{json.dumps(business_context, ensure_ascii=False)}"""


_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    business_context: Optional[dict[str, Any]] = None,
) -> str:
    """Single turn chat. Returns assistant text or raises LLMError.

    Retries 3x on 5xx / 429 with exponential backoff (1s, 2s, 4s).
    """
    if not is_configured():
        raise LLMError("LLM 未配置")

    system = _build_system_prompt(business_context or {})

    payload = {
        "model": settings.llm_model,
        "messages": [{"role": "system", "content": system}, *messages],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    # NOTE: NVIDIA Free Endpoint API doesn't accept extra_body, so we send plain
    # parameters. Reasoning models will include reasoning_content in the response
    # which our parser handles gracefully.
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }

    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    timeout = httpx.Timeout(settings.llm_timeout_seconds, connect=10)

    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=payload, headers=headers)

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    msg = data["choices"][0]["message"]
                    content = (msg.get("content") or "").strip()
                    if content:
                        return content
                    # Reasoning models (e.g. gpt-oss-20b) may put the answer in
                    # reasoning_content — especially when max_tokens was consumed
                    # by the thinking phase. Use the tail (conclusion) as fallback.
                    reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
                    if reasoning:
                        tail = reasoning[-500:]
                        # Prefer the text after the last sentence boundary in the tail
                        for sep in ("。", "\n", ". "):
                            idx = tail.rfind(sep)
                            if idx > 0 and idx > len(tail) - 300:
                                tail = tail[idx + len(sep):].strip() or tail
                                break
                        return tail
                    raise LLMError("LLM 返回空内容")
                except LLMError:
                    raise
                except (KeyError, IndexError, json.JSONDecodeError) as exc:
                    raise LLMError(f"LLM 返回格式异常") from exc

            if resp.status_code in _RETRYABLE_STATUSES and attempt < 2:
                time.sleep(2 ** attempt)
                continue

            # Non-retryable
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", resp.text[:200])
            except Exception:
                detail = resp.text[:200]
            if resp.status_code == 401:
                raise LLMError("LLM 认证失败 (API Key 无效)")
            if resp.status_code == 404:
                raise LLMError("LLM 端点未找到 (检查 base_url 或 model)")
            raise LLMError(f"LLM 返回 HTTP {resp.status_code}")

        except httpx.TimeoutException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue
        except httpx.ConnectError as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue
        except httpx.HTTPError as exc:
            last_exc = exc
            raise LLMError(f"LLM 网络错误") from exc

    raise LLMError(f"LLM 请求失败 (重试3次后仍不可用)") from last_exc


def chat_stream(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    business_context: Optional[dict[str, Any]] = None,
) -> Iterator[str]:
    """Streaming chat. Yields incremental text chunks as they arrive.

    Uses server-sent events from the OpenAI-compatible endpoint. Each yielded
    string is a content delta (already stripped). Empty chunks are skipped.
    Raises LLMError on configuration / network / parse failures so the caller
    can fall back to non-streaming or rule-based summary.

    Reasoning models (e.g. NVIDIA gpt-oss) stream reasoning_content deltas too;
    we yield them so users see the thinking process in real time, marked as
    a separate stream segment by the caller. Content and reasoning_content
    may interleave per chunk; the renderer can split them by event type.
    """
    if not is_configured():
        raise LLMError("LLM 未配置")

    system = _build_system_prompt(business_context or {})

    payload = {
        "model": settings.llm_model,
        "messages": [{"role": "system", "content": system}, *messages],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }

    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    timeout = httpx.Timeout(settings.llm_timeout_seconds, connect=10)

    with httpx.Client(timeout=timeout) as client:
        with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                # Read a small prefix of the body for diagnostics and surface a sanitised error.
                detail = ""
                try:
                    # Read up to 4KB for error message
                    raw = b""
                    for chunk in resp.iter_bytes():
                        raw += chunk
                        if len(raw) > 4096:
                            break
                    try:
                        detail = json.loads(raw.decode("utf-8", errors="ignore")).get("error", {}).get("message", "")
                    except Exception:
                        detail = raw[:200].decode("utf-8", errors="ignore")
                except Exception:
                    detail = ""
                if resp.status_code == 401:
                    raise LLMError("LLM 认证失败 (API Key 无效)")
                if resp.status_code == 404:
                    raise LLMError("LLM 端点未找到 (检查 base_url 或 model)")
                raise LLMError(f"LLM 返回 HTTP {resp.status_code}" + (f": {detail[:120]}" if detail else ""))

            # Parse SSE: each line "data: {...}", and a final "data: [DONE]"
            saw_any_chunk = False
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    payload_str = line[5:].strip()
                    if payload_str == "[DONE]":
                        break
                    try:
                        evt = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    try:
                        choice = evt["choices"][0]
                    except (KeyError, IndexError):
                        continue
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    emitted = False
                    if content:
                        text = content.strip()
                        if text:
                            saw_any_chunk = True
                            yield f"[content]{text}"
                            emitted = True
                    if reasoning:
                        text = reasoning.strip()
                        if text:
                            saw_any_chunk = True
                            yield f"[reasoning]{text}"
                            emitted = True
                    if not emitted:
                        # Some models send role=fetch or finish_reason; ignore silently
                        pass

            if not saw_any_chunk:
                raise LLMError("LLM 流式返回空内容")


def summarize_tool_result(tool_name: str, tool_data: dict[str, Any], user_query: str) -> str:
    """Use LLM to summarize a tool's structured result into natural language."""
    if not is_configured():
        # Fallback: return a brief rule-based summary (caller can still try, we just skip LLM polish)
        if tool_name == "get_due_leases":
            return f"未来到期合同共 {tool_data.get('row_count', 0)} 份。"
        if tool_name == "get_overdue_charges":
            return f"当前逾期费用共 {tool_data.get('row_count', 0)} 笔。"
        if tool_name == "get_business_metrics":
            o = tool_data.get("occupancy", {})
            return f"数量出租率 {o.get('space_count_occupancy', {}).get('ratio_pct', '—')}%、面积出租率 {o.get('area_occupancy', {}).get('ratio_pct', '—')}%。"
        return f"查询完成，工具 {tool_name} 返回数据。"

    # Truncate tool data to avoid huge prompts
    safe_data = json.dumps(tool_data, ensure_ascii=False)
    if len(safe_data) > 3000:
        safe_data = safe_data[:3000] + "…(截断)"

    messages = [
        {"role": "user", "content": f"""用户原查询：{user_query}

工具：{tool_name}
工具返回（JSON）：{safe_data}

请用中文写一段简洁的业务总结（2-4 句），突出关键数字和结论。如果数据为空也要如实说明。"""},
    ]

    try:
        return chat(messages)
    except LLMError:
        # Fallback to a minimal summary — never fail the whole response
        if tool_name == "get_due_leases":
            return f"未来到期合同共 {tool_data.get('row_count', 0)} 份。"
        if tool_name == "get_overdue_charges":
            return f"当前逾期费用共 {tool_data.get('row_count', 0)} 笔。"
        return f"工具 {tool_name} 返回 {tool_data.get('row_count', 0)} 条结果。"
