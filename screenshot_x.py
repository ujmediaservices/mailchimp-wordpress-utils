"""Screenshot an X (Twitter) tweet as a clean embed card.

We never navigate to x.com directly: headless Chrome (and Playwright) hitting
x.com/user/status/id gets the login wall. Instead we use Twitter's public
oEmbed endpoint (https://publish.twitter.com/oembed), which returns a
blockquote + widgets.js URL. Loaded in a local HTML wrapper, widgets.js
upgrades the blockquote into the real tweet iframe (avatar, media, engagement,
"Read replies"). It is the same public widget any site uses to embed a tweet. This
rendering surface does NOT trip X's bot wall (that wall is for x.com page
navigation, which we avoid).

Capture ladder (capture_tweet tries in order):
1. Playwright (PRIMARY): render the local oEmbed wrapper, wait for widgets.js
   to produce `.twitter-tweet-rendered`, and screenshot just that element ->
   a tight, clean card. (Earlier lore that "Playwright fails for X" was about
   navigating x.com itself; rendering the local public widget is fine.)
2. One-shot headless Chrome on the same wrapper (legacy fallback). This fired
   the screenshot before the iframe painted, so it often captured the bare
   unstyled blockquote; kept only as a fallback if Playwright is unavailable.
3. Direct X URL (last resort, usually the login wall, but leaves a file Jay
   can replace by hand).

Usage:
    python screenshot_x.py --url https://x.com/.../status/... --out out.png
    python screenshot_x.py --url ... --out out.png --width 600 --height 1200
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests


CHROME_PATH_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


def find_chrome() -> str:
    for p in CHROME_PATH_CANDIDATES:
        if Path(p).is_file():
            return p
    found = (
        shutil.which("chrome")
        or shutil.which("google-chrome")
        or shutil.which("chromium")
    )
    if found:
        return found
    print("ERROR: could not locate Chrome / Chromium binary.", file=sys.stderr)
    sys.exit(1)


def fetch_oembed(tweet_url: str) -> dict:
    """Hit Twitter's public oEmbed endpoint for a tweet.

    Returns the parsed JSON. Raises on HTTP error so the caller can fall back
    to a bare-URL screenshot (which usually shows the X login wall but at
    least produces a file Jay can manually replace).
    """
    resp = requests.get(
        "https://publish.twitter.com/oembed",
        params={
            "url": tweet_url,
            "omit_script": "true",
            "dnt": "true",
            "hide_thread": "true",
        },
        timeout=20,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        },
    )
    resp.raise_for_status()
    return resp.json()


def build_wrapper_html(blockquote_html: str) -> str:
    """Wrap the oEmbed blockquote in a minimal HTML doc with widgets.js."""
    return (
        "<!doctype html>\n"
        '<html><head><meta charset="utf-8">'
        "<style>"
        "body { margin: 0; padding: 0; background: #ffffff; "
        'font-family: "Helvetica Neue", Arial, sans-serif; }'
        ".twitter-tweet { margin: 0 !important; }"
        "</style></head><body>\n"
        f"{blockquote_html}\n"
        '<script async src="https://platform.twitter.com/widgets.js" '
        'charset="utf-8"></script>\n'
        "</body></html>\n"
    )


def screenshot_via_playwright(
    tweet_url: str, out_path: Path, *,
    card_width: int = 550, scale: int = 2, timeout_ms: int = 30000,
) -> bool:
    """Render the oEmbed widget with Playwright and screenshot the rendered card.

    This is the primary path: it waits for widgets.js to fully upgrade the
    blockquote into the real tweet iframe (avatar, media, engagement, "Read
    replies") and captures just that element, so the result is a clean embed
    card rather than the bare fallback blockquote. The legacy one-shot
    `--screenshot` Chrome path fired before the iframe painted, producing
    unstyled text.
    """
    try:
        embed = fetch_oembed(tweet_url)
    except (requests.RequestException, json.JSONDecodeError) as exc:
        print(f"  oEmbed fetch failed: {exc}", file=sys.stderr)
        return False
    blockquote = (embed.get("html") or "").strip()
    if not blockquote:
        print("  oEmbed returned no html.", file=sys.stderr)
        return False

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Playwright not installed; falling back.", file=sys.stderr)
        return False

    html = build_wrapper_html(blockquote)
    out_abs = str(Path(out_path).absolute())
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    viewport={"width": card_width + 60, "height": 1800},
                    device_scale_factor=scale,
                )
                page.set_content(html, wait_until="domcontentloaded")
                page.wait_for_selector(".twitter-tweet-rendered", timeout=timeout_ms)
                # Let the embedded media + engagement counts paint.
                page.wait_for_timeout(3500)
                el = (page.query_selector(".twitter-tweet-rendered")
                      or page.query_selector("twitter-widget"))
                if el is None:
                    print("  No rendered widget element found.", file=sys.stderr)
                    return False
                el.screenshot(path=out_abs)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - any render failure -> fall back
        print(f"  Playwright render failed: {exc}", file=sys.stderr)
        return False

    p_out = Path(out_path)
    return p_out.is_file() and p_out.stat().st_size > 0


def screenshot_via_oembed(
    tweet_url: str, out_path: Path, width: int, height: int,
) -> bool:
    """Render the oEmbed wrapper in headless Chrome and screenshot it.

    Returns True on success; False if the oEmbed fetch fails (caller falls
    back to direct-URL screenshot).
    """
    try:
        embed = fetch_oembed(tweet_url)
    except (requests.RequestException, json.JSONDecodeError) as exc:
        print(
            f"  oEmbed fetch failed: {exc}; falling back to direct-URL "
            "screenshot.",
            file=sys.stderr,
        )
        return False

    blockquote = embed.get("html", "").strip()
    if not blockquote:
        print(
            "  oEmbed returned no html; falling back to direct-URL "
            "screenshot.",
            file=sys.stderr,
        )
        return False

    chrome = find_chrome()
    with tempfile.TemporaryDirectory(prefix="uj_x_screenshot_") as tmp:
        wrapper_path = Path(tmp) / "tweet.html"
        wrapper_path.write_text(build_wrapper_html(blockquote), encoding="utf-8")
        cmd = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-sandbox",
            f"--window-size={width},{height}",
            # widgets.js loads the iframe + paints the card; give it time.
            "--virtual-time-budget=15000",
            f"--screenshot={out_path.absolute()}",
            wrapper_path.as_uri(),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if not out_path.is_file() or out_path.stat().st_size == 0:
            print(
                "  Chrome failed to write oEmbed screenshot.",
                file=sys.stderr,
            )
            if result.stderr:
                print(f"  stderr: {result.stderr[:400]}", file=sys.stderr)
            return False
        return True


def screenshot_direct_url(
    tweet_url: str, out_path: Path, width: int, height: int,
) -> bool:
    """Fallback: point headless Chrome at the X URL itself.

    Usually shows the login wall, but produces a file Jay can manually
    replace. We never want the staging script to leave a broken image
    reference in the .md file.
    """
    chrome = find_chrome()
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-sandbox",
        f"--window-size={width},{height}",
        "--virtual-time-budget=10000",
        f"--screenshot={out_path.absolute()}",
        tweet_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    ok = out_path.is_file() and out_path.stat().st_size > 0
    if not ok and result.stderr:
        print(f"  Chrome stderr: {result.stderr[:400]}", file=sys.stderr)
    return ok


def capture_tweet(
    tweet_url: str, out_path: str | Path,
    width: int = 600, height: int = 1200,
) -> Path:
    """Capture a tweet screenshot, oEmbed wrapper first, direct-URL fallback.

    Returns the output path on success. Raises RuntimeError if both methods
    fail.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"  Capturing {tweet_url} -> {out}", file=sys.stderr)
    # Primary: Playwright renders the widget and captures a tight, clean card.
    if screenshot_via_playwright(tweet_url, out):
        return out
    # Legacy fallback: one-shot headless Chrome on the oEmbed wrapper.
    if screenshot_via_oembed(tweet_url, out, width, height):
        return out
    if screenshot_direct_url(tweet_url, out, width, height):
        print(
            "  WARNING: oEmbed path failed; saved direct-URL screenshot "
            "instead (probably shows login wall). Replace manually.",
            file=sys.stderr,
        )
        return out
    raise RuntimeError(
        f"Failed to capture screenshot for {tweet_url}; both oEmbed wrapper "
        "and direct URL produced no file."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="X tweet screenshot via Chrome.")
    parser.add_argument("--url", required=True, help="Tweet URL.")
    parser.add_argument("--out", required=True, help="Output PNG path.")
    parser.add_argument("--width", type=int, default=600)
    parser.add_argument("--height", type=int, default=1200)
    args = parser.parse_args()
    capture_tweet(args.url, args.out, args.width, args.height)
    print(str(Path(args.out).absolute()))


if __name__ == "__main__":
    main()
