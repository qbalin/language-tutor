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

2. **Grammar**: ask the student to copy a grammar reference (PDF, HTML,
   Markdown, or plain text) into `languages/<lang>/grammar/`. A well-structured
   reference grammar with numbered sections works best. Then:

   ```
   ./ll ingest_grammar --lang <lang>
   ```

   Re-run this any time files are added or replaced; it rebuilds the index.

3. **Verify**: `./ll session start --lang <lang>` must report `"ready": true`.
