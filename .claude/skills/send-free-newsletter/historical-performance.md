# Free newsletter historical performance

Source: 126 free-newsletter campaigns sent through 2026-04-30, profiled via `profile-campaigns.py`.

Use these patterns when proposing subject lines, preview text, and (when the user asks) lead picks.

## Important: trust click rate, not open rate

Apple Mail Privacy Protection inflates open rates across the board. The three highest-open subjects in the corpus are house asks with no article links and ~1% click rate ("Can you help us get to 100?", "Do you hate paywalls? So do we.", "Can you help us with something?"). They get attention but don't move readers. Always weight click rate as the truer engagement signal.

## Subject line patterns that work

Each pattern below is supported by multiple top-quartile click-rate campaigns.

| Pattern | Examples |
|---|---|
| Ranking / listicle promise | "Japan's most ill-mannered city?" (14.3% click), "Here's what tourists hate most about Japan" (14.0%) |
| Named-brand conflict | "Hilton apologizes after attacking Japanese ryokans" (14.0%), "The dark side of APA hotels" (10.6%), "Restaurant closes after telling foreigners 'learn Japanese'" (14.1%) |
| "Forget X, do Y instead" | "Forget Duolingo - learn Japanese this way instead" (13.5%) |
| Curiosity gap with named subject | "How a record company disrespected a dead Japanese singer" (9.4%), "Why a young Japanese woman's murder earned sympathy for her killer" (12.0%), "Why this cosplay earned a Japanese woman death threats" (11.4%) |
| Question with real stakes | "Will tax-free shopping for tourists in Japan go away?" (8.4%), "Does loving ramen make Japanese men undateable?" (6.8%), "Will AV star sue over wedding dress insult?" (8.4%) |
| Direct quote in headline | "Restaurant closes after telling foreigners 'learn Japanese'" (14.1%) |
| "How did X happen" framing | "How did an American tourist get a gun into Japan?" (9.2%) |

Common features: a named entity (person, brand, place), a concrete action or claim, a clear stakes hook, length around 60 characters.

## Subject line patterns to avoid

| Anti-pattern | Why |
|---|---|
| Generic curiosity without a named subject | "Japanese idol bombarded online with Switch 2 accusations" got 61.8% open but only 3.8% click. Tease without payoff. |
| House asks dressed as articles | High opens, near-zero clicks. Use for fundraising emails, not regular newsletters. |
| "This week in Japan" / generic catch-up framing | Provides no specific hook. |
| Multiple weak hooks crammed into one line | Pick one strong hook for the subject; surface the rest in preview text. |

## Article categories that over-index on click rate

When the lead post falls into one of these, lean into the pattern hard:

1. **Foreigner-in-Japan friction.** Tourist complaints, businesses turning foreigners away, manners surveys, immigration friction. Top per-campaign performers in this category include:
   - [tourist-complaints-japan](https://unseen-japan.com/tourist-complaints-japan/): 223 unique clicks/campaign average
   - [izakaya-closes-japanese-only-controversy](https://unseen-japan.com/izakaya-closes-japanese-only-controversy/): 138/campaign
   - [japan-worst-manners-city-survey](https://unseen-japan.com/japan-worst-manners-city-survey/): 135/campaign

2. **Language learning, especially with a Duolingo-skewer.** Evergreen.
   - [sentence-mining-japanese-how-to](https://unseen-japan.com/sentence-mining-japanese-how-to/): 68/campaign across 11 campaigns (highest total clicks)
   - [learn-japanese-without-duolingo](https://unseen-japan.com/learn-japanese-without-duolingo/): 84/campaign

3. **Named-brand controversies.** APA Hotels, Hilton, Maruchan, Tenga. Concrete + nameable converts harder than abstract.

4. **Cultural-norm violation stories.** Things that surprise both Japanese and foreign readers.

5. **Celebrity or public-figure deaths/scandals.** [mogami-ai-sato-airi-murder](https://unseen-japan.com/mogami-ai-sato-airi-murder/) (123/campaign), [nakayama-miho-heat-shock](https://unseen-japan.com/nakayama-miho-heat-shock/) (39/campaign).

## Categories that underperform on clicks

- Generic political analysis without a named subject
- Soft cultural explainers without a controversy hook
- Travel logistics without a stakes angle (visa changes do well *because* of stakes; itinerary recommendations do not)

## Pitch strategy: Insider yes, Tours no

Source: revenue attribution against 96 free-newsletter campaigns since 2024-01-01, joining Mailchimp pitch presence to Stripe and PayPal subscription/payment-intent timestamps within 24h, 48h, and 7-day windows. See `attribute-revenue.py`. Insider coverage is complete (Insider launched April 2025, both processors fully captured). Tours coverage is Stripe-only (no PayPal Tours flow).

**Insider pitching produces signups.** Cohorts that pitched Insider attributed 6-60x more Insider signups than cohorts that did not, depending on attribution window. Signup value at first payment averages roughly $3/send. Over a 12-month typical subscriber lifetime, true value is closer to $35-50/send. Article click rate is unaffected by Insider pitching.

**Tours pitching does not produce bookings.** The "neither" cohort (no pitch at all) had the highest Tours revenue per send across every attribution window, including 24h. The Tours-only cohort actually had lower Tours-attributed revenue than no-pitch cohorts. Tours bookings come from direct, organic, social, and word-of-mouth channels — not the newsletter. Pitching Tours in the newsletter also depresses article click rate (10.76% vs 11.95% baseline).

### Editorial rule

- **Insider CTA in every free newsletter.** This is the existing template default. Do not remove it.
- **Do not add Tours blocks or Tours pitches to free newsletters.** No Tours subject hooks, no Tours preview teasers, no in-body Tours promotion. The data does not support it.
- If the user explicitly asks for a Tours mention in a specific newsletter, comply, but flag that the historical data shows Tours pitching does not move bookings and depresses article engagement.

## Refresh

Re-run `python profile-campaigns.py --top 20` periodically (every 3-6 months) to update subject and article rankings. The script also accepts `--skip-articles` for a faster subject-only pass.

For revenue attribution, re-run `python attribute-revenue.py` after a meaningful change in pitch strategy. The first run takes a few minutes (Stripe pagination); use `--no-fetch` to re-analyze the cached `data/stripe-events.json` without re-fetching.
