# mixelite-cache-warmer

Every 5 minutes it checks 3 pages on mixelite.com (home, newest blog post, /courses/).
If 2 or more are not cached (someone published, updated a page, or cleared the cache), it loads every page in
the sitemaps once as a desktop browser and once as a phone, so real visitors get fast cached pages again.

- It also warms the exact links YouTube viewers land on (`youtube-links.txt`, with their `?vid=`, busiest first),
  because each of those is cached separately. Refresh the list every month or two: `python tools/refresh_youtube_links.py`.
- Gentle: 2 pages at a time, 1.5 s pause each, no cookies, never touches cart/checkout/my-account.
- If the site's firewall ever challenges it, it stops at once and the run shows red.
- How it stays on: one run keeps watch for ~5.5 hours, then starts the next one itself (GitHub's own
  15-minute timer never fired for this repo, so it is only a backup).
- Results: Actions tab -> a run -> the summary at the bottom.
- Switch off: Actions tab -> "Warm mixelite.com cache" -> "..." -> Disable workflow (the running watch stops within 5 minutes).
- Run by hand: same page -> "Run workflow" (tick "force" to warm even if it looks warm).
