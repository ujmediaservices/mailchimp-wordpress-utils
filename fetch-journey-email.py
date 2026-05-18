#!/usr/bin/env python3
"""Fetch the rendered content of a Customer Journey email as markdown.

Customer Journey emails are NOT exposed via /campaigns/{id}/content — that
endpoint returns 404 for journey-internal campaigns. Workaround: each journey
step's `action_details.email.long_archive_url` is a public "view in browser"
URL that renders the same HTML the subscriber sees. We fetch that URL and
convert it to markdown for review.

Usage:
  python fetch-journey-email.py --list
  python fetch-journey-email.py --journey "Plan Your Japan Trip Flow" --list
  python fetch-journey-email.py --journey 13437 --step 3       # 1-indexed
  python fetch-journey-email.py --journey 13437 --all > out.md
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.request

import html2text
import requests


def dc_from_key(key: str) -> str:
    if "-" not in key:
        sys.exit("MAILCHIMP_API_KEY missing '-<dc>' suffix")
    return key.rsplit("-", 1)[1]


API_KEY = os.environ.get("MAILCHIMP_API_KEY") or sys.exit("MAILCHIMP_API_KEY not set")
DC = dc_from_key(API_KEY)
BASE = f"https://{DC}.api.mailchimp.com/3.0"
AUTH = ("anystring", API_KEY)


def list_journeys() -> list[dict]:
    r = requests.get(f"{BASE}/customer-journeys/journeys", auth=AUTH, params={"count": 100}, timeout=30)
    r.raise_for_status()
    return r.json().get("journeys", [])


def resolve_journey(arg: str | None) -> dict:
    """Accept a journey ID, exact name, or substring match. If None, list all."""
    journeys = list_journeys()
    if not arg:
        return None  # caller will list
    # Try ID first
    if arg.isdigit():
        for j in journeys:
            if j["id"] == int(arg):
                return j
        sys.exit(f"No journey with id {arg}")
    # Then exact name, then substring
    arg_lower = arg.lower()
    matches = [j for j in journeys if arg_lower in j["journey_name"].lower()]
    if not matches:
        sys.exit(f"No journey matching {arg!r}")
    if len(matches) > 1:
        names = [f"  {m['id']}  {m['journey_name']!r}" for m in matches]
        sys.exit(f"Ambiguous journey name; matches:\n" + "\n".join(names))
    return matches[0]


def get_email_steps(journey_id: int) -> list[dict]:
    """Return the journey's email-sending steps in order, with embedded email
    detail (subject line, archive URL, etc.)."""
    r = requests.get(f"{BASE}/customer-journeys/journeys/{journey_id}/steps", auth=AUTH, timeout=30)
    r.raise_for_status()
    steps = r.json()["steps"]
    out = []
    for s in steps:
        if s.get("step_type") != "action-send_email":
            continue
        # Re-fetch the step to get expanded action_details (the list endpoint
        # returns abbreviated step bodies)
        detail = requests.get(
            f"{BASE}/customer-journeys/journeys/{journey_id}/steps/{s['id']}",
            auth=AUTH,
            timeout=30,
        ).json()
        email = detail.get("action_details", {}).get("email", {})
        out.append({
            "step_id": s["id"],
            "campaign_id": detail.get("action_settings", {}).get("campaign_id"),
            "subject": email.get("settings", {}).get("subject_line", "(no subject)"),
            "archive_url": email.get("long_archive_url") or email.get("archive_url"),
            "status": email.get("status"),
            "create_time": email.get("create_time"),
        })
    return out


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (uj-mailchimp-review)"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="replace")


def html_to_markdown(html: str) -> str:
    h = html2text.HTML2Text()
    h.body_width = 0  # no line wrapping
    h.ignore_images = False
    h.images_to_alt = False
    h.protect_links = True
    h.unicode_snob = True
    h.skip_internal_links = True
    return h.handle(html)


# Zero-width / soft-hyphen / Hangul-filler runs Mailchimp uses for preview text
# at the top of the email body — strips entire lines that are nothing but these.
_INVISIBLE_RUN = re.compile(r"^[\s­​-‍⁠ㅤᅟᅠ͏‌]+$")
# Mailchimp's template uses giant nested tables; html2text turns them into rows
# of bare `|` and `---`. Strip lines that are only those chars/spaces.
_TABLE_NOISE = re.compile(r"^[\s|\-]+$")


def clean_markdown(md: str, trim_footer: bool = True) -> str:
    """Strip Mailchimp template noise: preview-text invisible-char runs, table
    delimiter junk, leading bare-pipe table-cell markers, and the boilerplate
    footer (copyright / unsubscribe)."""
    lines = []
    for ln in md.splitlines():
        if _INVISIBLE_RUN.match(ln) or _TABLE_NOISE.match(ln):
            continue
        # Strip leading runs of "| " (table cell markers) — Mailchimp wraps each
        # paragraph in a table cell, so html2text emits "| | content" frequently.
        ln = re.sub(r"^(\s*\|\s*)+", "", ln)
        lines.append(ln)
    out = "\n".join(lines)
    if trim_footer:
        for marker in ("*Copyright (C)", "You are receiving this email", "_Copyright (C)"):
            idx = out.find(marker)
            if idx != -1:
                out = out[:idx].rstrip()
                break
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip() + "\n"


def format_email(idx: int, step: dict) -> str:
    out = [
        f"# Day {idx}: {step['subject']}",
        f"_step_id={step['step_id']} campaign_id={step['campaign_id']} status={step['status']}_",
        f"_archive_url: {step['archive_url']}_",
        "",
    ]
    if not step["archive_url"]:
        out.append("**(no archive URL — email may not yet be rendered)**")
        return "\n".join(out)
    html = fetch_html(step["archive_url"])
    md = clean_markdown(html_to_markdown(html))
    out.append(md)
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--journey", help="Journey ID, exact name, or substring")
    ap.add_argument("--list", action="store_true", help="List journeys (or steps if --journey is given)")
    ap.add_argument("--step", type=int, help="1-indexed step number within the journey")
    ap.add_argument("--all", action="store_true", help="Print all steps")
    args = ap.parse_args()

    if args.list and not args.journey:
        for j in list_journeys():
            print(f"  id={j['id']:<6} status={j.get('status','?'):<10} {j['journey_name']!r}")
        return 0

    if not args.journey:
        ap.error("--journey is required unless using bare --list")

    journey = resolve_journey(args.journey)
    print(f"# Journey: {journey['journey_name']} (id={journey['id']}, status={journey['status']})\n", file=sys.stderr)

    steps = get_email_steps(journey["id"])
    if args.list:
        for i, s in enumerate(steps, 1):
            print(f"  Day {i}: {s['subject']}  (step={s['step_id']}, campaign={s['campaign_id']}, status={s['status']})")
        return 0

    if args.all:
        for i, s in enumerate(steps, 1):
            print(format_email(i, s))
            print("\n\n---\n\n")
        return 0

    if args.step is None:
        ap.error("Specify --step N, --all, or --list")
    if not (1 <= args.step <= len(steps)):
        sys.exit(f"--step out of range (journey has {len(steps)} email steps)")
    print(format_email(args.step, steps[args.step - 1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
