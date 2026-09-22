# Markdown 引用闭包校验服务

基于 **FastAPI + SQLite** 的 Markdown 文档引用闭包校验 API。客户端提交一组带路径的
Markdown 文档，服务解析相对链接、标题锚点与围栏代码块，建立跨文档引用图，检测
**断链、锚点缺失、锚点碰撞与循环引用**，返回按路径与行列定位的诊断；并提供
**修复预览**——只重写受影响的标题锚点与链接目标，代码块与正文保持不变。

## 功能

- **文档导入**：批量提交 `{path, content}`，路径归一化并校验（拒绝绝对路径与 `..`）
- **异步校验任务**：后台构建引用图，任务状态与诊断结果落库，可随时查询
- **诊断类型**
  | code | 级别 | 说明 |
  |---|---|---|
  | `broken_link` | error | 相对链接指向的 `.md` 文档不存在（附同名文件候选建议） |
  | `broken_anchor` | error | 目标文档存在但锚点缺失（附相似锚点建议） |
  | `anchor_collision` | warning | 同一文档内多个标题解析出相同锚点 |
  | `cycle` | warning | 文档引用图中的循环引用（Tarjan 强连通分量） |
- **修复预览**（不写回文档集）：
  - 断链 → 重写为唯一同名候选文件的相对路径
  - 断锚 → 重写为大小写/相似度匹配的现有锚点
  - 锚点碰撞 → 为第 2 处起的重复标题追加显式锚点 `{#slug-N}`
  - 循环引用不可自动修复，列入 `unfixable`
- **解析保证**：围栏代码块（` ``` ` / `~~~`）与行内代码中的"标题""链接"不参与解析

## 快速开始

### Docker（推荐）

```bash
docker compose up --build
# 服务监听 http://localhost:8000，SQLite 持久化在 md_data 卷（/data/app.db）
# 启动时自动初始化数据库（建库建表，幂等）
```

### 本地开发

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload        # 默认数据库 ./data/app.db，可用 APP_DB_PATH 覆盖
pytest                               # 运行测试
```

交互式 API 文档：http://localhost:8000/docs

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/api/v1/documents/import` | 导入文档集，返回 `set_id` |
| GET | `/api/v1/document-sets/{set_id}/documents` | 列出文档集内容 |
| POST | `/api/v1/validations` | 对文档集发起异步校验，返回 `task_id`（202） |
| GET | `/api/v1/validations/{task_id}` | 查询任务状态、汇总与诊断 |
| POST | `/api/v1/fix-previews` | 基于已完成任务生成修复预览 |

### 示例

```bash
# 1. 导入
curl -X POST localhost:8000/api/v1/documents/import -H 'Content-Type: application/json' -d '{
  "name": "demo",
  "documents": [
    {"path": "README.md",     "content": "# Guide\n\nSee [setup](docs/setup.md#install).\n"},
    {"path": "docs/setup.md", "content": "# Setup\n\n## Install\n\n## Install\n"}
  ]}'
# => {"set_id": 1, ...}

# 2. 发起校验
curl -X POST localhost:8000/api/v1/validations -H 'Content-Type: application/json' \
  -d '{"set_id": 1}'
# => {"task_id": "...", "status": "pending"}

# 3. 查询诊断
curl localhost:8000/api/v1/validations/<task_id>
# diagnostics 中每条含 path/line/column/severity/code/message/target/suggestion

# 4. 修复预览
curl -X POST localhost:8000/api/v1/fix-previews -H 'Content-Type: application/json' \
  -d '{"task_id": "<task_id>"}'
# documents[]: {path, edits[{line, before, after, reasons}], fixed_content}
# unfixable[]: 无法自动修复的诊断（如循环引用）
```

诊断示例：

```json
{
  "path": "docs/setup.md",
  "line": 7,
  "column": 1,
  "severity": "warning",
  "code": "anchor_collision",
  "message": "锚点 '#install' 在文档内出现 2 次（行 3, 7），引用将始终指向第一处",
  "target": "#install",
  "suggestion": null
}
```

## 设计说明

- **锚点规则**：GitHub 风格 slug（小写、去标点、空白转连字符，保留 CJK）；标题可用
  `{#custom-id}` 显式指定锚点（显式优先于隐式 slug），修复预览对碰撞标题也采用该语法。
- **链接解析**：支持行内链接与图片语法 `[text](target)`；`http(s):`、`mailto:` 等外部
  目标与协议相对 URL 跳过；非 `.md`/`.markdown` 目标视为静态资产，不纳入闭包校验；
  路径做 URL 解码并相对来源文档目录归一化，越出根目录判为断链。
- **引用图**：节点为文档，边为已解析的文档间链接（忽略自环）；用 Tarjan 算法求非平凡
  强连通分量作为循环引用。
- **修复预览的幂等性**：预览仅基于诊断与原文计算，不修改数据库；将 `fixed_content`
  重新导入并校验可验证修复效果（见 `tests/test_api.py::test_fixed_docs_pass_validation`）。
- **存储**：SQLite 单文件，四张表（`document_sets` / `documents` / `tasks` /
  `diagnostics`），外键级联删除；服务启动时通过 lifespan 钩子自动 `init_db()`。

## 已知限制

- 不支持引用式链接 `[text][ref]`、Setext 标题（`===`/`---` 下划线式）、缩进代码块与
  HTML 块内内容的识别
- 断链建议仅在"同名文件唯一"时给出；断锚建议基于大小写不敏感匹配与 difflib 相似度
- 校验任务为进程内后台执行（`BackgroundTasks`），多副本部署时应改用任务队列

## 项目结构

```
app/
  main.py       # FastAPI 入口与路由（导入 / 校验任务 / 修复预览）
  markdown.py   # Markdown 解析：标题锚点、行内链接、围栏代码块
  validator.py  # 引用图构建与四类诊断
  fixer.py      # 修复预览生成（仅重写标题锚点与链接目标）
  db.py         # SQLite 连接与启动初始化
tests/test_api.py
Dockerfile / docker-compose.yml / requirements*.txt
```
