"""端到端 API 测试：契约注册、自动合并、破坏性变更流程、规则回退。

注意：测试按顺序执行，状态在类内累积（test_01 注册契约，后续用例基于其内容）。
"""
import os
import sys
import tempfile
import unittest

# 必须在导入 app 之前指定独立数据库
_tmp = tempfile.mkdtemp(prefix="contract-ci-test-")
os.environ["CONTRACT_CI_DB"] = os.path.join(_tmp, "test.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


def content(fields, partitions=None, rules=None, deps=None):
    return {
        "fields": fields,
        "partitions": partitions or [],
        "quality_rules": rules or [],
        "dependencies": deps if deps is not None else [],
    }


BASE_FIELDS = [
    {"name": "id", "type": "bigint", "nullable": False, "semantic_tags": ["bi_key"],
     "description": "主键"},
    {"name": "val", "type": "string", "nullable": True, "semantic_tags": [],
     "description": "值"},
]
EXTRA_FIELD = {"name": "extra", "type": "string", "nullable": True,
               "semantic_tags": [], "description": "新增可空字段"}
ORDERS_DEPS = [{"name": "t.downstream", "team": "下游组", "kind": "table",
                "criticality": "high"}]
DOWNSTREAM_DEPS = [{"name": "t.transitive", "team": "更下游", "kind": "job",
                    "criticality": "medium"}]


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.client = TestClient(app)

    def test_01_create_contract(self):
        r = self.client.post("/api/contracts", json={
            "name": "t.orders", "owner": "测试组", "description": "测试契约",
            "content": content(BASE_FIELDS, deps=ORDERS_DEPS),
        })
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["current_version"]["version"], 1)

        r2 = self.client.post("/api/contracts", json={
            "name": "t.downstream", "owner": "下游组", "description": "",
            "content": content(BASE_FIELDS, deps=DOWNSTREAM_DEPS),
        })
        self.assertEqual(r2.status_code, 201)

        r3 = self.client.post("/api/contracts", json={
            "name": "t.orders", "owner": "x", "description": "",
            "content": content(BASE_FIELDS),
        })
        self.assertEqual(r3.status_code, 409)  # 重复注册

    def test_02_compatible_change_auto_merges(self):
        # 仅新增可空字段（保留原有依赖）→ PASS → 自动合并
        r = self.client.post("/api/contracts/t.orders/changes", json={
            "title": "加字段", "author": "dev",
            "content": content(BASE_FIELDS + [EXTRA_FIELD], deps=ORDERS_DEPS),
        })
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["gate_status"], "PASS")
        self.assertEqual(body["status"], "merged")
        self.assertEqual(body["auto_merged"], 1)
        c = self.client.get("/api/contracts/t.orders").json()
        self.assertEqual(c["current_version"]["version"], 2)

    def test_03_semantic_tag_change_needs_review(self):
        # 语义标记变化（val → pii）→ NEEDS_REVIEW；批准后合并
        fields = [dict(f, semantic_tags=["pii"]) if f["name"] == "val" else dict(f)
                  for f in BASE_FIELDS] + [EXTRA_FIELD]
        r = self.client.post("/api/contracts/t.orders/changes", json={
            "title": "val 标记为 PII", "author": "dev",
            "content": content(fields, deps=ORDERS_DEPS),
        })
        body = r.json()
        self.assertEqual(body["gate_status"], "NEEDS_REVIEW")
        self.assertEqual(body["status"], "open")
        r2 = self.client.post(f"/api/changes/{body['id']}/merge")
        self.assertEqual(r2.status_code, 403)  # 未批准不可合并
        self.client.post(f"/api/changes/{body['id']}/approvals", json={
            "approver": "steward-1", "role": "steward", "decision": "approve"})
        r3 = self.client.post(f"/api/changes/{body['id']}/merge")
        self.assertEqual(r3.status_code, 200, r3.text)
        self.assertEqual(r3.json()["status"], "merged")

    def test_04_breaking_change_full_flow(self):
        # 删除 val 字段 + id 类型收窄 → BLOCKED，自动生成影响报告与迁移任务
        fields = [dict(EXTRA_FIELD),
                  {"name": "id", "type": "int", "nullable": False,
                   "semantic_tags": ["bi_key"], "description": "主键"}]
        r = self.client.post("/api/contracts/t.orders/changes", json={
            "title": "删字段并收窄类型", "author": "dev",
            "content": content(fields, deps=ORDERS_DEPS),
        })
        body = r.json()
        self.assertEqual(body["gate_status"], "BLOCKED")
        self.assertEqual(body["status"], "open")

        detail = self.client.get(f"/api/changes/{body['id']}").json()
        ir = detail["impact_report"]
        self.assertIsNotNone(ir)
        names = {a["name"] for a in ir["affected_downstreams"]}
        self.assertIn("t.downstream", names)   # 直接下游
        self.assertIn("t.transitive", names)   # 传递下游
        self.assertEqual(ir["risk_level"], "high")
        self.assertGreater(len(detail["tasks"]), 0)
        self.assertFalse(detail["merge_allowed"])
        r_merge = self.client.post(f"/api/changes/{body['id']}/merge")
        self.assertEqual(r_merge.status_code, 403)

        # 推进全部迁移任务
        for t in detail["tasks"]:
            r_t = self.client.patch(f"/api/tasks/{t['id']}", json={"status": "in_progress"})
            self.assertEqual(r_t.status_code, 200)
        # 两个批准，含一个下游负责人
        self.client.post(f"/api/changes/{body['id']}/approvals", json={
            "approver": "owner-1", "role": "owner", "decision": "approve"})
        self.client.post(f"/api/changes/{body['id']}/approvals", json={
            "approver": "下游组-lead", "role": "downstream_owner", "decision": "approve"})
        detail2 = self.client.get(f"/api/changes/{body['id']}").json()
        self.assertTrue(detail2["merge_allowed"], detail2["merge_reasons"])
        r_merge2 = self.client.post(f"/api/changes/{body['id']}/merge")
        self.assertEqual(r_merge2.status_code, 200, r_merge2.text)
        self.assertEqual(r_merge2.json()["status"], "merged")

    def test_05_reject_blocks_merge(self):
        new_fields = BASE_FIELDS + [
            {"name": "req", "type": "int", "nullable": False, "semantic_tags": [],
             "description": "非空新字段"},
        ]
        r = self.client.post("/api/contracts/t.downstream/changes", json={
            "title": "加非空字段", "author": "dev",
            "content": content(new_fields, deps=DOWNSTREAM_DEPS),
        })
        cr_id = r.json()["id"]
        self.assertEqual(r.json()["gate_status"], "NEEDS_REVIEW")
        self.client.post(f"/api/changes/{cr_id}/approvals", json={
            "approver": "a", "role": "owner", "decision": "reject", "comment": "再想想"})
        detail = self.client.get(f"/api/changes/{cr_id}").json()
        self.assertFalse(detail["merge_allowed"])
        # 驳回后再批准 → 可合并
        self.client.post(f"/api/changes/{cr_id}/approvals", json={
            "approver": "b", "role": "owner", "decision": "approve"})
        detail2 = self.client.get(f"/api/changes/{cr_id}").json()
        self.assertTrue(detail2["merge_allowed"], detail2["merge_reasons"])

    def test_06_ruleset_rollback(self):
        r = self.client.post("/api/rulesets", json={
            "name": "rs-test", "created_by": "qa", "note": "v1",
            "rules": [{"id": "r1", "name": "规则一", "rule_type": "not_null",
                       "params": {}, "severity": "blocking"}],
        })
        self.assertEqual(r.status_code, 201)
        v1_id = r.json()["id"]
        self.client.post("/api/rulesets", json={
            "name": "rs-test", "created_by": "qa", "note": "v2",
            "rules": [{"id": "r1", "name": "规则一", "rule_type": "not_null",
                       "params": {}, "severity": "blocking"},
                      {"id": "r2", "name": "规则二", "rule_type": "unique",
                       "params": {}, "severity": "warn"}],
        })
        r3 = self.client.post("/api/rulesets/rs-test/rollback", json={
            "target_version_id": v1_id, "actor": "qa", "note": "v2 有问题"})
        self.assertEqual(r3.status_code, 201, r3.text)
        body = r3.json()
        self.assertEqual(body["version"], 3)
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["rollback_of_id"], v1_id)
        self.assertEqual(len(db.loads(body["rules_json"])), 1)  # 内容回到 v1
        versions = [v for rs in self.client.get("/api/rulesets").json()
                    if rs["name"] == "rs-test" for v in rs["versions"]]
        self.assertEqual(len(versions), 3)  # 历史版本全部保留

    def test_07_pages_render(self):
        for path in ["/", "/changes", "/rulesets", "/graph", "/contracts/t.orders"]:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, f"{path} -> {r.status_code}")
        cr_id = self.client.get("/api/changes").json()[0]["id"]
        r = self.client.get(f"/changes/{cr_id}")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
