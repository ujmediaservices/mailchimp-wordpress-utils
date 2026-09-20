A simple set of utilities for generating Mailchimp campaigns for a WordPress website. Originally built for use with Unseen-Japan.com, but can be used with any WordPress website. 

I wrote these because the plugins for doing this are all expensive. Also, none seemed to give me the flexibility I wanted/needed with formatting. By generating my own code, I can tailor this over time to our site's needs.

Each utility uses Jinja2 templating for the email formatting. This makes it easy to make changes to the core template. The HTML code is clean, making it simple to modify in the Mailchimp editor if you want to add custom content to a specific campaign.

Since this is Python, it can theoretically run anywhere. I run it locally but you can also set it up as a scheduled job to run, e.g., automatically every week.

## Claude Code skill: `/send-free-newsletter`

The recommended way to draft the weekly free newsletter. Pass a list of post IDs (or letters from a candidate list) and the skill proposes a subject line + preview text, confirms with you, then runs `newsletter-free.py` to create the Mailchimp draft. Tracks the IDs used in the last newsletter so they're filtered out next time.

```
/send-free-newsletter 88520 88516 88486 88530 88463 88432 88444
/send-free-newsletter            # no args: scores recent posts and proposes a slate
```

Invoked without a post list, the skill scores the last 8 days of posts against a lead-candidate rubric and **proactively proposes a 6-7 post slate with a recommended lead** rather than asking you to pick from scratch. Approve, swap, or reorder. By convention, the **third post in the slate is the lead** (used for the subject line and preview text) and an available Insider post slots into **position 4**. Override with "make X the lead" in the same message.

Two sections are staged as editable markdown before the draft is built, so you can review and edit them first:

- **"What Japan's talking about this week"**: a week-in-review of viral posts about Japan sourced from `/find-social` shortlists, with screenshots and translations. Staged unconditionally (`python jp_social.py stage`, 2 picks by default); only skipped if you explicitly say so. Japanese-language picks render as a JP/EN translation pair; an English-origin post (staged with `jp: None`) renders its text plain, since there is nothing to translate.
- **A weekly editor's note**: required, read from `inserts/editors-notes.md` (auto-archived after a successful send so each week starts fresh).

See [`.claude/skills/send-free-newsletter/SKILL.md`](.claude/skills/send-free-newsletter/SKILL.md) for the full workflow.

## Claude Code skill: `/send-insider-newsletter`

Drafts the members-only Insider newsletter by wrapping the most recent free draft: the full Insider article on top, the carried-over free content below, and the ad slots stripped. Reads `data/last-free-newsletter.json` (written by the free run), so send the free newsletter first and the Insider drop within a few days.

Dashes from the Insider WordPress article body are stripped automatically at render time (`newsletter-insider.py` wraps the body in `strip_banned_dashes`). The Insider post itself is never edited via the API. Only non-article copy (editor's note, JP tweets, extras, subject/preview) is fixed at its source.

See [`.claude/skills/send-insider-newsletter/SKILL.md`](.claude/skills/send-insider-newsletter/SKILL.md) for the full workflow.

## Claude Code skill: `/send-insider-single`

Drafts a standalone Insider email for ONE article, with no newsletter wrapped around it and no dependency on the free-newsletter state file. Two formats: the full article in the email, or `--link-full-article`, which sends only the portion before the `[uj_insider_gate]` paywall break followed by a "Read more on Unseen Japan" button. The teaser format exists for pieces whose full text shouldn't land in an inbox; everything before the gate is already public on the site.

See [`.claude/skills/send-insider-single/SKILL.md`](.claude/skills/send-insider-single/SKILL.md) for the full workflow.

## Edit before send (all three newsletters)

Mailchimp's classic editor is painful for hand edits, so every newsletter script can stop after rendering and hand you the HTML to edit locally:

```bash
# Phase 1: render the real email to a file. No campaign is created.
python newsletter-free.py --title "..." --preview "..." --posts 88520 88516 --write-html out/free.html

# ...edit out/free.html in VS Code...

# Phase 2: create the draft from exactly that file.
python newsletter-free.py --send-html out/free.html --segment-id 12345
```

Phase 1 also writes `out/free.html.meta.json` beside the HTML, holding the campaign settings (subject, preview, segment, folder) plus any post-send bookkeeping the script needs to replay. That is why `--send-html` still writes `data/last-free-newsletter.json`, updates the JP-social ledger, and archives the editor's note: nothing is refetched, and the edited file ships byte for byte.

`--send-html` aborts on any em/en dash in the file (the render layer strips them automatically, so a hit means a hand edit introduced one). Override with `--allow-dashes`.

On the free newsletter's `--send-html` path, the script re-reads the file it is about to ship and harvests the rendered blocks back out (`free_fragments.py`), storing them in the state file alongside the structured data. The Insider run prefers those as-sent fragments over re-rendering. Without this, the sidecar manifest captured at render time is all the Insider sees, so anything edited between phase 1 and phase 2 silently reverts: a lead rewritten from a one-line excerpt into three paragraphs shipped correctly in the free email and then reappeared as the bare WordPress excerpt in the Insider. Fragments are keyed by post URL, not by position, so deleting or reordering blocks while editing stays safe.

This is distinct from `--dump-html`, which prints to stdout and, in `newsletter-free.py`, renders sample posts as a layout preview. Shared implementation lives in `campaign_html.py`.

## Shared rendering behavior

Everything that pulls a WordPress body goes through `wp_post.py`, so these apply to every newsletter:

- **YouTube embeds become clickable thumbnails.** Gutenberg's `wp:embed` YouTube block survives the shortcode strip as a bare URL inside a `<figure>`, which email clients render as plain text. Those (and any `<iframe>` embed that comes through the `context=view` fallback) are replaced with a centered `img.youtube.com` still linking to the video, captioned "Watch on YouTube". Non-YouTube embed providers are left alone.
- **Em and en dashes are stripped at render time**, in both literal and HTML-entity form, and replaced with a comma. The pattern absorbs the spaces and tabs hugging the dash, so a spaced dash ("meet - and stalk - young women") collapses to "meet, and stalk, young women" instead of leaving a floating " , " behind. Newlines are preserved so HTML line structure survives. `check_dashes.py` shares the character list but deliberately does not absorb the whitespace, since a detector should report the dash's own offset.

## Requirements

WORDPRESS_URL, WORDPRESS_USERNAME, and WORDPRESS_PASSWORD are set as environment variables. Password should be an [Application Password](https://developer.wordpress.org/advanced-administration/security/application-passwords/), or the scripts will be unable to access protected website content (e.g., paywalled content). 

## newsletter-free.py

Sends a newsletter consisting of the list of posts referenced by their WordPress IDs. Currently targets All Audience by default.

```python
python newsletter-free.py --title "Japan can't agree what this soda tastes like" --preview "Also on UJ: No phoning while eating ramen, sandwich theft jail time, Nara's deer are moving" --posts 88520 88516 88486 88530 88463 88432 88444
```

### Listing candidates (`--list-candidates`)

A read-only pull of everything published in the last 8 days, letter-labeled so a slate can be picked by letter rather than by ID. No Mailchimp calls are made.

```bash
python newsletter-free.py --list-candidates          # Letter | ID | Date | Title table
python newsletter-free.py --list-candidates --json   # same data, structured
```

Two exclusions are applied automatically. Advertorial and sponsored posts are dropped, matched on category **name** (`advertor`/`sponsor`) rather than title, because sponsored pieces are titled like ordinary editorial. Posts used in the previous newsletter are dropped too, taken from the union of `data/last-free-newsletter.json` and `.claude/skills/send-free-newsletter/last-posts.json`, so a post that just went out can't resurface regardless of which file recorded it. Both exclusion lists are printed above the table instead of being applied silently.

This replaces the inline `python -c` block the `/send-free-newsletter` skill used to carry.

### Insider post detection

Members-only posts get an "Upgrade to our Insider newsletter" blurb appended to their excerpt, and only the first Insider in `--posts` order gets it. Detection is by WordPress **category** (slug `insider`), resolved against the live taxonomy on every run so a category-ID change on the WordPress side can't silently switch the blurb off. UJ retired the old `[Insider]` title tag in August 2026; it is still honored as a fallback, and a failed category lookup falls back to it rather than erroring.

### "Also from Japan this week"

Append a section of external stories that UJ noticed but didn't cover, sourced from the `/find-content` skill's trend log. A jump-link teaser at the top of the email lets readers click straight to a specific story below.

```python
python newsletter-free.py \
  --title "..." --preview "..." --posts 88520 88516 \
  --extras-from-trend-log \
  --extras-days 7 --extras-cap 2 \
  --extras-exclude "https://www.asahi.com/articles/already-on-bluesky"
```

The cap defaults to 2 stories. Filters: only HIGH and VERY HIGH observations within the lookback window; URLs already cited in any covered post's body are dropped automatically; pass `--extras-exclude URL` (repeatable) to drop stories already shared on social. Use `--extras-json path.json` to bypass the trend log and supply hand-curated entries (a list of objects with `url`, `title_en`, `source`, `synopsis`, `topics`).

The trend log path defaults to `D:/uj/find-content/trends/observations.ndjson` (override with `--extras-log-path` or the `FIND_CONTENT_TREND_LOG` env var). When extras are enabled but nothing matches the filter, the section is silently omitted.

## newsletter-insider.py

Builds the members-only Insider newsletter by wrapping the most recent free draft: the full Insider article renders on top, the carried-over free posts below, and ad slots are stripped. Reads `data/last-free-newsletter.json` (written by `newsletter-free.py`) for the carried content, so run the free newsletter first.

The Insider article body is fetched from WordPress and passed through `strip_banned_dashes` at render time, so em/en dashes in the article never reach the email even though the WordPress post is left untouched.

Two things are corrected on the way through:

- **The free newsletter's upgrade pitch is stripped** from every carried-over excerpt. `newsletter-free.py` bakes that "Upgrade to our Insider newsletter" text into the excerpt itself rather than adding it in a template, and Insider recipients already pay, so it is removed here for the same reason the Insider template drops the footer upgrade CTA.
- **As-sent section HTML wins over a re-render.** When the state file carries harvested `jp_social_html` / `extras_html` fragments, the template uses them verbatim instead of rebuilding those sections from the structured lists. The partials remain the fallback for a state file written before this existed, or by a one-shot free run with no edit step.

Driven via the `/send-insider-newsletter` skill.

## newsletter-insider-single.py

Sends ONE Insider article on its own: no carried-over posts, no editor's note, no JP tweets, no extras, no ad inserts, and no dependency on `data/last-free-newsletter.json`. Goes to the Insider segment.

```bash
# Full article in the email
python newsletter-insider-single.py --post-id 95679 --segment-id 4230717

# Teaser: everything before the paywall break, then a "Read more" button
python newsletter-insider-single.py --post-id 95679 --link-full-article --segment-id 4230717
```

`--link-full-article` cuts at the theme's `[uj_insider_gate]` shortcode, so the email carries exactly the portion that is already public on the site. The cut is made on the raw Gutenberg body (`wp_post.split_raw_at_insider_gate`) because the shortcode is stripped from the rendered HTML. If a post has no gate the script hard-errors rather than falling back to the full body, since not sending the gated text is the entire point of the mode.

`--message PATH.md` renders a one-off note above the article using the same Markdown vocabulary as the newsletter inserts; see `inserts/insider-single-message.example.md`. Layout lives in `templates/newsletter-insider-single.html.j2`, including the greeting and read-more copy, so one-off wording changes need no code edit. Driven via the `/send-insider-single` skill.

## newsletter-oneoff.py

Send a one-off newsletter written entirely in a markdown file. The frontmatter
holds the subject and preview text; the body is your content.

```python
python newsletter-oneoff.py path/to/draft.md
python newsletter-oneoff.py path/to/draft.md --dump-html   # preview locally
python newsletter-oneoff.py path/to/draft.md --subject "Override"
```

Frontmatter fields (all optional unless noted):

```markdown
---
subject:    "Email subject line"        # required
preview:    "Inbox preview text"        # required
title:      "Internal Mailchimp title"  # defaults to subject
signoff:    true                        # adds the "Jay Allen / UJ" block
from_name:  "Jay at Unseen Japan"
reply_to:   "jay@unseenjapan.com"
audience:   "Unseen Japan"
segment_id: 12345
---

Body in markdown here. Headings, **bold**, lists, blockquotes,
[links](https://...), and `![alt](https://...)` images all work.
```

See [`templates/newsletter-oneoff.example.md`](templates/newsletter-oneoff.example.md)
for a working example.

Images must be hosted at a public URL. The script does not upload local images.

## newsletter-single-post.py

Sends a single post via Mailchimp. Can be used with membership plugins such as Simple Membership Pro. Will specifically strip out Simple Membership Pro shortcodes from posts.  

```python
python newsletter-single-post.py --post-id 88530 --title "Why Japan is sick to death of bicyclists behaving badly" --preview "Police are doling out fines to misbehaving cyclists. Many say it’s about damn time."
```

NOTE: Base code generated by Claude Code with manual cleanup/fixes.

## TO DO

Need to add flags to enable Send Now or Schedule, depending on when you want it sent. This may be involved, as I want to support the options available at various Mailchimp tiers.
