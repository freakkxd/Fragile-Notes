from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fragilenotes.core.media import load_media_lite, media_stats
from fragilenotes.vault import parse_frontmatter, serialize_frontmatter, write_md


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="fn-test-"))


def test_media_parse() -> None:
    tmp_path = _tmp()
    corpus = tmp_path / "04 FreakyWiki/_System/FreakyDB/Data"
    corpus.mkdir(parents=True)
    lines = [
        {"t": "anime", "o": "A.md", "title": "Anime One", "status": "completed", "rating": 9, "year": 2019, "cover_url": "http://x/a.jpg"},
        {"t": "games", "o": "G.md", "title": "Game Two", "status": "planned", "rating": 0, "year": None},
        "not json",
        {"t": "music", "o": "M.md", "title": "Music Three"},
    ]
    (corpus / "media_lite_index.jsonl").write_text(
        "\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines),
        encoding="utf-8",
    )
    recs = load_media_lite(tmp_path)
    assert len(recs) == 3
    assert recs[0].type == "anime"
    assert recs[0].title == "Anime One"
    assert recs[0].status_label == "пройдено"
    assert recs[1].type == "game"  # alias games -> game
    assert recs[2].year is None


def test_media_bad_record_skipped() -> None:
    """Одна запись с битым int не должна ронять весь корпус."""
    tmp_path = _tmp()
    corpus = tmp_path / "04 FreakyWiki/_System/FreakyDB/Data"
    corpus.mkdir(parents=True)
    lines = [
        {"t": "anime", "o": "A.md", "title": "Good", "year": 2020},
        {"t": "anime", "o": "B.md", "title": "Bad", "year": "не число"},
        {"t": "anime", "o": "C.md", "title": "Also", "rating": "abc"},
    ]
    (corpus / "media_lite_index.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines), encoding="utf-8"
    )
    recs = load_media_lite(tmp_path)
    assert [r.title for r in recs] == ["Good"]


def test_media_stats_missing() -> None:
    assert media_stats(_tmp()) == {}


def test_frontmatter_roundtrip() -> None:
    fm, body = parse_frontmatter(
        "---\nstatus: active\npriority: critical\ndue_date: 2026-08-12\n---\n# Title\nbody"
    )
    assert fm["status"] == "active"
    assert fm["due_date"] == "2026-08-12"
    assert body.startswith("# Title")
    out = serialize_frontmatter(fm) + body
    fm2, body2 = parse_frontmatter(out)
    assert fm2 == fm
    assert body2 == body


def test_write_md() -> None:
    p = _tmp() / "t.md"
    write_md(p, {"a": 1, "tags": ["x", "y"]}, "text")
    fm, body = parse_frontmatter(p.read_text(encoding="utf-8"))
    assert fm == {"a": 1, "tags": ["x", "y"]}
    assert body == "text"
