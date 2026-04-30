---
name: send-free-newsletter
description: Draft and send the Unseen Japan free newsletter. Accepts a list of WordPress post IDs, or fetches the 12 most recent posts (excluding any from the previous newsletter) for selection if none are provided. Suggests a subject line and preview text from the posts, confirms with the user, then runs newsletter-free.py to create the Mailchimp draft. Remembers the IDs used in the last newsletter so they can be filtered out next time. Use when the user asks to send/draft/build the free newsletter.
---

# Send free newsletter

You are drafting the weekly free newsletter for Unseen Japan. The user will give you a list of WordPress post IDs (roughly 5–8). Your job is to turn those into a Mailchimp draft by running `newsletter-free.py`, and to propose a good subject line and preview text along the way.

## Working directory

This skill assumes the working directory is `G:\My Drive\Unseen Japan\Code\mailchimp-wordpress-utils`. If invoked from elsewhere, run first:

```bash
cd "G:\My Drive\Unseen Japan\Code\mailchimp-wordpress-utils"
```

All relative paths and scripts referenced below resolve from that directory.

## Inputs

Expect one of these shapes:
- A space- or comma-separated list of numeric post IDs (e.g. `88520 88516 88486` or `88520, 88516, 88486`).
- **A list of letters** referring to a candidate list you previously showed (e.g. `A C E F G` or `a, c, e, f, g`). Translate each letter back to its post ID using the labels you assigned in the candidate list. Preserve the user's letter order when building the ID list.
- **No list at all.** In that case, fetch the 12 most recent posts and show them to the user for selection (see "No post list provided" below), then stop and wait for the user to pick.

**Lead post convention:** by default, **the third post in the user's list is the lead** (not the first). This is the user's standing preference. Only override if the user explicitly says otherwise in the same message (e.g. "make X the lead", "lead with Y", or "lead with E"). The lead designation only informs the *subject line and preview text* — it does **not** affect `--posts` ordering. **Always pass the IDs to `--posts` in the exact order the user gave them** (i.e. the order of their letters or numeric IDs). Do not reorder.

If the user gave fewer than 3 IDs/letters or anything ambiguous, ask before proceeding.

## State: last-newsletter tracking

Every successful run writes the chosen post IDs to `.claude/skills/send-free-newsletter/last-posts.json` (relative to the project root). Format:

```json
{"post_ids": [89101, 88879, 88846], "title": "...", "sent_at": "2026-04-19T12:34:56Z"}
```

This file is per-machine runtime state — it is gitignored and should not be committed.

## No post list provided

If the user invoked the skill without specifying posts, fetch recent published posts from WordPress, exclude any that appear in `last-posts.json`, and show the top 12 remaining as `Letter | ID | Date | Title` (most-recent first), with letters `A`, `B`, `C`, … assigned in display order. Also mention *which* IDs were excluded so the user knows what was filtered. Then stop — do not proceed to steps 1–5 until the user picks which posts to include.

Fetch 20 posts (rather than exactly 12) so you still have at least 12 to show after filtering out the previous newsletter's picks.

```bash
python -c "
import json, os, requests, html, string
from pathlib import Path
base = os.environ['WORDPRESS_URL'].rstrip('/')
auth = (os.environ['WORDPRESS_USERNAME'], os.environ['WORDPRESS_PASSWORD'])

state_path = Path('.claude/skills/send-free-newsletter/last-posts.json')
excluded = set()
if state_path.exists():
    excluded = set(json.loads(state_path.read_text()).get('post_ids', []))

r = requests.get(f'{base}/wp-json/wp/v2/posts',
                 params={'_fields': 'id,title,date', 'per_page': 20,
                         'orderby': 'date', 'order': 'desc', 'status': 'publish'},
                 auth=auth, timeout=30)
r.raise_for_status()

filtered = [p for p in r.json() if p['id'] not in excluded][:12]
if excluded:
    hit = sorted(excluded & {p['id'] for p in r.json()})
    print(f'# Excluded from previous newsletter: {hit}')
for letter, p in zip(string.ascii_uppercase, filtered):
    print(letter, '|', p['id'], '|', p['date'][:10], '|', html.unescape(p['title']['rendered']))
"
```

If `last-posts.json` doesn't exist yet (first run on this machine), show the top 12 with no filtering.

**Remember the letter→ID mapping** you displayed — you'll need it to translate the user's reply. Present the list, then ask the user which posts to include (by letter, e.g. `A C E F G`) and which is the lead. Once they reply, translate the letters back to post IDs in the same order, and continue from step 1 with those IDs.

## Steps

1. **Fetch post titles and excerpts.** Hit the WordPress REST API directly with the credentials in the environment. Use a single Python one-liner via Bash rather than writing a file:

   ```bash
   python -c "
   import os, requests, html
   from bs4 import BeautifulSoup
   base = os.environ['WORDPRESS_URL'].rstrip('/')
   auth = (os.environ['WORDPRESS_USERNAME'], os.environ['WORDPRESS_PASSWORD'])
   for pid in [88520, 88516]:  # replace with the user's IDs
       r = requests.get(f'{base}/wp-json/wp/v2/posts/{pid}',
                        params={'_fields': 'title,excerpt,link'},
                        auth=auth, timeout=30)
       r.raise_for_status()
       d = r.json()
       title = html.unescape(d['title']['rendered'])
       excerpt = BeautifulSoup(d['excerpt']['rendered'], 'html.parser').get_text(strip=True)
       print(pid, '|', title)
       print('  ', excerpt[:200])
   "
   ```

   Treat the **third ID** as the lead by default (see "Lead post convention" above). If any fetch fails, report the bad ID and ask how to proceed — do not silently drop it.

   **Insider posts:** if any fetched title contains `[Insider]` (case-insensitive), the script automatically appends a paywall/upgrade blurb to that post's description in the rendered newsletter. You don't need to do anything — just confirm it to the user when proposing the subject and preview so they know the blurb will appear.

2. **Propose a subject line and preview text.** Base them on the fetched titles:
   - **Subject line:** lead with the hook from the lead post. Keep it punchy and specific — Unseen Japan's voice is dry, curious, slightly irreverent. Avoid clickbait ("You won't believe…"), avoid generic "This week in Japan". Aim for ~60 chars so it doesn't truncate on mobile.
   - **Preview text:** "Also on UJ: " followed by short teasers from the other posts, comma-separated. Aim for ~90–110 chars. Teasers should be noun phrases or short clauses, not full sentences — e.g. `No phoning while eating ramen, sandwich theft jail time, Nara's deer are moving`.

   Look at the `## newsletter-free.py` example in [README.md](README.md) for the tone and format to match.

   Offer **2–3 subject line variants** so the user can pick. Present them as a numbered list with the preview text below.

3. **Confirm with the user.** Show the chosen IDs, the proposed subject, and the proposed preview. Wait for approval or edits before running anything. The user often tweaks wording.

4. **Run the script.** Once approved, execute from the project root:

   ```bash
   python newsletter-free.py --title "APPROVED_TITLE" --preview "APPROVED_PREVIEW" --posts ID1 ID2 ID3 ...
   ```

   Pass the post IDs in the **exact order** the user gave them. Do not reorder based on the lead. The script prints a Mailchimp edit URL on success — surface that URL to the user so they can open the draft.

5. **Save state.** Only after the script exits successfully, overwrite `.claude/skills/send-free-newsletter/last-posts.json` with the IDs that were sent. Use a Python one-liner:

   ```bash
   python -c "
   import json, datetime
   from pathlib import Path
   state = {
       'post_ids': [89101, 88879, 88846],  # replace with the IDs just sent
       'title': 'APPROVED_TITLE',
       'sent_at': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
   }
   Path('.claude/skills/send-free-newsletter/last-posts.json').write_text(json.dumps(state, indent=2))
   "
   ```

   If the script fails, don't write the state file — the next run should still filter against the previously-successful newsletter.

6. **Flag the segment reminder.** The script targets the full "Unseen Japan" list by default. If the script's output includes the "NOTE: No segment specified" line, pass it along — the user needs to set the audience segment in Mailchimp before sending.

## Don'ts

- Don't send the campaign. The script creates a **draft** only; sending happens in Mailchimp.
- Don't invent post IDs or guess at titles if a fetch fails. Ask.
- Don't use the `--intro` flag unless the user explicitly asks — the default excerpt behavior is what the free newsletter expects.
- Don't edit `newsletter-free.py` or the Jinja template as part of this workflow. If something needs changing there, surface it separately.
