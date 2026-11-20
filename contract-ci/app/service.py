"""业务服务层：契约注册、变更提交、合并、规则集版本与回退。"""
from __future__ import annotations

from . import db
from .diff_engine import diff_contracts, summarize
from .gate import (build_impact_report, build_migration_tasks, evaluate_gate,
                   merge_policy)


class ServiceError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


# ---------- 契约 ----------

def create_contract(name: str, owner: str, description: str, content: dict,
                    author: str = "") -> dict:
    if db.query_one("SELECT id FROM contracts WHERE name = ?", (name,)):
        raise ServiceError(409, f"契约 {name} 已存在")
    cid = db.execute(
        "INSERT INTO contracts(name, owner, description, created_at) VALUES (?,?,?,?)",
        (name, owner, description, db.now()),
    )
    vid = db.execute(
        """INSERT INTO contract_versions(contract_id, version, content_json, status, created_by, created_at)
           VALUES (?,?,?,?,?,?)""",
        (cid, 1, db.dumps(content), "published", author or owner, db.now()),
    )
    db.execute("UPDATE contracts SET current_version_id = ? WHERE id = ?", (vid, cid))
    return get_contract(name)


def get_contract(name: str) -> dict:
    row = db.query_one("SELECT * FROM contracts WHERE name = ?", (name,))
    if not row:
        raise ServiceError(404, f"契约 {name} 不存在")
    ver = None
    if row.get("current_version_id"):
        ver = db.query_one("SELECT * FROM contract_versions WHERE id = ?", (row["current_version_id"],))
    row["current_version"] = ver
    row["content"] = db.loads(ver["content_json"], {}) if ver else {}
    return row


def list_contracts() -> list[dict]:
    rows = db.query("SELECT * FROM contracts ORDER BY name")
    for r in rows:
        ver = db.query_one("SELECT * FROM contract_versions WHERE id = ?",
                           (r.get("current_version_id") or -1,))
        r["version"] = ver["version"] if ver else 0
        r["open_changes"] = db.query_one(
            "SELECT COUNT(*) AS n FROM change_requests WHERE contract_id = ? AND status = 'open'",
            (r["id"],))["n"]
    return rows


# ---------- 变更请求 ----------

def _insert_impact_and_tasks(cr_id: int, contract_name: str, changes: list[dict]) -> None:
    report = build_impact_report(contract_name, changes)
    db.execute(
        "INSERT INTO impact_reports(change_request_id, report_json, generated_at) VALUES (?,?,?)",
        (cr_id, db.dumps(report), db.now()),
    )
    tasks = build_migration_tasks(changes, report["affected_downstreams"])
    for t in tasks:
        db.execute(
            """INSERT INTO migration_tasks(change_request_id, title, description, assignee_team,
               status, source_change_key, created_at, updated_at)
               VALUES (?,?,?,?, 'todo', ?, ?, ?)""",
            (cr_id, t["title"], t["description"], t["assignee_team"],
             t["source_change_key"], db.now(), db.now()),
        )


def _publish_version(contract_id: int, content: dict, author: str, cr_id: int) -> int:
    latest = db.query_one(
        "SELECT MAX(version) AS v FROM contract_versions WHERE contract_id = ?", (contract_id,))
    version = (latest["v"] or 0) + 1
    vid = db.execute(
        """INSERT INTO contract_versions(contract_id, version, content_json, status, created_by,
           change_request_id, created_at) VALUES (?,?,?,?,?,?,?)""",
        (contract_id, version, db.dumps(content), "published", author, cr_id, db.now()),
    )
    db.execute("UPDATE contracts SET current_version_id = ? WHERE id = ?", (vid, contract_id))
    return vid


def submit_change(contract_name: str, title: str, author: str, content: dict) -> dict:
    contract = get_contract(contract_name)
    base_content = contract["content"]
    changes = diff_contracts(base_content, content)
    gate = evaluate_gate(changes)
    cr_id = db.execute(
        """INSERT INTO change_requests(contract_id, title, author, base_version_id, proposed_json,
           diff_json, gate_status, gate_detail_json, status, created_at)
           VALUES (?,?,?,?,?,?,?,?, 'open', ?)""",
        (contract["id"], title, author, contract.get("current_version_id"),
         db.dumps(content), db.dumps(changes), gate["status"], db.dumps(gate), db.now()),
    )
    if gate["status"] == "BLOCKED":
        _insert_impact_and_tasks(cr_id, contract_name, changes)
    elif gate["auto_merge"]:
        _do_merge(cr_id, auto=True)
    return get_change(cr_id)


def get_change(cr_id: int) -> dict:
    cr = db.query_one("SELECT * FROM change_requests WHERE id = ?", (cr_id,))
    if not cr:
        raise ServiceError(404, f"变更请求 #{cr_id} 不存在")
    contract = db.query_one("SELECT * FROM contracts WHERE id = ?", (cr["contract_id"],))
    cr["contract_name"] = contract["name"] if contract else "?"
    cr["diff"] = db.loads(cr.pop("diff_json"), [])
    cr["gate_detail"] = db.loads(cr.pop("gate_detail_json"), {})
    cr["proposed"] = db.loads(cr.pop("proposed_json"), {})
    base = db.query_one("SELECT * FROM contract_versions WHERE id = ?",
                        (cr.get("base_version_id") or -1,))
    cr["base_version"] = base
    cr["base_content"] = db.loads(base["content_json"], {}) if base else {}
    if cr.get("merged_version_id"):
        mv = db.query_one("SELECT version FROM contract_versions WHERE id = ?",
                          (cr["merged_version_id"],))
        cr["merged_version"] = mv["version"] if mv else None
    else:
        cr["merged_version"] = None
    report = db.query_one(
        "SELECT * FROM impact_reports WHERE change_request_id = ? ORDER BY id DESC LIMIT 1", (cr_id,))
    cr["impact_report"] = db.loads(report["report_json"], None) if report else None
    cr["tasks"] = db.query(
        "SELECT * FROM migration_tasks WHERE change_request_id = ? ORDER BY id", (cr_id,))
    cr["approvals"] = db.query(
        "SELECT * FROM approvals WHERE change_request_id = ? ORDER BY created_at, id", (cr_id,))
    allowed, reasons = merge_policy(
        cr["gate_status"], cr["approvals"], cr["tasks"], cr["impact_report"])
    cr["merge_allowed"] = allowed
    cr["merge_reasons"] = reasons
    cr["summary"] = summarize(cr["diff"])
    return cr


def list_changes(status: str | None = None) -> list[dict]:
    sql = """SELECT cr.*, c.name AS contract_name FROM change_requests cr
             JOIN contracts c ON c.id = cr.contract_id"""
    args: tuple = ()
    if status:
        sql += " WHERE cr.status = ?"
        args = (status,)
    sql += " ORDER BY cr.id DESC"
    rows = db.query(sql, args)
    for r in rows:
        diff = db.loads(r["diff_json"], [])
        r["summary"] = summarize(diff)
        r.pop("diff_json", None)
        r.pop("proposed_json", None)
        r.pop("gate_detail_json", None)
    return rows


def add_approval(cr_id: int, approver: str, role: str, decision: str, comment: str) -> dict:
    cr = db.query_one("SELECT * FROM change_requests WHERE id = ?", (cr_id,))
    if not cr:
        raise ServiceError(404, f"变更请求 #{cr_id} 不存在")
    if cr["status"] != "open":
        raise ServiceError(409, "变更请求已关闭，无法审批")
    if decision not in ("approve", "reject"):
        raise ServiceError(400, "decision 必须是 approve 或 reject")
    db.execute(
        "INSERT INTO approvals(change_request_id, approver, role, decision, comment, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (cr_id, approver, role, decision, comment, db.now()),
    )
    return get_change(cr_id)


def _do_merge(cr_id: int, auto: bool = False) -> None:
    cr = db.query_one("SELECT * FROM change_requests WHERE id = ?", (cr_id,))
    content = db.loads(cr["proposed_json"], {})
    vid = _publish_version(cr["contract_id"], content, cr["author"], cr_id)
    db.execute(
        """UPDATE change_requests SET status='merged', auto_merged=?, merged_version_id=?,
           merged_at=? WHERE id=?""",
        (1 if auto else 0, vid, db.now(), cr_id),
    )


def merge_change(cr_id: int) -> dict:
    cr = get_change(cr_id)
    if cr["status"] != "open":
        raise ServiceError(409, "变更请求已关闭")
    if not cr["merge_allowed"]:
        raise ServiceError(403, "门禁未通过：" + "；".join(cr["merge_reasons"]))
    _do_merge(cr_id, auto=False)
    return get_change(cr_id)


def close_change(cr_id: int) -> dict:
    cr = db.query_one("SELECT * FROM change_requests WHERE id = ?", (cr_id,))
    if not cr:
        raise ServiceError(404, f"变更请求 #{cr_id} 不存在")
    if cr["status"] != "open":
        raise ServiceError(409, "变更请求已关闭")
    db.execute("UPDATE change_requests SET status='rejected' WHERE id=?", (cr_id,))
    return get_change(cr_id)


def update_task(task_id: int, status: str) -> dict:
    if status not in ("todo", "in_progress", "done"):
        raise ServiceError(400, "非法任务状态")
    t = db.query_one("SELECT * FROM migration_tasks WHERE id = ?", (task_id,))
    if not t:
        raise ServiceError(404, f"迁移任务 #{task_id} 不存在")
    db.execute("UPDATE migration_tasks SET status=?, updated_at=? WHERE id=?",
               (status, db.now(), task_id))
    return db.query_one("SELECT * FROM migration_tasks WHERE id = ?", (task_id,))


# ---------- 质量规则集（版本 + 回退） ----------

def create_ruleset(name: str, rules: list[dict], created_by: str, note: str) -> dict:
    latest = db.query_one("SELECT MAX(version) AS v FROM rulesets WHERE name = ?", (name,))
    version = (latest["v"] or 0) + 1
    db.execute("UPDATE rulesets SET status='superseded' WHERE name=? AND status='active'", (name,))
    rid = db.execute(
        """INSERT INTO rulesets(name, version, rules_json, status, parent_id, note, created_by, created_at)
           VALUES (?,?,?, 'active', ?, ?, ?, ?)""",
        (name, version, db.dumps(rules),
         db.query_one("SELECT id FROM rulesets WHERE name=? AND version=?",
                      (name, version - 1))["id"] if version > 1 else None,
         note, created_by, db.now()),
    )
    return db.query_one("SELECT * FROM rulesets WHERE id = ?", (rid,))


def rollback_ruleset(name: str, target_version_id: int, actor: str, note: str) -> dict:
    target = db.query_one("SELECT * FROM rulesets WHERE id = ? AND name = ?",
                          (target_version_id, name))
    if not target:
        raise ServiceError(404, "目标规则版本不存在")
    active = db.query_one("SELECT * FROM rulesets WHERE name=? AND status='active'", (name,))
    if active and active["id"] == target["id"]:
        raise ServiceError(409, "目标版本已是当前生效版本")
    latest = db.query_one("SELECT MAX(version) AS v FROM rulesets WHERE name = ?", (name,))
    version = (latest["v"] or 0) + 1
    db.execute("UPDATE rulesets SET status='superseded' WHERE name=? AND status='active'", (name,))
    rid = db.execute(
        """INSERT INTO rulesets(name, version, rules_json, status, parent_id, rollback_of_id,
           note, created_by, created_at) VALUES (?,?,?, 'active', ?, ?, ?, ?, ?)""",
        (name, version, target["rules_json"],
         active["id"] if active else None, target["id"],
         note or f"回退到 v{target['version']}", actor, db.now()),
    )
    return db.query_one("SELECT * FROM rulesets WHERE id = ?", (rid,))


def list_rulesets() -> list[dict]:
    names = db.query("SELECT DISTINCT name FROM rulesets ORDER BY name")
    out = []
    for n in names:
        versions = db.query(
            "SELECT * FROM rulesets WHERE name = ? ORDER BY version DESC", (n["name"],))
        for v in versions:
            v["rules"] = db.loads(v["rules_json"], [])
            if v.get("rollback_of_id"):
                tgt = db.query_one("SELECT version FROM rulesets WHERE id = ?",
                                   (v["rollback_of_id"],))
                v["rollback_of_version"] = tgt["version"] if tgt else None
        out.append({"name": n["name"], "versions": versions})
    return out


# ---------- 依赖图 ----------

def dependency_graph() -> dict:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for c in list_contracts():
        nodes[c["name"]] = {"name": c["name"], "kind": "contract", "owner": c["owner"]}
        content = get_contract(c["name"])["content"]
        for dep in content.get("dependencies", []):
            dname = dep.get("name", "")
            if not dname:
                continue
            if dname not in nodes:
                nodes[dname] = {"name": dname, "kind": dep.get("kind", "table"),
                                "owner": dep.get("team", "")}
            edges.append({"source": c["name"], "target": dname,
                          "criticality": dep.get("criticality", "medium")})
    return {"nodes": list(nodes.values()), "edges": edges}
