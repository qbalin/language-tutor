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
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
LL = os.path.join(ROOT, "ll")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

# The harness-specific preamble. The actual procedure and rules are loaded
# VERBATIM from CLAUDE.md and the skill files at startup (see build_system),
# so this stays in sync with what Claude Code itself would read.
HARNESS_PREAMBLE = """\
You are running as a standalone local-model tutor, not inside Claude Code.
Adapt to these harness facts, which override anything below that assumes a
richer agent:

- Your ONLY tool is `ll`: it runs this project's `./ll <args>` and returns
  JSON. You have no shell, no file access, no web, and no other tools.
- You CANNOT invoke skills. Everything a skill would provide is already
  pasted below under its heading. When an instruction says "use the
  setup-language skill", follow the inlined SETUP-LANGUAGE SKILL text instead.
- Every `./ll ...` command quoted in an `instruction` or `note` field is an
  order: run it through the `ll` tool. Pass args as a JSON array of strings,
  e.g. {"args": ["dict","lookup","amaverunt","--lang","latin"]}.
- When you need the student to act (pick a language, answer exercises, confirm
  a topic), STOP calling tools and write a plain message; the harness relays
  their reply as the next user turn.
- Begin by running ll(["session","languages"]).

The project instructions (CLAUDE.md), the tutor procedure (tutor skill), and
the setup procedure (setup-language skill) follow, verbatim.
"""

# Files pasted verbatim into the system prompt, in order.
CONTEXT_FILES = [
    ("PROJECT INSTRUCTIONS (CLAUDE.md)", "CLAUDE.md"),
    ("TUTOR SKILL", ".claude/skills/tutor/SKILL.md"),
    ("SETUP-LANGUAGE SKILL", ".claude/skills/setup-language/SKILL.md"),
]


def build_system():
    """Assemble the system prompt from the real project files at runtime."""
    parts = [HARNESS_PREAMBLE]
    for label, rel in CONTEXT_FILES:
        path = os.path.join(ROOT, rel)
        try:
            with open(path, encoding="utf-8") as f:
                body = f.read().strip()
        except OSError as e:
            body = f"[could not read {rel}: {e}]"
        parts.append(f"\n===== {label} ({rel}) =====\n{body}")
    return "\n".join(parts)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "ll",
        "description": ("Run a ./ll tutor command and return its JSON stdout. "
                        "args is the argument vector, e.g. "
                        '["session","next","--lang","latin"].'),
        "parameters": {
            "type": "object",
            "properties": {
                "args": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["args"],
        },
    },
}]


def chat(model, messages, think=False):
    """One /api/chat turn, streaming off. `think` toggles reasoning."""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "think": think,
        "stream": False,
        "options": {"temperature": 0.3, "num_ctx": 16384},
    }).encode()
    req = urllib.request.Request(
        OLLAMA + "/api/chat", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())["message"]


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
                a = fn.get("arguments", {}) or {}
                ll_args = a.get("args", a if isinstance(a, list) else [])
                print(f"  · ll {' '.join(ll_args)}")
                result = run_ll(ll_args)
                messages.append({"role": "tool", "tool_name": "ll",
                                 "content": result})
            continue

        # No tool call -> a message for the student.
        tool_streak = 0
        content = (msg.get("content") or "").strip()
        if content:
            print(f"\nTutor: {content}\n")
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
