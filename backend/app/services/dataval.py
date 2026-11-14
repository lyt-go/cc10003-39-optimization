"""数据验证业务规则：状态流转、字段校验与筛选口径都收在这里。

验证任务的“已验证”不能由人工动作直接写入：必须以 ``metric_conclusion``
里完整落库的结论为准；存在样本缺口时只能停在验证中，等下一次续跑。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "dataval"
CONCLUSION_MODULE = "metric_conclusion"
REQUIRED_FIELDS = ["任务编号", "验证数据集", "验证类型"]
STATUS_ORDER = ["草稿", "验证中", "已验证", "异常"]
ACTION_RULES = {"提交验证": "验证中", "标记已验证": "已验证", "标记异常": "异常"}
NEGATIVE_ACTIONS = []
COMPLETE_STATES = {"complete", "abnormal"}


def _task_code(entry: dict[str, Any]) -> str:
    return str(entry.get("任务编号") or "").strip()


def _latest_complete_conclusion(task_code: str) -> dict[str, Any] | None:
    rows = [
        row for row in store.rows(CONCLUSION_MODULE)
        if str(row.get("task_code") or "").strip() == task_code
        and row.get("status") in COMPLETE_STATES
    ]
    return max(rows, key=lambda row: int(row.get("id", 0))) if rows else None


def _latest_conclusion(task_code: str) -> dict[str, Any] | None:
    rows = [
        row for row in store.rows(CONCLUSION_MODULE)
        if str(row.get("task_code") or "").strip() == task_code
    ]
    return max(rows, key=lambda row: int(row.get("id", 0))) if rows else None


class DatavalService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._public(row) for row in rows[start:start + size]], total

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        public = dict(row)
        conclusion = _latest_complete_conclusion(_task_code(row))
        public["metric_id"] = row.get("metric_id")
        # 任务自身绑定的是当前目标版本；结论版本单独给出，避免复用结论被误读成已按新版本验证。
        public["metric_revision"] = row.get("metric_revision")
        public["conclusion_revision"] = (conclusion or {}).get("metric_revision")
        public["conclusion_status"] = (conclusion or {}).get("status")
        return public

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._public(row) if row else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return self._public(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"验证任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据验证可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if target == "已验证":
            conclusion = _latest_complete_conclusion(_task_code(entry))
            latest = _latest_conclusion(_task_code(entry))
            # 只要同版本还有未到终态的结论（样本缺口/执行中），就不能算已验证。
            if latest is not None and latest.get("status") not in COMPLETE_STATES:
                return None, (
                    "该任务同版本仍有样本缺口未补齐，不能提前标成已验证；"
                    "请先补数并重新执行影响范围"
                )
            if conclusion is None:
                return None, "该任务还没有完整落库的验证结论，存在样本缺口时不能提前标成已验证"
            target_revision = str(entry.get("metric_revision") or "").strip()
            if target_revision and conclusion.get("metric_revision") != target_revision:
                return None, (
                    f"任务已绑定口径版本 {target_revision}，但只有 "
                    f"{conclusion.get('metric_revision')} 的完整结论，不能提前标成已验证"
                )
            if conclusion.get("status") != "complete":
                return None, (
                    f"该任务最近一次完整结论为「{conclusion.get('verdict')}」，"
                    "异常或待补数状态下不能标成已验证"
                )
            entry["metric_revision"] = conclusion.get("metric_revision")
            entry["最近验证结果"] = conclusion.get("verdict")
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return self._public(entry), f"验证任务已{action}"
