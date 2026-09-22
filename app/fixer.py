"""修复预览：只重写受影响的标题锚点与链接目标，代码块与正文保持不变。"""
from __future__ import annotations

import re

from .markdown import parse_markdown

_EXPLICIT_TAIL_RE = re.compile(r"\s*\{#[A-Za-z0-9_.:-]+\}\s*$")


def _apply_heading_anchor(line: str, new_id: str) -> str:
    """为标题行指定显式锚点：已有 {#id} 则替换，否则追加。"""
    if _EXPLICIT_TAIL_RE.search(line):
        return _EXPLICIT_TAIL_RE.sub(" {#" + new_id + "}", line)
    return line.rstrip() + " {#" + new_id + "}"


def build_fix_preview(docs: dict[str, str], diagnostics: list[dict]) -> dict:
    """根据诊断生成修复预览。

    返回 {"fixed_documents": n, "documents": [...], "unfixable": [...]}；
    每个 document 含 path、逐行 edits（before/after/reasons）与 fixed_content。
    """
    line_edits: dict[str, dict[int, list[dict]]] = {}

    def add(path: str, line: int, edit: dict) -> None:
        line_edits.setdefault(path, {}).setdefault(line, []).append(edit)

    # 1) 链接目标重写：断链改指向候选文件，断锚改指向候选锚点
    for d in diagnostics:
        code = d.get("code")
        suggestion = d.get("suggestion")
        target = d.get("target") or ""
        if not suggestion or d.get("line") is None or d.get("column") is None:
            continue
        if code == "broken_anchor" and "#" in target:
            path_part, _, frag = target.partition("#")
            add(
                d["path"],
                d["line"],
                {
                    "kind": "span",
                    "col": d["column"],
                    "old": target,
                    "new": f"{path_part}#{suggestion}",
                    "reason": f"锚点 '#{frag}' 不存在，修正为 '#{suggestion}'",
                },
            )
        elif code == "broken_link":
            path_part, sep, frag = target.partition("#")
            new_target = suggestion + (f"#{frag}" if sep else "")
            add(
                d["path"],
                d["line"],
                {
                    "kind": "span",
                    "col": d["column"],
                    "old": target,
                    "new": new_target,
                    "reason": f"文档 '{path_part}' 不存在，修正为 '{suggestion}'",
                },
            )

    # 2) 锚点碰撞：为第一处之后的重复标题追加显式锚点 {#slug-N}
    collision_paths = {d["path"] for d in diagnostics if d.get("code") == "anchor_collision"}
    for path in sorted(collision_paths):
        if path not in docs:
            continue
        parsed = parse_markdown(path, docs[path])
        table = parsed.anchor_table
        taken = set(table)
        for anchor, headings in table.items():
            if len(headings) < 2:
                continue
            n = 1
            for h in headings[1:]:
                n += 1
                new_id = f"{anchor}-{n}"
                while new_id in taken:
                    n += 1
                    new_id = f"{anchor}-{n}"
                taken.add(new_id)
                add(
                    path,
                    h.line,
                    {
                        "kind": "heading",
                        "new_id": new_id,
                        "reason": f"锚点 '#{anchor}' 碰撞，为标题指定显式锚点 '#{new_id}'",
                    },
                )

    # 3) 应用编辑：仅触碰标题行与链接目标，其余字节原样保留
    previews = []
    for path in sorted(line_edits):
        lines = docs[path].splitlines()
        changes = []
        for lineno in sorted(line_edits[path]):
            original = lines[lineno - 1]
            new_line = original
            reasons: list[str] = []

            spans = sorted(
                (e for e in line_edits[path][lineno] if e["kind"] == "span"),
                key=lambda e: e["col"],
                reverse=True,  # 同行多处替换从右往左，列号不受前序替换影响
            )
            for e in spans:
                start = e["col"] - 1
                if new_line[start : start + len(e["old"])] != e["old"]:
                    continue
                new_line = new_line[:start] + e["new"] + new_line[start + len(e["old"]) :]
                reasons.append(e["reason"])

            for e in line_edits[path][lineno]:
                if e["kind"] == "heading":
                    new_line = _apply_heading_anchor(new_line, e["new_id"])
                    reasons.append(e["reason"])

            if new_line != original:
                lines[lineno - 1] = new_line
                changes.append(
                    {"line": lineno, "before": original, "after": new_line, "reasons": reasons}
                )

        if changes:
            content = "\n".join(lines)
            if docs[path].endswith("\n"):
                content += "\n"
            previews.append({"path": path, "edits": changes, "fixed_content": content})

    unfixable = [
        d
        for d in diagnostics
        if (d.get("code") in ("broken_link", "broken_anchor") and not d.get("suggestion"))
        or d.get("code") == "cycle"
    ]
    return {"fixed_documents": len(previews), "documents": previews, "unfixable": unfixable}
