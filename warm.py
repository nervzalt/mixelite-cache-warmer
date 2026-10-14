"""Re-warm mixelite.com's cache when it goes cold.

Why: any post/page update or WP Rocket purge on mixelite.com wipes the WHOLE Cloudflare (Rocket.net) cache and
WP Rocket's page cache. Cold pages take 2-3 s instead of ~0.2 s. This checks 3 canary pages; if 2+ are not
cached, it fetches every page in the sitemaps once as a desktop browser and once as a phone (the site keeps a
separate mobile cache), 2 at a time, gently (4 at a time hit the site's rate limit after ~100 loads).

Hard rule: if the firewall answers 403 with `cf-mitigated: challenge` (or 429), stop at once and fail.

Usage: python warm.py          check, warm only if cold
       python warm.py --force  warm even if the canaries look warm
"""
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

SITE = "https://mixelite.com"
DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/130.0.0.0 Safari/537.36")
MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/17.5 Mobile/15E148 Safari/604.1")
HEADERS = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate, br"}
SITEMAPS = ("post", "page", "product", "wffn_landing", "wffn_optin", "category")
FIRST = ["/", "/join/", "/7-day-music-producer-course-zero-to-pro/", "/courses/", "/sounds/", "/blog/"]
SKIP = re.compile(r"/(cart|checkout|my-account|order-received|thank-you|wp-admin|wp-login)|/wp-content/|\?")
WORKERS = 2
PAUSE = 1.5  # seconds each worker waits between requests (at most ~80 requests a minute)


class Blocked(Exception):
    pass


stop = threading.Event()


def get(url, ua):
    """One cookie-free GET. Raises Blocked on a firewall challenge or rate limit."""
    if stop.is_set():
        return None
    t0 = time.perf_counter()
    r = requests.get(url, headers={**HEADERS, "User-Agent": ua}, timeout=60, allow_redirects=False)
    took = time.perf_counter() - t0
    if (r.status_code == 403 and "challenge" in r.headers.get("cf-mitigated", "").lower()) or r.status_code == 429:
        stop.set()
        info = {k: v for k, v in r.headers.items() if k.lower() in ("cf-mitigated", "retry-after", "server", "cf-ray")}
        raise Blocked(f"{r.status_code} {info} on {url}")
    return r.status_code, r.headers.get("cf-cache-status", "-").upper(), took


def sitemap_urls():
    idx = requests.get(f"{SITE}/sitemap_index.xml", headers={"User-Agent": DESKTOP}, timeout=60).text
    maps = [m for m in re.findall(r"<loc>([^<]+)</loc>", idx)
            if re.search(r"/(%s)-sitemap\d*\.xml$" % "|".join(SITEMAPS), m)]
    urls = []
    for m in maps:
        xml = requests.get(m, headers={"User-Agent": DESKTOP}, timeout=60).text
        urls += [u for u in re.findall(r"<loc>([^<]+)</loc>", xml) if not u.startswith(f"{SITE}/wp-content/")]
    return urls


def canaries():
    blog = next((u for u in re.findall(r"<loc>(https://mixelite\.com/blog/[^<]+)</loc>",
                                       requests.get(f"{SITE}/post-sitemap.xml", headers={"User-Agent": DESKTOP},
                                                    timeout=60).text)), f"{SITE}/blog/")
    return [f"{SITE}/", blog, f"{SITE}/courses/"]


def summary(text):
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def check_and_warm(force):
    stop.clear()
    t_start = time.time()

    checks = []
    for u in canaries():
        code, status, took = get(u, DESKTOP)
        checks.append(status)
        print(f"canary {status:8} {took:5.2f}s {u}")
        time.sleep(0.5)
    cold_canaries = sum(s != "HIT" for s in checks)
    if cold_canaries < 2 and not force:
        summary(f"Site is warm ({3 - cold_canaries} of 3 test pages cached). Nothing to do.")
        return 0

    urls, seen = [], set()
    for u in [SITE + p for p in FIRST] + sitemap_urls():
        u = u.strip()
        if u in seen or SKIP.search(u[len(SITE):]):
            continue
        seen.add(u)
        urls.append(u)
    jobs = [(u, ua) for u in urls for ua in (DESKTOP, MOBILE)]
    print(f"cold ({cold_canaries} of 3 test pages not cached): warming {len(urls)} pages x desktop+mobile")

    results, lock = [], threading.Lock()

    def work(job):
        if stop.is_set():
            return
        u, ua = job
        try:
            res = get(u, ua)
        except requests.RequestException as e:
            res = ("ERR", type(e).__name__, 0.0)
        if res:
            with lock:
                results.append((u, ua, *res))
        time.sleep(PAUSE)

    blocked = None
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = [pool.submit(work, j) for j in jobs]
        for f in futures:
            try:
                f.result()
            except Blocked as e:
                blocked = blocked or e
    mins = (time.time() - t_start) / 60

    if blocked:
        summary(f"STOPPED: the site's firewall challenged us ({blocked}). Warmed {len(results)} of {len(jobs)} "
                f"before stopping. Not retrying, so the firewall stays calm.")
        return 1

    ok = [r for r in results if r[2] == 200]
    cold = [r for r in ok if r[3] != "HIT"]
    slow = [r for r in ok if r[4] > 1.5]
    bad = [r for r in results if r[2] != 200]
    summary(f"{'Forced warm' if force else 'Site was cold, so it was re-warmed'}: {len(ok)} page loads ({len(urls)} pages x desktop + phone) "
            f"in {mins:.1f} min. {len(cold)} were not cached before this run ({len(slow)} took over 1.5 s).")
    if bad:
        summary(f"{len(bad)} loads did not return a normal page (redirects/errors), first few:")
        for u, ua, code, status, _ in bad[:10]:
            summary(f"- {code} {'phone' if ua == MOBILE else 'desktop'} {u}")
    return 0


def once(force):
    try:
        return check_and_warm(force)
    except Blocked as e:
        summary(f"STOPPED: the site's firewall challenged the cache check ({e}). Not retrying.")
        return 1
    except requests.RequestException as e:
        summary(f"Could not reach the site ({type(e).__name__}); will try again at the next check.")
        return 0


def switched_off():
    """True when Max disabled the workflow in the Actions tab (so a running loop stops too)."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN")
    if not (repo and token):
        return False
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/warm.yml",
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
        return r.ok and r.json().get("state") != "active"
    except requests.RequestException:
        return False


def loop(minutes):
    """Check every 5 minutes for `minutes`, then exit (the workflow starts the next run).
    GitHub's own 15-minute timer proved unreliable, so one run keeps watch for ~5.5 hours."""
    end, failed = time.time() + minutes * 60, 0
    while time.time() < end:
        if switched_off():
            summary("Workflow was switched off: stopping.")
            return 0
        t = time.time()
        print(time.strftime("%H:%M UTC", time.gmtime()), "check")
        if once(False):
            failed = 1
            time.sleep(15 * 60)  # after a firewall answer, wait an extra round before looking again
        time.sleep(max(0, 5 * 60 - (time.time() - t)))
    return failed


if __name__ == "__main__":
    if "--loop" in sys.argv:
        sys.exit(loop(int(sys.argv[sys.argv.index("--loop") + 1])))
    sys.exit(once("--force" in sys.argv))
