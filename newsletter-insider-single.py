"""Create a standalone Mailchimp Insider draft for ONE article.

Unlike ``newsletter-insider.py`` (the weekly wrapper, which layers the Insider
article on top of that week's free newsletter), this script sends the article
by itself: no carried-over posts, no editor's note, no JP tweets, no extras,
no ad inserts. Both modes go to the Insider segment.

Two formats:

* **full** (default) - the whole article body in the email, the way the weekly
  Insider newsletter renders it.
* **``--link-full-article``** - only the portion BEFORE the ``[uj_insider_gate]``
  paywall break, followed by a "Read more on Unseen Japan" button. Use this for
  pieces whose full text shouldn't land in an inbox (adult or otherwise
  sensitive content); readers finish it on the site.

Formatting lives in ``templates/newsletter-insider-single.html.j2`` so the
layout, the greeting copy, and the read-more blurb can be edited without
touching this file. ``--message PATH.md`` drops a one-off note (rendered with
the same Markdown vocabulary as the newsletter inserts) above the article.

Usage:
    python newsletter-insider-single.py --post-id 95679 --segment-id 4230717
    python newsletter-insider-single.py --post-id 95679 --link-full-article \\
        --segment-id 4230717 --message inserts/content-note.md
    python newsletter-insider-single.py --post-id 95679 --link-full-article \\
        --dump-html
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader

import campaign_html
import inserts as inserts_mod
import wp_post


LIST_NAME = "Unseen Japan"
PROJECT_ROOT = Path(__file__).parent
TEMPLATE_DIR = PROJECT_ROOT / "templates"
TEMPLATE_NAME = "newsletter-insider-single.html.j2"

INSIDER_BACK_ISSUES_URL = "https://unseen-japan.com/category/insider"
MEMBERSHIP_LOGIN_URL = "https://unseen-japan.com/membership-login/"

DEFAULT_READ_MORE_LABEL = "Read more on Unseen Japan"
DEFAULT_READ_MORE_BLURB = "The rest of this piece continues on the site."
DEFAULT_FOLDER = "UJ Insider"
SCRIPT_NAME = "newsletter-insider-single"

_BANNED_DASH_RE = re.compile(
    r"[ \t]*(?:—|–|&mdash;|&ndash;|&#8212;|&#x2014;|&#8211;|&#x2013;)[ \t]*"
)

# The `[Insider]` title prefix was abolished 2026-08-10, but old drafts and
# hand-typed overrides may still carry it. Same no-op-in-practice strip the
# weekly script keeps.
INSIDER_TAG_RE = re.compile(r"\[insider\]\s*", re.IGNORECASE)


def strip_banned_dashes(text: str) -> str:
    return _BANNED_DASH_RE.sub(", ", text)


# ---------------------------------------------------------------------------
# Mailchimp API (same shape as newsletter-free.py / newsletter-insider.py;
# copied for the same reason those two don't share it: each newsletter script
# stays standalone so a change to one can't regress a live weekly send)
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

    def find_folder(self, name):
        """Return the campaign-folder id whose name matches (case-insensitive)."""
        data = self._get("/campaign-folders", {"count": 100})
        target = name.strip().lower()
        for folder in data.get("folders", []):
            if folder.get("name", "").strip().lower() == target:
                return folder["id"]
        return None

    def create_campaign(self, list_id, title, subject, preview_text,
                        segment_id, folder_id=None):
        settings = {
            "subject_line": subject,
            "preview_text": preview_text,
            "title": title,
            "from_name": "Jay at Unseen Japan",
            "reply_to": "jay@unseenjapan.com",
        }
        if folder_id:
            settings["folder_id"] = folder_id
        return self._post("/campaigns", {
            "type": "regular",
            "recipients": {
                "list_id": list_id,
                # For a SAVED segment, Mailchimp wants saved_segment_id alone.
                # Adding match/conditions makes it an ad-hoc segment (with no
                # conditions => the whole list).
                "segment_opts": {
                    "saved_segment_id": segment_id,
                },
            },
            "settings": settings,
        })

    def set_campaign_content(self, campaign_id, html):
        return self._put(f"/campaigns/{campaign_id}/content", {"html": html})


# ---------------------------------------------------------------------------
# Body selection
# ---------------------------------------------------------------------------

def select_body_html(post: dict, *, link_mode: bool, log) -> str:
    """Return the article HTML to render: whole body, or the open portion.

    In link mode this hard-errors rather than falling back to the full body.
    The whole point of the mode is keeping gated text out of inboxes, so a
    missing marker must never degrade into "send everything."
    """
    if not link_mode:
        return post["content_html"]

    raw = post.get("raw") or ""
    if not raw:
        print(
            "ERROR: --link-full-article needs the raw post body to find the "
            "[uj_insider_gate] break, but WordPress returned only rendered "
            "content (the credentials likely lack edit access on this post). "
            "Refusing to guess where the paywall starts.",
            file=sys.stderr,
        )
        sys.exit(1)

    open_raw, found = wp_post.split_raw_at_insider_gate(raw)
    if not found:
        print(
            "ERROR: no [uj_insider_gate] (or legacy [swpm_protected]) marker "
            f"in post {post['url']}. Without it there is no paywall break to "
            "cut at, and sending the full body is exactly what "
            "--link-full-article exists to prevent. Add the gate to the post "
            "in WordPress, or drop --link-full-article to send it in full.",
            file=sys.stderr,
        )
        sys.exit(1)

    open_html = wp_post.raw_blocks_to_html(open_raw)
    if not open_html.strip():
        print(
            "ERROR: the portion before the Insider gate is empty, so the "
            "teaser would be a headline and a button. Move the gate further "
            "down the post in WordPress.",
            file=sys.stderr,
        )
        sys.exit(1)

    pct = 100 * len(open_raw) / len(raw)
    log(
        f"  Teaser mode: cutting at the Insider gate, keeping the first "
        f"{len(open_raw)} of {len(raw)} raw chars ({pct:.0f}%)."
    )
    return open_html


def load_message(path: Path | None, log) -> str:
    """Render an optional one-off Markdown note into newsletter HTML."""
    if path is None:
        return ""
    if not path.exists():
        print(f"ERROR: --message file not found: {path}", file=sys.stderr)
        sys.exit(1)
    if not path.read_text(encoding="utf-8").strip():
        print(f"ERROR: --message file is empty: {path}", file=sys.stderr)
        sys.exit(1)
    rendered = strip_banned_dashes(inserts_mod.render_markdown_insert(path))
    log(f"  One-off message: loaded {path.name}.")
    return rendered


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_html(
    *, post: dict, body_html: str, image_url: str | None,
    link_mode: bool, message_html: str,
    read_more_label: str, read_more_blurb: str,
    wp_site: str | None = None, wp_auth: tuple[str, str] | None = None,
) -> str:
    styled_body = strip_banned_dashes(
        wp_post.add_paragraph_spacing(
            body_html, wp_site=wp_site, wp_auth=wp_auth,
        )
    )
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template(TEMPLATE_NAME)
    return template.render(
        insider_title=strip_banned_dashes(
            INSIDER_TAG_RE.sub("", post["title"]).strip()
        ),
        insider_url=post["url"],
        insider_featured_image_url=image_url,
        insider_body_html=styled_body,
        link_mode=link_mode,
        message_html=message_html,
        read_more_label=strip_banned_dashes(read_more_label),
        read_more_blurb=strip_banned_dashes(read_more_blurb),
        back_issues_url=INSIDER_BACK_ISSUES_URL,
        login_url=MEMBERSHIP_LOGIN_URL,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _fetch_excerpt(wp_site, post_id, wp_auth) -> str:
    try:
        r = requests.get(
            f"{wp_site}/wp-json/wp/v2/posts/{post_id}",
            params={"_fields": "excerpt"}, auth=wp_auth, timeout=30,
        )
        r.raise_for_status()
        from bs4 import BeautifulSoup
        return BeautifulSoup(
            r.json().get("excerpt", {}).get("rendered", ""), "html.parser",
        ).get_text(strip=True)
    except requests.RequestException:
        return ""


def _suggest_subject_preview(title: str, excerpt: str) -> tuple[str, str]:
    clean = strip_banned_dashes(INSIDER_TAG_RE.sub("", title).strip())
    subject = f"Insider: {clean}"[:120]
    preview = strip_banned_dashes(excerpt or "")[:140]
    return subject, preview


def send_edited_html(args, log) -> None:
    """--send-html: create the draft from a hand-edited file, no render."""
    html, meta = campaign_html.read(
        args.send_html, script=SCRIPT_NAME,
        allow_dashes=args.allow_dashes, log=log,
    )
    cfg = campaign_html.settings(
        meta,
        subject=args.subject, preview=args.preview,
        segment_id=args.segment_id, folder=args.folder,
    )
    subject = cfg.get("subject")
    if not subject:
        print(
            "ERROR: no subject line. Pass --subject, or send a file whose "
            "sidecar carries one.", file=sys.stderr,
        )
        sys.exit(1)
    if cfg.get("segment_id") is None:
        print(
            "ERROR: no segment. Pass --segment-id (otherwise the campaign "
            "would target the full list).", file=sys.stderr,
        )
        sys.exit(1)

    mc_api_key = os.environ.get("MAILCHIMP_API_KEY")
    if not mc_api_key:
        print("ERROR: MAILCHIMP_API_KEY not set.", file=sys.stderr)
        sys.exit(1)
    mc = MailchimpAPI(mc_api_key)

    log("\nCreating Mailchimp campaign from the edited file...")
    _, web_id = campaign_html.create_draft(
        mc, list_name=LIST_NAME, subject=subject,
        preview=cfg.get("preview", ""), segment_id=cfg["segment_id"],
        folder=cfg.get("folder", DEFAULT_FOLDER), html=html, log=log,
    )
    print(
        f"\nInsider single-article draft created from {args.send_html}!\n"
        f"  Mode: {cfg.get('mode', 'unknown')}\n"
        f"  Subject: {subject}\n"
        f"  Preview: {cfg.get('preview', '')}\n"
        f"  Segment: {cfg['segment_id']}\n"
        f"  Edit: https://{mc.dc}.admin.mailchimp.com/campaigns/edit"
        f"?id={web_id}",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Standalone Insider draft for a single WordPress article.",
    )
    parser.add_argument(
        "--post-id", type=int, default=None,
        help=(
            "WordPress post ID of the Insider article. Required unless "
            "--send-html is supplying already-rendered content."
        ),
    )
    parser.add_argument(
        "--link-full-article", action="store_true",
        help=(
            "Send only the portion before the [uj_insider_gate] paywall break, "
            "followed by a 'Read more on Unseen Japan' button. Use for pieces "
            "whose full text shouldn't land in an inbox."
        ),
    )
    parser.add_argument(
        "--segment-id", type=int, default=None,
        help=(
            "Mailchimp saved-segment ID for the Insider audience. REQUIRED for "
            "live runs; omitting it aborts to prevent an accidental all-list "
            "send. Ignored in --dump-html mode."
        ),
    )
    parser.add_argument("--subject", default=None)
    parser.add_argument("--preview", default=None)
    parser.add_argument(
        "--message", type=Path, default=None, metavar="PATH.md",
        help=(
            "Markdown file rendered as a one-off note above the article "
            "(same syntax as newsletter inserts: image, ## heading, "
            "paragraphs with [links](url), {{URL Button Text}})."
        ),
    )
    parser.add_argument(
        "--read-more-label", default=DEFAULT_READ_MORE_LABEL,
        help="Button text in --link-full-article mode. Default: %(default)s",
    )
    parser.add_argument(
        "--read-more-blurb", default=DEFAULT_READ_MORE_BLURB,
        help="Line above the button in --link-full-article mode.",
    )
    parser.add_argument(
        "--no-image", action="store_true",
        help=(
            "Suppress the featured image. Useful when the artwork itself is "
            "the reason the piece is going out as a teaser."
        ),
    )
    parser.add_argument(
        "--folder", default=None,
        help=(
            "Mailchimp campaign-folder name to file the draft under (matched "
            f"case-insensitively). Default: {DEFAULT_FOLDER}. Pass an empty "
            "string to leave the campaign unfiled."
        ),
    )
    parser.add_argument(
        "--dump-html", action="store_true",
        help="Render and print HTML; don't touch Mailchimp.",
    )
    campaign_html.add_args(parser)
    args = parser.parse_args()

    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    # --send-html: ship a previously written, hand-edited file. Skips the
    # WordPress fetch and the render entirely.
    if args.send_html:
        send_edited_html(args, log)
        return

    if args.post_id is None:
        parser.error("--post-id is required (unless using --send-html)")

    folder = args.folder if args.folder is not None else DEFAULT_FOLDER

    print(f"Fetching post {args.post_id}...", file=sys.stderr)
    wp_site, wp_auth = wp_post.get_wp_config()
    post = wp_post.fetch_post_full(wp_site, args.post_id, wp_auth)
    log(f"  Title: {post['title']}")
    log(f"  URL: {post['url']}")
    log(f"  Mode: {'teaser + read-more link' if args.link_full_article else 'full article'}")

    body_html = select_body_html(post, link_mode=args.link_full_article, log=log)
    message_html = load_message(args.message, log=log)

    image_url: str | None = None
    if args.no_image:
        log("  Featured image: suppressed via --no-image.")
    elif post.get("featured_media"):
        try:
            image_url = wp_post.get_featured_image_url(
                wp_site, post["featured_media"], wp_auth,
            )
        except requests.HTTPError as exc:
            log(f"  WARNING: featured image fetch failed: {exc}")

    # ----------- subject / preview -----------
    excerpt = "" if args.preview else _fetch_excerpt(wp_site, args.post_id, wp_auth)
    sug_subject, sug_preview = _suggest_subject_preview(post["title"], excerpt)
    subject = args.subject or sug_subject
    preview = args.preview or sug_preview

    # ----------- live setup (before render, so a WebP featured image can be
    #             converted, hosted on Mailchimp, and used in the HTML).
    #             --write-html needs Mailchimp for that same reason, but not
    #             a segment: that can wait until --send-html. -----------------
    mc = None
    if not args.dump_html:
        if args.segment_id is None and not args.write_html:
            print(
                "ERROR: --segment-id is required for live runs (otherwise the "
                "campaign would target the full list).",
                file=sys.stderr,
            )
            sys.exit(1)
        mc_api_key = os.environ.get("MAILCHIMP_API_KEY")
        if not mc_api_key:
            print("ERROR: MAILCHIMP_API_KEY not set.", file=sys.stderr)
            sys.exit(1)
        mc = MailchimpAPI(mc_api_key)

        # Mailchimp rejects WebP. Convert to JPEG and host it there instead.
        if image_url:
            try:
                ir = requests.get(image_url, auth=wp_auth, timeout=60)
                ir.raise_for_status()
                if wp_post.is_webp_bytes(ir.content):
                    jpeg, fname, _ = wp_post.ensure_mailchimp_safe_image(
                        ir.content, f"insider-{args.post_id}.jpg",
                    )
                    image_url = mc.upload_image(fname, jpeg)
                    log("  Converted WebP featured image to JPEG and uploaded "
                        "to Mailchimp.")
            except requests.RequestException as exc:
                log(f"  WARNING: featured-image WebP check failed: {exc}; "
                    "using the WordPress URL.")

    # ----------- render -----------
    log("\nBuilding Insider single-article HTML...")
    html = render_html(
        post=post,
        body_html=body_html,
        image_url=image_url,
        link_mode=args.link_full_article,
        message_html=message_html,
        read_more_label=args.read_more_label,
        read_more_blurb=args.read_more_blurb,
        wp_site=wp_site, wp_auth=wp_auth,
    )

    if args.dump_html:
        print(html)
        return

    mode = "teaser + read-more link" if args.link_full_article else "full article"

    # --write-html: stop here so the file can be edited before it ships.
    if args.write_html:
        campaign_html.write(
            args.write_html, html,
            script=SCRIPT_NAME,
            campaign={
                "subject": subject,
                "preview": preview,
                "segment_id": args.segment_id,
                "folder": folder,
                "mode": mode,
            },
            log=log,
        )
        return

    log("\nCreating Mailchimp campaign...")
    campaign_id, web_id = campaign_html.create_draft(
        mc, list_name=LIST_NAME, subject=subject, preview=preview,
        segment_id=args.segment_id, folder=folder, html=html, log=log,
    )

    print(
        f"\nInsider single-article draft created!\n"
        f"  Mode: {mode}\n"
        f"  Subject: {subject}\n"
        f"  Preview: {preview}\n"
        f"  Segment: {args.segment_id}\n"
        f"  Edit: https://{mc.dc}.admin.mailchimp.com/campaigns/edit"
        f"?id={web_id}",
    )


if __name__ == "__main__":
    main()
