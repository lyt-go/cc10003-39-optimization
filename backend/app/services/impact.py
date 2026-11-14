"""指标口径换版到数据验证执行的影响分析与缓存边界。

职责边界：

1. **影响分析**（``analyze``）：读取指标版本，和历史版本比较公式、聚合粒度、
   对比维度、差异阈值与基准内容，按 ``(metric_id, grain_hash, baseline_uri,
   tolerance)`` 生成 impact_run；输出 affected / reused / pending / abnormal。
2. **结论缓存**（``metric_conclusion``）：只有完整落库的结论才可复用；结论记录
   一律绑定 ``metric_revision``。未命中的任务不追加新记录。
3. **执行与快照**（``execute``、``backfill``）：只执行 affected 成员；先追加
   building 快照，旧 active 快照在 building 完成前保持可读；重复触发同一 run
   不会重复处理，失败成员从样本缺口继续。
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from app import impact_keys as keys
from app.store import store

MODULE_TASKS = "dataval"
MODULE_METRIC = "metric"
MODULE_REVISIONS = "metric_revision"
MODULE_RUNS = "impact_run"
MODULE_MEMBERS = "impact_member"
MODULE_CONCLUSIONS = "metric_conclusion"
MODULE_SNAPSHOTS = "anomaly_snapshot"

COMPLETE_CONCLUSION_STATES = {"complete", "abnormal"}
GAP_SAMPLE_STATE = "gap"

_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _task_grain_fingerprint(task: dict[str, Any]) -> str:
    return keys.normalize_text(task.get("grain_hash") or keys.grain_hash(
        task.get("aggregation_grain") or task.get("统计粒度")
    ))


def _task_baseline(task: dict[str, Any]) -> str:
    return keys.normalize_text(task.get("baseline_uri") or task.get("基准数据"))


def _task_axis(task: dict[str, Any]) -> str:
    return keys.normalize_compare_axis(task.get("compare_axis") or task.get("对比维度"))


def _task_tolerance(task: dict[str, Any]) -> str:
    return keys.tolerance_key(task.get("tolerance") or task.get("差异阈值"))


def _task_code(task: dict[str, Any]) -> str:
    return keys.normalize_text(task.get("任务编号"))


class ImpactService:
    # ------------------------------------------------------------------
    # 指标版本
    # ------------------------------------------------------------------
    def _revisions(self, metric_id: str) -> list[dict[str, Any]]:
        rows = [
            row for row in store.rows(MODULE_REVISIONS)
            if keys.normalize_text(row.get("metric_id")) == keys.normalize_text(metric_id)
        ]
        return sorted(rows, key=lambda row: int(row.get("revision_seq", 0)))

    def _metric_by_id(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE_METRIC, entry_id)

    def _revision(self, metric_id: str, revision: str) -> dict[str, Any] | None:
        for row in self._revisions(metric_id):
            if keys.normalize_text(row.get("metric_revision")) == keys.normalize_text(revision):
                return row
        return None

    def list_revisions(self, metric_id: str) -> list[dict[str, Any]]:
        return [self._revision_public(row) for row in self._revisions(metric_id)]

    def _revision_public(self, row: dict[str, Any]) -> dict[str, Any]:
        public = dict(row)
        public["impact_key_fields"] = keys.impact_key_fields(
            row.get("metric_id"), row.get("grain_hash"), row.get("baseline_uri"), row.get("tolerance")
        )
        return public

    def publish_revision(
        self,
        entry_id: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """登记并发布一个新口径版本，随后立即做影响分析。

        发布动作本身就是版本演进的入口：写完 revision 后调用 ``analyze``，
        把“哪些验证任务需要重验”作为返回值的一部分带出去。
        """
        with _LOCK:
            metric = self._metric_by_id(entry_id)
            if metric is None:
                return None, f"指标定义 {entry_id} 不存在或已归档", None
            metric_id = keys.normalize_text(metric.get("指标编号"))
            revisions = self._revisions(metric_id)
            latest = revisions[-1] if revisions else None
            seq = int((latest or {}).get("revision_seq", 0)) + 1
            revision_label = keys.normalize_text(values.get("metric_revision")) or f"r{seq}"
            if any(keys.normalize_text(row.get("metric_revision")) == revision_label for row in revisions):
                return None, f"版本 {revision_label} 已存在，请使用新的版本号", None

            base = latest or {}
            formula = keys.normalize_text(values.get("formula") or metric.get("计算公式"))
            grain = keys.normalize_grain(
                values.get("aggregation_grain") or metric.get("统计粒度") or base.get("aggregation_grain")
            )
            compare_axis = keys.normalize_compare_axis(
                values.get("compare_axis") or metric.get("对比维度") or base.get("compare_axis")
            )
            tolerance = keys.normalize_tolerance(
                values.get("tolerance") or base.get("tolerance") or 0.01
            )
            baseline_uri = keys.normalize_text(
                values.get("baseline_uri") or base.get("baseline_uri")
                or f"s3://metrics/{metric_id.lower()}/baseline"
            )
            formula_fingerprint = keys.formula_hash(formula)
            grain_fingerprint = keys.grain_hash(grain)

            row = {
                "id": _next_id(store.rows(MODULE_REVISIONS)),
                "metric_id": metric_id,
                "metric_revision": revision_label,
                "revision_seq": seq,
                "status": "已发布",
                "formula": formula,
                "formula_hash": formula_fingerprint,
                "aggregation_grain": grain,
                "grain_hash": grain_fingerprint,
                "compare_axis": compare_axis,
                "tolerance": tolerance,
                "baseline_uri": baseline_uri,
                "baseline_digest": keys.baseline_digest(
                    baseline_uri,
                    formula_fingerprint=formula_fingerprint,
                    grain_fingerprint=grain_fingerprint,
                    compare_axis=compare_axis,
                ),
                "changed_fields": keys.diff_changed_fields(latest, {
                    "formula": formula,
                    "formula_hash": formula_fingerprint,
                    "aggregation_grain": grain,
                    "grain_hash": grain_fingerprint,
                    "compare_axis": compare_axis,
                    "tolerance": tolerance,
                    "baseline_uri": baseline_uri,
                    "baseline_digest": keys.baseline_digest(
                        baseline_uri,
                        formula_fingerprint=formula_fingerprint,
                        grain_fingerprint=grain_fingerprint,
                        compare_axis=compare_axis,
                    ),
                }),
                "created_at": _now(),
            }
            store.rows(MODULE_REVISIONS).append(row)
            metric["口径版本"] = revision_label
            metric["统计粒度"] = grain
            metric["计算公式"] = formula
            metric["status"] = "已发布"
            metric["pending"] = False

            run = self.analyze(metric_id, revision_label)
            return row, f"指标口径已发布为 {revision_label}，影响分析已生成", run

    # ------------------------------------------------------------------
    # 影响分析
    # ------------------------------------------------------------------
    def _task_public(self, task: dict[str, Any], *, target_revision: str) -> dict[str, Any]:
        return {
            "task_code": _task_code(task),
            "task_id": task.get("id"),
            "task_name": task.get("验证数据集"),
            "metric_id": task.get("metric_id"),
            "metric_revision": target_revision,
            "impact_key": keys.impact_key(
                task.get("metric_id"),
                _task_grain_fingerprint(task),
                _task_baseline(task),
                _task_tolerance(task),
            ),
            "impact_key_fields": keys.impact_key_fields(
                task.get("metric_id"),
                _task_grain_fingerprint(task),
                _task_baseline(task),
                _task_tolerance(task),
            ),
            "compare_axis": _task_axis(task),
            "tolerance": keys.normalize_tolerance(_task_tolerance(task)),
            "status": task.get("status"),
            "verdict": "待验证",
            "conclusion_status": "missing",
            "conclusion_revision": None,
            "needs_revalidation": False,
            "reason": "口径契约未变化",
        }

    def analyze(
        self,
        metric_id: str,
        revision: str | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        with _LOCK:
            metric_id = keys.normalize_text(metric_id)
            revisions = self._revisions(metric_id)
            if not revisions:
                return {"ok": False, "message": f"指标 {metric_id} 还没有已发布版本"}
            current = (
                self._revision(metric_id, revision) if revision else revisions[-1]
            )
            if current is None:
                return {"ok": False, "message": f"指标 {metric_id} 的版本 {revision} 不存在"}
            index = revisions.index(current)
            previous = revisions[index - 1] if index > 0 else None

            current_fields = keys.impact_key_fields(
                metric_id, current.get("grain_hash"), current.get("baseline_uri"), current.get("tolerance")
            )
            run_key = keys.impact_key(
                metric_id, current.get("grain_hash"), current.get("baseline_uri"), current.get("tolerance")
            )
            changed_fields = current.get("changed_fields") or keys.diff_changed_fields(previous, current)

            existing = self._find_run(metric_id, current.get("metric_revision"), run_key)
            if existing and not force:
                payload = self._run_detail(existing, deduplicated=True)
                payload["message"] = "同一 impact_run 已存在，本次只做一次影响登记，未重复创建"
                return payload

            run_id = self._make_run_code(metric_id, current.get("metric_revision"), run_key)
            run = {
                "id": _next_id(store.rows(MODULE_RUNS)),
                "run_id": run_id,
                "metric_id": metric_id,
                "metric_revision": current.get("metric_revision"),
                "previous_revision": (previous or {}).get("metric_revision"),
                "impact_key": run_key,
                "impact_key_fields": current_fields,
                "grain_hash": current.get("grain_hash"),
                "baseline_uri": current.get("baseline_uri"),
                "tolerance": keys.normalize_tolerance(current.get("tolerance")),
                "baseline_digest": current.get("baseline_digest"),
                "previous_baseline_digest": (previous or {}).get("baseline_digest"),
                "compare_axis": current.get("compare_axis"),
                "changed_fields": changed_fields,
                "status": "analyzed",
                "created_at": _now(),
                "updated_at": _now(),
                "execution_count": 0,
                "executing": False,
                "affected_task_codes": [],
            }
            store.rows(MODULE_RUNS).append(run)
            affected = self._affected_tasks(current, previous)
            run["affected_task_codes"] = [_task_code(task) for task in affected]
            for task in affected:
                self._ensure_member(run, task)
            payload = self._run_detail(run, deduplicated=False)
            payload["message"] = f"已生成 impact_run {run_id}，affected {len(payload['sets']['affected'])} 条"
            return payload

    def _find_run(self, metric_id: str, revision: Any, run_key: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE_RUNS):
            if (
                row.get("metric_id") == metric_id
                and row.get("metric_revision") == revision
                and row.get("impact_key") == run_key
            ):
                return row
        return None

    def _make_run_code(self, metric_id: str, revision: Any, run_key: str) -> str:
        slug = "".join(ch for ch in metric_id if ch.isalnum() or ch in "-_") or "METRIC"
        return f"RUN-{slug}-{keys.normalize_text(revision)}-{run_key[:8]}"

    def _is_affected(
        self,
        task: dict[str, Any],
        changed_fields: Iterable[str],
        current: dict[str, Any],
        previous: dict[str, Any] | None,
    ) -> tuple[bool, list[str]]:
        """按任务实际绑定的口径契约判断是否命中影响集合。

        判定核心是 ``baseline_digest``：分别用“上一版口径”和“当前版口径”
        计算任务视角的基准指纹（任务绑定自己的基准/粒度/对比维度时不跟随指标变化），
        指纹不同即说明该任务的比较契约被改动，需要重验；指纹相同则沿用历史结论。
        差异阈值单独判断，因为它不进入 baseline_digest，但会改变通过/异常边界。
        """
        changed = set(changed_fields)
        if not changed or changed == {"no_effective_change"}:
            return False, []

        previous = previous or {}
        task_baseline = _task_baseline(task)
        task_grain = _task_grain_fingerprint(task)
        task_axis = _task_axis(task)
        task_tolerance = _task_tolerance(task)

        previous_baseline = keys.normalize_text(previous.get("baseline_uri"))
        current_baseline = keys.normalize_text(current.get("baseline_uri"))
        baseline_bound = bool(task_baseline) and task_baseline in {previous_baseline, current_baseline}

        previous_grain = keys.normalize_text(previous.get("grain_hash"))
        current_grain = keys.normalize_text(current.get("grain_hash"))
        grain_bound = bool(task_grain) and task_grain in {previous_grain, current_grain}

        previous_axis = keys.normalize_compare_axis(previous.get("compare_axis"))
        current_axis = keys.normalize_compare_axis(current.get("compare_axis"))
        axis_bound = bool(task_axis) and task_axis in {previous_axis, current_axis}

        tolerance_bound = task_tolerance in {
            keys.tolerance_key(previous.get("tolerance")),
            keys.tolerance_key(current.get("tolerance")),
        }

        def task_digest(revision: dict[str, Any]) -> str:
            """按任务绑定视角计算某一版本的基准指纹。"""
            baseline_uri = (
                keys.normalize_text(revision.get("baseline_uri"))
                if baseline_bound else task_baseline
            )
            grain = (
                keys.normalize_text(revision.get("grain_hash"))
                if grain_bound else task_grain
            )
            axis = (
                keys.normalize_compare_axis(revision.get("compare_axis"))
                if axis_bound else task_axis
            )
            return keys.baseline_digest(
                baseline_uri,
                formula_fingerprint=keys.normalize_text(revision.get("formula_hash")),
                grain_fingerprint=grain,
                compare_axis=axis,
            )

        previous_digest = task_digest(previous) if previous else ""
        current_digest = task_digest(current)
        digest_changed = bool(previous) and previous_digest != current_digest

        reasons: list[str] = []
        if "formula" in changed and baseline_bound:
            reasons.append("计算公式变化（formula_hash 变化，baseline_digest 随基准内容更新）")
        if "aggregation_grain" in changed and grain_bound and baseline_bound:
            reasons.append(
                "聚合粒度由 "
                f"{keys.normalize_grain(previous.get('aggregation_grain')) or '—'} 变为 "
                f"{keys.normalize_grain(current.get('aggregation_grain')) or '—'}，baseline_digest 变化"
            )
        if "compare_axis" in changed and axis_bound and baseline_bound:
            reasons.append("对比维度变化，历史对比口径不再等价")
        if "baseline_uri" in changed and task_baseline == previous_baseline:
            reasons.append("基准数据地址变化（baseline_uri 变化）")
        if "initial_revision" in changed and baseline_bound:
            reasons.append("首个版本，任务尚无同版本结论")
        if (
            "tolerance" in changed
            and baseline_bound
            and tolerance_bound
            and keys.tolerance_key(previous.get("tolerance")) != keys.tolerance_key(current.get("tolerance"))
        ):
            reasons.append("差异阈值变化，原结论的通过/异常边界失效")
        if digest_changed and not reasons:
            reasons.append(
                f"baseline_digest 由 {previous_digest[:12]} 变为 {current_digest[:12]}，任务的比较契约已变化"
            )
        return bool(reasons), reasons

    def _affected_tasks(
        self,
        current: dict[str, Any],
        previous: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        metric_id = keys.normalize_text(current.get("metric_id"))
        changed = current.get("changed_fields") or keys.diff_changed_fields(previous, current)
        affected: list[dict[str, Any]] = []
        for task in store.rows(MODULE_TASKS):
            if keys.normalize_text(task.get("metric_id")) != metric_id:
                continue
            hit, _ = self._is_affected(task, changed, current, previous)
            if hit:
                affected.append(task)
        return affected

    def _latest_complete_conclusion(self, task_code: str) -> dict[str, Any] | None:
        rows = [
            row for row in store.rows(MODULE_CONCLUSIONS)
            if row.get("task_code") == task_code and row.get("status") in COMPLETE_CONCLUSION_STATES
        ]
        if not rows:
            return None
        return max(rows, key=lambda row: (int(row.get("id", 0)), int(row.get("version_seq", 0))))

    def _run_detail(self, run: dict[str, Any], *, deduplicated: bool = False) -> dict[str, Any]:
        metric_id = keys.normalize_text(run.get("metric_id"))
        revision = keys.normalize_text(run.get("metric_revision"))
        previous_revision = run.get("previous_revision")
        current_revision_row = self._revision(metric_id, revision)
        previous_revision_row = self._revision(metric_id, previous_revision) if previous_revision else None
        changed = run.get("changed_fields") or []
        affected_codes = set(run.get("affected_task_codes") or [])
        member_by_code = {row.get("task_code"): row for row in self._members(run)}

        sets: dict[str, list[dict[str, Any]]] = {
            "affected": [], "reused": [], "pending": [], "abnormal": [],
        }
        for task in store.rows(MODULE_TASKS):
            if keys.normalize_text(task.get("metric_id")) != metric_id:
                continue
            code = _task_code(task)
            hit, reasons = self._is_affected(
                task, changed, current_revision_row or {}, previous_revision_row
            )
            if code in affected_codes:
                hit = True
            historical = self._latest_complete_conclusion(code)
            target_conclusion = self._conclusion_for_revision(code, revision)
            member = member_by_code.get(code)

            item = self._task_public(task, target_revision=revision)
            item["reason"] = "；".join(reasons) or "口径契约未变化"

            if hit:
                authoritative = target_conclusion
                item["needs_revalidation"] = True
                item["conclusion_revision"] = (authoritative or {}).get("metric_revision")
                item["metric_revision"] = revision
                sample_results = (authoritative or {}).get("sample_results") or self._member_samples(member)
                has_abnormal_sample = any(
                    result.get("state") == "abnormal" for result in sample_results
                )
                item["sample_results"] = sample_results
                if authoritative is None:
                    # 本次 run 尚未落完整结论：affected 任务只能算 pending，
                    # 不能因为历史上存在旧版本结论就提前显示“已验证”。
                    item["conclusion_status"] = "pending"
                    item["verdict"] = "待验证"
                    sets["affected"].append(item)
                    sets["pending"].append(item)
                    if member and member.get("last_error"):
                        item["failure"] = member.get("last_error")
                    if has_abnormal_sample:
                        # 已发现异常样本但仍有缺口：进入 abnormal 供关注，
                        # 同时保留 pending，任务状态不得提前写成已验证。
                        sets["abnormal"].append(item)
                    continue
                item["conclusion_status"] = authoritative.get("status")
                item["verdict"] = authoritative.get("verdict")
                item["baseline_digest"] = authoritative.get("baseline_digest")
                sets["affected"].append(item)
                if authoritative.get("status") == "abnormal":
                    sets["abnormal"].append(item)
            elif historical:
                item["needs_revalidation"] = False
                item["conclusion_status"] = historical.get("status")
                item["conclusion_revision"] = historical.get("metric_revision")
                item["metric_revision"] = historical.get("metric_revision")
                item["verdict"] = historical.get("verdict")
                item["baseline_digest"] = historical.get("baseline_digest")
                item["sample_results"] = historical.get("sample_results") or []
                item["reason"] = (
                    f"影响键未命中，沿用 {historical.get('metric_revision')} 的完整结论"
                )
                sets["reused"].append(item)
                if historical.get("status") == "abnormal":
                    sets["abnormal"].append(item)
            else:
                item["needs_revalidation"] = False
                item["reason"] = "影响键未命中，且没有可复用的完整历史结论"
                sets["pending"].append(item)

        return {
            "ok": True,
            "run_id": run.get("run_id"),
            "metric_id": metric_id,
            "metric_revision": revision,
            "previous_revision": previous_revision,
            "impact_key": run.get("impact_key"),
            "impact_key_fields": run.get("impact_key_fields"),
            "baseline_digest": run.get("baseline_digest"),
            "changed_fields": changed,
            "status": run.get("status"),
            "execution_count": run.get("execution_count", 0),
            "deduplicated": deduplicated,
            "sets": sets,
            "counts": {name: len(rows) for name, rows in sets.items()},
            "snapshots": self._snapshot_view(run),
            "revision": current_revision_row,
        }

    # ------------------------------------------------------------------
    # 执行与快照
    # ------------------------------------------------------------------
    def _ensure_member(self, run: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        code = _task_code(task)
        for member in store.rows(MODULE_MEMBERS):
            if member.get("run_id") == run.get("run_id") and member.get("task_code") == code:
                return member
        member = {
            "id": _next_id(store.rows(MODULE_MEMBERS)),
            "run_id": run.get("run_id"),
            "task_code": code,
            "task_id": task.get("id"),
            "status": "pending",
            "attempts": 0,
            "last_error": None,
            "samples": {},
            "backfills": {},
            "created_at": _now(),
            "finished_at": None,
        }
        store.rows(MODULE_MEMBERS).append(member)
        return member

    def _members(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(MODULE_MEMBERS)
            if row.get("run_id") == run.get("run_id")
        ]

    def _member_samples(self, member: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not member:
            return []
        samples = member.get("samples") or {}
        return list(samples.values()) if isinstance(samples, dict) else list(samples)

    def execute(self, run_id: str) -> dict[str, Any]:
        """执行一个 impact_run；重复触发只处理未完成的成员。

        - 持锁串行化同一指标的验证写操作；
        - 成员级失败被隔离，下一次调用从该成员未完成的样本继续；
        - 只有所有成员都拿到完整结论才提交 building 快照。
        """
        with _LOCK:
            run = self._find_run_by_code(run_id)
            if run is None:
                return {"ok": False, "message": f"impact_run {run_id} 不存在"}
            if run.get("status") == "completed":
                return {
                    "ok": True,
                    "message": "该 impact_run 已完成，本次未重复处理",
                    "deduplicated": True,
                    "run": self._run_detail(run, deduplicated=True),
                }
            if run.get("executing"):
                return {
                    "ok": False,
                    "message": "同一 impact_run 正在执行，本次未重复处理",
                    "busy": True,
                    "run": self._run_detail(run),
                }
            conflict = next(
                (
                    row for row in store.rows(MODULE_RUNS)
                    if row is not run
                    and row.get("metric_id") == run.get("metric_id")
                    and row.get("executing")
                ),
                None,
            )
            if conflict is not None:
                return {
                    "ok": False,
                    "message": (
                        f"指标 {run.get('metric_id')} 正在执行 {conflict.get('run_id')}，"
                        "本次不并发改写同指标结论"
                    ),
                    "busy": True,
                    "busy_run_id": conflict.get("run_id"),
                    "run": self._run_detail(run),
                }

            run["executing"] = True
            run["execution_count"] = int(run.get("execution_count", 0)) + 1
            run["updated_at"] = _now()
            snapshot = self._ensure_building_snapshot(run)
            self._touch_building_snapshot(run, snapshot)
            processed: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            try:
                for member in self._members(run):
                    if member.get("status") == "complete":
                        skipped.append({
                            "task_code": member.get("task_code"),
                            "reason": "完整结论已落库，沿用本次 run 的结果",
                        })
                        continue
                    try:
                        outcome = self._execute_member(run, member, snapshot)
                        processed.append(outcome)
                    except Exception as exc:  # 成员失败不阻断其他成员
                        member["status"] = "failed"
                        member["last_error"] = str(exc)
                        member["finished_at"] = _now()
                        processed.append({
                            "task_code": member.get("task_code"),
                            "state": "failed",
                            "message": str(exc),
                        })
                    finally:
                        self._touch_building_snapshot(run, snapshot)
                all_complete = all(
                    member.get("status") == "complete" for member in self._members(run)
                )
                if all_complete and self._members(run):
                    self._commit_snapshot(run, snapshot)
                    run["status"] = "completed"
                else:
                    run["status"] = "partial"
                run["updated_at"] = _now()
            finally:
                run["executing"] = False
            detail = self._run_detail(run)
            detail["processed"] = processed
            detail["skipped"] = skipped
            detail["snapshot"] = self._snapshot_view(run)
            detail["message"] = (
                "影响范围内结论已完整落库，新快照已提交"
                if run.get("status") == "completed"
                else "仍有成员存在样本缺口或失败，旧快照继续保留，可再次触发从缺口续跑"
            )
            return detail

    def _find_run_by_code(self, run_id: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE_RUNS):
            if row.get("run_id") == run_id:
                return row
        return None

    def _execute_member(
        self,
        run: dict[str, Any],
        member: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        task = None
        for row in store.rows(MODULE_TASKS):
            if _task_code(row) == member.get("task_code"):
                task = row
                break
        if task is None:
            raise RuntimeError(f"验证任务 {member.get('task_code')} 不存在或已归档")

        member["status"] = "running"
        member["attempts"] = int(member.get("attempts", 0)) + 1
        member["last_error"] = None

        # 并发/重复执行时，以已经完整落库的同版本结论为准，不重复计算。
        existing = self._conclusion_for_revision(member.get("task_code"), run.get("metric_revision"))
        if existing is not None and existing.get("run_id") != run.get("run_id"):
            member["status"] = "complete"
            member["finished_at"] = _now()
            self._bind_member_samples_from_conclusion(member, existing)
            return {
                "task_code": member.get("task_code"),
                "state": "reused_conclusion",
                "metric_revision": existing.get("metric_revision"),
                "message": "同一指标版本已有完整结论，按落库结论复用",
            }

        if task.get("failure_mode") == "first_attempt" and member.get("attempts") == 1:
            raise RuntimeError("验证成员首次执行未拿到完整分区，已记录缺口，等待续跑")

        sample_results: dict[str, dict[str, Any]] = dict(member.get("samples") or {})
        for sample in task.get("samples") or []:
            sample_id = keys.normalize_text(sample.get("sample_id"))
            if not sample_id:
                continue
            previous = sample_results.get(sample_id)
            if previous and previous.get("state") in {"passed", "abnormal"}:
                continue  # 已到终态的样本不重算，只补缺口
            result = self._evaluate_sample(task, sample, member)
            sample_results[sample_id] = result
            member["samples"] = sample_results
            self._touch_building_snapshot(run, snapshot)

        member["status"] = "complete" if all(
            result.get("state") in {"passed", "abnormal"}
            for result in sample_results.values()
        ) and sample_results else "incomplete"
        member["finished_at"] = _now()
        conclusion = self._upsert_conclusion(run, task, member, sample_results)
        self._apply_task_state(task, conclusion, run)
        return {
            "task_code": member.get("task_code"),
            "state": member.get("status"),
            "conclusion_status": conclusion.get("status"),
            "verdict": conclusion.get("verdict"),
            "metric_revision": conclusion.get("metric_revision"),
            "samples": [
                {"sample_id": key, "state": value.get("state"), "verdict": value.get("verdict")}
                for key, value in sample_results.items()
            ],
        }

    def _evaluate_sample(
        self,
        task: dict[str, Any],
        sample: dict[str, Any],
        member: dict[str, Any],
    ) -> dict[str, Any]:
        sample_id = keys.normalize_text(sample.get("sample_id"))
        backfill = (member.get("backfills") or {}).get(sample_id)
        observed = sample.get("observed")
        if backfill is not None:
            observed = backfill.get("observed")
        if observed is None:
            return {
                "sample_id": sample_id,
                "label": sample.get("label"),
                "kind": sample.get("kind"),
                "state": GAP_SAMPLE_STATE,
                "verdict": "待补数",
                "reason": sample.get("gap_reason") or "样本观测值缺失，保留缺口等待补数",
                "expected": sample.get("expected"),
                "observed": None,
            }
        expected = float(sample.get("expected") or 0)
        observed_value = float(observed)
        tolerance = float(task.get("tolerance") or task.get("差异阈值") or 0.01)
        if expected == 0:
            relative_diff = 0.0 if observed_value == 0 else float("inf")
        else:
            relative_diff = abs(observed_value - expected) / abs(expected)
        abnormal = relative_diff > tolerance
        return {
            "sample_id": sample_id,
            "label": sample.get("label"),
            "kind": sample.get("kind"),
            "state": "abnormal" if abnormal else "passed",
            "verdict": "异常" if abnormal else "通过",
            "reason": sample.get("note") or (
                f"相对差异 {relative_diff:.4%} 超过阈值 {tolerance:.2%}"
                if abnormal else f"相对差异 {relative_diff:.4%} 在阈值 {tolerance:.2%} 内"
            ),
            "expected": expected,
            "observed": observed_value,
            "relative_diff": None if relative_diff == float("inf") else round(relative_diff, 6),
            "tolerance": tolerance,
        }

    def _conclusion_for_revision(self, task_code: str, revision: Any) -> dict[str, Any] | None:
        rows = [
            row for row in store.rows(MODULE_CONCLUSIONS)
            if row.get("task_code") == task_code
            and row.get("metric_revision") == revision
            and row.get("status") in COMPLETE_CONCLUSION_STATES
        ]
        return max(rows, key=lambda row: int(row.get("id", 0))) if rows else None

    def _bind_member_samples_from_conclusion(
        self,
        member: dict[str, Any],
        conclusion: dict[str, Any],
    ) -> None:
        member["samples"] = {
            item.get("sample_id"): item for item in conclusion.get("sample_results") or []
            if item.get("sample_id")
        }

    def _upsert_conclusion(
        self,
        run: dict[str, Any],
        task: dict[str, Any],
        member: dict[str, Any],
        sample_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        results = list(sample_results.values())
        has_gap = any(item.get("state") == GAP_SAMPLE_STATE for item in results)
        has_abnormal = any(item.get("state") == "abnormal" for item in results)
        if has_gap:
            status, verdict, task_state = "pending", "待补数", "验证中"
        elif has_abnormal:
            status, verdict, task_state = "abnormal", "异常", "异常"
        else:
            status, verdict, task_state = "complete", "通过", "已验证"

        existing = next(
            (
                row for row in store.rows(MODULE_CONCLUSIONS)
                if row.get("run_id") == run.get("run_id")
                and row.get("task_code") == _task_code(task)
            ),
            None,
        )
        revision = run.get("metric_revision")
        values = {
            "metric_id": run.get("metric_id"),
            "metric_revision": revision,
            "impact_key": run.get("impact_key"),
            "grain_hash": run.get("grain_hash"),
            "baseline_uri": run.get("baseline_uri"),
            "baseline_digest": run.get("baseline_digest"),
            "compare_axis": run.get("compare_axis"),
            "tolerance": keys.normalize_tolerance(run.get("tolerance")),
            "status": status,
            "verdict": verdict,
            "task_state": task_state,
            "sample_results": results,
            "has_abnormal_sample": has_abnormal,
            "has_gap": has_gap,
            "updated_at": _now(),
            "completed_at": _now() if status in COMPLETE_CONCLUSION_STATES else None,
        }
        if existing:
            existing.update(values)
            conclusion = existing
        else:
            conclusion = {
                "id": _next_id(store.rows(MODULE_CONCLUSIONS)),
                "task_code": _task_code(task),
                "task_name": task.get("验证数据集"),
                "run_id": run.get("run_id"),
                "created_at": _now(),
                "version_seq": 1,
            }
            conclusion.update(values)
            store.rows(MODULE_CONCLUSIONS).append(conclusion)
        member["conclusion_status"] = status
        member["verdict"] = verdict
        return conclusion

    def _apply_task_state(
        self,
        task: dict[str, Any],
        conclusion: dict[str, Any],
        run: dict[str, Any],
    ) -> None:
        """只有完整结论才能把任务标成已验证；待补数/失败一律保持未验证。

        任务契约字段只在它确实跟随该指标口径时才推进：
        - 任务的 grain_hash 出现在该指标任一版本里 → 视为跟随指标粒度，推进到当前版本；
        - 任务自带独立基准/粒度（如退款率按 user_id）→ 保持原绑定不动。
        """
        current_revision = self._revision(run.get("metric_id"), run.get("metric_revision")) or {}
        known_grains = {
            keys.normalize_text(row.get("grain_hash")) for row in self._revisions(run.get("metric_id"))
        }
        if keys.normalize_text(task.get("grain_hash")) in known_grains:
            task["grain_hash"] = keys.normalize_text(current_revision.get("grain_hash")) or task.get("grain_hash")
            task["aggregation_grain"] = (
                keys.normalize_grain(current_revision.get("aggregation_grain")) or task.get("aggregation_grain")
            )
        task["metric_revision"] = conclusion.get("metric_revision")
        task["最近验证结果"] = conclusion.get("verdict")
        if conclusion.get("status") == "complete":
            task["status"] = "已验证"
            task["pending"] = False
            task["abnormal"] = False
        elif conclusion.get("status") == "abnormal":
            task["status"] = "异常"
            task["pending"] = False
            task["abnormal"] = True
        else:
            task["status"] = "验证中"
            task["pending"] = True
            task["abnormal"] = False

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def _ensure_building_snapshot(self, run: dict[str, Any]) -> dict[str, Any]:
        for row in store.rows(MODULE_SNAPSHOTS):
            if row.get("run_id") == run.get("run_id") and row.get("state") == "building":
                return row
        snapshot = {
            "id": _next_id(store.rows(MODULE_SNAPSHOTS)),
            "snapshot_id": f"SNAP-{run.get('run_id')}",
            "run_id": run.get("run_id"),
            "metric_id": run.get("metric_id"),
            "metric_revision": run.get("metric_revision"),
            "state": "building",
            "created_at": _now(),
            "completed_at": None,
            "items": [],
        }
        store.rows(MODULE_SNAPSHOTS).append(snapshot)
        return snapshot

    def _touch_building_snapshot(self, run: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """building 快照只追加本次 run 的成员进度，旧 active 保持不变。"""
        by_code = {
            row.get("task_code"): row
            for row in store.rows(MODULE_CONCLUSIONS)
            if row.get("run_id") == run.get("run_id")
        }
        items: list[dict[str, Any]] = []
        for member in self._members(run):
            conclusion = by_code.get(member.get("task_code")) or {}
            items.append({
                "task_code": member.get("task_code"),
                "status": conclusion.get("status") or member.get("status"),
                "verdict": conclusion.get("verdict") or "待验证",
                "metric_revision": conclusion.get("metric_revision") or run.get("metric_revision"),
                "samples": self._member_samples(member),
            })
        snapshot["items"] = items
        snapshot["updated_at"] = _now()

    def _commit_snapshot(self, run: dict[str, Any], snapshot: dict[str, Any]) -> None:
        """提交新快照；旧 active 只转 superseded，不删除。"""
        for row in store.rows(MODULE_SNAPSHOTS):
            if (
                row.get("metric_id") == run.get("metric_id")
                and row.get("state") == "active"
                and row is not snapshot
            ):
                row["state"] = "superseded"
                row["superseded_at"] = _now()
        items: list[dict[str, Any]] = []
        for member in self._members(run):
            conclusion = next(
                (
                    row for row in store.rows(MODULE_CONCLUSIONS)
                    if row.get("run_id") == run.get("run_id")
                    and row.get("task_code") == member.get("task_code")
                ),
                None,
            )
            reused = False
            if conclusion is None:
                # 该成员按“其他 run 已完整落库的同版本结论”复用，快照里仍要给出权威结论。
                conclusion = self._conclusion_for_revision(
                    member.get("task_code"), run.get("metric_revision")
                )
                reused = conclusion is not None
            items.append({
                "task_code": member.get("task_code"),
                "status": (conclusion or {}).get("status"),
                "verdict": (conclusion or {}).get("verdict"),
                "metric_revision": (conclusion or {}).get("metric_revision"),
                "sample_results": (conclusion or {}).get("sample_results"),
                "reused_conclusion": reused,
            })
        snapshot["items"] = items
        snapshot["state"] = "active"
        snapshot["completed_at"] = _now()
        snapshot["updated_at"] = _now()

    def _snapshot_view(self, run: dict[str, Any]) -> dict[str, Any]:
        run_id = run.get("run_id")
        metric_id = run.get("metric_id")
        rows = [row for row in store.rows(MODULE_SNAPSHOTS) if row.get("run_id") == run_id]
        active_for_metric = [
            row for row in store.rows(MODULE_SNAPSHOTS)
            if row.get("state") == "active" and row.get("metric_id") == metric_id
        ]
        building = next((row for row in rows if row.get("state") == "building"), None)
        superseded = [
            row for row in store.rows(MODULE_SNAPSHOTS)
            if row.get("metric_id") == metric_id and row.get("state") == "superseded"
        ]
        return {
            "building": copy.deepcopy(building) if building else None,
            "run_active": copy.deepcopy(next((row for row in rows if row.get("state") == "active"), None)),
            # building 期间这里仍是旧 active，直到新结论完整落库才切换。
            "authoritative_active": copy.deepcopy(active_for_metric[0]) if active_for_metric else None,
            "superseded": copy.deepcopy(superseded),
            "history": copy.deepcopy(rows),
        }

    def backfill(
        self,
        run_id: str,
        *,
        task_code: str,
        sample_id: str,
        observed: Any,
        expected: Any = None,
    ) -> dict[str, Any]:
        """补一个样本缺口；下一次 execute 只从缺口继续。"""
        with _LOCK:
            run = self._find_run_by_code(run_id)
            if run is None:
                return {"ok": False, "message": f"impact_run {run_id} 不存在"}
            task_code = keys.normalize_text(task_code)
            sample_id = keys.normalize_text(sample_id)
            member = next(
                (
                    row for row in self._members(run)
                    if row.get("task_code") == task_code
                ),
                None,
            )
            if member is None:
                return {"ok": False, "message": f"任务 {task_code} 不在该 impact_run 的 affected 集合里"}
            task = next((row for row in store.rows(MODULE_TASKS) if _task_code(row) == task_code), None)
            sample = next(
                (item for item in (task or {}).get("samples") or []
                 if keys.normalize_text(item.get("sample_id")) == sample_id),
                None,
            )
            if sample is None:
                return {"ok": False, "message": f"样本 {sample_id} 不属于验证任务 {task_code}"}
            if observed is None:
                return {"ok": False, "message": "补数需要提供 observed 观测值"}
            member.setdefault("backfills", {})[sample_id] = {
                "observed": observed,
                "expected": expected if expected is not None else sample.get("expected"),
                "filled_at": _now(),
            }
            if member.get("status") != "complete":
                member["status"] = "pending"
            run["updated_at"] = _now()
            return {
                "ok": True,
                "message": f"已登记 {task_code}/{sample_id} 补数，下一次执行从缺口续跑",
                "run_id": run_id,
                "task_code": task_code,
                "sample_id": sample_id,
                "observed": observed,
            }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_runs(self, metric_id: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(MODULE_RUNS)
        if metric_id:
            rows = [row for row in rows if row.get("metric_id") == metric_id]
        rows = sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)
        return [self._run_detail(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self._find_run_by_code(run_id)
        return self._run_detail(run) if run else None

    def snapshots(self, run_id: str) -> dict[str, Any] | None:
        run = self._find_run_by_code(run_id)
        if run is None:
            return None
        view = self._snapshot_view(run)
        view["metric_id"] = run.get("metric_id")
        view["metric_revision"] = run.get("metric_revision")
        return view

    def trace(self, metric_id: str | None = None) -> dict[str, Any]:
        """验收用调用链视图：版本 → run → 成员 → 结论 → 快照。"""
        revisions = self._revisions(metric_id) if metric_id else store.rows(MODULE_REVISIONS)
        runs = self.list_runs(metric_id)
        return {
            "revisions": [self._revision_public(row) for row in revisions],
            "runs": runs,
            "conclusions": [
                row for row in store.rows(MODULE_CONCLUSIONS)
                if not metric_id or row.get("metric_id") == metric_id
            ],
            "snapshots": [
                row for row in store.rows(MODULE_SNAPSHOTS)
                if not metric_id or row.get("metric_id") == metric_id
            ],
        }


impact_service = ImpactService()
