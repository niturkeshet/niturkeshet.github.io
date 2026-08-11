#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
media-hub collector — runs headless (GitHub Actions / any machine with Python).
Two source types:
  1. SITES    — a writer/tag page scraped directly for that writer's articles
                (kept only if the headline contains a keyword).
  2. SEARCHES — Google News RSS query. Two flavours:
                * Israel Hayom writer pages (blocked to direct fetch) → site: query,
                  keyword-filtered, broad window.
                * The spec's Google-topic searches → unconditional (no keyword
                  condition) and LIMITED TO THE LAST 24h (when:1d, per the qdr:d
                  filter in the original request).
Publication date = the article's REAL published date (from the URL or the
article's <meta article:published_time> / datePublished), not the scrape date.
Dedupes (by normalized URL AND title) against data.json and rewrites it.

X/Twitter is NOT collected here — see README / the X note (needs the paid API).

Run:  python collector.py
Deps: pip install requests beautifulsoup4        (XML parsing uses the stdlib)
"""

import json, re, sys, datetime, urllib.parse, pathlib
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import requests
from bs4 import BeautifulSoup

HERE = pathlib.Path(__file__).parent
DATA = HERE / "data.json"
TODAY = datetime.date.today()
TODAY_S = TODAY.isoformat()
RECENT_CUTOFF = (TODAY - datetime.timedelta(days=1)).isoformat()   # "24h" window (google)
MAX_AGE_DAYS = 60                                                  # retention; UI picks the view window
AGE_CUTOFF = (TODAY - datetime.timedelta(days=MAX_AGE_DAYS)).isoformat()
BACKFILL_CAP = 25          # max article fetches per run to correct old dates
RUN_STATS = []             # per-source health for THIS run (fetch ok? how many kept?) → data.json "diag"
LAST_ERR = None           # set by get() on failure so callers can attach the reason to diag

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
}

KEYWORDS = ["קשת 12","ערוץ 12","חדשות 12","כוח מאה","גיא פלג","דודי ורטהיים",
    "קוקה קולה","חרם ערוץ 12","שלמה קרעי","חוק התקשורת","חוק השידורים","רפורמת התקשורת",
    "שוק התקשורת","החוק להחלשת התקשורת","הרשות השנייה",
    "אבי ניר","ארץ נהדרת","עובדה","רייטינג","ניר ברקת","יואב קיש","יעקב ברדוגו","ינון מגל",
    "גוגל","יוטיוב","מטא","פייסבוק","רגולציה","תרעלה","תבהלה",
    "חוק קרעי","גלית דיסטל","shlomo karhi","i24","הוט","HOT","ישראל בידור","אסף רפפורט",
    "ערוץ 14","מריוס נכט","מירילשוילי"]

# Writer / tag pages. `pattern` = regex an ARTICLE href must match on that domain.
SITES = [
    {"source":"גלובס · נבו טרבלסי","base":"https://www.globes.co.il",
     "url":"https://www.globes.co.il/news/%D7%A0%D7%91%D7%95_%D7%98%D7%A8%D7%91%D7%9C%D7%A1%D7%99.tag",
     "pattern":r"/news/article\.aspx\?did=\d+"},
    {"source":"גלובס · גלית חתן","base":"https://www.globes.co.il",
     "url":"https://www.globes.co.il/news/%D7%92%D7%9C%D7%99%D7%AA_%D7%97%D7%AA%D7%9F.tag",
     "pattern":r"/news/article\.aspx\?did=\d+"},
    {"source":"דה-מרקר · יסמין גואטה","base":"https://www.themarker.com",
     "url":"https://www.themarker.com/ty-WRITER/0000017f-da32-d42c-afff-dff2b5d20000",
     "pattern":r"/[a-z].*/ty-article"},
    {"source":"דה-מרקר · נתי טוקר","base":"https://www.themarker.com",
     "url":"https://www.themarker.com/ty-WRITER/0000017f-da30-d42c-afff-dff2b4680000",
     "pattern":r"/[a-z].*/ty-article"},
    {"source":"הארץ · עידו דוד כהן","base":"https://www.haaretz.co.il",
     "url":"https://www.haaretz.co.il/ty-WRITER/0000017f-da5b-d494-a17f-de5b31520000",
     "pattern":r"/[a-z].*/ty-article"},
]

# Google News RSS searches.
#  unconditional=False → Israel Hayom writer coverage, keyword-filtered, broad window.
#  unconditional=True + recent=True → the spec's Google searches: every new item,
#    no keyword condition, LAST 24h only (when:1d).
SEARCHES = [
    {"source":"ישראל היום · אילי זילברברג","q":"site:israelhayom.co.il אילי זילברברג","unconditional":False},
    {"source":"ישראל היום · ביני אשכנזי","q":"site:israelhayom.co.il ביני אשכנזי","unconditional":False},
    {"source":"ישראל היום · אלינור שירקני קופמן","q":"site:israelhayom.co.il אלינור שירקני קופמן","unconditional":False},
    {"source":"חיפוש גוגל · שלמה קרעי","q":"שלמה קרעי","unconditional":True,"recent":True},
    {"source":"חיפוש גוגל · shlomo karhi (בינלאומי)","q":"shlomo karhi","unconditional":True,"recent":True,"locale":"en"},
    {"source":"חיפוש גוגל · אבי ניר","q":"אבי ניר","unconditional":True,"recent":True},
    {"source":"חיפוש גוגל · דודי ורטהיים","q":"דודי ורטהיים","unconditional":True,"recent":True},
    {"source":"חיפוש גוגל · גלית דיסטל","q":"גלית דיסטל","unconditional":True,"recent":True},
    {"source":"חיפוש גוגל · פטריק דרהי","q":"פטריק דרהי","unconditional":True,"recent":True},
    {"source":"חיפוש גוגל · יפעת בן חי שגב","q":"יפעת בן חי שגב","unconditional":True,"recent":True},

    # Whole-outlet coverage via Google News site: search, keyword-filtered (kind=site).
    # n12 and mako are the SAME system (Keshet) — n12 article links point to mako.co.il —
    # so site:mako.co.il covers both.
    {"source":"n12 / מאקו · חדשות 12","q":"site:mako.co.il (חוק התקשורת OR חוק השידורים OR ערוץ 14 OR ערוץ 12 OR קרעי OR רייטינג OR קשת OR הרשות השנייה)","unconditional":False},
    {"source":"ice · תקשורת","q":"site:ice.co.il (ערוץ 14 OR ערוץ 12 OR קשת OR רייטינג OR חוק השידורים OR קרעי OR חוק התקשורת)","unconditional":False},
    {"source":"ביזפורטל","q":"site:bizportal.co.il (חוק התקשורת OR ערוץ 14 OR קרעי OR רגולציה OR רייטינג OR קשת)","unconditional":False},
    # Calcalist blocks direct scraping (HTTP 403) → route through Google News site: like the rest.
    {"source":"כלכליסט · עומר כביר","q":"site:calcalist.co.il (חוק התקשורת OR חוק השידורים OR ערוץ 14 OR ערוץ 12 OR קרעי OR רייטינג OR קשת OR הרשות השנייה OR מטא OR גוגל)","unconditional":False},
]

def norm_url(u):
    return re.sub(r"^https?://(www\.)?", "https://", u or "").rstrip("/")

def norm_title(t):
    return " ".join((t or "").split())

def matched_keywords(text):
    t = text or ""
    return [k for k in KEYWORDS if k in t]

def get(url):
    global LAST_ERR
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            LAST_ERR = f"HTTP {r.status_code}"
            print(f"  ! {url} -> HTTP {r.status_code}", file=sys.stderr)
            return None
        return r.text
    except Exception as e:
        LAST_ERR = f"{type(e).__name__}: {e}"
        print(f"  ! {url} -> {e}", file=sys.stderr)
        return None

def url_date(url):
    """Real pub date embedded in the URL path (TheMarker/Haaretz: /YYYY-MM-DD/)."""
    m = re.search(r"/(20\d\d-\d\d-\d\d)/", url or "")
    return m.group(1) if m else None

def fetch_pubdate(url):
    """Real pub date from the article page meta tags (Calcalist/Globes)."""
    html = get(url)
    if not html:
        return None
    for pat in (r'property=["\']article:published_time["\'][^>]*content=["\'](20\d\d-\d\d-\d\d)',
                r'content=["\'](20\d\d-\d\d-\d\d)[^"\']*["\'][^>]*property=["\']article:published_time',
                r'"datePublished"\s*:\s*["\'](20\d\d-\d\d-\d\d)',
                r'itemprop=["\']datePublished["\'][^>]*content=["\'](20\d\d-\d\d-\d\d)'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None

def scrape_site(site):
    html = get(site["url"])
    err = None if html else LAST_ERR
    out, local = [], set()
    if html:
        soup = BeautifulSoup(html, "html.parser")
        pat = re.compile(site["pattern"])
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not pat.search(href):
                continue
            title = norm_title(a.get_text(" ", strip=True))
            if len(title) < 15:            # skip nav/thumbnail links with no real title
                continue
            url = href if href.startswith("http") else site["base"] + href
            if url in local:
                continue
            local.add(url)
            kws = matched_keywords(title)
            if not kws:                    # site items require a keyword match
                continue
            d = url_date(url)              # free date if embedded in the URL
            out.append({"title":title,"url":url,"source":site["source"],"type":"site",
                        "date":d or TODAY_S,"keywords":kws,"pv":bool(d)})
    stat = {"source":site["source"],"kind":"site","ok":html is not None,"kept":len(out)}
    if err:
        stat["err"] = err
    RUN_STATS.append(stat)
    print(f"    -> {len(out)} kept", file=sys.stderr)
    return out

def scrape_search(s):
    q = s["q"] + (" when:1d" if s.get("recent") else "")
    loc = ("&hl=en-US&gl=US&ceid=US:en" if s.get("locale") == "en"
           else "&hl=he&gl=IL&ceid=IL:he")
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + loc
    xml = get(url)
    err = None if xml else LAST_ERR
    kind = "google" if s["unconditional"] else "site"   # google-topic search vs whole-outlet
    ok, root, out = xml is not None, None, []
    if ok:
        try:
            root = ET.fromstring(xml)
        except Exception as e:
            print(f"    ! parse error: {e}", file=sys.stderr); ok = False; err = f"parse error: {e}"
    if root is not None:
        for item in root.findall(".//item"):
            title = norm_title(item.findtext("title"))
            src_el = item.find("source")               # Google News appends " - <Publisher>"
            pub = src_el.text.strip() if (src_el is not None and src_el.text) else ""
            for sep in (" - ", " – "):
                if pub and title.endswith(sep + pub):
                    title = title[:-len(sep + pub)].strip(); break
            link = (item.findtext("link") or "").strip()
            if len(title) < 10 or not link.startswith("http"):
                continue
            date = TODAY_S
            pd = item.findtext("pubDate")
            if pd:
                try:
                    date = parsedate_to_datetime(pd).date().isoformat()
                except Exception:
                    pass
            if s.get("recent") and date < RECENT_CUTOFF:   # enforce 24h window
                continue
            kws = matched_keywords(title)
            if not s["unconditional"] and not kws:
                continue
            out.append({"title":title,"url":link,"source":s["source"],"type":kind,
                        "date":date,"keywords":kws,"pv":True,"publisher":pub})
    out = out[:12]                                   # cap per search
    stat = {"source":s["source"],"kind":kind,"ok":ok,"kept":len(out)}
    if not ok and err:
        stat["err"] = err
    RUN_STATS.append(stat)
    print(f"    -> {len(out)} kept", file=sys.stderr)
    return out

def backfill_dates(items):
    """Give every item its real pub date. `pv` (pub-verified) makes this idempotent
    across runs; only unverified items cost a fetch, capped per run."""
    budget = BACKFILL_CAP
    fixed = 0
    for i in items:
        if i.get("pv"):
            continue
        d = url_date(i["url"])
        if not d and i.get("type") == "site" and budget > 0:
            budget -= 1
            d = fetch_pubdate(i["url"])
        if d:
            i["date"] = d
            i["pv"] = True
            fixed += 1
        elif i.get("type") != "site":
            i["pv"] = True            # google/twitter dates are already real
    if fixed:
        print(f"backfilled {fixed} real dates", file=sys.stderr)

def main():
    old = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {"items":[]}
    items = old.get("items", [])
    for i in items:                                  # strip legacy " - Publisher" on old google items
        if i.get("type") == "google":
            i["title"] = re.sub(r"\s+[-–]\s+[^-–]{1,40}$", "", i["title"]).strip()
    seen_urls   = {norm_url(i["url"]) for i in items}
    seen_titles = {norm_title(i["title"]) for i in items}

    found = []
    for site in SITES:
        print(f"site: {site['source']}", file=sys.stderr)
        found += scrape_site(site)
    for s in SEARCHES:
        print(f"search: {s['source']}", file=sys.stderr)
        found += scrape_search(s)

    new = []
    for i in found:
        u, t = norm_url(i["url"]), norm_title(i["title"])
        if u in seen_urls or t in seen_titles:      # dedup by URL and by title
            continue
        seen_urls.add(u); seen_titles.add(t); new.append(i)

    merged = new + items
    backfill_dates(merged)                          # correct real pub dates
    # Google section = last 24h only: drop stale google items (incl. old residue).
    before = len(merged)
    merged = [i for i in merged
              if not (i.get("type") == "google" and str(i.get("date","")) < RECENT_CUTOFF)]
    if before != len(merged):
        print(f"purged {before-len(merged)} stale google items (>24h)", file=sys.stderr)
    merged = [i for i in merged if i.get("type") != "twitter"]        # X removed from sources
    valid_sources = {s["source"] for s in SITES} | {s["source"] for s in SEARCHES}
    before3 = len(merged)
    merged = [i for i in merged if i.get("source") in valid_sources]  # purge orphaned-source items
    if before3 != len(merged):
        print(f"purged {before3-len(merged)} items from removed/renamed sources", file=sys.stderr)
    before2 = len(merged)
    merged = [i for i in merged if str(i.get("date","")) >= AGE_CUTOFF]   # drop old items
    if before2 != len(merged):
        print(f"purged {before2-len(merged)} items older than {MAX_AGE_DAYS}d", file=sys.stderr)
    merged.sort(key=lambda i: str(i.get("date","")), reverse=True)   # newest first
    seen_u, seen_t, dedup = set(), set(), []                          # collapse any url/title dups
    for i in merged:
        u, t = norm_url(i["url"]), norm_title(i["title"])
        if u in seen_u or t in seen_t:
            continue
        seen_u.add(u); seen_t.add(t); dedup.append(i)
    merged = dedup[:400]                                              # hard cap

    # Full source manifest — so the hub can show EVERY monitored source (incl. 0).
    sources = ([{"source":s["source"],"kind":"site","active":True} for s in SITES]
             + [{"source":s["source"],"kind":("google" if s.get("recent") else "site"),
                 "active":True} for s in SEARCHES])

    out = {"updated": datetime.datetime.now().astimezone().isoformat(timespec="minutes"),
           "keywords": KEYWORDS, "sources": sources, "diag": RUN_STATS, "items": merged}
    DATA.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"added {len(new)} new; total {len(merged)}", file=sys.stderr)

if __name__ == "__main__":
    main()
