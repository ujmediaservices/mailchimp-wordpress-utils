"""Shared 'Also from Japan this week' helpers.

Both newsletter-free.py and newsletter-single-post.py append a section of
external Japanese-language stories that UJ tracked but didn't cover. The
source is the /find-content skill's NDJSON trend log.

Loader contract (used by both scripts):

    extras = load_extras_from_trend_log(
        days=7, cap=4,
        log_path=DEFAULT_TREND_LOG,
        exclude_urls={...},
    )
    template_extras = render_extras_for_template(extras)

Filters: relevance in HIGH/VERY HIGH, within `days`, URL not in exclude_urls.
Dedupes by URL (keeps freshest seen_at). Sorted by publish date desc.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup


DEFAULT_TREND_LOG = Path(
    os.environ.get(
        "FIND_CONTENT_TREND_LOG",
        r"G:/My Drive/Unseen Japan/Code/find-content/trends/observations.ndjson",
    )
)
RELEVANCE_FLOOR = {"HIGH", "VERY HIGH"}
DEFAULT_DAYS = 7
DEFAULT_CAP = 4


def fetch_post_body(
    wp_site: str, post_id: int, auth: tuple[str, str]
) -> str:
    resp = requests.get(
        f"{wp_site}/wp-json/wp/v2/posts/{post_id}",
        params={"_fields": "content"},
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("content", {}).get("rendered", "")


def collect_cited_urls(post_bodies: list[str]) -> set[str]:
    cited: set[str] = set()
    for body in post_bodies:
        soup = BeautifulSoup(body, "html.parser")
        for a in soup.find_all("a"):
            href = a.get("href", "").strip()
            if href.startswith(("http://", "https://")):
                cited.add(href)
    return cited


def load_extras_from_trend_log(
    *, days: int, cap: int, log_path: Path, exclude_urls: set[str]
) -> list[dict]:
    if not log_path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    by_url: dict[str, dict] = {}
    with log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen = rec.get("seen_at", "")
            try:
                t = datetime.fromisoformat(seen)
            except ValueError:
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if t < cutoff:
                continue
            if rec.get("relevance") not in RELEVANCE_FLOOR:
                continue
            url = (rec.get("url") or "").strip()
            if not url or url in exclude_urls:
                continue
            existing = by_url.get(url)
            if existing is None or rec.get("seen_at", "") > existing.get("seen_at", ""):
                by_url[url] = rec
    deduped = list(by_url.values())
    deduped.sort(key=lambda r: r.get("published_iso", ""), reverse=True)
    return deduped[:cap]


def render_extras_for_template(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for e in records:
        out.append({
            "title_en": e.get("title_en") or e.get("title_jp") or "(untitled)",
            "source": (e.get("source") or "").strip(),
            "url": (e.get("url") or "").strip(),
            "synopsis": (e.get("synopsis") or "").strip(),
            "topics": e.get("topics") or [],
        })
    return out


def add_extras_cli_args(parser) -> None:
    """Add the --extras-* flags to an argparse parser. Both scripts share these."""
    parser.add_argument(
        "--extras-from-trend-log", action="store_true",
        help=(
            "Append an 'Also from Japan this week' section sourced from the "
            "/find-content trend log. Filters to HIGH/VERY HIGH within "
            "--extras-days, drops URLs already cited in the covered posts, "
            "dedupes by URL, and caps at --extras-cap."
        ),
    )
    parser.add_argument(
        "--extras-json", type=Path, default=None,
        help=(
            "Hand-curated extras: path to a JSON file with a list of objects "
            "having url/title_en/source/synopsis/topics. Bypasses the trend log."
        ),
    )
    parser.add_argument(
        "--extras-days", type=int, default=DEFAULT_DAYS,
        help=f"Lookback window for trend-log extras (default {DEFAULT_DAYS}).",
    )
    parser.add_argument(
        "--extras-cap", type=int, default=DEFAULT_CAP,
        help=f"Maximum number of extras to include (default {DEFAULT_CAP}).",
    )
    parser.add_argument(
        "--extras-exclude", action="append", default=[],
        help=(
            "Repeatable: URL to exclude from extras (e.g. stories already "
            "shared on social)."
        ),
    )
    parser.add_argument(
        "--extras-log-path", type=Path, default=DEFAULT_TREND_LOG,
        help=(
            "Path to the find-content NDJSON trend log. Defaults to "
            "FIND_CONTENT_TREND_LOG env var or the standard project path."
        ),
    )


def resolve_extras(
    args, *, wp_site: str, wp_auth: tuple[str, str], post_ids: list[int],
    post_urls: list[str],
    log,
) -> list[dict]:
    """Resolve extras from CLI args. Returns template-ready dicts (or [])."""
    import sys as _sys
    if args.extras_json is not None:
        log(f"\nLoading extras from {args.extras_json}...")
        try:
            raw = json.loads(args.extras_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log(f"  ERROR: could not read extras JSON: {e}")
            _sys.exit(1)
        if not isinstance(raw, list):
            log("  ERROR: extras JSON must be a list of objects.")
            _sys.exit(1)
        extras = render_extras_for_template(raw)[: args.extras_cap]
        log(f"  Loaded {len(extras)} extra(s).")
        return extras

    if not args.extras_from_trend_log:
        return []

    log(f"\nLoading extras from trend log ({args.extras_log_path})...")
    exclude_urls: set[str] = set(post_urls)
    post_bodies: list[str] = []
    for post_id in post_ids:
        try:
            body_html = fetch_post_body(wp_site, post_id, wp_auth)
        except requests.exceptions.HTTPError as e:
            log(f"  WARNING: could not fetch body for {post_id}: {e}")
            continue
        post_bodies.append(body_html)
    cited = collect_cited_urls(post_bodies)
    exclude_urls.update(cited)
    for u in args.extras_exclude:
        exclude_urls.add(u.strip())
    log(
        f"  Excluding {len(exclude_urls)} URL(s) "
        f"({len(args.extras_exclude)} manual, {len(cited)} cited in posts)."
    )
    raw_extras = load_extras_from_trend_log(
        days=args.extras_days,
        cap=args.extras_cap,
        log_path=args.extras_log_path,
        exclude_urls=exclude_urls,
    )
    extras = render_extras_for_template(raw_extras)
    if not extras:
        log(
            f"  No HIGH/VERY HIGH observations found in the last "
            f"{args.extras_days} day(s) at {args.extras_log_path}. "
            f"The 'Also from Japan this week' section will be omitted."
        )
    else:
        log(f"  Loaded {len(extras)} extra(s).")
    return extras


SAMPLE_EXTRAS = [
    {
        "title_en": "Female Osaka Prosecutor Resigns Citing #MeToo Retaliation",
        "source": "Asahi Shimbun",
        "url": "https://www.asahi.com/example/1",
        "synopsis": "A senior Osaka district prosecutor resigned this week after publicly accusing supervisors of retaliating against her #MeToo complaint. Internal records cited in her statement show the inquiry was closed within a week.",
        "topics": ["#metoo", "prosecutor misconduct", "workplace harassment"],
    },
    {
        "title_en": "Tokyo Shimbun: Should Sex-Work Buyers Face Criminal Penalties?",
        "source": "Tokyo Shimbun",
        "url": "https://www.tokyo-np.co.jp/example/2",
        "synopsis": "An editorial weighing whether Japan should adopt a Nordic-model framework that criminalizes the buyer rather than the seller, citing data from Sweden and Norway.",
        "topics": ["sex work policy", "nordic model"],
    },
    {
        "title_en": "39% of Elderly Abuse Cases Now Involve Adult Sons, Ministry Says",
        "source": "NHK",
        "url": "https://www3.nhk.or.jp/example/3",
        "synopsis": "The Ministry of Health, Labour and Welfare's annual elder-abuse report flags adult sons as the largest single category of abusers for the first time.",
        "topics": ["elder abuse", "demographic shift"],
    },
    {
        "title_en": "Niigata Judo Shidōshi Faces Charges Over Decades-Old Coaching Abuse",
        "source": "Mainichi",
        "url": "https://mainichi.jp/example/4",
        "synopsis": "Police filed charges this week against a long-tenured judo instructor in Niigata after multiple former students came forward with corroborating accounts.",
        "topics": ["coaching abuse", "judo", "delayed reporting"],
    },
]
