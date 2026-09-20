#!/usr/bin/env python3
"""Banned em/en-dash scanner for newsletter output.

Shared helper for the send-free-newsletter and send-insider-newsletter
skills. Both previously inlined the identical ``re.findall`` scan in their
SKILL prose (send-free "NEVER use em dashes" section; send-insider "Banned-dash
verification" step). This is that scan, once.

Em and en dashes (and their HTML entity forms) are banned anywhere in the
rendered newsletter: they read as an LLM tell and clash with UJ house style.

The dash alternation matches ``newsletter_free.py``'s ``_BANNED_DASH_RE``, so
the CLI check and the script's build-time ``strip_banned_dashes`` agree on what
counts as a violation. Only the alternation is shared: the script's regex also
absorbs the spaces and tabs hugging a dash (so a spaced dash collapses to one
comma rather than leaving " , " behind), while this one deliberately does not,
since a detector should report the dash's own position.

Usage:
  # Pipe rendered HTML in (the primary use, mirrors the old inline check):
  python newsletter-free.py --dump-html [FLAGS] | python check_dashes.py

  # Check one or more files (staged inserts, extras JSON, templates):
  python check_dashes.py --file data/last-extras.json --file inserts/jp-social-2026-08-01.md
  python check_dashes.py data/last-extras.json          # positional works too

  # Show ~60 chars of context around each hit (for locating the source):
  python check_dashes.py --file templates/newsletter-free.html.j2 --context

Exit code: 1 if any banned dash is found, 0 if clean, 2 on a usage / IO error.
Always prints ``banned-dash count: N`` (the total) so callers can grep it.
"""
from __future__ import annotations

import argparse
import re
import sys

# The dash alternation below is shared with newsletter_free.py's
# `_BANNED_DASH_RE`; keep the two character lists in sync. That regex wraps this
# alternation in `[ 	]*...[ 	]*` so replacement swallows the flanking spaces;
# detection deliberately doesn't, so reported offsets point at the dash itself.
BANNED_DASH_RE = re.compile(
    r"—|–|&mdash;|&ndash;|&#8212;|&#x2014;|&#8211;|&#x2013;"
)

CONTEXT_CHARS = 30  # each side of a hit when --context is set


def _contexts(text: str) -> list[str]:
    """Return a ~60-char window around each banned-dash hit in `text`."""
    out: list[str] = []
    for m in BANNED_DASH_RE.finditer(text):
        start = max(0, m.start() - CONTEXT_CHARS)
        end = min(len(text), m.end() + CONTEXT_CHARS)
        snippet = text[start:end].replace("\n", " ").replace("\r", " ")
        out.append(f"...{snippet}...")
    return out


def scan_text(text: str) -> int:
    """Return the number of banned-dash hits in `text`."""
    return len(BANNED_DASH_RE.findall(text))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "files", nargs="*", metavar="FILE",
        help="File(s) to scan. If none given, read from stdin.",
    )
    ap.add_argument(
        "--file", action="append", default=[], dest="file_opts",
        metavar="F", help="File to scan (repeatable; combines with positional).",
    )
    ap.add_argument(
        "--context", action="store_true",
        help="Print a ~60-char window around each hit (helps locate the source).",
    )
    args = ap.parse_args(argv)

    paths = list(args.files) + list(args.file_opts)
    total = 0

    if paths:
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as e:
                print(f"ERROR: cannot read {path}: {e}", file=sys.stderr)
                return 2
            n = scan_text(text)
            total += n
            if len(paths) > 1:
                print(f"  {path}: {n}")
            if n and args.context:
                for snip in _contexts(text):
                    print(f"    {path}: {snip}")
    else:
        text = sys.stdin.read()
        total = scan_text(text)
        if total and args.context:
            for snip in _contexts(text):
                print(f"  {snip}")

    print(f"banned-dash count: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
