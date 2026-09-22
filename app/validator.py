"""引用闭包校验：断链、锚点缺失、锚点碰撞、循环引用。"""
from __future__ import annotations

import difflib
import posixpath
import sys
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Optional

from .markdown import is_external, parse_markdown

DOC_EXTS = (".md", ".markdown")


@dataclass
class Diagnostic:
    path: str
    line: Optional[int]
    column: Optional[int]
    severity: str  # error | warning
    code: str  # broken_link | broken_anchor | anchor_collision | cycle
    message: str
    target: Optional[str] = None
    suggestion: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _split_target(target: str) -> tuple[str, str]:
    path_part, _, frag = target.partition("#")
    return urllib.parse.unquote(path_part), urllib.parse.unquote(frag)


def _resolve(src_path: str, rel: str) -> Optional[str]:
    """把相对链接解析为集合内路径；越出根目录返回 None。"""
    base = posixpath.dirname(src_path)
    joined = posixpath.normpath(posixpath.join(base, rel)) if base else posixpath.normpath(rel)
    if joined == ".":
        joined = rel
    if joined.startswith("..") or joined.startswith("/"):
        return None
    return joined


def _suggest_path(src: str, missing: str, paths) -> Optional[str]:
    """目标文件缺失时，按同名文件（忽略大小写）给出唯一候选的相对路径。"""
    base = posixpath.basename(missing).lower()
    candidates = [p for p in paths if posixpath.basename(p).lower() == base]
    if len(candidates) != 1:
        return None
    src_dir = posixpath.dirname(src)
    return posixpath.relpath(candidates[0], src_dir) if src_dir else candidates[0]


def _suggest_anchor(bad: str, table: dict) -> Optional[str]:
    names = list(table)
    for n in names:
        if n.lower() == bad.lower():
            return n
    lowered = {n.lower(): n for n in names}
    matches = difflib.get_close_matches(bad.lower(), list(lowered), n=1, cutoff=0.6)
    return lowered[matches[0]] if matches else None


def _sccs(nodes, edges) -> list[list[str]]:
    """Tarjan 强连通分量，用于检测文档级循环引用。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = [0]

    def visit(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in edges.get(v, ()):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            result.append(scc)

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, 10000))
    try:
        for v in nodes:
            if v not in index:
                visit(v)
    finally:
        sys.setrecursionlimit(old_limit)
    return result


def validate(docs: dict[str, str]) -> tuple[list[Diagnostic], dict]:
    """对一组 {路径: 内容} 的文档做闭包校验，返回 (诊断列表, 统计信息)。"""
    parsed = {p: parse_markdown(p, c) for p, c in docs.items()}
    anchors = {p: d.anchor_table for p, d in parsed.items()}

    diagnostics: list[Diagnostic] = []
    edges: dict[str, set[str]] = {p: set() for p in parsed}
    links_checked = 0

    # 1) 锚点碰撞：同一文档内多个标题解析出相同锚点
    for path in sorted(parsed):
        for anchor, headings in sorted(anchors[path].items()):
            if len(headings) <= 1:
                continue
            lines = ", ".join(str(h.line) for h in headings)
            for h in headings[1:]:
                diagnostics.append(
                    Diagnostic(
                        path=path,
                        line=h.line,
                        column=1,
                        severity="warning",
                        code="anchor_collision",
                        message=(
                            f"锚点 '#{anchor}' 在文档内出现 {len(headings)} 次"
                            f"（行 {lines}），引用将始终指向第一处"
                        ),
                        target=f"#{anchor}",
                    )
                )

    # 2) 链接校验：断链 + 锚点缺失，同时累计文档间边
    for path in sorted(parsed):
        for link in parsed[path].links:
            target = link.target
            if is_external(target):
                continue
            path_part, frag = _split_target(target)
            if path_part and not path_part.lower().endswith(DOC_EXTS):
                continue  # 图片等静态资产不在文档集合范围内
            links_checked += 1

            if path_part:
                resolved = _resolve(path, path_part)
                if resolved is None or resolved not in parsed:
                    diagnostics.append(
                        Diagnostic(
                            path=path,
                            line=link.line,
                            column=link.column,
                            severity="error",
                            code="broken_link",
                            message=f"链接目标不存在: {path_part}",
                            target=target,
                            suggestion=_suggest_path(path, path_part, parsed),
                        )
                    )
                    continue
                if resolved != path:
                    edges[path].add(resolved)
                dest = resolved
            else:
                dest = path

            if frag and frag not in anchors[dest]:
                diagnostics.append(
                    Diagnostic(
                        path=path,
                        line=link.line,
                        column=link.column,
                        severity="error",
                        code="broken_anchor",
                        message=f"锚点 '#{frag}' 在 {dest} 中不存在",
                        target=target,
                        suggestion=_suggest_anchor(frag, anchors[dest]),
                    )
                )

    # 3) 循环引用：文档引用图中的非平凡强连通分量
    for scc in _sccs(list(parsed), edges):
        if len(scc) > 1:
            ordered = sorted(scc)
            diagnostics.append(
                Diagnostic(
                    path=ordered[0],
                    line=None,
                    column=None,
                    severity="warning",
                    code="cycle",
                    message="循环引用: " + " → ".join(ordered + [ordered[0]]),
                )
            )

    diagnostics.sort(key=lambda d: (d.path, d.line or 0, d.column or 0))
    stats = {
        "documents": len(parsed),
        "headings": sum(len(d.headings) for d in parsed.values()),
        "links_checked": links_checked,
    }
    return diagnostics, stats
