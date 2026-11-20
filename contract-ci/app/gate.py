"""门禁策略、影响报告与迁移任务生成。"""
from __future__ import annotations

from .diff_engine import BREAKING, INFO, WARNING, summarize
from . import db

# 门禁策略常量
REQUIRED_APPROVALS_REVIEW = 1     # NEEDS_REVIEW 所需批准数
REQUIRED_APPROVALS_BREAKING = 2   # BLOCKED 所需批准数
BREAKING_APPROVER_ROLES = {"downstream_owner", "steward"}  # BLOCKED 至少一名此类角色批准
TASK_ACTIVE_STATES = {"in_progress", "done"}  # 迁移任务须全部进入这些状态才允许合并

GATE_LABEL = {"PASS": "通过", "NEEDS_REVIEW": "需审批", "BLOCKED": "阻塞"}


def evaluate_gate(changes: list[dict]) -> dict:
    """根据变更分级计算门禁结果。"""
    breaking = [c for c in changes if c["severity"] == BREAKING]
    warning = [c for c in changes if c["severity"] == WARNING]
    if breaking:
        return {
            "status": "BLOCKED",
            "label": GATE_LABEL["BLOCKED"],
            "reasons": [f"存在 {len(breaking)} 项破坏性变更，必须生成影响报告与迁移任务，"
                        f"并获得 ≥{REQUIRED_APPROVALS_BREAKING} 个批准（含下游负责人/数据 steward）"],
            "required_approvals": REQUIRED_APPROVALS_BREAKING,
            "auto_merge": False,
        }
    if warning:
        return {
            "status": "NEEDS_REVIEW",
            "label": GATE_LABEL["NEEDS_REVIEW"],
            "reasons": [f"存在 {len(warning)} 项需审批变更，需 ≥{REQUIRED_APPROVALS_REVIEW} 个批准"],
            "required_approvals": REQUIRED_APPROVALS_REVIEW,
            "auto_merge": False,
        }
    return {
        "status": "PASS",
        "label": GATE_LABEL["PASS"],
        "reasons": ["全部为兼容变更，允许自动合并"] if changes else ["无变更"],
        "required_approvals": 0,
        "auto_merge": True,
    }


def _collect_affected(contract_name: str, max_depth: int = 6) -> list[dict]:
    """沿下游依赖图 BFS，返回受影响的直接 + 传递下游。"""
    affected: list[dict] = []
    visited = {contract_name}
    queue: list[tuple[str, int, list[str]]] = [(contract_name, 0, [contract_name])]
    while queue:
        name, depth, path = queue.pop(0)
        if depth >= max_depth:
            continue
        row = db.query_one("SELECT * FROM contracts WHERE name = ?", (name,))
        if not row or not row.get("current_version_id"):
            continue
        ver = db.query_one("SELECT * FROM contract_versions WHERE id = ?", (row["current_version_id"],))
        if not ver:
            continue
        content = db.loads(ver["content_json"], {})
        for dep in content.get("dependencies", []):
            dname = dep.get("name", "")
            if not dname or dname in visited:
                continue
            visited.add(dname)
            entry = {
                "name": dname,
                "team": dep.get("team", ""),
                "kind": dep.get("kind", "table"),
                "criticality": dep.get("criticality", "medium"),
                "depth": depth + 1,
                "path": path + [dname],
                "is_contract": db.query_one("SELECT id FROM contracts WHERE name = ?", (dname,)) is not None,
            }
            affected.append(entry)
            queue.append((dname, depth + 1, path + [dname]))
    return affected


def build_impact_report(contract_name: str, changes: list[dict]) -> dict:
    """破坏性变更 → 影响报告：受影响下游（含传递依赖）、风险等级、统计。"""
    breaking = [c for c in changes if c["severity"] == BREAKING]
    affected = _collect_affected(contract_name)
    high = [a for a in affected if a["criticality"] == "high"]
    risk = "high" if high else ("medium" if affected else "low")
    return {
        "contract": contract_name,
        "risk_level": risk,
        "risk_label": {"high": "高风险", "medium": "中风险", "low": "低风险"}[risk],
        "breaking_changes": breaking,
        "affected_downstreams": affected,
        "summary": {
            "breaking_count": len(breaking),
            "affected_count": len(affected),
            "direct_count": len([a for a in affected if a["depth"] == 1]),
            "transitive_count": len([a for a in affected if a["depth"] > 1]),
            "high_criticality_count": len(high),
            **summarize(changes),
        },
    }


_TASK_TEMPLATES = {
    "field_removed": ("停止读取并清理字段 {field} 的引用",
                      "字段 `{field}` 将被删除。请确认不再读取该字段，移除 SQL/代码中的引用，并验证任务通过。"),
    "type_narrowed": ("适配字段 {field} 类型收窄 {old} → {new}",
                      "字段 `{field}` 类型将由 `{old}` 收窄为 `{new}`。请转换存量数据、更新读取与解析逻辑。"),
    "type_incompatible": ("适配字段 {field} 类型变更 {old} → {new}",
                          "字段 `{field}` 类型将发生不兼容变更 `{old}` → `{new}`。请评估并改造读取链路。"),
    "partition_removed": ("调整分区逻辑：分区 {field} 将被删除",
                          "分区 `{field}` 将被删除。请调整分区裁剪、写入与回溯逻辑。"),
    "partition_changed": ("调整分区逻辑：分区 {field} 定义变更",
                          "分区 `{field}` 的定义将变更。请调整分区裁剪、写入与回溯逻辑。"),
}


def build_migration_tasks(changes: list[dict], affected: list[dict]) -> list[dict]:
    """按破坏性变更 × 受影响下游团队生成迁移任务。"""
    breaking = [c for c in changes if c["severity"] == BREAKING]
    teams = sorted({a["team"] for a in affected if a.get("team")}) or ["数据平台组"]
    tasks: list[dict] = []
    for c in breaking:
        field = c["path"].split(".")[1] if "." in c["path"] else c["path"]
        tmpl = _TASK_TEMPLATES.get(
            c["kind"],
            ("适配契约变更：" + c["message"], c["message"]),
        )
        old, new = c.get("old"), c.get("new")
        old_s = old if isinstance(old, str) else ""
        new_s = new if isinstance(new, str) else ""
        title = tmpl[0].format(field=field, old=old_s, new=new_s)
        desc = tmpl[1].format(field=field, old=old_s, new=new_s)
        for team in teams:
            tasks.append({
                "title": f"[{team}] {title}",
                "description": desc + f"\n\n关联变更：{c['message']}",
                "assignee_team": team,
                "source_change_key": c["id"],
            })
    if breaking:
        tasks.append({
            "title": "[数据平台组] 发布变更通知并约定迁移窗口",
            "description": "向所有受影响下游同步破坏性变更内容、影响报告与迁移截止时间。",
            "assignee_team": "数据平台组",
            "source_change_key": "meta:notify",
        })
    return tasks


def merge_policy(gate_status: str, approvals: list[dict], tasks: list[dict],
                 impact_report: dict | None) -> tuple[bool, list[str]]:
    """合并门禁判定。返回 (allowed, reasons)。"""
    if gate_status == "PASS":
        return True, ["兼容变更，允许自动合并"]

    approved = [a for a in approvals if a["decision"] == "approve"]
    rejected = [a for a in approvals if a["decision"] == "reject"]
    reasons: list[str] = []
    ok = True

    need = REQUIRED_APPROVALS_REVIEW if gate_status == "NEEDS_REVIEW" else REQUIRED_APPROVALS_BREAKING
    if len(approved) < need:
        ok = False
        reasons.append(f"批准数不足（{len(approved)}/{need}）")

    if gate_status == "BLOCKED":
        if not impact_report:
            ok = False
            reasons.append("缺少影响报告")
        pending = [t for t in tasks if t["status"] not in TASK_ACTIVE_STATES]
        if pending:
            ok = False
            reasons.append(f"还有 {len(pending)} 个迁移任务未认领或未完成")
        if not any(a["role"] in BREAKING_APPROVER_ROLES for a in approved):
            ok = False
            reasons.append("需要至少一名下游负责人或数据 steward 批准")

    if rejected:
        latest_reject = max(a["created_at"] for a in rejected)
        latest_approve = max((a["created_at"] for a in approved), default="")
        if latest_reject > latest_approve:
            ok = False
            reasons.append("存在最新的驳回记录，需重新获得批准")

    if ok:
        reasons.append("满足合并条件")
    return ok, reasons
