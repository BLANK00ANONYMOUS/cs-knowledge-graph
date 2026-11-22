import json
import sqlite3

import pytest

import kg
from conftest import note


def test_rate_note_keeps_frontmatter_of_bom_note(tmp_path):  # B1
    p = note(tmp_path / "x.md", "title: X\ntype: concept\nprereqs: [a]\nmastery: 1", bom=True)
    n = kg.parse_note(p)
    kg.rate_note(n, 3)
    text = p.read_text(encoding="utf-8")
    assert not text.startswith("﻿")
    again = kg.parse_note(p)
    assert again.title == "X" and again.mastery == 3 and again.refs("prereqs") == ["a"]
    assert text.count("---\n") == 2   # no second frontmatter block


def test_set_frontmatter_field_refuses_to_bury_malformed_frontmatter():  # B1
    with pytest.raises(ValueError):
        kg.set_frontmatter_field("---\ntitle: [unclosed\nno end marker", "mastery", "2")
    assert kg.set_frontmatter_field("plain body", "mastery", "2").startswith("---\nmastery: 2\n---\n")


def test_rate_without_stamp_leaves_last_reviewed(tmp_path):  # B21
    p = note(tmp_path / "x.md", "title: X\nmastery: 1\nlast_reviewed: 2026-01-01")
    kg.rate_note(kg.parse_note(p), 2, stamp=False)
    assert str(kg.parse_note(p).meta["last_reviewed"]) == "2026-01-01"


def test_mastery_is_clamped_and_reviews_dont_crash(tmp_path):  # B5
    p = note(tmp_path / "x.md", "title: X\ntype: concept\nmastery: 7\nlast_reviewed: 2020-01-01")
    n = kg.parse_note(p)
    assert n.mastery == 5
    assert kg.due_reviews({"x": n})


def test_non_mapping_frontmatter_is_a_validation_error(vault):  # B5
    (vault / "cs").mkdir()
    (vault / "cs" / "hr.md").write_text("---\n- just a list\n---\nbody", encoding="utf-8")
    notes, errors = kg.load_vault(vault)
    assert "hr" not in notes and any("hr.md" in e for e in errors)


def test_books_dir_is_relative_to_repo(monkeypatch):  # B4
    monkeypatch.setattr(kg, "load_settings", lambda: {"books_dir": "books"})
    monkeypatch.delenv("KG_BOOKS_DIR", raising=False)
    assert kg.load_config()[1] == kg.ROOT / "books"
    monkeypatch.setattr(kg, "load_settings", lambda: {"books_dir": "/abs/books"})
    assert str(kg.load_config()[1]) == "/abs/books"


def test_write_json_is_atomic_and_read_json_tolerant(tmp_path):  # B5
    p = tmp_path / "h.json"
    kg.write_json(p, [1, 2])
    assert json.loads(p.read_text()) == [1, 2]
    p.write_text("[1, 2", encoding="utf-8")   # truncated by a crash
    assert kg.read_json(p, "dflt") == "dflt"
    assert not list(tmp_path.glob("*.tmp"))


def test_book_slugs_dont_collide(tmp_path):  # B6
    db = sqlite3.connect(tmp_path / "i.db")
    db.execute("CREATE TABLE files(path TEXT PRIMARY KEY, sha1 TEXT, slug TEXT, pages INTEGER)")
    a = kg.book_slug(db, "Book (1).pdf", "Book (1)")
    db.execute("INSERT INTO files VALUES (?,?,?,?)", ("Book (1).pdf", "x", a, 1))
    b = kg.book_slug(db, "Book 1.pdf", "Book 1")
    assert a == "book-book-1" and b != a and b.startswith("book-book-1-")
    assert kg.book_slug(db, "Book (1).pdf", "Book (1)") == a   # stable for the same file


def test_cached_vault_reloads_after_edit(vault):  # B11
    p = note(vault / "cs" / "a.md", "title: A")
    assert kg.load_vault_cached(vault)[0]["a"].title == "A"
    p.write_text("---\ntitle: A2 renamed\n---\n", encoding="utf-8")
    assert kg.load_vault_cached(vault)[0]["a"].title == "A2 renamed"
    first = kg.load_vault_cached(vault)[0]
    first.pop("a")
    assert "a" in kg.load_vault_cached(vault)[0]   # callers get a copy


def test_book_search_skips_note_refresh(tmp_path, monkeypatch):  # B12
    monkeypatch.setattr(kg, "INDEX_PATH", tmp_path / "index.db")
    called = []
    monkeypatch.setattr(kg, "refresh_note_index", lambda db, n: called.append(1))
    kg.search({}, "anything", kind="book")
    assert not called
    kg.search({}, "anything", kind="note")
    assert called
