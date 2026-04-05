"""Create a Mailchimp newsletter draft for a single WordPress post.

Sends to all subscribers using the "Insider Test" campaign as a
formatting reference.

Usage:
    python uj-newsletter-single-post.py --post-id 88516 --title "Title" --preview "Preview"
    python uj-newsletter-single-post.py --dump-template
"""

import argparse
import html as html_mod
import os
import sys

import keyring
import requests
from bs4 import BeautifulSoup, Tag

WP_SITE = "https://unseen-japan.com"
CREDENTIAL_TARGET = "https://unseen-japan.com"
LIST_NAME = "Unseen Japan"

# Reference campaign ID — the "Insider Test" campaign whose HTML structure
# serves as the base template for single-post newsletters.
REFERENCE_CAMPAIGN_ID = "56c7f05f48"


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
    """Fetch title, link, and full rendered content for a post."""
    url = f"{WP_SITE}/wp-json/wp/v2/posts/{post_id}"
    resp = requests.get(
        url,
        params={"_fields": "title,link,content"},
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "title": html_mod.unescape(data["title"]["rendered"]),
        "url": data["link"],
        "content_html": data["content"]["rendered"],
    }


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

    def get_campaign_content_html(self, campaign_id: str) -> str:
        data = self._get(f"/campaigns/{campaign_id}/content")
        return data.get("html", "")

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
# Template HTML manipulation
# ---------------------------------------------------------------------------

def _find_main_tbody(soup: BeautifulSoup) -> Tag | None:
    """Find the main tbody containing all newsletter rows.

    Identified as the tbody with 5+ <tr> children.
    """
    for tbody in soup.find_all("tbody"):
        trs = [c for c in tbody.children if isinstance(c, Tag) and c.name == "tr"]
        if len(trs) >= 5:
            return tbody
    return None


def build_newsletter_html(base_html: str, post: dict) -> str:
    """Replace the title and article body in the reference campaign HTML.

    The reference structure is:
      tr[0]: "View this email in your browser"
      tr[1]: Intro greeting text
      tr[2]: Article title (h1 inside mceText div)
      tr[3]: Article body (mceText div with full post content)
      tr[4]: Divider
      tr[5]: Sign-off
      tr[6]: Footer
    """
    soup = BeautifulSoup(base_html, "html.parser")

    main_tbody = _find_main_tbody(soup)
    if main_tbody is None:
        print("ERROR: Could not find the main content tbody.", file=sys.stderr)
        sys.exit(1)

    trs = [c for c in main_tbody.children if isinstance(c, Tag) and c.name == "tr"]

    if len(trs) < 5:
        print(
            f"ERROR: Expected at least 5 rows, found {len(trs)}.",
            file=sys.stderr,
        )
        sys.exit(1)

    # -- Replace title in tr[2] --
    title_row = trs[2]
    for h1 in title_row.find_all("h1"):
        h1.string = post["title"]
        break

    # -- Replace article body in tr[3] --
    body_row = trs[3]
    body_div = body_row.find("div", class_="mceText")
    if body_div is None:
        print("ERROR: Could not find body mceText div.", file=sys.stderr)
        sys.exit(1)

    # Parse the WordPress content HTML and inject it
    content_soup = BeautifulSoup(post["content_html"], "html.parser")

    # Build a new mceText div with the article content
    new_body = BeautifulSoup(
        '<div class="mceText"></div>', "html.parser"
    ).find("div")

    # Preserve all original attributes (id, data-block-id, style, etc.)
    for attr, val in body_div.attrs.items():
        new_body[attr] = val

    # Append each element from the WordPress content
    for el in content_soup.children:
        new_body.append(el.__copy__() if hasattr(el, "__copy__") else el)

    body_div.replace_with(new_body)

    return str(soup)


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
        "--dump-template", action="store_true",
        help="Fetch and print the reference campaign HTML, then exit.",
    )
    args = parser.parse_args()

    if not args.dump_template and (
        not args.post_id or not args.title or not args.preview
    ):
        parser.error(
            "--post-id, --title, and --preview are required "
            "(unless using --dump-template)"
        )

    # Init Mailchimp
    mc_api_key = os.environ.get("MAILCHIMP_API_KEY")
    if not mc_api_key:
        print(
            "ERROR: MAILCHIMP_API_KEY environment variable not set.",
            file=sys.stderr,
        )
        sys.exit(1)
    mc = MailchimpAPI(mc_api_key)

    # --dump-template mode
    if args.dump_template:
        html = mc.get_campaign_content_html(REFERENCE_CAMPAIGN_ID)
        if not html:
            print(
                "ERROR: No HTML content in reference campaign.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(html)
        return

    # -----------------------------------------------------------------------
    # Fetch WordPress post
    # -----------------------------------------------------------------------
    print("Fetching WordPress credentials...", file=sys.stderr)
    wp_auth = get_wp_credentials()

    print(f"  Fetching post {args.post_id}...", file=sys.stderr)
    post = fetch_post(args.post_id, wp_auth)
    print(f"  Title: {post['title']}", file=sys.stderr)

    # -----------------------------------------------------------------------
    # Get reference HTML and find audience
    # -----------------------------------------------------------------------
    print("\nFetching reference campaign HTML...", file=sys.stderr)
    base_html = mc.get_campaign_content_html(REFERENCE_CAMPAIGN_ID)
    if not base_html:
        print(
            "ERROR: Could not fetch reference campaign HTML.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"  Reference HTML: {len(base_html)} chars", file=sys.stderr)

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
    newsletter_html = build_newsletter_html(base_html, post)

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
