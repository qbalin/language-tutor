"""Session bootstrap and orchestration: what exists, what is missing, and —
so the model never has to hold the whole procedure in its head — what to do
next at any point of a tutoring session.

  python scripts/session.py languages
  python scripts/session.py start --lang latin
  python scripts/session.py next --lang latin        # the session state machine
  python scripts/session.py next-topic --lang latin  # earliest uncovered section
"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone

from checkpoint import auto_save
from common import (CARDS_DB, DICT_DB, GRAMMAR_DB, LANGUAGES,
                    JsonArgumentParser, db_path, lang_dir, normalize,
                    open_db, out)


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


def open_cards_db(lang):
    path = db_path(lang, CARDS_DB)
    if not path.exists():
        return None
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def cards_status(lang):
    conn = open_cards_db(lang)
    if conn is None:
        return {"cards": 0, "due_now": 0, "inbox_open": 0}
    now = datetime.now(timezone.utc).isoformat()
    total = conn.execute("SELECT count(*) c FROM cards").fetchone()["c"]
    due = sum(1 for r in conn.execute("SELECT fsrs FROM cards WHERE suspended=0")
              if json.loads(r["fsrs"])["due"] <= now)
    inbox = conn.execute(
        "SELECT count(*) c FROM inbox WHERE status='open'").fetchone()["c"]
    conn.close()
    return {"cards": total, "due_now": due, "inbox_open": inbox}


def first_due_card(lang):
    conn = open_cards_db(lang)
    if conn is None:
        return None
    now = datetime.now(timezone.utc).isoformat()
    card = None
    for r in conn.execute("SELECT * FROM cards WHERE suspended = 0 "
                          "ORDER BY json_extract(fsrs, '$.due')"):
        if json.loads(r["fsrs"])["due"] <= now:
            mistakes = conn.execute(
                "SELECT ts, produced, expected, note FROM mistakes "
                "WHERE card_id = ? ORDER BY id DESC LIMIT 3", (r["id"],)).fetchall()
            card = {"id": r["id"], "concept": r["concept"],
                    "grammar_refs": r["grammar_refs"],
                    "recent_mistakes": [
                        {k: m[k] for k in ("ts", "produced", "expected", "note")
                         if m[k]} for m in mistakes]}
            break
    conn.close()
    return card


def open_inbox_items(lang):
    conn = open_cards_db(lang)
    if conn is None:
        return []
    rows = conn.execute(
        "SELECT * FROM inbox WHERE status = 'open' ORDER BY id").fetchall()
    conn.close()
    return [{"id": r["id"], "produced": r["produced"], "note": r["note"],
             "concept_hint": r["concept_hint"]} for r in rows]


def covered_refs(lang):
    conn = open_cards_db(lang)
    if conn is None:
        return set()
    refs = set()
    for r in conn.execute("SELECT grammar_refs FROM cards"):
        for ref in (r["grammar_refs"] or "").split(","):
            if ref.strip():
                refs.add(ref.strip())
    conn.close()
    return refs


# Titles that are book apparatus, not grammar topics (matched against the
# diacritic-stripped lowercase title, so e.g. INTRŌDVCTIŌ is caught).
FRONT_MATTER = ("contents", "foreword", "preface", "introd", "acknowledg",
                "copyright", "dedication", "index", "bibliograph", "glossar",
                "about the", "maps", "illustration", "abbreviation", "edition",
                "title page", "epigraph", "also by", "credits", "endorsement")

MIN_TOPIC_CHARS = 500  # anything shorter is book apparatus, not a lesson


def uncovered_sections(lang, limit=4):
    conn = open_db(lang, GRAMMAR_DB)
    rows = conn.execute(
        "SELECT ref, title, length(content) AS n FROM sections ORDER BY id"
    ).fetchall()
    conn.close()
    covered = covered_refs(lang)
    # A ref like "s9" covers its "s9/2" subsections; any covered ref covers
    # every section sharing its title (chapters are split across subsections).
    covered_titles = {r["title"] for r in rows
                      if r["ref"] in covered or r["ref"].split("/")[0] in covered}
    length_by_title = {}
    for r in rows:
        length_by_title[r["title"]] = length_by_title.get(r["title"], 0) + r["n"]
    picks, seen = [], set()
    for r in rows:
        title = r["title"]
        if title in seen or title in covered_titles:
            continue
        seen.add(title)
        if (any(k in normalize(title) for k in FRONT_MATTER)
                or length_by_title[title] < MIN_TOPIC_CHARS):
            continue
        picks.append({"ref": r["ref"], "title": title})
        if len(picks) >= limit:
            break
    return picks, len(rows)


def setup_steps(lang, d, g):
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
    return steps


def cmd_start(args):
    lang = args.lang
    (lang_dir(lang) / "grammar").mkdir(parents=True, exist_ok=True)
    d, g, c = dict_status(lang), grammar_status(lang), cards_status(lang)
    steps = setup_steps(lang, d, g)
    if d["ok"] and g["ok"] and c["cards"] == 0:
        steps.append("No cards yet. Pick a first topic with `grammar.py toc` "
                     "and create a card.")
    out({"lang": lang, "dictionary": d, "grammar": g, "deck": c,
         "ready": d["ok"] and g["ok"],
         "next_steps": steps or [f"Drive the session: python scripts/session.py "
                                 f"next --lang {lang}"]})


def next_topic_payload(lang):
    picks, total = uncovered_sections(lang)
    if not picks:
        return {"next_topic": None,
                "note": f"all {total} grammar sections are covered by cards; "
                        "ask the student what they want to work on"}
    first, rest = picks[0], picks[1:]
    return {"next_topic": first, "alternatives": rest,
            "note": "propose this topic to the student; if they agree run: "
                    f"./ll cards create --lang {lang} --concept "
                    f"\"{first['title']}\" --refs \"{first['ref']}\", teach it "
                    f"from ./ll grammar show {first['ref']} --lang {lang}, "
                    "then exercise it like any due card. If next_topic is "
                    "still book front matter rather than a grammar topic, use "
                    "the first alternative that is a real topic instead"}


def cmd_next_topic(args):
    out({"lang": args.lang, **next_topic_payload(args.lang)})


def cmd_next(args):
    lang = args.lang
    (lang_dir(lang) / "grammar").mkdir(parents=True, exist_ok=True)
    d, g = dict_status(lang), grammar_status(lang)
    steps = setup_steps(lang, d, g)
    rerun = f"then run: ./ll session next --lang {lang}"
    if steps:
        out({"lang": lang, "state": "setup", "next_steps": steps,
             "instruction": "the language is not set up yet; complete "
                            f"next_steps (use the setup-language skill), {rerun}"})
        return
    auto_save(lang)  # at most one snapshot per UTC day: the rollback point
    c = cards_status(lang)
    if c["cards"] == 0:
        out({"lang": lang, "state": "placement",
             "instruction":
                 "first session, empty deck: place the student before creating "
                 "cards. Pick 5-8 topics spanning ./ll grammar toc --lang "
                 f"{lang} from first chapter to last; write one English "
                 "sentence to translate per topic, easiest first; present them "
                 "all at once and do not reveal answers; verify every answer "
                 "with dict/grammar commands; report placement item by item "
                 "with full corrections; create the first card on the earliest "
                 "concept the student got WRONG: ./ll cards create --lang "
                 f"{lang} --concept \"...\" --refs \"...\" (vocabulary slips "
                 f"go to the inbox, not to cards); {rerun}"})
        return
    card = first_due_card(lang)
    if card:
        out({"lang": lang, "state": "review", "due_count": c["due_now"],
             "card": card,
             "instruction":
                 "review this card. Write a set of exercises on its concept — "
                 "exactly 2 for a simple rule, 3-4 for a complex one, never "
                 "fewer than 2 — one sentence each (English to translate, or a "
                 "prompt in the language), probing recent_mistakes if any; "
                 "number them, present all at once, do not reveal answers, and "
                 "wait for the student. Verify their answers with ./ll dict "
                 f"lookup <word> <word> ... --lang {lang} (batch the words), "
                 "./ll dict inflections and ./ll grammar search/show — never "
                 "from memory. Grade once for the whole set (1 failed / 2 "
                 "faltered / 3 correct / 4 effortless): write "
                 '[{"prompt": "...", "answer": "..."}, ...] with verbatim '
                 "answers to a JSON file and run ./ll cards grade "
                 f"{card['id']} <rating> --lang {lang} --pairs-file <file> "
                 "(--note on failure), then follow the note in its output. "
                 "Unrelated mistakes: ./ll cards inbox add --lang "
                 f"{lang} --produced \"...\" --note \"...\"; {rerun}"})
        return
    items = open_inbox_items(lang)
    if items:
        it = items[0]
        resolve = f"./ll cards inbox resolve {it['id']} --lang {lang}"
        out({"lang": lang, "state": "inbox", "open_count": len(items),
             "item": it,
             "instruction":
                 "deck done for today; triage this inbox item with the "
                 f"student (existing cards: ./ll cards list --lang {lang}) "
                 f"and run exactly one of: {resolve} --card <id> --rating 1; "
                 f"{resolve} --create-concept \"...\" --refs \"...\"; "
                 f"{resolve} --dismiss; {rerun}"})
        return
    topic = next_topic_payload(lang)
    if topic["next_topic"]:
        out({"lang": lang, "state": "new_topic", **topic,
             "instruction":
                 "deck done and inbox empty; tell the student, then offer "
                 "next_topic (or an alternative) as described in note. If they "
                 f"agree, after creating the card {rerun} — it is due "
                 "immediately. If they decline, run ./ll checkpoint sync to "
                 "back up progress; the session is over."})
        return
    out({"lang": lang, "state": "done",
         "instruction": "deck done, inbox empty, every grammar section already "
                        "has a card; run ./ll checkpoint sync to back up "
                        "progress, then tell the student the session is over "
                        "and ask what they would like to do"})


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
    ap = JsonArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("start", "next", "next-topic"):
        sub.add_parser(name).add_argument("--lang", required=True)
    sub.add_parser("languages")
    args = ap.parse_args()
    {"start": cmd_start, "next": cmd_next, "next-topic": cmd_next_topic,
     "languages": cmd_languages}[args.cmd](args)


if __name__ == "__main__":
    main()
