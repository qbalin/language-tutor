"""Checkpoint the student's progress: portable JSON snapshots of cards.db.

Snapshots live in progress/<lang>/<UTC timestamp>.json — a directory meant to
become the student's own git repo with a PRIVATE remote, so progress survives
machine loss without ever entering the (public) tutor repo.

  save     snapshot cards.db (no-op if nothing changed since the last one)
  list     checkpoints for a language, newest first
  restore  rebuild cards.db from a checkpoint ("latest" or a name from list)
  sync     save every language, then commit and push progress/ if it is a
           git repo (prints the one-time setup instructions if it is not)

Examples:
  python scripts/checkpoint.py save --lang latin
  python scripts/checkpoint.py restore latest --lang latin
  python scripts/checkpoint.py sync
"""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone

from common import (CARDS_DB, LANGUAGES, PROGRESS, JsonArgumentParser,
                    db_path, lang_dir, out, die)

TABLES = ("cards", "reviews", "exercises", "mistakes", "inbox")
FORMAT = 1


def progress_dir(lang):
    return PROGRESS / lang_dir(lang).name


def checkpoints(lang):
    d = progress_dir(lang)
    return sorted(d.glob("*.json")) if d.exists() else []


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def content_hash(tables):
    canon = json.dumps(tables, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def export_tables(lang):
    conn = sqlite3.connect(db_path(lang, CARDS_DB))
    conn.row_factory = sqlite3.Row
    have = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    tables = {t: [dict(r) for r in
                  conn.execute(f"SELECT * FROM {t} ORDER BY id")]
              if t in have else [] for t in TABLES}
    conn.close()
    return tables


def save_lang(lang):
    """Snapshot cards.db (which must exist) into progress/<lang>/."""
    tables = export_tables(lang)
    digest = content_hash(tables)
    existing = checkpoints(lang)
    if existing and load(existing[-1]).get("content_hash") == digest:
        return {"lang": lang, "unchanged": True, "latest": existing[-1].name}
    d = progress_dir(lang)
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    path, n = d / f"{stamp}.json", 2
    while path.exists():  # same-second collision; "_" sorts after ".json"
        path = d / f"{stamp}_{n}.json"
        n += 1
    doc = {"format": FORMAT, "lang": lang,
           "saved": datetime.now(timezone.utc).isoformat(),
           "content_hash": digest, "tables": tables}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
    return {"lang": lang, "saved": path.name,
            "cards": len(tables["cards"]), "reviews": len(tables["reviews"])}


def auto_save(lang):
    """Date-throttled snapshot for session start: at most one per UTC day,
    so the first `session next` of the day records a pre-session rollback
    point. Returns the new checkpoint name, or None."""
    if not db_path(lang, CARDS_DB).exists():
        return None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if any(p.name.startswith(today) for p in checkpoints(lang)):
        return None
    return save_lang(lang).get("saved")


def require_db(lang):
    path = db_path(lang, CARDS_DB)
    if not path.exists():
        die(f"no {CARDS_DB} for language '{lang}' (expected {path}); "
            "nothing to checkpoint")


# ---------------------------------------------------------------- commands

def cmd_save(args):
    require_db(args.lang)
    out({"ok": True, **save_lang(args.lang)})


def cmd_list(args):
    entries = []
    for p in reversed(checkpoints(args.lang)):
        doc = load(p)
        t = doc.get("tables", {})
        entries.append({"name": p.name, "saved": doc.get("saved"),
                        "cards": len(t.get("cards", [])),
                        "reviews": len(t.get("reviews", []))})
    result = {"lang": args.lang, "count": len(entries), "checkpoints": entries}
    if not entries:
        result["note"] = ("no checkpoints yet; run: ./ll checkpoint save "
                          f"--lang {args.lang}")
    out(result)


def cmd_restore(args):
    lang = args.lang
    existing = checkpoints(lang)
    if not existing:
        die(f"no checkpoints for '{lang}' in {progress_dir(lang)}")
    if args.name == "latest":
        path = existing[-1]
    else:
        name = args.name if args.name.endswith(".json") else args.name + ".json"
        path = progress_dir(lang) / name
        if path not in existing:
            die(f"no checkpoint '{args.name}' for '{lang}'",
                available=[p.name for p in existing])
    doc = load(path)
    if doc.get("format") != FORMAT or doc.get("lang") != lang:
        die(f"{path.name} is not a format-{FORMAT} checkpoint for '{lang}'")
    result = {"ok": True, "restored": path.name}
    real = db_path(lang, CARDS_DB)
    if real.exists():
        safety = save_lang(lang)
        result["current_state_saved_as"] = (safety.get("saved")
                                            or safety.get("latest"))
    from cards import SCHEMA
    tmp = real.parent / (real.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    real.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(SCHEMA)
    for t in TABLES:
        for row in doc["tables"].get(t, []):
            cols = list(row)
            conn.execute(
                f"INSERT INTO {t} ({', '.join(cols)}) "
                f"VALUES ({', '.join(['?'] * len(cols))})",
                [row[c] for c in cols])
    conn.commit()
    conn.close()
    os.replace(tmp, real)
    tables = doc["tables"]
    result.update({"cards": len(tables.get("cards", [])),
                   "reviews": len(tables.get("reviews", []))})
    out(result)


SETUP_NOTE = (
    "progress/ is not a git repo yet, so backups are local-only. One-time "
    "setup for durable, machine-portable backups (works for any account): "
    "cd progress && git init && gh repo create language-progress --private "
    "--source . --push — or without gh: git init, create a PRIVATE repo on "
    "your host, then git remote add origin <url> && git push -u origin HEAD. "
    "Keep it private and separate from the tutor repo: checkpoints contain "
    "the student's personal review history.")


def run_git(*args):
    p = subprocess.run(["git", "-C", str(PROGRESS)] + list(args),
                       capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def cmd_sync(args):
    saved = []
    if LANGUAGES.exists():
        for p in sorted(LANGUAGES.iterdir()):
            if p.is_dir() and (p / CARDS_DB).exists():
                saved.append(save_lang(p.name))
    result = {"ok": True, "saved": saved}
    if not (PROGRESS / ".git").exists():
        out({**result, "synced": False, "note": SETUP_NOTE})
        return
    run_git("add", "-A")
    _, staged, _ = run_git("status", "--porcelain")
    if staged:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        rc, _, err = run_git("commit", "-m", f"checkpoint {stamp}")
        result["committed"] = rc == 0
        if rc != 0:
            result["commit_error"] = err[-300:]
    else:
        result["committed"] = False
        result["commit_note"] = "nothing new to commit"
    _, remotes, _ = run_git("remote")
    remote = remotes.split()[0] if remotes else None
    if not remote:
        result["pushed"] = False
        result["note"] = ("no remote configured for progress/; add your own "
                          "PRIVATE one: git -C progress remote add origin "
                          "<url>, then sync again")
    else:
        rc, _, err = run_git("push", "-u", remote, "HEAD")
        result["pushed"] = rc == 0
        if rc != 0:
            result["push_error"] = err[-300:]
    out(result)


def main():
    ap = JsonArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("save", "list"):
        sub.add_parser(name).add_argument("--lang", required=True)
    p = sub.add_parser("restore")
    p.add_argument("name", help='checkpoint name from `list`, or "latest"')
    p.add_argument("--lang", required=True)
    sub.add_parser("sync")
    args = ap.parse_args()
    {"save": cmd_save, "list": cmd_list, "restore": cmd_restore,
     "sync": cmd_sync}[args.cmd](args)


if __name__ == "__main__":
    main()
