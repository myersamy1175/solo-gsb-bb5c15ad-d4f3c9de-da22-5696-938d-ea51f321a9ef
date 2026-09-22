"""Markdown 解析：ATX 标题锚点、行内相对链接、围栏代码块。

解析时跳过围栏代码块（``` 与 ~~~）与行内代码，保证代码块中的
"标题" 与 "链接" 不会进入引用图。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<rest>.*)$")
_HEADING_RE = re.compile(r"^ {0,3}(?P<hashes>#{1,6})[ \t]+(?P<text>.*?)[ \t]*$")
_TRAILING_HASHES_RE = re.compile(r"[ \t]+#+[ \t]*$")
_EXPLICIT_ID_RE = re.compile(r"^(?P<text>.*?)\s*\{#(?P<id>[A-Za-z0-9_.:-]+)\}\s*$")
_LINK_RE = re.compile(r"!?\[[^\]]*\]\((?P<target><[^>]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1")
_MD_LINK_IN_TEXT_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_EXTERNAL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def slugify(text: str) -> str:
    """GitHub 风格锚点：取纯文本、小写、去标点、空白转连字符。"""
    text = _INLINE_CODE_RE.sub(lambda m: m.group(2), text)
    text = _MD_LINK_IN_TEXT_RE.sub(r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text.strip())


def is_external(target: str) -> bool:
    """http://、mailto:、//cdn... 等外部目标不参与闭包校验。"""
    return bool(_EXTERNAL_RE.match(target)) or target.startswith("//")


@dataclass
class Heading:
    line: int
    level: int
    text: str
    anchor: str
    explicit: bool


@dataclass
class Link:
    line: int
    column: int  # 1 起始，指向 target 在行内的起始位置
    target: str
    raw: str


@dataclass
class ParsedDoc:
    path: str
    headings: list[Heading] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    code_lines: set[int] = field(default_factory=set)

    @property
    def anchor_table(self) -> dict[str, list[Heading]]:
        table: dict[str, list[Heading]] = {}
        for h in self.headings:
            table.setdefault(h.anchor, []).append(h)
        return table


def parse_markdown(path: str, content: str) -> ParsedDoc:
    doc = ParsedDoc(path=path)
    in_fence = False
    fence_char = ""
    fence_len = 0

    for lineno, line in enumerate(content.splitlines(), 1):
        fence = _FENCE_RE.match(line)

        if in_fence:
            doc.code_lines.add(lineno)
            if (
                fence
                and fence.group("fence")[0] == fence_char
                and len(fence.group("fence")) >= fence_len
                and fence.group("rest").strip() == ""
            ):
                in_fence = False
            continue

        if fence and not (fence.group("fence")[0] == "`" and "`" in fence.group("rest")):
            in_fence = True
            fence_char = fence.group("fence")[0]
            fence_len = len(fence.group("fence"))
            doc.code_lines.add(lineno)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            text = _TRAILING_HASHES_RE.sub("", heading.group("text"))
            explicit = _EXPLICIT_ID_RE.match(text)
            if explicit:
                anchor = explicit.group("id")
                text = explicit.group("text").strip()
            else:
                anchor = slugify(text)
            doc.headings.append(
                Heading(
                    line=lineno,
                    level=len(heading.group("hashes")),
                    text=text,
                    anchor=anchor,
                    explicit=bool(explicit),
                )
            )
            continue

        # 行内代码中的 [x](y) 不是链接；用等长空白遮蔽以保持列号不变
        masked = _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), line)
        for m in _LINK_RE.finditer(masked):
            target = m.group("target")
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            doc.links.append(
                Link(line=lineno, column=m.start("target") + 1, target=target, raw=m.group(0))
            )

    return doc
