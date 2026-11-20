# 数据契约 CI 服务

把**字段、分区、质量规则、下游依赖**纳入变更检查的数据契约门禁服务。提交契约变更后自动完成
diff 分级、门禁判定、影响分析与迁移任务生成；兼容变更自动合并，破坏性变更必须走完
影响报告 + 迁移任务 + 多方审批才能合并。

## 快速开始

```bash
pip install fastapi "uvicorn[standard]" jinja2 python-multipart   # 或 pip install -r requirements.txt
./run.sh                        # http://localhost:8000，首次启动自动建库并写入演示数据
python3 -m pytest tests/ -q     # 运行测试（25 个用例）
```

页面：工作台 `/` · 变更请求 `/changes` · 变更详情 `/changes/{id}` · 质量规则集 `/rulesets` · 依赖图 `/graph`

## 变更分级

| 变更 | 级别 | 说明 |
|---|---|---|
| 新增可空/有默认值字段、类型拓宽（int→bigint、decimal 扩精度、date→timestamp 等）、空值约束放宽、描述更新、新增下游 | **INFO 兼容** | 门禁通过，**自动合并**并发布新版本 |
| 新增非空字段、空值约束收紧、**语义标记变化**（如增删 `pii`）、新增分区、新增/移除/收紧质量规则、移除下游依赖 | **WARNING 需审批** | ≥1 个批准方可合并 |
| **删除字段**、**类型收窄**（bigint→int、decimal 降精度、string→int 等）、跨类型族变更、删除/修改分区 | **BREAKING 破坏性** | 门禁阻塞：自动生成影响报告 + 迁移任务，满足合并策略才放行 |

类型拓宽/收窄由类型格（数值链、字符串链、参数化类型 `decimal(p,s)`/`varchar(n)`、date↔timestamp）判定，见 `app/diff_engine.py`。

## 门禁与合并策略（`app/gate.py`）

- **PASS**：全部兼容 → 创建即自动合并，发布新契约版本。
- **NEEDS_REVIEW**：含需审批变更 → ≥1 个批准；最新结论为驳回时阻塞。
- **BLOCKED**：含破坏性变更 → 必须同时满足：
  1. 影响报告已生成（创建时自动生成，含直接 + 传递下游、风险等级）；
  2. 迁移任务全部进入「进行中/已完成」；
  3. ≥2 个批准，且至少一名**下游负责人或数据 Steward**。

## 影响报告与迁移任务

破坏性变更提交时自动：

- 沿下游依赖图 BFS（下游名字与系统内契约同名时继续传递展开），产出受影响下游清单、
  依赖路径、风险等级（存在高关键级别下游 → 高风险）；
- 按「破坏性变更 × 受影响团队」生成迁移任务（停止读取被删字段、适配类型收窄、调整分区逻辑等），
  外加一条变更通知任务。

## 质量规则版本与回退

规则集（`/rulesets`）按版本管理，**回退 = 以目标版本内容生成新的生效版本**，历史版本全部保留，
时间线上标注「回退自 vN」。API：`POST /api/rulesets/{name}/rollback`。

## API 摘要

```
POST   /api/contracts                        注册契约（含首版本）
GET    /api/contracts | /api/contracts/{name} | /api/contracts/{name}/versions
POST   /api/contracts/{name}/changes         提交变更（自动分级/门禁/自动合并或生成影响报告）
GET    /api/changes | /api/changes/{id}      变更详情（diff、门禁、影响报告、任务、审批）
POST   /api/changes/{id}/approvals           审批（approve/reject，角色：owner/steward/downstream_owner）
POST   /api/changes/{id}/merge | /close      合并（校验门禁策略）/ 关闭
PATCH  /api/tasks/{id}                       迁移任务状态流转
GET    /api/rulesets | POST /api/rulesets    规则集版本
POST   /api/rulesets/{name}/rollback         回退到指定版本
GET    /api/graph                            全局依赖图数据
```

契约内容 JSON 结构：`{fields: [{name, type, nullable, semantic_tags, description, default}],
partitions: [{name, type, granularity}], quality_rules: [{id, name, rule_type, field, params, severity}],
dependencies: [{name, team, kind, criticality}]}`。

## 代码结构

```
app/
  diff_engine.py   变更 diff 与三级分类（类型拓宽/收窄格）
  gate.py          门禁判定、影响报告（BFS 传递依赖）、迁移任务模板、合并策略
  service.py       业务层：契约/变更/审批/合并/规则集回退/依赖图
  main.py          FastAPI 路由（API + 页面）
  db.py            SQLite 存储层（CONTRACT_CI_DB 环境变量可指定库文件）
  seed.py          演示数据（4 个契约、规则集含一次回退、三种门禁级别各一个变更）
  templates/       页面：工作台、契约详情、变更详情（diff/影响图/任务/审批）、规则集、依赖图
  static/graph.js  依赖图渲染（分层布局 SVG，色板经 CVD 校验）
tests/             25 个单元 + 端到端测试
```
