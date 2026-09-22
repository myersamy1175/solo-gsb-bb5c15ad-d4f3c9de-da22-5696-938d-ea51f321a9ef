import os

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


README = """# Guide

See [setup](docs/setup.md#install) and [missing](Ghost.md).

```markdown
# Not A Heading
[fake](fake.md#nope)
```

Inline code `[not a link](no.md)` either.

## Overview
"""

SETUP = """# Setup

## Install

Back to [guide](../README.md#giude).

## Install
"""

GHOST = "# Ghost\n\nNothing links here correctly yet.\n"


def import_docs(client, docs, name="test-set"):
    resp = client.post(
        "/api/v1/documents/import",
        json={"name": name, "documents": [{"path": p, "content": c} for p, c in docs.items()]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["set_id"]


def run_validation(client, set_id):
    resp = client.post("/api/v1/validations", json={"set_id": set_id})
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_id"]
    resp = client.get(f"/api/v1/validations/{task_id}")
    assert resp.json()["status"] == "done", resp.json()
    return resp.json()


@pytest.fixture()
def validated(client):
    set_id = import_docs(
        client, {"README.md": README, "docs/setup.md": SETUP, "docs/ghost.md": GHOST}
    )
    return client, set_id, run_validation(client, set_id)


def codes(result):
    return [d["code"] for d in result["diagnostics"]]


def test_import_and_list(client):
    set_id = import_docs(client, {"a.md": "# A\n", "docs/b.md": "# B\n"})
    resp = client.get(f"/api/v1/document-sets/{set_id}/documents")
    assert resp.status_code == 200
    assert [d["path"] for d in resp.json()["documents"]] == ["a.md", "docs/b.md"]


def test_import_rejects_bad_paths(client):
    for bad in ("../evil.md", "/abs.md", "a/../../b.md"):
        resp = client.post(
            "/api/v1/documents/import",
            json={"documents": [{"path": bad, "content": "# x\n"}]},
        )
        assert resp.status_code == 422, bad
    resp = client.post(
        "/api/v1/documents/import",
        json={
            "documents": [
                {"path": "a.md", "content": "1"},
                {"path": "./a.md", "content": "2"},
            ]
        },
    )
    assert resp.status_code == 422  # 归一化后重复


def test_broken_link_with_suggestion(validated):
    _, _, result = validated
    broken = [d for d in result["diagnostics"] if d["code"] == "broken_link"]
    assert len(broken) == 1
    assert broken[0]["path"] == "README.md"
    assert broken[0]["line"] == 3
    assert broken[0]["severity"] == "error"
    assert broken[0]["suggestion"] == "docs/ghost.md"  # 同名文件（忽略大小写）


def test_broken_anchor_with_suggestion(validated):
    _, _, result = validated
    anchors = [d for d in result["diagnostics"] if d["code"] == "broken_anchor"]
    assert len(anchors) == 1
    assert anchors[0]["path"] == "docs/setup.md"
    assert anchors[0]["target"] == "../README.md#giude"
    assert anchors[0]["suggestion"] == "guide"


def test_anchor_collision(validated):
    _, _, result = validated
    collisions = [d for d in result["diagnostics"] if d["code"] == "anchor_collision"]
    assert len(collisions) == 1
    assert collisions[0]["path"] == "docs/setup.md"
    assert collisions[0]["line"] == 7  # 第二个 ## Install


def test_cycle_detected(validated):
    _, _, result = validated
    cycles = [d for d in result["diagnostics"] if d["code"] == "cycle"]
    assert len(cycles) == 1
    assert "README.md" in cycles[0]["message"]
    assert "docs/setup.md" in cycles[0]["message"]


def test_code_block_and_inline_code_ignored(validated):
    _, _, result = validated
    targets = [d["target"] or "" for d in result["diagnostics"]]
    assert not any("fake.md" in t for t in targets)  # 代码块中的链接不校验
    assert not any("no.md" in t for t in targets)  # 行内代码中的链接不校验
    # 代码块中的 "# Not A Heading" 不产生锚点，README 锚点只有 guide / overview
    summary = result["summary"]
    assert summary["documents"] == 3
    assert summary["headings"] == 6  # 代码块中的 "# Not A Heading" 不计入


def test_fix_preview(validated):
    client, _, result = validated
    resp = client.post("/api/v1/fix-previews", json={"task_id": result["task_id"]})
    assert resp.status_code == 200, resp.text
    preview = resp.json()

    by_path = {d["path"]: d for d in preview["documents"]}
    assert set(by_path) == {"README.md", "docs/setup.md"}

    # 断链重写为候选文件
    readme_fixed = by_path["README.md"]["fixed_content"]
    assert "[missing](docs/ghost.md)" in readme_fixed

    # 断锚重写 + 碰撞标题追加显式锚点
    setup_fixed = by_path["docs/setup.md"]["fixed_content"]
    assert "[guide](../README.md#guide)" in setup_fixed
    assert "## Install {#install-2}" in setup_fixed

    # 代码块与正文保持不变
    assert "# Not A Heading" in readme_fixed
    assert "[fake](fake.md#nope)" in readme_fixed
    assert "`[not a link](no.md)`" in readme_fixed
    assert readme_fixed.count("# Guide") == 1

    # 循环引用不可自动修复
    assert any(d["code"] == "cycle" for d in preview["unfixable"])

    # 预览不写回：重新校验仍得到相同诊断数
    again = run_validation(client, result["set_id"])
    assert len(again["diagnostics"]) == len(result["diagnostics"])


def test_fixed_docs_pass_validation(client):
    """应用预览结果后重新导入，诊断应清零（除无候选的项）。"""
    set_id = import_docs(client, {"README.md": README, "docs/setup.md": SETUP, "docs/ghost.md": GHOST})
    result = run_validation(client, set_id)
    preview = client.post("/api/v1/fix-previews", json={"task_id": result["task_id"]}).json()

    new_docs = {"README.md": README, "docs/setup.md": SETUP, "docs/ghost.md": GHOST}
    for doc in preview["documents"]:
        new_docs[doc["path"]] = doc["fixed_content"]
    new_set = import_docs(client, new_docs, name="fixed")
    new_result = run_validation(client, new_set)
    remaining = [d["code"] for d in new_result["diagnostics"]]
    assert "broken_link" not in remaining
    assert "broken_anchor" not in remaining
    assert "anchor_collision" not in remaining
    assert "cycle" in remaining  # 循环引用需人工处理


def test_validation_not_found_and_not_done(client):
    assert client.get("/api/v1/validations/nope").status_code == 404
    resp = client.post("/api/v1/fix-previews", json={"task_id": "nope"})
    assert resp.status_code == 404
    resp = client.post("/api/v1/validations", json={"set_id": 999})
    assert resp.status_code == 404


def test_anchor_only_link(client):
    docs = {
        "a.md": "# Title\n\nJump to [missing](#nope) and [ok](#title).\n",
    }
    set_id = import_docs(client, docs)
    result = run_validation(client, set_id)
    anchors = [d for d in result["diagnostics"] if d["code"] == "broken_anchor"]
    assert len(anchors) == 1
    assert anchors[0]["target"] == "#nope"
