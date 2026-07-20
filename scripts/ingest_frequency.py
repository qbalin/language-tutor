"""Build a lemma-frequency list for a language from a corpus of real texts.

Drop plain-text works (e.g. Project Gutenberg classics) into
languages/<lang>/texts/ and run:

  python scripts/ingest_frequency.py --lang latin

Each text is tokenized, every token is normalized and resolved to a dictionary
lemma (directly, or via its inflected form), and the per-lemma counts are
written to languages/<lang>/frequencies.db. `./ll dict sample` then draws
level-appropriate vocabulary from this list so exercises stop leaning on the
same handful of words. Re-running rebuilds the whole list.

Requires the dictionary (languages/<lang>/dictionary.db) to be built first —
lemmatization joins against it.
"""
import argparse
import json
import re
import sqlite3
from collections import Counter, defaultdict

from common import (DICT_DB, FREQ_DB, JsonArgumentParser, db_path, lang_dir,
                    normalize, open_db, out)

SCHEMA = """
DROP TABLE IF EXISTS frequency;
DROP TABLE IF EXISTS meta;
CREATE TABLE frequency (
  lemma TEXT NOT NULL,
  lemma_norm TEXT NOT NULL,
  pos TEXT,
  count REAL NOT NULL,
  rank INTEGER NOT NULL
);
CREATE INDEX idx_freq_rank ON frequency(rank);
CREATE INDEX idx_freq_pos ON frequency(pos);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Project Gutenberg wraps the actual work between these markers; everything
# outside them is license/credits boilerplate that would skew the counts.
PG_START = re.compile(r"\*\*\*\s*START OF TH(?:E|IS) PROJECT GUTENBERG.*?\*\*\*",
                      re.I | re.S)
PG_END = re.compile(r"\*\*\*\s*END OF TH(?:E|IS) PROJECT GUTENBERG.*?\*\*\*",
                    re.I | re.S)

# Runs of letters (Unicode), excluding digits and underscore. Works for Latin
# and Greek scripts alike.
TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def strip_boilerplate(raw: str) -> str:
    m = PG_START.search(raw)
    if m:
        raw = raw[m.end():]
    m = PG_END.search(raw)
    if m:
        raw = raw[:m.start()]
    return raw


def count_tokens(texts_dir, lang):
    """Counter of normalized surface tokens across every .txt in texts_dir,
    plus the per-file raw token totals for reporting."""
    surface = Counter()
    per_file = {}
    for path in sorted(texts_dir.glob("*.txt")):
        raw = strip_boilerplate(
            path.read_text(encoding="utf-8", errors="replace"))
        n = 0
        for tok in TOKEN_RE.findall(raw):
            norm = normalize(tok, lang)
            # single letters are Roman numerals and manuscript sigla (I, V, P,
            # ...) far more often than real words, and are useless as practice
            # vocabulary regardless
            if len(norm) > 1:
                surface[norm] += 1
                n += 1
        per_file[path.name] = n
    return surface, per_file


def lemma_of(word, word_norm, pos, data, lang):
    """Reduce a dictionary entry to its true lemma. Wiktionary stores
    non-lemma "form-of" entries (e.g. `homines` = plural of `homō`); those are
    redirected to the lemma they point at so counts land on the lemma, not the
    inflected form."""
    for sense in json.loads(data).get("senses", []):
        target = sense.get("form_of")
        if target:
            return (target, normalize(target, lang), pos)
    return (word, word_norm, pos)


def resolve(conn, token, lang):
    """Candidate true lemmas for a normalized token: (word, word_norm, pos).

    A token may itself be a headword and/or an inflected form of one or more
    lemmas; every distinct lemma it could belong to is a candidate, with
    form-of entries collapsed onto their lemma.
    """
    cands = {}
    for row in conn.execute(
            "SELECT word, word_norm, pos, data FROM entries WHERE word_norm = ?",
            (token,)):
        word, wn, pos = lemma_of(row["word"], row["word_norm"], row["pos"],
                                 row["data"], lang)
        cands.setdefault((wn, pos), word)
    for row in conn.execute(
            "SELECT DISTINCT e.word, e.word_norm, e.pos, e.data "
            "FROM forms f JOIN entries e ON e.id = f.entry_id "
            "WHERE f.form_norm = ?", (token,)):
        word, wn, pos = lemma_of(row["word"], row["word_norm"], row["pos"],
                                 row["data"], lang)
        cands.setdefault((wn, pos), word)
    return [(word, wn, pos) for (wn, pos), word in cands.items()]


def build_counts(dict_conn, surface, lang):
    """Attribute each surface token's count to its lemma candidates, splitting
    ambiguous tokens fractionally (1/k) so they don't inflate any one lemma."""
    lemma_count = defaultdict(float)      # (word_norm, pos) -> count
    lemma_word = {}                       # (word_norm, pos) -> display word
    resolved = 0
    for token, c in surface.items():
        cands = resolve(dict_conn, token, lang)
        if not cands:
            continue                      # proper name, OCR noise, foreign word
        resolved += c
        share = c / len(cands)
        for word, wn, pos in cands:
            key = (wn, pos)
            lemma_count[key] += share
            lemma_word.setdefault(key, word)
    return lemma_count, lemma_word, resolved


def coverage_cutoffs(ranked):
    """Rank cutoffs for the beginner/intermediate/advanced vocabulary bands,
    derived from the corpus itself: the top-N lemmas needed to cover 80 / 90 /
    95% of running text. These self-calibrate to each language's distribution
    instead of hard-coded ranks. Returns {band_beginner, ...} rank ints."""
    total = sum(c for _, c in ranked) or 1
    want = {"band_beginner": 0.80, "band_intermediate": 0.90,
            "band_advanced": 0.95}
    marks, cum = {}, 0.0
    for i, (_, c) in enumerate(ranked, 1):
        cum += c
        for key, thr in want.items():
            if key not in marks and cum / total >= thr:
                marks[key] = i
    n = len(ranked)
    # tiny corpus that never reaches a threshold: fall back to quarters
    b = marks.get("band_beginner", max(1, n // 4))
    im = max(marks.get("band_intermediate", n // 2), b + 1)
    a = max(marks.get("band_advanced", n), im + 1)
    return {"band_beginner": min(b, n), "band_intermediate": min(im, n),
            "band_advanced": min(a, n)}


def main():
    ap = JsonArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lang", required=True)
    args = ap.parse_args()

    texts_dir = lang_dir(args.lang) / "texts"
    texts_dir.mkdir(parents=True, exist_ok=True)
    if not any(texts_dir.glob("*.txt")):
        out({"error": f"no .txt corpus found in {texts_dir}; drop some plain-"
                      f"text works (e.g. Project Gutenberg classics) there and "
                      f"re-run"})
        return

    dict_conn = open_db(args.lang, DICT_DB)   # must exist; errors clearly if not
    surface, per_file = count_tokens(texts_dir, args.lang)
    lemma_count, lemma_word, resolved = build_counts(dict_conn, surface,
                                                     args.lang)
    dict_conn.close()

    ranked = sorted(lemma_count.items(),
                    key=lambda kv: (-kv[1], kv[0][0], kv[0][1] or ""))

    dbfile = db_path(args.lang, FREQ_DB)
    conn = sqlite3.connect(dbfile)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO frequency (lemma, lemma_norm, pos, count, rank) "
        "VALUES (?,?,?,?,?)",
        [(lemma_word[(wn, pos)], wn, pos, round(cnt, 3), i)
         for i, ((wn, pos), cnt) in enumerate(ranked, 1)])
    total_tokens = sum(per_file.values())
    meta = {"total_lemmas": len(ranked), "total_tokens": total_tokens,
            "resolved_tokens": int(resolved), **coverage_cutoffs(ranked)}
    conn.executemany("INSERT INTO meta (key, value) VALUES (?,?)",
                     [(k, str(v)) for k, v in meta.items()])
    conn.commit()
    conn.close()

    out({"ok": True, "db": str(dbfile), "lemmas": len(ranked),
         "tokens": total_tokens, "resolved_tokens": int(resolved),
         "files": per_file,
         "top": [{"lemma": lemma_word[(wn, pos)], "pos": pos,
                  "count": round(cnt, 1)}
                 for (wn, pos), cnt in ranked[:15]]})


if __name__ == "__main__":
    main()
