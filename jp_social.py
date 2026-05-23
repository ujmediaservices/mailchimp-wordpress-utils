"""Stage and render the weekly "What Japan's talking about this week" section.

Two phases:

1. ``stage``: week-in-review pick. Pool every /find-social shortlist from the
   last 7 days, dedupe, rank, and stage the top N as primary picks plus a few
   backups. Captures screenshots and writes a draft markdown for Jay to edit
   before send. (Pass --shortlist to pin to a single day instead.)
2. ``load_section``: called by newsletter-free.py; reads the (Jay-edited)
   markdown, optionally uploads screenshots to Mailchimp's CDN, returns
   template-ready dicts.

Selection filters, in order:
  - News-outlet exclusion (jp_social_news_handles.txt)
  - "Already became a UJ story" exclusion (data/jp-social-excludes.txt)
  - Direct-link exclusion (tweet URL appears in any recent UJ post body)
  - Previous-weeks dedup (data/jp-social-used.json, last 4 weeks)

Surviving items ranked by newsletter_fit (HIGH > MEDIUM) then engagement
(likes + 2*retweets). Top N picked as primaries; the next M as backups.
Backups are staged (screenshot + block) but live under a "Backup" header that
``parse_markdown`` skips, so they don't render until promoted to a Tweet block.

Usage:
    python jp_social.py stage [--days 7] [--count 3] [--backups 3]
    python jp_social.py stage --shortlist YYYY-MM-DD   # single-day mode
    python jp_social.py validate inserts/jp-social-2026-05-21.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import requests

import extras as extras_mod
import inserts as inserts_mod
import screenshot_x
import wp_post


PROJECT_ROOT = Path(__file__).parent
SHORTLISTS_DIR = Path(r"D:/uj/sentiment-analysis/shortlists")
NEWS_HANDLES_FILE = PROJECT_ROOT / "jp_social_news_handles.txt"
EXCLUDES_FILE = PROJECT_ROOT / "data" / "jp-social-excludes.txt"
USED_LEDGER = PROJECT_ROOT / "data" / "jp-social-used.json"
INSERTS_DIR = PROJECT_ROOT / "inserts"

# How many days back the WP-body link grep covers + how many weeks of past
# JP-social picks to dedup against.
WP_LOOKBACK_DAYS = 14
USED_LOOKBACK_DAYS = 28

FIT_RANK = {"HIGH": 2, "MEDIUM": 1, "LOW": 0}


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def load_news_handles() -> set[str]:
    """Lowercase set of @ handles to exclude (news outlets etc)."""
    if not NEWS_HANDLES_FILE.exists():
        return set()
    handles: set[str] = set()
    for line in NEWS_HANDLES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        handles.add(line.lstrip("@").lower())
    return handles


def load_excludes_file() -> set[str]:
    if not EXCLUDES_FILE.exists():
        return set()
    out: set[str] = set()
    for line in EXCLUDES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def load_used_ledger() -> dict:
    if not USED_LEDGER.exists():
        return {}
    try:
        return json.loads(USED_LEDGER.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_used_ledger(data: dict) -> None:
    USED_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    USED_LEDGER.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
    )


def recent_used_urls(ledger: dict, days: int = USED_LOOKBACK_DAYS) -> set[str]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    out: set[str] = set()
    for ts_str, urls in ledger.items():
        try:
            ts = dt.datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        if ts >= cutoff:
            out.update(urls)
    return out


def find_latest_shortlist(date_str: str | None = None) -> Path:
    """Return the JSON shortlist for `date_str` or the most-recent one <=7d old."""
    if date_str:
        path = SHORTLISTS_DIR / f"{date_str}.json"
        if not path.exists():
            raise FileNotFoundError(f"No shortlist at {path}")
        return path
    candidates = sorted(SHORTLISTS_DIR.glob("*.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No shortlists in {SHORTLISTS_DIR}")
    latest = candidates[0]
    name_match = re.match(r"(\d{4}-\d{2}-\d{2})\.json$", latest.name)
    if name_match:
        latest_date = dt.date.fromisoformat(name_match.group(1))
        if (dt.date.today() - latest_date).days > 7:
            raise RuntimeError(
                f"Most recent shortlist {latest.name} is more than 7 days "
                "old. Re-run /find-social before staging JP tweets."
            )
    return latest


def fetch_recent_uj_post_urls(days: int = WP_LOOKBACK_DAYS) -> set[str]:
    """Set of all URLs linked from any UJ post published in the last `days`.

    Cheap: one paginated REST call. Returns an empty set on any HTTP error so
    a network blip doesn't block staging — the other filters still apply.
    """
    try:
        wp_site, wp_auth = wp_post.get_wp_config()
    except SystemExit:
        return set()
    cutoff = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    ).isoformat()
    bodies: list[str] = []
    page = 1
    while True:
        try:
            resp = requests.get(
                f"{wp_site}/wp-json/wp/v2/posts",
                params={
                    "_fields": "content",
                    "per_page": 50,
                    "page": page,
                    "after": cutoff,
                    "status": "publish",
                },
                auth=wp_auth,
                timeout=30,
            )
        except requests.RequestException as exc:
            print(f"  WARNING: WP fetch failed ({exc}); skipping link grep.",
                  file=sys.stderr)
            return set()
        if resp.status_code == 400:
            break
        if resp.status_code != 200:
            print(
                f"  WARNING: WP fetch returned {resp.status_code}; "
                "skipping link grep.",
                file=sys.stderr,
            )
            return set()
        page_data = resp.json()
        if not page_data:
            break
        for post in page_data:
            bodies.append(post.get("content", {}).get("rendered", ""))
        if len(page_data) < 50:
            break
        page += 1
        if page > 5:  # 250 posts cap; UJ never hits this in 14 days
            break
    return extras_mod.collect_cited_urls(bodies)


def normalize_x_url(url: str) -> str:
    """Map twitter.com/x.com hosts to a single canonical form for dedup."""
    return re.sub(
        r"^https?://(www\.)?(twitter|x)\.com/",
        "https://x.com/",
        url.strip(),
    )


def score_item(item: dict) -> tuple[int, int]:
    fit = FIT_RANK.get((item.get("newsletter_fit") or "").upper(), 0)
    eng = item.get("engagement") or {}
    score = (eng.get("likes") or 0) + 2 * (eng.get("retweets") or 0)
    return (fit, score)


def filter_items(items: list[dict]) -> list[dict]:
    """Apply the news/excludes/linked/used filters; return survivors (unsorted)."""
    news_handles = load_news_handles()
    excludes = load_excludes_file()
    used = recent_used_urls(load_used_ledger())
    wp_cited = fetch_recent_uj_post_urls()

    excludes_norm = {normalize_x_url(u) for u in excludes}
    used_norm = {normalize_x_url(u) for u in used}
    wp_cited_norm = {normalize_x_url(u) for u in wp_cited}

    print(
        f"  Filters loaded: {len(news_handles)} news handles, "
        f"{len(excludes)} manual excludes, {len(used)} recent picks, "
        f"{len(wp_cited)} URLs linked from recent UJ posts.",
        file=sys.stderr,
    )

    survivors: list[dict] = []
    dropped = {"news": 0, "excluded": 0, "linked": 0, "used": 0}
    for item in items:
        handle = (item.get("source") or "").lstrip("@").lower()
        if handle in news_handles:
            dropped["news"] += 1
            continue
        norm = normalize_x_url(item.get("url") or "")
        if norm in excludes_norm:
            dropped["excluded"] += 1
            continue
        if norm in wp_cited_norm:
            dropped["linked"] += 1
            continue
        if norm in used_norm:
            dropped["used"] += 1
            continue
        survivors.append(item)

    print(
        f"  Filtered out: {dropped['news']} news, "
        f"{dropped['excluded']} on excludes list, "
        f"{dropped['linked']} linked from recent UJ posts, "
        f"{dropped['used']} used in past 4 weeks. "
        f"{len(survivors)} survivors.",
        file=sys.stderr,
    )
    return survivors


def select_picks(shortlist_path: Path, count: int) -> list[dict]:
    """Single-shortlist selection (back-compat: used when --shortlist is given)."""
    data = json.loads(shortlist_path.read_text(encoding="utf-8"))
    survivors = filter_items(data.get("items", []))
    survivors.sort(key=score_item, reverse=True)
    return survivors[:count]


def collect_week_shortlists(days: int) -> list[Path]:
    """Shortlist JSONs dated within the last `days` days (inclusive), newest first."""
    cutoff = dt.date.today() - dt.timedelta(days=days - 1)
    dated: list[tuple[dt.date, Path]] = []
    for p in SHORTLISTS_DIR.glob("*.json"):
        m = re.match(r"(\d{4}-\d{2}-\d{2})\.json$", p.name)
        if not m:
            continue
        d = dt.date.fromisoformat(m.group(1))
        if d >= cutoff:
            dated.append((d, p))
    dated.sort(reverse=True)
    return [p for _, p in dated]


def pool_items(paths: list[Path]) -> list[dict]:
    """Concatenate items across shortlists, deduped by URL (keep best-scoring instance).

    Engagement grows over time, so when the same tweet appears in multiple daily
    shortlists we keep the highest-scoring copy. Each survivor is tagged with the
    shortlist it came from (``_shortlist``) for display in the staged markdown.
    """
    by_url: dict[str, dict] = {}
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for item in data.get("items", []):
            norm = normalize_x_url(item.get("url") or "")
            if not norm:
                continue
            tagged = {**item, "_shortlist": path.stem}
            prev = by_url.get(norm)
            if prev is None or score_item(tagged) > score_item(prev):
                by_url[norm] = tagged
    return list(by_url.values())


def slot_of(item: dict) -> str:
    return (item.get("editorial_slot") or item.get("folder") or "?").lower()


def diversify_by_slot(items: list[dict], per_slot: int) -> list[dict]:
    """Reorder a score-sorted list so editorial slots spread across the top.

    Greedy: walk in score order, taking an item only while its slot is still
    under `per_slot`; items skipped for exceeding the cap are appended afterward
    in their original score order. With per_slot=1 the leading items are all
    distinct slots until the slots run out, so the primary picks span UJ's
    editorial mix instead of clustering in one slot (e.g. all "linguistic").
    `per_slot <= 0` disables diversification.
    """
    if per_slot <= 0:
        return items
    counts: dict[str, int] = {}
    taken: list[dict] = []
    overflow: list[dict] = []
    for it in items:
        s = slot_of(it)
        if counts.get(s, 0) < per_slot:
            counts[s] = counts.get(s, 0) + 1
            taken.append(it)
        else:
            overflow.append(it)
    return taken + overflow


def select_week(
    days: int, count: int, backups: int, *, per_slot: int = 1,
) -> tuple[list[dict], list[dict], list[Path]]:
    """Week-in-review selection: pool all recent shortlists, rank, split primary/backup.

    `per_slot` caps how many picks share an editorial slot near the top (default
    1) so the section spans UJ's mix; pass 0 to rank purely by engagement.
    """
    paths = collect_week_shortlists(days)
    if not paths:
        raise FileNotFoundError(
            f"No shortlists dated within the last {days} days under "
            f"{SHORTLISTS_DIR}. Run /find-social first."
        )
    pooled = pool_items(paths)
    print(
        f"  Pooled {len(pooled)} unique tweets across {len(paths)} shortlist(s): "
        f"{', '.join(p.stem for p in paths)}.",
        file=sys.stderr,
    )
    survivors = filter_items(pooled)
    survivors.sort(key=score_item, reverse=True)
    ordered = diversify_by_slot(survivors, per_slot)
    slots = [slot_of(it) for it in ordered[:count + backups]]
    print(
        f"  Slot-diversified (per_slot={per_slot}); top {len(slots)} slots: "
        f"{', '.join(slots)}.",
        file=sys.stderr,
    )
    return ordered[:count], ordered[count:count + backups], paths


# ---------------------------------------------------------------------------
# Staging (write the draft markdown)
# ---------------------------------------------------------------------------

def _slug_for_dir(date_str: str) -> Path:
    return INSERTS_DIR / f"jp-social-{date_str}"


def _md_path_for(date_str: str) -> Path:
    return INSERTS_DIR / f"jp-social-{date_str}.md"


def _clean_dashes(text: str) -> str:
    """Strip em/en dashes (banned in UJ copy). Leaves U+30FC (ー) and 〜 alone."""
    if not text:
        return text
    return re.sub(r"\s*[\u2014\u2013]\s*", ", ", text)


def _capture_and_block(
    kind: str, n: int, pick: dict, today: str, out_dir: Path,
) -> str:
    """Screenshot one pick (best-effort) and return its markdown block.

    `kind` is "Tweet" (rendered) or "Backup" (skipped at render time until the
    user promotes it by renaming the header). Screenshots are still captured for
    backups so promoting one is a one-line edit, no re-staging needed.
    """
    url = pick.get("url", "")
    fname = f"{kind.lower()}-{n}.png"
    screenshot_rel = f"jp-social-{today}/{fname}"
    screenshot_abs = out_dir / fname
    try:
        screenshot_x.capture_tweet(url, screenshot_abs)
    except (RuntimeError, FileNotFoundError) as exc:
        print(
            f"  WARNING: screenshot failed for {kind.lower()} {n}: {exc}. "
            "Leaving placeholder; Jay drops in manually.",
            file=sys.stderr,
        )
    eng = pick.get("engagement") or {}
    meta = (
        f"> meta: {eng.get('likes', 0):,} likes | "
        f"fit={pick.get('newsletter_fit', '?')} | "
        f"slot={pick.get('editorial_slot') or pick.get('folder', '?')}"
    )
    if pick.get("_shortlist"):
        meta += f" | from {pick['_shortlist']}"
    return (
        f"### {kind} {n}\n"
        f"{meta}\n"
        f"url: {url}\n"
        f"author: {pick.get('source', '')}\n"
        f"screenshot: {screenshot_rel}\n"
        f"jp: {_clean_dashes(pick.get('title_jp', ''))}\n"
        f"en: {_clean_dashes(pick.get('title_en', ''))}\n"
        f"context: {_clean_dashes(pick.get('synopsis', ''))}\n"
    )


def stage(
    date_str: str | None = None, count: int = 3, *,
    days: int = 7, backups: int = 3, per_slot: int = 1,
) -> Path:
    """Stage the weekly JP-tweets section.

    Default (no `date_str`): a week-in-review pool across every shortlist in the
    last `days` days, deduped and ranked, yielding `count` primary picks plus
    `backups` alternates, spread across editorial slots (`per_slot` cap).
    Passing `date_str` pins to a single shortlist (the old single-day behavior)
    and stages no backups.
    """
    today = dt.date.today().isoformat()

    if date_str:
        shortlist_path = find_latest_shortlist(date_str)
        print(f"Using single shortlist {shortlist_path.name}", file=sys.stderr)
        picks = select_picks(shortlist_path, count)
        backup_picks: list[dict] = []
        source_label = shortlist_path.stem
    else:
        picks, backup_picks, paths = select_week(
            days, count, backups, per_slot=per_slot,
        )
        source_label = f"week {paths[-1].stem}..{paths[0].stem}"

    if not picks:
        raise RuntimeError(
            "No surviving picks after filters. Either widen --days/--count, "
            "thin out the excludes list, or wait for a new /find-social run."
        )

    out_dir = _slug_for_dir(today)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = _md_path_for(today)

    print(f"Capturing {len(picks)} primary screenshot(s)...", file=sys.stderr)
    primary_blocks = [
        _capture_and_block("Tweet", i, pick, today, out_dir)
        for i, pick in enumerate(picks, start=1)
    ]

    backup_blocks: list[str] = []
    if backup_picks:
        print(
            f"Capturing {len(backup_picks)} backup screenshot(s)...",
            file=sys.stderr,
        )
        backup_blocks = [
            _capture_and_block("Backup", i, pick, today, out_dir)
            for i, pick in enumerate(backup_picks, start=1)
        ]

    parts = [
        "---",
        f"generated_at: {today}",
        f"shortlist: {source_label}",
        "---",
        "",
        "## What Japan's talking about this week",
        "",
        "\n".join(primary_blocks),
    ]
    if backup_blocks:
        parts += [
            "",
            "## Backups (not included unless you promote one)",
            "",
            ("<!-- To use a backup: rename its '### Backup N' header to "
             "'### Tweet 4' (and delete a Tweet block you're dropping). Blocks "
             "under a 'Backup' header are ignored at render time. -->"),
            "",
            "\n".join(backup_blocks),
        ]
    md_path.write_text("\n".join(parts) + "\n", encoding="utf-8")

    print(
        f"\nStaged: {md_path}\n"
        f"  {len(primary_blocks)} primary + {len(backup_blocks)} backup tweet(s).\n"
        f"  Edit translations + context, swap or delete tweets, then run\n"
        f"  newsletter-free.py with --jp-social-auto (or --jp-social {md_path}).\n"
        f"  Screenshots: {out_dir}\\",
        file=sys.stderr,
    )
    return md_path


# ---------------------------------------------------------------------------
# Loading (called from newsletter-free.py at render time)
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("url", "author", "screenshot", "jp", "en", "context")


def unwrap_md_link(value: str) -> str:
    """If `value` is a bare Markdown link `[text](href)`, return the href.

    The user often edits the staged markdown in a previewer that turns plain
    URLs into `[url](url)`; the `url:`/`screenshot:` fields must stay bare.
    """
    m = re.match(r"^\[.*?\]\(([^)]+)\)\s*$", value.strip())
    return m.group(1).strip() if m else value.strip()


def _parse_block(block_text: str, block_label: str) -> dict:
    fields: dict[str, str] = {}
    for line in block_text.splitlines():
        line = line.rstrip()
        # Tolerate Markdown blockquote markers (the user reformats blocks as
        # "> field: value" with trailing hard-break spaces).
        while line.startswith(">"):
            line = line[1:].lstrip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if key in ("url", "screenshot"):
            value = unwrap_md_link(value)
        fields[key] = value
    missing = [f for f in _REQUIRED_FIELDS if not fields.get(f)]
    if missing:
        raise ValueError(
            f"JP-social block '{block_label}' is missing required fields: "
            f"{', '.join(missing)}. Either fill them or delete the block."
        )
    return fields


def parse_markdown(md_path: Path) -> list[dict]:
    text = md_path.read_text(encoding="utf-8")
    # Drop YAML-ish frontmatter and the section heading
    body = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)
    parts = re.split(r"\n(?=### )", body)
    blocks: list[dict] = []
    for part in parts:
        part = part.strip()
        if not part.startswith("###"):
            continue
        header, _, rest = part.partition("\n")
        label = header.lstrip("# ").strip() or "(unlabeled)"
        if label.lower().startswith("backup"):
            continue  # backups don't render until promoted to a Tweet block
        blocks.append({"_label": label, **_parse_block(rest, label)})
    return blocks


def load_section(
    md_path: Path,
    *,
    mc=None,
    log=lambda msg: print(msg, file=sys.stderr),
) -> list[dict]:
    """Read the staged markdown and return template-ready dicts.

    If `mc` (a MailchimpAPI instance) is given, screenshots are uploaded to
    Mailchimp's file manager and the returned dicts use the CDN URL. Without
    `mc`, the local filesystem path is returned (only useful for --dump-html
    previews).
    """
    if not md_path.exists():
        raise FileNotFoundError(f"JP-social markdown not found: {md_path}")

    raw_blocks = parse_markdown(md_path)
    if not raw_blocks:
        raise ValueError(
            f"No tweet blocks found in {md_path}. Each tweet must start with "
            "a '### Tweet N' header."
        )

    out: list[dict] = []
    for block in raw_blocks:
        screenshot_rel = block["screenshot"]
        screenshot_abs = md_path.parent / screenshot_rel
        if not screenshot_abs.is_file():
            raise FileNotFoundError(
                f"Screenshot missing for {block['_label']}: {screenshot_abs}"
            )
        image_url: str
        if mc is not None:
            try:
                mc_url = mc.upload_image(
                    f"jp-social-{md_path.stem}-{screenshot_abs.name}",
                    screenshot_abs.read_bytes(),
                )
                image_url = mc_url
                log(f"  Uploaded JP-social image: {screenshot_abs.name}")
            except requests.exceptions.HTTPError as exc:
                log(f"  WARNING: MC upload failed for {screenshot_abs.name}: {exc}")
                image_url = screenshot_abs.resolve().as_uri()
        else:
            image_url = screenshot_abs.resolve().as_uri()
        out.append({
            "url": block["url"],
            "author": block["author"],
            "image_url": image_url,
            "jp": block["jp"],
            # en/context render as raw HTML (template autoescape is off), so
            # linkify any [text](url) the user added and HTML-escape the rest.
            "en": inserts_mod.render_inline(block["en"]),
            "context": inserts_mod.render_inline(block["context"]),
        })
    return out


def record_used(md_path: Path) -> None:
    """Append today's picked URLs to the used-ledger so next week's dedup works."""
    try:
        blocks = parse_markdown(md_path)
    except (FileNotFoundError, ValueError):
        return
    urls = [normalize_x_url(b["url"]) for b in blocks if b.get("url")]
    if not urls:
        return
    ledger = load_used_ledger()
    today = dt.datetime.now(dt.timezone.utc).isoformat()
    ledger[today] = urls
    save_used_ledger(ledger)


# ---------------------------------------------------------------------------
# Resolver for newsletter-free.py CLI
# ---------------------------------------------------------------------------

def resolve_md_path(
    *, jp_social: str | None, jp_social_auto: bool,
) -> Path | None:
    """Translate --jp-social / --jp-social-auto into a Path (or None)."""
    if jp_social:
        return Path(jp_social)
    if jp_social_auto:
        today = dt.date.today().isoformat()
        path = _md_path_for(today)
        if not path.exists():
            raise FileNotFoundError(
                f"--jp-social-auto looked for {path} but it does not exist. "
                "Run `python jp_social.py stage` first."
            )
        return path
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage / validate the weekly JP tweets newsletter section.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stage = sub.add_parser("stage", help="Pick + screenshot + write draft .md")
    p_stage.add_argument(
        "--shortlist", default=None,
        help="Pin to one shortlist date (YYYY-MM-DD), single-day mode. "
             "Default: week-in-review pool across recent shortlists.",
    )
    p_stage.add_argument("--count", type=int, default=3,
                         help="Primary picks to stage. Default 3.")
    p_stage.add_argument(
        "--days", type=int, default=7,
        help="Week-pool window in days (ignored if --shortlist is given). "
             "Default 7.",
    )
    p_stage.add_argument(
        "--backups", type=int, default=3,
        help="Backup picks staged below the primaries. Default 3.",
    )
    p_stage.add_argument(
        "--per-slot", type=int, default=1,
        help="Max picks sharing an editorial slot near the top, so the section "
             "spans UJ's mix. Default 1; pass 0 to rank purely by engagement.",
    )

    p_val = sub.add_parser(
        "validate", help="Sanity-check a staged .md (all fields filled)",
    )
    p_val.add_argument("md_path", type=Path)

    args = parser.parse_args()

    if args.cmd == "stage":
        stage(date_str=args.shortlist, count=args.count,
              days=args.days, backups=args.backups, per_slot=args.per_slot)
    elif args.cmd == "validate":
        blocks = parse_markdown(args.md_path)
        print(
            f"{args.md_path}: {len(blocks)} tweet block(s), all required "
            "fields filled."
        )


if __name__ == "__main__":
    main()
