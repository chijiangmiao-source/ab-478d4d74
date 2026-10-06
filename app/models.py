"""规范化与稳定标识：相同业务输入 => 相同 export_id / 摘要。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

FIELD_ORDER = [
    "ship_id",
    "track_id",
    "timestamp",
    "longitude",
    "latitude",
    "sog",
    "cog",
    "heading",
    "depth",
    "note",
]

CANONICAL_VERSION = "marine-track/v1"


def stable_hash(data: Any) -> str:
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _normalize_value(value: Any) -> Any:
    """规范化字段值：浮点统一为保留 6 位小数字符串，其余转成标量。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # 避免 1.0 与 1.000000 之类的表示差异
        return round(value, 6)
    if value is None:
        return None
    return str(value)


def normalize_record(raw: dict) -> dict:
    """把单条航迹记录规范化为字段顺序固定的对象。"""
    if not isinstance(raw, dict):
        raise ValueError("每条记录必须是 JSON 对象")
    out: dict[str, Any] = {}
    for key in FIELD_ORDER:
        if key in raw:
            out[key] = _normalize_value(raw[key])
    extras = sorted(k for k in raw.keys() if k not in FIELD_ORDER)
    for key in extras:
        out[key] = _normalize_value(raw[key])
    return out


def normalize_payload(payload: Any) -> list[dict]:
    if not isinstance(payload, list) or not payload:
        raise ValueError("records 必须是非空数组")
    if len(payload) > 1000:
        raise ValueError("单次提交记录数不得超过 1000")
    return [normalize_record(r) for r in payload]


def normalize_rules(raw: Any) -> dict:
    """规则快照：遮蔽字段名数组规范化（去空白、去重、保序排序）。"""
    if raw is None:
        raw = {"mask_fields": []}
    if not isinstance(raw, dict):
        raise ValueError("rules 必须是对象")
    mask_fields = raw.get("mask_fields", [])
    if not isinstance(mask_fields, list):
        raise ValueError("rules.mask_fields 必须是数组")
    fields: list[str] = []
    seen = set()
    for item in mask_fields:
        name = str(item).strip()
        if not name:
            continue
        if name not in seen:
            seen.add(name)
            fields.append(name)
    norm = {"version": CANONICAL_VERSION, "mask_fields": sorted(fields)}
    return norm


def make_export_id(records_norm: list[dict], rules_norm: dict) -> str:
    digest = stable_hash({"v": CANONICAL_VERSION, "records": records_norm, "rules": rules_norm})
    return f"exp_{digest[:32]}"


def make_business_key(records_norm: list[dict]) -> str:
    """业务等价键：同一船舶的同一航迹（ship_id + track_id）即视为同一笔业务。

    身份字段缺失时退化为全部规范化记录的哈希，保证键仍然确定。
    """
    identities = []
    for rec in records_norm:
        ship = rec.get("ship_id")
        track = rec.get("track_id")
        if ship is None and track is None:
            identities.append(rec)
        else:
            identities.append([ship, track])
    return stable_hash({"v": CANONICAL_VERSION, "identity": identities})


def render_artifact(export_id: str, records_norm: list[dict], rules_norm: dict) -> bytes:
    """按冻结快照应用遮蔽规则，生成导出工件内容。"""
    masked = set(rules_norm["mask_fields"])
    rows = []
    for rec in records_norm:
        row = {k: ("***MASKED***" if k in masked else v) for k, v in rec.items()}
        rows.append(row)
    doc = {
        "export_id": export_id,
        "format_version": CANONICAL_VERSION,
        "rule_snapshot": rules_norm,
        "record_count": len(rows),
        "records": rows,
    }
    return (json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
