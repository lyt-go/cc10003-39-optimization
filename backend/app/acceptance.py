"""口径换版到验证执行的验收脚本。

不依赖 pytest/httpx：直接调用服务层与 ASGI 路由函数，覆盖
affected / reused / pending / abnormal 四个集合、快照边界与去重语义。

运行：``cd backend && .venv/bin/python -m app.acceptance``
"""
from __future__ import annotations

import sys
from typing import Any

from app.services.impact import impact_service
from app.store import store

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    CHECKS.append((name, passed, detail))
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  | {detail}" if detail else ""))


def codes(rows: list[dict[str, Any]]) -> list[str]:
    return [row["task_code"] for row in rows]


def run() -> int:
    # 1. 影响分析：r2 -> r3 只改聚合粒度。
    analysis = impact_service.analyze("METR-ORDER_COUNT", "r3")
    check("影响分析生成 impact_run", bool(analysis.get("run_id")), analysis.get("run_id", ""))
    check(
        "影响键四字段齐全",
        set(analysis["impact_key_fields"]) == {"metric_id", "grain_hash", "baseline_uri", "tolerance"},
        str(analysis["impact_key_fields"]),
    )
    check("变化字段为 aggregation_grain", analysis["changed_fields"] == ["aggregation_grain"],
          str(analysis["changed_fields"]))
    check("affected 仅 DATA-ORDER_COUNT", codes(analysis["sets"]["affected"]) == ["DATA-ORDER_COUNT"],
          str(codes(analysis["sets"]["affected"])))
    check("reused 为 DATA-REFUND_RATE", codes(analysis["sets"]["reused"]) == ["DATA-REFUND_RATE"],
          str(codes(analysis["sets"]["reused"])))
    check("reused 沿用 r2 结论", analysis["sets"]["reused"][0]["metric_revision"] == "r2")
    check("affected 初始进入 pending", "DATA-ORDER_COUNT" in codes(analysis["sets"]["pending"]))
    check("未命中任务不生成新记录",
          all(row["run_id"] != analysis["run_id"] for row in store.rows("metric_conclusion")),
          "r3 尚无结论记录")

    # 2. 重复触发同一 impact_run：只登记一次。
    again = impact_service.analyze("METR-ORDER_COUNT", "r3")
    check("重复 analyze 去重", again.get("deduplicated") is True)
    check("impact_run 只有一条", len(impact_service.list_runs("METR-ORDER_COUNT")) == 1)

    run_id = analysis["run_id"]

    # 3. 首次执行失败：任务不能被提前标成已验证；旧 active 快照保留。
    first = impact_service.execute(run_id)
    check("首次执行 partial", first["status"] == "partial", first["message"])
    task_one = next(row for row in store.rows("dataval") if row["任务编号"] == "DATA-ORDER_COUNT")
    check("失败成员未提前标已验证", task_one["status"] != "已验证", task_one["status"])
    check("旧 active 快照仍为 r2",
          first["snapshot"]["authoritative_active"]["metric_revision"] == "r2")
    check("building 快照已追加", first["snapshot"]["building"]["state"] == "building")

    # 4. 手动标记已验证应被拒绝（同版本仍有缺口）。
    from app.services.dataval import DatavalService

    _, message = DatavalService().run_action(1, "标记已验证")
    check("缺口未补齐拒绝标记已验证", "不能提前标成已验证" in message, message)

    # 5. 续跑：02-29 通过、跨月首日异常、空地区缺口。
    second = impact_service.execute(run_id)
    sample_states = {
        item["sample_id"]: item["state"]
        for item in second["sets"]["abnormal"][0]["sample_results"]
    }
    check("02-29 闰日判为通过", sample_states.get("SAMPLE-LEAP-0229") == "passed")
    check("跨月首日判为异常", sample_states.get("SAMPLE-MONTH-FIRST") == "abnormal")
    check("空地区保留缺口", sample_states.get("SAMPLE-EMPTY-REGION") == "gap")
    check("异常样本进入 abnormal 集合", "DATA-ORDER_COUNT" in codes(second["sets"]["abnormal"]))
    check("仍有缺口时保持 pending", "DATA-ORDER_COUNT" in codes(second["sets"]["pending"]))
    check("仍有缺口时不提交快照",
          second["snapshot"]["authoritative_active"]["metric_revision"] == "r2")
    task_one = next(row for row in store.rows("dataval") if row["任务编号"] == "DATA-ORDER_COUNT")
    check("缺口期间任务状态为验证中", task_one["status"] == "验证中", task_one["status"])

    # 6. 补数后只从缺口续跑，并提交 building 快照。
    backfill = impact_service.backfill(
        run_id,
        task_code="DATA-ORDER_COUNT",
        sample_id="SAMPLE-EMPTY-REGION",
        observed=300.5,
    )
    check("登记补数成功", backfill.get("ok") is True, backfill.get("message", ""))
    final = impact_service.execute(run_id)
    check("全部结论完整后 run completed", final["status"] == "completed", final["message"])
    check("只处理缺口成员", all(
        item["task_code"] == "DATA-ORDER_COUNT" for item in final.get("processed", [])
    ))
    check("新快照切换为 active r3",
          final["snapshot"]["authoritative_active"]["metric_revision"] == "r3")
    check("旧快照保留为 superseded", any(
        item["snapshot_id"] == "SNAP-METR-ORDER_COUNT-r2" for item in final["snapshot"]["superseded"]
    ))
    check("最终重建了 pending 集合", codes(final["sets"]["pending"]) == [])
    check("每条结论绑定 metric_revision", all(
        row.get("metric_revision") for row in store.rows("metric_conclusion")
    ))
    check("reused 任务未追加新记录", all(
        row["task_code"] != "DATA-REFUND_RATE" or row["metric_revision"] == "r2"
        for row in store.rows("metric_conclusion")
    ))
    check("reused 结论记录仍只有一条", sum(
        1 for row in store.rows("metric_conclusion") if row["task_code"] == "DATA-REFUND_RATE"
    ) == 1)

    # 7. 已完成 run 重复触发不重复处理。
    repeat = impact_service.execute(run_id)
    check("completed run 重复执行幂等", repeat.get("deduplicated") is True)
    check("重复执行不增加执行次数",
          repeat["run"]["execution_count"] == final["execution_count"])

    # 8. 只改 tolerance：影响键变化，影响分析重新登记。
    revision_row, _, new_analysis = impact_service.publish_revision(
        4, {"metric_revision": "r4", "tolerance": 0.02}
    )
    check("登记新版本成功", revision_row is not None, str(revision_row and revision_row["metric_revision"]))
    check("tolerance 变化被识别", "tolerance" in (new_analysis or {}).get("changed_fields", []))
    check("tolerance 变化产生新 impact_run", new_analysis and new_analysis["run_id"] != run_id)

    failed = [name for name, passed, _ in CHECKS if not passed]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
    if failed:
        print("失败项：" + "、".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
