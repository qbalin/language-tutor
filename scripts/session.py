"""Session bootstrap and orchestration: what exists, what is missing, and —
so the model never has to hold the whole procedure in its head — what to do
next at any point of a tutoring session.

  python scripts/session.py languages
  python scripts/session.py start --lang latin
  python scripts/session.py next --lang latin        # the session state machine
  python scripts/session.py next-topic --lang latin  # level-aware topic pick
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


def card_refs(lang):
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


def cards_db_query(lang, sql, default):
    """Run a query against cards.db, tolerating decks created before the
    settings/known_sections tables existed."""
    conn = open_cards_db(lang)
    if conn is None:
        return default
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        rows = None
    conn.close()
    return rows if rows is not None else default


def known_refs(lang):
    rows = cards_db_query(lang, "SELECT ref FROM known_sections", [])
    return {r["ref"] for r in rows}


def covered_refs(lang):
    return card_refs(lang) | known_refs(lang)


def frontier_ref(lang):
    rows = cards_db_query(
        lang, "SELECT value FROM settings WHERE key = 'frontier'", [])
    return rows[0]["value"] if rows else None


# Titles that are book apparatus, not grammar topics (matched against the
# diacritic-stripped lowercase title, so e.g. INTRŌDVCTIŌ is caught).
FRONT_MATTER = ("contents", "foreword", "preface", "introd", "acknowledg",
                "copyright", "dedication", "index", "bibliograph", "glossar",
                "about the", "maps", "illustration", "abbreviation", "edition",
                "title page", "epigraph", "also by", "credits", "endorsement",
                "appendix", "footnote", "backmatter", "back matter",
                "answer key", "key to", "self-tutorial", "searchable",
                "loci antiq", "loci imm", "vocabvla", "svmmarivm",
                # recurring per-chapter apparatus (Wheelock's section marks;
                # subsections inherit them as a "(REGION)" title suffix, so
                # these also catch e.g. reading passages under LĒCTIŌ)
                "grammatica (", "lectio", "exercitatio", "exercises for",
                "sententiae", "latina est", "scripta in parietibus",
                "praefati", "authors and works", "alphabet", "pronunciation")

# Content openings that betray publishing apparatus (series lists, copyright
# pages, book blurbs) whatever the section is titled.
APPARATUS_CONTENT = ("other books in", "all rights reserved",
                     "library of congress", "first published", "welcome to")

MIN_TOPIC_CHARS = 500  # anything shorter is book apparatus, not a lesson

PLACEMENT_TOPICS = 7      # quiz size: evenly spaced across the book
SPOT_CHECK_MIN_EVERY = 2  # spot-check cadence: at most every 2nd new topic
SPOT_CHECK_MAX_EVERY = 6  # ... and at least every 6th while any remain


def grammar_rows(lang):
    conn = open_db(lang, GRAMMAR_DB)
    rows = conn.execute(
        "SELECT ref, title, source, substr(content, 1, 300) AS head, "
        "length(content) AS n FROM sections ORDER BY id").fetchall()
    conn.close()
    return rows


def topic_inventory(rows, covered):
    """Real grammar topics in book order, one per title, with coverage.

    A ref like "s9" covers its "s9/2" subsections; any covered ref covers
    every section sharing its title (chapters are split across subsections).
    `pos` is the topic's book-order section index, for frontier comparisons.
    """
    covered_titles = {r["title"] for r in rows
                      if r["ref"] in covered or r["ref"].split("/")[0] in covered}
    length_by_title = {}
    for r in rows:
        length_by_title[r["title"]] = length_by_title.get(r["title"], 0) + r["n"]
    topics, seen = [], set()
    for pos, r in enumerate(rows):
        title = r["title"]
        if title in seen:
            continue
        seen.add(title)
        norm = normalize(title)
        if (any(k in norm for k in FRONT_MATTER)
                # a letter or two with no numbering ("G", "Q") is a vocabulary
                # letter heading, not a topic; bare numbering is a real topic
                or (sum(c.isalpha() for c in title) < 3
                    and not any(c.isdigit() for c in title))
                or length_by_title[title] < MIN_TOPIC_CHARS
                # a title-page section repeats the book's own title/series
                or (norm and norm in normalize(r["source"] or ""))
                or any(k in normalize(r["head"] or "")
                       for k in APPARATUS_CONTENT)):
            continue
        topics.append({"ref": r["ref"], "title": title, "pos": pos,
                       "head": r["head"] or "",
                       "covered": title in covered_titles})
    return topics


def display_title(section):
    """A title the model can propose; bare numbering ("9.4.1") gets a
    content snippet so the topic has a name. Works on topic dicts and
    grammar_rows entries alike."""
    title = section["title"]
    if any(c.isalpha() for c in title):
        return title
    return f"{title} — {' '.join((section['head'] or '').split())[:100]}…"


def frontier_pos(lang, rows, topics):
    """Book-order position of the student's level: the stored frontier ref,
    or for decks placed before frontiers existed, the furthest carded section
    that is a real topic (cards may also cite appendices, which would place
    the level at the back of the book). None when there is no signal at all."""
    pos_by_ref = {r["ref"]: i for i, r in enumerate(rows)}
    ref = frontier_ref(lang)
    if ref in pos_by_ref:
        return pos_by_ref[ref]
    topic_titles = {t["title"] for t in topics}
    title_by_ref = {r["ref"]: r["title"] for r in rows}
    carded = [pos_by_ref[r] for r in card_refs(lang)
              if r in pos_by_ref and title_by_ref[r] in topic_titles]
    return max(carded) if carded else None


def placement_topics(lang):
    """Evenly spaced real topics spanning the whole book, easiest first."""
    topics = topic_inventory(grammar_rows(lang), set())
    n = min(PLACEMENT_TOPICS, len(topics))
    if n < 2:
        picks = topics
    else:
        step = (len(topics) - 1) / (n - 1)
        picks = [topics[round(i * step)] for i in range(n)]
    return [{"ref": t["ref"], "title": display_title(t)} for t in picks]


def spot_check_pick(below_all):
    """The below-frontier topic to probe next: the midpoint of the largest
    run of unprobed topics, with every covered topic (carded or known —
    placement answers, passed and failed spot checks alike) as an anchor.
    Binary search over regions: after k probes no unprobed stretch is longer
    than ~1/k of the below-frontier region, so holes surface early wherever
    they hide. Deterministic, so rerunning `next` proposes the same topic."""
    anchors = ([-1] + [i for i, t in enumerate(below_all) if t["covered"]]
               + [len(below_all)])
    a, b = max(zip(anchors, anchors[1:]), key=lambda g: g[1] - g[0])
    return below_all[(a + b) // 2]


def deck_event_count(lang):
    """Cards created + sections marked known: the new-topic event counter
    that paces spot checks without any extra stored state."""
    n = len(cards_db_query(lang, "SELECT id FROM cards", []))
    return n + len(known_refs(lang))


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
        steps.append(f"No cards yet. Run the placement quiz: python "
                     f"scripts/session.py next --lang {lang}")
    out({"lang": lang, "dictionary": d, "grammar": g, "deck": c,
         "ready": d["ok"] and g["ok"],
         "next_steps": steps or [f"Drive the session: python scripts/session.py "
                                 f"next --lang {lang}"]})


def next_topic_payload(lang):
    rows = grammar_rows(lang)
    topics = topic_inventory(rows, covered_refs(lang))
    uncovered = [t for t in topics if not t["covered"]]
    if not uncovered:
        return {"next_topic": None,
                "note": f"all {len(topics)} grammar topics are covered by "
                        "cards or marked known; ask the student what they "
                        "want to work on"}
    fpos = frontier_pos(lang, rows, topics)
    if fpos is None:
        below_all, behind, ahead = [], [], uncovered
    else:
        below_all = [t for t in topics if t["pos"] < fpos]
        behind = [t for t in below_all if not t["covered"]]
        ahead = [t for t in uncovered if t["pos"] >= fpos]
    coverage = ({} if fpos is None else
                {"below_level": {"unverified": len(behind),
                                 "of": len(below_all)}})

    def strip(t):
        return {"ref": t["ref"], "title": display_title(t)}

    # spot-check share of new topics scales with the unverified backlog:
    # every 2nd while it dominates, decaying to every 6th as it shrinks
    events = deck_event_count(lang)
    spot = False
    if behind:
        k = min(SPOT_CHECK_MAX_EVERY,
                max(SPOT_CHECK_MIN_EVERY, round(len(uncovered) / len(behind))))
        spot = not ahead or events % k == k - 1
    if spot:
        pick = spot_check_pick(below_all)
        return {"next_topic": strip(pick), "kind": "spot_check", **coverage,
                "alternatives": [strip(t) for t in ahead[:3]],
                "note": "spot check: this topic is BELOW the student's placed "
                        "level, picked to bisect the largest unprobed stretch "
                        f"of the book ({len(behind)} of {len(below_all)} "
                        "lower topics still unverified) and catch holes in "
                        "fundamentals. Read "
                        f"./ll grammar show {pick['ref']} --lang {lang} "
                        "yourself but do NOT teach it: give 2 one-sentence "
                        "exercises on it straight away, verify the answers "
                        "with dict/grammar commands, then either (solid) run "
                        f"./ll cards known add --refs \"{pick['ref']}\" "
                        f"--lang {lang} --reason \"spot check passed\" and "
                        "move on — no card — or (faltered) create a card: "
                        f"./ll cards create --lang {lang} --concept "
                        f"\"{display_title(pick)}\" --refs \"{pick['ref']}\" "
                        "and teach the section to the student"}
    first, rest = ahead[0], ahead[1:4]
    return {"next_topic": strip(first), "kind": "advance", **coverage,
            "alternatives": [strip(t) for t in rest],
            "note": "the first topic at or above the student's level; propose "
                    "it to the student; if they agree run: "
                    f"./ll cards create --lang {lang} --concept "
                    f"\"{display_title(first)}\" --refs \"{first['ref']}\", "
                    f"teach it from ./ll grammar show {first['ref']} --lang "
                    f"{lang}, then exercise it like any due card. If "
                    "next_topic is still book front matter rather than a "
                    "grammar topic, use the first alternative that is a real "
                    "topic instead"}


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
             "quiz_topics": placement_topics(lang),
             "instruction":
                 "first session, empty deck: place the student before creating "
                 "cards. quiz_topics are evenly spaced across the grammar, "
                 "easiest first: write ONE English sentence to translate per "
                 "topic, in the given order; present them all at once; tell "
                 "the student to answer in order and stop (or write \"don't "
                 "know\") when they run out of depth; do not reveal answers; "
                 "verify every answer with dict/grammar commands; report "
                 "placement item by item with full corrections. Then record "
                 "the result: (a) set the level with the ref of the HARDEST "
                 "topic answered correctly (the first quiz ref if none were): "
                 f"./ll cards frontier set <ref> --lang {lang}; (b) mark the "
                 "topics answered correctly as known: ./ll cards known add "
                 f"--refs \"<ref>,<ref>,...\" --lang {lang} --reason "
                 "placement; (c) create the first card on the EARLIEST topic "
                 f"answered wrong: ./ll cards create --lang {lang} --concept "
                 "\"...\" --refs \"...\" (vocabulary slips go to the inbox, "
                 f"not to cards; if every item was perfect, create no card); "
                 f"{rerun}"})
        return
    card = first_due_card(lang)
    if card:
        rows = grammar_rows(lang)
        fpos = frontier_pos(lang, rows, topic_inventory(rows, covered_refs(lang)))
        level = ("" if fpos is None else
                 " Pitch vocabulary and sentence difficulty at the student's "
                 f"overall level — around \"{display_title(rows[fpos])}\" — "
                 "not at the minimum the concept needs.")
        out({"lang": lang, "state": "review", "due_count": c["due_now"],
             "card": card,
             "instruction":
                 "review this card. Write a set of exercises on its concept — "
                 "exactly 2 for a simple rule, 3-4 for a complex one, never "
                 "fewer than 2 — one sentence each (English to translate, or a "
                 "prompt in the language), probing recent_mistakes if any."
                 f"{level} "
                 "Number them, present all at once, do not reveal answers, and "
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
