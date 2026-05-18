"""Profile sent Mailchimp campaigns: rank subject lines and articles by engagement.

Usage:
    python profile-campaigns.py
    python profile-campaigns.py --min-recipients 3000 --top 15

By default treats any sent campaign with at least 3,000 recipients as a free
newsletter (Insider campaigns go to ~138 paid subscribers).
"""

import argparse
import os
import sys
from collections import defaultdict
from urllib.parse import urlparse, urlunparse

import requests


def get_mc_config() -> tuple[str, tuple[str, str]]:
    key = os.environ.get("MAILCHIMP_API_KEY")
    if not key:
        print("ERROR: MAILCHIMP_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    dc = key.rsplit("-", 1)[-1]
    return f"https://{dc}.api.mailchimp.com/3.0", ("apikey", key)


def fetch_all_sent_campaigns(base: str, auth, min_recipients: int) -> list[dict]:
    out: list[dict] = []
    offset = 0
    page_size = 100
    while True:
        r = requests.get(
            f"{base}/campaigns",
            auth=auth,
            params={
                "status": "sent",
                "count": page_size,
                "offset": offset,
                "sort_field": "send_time",
                "sort_dir": "DESC",
            },
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        batch = data.get("campaigns", [])
        out.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return [c for c in out if (c.get("emails_sent") or 0) >= min_recipients]


def fetch_click_details(base: str, auth, campaign_id: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        r = requests.get(
            f"{base}/reports/{campaign_id}/click-details",
            auth=auth,
            params={"count": 1000, "offset": offset},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        batch = data.get("urls_clicked", [])
        out.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return out


def normalize_article_url(url: str) -> str | None:
    """Return a canonical https://unseen-japan.com/<slug>/ URL or None."""
    try:
        p = urlparse(url)
    except Exception:
        return None
    host = p.netloc.lower()
    if "unseen-japan.com" not in host and "unseenjapan.com" not in host:
        return None
    path = p.path
    if not path.endswith("/"):
        path = path + "/"
    slug = path.strip("/")
    if not slug or slug in {
        "latest", "subscribe", "support", "membership-registration",
        "category/insider", "about", "contact", "donate",
    }:
        return None
    return urlunparse(("https", "unseen-japan.com", path, "", "", ""))


def print_subject_table(label: str, rows: list[dict], sort_key: str, top: int) -> None:
    print(f"\n=== {label} ===\n")
    ranked = sorted(rows, key=lambda r: r[sort_key], reverse=True)[:top]
    print(f"{'Open%':>6}  {'Click%':>6}  {'Sent':>6}  {'Date':10}  Subject")
    for r in ranked:
        print(
            f"{r['open_rate'] * 100:5.1f}%  "
            f"{r['click_rate'] * 100:5.1f}%  "
            f"{r['emails_sent']:6d}  "
            f"{r['send_time']:10}  "
            f"{r['subject']}"
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--min-recipients", type=int, default=3000,
        help="Minimum emails_sent to qualify (default: 3000)",
    )
    p.add_argument("--top", type=int, default=15, help="Rows per ranking")
    p.add_argument(
        "--skip-articles", action="store_true",
        help="Skip per-article click aggregation (faster)",
    )
    args = p.parse_args()

    base, auth = get_mc_config()

    print("Fetching campaigns...", file=sys.stderr)
    campaigns = fetch_all_sent_campaigns(base, auth, args.min_recipients)
    print(f"  {len(campaigns)} campaigns with >= {args.min_recipients} recipients",
          file=sys.stderr)

    subject_rows: list[dict] = []
    for c in campaigns:
        s = c.get("report_summary") or {}
        sent = c.get("emails_sent") or 0
        if sent == 0:
            continue
        subject_rows.append({
            "subject": c["settings"]["subject_line"],
            "send_time": (c.get("send_time") or "")[:10],
            "emails_sent": sent,
            "unique_opens": s.get("unique_opens", 0),
            "open_rate": s.get("open_rate", 0),
            "subscriber_clicks": s.get("subscriber_clicks", 0),
            "click_rate": s.get("click_rate", 0),
            "campaign_id": c["id"],
        })

    print_subject_table(
        "TOP SUBJECT LINES BY UNIQUE OPEN RATE",
        subject_rows, "open_rate", args.top,
    )
    print_subject_table(
        "TOP SUBJECT LINES BY UNIQUE CLICK RATE (more reliable post-Apple-MPP)",
        subject_rows, "click_rate", args.top,
    )

    if args.skip_articles:
        return

    print(f"\nFetching click-details for {len(campaigns)} campaigns...", file=sys.stderr)
    url_stats: dict[str, dict] = defaultdict(
        lambda: {"unique_clicks": 0, "total_clicks": 0, "campaigns": 0}
    )
    for i, c in enumerate(campaigns):
        if i % 20 == 0:
            print(f"  {i}/{len(campaigns)}", file=sys.stderr)
        try:
            urls = fetch_click_details(base, auth, c["id"])
        except requests.HTTPError as e:
            print(f"  WARN: {c['id']}: {e}", file=sys.stderr)
            continue
        for u in urls:
            url = u.get("url", "")
            canonical = normalize_article_url(url)
            if not canonical:
                continue
            url_stats[canonical]["unique_clicks"] += u.get("unique_clicks", 0) or 0
            url_stats[canonical]["total_clicks"] += u.get("total_clicks", 0) or 0
            url_stats[canonical]["campaigns"] += 1

    print("\n=== TOP ARTICLES BY TOTAL UNIQUE CLICKS (across all campaigns) ===\n")
    article_rows = sorted(
        url_stats.items(),
        key=lambda kv: kv[1]["unique_clicks"],
        reverse=True,
    )[:args.top]
    print(f"{'Uniq':>5}  {'Total':>5}  {'In #':>4}  URL")
    for url, stats in article_rows:
        print(
            f"{stats['unique_clicks']:5d}  "
            f"{stats['total_clicks']:5d}  "
            f"{stats['campaigns']:4d}  "
            f"{url}"
        )


if __name__ == "__main__":
    main()
