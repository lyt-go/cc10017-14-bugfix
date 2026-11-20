"""异常分区修复接口：登记支付事件、生成/归属修复清单、断点恢复与 Iceberg 元数据同步。"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query

from app.schemas import ActionResult, PageResult
from app.services.partition_repair import PAY_TABLE, repair_service

router = APIRouter(prefix="/api/partition_repair", tags=["异常分区修复"])


def _operator_team(payload: dict[str, object] | None, header_value: str | None) -> str | None:
    """操作团队优先取请求体（可携带中文），未提供时退回请求头。"""
    team = (payload or {}).get("操作团队")
    if team:
        return str(team).strip()
    return header_value


@router.get("/repairs", response_model=PageResult[dict])
def list_repairs(
    table_name: str | None = Query(default=None, description="按目标表名过滤，支付表为 payments"),
    status: str | None = Query(default=None, description="待修复、已归属"),
    keyword: str | None = Query(default=None, description="按清单编号或支付事件编号检索"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """读取异常分区修复清单；原始时间与目标分区成对返回。"""
    items, total = repair_service.list_repairs(
        table_name=table_name, status=status, keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/repairs/{entry_id}", response_model=dict)
def get_repair(entry_id: int) -> dict:
    """读取单条清单明细。"""
    entry = repair_service.get_repair(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"异常分区修复清单 {entry_id} 不存在")
    return entry


@router.post("/events", response_model=ActionResult)
def register_event(payload: dict[str, object]) -> ActionResult:
    """登记支付事件：缺业务日期时进入修复清单，不再用当天日期兜底分区。"""
    event, message = repair_service.register_event(payload)
    if event is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=event)


@router.post("/events/check", response_model=dict)
def check_events(payload: dict[str, object] | None = None) -> dict:
    """复核同一支付事件的归属状态：只应出现一个归属分区。"""
    event_code = str((payload or {}).get("支付事件编号") or "").strip()
    repairs = [
        row for row in repair_service.list_repairs(size=100000)[0]
        if not event_code or row.get("支付事件编号") == event_code
    ]
    return {
        "event_code": event_code or None,
        "repair_count": len(repairs),
        "assigned_partitions": sorted({
            str(row.get("目标分区")) for row in repairs if row.get("状态") == "已归属"
        }),
        "repairs": repairs,
    }


@router.post("/repairs/{entry_id}/assign", response_model=ActionResult)
def assign_repair(
    entry_id: int,
    payload: dict[str, object] | None = None,
    x_operator_team: str | None = Header(default=None),
) -> ActionResult:
    """把待修复清单推进到已归属，并同步 Iceberg 分区元数据；已归属不允许重复改写。"""
    entry, message = repair_service.assign_partition(
        entry_id, operator_team=_operator_team(payload, x_operator_team))
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/recover", response_model=ActionResult)
def recover(payload: dict[str, object] | None = None) -> ActionResult:
    """写入中断后从断点恢复：已登记的清单行原样保留，只补登记未进入清单的事件。"""
    table_name = str((payload or {}).get("目标表名") or PAY_TABLE)
    result = repair_service.recover_from_breakpoint(table_name=table_name)
    return ActionResult(
        ok=True,
        message=f"断点恢复完成：新增 {result['recovered_count']} 条，保留 {result['skipped_count']} 条已有清单",
        entry=result,
    )


@router.get("/iceberg-partitions", response_model=dict)
def iceberg_partitions(
    table_name: str = Query(default=PAY_TABLE, description="目标表名，默认支付表 payments"),
) -> dict:
    """Iceberg 管理页读取支付表分区：按事件去重，并带出湖上分区的来源版本。"""
    return repair_service.list_iceberg_partitions(table_name=table_name)


@router.get("/routing", response_model=dict)
def get_routing(table_name: str = Query(default=PAY_TABLE)) -> dict:
    """读取缺失业务日期时的归属口径。"""
    definition = repair_service.partition_definition(table_name)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"支付表 {table_name} 未配置分区定义")
    return {
        "table_name": table_name,
        "routing_rule": repair_service.routing_rule(table_name),
        "source_version": definition.get("来源版本"),
        "owner_team": definition.get("归属团队"),
    }


@router.post("/routing", response_model=ActionResult)
def set_routing(
    payload: dict[str, object],
    x_operator_team: str | None = Header(default=None),
) -> ActionResult:
    """调整归属口径；按数据归属校验修改权限。"""
    rule = str(payload.get("归属口径") or "").strip()
    table_name = str(payload.get("目标表名") or PAY_TABLE)
    definition, message = repair_service.set_routing_rule(
        rule, operator_team=_operator_team(payload, x_operator_team), table_name=table_name)
    if definition is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=definition)
