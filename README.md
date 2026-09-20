# Archive Path Preflight API（压缩包路径预检 API）

使用 **Python + FastAPI + 纯内存计算** 的跨平台压缩包路径预检服务。
接收待解包条目的原始路径与 `file` / `directory` 类型，按 **Windows 目标规则**
规范化路径，识别危险条目，并输出同名碰撞组与文件-目录层级冲突。

> 服务**只对路径字符串做词法分析，全程不访问文件系统**，不会读取或写入任何条目。

## 一键启动（Docker Compose，端口 8000）

```bash
docker compose up --build
```

启动后：

- API 服务：<http://localhost:8000>
- Swagger 文档：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>

停止：`docker compose down`

依赖在**容器启动时**由 `docker/entrypoint.sh` 执行 `pip install -r requirements.txt`
安装（venv 位于 `/opt/venv`，以非 root 用户 `appuser` 运行）。

## 本地运行（非 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 测试
pytest -q
```

## 接口

### `GET /health`

健康检查，返回：

```json
{ "status": "ok", "service": "archive-path-preflight" }
```

### `POST /api/v1/preflight`

请求体：

```json
{
  "entries": [
    { "path": "Docs\\ReadMe.txt", "type": "file" },
    { "path": "docs/readme.txt", "type": "file" },
    { "path": "../evil.bin", "type": "file" },
    { "path": "a/b", "type": "file" },
    { "path": "a/b/c.txt", "type": "file" }
  ]
}
```

`type` 仅接受 `file` 或 `directory`。响应中的 `entries` **严格保持输入顺序**，
`index` 从 1 开始。

响应关键字段：

| 字段 | 说明 |
| --- | --- |
| `entries[].canonical_path` | 规范路径：正斜杠 `/` 分隔、每段 Unicode NFC、段尾空格与句点已裁剪，保留原始大小写用于展示 |
| `entries[].comparison_key` | Windows 等价比较键（在规范路径上做大小写折叠 `casefold`）；不安全条目为 `null` |
| `entries[].safe` | 是否可安全解包 |
| `entries[].collision_group` | 所属同名碰撞组编号，无碰撞为 `null` |
| `entries[].blocked_by_file_ancestors` | 占据本条目祖先路径的文件条目下标（层级冲突） |
| `entries[].blocks_descendants` | 本文件条目阻挡了哪些后代条目（仅文件条目） |
| `entries[].issues` | 规范化过程中的问题（`severity` 为 `error` 或 `info`） |
| `collision_groups` | 碰撞组明细（含每个成员的下标与规范路径），按首次出现顺序编号 |
| `hierarchy_conflicts` | `(file_index, descendant_index)` 层级冲突对 |
| `summary` | `total / safe / unsafe / collisions / collision_entries / hierarchy_conflicts` 计数 |

curl 示例：

```bash
curl -s http://localhost:8000/api/v1/preflight \
  -H 'Content-Type: application/json' \
  -d '{"entries":[{"path":"Docs\\ReadMe.txt","type":"file"},{"path":"docs/readme.txt","type":"file"}]}'
```

## 规范化规则（Windows 目标）

1. **统一分隔符**：`\` 与 `/` 均视为路径分隔符，输出统一为 `/`。
2. **大小写**：Windows 文件系统不区分大小写；保留原始大小写用于展示，
   碰撞/层级判断使用 `str.casefold()` 比较键。
3. **Unicode**：每个路径段做 **NFC** 规范化；发生转换时附带 `UNICODE_NORMALIZED` 信息。
4. **段尾空格/句点**：Windows 会忽略每段末尾的空格与 `.`，逐段 `rstrip(" .")`；
   发生裁剪时附带 `SEGMENT_TRIMMED` 信息（裁剪后可能造成同名碰撞）。
5. **保留设备名**：每段（含中间目录段）按首个 `.` 前的词干，不区分大小写识别
   `CON`、`PRN`、`AUX`、`NUL`、`CLOCK$`、`COM1`–`COM9`、`LPT1`–`LPT9`
   （如 `NUL.log`、`aux` 也算），命中标为 `RESERVED_NAME`。
6. **非法字符**：段内含 `< > : " / | ? *` 或 ASCII 控制字符（< 0x20）标为
   `INVALID_CHARACTER`。
7. 重复或结尾分隔符产生的空段会被折叠，并给出 `EMPTY_SEGMENT` 信息；
   段裁剪后为空（如 `"..."`）记为错误。
8. 规范化后没有任何有效段（如空路径或单独的 `.`）记为 `EMPTY_TARGET`。

## 判定为 unsafe 的情形（`severity = error`）

| code | 触发条件 |
| --- | --- |
| `ABSOLUTE_PATH` | 绝对路径（以 `/` 开头） |
| `UNC_PATH` | UNC 网络路径（以 `\\` 或 `//` 开头） |
| `DRIVE_PATH` | 盘符路径（如 `C:\…`、`D:/…`） |
| `TRAVERSAL_ESCAPE` | 词法遍历中 `..` 越过目标根（根内的 `a/../b` 合法） |
| `RESERVED_NAME` | 命中 Windows 保留设备名 |
| `INVALID_CHARACTER` | 含 Windows 非法文件名字符或控制字符 |
| `EMPTY_TARGET` | 规范化后路径为空 |

不安全条目仍会返回**尽力而为**的 `canonical_path` 与问题明细，方便展示与修复，
但**不参与**碰撞分组与层级冲突计算。

## 冲突检测

- **同名碰撞**：安全条目规范化后的 `comparison_key` 相同即落入同一碰撞组
  （大小写、NFC、段尾点/空格差异都会被抹平），组号按该组首次出现的顺序分配。
- **层级冲突**：若某**文件**条目的规范路径恰好是另一条目（文件或目录）的
  祖先路径（如文件 `a/b` 与后代 `a/b/c.txt`），解包时无法在同名路径上创建目录，
  记为层级冲突。目录占据祖先路径属于正常布局，不报告。

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由（/health、/api/v1/preflight）
│   ├── normalize.py     # Windows 路径规范化与 unsafe 判定（纯函数）
│   ├── schemas.py       # Pydantic 请求/响应模型
│   └── service.py       # 碰撞分组与层级冲突编排
├── tests/               # pytest 用例
├── docker/entrypoint.sh # 启动时安装依赖并启动服务
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```
