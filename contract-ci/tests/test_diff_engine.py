"""变更分级引擎单元测试。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.diff_engine import (BREAKING, INFO, WARNING, compare_types,
                             diff_contracts, diff_fields, diff_partitions,
                             diff_quality_rules, diff_dependencies, summarize)


def field(name, type_="string", nullable=True, tags=None, desc=""):
    return {"name": name, "type": type_, "nullable": nullable,
            "semantic_tags": tags or [], "description": desc}


class TestCompareTypes(unittest.TestCase):
    def test_same(self):
        self.assertEqual(compare_types("bigint", "bigint")[0], "same")
        self.assertEqual(compare_types("decimal(10,2)", "decimal(10,2)")[0], "same")

    def test_widening(self):
        self.assertEqual(compare_types("int", "bigint")[0], "widening")
        self.assertEqual(compare_types("float", "double")[0], "widening")
        self.assertEqual(compare_types("varchar(50)", "varchar(100)")[0], "widening")
        self.assertEqual(compare_types("decimal(10,2)", "decimal(12,2)")[0], "widening")
        self.assertEqual(compare_types("date", "timestamp")[0], "widening")
        self.assertEqual(compare_types("int", "string")[0], "widening")

    def test_narrowing(self):
        self.assertEqual(compare_types("bigint", "int")[0], "narrowing")
        self.assertEqual(compare_types("decimal(10,2)", "decimal(8,2)")[0], "narrowing")
        self.assertEqual(compare_types("string", "int")[0], "narrowing")
        self.assertEqual(compare_types("timestamp", "date")[0], "narrowing")
        self.assertEqual(compare_types("varchar(100)", "varchar(50)")[0], "narrowing")

    def test_incompatible(self):
        self.assertEqual(compare_types("boolean", "timestamp")[0], "incompatible")


class TestFieldDiff(unittest.TestCase):
    def test_add_nullable_is_info(self):
        changes = diff_fields([], [field("a", "int", nullable=True)])
        self.assertEqual(changes[0]["severity"], INFO)
        self.assertEqual(changes[0]["kind"], "field_added")

    def test_add_required_is_warning(self):
        changes = diff_fields([], [field("a", "int", nullable=False)])
        self.assertEqual(changes[0]["severity"], WARNING)
        self.assertEqual(changes[0]["kind"], "field_added_required")

    def test_remove_is_breaking(self):
        changes = diff_fields([field("a")], [])
        self.assertEqual(changes[0]["severity"], BREAKING)
        self.assertEqual(changes[0]["kind"], "field_removed")

    def test_type_narrowing_is_breaking(self):
        changes = diff_fields([field("a", "bigint")], [field("a", "int")])
        sev = {c["kind"]: c["severity"] for c in changes}
        self.assertEqual(sev["type_narrowed"], BREAKING)

    def test_type_widening_is_info(self):
        changes = diff_fields([field("a", "int")], [field("a", "bigint")])
        self.assertEqual(changes[0]["severity"], INFO)

    def test_semantic_tags_is_warning(self):
        changes = diff_fields([field("a", tags=["pii"])], [field("a", tags=[])])
        kinds = [c["kind"] for c in changes]
        self.assertIn("semantic_tags_changed", kinds)
        self.assertEqual(changes[0]["severity"], WARNING)

    def test_nullable_tighten_warning_relax_info(self):
        c1 = diff_fields([field("a", nullable=True)], [field("a", nullable=False)])
        self.assertEqual(c1[0]["severity"], WARNING)
        c2 = diff_fields([field("a", nullable=False)], [field("a", nullable=True)])
        self.assertEqual(c2[0]["severity"], INFO)


class TestOtherDiffs(unittest.TestCase):
    def test_partition_removed_breaking(self):
        old = [{"name": "dt", "type": "string", "granularity": "day"}]
        self.assertEqual(diff_partitions(old, [])[0]["severity"], BREAKING)

    def test_partition_added_warning(self):
        new = [{"name": "hr", "type": "string", "granularity": "hour"}]
        self.assertEqual(diff_partitions([], new)[0]["severity"], WARNING)

    def test_partition_granularity_changed_breaking(self):
        old = [{"name": "dt", "type": "string", "granularity": "day"}]
        new = [{"name": "dt", "type": "string", "granularity": "hour"}]
        self.assertEqual(diff_partitions(old, new)[0]["severity"], BREAKING)

    def test_rule_added_and_removed_warning(self):
        r = {"id": "q1", "name": "r1", "rule_type": "not_null", "field": "a",
             "params": {}, "severity": "blocking"}
        self.assertEqual(diff_quality_rules([], [r])[0]["severity"], WARNING)
        self.assertEqual(diff_quality_rules([r], [])[0]["severity"], WARNING)

    def test_rule_severity_lowered_info(self):
        r1 = {"id": "q1", "name": "r1", "rule_type": "not_null", "params": {}, "severity": "blocking"}
        r2 = dict(r1, severity="warn")
        self.assertEqual(diff_quality_rules([r1], [r2])[0]["severity"], INFO)

    def test_dependency_added_info_removed_warning(self):
        d = {"name": "bi.x", "team": "BI", "kind": "dashboard", "criticality": "high"}
        self.assertEqual(diff_dependencies([], [d])[0]["severity"], INFO)
        self.assertEqual(diff_dependencies([d], [])[0]["severity"], WARNING)


class TestOverall(unittest.TestCase):
    def test_sorted_breaking_first(self):
        old = {"fields": [field("a"), field("b", "bigint")], "partitions": [],
               "quality_rules": [], "dependencies": []}
        new = {"fields": [field("b", "int"), field("c")], "partitions": [],
               "quality_rules": [], "dependencies": []}
        changes = diff_contracts(old, new)
        self.assertEqual(changes[0]["severity"], BREAKING)
        s = summarize(changes)
        self.assertEqual(s["by_severity"][BREAKING], 2)  # 删除 a + b 类型收窄
        self.assertEqual(s["by_severity"][INFO], 1)      # 新增可空 c


if __name__ == "__main__":
    unittest.main()
