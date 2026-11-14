"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS
from app.seed_impact import DATAVAL_IMPACT_ROWS, IMPACT_SEED_ROWS


def _merge_seed() -> dict[str, list[dict[str, Any]]]:
    tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    # 口径换版链路需要验证任务携带指标绑定与样本；只覆盖 dataval 示例，其他模块原样保留。
    tables["dataval"] = [dict(row) for row in DATAVAL_IMPACT_ROWS]
    tables["metric"].append({
        "id": 4,
        "status": "已发布",
        "pending": False,
        "abnormal": False,
        "指标编号": "METR-ORDER_COUNT",
        "指标名称": "订单量",
        "计算公式": "sum(paid_order_amount) / count(distinct order_id)",
        "统计粒度": "order_date,region,channel",
        "时间窗口": "每日",
        "筛选条件": "paid_status = paid",
        "精度位数": "2",
        "口径版本": "r3",
    })
    for name, rows in IMPACT_SEED_ROWS.items():
        tables.setdefault(name, [])
        tables[name].extend(dict(row) for row in rows)
    return tables


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = _merge_seed()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
