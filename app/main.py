"""FastAPI 入口：文档导入、校验任务、修复预览。"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .db import connect, init_db
from .fixer import build_fix_preview
from .validator import validate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()  # 启动时自动初始化数据库
    yield


app = FastAPI(
    title="Markdown 引用闭包校验服务",
    version="1.0.0",
    description="解析 Markdown 文档集合的相对链接与标题锚点，检测断链、锚点碰撞与循环引用，并提供修复预览。",
    lifespan=lifespan,
)


# ---------- 请求模型 ----------

class DocumentIn(BaseModel):
    path: str = Field(..., examples=["docs/setup.md"])
    content: str = Field(..., examples=["# Setup\n"])


class ImportRequest(BaseModel):
    name: str | None = Field(default=None, examples=["my-docs"])
    documents: list[DocumentIn] = Field(..., min_length=1)


class ValidationRequest(BaseModel):
    set_id: int


class FixPreviewRequest(BaseModel):
    task_id: str


# ---------- 工具 ----------

def _normalize_path(raw: str) -> str:
    p = raw.strip().replace("\\", "/")
    if not p or p.startswith("/") or p.startswith("~"):
        raise HTTPException(422, f"非法路径（不允许绝对路径）: {raw!r}")
    parts = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise HTTPException(422, f"路径不允许包含 '..': {raw!r}")
        parts.append(seg)
    if not parts:
        raise HTTPException(422, f"非法路径: {raw!r}")
    return "/".join(parts)


def _load_docs(set_id: int) -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT path, content FROM documents WHERE set_id = ? ORDER BY path", (set_id,)
        ).fetchall()
    return {r["path"]: r["content"] for r in rows}


def _run_validation(task_id: str, set_id: int) -> None:
    """后台执行校验并落库诊断结果。"""
    try:
        with connect() as conn:
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
        docs = _load_docs(set_id)
        diagnostics, stats = validate(docs)

        by_code: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for d in diagnostics:
            by_code[d.code] = by_code.get(d.code, 0) + 1
            by_severity[d.severity] = by_severity.get(d.severity, 0) + 1
        summary = {**stats, "diagnostics": len(diagnostics), "by_code": by_code, "by_severity": by_severity}

        with connect() as conn:
            conn.executemany(
                """INSERT INTO diagnostics
                   (task_id, path, line, column, severity, code, message, target, suggestion)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (task_id, d.path, d.line, d.column, d.severity, d.code, d.message, d.target, d.suggestion)
                    for d in diagnostics
                ],
            )
            conn.execute(
                "UPDATE tasks SET status = 'done', finished_at = ?, summary = ? WHERE id = ?",
                (_now(), json.dumps(summary, ensure_ascii=False), task_id),
            )
    except Exception as exc:  # noqa: BLE001 - 任务失败需落库而非吞掉
        with connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (_now(), str(exc), task_id),
            )


# ---------- 接口 ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/v1/documents/import", status_code=201)
def import_documents(req: ImportRequest):
    """导入一组带路径的 Markdown 文档，返回文档集 ID。"""
    normalized: dict[str, str] = {}
    for doc in req.documents:
        path = _normalize_path(doc.path)
        if path in normalized:
            raise HTTPException(422, f"文档路径重复: {path}")
        normalized[path] = doc.content

    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO document_sets (name, created_at) VALUES (?, ?)",
            (req.name or "", _now()),
        )
        set_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO documents (set_id, path, content) VALUES (?, ?, ?)",
            [(set_id, p, c) for p, c in normalized.items()],
        )
    return {"set_id": set_id, "document_count": len(normalized), "paths": sorted(normalized)}


@app.get("/api/v1/document-sets/{set_id}/documents")
def list_documents(set_id: int):
    with connect() as conn:
        doc_set = conn.execute(
            "SELECT id, name, created_at FROM document_sets WHERE id = ?", (set_id,)
        ).fetchone()
        if not doc_set:
            raise HTTPException(404, "文档集不存在")
        rows = conn.execute(
            "SELECT path, length(content) AS size FROM documents WHERE set_id = ? ORDER BY path",
            (set_id,),
        ).fetchall()
    return {
        "set_id": doc_set["id"],
        "name": doc_set["name"],
        "created_at": doc_set["created_at"],
        "documents": [{"path": r["path"], "size": r["size"]} for r in rows],
    }


@app.post("/api/v1/validations", status_code=202)
def create_validation(req: ValidationRequest, background_tasks: BackgroundTasks):
    """对指定文档集发起异步校验，返回任务 ID。"""
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM document_sets WHERE id = ?", (req.set_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, "文档集不存在")
        task_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO tasks (id, set_id, status, created_at) VALUES (?, ?, 'pending', ?)",
            (task_id, req.set_id, _now()),
        )
    background_tasks.add_task(_run_validation, task_id, req.set_id)
    return {"task_id": task_id, "set_id": req.set_id, "status": "pending"}


@app.get("/api/v1/validations/{task_id}")
def get_validation(task_id: str):
    """查询校验任务状态与诊断结果。"""
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "校验任务不存在")
        rows = conn.execute(
            """SELECT path, line, column, severity, code, message, target, suggestion
               FROM diagnostics WHERE task_id = ?
               ORDER BY path, line, column""",
            (task_id,),
        ).fetchall()
    return {
        "task_id": task["id"],
        "set_id": task["set_id"],
        "status": task["status"],
        "created_at": task["created_at"],
        "finished_at": task["finished_at"],
        "summary": json.loads(task["summary"]) if task["summary"] else None,
        "error": task["error"],
        "diagnostics": [dict(r) for r in rows],
    }


@app.post("/api/v1/fix-previews")
def fix_preview(req: FixPreviewRequest):
    """基于已完成任务的诊断生成修复预览（不写回文档集）。

    预览只重写受影响的标题锚点与链接目标，代码块与正文保持不变。
    """
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (req.task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "校验任务不存在")
        if task["status"] != "done":
            raise HTTPException(409, f"任务尚未完成（当前状态: {task['status']}）")
        rows = conn.execute(
            """SELECT path, line, column, severity, code, message, target, suggestion
               FROM diagnostics WHERE task_id = ?""",
            (req.task_id,),
        ).fetchall()

    docs = _load_docs(task["set_id"])
    preview = build_fix_preview(docs, [dict(r) for r in rows])
    return {"task_id": req.task_id, "set_id": task["set_id"], **preview}
