"""口径影响分析的键计算工具。

这一层只做纯函数，不依赖数据仓库，方便 seed、service 与测试共用同一套口径：

- ``aggregation_grain`` 归一化后生成 ``grain_hash``；
- ``baseline_uri`` + 公式/粒度指纹生成 ``baseline_digest``；
- 影响键固定为 ``(metric_id, grain_hash, baseline_uri, tolerance)``。

所有比较都走规范化字符串，避免浮点误差和字段顺序导致同一个版本被拆成多个 run。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

_GRAIN_SPLIT_RE = r"[,，、|/]+"
_TOLERANCE_QUANT = Decimal("0.000001")


def normalize_text(value: Any) -> str:
    """把空值统一成空串，并压掉首尾空白。"""
    if value is None:
        return ""
    return str(value).strip()


def normalize_grain(value: Any) -> str:
    """归一化聚合粒度。

    支持 ``"order_date,region"``、``["order_date", "region"]`` 等写法；
    字段顺序保留（粒度顺序有时参与计算），但去掉空白与空字段。
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        parts = [normalize_text(item) for item in value]
    else:
        import re

        parts = [normalize_text(item) for item in re.split(_GRAIN_SPLIT_RE, str(value))]
    parts = [part for part in parts if part]
    return ",".join(parts)


def grain_hash(value: Any) -> str:
    """聚合粒度指纹；只由归一化后的粒度决定。"""
    normalized = normalize_grain(value)
    payload = f"grain:{normalized}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def formula_hash(value: Any) -> str:
    """计算公式指纹；公式不变时只改粒度不会动这一项。"""
    normalized = " ".join(normalize_text(value).split())
    if not normalized:
        return ""
    return hashlib.sha256(f"formula:{normalized}".encode("utf-8")).hexdigest()[:16]


def normalize_compare_axis(value: Any) -> str:
    """对比维度归一化，允许列表写法。"""
    return normalize_grain(value)


def tolerance_key(value: Any) -> str:
    """差异阈值规范化为 6 位小数字符串，避免 0.01 与 0.010 被当成两个键。"""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        number = Decimal("0.01")
    return str(number.quantize(_TOLERANCE_QUANT))


def normalize_tolerance(value: Any) -> float:
    return float(tolerance_key(value))


def baseline_digest(
    baseline_uri: Any,
    *,
    formula_fingerprint: str = "",
    grain_fingerprint: str = "",
    compare_axis: Any = "",
) -> str:
    """基准内容指纹。

    基准 URI 不变、但公式/粒度/对比维度变化时，digest 会变化，
    用于判定“同一个基准契约的内容已经换了”，而不是简单按 URI 重新跑全部任务。
    """
    payload = json.dumps(
        {
            "baseline_uri": normalize_text(baseline_uri),
            "formula_hash": formula_fingerprint,
            "grain_hash": grain_fingerprint,
            "compare_axis": normalize_compare_axis(compare_axis),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def impact_key_fields(
    metric_id: Any,
    grain_fingerprint: Any,
    baseline_uri: Any,
    tolerance: Any,
) -> dict[str, str]:
    """影响键的四个字段，统一成可展示、可比较的字符串。"""
    return {
        "metric_id": normalize_text(metric_id),
        "grain_hash": normalize_text(grain_fingerprint),
        "baseline_uri": normalize_text(baseline_uri),
        "tolerance": tolerance_key(tolerance),
    }


def impact_key(
    metric_id: Any,
    grain_fingerprint: Any,
    baseline_uri: Any,
    tolerance: Any,
) -> str:
    """四字段影响键的稳定哈希。"""
    fields = impact_key_fields(metric_id, grain_fingerprint, baseline_uri, tolerance)
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def diff_changed_fields(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    """对比相邻版本，列出真正变化的口径字段，作为 affected 的解释依据。

    ``grain_hash`` / ``baseline_digest`` 是派生指纹，不单独作为“改了什么”展示；
    它们参与影响判定，但展示层只保留公式、粒度、对比维度、阈值、基准这类可读字段。
    """
    if not previous:
        return ["initial_revision"]
    mappings: Iterable[tuple[str, str]] = (
        ("formula", "formula"),
        ("aggregation_grain", "aggregation_grain"),
        ("compare_axis", "compare_axis"),
        ("tolerance", "tolerance"),
        ("baseline_uri", "baseline_uri"),
    )
    changed: list[str] = []
    for label, field in mappings:
        before = previous.get(field)
        after = current.get(field)
        if field == "tolerance":
            before, after = tolerance_key(before), tolerance_key(after)
        elif field in {"aggregation_grain", "compare_axis"}:
            before, after = normalize_grain(before), normalize_grain(after)
        elif field == "formula":
            before = formula_hash(previous.get("formula") or previous.get("formula_hash"))
            after = formula_hash(current.get("formula") or current.get("formula_hash"))
        elif field == "baseline_uri":
            before, after = normalize_text(before), normalize_text(after)
        if before != after and label not in changed:
            changed.append(label)
    return changed or ["no_effective_change"]
