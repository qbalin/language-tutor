"""Query the local Wiktionary dictionary. All output is compact JSON.

  lookup       identify words (lemma, or which inflected form of which lemma);
               pass several words to check a whole sentence in one call
      python scripts/dict.py lookup urbe capta amaverunt --lang latin
  translate    English -> target language, via full-text search over glosses
      python scripts/dict.py translate love --lang latin
  inflections  list attested forms of a lemma, optionally filtered by tags
      python scripts/dict.py inflections amo --lang latin --tags "perfect third-person"
  sample       draw varied, level-appropriate vocabulary from the corpus
               frequency list (built by ingest_frequency), to seed exercises
      python scripts/dict.py sample --lang latin --count 15 --pos noun --max-rank 800
"""
import argparse
import json
import sqlite3

from common import (DICT_DB, FREQ_DB, JsonArgumentParser, db_path, fts_quote,
                    normalize, open_db)

MAX_MATCHES = 8

# Frequency-rank bands for `sample`, so difficulty can be asked for by name.
BANDS = {"beginner": (1, 500), "intermediate": (501, 2000),
         "advanced": (2001, 10 ** 9)}
CONTENT_POS = ("noun", "verb", "adj", "adv")   # default: real vocabulary
DEFAULT_MAX_RANK = 2000                         # skip rare words unless asked


def entry_senses(row, max_senses):
    senses = json.loads(row["data"])["senses"][:max_senses]
    result = []
    for s in senses:
        gloss = s["gloss"][:200]
        if "form_of" in s:
            gloss = f"[form of {s['form_of']}] {gloss}"
        if s.get("tags"):
            gloss = f"({', '.join(s['tags'][:4])}) {gloss}"
        result.append(gloss)
    return result


def lookup_word(conn, word, lang, max_senses):
    q = normalize(word, lang)
    matches = []
    for row in conn.execute(
            "SELECT * FROM entries WHERE word_norm = ? LIMIT ?",
            (q, MAX_MATCHES)):
        matches.append({"word": row["word"], "pos": row["pos"],
                        "match": "entry",
                        "senses": entry_senses(row, max_senses)})
    seen_lemmas = {(m["word"], m["pos"]) for m in matches}
    for row in conn.execute(
            """SELECT f.form, f.tags, e.word, e.pos, e.data
               FROM forms f JOIN entries e ON e.id = f.entry_id
               WHERE f.form_norm = ? LIMIT ?""", (q, MAX_MATCHES * 3)):
        if (row["word"], row["pos"]) in seen_lemmas:
            continue
        seen_lemmas.add((row["word"], row["pos"]))
        matches.append({"form": row["form"], "form_tags": row["tags"],
                        "match": "inflected form",
                        "lemma": row["word"], "pos": row["pos"],
                        "senses": entry_senses(row, 2)})
        if len(matches) >= MAX_MATCHES:
            break
    result = {"query": word, "matches": matches}
    if not matches:
        sugg = [r["word"] for r in conn.execute(
            "SELECT DISTINCT word FROM entries WHERE word_norm LIKE ? LIMIT 5",
            (q[:4] + "%",))]
        result["note"] = "no match; the word may be misspelled or absent"
        if sugg:
            result["similar"] = sugg
    return result


def cmd_lookup(conn, args):
    return {"results": [lookup_word(conn, w, args.lang, args.max_senses)
                        for w in args.words]}


def cmd_translate(conn, args):
    match = fts_quote(args.phrase)
    if not match:
        return {"error": "empty query"}
    rows = conn.execute(
        """SELECT g.gloss, e.word, e.pos
           FROM gloss_fts g JOIN entries e ON e.id = g.entry_id
           WHERE gloss_fts MATCH ? ORDER BY rank LIMIT ?""",
        (match, MAX_MATCHES)).fetchall()
    return {"query": args.phrase,
            "candidates": [{"word": r["word"], "pos": r["pos"],
                            "gloss": r["gloss"][:200]} for r in rows],
            "note": "verify the chosen word with `lookup` before using it"}


def cmd_inflections(conn, args):
    q = normalize(args.lemma, args.lang)
    entries = conn.execute(
        "SELECT * FROM entries WHERE word_norm = ?", (q,)).fetchall()
    if not entries:
        return {"error": f"no entry found for '{args.lemma}'"}
    want = [t.strip().lower() for t in (args.tags or "").split() if t.strip()]
    result = []
    for e in entries:
        rows = conn.execute(
            "SELECT form, tags FROM forms WHERE entry_id = ?", (e["id"],)).fetchall()
        forms = []
        for r in rows:
            tags = (r["tags"] or "").lower()
            if all(w in tags for w in want):
                forms.append({"form": r["form"], "tags": r["tags"]})
        if not rows:
            continue
        item = {"lemma": e["word"], "pos": e["pos"], "total_forms": len(rows),
                "forms": forms[:args.limit]}
        if len(forms) > args.limit:
            item["note"] = (f"{len(forms)} forms matched, showing {args.limit}; "
                            f"narrow with --tags")
        result.append(item)
    if not result:
        return {"error": f"'{args.lemma}' has no inflection table in the dictionary"}
    return {"query": args.lemma, "filter": want, "entries": result}


def first_gloss(conn, lemma_norm, pos):
    row = conn.execute(
        "SELECT data FROM entries WHERE word_norm = ? AND pos = ? LIMIT 1",
        (lemma_norm, pos)).fetchone()
    if not row:
        return ""
    senses = json.loads(row["data"]).get("senses") or []
    return senses[0]["gloss"][:120] if senses else ""


def cmd_sample(conn, args):
    fpath = db_path(args.lang, FREQ_DB)
    if not fpath.exists():
        return {"error": f"no frequency list for '{args.lang}'",
                "note": "this language has no vocabulary frequency list; build "
                        "one by dropping plain-text works into "
                        f"languages/{args.lang}/texts/ and running "
                        f"./ll ingest_frequency --lang {args.lang}, or just "
                        "vary vocabulary yourself"}
    lo, hi = 1, DEFAULT_MAX_RANK
    if args.band:
        lo, hi = BANDS[args.band]
    if args.min_rank is not None:
        lo = args.min_rank
    if args.max_rank is not None:
        hi = args.max_rank

    where = ["rank >= ?", "rank <= ?"]
    params = [lo, hi]
    if args.pos:
        where.append("pos = ?")
        params.append(args.pos)
    else:
        where.append("pos IN (%s)" % ",".join("?" * len(CONTENT_POS)))
        params.extend(CONTENT_POS)
    excluded = [normalize(w, args.lang)
                for w in (args.exclude or "").split(",") if w.strip()]
    if excluded:
        where.append("lemma_norm NOT IN (%s)" % ",".join("?" * len(excluded)))
        params.extend(excluded)

    fconn = sqlite3.connect(fpath)
    fconn.row_factory = sqlite3.Row
    rows = fconn.execute(
        f"SELECT lemma, lemma_norm, pos, rank FROM frequency "
        f"WHERE {' AND '.join(where)} ORDER BY random() LIMIT ?",
        params + [args.count]).fetchall()
    fconn.close()

    words = [{"lemma": r["lemma"], "pos": r["pos"], "rank": r["rank"],
              "gloss": first_gloss(conn, r["lemma_norm"], r["pos"])}
             for r in rows]
    result = {"words": words, "count": len(words),
              "rank_band": [lo, ("+" if hi >= 10 ** 8 else hi)],
              "note": "candidate vocabulary drawn from the corpus frequency "
                      "list; build exercises around these instead of reaching "
                      "for the same stock words, and verify any form with lookup"}
    if not words:
        result["note"] = ("no words matched; widen the rank band (--max-rank) "
                          "or drop the --pos filter")
    return result


def main():
    ap = JsonArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lookup", help="identify one or more words")
    p.add_argument("words", nargs="+")
    p.add_argument("--lang", required=True)
    p.add_argument("--max-senses", type=int, default=4)

    p = sub.add_parser("translate", help="English -> target language")
    p.add_argument("phrase")
    p.add_argument("--lang", required=True)

    p = sub.add_parser("inflections", help="list forms of a lemma")
    p.add_argument("lemma")
    p.add_argument("--lang", required=True)
    p.add_argument("--tags", default="",
                   help='space-separated tag filter, e.g. "perfect singular"')
    p.add_argument("--limit", type=int, default=40)

    p = sub.add_parser("sample", help="draw varied vocabulary from the corpus "
                                      "frequency list")
    p.add_argument("--lang", required=True)
    p.add_argument("--count", type=int, default=15, help="how many words")
    p.add_argument("--pos", default=None,
                   help="restrict to one part of speech (noun/verb/adj/adv/...); "
                        "default is content words (noun, verb, adj, adv)")
    p.add_argument("--band", choices=sorted(BANDS),
                   help="frequency band: beginner (top 500), intermediate "
                        "(501-2000), advanced (rarer)")
    p.add_argument("--min-rank", type=int, default=None,
                   help="lowest frequency rank to include (overrides --band)")
    p.add_argument("--max-rank", type=int, default=None,
                   help="highest frequency rank to include (overrides --band)")
    p.add_argument("--exclude", default="",
                   help="comma-separated words to leave out (e.g. ones already "
                        "used this session)")

    args = ap.parse_args()
    conn = open_db(args.lang, DICT_DB)
    from common import out
    out({"lookup": cmd_lookup, "translate": cmd_translate,
         "inflections": cmd_inflections, "sample": cmd_sample}[args.cmd](conn, args))


if __name__ == "__main__":
    main()
