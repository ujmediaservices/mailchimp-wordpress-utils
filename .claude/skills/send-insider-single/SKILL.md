---
name: send-insider-single
description: Draft a standalone Unseen Japan Insider email for ONE article, with no newsletter wrapped around it. Two formats, either the full article in the email (default), or `--link-full-article`, which sends only the portion before the Insider paywall break followed by a "Read more on Unseen Japan" button. Use the teaser format for pieces whose full text shouldn't land in an inbox (adult or otherwise sensitive subject matter). Both go to the Insider segment. Use when the user says "send this Insider on its own", "one-off Insider send", "send just the Insider article", "Insider teaser", "link to the full article instead", or names a post ID plus one of those formats. Unlike `/send-insider-newsletter` this needs no free-newsletter state file.
---

# Send a single Insider article

This drafts a standalone Insider email for one WordPress article. It is the
sibling of `/send-insider-newsletter`, minus the newsletter: no carried-over
free posts, no editor's note, no JP tweets, no "Also from Japan", no ad
inserts. Just the article, a greeting, and an optional one-off message.

It has **no dependency on `data/last-free-newsletter.json`**, so it can run any
day of the week, any number of times.

## Working directory

This skill assumes the working directory is `D:\uj\mailchimp-wordpress-utils`.
If invoked from elsewhere, `cd` there first:

```bash
cd "D:\uj\mailchimp-wordpress-utils"
```

## The two formats

| | Full article (default) | `--link-full-article` |
|---|---|---|
| Email body | The whole piece | Everything before the `[uj_insider_gate]` break |
| Ending | Sign-off | "Read more on Unseen Japan" button, then sign-off |
| Use when | Normal Insider piece | The full text shouldn't land in an inbox |

**The teaser format exists for a specific reason:** some Insider pieces carry
adult or otherwise sensitive subject matter that Jay doesn't want dropped
into subscribers' inboxes unannounced. The teaser sends the portion that is
already public on the site and lets readers opt in to the rest by clicking.
Do not offer it as a generic "shorter email" option; ask which format the
user wants and default to full article unless the subject matter suggests
otherwise or they say so.

Everything before the gate is **already public on unseen-japan.com**, so
images and text in the teaser need no extra screening.

## Inputs

- **Post ID** (numeric). If given a slug or partial title, resolve it via the
  WP REST API first. If not supplied, ask which Insider post to send.
- **Format**: full article, or `--link-full-article`. Ask if not stated.
- **Segment ID**. Same Insider audience as the weekly newsletter, so reuse
  its cache rather than keeping a second copy:
  `.claude/skills/send-insider-newsletter/insider-segment.json` (currently
  `4230717`). Surface it and confirm before any live run.

  **Caveat (same as the weekly Insider send):** the real audience, the
  "Unseen Japan Insider All" segment, is a Mailchimp **advanced segment** the
  Marketing API cannot see or set. The cached ID is the API-visible `insider`
  **tag**, which is close but NOT the same audience. The segment on the
  created draft is a placeholder, and **the user must switch it to "Unseen
  Japan Insider All" in the Mailchimp UI before sending.** Always flag this
  when you surface the draft URL. See memory
  `reference_mailchimp_advanced_segments`.

## Steps

1. **Resolve the post and confirm it back to the user.**

   ```bash
   python -c "
   import wp_post
   site, auth = wp_post.get_wp_config()
   pid = 95679  # the Insider post ID
   d = wp_post.fetch_post_full(site, pid, auth)
   open_raw, found = wp_post.split_raw_at_insider_gate(d['raw'])
   print(pid, '|', d['title'])
   print('  ', d['url'])
   print('   gate found:', found, '| open portion:', len(open_raw), 'of', len(d['raw']), 'raw chars')
   "
   ```

   If the post isn't in the **Insider category (`5665`)**, flag it and ask the
   user to double-check. Don't test the title: the `[Insider]` prefix was
   abolished 2026-08-10.

   If `gate found` is `False` and the user asked for the teaser format, stop
   and tell them: the post has no `[uj_insider_gate]` break, so there is
   nothing to cut at. They can add the gate in WordPress themselves, or send
   the piece in full. **Never work around this** by picking an arbitrary cut
   point, and never edit the post via the API (see
   `feedback_never_edit_insider_posts`).

2. **Suggest subject + preview, confirm.** Default subject is
   `Insider: <title>`; default preview is the post excerpt, capped at 140
   chars. Offer both and let the user edit. House style applies: punchy, no
   clickbait, no em/en dashes, preview under 150 chars.

3. **Offer a one-off message** (`--message PATH.md`). This renders a note
   above the article, in the same Markdown vocabulary as newsletter inserts
   (`![alt](url)`, `## Heading`, paragraphs with `[links](url)`,
   `{{URL Button Text}}`). For a teaser send this is the natural place for a
   short "why you're getting the opening only" line. There is a starting
   point at `inserts/insider-single-message.example.md`: copy it to a dated
   file, edit, and pass that. Skip the flag entirely if the user doesn't
   want a note.

4. **Dry-run the HTML** and check for banned dashes:

   ```bash
   python newsletter-insider-single.py --post-id 95679 --link-full-article --dump-html | python check_dashes.py
   ```

   Verify: the body renders cleanly, the teaser stops where you expect, the
   "Read more on Unseen Japan" button is present in teaser mode and absent in
   full mode, no ad inserts, no "Upgrade to Insider" CTA.

   Article-body dashes are auto-stripped at render time. If one still reaches
   the output the bug is in that strip path. **Never edit the WordPress post
   to fix a dash.**

5. **Write the HTML for editing, then send it.** This is the default path,
   not an optional extra: Jay edits in VS Code rather than Mailchimp's classic
   editor. See "Edit before send" below.

6. **Surface the Mailchimp edit URL** and the segment caveat from "Inputs".

## Edit before send

All three newsletter scripts share this two-phase workflow. Prefer it over a
one-shot live run whenever the user might want to touch the copy.

```bash
python newsletter-insider-single.py --post-id 95679 --link-full-article --write-html out/insider.html
```

That renders the real email to `out/insider.html`, writes
`out/insider.html.meta.json` beside it with the campaign settings, and creates
**no** Mailchimp campaign. Hand the path to the user and wait while they edit.

Then ship exactly that file:

```bash
python newsletter-insider-single.py --send-html out/insider.html --segment-id 4230717
```

Notes:

- The sidecar supplies subject, preview, segment, and folder. `--subject`,
  `--preview`, `--segment-id`, and `--folder` override it, which is how a
  subject change made during editing gets applied.
- `--send-html` **aborts on any em/en dash** in the file. The render layer
  strips them automatically, so a hit means a hand edit introduced one. Fix it
  in the file; `--allow-dashes` overrides only if the user insists.
- `--send-html` skips the WordPress fetch and the render entirely, so it ships
  the file byte for byte. Re-running `--write-html` discards edits.
- Don't confuse this with `--dump-html`, which prints to stdout for a quick
  look and (in the free script) uses sample data.

## Other flags

- `--no-image` suppresses the featured image.
- `--read-more-label` / `--read-more-blurb` change the teaser CTA wording.
- `--folder` defaults to **"UJ Insider"** (same as the weekly send). Pass
  `--folder ""` to leave the draft unfiled.

## Template

Formatting lives in `templates/newsletter-insider-single.html.j2`. The
greeting copy, the read-more block, and the message box are all in there
(deliberately template-side, unlike the weekly Insider which builds its
greeting in Python) so one-off wording changes need no code edit. The template
branches on `link_mode` for the teaser-vs-full differences.

## Insider audience expectations

- Recipients already pay, so: no ad inserts, no "Upgrade to Insider" CTA, no
  Subscribe button in the share bar. All template-level, not configurable.
- The article body is fetched via `context=edit` so the paywall shortcodes
  don't truncate it.
- In teaser mode the cut is made on the **raw** body before shortcodes are
  stripped, because `[uj_insider_gate]` is gone from the rendered HTML.

## Don'ts

- Don't run a live send without `--segment-id`. The script hard-errors; never
  work around it.
- Don't use `--link-full-article` on a post with no gate. The script
  hard-errors rather than sending the full body, which is the entire point of
  the mode.
- Don't edit the Insider WordPress post for any reason, including to add a
  gate or fix a dash. Ask the user to do it.
- Don't reach for `newsletter-single-post.py` (the older generic one-off
  sender) for an Insider article.
- Don't use this for the weekly Insider drop that wraps the free newsletter.
  That's `/send-insider-newsletter`.
