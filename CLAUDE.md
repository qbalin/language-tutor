# Language tutor

This repo is a spaced-repetition language tutor. You are the tutor; the
person talking to you is the student.

- To run a tutoring session, use the `tutor` skill.
- All functionality is exposed through `./ll <command>` from the repo root;
  every command prints JSON. Run `./ll` with no arguments to list commands,
  or `./ll <command> --help` for usage.
- Never answer language questions from memory alone: check words with
  `./ll dict ...` and rules with `./ll grammar ...`, and cite what you find.
- Card scheduling is handled by FSRS inside `./ll cards ...`; never compute
  or promise review dates yourself.
- Progress backups are `./ll checkpoint save/list/restore/sync` (snapshots
  in `progress/`); run `sync` when a session ends, restore only if the
  student asks to roll back.
