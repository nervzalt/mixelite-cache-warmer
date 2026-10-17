"""Rebuild youtube-links.txt: the exact mixelite.com links YouTube viewers land on (with ?vid=), busiest first.

Runs on Max's PC only (it uses the Co-CEO folder's Google Analytics login). Refresh every month or two:
    python tools/refresh_youtube_links.py
then commit and push youtube-links.txt.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\Ronni\Downloads\Claude\coceo")
import pull_ga4_oauth as g  # noqa: E402
from google.analytics.data_v1beta import BetaAnalyticsDataClient  # noqa: E402
from google.analytics.data_v1beta.types import (DateRange, Dimension, Filter, FilterExpression, Metric,  # noqa: E402
                                                RunReportRequest)

TOP = 100
OUT = Path(__file__).resolve().parents[1] / "youtube-links.txt"
# pages a cookie-free visit must never load (same idea as SKIP in warm.py)
NEVER = re.compile(r"checkout|/cart|add-to-cart|my-account|order-received|thank-you|/offer/|/upsell|wp-admin|wp-login")
# short links that only redirect (Rank Math rules) are not worth warming: redirects are never cached
REDIRECTS = Path(r"C:\Users\Ronni\Downloads\Claude\mixelite-seo\backups")

client = BetaAnalyticsDataClient(credentials=g.get_creds())
resp = client.run_report(RunReportRequest(
    property="properties/403576156", dimensions=[Dimension(name="landingPagePlusQueryString")],
    metrics=[Metric(name="sessions")], date_ranges=[DateRange(start_date="60daysAgo", end_date="yesterday")],
    dimension_filter=FilterExpression(filter=Filter(field_name="landingPagePlusQueryString", string_filter=Filter.StringFilter(
        match_type=Filter.StringFilter.MatchType.CONTAINS, value="vid="))), limit=100000))
rows = sorted(((r.dimension_values[0].value, int(r.metric_values[0].value)) for r in resp.rows), key=lambda x: -x[1])

sources = set()
latest = sorted(REDIRECTS.glob("redirects-*.json"))
if latest:
    for rule in json.loads(latest[-1].read_text(encoding="utf-8")):
        if rule.get("status") == "active":
            sources |= {s["pattern"].strip("/").lower() for s in rule["sources"] if s["comparison"] == "exact"}

links = [u for u, _ in rows if u.startswith("/") and not NEVER.search(u)
         and u.split("?")[0].strip("/").lower() not in sources][:TOP]
total = sum(n for _, n in rows)
covered = sum(n for u, n in rows if u in set(links))
OUT.write_text("# Exact links YouTube viewers land on (GA4, last 60 days), busiest first. Built by tools/refresh_youtube_links.py\n"
               + "\n".join(links) + "\n", encoding="utf-8")
print(f"{len(links)} links, {covered / total:.0%} of {total} YouTube (vid=) visits")
