---
name: send-insider-newsletter
description: Draft this week's Unseen Japan Insider newsletter. Runs AFTER `/send-free-newsletter` and wraps that draft, adding the full Insider article on top and dropping ad-style inserts. Reads the free newsletter's state file (`data/last-free-newsletter.json`), takes one Insider post ID, suggests subject + preview, confirms the Insider Mailchimp segment, then creates a draft via `newsletter-insider.py`. Use when the user says "send the Insider newsletter", "build the Insider email", "Insider drop", "schedule Insider", or anything similar. Pre-flight: the free state file must exist and be <3 days old; if not, ask the user to send the free newsletter first.
---

# Send Insider newsletter

You are drafting the weekly Unseen Japan **Insider** newsletter. Unlike
`/send-free-newsletter` (the upstream sibling that drafts the free email),
this one is a thin wrapper: it layers the week's Insider article on top of
whatever the free newsletter already contains, then strips ads since
Insiders pay for an ad-free experience.

## Working directory

This skill assumes the working directory is `D:\uj\mailchimp-wordpress-utils`.
If invoked from elsewhere, `cd` there first:

```bash
cd "D:\uj\mailchimp-wordpress-utils"
```

## Pre-flight check

The Insider newsletter depends on `data/last-free-newsletter.json`, written
by `newsletter-free.py` after a successful free draft. Before doing anything
else, verify it exists and is recent.

```bash
python -c "
import json, datetime as dt, sys
from pathlib import Path
p = Path('data/last-free-newsletter.json')
if not p.exists():
    print('STATE_MISSING'); sys.exit(0)
state = json.loads(p.read_text(encoding='utf-8'))
gen = dt.datetime.fromisoformat(state['generated_at'])
age = dt.datetime.now(dt.timezone.utc) - gen.replace(tzinfo=dt.timezone.utc)
print(f'OK generated_at={state[\"generated_at\"]} age_days={age.days}')
print(f'  free posts: {[p[\"post_id\"] for p in state.get(\"posts\", [])]}')
print(f'  extras: {len(state.get(\"extras\", []))}  jp_social: {len(state.get(\"jp_social\", []))}  editor_note: {bool(state.get(\"editors_note_html\"))}')
"
```

If the output is `STATE_MISSING` or `age_days` is `>= 3`, tell the user:
"The free newsletter state file is missing or too old. Run
`/send-free-newsletter` for this week first, then come back to this skill."
Do not proceed.

## Inputs

- **Insider post ID** (numeric). If the user gives a slug or partial title
  instead, look it up via the WP REST API before continuing. If the user
  doesn't supply one, ask: "Which Insider post are we promoting this week?
  Either give me the WP post ID or its title and I'll resolve it."
- **Insider segment ID** (numeric). Cached locally between runs in
  `.claude/skills/send-insider-newsletter/insider-segment.json`. First-run
  workflow:
  - Check whether the cache file exists. If yes, surface the cached ID and
    ask the user to confirm or override.
  - If no cache, list candidate segments and ask the user to pick:

    ```bash
    python -c "
    import os, requests
    api = os.environ['MAILCHIMP_API_KEY']
    dc = api.rsplit('-', 1)[-1]
    auth = ('apikey', api)
    # Find the audience first
    r = requests.get(f'https://{dc}.api.mailchimp.com/3.0/lists', params={'count': 100}, auth=auth, timeout=30)
    r.raise_for_status()
    audience = next(l for l in r.json()['lists'] if l['name'] == 'Unseen Japan')
    # List its segments
    r = requests.get(f'https://{dc}.api.mailchimp.com/3.0/lists/{audience[\"id\"]}/segments', params={'count': 100}, auth=auth, timeout=30)
    r.raise_for_status()
    for s in r.json()['segments']:
        print(f'{s[\"id\"]:>10}  {s[\"name\"]}  ({s.get(\"member_count\", \"?\")} members)')
    "
    ```

  - Once the user picks an ID, write it to
    `.claude/skills/send-insider-newsletter/insider-segment.json`:

    ```bash
    python -c "
    import json
    from pathlib import Path
    Path('.claude/skills/send-insider-newsletter').mkdir(parents=True, exist_ok=True)
    Path('.claude/skills/send-insider-newsletter/insider-segment.json').write_text(json.dumps({'segment_id': 12345}))
    "
    ```

  This cache is per-machine and gitignored. The script refuses to run live
  without `--insider-segment-id` (deliberate guardrail: omitting it would
  target the full list).

## Steps

1. **Pre-flight** (above). Bail early if state is missing or stale.

2. **Resolve the Insider post.** Either accept the user-given ID or look it
   up. Confirm the title back to the user:

   ```bash
   python -c "
   import os, requests, html
   base = os.environ['WORDPRESS_URL'].rstrip('/')
   auth = (os.environ['WORDPRESS_USERNAME'], os.environ['WORDPRESS_PASSWORD'])
   pid = 88516  # the Insider post ID
   r = requests.get(f'{base}/wp-json/wp/v2/posts/{pid}', params={'_fields': 'title,link,excerpt'}, auth=auth, timeout=30)
   r.raise_for_status()
   d = r.json()
   print(pid, '|', html.unescape(d['title']['rendered']))
   print('  ', d['link'])
   "
   ```

   If the title doesn't contain `[Insider]`, ask the user to double-check the
   ID — you can still proceed if they confirm, but flag it.

3. **Suggest subject + preview, confirm.** Default subject is
   `Insider: <clean title>` (the script's built-in default strips the
   `[Insider]` tag). Default preview is the post excerpt. Offer both and ask
   the user to confirm, edit, or supply alternatives. Match the
   `/send-free-newsletter` voice rules — punchy, specific, no clickbait, no
   em dashes (`—`/`–` are banned everywhere).

4. **Confirm the segment.** Surface the cached or freshly-picked
   Insider-segment ID and confirm it with the user before sending. If the
   user wants a different one, repeat the segment-picker bash from "Inputs".

   **Caveat (confirmed 2026-05-23):** the real audience, the "Unseen Japan
   Insider All" segment, is a Mailchimp **advanced segment** that the Marketing
   API cannot see or set (its id is `mc-...-MonolithAdvanced-NNNNN`). The
   cached `--insider-segment-id` (`4230717`) is the API-visible `insider`
   **tag** (~154), which is close but NOT the same audience. So the segment on
   the created draft is only a placeholder, and **the user must switch it to
   "Unseen Japan Insider All" in the Mailchimp UI before sending.** Don't try
   to automate this; see memory `reference_mailchimp_advanced_segments`. Always
   flag this to the user when you surface the draft URL.

5. **Dry-run the HTML** (recommended on first weekly run, optional after):

   ```bash
   python newsletter-insider.py --dump-html --insider-post-id 88516 > /tmp/insider-preview.html
   ```

   Verify: the Insider article body renders cleanly, the editor's note
   appears once, the JP-tweets section appears with screenshots, the
   carried-over free posts appear minus the Insider one, NO Tours/sponsor
   inserts are visible, and NO "Upgrade to Insider" CTA is at the bottom.

6. **Run the script.** Live send (still creates a draft, not a sent email):

   ```bash
   python newsletter-insider.py --insider-post-id 88516 \
       --insider-segment-id 12345 \
       --subject "APPROVED_SUBJECT" --preview "APPROVED_PREVIEW"
   ```

   The script files the draft into the **"UJ Insider"** campaign folder by
   default (via `--folder`, matched case-insensitively; pass `--folder ""` to
   leave it unfiled). It prints a Mailchimp edit URL on success. Surface it to
   the user so they can open the draft in Mailchimp.

7. **Banned-dash verification.** Before the live send, run the same
   banned-dash grep used in `/send-free-newsletter` against the rendered
   HTML — Insider content goes to paying customers, so the AI-tell tolerance
   is zero:

   ```bash
   python newsletter-insider.py --dump-html --insider-post-id 88516 | python -c "
   import sys, re
   text = sys.stdin.read()
   hits = re.findall(r'—|–|&mdash;|&ndash;|&#8212;|&#x2014;|&#8211;|&#x2013;', text)
   print(f'banned-dash count: {len(hits)}')
   sys.exit(1 if hits else 0)
   "
   ```

   If the count is nonzero, find the source (the Insider article itself is
   the most likely culprit — fix in WordPress and re-run) and resolve before
   the live send.

## Insider audience expectations

Per the agreed design:

- **Insiders see MORE than free subscribers**, never less. The Insider
  article (full body) is the value-add on top of everything in the free
  newsletter.
- **Editor's note, JP-tweets, "Also from Japan", carried-over free posts**
  all render in the Insider variant — same data the free newsletter used.
- **Ad-style inserts (Tours CTA, sponsor blocks) are stripped.** That's
  template-level, not configurable — the Insider template doesn't have
  insert slots.
- **Footer "Upgrade to Insider" CTA is stripped** (irrelevant — they're
  already Insiders).
- **Subscribe button in the share bar is stripped** (same reason).
- **The article body is fetched via `context=edit`** so the SWPM paywall
  shortcodes don't truncate it.

## Don'ts

- Don't run the live send without `--insider-segment-id`. The script
  hard-errors there, but never try to work around it.
- Don't pass `--no-editors-note` — Insiders should see this week's note
  alongside everyone else.
- Don't fall back to `newsletter-single-post.py` for an Insider send. It
  still exists for generic one-off single-article sends, but Insider
  newsletters always go through this wrapper.
- Don't write to `.claude/skills/send-insider-newsletter/insider-segment.json`
  on a failed run.
