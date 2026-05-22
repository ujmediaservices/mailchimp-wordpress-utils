"""Screenshot an X (Twitter) tweet via headless Chrome.

Why not Playwright? X aggressively blocks Playwright-launched browsers; even a
persistent profile with stealth patches gets rate-limited or shown the login
wall after a few requests. We've been down that road.

Why not screenshot the X URL directly? Same problem: headless Chrome hitting
x.com/user/status/id hits the login wall and shows nothing useful.

What works: Twitter's public oEmbed endpoint
(https://publish.twitter.com/oembed) returns a blockquote + a widgets.js URL.
Loading that blockquote in a local HTML wrapper with widgets.js produces a
clean, branded tweet card without any login requirement — the same widget any
public site uses to embed tweets. Headless Chrome screenshots the rendered
wrapper.

Fallback ladder:
1. oEmbed wrapper (this script's default — clean card, public surface)
2. Direct X URL (this script's fallback — usually shows login wall but
   produces a file Jay can manually replace)
3. (NOT yet implemented) WordPress Embedly route — POST the tweet URL to the
   WP `/wp-json/oembed/1.0/proxy` endpoint, which auto-falls-back to Embedly
   when X's own oEmbed is down. Add this tier if both above stop working.

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
        "body { margin: 0; padding: 12px; background: #ffffff; "
        'font-family: "Helvetica Neue", Arial, sans-serif; }'
        "</style></head><body>\n"
        f"{blockquote_html}\n"
        '<script async src="https://platform.twitter.com/widgets.js" '
        'charset="utf-8"></script>\n'
        "</body></html>\n"
    )


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
