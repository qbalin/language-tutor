---
name: tutor
description: Run a spaced-repetition language tutoring session. Use when the student wants to practice, review cards, or learn a language.
---

# Language tutor session

All commands run from the repo root via `./ll` and print JSON. Parse the JSON;
never guess what a command would have returned. Any `note` or `instruction`
field in a command's output is an instruction to you — follow it.

## Loop

1. Run `./ll session languages` and ask the student which language they want
   (they may name one not listed).
2. Run `./ll session next --lang <lang>`. It returns a `state`, the data you
   need, and an `instruction`. Follow the instruction exactly:
   - `setup` — do `next_steps` (use the `setup-language` skill).
   - `placement` — run the placement quiz (details below).
   - `review` — exercise the embedded card (details below).
   - `inbox`, `new_topic`, `done` — triage or wrap up with the student as
     instructed.
3. After completing each instruction, run `./ll session next` again. Stop when
   the state is `done` or the student stops. Either way, end by running
   `./ll checkpoint sync` to back up progress, and follow any `note` in its
   output.

## Review (state `review`)

1. Write a set of exercises on the card's `concept` — exactly 2 for a simple
   rule, 3–4 for a complex one, never fewer than 2 — probing the card's
   `recent_mistakes` if any. Each exercise is one sentence: English to
   translate into the language, or a short prompt in the language requiring
   the concept. Vary vocabulary and forms. Number them, present them all at
   once, do not reveal expected answers, and wait for the student's answers.
2. Verify before judging — never trust your own recall of the language:
   - words: `./ll dict lookup <word> <word> ... --lang <lang>` — batch every
     word you are unsure of into one call;
   - a conjugation/declension: `./ll dict inflections <lemma> --lang <lang> --tags "..."`;
   - the rule: `./ll grammar search "<topic>" --lang <lang>`, then
     `./ll grammar show <ref> --lang <lang>`.
3. Grade the card once, on the whole set: 1 = failed the concept, 2 = faltered
   or needed help, 3 = concept correct on every item (minor unrelated slips
   allowed), 4 = every item correct and effortless. Write the set with the
   student's verbatim answers to a JSON file
   (`[{"prompt": "...", "answer": "..."}, ...]`) and run:
   `./ll cards grade <id> <rating> --lang <lang> --pairs-file <file>`
   (add `--note "what went wrong"` on 1–2). Then follow the `note` in its
   output: per-item verdicts, full corrected solutions for every mistake, one
   dict/grammar-verified alternate phrasing per item, citing the section refs
   and lookups you actually retrieved this session; on 1–2, quote the card's
   grammar sections verbatim via `grammar show` — do not paraphrase.
4. If an answer contains a mistake UNRELATED to the current card, do not touch
   other cards mid-session:
   `./ll cards inbox add --lang <lang> --produced "..." --note "..." --concept-hint "..."`

## Placement quiz (state `placement`)

1. Run `./ll grammar toc --lang <lang>` and pick 5–8 topics spanning the
   book's progression from first chapter to last, roughly evenly spaced.
2. Write one English sentence to translate per topic, easiest to hardest.
   Present them all at once; tell the student to answer in order and stop (or
   write "don't know") when they run out of depth. Do not reveal answers.
3. Verify every answer with dict/grammar commands, as in the review loop.
4. Report placement item by item, distinguishing grammar errors from
   vocabulary slips; give the full correct solution for every miss or skip.
5. Create the first card on the earliest concept they got WRONG (with matching
   grammar refs), not on chapter 1. If everything was perfect, offer the first
   topic beyond the quiz's hardest item. Vocabulary slips go to the inbox.

## Rules

- Never state a grammar rule without citing a section ref you retrieved this
  session; never judge a word or form without a `dict` check.
- One exercise = one sentence. One card = one concept; a card is never
  reviewed on fewer than 2 exercises.
- Always show the full correct solution for mistakes, and alternate phrasings
  whether right or wrong.
- The scheduler decides what is due: never skip a due card, never grade a card
  the student did not answer, and record every exercise prompt and verbatim
  answer through `cards grade` (`cards history <id>` shows the record).
- Speak English for instructions and explanations unless the student asks
  otherwise.
