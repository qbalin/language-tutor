"""Session bootstrap: report what exists for a language and what is missing.

  python scripts/session.py start --lang latin
  python scripts/session.py languages
"""
import argparse
import sqlite3

from common import CARDS_DB, DICT_DB, GRAMMAR_DB, LANGUAGES, db_path, lang_dir, out


def dict_status(lang):
    path = db_path(lang, DICT_DB)
    if not path.exists():
        return {"ok": False}
    conn = sqlite3.connect(path)
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    conn.close()
    return {"ok": True, "entries": int(meta.get("entries", 0)),
            "forms": int(meta.get("forms", 0))}


def grammar_status(lang):
    path = db_path(lang, GRAMMAR_DB)
    src = lang_dir(lang) / "grammar"
    files = sorted(p.name for p in src.iterdir() if p.is_file()) if src.exists() else []
    if not path.exists():
        return {"ok": False, "source_files": files}
    conn = sqlite3.connect(path)
    n = conn.execute("SELECT count(*) FROM sections").fetchone()[0]
    conn.close()
    return {"ok": True, "sections": n, "source_files": files}


def cards_status(lang):
    path = db_path(lang, CARDS_DB)
    if not path.exists():
        return {"cards": 0, "due_now": 0, "inbox_open": 0}
    import json
    from datetime import datetime, timezone
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()
    total = conn.execute("SELECT count(*) c FROM cards").fetchone()["c"]
    due = sum(1 for r in conn.execute("SELECT fsrs FROM cards WHERE suspended=0")
              if json.loads(r["fsrs"])["due"] <= now)
    inbox = conn.execute(
        "SELECT count(*) c FROM inbox WHERE status='open'").fetchone()["c"]
    conn.close()
    return {"cards": total, "due_now": due, "inbox_open": inbox}


def cmd_start(args):
    lang = args.lang
    (lang_dir(lang) / "grammar").mkdir(parents=True, exist_ok=True)
    d, g, c = dict_status(lang), grammar_status(lang), cards_status(lang)
    steps = []
    if not d["ok"]:
        steps.append(
            f"No dictionary. Build it (needs network, can take a while): "
            f"python scripts/ingest_dictionary.py --lang {lang}")
    if not g["ok"]:
        if g["source_files"]:
            steps.append(
                f"Grammar files present but not indexed. Run: "
                f"python scripts/ingest_grammar.py --lang {lang}")
        else:
            steps.append(
                f"No grammar. Ask the student to place a grammar reference "
                f"(PDF/EPUB/HTML/Markdown/text) in {lang_dir(lang) / 'grammar'} "
                f"then run: python scripts/ingest_grammar.py --lang {lang}")
    if d["ok"] and g["ok"] and c["cards"] == 0:
        steps.append("No cards yet. Pick a first topic with `grammar.py toc` "
                     "and create a card.")
    out({"lang": lang, "dictionary": d, "grammar": g, "deck": c,
         "ready": d["ok"] and g["ok"],
         "next_steps": steps or ["Start reviewing: python scripts/cards.py due "
                                 f"--lang {lang}"]})


def cmd_languages(args):
    langs = []
    if LANGUAGES.exists():
        for p in sorted(LANGUAGES.iterdir()):
            if p.is_dir():
                langs.append({"lang": p.name,
                              "dictionary": db_path(p.name, DICT_DB).exists(),
                              "grammar": db_path(p.name, GRAMMAR_DB).exists(),
                              **cards_status(p.name)})
    out({"languages": langs,
         "note": "ask the student which language they want, including ones "
                 "not listed here (new languages get set up on first start)"})


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("--lang", required=True)
    sub.add_parser("languages")
    args = ap.parse_args()
    {"start": cmd_start, "languages": cmd_languages}[args.cmd](args)


if __name__ == "__main__":
    main()
