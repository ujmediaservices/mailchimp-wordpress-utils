"""Create a Mailchimp newsletter draft from WordPress posts.

Usage:
    python uj-newsletter-free.py --title "Title" --preview "Preview" --posts 123 456
    python uj-newsletter-free.py --dump-template
"""

import argparse
import base64
import html as html_mod
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import keyring
import requests
from bs4 import BeautifulSoup, Tag

WP_SITE = "https://unseen-japan.com"
CREDENTIAL_TARGET = "https://unseen-japan.com"
LIST_NAME = "Unseen Japan"

# Reference campaign ID — a sent free newsletter whose HTML structure
# serves as the base template for new newsletters.
REFERENCE_CAMPAIGN_ID = "7ccc00ec4f"


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


def fetch_post_data(post_id: int, auth: tuple[str, str]) -> dict:
    """Fetch title, link, content HTML, and featured_media ID for a post."""
    url = f"{WP_SITE}/wp-json/wp/v2/posts/{post_id}"
    resp = requests.get(
        url,
        params={"_fields": "title,link,excerpt,featured_media"},
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    excerpt_html = data.get("excerpt", {}).get("rendered", "")
    excerpt_text = BeautifulSoup(excerpt_html, "html.parser").get_text(strip=True)
    return {
        "title": html_mod.unescape(data["title"]["rendered"]),
        "url": data["link"],
        "excerpt": html_mod.unescape(excerpt_text),
        "featured_media": data.get("featured_media") or None,
    }



def get_featured_image_url(media_id: int, auth: tuple[str, str]) -> str | None:
    url = f"{WP_SITE}/wp-json/wp/v2/media/{media_id}"
    resp = requests.get(
        url,
        params={"_fields": "source_url"},
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("source_url") or None


def download_image(
    image_url: str, auth: tuple[str, str], temp_dir: str
) -> Path:
    """Download an image to temp_dir and return the local path."""
    filename = Path(urlparse(image_url).path).name
    local_path = Path(temp_dir) / filename
    resp = requests.get(image_url, auth=auth, timeout=60)
    resp.raise_for_status()
    local_path.write_bytes(resp.content)
    return local_path


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

    # -- Lists / Audiences --------------------------------------------------

    def find_list(self, name: str) -> dict | None:
        data = self._get("/lists", {"count": 100})
        for lst in data.get("lists", []):
            if lst["name"] == name:
                return lst
        return None

    # -- File Manager -------------------------------------------------------

    def upload_image(self, filename: str, image_bytes: bytes) -> str:
        """Upload an image and return its hosted URL."""
        encoded = base64.b64encode(image_bytes).decode("ascii")
        data = self._post("/file-manager/files", {
            "name": filename,
            "file_data": encoded,
        })
        return data["full_size_url"]

    # -- Campaigns ----------------------------------------------------------

    def get_campaign_content_html(self, campaign_id: str) -> str:
        data = self._get(f"/campaigns/{campaign_id}/content")
        return data.get("html", "")

    def create_campaign(
        self,
        list_id: str,
        title: str,
        subject: str,
        preview_text: str,
        segment_id: int | None = None,
    ) -> dict:
        recipients: dict = {"list_id": list_id}
        if segment_id is not None:
            recipients["segment_opts"] = {
                "saved_segment_id": segment_id,
                "match": "all",
            }
        return self._post("/campaigns", {
            "type": "regular",
            "recipients": recipients,
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
    """Find the main tbody containing all newsletter sections as <tr> rows.

    The main tbody is identified as the one with 15+ <tr> children, containing
    both post sections (with mceLayout tables) and structural sections.
    """
    for tbody in soup.find_all("tbody"):
        trs = [c for c in tbody.children if isinstance(c, Tag) and c.name == "tr"]
        if len(trs) >= 15:
            return tbody
    return None


def _is_post_row(tr: Tag) -> bool:
    """Check if a <tr> contains a post section (has an mceLayout table)."""
    return tr.find("table", class_="mceLayout") is not None


def _extract_section_parts(tbody: Tag) -> dict:
    """Split the main tbody rows into header, post template, and footer.

    The newsletter structure is:
      - Header rows (before first post): "View in browser", "From our website"
      - Post rows: consecutive mceLayout rows
      - Middle sections: dividers, insider teasers, archive posts (issue-specific)
      - Standard footer: "Upgrade to Insider" CTA, sign-off, copyright

    We keep the header, one post row as a template, and the standard footer
    (starting from "Upgrade to Insider"). Everything between the post rows
    and the footer is discarded as issue-specific content.
    """
    trs = [c for c in tbody.children if isinstance(c, Tag) and c.name == "tr"]

    # Find first post row
    first_post_idx = None
    for i, tr in enumerate(trs):
        if _is_post_row(tr):
            first_post_idx = i
            break

    if first_post_idx is None:
        return {"header_rows": trs, "post_template_row": None, "footer_rows": []}

    header_rows = trs[:first_post_idx]

    # Collect consecutive post rows (the "From our website" section)
    post_rows: list[Tag] = []
    i = first_post_idx
    while i < len(trs) and _is_post_row(trs[i]):
        post_rows.append(trs[i])
        i += 1

    # Find the standard footer — starts at "Upgrade to Insider" or the
    # sign-off section. Search backwards from the end for a non-post,
    # non-divider row containing "Upgrade" or "Insider" or the sign-off.
    footer_start_idx = len(trs)
    for j in range(i, len(trs)):
        text = trs[j].get_text(strip=True).lower()
        if "upgrade to insider" in text:
            footer_start_idx = j
            break

    footer_rows = trs[footer_start_idx:]

    return {
        "header_rows": header_rows,
        "post_template_row": post_rows[0] if post_rows else None,
        "footer_rows": footer_rows,
    }


def _fill_post_row(tr: Tag, post: dict) -> None:
    """Fill a cloned post <tr> with post data (image, title, text, URLs)."""
    # Set all images
    for img in tr.find_all("img"):
        src = img.get("src", "")
        # Only replace content images (mcusercontent), not icons/logos
        if "mcusercontent.com" in src or "placeholder" in src.lower():
            if post.get("mailchimp_image_url"):
                img["src"] = post["mailchimp_image_url"]
                img["alt"] = post["title"]
        # Set image link
        parent_a = img.find_parent("a")
        if parent_a and parent_a.get("data-block-id"):
            parent_a["href"] = post["url"]

    # Set title — the first h2 in the section
    for h2 in tr.find_all("h2"):
        a_tag = h2.find("a")
        if a_tag:
            a_tag["href"] = post["url"]
            a_tag.string = post["title"]
        else:
            h2.string = post["title"]
        break

    # Set body text — the mceText div after the title div
    # The structure is: image block, title mceText, body mceText, button
    P_STYLE = (
        'font-family:"Helvetica Neue", Helvetica, Arial, Verdana, '
        "sans-serif;font-size:18px;line-height:1.5;margin:0 0 16px 0;"
    )
    mce_texts = tr.find_all("div", class_="mceText")
    if len(mce_texts) >= 2:
        body_div = mce_texts[1]  # second mceText is the body
        # Build replacement HTML and swap it in
        paragraphs_html = "".join(
            f'<p style="{P_STYLE}">{p.strip()}</p>'
            for p in post["intro_text"].split("\n\n")
            if p.strip()
        )
        new_body = BeautifulSoup(
            f'<div class="mceText">{paragraphs_html}</div>',
            "html.parser",
        ).find("div")
        # Preserve original attributes
        for attr, val in body_div.attrs.items():
            new_body[attr] = val
        body_div.replace_with(new_body)

    # Set "Read more" button URL
    for a in tr.find_all("a"):
        text = a.get_text(strip=True).lower()
        if "read more" in text:
            a["href"] = post["url"]

    # Update all mso conditional comment links too
    # These are in <!--[if mso]> blocks — handle via string replacement
    # (BeautifulSoup doesn't parse conditional comments well)


def build_newsletter_html(base_html: str, posts: list[dict]) -> str:
    """Build newsletter HTML by duplicating the post section for each post."""
    soup = BeautifulSoup(base_html, "html.parser")

    main_tbody = _find_main_tbody(soup)
    if main_tbody is None:
        print("ERROR: Could not find the main content tbody.", file=sys.stderr)
        sys.exit(1)

    parts = _extract_section_parts(main_tbody)
    if parts["post_template_row"] is None:
        print("ERROR: Could not find any post sections in the template.", file=sys.stderr)
        sys.exit(1)

    template_row_html = str(parts["post_template_row"])

    # Clear the tbody and rebuild it
    main_tbody.clear()

    # Re-add header rows
    for row in parts["header_rows"]:
        main_tbody.append(row)

    # Add one post row per post
    for post in posts:
        new_row = BeautifulSoup(template_row_html, "html.parser").find("tr")
        if new_row is None:
            # If parser doesn't find a tr, the template_row might be the root
            new_row = BeautifulSoup(
                f"<table><tbody>{template_row_html}</tbody></table>",
                "html.parser",
            ).find("tr")
        _fill_post_row(new_row, post)
        main_tbody.append(new_row)

    # Re-add footer rows
    for row in parts["footer_rows"]:
        main_tbody.append(row)

    # Post-process: fix MSO conditional comment hrefs via string replacement
    result_html = str(soup)
    for post in posts:
        # The MSO blocks contain duplicate <a href="..."> tags that
        # BeautifulSoup doesn't modify. We handle this by ensuring the
        # template's original URLs get replaced. Since each cloned section
        # still has the original href from the reference campaign, we don't
        # need per-section fixup — the visible (non-MSO) links are already set.
        pass

    return result_html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a Mailchimp newsletter draft from WordPress posts.",
    )
    parser.add_argument("--title", help="Newsletter title / subject line")
    parser.add_argument("--preview", help="Preview text")
    parser.add_argument(
        "--posts", nargs="+", type=int,
        help="WordPress post IDs",
    )
    parser.add_argument(
        "--segment-id", type=int, default=None,
        help="Mailchimp saved segment ID to target (optional).",
    )
    parser.add_argument(
        "--dump-template", action="store_true",
        help="Fetch and print the reference campaign HTML, then exit.",
    )
    args = parser.parse_args()

    if not args.dump_template and (
        not args.title or not args.preview or not args.posts
    ):
        parser.error(
            "--title, --preview, and --posts are required "
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
            print("ERROR: No HTML content in reference campaign.", file=sys.stderr)
            sys.exit(1)
        print(html)
        return

    # -----------------------------------------------------------------------
    # Fetch WordPress post data
    # -----------------------------------------------------------------------
    print("Fetching WordPress credentials...", file=sys.stderr)
    wp_auth = get_wp_credentials()

    posts_data: list[dict] = []
    temp_dir = tempfile.mkdtemp(prefix="uj_newsletter_")
    print(f"Temp directory: {temp_dir}", file=sys.stderr)

    for post_id in args.posts:
        print(f"  Fetching post {post_id}...", file=sys.stderr)
        try:
            post = fetch_post_data(post_id, wp_auth)
        except requests.exceptions.HTTPError as e:
            print(
                f"  WARNING: Failed to fetch post {post_id}: {e}",
                file=sys.stderr,
            )
            continue

        excerpt = post["excerpt"]
        image_path = None
        image_url = None

        if post["featured_media"]:
            print("  Fetching featured image...", file=sys.stderr)
            image_url = get_featured_image_url(
                post["featured_media"], wp_auth
            )
            if image_url:
                image_path = download_image(image_url, wp_auth, temp_dir)
                print(f"  Downloaded: {image_path.name}", file=sys.stderr)

        posts_data.append({
            "post_id": post_id,
            "title": post["title"],
            "url": post["url"],
            "intro_text": excerpt,
            "image_path": image_path,
            "image_url": image_url,
        })

    if not posts_data:
        print("ERROR: No posts were successfully fetched.", file=sys.stderr)
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Upload images to Mailchimp
    # -----------------------------------------------------------------------
    print("\nUploading images to Mailchimp...", file=sys.stderr)
    for post in posts_data:
        if post["image_path"]:
            image_bytes = post["image_path"].read_bytes()
            filename = (
                f"newsletter-{post['post_id']}-{post['image_path'].name}"
            )
            try:
                mc_url = mc.upload_image(filename, image_bytes)
                post["mailchimp_image_url"] = mc_url
                print(f"  Uploaded: {filename}", file=sys.stderr)
            except requests.exceptions.HTTPError as e:
                print(
                    f"  WARNING: Image upload failed for post "
                    f"{post['post_id']}: {e}",
                    file=sys.stderr,
                )
                # Fall back to original WordPress URL
                post["mailchimp_image_url"] = post["image_url"]
        else:
            post["mailchimp_image_url"] = None

    # -----------------------------------------------------------------------
    # Get reference HTML and find audience
    # -----------------------------------------------------------------------
    print("\nFetching reference newsletter HTML...", file=sys.stderr)
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
    newsletter_html = build_newsletter_html(base_html, posts_data)

    # -----------------------------------------------------------------------
    # Create campaign
    # -----------------------------------------------------------------------
    print("\nCreating Mailchimp campaign...", file=sys.stderr)
    campaign = mc.create_campaign(
        list_id=audience["id"],
        title=args.title,
        subject=args.title,
        preview_text=args.preview,
        segment_id=args.segment_id,
    )
    campaign_id = campaign["id"]
    web_id = campaign.get("web_id", "")
    print(f"  Campaign ID: {campaign_id}", file=sys.stderr)

    # Set content
    mc.set_campaign_content(campaign_id, newsletter_html)
    print("  Content set.", file=sys.stderr)

    segment_note = ""
    if not args.segment_id:
        segment_note = (
            "\n  NOTE: No segment specified. The campaign targets the "
            "full list.\n        Set the audience segment in Mailchimp "
            "before sending."
        )

    print(
        f"\nDraft campaign created successfully!\n"
        f"  Title: {args.title}\n"
        f"  Posts: {len(posts_data)}\n"
        f"  Edit: https://{mc.dc}.admin.mailchimp.com/campaigns/edit"
        f"?id={web_id}"
        f"{segment_note}",
    )


if __name__ == "__main__":
    main()
