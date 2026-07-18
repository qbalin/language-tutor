"""Shared helpers for the language-tutor scripts.

All scripts print a single JSON document to stdout so an LLM (or any
program) can parse the result without scraping prose.
"""
import json
import sqlite3
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANGUAGES = ROOT / "languages"

DICT_DB = "dictionary.db"
GRAMMAR_DB = "grammar.db"
CARDS_DB = "cards.db"


def lang_dir(lang: str) -> Path:
    return LANGUAGES / lang.strip().lower().replace(" ", "_")


def db_path(lang: str, name: str) -> Path:
    return lang_dir(lang) / name


def open_db(lang: str, name: str, must_exist: bool = True) -> sqlite3.Connection:
    path = db_path(lang, name)
    if must_exist and not path.exists():
        die(f"{name} not found for language '{lang}' (expected {path}). "
            f"Run the matching ingest script first.")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def die(msg: str, **extra) -> None:
    print(json.dumps({"error": msg, **extra}, ensure_ascii=False), file=sys.stdout)
    sys.exit(1)


def normalize(word: str, lang: str = "") -> str:
    """Lowercase, strip diacritics; language-specific letter folding.

    For Latin, macrons/breves are editorial and u/v, i/j are spelling
    variants, so both sides of a lookup are folded the same way.
    """
    s = unicodedata.normalize("NFD", word.strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    if lang.strip().lower() in ("latin", "la"):
        s = s.replace("j", "i").replace("v", "u")
    return s


def fts_quote(query: str) -> str:
    """Escape a free-text query for FTS5 MATCH: quote each token."""
    tokens = [t.replace('"', '""') for t in query.split()]
    return " ".join(f'"{t}"' for t in tokens if t)
