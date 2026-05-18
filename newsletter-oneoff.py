"""Create a Mailchimp newsletter draft from a markdown file.

The markdown file is the entire newsletter spec. A YAML frontmatter
block at the top provides the subject line, preview text, and other
metadata; the body is rendered to HTML and dropped into the standard
UJ newsletter chrome.

Usage:
    python newsletter-oneoff.py path/to/draft.md
    python newsletter-oneoff.py path/to/draft.md --dump-html
    python newsletter-oneoff.py path/to/draft.md --subject "Override"

Frontmatter fields (all optional unless noted):

    ---
    subject:    "Email subject line"        # required
    preview:    "Inbox preview text"        # required
    title:      "Internal Mailchimp title"  # defaults to subject
    signoff:    true                        # adds the "Jay Allen / UJ" block
    from_name:  "Jay at Unseen Japan"
    reply_to:   "jay@unseenjapan.com"
    audience:   "Unseen Japan"
    segment_id: 12345
    ---

    # Body in markdown...
"""

import argparse
import os
import sys
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader
import markdown as md

LIST_NAME = "Unseen Japan"
TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "newsletter-oneoff"

DEFAULT_FROM_NAME = "Jay at Unseen Japan"
DEFAULT_REPLY_TO = "jay@unseenjapan.com"

MARKDOWN_EXTENSIONS = ["extra", "sane_lists"]

# Inline styles applied to markdown-rendered elements so they survive
# Gmail's stylesheet stripping. Keep these in sync with the <style>
# block in newsletter-oneoff.html.j2.
INLINE_STYLES = {
    "h1": "font-size:32px; font-weight:bold; color:#b3421d; margin:24px 0 12px 0; line-height:1.3;",
    "h2": "font-size:24px; font-weight:bold; color:#b3421d; margin:24px 0 10px 0; line-height:1.3;",
    "h3": "font-size:20px; font-weight:bold; color:#222222; margin:20px 0 8px 0; line-height:1.3;",
    "p":  "font-size:18px; line-height:1.5; color:#222222; margin:0 0 16px 0;",
    "a":  "color:#b3421d; text-decoration:underline;",
    "blockquote": (
        "margin:16px 0; padding:12px 16px; background-color:#fff8dc;"
        " border-left:4px solid #b3421d; font-style:italic;"
    ),
    "ul": "margin:0 0 16px 0; padding-left:24px; font-size:18px; line-height:1.5; color:#222222;",
    "ol": "margin:0 0 16px 0; padding-left:24px; font-size:18px; line-height:1.5; color:#222222;",
    "li": "margin-bottom:6px;",
    "hr": "border:0; border-top:1px solid #e0e0e0; margin:24px 0;",
    "img": "width:100%; max-width:612px; height:auto; border-radius:4px; margin:16px 0; display:block;",
    "code": (
        'font-family:Menlo, Consolas, "Courier New", monospace; font-size:16px;'
        " background:#f0f0f0; padding:1px 4px; border-radius:3px;"
    ),
    "pre": (
        "background:#f0f0f0; padding:12px; border-radius:4px;"
        " overflow-x:auto; font-size:14px; line-height:1.4;"
    ),
}


# ---------------------------------------------------------------------------
# Markdown loading
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a `---`-delimited YAML frontmatter block off the top of text.

    Returns (metadata_dict, body_string). If no frontmatter is present,
    returns ({}, original_text).
    """
    if not text.startswith("---"):
        return {}, text
    head, _, rest = text.partition("\n")
    if head.strip() != "---":
        return {}, text
    end_idx = rest.find("\n---")
    if end_idx == -1:
        return {}, text
    yaml_block = rest[:end_idx]
    body = rest[end_idx + len("\n---"):].lstrip("\n")
    try:
        meta = yaml.safe_load(yaml_block) or {}
    except yaml.YAMLError as e:
        print(f"ERROR: invalid YAML frontmatter: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(meta, dict):
        print(
            "ERROR: YAML frontmatter must be a mapping (key: value pairs).",
            file=sys.stderr,
        )
        sys.exit(1)
    return meta, body


def render_markdown(body: str) -> str:
    """Convert markdown to HTML and inline email-safe styles."""
    raw_html = md.markdown(body, extensions=MARKDOWN_EXTENSIONS)
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag_name, style in INLINE_STYLES.items():
        for el in soup.find_all(tag_name):
            existing = el.get("style", "")
            el["style"] = f"{style} {existing}".strip()
    return str(soup)


# ---------------------------------------------------------------------------
# Mailchimp API
# ---------------------------------------------------------------------------

class MailchimpAPI:
    def __init__(self, api_key: str):
        self.dc = api_key.rsplit("-", 1)[-1]
        self.base_url = f"https://{self.dc}.api.mailchimp.com/3.0"
        self.auth = ("apikey", api_key)

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = requests.get(
            f"{self.base_url}{path}", params=params, auth=self.auth, timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json_body: dict) -> dict:
        resp = requests.post(
            f"{self.base_url}{path}", json=json_body, auth=self.auth, timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, json_body: dict) -> dict:
        resp = requests.put(
            f"{self.base_url}{path}", json=json_body, auth=self.auth, timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def find_list(self, name: str) -> dict | None:
        data = self._get("/lists", {"count": 100})
        for lst in data.get("lists", []):
            if lst["name"] == name:
                return lst
        return None

    def create_campaign(
        self, list_id: str, title: str, subject: str, preview_text: str,
        from_name: str, reply_to: str, segment_id: int | None = None,
    ) -> dict:
        recipients: dict = {"list_id": list_id}
        if segment_id is not None:
            recipients["segment_opts"] = {
                "saved_segment_id": segment_id, "match": "all",
            }
        return self._post("/campaigns", {
            "type": "regular",
            "recipients": recipients,
            "settings": {
                "subject_line": subject,
                "preview_text": preview_text,
                "title": title,
                "from_name": from_name,
                "reply_to": reply_to,
            },
        })

    def set_campaign_content(self, campaign_id: str, html: str) -> dict:
        return self._put(f"/campaigns/{campaign_id}/content", {"html": html})


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def build_html(body_html: str, signoff: bool) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template(f"{TEMPLATE_NAME}.html.j2")
    return template.render(body_html=body_html, signoff=signoff)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create a Mailchimp newsletter draft from a markdown file with "
            "YAML frontmatter."
        ),
    )
    parser.add_argument(
        "markdown_file", type=Path,
        help="Path to the markdown source file.",
    )
    parser.add_argument("--subject", help="Override the subject line.")
    parser.add_argument("--preview", help="Override the preview text.")
    parser.add_argument(
        "--no-signoff", action="store_true",
        help="Drop the 'Jay Allen / UJ' sign-off block.",
    )
    parser.add_argument(
        "--audience", default=None,
        help=f"Mailchimp audience name (default: {LIST_NAME}).",
    )
    parser.add_argument(
        "--segment-id", type=int, default=None,
        help="Mailchimp saved segment ID to target (optional).",
    )
    parser.add_argument(
        "--dump-html", action="store_true",
        help="Render and print the HTML; do not contact Mailchimp.",
    )
    args = parser.parse_args()

    if not args.markdown_file.exists():
        print(f"ERROR: file not found: {args.markdown_file}", file=sys.stderr)
        sys.exit(1)

    raw = args.markdown_file.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(raw)

    subject = args.subject or meta.get("subject")
    preview = args.preview or meta.get("preview")
    if not args.dump_html and (not subject or not preview):
        print(
            "ERROR: subject and preview are required (in frontmatter or via "
            "--subject/--preview).",
            file=sys.stderr,
        )
        sys.exit(1)

    title = meta.get("title") or subject or args.markdown_file.stem
    from_name = meta.get("from_name", DEFAULT_FROM_NAME)
    reply_to = meta.get("reply_to", DEFAULT_REPLY_TO)
    audience_name = args.audience or meta.get("audience") or LIST_NAME
    segment_id = args.segment_id
    if segment_id is None and meta.get("segment_id") is not None:
        segment_id = int(meta["segment_id"])
    signoff = meta.get("signoff", True)
    if args.no_signoff:
        signoff = False

    body_html = render_markdown(body)
    newsletter_html = build_html(body_html, signoff=signoff)

    if args.dump_html:
        print(newsletter_html)
        return

    mc_api_key = os.environ.get("MAILCHIMP_API_KEY")
    if not mc_api_key:
        print(
            "ERROR: MAILCHIMP_API_KEY environment variable not set.",
            file=sys.stderr,
        )
        sys.exit(1)
    mc = MailchimpAPI(mc_api_key)

    audience = mc.find_list(audience_name)
    if not audience:
        print(f"ERROR: list '{audience_name}' not found.", file=sys.stderr)
        sys.exit(1)
    print(
        f"  List: {audience['name']} (ID: {audience['id']})", file=sys.stderr,
    )

    print("\nCreating Mailchimp campaign...", file=sys.stderr)
    campaign = mc.create_campaign(
        list_id=audience["id"],
        title=title,
        subject=subject,
        preview_text=preview,
        from_name=from_name,
        reply_to=reply_to,
        segment_id=segment_id,
    )
    campaign_id = campaign["id"]
    web_id = campaign.get("web_id", "")
    print(f"  Campaign ID: {campaign_id}", file=sys.stderr)

    mc.set_campaign_content(campaign_id, newsletter_html)
    print("  Content set.", file=sys.stderr)

    segment_note = ""
    if segment_id is None:
        segment_note = (
            "\n  NOTE: No segment specified. The campaign targets the "
            "full list.\n        Set the audience segment in Mailchimp "
            "before sending."
        )

    print(
        f"\nDraft campaign created successfully!\n"
        f"  Title: {title}\n"
        f"  Subject: {subject}\n"
        f"  Edit: https://{mc.dc}.admin.mailchimp.com/campaigns/edit"
        f"?id={web_id}"
        f"{segment_note}",
    )


if __name__ == "__main__":
    main()
