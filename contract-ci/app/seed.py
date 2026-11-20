"""演示数据：契约、规则集（含一次回退）、三种门禁级别的变更请求。"""
from __future__ import annotations

from . import db, service

ORDERS_V1 = {
    "fields": [
        {"name": "order_id", "type": "bigint", "nullable": False,
         "semantic_tags": ["bi_key"], "description": "订单主键"},
        {"name": "user_id", "type": "bigint", "nullable": False,
         "semantic_tags": ["dimension"], "description": "用户 ID"},
        {"name": "amount", "type": "decimal(10,2)", "nullable": False,
         "semantic_tags": ["metric"], "description": "订单金额（元）"},
        {"name": "status", "type": "string", "nullable": False,
         "semantic_tags": ["dimension"], "description": "订单状态"},
        {"name": "buyer_phone", "type": "string", "nullable": True,
         "semantic_tags": ["pii", "sensitive"], "description": "买家手机号"},
        {"name": "created_at", "type": "timestamp", "nullable": False,
         "semantic_tags": [], "description": "下单时间"},
    ],
    "partitions": [
        {"name": "dt", "type": "string", "granularity": "day"},
    ],
    "quality_rules": [
        {"id": "q_order_id_nn", "name": "order_id 非空", "rule_type": "not_null",
         "field": "order_id", "params": {}, "severity": "blocking"},
        {"id": "q_amount_range", "name": "amount 非负", "rule_type": "range",
         "field": "amount", "params": {"min": 0}, "severity": "blocking"},
        {"id": "q_freshness", "name": "分区时效", "rule_type": "freshness",
         "field": None, "params": {"max_delay_hours": 3}, "severity": "warn"},
    ],
    "dependencies": [
        {"name": "dw.order_wide", "team": "数据平台组", "kind": "table", "criticality": "high"},
        {"name": "bi.daily_revenue", "team": "BI 组", "kind": "dashboard", "criticality": "high"},
        {"name": "recsys.feature_job", "team": "推荐组", "kind": "job", "criticality": "medium"},
    ],
}

USERS_V1 = {
    "fields": [
        {"name": "user_id", "type": "bigint", "nullable": False,
         "semantic_tags": ["bi_key"], "description": "用户主键"},
        {"name": "nickname", "type": "string", "nullable": True,
         "semantic_tags": [], "description": "昵称"},
        {"name": "id_card", "type": "string", "nullable": True,
         "semantic_tags": ["pii", "sensitive"], "description": "身份证号"},
        {"name": "city", "type": "string", "nullable": True,
         "semantic_tags": ["dimension"], "description": "城市"},
    ],
    "partitions": [{"name": "dt", "type": "string", "granularity": "day"}],
    "quality_rules": [
        {"id": "q_uid_unique", "name": "user_id 唯一", "rule_type": "unique",
         "field": "user_id", "params": {}, "severity": "blocking"},
    ],
    "dependencies": [
        {"name": "dw.order_wide", "team": "数据平台组", "kind": "table", "criticality": "high"},
    ],
}

ORDER_WIDE_V1 = {
    "fields": [
        {"name": "order_id", "type": "bigint", "nullable": False,
         "semantic_tags": ["bi_key"], "description": "订单主键"},
        {"name": "user_id", "type": "bigint", "nullable": False,
         "semantic_tags": ["dimension"], "description": "用户 ID"},
        {"name": "amount", "type": "decimal(10,2)", "nullable": True,
         "semantic_tags": ["metric"], "description": "订单金额"},
        {"name": "city", "type": "string", "nullable": True,
         "semantic_tags": ["dimension"], "description": "城市"},
    ],
    "partitions": [{"name": "dt", "type": "string", "granularity": "day"}],
    "quality_rules": [
        {"id": "q_ow_fresh", "name": "宽表时效", "rule_type": "freshness",
         "field": None, "params": {"max_delay_hours": 6}, "severity": "warn"},
    ],
    "dependencies": [
        {"name": "ml.risk_model", "team": "风控组", "kind": "job", "criticality": "high"},
        {"name": "bi.ops_board", "team": "BI 组", "kind": "dashboard", "criticality": "medium"},
    ],
}

PAYMENTS_V1 = {
    "fields": [
        {"name": "payment_id", "type": "bigint", "nullable": False,
         "semantic_tags": ["bi_key"], "description": "支付单号"},
        {"name": "order_id", "type": "bigint", "nullable": False,
         "semantic_tags": ["dimension"], "description": "关联订单"},
        {"name": "pay_amount", "type": "double", "nullable": False,
         "semantic_tags": ["metric"], "description": "支付金额"},
    ],
    "partitions": [{"name": "dt", "type": "string", "granularity": "day"}],
    "quality_rules": [],
    "dependencies": [
        {"name": "fin.settlement", "team": "财务组", "kind": "job", "criticality": "high"},
    ],
}

RULESET_V1 = [
    {"id": "pk_not_null", "name": "主键非空", "rule_type": "not_null",
     "params": {"target": "primary_key"}, "severity": "blocking"},
    {"id": "freshness_24h", "name": "24 小时时效", "rule_type": "freshness",
     "params": {"max_delay_hours": 24}, "severity": "warn"},
]
RULESET_V2 = RULESET_V1 + [
    {"id": "pk_unique", "name": "主键唯一", "rule_type": "unique",
     "params": {"target": "primary_key"}, "severity": "blocking"},
]
RULESET_V3 = RULESET_V2 + [
    {"id": "volume_guard", "name": "行数波动告警", "rule_type": "volume",
     "params": {"max_change_pct": 20}, "severity": "warn"},
]


def seed_if_empty() -> None:
    if db.query_one("SELECT id FROM contracts LIMIT 1"):
        return

    # 契约
    service.create_contract("dw.orders", "交易数据组", "订单事实表（每日快照）", ORDERS_V1)
    service.create_contract("dw.users", "用户数据组", "用户维表", USERS_V1)
    service.create_contract("dw.order_wide", "数据平台组", "订单宽表（依赖 orders/users）", ORDER_WIDE_V1)
    service.create_contract("dw.payments", "支付数据组", "支付流水表", PAYMENTS_V1)

    # 规则集：v1 → v2 → v3 → v4（回退到 v2）
    service.create_ruleset("default-quality", RULESET_V1, "数据平台组", "初始版本")
    service.create_ruleset("default-quality", RULESET_V2, "数据平台组", "新增主键唯一约束")
    service.create_ruleset("default-quality", RULESET_V3, "数据平台组", "新增行数波动告警")
    v3 = db.query_one("SELECT id FROM rulesets WHERE name='default-quality' AND version=2")
    service.rollback_ruleset("default-quality", v3["id"], "数据平台组",
                             "行数波动告警误报过多，回退到 v2")

    # 1) 兼容变更 → 自动合并
    users_compat = dict(USERS_V1)
    users_compat["fields"] = USERS_V1["fields"] + [
        {"name": "register_channel", "type": "string", "nullable": True,
         "semantic_tags": ["dimension"], "description": "注册渠道"},
    ]
    service.submit_change("dw.users", "新增注册渠道字段", "用户数据组-李雷", users_compat)

    # 2) 需审批变更（语义标记 + 新增非空字段 + 新增质量规则）
    payments_review = dict(PAYMENTS_V1)
    payments_review["fields"] = [
        *PAYMENTS_V1["fields"][:2],
        {"name": "pay_amount", "type": "double", "nullable": False,
         "semantic_tags": ["metric", "financial"], "description": "支付金额"},
        {"name": "payer_account", "type": "string", "nullable": False,
         "semantic_tags": ["pii"], "description": "付款方账号"},
    ]
    payments_review["quality_rules"] = [
        {"id": "q_pay_nn", "name": "pay_amount 非空", "rule_type": "not_null",
         "field": "pay_amount", "params": {}, "severity": "blocking"},
    ]
    service.submit_change("dw.payments", "支付表补充付款方账号与金额语义标记", "支付数据组-韩梅",
                          payments_review)

    # 3) 破坏性变更 → 阻塞 + 影响报告 + 迁移任务
    orders_breaking = dict(ORDERS_V1)
    orders_breaking["fields"] = [
        f for f in ORDERS_V1["fields"] if f["name"] != "buyer_phone"
    ]
    orders_breaking["fields"] = [
        {**f, "type": "decimal(8,2)"} if f["name"] == "amount" else f
        for f in orders_breaking["fields"]
    ]
    service.submit_change("dw.orders", "下线买家手机号字段并收窄金额精度", "交易数据组-王凯",
                          orders_breaking)
