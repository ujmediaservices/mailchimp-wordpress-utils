"""Two-phase newsletter send: render to a local file, edit it, then ship it.

Mailchimp's classic editor is unpleasant for hand edits, so every newsletter
script supports stopping after the render:

    # Phase 1 - build the real newsletter and write it to disk
    python newsletter-free.py ... --write-html out/free.html

    # ...edit out/free.html in VS Code...

    # Phase 2 - create the campaign from exactly that file
    python newsletter-free.py --send-html out/free.html

Alongside the HTML, phase 1 writes a ``<path>.meta.json`` sidecar holding the
campaign settings (subject, preview, segment, folder) plus any post-send
bookkeeping payload the calling script needs to replay. Phase 2 reads both, so
nothing is refetched from WordPress and the edited file ships verbatim.

This is distinct from ``--dump-html``, which renders the template with sample
data as a layout preview and never carries real content.

Written and read as UTF-8. Reads tolerate a BOM, since editors on Windows
sometimes add one.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

# Identical to check_dashes.BANNED_DASH_RE. Hand edits are the one place a
# banned dash can enter after the render layer already stripped them.
BANNED_DASH_RE = re.compile(
    r"—|–|&mdash;|&ndash;|&#8212;|&#x2014;|&#8211;|&#x2013;"
)

CONTEXT_CHARS = 40


def add_args(parser) -> None:
    """Register --write-html / --send-html / --allow-dashes on a parser."""
    group = parser.add_argument_group("edit-before-send workflow")
    group.add_argument(
        "--write-html", type=Path, default=None, metavar="PATH",
        help=(
            "Render the real newsletter to PATH and stop, without touching "
            "Mailchimp. Also writes PATH.meta.json with the campaign settings. "
            "Edit PATH, then re-run with --send-html PATH."
        ),
    )
    group.add_argument(
        "--send-html", type=Path, default=None, metavar="PATH",
        help=(
            "Create the Mailchimp draft from a previously written (and "
            "possibly hand-edited) HTML file, skipping the render entirely. "
            "Settings come from PATH.meta.json; --subject / --preview / "
            "--segment-id / --folder override it."
        ),
    )
    group.add_argument(
        "--allow-dashes", action="store_true",
        help=(
            "Permit em/en dashes in a --send-html file. They are banned house "
            "style, so the send aborts on one by default."
        ),
    )


def meta_path(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def write(
    path: Path, html: str, *, script: str, campaign: dict,
    bookkeeping: dict | None = None, log,
) -> None:
    """Write the rendered HTML plus its sidecar manifest, then explain next steps.

    `campaign` carries the Mailchimp settings (subject, preview, segment_id,
    folder). `bookkeeping` is an opaque per-script payload replayed after the
    draft is created in phase 2.
    """
    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")

    meta = {
        "script": script,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "html_file": path.name,
        "campaign": campaign,
        "bookkeeping": bookkeeping or {},
    }
    # default=str so a stray Path (or similar) in a bookkeeping payload
    # degrades to its string form instead of failing the whole write.
    meta_path(path).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    log(f"\nHTML written for editing: {path}")
    log(f"  Sidecar settings: {meta_path(path).name}")
    log(f"  Subject: {campaign.get('subject', '')}")
    log(f"  Preview: {campaign.get('preview', '')}")
    log("\nNo Mailchimp campaign was created. Edit the file, then run:")
    log(f"  python {script}.py --send-html {path}")


def _dash_contexts(text: str) -> list[str]:
    out = []
    for m in BANNED_DASH_RE.finditer(text):
        start = max(0, m.start() - CONTEXT_CHARS)
        end = min(len(text), m.end() + CONTEXT_CHARS)
        out.append("..." + text[start:end].replace("\n", " ") + "...")
    return out


def read(
    path: Path, *, script: str, allow_dashes: bool = False, log,
) -> tuple[str, dict]:
    """Read an edited HTML file and its sidecar; return (html, meta).

    Aborts on a banned dash unless `allow_dashes`, since a hand edit is the
    one path that bypasses the render layer's automatic strip.
    """
    path = Path(path)
    if not path.exists():
        print(f"ERROR: --send-html file not found: {path}", file=sys.stderr)
        sys.exit(1)

    html = path.read_text(encoding="utf-8-sig")
    if not html.strip():
        print(f"ERROR: --send-html file is empty: {path}", file=sys.stderr)
        sys.exit(1)

    meta: dict = {}
    mpath = meta_path(path)
    if mpath.exists():
        meta = json.loads(mpath.read_text(encoding="utf-8-sig"))
        wrote = meta.get("script")
        if wrote and wrote != script:
            print(
                f"ERROR: {mpath.name} was written by {wrote}.py, but you are "
                f"running {script}.py. Re-run the send with {wrote}.py, or "
                "delete the sidecar and pass the campaign settings on the "
                "command line.",
                file=sys.stderr,
            )
            sys.exit(1)
        log(f"Loaded edited HTML: {path} ({len(html)} chars)")
        log(f"  Settings from {mpath.name} (written {meta.get('generated_at', '?')}).")
    else:
        log(f"Loaded edited HTML: {path} ({len(html)} chars)")
        log(
            f"  WARNING: no {mpath.name} sidecar; campaign settings must come "
            "from the command line, and post-send bookkeeping will be skipped."
        )

    hits = _dash_contexts(html)
    if hits:
        if allow_dashes:
            log(f"  WARNING: {len(hits)} banned dash(es) present, sending anyway "
                "(--allow-dashes).")
        else:
            print(
                f"ERROR: {len(hits)} banned em/en dash(es) in {path}. These are "
                "banned house style and the render layer strips them "
                "automatically, so each one came from a hand edit. Fix them in "
                "the file (or pass --allow-dashes to override):",
                file=sys.stderr,
            )
            for snip in hits[:10]:
                print(f"    {snip}", file=sys.stderr)
            if len(hits) > 10:
                print(f"    ... and {len(hits) - 10} more.", file=sys.stderr)
            sys.exit(1)

    return html, meta


def settings(meta: dict, **overrides):
    """Merge sidecar campaign settings with non-None CLI overrides."""
    merged = dict(meta.get("campaign") or {})
    for key, value in overrides.items():
        if value is not None:
            merged[key] = value
    return merged


def create_draft(
    mc, *, list_name: str, subject: str, preview: str,
    segment_id: int | None, folder: str | None, html: str, log,
) -> tuple[str, str]:
    """Create the campaign and set its content. Returns (campaign_id, web_id).

    Takes any of the newsletter scripts' MailchimpAPI objects; all three expose
    the same find_list / find_folder / create_campaign / set_campaign_content
    signatures.
    """
    audience = mc.find_list(list_name)
    if not audience:
        print(f"ERROR: list '{list_name}' not found.", file=sys.stderr)
        sys.exit(1)

    folder_id = None
    if folder and folder.strip():
        folder_id = mc.find_folder(folder)
        if folder_id:
            log(f"  Filing under folder: {folder} ({folder_id})")
        else:
            log(f"  WARNING: no campaign folder named {folder!r}; "
                "leaving the draft unfiled.")

    campaign = mc.create_campaign(
        list_id=audience["id"],
        title=subject,
        subject=subject,
        preview_text=preview,
        segment_id=segment_id,
        folder_id=folder_id,
    )
    campaign_id = campaign["id"]
    web_id = str(campaign.get("web_id", ""))
    log(f"  Campaign ID: {campaign_id}")

    mc.set_campaign_content(campaign_id, html)
    log("  Content set.")
    return campaign_id, web_id
