---
name: send-free-newsletter
description: Draft and send the Unseen Japan free newsletter. Accepts a list of WordPress post IDs, or fetches recent posts from the last 8 days (excluding any from the previous newsletter) for selection if none are provided. Scores each candidate's lead potential against historical click-rate patterns and recommends a lead. Suggests a subject line and preview text, confirms with the user, then builds an "Also from Japan this week" section with 2-3 sentence English synopses sourced from the find-content trend log. Optionally inserts a "Hello Loyal UJ Reader," intro placeholder block for a hand-written personal note, and supports embedding Markdown inserts (image + heading + paragraphs + CTA button) at a chosen post position via --insert PATH:POS. Runs newsletter-free.py to create the Mailchimp draft. Remembers the IDs used in the last newsletter so they can be filtered out next time. Use when the user asks to send/draft/build the free newsletter.
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
- **No list at all.** In that case, fetch posts published in the last 8 days and show them to the user for selection (see "No post list provided" below), then stop and wait for the user to pick.

**Lead post convention:** by default, **the third post in the user's list is the lead** (not the first). This is the user's standing preference. Only override if the user explicitly says otherwise in the same message (e.g. "make X the lead", "lead with Y", or "lead with E"). The lead designation only informs the *subject line and preview text* — it does **not** affect `--posts` ordering. **Always pass the IDs to `--posts` in the exact order the user gave them** (i.e. the order of their letters or numeric IDs). Do not reorder.

If the user gave fewer than 3 IDs/letters or anything ambiguous, ask before proceeding.

## State: last-newsletter tracking

Every successful run writes the chosen post IDs to `.claude/skills/send-free-newsletter/last-posts.json` (relative to the project root). Format:

```json
{"post_ids": [89101, 88879, 88846], "title": "...", "sent_at": "2026-04-19T12:34:56Z"}
```

This file is per-machine runtime state — it is gitignored and should not be committed.

## No post list provided

If the user invoked the skill without specifying posts, fetch posts published in the **last 8 days** from WordPress, exclude any that appear in `last-posts.json`, and show the remaining candidates as `Letter | ID | Date | Title` (most-recent first), with letters `A`, `B`, `C`, … assigned in display order. Also mention *which* IDs were excluded so the user knows what was filtered. Then stop — do not proceed to steps 1–5 until the user picks which posts to include.

Use the WordPress REST API's `after` parameter to enforce the 8-day window server-side (compute the cutoff from the current date, in ISO 8601). Set `per_page=50` so the window isn't artificially capped — UJ rarely publishes more than that in 8 days, but the cap should not be the limiting factor.

```bash
python -c "
import json, os, requests, html, string
from datetime import datetime, timedelta, timezone
from pathlib import Path
base = os.environ['WORDPRESS_URL'].rstrip('/')
auth = (os.environ['WORDPRESS_USERNAME'], os.environ['WORDPRESS_PASSWORD'])

state_path = Path('.claude/skills/send-free-newsletter/last-posts.json')
excluded = set()
if state_path.exists():
    excluded = set(json.loads(state_path.read_text()).get('post_ids', []))

cutoff = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
r = requests.get(f'{base}/wp-json/wp/v2/posts',
                 params={'_fields': 'id,title,date', 'per_page': 50,
                         'orderby': 'date', 'order': 'desc', 'status': 'publish',
                         'after': cutoff},
                 auth=auth, timeout=30)
r.raise_for_status()
posts = r.json()

filtered = [p for p in posts if p['id'] not in excluded]
if excluded:
    hit = sorted(excluded & {p['id'] for p in posts})
    if hit:
        print(f'# Excluded from previous newsletter: {hit}')
print(f'# Window: posts published since {cutoff[:10]} ({len(filtered)} candidates)')
for letter, p in zip(string.ascii_uppercase, filtered):
    print(letter, '|', p['id'], '|', p['date'][:10], '|', html.unescape(p['title']['rendered']))
"
```

If the 8-day window returns fewer than 5 candidates after filtering, tell the user the count is low and ask whether they want to widen the window before proceeding. If `last-posts.json` doesn't exist yet (first run on this machine), the previous-newsletter filter is a no-op and only the 8-day window applies.

**Annotate each candidate with a lead score.** When rendering the candidate list, score each row using the rubric in "Lead candidate scoring" below and add a `Lead` column so the user can see which posts are strongest. Format the table as `Letter | ID | Date | Lead | Title`. Score from the title alone unless it's ambiguous, in which case fetch the excerpt for that one post.

**Remember the letter→ID mapping** you displayed — you'll need it to translate the user's reply. Present the list, then ask the user which posts to include (by letter, e.g. `A C E F G`) and which is the lead. Once they reply, translate the letters back to post IDs in the same order, and continue from step 1 with those IDs.

## Lead candidate scoring

When recommending a lead post (or annotating candidates), score each candidate against the historical-performance patterns in [historical-performance.md](historical-performance.md). The categories below are the actionable summary; consult the file for examples and per-category click numbers.

**Categories that over-index on click rate:**

- **Foreigner-in-Japan friction** — tourist complaints, visa friction, businesses turning foreigners away, manners surveys, "learn Japanese" callouts.
- **Named-brand or named-person controversy** — APA, Hilton, Maruchan, Tenga, etc. The named entity is what converts.
- **Language learning** — especially Duolingo-skewer or "Forget X, do Y" framings.
- **Cultural-norm violation** — something that surprises *both* Japanese and foreign readers.
- **Celebrity / public-figure death or scandal** — must have a named subject.
- **Real-stakes question or ranking** — "Japan's worst-mannered city", "Will tax-free shopping go away?", listicle promises with a sharp angle.

**Categories that under-index on clicks (avoid as lead):**

- Generic political analysis without a named subject.
- Soft cultural explainers without a controversy hook.
- Travel logistics or itinerary recommendations without stakes (visa changes do well *because* of stakes; "scenic trains" or "guide to X city" do not).
- Generic curiosity without a named subject.

**Score each candidate as one of:**

- `strong` — fits a high-CTR category AND has a sharp, named hook (person, brand, place, specific number, or direct quote).
- `neutral` — touches a high-CTR category but with a soft framing, OR has some hook but lacks the named angle.
- `weak` — sits squarely in an under-indexing category (logistics-only travel, soft explainer with no controversy).

Add a 4–8 word reason after the label, e.g. `strong (foreigner-friction, named visa rules)` or `weak (travel guide, no stakes)`.

**Recommendation rule.** The user's standing default is "third post is the lead." Don't unilaterally override it. After scoring the user's chosen subset, do this:

- If the third post scores `strong`, confirm the default and proceed.
- If the third post scores `neutral` or `weak` AND another post scores higher, name that post as the recommended lead, give the one-line reason, and ask whether to switch. Wait for the user before retargeting subject lines.
- If multiple posts tie at `strong`, recommend the one whose hook is most concrete (named entity beats abstract; specific number beats general claim) and ask the user to confirm.

The recommendation only affects which post the *subject line and preview text* lead on. It never reorders the `--posts` argument — that always follows the user's input order.

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
   - **Subject line:** lead with the hook from the lead post. Keep it punchy and specific. Unseen Japan's voice is dry, curious, slightly irreverent. Avoid clickbait ("You won't believe…"), avoid generic "This week in Japan". Aim for ~60 chars so it doesn't truncate on mobile.
   - **Preview text:** "Also on UJ: " followed by short teasers from the other posts, comma-separated. Aim for ~90–110 chars. Teasers should be noun phrases or short clauses, not full sentences. Example: `No phoning while eating ramen, sandwich theft jail time, Nara's deer are moving`.

   Look at the `## newsletter-free.py` example in [README.md](README.md) for the tone and format to match.

   **Apply the historical-performance patterns.** See [historical-performance.md](historical-performance.md) for the full data; the actionable summary:

   - **Patterns that historically over-index on click rate** (use these when shaping the subject):
     - Ranking or listicle promise ("Japan's most ill-mannered city?", "Here's what tourists hate most about Japan")
     - Named-brand or named-person conflict ("Hilton apologizes after attacking Japanese ryokans", "The dark side of APA hotels")
     - "Forget X, do Y instead" framing ("Forget Duolingo, learn Japanese this way")
     - Direct quote in the headline (use actual words from the story)
     - Question with real stakes ("Will tax-free shopping go away?", "Does loving ramen make Japanese men undateable?")
     - Curiosity gap *with a named subject* (vs. generic "Japanese idol")

   - **Patterns to avoid** (high opens, low clicks, they tease but don't convert):
     - Generic curiosity without a named subject
     - Soft questions without stakes
     - "This week in Japan" framing
     - House asks dressed as articles

   - **Run lead scoring before drafting subjects.** Apply the rubric in "Lead candidate scoring" above to all chosen posts. If the recommendation rule says to suggest a different lead, do that *before* writing subject lines and wait for the user's call. Once the lead is settled, lean hardest into whichever high-CTR pattern fits that post.

   Offer **2–3 subject line variants** so the user can pick. Present them as a numbered list with the preview text below. When relevant, label which historical pattern each variant uses (e.g. "ranking", "named-brand conflict") so the user can pick by intent.

3. **Confirm with the user.** Show the chosen IDs, the proposed subject, and the proposed preview. Also ask whether they want a **"Hello Loyal UJ Reader," intro block** in this newsletter (yes/no). If they already specified it in the original message (e.g. "include a personal intro" / "add a reader note"), don't ask again. Wait for approval or edits before running anything. The user often tweaks wording.

4. **Build extras with English synopses.** This step replaces the script's built-in trend-log loader with a synopsis-rich version. Rationale: the trend log only stores Japanese titles and topics, not narrative summaries, so the script's `--extras-from-trend-log` mode renders the section without any "what happened" text. The free newsletter wants 2–3 sentences per item.

   Workflow:

   a. Load candidate extras programmatically (re-uses the same filters the script would have applied: HIGH/VERY HIGH, last 7 days, dedupes, excludes URLs cited in the chosen posts):

      ```bash
      python -c "
      import json, os, requests
      import extras as extras_mod
      from bs4 import BeautifulSoup
      base = os.environ['WORDPRESS_URL'].rstrip('/')
      auth = (os.environ['WORDPRESS_USERNAME'], os.environ['WORDPRESS_PASSWORD'])
      post_ids = [89429, 89160, 89398, 89633, 89352, 89336, 89308]  # the user's IDs
      bodies = []
      post_urls = []
      for pid in post_ids:
          r = requests.get(f'{base}/wp-json/wp/v2/posts/{pid}',
                           params={'_fields': 'content,link'},
                           auth=auth, timeout=30)
          r.raise_for_status()
          d = r.json()
          bodies.append(d.get('content', {}).get('rendered', ''))
          post_urls.append(d.get('link', ''))
      cited = extras_mod.collect_cited_urls(bodies)
      excluded = set(post_urls) | cited
      raw = extras_mod.load_extras_from_trend_log(
          days=extras_mod.DEFAULT_DAYS,
          cap=extras_mod.DEFAULT_CAP,
          log_path=extras_mod.DEFAULT_TREND_LOG,
          exclude_urls=excluded,
      )
      print(json.dumps(raw, ensure_ascii=False, indent=2))
      " > /tmp/extras-stub.json
      ```

      Read back the JSON; you'll get up to 4 records with `url`, `title_jp`, `title_en`, `source`, `topics`, `published_iso`, etc.

   b. **Write a 2-3 sentence English synopsis for each item.** Try WebFetch on the article URL first. If WebFetch refuses ("unable to fetch") or returns paywall stub content, fall back to **`browser_cookie3` + requests**: load Chrome's cookies for the domain and curl the page directly. This is significantly faster than the Claude-in-Chrome tools and handles both blocked-by-WebFetch domains (47news, etc.) and the user's subscription publications (`mainichi.jp`, `asahi.com`, `nikkei.com`).

      ```bash
      python -c "
      import browser_cookie3, requests
      from bs4 import BeautifulSoup
      url = 'https://example.com/article'
      domain = 'example.com'  # set to the registrable domain
      cj = browser_cookie3.chrome(domain_name=domain)
      r = requests.get(url, cookies=cj,
                       headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'},
                       timeout=30)
      r.encoding = r.apparent_encoding or 'utf-8'
      soup = BeautifulSoup(r.text, 'html.parser')
      # Try common selectors; fall back to 'main p' if nothing else hits.
      for sel in ['main article', 'article', '.article-body', '[itemprop=\"articleBody\"]', 'main p']:
          els = soup.select(sel)
          if els:
              text = ' '.join(e.get_text(' ', strip=True) for e in (els if isinstance(els, list) else [els]))
              if len(text) > 200:
                  print(text[:2000]); break
      "
      ```

      Notes:
      - `browser_cookie3` is installed system-wide. If a future run reports it missing, install with `pip install browser-cookie3`.
      - Mainichi articles are still partially paywalled even with cookies (only the lead-in is exposed unless logged in for that specific URL). The lead-in is usually enough for a 2-3 sentence synopsis.
      - For subscription domains where the cookie route still returns only a stub, fall back to writing from `title_jp` / `title_en` / `topics` / `uj_category` and flag the lower confidence to the user.
      - Do NOT use the Claude-in-Chrome browser tools for this step. They are slow (page-load timeouts on Japanese news sites are routine) and the cookie route gets the same content faster.

      The synopsis should explain *what happened* in dry, factual UJ-voice prose. No editorializing, no clickbait. Keep each synopsis to 2-3 sentences (roughly 40-80 words). **No em dashes or en dashes** anywhere in the synopsis text (see Don'ts).

   c. **Build the enriched extras JSON.** Write a list with the schema the template expects: `url`, `title_en`, `source`, `synopsis`, `topics`. Save to `data/last-extras.json`:

      ```bash
      python -c "
      import json
      from pathlib import Path
      enriched = [
          {
              'url': '...',
              'title_en': '...',
              'source': '...',
              'synopsis': 'Two-to-three-sentence English summary of what happened.',
              'topics': ['tag1', 'tag2'],
          },
          # ... up to 4
      ]
      Path('data/last-extras.json').write_text(json.dumps(enriched, indent=2, ensure_ascii=False))
      print(f'wrote {len(enriched)} extras')
      "
      ```

   d. Surface the enriched list to the user before invoking the script — so they can spot a synopsis they don't like and edit it.

   If step 4a returns 0 candidates, mention that and skip directly to step 5 with no `--extras-json` flag (and no `--extras-from-trend-log`). The script will then render no extras section.

5. **Run the script.** Once approved, execute from the project root:

   ```bash
   python newsletter-free.py --title "APPROVED_TITLE" --preview "APPROVED_PREVIEW" --posts ID1 ID2 ID3 ... --extras-json data/last-extras.json
   ```

   Add `--reader-intro` to the command if the user said yes in step 3. The script then inserts a "Hello Loyal UJ Reader," placeholder block at the top of the email body; the user replaces the placeholder paragraph in Mailchimp before sending (or deletes the whole block if they change their mind).

   Add `--insert PATH:POS` (repeatable) to embed a Markdown insert AFTER post `POS` (1-indexed). See "Markdown inserts" below for the file format. Example: `--insert inserts/tours-unique.md:3` places the tours promo between posts 3 and 4. Only use when the user asks for it.

   Pass the post IDs in the **exact order** the user gave them. Do not reorder based on the lead.

   Do **not** also pass `--extras-from-trend-log`; that's the synopsis-less fallback and is mutually exclusive with `--extras-json`. Only use `--extras-from-trend-log` if step 4 was skipped because the trend log returned 0 candidates AND the user explicitly wants the script to retry on its own.

   The script prints a Mailchimp edit URL on success — surface that URL to the user so they can open the draft.

6. **Save state.** Only after the script exits successfully, overwrite `.claude/skills/send-free-newsletter/last-posts.json` with the IDs that were sent. Use a Python one-liner:

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

7. **Flag the segment reminder.** The script targets the full "Unseen Japan" list by default. If the script's output includes the "NOTE: No segment specified" line, pass it along — the user needs to set the audience segment in Mailchimp before sending.

8. **Remind about the reader-intro placeholder.** If `--reader-intro` was passed, tell the user to replace the highlighted "[PERSONAL NOTE: ...]" paragraph in Mailchimp before sending (or delete the whole block if they decide against a note).

## Markdown inserts

The `--insert PATH:POS` flag embeds a Markdown blob (image + heading + paragraphs + CTA button) between posts. The file lives in `inserts/` and is styled by the newsletter template to match the rest of the email.

Supported syntax:

- `![alt](url)` — full-width image
- `## Heading` — h2 in UJ brand color
- Paragraph text with inline `[text](url)` Markdown links
- `{{URL Button Text}}` — renders as a centered, brand-colored button (same style as the "Read more" buttons under each post). The URL may also be wrapped as a Markdown link (`{{[url](url) Button Text}}`); the wrapper is stripped before parsing.

Inserts render with a subtle background tint to distinguish them visually from editorial content. `POS` is 1-indexed: `:3` means *after the third post*. Out-of-range positions are skipped with a warning. The flag is repeatable for multiple inserts.

Existing inserts in `inserts/`:

- `tours-unique.md` — Unseen Japan Tours promo.

When the user asks to "include the tours insert" or similar, default to `--insert inserts/tours-unique.md:3` (after the third post, near the lead) unless they specify a position.

## NEVER use em dashes or en dashes — absolute rule

This is the single most important rule in this skill. Read it before every run.

**Banned characters, anywhere in newsletter output:**

- `—` (U+2014 em dash)
- `–` (U+2013 en dash)
- `&mdash;` (HTML entity for em dash)
- `&ndash;` (HTML entity for en dash)
- `&#8212;`, `&#x2014;`, `&#8211;`, `&#x2013;` (numeric entity forms)

**Scope:** subjects, preview text, extras synopses (`title_en`, `synopsis`, anywhere in `data/last-extras.json`), Markdown inserts in `inserts/`, the Jinja templates in `templates/`, the `INSIDER_BLURB` and any other Python string constants in `newsletter-free.py`, and anything else that ends up in the rendered HTML the script sends to Mailchimp. Drafted copy AND template defaults are both in scope.

**Allowed substitutes:** commas, colons, semicolons, periods, parentheses, hyphens (`-`).

**Mandatory verification before calling `set_campaign_content` (i.e. before running `newsletter-free.py`):**

1. Run `--dump-html` with the same flags you're about to use for the real run.
2. Pipe through this check; the count must be `0`:

   ```bash
   python newsletter-free.py --dump-html [SAME FLAGS] | python -c "
   import sys, re
   text = sys.stdin.read()
   hits = re.findall(r'—|–|&mdash;|&ndash;|&#8212;|&#x2014;|&#8211;|&#x2013;', text)
   print(f'banned-dash count: {len(hits)}')
   sys.exit(1 if hits else 0)
   "
   ```

3. If the count is nonzero, find the source (template, insert file, extras JSON, or a Python constant) and fix it before proceeding. Do NOT just edit the rendered HTML.
4. After any edit to a template, Python constant, or insert file, re-grep the project with the Grep tool using pattern `—|–|&mdash;|&ndash;` (exclude `data/` and `.claude/skills/`). Fix any new hit before re-rendering.

This rule exists because em dashes are a tell of LLM-generated copy and conflict with UJ's editorial voice. Failing this check has happened before and is a real annoyance to fix after the fact.

## Don'ts

- Don't send the campaign. The script creates a **draft** only; sending happens in Mailchimp.
- Don't invent post IDs or guess at titles if a fetch fails. Ask.
- Don't use the `--intro` flag unless the user explicitly asks. The default excerpt behavior is what the free newsletter expects.
- Don't edit `newsletter-free.py` or the Jinja template as part of this workflow. If something needs changing there, surface it separately.
