"""Build a per-language SQLite dictionary from a kaikki.org Wiktionary extract.

kaikki.org publishes machine-readable JSONL of all Wiktionary entries per
language (produced by the wiktextract project). This script downloads the
extract, streams it, and loads three things into languages/<lang>/dictionary.db:

  entries   one row per Wiktionary entry (lemma or form-of entry)
  forms     every inflected form listed in an entry's inflection table,
            mapped back to its entry -- this is what makes conjugation and
            declension checks a deterministic lookup
  gloss_fts FTS5 index over English glosses, for English -> target-language
            lookups

Reusable for any language on kaikki.org:
  python scripts/ingest_dictionary.py --lang latin
  python scripts/ingest_dictionary.py --lang "ancient greek" --kaikki-name "Ancient Greek"
  python scripts/ingest_dictionary.py --lang latin --file /path/to/dump.jsonl.gz
"""
import argparse
import gzip
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from common import DICT_DB, JsonArgumentParser, db_path, lang_dir, normalize, out

# wiktextract emits pseudo-form rows describing the table itself; skip them.
SKIP_FORM_TAGS = {
    "table-tags", "inflection-template", "class", "multiword-construction",
    "romanization",
}
SKIP_FORMS = {"", "-", "—"}

SCHEMA = """
DROP TABLE IF EXISTS entries;
DROP TABLE IF EXISTS forms;
DROP TABLE IF EXISTS gloss_fts;
DROP TABLE IF EXISTS meta;
CREATE TABLE entries (
  id INTEGER PRIMARY KEY,
  word TEXT NOT NULL,
  word_norm TEXT NOT NULL,
  pos TEXT,
  data TEXT NOT NULL
);
CREATE TABLE forms (
  form TEXT NOT NULL,
  form_norm TEXT NOT NULL,
  tags TEXT,
  entry_id INTEGER NOT NULL REFERENCES entries(id)
);
CREATE VIRTUAL TABLE gloss_fts USING fts5(gloss, entry_id UNINDEXED);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def default_url(kaikki_name: str) -> str:
    quoted = urllib.parse.quote(kaikki_name)
    compact = kaikki_name.replace(" ", "")
    return (f"https://kaikki.org/dictionary/{quoted}/"
            f"kaikki.org-dictionary-{compact}.jsonl.gz")


def download(url: str, dest: Path) -> Path:
    print(f"downloading {url} -> {dest}", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "language-tutor/1.0"})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if done % (50 << 20) < (1 << 20):
                pct = f" ({done * 100 // total}%)" if total else ""
                print(f"  {done >> 20} MB{pct}", file=sys.stderr)
    return dest


def open_jsonl(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "rt", encoding="utf-8")


def sense_summary(sense: dict):
    glosses = sense.get("glosses") or sense.get("raw_glosses") or []
    if not glosses:
        return None
    item = {"gloss": glosses[-1]}
    tags = sense.get("tags") or []
    if tags:
        item["tags"] = tags
    form_of = sense.get("form_of") or sense.get("alt_of")
    if form_of:
        words = [x.get("word") for x in form_of if x.get("word")]
        if words:
            item["form_of"] = words[0]
    return item


def ingest(jsonl: Path, lang: str, limit: int = 0) -> None:
    import sqlite3
    path = db_path(lang, DICT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    n_entries = n_forms = n_lines = 0
    entry_batch, form_batch, gloss_batch = [], [], []
    start = time.time()

    def flush():
        nonlocal entry_batch, form_batch, gloss_batch
        conn.executemany(
            "INSERT INTO entries (id, word, word_norm, pos, data) VALUES (?,?,?,?,?)",
            entry_batch)
        conn.executemany(
            "INSERT INTO forms (form, form_norm, tags, entry_id) VALUES (?,?,?,?)",
            form_batch)
        conn.executemany(
            "INSERT INTO gloss_fts (gloss, entry_id) VALUES (?,?)", gloss_batch)
        conn.commit()
        entry_batch, form_batch, gloss_batch = [], [], []

    with open_jsonl(jsonl) as fh:
        for line in fh:
            n_lines += 1
            if limit and n_entries >= limit:
                break
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            word, pos = e.get("word"), e.get("pos")
            if not word or not pos:
                continue
            senses = []
            for s in e.get("senses") or []:
                item = sense_summary(s)
                if item:
                    senses.append(item)
            if not senses:
                continue
            n_entries += 1
            eid = n_entries
            entry_batch.append((eid, word, normalize(word, lang), pos,
                                json.dumps({"senses": senses}, ensure_ascii=False)))
            for item in senses:
                if "form_of" not in item:
                    gloss_batch.append((item["gloss"], eid))
            seen = set()
            for f in e.get("forms") or []:
                form = (f.get("form") or "").strip()
                tags = f.get("tags") or []
                if form in SKIP_FORMS or SKIP_FORM_TAGS.intersection(tags):
                    continue
                key = (form, tuple(tags))
                if key in seen:
                    continue
                seen.add(key)
                n_forms += 1
                form_batch.append((form, normalize(form, lang),
                                   ",".join(tags), eid))
            if len(entry_batch) >= 5000:
                flush()
            if n_lines % 100000 == 0:
                print(f"  {n_lines} lines, {n_entries} entries, "
                      f"{n_forms} forms ({int(time.time() - start)}s)",
                      file=sys.stderr)
    flush()
    print("building indexes...", file=sys.stderr)
    conn.execute("CREATE INDEX idx_entries_norm ON entries(word_norm)")
    conn.execute("CREATE INDEX idx_forms_norm ON forms(form_norm)")
    conn.execute("CREATE INDEX idx_forms_entry ON forms(entry_id)")
    conn.execute("INSERT INTO meta VALUES ('lang', ?)", (lang,))
    conn.execute("INSERT INTO meta VALUES ('source', ?)", (str(jsonl.name),))
    conn.execute("INSERT INTO meta VALUES ('entries', ?)", (str(n_entries),))
    conn.execute("INSERT INTO meta VALUES ('forms', ?)", (str(n_forms),))
    conn.commit()
    conn.close()
    out({"ok": True, "db": str(path), "entries": n_entries, "forms": n_forms,
         "seconds": int(time.time() - start)})


def main():
    ap = JsonArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lang", required=True,
                    help="language name used for the local folder, e.g. latin")
    ap.add_argument("--kaikki-name", default=None,
                    help="language name as kaikki.org spells it "
                         "(default: --lang capitalized)")
    ap.add_argument("--url", default=None, help="override the download URL")
    ap.add_argument("--file", default=None,
                    help="use an already-downloaded .jsonl or .jsonl.gz instead "
                         "of downloading")
    ap.add_argument("--keep-download", action="store_true",
                    help="keep the downloaded dump after ingest")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N entries (for testing)")
    args = ap.parse_args()

    if args.file:
        jsonl = Path(args.file)
        downloaded = False
    else:
        name = args.kaikki_name or args.lang.strip().title()
        url = args.url or default_url(name)
        dest = lang_dir(args.lang) / "kaikki-dump.jsonl.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        jsonl = download(url, dest)
        downloaded = True

    ingest(jsonl, args.lang, args.limit)

    if downloaded and not args.keep_download:
        jsonl.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
