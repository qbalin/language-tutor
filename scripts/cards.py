"""FSRS-scheduled concept cards. The scheduler is code; the LLM only reports
a rating (1=again 2=hard 3=good 4=easy) and never computes intervals.

  due      cards due now (includes each card's recent mistakes)
  create   new concept card (due immediately)
  grade    record a review with every prompt/answer pair of the exercise set;
           optionally log the mistake that caused a failing grade
  mistake  log a mistake on a card without grading it
  show     full card detail
  history  all reviews of a card with their date, rating, and prompt/answer pairs
  list     all cards with due dates
  inbox    holding pen for mistakes unrelated to the card under review:
             inbox add / inbox list / inbox resolve
  stats    deck overview

Examples:
  python scripts/cards.py due --lang latin
  python scripts/cards.py create --lang latin --concept "ablative absolute" --refs "419,420"
  python scripts/cards.py grade ablative-absolute 1 --lang latin \
      --prompt "Translate: with the city captured, ..." --answer "urbe capta erat ..." \
      --prompt "Translate: with the king expelled, ..." --answer "rege expulso ..." \
      --produced "urbe capta erat" --note "used erat inside the construction"
  python scripts/cards.py history ablative-absolute --lang latin
  python scripts/cards.py inbox add --lang latin --produced "amavi puellam heri" \
      --note "wrong word order emphasis" --concept-hint "word order"
  python scripts/cards.py inbox resolve 3 --lang latin --card word-order
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone

from common import CARDS_DB, open_db, out, die

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
"""

RATINGS = {"1": 1, "2": 2, "3": 3, "4": 4,
           "again": 1, "hard": 2, "good": 3, "easy": 4}


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
    if args.produced or args.note or getattr(args, "expected", None):
        conn.execute(
            "INSERT INTO mistakes (card_id, ts, produced, expected, note) "
            "VALUES (?,?,?,?,?)",
            (card_id, now().isoformat(), args.produced,
             getattr(args, "expected", None), args.note))


def apply_review(conn, card_id, rating_int):
    row = get_card(conn, card_id)
    card = Card.from_dict(json.loads(row["fsrs"]))
    card = FSRS().repeat(card, now())[Rating(rating_int)].card
    conn.execute("UPDATE cards SET fsrs = ? WHERE id = ?",
                 (json.dumps(card.to_dict(), default=str), card_id))
    cur = conn.execute("INSERT INTO reviews (card_id, ts, rating) VALUES (?,?,?)",
                       (card_id, now().isoformat(), rating_int))
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


def cmd_grade(conn, args):
    rating = RATINGS.get(str(args.rating).lower())
    if not rating:
        die("rating must be 1-4 or again/hard/good/easy")
    prompts = args.prompt or []
    answers = args.answer or []
    if not prompts:
        die("grading requires the exercises: repeat --prompt \"...\" "
            "--answer \"...\" for each exercise in the set")
    if len(prompts) != len(answers):
        die(f"got {len(prompts)} --prompt but {len(answers)} --answer; "
            "each --prompt needs a matching --answer")
    if rating <= 2:
        log_mistake(conn, args.card_id, args)
    due, review_id = apply_review(conn, args.card_id, rating)
    conn.executemany(
        "INSERT INTO exercises (card_id, review_id, prompt, answer) "
        "VALUES (?,?,?,?)",
        [(args.card_id, review_id, p, a) for p, a in zip(prompts, answers)])
    return {"ok": True, "card": args.card_id, "rating": rating,
            "exercises_recorded": len(prompts), "next_due": due.isoformat()}


def cmd_mistake(conn, args):
    get_card(conn, args.card_id)
    if not (args.produced or args.note):
        die("provide --produced and/or --note")
    log_mistake(conn, args.card_id, args)
    return {"ok": True, "card": args.card_id, "logged": True}


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
            "SELECT prompt, answer FROM exercises WHERE review_id = ? ORDER BY id",
            (rev["id"],)).fetchall()
        history.append({"ts": rev["ts"], "rating": rev["rating"],
                        "exercises": [{"prompt": p["prompt"],
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
    ap = argparse.ArgumentParser(description=__doc__,
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
    p.add_argument("--produced", default=None,
                   help="what the student wrote (log with failing grades)")
    p.add_argument("--expected", default=None)
    p.add_argument("--note", default=None, help="what went wrong")

    p = lang(sub.add_parser("mistake"))
    p.add_argument("card_id")
    p.add_argument("--produced", default=None)
    p.add_argument("--expected", default=None)
    p.add_argument("--note", default=None)

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

    args = ap.parse_args()
    conn = open_db(args.lang, CARDS_DB, must_exist=False)
    conn.executescript(SCHEMA)
    handlers = {"due": cmd_due, "create": cmd_create, "grade": cmd_grade,
                "mistake": cmd_mistake, "show": cmd_show, "history": cmd_history,
                "list": cmd_list, "stats": cmd_stats, "inbox": cmd_inbox}
    result = handlers[args.cmd](conn, args)
    conn.commit()
    out(result)


if __name__ == "__main__":
    main()
