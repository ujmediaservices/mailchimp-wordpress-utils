"""Create a Mailchimp newsletter draft for a single WordPress post.

Sends to all subscribers using a Jinja2 template for formatting.

Usage:
    python uj-newsletter-single-post.py --post-id 88516 --title "Title" --preview "Preview"
    python uj-newsletter-single-post.py --dump-html
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
TEMPLATE_NAME = "single_post.html.j2"


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


def fetch_post(
    wp_site: str, post_id: int, auth: tuple[str, str]
) -> dict:
    """Fetch title, link, and full rendered content for a post.

    First tries ``context=edit`` to get the raw Gutenberg content (which
    bypasses any membership paywall).  Falls back to ``context=view`` if
    the credentials lack edit access.
    """
    url = f"{wp_site}/wp-json/wp/v2/posts/{post_id}"

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
    wp_site: str, media_id: int, auth: tuple[str, str]
) -> str | None:
    """Get the source URL of a post's featured image."""
    url = f"{wp_site}/wp-json/wp/v2/media/{media_id}"
    resp = requests.get(
        url,
        params={"_fields": "source_url"},
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("source_url") or None


def fetch_also_post(
    wp_site: str, post_id: int, auth: tuple[str, str]
) -> dict:
    """Fetch the title, link, excerpt, and featured_media for one
    also-on-UJ post (lightweight — used for the recap section)."""
    url = f"{wp_site}/wp-json/wp/v2/posts/{post_id}"
    resp = requests.get(
        url,
        params={"_fields": "title,link,excerpt,featured_media"},
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    excerpt_text = BeautifulSoup(
        data.get("excerpt", {}).get("rendered", ""), "html.parser",
    ).get_text(strip=True)
    return {
        "post_id": post_id,
        "title": html_mod.unescape(data["title"]["rendered"]),
        "url": data["link"],
        "excerpt": html_mod.unescape(excerpt_text),
        "featured_media": data.get("featured_media") or None,
    }


def download_image(
    image_url: str, auth: tuple[str, str], temp_dir: str,
) -> Path:
    """Download an image to temp_dir and return the local path."""
    filename = Path(urlparse(image_url).path).name
    local_path = Path(temp_dir) / filename
    resp = requests.get(image_url, auth=auth, timeout=60)
    resp.raise_for_status()
    local_path.write_bytes(resp.content)
    return local_path


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


def _add_paragraph_spacing(
    content_html: str,
    wp_site: str | None = None,
    wp_auth: tuple[str, str] | None = None,
) -> str:
    """Add margin/word-wrap styles to paragraphs and headings, and cap
    inline image widths at the 612px article body so large images scale
    down (without upscaling small ones)."""
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

    MAX_BODY_WIDTH = 612
    media_width_cache: dict[int, int | None] = {}

    def _natural_width(img) -> int | None:
        # 1) Existing width attribute
        try:
            w = int(img.get("width", "0"))
            if w > 0:
                return w
        except (TypeError, ValueError):
            pass
        # 2) URL size suffix like -1024x760.png
        src = img.get("src", "")
        m = re.search(r"-(\d+)x\d+\.[a-zA-Z]+(?:[?#]|$)", src)
        if m:
            return int(m.group(1))
        # 3) WordPress wp-image-{id} class -> media API lookup
        if wp_site and wp_auth:
            cls = img.get("class", "")
            if isinstance(cls, list):
                cls = " ".join(cls)
            m = re.search(r"wp-image-(\d+)", cls)
            if m:
                media_id = int(m.group(1))
                if media_id not in media_width_cache:
                    try:
                        r = requests.get(
                            f"{wp_site}/wp-json/wp/v2/media/{media_id}",
                            params={"_fields": "media_details"},
                            auth=wp_auth, timeout=15,
                        )
                        if r.ok:
                            media_width_cache[media_id] = (
                                r.json().get("media_details", {}).get("width")
                            )
                        else:
                            media_width_cache[media_id] = None
                    except requests.RequestException:
                        media_width_cache[media_id] = None
                return media_width_cache[media_id]
        return None

    for img in soup.find_all("img"):
        natural_w = _natural_width(img)
        if natural_w is None:
            # Unknown — don't force a width; rely on max-width style.
            img.attrs.pop("width", None)
        elif natural_w > MAX_BODY_WIDTH:
            img["width"] = str(MAX_BODY_WIDTH)
        else:
            img["width"] = str(natural_w)
        # Drop fixed height so aspect ratio is preserved.
        img.attrs.pop("height", None)
        existing = img.get("style", "")
        img["style"] = (
            (existing.rstrip(";") + ";" if existing else "")
            + "max-width:100%;height:auto;display:block;margin:0 auto;"
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

    def upload_image(self, filename: str, image_bytes: bytes) -> str:
        """Upload an image to the Mailchimp file manager so newsletter
        embeds resolve from Mailchimp's CDN. Returns the hosted URL."""
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
    wp_site: str | None = None,
    wp_auth: tuple[str, str] | None = None,
    also_posts: list[dict] | None = None,
    extras: list[dict] | None = None,
) -> str:
    """Render the Jinja2 single-post template.

    Pass ``wp_site``/``wp_auth`` so inline images can have their natural
    widths resolved via the WordPress media API when the embedded markup
    doesn't carry a width attribute or size-suffixed URL.

    ``also_posts`` is an optional list of dicts (``title``, ``url``,
    ``excerpt``, ``image_url``) rendered as an "Also on UJ" recap section
    after the main article. ``extras`` is the parallel "Also from Japan
    this week" section sourced from the /find-content trend log.
    """
    content_html = _add_paragraph_spacing(
        post["content_html"], wp_site=wp_site, wp_auth=wp_auth,
    )

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template(TEMPLATE_NAME)
    return template.render(
        title=title,
        post_title=post["title"],
        content_html=content_html,
        featured_image_url=featured_image_url,
        also_posts=also_posts or [],
        extras=extras or [],
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
        "--also-posts", nargs="*", type=int, default=[],
        help=(
            "Optional list of WordPress post IDs to render as an "
            '"Also on UJ" recap section after the main article.'
        ),
    )
    parser.add_argument(
        "--dump-html", action="store_true",
        help="Render the template with sample data and print it.",
    )
    extras_mod.add_extras_cli_args(parser)
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
            extras=extras_mod.SAMPLE_EXTRAS,
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
    wp_site, wp_auth = get_wp_config()

    print(f"  Fetching post {args.post_id}...", file=sys.stderr)
    post = fetch_post(wp_site, args.post_id, wp_auth)
    print(f"  Title: {post['title']}", file=sys.stderr)

    # Fetch featured image URL
    featured_image_url = None
    if post.get("featured_media"):
        print("  Fetching featured image...", file=sys.stderr)
        featured_image_url = get_featured_image_url(
            wp_site, post["featured_media"], wp_auth
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
    # -----------------------------------------------------------------------
    # Fetch "Also on UJ" recap posts (optional)
    # -----------------------------------------------------------------------
    also_posts: list[dict] = []
    if args.also_posts:
        print(
            f"\nFetching {len(args.also_posts)} also-on-UJ post(s)...",
            file=sys.stderr,
        )
        temp_dir = tempfile.mkdtemp(prefix="uj_also_")
        for pid in args.also_posts:
            print(f"  Fetching post {pid}...", file=sys.stderr)
            try:
                ap = fetch_also_post(wp_site, pid, wp_auth)
            except requests.exceptions.HTTPError as exc:
                print(
                    f"  WARNING: failed to fetch also-post {pid}: {exc}",
                    file=sys.stderr,
                )
                continue

            image_url = None
            if ap["featured_media"]:
                wp_image_url = get_featured_image_url(
                    wp_site, ap["featured_media"], wp_auth,
                )
                if wp_image_url:
                    try:
                        local_path = download_image(
                            wp_image_url, wp_auth, temp_dir,
                        )
                        mc_filename = (
                            f"newsletter-{pid}-{local_path.name}"
                        )
                        image_url = mc.upload_image(
                            mc_filename, local_path.read_bytes(),
                        )
                        print(
                            f"  Uploaded image: {mc_filename}",
                            file=sys.stderr,
                        )
                    except (
                        requests.exceptions.HTTPError,
                        requests.exceptions.RequestException,
                    ) as exc:
                        print(
                            f"  WARNING: image upload failed for {pid}: "
                            f"{exc} (falling back to WP URL)",
                            file=sys.stderr,
                        )
                        image_url = wp_image_url

            also_posts.append({
                "post_id": pid,
                "title": ap["title"],
                "url": ap["url"],
                "excerpt": ap["excerpt"],
                "image_url": image_url or "",
            })

    # -----------------------------------------------------------------------
    # Resolve "Also from Japan this week" extras
    # -----------------------------------------------------------------------
    # Cited-URL detection runs against the main post body and any also-posts
    # bodies, so we don't tease a story we already wrote up or linked to.
    extras_post_ids = [args.post_id] + list(args.also_posts)
    extras_post_urls = [post["url"]] + [ap["url"] for ap in also_posts]
    extras = extras_mod.resolve_extras(
        args,
        wp_site=wp_site,
        wp_auth=wp_auth,
        post_ids=extras_post_ids,
        post_urls=extras_post_urls,
        log=lambda msg: print(msg, file=sys.stderr),
    )

    print("\nBuilding newsletter HTML...", file=sys.stderr)
    newsletter_html = build_newsletter_html(
        title=args.title,
        post=post,
        featured_image_url=featured_image_url,
        wp_site=wp_site,
        wp_auth=wp_auth,
        also_posts=also_posts,
        extras=extras,
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
