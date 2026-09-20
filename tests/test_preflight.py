import unicodedata

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _call(entries):
    return client.post("/api/v1/preflight", json={"entries": entries})


def _by_index(resp):
    return {e["index"]: e for e in resp["entries"]}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "archive-path-preflight"}


def test_windows_normalization_and_collision_group():
    # Å (U+00C5) 的 NFD = A(U+0041) + 组合环(U+030A)；NFC 后为 Å，casefold 为 å。
    nfd_ring = unicodedata.normalize("NFD", "Å")
    entries = [
        {"path": "Docs\\ReadMe.txt", "type": "file"},
        {"path": "docs/readme.TXT", "type": "file"},  # 大小写折叠后同键
        {"path": f"docs/caf{nfd_ring}.txt", "type": "file"},  # NFC 归一（信息提示）
        {"path": "docs/readme.txt.  ", "type": "file"},  # 段尾点空格裁剪后同键
        {"path": "docs/CAFÅ.TXT", "type": "file"},  # 与 #3 经 NFC+折叠后碰撞
        {"path": "docs/other.md", "type": "file"},
    ]
    resp = _call(entries).json()
    by = _by_index(resp)

    assert by[1]["canonical_path"] == "Docs/ReadMe.txt"
    assert by[1]["comparison_key"] == "docs/readme.txt"
    assert by[3]["canonical_path"] == "docs/cafÅ.txt"
    assert by[4]["canonical_path"] == "docs/readme.txt"
    # 组 1：#1/#2/#4
    for i in (1, 2, 4):
        assert by[i]["safe"] is True
        assert by[i]["collision_group"] == 1
        assert "COLLISION" in [iss["code"] for iss in by[i]["issues"]]
    # 组 2：#3/#5（NFC + 大小写折叠）
    assert by[3]["collision_group"] == 2
    assert by[5]["collision_group"] == 2
    assert "UNICODE_NORMALIZED" in [i["code"] for i in by[3]["issues"]]
    assert "SEGMENT_TRIMMED" in [i["code"] for i in by[4]["issues"]]
    assert by[6]["collision_group"] is None
    groups = resp["collision_groups"]
    assert len(groups) == 2
    assert groups[0]["group"] == 1 and len(groups[0]["members"]) == 3
    assert groups[1]["group"] == 2 and len(groups[1]["members"]) == 2
    assert resp["summary"]["collisions"] == 2


def test_hierarchy_conflict_file_ancestor():
    entries = [
        {"path": "a/b", "type": "file"},
        {"path": "a/b/c.txt", "type": "file"},  # 被 #1 的文件阻挡
        {"path": "a/b/sub", "type": "directory"},  # 同样被阻挡
        {"path": "a/b2/c.txt", "type": "file"},  # 不冲突（仅词法前缀）
    ]
    resp = _call(entries).json()
    by = _by_index(resp)
    assert by[2]["blocked_by_file_ancestors"] == [1]
    assert by[3]["blocked_by_file_ancestors"] == [1]
    assert by[1]["blocks_descendants"] == [2, 3]
    assert by[4]["blocked_by_file_ancestors"] == []
    assert resp["summary"]["hierarchy_conflicts"] == 2
    pairs = [
        (c["file_index"], c["descendant_index"])
        for c in resp["hierarchy_conflicts"]
    ]
    assert pairs == [(1, 2), (1, 3)]


def test_unsafe_absolute_drive_and_traversal():
    entries = [
        {"path": "/etc/passwd", "type": "file"},
        {"path": "C:\\Windows\\system32", "type": "directory"},
        {"path": "../../evil.bin", "type": "file"},
        {"path": "safe/../inside.txt", "type": "file"},  # 根内 .. 合法
        {"path": "a/../../escape.txt", "type": "file"},  # 越过根
        {"path": "\\\\server\\share\\x", "type": "file"},  # UNC
    ]
    resp = _call(entries).json()
    by = _by_index(resp)
    assert by[1]["safe"] is False
    assert by[1]["issues"][0]["code"] == "ABSOLUTE_PATH"
    assert by[2]["safe"] is False
    assert by[2]["issues"][0]["code"] == "DRIVE_PATH"
    assert by[3]["safe"] is False
    assert by[4]["safe"] is True
    assert by[4]["canonical_path"] == "inside.txt"
    assert by[5]["safe"] is False
    assert by[6]["safe"] is False
    assert by[6]["issues"][0]["code"] == "UNC_PATH"
    assert resp["summary"]["unsafe"] == 5
    assert resp["summary"]["safe"] == 1


def test_reserved_names_and_invalid_chars():
    entries = [
        {"path": "dir/CON", "type": "file"},
        {"path": "LPT9.txt", "type": "file"},
        {"path": "COM0", "type": "file"},  # COM0 不是文档保留名
        {"path": "bad:name?.txt", "type": "file"},
        {"path": "nul/x", "type": "directory"},  # 中间段保留名也拦截
    ]
    resp = _call(entries).json()
    by = _by_index(resp)
    assert by[1]["safe"] is False
    assert by[2]["safe"] is False
    assert by[3]["safe"] is True
    assert by[4]["safe"] is False
    assert by[5]["safe"] is False
    codes_5 = {i["code"] for i in by[5]["issues"]}
    assert "RESERVED_NAME" in codes_5


def test_order_preserved_and_trimming_issues():
    entries = [
        {"path": "z z/last. .", "type": "file"},
        {"path": ".", "type": "directory"},  # 规范化为空
        {"path": "a//b/", "type": "file"},
    ]
    resp = _call(entries).json()
    assert [e["index"] for e in resp["entries"]] == [1, 2, 3]
    by = _by_index(resp)
    assert by[1]["canonical_path"] == "z z/last"
    codes_1 = {i["code"] for i in by[1]["issues"]}
    assert "SEGMENT_TRIMMED" in codes_1
    assert by[2]["safe"] is False
    assert any(i["code"] == "EMPTY_TARGET" for i in by[2]["issues"])
    assert by[3]["canonical_path"] == "a/b"
    assert any(i["code"] == "EMPTY_SEGMENT" for i in by[3]["issues"])


def test_empty_body_rejected():
    r = _call([])
    assert r.status_code == 422


def test_directory_ancestor_does_not_conflict():
    entries = [
        {"path": "a/b", "type": "directory"},
        {"path": "a/b/c", "type": "file"},
    ]
    resp = _call(entries).json()
    assert resp["summary"]["hierarchy_conflicts"] == 0
