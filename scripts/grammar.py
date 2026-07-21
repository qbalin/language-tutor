"""Query the indexed grammar. All output is JSON.

  search   full-text search, returns refs + snippets
      python scripts/grammar.py search "ablative absolute" --lang latin
  show     print a full section by ref (as returned by search/toc), plus the
           sections on either side, which often qualify the rule
      python scripts/grammar.py show 419 --lang latin
      python scripts/grammar.py show 419 --lang latin --no-neighbors
  toc      list all sections (the topic inventory for picking new cards)
      python scripts/grammar.py toc --lang latin --offset 0
"""
import argparse

from common import GRAMMAR_DB, JsonArgumentParser, fts_quote, open_db, out


def cmd_search(conn, args):
    match = fts_quote(args.query)
    rows = conn.execute(
        """SELECT ref, title, source,
                  snippet(grammar_fts, 1, '>>', '<<', ' … ', 40) AS snip
           FROM grammar_fts WHERE grammar_fts MATCH ?
           ORDER BY rank LIMIT ?""", (match, args.limit)).fetchall()
    return {"query": args.query,
            "results": [{"ref": r["ref"], "title": r["title"],
                         "snippet": r["snip"], "source": r["source"]}
                        for r in rows],
            "note": "use `show <ref>` to read a full section"}


def cmd_show(conn, args):
    rows = conn.execute(
        "SELECT * FROM sections WHERE ref = ?", (args.ref,)).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT * FROM sections WHERE ref LIKE ? LIMIT 3",
            (args.ref + "%",)).fetchall()
    if not rows:
        return {"error": f"no section with ref '{args.ref}'"}

    def render(r):
        return {"ref": r["ref"], "title": r["title"], "source": r["source"],
                "content": r["content"][:args.max_chars]}

    result = {"sections": [render(r) for r in rows]}
    if args.neighbors <= 0:
        return result

    # A rule is rarely self-contained: the section that qualifies it usually
    # sits next to it, and a reader who only fetches the ref they were handed
    # never learns that the qualification exists.
    seen = {r["ref"] for r in rows}
    around = []
    for r in rows:
        for n in conn.execute(
                "SELECT * FROM sections WHERE id BETWEEN ? AND ? ORDER BY id",
                (r["id"] - args.neighbors, r["id"] + args.neighbors)):
            if n["ref"] not in seen:
                seen.add(n["ref"])
                around.append(render(n))
    if around:
        result["context_sections"] = around
        result["note"] = ("context_sections are the sections surrounding the "
                          "one you asked for. They often qualify or contradict "
                          "it -- read them before stating the rule.")
    return result


def cmd_toc(conn, args):
    total = conn.execute("SELECT count(*) c FROM sections").fetchone()["c"]
    rows = conn.execute(
        "SELECT ref, title, source FROM sections ORDER BY id LIMIT ? OFFSET ?",
        (args.limit, args.offset)).fetchall()
    result = {"total_sections": total, "offset": args.offset,
              "sections": [{"ref": r["ref"], "title": r["title"]} for r in rows]}
    if args.offset + len(rows) < total:
        result["note"] = f"more sections: rerun with --offset {args.offset + len(rows)}"
    return result


def main():
    ap = JsonArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--lang", required=True)
    p.add_argument("--limit", type=int, default=5)

    p = sub.add_parser("show")
    p.add_argument("ref")
    p.add_argument("--lang", required=True)
    p.add_argument("--max-chars", type=int, default=5000)
    p.add_argument("--neighbors", type=int, default=1, metavar="N",
                   help="also return the N sections on either side, which "
                        "often qualify the rule (0 to disable; default 1)")
    p.add_argument("--no-neighbors", dest="neighbors", action="store_const",
                   const=0, help="return only the requested section")

    p = sub.add_parser("toc")
    p.add_argument("--lang", required=True)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--offset", type=int, default=0)

    args = ap.parse_args()
    conn = open_db(args.lang, GRAMMAR_DB)
    out({"search": cmd_search, "show": cmd_show, "toc": cmd_toc}[args.cmd](conn, args))


if __name__ == "__main__":
    main()
