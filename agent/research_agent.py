"""Wireless Market Research Agent  (Wi-Fi / Bluetooth / short-range wireless)

Runs unattended (nightly via Windows Task Scheduler or GitHub Actions) and:
  1. Searches PUBLIC news feeds (Google News RSS in several regional editions + Bing News RSS)
     for every tracked company, application and industry topic.
  2. Filters for relevance, de-duplicates, clusters identical stories, tags themes
     (Wi-Fi 7/8, LE Audio, M&A, launches ...), detects regions and scores importance.
  3. Extracts public market-size forecasts (USD value, target year, CAGR) from press
     releases and builds 10-year projections for each application.
  4. Writes JS data files consumed by the static website in ../site (works from file://
     or any static web host). Every item keeps its clickable source URL.

Usage:
    python research_agent.py              # full nightly run
    python research_agent.py --quick      # small smoke-test run
Optional env vars for an LLM-written narrative (otherwise a deterministic summary is built):
    LLM_API_KEY, LLM_BASE_URL (default https://api.openai.com/v1), LLM_MODEL (default gpt-4o-mini)
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import random
import re
import statistics
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

import requests

from catalog_applications import APPLICATIONS, EDITIONS, REGIONAL_TOPICS, TOPICS
from catalog_companies import CATEGORIES, COMPANIES, REGIONS

ROOT = Path(__file__).resolve().parent
SITE = ROOT.parent / "site"
DATA = SITE / "data"
STATE = ROOT / "state"
NOW = datetime.now(timezone.utc)
WINDOW_DAYS = 30
KEEP_DAYS = 40
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 WirelessResearchAgent/1.0")

LOG_LINES: list[str] = []
PERSIST = ("id", "title", "url", "desc", "publisher", "domain", "published", "via", "edition", "companies", "apps",
           "topics", "found_by", "app", "cagr", "target_value_b", "target_year", "base_value_b", "base_year",
           "start_value_b", "start_year", "snippet", "kind")


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


# ----------------------------------------------------------------------------- HTTP
class Fetcher:
    """Polite fetcher: per-host pacing, retries with back-off."""

    GAP = {"news.google.com": 0.6, "www.bing.com": 0.45}

    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})
        self.locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.last: dict[str, float] = defaultdict(float)
        self.stats = Counter()

    def get(self, url: str) -> str | None:
        host = urlparse(url).netloc
        for attempt in range(4):
            with self.locks[host]:
                wait = self.GAP.get(host, 0.3) - (time.time() - self.last[host])
                if wait > 0:
                    time.sleep(wait + random.random() * 0.2)
                self.last[host] = time.time()
            try:
                r = self.s.get(url, timeout=25)
                if r.status_code == 200:
                    self.stats[host + ":ok"] += 1
                    return r.text
                self.stats[host + f":{r.status_code}"] += 1
                if r.status_code in (429, 500, 502, 503):
                    time.sleep(4 * (attempt + 1) ** 2)
                    continue
                return None
            except requests.RequestException:
                self.stats[host + ":err"] += 1
                time.sleep(2 * (attempt + 1))
        return None


def gnews_url(q: str, ed: str = "US", window: str = "30d") -> str:
    hl, gl, ceid, _ = EDITIONS[ed]
    full = f"{q} when:{window}" if window else q
    return f"https://news.google.com/rss/search?q={quote_plus(full)}&hl={hl}&gl={gl}&ceid={quote_plus(ceid)}"


def bing_url(q: str, interval: int | None = 9) -> str:
    # interval 7 = 24h, 8 = week, 9 = month (Bing News filter)
    u = f"https://www.bing.com/news/search?q={quote_plus(q)}&format=rss&mkt=en-US&setlang=en"
    if interval:
        u += f"&qft=interval%3d%22{interval}%22"
    return u


# ----------------------------------------------------------------------------- parsing
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean(t: str | None) -> str:
    if not t:
        return ""
    return WS_RE.sub(" ", TAG_RE.sub(" ", html.unescape(t))).strip()


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def domain_of(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


def parse_rss(xml_text: str, via: str, edition: str) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = clean(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        pub = parse_date(it.findtext("pubDate"))
        if not title or not link or not pub:
            continue
        desc = ""
        publisher, pub_url = "", ""
        if via == "google":
            src = it.find("source")
            if src is not None:
                publisher = clean(src.text)
                pub_url = src.get("url", "")
            if publisher and title.endswith(" - " + publisher):
                title = title[: -(len(publisher) + 3)].strip()
            elif " - " in title:
                head, tail = title.rsplit(" - ", 1)
                if len(tail) < 45:
                    title, publisher = head.strip(), publisher or tail.strip()
            pdomain = domain_of(pub_url) if pub_url else ""
        else:  # bing
            desc = clean(it.findtext("description"))[:600]
            qs = parse_qs(urlparse(link).query)
            if "url" in qs:
                link = qs["url"][0]
            for child in it:
                if child.tag.endswith("Source"):
                    publisher = clean(child.text)
            pdomain = domain_of(link)
            if not publisher:
                publisher = pdomain
        out.append(dict(title=title, url=link, desc=desc, publisher=publisher or pdomain,
                        domain=pdomain, published=pub, via=via, edition=edition))
    return out


# ----------------------------------------------------------------------------- taxonomy
THEMES = [
    ("wifi8", "Wi-Fi 8 / UHR", r"wi-?fi ?8\b|802\.11bn|ultra high reliability", 2.5),
    ("wifi7", "Wi-Fi 7", r"wi-?fi ?7\b|802\.11be|wifi7", 1.5),
    ("wifi6", "Wi-Fi 6 / 6E", r"wi-?fi ?6e?\b|802\.11ax", 0.7),
    ("halow", "Wi-Fi HaLow", r"halow|802\.11ah", 1.5),
    ("sensing", "Wi-Fi Sensing", r"wi-?fi sensing|802\.11bf|wifi motion|wi-fi motion", 1.5),
    ("spectrum", "Spectrum & Regulation", r"6 ?ghz|spectrum|\bfcc\b|ofcom|\bafc\b|regulat|\bmiit\b|\bbnetza\b", 1.5),
    ("leaudio", "LE Audio / Auracast", r"le audio|auracast|\blc3\b", 1.5),
    ("cs", "Bluetooth Channel Sounding", r"channel sounding", 1.8),
    ("bluetooth", "Bluetooth", r"bluetooth|\bble\b|蓝牙|藍牙", 0.6),
    ("wifi", "Wi-Fi (general)", r"wi-?fi|wlan|wireless lan|无线局域网", 0.4),
    ("uwb", "UWB", r"\buwb\b|ultra-?wideband", 1.2),
    ("matter", "Matter / Thread / Zigbee", r"\bmatter\b|\bthread\b|zigbee|z-wave", 1.0),
    ("nearlink", "NearLink / SparkLink", r"nearlink|sparklink|星闪", 1.5),
    ("ai", "AI / Edge AI", r"\bai\b|artificial intelligence|edge ai|genai|\bllm\b|machine learning|neural|人工智能|\bnpu\b", 1.0),
    ("mna", "M&A / Divestiture", r"acqui|merger|\bmerge|takeover|buyout|divest|to buy\b|stake in|收购|并购", 3.0),
    ("partnership", "Partnership", r"partner|collaborat|teams up|alliance with|joins forces|合作|strategic agreement", 1.5),
    ("launch", "Product Launch", r"launch|unveil|introduc|debut|announc|rolls? out|releases|showcas|发布|推出|上市", 1.2),
    ("earnings", "Earnings & Financials", r"earnings|revenue|quarter|\bq[1-4]\b|results|guidance|profit|outlook|财报|营收|业绩", 1.0),
    ("funding", "Funding & IPO", r"funding|raises \$|series [a-e]\b|\bipo\b|venture|融资", 1.5),
    ("legal", "Legal, Patents & Trade", r"lawsuit|patent|\bsues?\b|litigation|antitrust|tariff|export control|sanction|\bban\b|security flaw|vulnerab|cve-", 1.8),
    ("wearables", "Wearables & Hearables", r"smart ?watch|apple watch|galaxy watch|pixel watch|watch (gt|ultra|series|fit|\d)|"
     r"earbud|wearable|smart ring|oura ring|galaxy ring|glasses|headphone|hearable|hearing aid|airpods|\bbuds\b|耳机|手表|眼镜|手环", 0.8),
    ("smarthome", "Smart Home & IoT", r"smart home|home automation|\biot\b|internet of things|smart appliance|物联网", 0.8),
    ("automotive", "Automotive", r"automotive|vehicle|\bcar\b|digital key|\bev\b|汽车", 0.8),
    ("xr", "XR / AR / VR", r"\bxr\b|\bar\b glasses|augmented reality|virtual reality|mixed reality|\bvr\b|headset", 0.8),
    ("chips", "Chips & Silicon", r"\bchip|\bsoc\b|semiconductor|silicon|芯片|晶片|半导体", 0.6),
]
THEME_RE = [(k, lbl, re.compile(p, re.I), w) for k, lbl, p, w in THEMES]
THEME_LABEL = {k: lbl for k, lbl, _, _ in THEMES}

WIRELESS_RE = re.compile(r"wi-?fi|wlan|bluetooth|\bble\b|wireless|\buwb\b|ultra-?wideband|matter\b|thread\b|zigbee|halow|"
                         r"auracast|le audio|nearlink|sparklink|802\.11|router|mesh|earbud|headphone|smartwatch|"
                         r"smart glasses|ai glasses|wearable|蓝牙|藍牙|星闪|无线|無線|connectivity|6 ?ghz|access point", re.I)
WIFI_RE = re.compile(r"wi-?fi|wlan|802\.11|halow|router|access point|mesh wi|6 ?ghz|無線lan", re.I)
BT_RE = re.compile(r"bluetooth|\bble\b|le audio|auracast|channel sounding|蓝牙|藍牙|earbud|headphone|hearable|hearing aid", re.I)
OTHER_RE = re.compile(r"\buwb\b|ultra-?wideband|matter\b|thread\b|zigbee|z-wave|nearlink|sparklink|星闪|\bnfc\b", re.I)
BIZ_RE = re.compile(r"acqui|merger|earnings|revenue|tariff|funding|ipo\b|stake|lawsuit|quarter|guidance|收购|财报", re.I)
# Shopping-deal / investor-clickbait headlines carry no market-research signal
NOISE_RE = re.compile(r"deals? of the day|best deals?\b|early black friday|black friday|cyber monday|prime day|prime big deal|"
                      r"\d+% off|\$\d+ off|price cut|price drop|lowest price|record low|all-time low|on sale\b|\bsave \$|\bsave \d+%|"
                      r"\bsave up to|coupon|\bdiscount|deal alert|^deal:|\bdeal:|grab the|bargain|stocks? to buy|should you buy|"
                      r"better buy|could produce|millionaire|buy the dip|is it time to buy|gift guide|holiday gift", re.I)
# Headline must look wireless / device / silicon related for companies whose news is mostly about other things
DEVICE_RE = re.compile(r"airpods|watch|earbud|\bbuds\b|headphone|headset|glasses|wearable|\bring\b|tracker|airtag|smarttag|"
                       r"speaker|soundbar|\becho\b|alexa|\bnest\b|smart home|home hub|\bhome\b|thermostat|doorbell|camera|router|"
                       r"mesh|eero|\btv\b|streamer|chip|silicon|modem|\bsoc\b|iphone|pixel|galaxy|phone|tablet|ipad|vision pro|"
                       r"quest|\bxr\b|\bvr\b|\bar\b|robot|device|gadget|console|controller|switch 2|playstation|xbox|ai pc|"
                       r"copilot\+|\bband\b|fitness|health|sensor|kindle|fire tv|sidewalk|iot|edge ai|hardware|"
                       r"蓝牙|耳机|手表|眼镜|手环|路由|芯片|音箱|智能|星闪|穿戴", re.I)
SEGMENT_REL = {
    "oem": DEVICE_RE, "ai": DEVICE_RE,
    "chip": re.compile(r"\biot\b|\bmcus?\b|microcontroller|connectivity|edge ai|module|sensor|\bsoc\b|chip|launch|introduc|"
                       r"unveil|acqui|automotive|radar|smart home|wearable|matter|processor|platform|design win|"
                       r"earnings|revenue|quarter|results|guidance", re.I),
    "module": re.compile(r"\biot\b|module|\brf\b|connectivity|antenna|front-end|filter|launch|introduc|acqui|merger|"
                         r"earnings|revenue|quarter|results", re.I),
    "ipsw": re.compile(r"\biot\b|licens|\bip\b|platform|software|launch|acqui|partner|earnings|revenue", re.I),
    "test": re.compile(r"\brf\b|\b[56]g\b|test|signal|conformance|certif|802|emulat|validation", re.I),
    "infra": re.compile(r"access point|campus|meraki|aruba|\bmist\b|catalyst|broadband|gateway|fib(re|er)|router|\blan\b|"
                        r"networking|\bnaas\b|ai-native|aiops|cpe", re.I),
    "std": None,
}
# Fund-holding filings & analyst chatter (MarketBeat-style) — not market intelligence
FIN_NOISE_RE = re.compile(r"shares (sold|bought|purchased|acquired) by|acquires? (new )?(shares|position)|"
                          r"(raises|lowers|trims|boosts|cuts|increases|reduces|grows|sells|buys) (its |her |his )?(stake|position|holdings)|"
                          r"position in .*\b(inc|corp|ltd|plc)\b|shares gap (up|down)|price target|analysts? (upgrade|downgrade)|"
                          r"brokers are|short interest|insider (selling|buying|sale|sold|bought)|share sale is proposed|officer .* sale|"
                          r"sale is proposed|reports? \d+(\.\d+)?% of|ex-dividend|technical analysis|stock forecast|price prediction|"
                          r"is it a buy|stock (rating|upgraded|downgraded)|(upgraded|downgraded) (to|by)|given (a )?consensus|"
                          r"average rating|research analysts|options (activity|traders)|unusual options|institutional investors", re.I)

PRESS_WIRES = {"businesswire.com", "prnewswire.com", "globenewswire.com", "accesswire.com", "newsfilecorp.com",
               "einpresswire.com", "prweb.com", "newswire.ca", "newswire.com", "eqs-news.com", "cision.com",
               "Business Wire", "PR Newswire", "GlobeNewswire", "ACCESSWIRE", "Newsfile", "EIN Presswire", "PRWeb"}
TIER1 = {"reuters", "bloomberg", "wsj", "wall street journal", "financial times", "ft.com", "cnbc", "the verge", "techcrunch",
         "ee times", "eetimes", "nikkei", "digitimes", "electronics weekly", "fierce", "light reading", "the information",
         "9to5mac", "9to5google", "android authority", "tom's hardware", "engadget", "wired", "ars technica", "zdnet",
         "the register", "caixin", "scmp", "south china morning post", "barron", "forbes", "associated press", "ap news",
         "heise", "golem", "computerbase", "mobile world live", "rcr wireless", "sdxcentral", "embedded.com", "eenews",
         "semiconductor engineering", "trendforce", "counterpoint", "idc", "canalys"}
ANNOUNCE_RE = re.compile(r"\b(announc\w*|launch\w*|introduc\w*|unveil\w*|debut\w*|expands?|partners?|selects?|completes?|"
                         r"releases?|showcases?|demonstrat\w*|achiev\w*|certif\w*|wins?|appoints?|names?|joins?|signs?|teams|"
                         r"collaborat\w*|acquir\w*|ships?|delivers?|enables?|powers?|brings?|adds?|opens?|invests?)\b", re.I)
SIGNAL_RE = re.compile(r"shipments?|market share|forecast|\bgrew\b|growth|declin|\bidc\b|counterpoint|canalys|omdia|"
                       r"trendforce|gartner|abi research|strategy analytics|circana|\bsales\b|outlook|demand", re.I)

STOP = set("a an the of for and or to in on with by at from as is are be its it this that new how why what who will can "
           "into over after vs via amid more than up out about – - — | : , . 's".split())
TLD_REGION = {
    "cn": "China", "hk": "China", "tw": "APAC", "jp": "APAC", "kr": "APAC", "in": "APAC", "sg": "APAC", "au": "APAC",
    "my": "APAC", "id": "APAC", "vn": "APAC", "th": "APAC", "ph": "APAC", "nz": "APAC",
    "uk": "Europe", "de": "Europe", "fr": "Europe", "it": "Europe", "es": "Europe", "nl": "Europe", "se": "Europe",
    "no": "Europe", "fi": "Europe", "dk": "Europe", "ch": "Europe", "at": "Europe", "be": "Europe", "pl": "Europe",
    "ie": "Europe", "pt": "Europe", "cz": "Europe", "eu": "Europe", "gr": "Europe", "hu": "Europe", "ro": "Europe",
    "ca": "Americas", "us": "Americas", "br": "Americas", "mx": "Americas", "ar": "Americas", "cl": "Americas",
}
AMBIG = {"Ring", "Echo", "Nest", "Hue", "Matter", "Bluetooth", "UWB", "Pixel", "Galaxy", "Snap", "Quest", "Specs",
         "Plume", "Honor", "HONOR", "vivo", "Vivo", "Nothing", "Origin", "Windows", "Android", "Alexa", "Kindle", "Blink",
         "Logi", "Poly", "Legion", "Mist", "WiZ", "Quark", "Spectacles", "Ray-Ban", "Fire TV", "Aruba", "Juniper",
         "Actions", "AIC", "ASR", "BES", "CSA", "WBA", "IEEE", "802.11", "GN", "AVM", "FRITZ", "Even", "rabbit", "r1",
         "Rabbit", "PICO", "Pico", "Laird", "Extreme", "Ceva", "CEVA", "Auracast", "TI ", "ST ", "Motorola", "Cognitive Systems",
         "HyperX", "Origin", "Xbox", "Surface", "Bang", "B&O", "OpenRoaming", "FiRa", "Hisilicon", "Tapo", "Eufy", "eufy",
         "ThinkPad", "Redmi", "OnePlus", "Specs", "Galaxy", "ByteDance", "Jony Ive", "Carl Pei", "CMF", "SteelSeries", "JBL",
         "Amazfit", "Zepp", "Nordic", "Harman", "Meta", "HP", "LG", "Snap", "ADI"}


def norm_tokens(title: str) -> set[str]:
    t = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", title.lower())
    toks = [w for w in t.split() if w not in STOP and len(w) > 1]
    # CJK titles: use character bigrams
    if any("\u4e00" <= ch <= "\u9fff" for ch in title):
        s = re.sub(r"\s+", "", title)
        toks += [s[i:i + 2] for i in range(len(s) - 1)]
    return set(toks)


def art_id(title: str) -> str:
    key = re.sub(r"[^\w\u4e00-\u9fff]", "", title.lower())[:120]
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:14]


def alias_regex(aliases: list[str], case_sensitive: bool) -> re.Pattern | None:
    """Ambiguous brand words (Ring, Echo, Nest, Honor...) are always matched case-sensitively."""
    parts = []
    for a in aliases:
        a = a.strip()
        if not a:
            continue
        if re.search(r"[\u4e00-\u9fff]", a):
            p = re.escape(a)
        else:
            p = r"(?<![\w-])" + re.escape(a) + r"(?![\w])"
        if not case_sensitive and a in AMBIG:
            p = "(?-i:" + p + ")"
        parts.append(p)
    if not parts:
        return None
    return re.compile("|".join(parts), 0 if case_sensitive else re.I)


def base_name(name: str) -> str:
    return re.sub(r"\s*\(.*?\)\s*", " ", name).strip()


def company_aliases(c: dict) -> tuple[list[str], list[str]]:
    al = list(c.get("aliases") or [])
    b = base_name(c["name"])
    al.append(b)
    for suf in (" Semiconductor", " Semiconductors", " Technologies", " Technology", " Electronics", " Systems",
                " Microelectronics", " Manufacturing", " Networks", " Inc.", " Platforms", " Group", " Solutions",
                " Innovations", " Micro"):
        if b.endswith(suf) and len(b) - len(suf) >= 4:
            al.append(b[: -len(suf)])
    al = list(dict.fromkeys(al))
    strict = [a for a in al if a not in AMBIG and len(a.strip()) >= 4]
    if b not in AMBIG and b not in strict and len(b) >= 3:
        strict.append(b)
    return al, strict


# ----------------------------------------------------------------------------- forecasts
MONEY1 = re.compile(r"(?:USD|US\$|\$|US Dollars?)\s?(\d{1,4}(?:[.,]\d{1,3})?)\s?(trillion|billion|bn|million|mn|mln|b|m)\b", re.I)
MONEY2 = re.compile(r"(\d{1,4}(?:\.\d{1,3})?)\s?(trillion|billion|million)\s?(?:USD|US dollars|dollars)", re.I)
YEAR_AFTER = re.compile(r"^.{0,45}?\b(?:by|in|till|until|through|to|for|at the end of|end of)\s+(?:the\s+)?(?:year\s+)?(20[2-4]\d)", re.I)
YEAR_BEFORE = re.compile(r"(?:in|by|for)\s+(20[2-4]\d)\b[^.]{0,30}$", re.I)
CAGR_RE = [re.compile(r"CAGR[^0-9%]{0,30}?(\d{1,2}(?:\.\d{1,2})?)\s?%", re.I),
           re.compile(r"(\d{1,2}(?:\.\d{1,2})?)\s?%\s*(?:CAGR|compound annual)", re.I),
           re.compile(r"compound annual growth rate[^0-9%]{0,30}?(\d{1,2}(?:\.\d{1,2})?)\s?%", re.I)]
UNIT = {"trillion": 1000, "billion": 1, "bn": 1, "b": 1, "million": 0.001, "mn": 0.001, "mln": 0.001, "m": 0.001}
BASE_YEAR = NOW.year


def extract_forecast(text: str) -> dict | None:
    """Pull (value, year, CAGR) from a public market-forecast headline/snippet.
    Returns kind = full (value+year+CAGR) | cagr (CAGR only) | value (value+target year only)."""
    t = text.replace("\u00a0", " ")
    cagr = None
    for rx in CAGR_RE:
        m = rx.search(t)
        if m:
            cagr = float(m.group(1)) / 100
            break
    pairs = []
    for rx in (MONEY1, MONEY2):
        for m in rx.finditer(t):
            before = t[max(0, m.start() - 50): m.start()]
            if re.search(r"(grow|increase|expand|rise)\s+by\s*$|opportunit|incremental", before, re.I):
                continue  # incremental growth figure, not market size
            try:
                raw = m.group(1)
                v = float(raw.replace(",", ".") if raw.count(",") == 1 and "." not in raw and len(raw.split(",")[1]) != 3
                          else raw.replace(",", ""))
            except ValueError:
                continue
            v *= UNIT[m.group(2).lower()]
            after = t[m.end(): m.end() + 60]
            ya = YEAR_AFTER.search(after)
            yb = YEAR_BEFORE.search(before)
            y = int(ya.group(1)) if ya else (int(yb.group(1)) if yb else None)
            if y and v > 0 and 2018 <= y <= 2045:
                pairs.append((v, y))
    pairs = sorted(set(pairs), key=lambda p: p[1])
    tv = ty = None
    if pairs and pairs[-1][1] >= BASE_YEAR:
        tv, ty = pairs[-1]
    if cagr is None and tv and len(pairs) >= 2 and pairs[0][1] < ty and pairs[0][0] > 0:
        cagr = (tv / pairs[0][0]) ** (1 / (ty - pairs[0][1])) - 1
    if cagr is not None and not (0.005 < cagr < 0.7):
        cagr = None
    if cagr is None and tv is None:
        return None
    kind = "full" if (cagr is not None and tv) else ("cagr" if cagr is not None else "value")
    base = tv / ((1 + cagr) ** (ty - BASE_YEAR)) if kind == "full" else None
    if base is not None and not (0.0005 < base < 1500):
        return None
    return dict(kind=kind, cagr=round(cagr * 100, 2) if cagr is not None else None,
                target_value_b=round(tv, 3) if tv else None, target_year=ty,
                base_value_b=round(base, 3) if base else None, base_year=BASE_YEAR,
                start_value_b=round(pairs[0][0], 3) if len(pairs) > 1 else None,
                start_year=pairs[0][1] if len(pairs) > 1 else None)


# ----------------------------------------------------------------------------- jobs
def build_jobs(quick: bool) -> list[dict]:
    jobs = []
    comps = COMPANIES[:8] if quick else COMPANIES
    apps = APPLICATIONS[:3] if quick else APPLICATIONS
    topics = TOPICS[:3] if quick else TOPICS
    for c in comps:
        b = base_name(c["name"])
        q = c.get("q") or (f'"{b}"' if c.get("broad") else f'"{b}" (Wi-Fi OR WiFi OR Bluetooth OR wireless OR chip)')
        jobs.append(dict(kind="company", eid=c["id"], url=gnews_url(q), via="google", edition="US"))
        jobs.append(dict(kind="company", eid=c["id"], url=bing_url(q), via="bing", edition="US"))
        if c.get("region") == "Europe" and not quick:
            jobs.append(dict(kind="company", eid=c["id"], url=gnews_url(q, "UK"), via="google", edition="UK"))
        if c.get("zh"):
            jobs.append(dict(kind="company", eid=c["id"], url=gnews_url(c["zh"], "CN"), via="google", edition="CN"))
    for a in apps:
        jobs.append(dict(kind="app", eid=a["id"], url=gnews_url(a["q"]), via="google", edition="US"))
        jobs.append(dict(kind="app", eid=a["id"], url=bing_url(a["q"]), via="bing", edition="US"))
        alts = [m.strip() for m in a["mkt"].split("|")][:4]
        gq = " OR ".join(f'"{m} market"' for m in alts)
        jobs.append(dict(kind="forecast", eid=a["id"], url=gnews_url(f"({gq}) (CAGR OR billion)", window="1y"), via="google", edition="US"))
        jobs.append(dict(kind="forecast", eid=a["id"], url=bing_url(f'"{alts[0]} market" CAGR', None), via="bing", edition="US"))
        jobs.append(dict(kind="forecast", eid=a["id"], url=bing_url(f'{alts[-1] if len(alts) > 1 else alts[0]} market size CAGR billion', None), via="bing", edition="US"))
    for tid, _lbl, q, _b in topics:
        jobs.append(dict(kind="topic", eid=tid, url=gnews_url(q), via="google", edition="US"))
        jobs.append(dict(kind="topic", eid=tid, url=bing_url(q, 8), via="bing", edition="US"))
    if not quick:
        for ed, q, _b in REGIONAL_TOPICS:
            jobs.append(dict(kind="topic", eid=f"regional-{ed}", url=gnews_url(q, ed, "14d"), via="google", edition=ed))
    return jobs


# ----------------------------------------------------------------------------- main pipeline
class Agent:
    def __init__(self, quick: bool = False) -> None:
        self.quick = quick
        self.fetch = Fetcher()
        self.companies = {c["id"]: c for c in COMPANIES}
        self.apps = {a["id"]: a for a in APPLICATIONS}
        for a in APPLICATIONS:
            alts = [re.sub(r"[\s\-/]+", r"(?:\\s*\\([^)]{1,15}\\))?[\\s\\-/]*", re.escape(m.strip()).replace(r"\ ", " ").replace(r"\-", "-").replace(r"\/", "/"))
                    for m in a["mkt"].split("|")]
            a["_mkt_rx"] = re.compile(r"(?<![\w-])(?:" + "|".join(alts) + r")s?(?:\s*\([^)]{1,20}\))?[\s\-]+(?:market|industry)\b", re.I)
        self.alias_any: dict[str, re.Pattern] = {}
        self.alias_strict: dict[str, re.Pattern] = {}
        for cid, c in self.companies.items():
            al, strict = company_aliases(c)
            c["_aliases"], c["_strict"] = al, strict
            self.alias_any[cid] = alias_regex(al, False)
            rx = alias_regex(strict, True)
            if rx:
                self.alias_strict[cid] = rx
        self.store: dict[str, dict] = {}
        self.forecasts: dict[str, dict] = {}
        self.fc_pending: list[tuple[str, dict]] = []
        self.job_log: list[dict] = []

    # ---- state
    def load_state(self) -> None:
        for name, target in (("articles.json", "store"), ("forecasts.json", "forecasts")):
            p = STATE / name
            if p.exists():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    for k, v in raw.items():
                        v["published"] = datetime.fromisoformat(v["published"])
                        for key in ("companies", "apps", "topics", "found_by"):
                            if key in v:
                                v[key] = set(v[key])
                    setattr(self, target, raw)
                except Exception as e:  # corrupted state should never stop the nightly run
                    log(f"WARN could not load {name}: {e}")
        log(f"Loaded state: {len(self.store)} articles, {len(self.forecasts)} forecasts")

    def save_state(self) -> None:
        STATE.mkdir(parents=True, exist_ok=True)

        def ser(d):
            o = {k: d[k] for k in PERSIST if k in d}
            o["published"] = d["published"].isoformat()
            for key in ("companies", "apps", "topics", "found_by"):
                if key in o:
                    o[key] = sorted(o[key])
            return o
        cutoff = NOW - timedelta(days=KEEP_DAYS)
        arts = {k: ser(v) for k, v in self.store.items() if v["published"] >= cutoff}
        fc_cut = NOW - timedelta(days=400)
        fcs = {k: ser(v) for k, v in self.forecasts.items() if v["published"] >= fc_cut}
        (STATE / "articles.json").write_text(json.dumps(arts, ensure_ascii=False), encoding="utf-8")
        (STATE / "forecasts.json").write_text(json.dumps(fcs, ensure_ascii=False), encoding="utf-8")

    # ---- crawl
    def crawl(self) -> None:
        jobs = build_jobs(self.quick)
        log(f"Crawling {len(jobs)} public feed queries ...")
        t0 = time.time()
        results = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(self.fetch.get, j["url"]): j for j in jobs}
            for i, f in enumerate(as_completed(futs), 1):
                j = futs[f]
                txt = None
                try:
                    txt = f.result()
                except Exception:
                    pass
                items = parse_rss(txt, j["via"], j["edition"]) if txt else []
                self.job_log.append(dict(kind=j["kind"], eid=j["eid"], via=j["via"], edition=j["edition"],
                                         url=j["url"], ok=txt is not None, items=len(items)))
                results.append((j, items))
                if i % 50 == 0:
                    log(f"  {i}/{len(jobs)} queries done")
        log(f"Crawl finished in {time.time() - t0:.0f}s; fetch stats {dict(self.fetch.stats)}")
        for j, items in results:
            self.ingest(j, items)
        self.read_forecast_pages()

    def read_forecast_pages(self) -> None:
        """For forecast headlines without numbers, open the public press-release page and read the figures
        that sit right next to '<application> market' (max 4 pages per application)."""
        per_app, todo, seen = Counter(), [], set()
        known = {f["title"] for f in self.forecasts.values()}
        for aid, it in self.fc_pending:
            if it["url"] in seen or it["title"] in known or per_app[aid] >= 4:
                continue
            seen.add(it["url"])
            per_app[aid] += 1
            todo.append((aid, it))
        if not todo:
            return

        def work(pair):
            aid, it = pair
            try:
                r = self.fetch.s.get(it["url"], timeout=12)
                if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                    return None
                txt = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text[:400000])
                txt = clean(txt)[:60000]
                for m in self.apps[aid]["_mkt_rx"].finditer(txt):
                    fc = extract_forecast(txt[m.start(): m.start() + 450])
                    if fc:
                        return aid, it, fc, txt[m.start(): m.start() + 300]
            except Exception:
                return None
            return None
        got = 0
        with ThreadPoolExecutor(max_workers=6) as ex:
            for res in ex.map(work, todo):
                if res:
                    aid, it, fc, snip = res
                    it = dict(it, desc=it["desc"] or snip)
                    self.add_forecast(self.apps[aid], it, fc)
                    got += 1
        log(f"Read {len(todo)} public forecast pages, extracted {got} additional forecasts")

    def ingest(self, job: dict, items: list[dict]) -> None:
        cutoff = NOW - timedelta(days=WINDOW_DAYS + 1)
        for it in items:
            if job["kind"] == "forecast":
                self.ingest_forecast(job, it)
                continue
            if it["published"] < cutoff or it["published"] > NOW + timedelta(hours=12):
                continue
            if NOISE_RE.search(it["title"]) or FIN_NOISE_RE.search(it["title"]):
                continue
            if job["kind"] == "company":
                rx = self.alias_any.get(job["eid"])
                if rx and not rx.search(it["title"]):
                    continue  # relevance guard: company must be named in the headline
                if not self.relevant(job["eid"], it["title"]):
                    continue
            aid = art_id(it["title"])
            a = self.store.get(aid)
            if a is None:
                a = dict(id=aid, title=it["title"], url=it["url"], desc=it["desc"], publisher=it["publisher"],
                         domain=it["domain"], published=it["published"], via=it["via"], edition=it["edition"],
                         companies=set(), apps=set(), topics=set(), found_by=set())
                self.store[aid] = a
            else:
                # prefer direct publisher URLs (Bing) over aggregator redirects, keep richer description
                if a.get("via") == "google" and it["via"] == "bing":
                    a["url"], a["domain"], a["via"] = it["url"], it["domain"], "bing"
                if not a.get("desc") and it["desc"]:
                    a["desc"] = it["desc"]
                if it["published"] < a["published"]:
                    a["published"] = it["published"]
            a["found_by"].add(f'{job["kind"]}:{job["eid"]}')
            if job["kind"] == "company":
                a["companies"].add(job["eid"])
            elif job["kind"] == "app":
                a["apps"].add(job["eid"])
            elif job["kind"] == "topic":
                a["topics"].add(job["eid"])

    def relevant(self, cid: str, title: str) -> bool:
        """Wireless pure-plays: everything counts. Diversified companies: the headline itself must be
        about wireless, or about the segment-specific business that matters to wireless marketers."""
        c = self.companies[cid]
        if c.get("broad") or WIRELESS_RE.search(title):
            return True
        rx = SEGMENT_REL.get(c["cat"])
        return bool(rx and rx.search(title))

    def ingest_forecast(self, job: dict, it: dict) -> None:
        app = self.apps[job["eid"]]
        text = f'{it["title"]}. {it["desc"]}'
        # the forecast must be about THIS application's market: "<phrase> [(abbr)] market|industry" in the headline
        if not app["_mkt_rx"].search(it["title"]):
            return
        if it["published"] < NOW - timedelta(days=400):
            return
        fc = extract_forecast(text)
        if not fc:
            if it["via"] == "bing" and not it["url"].startswith("https://news.google"):
                self.fc_pending.append((app["id"], it))
            return
        self.add_forecast(app, it, fc)

    def add_forecast(self, app: dict, it: dict, fc: dict) -> None:
        fid = art_id(it["title"]) + "-" + app["id"]
        fc.update(id=fid, app=app["id"], title=it["title"], url=it["url"], publisher=it["publisher"],
                  domain=it["domain"], published=it["published"], snippet=it["desc"][:300])
        self.forecasts[fid] = fc

    # ---- enrich
    def enrich(self) -> None:
        cutoff = NOW - timedelta(days=WINDOW_DAYS)
        self.window = [a for a in self.store.values() if a["published"] >= cutoff and not NOISE_RE.search(a["title"]) and not FIN_NOISE_RE.search(a["title"])]
        for a in self.window:
            text = f'{a["title"]} {a.get("desc", "")}'
            title = a["title"]
            from_wireless_feed = any(f.startswith(("topic:", "app:")) for f in a["found_by"])
            # re-validate stored associations with the current rules (state may come from older runs)
            a["companies"] = {cid for cid in a["companies"] if cid in self.companies and
                              (self.alias_any[cid] is None or self.alias_any[cid].search(title)) and
                              (from_wireless_feed or self.relevant(cid, title))}
            for cid, rx in self.alias_strict.items():
                if rx.search(title) and (from_wireless_feed or self.relevant(cid, title)):
                    a["companies"].add(cid)
            a["themes"] = [k for k, _l, rx, _w in THEME_RE if rx.search(text)]
            buckets = []
            if WIFI_RE.search(text):
                buckets.append("Wi-Fi")
            if BT_RE.search(text):
                buckets.append("Bluetooth")
            if OTHER_RE.search(text):
                buckets.append("Other wireless")
            if BIZ_RE.search(text):
                buckets.append("Business")
            a["tech"] = buckets or ["General"]
            regs = set()
            ed_region = EDITIONS.get(a["edition"], (None, None, None, "Americas"))[3]
            if a["edition"] != "US":
                regs.add(ed_region)
            tld = a["domain"].rsplit(".", 1)[-1] if a["domain"] else ""
            if tld in TLD_REGION:
                regs.add(TLD_REGION[tld])
            for cid in a["companies"]:
                regs.add(self.companies[cid]["region"])
            a["regions"] = sorted(regs) or ["Global"]
            pub_l = (a["publisher"] or "").lower()
            a["is_wire"] = a["domain"] in PRESS_WIRES or a["publisher"] in PRESS_WIRES or "newswire" in pub_l or "presswire" in pub_l
            a["tier1"] = any(t in pub_l or t in a["domain"] for t in TIER1)
            a["wireless"] = bool(WIRELESS_RE.search(title))
        self.window = [a for a in self.window if a["companies"] or a["apps"] or a["topics"]]
        self.cluster()
        for a in self.window:
            age = max(0.0, (NOW - a["published"]).total_seconds() / 86400)
            w_theme = min(3.5, sum(w for k, _l, _r, w in THEME_RE if k in a["themes"]))
            a["score"] = round(2.2 * math.exp(-age / 9) + w_theme + 1.6 * math.log2(1 + a["coverage"])
                               + (1.0 if a["tier1"] else 0) + (0.8 if a["wireless"] else 0)
                               + 0.3 * min(3, len(a["companies"])), 3)
        log(f"Enriched {len(self.window)} articles in the {WINDOW_DAYS}-day window")

    def cluster(self) -> None:
        """Group near-identical headlines (same story, many outlets) -> coverage count."""
        arts = sorted(self.window, key=lambda a: a["published"], reverse=True)
        index: dict[str, list[int]] = defaultdict(list)
        clusters: list[dict] = []
        for a in arts:
            toks = norm_tokens(a["title"])
            a["_tok"] = toks
            cand = Counter()
            for t in toks:
                for ci in index.get(t, ()):
                    cand[ci] += 1
            best, best_j = None, 0.0
            for ci, ov in cand.most_common(8):
                if ov < 3:
                    break
                c = clusters[ci]
                if abs((c["date"] - a["published"]).days) > 5:
                    continue
                j = ov / len(toks | c["tok"])
                if j > best_j:
                    best, best_j = ci, j
            if best is not None and best_j >= 0.5:
                c = clusters[best]
                c["members"].append(a)
                a["cluster"] = best
            else:
                ci = len(clusters)
                clusters.append(dict(tok=toks, date=a["published"], members=[a]))
                a["cluster"] = ci
                for t in toks:
                    index[t].append(ci)
        for ci, c in enumerate(clusters):
            pubs = {m["publisher"] for m in c["members"]}
            lead = sorted(c["members"], key=lambda m: (not m["tier1"], m["via"] != "bing", m["published"]))[0]
            for m in c["members"]:
                m["coverage"] = len(pubs)
                m["lead"] = m is lead
                m["also"] = [dict(p=o["publisher"], u=o["url"]) for o in c["members"] if o is not m][:6]
                # share entity tags across the cluster so the story shows up everywhere it belongs
                for o in c["members"]:
                    if o is not m:
                        m["companies"] |= o["companies"]
                        m["apps"] |= o["apps"]
                        m["topics"] |= o["topics"]
        self.clusters = clusters

    # ---- outputs
    @staticmethod
    def pub(a: dict, full: bool = False) -> dict:
        d = dict(id=a["id"], t=a["title"], u=a["url"], p=a["publisher"], d=a["published"].isoformat(),
                 th=a["themes"], tech=a["tech"], rg=a["regions"], s=a["score"], cv=a["coverage"],
                 c=sorted(a["companies"]), w=1 if a["is_wire"] else 0)
        if a.get("also"):
            d["al"] = a["also"]
        if a.get("topics"):
            d["tp"] = sorted(a["topics"])
        if full and a.get("desc"):
            d["x"] = a["desc"][:280]
        if a.get("edition") and a["edition"] != "US":
            d["ed"] = a["edition"]
        return d

    def leads(self, arts: list[dict]) -> list[dict]:
        seen, out = set(), []
        for a in sorted(arts, key=lambda x: x["score"], reverse=True):
            if a["cluster"] in seen:
                continue
            seen.add(a["cluster"])
            out.append(a)
        return out

    def company_outputs(self) -> list[dict]:
        by_c: dict[str, list[dict]] = defaultdict(list)
        for a in self.window:
            for cid in a["companies"]:
                by_c[cid].append(a)
        rows = []
        (DATA / "companies").mkdir(parents=True, exist_ok=True)
        app_refs = defaultdict(list)
        for ap in APPLICATIONS:
            for p in ap["players"]:
                if p["cid"]:
                    app_refs[p["cid"]].append(dict(id=ap["id"], name=ap["name"], role="Player", what=p["products"]))
            for ch in ap["chips"]:
                if ch["cid"]:
                    app_refs[ch["cid"]].append(dict(id=ap["id"], name=ap["name"], role="Chip supplier", what=ch["parts"]))
        for c in COMPANIES:
            arts = self.leads(by_c.get(c["id"], []))
            n30 = len(arts)
            n7 = sum(1 for a in arts if (NOW - a["published"]).days < 7)
            exp7 = (n30 - n7) / 23 * 7 if n30 > n7 else 0
            momentum = round((n7 - exp7) / max(exp7, 1.0), 2)
            th = Counter(t for a in arts for t in a["themes"] if t not in ("wifi", "bluetooth", "chips"))
            focus = [k for k, _ in th.most_common(4)]
            regions = Counter(r for a in arts for r in a["regions"])
            alias_rx = self.alias_any.get(c["id"])
            ann = [a for a in arts if a["is_wire"] or (
                ANNOUNCE_RE.search(a["title"]) and alias_rx is not None and
                (m := alias_rx.search(a["title"])) is not None and m.start() <= 2)]
            ann = sorted(ann, key=lambda a: a["published"], reverse=True)[:25]
            highlights = sorted(arts, key=lambda a: a["score"], reverse=True)[:4]
            brief = self.company_brief(c, n30, n7, momentum, focus, highlights)
            row = dict(id=c["id"], name=c["name"], cat=c["cat"], region=c["region"], hq=c["hq"], tech=c["tech"],
                       tier=c["tier"], n30=n30, n7=n7, mom=momentum, focus=focus,
                       latest=self.pub(max(arts, key=lambda a: a["published"])) if arts else None,
                       hl=[self.pub(a) for a in highlights[:3]], brief=brief)
            rows.append(row)
            page = dict(c)
            for k in ("_aliases", "_strict"):
                page.pop(k, None)
            page.update(stats=dict(n30=n30, n7=n7, mom=momentum, themes=th.most_common(12), regions=regions.most_common()),
                        brief=brief, announcements=[self.pub(a, True) for a in ann],
                        news=[self.pub(a, True) for a in sorted(arts, key=lambda a: a["published"], reverse=True)[:120]],
                        apps=app_refs.get(c["id"], []),
                        peers=[dict(id=o["id"], name=o["name"]) for o in COMPANIES
                               if o["cat"] == c["cat"] and o["id"] != c["id"]][:14],
                        queries=[j["url"] for j in self.job_log if j["kind"] == "company" and j["eid"] == c["id"]],
                        generated=NOW.isoformat())
            write_js(DATA / "companies" / f'{c["id"]}.js', "WR_COMPANY", page)
        return rows

    def company_brief(self, c, n30, n7, mom, focus, hl) -> str:
        if n30 == 0:
            return f'No {c["name"]} headlines were found in public news over the last {WINDOW_DAYS} days.'
        trend = "accelerating" if mom > 0.5 else ("cooling" if mom < -0.4 else "steady")
        f = ", ".join(THEME_LABEL.get(k, k) for k in focus[:3]) or "general corporate news"
        s = (f'{n30} distinct stories in {WINDOW_DAYS} days ({n7} in the last 7 days, {trend} coverage). '
             f'Main threads: {f}.')
        if hl:
            s += f' Top story: "{hl[0]["title"]}" ({hl[0]["publisher"]}).'
        return s

    def theme_stats(self) -> list[dict]:
        leads = self.leads(self.window)
        n7 = Counter()
        n30 = Counter()
        for a in leads:
            for t in a["themes"]:
                n30[t] += 1
                if (NOW - a["published"]).days < 7:
                    n7[t] += 1
        out = []
        for k, lbl, _r, _w in THEME_RE:
            if n30[k]:
                exp7 = (n30[k] - n7[k]) / 23 * 7
                out.append(dict(id=k, label=lbl, n30=n30[k], n7=n7[k], mom=round((n7[k] - exp7) / max(exp7, 1), 2)))
        return sorted(out, key=lambda x: x["n30"], reverse=True)

    def news_output(self) -> list[dict]:
        pool = [a for a in self.window if a["topics"] or a["wireless"] or (a["companies"] and any(
            t in a["themes"] for t in ("mna", "partnership", "launch", "funding", "legal", "wifi7", "wifi8")))]
        leads = self.leads(pool)
        return [self.pub(a, True) for a in leads[:900]]

    def app_outputs(self) -> list[dict]:
        by_a: dict[str, list[dict]] = defaultdict(list)
        for a in self.window:
            for ap in a["apps"]:
                by_a[ap].append(a)
        fc_by = defaultdict(list)
        for f in self.forecasts.values():
            fc_by[f["app"]].append(f)
        out = []
        for ap in APPLICATIONS:
            fcs = sorted(fc_by.get(ap["id"], []), key=lambda f: f["published"], reverse=True)
            # one entry per distinct forecast (syndicated copies of the same release are merged)
            seen, uniq = set(), []
            for f in fcs:
                f.pop("_out", None)
                f.pop("_base", None)
                key = (f.get("cagr") and round(f["cagr"], 1), f.get("target_year"),
                       f.get("target_value_b") and round(f["target_value_b"], 1))
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(f)
            uniq = uniq[:30]
            proj = projection(uniq, ap["prior"])
            arts = self.leads(by_a.get(ap["id"], []))
            signals = [a for a in arts if SIGNAL_RE.search(a["title"])]
            d = {k: v for k, v in ap.items() if k not in ("q",) and not k.startswith("_")}
            d["mkt"] = ap["mkt"].split("|")[0]
            d.update(news=[self.pub(a, True) for a in sorted(arts, key=lambda a: a["score"], reverse=True)[:40]],
                     signals=[self.pub(a, True) for a in sorted(signals, key=lambda a: a["published"], reverse=True)[:12]],
                     n30=len(arts),
                     forecasts=[dict(t=f["title"], u=f["url"], p=f["publisher"], d=f["published"].isoformat(),
                                     k=f.get("kind", "full"), cagr=f.get("cagr"), tv=f.get("target_value_b"),
                                     ty=f.get("target_year"), bv=f.get("base_value_b"), sv=f.get("start_value_b"),
                                     sy=f.get("start_year"), out=bool(f.get("_out")), x=f.get("snippet", "")[:260])
                                for f in uniq],
                     projection=proj,
                     queries=[j["url"] for j in self.job_log if j["kind"] in ("app", "forecast") and j["eid"] == ap["id"]])
            out.append(d)
        return out

    # ---- executive summary
    def summary(self, rows: list[dict], themes: list[dict], news: list[dict]) -> dict:
        leads = self.leads(self.window)
        top = sorted(rows, key=lambda r: r["n30"], reverse=True)
        movers = sorted([r for r in rows if r["n30"] >= 4], key=lambda r: r["mom"], reverse=True)
        bullets = []
        silicon = [r for r in top if r["cat"] in ("chip", "ipsw", "module")]
        devices = [r for r in top if r["cat"] in ("oem", "ai")]
        if silicon:
            bullets.append(dict(h="Most active silicon, IP & module vendors (30 days)",
                                items=[dict(t=f'{r["name"]} — {r["n30"]} stories', cid=r["id"]) for r in silicon[:8]]))
        if devices:
            bullets.append(dict(h="Most active OEMs & AI-device makers (30 days)",
                                items=[dict(t=f'{r["name"]} — {r["n30"]} stories', cid=r["id"]) for r in devices[:8]]))
        if movers:
            bullets.append(dict(h="Fastest-rising coverage (last 7 days vs. prior trend)",
                                items=[dict(t=f'{r["name"]} — {r["n7"]} stories this week', cid=r["id"]) for r in movers[:6]]))
        tech_themes = [t for t in themes if t["id"] not in ("wifi", "bluetooth", "chips", "launch", "earnings", "ai")]
        if tech_themes:
            bullets.append(dict(h="Dominant technology & business threads",
                                items=[dict(t=f'{t["label"]}: {t["n30"]} stories ({t["n7"]} this week)', theme=t["id"]) for t in tech_themes[:8]]))

        def pick(theme, n=6):
            sel = [a for a in leads if theme in a["themes"] and (a["companies"] or a["wireless"])]
            return [self.pub(a) for a in sorted(sel, key=lambda a: a["score"], reverse=True)[:n]]
        sections = [
            dict(id="mna", h="Deals: M&A, divestitures & funding", items=pick("mna") + pick("funding", 3)),
            dict(id="launch", h="Product launches & announcements", items=pick("launch", 8)),
            dict(id="partnership", h="Partnerships & design wins", items=pick("partnership")),
            dict(id="wifi8", h="Next-gen standards (Wi-Fi 8, Channel Sounding, LE Audio)", items=pick("wifi8", 4) + pick("cs", 3) + pick("leaudio", 3)),
            dict(id="legal", h="Regulation, spectrum, trade & security", items=pick("legal", 4) + pick("spectrum", 4)),
            dict(id="earnings", h="Earnings & financial signals", items=pick("earnings", 6)),
        ]
        for s in sections:  # de-dup within a section
            seen, uniq = set(), []
            for i in s["items"]:
                if i["id"] not in seen:
                    seen.add(i["id"])
                    uniq.append(i)
            s["items"] = uniq
        reg = Counter(r for a in leads for r in a["regions"])
        narrative = llm_narrative(news[:70])
        return dict(bullets=bullets, sections=sections, regions=reg.most_common(), narrative=narrative)

    def run(self) -> None:
        t0 = time.time()
        DATA.mkdir(parents=True, exist_ok=True)
        self.load_state()
        self.crawl()
        self.enrich()
        rows = self.company_outputs()
        themes = self.theme_stats()
        news = self.news_output()
        apps = self.app_outputs()
        summ = self.summary(rows, themes, news)
        pubs = {a["publisher"] for a in self.window}
        ok = sum(1 for j in self.job_log if j["ok"])
        kpis = dict(companies=len(COMPANIES), applications=len(APPLICATIONS), articles=len(self.leads(self.window)),
                    raw=len(self.window), publishers=len(pubs), forecasts=len(self.forecasts),
                    queries=len(self.job_log), queries_ok=ok)
        report = dict(generated=NOW.isoformat(), window=WINDOW_DAYS, kpis=kpis, themes=themes, summary=summ,
                      companies=rows, top=news[:30], categories=CATEGORIES, regions=REGIONS,
                      theme_labels=THEME_LABEL)
        write_js(DATA / "report.js", "WR_REPORT", report)
        write_js(DATA / "news.js", "WR_NEWS", dict(generated=NOW.isoformat(), items=news, theme_labels=THEME_LABEL,
                                                  topics=[dict(id=t[0], label=t[1]) for t in TOPICS] +
                                                  [dict(id=f"regional-{e}", label=f"Regional press: {e} edition")
                                                   for e in dict.fromkeys(r[0] for r in REGIONAL_TOPICS)]))
        write_js(DATA / "apps.js", "WR_APPS", dict(generated=NOW.isoformat(), apps=apps, theme_labels=THEME_LABEL))
        write_js(DATA / "index.js", "WR_INDEX", dict(
            companies=[dict(id=c["id"], n=c["name"], cat=c["cat"], r=c["region"]) for c in COMPANIES],
            apps=[dict(id=a["id"], n=a["name"], i=a["icon"]) for a in APPLICATIONS], categories=CATEGORIES,
            theme_labels=THEME_LABEL))
        self.save_state()
        dur = time.time() - t0
        runlog = dict(generated=NOW.isoformat(), duration_s=round(dur), kpis=kpis, fetch=dict(self.fetch.stats),
                      jobs=self.job_log, log=LOG_LINES[-200:],
                      topics=[dict(id=t[0], label=t[1], q=t[2]) for t in TOPICS],
                      regional=[dict(edition=e, q=q) for e, q, _ in REGIONAL_TOPICS],
                      llm=bool(os.environ.get("LLM_API_KEY")))
        write_js(DATA / "runlog.js", "WR_RUNLOG", runlog)
        log(f"DONE in {dur:.0f}s - {kpis}")


def projection(fcs: list[dict], prior: float) -> dict:
    """10-year projection from public forecasts. Median of evidence, outliers (>4x / <1/4x median
    base, or CAGR far from median) excluded and flagged. Falls back to a clearly-labelled prior."""
    years = list(range(BASE_YEAR, BASE_YEAR + 11))
    cg = [f for f in fcs if f.get("cagr") is not None]
    if len(cg) >= 3:
        med = statistics.median(f["cagr"] for f in cg)
        for f in cg:
            if f["cagr"] > max(2.5 * med, med + 15) or f["cagr"] < min(0.3 * med, med - 15):
                f["_out"] = True
    cgk = [f["cagr"] / 100 for f in cg if not f.get("_out")]
    mc = statistics.median(cgk) if cgk else None
    use_c = mc if mc is not None else prior / 100
    for f in fcs:
        if f.get("base_value_b") is None and f.get("target_value_b") and f.get("target_year"):
            f["_base"] = f["target_value_b"] / ((1 + use_c) ** (f["target_year"] - BASE_YEAR))
        elif f.get("base_value_b"):
            f["_base"] = f["base_value_b"]
    bs = [f for f in fcs if f.get("_base") and not f.get("_out")]
    if len(bs) >= 3:
        mb0 = statistics.median(f["_base"] for f in bs)
        for f in bs:
            if f["_base"] > 4 * mb0 or f["_base"] < mb0 / 4:
                f["_out"] = True
        bs = [f for f in bs if not f.get("_out")]
    n_used = len({id(f) for f in fcs if not f.get("_out") and (f.get("cagr") is not None or f.get("_base"))})
    if bs:
        mb = statistics.median(f["_base"] for f in bs)
        series = [round(mb * (1 + use_c) ** (y - BASE_YEAR), 3) for y in years]
        per = [[f["_base"] * (1 + ((f["cagr"] / 100) if f.get("cagr") is not None else use_c)) ** (y - BASE_YEAR)
                for f in bs] for y in years]
        return dict(source="public", n=n_used, years=years, mid=series, lo=[round(min(v), 3) for v in per],
                    hi=[round(max(v), 3) for v in per], cagr=round(use_c * 100, 1), base=round(mb, 3),
                    cagr_from="public" if mc is not None else "prior",
                    cagr_range=[round(min(cgk) * 100, 1), round(max(cgk) * 100, 1)] if cgk else None)
    if mc is not None:
        idx = [round(100 * (1 + mc) ** (y - BASE_YEAR), 1) for y in years]
        return dict(source="public-cagr", n=n_used, years=years, mid=idx, lo=None, hi=None, cagr=round(mc * 100, 1),
                    base=None, index=True, cagr_range=[round(min(cgk) * 100, 1), round(max(cgk) * 100, 1)])
    idx = [round(100 * (1 + prior / 100) ** (y - BASE_YEAR), 1) for y in years]
    return dict(source="prior", n=0, years=years, mid=idx, lo=None, hi=None, cagr=prior, base=None, index=True)


def llm_narrative(news: list[dict]) -> dict | None:
    key = os.environ.get("LLM_API_KEY")
    if not key or not news:
        return None
    base = (os.environ.get("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL") or "gpt-4o-mini"
    lines = "\n".join(f"[{i + 1}] {n['t']} ({n['p']}, {n['d'][:10]})" for i, n in enumerate(news))
    prompt = ("You are a senior wireless (Wi-Fi/Bluetooth) product-marketing analyst. Using ONLY the numbered public "
              "headlines below, write 6-8 crisp executive bullets on what the industry is doing. Cite sources as [n] "
              "after each claim. Never invent facts, numbers or companies that are not in the headlines.\n\n" + lines)
    try:
        r = requests.post(f"{base}/chat/completions", timeout=90,
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json=dict(model=model, temperature=0.2, messages=[dict(role="user", content=prompt)]))
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        return dict(model=model, text=text, refs=[dict(n=i + 1, t=n["t"], u=n["u"], p=n["p"]) for i, n in enumerate(news)])
    except Exception as e:
        log(f"LLM narrative skipped: {e}")
        return None


def write_js(path: Path, var: str, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"window.{var} = " + json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str) + ";\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="small smoke-test run")
    args = ap.parse_args()
    try:
        Agent(quick=args.quick).run()
        return 0
    except Exception:
        log("FATAL " + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())

