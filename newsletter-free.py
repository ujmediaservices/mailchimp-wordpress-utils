"""Create a Mailchimp newsletter draft from WordPress posts.

Usage:
    python uj-newsletter-free.py --title "Title" --preview "Preview" --posts 123 456
    python uj-newsletter-free.py --dump-html
"""

import argparse
import base64
import html as html_mod
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

import extras as extras_mod

LIST_NAME = "Unseen Japan"
TEMPLATE_DIR = Path(__file__).parent / "templates"
DEFAULT_TEMPLATE = "newsletter-free"

INSIDER_BLURB = (
    '<br /><br />'
    '<a href="https://unseen-japan.com/subscribe">Upgrade to our Insider '
    'newsletter</a> to get access to this and over 50+ articles exclusively '
    'for our supporters! You can also '
    '<a href="https://unseenjapan.substack.com">subscribe to us on Substack</a>.'
)


def is_insider_post(title: str) -> bool:
    return "[insider]" in title.lower()


# ---------------------------------------------------------------------------
# WordPress helpers
# ---------------------------------------------------------------------------

def get_wp_config() -> tuple[str, tuple[str, str]]:
    """Return (site_url, (username, password)) from environment variables."""
    wp_url = os.environ.get("WORDPRESS_URL")
    username = os.environ.get("WORDPRESS_USERNAME")
    password = os.environ.get("WORDPRESS_PASSWORD")
    if not wp_url or not username or not password:
        print(
            "ERROR: WORDPRESS_URL, WORDPRESS_USERNAME, and "
            "WORDPRESS_PASSWORD environment variables must be set.",
            file=sys.stderr,
        )
        sys.exit(1)
    return wp_url.rstrip("/"), (username, password)


def extract_intro(content_html: str) -> str:
    """Return the plain text of a post's content up to the first <h2>.

    Skips table-of-contents blocks, embedded video/iframe containers,
    and other non-paragraph boilerplate.
    """
    soup = BeautifulSoup(content_html, "html.parser")

    # Remove TOC containers and embedded media before traversal
    for unwanted in soup.select(
        "#ez-toc-container, .ez-toc-container, "
        "figure, iframe, blockquote.wp-embedded-content, "
        "[data-elementor-type]"
    ):
        unwanted.decompose()

    parts: list[str] = []
    for element in soup.children:
        if getattr(element, "name", None) == "h2":
            break
        # Only keep <p> tags to avoid picking up stray divs/scripts
        if getattr(element, "name", None) == "p":
            text = element.get_text(separator=" ", strip=True)
            # Collapse runs of whitespace and fix spaces before punctuation
            text = re.sub(r"\s+", " ", text)
            text = re.sub(r"\s+([.,;:!?])", r"\1", text)
            if text:
                parts.append(text)
    return "<br /><br />".join(parts)


def fetch_post_data(
    wp_site: str, post_id: int, auth: tuple[str, str],
    *, use_intro: bool = False,
) -> dict:
    """Fetch title, link, excerpt, and featured_media ID for a post."""
    fields = "title,link,excerpt,featured_media"
    if use_intro:
        fields += ",content"
    url = f"{wp_site}/wp-json/wp/v2/posts/{post_id}"
    resp = requests.get(
        url,
        params={"_fields": fields},
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if use_intro:
        content_html = data.get("content", {}).get("rendered", "")
        excerpt_text = extract_intro(content_html)
    else:
        excerpt_html = data.get("excerpt", {}).get("rendered", "")
        excerpt_text = BeautifulSoup(excerpt_html, "html.parser").get_text(
            strip=True
        )

    return {
        "title": html_mod.unescape(data["title"]["rendered"]),
        "url": data["link"],
        "excerpt": html_mod.unescape(excerpt_text),
        "featured_media": data.get("featured_media") or None,
    }


def get_featured_image_url(
    wp_site: str, media_id: int, auth: tuple[str, str]
) -> str | None:
    url = f"{wp_site}/wp-json/wp/v2/media/{media_id}"
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

    def find_list(self, name: str) -> dict | None:
        data = self._get("/lists", {"count": 100})
        for lst in data.get("lists", []):
            if lst["name"] == name:
                return lst
        return None

    def upload_image(self, filename: str, image_bytes: bytes) -> str:
        """Upload an image and return its hosted URL."""
        encoded = base64.b64encode(image_bytes).decode("ascii")
        data = self._post("/file-manager/files", {
            "name": filename,
            "file_data": encoded,
        })
        return data["full_size_url"]

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
# Newsletter HTML rendering
# ---------------------------------------------------------------------------

def build_newsletter_html(
    posts: list[dict], template_name: str, extras: list[dict] | None = None
) -> str:
    """Render the Jinja2 newsletter template with the given posts."""
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template(f"{template_name}.html.j2")
    return template.render(posts=posts, extras=extras or [])


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
        "--template", default=DEFAULT_TEMPLATE,
        help=(
            "Template name (without .html.j2 extension) from the templates/ "
            "directory. Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--intro", action="store_true",
        help=(
            "Use the post body text up to the first H2 as the excerpt "
            "instead of the WordPress excerpt field."
        ),
    )
    parser.add_argument(
        "--dump-html", action="store_true",
        help="Render the template with sample data and print it.",
    )
    extras_mod.add_extras_cli_args(parser)
    args = parser.parse_args()

    if not args.dump_html and (
        not args.title or not args.preview or not args.posts
    ):
        parser.error(
            "--title, --preview, and --posts are required "
            "(unless using --dump-html)"
        )

    # --dump-html mode
    if args.dump_html:
        sample_posts = [{
            "title": "Sample Post Title",
            "url": "https://unseen-japan.com/sample/",
            "image_url": "https://via.placeholder.com/628x400",
            "excerpt": "This is a sample excerpt for template preview.",
        }]
        print(build_newsletter_html(
            sample_posts, args.template, extras_mod.SAMPLE_EXTRAS,
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
    # Fetch WordPress post data
    # -----------------------------------------------------------------------
    print("Fetching WordPress credentials...", file=sys.stderr)
    wp_site, wp_auth = get_wp_config()

    posts_data: list[dict] = []
    temp_dir = tempfile.mkdtemp(prefix="uj_newsletter_")
    print(f"Temp directory: {temp_dir}", file=sys.stderr)

    for post_id in args.posts:
        print(f"  Fetching post {post_id}...", file=sys.stderr)
        try:
            post = fetch_post_data(wp_site, post_id, wp_auth, use_intro=args.intro)
        except requests.exceptions.HTTPError as e:
            print(
                f"  WARNING: Failed to fetch post {post_id}: {e}",
                file=sys.stderr,
            )
            continue

        image_path = None
        image_url = None

        if post["featured_media"]:
            print("  Fetching featured image...", file=sys.stderr)
            image_url = get_featured_image_url(
                wp_site, post["featured_media"], wp_auth
            )
            if image_url:
                image_path = download_image(image_url, wp_auth, temp_dir)
                print(f"  Downloaded: {image_path.name}", file=sys.stderr)

        excerpt = post["excerpt"]
        if is_insider_post(post["title"]):
            excerpt = excerpt + INSIDER_BLURB
            print("  Insider post detected — appended upgrade blurb.", file=sys.stderr)

        posts_data.append({
            "post_id": post_id,
            "title": post["title"],
            "url": post["url"],
            "excerpt": excerpt,
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
                post["image_url"] = mc_url
                print(f"  Uploaded: {filename}", file=sys.stderr)
            except requests.exceptions.HTTPError as e:
                print(
                    f"  WARNING: Image upload failed for post "
                    f"{post['post_id']}: {e}",
                    file=sys.stderr,
                )
                # image_url already set to WP URL as fallback
        else:
            post["image_url"] = post.get("image_url") or ""

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
    # Resolve "Also from Japan this week" extras
    # -----------------------------------------------------------------------
    extras = extras_mod.resolve_extras(
        args,
        wp_site=wp_site,
        wp_auth=wp_auth,
        post_ids=args.posts,
        post_urls=[p["url"] for p in posts_data],
        log=lambda msg: print(msg, file=sys.stderr),
    )

    # -----------------------------------------------------------------------
    # Build newsletter HTML
    # -----------------------------------------------------------------------
    print("\nBuilding newsletter HTML...", file=sys.stderr)
    newsletter_html = build_newsletter_html(posts_data, args.template, extras)

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
