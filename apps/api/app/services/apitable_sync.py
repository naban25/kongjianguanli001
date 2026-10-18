"""APITable / 维格表单向同步服务。

架构：Neon PostgreSQL 为唯一主数据源，APITable/维格表为只读协作视图层。
策略：全量覆盖同步 —— 查找或创建同名 datasheet → 清空旧记录 → 批量写入新记录。

两大云版差异（自动根据 base_url 检测）：

┌──────────────────────┬───────────────────────────┬──────────────────────────┐
│                      │ 国际版 apitable.com       │ 国内版 api.vika.cn       │
├──────────────────────┼───────────────────────────┼──────────────────────────┤
│ base_url             │ https://aitable.ai        │ https://api.vika.cn      │
│                      │ https://apitable.com      │                          │
├──────────────────────┼───────────────────────────┼──────────────────────────┤
│ 字段 type 格式        │ 数字 (1,2,3,5...)         │ 字符串 ("Text",...)      │
│ enum type            │ SingleSelect (3) + options│ "Text" 统一兜底          │
│ number type          │ Number (2)                │ "Text" 统一兜底          │
│ date type            │ DateTime (5)              │ "Text" 统一兜底          │
├──────────────────────┼───────────────────────────┼──────────────────────────┤
│ datasheets 列表       │ /spaces/{id}/datasheets  │ /spaces/{id}/nodes       │
│                      │                           │  (过滤 type=Datasheet)    │
├──────────────────────┼───────────────────────────┼──────────────────────────┤
│ records 创建          │ batch_create (批量)       │ /records (records=[])    │
│                      │ fieldKey + records[]      │ fieldKey + records[]     │
├──────────────────────┼───────────────────────────┼──────────────────────────┤
│ QPS 免费版            │ 2 QPS                     │ 2 QPS                    │
└──────────────────────┴───────────────────────────┴──────────────────────────┘
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any, Iterable

import httpx

from ..core.config import get_settings

logger = logging.getLogger(__name__)

# 国际版字段类型常量
INTL_TYPE_TEXT = 1
INTL_TYPE_NUMBER = 2
INTL_TYPE_SINGLESELECT = 3
INTL_TYPE_DATETIME = 5

# 国内版字段类型（字符串）
CN_TYPE_TEXT = "Text"
CN_TYPE_NUMBER = "Number"
CN_TYPE_SINGLESELECT = "SingleSelect"
CN_TYPE_DATETIME = "DateTime"

BATCH_SIZE = 10
TIMEOUT = 30.0
QPS_INTERVAL = 0.55  # 免费版 2 QPS（保守点）


def _detect_region(base_url: str) -> str:
    """根据 base_url 检测是国际版还是国内版。"""
    base = (base_url or "").lower()
    if "vika.cn" in base:
        return "cn"
    return "intl"


# ============================================================
#  字段映射器（自动选择国际版/国内版格式）
# ============================================================

def map_fields(col_meta: list[dict], region: str = "intl") -> list[dict]:
    """将内部 COLUMN_META 转为目标平台的 datasheet fields 数组。

    国内版 vika.cn 在创建 datasheet 时支持 Number / SingleSelect / DateTime 类型，
    直接创建正确类型，避免用户后续手动修改。
    国际版用数字 type + property 精确配置。
    """
    SKIP = ("id", "organization_id", "project_id", "version",
            "source_import_row_id", "building_id", "created_at", "updated_at")

    # vika.cn 首列不允许为 SingleSelect。
    # 若首列业务字段是 enum，则在前面插入"_源记录ID"作为首列，让业务 enum 能保留为 SingleSelect。
    need_prepend_src = False
    if region == "cn" and col_meta:
        first_non_skipped = next((c for c in col_meta if c["name"] not in SKIP), None)
        if first_non_skipped and first_non_skipped.get("type") == "enum":
            need_prepend_src = True

    fields: list[dict] = []

    # 隐藏字段：源 ID（用于幂等）。CN 版当首列会被替换时前置。
    src_field = {"name": "_源记录ID"}
    if region == "cn":
        src_field["type"] = CN_TYPE_TEXT
    else:
        src_field["type"] = INTL_TYPE_TEXT
    if need_prepend_src:
        fields.append(dict(src_field))

    for _idx, col in enumerate(col_meta):
        name = col["name"]
        if name in SKIP:
            continue
        label = col["label"]
        ctype = col.get("type", "text")
        f = {"name": label}

        # vika.cn 首列不允许为 SingleSelect，已通过前置 _源记录ID 解决首列问题
        is_first_field = (len(fields) == 0)

        if region == "cn":
            # 国内版：直接创建正确的字段类型
            if ctype == "text" or (is_first_field and ctype == "enum"):
                f["type"] = CN_TYPE_TEXT
            elif ctype == "number":
                f["type"] = CN_TYPE_NUMBER
                f["property"] = {"precision": 0}
            elif ctype == "currency":
                f["type"] = CN_TYPE_NUMBER
                f["property"] = {"precision": 2}
            elif ctype == "enum":
                enum_map: dict[str, str] = col.get("enum_map", {})
                f["type"] = CN_TYPE_SINGLESELECT
                f["property"] = {"options": [{"name": zh} for zh in enum_map.values()]}
            elif ctype in ("date", "datetime"):
                f["type"] = CN_TYPE_DATETIME
                f["property"] = {"dateFormat": "YYYY-MM-DD", "timeFormat": "HH:mm", "autoFill": False}
            else:
                f["type"] = CN_TYPE_TEXT
        else:
            # 国际版：精确 type + property
            if ctype == "text":
                f["type"] = INTL_TYPE_TEXT
            elif ctype == "number":
                f["type"] = INTL_TYPE_NUMBER
            elif ctype == "currency":
                f["type"] = INTL_TYPE_NUMBER
                f["property"] = {"formatter": "0.00", "currencySymbol": "¥", "symbolAlign": "left"}
            elif ctype == "enum":
                f["type"] = INTL_TYPE_SINGLESELECT
                enum_map = col.get("enum_map", {})
                f["property"] = {"options": [{"name": zh} for zh in enum_map.values()]}
            elif ctype in ("date", "datetime"):
                f["type"] = INTL_TYPE_DATETIME
                f["property"] = {"dateFormat": "YYYY-MM-DD", "timeFormat": "HH:mm", "autoFill": False}
            else:
                f["type"] = INTL_TYPE_TEXT

        fields.append(f)

    # 不需要前置时，把 _源记录ID 追加到末尾
    if not need_prepend_src:
        fields.append(dict(src_field))

    return fields


def transform_record(row: dict, col_meta: list[dict], region: str = "intl",
                     field_types: dict[str, str] | None = None) -> dict:
    """将 Neon 记录转为目标平台 records.fields 格式。

    field_types: 目标 datasheet 中各字段（按 label）的实际类型，用于适配值格式。
                当提供时，以实际字段类型为准格式化值（兼容用户在 vika.cn 手动改过的类型）。
                若为 None，按 region + col_meta 类型处理。
    """
    SKIP = ("id", "organization_id", "project_id", "version",
            "source_import_row_id", "building_id", "created_at", "updated_at")
    fields: dict[str, Any] = {}

    for col in col_meta:
        name = col["name"]
        if name in SKIP:
            continue
        label = col["label"]
        ctype = col.get("type", "text")
        raw = row.get(name)

        if raw is None or raw == "":
            continue

        actual_type = field_types.get(label) if field_types else None

        # 若知道目标字段实际类型，以实际类型为准格式化值
        if actual_type:
            if actual_type == CN_TYPE_NUMBER:
                try:
                    fields[label] = float(raw)
                    continue
                except (TypeError, ValueError):
                    continue  # 无法转数字则跳过该字段
            elif actual_type == CN_TYPE_DATETIME:
                try:
                    dt = _parse_dt(raw)
                    if dt:
                        fields[label] = int(dt.timestamp() * 1000)
                    continue
                except Exception:
                    continue
            else:
                # Text / SingleSelect：发送字符串
                if ctype == "enum":
                    enum_map: dict[str, str] = col.get("enum_map", {})
                    fields[label] = enum_map.get(str(raw), str(raw))
                else:
                    fields[label] = str(raw) if not isinstance(raw, str) else raw
                continue

        # 无实际类型信息时，按 col_meta + region 默认处理
        if ctype == "enum":
            enum_map = col.get("enum_map", {})
            fields[label] = enum_map.get(str(raw), str(raw))

        elif ctype in ("date", "datetime"):
            try:
                dt = _parse_dt(raw)
                if dt:
                    if region == "intl":
                        fields[label] = int(dt.timestamp() * 1000)
                    else:
                        fields[label] = dt.astimezone().strftime("%Y-%m-%d %H:%M")
            except Exception:
                fields[label] = str(raw)

        elif ctype in ("number", "currency"):
            try:
                val = float(raw)
                if region == "intl":
                    fields[label] = val
                else:
                    fields[label] = f"{val:.2f}" if ctype == "currency" else str(val)
            except (TypeError, ValueError):
                continue
        else:
            fields[label] = str(raw) if not isinstance(raw, str) else raw

    src_id = row.get("id")
    if src_id:
        fields["_源记录ID"] = str(src_id)
    return fields


def _parse_dt(raw: Any) -> datetime | None:
    """将字符串 / date / datetime 解析为带时区的 datetime。"""
    if isinstance(raw, str):
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day)
    return None


# ============================================================
#  客户端（自动根据 base_url 选路径 + 参数格式）
# ============================================================

class APITableError(Exception):
    def __init__(self, message: str, code: int | None = None):
        self.code = code
        super().__init__(message)


class APITableClient:
    def __init__(self, token: str, base_url: str):
        self.token = token
        self.base = base_url.rstrip("/")
        self.region = _detect_region(base_url)
        logger.info("[apitable] client init: base=%s region=%s", self.base, self.region)

    async def _request(self, method: str, path: str, json_body: dict | None = None,
                       params: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(method, url, json=json_body, params=params, headers=headers)
            try:
                data = resp.json()
            except Exception:
                raise APITableError(f"非 JSON 响应: HTTP {resp.status_code} - {resp.text[:200]}")

        if resp.status_code >= 400 or not data.get("success", True):
            raise APITableError(
                data.get("message") or data.get("msg") or f"HTTP {resp.status_code}",
                code=data.get("code"),
            )
        return data

    # --- 基础能力 ---------------------------------------------------------

    async def ping(self) -> dict:
        return await self._request("GET", "/fusion/v1/spaces")

    # --- Datasheet 操作 ---------------------------------------------------

    async def create_datasheet(self, space_id: str, name: str, fields: list[dict]) -> str:
        body = {"name": name, "fields": fields}
        data = await self._request("POST", f"/fusion/v1/spaces/{space_id}/datasheets", json_body=body)
        return data["data"]["id"]

    async def _list_datasheets_raw(self, space_id: str) -> list[dict]:
        """返回 datasheet 条目列表（兼容国际版 datasheets 和国内版 nodes）。"""
        if self.region == "cn":
            data = await self._request("GET", f"/fusion/v1/spaces/{space_id}/nodes")
            return [{"id": n["id"], "name": n["name"]}
                    for n in data.get("data", {}).get("nodes", [])
                    if n.get("type") == "Datasheet"]
        else:
            data = await self._request("GET", f"/fusion/v1/spaces/{space_id}/datasheets")
            return data.get("data", {}).get("datasheets", [])

    async def find_datasheet_by_name(self, space_id: str, name: str) -> dict | None:
        for ds in await self._list_datasheets_raw(space_id):
            if ds.get("name") == name:
                return ds
        return None

    async def list_fields(self, datasheet_id: str) -> list[dict]:
        """获取 datasheet 的全部字段（含 type / property）。"""
        data = await self._request("GET", f"/fusion/v1/datasheets/{datasheet_id}/fields")
        return data.get("data", {}).get("fields", [])

    # --- Record 操作 ------------------------------------------------------

    async def list_records(self, datasheet_id: str, page_size: int = 1000) -> list[dict]:
        all_records: list[dict] = []
        offset = None
        while True:
            params: dict[str, Any] = {"pageSize": page_size}
            if offset:
                params["offset"] = offset
            data = await self._request(
                "GET", f"/fusion/v1/datasheets/{datasheet_id}/records", params=params,
            )
            payload = data.get("data", {})
            all_records.extend(payload.get("records", []))
            if not payload.get("hasMore"):
                break
            offset = payload.get("offset")
        return all_records

    async def delete_records(self, datasheet_id: str, record_ids: Iterable[str]) -> None:
        ids = list(record_ids)
        for i in range(0, len(ids), 10):
            chunk = ids[i : i + 10]
            params = {"recordIds": ",".join(chunk)}
            await self._request(
                "DELETE", f"/fusion/v1/datasheets/{datasheet_id}/records", params=params,
            )
            await asyncio.sleep(QPS_INTERVAL)

    async def batch_create_records(self, datasheet_id: str, records: list[dict]) -> int:
        """批量写入 records。根据 region 自动选正确的 API 端点 + 参数格式。"""
        written = 0
        for i in range(0, len(records), BATCH_SIZE):
            chunk = records[i : i + BATCH_SIZE]

            if self.region == "intl":
                # 国际版：batch_create 专用端点
                data = await self._request(
                    "POST",
                    f"/fusion/v1/datasheets/{datasheet_id}/records/batch_create",
                    json_body={"records": chunk, "fieldKey": "name"},
                )
            else:
                # 国内版：普通 /records 端点 + records 数组
                data = await self._request(
                    "POST",
                    f"/fusion/v1/datasheets/{datasheet_id}/records",
                    json_body={"records": chunk, "fieldKey": "name"},
                )

            # 返回 records 对象（结构国际版国内版不同，都取列表）
            resp_records = data.get("data", {}).get("records", [])
            written += len(resp_records)
            await asyncio.sleep(QPS_INTERVAL)

        return written


# ============================================================
#  同步编排
# ============================================================

ENTITY_TO_DATASHEET = {
    "spaces": "空间管理",
    "tenants": "租户管理",
    "leases": "租约管理",
    "charges": "费用台账",
    "payments": "收款记录",
}


async def sync_entity(entity: str, records: list[dict], col_meta: list[dict]) -> dict:
    settings = get_settings()
    if not settings.apitable_configured:
        raise APITableError("APITable 未配置")

    client = APITableClient(token=settings.apitable_token, base_url=settings.apitable_base_url)
    region = client.region
    space_id = settings.apitable_space_id
    datasheet_name = ENTITY_TO_DATASHEET.get(entity, entity)

    # 1. 确保 datasheet 存在（必要时自动重建）
    existing = await client.find_datasheet_by_name(space_id, datasheet_name)
    recreate_reason: str | None = None
    actual_datasheet_name = datasheet_name

    if existing:
        datasheet_id = existing["id"]
        actual_datasheet_name = existing.get("name", datasheet_name)
        logger.info("[apitable] datasheet 已存在: %s (%s)", actual_datasheet_name, datasheet_id)
    else:
        fields = map_fields(col_meta, region=region)
        datasheet_id = await client.create_datasheet(space_id, datasheet_name, fields)
        logger.info("[apitable] 新建 datasheet: %s (%s) → %s", datasheet_name, region, datasheet_id)

    # 2. 读取 datasheet 实际字段类型，用于适配记录值格式
    actual_fields = await client.list_fields(datasheet_id)
    field_types: dict[str, str] = {f["name"]: f.get("type", "Text") for f in actual_fields}

    # 检测字段类型是否需要修正
    SKIP = ("id", "organization_id", "project_id", "version",
            "source_import_row_id", "building_id", "created_at", "updated_at")
    mismatches: list[dict] = []
    for col in col_meta:
        if col["name"] in SKIP:
            continue
        label = col["label"]
        ctype = col.get("type", "text")
        actual = field_types.get(label)
        expected = _expected_cn_type(ctype) if region == "cn" else None
        if expected and actual and actual != expected:
            mismatches.append({"field": label, "expected": expected, "actual": actual})

    # 若存在字段类型不匹配且当前数据为空，自动创建 _v2 新表保留正确类型，避免向旧表写入。
    # 若旧表已有用户数据，则保留旧表不动，仅标记 needs_rebuild 由用户决定。
    if mismatches:
        old_count_in_datasheet = len(await client.list_records(datasheet_id))
        if old_count_in_datasheet == 0:
            # 自动创建 _v2 表
            v2_name = f"{datasheet_name}_v2"
            existing_v2 = await client.find_datasheet_by_name(space_id, v2_name)
            if existing_v2:
                datasheet_id = existing_v2["id"]
                actual_datasheet_name = existing_v2.get("name", v2_name)
            else:
                fields = map_fields(col_meta, region=region)
                datasheet_id = await client.create_datasheet(space_id, v2_name, fields)
                actual_datasheet_name = v2_name
                recreate_reason = f"原表字段类型错误（{mismatches[0]['field']}: {mismatches[0]['actual']}→{mismatches[0]['expected']}），已自动创建 {v2_name}"
                logger.info("[apitable] 自动重建 datasheet: %s → %s", datasheet_name, v2_name)
            # 重新读取新表的字段类型
            actual_fields = await client.list_fields(datasheet_id)
            field_types = {f["name"]: f.get("type", "Text") for f in actual_fields}
            mismatches = []  # 新表已无 mismatch

    # 3. 清空旧数据
    old_records = await client.list_records(datasheet_id)
    if old_records:
        old_ids = [r["recordId"] for r in old_records]
        await client.delete_records(datasheet_id, old_ids)
        logger.info("[apitable] 清空 %d 条旧记录", len(old_ids))

    # 4. 写入新数据（按实际字段类型适配值格式）
    apitable_records = [
        {"fields": transform_record(r, col_meta, region=region, field_types=field_types)}
        for r in records
    ]
    written = await client.batch_create_records(datasheet_id, apitable_records)
    logger.info("[apitable] %s 同步完成: %d/%d", entity, written, len(records))

    return {
        "entity": entity,
        "datasheet_id": datasheet_id,
        "datasheet_name": actual_datasheet_name,
        "region": region,
        "old_count": len(old_records),
        "new_count": len(records),
        "written": written,
        "field_type_mismatches": mismatches,
        "needs_rebuild": len(mismatches) > 0,
        "recreate_reason": recreate_reason,
    }


def _expected_cn_type(ctype: str) -> str | None:
    """返回 CN 版该内部类型对应的 vika 字段类型。"""
    if ctype in ("number", "currency"):
        return CN_TYPE_NUMBER
    if ctype == "enum":
        return CN_TYPE_SINGLESELECT
    if ctype in ("date", "datetime"):
        return CN_TYPE_DATETIME
    return CN_TYPE_TEXT


async def verify_token() -> dict:
    """验证 Token 是否有效（ping spaces 端点）。"""
    settings = get_settings()
    if not settings.apitable_configured:
        raise APITableError("APITable 未配置")
    client = APITableClient(token=settings.apitable_token, base_url=settings.apitable_base_url)
    return await client.ping()
