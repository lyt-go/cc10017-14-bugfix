"""支付事件分区写入与异常分区修复清单。

业务背景：
- 支付事件按业务日期（`业务日期`）落入湖表日分区；缺少业务日期时，旧实现用当天日期
  兜底，结果事件混入“今天”的正常分区，生命周期归档与按日查询都会读到错误数据。
- 修复过程读取支付表的分区定义（`lake_partition` 中目标表为 payments 的那条），
  把缺业务日期的支付事件登记进“异常分区修复清单”，清单保留原始时间与目标分区。
- 清单状态是单向的：待修复 → 已归属。归属一旦落库，同一个事件只会有一个可查询分区，
  重复检查只会命中已有归属，不会让事件在多个分区残留。

归属判定（同一事件缺业务日期、同时又存在原始接入时间时）：
- 配置了“按接入时间归属”时，目标分区取接入时间所在分区；
- 配置了“按空值隔离”时，事件落到隔离分区，不与正常日分区混在一起。
两种口径留下的目标分区不同，但事件最终只会归属一个可查询分区。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.store import store

EVENT_MODULE = "pay_event"
REPAIR_MODULE = "abnormal_partition_repair"
PAY_TABLE = "payments"

# 分区归属口径：按接入时间归属 / 按空值隔离
ROUTING_BY_INGEST = "按接入时间归属"
ROUTING_BY_ISOLATION = "按空值隔离"
ROUTING_RULES = (ROUTING_BY_INGEST, ROUTING_BY_ISOLATION)
DEFAULT_ROUTING = ROUTING_BY_INGEST

ISOLATION_PARTITION = "payments/__missing_business_date__"

REPAIR_PENDING = "待修复"
REPAIR_ASSIGNED = "已归属"
REPAIR_STATUS_ORDER = [REPAIR_PENDING, REPAIR_ASSIGNED]

# 清单字段口径：原始时间与目标分区必须成对保留，供页面按列渲染。
REPAIR_FIELDS = [
    "清单编号", "支付事件编号", "目标表名", "原始时间", "目标分区",
    "归属口径", "来源版本", "状态",
]
REPAIR_EDITABLE_STATUS = REPAIR_PENDING

# 修改权限按数据归属控制：清单的归属团队与分区定义保持一致。
REPAIR_OWNER_TEAM = "数据平台组"


def _parse_datetime(value: Any) -> datetime | None:
    """解析 ISO 时间字符串；解析不了就返回 None，让调用方走缺失分支。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("-", "—"):
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class AbnormalPartitionRepairService:
    """异常分区修复清单：登记、断点恢复、归属推进与 Iceberg 元数据同步。"""

    # ───────────────────────────────
    # 分区定义 / 归属口径
    # ───────────────────────────────

    def partition_definition(self, table_name: str = PAY_TABLE) -> dict[str, Any] | None:
        """读取支付表的分区定义；修复清单的字段口径与来源版本都来自这里。"""
        for row in store.rows("lake_partition"):
            if row.get("目标表名") == table_name:
                return row
        return None

    def routing_rule(self, table_name: str = PAY_TABLE) -> str:
        """读取支付表当前的分区归属口径，未配置时按接入时间归属。"""
        definition = self.partition_definition(table_name)
        rule = (definition or {}).get("缺失业务日期归属") or DEFAULT_ROUTING
        return rule if rule in ROUTING_RULES else DEFAULT_ROUTING

    def set_routing_rule(self, rule: str, *, operator_team: str | None = None,
                         table_name: str = PAY_TABLE) -> tuple[dict[str, Any] | None, str]:
        """调整归属口径；只影响之后新登记的异常分区，已归属的事件不重算。"""
        if rule not in ROUTING_RULES:
            return None, f"归属口径「{rule}」不在可选范围：{'、'.join(ROUTING_RULES)}"
        definition = self.partition_definition(table_name)
        if definition is None:
            return None, f"支付表 {table_name} 未配置分区定义"
        blocked = self._check_owner(definition, operator_team)
        if blocked:
            return None, blocked
        definition["缺失业务日期归属"] = rule
        return definition, f"支付表缺失业务日期归属已调整为「{rule}」"

    def register_event(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """登记一条支付事件：有业务日期落正常日分区，缺业务日期进入修复清单。"""
        event_code = str(values.get("支付事件编号") or "").strip()
        if not event_code:
            return None, "缺少必填字段：支付事件编号"
        rows = store.rows(EVENT_MODULE)
        if any(row.get("支付事件编号") == event_code for row in rows):
            return None, f"支付事件 {event_code} 已登记，同一事件只允许一个归属"
        business_date = self._normalise_business_date(values.get("业务日期"))
        ingested_at = str(values.get("接入时间") or "").strip() or datetime.now().isoformat(timespec="seconds")
        entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "支付事件编号": event_code,
            "业务日期": business_date,
            "接入时间": ingested_at,
            "目标表名": str(values.get("目标表名") or PAY_TABLE),
            "金额": values.get("金额"),
        }
        if business_date:
            # 有业务日期：直接落到业务日期所在分区，不进入修复清单。
            entry["目标分区"] = self._business_partition(entry["目标表名"], business_date)
            entry["状态"] = REPAIR_ASSIGNED
            rows.append(entry)
            return entry, f"支付事件已落入业务日期分区 {entry['目标分区']}"

        # 缺业务日期：旧实现此时会用当天日期补分区，这里不再兜底到当天，
        # 而是登记修复清单，按归属口径确定目标分区。
        entry["状态"] = REPAIR_PENDING
        entry["目标分区"] = None
        rows.append(entry)
        repair, message = self.ensure_repair_entry(entry)
        if repair is not None:
            entry["目标分区"] = repair.get("目标分区")
            entry["状态"] = repair.get("状态")
        return entry, message

    # ───────────────────────────────
    # 修复清单
    # ───────────────────────────────

    def ensure_repair_entry(self, event: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """为缺业务日期的支付事件登记（或复用）修复清单行。

        幂等：同一支付事件只保留一条清单；已归属的行直接返回，不再计算新的目标分区。
        """
        event_code = event.get("支付事件编号")
        rows = store.rows(REPAIR_MODULE)
        existing = next((row for row in rows if row.get("支付事件编号") == event_code), None)
        if existing is not None:
            if existing.get("状态") == REPAIR_ASSIGNED:
                return existing, f"支付事件 {event_code} 已归属分区 {existing.get('目标分区')}，不会重复落库"
            return existing, f"支付事件 {event_code} 已在修复清单中等待归属"

        definition = self.partition_definition(event.get("目标表名") or PAY_TABLE)
        rule = self.routing_rule(event.get("目标表名") or PAY_TABLE)
        target_partition = self._resolve_target_partition(event, rule)
        repair = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "清单编号": f"REPR-{(max((int(row.get('id', 0)) for row in rows), default=0) + 1):04d}",
            "支付事件编号": event_code,
            "目标表名": event.get("目标表名") or PAY_TABLE,
            "原始时间": event.get("接入时间"),
            "目标分区": target_partition,
            "归属口径": rule,
            "来源版本": (definition or {}).get("来源版本"),
            "归属团队": (definition or {}).get("归属团队") or REPAIR_OWNER_TEAM,
            "状态": REPAIR_PENDING,
            "pending": True,
            "abnormal": True,
            "已同步Iceberg": False,
        }
        rows.append(repair)
        return repair, f"支付事件 {event_code} 缺少业务日期，已登记异常分区修复清单"

    def list_repairs(
        self,
        *,
        table_name: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """按目标表、状态与清单编号/事件编号检索修复清单。

        同一支付事件只展示一条清单行，避免历史重复登记导致错位或重复显示。
        """
        deduped: dict[str, dict[str, Any]] = {}
        for row in store.rows(REPAIR_MODULE):
            deduped.setdefault(str(row.get("支付事件编号")), row)
        rows = list(deduped.values())
        if table_name:
            rows = [row for row in rows if row.get("目标表名") == table_name]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        if keyword:
            needle = keyword.casefold()
            rows = [
                row for row in rows
                if needle in str(row.get("清单编号", "")).casefold()
                or needle in str(row.get("支付事件编号", "")).casefold()
            ]
        rows.sort(key=lambda row: int(row.get("id", 0)))
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_repair(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(REPAIR_MODULE, entry_id)

    def assign_partition(
        self, entry_id: int, *, operator_team: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """把一条待修复记录推进到已归属，并同步 Iceberg 分区元数据。

        单向落库：已归属的记录不允许回退或再次改写，重复归属直接返回已有结果。
        目标分区只认清单登记时保留的那一个，不接受调用方临时改写。
        """
        entry = store.find(REPAIR_MODULE, entry_id)
        if entry is None:
            return None, f"异常分区修复清单 {entry_id} 不存在"
        blocked = self._check_owner(entry, operator_team)
        if blocked:
            return None, blocked
        if entry.get("状态") == REPAIR_ASSIGNED:
            return entry, f"清单 {entry.get('清单编号')} 已归属分区 {entry.get('目标分区')}，不能重复改写"

        event = self._find_event(entry.get("支付事件编号"))
        table_name = str(entry.get("目标表名") or PAY_TABLE)
        recorded = entry.get("目标分区")
        # 与“当前”归属口径比对：口径在登记后被调整时，两种做法会算出不同的目标分区。
        current_rule = self.routing_rule(table_name)
        resolved_by_rule = self._resolve_target_partition(event or entry, current_rule)
        # 清单保留登记时的目标分区作为唯一归属，冲突只做记录，不让同一事件落到第二个分区。
        resolved = recorded or resolved_by_rule
        if resolved_by_rule and resolved and resolved_by_rule != resolved:
            entry["冲突判定"] = (
                f"归属口径现值为「{current_rule}」应落 {resolved_by_rule}，保留登记时的 {resolved}")
        if not resolved:
            return None, "缺少可归属的目标分区"

        # 同一支付事件若已在其他分区完成归属，禁止再落第二个分区。
        conflict = next(
            (row for row in store.rows(REPAIR_MODULE)
             if row.get("支付事件编号") == entry.get("支付事件编号")
             and row.get("id") != entry.get("id")
             and row.get("状态") == REPAIR_ASSIGNED),
            None,
        )
        if conflict is not None:
            return None, (
                f"支付事件 {entry.get('支付事件编号')} 已归属分区 {conflict.get('目标分区')}，"
                "同一事件只能归属一个可查询分区"
            )

        entry["目标分区"] = resolved
        entry["状态"] = REPAIR_ASSIGNED
        entry["pending"] = False
        entry["abnormal"] = False
        entry["归属时间"] = datetime.now().isoformat(timespec="seconds")

        if event is not None:
            event["状态"] = REPAIR_ASSIGNED
            event["目标分区"] = resolved

        iceberg_message = self.sync_iceberg_metadata(entry)
        return entry, f"清单 {entry.get('清单编号')} 已归属分区 {resolved}；{iceberg_message}"

    def recover_from_breakpoint(self, *, table_name: str = PAY_TABLE) -> dict[str, Any]:
        """写入中断后的断点恢复。

        只补登记尚未进入清单的缺业务日期事件，已经在清单里的行（无论待修复还是已归属）
        原样保留，避免反复改写让同一事件在多个分区残留。
        """
        repaired_events = {row.get("支付事件编号") for row in store.rows(REPAIR_MODULE)}
        recovered: list[dict[str, Any]] = []
        skipped = 0
        for event in store.rows(EVENT_MODULE):
            if (event.get("目标表名") or PAY_TABLE) != table_name:
                continue
            if event.get("业务日期"):
                continue
            if event.get("支付事件编号") in repaired_events:
                skipped += 1
                continue
            repair, _ = self.ensure_repair_entry(event)
            if repair is not None:
                recovered.append(repair)
                repaired_events.add(event.get("支付事件编号"))
        pending = sum(
            1 for row in store.rows(REPAIR_MODULE)
            if row.get("状态") == REPAIR_PENDING and (row.get("目标表名") or PAY_TABLE) == table_name
        )
        return {
            "table_name": table_name,
            "recovered_count": len(recovered),
            "skipped_count": skipped,
            "pending_count": pending,
            "recovered": recovered,
        }

    # ───────────────────────────────
    # Iceberg 元数据
    # ───────────────────────────────

    def sync_iceberg_metadata(self, repair: dict[str, Any]) -> str:
        """把清单的归属结果写进 Iceberg 分区元数据，并携带湖上分区的来源版本。"""
        table_name = repair.get("目标表名") or PAY_TABLE
        iceberg = next(
            (row for row in store.rows("iceberg_table") if row.get("表名称") == table_name), None)
        if iceberg is None:
            return f"Iceberg 表 {table_name} 未登记，元数据未同步"

        partition_rows = store.rows("iceberg_partition_metadata")
        existing = next(
            (row for row in partition_rows
             if row.get("table_name") == table_name
             and row.get("event_code") == repair.get("支付事件编号")),
            None,
        )
        payload = {
            "table_name": table_name,
            "event_code": repair.get("支付事件编号"),
            "partition": repair.get("目标分区"),
            "original_time": repair.get("原始时间"),
            "source_version": repair.get("来源版本"),
            "repair_id": repair.get("id"),
            "repair_code": repair.get("清单编号"),
            "status": REPAIR_ASSIGNED,
            "synced_at": datetime.now().isoformat(timespec="seconds"),
        }
        if existing is not None:
            existing.update(payload)
        else:
            partition_rows.append(payload)
        # 同一事件在 Iceberg 元数据中只能有一条归属记录，删掉指向其他分区的历史残留。
        stale = [
            row for row in partition_rows
            if row.get("table_name") == table_name
            and row.get("event_code") == repair.get("支付事件编号")
            and row.get("partition") != repair.get("目标分区")
        ]
        for row in stale:
            partition_rows.remove(row)
        iceberg["最近分区修复"] = repair.get("目标分区")
        iceberg["分区来源版本"] = repair.get("来源版本")
        repair["已同步Iceberg"] = True
        return f"Iceberg 表 {table_name} 分区元数据已更新"

    def list_iceberg_partitions(
        self, *, table_name: str = PAY_TABLE,
    ) -> dict[str, Any]:
        """Iceberg 管理页查看支付表分区：归属状态按事件去重后展示。"""
        iceberg = next(
            (row for row in store.rows("iceberg_table") if row.get("表名称") == table_name), None)
        rows = [
            row for row in store.rows("iceberg_partition_metadata")
            if row.get("table_name") == table_name
        ]
        # 同一事件只保留一条归属结果；历史残留按同步时间取最新。
        deduped: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("event_code"))
            current = deduped.get(key)
            if current is None or str(row.get("synced_at", "")) >= str(current.get("synced_at", "")):
                deduped[key] = row
        entries = sorted(deduped.values(), key=lambda row: str(row.get("event_code")))
        pending_repairs = sum(
            1 for row in store.rows(REPAIR_MODULE)
            if (row.get("目标表名") or PAY_TABLE) == table_name
            and row.get("状态") == REPAIR_PENDING
        )
        return {
            "table_name": table_name,
            "iceberg_exists": iceberg is not None,
            "metadata_version": iceberg.get("元数据版本") if iceberg else None,
            "partition_source_version": iceberg.get("分区来源版本") if iceberg else None,
            "partition_count": len(entries),
            "pending_repair_count": pending_repairs,
            "partitions": entries,
        }

    # ───────────────────────────────
    # 内部辅助
    # ───────────────────────────────

    def _normalise_business_date(self, value: Any) -> str | None:
        """业务日期只接受 YYYY-MM-DD；空值、占位符与非法日期都按缺失处理。"""
        if value is None:
            return None
        text = str(value).strip()
        if not text or text in ("-", "—", "null", "None"):
            return None
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None

    def _business_partition(self, table_name: str, business_date: str) -> str:
        definition = self.partition_definition(table_name)
        granularity = (definition or {}).get("分区粒度", "day")
        if granularity == "hour":
            return f"{table_name}/dt={business_date}"
        if granularity == "month":
            return f"{table_name}/dt={business_date[:7]}"
        return f"{table_name}/dt={business_date}"

    def _resolve_target_partition(self, event: dict[str, Any], rule: str) -> str:
        """按归属口径计算目标分区：按接入时间归属 或 按空值隔离。"""
        table_name = event.get("目标表名") or PAY_TABLE
        if rule == ROUTING_BY_ISOLATION:
            return ISOLATION_PARTITION
        ingested = _parse_datetime(event.get("接入时间"))
        if ingested is None:
            return ISOLATION_PARTITION
        return self._business_partition(table_name, ingested.date().isoformat())

    def _find_event(self, event_code: Any) -> dict[str, Any] | None:
        for row in store.rows(EVENT_MODULE):
            if row.get("支付事件编号") == event_code:
                return row
        return None

    def _check_owner(self, row: dict[str, Any], operator_team: str | None) -> str | None:
        """修改权限按数据归属控制；其他团队只读，返回可读的拦截原因。"""
        owner = row.get("归属团队")
        if not owner or operator_team is None:
            return None
        if operator_team != owner:
            return f"清单归属团队为 {owner}，{operator_team} 只能查看受控结果"
        return None


repair_service = AbnormalPartitionRepairService()
