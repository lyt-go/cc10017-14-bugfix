"""Iceberg管理业务规则：状态流转、字段校验与筛选口径都收在这里。

增强：Iceberg 管理跨模块接收湖上分区生成的异常分区修复清单，把支付事件归属结果
写进分区元数据，并保留湖上分区的来源版本。
"""
from __future__ import annotations

from typing import Any

from app.services.partition_repair import PAY_TABLE, repair_service
from app.store import store

MODULE = "iceberg_table"
LIST_FIELDS = ["表编号", "表名称", "存储格式", "分区策略", "排序字段", "快照过期", "孤立文件清理", "元数据版本"]
REQUIRED_FIELDS = ["表编号", "表名称", "存储格式"]
STATUS_ORDER = ["草稿", "待校验", "已就绪", "已下线"]
ACTION_RULES = {"提交校验": "待校验", "确认就绪": "已就绪", "标记下线": "已下线"}
NEGATIVE_ACTIONS = []


class IcebergTableService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("表编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"表元数据 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于Iceberg管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"表元数据已{action}"

    def payment_partitions(self, table_name: str = PAY_TABLE) -> dict[str, Any]:
        """查看支付表分区归属：读取湖上分区生成的异常分区修复清单，按事件去重。"""
        return repair_service.list_iceberg_partitions(table_name=table_name)

    def apply_repair_list(self, table_name: str = PAY_TABLE) -> tuple[dict[str, Any] | None, str]:
        """接收异常分区修复清单并更新支付表分区元数据。

        已归属的记录按清单口径重放同步，待修复的记录不落元数据，保证元数据里
        同一支付事件只有一个可查询分区。
        """
        iceberg = next(
            (row for row in store.rows(MODULE) if row.get("表名称") == table_name), None)
        if iceberg is None:
            return None, f"表元数据 {table_name} 未登记"
        repairs, _total = repair_service.list_repairs(table_name=table_name, size=100000)
        applied = 0
        for repair in repairs:
            if repair.get("状态") != "已归属":
                continue
            repair_service.sync_iceberg_metadata(repair)
            applied += 1
        detail = repair_service.list_iceberg_partitions(table_name=table_name)
        return {
            "table_name": table_name,
            "applied_count": applied,
            "pending_repairs": len(repairs) - applied,
            "partition_source_version": detail.get("partition_source_version"),
            "partitions": detail.get("partitions"),
        }, f"异常分区修复清单已同步，支付表 {table_name} 分区元数据更新 {applied} 条"
