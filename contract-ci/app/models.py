"""请求体模型。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class FieldDef(BaseModel):
    name: str
    type: str = "string"
    nullable: bool = True
    semantic_tags: list[str] = Field(default_factory=list)
    description: str = ""
    default: Optional[Any] = None


class PartitionDef(BaseModel):
    name: str
    type: str = "string"
    granularity: str = "day"  # hour | day | month


class QualityRuleDef(BaseModel):
    id: Optional[str] = None
    name: str
    rule_type: str = "custom"  # not_null | unique | range | freshness | volume | regex | custom
    field: Optional[str] = None
    params: dict = Field(default_factory=dict)
    severity: str = "blocking"  # blocking | warn


class DependencyDef(BaseModel):
    name: str
    team: str = ""
    kind: str = "table"  # table | dashboard | job | api
    criticality: str = "medium"  # low | medium | high


class ContractContent(BaseModel):
    fields: list[FieldDef] = Field(default_factory=list)
    partitions: list[PartitionDef] = Field(default_factory=list)
    quality_rules: list[QualityRuleDef] = Field(default_factory=list)
    dependencies: list[DependencyDef] = Field(default_factory=list)


class CreateContractRequest(BaseModel):
    name: str
    owner: str = ""
    description: str = ""
    content: ContractContent = Field(default_factory=ContractContent)


class CreateChangeRequest(BaseModel):
    title: str = ""
    author: str = ""
    content: ContractContent


class ApprovalRequest(BaseModel):
    approver: str
    role: str = "owner"  # owner | steward | downstream_owner
    decision: str = "approve"  # approve | reject
    comment: str = ""


class TaskUpdateRequest(BaseModel):
    status: str  # todo | in_progress | done


class CreateRulesetRequest(BaseModel):
    name: str
    rules: list[dict] = Field(default_factory=list)
    created_by: str = ""
    note: str = ""


class RollbackRequest(BaseModel):
    target_version_id: int
    actor: str = ""
    note: str = ""
