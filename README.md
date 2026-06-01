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

Invoked without a post list, the skill scores the last 8 days of posts against a lead-candidate rubric and **proactively proposes a 6-7 post slate with a recommended lead** rather than asking you to pick from scratch — approve, swap, or reorder. By convention, the **third post in the slate is the lead** (used for the subject line and preview text) and an available Insider post slots into **position 4**. Override with "make X the lead" in the same message.

Two sections are staged as editable markdown before the draft is built, so you can review and edit them first:

- **"What Japan's talking about this week"** — a week-in-review of viral JP tweets sourced from `/find-social` shortlists, with screenshots + translations. Staged unconditionally (`python jp_social.py stage`); only skipped if you explicitly say so.
- **A weekly editor's note** — required, read from `inserts/editors-notes.md` (auto-archived after a successful send so each week starts fresh).

See [`.claude/skills/send-free-newsletter/SKILL.md`](.claude/skills/send-free-newsletter/SKILL.md) for the full workflow.

## Claude Code skill: `/send-insider-newsletter`

Drafts the members-only Insider newsletter by wrapping the most recent free draft: the full Insider article on top, the carried-over free content below, and the ad slots stripped. Reads `data/last-free-newsletter.json` (written by the free run), so send the free newsletter first and the Insider drop within a few days.

Dashes from the Insider WordPress article body are stripped automatically at render time (`newsletter-insider.py` wraps the body in `strip_banned_dashes`) — the Insider post itself is never edited via the API. Only non-article copy (editor's note, JP tweets, extras, subject/preview) is fixed at its source.

See [`.claude/skills/send-insider-newsletter/SKILL.md`](.claude/skills/send-insider-newsletter/SKILL.md) for the full workflow.

## Requirements

WORDPRESS_URL, WORDPRESS_USERNAME, and WORDPRESS_PASSWORD are set as environment variables. Password should be an [Application Password](https://developer.wordpress.org/advanced-administration/security/application-passwords/), or the scripts will be unable to access protected website content (e.g., paywalled content). 

## newsletter-free.py

Sends a newsletter consisting of the list of posts referenced by their WordPress IDs. Currently targets All Audience by default.

```python
python newsletter-free.py --title "Japan can't agree what this soda tastes like" --preview "Also on UJ: No phoning while eating ramen, sandwich theft jail time, Nara's deer are moving" --posts 88520 88516 88486 88530 88463 88432 88444
```

### "Also from Japan this week"

Append a section of external stories that UJ noticed but didn't cover, sourced from the `/find-content` skill's trend log. A jump-link teaser at the top of the email lets readers click straight to a specific story below.

```python
python newsletter-free.py \
  --title "..." --preview "..." --posts 88520 88516 \
  --extras-from-trend-log \
  --extras-days 7 --extras-cap 4 \
  --extras-exclude "https://www.asahi.com/articles/already-on-bluesky"
```

Filters: only HIGH and VERY HIGH observations within the lookback window; URLs already cited in any covered post's body are dropped automatically; pass `--extras-exclude URL` (repeatable) to drop stories already shared on social. Use `--extras-json path.json` to bypass the trend log and supply hand-curated entries (a list of objects with `url`, `title_en`, `source`, `synopsis`, `topics`).

The trend log path defaults to `D:/uj/find-content/trends/observations.ndjson` (override with `--extras-log-path` or the `FIND_CONTENT_TREND_LOG` env var). When extras are enabled but nothing matches the filter, the section is silently omitted.

## newsletter-insider.py

Builds the members-only Insider newsletter by wrapping the most recent free draft: the full Insider article renders on top, the carried-over free posts below, and ad slots are stripped. Reads `data/last-free-newsletter.json` (written by `newsletter-free.py`) for the carried content, so run the free newsletter first.

The Insider article body is fetched from WordPress and passed through `strip_banned_dashes` at render time, so em/en dashes in the article never reach the email even though the WordPress post is left untouched. Driven via the `/send-insider-newsletter` skill.

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
