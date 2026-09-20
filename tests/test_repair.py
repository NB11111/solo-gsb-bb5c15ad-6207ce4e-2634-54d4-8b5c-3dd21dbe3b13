"""repair=true 修复计划的测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _call(entries, **extra):
    body = {"entries": entries}
    body.update(extra)
    return client.post("/api/v1/preflight", json=body)


def _items(resp):
    return {i["index"]: i for i in resp["repair_plan"]["items"]}


def test_repair_omitted_keeps_response_compatible():
    resp = _call(
        [
            {"path": "docs/readme.txt", "type": "file"},
            {"path": "docs/README.TXT", "type": "file"},
        ]
    ).json()
    assert resp["repair_plan"] is None
    # 原有字段不受影响。
    assert resp["summary"]["collisions"] == 1
    assert resp["entries"][1]["collision_group"] == 1


def test_repair_false_explicit_keeps_response_compatible():
    resp = _call(
        [{"path": "a.txt", "type": "file"}],
        repair=False,
    ).json()
    assert resp["repair_plan"] is None


def test_repair_collision_basic():
    resp = _call(
        [
            {"path": "docs/readme.txt", "type": "file"},
            {"path": "docs/README.TXT", "type": "file"},
            {"path": "docs/readme.txt ", "type": "file"},  # 段尾裁剪后同键
            {"path": "docs/other.md", "type": "file"},
        ],
        repair=True,
    ).json()
    plan = resp["repair_plan"]
    items = _items(resp)

    # 最先出现的保留，其余在扩展名前插入 " (n)"（保留各自原始大小写）。
    assert items[1]["status"] == "kept"
    assert items[1]["repaired_path"] == "docs/readme.txt"
    assert items[1]["reasons"] == []
    assert items[2]["status"] == "renamed"
    assert items[2]["repaired_path"] == "docs/README (1).TXT"
    assert items[2]["reasons"] == ["collision"]
    assert items[3]["repaired_path"] == "docs/readme (2).txt"
    assert items[4]["status"] == "kept"

    assert plan["renamed"] == 2
    assert plan["skipped"] == 0
    assert plan["remaining_collisions"] == 0
    assert plan["remaining_hierarchy_conflicts"] == 0
    # 修复计划按输入顺序覆盖全部条目。
    assert [i["index"] for i in plan["items"]] == [1, 2, 3, 4]


def test_repair_collision_candidate_increment_until_free():
    # 候选名 a (1).txt 已被占用（含大小写变体），须递增 n。
    resp = _call(
        [
            {"path": "a.txt", "type": "file"},
            {"path": "A.TXT", "type": "file"},
            {"path": "a (1).txt", "type": "file"},
            {"path": "A (1).TXT", "type": "file"},
        ],
        repair=True,
    ).json()
    items = _items(resp)
    assert items[1]["repaired_path"] == "a.txt"
    # a (1).txt 被占用 → 递增为 A (2).TXT。
    assert items[2]["repaired_path"] == "A (2).TXT"
    assert items[3]["repaired_path"] == "a (1).txt"
    # 在末段扩展名前机械插入：原名已含 (1) 时得到 (1) (1)。
    assert items[4]["repaired_path"] == "A (1) (1).TXT"
    assert resp["repair_plan"]["remaining_collisions"] == 0


def test_repair_shared_key_occupancy_kept_after_group_member_renamed():
    # 回归：同键组成员被改名移出后，保留者的占用不得丢失。
    resp = _call(
        [
            {"path": "a (1).txt", "type": "file"},
            {"path": "A (1).TXT", "type": "file"},
            {"path": "a.txt", "type": "file"},
            {"path": "A.TXT", "type": "file"},
        ],
        repair=True,
    ).json()
    items = _items(resp)
    assert items[1]["repaired_path"] == "a (1).txt"
    assert items[2]["repaired_path"] == "A (1) (1).TXT"
    assert items[3]["repaired_path"] == "a.txt"
    # a (1).txt 仍被 #1 占用 → #4 须递增到 (2)。
    assert items[4]["repaired_path"] == "A (2).TXT"
    assert resp["repair_plan"]["remaining_collisions"] == 0


def test_repair_extension_variants():
    resp = _call(
        [
            {"path": "README", "type": "file"},
            {"path": "readme", "type": "file"},  # 无扩展名
            {"path": ".gitignore", "type": "file"},
            {"path": ".GITIGNORE", "type": "file"},  # dotfile 整体作词干
            {"path": "archive.tar.gz", "type": "file"},
            {"path": "ARCHIVE.TAR.GZ", "type": "file"},  # 仅末段扩展名前插入
        ],
        repair=True,
    ).json()
    items = _items(resp)
    assert items[2]["repaired_path"] == "readme (1)"
    assert items[4]["repaired_path"] == ".GITIGNORE (1)"
    assert items[6]["repaired_path"] == "ARCHIVE.TAR (1).GZ"
    assert resp["repair_plan"]["remaining_collisions"] == 0


def test_repair_hierarchy_renames_blocking_file():
    resp = _call(
        [
            {"path": "a/b", "type": "file"},
            {"path": "a/b/c.txt", "type": "file"},
            {"path": "a/b/sub", "type": "directory"},
        ],
        repair=True,
    ).json()
    items = _items(resp)
    # 文件阻挡后代 → 重命名该文件，后代保持不动。
    assert items[1]["status"] == "renamed"
    assert items[1]["repaired_path"] == "a/b (1)"
    assert items[1]["reasons"] == ["blocks_descendants"]
    assert items[2]["status"] == "kept"
    assert items[2]["repaired_path"] == "a/b/c.txt"
    assert items[3]["status"] == "kept"
    plan = resp["repair_plan"]
    assert plan["renamed"] == 1
    assert plan["remaining_collisions"] == 0
    assert plan["remaining_hierarchy_conflicts"] == 0


def test_repair_hierarchy_chain():
    resp = _call(
        [
            {"path": "a", "type": "file"},
            {"path": "a/b", "type": "file"},
            {"path": "a/b/c.txt", "type": "file"},
        ],
        repair=True,
    ).json()
    items = _items(resp)
    assert items[1]["repaired_path"] == "a (1)"
    assert items[2]["repaired_path"] == "a/b (1)"
    assert items[3]["status"] == "kept"
    assert resp["repair_plan"]["remaining_hierarchy_conflicts"] == 0


def test_repair_candidate_must_not_create_new_hierarchy_conflict():
    # 文件 a 阻挡目录 a/b；但 a (1) 会成为已有文件 a (1)/c.txt 的祖先，须跳到 a (2)。
    resp = _call(
        [
            {"path": "a", "type": "file"},
            {"path": "a/b", "type": "directory"},
            {"path": "a (1)/c.txt", "type": "file"},
        ],
        repair=True,
    ).json()
    items = _items(resp)
    assert items[1]["repaired_path"] == "a (2)"
    assert items[2]["status"] == "kept"
    assert items[3]["status"] == "kept"
    plan = resp["repair_plan"]
    assert plan["remaining_collisions"] == 0
    assert plan["remaining_hierarchy_conflicts"] == 0


def test_repair_blocking_file_moves_before_blocked_entry():
    # 回归：文件 D 阻挡 d/e 且自身待改名；d/e 的候选祖先链依赖 D 先移走，
    # 改名须按原始键深度递增处理，否则候选永远被未移走的 D 占据。
    resp = _call(
        [
            {"path": "a", "type": "directory"},
            {"path": "b/C.txt", "type": "directory"},
            {"path": "d/e", "type": "file"},
            {"path": "d/e", "type": "directory"},
            {"path": "README", "type": "file"},
            {"path": "D", "type": "file"},
            {"path": "README", "type": "file"},
            {"path": "b", "type": "directory"},
        ],
        repair=True,
    ).json()
    items = _items(resp)
    assert items[6]["status"] == "renamed"
    assert items[6]["repaired_path"] == "D (1)"
    assert items[6]["reasons"] == ["blocks_descendants"]
    assert items[4]["repaired_path"] == "d/e (1)"
    assert items[7]["repaired_path"] == "README (1)"
    plan = resp["repair_plan"]
    assert plan["remaining_collisions"] == 0
    assert plan["remaining_hierarchy_conflicts"] == 0


def test_repair_keeper_is_first_non_blocking_member():
    # #1 文件阻挡 #3 → 不能保留；#2 目录不阻挡后代 → 保留原名。
    resp = _call(
        [
            {"path": "a", "type": "file"},
            {"path": "A", "type": "directory"},
            {"path": "a/x.txt", "type": "file"},
        ],
        repair=True,
    ).json()
    items = _items(resp)
    assert items[1]["status"] == "renamed"
    assert items[1]["repaired_path"] == "a (1)"
    assert items[1]["reasons"] == ["collision", "blocks_descendants"]
    assert items[2]["status"] == "kept"
    assert items[2]["repaired_path"] == "A"
    assert items[3]["status"] == "kept"
    assert resp["repair_plan"]["remaining_hierarchy_conflicts"] == 0


def test_repair_all_group_members_blocking_all_renamed():
    # 两个同名文件都阻挡后代：组内无可保留者，全部改名。
    resp = _call(
        [
            {"path": "a", "type": "file"},
            {"path": "A", "type": "file"},
            {"path": "a/x.txt", "type": "file"},
        ],
        repair=True,
    ).json()
    items = _items(resp)
    assert items[1]["repaired_path"] == "a (1)"
    assert items[2]["repaired_path"] == "A (2)"
    assert items[3]["status"] == "kept"
    plan = resp["repair_plan"]
    assert plan["remaining_collisions"] == 0
    assert plan["remaining_hierarchy_conflicts"] == 0


def test_repair_directory_loses_collision_to_file():
    resp = _call(
        [
            {"path": "a", "type": "file"},
            {"path": "A", "type": "directory"},
        ],
        repair=True,
    ).json()
    items = _items(resp)
    assert items[1]["status"] == "kept"
    assert items[2]["status"] == "renamed"
    assert items[2]["repaired_path"] == "A (1)"
    assert items[2]["reasons"] == ["collision"]


def test_repair_unsafe_entries_skipped():
    resp = _call(
        [
            {"path": "../evil.bin", "type": "file"},
            {"path": "docs/a.txt", "type": "file"},
            {"path": "docs/A.TXT", "type": "file"},
            {"path": "C:/abs.txt", "type": "file"},
        ],
        repair=True,
    ).json()
    items = _items(resp)
    # 不安全条目标记 skipped 且不改名。
    assert items[1]["status"] == "skipped"
    assert items[1]["repaired_path"] is None
    assert items[1]["reasons"] == []
    assert items[4]["status"] == "skipped"
    assert items[4]["repaired_path"] is None
    # 安全条目照常修复。
    assert items[3]["repaired_path"] == "docs/A (1).TXT"
    plan = resp["repair_plan"]
    assert plan["skipped"] == 2
    assert plan["renamed"] == 1
    assert plan["remaining_collisions"] == 0


def test_repair_no_conflicts_all_kept():
    resp = _call(
        [
            {"path": "a/b.txt", "type": "file"},
            {"path": "a/c", "type": "directory"},
        ],
        repair=True,
    ).json()
    plan = resp["repair_plan"]
    assert plan["renamed"] == 0
    assert plan["skipped"] == 0
    assert plan["remaining_collisions"] == 0
    assert plan["remaining_hierarchy_conflicts"] == 0
    for item in plan["items"]:
        assert item["status"] == "kept"
    # kept 条目的 repaired_path 即其 canonical_path。
    by = {e["index"]: e for e in resp["entries"]}
    for item in plan["items"]:
        assert item["repaired_path"] == by[item["index"]]["canonical_path"]


def test_repair_deterministic_across_calls():
    entries = [
        {"path": "docs/readme.txt", "type": "file"},
        {"path": "docs/README.TXT", "type": "file"},
        {"path": "docs/readme (1).txt", "type": "file"},
        {"path": "a", "type": "file"},
        {"path": "a/b.txt", "type": "file"},
    ]
    first = _call(entries, repair=True).json()["repair_plan"]
    second = _call(entries, repair=True).json()["repair_plan"]
    assert first == second


def test_repair_does_not_change_base_analysis():
    # repair=true 时，原有 entries/collision_groups/hierarchy_conflicts/summary
    # 仍基于原始路径，不受改名影响。
    entries = [
        {"path": "a/b", "type": "file"},
        {"path": "a/b/c.txt", "type": "file"},
        {"path": "a/B", "type": "file"},
    ]
    base = _call(entries).json()
    repaired = _call(entries, repair=True).json()
    for key in ("entries", "collision_groups", "hierarchy_conflicts", "summary"):
        assert repaired[key] == base[key]
    assert repaired["repair_plan"] is not None
