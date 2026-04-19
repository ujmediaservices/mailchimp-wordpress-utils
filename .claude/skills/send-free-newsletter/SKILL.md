---
name: send-free-newsletter
description: Draft and send the Unseen Japan free newsletter. Accepts a list of WordPress post IDs, or fetches the 12 most recent posts for selection if none are provided. Suggests a subject line and preview text from the posts, confirms with the user, then runs newsletter-free.py to create the Mailchimp draft. Use when the user asks to send/draft/build the free newsletter.
---

# Send free newsletter

You are drafting the weekly free newsletter for Unseen Japan. The user will give you a list of WordPress post IDs (roughly 5–8). Your job is to turn those into a Mailchimp draft by running `newsletter-free.py`, and to propose a good subject line and preview text along the way.

## Inputs

Expect one of these shapes:
- A space- or comma-separated list of numeric post IDs (e.g. `88520 88516 88486` or `88520, 88516, 88486`).
- The lead post ID called out as "lead" or listed first, with the rest as follow-ups.
- **No list at all.** In that case, fetch the 12 most recent posts and show them to the user for selection (see "No post list provided" below), then stop and wait for the user to pick.

If the user gave fewer than 3 IDs or anything ambiguous, ask before proceeding.

## No post list provided

If the user invoked the skill without specifying posts, fetch the 12 latest published posts from WordPress, most-recent first, and print each as `ID | Title`. Then stop — do not proceed to steps 1–5 until the user picks which posts to include.

```bash
python -c "
import os, requests, html
base = os.environ['WORDPRESS_URL'].rstrip('/')
auth = (os.environ['WORDPRESS_USERNAME'], os.environ['WORDPRESS_PASSWORD'])
r = requests.get(f'{base}/wp-json/wp/v2/posts',
                 params={'_fields': 'id,title,date', 'per_page': 12,
                         'orderby': 'date', 'order': 'desc', 'status': 'publish'},
                 auth=auth, timeout=30)
r.raise_for_status()
for p in r.json():
    print(p['id'], '|', html.unescape(p['title']['rendered']))
"
```

Present the list, then ask the user which posts to include (and which is the lead). Once they reply, continue from step 1 with those IDs.

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

   Treat the first ID as the **lead** unless the user said otherwise. If any fetch fails, report the bad ID and ask how to proceed — do not silently drop it.

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

   Pass the post IDs in the order the user confirmed (lead first). The script prints a Mailchimp edit URL on success — surface that URL to the user so they can open the draft.

5. **Flag the segment reminder.** The script targets the full "Unseen Japan" list by default. If the script's output includes the "NOTE: No segment specified" line, pass it along — the user needs to set the audience segment in Mailchimp before sending.

## Don'ts

- Don't send the campaign. The script creates a **draft** only; sending happens in Mailchimp.
- Don't invent post IDs or guess at titles if a fetch fails. Ask.
- Don't use the `--intro` flag unless the user explicitly asks — the default excerpt behavior is what the free newsletter expects.
- Don't edit `newsletter-free.py` or the Jinja template as part of this workflow. If something needs changing there, surface it separately.
