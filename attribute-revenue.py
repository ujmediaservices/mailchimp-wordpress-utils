"""Attribute Stripe revenue (Insider subs + Tours payments) to Mailchimp newsletters
using time-window proximity matching.

Reads pitch-cohort data from data/pitch-analysis.json (produced by an earlier
profile-campaigns run; supplemented here with full timestamps from Mailchimp).
Pulls subscriptions and invoices from Stripe, classifies each as Insider or
Tours, and attributes them to the closest preceding campaign within a window.

Usage:
    python attribute-revenue.py
    python attribute-revenue.py --since 2024-01-01 --windows 24,48,168

Requires:
    STRIPE_API_KEY        (read access to subscriptions, invoices, products, prices)
    MAILCHIMP_API_KEY     (for fresh campaign send timestamps)
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

STRIPE_BASE = "https://api.stripe.com/v1"
PAYPAL_BASE = "https://api-m.paypal.com"
DATA_DIR = Path(__file__).parent / "data"


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------

def stripe_list(endpoint: str, params: dict | None = None,
                expand: list[str] | None = None) -> list[dict]:
    auth = (os.environ["STRIPE_API_KEY"], "")
    out: list[dict] = []
    p = dict(params or {})
    p["limit"] = 100
    if expand:
        for i, e in enumerate(expand):
            p[f"expand[{i}]"] = e
    while True:
        r = requests.get(f"{STRIPE_BASE}/{endpoint}", auth=auth,
                         params=p, timeout=60)
        r.raise_for_status()
        data = r.json()
        out.extend(data["data"])
        if not data.get("has_more"):
            break
        p["starting_after"] = data["data"][-1]["id"]
        time.sleep(0.05)
    return out


# ---------------------------------------------------------------------------
# PayPal
# ---------------------------------------------------------------------------

def paypal_token() -> str | None:
    cid = os.environ.get("PAYPAL_CLIENT_ID")
    sec = os.environ.get("PAYPAL_CLIENT_SECRET")
    if not cid or not sec:
        return None
    r = requests.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        auth=(cid, sec),
        data={"grant_type": "client_credentials"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def paypal_get(endpoint: str, token: str, params: dict | None = None) -> dict:
    r = requests.get(
        f"{PAYPAL_BASE}{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def fetch_paypal_insider_events(since_ts: int) -> list[dict]:
    """Pull PayPal billing subscriptions on Insider plans, return signup events.

    Each event has the same shape as a Stripe Insider event so attribution code
    can treat them uniformly. PayPal v1/billing/subscriptions only exposes subs
    created via the v1 billing API (April 2025 onward for UJ); older PayPal
    Insider activity is not visible here.
    """
    token = paypal_token()
    if not token:
        print("  PAYPAL_CLIENT_ID/SECRET not set; skipping PayPal pull.",
              file=sys.stderr)
        return []

    # Paginate all subs
    all_subs: list[dict] = []
    page = 1
    while True:
        d = paypal_get("/v1/billing/subscriptions", token,
                       {"page_size": 20, "page": page, "total_required": "true"})
        all_subs.extend(d.get("subscriptions", []))
        has_next = any(l.get("rel") == "next" for l in d.get("links", []))
        if not has_next or page > 200:
            break
        page += 1
        time.sleep(0.05)
    print(f"  {len(all_subs)} PayPal subscriptions total", file=sys.stderr)

    # Cache plan name lookups
    plan_name: dict[str, str] = {}
    events: list[dict] = []
    for s in all_subs:
        status = s.get("status", "")
        if status == "APPROVAL_PENDING":
            continue
        ct = s.get("create_time", "")
        if not ct:
            continue
        created_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        if int(created_dt.timestamp()) < since_ts:
            continue
        # Fetch detail (for plan_id and last_payment)
        try:
            detail = paypal_get(f"/v1/billing/subscriptions/{s['id']}", token)
        except requests.HTTPError as e:
            print(f"  WARN: detail fetch {s['id']}: {e}", file=sys.stderr)
            continue
        time.sleep(0.05)
        plan_id = detail.get("plan_id")
        if plan_id and plan_id not in plan_name:
            try:
                plan = paypal_get(f"/v1/billing/plans/{plan_id}", token)
                plan_name[plan_id] = plan.get("name", "")
            except requests.HTTPError:
                plan_name[plan_id] = ""
            time.sleep(0.05)
        name = plan_name.get(plan_id, "")
        if "insider" not in name.lower():
            continue
        last_payment = (detail.get("billing_info") or {}).get("last_payment") or {}
        amount = last_payment.get("amount") or {}
        amt_str = amount.get("value")
        try:
            amt_cents = int(round(float(amt_str) * 100)) if amt_str else 0
        except ValueError:
            amt_cents = 0
        events.append({
            "type": "insider",
            "id": s["id"],
            "created": int(created_dt.timestamp()),
            "amount_cents": amt_cents,
            "currency": (amount.get("currency_code") or "usd").lower(),
            "product_id": plan_id,
            "status": status,
            "source": "paypal",
            "plan_name": name,
        })
    return events


# Confirmed Insider product IDs (derived from inspecting all paid subs since 2024)
INSIDER_PRODUCT_IDS: set[str] = {
    "prod_R0abnJ46feXmDz",  # "Subscribe" (custom-amount Insider, ~91% of paid subs)
    "prod_U9jMbYMmtbiRml",  # "$6 a month"
    "prod_U9jMyAVpmM8qPM",  # "$65 a year"
    "prod_TbDg02d8AZIVIh",  # "$8 a month"
    "prod_TbDg9o2sFgqYq8",  # "$80 a year"
}

# Tours-revenue heuristic per user: any one-off charge over this threshold is Tours.
# Insider signups (monthly $5-20, yearly $65-80) all fall below it.
TOURS_AMOUNT_THRESHOLD_CENTS = 10_000  # $100


# ---------------------------------------------------------------------------
# Mailchimp
# ---------------------------------------------------------------------------

def fetch_campaign_timestamps(min_recipients: int) -> dict[str, dict]:
    """Return {campaign_id: {send_ts, send_iso, sent, subject}} for free campaigns."""
    key = os.environ["MAILCHIMP_API_KEY"]
    dc = key.rsplit("-", 1)[-1]
    base = f"https://{dc}.api.mailchimp.com/3.0"
    auth = ("apikey", key)
    out: dict[str, dict] = {}
    offset = 0
    while True:
        r = requests.get(f"{base}/campaigns", auth=auth, params={
            "status": "sent", "count": 100, "offset": offset,
            "sort_field": "send_time", "sort_dir": "DESC",
        }, timeout=60)
        r.raise_for_status()
        data = r.json()
        batch = data.get("campaigns", [])
        for c in batch:
            sent = c.get("emails_sent") or 0
            if sent < min_recipients:
                continue
            ts_iso = c.get("send_time") or ""
            if not ts_iso:
                continue
            dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
            out[c["id"]] = {
                "send_ts": int(dt.timestamp()),
                "send_iso": ts_iso,
                "sent": sent,
                "subject": c["settings"]["subject_line"],
            }
        if len(batch) < 100:
            break
        offset += 100
    return out


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def attribute_event(event_ts: int, campaign_index: list[tuple[int, str]],
                    window_seconds: int) -> str | None:
    """Find the most recent campaign whose send_ts is within `window_seconds`
    before event_ts. campaign_index is sorted by send_ts ascending."""
    # Binary search would be tidier; linear is fine at this scale.
    matched: tuple[int, str] | None = None
    for ts, cid in campaign_index:
        if ts > event_ts:
            break
        if event_ts - ts <= window_seconds:
            matched = (ts, cid)
    return matched[1] if matched else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2024-01-01",
                        help="Earliest date to pull Stripe data (default: 2024-01-01)")
    parser.add_argument("--windows", default="24,48,168",
                        help="Comma-separated attribution windows in hours (default: 24,48,168)")
    parser.add_argument("--min-recipients", type=int, default=3000,
                        help="Treat campaigns with at least this many recipients as free newsletters")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip Stripe + Mailchimp fetches; reuse data/stripe-events.json")
    args = parser.parse_args()

    windows = [int(w) for w in args.windows.split(",")]
    DATA_DIR.mkdir(exist_ok=True)
    cache_path = DATA_DIR / "stripe-events.json"
    cohort_path = DATA_DIR / "pitch-analysis.json"

    if not cohort_path.exists():
        print(f"ERROR: {cohort_path} not found. Run profile-campaigns.py first or "
              f"refresh pitch data.", file=sys.stderr)
        sys.exit(1)

    # ----------------------------------------------------------------------
    # Fetch Stripe + Mailchimp
    # ----------------------------------------------------------------------
    if args.no_fetch and cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        events = cache["events"]
        campaign_meta = cache["campaign_meta"]
        print(f"Loaded {len(events)} events and {len(campaign_meta)} campaigns from cache",
              file=sys.stderr)
    else:
        since_ts = int(datetime.fromisoformat(args.since)
                       .replace(tzinfo=timezone.utc).timestamp())

        # Insider signups via subscriptions
        print(f"Fetching Stripe subscriptions since {args.since}...", file=sys.stderr)
        subs = stripe_list("subscriptions",
                           {"status": "all", "created[gte]": since_ts},
                           expand=["data.items.data.price"])
        print(f"  {len(subs)} subscriptions", file=sys.stderr)

        events: list[dict] = []
        for s in subs:
            if s.get("status") == "incomplete_expired":
                continue
            items = s.get("items", {}).get("data", [])
            if not items:
                continue
            price = items[0].get("price", {}) or {}
            prod_id = price.get("product")
            if prod_id not in INSIDER_PRODUCT_IDS:
                continue
            events.append({
                "type": "insider",
                "id": s["id"],
                "created": s["created"],
                "amount_cents": price.get("unit_amount") or 0,
                "currency": price.get("currency", "usd"),
                "product_id": prod_id,
                "status": s.get("status"),
                "source": "stripe",
            })
        ins_n = sum(1 for e in events if e["type"] == "insider")
        ins_amt = sum(e["amount_cents"] for e in events if e["type"] == "insider") / 100
        print(f"  {ins_n} Stripe Insider signups (signup-value sum: ${ins_amt:,.2f})",
              file=sys.stderr)

        # PayPal Insider signups
        print("Fetching PayPal Insider subscriptions...", file=sys.stderr)
        paypal_events = fetch_paypal_insider_events(since_ts)
        events.extend(paypal_events)
        pp_amt = sum(e["amount_cents"] for e in paypal_events) / 100
        print(f"  {len(paypal_events)} PayPal Insider signups "
              f"(signup-value sum: ${pp_amt:,.2f})", file=sys.stderr)

        # Tours via direct payment intents > $100
        print(f"Fetching Stripe payment intents since {args.since}...", file=sys.stderr)
        pis = stripe_list("payment_intents", {"created[gte]": since_ts})
        print(f"  {len(pis)} payment intents", file=sys.stderr)

        for pi in pis:
            if pi.get("status") != "succeeded":
                continue
            if (pi.get("amount") or 0) < TOURS_AMOUNT_THRESHOLD_CENTS:
                continue
            if pi.get("invoice"):
                # Linked to an invoice; skip (would only be sub-related at this scale).
                continue
            events.append({
                "type": "tours",
                "id": pi["id"],
                "created": pi["created"],
                "amount_cents": pi["amount"],
                "currency": pi.get("currency", "usd"),
                "description": (pi.get("description") or "")[:120],
                "status": "succeeded",
                "source": "stripe",
            })
        tours_n = sum(1 for e in events if e["type"] == "tours")
        tours_amt = sum(e["amount_cents"] for e in events if e["type"] == "tours") / 100
        print(f"  {tours_n} Tours payment events (sum: ${tours_amt:,.2f})", file=sys.stderr)

        print("Fetching Mailchimp campaign timestamps...", file=sys.stderr)
        campaign_meta = fetch_campaign_timestamps(args.min_recipients)
        print(f"  {len(campaign_meta)} free-newsletter campaigns", file=sys.stderr)

        cache_path.write_text(json.dumps({
            "events": events, "campaign_meta": campaign_meta,
        }, indent=2), encoding="utf-8")
        print(f"Cached to {cache_path}", file=sys.stderr)

    # ----------------------------------------------------------------------
    # Load cohort data
    # ----------------------------------------------------------------------
    cohort_rows = json.loads(cohort_path.read_text(encoding="utf-8"))
    cohort_by_id = {r["id"]: r for r in cohort_rows}

    # Cohort assignment per campaign
    def cohort_of(c: dict) -> str:
        if c["has_tours"] and c["has_insider"]: return "both"
        if c["has_tours"]: return "tours_only"
        if c["has_insider"]: return "insider_only"
        return "neither"

    # Build campaign index sorted by send_ts ascending
    enriched: list[dict] = []
    for cid, meta in campaign_meta.items():
        if cid not in cohort_by_id:
            continue
        row = cohort_by_id[cid]
        send_ts = meta["send_ts"]
        if datetime.fromtimestamp(send_ts, tz=timezone.utc).year < 2024:
            # Outside Stripe pull window; skip to avoid undercounting attribution
            continue
        enriched.append({
            "id": cid,
            "send_ts": send_ts,
            "send_iso": meta["send_iso"],
            "sent": meta["sent"],
            "subject": meta["subject"],
            "cohort": cohort_of(row),
        })
    enriched.sort(key=lambda r: r["send_ts"])
    campaign_index = [(r["send_ts"], r["id"]) for r in enriched]
    print(f"\nAttributing across {len(enriched)} campaigns (>= 2024)",
          file=sys.stderr)

    # ----------------------------------------------------------------------
    # Attribute and aggregate per window
    # ----------------------------------------------------------------------
    def empty_bucket():
        return {"insider_amt": 0, "insider_n": 0, "tours_amt": 0, "tours_n": 0,
                "campaigns": 0, "sent": 0}

    for window_h in windows:
        window_s = window_h * 3600
        cohort_stats: dict[str, dict] = defaultdict(empty_bucket)
        attributed: dict[str, dict] = {r["id"]: empty_bucket() for r in enriched}
        for ev in events:
            cid = attribute_event(ev["created"], campaign_index, window_s)
            if not cid:
                continue
            if cid not in attributed:
                continue
            kind = ev["type"]
            attributed[cid][f"{kind}_amt"] += ev["amount_cents"]
            attributed[cid][f"{kind}_n"] += 1

        for r in enriched:
            cohort = r["cohort"]
            stats = cohort_stats[cohort]
            stats["campaigns"] += 1
            stats["sent"] += r["sent"]
            for k in ("insider_amt", "insider_n", "tours_amt", "tours_n"):
                stats[k] += attributed[r["id"]][k]

        # Output
        print()
        print(f"=== Attribution window: {window_h}h ({window_h//24}d {window_h%24}h) ===")
        print(f"{'Cohort':14} {'N':>3} {'Sent':>7} "
              f"{'Insider $':>11} {'Insider/send':>14} "
              f"{'Tours $':>11} {'Tours/send':>13} "
              f"{'Total/send':>13}")
        order = ["insider_only", "tours_only", "both", "neither"]
        for c in order:
            s = cohort_stats[c]
            if s["campaigns"] == 0:
                continue
            ins_total = s["insider_amt"] / 100
            tours_total = s["tours_amt"] / 100
            ins_per = ins_total / s["campaigns"]
            tours_per = tours_total / s["campaigns"]
            total_per = ins_per + tours_per
            print(f"{c:14} {s['campaigns']:>3} {s['sent']:>7} "
                  f"${ins_total:>9.0f} ({s['insider_n']:>2})  ${ins_per:>10.2f}  "
                  f"${tours_total:>8.0f} ({s['tours_n']:>2})  ${tours_per:>10.2f}  "
                  f"${total_per:>10.2f}")


if __name__ == "__main__":
    main()
