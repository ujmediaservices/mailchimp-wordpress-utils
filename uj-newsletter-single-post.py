"""Create a Mailchimp newsletter draft for a single WordPress post.

Sends to all subscribers using a Jinja2 template for formatting.

Usage:
    python uj-newsletter-single-post.py --post-id 88516 --title "Title" --preview "Preview"
    python uj-newsletter-single-post.py --dump-html
"""

import argparse
import html as html_mod
import os
import re
import sys
from pathlib import Path

import keyring
import requests
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

WP_SITE = "https://unseen-japan.com"
CREDENTIAL_TARGET = "https://unseen-japan.com"
LIST_NAME = "Unseen Japan"
TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "single_post.html.j2"


# ---------------------------------------------------------------------------
# WordPress helpers
# ---------------------------------------------------------------------------

def get_wp_credentials() -> tuple[str, str]:
    cred = keyring.get_credential(CREDENTIAL_TARGET, None)
    if cred is None:
        print(
            f"No credential found in Windows Credential Manager "
            f"for '{CREDENTIAL_TARGET}'.",
            file=sys.stderr,
        )
        sys.exit(1)
    return cred.username, cred.password


def fetch_post(post_id: int, auth: tuple[str, str]) -> dict:
    """Fetch title, link, and full rendered content for a post.

    First tries ``context=edit`` to get the raw Gutenberg content (which
    bypasses any membership paywall).  Falls back to ``context=view`` if
    the credentials lack edit access.
    """
    url = f"{WP_SITE}/wp-json/wp/v2/posts/{post_id}"

    # Try edit context first (bypasses membership plugin truncation)
    resp = requests.get(
        url,
        params={"context": "edit"},
        auth=auth,
        timeout=30,
    )
    if resp.status_code == 200:
        data = resp.json()
        raw = data.get("content", {}).get("raw", "")
        if raw:
            content_html = _raw_blocks_to_html(raw)
            return {
                "title": html_mod.unescape(data["title"]["rendered"]),
                "url": data["link"],
                "content_html": content_html,
                "featured_media": data.get("featured_media") or None,
            }

    # Fall back to view context
    resp = requests.get(
        url,
        params={"_fields": "title,link,content,featured_media"},
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    content_html = _clean_rendered_html(data["content"]["rendered"])
    return {
        "title": html_mod.unescape(data["title"]["rendered"]),
        "url": data["link"],
        "content_html": content_html,
        "featured_media": data.get("featured_media") or None,
    }


def get_featured_image_url(
    media_id: int, auth: tuple[str, str]
) -> str | None:
    """Get the source URL of a post's featured image."""
    url = f"{WP_SITE}/wp-json/wp/v2/media/{media_id}"
    resp = requests.get(
        url,
        params={"_fields": "source_url"},
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("source_url") or None


def _raw_blocks_to_html(raw: str) -> str:
    """Convert Gutenberg raw block content to clean HTML.

    Strips ``<!-- wp:… -->`` comment markers, removes membership
    shortcodes (``[swpm_protected …]`` / ``[/swpm_protected]``) and
    other shortcodes, and returns the remaining HTML.
    """
    # Remove Gutenberg block comments
    html = re.sub(r"<!--\s*/?wp:\S*?(?:\s+\{.*?\})?\s*-->", "", raw)
    # Remove membership protection shortcodes (keep the content inside)
    html = re.sub(r"\[swpm_protected[^\]]*\]", "", html)
    html = re.sub(r"\[/swpm_protected\]", "", html)
    # Remove other shortcodes like [elementor-template id="…"]
    html = re.sub(r"\[elementor-template[^\]]*\]", "", html)
    html = re.sub(r"\[[a-zA-Z_-]+[^\]]*\]", "", html)
    return html.strip()


def _clean_rendered_html(rendered: str) -> str:
    """Remove Elementor shortcode output and other non-article cruft
    from the ``context=view`` rendered content.
    """
    soup = BeautifulSoup(rendered, "html.parser")

    # Remove Elementor template output
    for el in soup.find_all(attrs={"data-elementor-type": True}):
        el.decompose()

    # Remove any remaining forms
    for form in soup.find_all("form"):
        form.decompose()

    return str(soup)


def _add_paragraph_spacing(content_html: str) -> str:
    """Add margin and word-wrap styles to paragraphs and headings."""
    soup = BeautifulSoup(content_html, "html.parser")

    P_STYLE = (
        'font-family:"Helvetica Neue", Helvetica, Arial, sans-serif;'
        "font-size:18px;line-height:1.5;color:#222222;"
        "margin:0 0 16px 0;"
        "word-wrap:break-word;overflow-wrap:break-word;"
    )

    for p in soup.find_all("p"):
        existing = p.get("style", "")
        if existing:
            p["style"] = (
                existing.rstrip(";")
                + ";margin:0 0 16px 0;"
                "word-wrap:break-word;overflow-wrap:break-word;"
            )
        else:
            p["style"] = P_STYLE

    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        existing = h.get("style", "")
        h["style"] = (
            (existing.rstrip(";") + ";" if existing else "")
            + "word-wrap:break-word;overflow-wrap:break-word;"
        )

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
            f"{self.base_url}{path}",
            params=params,
            auth=self.auth,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json_body: dict) -> dict:
        resp = requests.post(
            f"{self.base_url}{path}",
            json=json_body,
            auth=self.auth,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, json_body: dict) -> dict:
        resp = requests.put(
            f"{self.base_url}{path}",
            json=json_body,
            auth=self.auth,
            timeout=30,
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
        self,
        list_id: str,
        title: str,
        subject: str,
        preview_text: str,
    ) -> dict:
        return self._post("/campaigns", {
            "type": "regular",
            "recipients": {"list_id": list_id},
            "settings": {
                "subject_line": subject,
                "preview_text": preview_text,
                "title": title,
                "from_name": "Jay at Unseen Japan",
                "reply_to": "jay@unseenjapan.com",
            },
        })

    def set_campaign_content(self, campaign_id: str, html: str) -> dict:
        return self._put(f"/campaigns/{campaign_id}/content", {"html": html})


# ---------------------------------------------------------------------------
# Newsletter HTML rendering
# ---------------------------------------------------------------------------

def build_newsletter_html(
    title: str,
    post: dict,
    featured_image_url: str | None = None,
) -> str:
    """Render the Jinja2 single-post template."""
    content_html = _add_paragraph_spacing(post["content_html"])

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template(TEMPLATE_NAME)
    return template.render(
        title=title,
        post_title=post["title"],
        content_html=content_html,
        featured_image_url=featured_image_url,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a Mailchimp single-post newsletter draft.",
    )
    parser.add_argument(
        "--post-id", type=int,
        help="WordPress post ID",
    )
    parser.add_argument("--title", help="Newsletter title / subject line")
    parser.add_argument("--preview", help="Preview text")
    parser.add_argument(
        "--dump-html", action="store_true",
        help="Render the template with sample data and print it.",
    )
    args = parser.parse_args()

    if not args.dump_html and (
        not args.post_id or not args.title or not args.preview
    ):
        parser.error(
            "--post-id, --title, and --preview are required "
            "(unless using --dump-html)"
        )

    # --dump-html mode
    if args.dump_html:
        sample_post = {
            "title": "Sample Article Title",
            "url": "https://unseen-japan.com/sample/",
            "content_html": (
                "<p>This is the first paragraph of the article.</p>"
                "<h2>A section heading</h2>"
                "<p>This is another paragraph with more detail.</p>"
            ),
        }
        print(build_newsletter_html(
            title="Sample Newsletter Title",
            post=sample_post,
            featured_image_url="https://via.placeholder.com/612x400",
        ))
        return

    # Init Mailchimp
    mc_api_key = os.environ.get("MAILCHIMP_API_KEY")
    if not mc_api_key:
        print(
            "ERROR: MAILCHIMP_API_KEY environment variable not set.",
            file=sys.stderr,
        )
        sys.exit(1)
    mc = MailchimpAPI(mc_api_key)

    # -----------------------------------------------------------------------
    # Fetch WordPress post
    # -----------------------------------------------------------------------
    print("Fetching WordPress credentials...", file=sys.stderr)
    wp_auth = get_wp_credentials()

    print(f"  Fetching post {args.post_id}...", file=sys.stderr)
    post = fetch_post(args.post_id, wp_auth)
    print(f"  Title: {post['title']}", file=sys.stderr)

    # Fetch featured image URL
    featured_image_url = None
    if post.get("featured_media"):
        print("  Fetching featured image...", file=sys.stderr)
        featured_image_url = get_featured_image_url(
            post["featured_media"], wp_auth
        )
        if featured_image_url:
            print(f"  Image: {featured_image_url}", file=sys.stderr)

    # -----------------------------------------------------------------------
    # Find audience
    # -----------------------------------------------------------------------
    audience = mc.find_list(LIST_NAME)
    if not audience:
        print(f"ERROR: List '{LIST_NAME}' not found.", file=sys.stderr)
        sys.exit(1)
    print(
        f"  List: {audience['name']} (ID: {audience['id']})",
        file=sys.stderr,
    )

    # -----------------------------------------------------------------------
    # Build newsletter HTML
    # -----------------------------------------------------------------------
    print("\nBuilding newsletter HTML...", file=sys.stderr)
    newsletter_html = build_newsletter_html(
        title=args.title,
        post=post,
        featured_image_url=featured_image_url,
    )

    # -----------------------------------------------------------------------
    # Create campaign
    # -----------------------------------------------------------------------
    print("\nCreating Mailchimp campaign...", file=sys.stderr)
    campaign = mc.create_campaign(
        list_id=audience["id"],
        title=args.title,
        subject=args.title,
        preview_text=args.preview,
    )
    campaign_id = campaign["id"]
    web_id = campaign.get("web_id", "")
    print(f"  Campaign ID: {campaign_id}", file=sys.stderr)

    # Set content
    mc.set_campaign_content(campaign_id, newsletter_html)
    print("  Content set.", file=sys.stderr)

    print(
        f"\nDraft campaign created successfully!\n"
        f"  Title: {args.title}\n"
        f"  Post: {post['title']}\n"
        f"  Edit: https://{mc.dc}.admin.mailchimp.com/campaigns/edit"
        f"?id={web_id}",
    )


if __name__ == "__main__":
    main()
