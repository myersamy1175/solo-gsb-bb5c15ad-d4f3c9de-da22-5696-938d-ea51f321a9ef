"""End-to-end and unit tests for the reference-closure validator."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.analyzer import analyze
from app.main import app
from app.parser import parse_markdown

CORPUS = {
    "index.md": (
        "# Home\n"
        "\n"
        "See the [guide](docs/guide.md#install) and [missing](docs/nope.md).\n"
        "\n"
        "```\n"
        "[fake](ghost.md)\n"
        "# not a heading\n"
        "```\n"
        "\n"
        "Ref [api](docs/api.md).\n"
    ),
    "docs/guide.md": (
        "# Guide\n"
        "\n"
        "## Install\n"
        "\n"
        "Back to [home](../index.md#home).\n"
        "\n"
        "## Install\n"
        "\n"
        "See [old](../deep.md).\n"
        "\n"
        "Jump to [top](#guide).\n"
    ),
    "docs/api.md": (
        "# API\n"
        "\n"
        "Related: [setup](guide.md#instal) and [home](../index.md#home).\n"
        "\n"
        "[ref]: guide.md\n"
        "Follow [the guide][ref].\n"
    ),
    "docs/old/deep.md": (
        "# Deep\n"
        "\n"
        "Up: [guide](../guide.md).\n"
        "\n"
        "Empty [e](#).\n"
    ),
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def import_and_validate(client, corpus):
    r = client.post(
        "/api/imports",
        json={"documents": [{"path": p, "content": c} for p, c in corpus.items()]},
    )
    assert r.status_code == 201, r.text
    import_id = r.json()["import_id"]
    r = client.post("/api/validations", json={"import_id": import_id})
    assert r.status_code == 202, r.text
    vid = r.json()["validation_id"]
    r = client.get(f"/api/validations/{vid}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done", body
    return import_id, vid, body


# ------------------------------------------------------------------ parser ---

def test_parser_ignores_fenced_and_indented_code():
    doc = parse_markdown(
        "a.md",
        "# Real\n"
        "\n"
        "```\n"
        "# fake heading\n"
        "[x](ghost.md)\n"
        "```\n"
        "\n"
        "    [indented](nope.md)\n"
        "\n"
        "~~~text\n"
        "[tilde](nope2.md)\n"
        "~~~\n"
        "\n"
        "Inline `[not](a-link.md)` code and [real](ok.md).\n",
    )
    assert [h.text for h in doc.headings] == ["Real"]
    assert [l.raw_target for l in doc.links] == ["ok.md"]
    assert {3, 4, 5, 6, 8, 10, 11, 12} <= doc.code_lines  # both fences + indented


def test_parser_reference_links_and_explicit_anchors():
    doc = parse_markdown(
        "a.md",
        "# Title {#custom}\n"
        "\n"
        "[used][u] and [missing][m].\n"
        "\n"
        "[u]: target.md\n"
        "[unused]: nowhere.md\n",
    )
    assert doc.headings[0].explicit_id == "custom"
    targets = sorted(l.raw_target for l in doc.links)
    assert targets == ["target.md"]  # unused definition creates no edge


def test_parser_angle_bracket_and_title_destinations():
    doc = parse_markdown("a.md", "[x](<my file.md>) and [y](b.md \"title\").\n")
    assert [l.raw_target for l in doc.links] == ["my file.md", "b.md"]


# ------------------------------------------------------------------ analyze --

def test_analyze_detects_all_diagnostic_types():
    analysis = analyze(CORPUS)
    diags = analysis.diagnostics
    by_type = {}
    for d in diags:
        by_type.setdefault(d.type, []).append(d)

    broken_links = by_type["broken_link"]
    assert {(d.path, d.line) for d in broken_links} == {("index.md", 3), ("docs/guide.md", 9)}

    broken_anchors = by_type["broken_anchor"]
    assert {(d.path, d.line) for d in broken_anchors} == {
        ("docs/api.md", 3),
        ("docs/old/deep.md", 5),
    }

    collisions = by_type["anchor_collision"]
    assert len(collisions) == 1
    assert collisions[0].path == "docs/guide.md"
    assert collisions[0].line == 7
    assert collisions[0].details["assigned"] == "install-1"

    cycles = by_type["cycle"]
    assert {d.path for d in cycles} == {"index.md", "docs/guide.md", "docs/api.md"}

    # links inside the fenced code block were ignored
    assert all("ghost.md" not in str(d.details) for d in diags)
    assert analysis.summary["errors"] == 4
    assert analysis.summary["warnings"] == 4


# ---------------------------------------------------------------------- api --

def test_import_rejects_bad_paths(client):
    r = client.post("/api/imports", json={"documents": [{"path": "/abs.md", "content": "x"}]})
    assert r.status_code == 422
    r = client.post("/api/imports", json={"documents": [{"path": "../esc.md", "content": "x"}]})
    assert r.status_code == 422
    r = client.post(
        "/api/imports",
        json={"documents": [{"path": "a.md", "content": "x"}, {"path": "./a.md", "content": "y"}]},
    )
    assert r.status_code == 422  # duplicate after normalization


def test_import_and_get_documents(client):
    r = client.post(
        "/api/imports",
        json={"documents": [{"path": p, "content": c} for p, c in CORPUS.items()]},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["doc_count"] == 4
    import_id = body["import_id"]

    r = client.get(f"/api/imports/{import_id}")
    assert r.status_code == 200
    assert r.json()["paths"] == sorted(CORPUS)

    r = client.get(f"/api/imports/{import_id}/documents/docs/guide.md")
    assert r.status_code == 200
    assert r.json()["content"] == CORPUS["docs/guide.md"]

    assert client.get(f"/api/imports/{import_id}/documents/nope.md").status_code == 404
    assert client.get("/api/imports/does-not-exist").status_code == 404


def test_validation_flow_and_filters(client):
    _, vid, body = import_and_validate(client, CORPUS)
    assert body["summary"]["by_type"] == {
        "broken_link": 2,
        "broken_anchor": 2,
        "anchor_collision": 1,
        "cycle": 3,
    }

    r = client.get(f"/api/validations/{vid}", params={"type": "broken_link"})
    assert {d["path"] for d in r.json()["diagnostics"]} == {"index.md", "docs/guide.md"}

    r = client.get(f"/api/validations/{vid}", params={"path": "docs/api.md"})
    assert {d["type"] for d in r.json()["diagnostics"]} == {"broken_anchor", "cycle"}

    assert client.get("/api/validations/nope").status_code == 404
    assert client.post("/api/validations", json={"import_id": "nope"}).status_code == 404


def test_fix_preview_rewrites_only_anchors_and_references(client):
    _, vid, _ = import_and_validate(client, CORPUS)
    r = client.post(f"/api/validations/{vid}/fix-preview")
    assert r.status_code == 200, r.text
    preview = r.json()

    fixed = {d["path"]: d for d in preview["fixed_documents"]}
    assert set(fixed) == {"docs/guide.md", "docs/api.md"}

    # anchor collision: duplicate heading pinned with explicit anchor
    guide_lines = fixed["docs/guide.md"]["fixed_content"].split("\n")
    assert guide_lines[6] == "## Install  {#install-1}"
    # broken link: unique-basename rewrite, relative to the source document
    assert guide_lines[8] == "See [old](old/deep.md)."
    # code blocks and body text untouched
    assert fixed["docs/guide.md"]["fixed_content"].split("\n")[0] == "# Guide"

    # broken anchor: closest-match repair
    api_lines = fixed["docs/api.md"]["fixed_content"].split("\n")
    assert "guide.md#install" in api_lines[2]

    # unfixed: no-candidate link, empty anchor, and the cycles
    unfixed = preview["unfixed"]
    assert len(unfixed) == 5
    reasons = {(u["type"], u["path"]) for u in unfixed}
    assert ("broken_link", "index.md") in reasons
    assert ("broken_anchor", "docs/old/deep.md") in reasons
    assert sum(1 for u in unfixed if u["type"] == "cycle") == 3

    # every change is confined to a heading or a link destination
    for doc in preview["fixed_documents"]:
        for change in doc["changes"]:
            assert change["kind"] in {"heading-anchor", "link-target"}
            assert change["before"] != change["after"]


def test_fix_preview_result_revalidates_clean(client):
    import_id, vid, _ = import_and_validate(client, CORPUS)
    preview = client.post(f"/api/validations/{vid}/fix-preview").json()

    fixed_corpus = dict(CORPUS)
    for doc in preview["fixed_documents"]:
        fixed_corpus[doc["path"]] = doc["fixed_content"]

    reanalysis = analyze(fixed_corpus)
    remaining = {(d.type, d.path) for d in reanalysis.diagnostics}
    # fixable issues are gone; only unfixed ones remain
    assert ("anchor_collision", "docs/guide.md") not in remaining
    assert ("broken_anchor", "docs/api.md") not in remaining
    assert ("broken_link", "docs/guide.md") not in remaining
    assert ("broken_link", "index.md") in remaining
    assert ("cycle", "index.md") in remaining


def test_fix_preview_requires_done_status(client, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app import db

    import_id = db.create_import({"a.md": "# A\n"})
    vid = db.create_validation(import_id)  # stays 'pending'
    r = client.post(f"/api/validations/{vid}/fix-preview")
    assert r.status_code == 409


def test_crlf_preserved_in_fix_preview(client):
    corpus = {"a.md": "# T\r\n\r\n## Dup\r\n\r\n## Dup\r\n"}
    _, vid, _ = import_and_validate(client, corpus)
    preview = client.post(f"/api/validations/{vid}/fix-preview").json()
    fixed = preview["fixed_documents"][0]["fixed_content"]
    assert "\r\n" in fixed
    assert fixed.split("\r\n")[4] == "## Dup  {#dup-1}"
