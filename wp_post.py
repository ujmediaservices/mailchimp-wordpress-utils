"""Shared WordPress post-fetch helpers for newsletter scripts.

Extracted so newsletter-insider.py can reuse the full-post fetch +
Gutenberg-stripping + paragraph-spacing logic that newsletter-single-post.py
established. newsletter-single-post.py is intentionally NOT refactored to
import from here — it keeps its own copies to avoid regression risk on the
existing one-off-send workflow.
"""

from __future__ import annotations

import html as html_mod
import io
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


def get_wp_config() -> tuple[str, tuple[str, str]]:
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


def _raw_blocks_to_html(raw: str) -> str:
    """Strip Gutenberg block comments + membership shortcodes from raw HTML."""
    html = re.sub(r"<!--\s*/?wp:\S*?(?:\s+\{.*?\})?\s*-->", "", raw)
    html = re.sub(r"\[swpm_protected[^\]]*\]", "", html)
    html = re.sub(r"\[/swpm_protected\]", "", html)
    html = re.sub(r"\[elementor-template[^\]]*\]", "", html)
    html = re.sub(r"\[[a-zA-Z_-]+[^\]]*\]", "", html)
    # Closing shortcodes start with "[/" so the opening-tag rule above misses
    # them (e.g. [/uj_insider_gate], which was leaking into the Insider body).
    html = re.sub(r"\[/[a-zA-Z_-]+\]", "", html)
    return html.strip()


def _clean_rendered_html(rendered: str) -> str:
    """Drop Elementor template output + stray forms from a context=view body."""
    soup = BeautifulSoup(rendered, "html.parser")
    for el in soup.find_all(attrs={"data-elementor-type": True}):
        el.decompose()
    for form in soup.find_all("form"):
        form.decompose()
    return str(soup)


def fetch_post_full(
    wp_site: str, post_id: int, auth: tuple[str, str],
) -> dict:
    """Return {title, url, content_html, featured_media} with paywall bypass.

    Tries context=edit first (returns the raw Gutenberg, which includes the
    full body even inside swpm_protected blocks). Falls back to context=view
    if the credentials lack edit access.
    """
    url = f"{wp_site}/wp-json/wp/v2/posts/{post_id}"

    resp = requests.get(
        url, params={"context": "edit"}, auth=auth, timeout=30,
    )
    if resp.status_code == 200:
        data = resp.json()
        raw = data.get("content", {}).get("raw", "")
        if raw:
            return {
                "title": html_mod.unescape(data["title"]["rendered"]),
                "url": data["link"],
                "content_html": _raw_blocks_to_html(raw),
                "featured_media": data.get("featured_media") or None,
            }

    resp = requests.get(
        url,
        params={"_fields": "title,link,content,featured_media"},
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "title": html_mod.unescape(data["title"]["rendered"]),
        "url": data["link"],
        "content_html": _clean_rendered_html(data["content"]["rendered"]),
        "featured_media": data.get("featured_media") or None,
    }


def get_featured_image_url(
    wp_site: str, media_id: int, auth: tuple[str, str],
) -> str | None:
    url = f"{wp_site}/wp-json/wp/v2/media/{media_id}"
    resp = requests.get(
        url, params={"_fields": "source_url"}, auth=auth, timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("source_url") or None


def download_image(
    image_url: str, auth: tuple[str, str], temp_dir: str,
) -> Path:
    filename = Path(urlparse(image_url).path).name
    local_path = Path(temp_dir) / filename
    resp = requests.get(image_url, auth=auth, timeout=60)
    resp.raise_for_status()
    local_path.write_bytes(resp.content)
    return local_path


# ---------------------------------------------------------------------------
# Mailchimp image safety: Mailchimp's file manager rejects WebP uploads
# ("not an image, but has an image extension"). Detect WebP by magic bytes
# (so it also catches WebP saved with a .jpg name) and convert to JPEG.
# ---------------------------------------------------------------------------

def is_webp_bytes(data: bytes) -> bool:
    """True if `data` is a WebP image (RIFF....WEBP), regardless of extension."""
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def webp_bytes_to_jpeg(data: bytes, *, quality: int = 88) -> bytes:
    """Convert WebP (or any Pillow-readable) image bytes to JPEG bytes."""
    from PIL import Image  # lazy import; only needed when a conversion happens
    im = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def ensure_mailchimp_safe_image(
    data: bytes, filename: str,
) -> tuple[bytes, str, bool]:
    """Return (bytes, filename, converted) ready for a Mailchimp upload.

    If `data` is WebP, convert it to JPEG and swap the filename extension to
    .jpg. Otherwise return the inputs unchanged. The third element flags
    whether a conversion happened (handy for logging).
    """
    if not is_webp_bytes(data):
        return data, filename, False
    jpeg = webp_bytes_to_jpeg(data)
    new_name = re.sub(r"\.[A-Za-z0-9]+$", ".jpg", filename)
    if not new_name.lower().endswith(".jpg"):
        new_name += ".jpg"
    return jpeg, new_name, True


def add_paragraph_spacing(
    content_html: str,
    wp_site: str | None = None,
    wp_auth: tuple[str, str] | None = None,
) -> str:
    """Style paragraphs/headings + cap inline image widths for email rendering.

    Mirrors newsletter-single-post.py's _add_paragraph_spacing so the Insider
    full-article body renders the same way the single-post template does.
    """
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
        try:
            w = int(img.get("width", "0"))
            if w > 0:
                return w
        except (TypeError, ValueError):
            pass
        src = img.get("src", "")
        m = re.search(r"-(\d+)x\d+\.[a-zA-Z]+(?:[?#]|$)", src)
        if m:
            return int(m.group(1))
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
            img.attrs.pop("width", None)
        elif natural_w > MAX_BODY_WIDTH:
            img["width"] = str(MAX_BODY_WIDTH)
        else:
            img["width"] = str(natural_w)
        img.attrs.pop("height", None)
        existing = img.get("style", "")
        img["style"] = (
            (existing.rstrip(";") + ";" if existing else "")
            + "max-width:100%;height:auto;display:block;margin:0 auto;"
        )

    return str(soup)
