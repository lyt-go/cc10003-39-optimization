"""口径影响分析的示例数据。

只补充“指标换版 → 影响分析 → 验证执行”这条链路需要的表，不动原有
``seed.py`` 里的通用样例，避免影响其他模块的页面。
"""
from __future__ import annotations

from typing import Any

from app import impact_keys as keys

METRIC_ID = "METR-ORDER_COUNT"
ORDER_TASK = "DATA-ORDER_COUNT"
REFUND_TASK = "DATA-REFUND_RATE"
CURRENT_REVISION = "r3"
PREVIOUS_REVISION = "r2"

ORDER_BASELINE = "s3://warehouse/metrics/order_count/baseline"
REFUND_BASELINE = "s3://warehouse/metrics/refund_rate/baseline"
ORDER_GRAIN_R2 = "order_date,region"
ORDER_GRAIN_R3 = "order_date,region,channel"
REFUND_GRAIN = "user_id"

_ORDER_FORMULA = "sum(paid_order_amount) / count(distinct order_id)"
_REFUND_FORMULA = "sum(refund_amount) / sum(paid_order_amount)"


def _revision(
    revision: str,
    seq: int,
    grain: str,
    *,
    previous: dict[str, Any] | None,
    formula: str = _ORDER_FORMULA,
    baseline_uri: str = ORDER_BASELINE,
    compare_axis: str = "order_date",
    tolerance: float = 0.01,
) -> dict[str, Any]:
    formula_fingerprint = keys.formula_hash(formula)
    grain_fingerprint = keys.grain_hash(grain)
    digest = keys.baseline_digest(
        baseline_uri,
        formula_fingerprint=formula_fingerprint,
        grain_fingerprint=grain_fingerprint,
        compare_axis=compare_axis,
    )
    row = {
        "id": seq,
        "metric_id": METRIC_ID,
        "metric_revision": revision,
        "revision_seq": seq,
        "status": "已发布",
        "formula": formula,
        "formula_hash": formula_fingerprint,
        "aggregation_grain": grain,
        "grain_hash": grain_fingerprint,
        "compare_axis": compare_axis,
        "tolerance": tolerance,
        "baseline_uri": baseline_uri,
        "baseline_digest": digest,
        "created_at": f"2026-0{seq}-01T02:00:00+00:00",
    }
    row["changed_fields"] = keys.diff_changed_fields(previous, row)
    return row


_REVISION_R2 = _revision(PREVIOUS_REVISION, 2, ORDER_GRAIN_R2, previous=None)
_REVISION_R3 = _revision(CURRENT_REVISION, 3, ORDER_GRAIN_R3, previous=_REVISION_R2)

_ORDER_SAMPLES = [
    {
        "sample_id": "SAMPLE-LEAP-0229",
        "label": "02-29 闰日",
        "kind": "leap_day",
        "expected": 1000,
        "observed": 1008,
        "note": "闰日分区完整，相对差异 0.8% 在阈值内，判为通过",
    },
    {
        "sample_id": "SAMPLE-MONTH-FIRST",
        "label": "跨月首日",
        "kind": "month_first_day",
        "expected": 1200,
        "observed": 1272,
        "note": "跨月首日渠道分摊重复计入，相对差异 6% 超过阈值，判为异常",
    },
    {
        "sample_id": "SAMPLE-EMPTY-REGION",
        "label": "空地区",
        "kind": "empty_region",
        "expected": 300,
        "observed": None,
        "gap_reason": "空地区分区尚未补齐，保留样本缺口等待补数",
    },
]

_REFUND_SAMPLES = [
    {
        "sample_id": "SAMPLE-REFUND-0229",
        "label": "02-29 闰日",
        "kind": "leap_day",
        "expected": 0.08,
        "observed": 0.0802,
        "note": "退款率闰日样本在阈值内",
    },
    {
        "sample_id": "SAMPLE-REFUND-MONTH-FIRST",
        "label": "跨月首日",
        "kind": "month_first_day",
        "expected": 0.11,
        "observed": 0.1104,
        "note": "退款率跨月首日样本在阈值内",
    },
    {
        "sample_id": "SAMPLE-REFUND-EMPTY-REGION",
        "label": "空地区",
        "kind": "empty_region",
        "expected": 0.05,
        "observed": 0.0498,
        "note": "退款率空地区样本在阈值内",
    },
]


def _task(
    task_id: int,
    code: str,
    name: str,
    grain: str,
    baseline_uri: str,
    samples: list[dict[str, Any]],
    *,
    status: str = "验证中",
    revision: str = CURRENT_REVISION,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "status": status,
        "pending": status != "已验证",
        "abnormal": False,
        "任务编号": code,
        "验证数据集": name,
        "验证类型": "指标口径回归",
        "基准数据": baseline_uri,
        "对比维度": "order_date",
        "差异阈值": 0.01,
        "验证周期": "每日",
        "最近验证结果": "待验证",
        "metric_id": METRIC_ID,
        "metric_revision": revision,
        "aggregation_grain": grain,
        "grain_hash": keys.grain_hash(grain),
        "baseline_uri": baseline_uri,
        "compare_axis": "order_date",
        "tolerance": 0.01,
        "samples": samples,
        "failure_mode": None,
    }


_ORDER_TASK = _task(1, ORDER_TASK, "订单量口径验证", ORDER_GRAIN_R2, ORDER_BASELINE, _ORDER_SAMPLES)
_ORDER_TASK["failure_mode"] = "first_attempt"

# 退款率任务绑定自己的基准与粒度：r3 只改订单量的聚合粒度，它不会命中影响键。
# 种子里的任务状态如实反映 r2 的完整结论，执行期间不会被改写。
_REFUND_TASK = _task(
    2, REFUND_TASK, "退款率口径验证", REFUND_GRAIN, REFUND_BASELINE, _REFUND_SAMPLES,
    status="已验证", revision=PREVIOUS_REVISION,
)
_REFUND_TASK["最近验证结果"] = "通过"

# r2 的历史完整结论：两个任务都已完成，供 r3 影响分析时复用/对比。
_R2_ORDER_CONCLUSION = {
    "id": 1,
    "task_code": ORDER_TASK,
    "task_name": "订单量口径验证",
    "metric_id": METRIC_ID,
    "metric_revision": PREVIOUS_REVISION,
    "run_id": "RUN-METR-ORDER_COUNT-r2-LEGACY",
    "impact_key": keys.impact_key(
        METRIC_ID, keys.grain_hash(ORDER_GRAIN_R2), ORDER_BASELINE, 0.01
    ),
    "grain_hash": keys.grain_hash(ORDER_GRAIN_R2),
    "baseline_uri": ORDER_BASELINE,
    "baseline_digest": _REVISION_R2["baseline_digest"],
    "compare_axis": "order_date",
    "tolerance": 0.01,
    "status": "complete",
    "verdict": "通过",
    "task_state": "已验证",
    "has_abnormal_sample": False,
    "has_gap": False,
    "sample_results": [
        {"sample_id": "SAMPLE-LEAP-0229", "state": "passed", "verdict": "通过"},
        {"sample_id": "SAMPLE-MONTH-FIRST", "state": "passed", "verdict": "通过"},
        {"sample_id": "SAMPLE-EMPTY-REGION", "state": "passed", "verdict": "通过"},
    ],
    "version_seq": 1,
    "created_at": "2026-02-02T03:00:00+00:00",
    "updated_at": "2026-02-02T03:00:00+00:00",
    "completed_at": "2026-02-02T03:00:00+00:00",
}

_R2_REFUND_CONCLUSION = {
    "id": 2,
    "task_code": REFUND_TASK,
    "task_name": "退款率口径验证",
    "metric_id": METRIC_ID,
    "metric_revision": PREVIOUS_REVISION,
    "run_id": "RUN-METR-ORDER_COUNT-r2-LEGACY",
    "impact_key": keys.impact_key(
        METRIC_ID, keys.grain_hash(REFUND_GRAIN), REFUND_BASELINE, 0.01
    ),
    "grain_hash": keys.grain_hash(REFUND_GRAIN),
    "baseline_uri": REFUND_BASELINE,
    "baseline_digest": keys.baseline_digest(
        REFUND_BASELINE,
        formula_fingerprint=keys.formula_hash(_REFUND_FORMULA),
        grain_fingerprint=keys.grain_hash(REFUND_GRAIN),
        compare_axis="order_date",
    ),
    "compare_axis": "order_date",
    "tolerance": 0.01,
    "status": "complete",
    "verdict": "通过",
    "task_state": "已验证",
    "has_abnormal_sample": False,
    "has_gap": False,
    "sample_results": [
        {"sample_id": "SAMPLE-REFUND-0229", "state": "passed", "verdict": "通过"},
        {"sample_id": "SAMPLE-REFUND-MONTH-FIRST", "state": "passed", "verdict": "通过"},
        {"sample_id": "SAMPLE-REFUND-EMPTY-REGION", "state": "passed", "verdict": "通过"},
    ],
    "version_seq": 1,
    "created_at": "2026-02-02T03:00:00+00:00",
    "updated_at": "2026-02-02T03:00:00+00:00",
    "completed_at": "2026-02-02T03:00:00+00:00",
}

# r2 时代仍然有效的 active 快照；r3 的 building 快照追加期间它不能被清空。
_ACTIVE_SNAPSHOT = {
    "id": 1,
    "snapshot_id": "SNAP-METR-ORDER_COUNT-r2",
    "run_id": "RUN-METR-ORDER_COUNT-r2-LEGACY",
    "metric_id": METRIC_ID,
    "metric_revision": PREVIOUS_REVISION,
    "state": "active",
    "created_at": "2026-02-02T03:00:00+00:00",
    "completed_at": "2026-02-02T03:00:00+00:00",
    "items": [
        {"task_code": ORDER_TASK, "status": "complete", "verdict": "通过", "metric_revision": PREVIOUS_REVISION},
        {"task_code": REFUND_TASK, "status": "complete", "verdict": "通过", "metric_revision": PREVIOUS_REVISION},
    ],
    # 旧 active 快照里保存的是 r3 之前的结果集，building 期间仍按它对外读。
    "legacy_active": True,
}

IMPACT_SEED_ROWS: dict[str, list[dict[str, Any]]] = {
    "metric_revision": [dict(_REVISION_R2), dict(_REVISION_R3)],
    "metric_conclusion": [dict(_R2_ORDER_CONCLUSION), dict(_R2_REFUND_CONCLUSION)],
    "anomaly_snapshot": [dict(_ACTIVE_SNAPSHOT)],
    "impact_run": [],
    "impact_member": [],
}

# 覆盖 dataval 的示例任务：需要带指标绑定与样本，才能参与影响分析。
DATAVAL_IMPACT_ROWS = [dict(_ORDER_TASK), dict(_REFUND_TASK)]
