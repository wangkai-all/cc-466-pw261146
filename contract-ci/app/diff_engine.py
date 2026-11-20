"""变更分级引擎：对字段、分区、质量规则、下游依赖做 diff，并按
INFO（兼容）/ WARNING（需审批）/ BREAKING（破坏性）三级分类。"""
from __future__ import annotations

INFO = "INFO"          # 兼容变更，可自动合并
WARNING = "WARNING"    # 需人工审批
BREAKING = "BREAKING"  # 破坏性变更，阻塞合并，需影响报告 + 迁移任务

SEVERITY_ORDER = {INFO: 0, WARNING: 1, BREAKING: 2}
SEVERITY_LABEL = {INFO: "兼容", WARNING: "需审批", BREAKING: "破坏性"}

CATEGORY_LABEL = {
    "field": "字段",
    "partition": "分区",
    "quality_rule": "质量规则",
    "dependency": "下游依赖",
}

# 类型拓宽/收窄格
NUMERIC_RANK = {
    "tinyint": 1, "smallint": 2, "int": 3, "integer": 3,
    "bigint": 4, "long": 4, "float": 5, "double": 6, "decimal": 7,
}
STRING_RANK = {"char": 1, "varchar": 2, "string": 3, "text": 3}


def parse_type(t: str) -> tuple[str, tuple[int, ...]]:
    """'decimal(10,2)' -> ('decimal', (10, 2)); 'varchar(50)' -> ('varchar', (50,))"""
    t = (t or "").strip().lower()
    if "(" in t and t.endswith(")"):
        base = t[: t.index("(")].strip()
        try:
            params = tuple(
                int(p.strip()) for p in t[t.index("(") + 1 : -1].split(",") if p.strip()
            )
        except ValueError:
            params = ()
        return base, params
    return t, ()


def compare_types(old: str, new: str) -> tuple[str, str]:
    """返回 (relation, note)，relation ∈ same | widening | narrowing | incompatible。"""
    ob, op = parse_type(old)
    nb, np_ = parse_type(new)
    if ob == nb:
        if op == np_:
            return "same", ""
        if ob == "decimal" and len(op) == 2 and len(np_) == 2:
            if np_[0] >= op[0] and np_[1] >= op[1]:
                return "widening", "精度/标度扩大"
            if np_[0] <= op[0] and np_[1] <= op[1]:
                return "narrowing", "精度/标度收窄"
            return "incompatible", f"decimal 参数交叉变化 {op}→{np_}"
        if ob in ("varchar", "char") and len(op) == 1 and len(np_) == 1:
            if np_[0] > op[0]:
                return "widening", "长度扩大"
            if np_[0] < op[0]:
                return "narrowing", "长度收窄"
            return "same", ""
        return "incompatible", f"类型参数变化 {op}→{np_}"
    if ob in NUMERIC_RANK and nb in NUMERIC_RANK:
        rel = "widening" if NUMERIC_RANK[nb] > NUMERIC_RANK[ob] else "narrowing"
        return rel, "数值类型"
    if ob in STRING_RANK and nb in STRING_RANK:
        rel = "widening" if STRING_RANK[nb] > STRING_RANK[ob] else "narrowing"
        return rel, "字符串类型"
    if (ob, nb) == ("date", "timestamp"):
        return "widening", "日期→时间戳"
    if (ob, nb) == ("timestamp", "date"):
        return "narrowing", "时间戳→日期（丢失时分秒）"
    if ob in NUMERIC_RANK and nb in STRING_RANK:
        return "widening", "数值→字符串"
    if ob in STRING_RANK and nb in NUMERIC_RANK:
        return "narrowing", "字符串→数值"
    return "incompatible", f"跨类型族 {ob}→{nb}"


def _chg(category, kind, severity, path, message, old=None, new=None):
    return {
        "id": f"{category}:{kind}:{path}",
        "category": category,
        "category_label": CATEGORY_LABEL[category],
        "kind": kind,
        "severity": severity,
        "severity_label": SEVERITY_LABEL[severity],
        "path": path,
        "message": message,
        "old": old,
        "new": new,
    }


def diff_fields(old: list[dict], new: list[dict]) -> list[dict]:
    changes = []
    oldm = {f["name"]: f for f in old}
    newm = {f["name"]: f for f in new}

    for name, f in oldm.items():
        if name not in newm:
            changes.append(_chg(
                "field", "field_removed", BREAKING, f"fields.{name}",
                f"删除字段 `{name}`（{f.get('type', '?')}），下游读取将失败",
                old={"type": f.get("type"), "nullable": f.get("nullable", True)},
            ))

    for name, f in newm.items():
        if name not in oldm:
            nullable = f.get("nullable", True)
            has_default = f.get("default") is not None
            if nullable or has_default:
                changes.append(_chg(
                    "field", "field_added", INFO, f"fields.{name}",
                    f"新增可空字段 `{name}`（{f.get('type', '?')}）",
                    new={"type": f.get("type"), "nullable": nullable},
                ))
            else:
                changes.append(_chg(
                    "field", "field_added_required", WARNING, f"fields.{name}",
                    f"新增非空字段 `{name}`（{f.get('type', '?')}），写入方必须保证提供",
                    new={"type": f.get("type"), "nullable": False},
                ))

    for name in oldm.keys() & newm.keys():
        o, n = oldm[name], newm[name]
        ot, nt = o.get("type", ""), n.get("type", "")
        if ot.strip().lower() != nt.strip().lower():
            rel, note = compare_types(ot, nt)
            path = f"fields.{name}.type"
            if rel == "widening":
                changes.append(_chg("field", "type_widened", INFO, path,
                                    f"字段 `{name}` 类型拓宽 {ot} → {nt}（{note}）", ot, nt))
            elif rel == "narrowing":
                changes.append(_chg("field", "type_narrowed", BREAKING, path,
                                    f"字段 `{name}` 类型收窄 {ot} → {nt}（{note}）", ot, nt))
            else:
                changes.append(_chg("field", "type_incompatible", BREAKING, path,
                                    f"字段 `{name}` 类型不兼容变更 {ot} → {nt}（{note}）", ot, nt))

        on, nn = bool(o.get("nullable", True)), bool(n.get("nullable", True))
        if on != nn:
            if nn:
                changes.append(_chg("field", "nullable_relaxed", INFO, f"fields.{name}.nullable",
                                    f"字段 `{name}` 空值约束放宽：非空 → 可空", "not null", "nullable"))
            else:
                changes.append(_chg("field", "nullable_tightened", WARNING, f"fields.{name}.nullable",
                                    f"字段 `{name}` 空值约束收紧：可空 → 非空，写入方需保证无空值",
                                    "nullable", "not null"))

        otags, ntags = set(o.get("semantic_tags") or []), set(n.get("semantic_tags") or [])
        if otags != ntags:
            added, removed = sorted(ntags - otags), sorted(otags - ntags)
            parts = []
            if added:
                parts.append("新增 " + ", ".join(f"`{t}`" for t in added))
            if removed:
                parts.append("移除 " + ", ".join(f"`{t}`" for t in removed))
            extra = ""
            if "pii" in added:
                extra = "；标记为 PII，需确认下游脱敏与权限"
            elif "pii" in removed:
                extra = "；移除 PII 标记，需合规确认"
            changes.append(_chg(
                "field", "semantic_tags_changed", WARNING, f"fields.{name}.semantic_tags",
                f"字段 `{name}` 语义标记变化：{'，'.join(parts)}{extra}",
                sorted(otags), sorted(ntags),
            ))

        if (o.get("description") or "") != (n.get("description") or ""):
            changes.append(_chg("field", "description_changed", INFO, f"fields.{name}.description",
                                f"字段 `{name}` 描述更新",
                                o.get("description") or "", n.get("description") or ""))
    return changes


def diff_partitions(old: list[dict], new: list[dict]) -> list[dict]:
    changes = []
    oldm = {p["name"]: p for p in old}
    newm = {p["name"]: p for p in new}
    for name, p in oldm.items():
        if name not in newm:
            changes.append(_chg("partition", "partition_removed", BREAKING, f"partitions.{name}",
                                f"删除分区 `{name}`，下游分区裁剪逻辑将失效",
                                old={"type": p.get("type"), "granularity": p.get("granularity")}))
    for name, p in newm.items():
        if name not in oldm:
            changes.append(_chg("partition", "partition_added", WARNING, f"partitions.{name}",
                                f"新增分区 `{name}`（{p.get('granularity', 'day')}），下游查询需适配新分区",
                                new={"type": p.get("type"), "granularity": p.get("granularity")}))
    for name in oldm.keys() & newm.keys():
        o, n = oldm[name], newm[name]
        diffs = {}
        for k in ("type", "granularity"):
            if (o.get(k) or "") != (n.get(k) or ""):
                diffs[k] = [o.get(k), n.get(k)]
        if diffs:
            changes.append(_chg("partition", "partition_changed", BREAKING, f"partitions.{name}",
                                f"分区 `{name}` 定义变更：" + "，".join(
                                    f"{k} {v[0]}→{v[1]}" for k, v in diffs.items()),
                                old={k: o.get(k) for k in ("type", "granularity")},
                                new={k: n.get(k) for k in ("type", "granularity")}))
    return changes


_RULE_SEVERITY_RANK = {"warn": 1, "blocking": 2}


def _rule_key(r: dict) -> str:
    return r.get("id") or r.get("name") or ""


def diff_quality_rules(old: list[dict], new: list[dict]) -> list[dict]:
    changes = []
    oldm = {_rule_key(r): r for r in old}
    newm = {_rule_key(r): r for r in new}
    for key, r in oldm.items():
        if key not in newm:
            changes.append(_chg("quality_rule", "rule_removed", WARNING, f"quality_rules.{key}",
                                f"移除质量规则「{r.get('name', key)}」，质量保障减弱",
                                old={"rule_type": r.get("rule_type"), "params": r.get("params", {})}))
    for key, r in newm.items():
        if key not in oldm:
            changes.append(_chg("quality_rule", "rule_added", WARNING, f"quality_rules.{key}",
                                f"新增质量规则「{r.get('name', key)}」（{r.get('rule_type')}，{r.get('severity', 'blocking')}）",
                                new={"rule_type": r.get("rule_type"), "params": r.get("params", {})}))
    for key in oldm.keys() & newm.keys():
        o, n = oldm[key], newm[key]
        path = f"quality_rules.{key}"
        os_, ns_ = o.get("severity", "blocking"), n.get("severity", "blocking")
        if os_ != ns_:
            if _RULE_SEVERITY_RANK.get(ns_, 0) > _RULE_SEVERITY_RANK.get(os_, 0):
                changes.append(_chg("quality_rule", "rule_severity_raised", WARNING, path,
                                    f"规则「{n.get('name', key)}」级别收紧 {os_} → {ns_}", os_, ns_))
            else:
                changes.append(_chg("quality_rule", "rule_severity_lowered", INFO, path,
                                    f"规则「{n.get('name', key)}」级别放宽 {os_} → {ns_}", os_, ns_))
        if (o.get("rule_type") or "") != (n.get("rule_type") or ""):
            changes.append(_chg("quality_rule", "rule_type_changed", WARNING, path,
                                f"规则「{n.get('name', key)}」类型变更 {o.get('rule_type')} → {n.get('rule_type')}",
                                o.get("rule_type"), n.get("rule_type")))
        elif (o.get("params") or {}) != (n.get("params") or {}):
            changes.append(_chg("quality_rule", "rule_params_changed", WARNING, path,
                                f"规则「{n.get('name', key)}」参数调整",
                                o.get("params") or {}, n.get("params") or {}))
        if (o.get("field") or None) != (n.get("field") or None):
            changes.append(_chg("quality_rule", "rule_field_changed", WARNING, path,
                                f"规则「{n.get('name', key)}」作用字段变更 {o.get('field')} → {n.get('field')}",
                                o.get("field"), n.get("field")))
    return changes


def diff_dependencies(old: list[dict], new: list[dict]) -> list[dict]:
    changes = []
    oldm = {d["name"]: d for d in old}
    newm = {d["name"]: d for d in new}
    for name, d in oldm.items():
        if name not in newm:
            changes.append(_chg("dependency", "dependency_removed", WARNING, f"dependencies.{name}",
                                f"移除下游依赖 `{name}`，请确认该下游已下线或迁移",
                                old={"team": d.get("team"), "criticality": d.get("criticality")}))
    for name, d in newm.items():
        if name not in oldm:
            changes.append(_chg("dependency", "dependency_added", INFO, f"dependencies.{name}",
                                f"新增下游依赖 `{name}`（{d.get('team', '未知团队')}，{d.get('criticality', 'medium')}）",
                                new={"team": d.get("team"), "criticality": d.get("criticality")}))
    for name in oldm.keys() & newm.keys():
        o, n = oldm[name], newm[name]
        if (o.get("criticality") or "") != (n.get("criticality") or ""):
            changes.append(_chg("dependency", "dependency_criticality_changed", INFO,
                                f"dependencies.{name}",
                                f"下游 `{name}` 关键级别变更 {o.get('criticality')} → {n.get('criticality')}",
                                o.get("criticality"), n.get("criticality")))
    return changes


def diff_contracts(old: dict, new: dict) -> list[dict]:
    """old/new 均为 ContractContent 形态的 dict。结果按严重级别降序排列。"""
    changes = []
    changes += diff_fields(old.get("fields", []), new.get("fields", []))
    changes += diff_partitions(old.get("partitions", []), new.get("partitions", []))
    changes += diff_quality_rules(old.get("quality_rules", []), new.get("quality_rules", []))
    changes += diff_dependencies(old.get("dependencies", []), new.get("dependencies", []))
    changes.sort(key=lambda c: -SEVERITY_ORDER[c["severity"]])
    return changes


def summarize(changes: list[dict]) -> dict:
    by_sev = {INFO: 0, WARNING: 0, BREAKING: 0}
    by_cat = {k: 0 for k in CATEGORY_LABEL}
    for c in changes:
        by_sev[c["severity"]] += 1
        by_cat[c["category"]] += 1
    return {"total": len(changes), "by_severity": by_sev, "by_category": by_cat}
