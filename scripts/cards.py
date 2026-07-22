"""FSRS-scheduled concept cards. The scheduler is code; the LLM only reports
a rating (1=again 2=hard 3=good 4=easy) and never computes intervals.

  due      cards due now (includes each card's recent mistakes)
  create   new concept card (due immediately)
  grade    record a review with every prompt/answer pair of the exercise set,
           each optionally scored 1-4 on its own; optionally log the mistake
           that caused a failing grade
  show     full card detail
  history  all reviews of a card with their date, rating, and prompt/answer
           pairs, each with the time it was answered and its score
  list     all cards with due dates
  inbox    holding pen for mistakes unrelated to the card under review:
             inbox add / inbox list / inbox resolve
  frontier the student's placed level, a grammar section ref; topic selection
           targets it: frontier set / frontier show
  known    grammar sections proven known without needing a card (placement,
           passed spot checks): known add / known list
  stats    deck overview

Examples:
  python scripts/cards.py due --lang latin
  python scripts/cards.py create --lang latin --concept "ablative absolute" --refs "419,420"
  python scripts/cards.py grade ablative-absolute 1 --lang latin \
      --prompt "Translate: with the city captured, ..." --answer "urbe capta erat ..." \
      --prompt "Translate: with the king expelled, ..." --answer "rege expulso ..." \
      --produced "urbe capta erat" --note "used erat inside the construction"
  # same, without repeating flags: pass the whole set as one JSON argument.
  # "score" is optional per item and records how that one exercise went,
  # independently of the single rating FSRS schedules from.
  python scripts/cards.py grade ablative-absolute 1 --lang latin \
      --pairs-json '[{"prompt": "...", "answer": "...", "score": 1}]' \
      --note "used erat inside the construction"
  # or from a file (or "-" for stdin), for callers that can write one:
  python scripts/cards.py grade ablative-absolute 1 --lang latin \
      --pairs-file /tmp/set.json --note "used erat inside the construction"
  python scripts/cards.py history ablative-absolute --lang latin
  python scripts/cards.py inbox add --lang latin --produced "amavi puellam heri" \
      --note "wrong word order emphasis" --concept-hint "word order"
  python scripts/cards.py inbox resolve 3 --lang latin --card word-order
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from common import (CARDS_DB, GRAMMAR_DB, JsonArgumentParser, open_db, out,
                    die)

try:
    from fsrs import FSRS, Card, Rating
except ImportError:
    sys.exit("the fsrs package is required: .venv/bin/pip install fsrs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY,
  concept TEXT NOT NULL,
  grammar_refs TEXT,
  fsrs TEXT NOT NULL,
  created TEXT NOT NULL,
  suspended INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mistakes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT NOT NULL REFERENCES cards(id),
  ts TEXT NOT NULL,
  produced TEXT,
  expected TEXT,
  note TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT NOT NULL REFERENCES cards(id),
  ts TEXT NOT NULL,
  rating INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS exercises (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT NOT NULL REFERENCES cards(id),
  review_id INTEGER NOT NULL REFERENCES reviews(id),
  ts TEXT,
  score INTEGER,
  prompt TEXT NOT NULL,
  answer TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  produced TEXT,
  note TEXT,
  concept_hint TEXT,
  status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS known_sections (
  ref TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  reason TEXT
);
"""

RATINGS = {"1": 1, "2": 2, "3": 3, "4": 4,
           "again": 1, "hard": 2, "good": 3, "easy": 4}


def migrate(conn):
    """Add columns introduced after a deck was created.

    `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so decks
    built before per-exercise timestamps and scores need the columns added
    explicitly. Old exercise rows inherit their timestamp from the review
    they belong to, which is when they were answered; their score stays
    NULL, because a per-item score was never recorded for them.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(exercises)")}
    if "ts" not in cols:
        conn.execute("ALTER TABLE exercises ADD COLUMN ts TEXT")
        conn.execute("UPDATE exercises SET ts = (SELECT ts FROM reviews "
                     "WHERE reviews.id = exercises.review_id)")
    if "score" not in cols:
        conn.execute("ALTER TABLE exercises ADD COLUMN score INTEGER")


def now():
    return datetime.now(timezone.utc)


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "card"


def get_card(conn, card_id):
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        die(f"no card '{card_id}'; use `list` to see ids")
    return row


def recent_mistakes(conn, card_id, limit=3):
    rows = conn.execute(
        "SELECT ts, produced, expected, note FROM mistakes "
        "WHERE card_id = ? ORDER BY id DESC LIMIT ?", (card_id, limit)).fetchall()
    return [{k: r[k] for k in ("ts", "produced", "expected", "note") if r[k]}
            for r in rows]


def log_mistake(conn, card_id, args):
    if args.produced or args.note:
        conn.execute(
            "INSERT INTO mistakes (card_id, ts, produced, note) "
            "VALUES (?,?,?,?)",
            (card_id, now().isoformat(), args.produced, args.note))


def apply_review(conn, card_id, rating_int, when=None):
    when = when or now()
    row = get_card(conn, card_id)
    card = Card.from_dict(json.loads(row["fsrs"]))
    card = FSRS().repeat(card, when)[Rating(rating_int)].card
    conn.execute("UPDATE cards SET fsrs = ? WHERE id = ?",
                 (json.dumps(card.to_dict(), default=str), card_id))
    cur = conn.execute("INSERT INTO reviews (card_id, ts, rating) VALUES (?,?,?)",
                       (card_id, when.isoformat(), rating_int))
    return card.due, cur.lastrowid


# ---------------------------------------------------------------- commands

def cmd_due(conn, args):
    rows = conn.execute(
        "SELECT * FROM cards WHERE suspended = 0 ORDER BY json_extract(fsrs, '$.due')"
    ).fetchall()
    due = []
    for r in rows:
        d = json.loads(r["fsrs"])["due"]
        if d <= now().isoformat():
            due.append({"id": r["id"], "concept": r["concept"],
                        "grammar_refs": r["grammar_refs"],
                        "recent_mistakes": recent_mistakes(conn, r["id"])})
        if len(due) >= args.limit:
            break
    result = {"due_count": len(due), "cards": due}
    if not due:
        result["note"] = ("no cards due; announce this to the student, resolve "
                          "the inbox, then pick a new topic from `grammar toc`")
    return result


def cmd_create(conn, args):
    card_id = args.id or slugify(args.concept)
    base, n = card_id, 2
    while conn.execute("SELECT 1 FROM cards WHERE id = ?", (card_id,)).fetchone():
        card_id = f"{base}-{n}"
        n += 1
    conn.execute(
        "INSERT INTO cards (id, concept, grammar_refs, fsrs, created) "
        "VALUES (?,?,?,?,?)",
        (card_id, args.concept, args.refs,
         json.dumps(Card().to_dict(), default=str), now().isoformat()))
    return {"ok": True, "id": card_id, "concept": args.concept,
            "note": "card is due immediately"}


# A review set is written and graded minutes apart. Anything older is a
# leftover from an earlier session: grading it would silently record exercises
# the student never saw, against a card they did not answer.
PAIRS_MAX_AGE_S = 30 * 60


def parse_score(value, source):
    """A per-exercise score on the same 1-4 scale as the card rating, or
    None when the caller did not score that item individually."""
    if value is None or value == "":
        return None
    score = RATINGS.get(str(value).lower())
    if not score:
        die(f"{source}: score must be 1-4 or again/hard/good/easy, "
            f"got {value!r}")
    return score


def validate_pairs(pairs, source):
    if (not isinstance(pairs, list) or not pairs
            or not all(isinstance(p, dict) and p.get("prompt") and p.get("answer")
                       for p in pairs)):
        die(f"{source} must be a non-empty JSON list like "
            '[{"prompt": "...", "answer": "...", "score": 3}, ...] '
            "with prompt and answer non-empty in every item "
            "(score is optional, 1-4)")
    return [{"prompt": p["prompt"], "answer": p["answer"],
             "score": parse_score(p.get("score"), source)} for p in pairs]


def read_pairs(path):
    if path != "-":
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError as e:
            die(f"could not read exercise pairs from {path}: {e}")
        if age > PAIRS_MAX_AGE_S:
            die(f"{path} was last written {int(age // 60)} minutes ago, so it is "
                "almost certainly left over from an earlier session; grading it "
                "would record exercises the student never saw. Pass this set's "
                "pairs inline with --pairs-json, or write them to a fresh file.")
    try:
        text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
        pairs = json.loads(text)
    except (OSError, json.JSONDecodeError) as e:
        die(f"could not read exercise pairs from {path}: {e}")
    return validate_pairs(pairs, "the pairs file")


def duplicate_review(conn, card_id, rating_int, prompts):
    """A retry after a failed grade call is not a second review.

    Models routinely re-issue `grade` after a malformed first attempt; without
    this the same set lands twice and FSRS schedules off a doubled history.
    """
    cutoff = (now() - timedelta(minutes=10)).isoformat()
    rows = conn.execute(
        "SELECT id, ts FROM reviews WHERE card_id = ? AND rating = ? AND ts >= ? "
        "ORDER BY id DESC", (card_id, rating_int, cutoff)).fetchall()
    for r in rows:
        prev = [x["prompt"] for x in conn.execute(
            "SELECT prompt FROM exercises WHERE review_id = ? ORDER BY id",
            (r["id"],)).fetchall()]
        if prev == list(prompts):
            return r
    return None


def cmd_grade(conn, args):
    rating = RATINGS.get(str(args.rating).lower())
    if not rating:
        die("rating must be 1-4 or again/hard/good/easy")
    prompts = list(args.prompt or [])
    answers = list(args.answer or [])
    flag_scores = [parse_score(s, "--score") for s in (args.score or [])]
    if flag_scores and len(flag_scores) != len(prompts):
        die(f"got {len(flag_scores)} --score but {len(prompts)} --prompt; "
            "--score is optional, but when given there must be one per "
            "--prompt, in the same order")
    scores = flag_scores or [None] * len(prompts)
    if args.pairs_json:
        try:
            inline = json.loads(args.pairs_json)
        except json.JSONDecodeError as e:
            die(f"--pairs-json is not valid JSON: {e}")
        for p in validate_pairs(inline, "--pairs-json"):
            prompts.append(p["prompt"])
            answers.append(p["answer"])
            scores.append(p["score"])
    if args.pairs_file:
        for p in read_pairs(args.pairs_file):
            prompts.append(p["prompt"])
            answers.append(p["answer"])
            scores.append(p["score"])
    if not prompts:
        die("grading requires the exercises: repeat --prompt \"...\" "
            "--answer \"...\" for each exercise in the set, or pass the whole "
            'set as --pairs-json \'[{"prompt": ..., "answer": ...}, ...]\' '
            "(--pairs-file <file.json> does the same from a file, for callers "
            "that can write one)")
    if len(prompts) != len(answers):
        die(f"got {len(prompts)} --prompt but {len(answers)} --answer; "
            "each --prompt needs a matching --answer")
    dup = duplicate_review(conn, args.card_id, rating, prompts)
    if dup:
        return {"ok": True, "card": args.card_id, "rating": rating,
                "recorded": False, "duplicate_of_review": dup["id"],
                "exercises_recorded": 0,
                "note": f"this exact set was already graded at {dup['ts']}; "
                        "nothing was recorded a second time. If the student "
                        "really answered a new set, grade it with those "
                        "prompts. Otherwise carry on: give the per-item "
                        "verdicts to the student, then run "
                        f"./ll session next --lang {args.lang}"}
    if rating <= 2:
        log_mistake(conn, args.card_id, args)
    refs = get_card(conn, args.card_id)["grammar_refs"]
    when = now()
    due, review_id = apply_review(conn, args.card_id, rating, when)
    conn.executemany(
        "INSERT INTO exercises (card_id, review_id, ts, score, prompt, answer) "
        "VALUES (?,?,?,?,?,?)",
        [(args.card_id, review_id, when.isoformat(), s, p, a)
         for p, a, s in zip(prompts, answers, scores)])
    note = ("STOP calling tools and write the student a message now: a "
            "per-item verdict, the full correction for any mistake, and one "
            "dict/grammar-verified alternate phrasing per item. The grade you "
            "just recorded is invisible to them — feedback they never see is "
            "the same as no review. Only once you have sent it, run: "
            f"./ll session next --lang {args.lang}")
    if rating <= 2 and refs:
        note = (f"card failed: run ./ll grammar show <ref> --lang {args.lang} "
                f"for each of refs {refs} and quote the rule verbatim to the "
                "student; " + note)
    return {"ok": True, "card": args.card_id, "rating": rating,
            "exercises_recorded": len(prompts), "next_due": due.isoformat(),
            "note": note}


def cmd_show(conn, args):
    r = get_card(conn, args.card_id)
    f = json.loads(r["fsrs"])
    return {"id": r["id"], "concept": r["concept"],
            "grammar_refs": r["grammar_refs"], "created": r["created"],
            "due": f["due"], "reps": f["reps"], "lapses": f["lapses"],
            "suspended": bool(r["suspended"]),
            "recent_mistakes": recent_mistakes(conn, r["id"], 10)}


def cmd_history(conn, args):
    get_card(conn, args.card_id)
    reviews = conn.execute(
        "SELECT id, ts, rating FROM reviews WHERE card_id = ? ORDER BY id DESC",
        (args.card_id,)).fetchall()
    history = []
    for rev in reviews:
        pairs = conn.execute(
            "SELECT ts, score, prompt, answer FROM exercises "
            "WHERE review_id = ? ORDER BY id", (rev["id"],)).fetchall()
        history.append({"ts": rev["ts"], "rating": rev["rating"],
                        "exercises": [{"ts": p["ts"], "score": p["score"],
                                       "prompt": p["prompt"],
                                       "answer": p["answer"]} for p in pairs]})
    return {"card": args.card_id, "reviews": len(history), "history": history}


def cmd_list(conn, args):
    rows = conn.execute(
        "SELECT * FROM cards ORDER BY json_extract(fsrs, '$.due')").fetchall()
    return {"count": len(rows),
            "cards": [{"id": r["id"], "concept": r["concept"],
                       "due": json.loads(r["fsrs"])["due"][:16],
                       **({"suspended": True} if r["suspended"] else {})}
                      for r in rows]}


def cmd_stats(conn, args):
    total = conn.execute("SELECT count(*) c FROM cards").fetchone()["c"]
    due = sum(1 for r in conn.execute("SELECT fsrs FROM cards WHERE suspended=0")
              if json.loads(r["fsrs"])["due"] <= now().isoformat())
    reviews = conn.execute("SELECT count(*) c FROM reviews").fetchone()["c"]
    open_inbox = conn.execute(
        "SELECT count(*) c FROM inbox WHERE status='open'").fetchone()["c"]
    return {"cards": total, "due_now": due, "reviews_total": reviews,
            "inbox_open": open_inbox}


def grammar_position(lang, ref):
    """Book-order position of a section ref: (position, total, title)."""
    g = open_db(lang, GRAMMAR_DB)
    rows = g.execute("SELECT ref, title FROM sections ORDER BY id").fetchall()
    g.close()
    for i, r in enumerate(rows):
        if r["ref"] == ref:
            return i + 1, len(rows), r["title"]
    die(f"no grammar section with ref '{ref}'; "
        f"see ./ll grammar toc --lang {lang}")


def cmd_frontier(conn, args):
    if args.frontier_cmd == "set":
        pos, total, title = grammar_position(args.lang, args.ref)
        conn.execute("INSERT OR REPLACE INTO settings (key, value) "
                     "VALUES ('frontier', ?)", (args.ref,))
        return {"ok": True,
                "frontier": {"ref": args.ref, "title": title,
                             "position": pos, "of": total}}
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'frontier'").fetchone()
    if not row:
        return {"frontier": None,
                "note": "no frontier set; the placement quiz sets it, or run "
                        f"./ll cards frontier set <ref> --lang {args.lang}"}
    pos, total, title = grammar_position(args.lang, row["value"])
    return {"frontier": {"ref": row["value"], "title": title,
                         "position": pos, "of": total}}


def cmd_known(conn, args):
    if args.known_cmd == "add":
        refs = [r.strip() for r in args.refs.split(",") if r.strip()]
        if not refs:
            die("--refs must list at least one grammar section ref")
        for ref in refs:
            grammar_position(args.lang, ref)
        conn.executemany(
            "INSERT OR REPLACE INTO known_sections (ref, ts, reason) "
            "VALUES (?,?,?)",
            [(ref, now().isoformat(), args.reason) for ref in refs])
        return {"ok": True, "known_added": refs,
                "note": "these sections will no longer be proposed as new "
                        "topics"}
    rows = conn.execute("SELECT * FROM known_sections ORDER BY ts").fetchall()
    return {"known": [{"ref": r["ref"], "ts": r["ts"], "reason": r["reason"]}
                      for r in rows]}


def cmd_inbox(conn, args):
    if args.inbox_cmd == "add":
        conn.execute(
            "INSERT INTO inbox (ts, produced, note, concept_hint) VALUES (?,?,?,?)",
            (now().isoformat(), args.produced, args.note, args.concept_hint))
        return {"ok": True,
                "note": "recorded; resolve the inbox at the end of the session"}
    if args.inbox_cmd == "list":
        rows = conn.execute(
            "SELECT * FROM inbox WHERE status = 'open' ORDER BY id").fetchall()
        return {"open": [{"id": r["id"], "produced": r["produced"],
                          "note": r["note"], "concept_hint": r["concept_hint"]}
                         for r in rows]}
    # resolve
    row = conn.execute("SELECT * FROM inbox WHERE id = ? AND status = 'open'",
                       (args.inbox_id,)).fetchone()
    if not row:
        die(f"no open inbox item {args.inbox_id}")
    if args.dismiss:
        conn.execute("UPDATE inbox SET status='dismissed' WHERE id=?", (row["id"],))
        return {"ok": True, "dismissed": row["id"]}
    if args.card:
        target = args.card
        get_card(conn, target)
    elif args.create_concept:
        target = cmd_create(conn, argparse.Namespace(
            id=None, concept=args.create_concept, refs=args.refs))["id"]
    else:
        die("resolve needs --card <id>, --create-concept '<text>', or --dismiss")
    conn.execute(
        "INSERT INTO mistakes (card_id, ts, produced, expected, note) "
        "VALUES (?,?,?,?,?)",
        (target, row["ts"], row["produced"], None, row["note"]))
    result = {"ok": True, "resolved_into": target}
    if args.rating:
        due, _ = apply_review(conn, target, RATINGS[str(args.rating).lower()])
        result["next_due"] = due.isoformat()
    conn.execute("UPDATE inbox SET status='resolved' WHERE id=?", (row["id"],))
    return result


def main():
    ap = JsonArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def lang(p):
        p.add_argument("--lang", required=True)
        return p

    p = lang(sub.add_parser("due"))
    p.add_argument("--limit", type=int, default=10)

    p = lang(sub.add_parser("create"))
    p.add_argument("--concept", required=True)
    p.add_argument("--id", default=None)
    p.add_argument("--refs", default=None,
                   help="comma-separated grammar section refs")

    p = lang(sub.add_parser("grade"))
    p.add_argument("card_id")
    p.add_argument("rating", help="1-4 or again/hard/good/easy")
    p.add_argument("--prompt", action="append",
                   help="an exercise as shown to the student; repeat per exercise")
    p.add_argument("--answer", action="append",
                   help="the student's answer; one per --prompt, same order")
    p.add_argument("--score", action="append",
                   help="optional per-exercise score, 1-4 on the same scale as "
                        "the card rating; one per --prompt, same order")
    p.add_argument("--pairs-json", default=None, metavar="JSON",
                   help='the whole set inline: \'[{"prompt": "...", '
                        '"answer": "...", "score": 3}, ...]\' — needs no file, '
                        "so it works from harnesses that cannot write one; "
                        '"score" is optional per item')
    p.add_argument("--pairs-file", default=None, metavar="FILE",
                   help='JSON file (or "-" for stdin) with the whole set: '
                        '[{"prompt": "...", "answer": "...", "score": 3}, ...] '
                        "— avoids shell quoting")
    p.add_argument("--produced", default=None,
                   help="what the student wrote (log with failing grades)")
    p.add_argument("--note", default=None, help="what went wrong")

    p = lang(sub.add_parser("show"))
    p.add_argument("card_id")

    p = lang(sub.add_parser("history"))
    p.add_argument("card_id")

    lang(sub.add_parser("list"))
    lang(sub.add_parser("stats"))

    p = sub.add_parser("inbox")
    isub = p.add_subparsers(dest="inbox_cmd", required=True)
    pa = lang(isub.add_parser("add"))
    pa.add_argument("--produced", default=None)
    pa.add_argument("--note", required=True)
    pa.add_argument("--concept-hint", default=None)
    lang(isub.add_parser("list"))
    pr = lang(isub.add_parser("resolve"))
    pr.add_argument("inbox_id", type=int)
    pr.add_argument("--card", default=None, help="existing card id to attach to")
    pr.add_argument("--create-concept", default=None,
                    help="create a new card with this concept")
    pr.add_argument("--refs", default=None)
    pr.add_argument("--rating", default=None,
                    help="optionally also grade the target card (usually 1)")
    pr.add_argument("--dismiss", action="store_true")

    p = sub.add_parser("frontier")
    fsub = p.add_subparsers(dest="frontier_cmd", required=True)
    pf = lang(fsub.add_parser("set"))
    pf.add_argument("ref", help="grammar section ref of the student's level")
    lang(fsub.add_parser("show"))

    p = sub.add_parser("known")
    ksub = p.add_subparsers(dest="known_cmd", required=True)
    pk = lang(ksub.add_parser("add"))
    pk.add_argument("--refs", required=True,
                    help="comma-separated grammar section refs proven known")
    pk.add_argument("--reason", default=None,
                    help='e.g. "placement" or "spot check passed"')
    lang(ksub.add_parser("list"))

    args = ap.parse_args()
    conn = open_db(args.lang, CARDS_DB, must_exist=False)
    conn.executescript(SCHEMA)
    migrate(conn)
    handlers = {"due": cmd_due, "create": cmd_create, "grade": cmd_grade,
                "show": cmd_show, "history": cmd_history,
                "list": cmd_list, "stats": cmd_stats, "inbox": cmd_inbox,
                "frontier": cmd_frontier, "known": cmd_known}
    result = handlers[args.cmd](conn, args)
    conn.commit()
    out(result)


if __name__ == "__main__":
    main()
