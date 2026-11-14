"""口径影响分析接口：版本发布、影响集合、执行、补数与快照查询。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload
from app.services.impact import impact_service

router = APIRouter(prefix="/api/impact", tags=["口径影响分析"])


@router.get("/revisions/{metric_id}")
def list_revisions(metric_id: str) -> dict[str, Any]:
    """列出一个指标的全部已发布版本及其影响键字段。"""
    revisions = impact_service.list_revisions(metric_id)
    if not revisions:
        raise HTTPException(status_code=404, detail=f"指标 {metric_id} 还没有已发布版本")
    return {"metric_id": metric_id, "items": revisions, "total": len(revisions)}


@router.post("/publish")
def publish_revision(payload: EntryPayload) -> ActionResult:
    """登记并发布新版本；返回本次影响分析结果。

    同一个 metric_id + revision 重复提交时不会重复生成 impact_run。
    """
    values = payload.values
    entry_id = values.get("entry_id")
    if entry_id is None:
        return ActionResult(ok=False, message="缺少必填字段：entry_id")
    revision, message, analysis = impact_service.publish_revision(int(entry_id), values)
    if revision is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=revision, impact=analysis)


@router.post("/analyze")
def analyze(payload: EntryPayload) -> ActionResult:
    """对已发布版本做影响分析；同一 impact_run 只登记一次。"""
    metric_id = str(payload.values.get("metric_id") or "").strip()
    if not metric_id:
        return ActionResult(ok=False, message="缺少必填字段：metric_id")
    revision = str(payload.values.get("metric_revision") or "").strip() or None
    result = impact_service.analyze(metric_id, revision)
    return ActionResult(
        ok=bool(result.get("ok")),
        message=str(result.get("message") or ""),
        entry=result if result.get("ok") else None,
        impact=result,
    )


@router.get("/runs")
def list_runs(metric_id: str | None = Query(default=None, description="按指标编号过滤")) -> dict[str, Any]:
    items = impact_service.list_runs(metric_id)
    return {"items": items, "total": len(items)}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = impact_service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"impact_run {run_id} 不存在")
    return run


@router.post("/runs/{run_id}/execute")
def execute_run(run_id: str) -> ActionResult:
    """执行受影响任务；重复触发只处理未完成成员，结论全部完整才提交新快照。"""
    result = impact_service.execute(run_id)
    if not result.get("ok") and result.get("message") and "不存在" in str(result.get("message")):
        raise HTTPException(status_code=404, detail=str(result.get("message")))
    return ActionResult(
        ok=bool(result.get("ok")),
        message=str(result.get("message") or ""),
        entry=result.get("run") if isinstance(result.get("run"), dict) else result,
        impact=result,
    )


@router.post("/runs/{run_id}/backfill")
def backfill_sample(run_id: str, payload: EntryPayload) -> ActionResult:
    """给某个 affected 成员补一个样本缺口，随后 execute 只从缺口续跑。"""
    values = payload.values
    task_code = str(values.get("task_code") or "").strip()
    sample_id = str(values.get("sample_id") or "").strip()
    if not task_code or not sample_id:
        return ActionResult(ok=False, message="缺少必填字段：task_code、sample_id")
    result = impact_service.backfill(
        run_id,
        task_code=task_code,
        sample_id=sample_id,
        observed=values.get("observed"),
        expected=values.get("expected"),
    )
    return ActionResult(ok=bool(result.get("ok")), message=str(result.get("message") or ""), impact=result)


@router.get("/runs/{run_id}/snapshots")
def run_snapshots(run_id: str) -> dict[str, Any]:
    """查看 building / active / superseded 快照，确认旧结果未被提前清空。"""
    snapshots = impact_service.snapshots(run_id)
    if snapshots is None:
        raise HTTPException(status_code=404, detail=f"impact_run {run_id} 不存在")
    return snapshots


@router.get("/trace")
def trace(metric_id: str | None = Query(default=None, description="按指标编号过滤")) -> dict[str, Any]:
    """验收用：版本 → impact_run → 成员/结论 → 快照的完整链路。"""
    return impact_service.trace(metric_id)
