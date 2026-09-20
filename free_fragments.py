"""Harvest as-sent HTML fragments out of a rendered free newsletter.

Why this exists
---------------
``newsletter-free.py`` supports an edit-before-send flow: ``--write-html``
renders the email to a file, Jay edits that file, and ``--send-html`` ships it
byte for byte. But the state file that ``newsletter-insider.py`` wraps later in
the week was written from the sidecar manifest captured at *render* time, so
every edit made in between was invisible to the Insider run. The Insider then
re-rendered each block from the pre-edit structured data and quietly shipped
different copy than the free newsletter did.

The observed failure (2026-09-15): the free lead carried three paragraphs of
intro text, and the Insider's carry-over of the same post showed the one-line
WordPress excerpt instead.

So ``--send-html`` now re-reads the file it is about to ship and harvests the
rendered fragments back out. Those go into the state file, and the Insider
prefers them over re-rendering. Fragments are keyed by post URL rather than by
position, so deleting or reordering a block during editing stays safe.

Everything here is pure parsing: no network, no Mailchimp, no WordPress.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

# The free template writes `<!-- ── Post N: Title ── -->` before each post block
# and `<!-- Excerpt -->` immediately before the excerpt paragraph. The rules are
# box-drawing characters (U+2500), not dashes, so strip_banned_dashes leaves
# them alone. Match loosely anyway: the marker text is chrome, not a contract.
_POST_MARKER_RE = re.compile(r"<!--[^>]*?\bPost\s+\d+\s*:.*?-->", re.S)
_EXCERPT_RE = re.compile(r"<!--\s*Excerpt\s*-->\s*<p[^>]*>(.*?)</p>", re.S)

_SECTION_ANCHORS = (
    ("jp_social_html", "jp-social"),
    ("extras_html", "extras"),
)

_EDITORS_NOTE_HEADING = "From the publisher"


def _section_inner_html(soup: BeautifulSoup, anchor_name: str) -> str | None:
    """Inner HTML of the <td> holding the named anchor, minus the anchor itself.

    The Insider template owns its own divider and cell chrome, so only the
    content inside the cell carries over.
    """
    anchor = soup.find("a", attrs={"name": anchor_name})
    if anchor is None:
        return None
    cell = anchor.find_parent("td")
    if cell is None:
        return None
    parts = [str(node) for node in anchor.next_siblings]
    fragment = "".join(parts).strip()
    return fragment or None


def _editors_note_html(soup: BeautifulSoup) -> str | None:
    """Everything after the "From the publisher" heading, inside its cell."""
    for heading in soup.find_all("h2"):
        if heading.get_text(strip=True) != _EDITORS_NOTE_HEADING:
            continue
        if heading.find_parent("td") is None:
            continue
        fragment = "".join(str(node) for node in heading.next_siblings).strip()
        return fragment or None
    return None


def _post_excerpts(html: str, posts: list[dict]) -> dict[str, str]:
    """Map post URL -> as-sent excerpt HTML.

    Blocks are matched to posts by the URL they link to, not by position, so an
    edit that removes or reorders a block can't shift every later excerpt onto
    the wrong post.
    """
    urls = [p.get("url", "") for p in posts if p.get("url")]
    found: dict[str, str] = {}

    # The text before the first marker is header chrome; skip it.
    chunks = _POST_MARKER_RE.split(html)[1:]
    for chunk in chunks:
        match = _EXCERPT_RE.search(chunk)
        if not match:
            continue
        excerpt = match.group(1).strip()
        if not excerpt:
            continue
        # A block links to its own post several times (image, title, button).
        owners = [u for u in urls if u and u in chunk]
        if len(owners) != 1:
            # Zero owners means an unrecognized block; more than one means the
            # URLs aren't distinguishing (e.g. a post linked from its
            # neighbor's copy). Either way, guessing would be worse than
            # falling back to the structured excerpt.
            continue
        found.setdefault(owners[0], excerpt)
    return found


def harvest(html: str, posts: list[dict]) -> dict:
    """Pull the reusable fragments out of a rendered free newsletter.

    ``posts`` is the bookkeeping post list, used only for its URLs.

    Returns a dict with any of ``post_excerpts`` (url -> HTML),
    ``editors_note_html``, ``jp_social_html`` and ``extras_html`` that were
    found. Missing keys simply mean the caller should keep what it already has.
    """
    soup = BeautifulSoup(html, "html.parser")

    result: dict = {}

    excerpts = _post_excerpts(html, posts)
    if excerpts:
        result["post_excerpts"] = excerpts

    note = _editors_note_html(soup)
    if note:
        result["editors_note_html"] = note

    for key, anchor in _SECTION_ANCHORS:
        fragment = _section_inner_html(soup, anchor)
        if fragment:
            result[key] = fragment

    return result


def apply_to_bookkeeping(html: str, bookkeeping: dict, log=None) -> dict:
    """Fold harvested fragments into a bookkeeping manifest, in place.

    Called on the ``--send-html`` path so the state file records what actually
    shipped rather than what was rendered before editing.
    """
    posts = bookkeeping.get("posts") or []
    harvested = harvest(html, posts)
    if not harvested:
        if log:
            log("  No reusable fragments found; state keeps the rendered copy.")
        return bookkeeping

    changed: list[str] = []

    excerpts = harvested.get("post_excerpts") or {}
    matched = 0
    for post in posts:
        as_sent = excerpts.get(post.get("url", ""))
        if as_sent is None:
            continue
        matched += 1
        if as_sent != (post.get("excerpt") or ""):
            post["excerpt"] = as_sent
            changed.append(f"post {post.get('post_id')}")
    missing = len(posts) - matched
    if missing and log:
        log(
            f"  WARNING: {missing} of {len(posts)} post blocks had no "
            "harvestable excerpt; those keep their rendered copy."
        )

    for key in ("editors_note_html", "jp_social_html", "extras_html"):
        value = harvested.get(key)
        if value and value != (bookkeeping.get(key) or ""):
            bookkeeping[key] = value
            changed.append(key)

    if log:
        if changed:
            log(f"  Harvested as-sent fragments: {', '.join(changed)}.")
        else:
            log("  As-sent fragments match the rendered copy; nothing to update.")
    return bookkeeping
