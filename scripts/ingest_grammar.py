"""Index grammar reference files into a searchable per-language database.

Drop grammar files (PDF, EPUB, HTML, Markdown, or plain text) into
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
import sqlite3
import sys
from pathlib import Path

from common import (CARDS_DB, GRAMMAR_DB, JsonArgumentParser, db_path,
                    lang_dir, out)

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


# Publisher headings marked as styled paragraphs rather than <h*> tags
# (class contains "head", e.g. Wheelock's p.chapterHeadA/B/C). <p> only:
# a div with a head class typically wraps the real <h*> tag. The trailing
# letter of the class carries the depth (HeadA > HeadB > HeadC).
STYLED_HEAD_RE = re.compile(
    r'(?is)<p\b[^>]*class="([^"]*head[^"]*)"[^>]*>(.*?)</p>')


def styled_rank(cls):
    m = re.search(r"head\s*([a-z0-9]?)", cls, re.I)
    return {"a": 3, "b": 4}.get(((m and m.group(1)) or "").lower(), 5)


def split_html(raw: str, fallback_title: str):
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    raw = STYLED_HEAD_RE.sub(
        lambda m: '<h{0} data-styled="y">{1}</h{0}>'.format(
            styled_rank(m.group(1)), m.group(2)), raw)

    def strip_tags(s):
        s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", s)
        s = re.sub(r"<[^>]+>", " ", s)
        return clean(htmllib.unescape(s))

    parts = re.split(r"(?is)<h([1-6])([^>]*)>(.*?)</h\1>", raw)
    # parts = [preamble, level, attrs, title, level, attrs, title, body, ...]
    if len(parts) < 5:
        body = strip_tags(raw)
        if body:
            yield fallback_title, body
        return
    preamble = strip_tags(parts[0])
    if len(preamble) > 200:
        yield "preamble", preamble
    stack = []  # (level, title) of the headings enclosing the current one
    for i in range(1, len(parts) - 3, 4):
        level, styled = int(parts[i]), "data-styled" in parts[i + 1]
        title = strip_tags(parts[i + 2])
        body = strip_tags(parts[i + 3])
        while stack and stack[-1][0] >= level:
            stack.pop()
        # styled headings inherit their region: "Irregular Vīs (GRAMMATICA)"
        # — the parent tells apparatus subsections apart from grammar ones
        full = (f"{title} ({stack[-1][1][:40]})" if styled and stack and title
                else title)
        stack.append((level, title))
        if body:
            yield full or "untitled", body


def read_html(path: Path):
    raw = path.read_text(encoding="utf-8", errors="replace")
    yield from split_html(raw, path.stem)


def epub_spine_docs(zf):
    """Content-document paths in reading order, via container.xml -> OPF."""
    import posixpath
    import urllib.parse
    import xml.etree.ElementTree as ET

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    container = ET.fromstring(zf.read("META-INF/container.xml"))
    opf_path = next(el.get("full-path") for el in container.iter()
                    if local(el.tag) == "rootfile" and el.get("full-path"))
    opf_dir = posixpath.dirname(opf_path)
    opf = ET.fromstring(zf.read(opf_path))
    manifest, spine = {}, []
    for el in opf.iter():
        if local(el.tag) == "item":
            manifest[el.get("id")] = (el.get("href"), el.get("media-type") or "")
        elif local(el.tag) == "itemref":
            spine.append(el.get("idref"))
    docs = []
    for idref in spine:
        href, media = manifest.get(idref, (None, ""))
        if not href or "html" not in media:
            continue
        docs.append(posixpath.normpath(
            posixpath.join(opf_dir, urllib.parse.unquote(href))))
    return docs


def read_epub(path: Path):
    import zipfile
    with zipfile.ZipFile(path) as zf:
        try:
            docs = epub_spine_docs(zf)
        except Exception as exc:
            print(f"  {path.name}: could not read EPUB spine ({exc}); "
                  f"falling back to all HTML members", file=sys.stderr)
            docs = []
        if not docs:
            docs = sorted(n for n in zf.namelist()
                          if n.lower().endswith((".xhtml", ".html", ".htm")))
        for doc in docs:
            try:
                raw = zf.read(doc).decode("utf-8", errors="replace")
            except KeyError:
                continue
            yield from split_html(raw, Path(doc).stem)


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
           ".epub": read_epub, ".md": read_text, ".txt": read_text}


# --------------------------------------------------------- ref migration
# Re-ingesting renumbers positional refs, but the deck (cards.grammar_refs,
# known_sections, the frontier setting) cites the old ones. An old ref may
# even still exist in the rebuilt index while pointing at entirely different
# content, so every citation is re-resolved by matching the old section's
# content into the new sections, not by checking whether the ref resolves.

def squash(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


def load_sections(lang):
    dbfile = db_path(lang, GRAMMAR_DB)
    if not dbfile.exists():
        return []
    conn = sqlite3.connect(dbfile)
    try:
        rows = conn.execute(
            "SELECT ref, title, content FROM sections ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def remap_deck_refs(lang, old_sections):
    cards_file = db_path(lang, CARDS_DB)
    if not cards_file.exists() or not old_sections:
        return None
    new = [(r, t, squash(c)) for r, t, c in load_sections(lang)]
    old_by_ref = {r: (t, c) for r, t, c in old_sections}
    cache, unmatched = {}, set()

    def match(old_title, blob):
        # a new section that starts inside the old one (finer splits land on
        # the first concept the old section contained) ...
        for nref, _, ncontent in new:
            if len(ncontent) >= 40 and ncontent[:80] in blob:
                return nref
        # ... or, for a chunk that began mid-concept, the new section that
        # contains the old chunk's opening ...
        if len(blob) >= 60:
            for nref, _, ncontent in new:
                if blob[:60] in ncontent:
                    return nref
        # ... or at worst the first new section with the same title
        return next((nref for nref, ntitle, _ in new if ntitle == old_title),
                    None)

    def remap(ref):
        ref = ref.strip()
        if not ref or ref not in old_by_ref:
            return ref  # not a citation into the old index; leave untouched
        if ref not in cache:
            old_title, old_content = old_by_ref[ref]
            target = match(old_title, squash(old_content))
            if target is None:
                unmatched.add(ref)
                target = ref
            cache[ref] = target
        return cache[ref]

    conn = sqlite3.connect(cards_file)
    conn.row_factory = sqlite3.Row
    changes = {}
    for row in conn.execute("SELECT id, grammar_refs FROM cards").fetchall():
        refs = [x.strip() for x in (row["grammar_refs"] or "").split(",")
                if x.strip()]
        remapped = list(dict.fromkeys(remap(x) for x in refs))
        if remapped != refs:
            conn.execute("UPDATE cards SET grammar_refs = ? WHERE id = ?",
                         (",".join(remapped), row["id"]))
            changes[f"card:{row['id']}"] = ",".join(remapped)
    try:
        for row in conn.execute("SELECT ref FROM known_sections").fetchall():
            nref = remap(row["ref"])
            if nref != row["ref"]:
                conn.execute(
                    "INSERT OR IGNORE INTO known_sections (ref, ts, reason) "
                    "SELECT ?, ts, reason FROM known_sections WHERE ref = ?",
                    (nref, row["ref"]))
                conn.execute("DELETE FROM known_sections WHERE ref = ?",
                             (row["ref"],))
                changes[f"known:{row['ref']}"] = nref
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'frontier'").fetchone()
        if row and remap(row["value"]) != row["value"]:
            conn.execute("UPDATE settings SET value = ? WHERE key = 'frontier'",
                         (remap(row["value"]),))
            changes["frontier"] = remap(row["value"])
    except sqlite3.OperationalError:
        pass  # deck predates the known_sections/settings tables
    conn.commit()
    conn.close()
    return {"remapped": changes, "unmapped_citations": sorted(unmatched)}


def main():
    ap = JsonArgumentParser(description=__doc__,
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
        out({"error": f"no grammar files found in {src_dir}; drop a PDF/EPUB/"
                      f"HTML/Markdown/text grammar there and re-run"})
        sys.exit(1)

    old_sections = load_sections(args.lang)
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
    result = {"ok": True, "db": str(dbfile), "sections": n, "files": per_file}
    migration = remap_deck_refs(args.lang, old_sections)
    if migration:
        result["ref_migration"] = migration
        if migration["unmapped_citations"]:
            result["note"] = ("some deck citations could not be repointed to "
                              "the rebuilt sections; review them with "
                              "./ll cards list and fix the refs by hand")
    out(result)


if __name__ == "__main__":
    main()
