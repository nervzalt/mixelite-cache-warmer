"""Re-warm mixelite.com's cache when it goes cold.

Why: any post/page update or WP Rocket purge on mixelite.com wipes the WHOLE Cloudflare (Rocket.net) cache and
WP Rocket's page cache. Cold pages take 2-3 s instead of ~0.2 s. This checks 3 canary pages; if 2+ are not
cached, it fetches every page in the sitemaps once as a desktop browser and once as a phone (the site keeps a
separate mobile cache), 2 at a time, gently (4 at a time hit the site's rate limit after ~100 loads).

Two layers, checked separately for desktop AND phone (2026-10-21):
- the edge (Cloudflare, per location and per device type): clean-URL loads fill it where this runner is;
- the server's own page copies (WP Rocket, one per device), which every OTHER location falls back to: a real phone that
  missed the edge waited 2.3-3.6 s for a full rebuild because the server had no phone copy, although this warmer had just
  loaded the page as a phone (desktop copies were there; a phone that misses the edge rebuilds them in ~2.5 s).
  A load with a unique ?vid= (ignored by WP Rocket, not by Cloudflare) always reaches the server: it shows whether the
  server has the copy ("Debug: cached@" in the page) and makes the server save one if it had none.

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
# Never load a checkout/cart/upsell page: a cookie-free visit there is what once got an empty-cart checkout cached.
NEVER = r"checkout|/cart|add-to-cart|my-account|order-received|thank-you|/offer/|/upsell|wp-admin|wp-login|/wp-content/"
SKIP = re.compile(NEVER + r"|\?")
# YouTube links carry ?vid= (and some ?f=): Cloudflare caches each exact link separately, and WP Rocket never saves
# capitalised paths like /Free/, so after a purge the first viewer of each link waits 2-3 s. Warm the exact links
# viewers use (youtube-links.txt, busiest first; rebuilt by tools/refresh_youtube_links.py). They may have "?".
YT_SKIP = re.compile(NEVER)


def youtube_links():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "youtube-links.txt")
    if not os.path.exists(path):
        return []
    links = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            link = line.strip()
            if not link.startswith("/") or YT_SKIP.search(link):
                continue
            links.append(SITE + link)
            # The site's clean-URLs snippet 301s /Free/?vid=x and /join?vid=x to the lowercase, slash-ended address;
            # warm that page too, not just the redirect.
            p, q = (link.split("?", 1) + [""])[:2]
            clean = p.lower() if p.endswith("/") else p.lower() + "/"
            if clean != p and not re.search(r"\.[A-Za-z0-9]{1,8}$|^/(wp-|freebie/)", p, re.I):
                links.append(SITE + clean + ("?" + q if q else ""))
    return links
WORKERS = 2
PAUSE = 1.5  # seconds each worker waits between requests (at most ~80 requests a minute)


class Blocked(Exception):
    pass


stop = threading.Event()


def get(url, ua):
    """One cookie-free GET -> (code, edge status, seconds, saved): saved = True when the server sent its saved copy
    (WP Rocket's "Debug: cached@" mark), False when it built the page, None when unknown (edge HIT / not a page).
    Raises Blocked on a firewall challenge or rate limit."""
    if stop.is_set():
        return None
    t0 = time.perf_counter()
    r = requests.get(url, headers={**HEADERS, "User-Agent": ua}, timeout=60, allow_redirects=False)
    took = time.perf_counter() - t0
    if (r.status_code == 403 and "challenge" in r.headers.get("cf-mitigated", "").lower()) or r.status_code == 429:
        stop.set()
        info = {k: v for k, v in r.headers.items() if k.lower() in ("cf-mitigated", "retry-after", "server", "cf-ray")}
        raise Blocked(f"{r.status_code} {info} on {url}")
    edge = r.headers.get("cf-cache-status", "-").upper()
    saved = None
    if r.status_code == 200 and edge != "HIT":
        try:
            saved = "Debug: cached@" in r.text[-4000:]
        except Exception:
            saved = None
    return r.status_code, edge, took, saved


def fresh(url):
    """The same page with a never-seen ?vid=: Cloudflare can't answer it from its cache, WP Rocket ignores vid."""
    fresh.n += 1
    return url + ("&" if "?" in url else "?") + "vid=w%d%04d" % (int(time.time()), fresh.n % 10000)


fresh.n = 0


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
    posts = re.findall(r"<loc>(https://mixelite\.com/blog/[^<]+)</loc>",
                       requests.get(f"{SITE}/post-sitemap.xml", headers={"User-Agent": DESKTOP}, timeout=60).text)
    blog = posts[0] if posts else f"{SITE}/blog/"
    mid = posts[len(posts) // 2] if posts else f"{SITE}/blog/"
    # edge test pages, server test pages
    return [f"{SITE}/", blog, f"{SITE}/courses/"], [f"{SITE}/", blog, mid]


def summary(text):
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def check_and_warm(force):
    stop.clear()
    t_start = time.time()
    edge_pages, server_pages = canaries()
    devices = (("desktop", DESKTOP), ("phone", MOBILE))

    # 1) the edge where this runner is (clean URLs), desktop and phone
    edge_cold = {}
    for name, ua in devices:
        n = 0
        for u in edge_pages:
            code, status, took, _ = get(u, ua)
            n += status != "HIT"
            print(f"edge   {name:7} {status:8} {took:5.2f}s {u}")
            time.sleep(0.5)
        edge_cold[name] = n >= 2
    # 2) the server's saved copies (a unique ?vid= always reaches the server), desktop and phone
    server_cold = {}
    for name, ua in devices:
        n = 0
        for u in server_pages:
            code, status, took, saved = get(fresh(u), ua)
            n += saved is False
            word = "saved copy" if saved else ("REBUILT" if saved is False else f"{code} {status}")
            print(f"server {name:7} {word:10} {took:5.2f}s {u}")
            time.sleep(0.5)
        server_cold[name] = n >= 2
    cold_parts = [f"edge {d}" for d in edge_cold if edge_cold[d]] + [f"server {d}" for d in server_cold if server_cold[d]]
    if not cold_parts and not force:
        summary("Site is warm (edge + server copies, desktop + phone). Nothing to do.")
        return 0

    urls, seen = [], set()
    yt = set(youtube_links())
    for u in [SITE + p for p in FIRST] + youtube_links() + sitemap_urls():
        u = u.strip()
        if u in seen or (u not in yt and SKIP.search(u[len(SITE):])):
            continue
        seen.add(u)
        urls.append(u)
    jobs = [(u, name, ua) for u in urls for name, ua in devices]
    print(f"cold ({', '.join(cold_parts) or 'forced'}): warming {len(urls)} pages x desktop+phone")

    results, extra, lock = [], [], threading.Lock()

    def work(job):
        if stop.is_set():
            return
        u, name, ua = job
        try:
            res = get(u, ua)
        except requests.RequestException as e:
            res = ("ERR", type(e).__name__, 0.0, None)
        if not res:
            return
        with lock:
            results.append((u, name, *res))
        time.sleep(PAUSE)
        # The edge answered (HIT), so the server was not asked: when the server's copies for this device looked cold,
        # make sure it has one (other locations and devices fall back to it). A load that reached the server already
        # left a copy there (it either sent its copy or built and saved one).
        if res[0] == 200 and res[1] == "HIT" and (server_cold[name] or force) and not stop.is_set():
            try:
                r2 = get(fresh(u), ua)
            except requests.RequestException as e:
                r2 = ("ERR", type(e).__name__, 0.0, None)
            if r2:
                with lock:
                    extra.append((u, name, *r2))
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
    bad = [r for r in results if r[2] != 200]
    summary(f"{'Forced warm' if force else 'Site was cold (' + ', '.join(cold_parts) + '), so it was re-warmed'}: "
            f"{len(ok)} page loads ({len(urls)} pages x desktop + phone) + {len(extra)} server loads in {mins:.1f} min.")
    for name, _ in devices:
        mine = [r for r in ok if r[1] == name]
        hit = sum(r[3] == "HIT" for r in mine)
        reached = [r for r in mine if r[3] != "HIT"]
        sv = sum(r[5] is True for r in reached)
        rb = sum(r[5] is False for r in reached)
        ex = [r for r in extra if r[1] == name and r[2] == 200]
        summary(f"- {name}: edge already had {hit}; {len(reached)} went to the server ({sv} from its copy, {rb} rebuilt, "
                f"{sum(r[4] > 1.5 for r in reached)} over 1.5 s); extra server loads {len(ex)} "
                f"({sum(r[5] is True for r in ex)} had a copy, {sum(r[5] is False for r in ex)} rebuilt).")
    if bad:
        summary(f"{len(bad)} loads did not return a normal page (redirects/errors), first few:")
        for u, name, code, status, *_ in bad[:10]:
            summary(f"- {code} {name} {u}")
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
