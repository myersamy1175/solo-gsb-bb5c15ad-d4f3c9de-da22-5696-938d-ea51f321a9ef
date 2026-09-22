# Markdown 引用闭包校验服务

基于 **FastAPI + SQLite** 的 Markdown 文档引用闭包校验 API。客户端提交一组带路径的
Markdown 文档，服务解析相对链接、标题锚点与代码块，建立跨文档引用图，检测断链、
锚点碰撞与循环引用，并返回按路径定位的诊断；修复预览接口只重写受影响的标题锚点
与引用目标，代码块与正文保持不变。

## 功能

- **文档导入**：批量提交 `{path, content}`，路径自动规范化并校验（拒绝绝对路径与
  `..` 逃逸）。
- **引用图构建**：解析行内链接、图片、引用式链接（仅统计被使用的定义）、ATX 标题
  （GitHub 风格锚点，支持 `{#custom}` 显式锚点与 CJK），跳过围栏/缩进代码块与行内
  代码中的内容。
- **诊断**（按 `path` + `line` 定位）：
  | 类型 | 级别 | 说明 |
  |---|---|---|
  | `broken_link` | error | 链接目标不在导入的文档集合中 |
  | `broken_anchor` | error | 目标文档存在，但 `#fragment` 无对应锚点 |
  | `anchor_collision` | warning/error | 多个标题共享同一锚点（歧义）或锚点重复定义 |
  | `cycle` | warning | 文档间循环引用（强连通分量，含自引用） |
- **校验任务**：校验在后台任务中执行，可查询任务状态与诊断（支持按类型/路径过滤）。
- **修复预览**：返回受影响文档的逐行变更与完整修复后内容，**不改写代码块与正文**：
  - 锚点碰撞 → 为重复标题补充显式锚点 `{#id}`（尽量保持原锚点值不变）；
  - 断锚 → 以最相似的目标锚点重写引用片段（相似度阈值 0.6）；
  - 断链 → 若集合中存在唯一同名文件，重写为正确的相对路径；
  - 循环引用 → 结构性问题，不自动修复，列入 `unfixed`。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# API: http://localhost:8000  (文档: /docs)
```

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

数据库（SQLite）在启动时自动初始化，默认路径 `./data/app.db`，可用环境变量
`DB_PATH` 覆盖。

### 测试

```bash
pip install -r requirements-dev.txt
pytest
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/imports` | 导入文档集合 → `import_id` |
| `GET` | `/api/imports/{import_id}` | 查看导入批次与文档路径 |
| `GET` | `/api/imports/{import_id}/documents/{path}` | 读取单个文档 |
| `POST` | `/api/validations` | 对某导入批次发起校验（后台任务）→ `validation_id` |
| `GET` | `/api/validations` | 最近校验任务列表 |
| `GET` | `/api/validations/{validation_id}` | 任务状态 + 诊断（`?type=`、`?path=` 过滤） |
| `POST` | `/api/validations/{validation_id}/fix-preview` | 修复预览（任务须为 `done`） |

### 示例

```bash
# 1. 导入
curl -X POST localhost:8000/api/imports -H 'Content-Type: application/json' -d '{
  "documents": [
    {"path": "README.md", "content": "# 项目\n\n参见 [安装](docs/setup.md#安装)。\n"},
    {"path": "docs/setup.md", "content": "# 安装\n\n## 步骤\n\n## 步骤\n"}
  ]}'
# => {"import_id": "...", "doc_count": 2, ...}

# 2. 发起校验
curl -X POST localhost:8000/api/validations \
  -H 'Content-Type: application/json' -d '{"import_id": "<import_id>"}'
# => {"validation_id": "...", "status": "pending"}

# 3. 查询任务与诊断
curl localhost:8000/api/validations/<validation_id>
curl 'localhost:8000/api/validations/<validation_id>?type=broken_link&path=README.md'

# 4. 修复预览
curl -X POST localhost:8000/api/validations/<validation_id>/fix-preview
```

修复预览响应：

```json
{
  "fixed_documents": [
    {
      "path": "docs/setup.md",
      "changes": [
        {"line": 5, "kind": "heading-anchor",
         "before": "## 步骤", "after": "## 步骤  {#步骤-1}"}
      ],
      "fixed_content": "# 安装\n\n## 步骤\n\n## 步骤  {#步骤-1}\n"
    }
  ],
  "unfixed": [
    {"type": "cycle", "path": "README.md", "reason": "circular references require manual restructuring"}
  ],
  "stats": {"documents_rewritten": 1, "edits": 1, "unfixed": 1}
}
```

## 解析规则与已知限制

- 锚点生成遵循 GitHub 规则：小写、去标点、空白转 `-`，重复标题自动追加 `-1`、`-2`；
  支持 `{#custom-id}` 显式锚点（kramdown/Pandoc 风格）。
- 仅识别 ATX 标题（`#`），不支持 Setext（`===`/`---`）标题。
- 外部链接（`http:`、`mailto:` 等）与协议相对链接（`//host`）不参与校验。
- 链接目标按 POSIX 相对路径解析；以 `/` 开头视为仓库根目录绝对路径；
  `%20` 等百分号编码在比较前解码。
- 仅被实际引用的 reference-style 定义（`[label]: target`）计入引用图。
- 行内链接目的地不支持未转义的嵌套括号（CommonMark 本身亦建议用 `<>` 包裹）。
- 缩进代码块按"4 空格/Tab 且前接空行"的简化规则识别。

## 项目结构

```
app/
  main.py      # FastAPI 应用与路由，启动时自动初始化数据库
  parser.py    # Markdown 解析：标题/锚点/链接/代码块
  analyzer.py  # 引用图构建与诊断（断链/断锚/碰撞/环）
  fixer.py     # 修复预览：最小化重写锚点与引用
  db.py        # SQLite 持久层（imports / documents / validations / diagnostics）
  schemas.py   # 请求模型与路径校验
tests/
  test_api.py  # 解析器、分析器与 API 端到端测试
```
