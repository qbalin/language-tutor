# Language Tutor — spaced repetition beyond vocabulary

An LLM-driven language tutor that applies spaced repetition to the parts of a
language Anki can't reach: tenses, moods, declensions, conjugation, syntax,
idiom. Instead of fixed flashcards, the deck schedules **concepts** (e.g.
"ablative absolute", "subjunctive after verbs of fearing"). Each time a
concept comes due, the LLM improvises a fresh production exercise targeting
it — shaped by your own past mistakes on that concept — grades your written
answer against a real dictionary and a real grammar, and feeds the result back
into the scheduler. Like a human teacher who never forgets what you got wrong.

It is designed so that a **small local model** (e.g. a ~30B open-weights model)
is good enough to run it. Everything that can be deterministic is a script:

- **Scheduling** is [FSRS](https://github.com/open-spaced-repetition/py-fsrs)
  over SQLite. The model reports a 1–4 grade; it never computes intervals.
- **Morphology** is data, not model recall: the dictionary is built from
  [kaikki.org](https://kaikki.org)'s machine-readable Wiktionary extracts,
  including full inflection tables — "is *amāvisset* a real form of *amō*,
  and which one?" is a database lookup.
- **Grammar rules** come from a reference grammar you provide (PDF/EPUB/HTML/
  Markdown/text), indexed by section for full-text search, so every correction
  cites a section number instead of the model's imagination.

Of course it also works — better — with a strong model; the scripts and skills
are the same either way.

## Install

Requires Python 3.9+ (macOS/Linux).

```sh
git clone git@github.com:qbalin/language-tutor.git && cd language-tutor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Everything is driven through `./ll` (it picks up the venv automatically):

```sh
./ll session languages          # what's set up
./ll <command> --help           # usage for any command
```

## Set up a language (example: Latin)

If you use Claude Code, you can skip the commands below: open the repo, say
"set up Latin", and the `setup-language` skill walks through these steps with
you (including which grammar formats index best). Manually:

```sh
# 1. Dictionary: downloads the kaikki.org Wiktionary extract and builds
#    languages/latin/dictionary.db (Latin: ~105 MB download, several minutes)
./ll ingest_dictionary --lang latin

# 2. Grammar: drop a reference grammar into languages/latin/grammar/
#    (e.g. Allen & Greenough's "New Latin Grammar" — public domain, easy to
#    find as HTML or PDF), then index it:
./ll ingest_grammar --lang latin

# 3. Check:
./ll session start --lang latin     # should report "ready": true
```

Any language on kaikki.org works the same way. If the language name is spelled
differently there (e.g. "Ancient Greek"), pass `--kaikki-name "Ancient Greek"`,
or `--url` for a direct link.

## Run a session

With [Claude Code](https://claude.com/claude-code) (or any agent harness that
can run shell commands), open the repo and say:

> I want to practice Latin

The `tutor` skill (`.claude/skills/tutor/SKILL.md`) drives the session by
looping on `./ll session next`, a state machine that tells the model what to
do at each point — so the procedure lives in code, not in the model's memory:

1. `session next` hands the model the next due card (or the placement quiz,
   the mistake inbox, or the next uncovered topic — whatever the session
   needs),
2. the model writes a fresh set of production exercises for that concept
   (translate into the target language, or answer a prompt in it), targeting
   your recorded weaknesses on that card,
3. verifies your answers with `./ll dict lookup` / `./ll dict inflections` /
   `./ll grammar search` and explains corrections with citations,
4. grades the card (`./ll cards grade`) — FSRS decides when you see it again,
5. mistakes unrelated to the current card go to an inbox
   (`./ll cards inbox add`) and are turned into new or updated cards at the
   end of the session, with your confirmation,
6. when the deck is empty, `session next` proposes the earliest grammar topic
   not yet covered by a card (`./ll session next-topic`).

### Using a small local model

The scripts do the hard part; the model only needs to write/grade single
sentences and follow instructions that arrive exactly when they apply:
`./ll session next` is a state machine that says what to do at each step, and
command outputs carry `note` fields with the follow-up duties. Several
affordances exist specifically for small models:

- `./ll dict lookup` takes many words at once, so one call verifies a whole
  answer — tool round-trips, not tokens, dominate local-model latency.
- `./ll cards grade ... --pairs-file exercises.json` reads the exercise set
  from a JSON file (or stdin with `-`), avoiding shell-quoting of sentences
  full of apostrophes and accents — the classic small-model failure.
- Every error, including bad CLI arguments, comes back as JSON with a usage
  hint, so the model can self-correct without parsing stderr.

Keep the model's context lean:

- `.claude/settings.json` pre-authorizes `./ll` and denies web tools and
  subagents.
- Start Claude Code with `--strict-mcp-config` so no MCP servers are loaded.
- Keep global (`~/.claude`) skills and CLAUDE.md minimal for the account that
  runs the tutor.
- All durable state (deck, mistakes, inbox) lives in SQLite, so a custom
  driver can truncate conversation history aggressively — only the current
  card's exchange matters.

Runtime settings that matter (Ollama / llama.cpp):

- **Context**: 16–32k (`num_ctx`) is plenty for a session; more just slows
  prefill. Rely on prompt caching (automatic per slot in both servers) so the
  system prompt and skill are not reprocessed every turn.
- **Quantization**: Q4_K_M is the floor for ~30B instruction followers; prefer
  Q5/Q6 if RAM allows. Quantizing the KV cache to q8_0 buys context cheaply.
- **Sampling**: temperature ~0.2–0.4. CLI calls and JSON parsing punish
  creative sampling; exercise variety comes from the card's mistake history,
  not from temperature.
- **Reasoning effort**: do *not* run a small model in low-effort/no-think
  mode here. Everything deterministic is already a script; what remains —
  composing exercises, judging answers against retrieved evidence, following
  the loop — is exactly what degrades first without thinking, and it fails as
  skipped verification steps, not as slower answers. Keep a moderate thinking
  budget and get speed from the batching, caching, and truncation above.
  (Low effort is fine for a strong model.)

Nothing in `scripts/` depends on the harness: each command is a plain CLI that
prints JSON, so a minimal tool-calling loop around a local model (Ollama,
llama.cpp, ...) works too — expose "run `./ll ...`" as the only tool and reuse
`.claude/skills/tutor/SKILL.md` as the system prompt.

## Commands

| Command | Purpose |
|---|---|
| `./ll session start --lang X` | What exists for the language, what's missing, what's due |
| `./ll session next --lang X` | State machine: what the tutor should do next, with the data inline |
| `./ll session next-topic --lang X` | Earliest grammar section not yet covered by a card |
| `./ll ingest_dictionary --lang X` | Build dictionary DB from kaikki.org |
| `./ll ingest_grammar --lang X` | Index grammars from `languages/X/grammar/` |
| `./ll dict lookup WORD...` | Identify words (lemma or inflected form + tags), whole sentences at a time |
| `./ll dict translate PHRASE` | English → target-language candidates |
| `./ll dict inflections LEMMA --tags "..."` | List attested forms of a lemma |
| `./ll grammar search "..."` / `show REF` / `toc` | Search / read / list grammar sections |
| `./ll cards due / create / grade / show / list / stats` | FSRS concept deck |
| `./ll cards inbox add / list / resolve` | Park and triage off-topic mistakes |

All state lives under `languages/<lang>/` (dictionary.db, grammar.db,
cards.db, grammar/ sources). That directory is your personal data and is
`.gitignore`d — deleting `cards.db` resets your progress for that language;
the dictionary and grammar are rebuildable.

## Design notes

- **Don't trust the model**: every judgment a weak model could hallucinate is
  either delegated to a script (scheduling, morphology) or forced through
  retrieval with citations (grammar rules, word senses).
- **Mistake inbox instead of mid-session card edits**: misattributed errors
  are the most likely model failure, so tangential mistakes are parked and
  resolved with the student at session end, not silently written into the
  schedule.
- **Cards are concepts, not sentences**: the card stores the concept, grammar
  refs, and a log of your recent mistakes on it — the exercise is regenerated
  every review, so you can't memorize the card instead of the grammar.
