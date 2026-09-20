"""Create a Mailchimp newsletter draft from WordPress posts.

Usage:
    python uj-newsletter-free.py --title "Title" --preview "Preview" --posts 123 456
    python uj-newsletter-free.py --dump-html
"""

import argparse
import base64
import datetime as dt
import html as html_mod
import json
import os
import re
import shutil
import string
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

import campaign_html
import extras as extras_mod
import free_fragments
import inserts as inserts_mod
import jp_social as jp_social_mod
import wp_post

# Shared UJ WordPress client (D:\uj\uj-common), used by --list-candidates for
# the 8-day recent-posts pull. The newsletter's own per-post excerpt fetch
# stays in get_wp_config()/fetch_post_data below (it needs the excerpt field +
# banned-dash stripping, which ujwp deliberately doesn't do).
sys.path.insert(0, r"D:\uj\uj-common")
import ujwp  # noqa: E402

LIST_NAME = "Unseen Japan"
TEMPLATE_DIR = Path(__file__).parent / "templates"
DEFAULT_TEMPLATE = "newsletter-free"
DEFAULT_FOLDER = "Unseen Japan Newsletter"
SCRIPT_NAME = "newsletter-free"

# Editor's note defaults: required at the top of every send unless explicitly
# disabled. Archive-after-success rule prevents accidental week-to-week reuse.
PROJECT_ROOT = Path(__file__).parent
DEFAULT_EDITORS_NOTE = PROJECT_ROOT / "inserts" / "editors-notes.md"
EDITORS_NOTE_ARCHIVE_DIR = PROJECT_ROOT / "inserts" / "archive"
STATE_FILE = PROJECT_ROOT / "data" / "last-free-newsletter.json"

INSIDER_BLURB = (
    '<br /><br />'
    '<a href="https://unseen-japan.com/subscribe">Upgrade to our Insider '
    'newsletter</a> to get access to this and <a href="https://unseen-japan.com/insider">all members-only content</a>. '
    "You'll get a special ad-free newsletter plus ad-free website access - over eight years of "
    'Japan coverage, distraction-free.'
)


# UJ dropped the old "[Insider]" title tag (2026-08); members-only posts are
# now marked by the "Insider" WordPress category. Detection is category-based,
# with the legacy title tag kept as a backward-compatible fallback.
INSIDER_CATEGORY_SLUG = "insider"


def resolve_insider_category_ids() -> set[int]:
    """IDs of the members-only "Insider" category, matched by slug.

    Resolved once per run against the live taxonomy so a category-ID change on
    the WP side can't silently disable the paywall blurb. Returns an empty set
    if the lookup fails (detection then falls back to the legacy title tag).
    """
    try:
        cats = ujwp.wp_get(
            "categories", slug=INSIDER_CATEGORY_SLUG,
            _fields="id,slug", per_page=10,
        )
    except Exception:
        return set()
    return {
        c["id"] for c in cats
        if c.get("slug", "").lower() == INSIDER_CATEGORY_SLUG
    }


def is_insider_post(post: dict, insider_category_ids: set[int]) -> bool:
    """True if a post is a members-only Insider post.

    Primary signal is membership in the "Insider" category; the legacy
    "[insider]" title tag is honored as a fallback.
    """
    if insider_category_ids & set(post.get("category_ids") or []):
        return True
    return "[insider]" in (post.get("title") or "").lower()


_BANNED_DASH_RE = re.compile(
    r"[ \t]*(?:—|–|&mdash;|&ndash;|&#8212;|&#x2014;|&#8211;|&#x2013;)[ \t]*"
)


def strip_banned_dashes(text: str) -> str:
    """Replace em/en dashes (and their HTML entity forms) with commas.

    Absolute editorial rule: em and en dashes are banned anywhere in the
    rendered newsletter. WordPress content frequently contains them, so we
    normalize after fetching. See SKILL.md "NEVER use em dashes" section.

    Spaces and tabs hugging the dash are absorbed into the comma, so a
    spaced dash ("meet - and stalk - young women") yields "meet, and stalk,
    young women" rather than "meet , and stalk , young women". Newlines are
    left alone so HTML line structure survives.
    """
    return _BANNED_DASH_RE.sub(", ", text)


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
    fields = "title,link,excerpt,featured_media,categories"
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
        "title": strip_banned_dashes(html_mod.unescape(data["title"]["rendered"])),
        "url": data["link"],
        "excerpt": strip_banned_dashes(html_mod.unescape(excerpt_text)),
        "featured_media": data.get("featured_media") or None,
        "category_ids": data.get("categories", []) or [],
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
# Candidate listing (--list-candidates): the 8-day recent-posts pull the
# send-free-newsletter skill uses to propose a slate. READ-ONLY, no Mailchimp
# calls. Replaces the ~50-line inline `python -c` block the skill used to carry.
# ---------------------------------------------------------------------------

CANDIDATE_WINDOW_DAYS = 8
LAST_POSTS_STATE = (
    PROJECT_ROOT / ".claude" / "skills" / "send-free-newsletter" / "last-posts.json"
)


def _load_excluded_ids() -> set[int]:
    """Post IDs used in the last newsletter, unioned across both state files.

    Primary source is data/last-free-newsletter.json (written by this script's
    write_state_file). The skill also writes .claude/.../last-posts.json after a
    successful run; union both so a just-sent post never resurfaces regardless
    of which file recorded it. Missing files are a no-op (first run).
    """
    excluded: set[int] = set()
    for path in (STATE_FILE, LAST_POSTS_STATE):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for pid in data.get("post_ids", []):
            try:
                excluded.add(int(pid))
            except (TypeError, ValueError):
                continue
    return excluded


def _is_advertorial(post: dict, cat_names: dict[int, str]) -> bool:
    """True if any of the post's categories is an Advertorial/sponsored one.

    UJ files sponsored content under the "Advertorial" category; the title
    alone can read like normal editorial, so match on category name.
    """
    for cid in post.get("categories", []):
        name = cat_names.get(cid, "").lower()
        if "advertor" in name or "sponsor" in name:
            return True
    return False


def collect_candidates() -> dict:
    """Fetch publish posts from the last 8 days, drop advertorials + the last
    newsletter's IDs, and letter-label the survivors. Pure read.

    Returns {window_start, excluded_advertorial, excluded_previous,
    candidate_count, candidates=[{letter,id,date,title}]}.
    """
    cutoff = (
        dt.datetime.now(dt.timezone.utc)
        - dt.timedelta(days=CANDIDATE_WINDOW_DAYS)
    ).isoformat()
    posts = ujwp.wp_get(
        "posts",
        _fields="id,title,date,categories",
        per_page=50,
        orderby="date",
        order="desc",
        status="publish",
        after=cutoff,
    )

    # Resolve category names so Advertorial/sponsored posts can be dropped.
    cat_ids = {c for p in posts for c in p.get("categories", [])}
    cat_names: dict[int, str] = {}
    if cat_ids:
        for c in ujwp.wp_get(
            "categories",
            include=",".join(map(str, sorted(cat_ids))),
            _fields="id,name",
            per_page=100,
        ):
            cat_names[c["id"]] = c.get("name", "")

    excluded = _load_excluded_ids()
    ads = sorted(p["id"] for p in posts if _is_advertorial(p, cat_names))
    ad_set = set(ads)
    filtered = [
        p for p in posts if p["id"] not in excluded and p["id"] not in ad_set
    ]
    excluded_prev = sorted(excluded & {p["id"] for p in posts})

    candidates = [
        {
            "letter": letter,
            "id": p["id"],
            "date": p["date"][:10],
            "title": html_mod.unescape(p["title"]["rendered"]),
        }
        for letter, p in zip(string.ascii_uppercase, filtered)
    ]
    return {
        "window_start": cutoff[:10],
        "excluded_advertorial": ads,
        "excluded_previous": excluded_prev,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def render_candidates(data: dict, as_json: bool) -> str:
    """Render collect_candidates() output as JSON or the skill's Letter|ID|Date|Title table."""
    if as_json:
        return json.dumps(data, ensure_ascii=False, indent=2)
    lines: list[str] = []
    if data["excluded_advertorial"]:
        lines.append(
            "# Excluded Advertorial/sponsored posts (NEVER include unless user "
            f"explicitly asks): {data['excluded_advertorial']}"
        )
    if data["excluded_previous"]:
        lines.append(
            f"# Excluded from previous newsletter: {data['excluded_previous']}"
        )
    lines.append(
        f"# Window: posts published since {data['window_start']} "
        f"({data['candidate_count']} candidates)"
    )
    for c in data["candidates"]:
        lines.append(f"{c['letter']} | {c['id']} | {c['date']} | {c['title']}")
    return "\n".join(lines)


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

    def find_folder(self, name: str) -> str | None:
        """Return the campaign-folder id whose name matches (case-insensitive)."""
        data = self._get("/campaign-folders", {"count": 100})
        target = name.strip().lower()
        for folder in data.get("folders", []):
            if folder.get("name", "").strip().lower() == target:
                return folder["id"]
        return None

    def create_campaign(
        self,
        list_id: str,
        title: str,
        subject: str,
        preview_text: str,
        segment_id: int | None = None,
        folder_id: str | None = None,
    ) -> dict:
        recipients: dict = {"list_id": list_id}
        if segment_id is not None:
            # Saved segment: saved_segment_id alone. Adding match/conditions
            # makes Mailchimp treat it as an ad-hoc segment with no conditions
            # (= the whole list), which silently breaks targeting.
            recipients["segment_opts"] = {
                "saved_segment_id": segment_id,
            }
        settings: dict = {
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
            "recipients": recipients,
            "settings": settings,
        })

    def set_campaign_content(self, campaign_id: str, html: str) -> dict:
        return self._put(f"/campaigns/{campaign_id}/content", {"html": html})


# ---------------------------------------------------------------------------
# Newsletter HTML rendering
# ---------------------------------------------------------------------------

def build_newsletter_html(
    posts: list[dict], template_name: str, extras: list[dict] | None = None,
    reader_intro: bool = False, inserts: dict[int, str] | None = None,
    editors_note_html: str = "", jp_social: list[dict] | None = None,
) -> str:
    """Render the Jinja2 newsletter template with the given posts."""
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template(f"{template_name}.html.j2")
    return template.render(
        posts=posts, extras=extras or [], reader_intro=reader_intro,
        inserts=inserts or {},
        editors_note_html=editors_note_html,
        jp_social=jp_social or [],
    )


# ---------------------------------------------------------------------------
# Editor's note: required by default, archived after a successful send
# ---------------------------------------------------------------------------

def load_editors_note(
    note_path: Path | None, *, no_editors_note: bool,
    log,
) -> str:
    """Return rendered HTML for the editor's note, or ''.

    Hard-errors when the canonical file is missing/empty unless
    `--no-editors-note` was passed. Reuses inserts.render_markdown_insert so
    the markdown vocabulary (image, ## heading, paragraphs, {{URL Button}})
    matches the rest of the newsletter.
    """
    if no_editors_note:
        log("  Editor's note: suppressed via --no-editors-note.")
        return ""
    path = note_path or DEFAULT_EDITORS_NOTE
    if not path.exists():
        print(
            f"ERROR: editor's note required but {path} is missing. Either "
            f"write this week's note (default path: {DEFAULT_EDITORS_NOTE}) "
            "or pass --no-editors-note.",
            file=sys.stderr,
        )
        sys.exit(1)
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        print(
            f"ERROR: editor's note at {path} is empty. Either write this "
            "week's note or pass --no-editors-note.",
            file=sys.stderr,
        )
        sys.exit(1)
    rendered = strip_banned_dashes(inserts_mod.render_markdown_insert(path))
    log(f"  Editor's note: loaded {path.name}.")
    return rendered


def archive_editors_note(note_path: Path, log) -> None:
    """Move the editor's note file to inserts/archive/ after a successful send.

    Forces Jay to rewrite for next week (the absence of the canonical file is
    the source of truth). Skipped silently if the path was an override
    (--editors-note PATH pointing somewhere other than the default), since
    overrides are usually intentional re-runs.
    """
    if note_path.resolve() != DEFAULT_EDITORS_NOTE.resolve():
        log(
            f"  Editor's note from {note_path} not archived "
            "(non-default path)."
        )
        return
    if not note_path.exists():
        return
    EDITORS_NOTE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    target = EDITORS_NOTE_ARCHIVE_DIR / f"editors-notes-{stamp}.md"
    shutil.move(str(note_path), str(target))
    log(f"  Editor's note archived: {target.name} (next week, write a new one).")


# ---------------------------------------------------------------------------
# State file: passes content from this run to newsletter-insider.py
# ---------------------------------------------------------------------------

def write_state_file(
    *, campaign_id: str, web_id: str, subject: str, preview: str,
    posts: list[dict], extras: list[dict], jp_social: list[dict],
    editors_note_html: str, log,
    jp_social_html: str = "", extras_html: str = "",
) -> None:
    """Persist what this free run produced so newsletter-insider.py can wrap it.

    The Insider script reuses extras + jp_social + editors_note_html verbatim,
    layers the full Insider article on top, and skips ad-style inserts. Only
    the most-recent free run is preserved (file is overwritten each time).

    On the --send-html path the post excerpts and the *_html section fragments
    are harvested from the file that actually shipped (see free_fragments), so
    hand edits made between --write-html and --send-html reach the Insider
    instead of being silently dropped.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "campaign_id": campaign_id,
        "web_id": web_id,
        "subject": subject,
        "preview": preview,
        "post_ids": [p["post_id"] for p in posts],
        "posts": [
            {
                "post_id": p["post_id"],
                "title": p["title"],
                "url": p["url"],
                "excerpt": p["excerpt"],
                "image_url": p.get("image_url") or "",
            }
            for p in posts
        ],
        "extras": extras,
        "jp_social": jp_social,
        "editors_note_html": editors_note_html,
        # As-sent section HTML, when harvested. The Insider prefers these over
        # re-rendering the partials from the structured lists above.
        "jp_social_html": jp_social_html,
        "extras_html": extras_html,
    }
    STATE_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    log(f"  State file written: {STATE_FILE.relative_to(PROJECT_ROOT)}")


def run_bookkeeping(
    *, campaign_id: str, web_id: str, subject: str, preview: str,
    bookkeeping: dict, log,
) -> None:
    """Persist state for the Insider wrapper, record JP-social picks, and
    archive the editor's note so next week starts fresh.

    Split out of main() so the --send-html path replays exactly the same side
    effects from the sidecar manifest instead of refetching WordPress.
    """
    if not bookkeeping:
        log(
            "\nPost-send bookkeeping SKIPPED: no sidecar manifest. The Insider "
            "state file was not written, the JP-social ledger was not updated, "
            "and the editor's note was not archived. Do these by hand, or "
            "re-run the render with --write-html so the sidecar exists."
        )
        return

    log("\nPost-send bookkeeping...")
    write_state_file(
        campaign_id=campaign_id,
        web_id=web_id,
        subject=subject,
        preview=preview,
        posts=bookkeeping.get("posts") or [],
        extras=bookkeeping.get("extras") or [],
        jp_social=bookkeeping.get("jp_social") or [],
        editors_note_html=bookkeeping.get("editors_note_html", ""),
        jp_social_html=bookkeeping.get("jp_social_html", ""),
        extras_html=bookkeeping.get("extras_html", ""),
        log=log,
    )
    jp_social_md = bookkeeping.get("jp_social_md") or ""
    if jp_social_md:
        jp_social_mod.record_used(Path(jp_social_md))
        log("  JP-social ledger updated: data/jp-social-used.json")
    if not bookkeeping.get("no_editors_note"):
        note_path = bookkeeping.get("editors_note_path") or ""
        if note_path:
            archive_editors_note(Path(note_path), log=log)


def send_edited_html(args, log) -> None:
    """--send-html: create the draft from a hand-edited file, no render."""
    html, meta = campaign_html.read(
        args.send_html, script=SCRIPT_NAME,
        allow_dashes=args.allow_dashes, log=log,
    )
    cfg = campaign_html.settings(
        meta,
        subject=args.title, preview=args.preview,
        segment_id=args.segment_id, folder=args.folder,
    )
    subject = cfg.get("subject")
    if not subject:
        print(
            "ERROR: no subject line. Pass --title, or send a file whose "
            "sidecar carries one.", file=sys.stderr,
        )
        sys.exit(1)

    mc_api_key = os.environ.get("MAILCHIMP_API_KEY")
    if not mc_api_key:
        print("ERROR: MAILCHIMP_API_KEY environment variable not set.",
              file=sys.stderr)
        sys.exit(1)
    mc = MailchimpAPI(mc_api_key)

    log("\nCreating Mailchimp campaign from the edited file...")
    campaign_id, web_id = campaign_html.create_draft(
        mc, list_name=LIST_NAME, subject=subject,
        preview=cfg.get("preview", ""), segment_id=cfg.get("segment_id"),
        folder=cfg.get("folder", DEFAULT_FOLDER), html=html, log=log,
    )

    # Harvest the copy as it actually stands in the file we just shipped. The
    # sidecar manifest was captured at render time, before any hand edits, so
    # without this the Insider run would wrap the pre-edit version.
    bookkeeping = meta.get("bookkeeping") or {}
    if bookkeeping:
        free_fragments.apply_to_bookkeeping(html, bookkeeping, log=log)

    run_bookkeeping(
        campaign_id=campaign_id, web_id=str(web_id),
        subject=subject, preview=cfg.get("preview", ""),
        bookkeeping=bookkeeping, log=log,
    )

    segment_note = ""
    if not cfg.get("segment_id"):
        segment_note = (
            "\n  NOTE: No segment specified. The campaign targets the "
            "full list.\n        Set the audience segment in Mailchimp "
            "before sending."
        )
    print(
        f"\nDraft campaign created from {args.send_html}!\n"
        f"  Title: {subject}\n"
        f"  Edit: https://{mc.dc}.admin.mailchimp.com/campaigns/edit"
        f"?id={web_id}"
        f"{segment_note}",
    )


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
        "--folder", default=None,
        help=(
            "Mailchimp campaign-folder name to file the draft under "
            f"(matched case-insensitively). Default: {DEFAULT_FOLDER}. Pass an "
            "empty string to leave the campaign unfiled."
        ),
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
        "--reader-intro", action="store_true",
        help=(
            "Insert a 'Hello Loyal UJ Reader,' placeholder block at the top "
            "of the email body for a hand-written personal note. The block "
            "contains a stub paragraph that the editor replaces in Mailchimp "
            "before sending."
        ),
    )
    parser.add_argument(
        "--no-insider-blurb", action="store_true",
        help=(
            "Suppress the auto-appended Insider paywall/upgrade blurb on "
            "[Insider]-titled posts. Use when an embedded insert (e.g. a "
            "tours promo) is the week's CTA and the standard Insider "
            "upgrade pitch would compete with it."
        ),
    )
    parser.add_argument(
        "--insert", action="append", default=[], metavar="PATH:POS",
        help=(
            "Embed a Markdown insert AFTER the given post position "
            "(1-indexed). Example: --insert inserts/tours-unique.md:3 places "
            "the insert between posts 3 and 4. Repeatable for multiple "
            "inserts. Supported Markdown: image, ## heading, paragraphs with "
            "[text](url) links, and {{URL Button Text}} which renders as a "
            "newsletter-style button."
        ),
    )
    parser.add_argument(
        "--editors-note", type=Path, default=None,
        help=(
            f"Path to a markdown editor's note. Default: "
            f"{DEFAULT_EDITORS_NOTE.relative_to(PROJECT_ROOT)}. Hard-errors "
            "if missing/empty (use --no-editors-note to suppress). After a "
            "successful send the default-path file is moved to "
            "inserts/archive/ to force a fresh note next week."
        ),
    )
    parser.add_argument(
        "--no-editors-note", action="store_true",
        help=(
            "Skip the editor's note. Use only for autonomous test runs; "
            "real sends should always carry a fresh note."
        ),
    )
    parser.add_argument(
        "--jp-social", type=Path, default=None,
        help=(
            "Path to a staged JP-social markdown file (from "
            "`python jp_social.py stage`). Renders the 'What Japan's talking "
            "about this week' section between posts and 'Also from Japan'."
        ),
    )
    parser.add_argument(
        "--jp-social-auto", action="store_true",
        help=(
            f"Look for today's JP-social file at "
            f"inserts/jp-social-YYYY-MM-DD.md and use it. Hard-errors if "
            "missing — run `python jp_social.py stage` first."
        ),
    )
    parser.add_argument(
        "--dump-html", action="store_true",
        help="Render the template with sample data and print it.",
    )
    parser.add_argument(
        "--list-candidates", action="store_true",
        help=(
            "READ-ONLY: list publish posts from the last 8 days (advertorials "
            "and last newsletter's IDs dropped, survivors letter-labeled) as a "
            "Letter|ID|Date|Title table. Combine with --json for structured "
            "output. Makes no Mailchimp calls."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="With --list-candidates, emit JSON instead of the table.",
    )
    extras_mod.add_extras_cli_args(parser)
    campaign_html.add_args(parser)
    args = parser.parse_args()

    # --list-candidates mode: pure WP read, no title/preview/posts required.
    if args.list_candidates:
        print(render_candidates(collect_candidates(), as_json=args.json))
        return

    # --send-html: ship a previously written, hand-edited file. Skips the WP
    # fetches and the render; bookkeeping is replayed from the sidecar.
    if args.send_html:
        send_edited_html(args, log=lambda m: print(m, file=sys.stderr))
        return

    if not args.dump_html and (
        not args.title or not args.preview or not args.posts
    ):
        parser.error(
            "--title, --preview, and --posts are required "
            "(unless using --dump-html or --list-candidates)"
        )

    folder = args.folder if args.folder is not None else DEFAULT_FOLDER

    # --dump-html mode
    if args.dump_html:
        sample_posts = [
            {
                "title": f"Sample Post {i}",
                "url": f"https://unseen-japan.com/sample-{i}/",
                "image_url": "https://via.placeholder.com/628x400",
                "excerpt": f"Sample excerpt for post {i}.",
            }
            for i in range(1, 6)
        ]
        sample_inserts = inserts_mod.resolve_inserts(
            args.insert, post_count=len(sample_posts),
            log=lambda msg: print(msg, file=sys.stderr),
        )
        editors_note_html = load_editors_note(
            args.editors_note,
            no_editors_note=args.no_editors_note,
            log=lambda msg: print(msg, file=sys.stderr),
        )
        jp_social_md = jp_social_mod.resolve_md_path(
            jp_social=str(args.jp_social) if args.jp_social else None,
            jp_social_auto=args.jp_social_auto,
        )
        jp_social_items: list[dict] = []
        if jp_social_md:
            jp_social_items = jp_social_mod.load_section(
                jp_social_md, mc=None,
                log=lambda msg: print(msg, file=sys.stderr),
            )
        print(build_newsletter_html(
            sample_posts, args.template, extras_mod.SAMPLE_EXTRAS,
            reader_intro=args.reader_intro, inserts=sample_inserts,
            editors_note_html=editors_note_html,
            jp_social=jp_social_items,
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

    insider_category_ids = resolve_insider_category_ids()
    # The upgrade blurb goes on the FIRST Insider post only; later ones render
    # their excerpt clean. Tracked across the whole post loop.
    insider_blurb_used = False
    if insider_category_ids:
        print(
            f"Insider category IDs: {sorted(insider_category_ids)}",
            file=sys.stderr,
        )

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
        if is_insider_post(post, insider_category_ids):
            if args.no_insider_blurb:
                print(
                    "  Insider post detected; upgrade blurb suppressed "
                    "(--no-insider-blurb).", file=sys.stderr,
                )
            elif insider_blurb_used:
                # One upgrade pitch per newsletter (Jay's call, 2026-08-10).
                # A second identical blurb further down reads as nagging and
                # competes with whatever insert is that week's CTA.
                print(
                    "  Insider post detected; upgrade blurb skipped "
                    "(already used earlier in this newsletter).",
                    file=sys.stderr,
                )
            else:
                excerpt = excerpt + INSIDER_BLURB
                insider_blurb_used = True
                print("  Insider post detected, appended upgrade blurb.", file=sys.stderr)

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
            # Mailchimp rejects WebP; convert to JPEG first if needed.
            image_bytes, filename, converted = wp_post.ensure_mailchimp_safe_image(
                image_bytes, filename,
            )
            if converted:
                print(
                    f"  Converted WebP featured image to JPEG for post "
                    f"{post['post_id']}.", file=sys.stderr,
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
    for e in extras:
        for k in ("title_en", "source", "synopsis"):
            if e.get(k):
                e[k] = strip_banned_dashes(e[k])

    # -----------------------------------------------------------------------
    # Resolve Markdown inserts (--insert PATH:POS, repeatable)
    # -----------------------------------------------------------------------
    inserts_map: dict[int, str] = {}
    if args.insert:
        print("\nLoading Markdown inserts...", file=sys.stderr)
        inserts_map = inserts_mod.resolve_inserts(
            args.insert, post_count=len(posts_data),
            log=lambda msg: print(msg, file=sys.stderr),
        )

    # -----------------------------------------------------------------------
    # Load editor's note (required by default; archived after success)
    # -----------------------------------------------------------------------
    print("\nLoading editor's note...", file=sys.stderr)
    editors_note_path = args.editors_note or DEFAULT_EDITORS_NOTE
    editors_note_html = load_editors_note(
        args.editors_note,
        no_editors_note=args.no_editors_note,
        log=lambda msg: print(msg, file=sys.stderr),
    )

    # -----------------------------------------------------------------------
    # Load JP tweets section (optional; uploads screenshots to Mailchimp)
    # -----------------------------------------------------------------------
    jp_social_md = jp_social_mod.resolve_md_path(
        jp_social=str(args.jp_social) if args.jp_social else None,
        jp_social_auto=args.jp_social_auto,
    )
    jp_social_items: list[dict] = []
    if jp_social_md:
        print(f"\nLoading JP-social section from {jp_social_md}...",
              file=sys.stderr)
        jp_social_items = jp_social_mod.load_section(
            jp_social_md, mc=mc,
            log=lambda msg: print(msg, file=sys.stderr),
        )

    # -----------------------------------------------------------------------
    # Build newsletter HTML
    # -----------------------------------------------------------------------
    print("\nBuilding newsletter HTML...", file=sys.stderr)
    newsletter_html = build_newsletter_html(
        posts_data, args.template, extras, reader_intro=args.reader_intro,
        inserts=inserts_map,
        editors_note_html=editors_note_html,
        jp_social=jp_social_items,
    )

    log = lambda msg: print(msg, file=sys.stderr)  # noqa: E731

    # Everything phase 2 needs to finish the job without refetching anything.
    # Posts are trimmed to the fields write_state_file consumes: the rest
    # (notably image_path, a local Path) is render-time only.
    bookkeeping = {
        "posts": [
            {
                "post_id": p["post_id"],
                "title": p["title"],
                "url": p["url"],
                "excerpt": p["excerpt"],
                "image_url": p.get("image_url") or "",
            }
            for p in posts_data
        ],
        "extras": extras,
        "jp_social": jp_social_items,
        "editors_note_html": editors_note_html,
        "jp_social_md": str(jp_social_md) if jp_social_md else "",
        "editors_note_path": str(editors_note_path),
        "no_editors_note": bool(args.no_editors_note),
    }

    # -----------------------------------------------------------------------
    # --write-html: stop here so the file can be edited before it ships.
    # -----------------------------------------------------------------------
    if args.write_html:
        campaign_html.write(
            args.write_html, newsletter_html,
            script=SCRIPT_NAME,
            campaign={
                "subject": args.title,
                "preview": args.preview,
                "segment_id": args.segment_id,
                "folder": folder,
            },
            bookkeeping=bookkeeping,
            log=log,
        )
        log(
            f"  Bookkeeping deferred: {len(posts_data)} posts, editor's note, "
            "and the JP-social ledger are recorded when you --send-html."
        )
        return

    # -----------------------------------------------------------------------
    # Create campaign
    # -----------------------------------------------------------------------
    print("\nCreating Mailchimp campaign...", file=sys.stderr)
    campaign_id, web_id = campaign_html.create_draft(
        mc, list_name=LIST_NAME, subject=args.title, preview=args.preview,
        segment_id=args.segment_id, folder=folder, html=newsletter_html,
        log=log,
    )

    run_bookkeeping(
        campaign_id=campaign_id, web_id=str(web_id),
        subject=args.title, preview=args.preview,
        bookkeeping=bookkeeping, log=log,
    )

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
