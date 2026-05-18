"""Render Markdown 'insert' files into newsletter-style HTML.

An insert is a small Markdown blob (image + heading + paragraphs + CTA)
embedded between posts in the free newsletter. Supported syntax:

    ![alt](image-url)         -> full-width image
    ## Heading                -> h2
    Paragraph text.           -> p with inline [text](url) links
    {{URL Button Text}}       -> centered button styled like "Read more"

Inside `{{...}}` the URL may be wrapped as a Markdown link
(`[url](url)`); the wrapper is stripped before splitting on whitespace.
"""

from __future__ import annotations

import html as html_mod
import re
from pathlib import Path


_BRAND = "#b3421d"


def _inline(text: str) -> str:
    """Convert inline `[text](url)` to <a>, escape everything else."""
    out: list[str] = []
    pos = 0
    for m in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", text):
        out.append(html_mod.escape(text[pos:m.start()]))
        label = html_mod.escape(m.group(1))
        href = html_mod.escape(m.group(2), quote=True)
        out.append(
            f'<a href="{href}" target="_blank" '
            f'style="color:{_BRAND}; text-decoration:underline;">{label}</a>'
        )
        pos = m.end()
    out.append(html_mod.escape(text[pos:]))
    return "".join(out)


def _image_html(alt: str, url: str) -> str:
    return (
        f'<img src="{html_mod.escape(url, quote=True)}" '
        f'alt="{html_mod.escape(alt)}" width="612" '
        'style="width:100%; max-width:612px; height:auto; '
        'border-radius:4px; margin: 0 0 16px 0; display:block;" />'
    )


def _button_html(url: str, label: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        'style="margin: 16px auto 8px auto;">'
        "<tr>"
        f'<td align="center" style="border-radius:4px; background-color:{_BRAND};">'
        f'<a href="{html_mod.escape(url, quote=True)}" target="_blank" class="btn" '
        f'style="background-color:{_BRAND}; border-radius:4px; color:#ffffff; '
        "display:inline-block; font-family:Arial, Helvetica, sans-serif; "
        "font-size:16px; font-weight:bold; padding:14px 28px; "
        f'text-decoration:none;">{html_mod.escape(label)}</a>'
        "</td></tr></table>"
    )


def _parse_button(inner: str) -> tuple[str, str]:
    """Parse `{{...}}` inner content into (url, label).

    Accepts either a bare URL or a Markdown-link-wrapped URL as the first
    token; the rest of the string is the button label.
    """
    inner = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\2", inner).strip()
    parts = inner.split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1].strip()
    return parts[0], parts[0]


def render_markdown_insert(md_path: Path) -> str:
    """Read a Markdown insert file and return newsletter-ready HTML.

    Returned HTML is the inner content of a `<td class="content">` cell:
    images, headings, paragraphs, and buttons in document order.
    """
    text = md_path.read_text(encoding="utf-8").strip()
    blocks = re.split(r"\n\s*\n", text)
    out: list[str] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue

        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", block)
        if m:
            out.append(_image_html(m.group(1), m.group(2)))
            continue

        m = re.match(r"^\{\{(.+?)\}\}\s*$", block, flags=re.DOTALL)
        if m:
            url, label = _parse_button(m.group(1).strip())
            out.append(_button_html(url, label))
            continue

        m = re.match(r"^##\s+(.+)$", block)
        if m:
            out.append(f"<h2>{_inline(m.group(1).strip())}</h2>")
            continue

        m = re.match(r"^#\s+(.+)$", block)
        if m:
            out.append(f"<h1>{_inline(m.group(1).strip())}</h1>")
            continue

        para = " ".join(line.strip() for line in block.splitlines())
        out.append(
            '<p style="font-size:18px; line-height:1.5; color:#222222; '
            f'margin:0 0 16px 0;">{_inline(para)}</p>'
        )
    return "\n".join(out)


def parse_insert_spec(spec: str) -> tuple[Path, int]:
    """Parse a `PATH:POS` CLI argument.

    Splits from the right to tolerate Windows drive letters in PATH.
    POS is 1-indexed: the insert renders AFTER post POS.
    """
    if ":" not in spec:
        raise ValueError(
            f"Insert spec must be PATH:POS (got {spec!r}). "
            "Example: inserts/tours-unique.md:3"
        )
    path_str, pos_str = spec.rsplit(":", 1)
    try:
        pos = int(pos_str)
    except ValueError as e:
        raise ValueError(
            f"Insert position must be an integer (got {pos_str!r} in {spec!r})."
        ) from e
    if pos < 1:
        raise ValueError(
            f"Insert position must be >= 1 (got {pos} in {spec!r})."
        )
    return Path(path_str), pos


def resolve_inserts(specs: list[str], post_count: int, log) -> dict[int, str]:
    """Parse CLI specs into a {post_index: html} dict; warn on out-of-range."""
    out: dict[int, str] = {}
    for spec in specs:
        path, pos = parse_insert_spec(spec)
        if not path.exists():
            raise FileNotFoundError(f"Insert file not found: {path}")
        if pos > post_count:
            log(
                f"  WARNING: insert position {pos} exceeds post count "
                f"{post_count}; skipping {path}."
            )
            continue
        if pos in out:
            log(
                f"  WARNING: multiple inserts at position {pos}; "
                f"using last ({path})."
            )
        out[pos] = render_markdown_insert(path)
        log(f"  Loaded insert: {path} (after post {pos})")
    return out
