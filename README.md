# mixelite-cache-warmer

Every 15 minutes GitHub checks 3 pages on mixelite.com (home, newest blog post, /courses/).
If 2 or more are not cached (someone published, updated a page, or cleared the cache), it loads every page in
the sitemaps once as a desktop browser and once as a phone, so real visitors get fast cached pages again.

- Gentle: 2 pages at a time, 1.5 s pause each, no cookies, never touches cart/checkout/my-account.
- If the site's firewall ever challenges it, it stops at once and the run shows red.
- Results: Actions tab -> a run -> the summary at the bottom.
- Switch off: Actions tab -> "Warm mixelite.com cache" -> "..." -> Disable workflow.
- Run by hand: same page -> "Run workflow" (tick "force" to warm even if it looks warm).
