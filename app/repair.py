"""确定性改名修复：为安全但冲突的条目生成无冲突的 repaired_path（纯内存）。

规则：
- 同名碰撞：保留组内最先出现且不阻挡后代的条目，其余在末段扩展名前插入 " (n)"。
- 文件阻挡后代：重命名该文件（后代条目保持不动）。
- 每个候选名都按 Windows comparison_key 同时校验同名碰撞与双向层级冲突，
  n 从 1 递增，直至整批无冲突（改名按原始键深度递增顺序处理，保证终止）。
- 不安全条目不参与改名（skipped）。
"""

from __future__ import annotations

from .normalize import NormalizedEntry
from .schemas import (
    EntryType,
    RepairItem,
    RepairPlan,
    RepairReason,
    RepairStatus,
)


def _split_stem_ext(segment: str) -> tuple[str, str]:
    """把末段拆成 (词干, 扩展名)；扩展名含前导点，无扩展名或 dotfile 时为 ""。"""
    dot = segment.rfind(".")
    if dot <= 0:  # 无扩展名，或 .gitignore 这类 dotfile
        return segment, ""
    return segment[:dot], segment[dot:]


class _Occupied:
    """已占用的 comparison_key 集合，支持碰撞与双向层级冲突检查。

    同一键可被多条目占用（碰撞组），故按键计数，移除时仅减少一份。
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}  # comparison_key -> 占用条目数
        self._file_counts: dict[str, int] = {}  # comparison_key -> 其中文件条目数

    def add(self, key: str, entry_type: str) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1
        if entry_type == EntryType.FILE.value:
            self._file_counts[key] = self._file_counts.get(key, 0) + 1

    def remove(self, key: str, entry_type: str) -> None:
        if self._counts.get(key, 0) <= 1:
            self._counts.pop(key, None)
        else:
            self._counts[key] -= 1
        if entry_type == EntryType.FILE.value:
            remaining = self._file_counts.get(key, 0) - 1
            if remaining > 0:
                self._file_counts[key] = remaining
            else:
                self._file_counts.pop(key, None)

    def conflicts(self, key_parts: list[str], entry_type: str) -> bool:
        key = "/".join(key_parts)
        # 同名碰撞。
        if key in self._counts:
            return True
        # 祖先链上已有文件 → 被文件祖先阻挡。
        for depth in range(1, len(key_parts)):
            if "/".join(key_parts[:depth]) in self._file_counts:
                return True
        # 文件条目不得占据任何已占用键的祖先路径。
        if entry_type == EntryType.FILE.value:
            prefix = key + "/"
            if any(used.startswith(prefix) for used in self._counts):
                return True
        return False


def _remaining_conflicts(final: list[tuple[list[str], str]]) -> tuple[int, int]:
    """对修复后的最终键重新计数，返回 (碰撞组数, 层级冲突对数)。"""
    buckets: dict[str, int] = {}
    for key_parts, _ in final:
        key = "/".join(key_parts)
        buckets[key] = buckets.get(key, 0) + 1
    collisions = sum(1 for count in buckets.values() if count > 1)

    file_keys = {
        "/".join(key_parts)
        for key_parts, entry_type in final
        if entry_type == EntryType.FILE.value
    }
    hierarchy = 0
    for key_parts, _ in final:
        hierarchy += sum(
            1
            for depth in range(1, len(key_parts))
            if "/".join(key_parts[:depth]) in file_keys
        )
    return collisions, hierarchy


def build_repair_plan(
    entries: list[NormalizedEntry],
    index_to_group: dict[int, int],
    blocks: dict[int, list[int]],
) -> RepairPlan:
    """基于原始路径的分析结果生成确定性修复计划。

    entries 按下标升序（与输入一致）；index_to_group 与 blocks 分别来自原始
    路径的碰撞分组与层级冲突检测。修复过程不改动 entries 本身。
    """
    blocking = set(blocks)  # 阻挡后代的文件条目下标

    group_members: dict[int, list[NormalizedEntry]] = {}
    for entry in entries:
        group_no = index_to_group.get(entry.index)
        if group_no is not None:
            group_members.setdefault(group_no, []).append(entry)

    # 每条安全条目的改名原因（顺序：collision 先于 blocks_descendants）。
    reasons: dict[int, list[RepairReason]] = {}
    for members in group_members.values():
        # 保留组内最先出现且不阻挡后代的条目。
        keeper = next((m for m in members if m.index not in blocking), None)
        for member in members:
            if keeper is not None and member.index == keeper.index:
                continue
            reasons.setdefault(member.index, []).append(RepairReason.COLLISION)
    for index in blocking:
        reasons.setdefault(index, []).append(RepairReason.BLOCKS_DESCENDANTS)

    # 初始占用集 = 全部安全条目的原始键；逐条改名。
    occupied = _Occupied()
    for entry in entries:
        if not entry.unsafe and entry.comparison_key is not None:
            occupied.add(entry.comparison_key, entry.entry_type)

    # 处理顺序按 (原始键深度, 下标)：阻挡者的键深度必严格小于被阻挡者，
    # 保证文件祖先先移走，后续候选名的祖先链不会被待改名的文件占据，
    # 从而 n 递增必然在有限步内找到无冲突候选。
    to_rename = [e for e in entries if not e.unsafe and e.index in reasons]
    to_rename.sort(key=lambda e: (len(e.key_parts), e.index))

    final_key_parts: dict[int, list[str]] = {}
    repaired_paths: dict[int, str] = {}
    for entry in to_rename:
        occupied.remove(entry.comparison_key, entry.entry_type)
        stem, ext = _split_stem_ext(entry.canonical_parts[-1])
        n = 0
        while True:
            n += 1
            candidate_last = f"{stem} ({n}){ext}"
            candidate_keys = entry.key_parts[:-1] + [candidate_last.casefold()]
            if not occupied.conflicts(candidate_keys, entry.entry_type):
                break
        occupied.add("/".join(candidate_keys), entry.entry_type)
        final_key_parts[entry.index] = candidate_keys
        repaired_paths[entry.index] = "/".join(
            entry.canonical_parts[:-1] + [candidate_last]
        )

    # 修复后的最终键（未改名条目保持原键），据实重新计数两类冲突。
    final: list[tuple[list[str], str]] = [
        (final_key_parts.get(e.index, e.key_parts), e.entry_type)
        for e in entries
        if not e.unsafe
    ]
    remaining_collisions, remaining_hierarchy = _remaining_conflicts(final)

    items: list[RepairItem] = []
    for entry in entries:
        if entry.unsafe:
            items.append(
                RepairItem(
                    index=entry.index,
                    original_path=entry.original_path,
                    repaired_path=None,
                    status=RepairStatus.SKIPPED,
                )
            )
        elif entry.index in repaired_paths:
            items.append(
                RepairItem(
                    index=entry.index,
                    original_path=entry.original_path,
                    repaired_path=repaired_paths[entry.index],
                    status=RepairStatus.RENAMED,
                    reasons=reasons[entry.index],
                )
            )
        else:
            items.append(
                RepairItem(
                    index=entry.index,
                    original_path=entry.original_path,
                    repaired_path=entry.canonical_path,
                    status=RepairStatus.KEPT,
                )
            )

    return RepairPlan(
        items=items,
        renamed=len(repaired_paths),
        skipped=sum(1 for e in entries if e.unsafe),
        remaining_collisions=remaining_collisions,
        remaining_hierarchy_conflicts=remaining_hierarchy,
    )
