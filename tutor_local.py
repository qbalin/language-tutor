#!/usr/bin/env python3
"""Minimal local-model tutor loop.

Drives a spaced-repetition tutoring session with a local Ollama model instead
of Claude Code. The model's ONLY tool is `./ll` -- nothing else can be
executed -- and thinking defaults to off so latency stays low.

Run `./tutor_local.py --help` for options. Thinking can be toggled with
`--think on|off`.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
LL = os.path.join(ROOT, "ll")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

# A session is long: a 3k-token system prompt plus every dict/grammar result
# accumulates fast, and at 16384 a real session overflowed, silently evicting
# the procedure the model is supposed to be following. These models advertise
# 262144, so the ceiling was ours, not theirs.
NUM_CTX = int(os.environ.get("LL_NUM_CTX", 65536))
# Low, not zero: tool calls should be near-deterministic, exercises still vary.
# Too low and a stuck model repeats one call forever -- see REPEAT_LIMIT.
TEMPERATURE = float(os.environ.get("LL_TEMPERATURE", 0.2))
# How many times the same command may be run verbatim before we intervene.
REPEAT_LIMIT = 3
# Attempts per Ollama call, for the malformed-tool-call 500s described in chat_raw.
API_RETRIES = 4

# The harness-specific preamble. The actual procedure and rules are loaded
# VERBATIM from CLAUDE.md and the skill files at startup (see build_system),
# so this stays in sync with what Claude Code itself would read.
HARNESS_PREAMBLE = """\
You are running as a standalone local-model tutor, not inside Claude Code.
Adapt to these harness facts, which override anything below that assumes a
richer agent:

- Your ONLY tool is `ll`: it runs this project's `./ll <args>` and returns
  JSON. You have no shell, no file access, no web, and no other tools.
- Because you cannot create files, `--pairs-file` is USELESS to you: a path
  you invent either does not exist or is a stale leftover from another
  session, which would record exercises the student never saw. Record an
  exercise set with `--pairs-json '[{"prompt": "...", "answer": "..."}, ...]'`
  or with repeated `--prompt`/`--answer` pairs. Never pass `--pairs-file`.
- Feedback the student cannot see does not count. Putting your verdicts in a
  `--note` argument is not telling them; `--note` is a private record. After
  grading, write the per-item verdicts as a plain message BEFORE any further
  tool call.
- You CANNOT invoke skills. Everything a skill would provide is already
  pasted below under its heading. When an instruction says "use the
  setup-language skill", follow the inlined SETUP-LANGUAGE SKILL text instead.
- Every `./ll ...` command quoted in an `instruction` or `note` field is an
  order: run it through the `ll` tool. Pass the subcommand as `command` and
  everything after it as `args`, e.g.
  {"command": "dict", "args": ["lookup","amaverunt","--lang","latin"]}.
- You will not be allowed to run `session next` while the student is still
  owed the verdicts on a set you just graded. Send them first.
- When you need the student to act (pick a language, answer exercises, confirm
  a topic), STOP calling tools and write a plain message; the harness relays
  their reply as the next user turn.
- Begin by running ll(command="session", args=["languages"]).

The project instructions (CLAUDE.md) and the tutor procedure (tutor skill)
follow, verbatim.
"""

# Files pasted verbatim into the system prompt, in order.
CONTEXT_FILES = [
    ("PROJECT INSTRUCTIONS (CLAUDE.md)", "CLAUDE.md"),
    ("TUTOR SKILL", ".claude/skills/tutor/SKILL.md"),
]
SETUP_SKILL = ("SETUP-LANGUAGE SKILL", ".claude/skills/setup-language/SKILL.md")

# Stands in for the setup skill when no language actually needs setting up.
# The skill is a fifth of the system prompt and every token of it competes with
# the procedure the model is running right now.
SETUP_STUB = """\
Every language already has its dictionary and grammar, so no setup procedure is
loaded. If the student asks for a language not in `session languages`, run
`./ll session start --lang <name>` and follow the `next_steps` it returns."""


def setup_needed():
    """True when some language is missing a dictionary or a grammar.

    Mirrors the existence check in session.cmd_languages so the prompt reflects
    the same reality the tool reports.
    """
    langs = os.path.join(ROOT, "languages")
    if not os.path.isdir(langs):
        return True
    for name in os.listdir(langs):
        d = os.path.join(langs, name)
        if not os.path.isdir(d):
            continue
        if not all(os.path.exists(os.path.join(d, db))
                   for db in ("dictionary.db", "grammar.db")):
            return True
    return False


def build_system():
    """Assemble the system prompt from the real project files at runtime."""
    files = list(CONTEXT_FILES)
    if setup_needed():
        files.append(SETUP_SKILL)
    parts = [HARNESS_PREAMBLE]
    for label, rel in files:
        path = os.path.join(ROOT, rel)
        try:
            with open(path, encoding="utf-8") as f:
                body = f.read().strip()
        except OSError as e:
            body = f"[could not read {rel}: {e}]"
        parts.append(f"\n===== {label} ({rel}) =====\n{body}")
    if not setup_needed():
        parts.append(f"\n===== SETUP-LANGUAGE =====\n{SETUP_STUB}")
    return "\n".join(parts)

LL_COMMANDS = ["session", "dict", "grammar", "cards", "checkpoint",
               "ingest_dictionary", "ingest_grammar"]

TOOLS = [{
    "type": "function",
    "function": {
        "name": "ll",
        "description": (
            "Run a ./ll tutor command and return its JSON stdout. `command` is "
            "the subcommand; `args` is everything after it. Every command "
            "needs --lang except `session languages`. Common forms:\n"
            '  session languages | session next --lang latin\n'
            '  dict lookup <word> <word> ... --lang latin\n'
            '  dict inflections <lemma> --lang latin --tags "ablative,singular"\n'
            '  dict sample --lang latin --count 15\n'
            '  grammar search "<topic>" --lang latin | grammar show <ref> --lang latin\n'
            '  cards due --lang latin | cards show <id> --lang latin\n'
            "  cards grade <id> <1-4> --lang latin --pairs-json "
            "'[{\"prompt\": \"...\", \"answer\": \"...\"}, ...]' [--note \"...\"]\n"
            '  cards inbox add --lang latin --produced "..." --note "..."'),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": LL_COMMANDS},
                "args": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["command", "args"],
        },
    },
}]


def chat_raw(model, messages, think=False):
    """One /api/chat turn, streaming off. `think` toggles reasoning.

    Retries on 5xx: these models intermittently emit a malformed <function>
    tool call that Ollama cannot parse, and it fails the whole request. It is
    a fresh sample away from working, so losing the session over it is silly.
    """
    body = json.dumps({
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "think": think,
        "stream": False,
        "options": {"temperature": TEMPERATURE, "num_ctx": NUM_CTX},
    }).encode()
    last = None
    for attempt in range(API_RETRIES):
        req = urllib.request.Request(
            OLLAMA + "/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.loads(r.read()), attempt
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = e
    raise last


def chat(model, messages, think=False):
    return chat_raw(model, messages, think)[0]["message"]


REASONING_RE = re.compile(
    r"<(think|thinking|reasoning)>.*?</\1>|^\s*</(think|thinking|reasoning)>",
    re.DOTALL | re.IGNORECASE | re.MULTILINE)


def strip_reasoning(content):
    """Keep private reasoning out of the student's view.

    With thinking off, several models still emit a reasoning block in the
    content channel -- sometimes only the closing tag -- and the student ends
    up reading the model's scratchpad instead of a lesson.
    """
    return REASONING_RE.sub("", content).strip()


def run_ll(args):
    """Execute ./ll with the given argv. This is the ONLY thing we ever run."""
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return json.dumps({"error": "args must be a list of strings"})
    try:
        p = subprocess.run([LL, *args], capture_output=True, text=True,
                           cwd=ROOT, timeout=120)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "ll command timed out"})
    out = p.stdout.strip()
    if p.returncode != 0:
        return json.dumps({"error": f"exit {p.returncode}",
                           "stderr": p.stderr.strip(), "stdout": out})
    return out or json.dumps({"ok": True, "stdout": ""})


class FeedbackGate:
    """Refuses to advance the session while the student is owed feedback.

    The commonest failure of a small model here is to grade a card, write its
    verdicts into the private `--note` field, and immediately run
    `session next` -- so the only thing the student ever sees is triage for
    some unrelated card. Instructions alone did not stop it; this makes the
    state unreachable.
    """

    def __init__(self):
        self.pending = False
        self.repeats = {}
        # Refusals since the last message. A refusal the model answers with the
        # same call again is a livelock, so the wording escalates and the
        # harness eventually forces a plain-text turn.
        self.blocks = 0

    def blocked(self, argv):
        """The error to return instead of running argv, or None to allow it."""
        key = "\x00".join(argv)
        n = self.repeats.get(key, 0)
        if n >= REPEAT_LIMIT:
            # A stuck model will re-run one lookup until it burns the step
            # budget. Returning the same output again cannot help it.
            return json.dumps({
                "error": f"you have already run this exact command {n} times",
                "instruction": "Re-running it will return the same thing. Use "
                               "what you already have: if you still cannot "
                               "decide, say so to the student in a message "
                               "rather than looking it up again."})
        if self.pending and argv[:2] == ["session", "next"]:
            if self.blocks:
                return json.dumps({
                    "error": "still blocked, and calling it again cannot help",
                    "instruction": "EMIT NO TOOL CALL AT ALL in your next "
                                   "turn. Reply with plain text only: the "
                                   "per-item verdicts and corrections for the "
                                   "set you just graded. Nothing else will "
                                   "unblock the session."})
            return json.dumps({
                "error": "the student has not been given their verdicts yet",
                "instruction": "You graded a set but have not told the student "
                               "anything about it. Stop calling tools and write "
                               "them a message now: a per-item verdict, the full "
                               "correction for every mistake, and one "
                               "dict/grammar-verified alternate phrasing per "
                               "item. `session next` will work once you have."})
        return None

    def needs_nudge(self):
        """True when refusals alone are not breaking the loop."""
        return self.blocks >= 3

    NUDGE = ("[harness] You keep issuing a call that is blocked. Write the "
             "per-item verdicts and corrections for the set you just graded "
             "as plain text now. Do not call any tool in your next turn.")

    def observe(self, argv, output):
        """Note a recorded grade, so the next `session next` is refused."""
        key = "\x00".join(argv)
        self.repeats[key] = self.repeats.get(key, 0) + 1
        if argv[:2] != ["cards", "grade"]:
            return
        try:
            res = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return
        # The duplicate guard reports ok with recorded=false; nothing new is
        # owed to the student in that case.
        if res.get("ok") and res.get("recorded") is not False:
            self.pending = True

    def note_refusal(self):
        self.blocks += 1

    def message_sent(self):
        self.pending = False
        self.blocks = 0
        self.repeats.clear()


def call_argv(arguments):
    """Normalise a model's tool arguments into an argv list.

    Accepts the documented {"command": ..., "args": [...]} shape and still
    tolerates the older bare-array form, plus the JSON-encoded string some
    models emit instead of an object.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return []
    if isinstance(arguments, list):
        return [str(x) for x in arguments]
    if not isinstance(arguments, dict):
        return []
    args = arguments.get("args")
    if not isinstance(args, list):
        args = [args] if isinstance(args, str) else []
    cmd = arguments.get("command")
    argv = ([cmd] if isinstance(cmd, str) and cmd else []) + list(args)
    return [str(x) for x in argv]


def guarded_run_ll(gate, argv):
    """run_ll, but with the feedback gate applied. Used by every caller."""
    refusal = gate.blocked(argv)
    if refusal is not None:
        gate.note_refusal()
        return refusal
    out = run_ll(argv)
    gate.observe(argv, out)
    return out


def main():
    ap = argparse.ArgumentParser(
        prog="tutor_local.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Run a spaced-repetition tutoring session with a local Ollama "
            "model.\nThe model's only tool is `./ll`; it can execute nothing "
            "else."),
        epilog="""\
examples:
  ./tutor_local.py                          default model, thinking off
  ./tutor_local.py --think on               enable the model's reasoning
  ./tutor_local.py --model qwen3.6:35b-a3b-nvfp4   use the MLX build
  LL_MODEL=qwen3-coder:30b ./tutor_local.py set the model via env var

during a session:
  type your answers at the `You:` prompt
  /quit (or /exit, /q)   end the session (asks the tutor to sync progress)
  each `. ll ...` line shows a command the model ran

thinking:
  off (default)  fastest; recommended -- these models otherwise spend their
                 whole budget "thinking" and stall the session
  on             slower, occasionally better reasoning; watch for long pauses

env vars:
  LL_MODEL       default model name (overridden by --model)
  OLLAMA_HOST    Ollama base URL (default http://localhost:11434)
""")
    ap.add_argument(
        "--model", metavar="NAME",
        default=os.environ.get("LL_MODEL", "qwen3.6:35b-a3b"),
        help="Ollama model to use (default: $LL_MODEL or qwen3.6:35b-a3b)")
    ap.add_argument(
        "--think", choices=["on", "off"], default="off",
        help="turn the model's thinking on or off (default: off)")
    ap.add_argument("--max-tool-steps", type=int, default=30, metavar="N",
                    help="max consecutive tool calls before forcing a pause "
                         "(default: 30)")
    args = ap.parse_args()
    think = args.think == "on"

    print(f"# local tutor  model={args.model}  "
          f"thinking={'on' if think else 'off'}  type /quit to end\n")
    messages = [
        {"role": "system", "content": build_system()},
        {"role": "user", "content": "Start the session."},
    ]

    tool_streak = 0
    ending = False
    gate = FeedbackGate()
    while True:
        try:
            msg = chat(args.model, messages, think=think)
        except Exception as e:  # noqa: BLE001 - surface any transport error
            print(f"[error talking to Ollama: {e}]", file=sys.stderr)
            return 1
        messages.append(msg)
        calls = msg.get("tool_calls") or []

        if calls:
            tool_streak += 1
            if tool_streak > args.max_tool_steps:
                print("[too many tool calls without pausing; stopping]",
                      file=sys.stderr)
                return 1
            for c in calls:
                fn = c.get("function", {})
                ll_args = call_argv(fn.get("arguments", {}) or {})
                print(f"  · ll {' '.join(ll_args)}")
                result = guarded_run_ll(gate, ll_args)
                messages.append({"role": "tool", "tool_name": "ll",
                                 "content": result})
            if gate.needs_nudge():
                messages.append({"role": "user", "content": gate.NUDGE})
                gate.blocks = 0
            continue

        # No tool call -> a message for the student.
        tool_streak = 0
        content = strip_reasoning(msg.get("content") or "")
        if content:
            gate.message_sent()
            print(f"\nTutor: {content}\n")
        else:
            # Some models spend the whole budget reasoning and surface nothing.
            # Say so rather than dropping the student at a bare prompt.
            print("\n[the model returned no message for you — it spent this "
                  "turn thinking. Press enter to let it continue, or type "
                  "something to steer it.]\n")
        if ending:  # we already asked for a sync + goodbye; that was it.
            return 0
        try:
            reply = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            reply = "/quit"
        if reply.lower() in ("/quit", "/exit", "/q"):
            print("\n[ending; asking the tutor to sync progress...]")
            ending = True
            messages.append({"role": "user",
                             "content": "I'm ending the session now. Please run "
                                        "checkpoint sync and say goodbye."})
            continue
        messages.append({"role": "user", "content": reply})


if __name__ == "__main__":
    sys.exit(main())
