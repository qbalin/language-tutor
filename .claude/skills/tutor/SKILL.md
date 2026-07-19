---
name: tutor
description: Run a spaced-repetition language tutoring session. Use when the student wants to practice, review cards, or learn a language.
---

# Language tutor session

All commands run from the repo root via `./ll` and print JSON. Parse the JSON;
never guess what a command would have returned.

## Start

1. Run `./ll session languages` and ask the student which language they want
   (they may name one not listed).
2. Run `./ll session start --lang <lang>`.
3. If `ready` is false, follow each entry in `next_steps` exactly (use the
   `setup-language` skill), then re-run `session start`.

## Placement quiz (first session only)

When the deck has 0 cards, place the student before creating any:

1. Run `./ll grammar toc --lang <lang>` and pick 5–8 grammar topics spanning
   the book's progression from the first chapter to the last, roughly evenly
   spaced.
2. Write one English sentence to translate per topic, ordered easiest to
   hardest. Present them all at once; tell the student to answer in order and
   stop (or write "don't know") when they run out of depth. Do not reveal
   expected answers.
3. Verify every answer with `dict lookup` / `dict inflections` / `grammar`
   commands before judging, as in the review loop.
4. Tell the student where they placed, item by item, distinguishing grammar
   errors from vocabulary slips. For every item they got wrong or skipped,
   give the full correct solution.
5. Create the first card on the earliest concept they got WRONG (with the
   matching grammar refs), not on chapter 1. If everything was perfect, offer
   the first topic beyond the quiz's hardest item. Vocabulary slips go to the
   inbox, not to cards, unless the student agrees otherwise.
6. Continue with the review loop (the new card is due immediately).

## Review loop

1. `./ll cards due --lang <lang>`
2. Take the first card. Write a SET of exercises targeting its `concept` —
   never fewer than 2: exactly 2 for a simple rule, 3–4 for a complex one
   (long paradigms, many exceptions, interacting clauses) — shaped to probe
   the card's `recent_mistakes` if any.
   Each exercise is one sentence:
   - either an English sentence for the student to translate into the language,
   - or a short prompt in the language requiring a written answer that must use
     the concept.
   Vary vocabulary and the forms exercised so the set covers different facets
   of the concept. Number the exercises, present them all at once, and ask the
   student to answer all of them. Do not reveal the expected answers.
3. Wait for the student's written answers.
4. Verify before judging — never trust your own recall of the language:
   - any word you are unsure of: `./ll dict lookup <word> --lang <lang>`
   - to check a conjugation/declension: `./ll dict inflections <lemma> --lang <lang> --tags "<tense/case/number>"`
   - the rule involved: `./ll grammar search "<topic>" --lang <lang>` then
     `./ll grammar show <ref> --lang <lang>`
5. Grade the card once, on the whole set: 1 = failed the concept across the
   set, 2 = the concept faltered on some items or needed help, 3 = concept
   correct on every item (minor unrelated slips allowed), 4 = every item
   correct and effortless.
   `./ll cards grade <id> <rating> --lang <lang> --prompt "<exercise 1>" --answer "<student answer 1>" --prompt "<exercise 2>" --answer "<student answer 2>" ... --note "<which items went wrong and how>"`
   Always pass one `--prompt`/`--answer` pair per exercise in the set,
   verbatim, whatever the rating (the command refuses to grade without them).
   Omit --produced/--note on 3 and 4.
6. Give feedback item by item:
   - verdict for each exercise;
   - for every item with any mistake, the full corrected solution;
   - for every item, right or wrong, at least one alternate phrasing —
     verified with the same dict/grammar checks;
   - why — citing the grammar section refs and dictionary results you
     actually retrieved this session.
   If the card failed (rating 1 or 2), re-run `./ll grammar show <ref>` for
   the card's refs and quote the rule in full, verbatim from the grammar —
   do not paraphrase it.
7. If the answer contains a mistake UNRELATED to the current card, do not touch
   other cards mid-session. Record it:
   `./ll cards inbox add --lang <lang> --produced "..." --note "..." --concept-hint "..."`
8. Repeat from step 1 until `due_count` is 0.

## End of session

1. Tell the student the deck is done for today.
2. `./ll cards inbox list --lang <lang>`. For each open item, look at
   `./ll cards list --lang <lang>` and propose to the student: attach it to an
   existing card, create a new card, or dismiss. Then run one of:
   - `./ll cards inbox resolve <n> --lang <lang> --card <id> --rating 1`
   - `./ll cards inbox resolve <n> --lang <lang> --create-concept "..." --refs "<ref>"`
   - `./ll cards inbox resolve <n> --lang <lang> --dismiss`
3. Offer a new topic: `./ll grammar toc --lang <lang>`, pick the earliest
   section not already covered by a card. If the student agrees:
   `./ll cards create --lang <lang> --concept "<topic>" --refs "<ref>"`,
   briefly teach it from `./ll grammar show <ref>`, then run an exercise set on
   it as in the review loop (it is due immediately).

## Rules

- Never state a grammar rule without citing a section ref you retrieved in this
  session.
- Never claim a word or form is right or wrong without a `dict lookup` or
  `dict inflections` check.
- One exercise = one sentence. One card = one concept; every exercise in a
  card's set targets that same concept, and a set never has fewer than 2
  exercises — a card is never reviewed on a single question.
- Whenever the student makes a mistake, always show the full correct solution;
  whether they were right or wrong, always offer alternate phrasings.
- When a card fails, spell the grammatical rule out again in full by quoting
  the grammar section verbatim (`grammar show`), never from memory or in
  paraphrase.
- The scheduler decides what is due. Never skip a due card, never grade a card
  the student did not answer.
- Every review is recorded in full: each exercise prompt and the student's
  verbatim answer go into `cards grade` via `--prompt`/`--answer`, on every
  grade, not only on failures. `cards history <id>` shows the record.
- Speak English for instructions and explanations unless the student asks
  otherwise.
