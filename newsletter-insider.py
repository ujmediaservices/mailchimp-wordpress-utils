"""Create a Mailchimp Insider newsletter draft that wraps the free newsletter.

Reads ``data/last-free-newsletter.json`` (written by newsletter-free.py),
layers the full Insider article on top, reuses the editor's note + JP-social
+ extras the free run produced, drops the promoted Insider post from the
carried-over post list if it was teased there, and renders the
``newsletter-insider`` template (which omits ad-style inserts + Insider
upgrade CTAs since the recipients are already paying).

Usage:
    python newsletter-insider.py --insider-post-id 88516 \\
        --insider-segment-id 12345
    python newsletter-insider.py --dump-html --insider-post-id 88516
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html as html_mod
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader

import wp_post


LIST_NAME = "Unseen Japan"
PROJECT_ROOT = Path(__file__).parent
TEMPLATE_DIR = PROJECT_ROOT / "templates"
TEMPLATE_NAME = "newsletter-insider.html.j2"
STATE_FILE = PROJECT_ROOT / "data" / "last-free-newsletter.json"
STATE_MAX_AGE_DAYS = 3

_BANNED_DASH_RE = re.compile(
    r"—|–|&mdash;|&ndash;|&#8212;|&#x2014;|&#8211;|&#x2013;"
)


def strip_banned_dashes(text: str) -> str:
    return _BANNED_DASH_RE.sub(",", text)


# ---------------------------------------------------------------------------
# State file loading
# ---------------------------------------------------------------------------

def load_free_state(path: Path = STATE_FILE) -> dict:
    if not path.exists():
        print(
            f"ERROR: free-newsletter state file not found at {path}. Run "
            "`newsletter-free.py` (the Mailchimp draft step) before invoking "
            "the Insider wrapper.",
            file=sys.stderr,
        )
        sys.exit(1)
    state = json.loads(path.read_text(encoding="utf-8"))

    generated = state.get("generated_at", "")
    try:
        gen_ts = dt.datetime.fromisoformat(generated)
    except ValueError:
        gen_ts = None
    if gen_ts is not None:
        if gen_ts.tzinfo is None:
            gen_ts = gen_ts.replace(tzinfo=dt.timezone.utc)
        age = dt.datetime.now(dt.timezone.utc) - gen_ts
        if age > dt.timedelta(days=STATE_MAX_AGE_DAYS):
            print(
                f"ERROR: free-newsletter state file is {age.days} days old "
                f"(max {STATE_MAX_AGE_DAYS}). Re-run newsletter-free.py for "
                "this week before sending the Insider newsletter.",
                file=sys.stderr,
            )
            sys.exit(1)
    return state


# ---------------------------------------------------------------------------
# Mailchimp API (same shape as newsletter-free.py; copied to keep this
# script standalone — see plan §"Out of scope" for the no-refactor rationale)
# ---------------------------------------------------------------------------

class MailchimpAPI:
    def __init__(self, api_key: str):
        self.dc = api_key.rsplit("-", 1)[-1]
        self.base_url = f"https://{self.dc}.api.mailchimp.com/3.0"
        self.auth = ("apikey", api_key)

    def _get(self, path, params=None):
        r = requests.get(f"{self.base_url}{path}", params=params,
                         auth=self.auth, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path, body):
        r = requests.post(f"{self.base_url}{path}", json=body,
                          auth=self.auth, timeout=30)
        r.raise_for_status()
        return r.json()

    def _put(self, path, body):
        r = requests.put(f"{self.base_url}{path}", json=body,
                         auth=self.auth, timeout=30)
        r.raise_for_status()
        return r.json()

    def find_list(self, name):
        data = self._get("/lists", {"count": 100})
        for lst in data.get("lists", []):
            if lst["name"] == name:
                return lst
        return None

    def upload_image(self, filename, image_bytes):
        encoded = base64.b64encode(image_bytes).decode("ascii")
        data = self._post("/file-manager/files", {
            "name": filename, "file_data": encoded,
        })
        return data["full_size_url"]

    def create_campaign(self, list_id, title, subject, preview_text,
                        segment_id):
        return self._post("/campaigns", {
            "type": "regular",
            "recipients": {
                "list_id": list_id,
                "segment_opts": {
                    "saved_segment_id": segment_id, "match": "all",
                },
            },
            "settings": {
                "subject_line": subject,
                "preview_text": preview_text,
                "title": title,
                "from_name": "Jay at Unseen Japan",
                "reply_to": "jay@unseenjapan.com",
            },
        })

    def set_campaign_content(self, campaign_id, html):
        return self._put(f"/campaigns/{campaign_id}/content", {"html": html})


# ---------------------------------------------------------------------------
# Insider article fetch + intro paragraph
# ---------------------------------------------------------------------------

INSIDER_TAG_RE = re.compile(r"\[insider\]\s*", re.IGNORECASE)


def insider_intro_html(post_title: str, post_url: str) -> str:
    """The lead paragraph that identifies the Insider article + login CTA.

    Sits directly above the full article body. Keeps the brand-color
    framing consistent with the rest of the template.
    """
    clean_title = strip_banned_dashes(INSIDER_TAG_RE.sub("", post_title).strip())
    safe_title = html_mod.escape(clean_title)
    safe_url = html_mod.escape(post_url, quote=True)
    return (
        '<p style="font-size:18px;line-height:1.5;color:#222222;'
        'padding:14px 16px;background-color:#faf3ee;'
        'border-left:4px solid #b3421d;margin:0 0 20px 0;">'
        "This week's Insider exclusive: "
        f'<strong>{safe_title}</strong>. '
        "You're reading the full piece below. To read it on "
        f'<a href="{safe_url}" target="_blank" '
        'style="color:#b3421d;">unseen-japan.com</a>, just log in to your '
        "Insider account."
        "</p>"
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_html(
    *, free_state: dict, insider_post: dict,
    insider_image_url: str | None,
    carried_posts: list[dict],
    wp_site: str | None = None, wp_auth: tuple[str, str] | None = None,
) -> str:
    body_html = wp_post.add_paragraph_spacing(
        insider_post["content_html"], wp_site=wp_site, wp_auth=wp_auth,
    )
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template(TEMPLATE_NAME)
    return template.render(
        insider_intro_html=insider_intro_html(
            insider_post["title"], insider_post["url"],
        ),
        insider_title=strip_banned_dashes(
            INSIDER_TAG_RE.sub("", insider_post["title"]).strip()
        ),
        insider_featured_image_url=insider_image_url,
        insider_body_html=body_html,
        editors_note_html=free_state.get("editors_note_html", ""),
        jp_social=free_state.get("jp_social") or [],
        carried_posts=carried_posts,
        extras=free_state.get("extras") or [],
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _suggest_subject_preview(insider_title: str, insider_excerpt: str) -> tuple[str, str]:
    clean = strip_banned_dashes(INSIDER_TAG_RE.sub("", insider_title).strip())
    subject = f"Insider: {clean}"[:120]
    preview = strip_banned_dashes(insider_excerpt or "")[:140]
    return subject, preview


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Insider newsletter that wraps last week's free draft.",
    )
    parser.add_argument(
        "--insider-post-id", type=int, required=True,
        help="WordPress post ID of this week's Insider article.",
    )
    parser.add_argument(
        "--insider-segment-id", type=int, default=None,
        help=(
            "Mailchimp saved-segment ID for the Insider audience. REQUIRED "
            "for live sends; omitting it aborts the run to prevent an "
            "accidental all-list send. Skipped in --dump-html mode."
        ),
    )
    parser.add_argument("--subject", default=None)
    parser.add_argument("--preview", default=None)
    parser.add_argument(
        "--state", type=Path, default=STATE_FILE,
        help=f"Path to the free-run state file (default {STATE_FILE}).",
    )
    parser.add_argument(
        "--dump-html", action="store_true",
        help="Render and print HTML; don't touch Mailchimp.",
    )
    args = parser.parse_args()

    free_state = load_free_state(args.state)
    print(
        f"Free state loaded: {len(free_state.get('posts', []))} posts, "
        f"{len(free_state.get('extras', []))} extras, "
        f"{len(free_state.get('jp_social', []))} jp tweets, "
        f"editor's note: {'yes' if free_state.get('editors_note_html') else 'no'}.",
        file=sys.stderr,
    )

    # Fetch the Insider article in full
    print(f"Fetching Insider post {args.insider_post_id}...", file=sys.stderr)
    wp_site, wp_auth = wp_post.get_wp_config()
    insider_post = wp_post.fetch_post_full(wp_site, args.insider_post_id, wp_auth)
    print(f"  Title: {insider_post['title']}", file=sys.stderr)

    insider_image_url: str | None = None
    if insider_post.get("featured_media"):
        try:
            insider_image_url = wp_post.get_featured_image_url(
                wp_site, insider_post["featured_media"], wp_auth,
            )
        except requests.HTTPError as exc:
            print(f"  WARNING: featured image fetch failed: {exc}",
                  file=sys.stderr)

    # Build carried-over posts: free posts minus the Insider one we're lifting
    carried_posts = [
        {
            "post_id": p["post_id"],
            "title": strip_banned_dashes(p["title"]),
            "url": p["url"],
            "excerpt": strip_banned_dashes(p["excerpt"]),
            "image_url": p.get("image_url") or "",
        }
        for p in (free_state.get("posts") or [])
        if p["post_id"] != args.insider_post_id
    ]
    skipped = len(free_state.get("posts") or []) - len(carried_posts)
    if skipped:
        print(
            f"  Carrying over {len(carried_posts)} free-newsletter posts "
            f"(dropped {skipped} that match the Insider post).",
            file=sys.stderr,
        )
    else:
        print(
            f"  Carrying over {len(carried_posts)} free-newsletter posts.",
            file=sys.stderr,
        )

    # ----------- subject / preview suggestions -----------
    excerpt_for_preview = ""
    if not args.preview:
        # Fetch excerpt via the WP API for a preview-text default
        try:
            r = requests.get(
                f"{wp_site}/wp-json/wp/v2/posts/{args.insider_post_id}",
                params={"_fields": "excerpt"}, auth=wp_auth, timeout=30,
            )
            r.raise_for_status()
            from bs4 import BeautifulSoup
            excerpt_for_preview = BeautifulSoup(
                r.json().get("excerpt", {}).get("rendered", ""),
                "html.parser",
            ).get_text(strip=True)
        except requests.RequestException:
            pass
    sug_subject, sug_preview = _suggest_subject_preview(
        insider_post["title"], excerpt_for_preview,
    )
    subject = args.subject or sug_subject
    preview = args.preview or sug_preview

    # ----------- render HTML -----------
    print("\nBuilding Insider newsletter HTML...", file=sys.stderr)
    html = render_html(
        free_state=free_state,
        insider_post=insider_post,
        insider_image_url=insider_image_url,
        carried_posts=carried_posts,
        wp_site=wp_site, wp_auth=wp_auth,
    )

    if args.dump_html:
        print(html)
        return

    # ----------- live send: require segment -----------
    if args.insider_segment_id is None:
        print(
            "ERROR: --insider-segment-id is required for live sends "
            "(otherwise the campaign would target the full list).",
            file=sys.stderr,
        )
        sys.exit(1)

    mc_api_key = os.environ.get("MAILCHIMP_API_KEY")
    if not mc_api_key:
        print("ERROR: MAILCHIMP_API_KEY not set.", file=sys.stderr)
        sys.exit(1)
    mc = MailchimpAPI(mc_api_key)

    audience = mc.find_list(LIST_NAME)
    if not audience:
        print(f"ERROR: list '{LIST_NAME}' not found.", file=sys.stderr)
        sys.exit(1)

    print("\nCreating Mailchimp campaign...", file=sys.stderr)
    campaign = mc.create_campaign(
        list_id=audience["id"],
        title=subject,
        subject=subject,
        preview_text=preview,
        segment_id=args.insider_segment_id,
    )
    campaign_id = campaign["id"]
    web_id = campaign.get("web_id", "")
    print(f"  Campaign ID: {campaign_id}", file=sys.stderr)

    mc.set_campaign_content(campaign_id, html)
    print("  Content set.", file=sys.stderr)

    print(
        f"\nInsider draft created!\n"
        f"  Subject: {subject}\n"
        f"  Preview: {preview}\n"
        f"  Segment: {args.insider_segment_id}\n"
        f"  Edit: https://{mc.dc}.admin.mailchimp.com/campaigns/edit"
        f"?id={web_id}",
    )


if __name__ == "__main__":
    main()
