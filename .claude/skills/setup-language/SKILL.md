---
name: setup-language
description: Set up the dictionary and grammar for a new language. Use when session start reports a missing dictionary or grammar.
---

# Set up a language

1. **Dictionary** (downloads from kaikki.org — needs network; large languages
   take several minutes and a few GB of disk):

   ```
   ./ll ingest_dictionary --lang <lang>
   ```

   If the download 404s, the language is probably spelled differently on
   kaikki.org (check https://kaikki.org/dictionary/). Then use
   `--kaikki-name "Ancient Greek"` (exact kaikki spelling) or
   `--url <direct link to the .jsonl.gz>`.

2. **Grammar**: ask the student to copy a grammar reference into
   `languages/<lang>/grammar/`, and suggest the best format they can get,
   in this order:

   1. **Markdown (or plain text) with numbered headings** — e.g.
      `## 28.1 Purpose clauses`. Content is indexed verbatim, and the leading
      number in a heading becomes the section's citable ref. Keep each
      section under ~6,000 characters and write paradigms as plain-text or
      pipe tables.
   2. **HTML** with `<h1>`–`<h4>` headings — good structure, but tables are
      flattened during tag-stripping.
   3. **EPUB** — works, but paradigm tables get scrambled and refs are
      positional (`s37`) rather than matching the book's own numbering.
   4. **PDF** — last resort; needs `pymupdf` and an embedded table of
      contents. Scanned PDFs yield nothing.

   Then:

   ```
   ./ll ingest_grammar --lang <lang>
   ```

   Re-run this any time files are added or replaced; it rebuilds the index.

3. **Verify**: `./ll session start --lang <lang>` must report `"ready": true`.

4. **Prune apparatus (optional)**: if `./ll session next-topic` proposes
   non-topics — the book's own reading passages, exercise sections, per-chapter
   section marks — list those title substrings in
   `languages/<lang>/grammar_skip.txt` (one per line, `#` comments; see
   `languages/latin/grammar_skip.txt` for an example). Universal front matter
   (contents, preface, index, ...) is already filtered; the file is only for
   apparatus named by this particular book. No re-ingest needed.
