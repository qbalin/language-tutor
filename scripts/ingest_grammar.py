"""Index grammar reference files into a searchable per-language database.

Drop grammar files (PDF, HTML, Markdown, or plain text) into
languages/<lang>/grammar/ and run:

  python scripts/ingest_grammar.py --lang latin

The text is split into sections (using the PDF table of contents or
HTML/Markdown headings when available) and indexed with SQLite FTS5 into
languages/<lang>/grammar.db. Sections keep a `ref` (section number when one
can be detected in the heading, e.g. "121.3") so corrections can cite the
grammar precisely. Re-running rebuilds the whole index.

PDF support needs pymupdf: .venv/bin/pip install pymupdf
"""
import argparse
import html as htmllib
import re
import sys
from pathlib import Path

from common import GRAMMAR_DB, db_path, lang_dir, out

SCHEMA = """
DROP TABLE IF EXISTS sections;
DROP TABLE IF EXISTS grammar_fts;
CREATE TABLE sections (
  id INTEGER PRIMARY KEY,
  source TEXT,
  ref TEXT,
  title TEXT,
  content TEXT
);
CREATE VIRTUAL TABLE grammar_fts USING fts5(title, content, ref UNINDEXED, source UNINDEXED);
"""

MAX_SECTION = 6000   # split anything longer, keeps retrieval chunks usable
TARGET_CHUNK = 2500

REF_RE = re.compile(r"^\W{0,3}((?:§\s*)?\d+(?:\.\d+)*[a-z]?)\b")


def clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_long(title, content):
    """Yield (suffix, chunk) pieces no longer than MAX_SECTION."""
    if len(content) <= MAX_SECTION:
        yield "", content
        return
    paras = content.split("\n\n")
    buf, n = [], 1
    size = 0
    for p in paras:
        if size + len(p) > TARGET_CHUNK and buf:
            yield ("" if n == 1 else f"/{n}"), "\n\n".join(buf)
            buf, size = [], 0
            n += 1
        buf.append(p)
        size += len(p)
    if buf:
        yield ("" if n == 1 else f"/{n}"), "\n\n".join(buf)


def extract_ref(title: str, fallback: str) -> str:
    m = REF_RE.match(title or "")
    if m:
        return m.group(1).replace("§", "").strip()
    return fallback


# ---------------------------------------------------------------- readers

def read_pdf(path: Path):
    try:
        import fitz  # pymupdf
    except ImportError:
        sys.exit("pymupdf is required for PDF grammars: "
                 ".venv/bin/pip install pymupdf")
    doc = fitz.open(path)
    toc = doc.get_toc()
    if toc:
        # section i spans from its page to the next toc item's page
        for i, (level, title, page) in enumerate(toc):
            end = toc[i + 1][2] if i + 1 < len(toc) else doc.page_count
            text = "".join(doc[p].get_text()
                           for p in range(max(page - 1, 0), max(end, page)))
            yield title.strip(), clean(text)
    else:
        for p in range(doc.page_count):
            yield f"page {p + 1}", clean(doc[p].get_text())


def read_html(path: Path):
    raw = path.read_text(encoding="utf-8", errors="replace")
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)

    def strip_tags(s):
        s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", s)
        s = re.sub(r"<[^>]+>", " ", s)
        return clean(htmllib.unescape(s))

    parts = re.split(r"(?is)<h([1-4])[^>]*>(.*?)</h\1>", raw)
    # parts = [preamble, level, title, body, level, title, body, ...]
    if len(parts) < 4:
        yield path.stem, strip_tags(raw)
        return
    preamble = strip_tags(parts[0])
    if len(preamble) > 200:
        yield "preamble", preamble
    for i in range(1, len(parts) - 2, 3):
        title = strip_tags(parts[i + 1])
        body = strip_tags(parts[i + 2])
        if body:
            yield title or "untitled", body


def read_text(path: Path):
    raw = path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"(?m)^(#{1,4} .*)$", raw)
    if len(parts) < 3:
        yield path.stem, clean(raw)
        return
    if clean(parts[0]):
        yield "preamble", clean(parts[0])
    for i in range(1, len(parts) - 1, 2):
        yield parts[i].lstrip("# ").strip(), clean(parts[i + 1])


READERS = {".pdf": read_pdf, ".html": read_html, ".htm": read_html,
           ".md": read_text, ".txt": read_text}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lang", required=True)
    ap.add_argument("--file", default=None,
                    help="ingest a single file instead of everything in "
                         "languages/<lang>/grammar/")
    args = ap.parse_args()

    src_dir = lang_dir(args.lang) / "grammar"
    src_dir.mkdir(parents=True, exist_ok=True)
    files = ([Path(args.file)] if args.file
             else sorted(p for p in src_dir.iterdir()
                         if p.suffix.lower() in READERS))
    if not files:
        out({"error": f"no grammar files found in {src_dir}; drop a PDF/HTML/"
                      f"Markdown/text grammar there and re-run"})
        sys.exit(1)

    import sqlite3
    dbfile = db_path(args.lang, GRAMMAR_DB)
    conn = sqlite3.connect(dbfile)
    conn.executescript(SCHEMA)

    n = 0
    per_file = {}
    for path in files:
        count = 0
        for idx, (title, content) in enumerate(READERS[path.suffix.lower()](path), 1):
            if not content or len(content) < 40:
                continue
            ref = extract_ref(title, f"s{idx}")
            for suffix, chunk in split_long(title, content):
                n += 1
                count += 1
                conn.execute(
                    "INSERT INTO sections (id, source, ref, title, content) "
                    "VALUES (?,?,?,?,?)",
                    (n, path.name, ref + suffix, title[:200], chunk))
                conn.execute(
                    "INSERT INTO grammar_fts (title, content, ref, source) "
                    "VALUES (?,?,?,?)",
                    (title[:200], chunk, ref + suffix, path.name))
        per_file[path.name] = count
    conn.commit()
    conn.close()
    out({"ok": True, "db": str(dbfile), "sections": n, "files": per_file})


if __name__ == "__main__":
    main()
