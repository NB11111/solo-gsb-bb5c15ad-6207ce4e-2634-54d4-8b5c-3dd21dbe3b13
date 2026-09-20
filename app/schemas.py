"""请求/响应模型（Pydantic v2）。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .normalize import IssueCode, IssueSeverity


class EntryType(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"


class PreflightEntryIn(BaseModel):
    path: str = Field(..., description="待解包条目的原始路径")
    type: EntryType = Field(..., description="条目类型：file 或 directory")


class PreflightRequest(BaseModel):
    entries: list[PreflightEntryIn] = Field(
        ..., min_length=1, description="待预检的条目列表（至少 1 条）"
    )
    repair: bool = Field(
        False,
        description=(
            "为 true 时额外返回 repair_plan：对安全但存在同名碰撞或文件祖先冲突的"
            "条目生成确定性的修复路径；不安全条目跳过不改名"
        ),
    )


class IssueOut(BaseModel):
    code: IssueCode
    severity: IssueSeverity
    message: str
    segment: int | None = None
    related_index: int | None = None


class CollisionMember(BaseModel):
    index: int
    canonical_path: str


class CollisionGroup(BaseModel):
    group: int = Field(..., description="碰撞组编号（按首次出现顺序分配）")
    canonical_path: str = Field(..., description="该组对应的规范路径（展示用）")
    members: list[CollisionMember]


class HierarchyConflict(BaseModel):
    file_index: int = Field(..., description="作为祖先路径的文件条目下标")
    file_canonical_path: str
    descendant_index: int = Field(..., description="被阻挡的后代条目下标")
    descendant_canonical_path: str


class PreflightEntryOut(BaseModel):
    index: int = Field(..., description="条目下标（从 1 开始，与输入顺序一致）")
    type: EntryType
    original_path: str
    canonical_path: str = Field(..., description="规范化路径（NFC、段尾已裁剪，正斜杠分隔）")
    comparison_key: str | None = Field(
        ..., description="Windows 等价比较键（小写折叠，仅安全条目可用）"
    )
    safe: bool
    collision_group: int | None = Field(
        ..., description="所属同名碰撞组编号；无碰撞或不安全时为 null"
    )
    blocked_by_file_ancestors: list[int] = Field(
        default_factory=list, description="阻挡本条目的文件祖先下标列表"
    )
    blocks_descendants: list[int] = Field(
        default_factory=list, description="被本文件条目阻挡的后代下标列表（仅文件条目）"
    )
    issues: list[IssueOut] = Field(default_factory=list)


class RepairReason(str, Enum):
    COLLISION = "collision"
    BLOCKS_DESCENDANTS = "blocks_descendants"


class RepairStatus(str, Enum):
    KEPT = "kept"
    RENAMED = "renamed"
    SKIPPED = "skipped"


class RepairItem(BaseModel):
    index: int = Field(..., description="条目下标（从 1 开始，与输入顺序一致）")
    original_path: str
    repaired_path: str | None = Field(
        ..., description="修复后的最终规范路径；不安全（skipped）条目为 null"
    )
    status: RepairStatus = Field(
        ...,
        description="kept=保留原名，renamed=已改名，skipped=不安全条目跳过不改名",
    )
    reasons: list[RepairReason] = Field(
        default_factory=list, description="改名原因（仅 renamed 条目非空）"
    )


class RepairPlan(BaseModel):
    items: list[RepairItem] = Field(..., description="按输入顺序逐条对应全部条目")
    renamed: int = Field(..., description="被改名的条目数")
    skipped: int = Field(..., description="不安全而被跳过的条目数")
    remaining_collisions: int = Field(..., description="修复后仍存在的同名碰撞组数")
    remaining_hierarchy_conflicts: int = Field(
        ..., description="修复后仍存在的文件-目录层级冲突对数"
    )


class Summary(BaseModel):
    total: int
    safe: int
    unsafe: int
    collisions: int = Field(..., description="同名碰撞组数量")
    collision_entries: int
    hierarchy_conflicts: int


class PreflightResponse(BaseModel):
    target: str = "windows"
    entries: list[PreflightEntryOut]
    collision_groups: list[CollisionGroup]
    hierarchy_conflicts: list[HierarchyConflict]
    summary: Summary
    repair_plan: RepairPlan | None = Field(
        None, description="修复计划；仅当请求 repair=true 时返回，否则为 null"
    )


class HealthResponse(BaseModel):
    status: str
    service: str
