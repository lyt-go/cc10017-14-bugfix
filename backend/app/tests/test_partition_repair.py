"""异常分区修复清单测试：缺业务日期归属、单向落库、断点恢复与 Iceberg 同步。"""
from __future__ import annotations

import unittest

from app.services.partition_repair import (
    REPAIR_ASSIGNED,
    REPAIR_PENDING,
    AbnormalPartitionRepairService,
)
from app.store import Store, store


class RepairServiceCase(unittest.TestCase):
    """每个用例重置内存仓库，避免清单跨用例互相污染。"""

    def setUp(self) -> None:
        fresh = Store()
        store._tables = fresh._tables
        # 清空种子里的支付事件与清单，只保留分区定义 / Iceberg 表这类配置数据。
        store.rows("pay_event").clear()
        store.rows("abnormal_partition_repair").clear()
        store.rows("iceberg_partition_metadata").clear()
        self.service = AbnormalPartitionRepairService()

    def test_missing_business_date_does_not_use_today_partition(self) -> None:
        """缺业务日期不再兜底到当天分区，而是进入修复清单。"""
        event, _ = self.service.register_event({
            "支付事件编号": "PAYE-T1", "接入时间": "2026-11-19T09:05:00",
        })
        self.assertIsNone(event["业务日期"])
        self.assertEqual(event["状态"], REPAIR_PENDING)
        repairs, total = self.service.list_repairs()
        self.assertEqual(total, 1)
        self.assertEqual(repairs[0]["目标分区"], "payments/dt=2026-11-19")
        self.assertNotEqual(repairs[0]["目标分区"], "payments/dt=2026-11-20")

    def test_repair_keeps_original_time_and_target_partition(self) -> None:
        """清单同时保留原始时间、目标分区与来源版本。"""
        self.service.register_event({
            "支付事件编号": "PAYE-T2", "接入时间": "2026-11-18T23:40:00",
        })
        repair = self.service.list_repairs()[0][0]
        self.assertEqual(repair["原始时间"], "2026-11-18T23:40:00")
        self.assertEqual(repair["目标分区"], "payments/dt=2026-11-18")
        self.assertEqual(repair["来源版本"], "LAKE-0003@v1")

    def test_assign_is_one_way_and_syncs_iceberg(self) -> None:
        """待修复 → 已归属单向落库，重复归属不改写并同步 Iceberg 元数据。"""
        self.service.register_event({
            "支付事件编号": "PAYE-T3", "接入时间": "2026-11-19T09:05:00",
        })
        repair = self.service.list_repairs()[0][0]
        entry, _ = self.service.assign_partition(repair["id"], operator_team="数据平台组")
        self.assertEqual(entry["状态"], REPAIR_ASSIGNED)
        assigned_partition = entry["目标分区"]

        # 再次归属：状态不变、分区不变，不允许回退到待修复。
        again, message = self.service.assign_partition(
            repair["id"], operator_team="数据平台组")
        self.assertEqual(again["状态"], REPAIR_ASSIGNED)
        self.assertEqual(again["目标分区"], assigned_partition)
        self.assertIn("不能重复改写", message)

        detail = self.service.list_iceberg_partitions()
        self.assertEqual(detail["partition_count"], 1)
        self.assertEqual(detail["partitions"][0]["partition"], assigned_partition)
        self.assertEqual(detail["partition_source_version"], "LAKE-0003@v1")
        self.assertEqual(detail["pending_repair_count"], 0)

    def test_same_event_has_single_ownership(self) -> None:
        """同一事件重复登记只保留一条清单、一个归属分区。"""
        self.service.register_event({
            "支付事件编号": "PAYE-T4", "接入时间": "2026-11-19T09:05:00",
        })
        _event, message = self.service.register_event({
            "支付事件编号": "PAYE-T4", "接入时间": "2026-11-19T09:05:00",
        })
        self.assertIn("已登记", message)
        self.assertEqual(self.service.list_repairs()[1], 1)

        repair = self.service.list_repairs()[0][0]
        self.service.assign_partition(repair["id"], operator_team="数据平台组")
        self.service.register_event({
            "支付事件编号": "PAYE-T4", "接入时间": "2026-11-19T09:05:00",
        })
        self.assertEqual(self.service.list_repairs()[1], 1)
        self.assertEqual(self.service.list_iceberg_partitions()["partition_count"], 1)

    def test_routing_rules_leave_different_partitions_but_one_ownership(self) -> None:
        """按接入时间归属与按空值隔离目标分区不同，但事件只归属一个可查询分区。"""
        self.service.set_routing_rule("按空值隔离", operator_team="数据平台组")
        event, _ = self.service.register_event({
            "支付事件编号": "PAYE-T5", "接入时间": "2026-11-19T09:05:00",
        })
        repair = self.service.list_repairs()[0][0]
        self.assertEqual(repair["目标分区"], "payments/__missing_business_date__")

        # 归属口径在登记后改回按接入时间：已登记的目标分区保持不变，只记录冲突判定。
        self.service.set_routing_rule("按接入时间归属", operator_team="数据平台组")
        entry, _ = self.service.assign_partition(repair["id"], operator_team="数据平台组")
        self.assertEqual(entry["目标分区"], "payments/__missing_business_date__")
        self.assertIn("冲突判定", entry)
        self.assertIn("payments/dt=2026-11-19", entry["冲突判定"])
        detail = self.service.list_iceberg_partitions()
        self.assertEqual([row["partition"] for row in detail["partitions"]],
                         ["payments/__missing_business_date__"])

    def test_breakpoint_recovery_keeps_existing_rows(self) -> None:
        """断点恢复保留此前完整结果，只补登记未进清单的事件。"""
        self.service.register_event({
            "支付事件编号": "PAYE-T6", "接入时间": "2026-11-19T09:05:00",
        })
        repair = self.service.list_repairs()[0][0]
        self.service.assign_partition(repair["id"], operator_team="数据平台组")
        before = dict(self.service.get_repair(repair["id"]))

        # 模拟写入中断：新事件已进入事件表，但还没写入清单。
        store.rows("pay_event").append({
            "id": 99, "支付事件编号": "PAYE-T7", "业务日期": None,
            "接入时间": "2026-11-19T22:10:00", "目标表名": "payments",
            "金额": "1.00", "状态": REPAIR_PENDING, "目标分区": None,
        })
        result = self.service.recover_from_breakpoint()
        self.assertEqual(result["recovered_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        after = self.service.get_repair(repair["id"])
        self.assertEqual(after["状态"], before["状态"])
        self.assertEqual(after["目标分区"], before["目标分区"])
        self.assertEqual(self.service.list_repairs()[1], 2)

    def test_assign_ignores_caller_supplied_partition(self) -> None:
        """归属只认清单保留的目标分区，调用方不能临时改写到别的分区。"""
        self.service.register_event({
            "支付事件编号": "PAYE-T9", "接入时间": "2026-11-19T09:05:00",
        })
        repair = self.service.list_repairs()[0][0]
        entry, _ = self.service.assign_partition(repair["id"], operator_team="数据平台组")
        self.assertEqual(entry["目标分区"], "payments/dt=2026-11-19")
        detail = self.service.list_iceberg_partitions()
        self.assertEqual([row["partition"] for row in detail["partitions"]],
                         ["payments/dt=2026-11-19"])

    def test_owner_team_controls_modification(self) -> None:
        """其他团队只能查看，不能推进归属或调整口径。"""
        self.service.register_event({
            "支付事件编号": "PAYE-T8", "接入时间": "2026-11-19T09:05:00",
        })
        repair = self.service.list_repairs()[0][0]
        entry, message = self.service.assign_partition(repair["id"], operator_team="订单数据组")
        self.assertIsNone(entry)
        self.assertIn("只能查看受控结果", message)
        self.assertEqual(self.service.get_repair(repair["id"])["状态"], REPAIR_PENDING)

        definition, message = self.service.set_routing_rule("按空值隔离", operator_team="订单数据组")
        self.assertIsNone(definition)
        self.assertIn("只能查看受控结果", message)


if __name__ == "__main__":
    unittest.main()
