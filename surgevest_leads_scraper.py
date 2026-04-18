"""
surgevest_leads_scraper.py
Scrapes Google Search + Google Maps for local business leads.
No API key required — uses requests + BeautifulSoup.

Dependencies:
    pip install requests beautifulsoup4 lxml

Usage:
    python surgevest_leads_scraper.py "med spas in Atlanta"
    python surgevest_leads_scraper.py "restaurants in Miami" --enrich
    python surgevest_leads_scraper.py "law firms in Dallas" --max 30

Output: surgevest_leads.csv
"""

import argparse
import csv
import json
import re
import random
import sys
import time
from dataclasses import dataclass
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

# ── Constants ──────────────────────────────────────────────────────────────────

OUTPUT_FILE = "surgevest_leads.csv"
CSV_FIELDS  = [
    "Business Name",
    "Phone Number",
    "Address",
    "Website",
    "Business Type",
    "Call Window",
]

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

# US phone: optional +1 prefix, then (NXX) NXX-XXXX in various separators
_PHONE_RE = re.compile(
    r"(?<!\d)(\+?1[\s.\-]?)?(\(?[2-9]\d{2}\)?[\s.\-]\d{3}[\s.\-]\d{4})(?!\d)"
)

# Street address ending with city, ST ZIP
_ADDR_RE = re.compile(
    r"\d{1,5}\s+[\w\s]{3,40}"
    r"(?:St(?:reet)?|Ave(?:nue)?|Blvd|Dr(?:ive)?|Rd|Ln|Ct|Way|Pkwy|Pl(?:ace)?|S(?:ui)?te)"
    r"[\w\s.,#-]{0,60}"
    r",\s*[\w\s]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?",
    re.IGNORECASE,
)

# UI labels to skip when scanning aria-label attributes on Google Maps
_SKIP_LABELS = frozenset({
    "menu", "search", "directions", "share", "save", "close", "back",
    "more options", "zoom in", "zoom out", "map", "satellite", "street view",
    "google maps", "report a map error", "terms", "privacy", "keyboard shortcuts",
    "send feedback", "layers", "terrain",
})

# JSON-LD @type values that indicate a local business record
_BUSINESS_TYPES = {
    "LocalBusiness", "MedicalBusiness", "HealthAndBeautyBusiness",
    "BeautySalon", "HairSalon", "DaySpa", "MedicalClinic", "Dentist",
    "Physician", "Restaurant", "FoodEstablishment", "LegalService",
    "FinancialService", "AutoRepair", "Store", "Organization",
}

# ── Call Window Logic ──────────────────────────────────────────────────────────

# First matching keyword group wins.
_CALL_WINDOWS: list[tuple[tuple[str, ...], str]] = [
    (
        ("restaurant", "food", "bar", "grill", "bistro", "diner", "eatery",
         "pub", "pizza", "sushi", "burger", "bbq", "steakhouse", "tavern",
         "cafe", "coffee"),
        "Evenings 5–8 PM",
    ),
    (
        ("salon", "spa", "med spa", "medspa", "beauty", "hair", "nail",
         "lash", "massage", "waxing", "aesthetics", "skincare", "botox",
         "filler", "facial", "tanning", "eyebrow", "microblading"),
        "Midday 11 AM–2 PM",
    ),
    (
        ("gym", "fitness", "yoga", "pilates", "crossfit", "boxing",
         "martial arts", "personal training", "boot camp"),
        "Mornings 8–10 AM or Evenings 5–7 PM",
    ),
    (
        ("doctor", "physician", "dentist", "dental", "clinic", "medical",
         "urgent care", "chiropractic", "optometry", "vision", "therapy",
         "mental health", "psychiatry", "orthopedic", "dermatology",
         "pediatric", "pharmacy"),
        "Mornings 9–11 AM",
    ),
    (
        ("lawyer", "attorney", "law firm", "legal", "accounting",
         "accountant", "cpa", "financial", "finance", "insurance",
         "real estate", "mortgage", "notary", "consulting",
         "marketing agency", "staffing", "recruiting"),
        "Mornings 9–11 AM or Afternoons 2–4 PM",
    ),
    (
        ("auto", "car", "truck", "mechanic", "tire", "oil change",
         "body shop", "detailing", "towing", "transmission"),
        "Mornings 8–10 AM",
    ),
    (
        ("hotel", "motel", "inn", "resort", "lodging"),
        "Mornings 9–11 AM or Afternoons 2–4 PM",
    ),
    (
        ("store", "shop", "boutique", "market", "retail", "clothing",
         "jewelry", "florist", "furniture", "electronics"),
        "Midday 11 AM–2 PM",
    ),
    (
        ("school", "tutoring", "daycare", "childcare", "preschool",
         "academy", "learning center"),
        "Mornings 9–11 AM",
    ),
]
_DEFAULT_WINDOW = "Mornings 9–11 AM or Afternoons 2–4 PM"


def infer_call_window(business_type: str, query: str = "") -> str:
    text = f"{business_type} {query}".lower()
    for keywords, window in _CALL_WINDOWS:
        if any(k in text for k in keywords):
            return window
    return _DEFAULT_WINDOW


# ── Data Model ─────────────────────────────────────────────────────────────────

@dataclass
class Lead:
    business_name: str = ""
    phone_number:  str = ""
    address:       str = ""
    website:       str = ""
    business_type: str = ""
    call_window:   str = ""

    @property
    def key(self) -> str:
        return self.business_name.lower().strip()

    def to_row(self) -> dict:
        return {
            "Business Name": self.business_name,
            "Phone Number":  self.phone_number,
            "Address":       self.address,
            "Website":       self.website,
            "Business Type": self.business_type,
            "Call Window":   self.call_window,
        }

    def merge(self, other: "Lead") -> None:
        """Fill missing fields from a duplicate record."""
        if not self.phone_number:  self.phone_number  = other.phone_number
        if not self.address:       self.address        = other.address
        if not self.website:       self.website        = other.website
        if not self.business_type: self.business_type  = other.business_type
        if not self.call_window:   self.call_window    = other.call_window


# ── HTTP Session ───────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":                random.choice(_USER_AGENTS),
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate, br",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "none",
        "Cache-Control":             "max-age=0",
    })
    # Prime the session with a Google homepage visit to get cookies
    try:
        s.get("https://www.google.com/?hl=en", timeout=10)
        _sleep(1.5, 2.5)
    except requests.RequestException:
        pass
    return s


def fetch(session: requests.Session, url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 429:
                wait = 15 * (attempt + 1)
                print(f"  [rate-limited] sleeping {wait}s (attempt {attempt + 1}/{retries})...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            if attempt == retries - 1:
                print(f"  [fetch error] {url[:80]}: {exc}")
            else:
                time.sleep(3 * (attempt + 1))
    return None


def _sleep(lo: float = 1.0, hi: float = 2.5) -> None:
    time.sleep(random.uniform(lo, hi))


# ── Extraction Utilities ───────────────────────────────────────────────────────

def extract_phone(text: str) -> str:
    m = _PHONE_RE.search(text)
    if m:
        return ((m.group(1) or "") + m.group(2)).strip()
    return ""


def extract_address(text: str) -> str:
    m = _ADDR_RE.search(text)
    return m.group(0).strip() if m else ""


def unwrap_google_url(href: str) -> str:
    """Resolve /url?q=https://example.com redirect to the real URL."""
    if not href:
        return ""
    if href.startswith("http") and "google." not in urlparse(href).netloc:
        return href
    parsed = urlparse(href)
    for segment in parsed.query.split("&"):
        if segment.startswith("q="):
            return segment[2:].split("&")[0]
    return ""


def parse_jsonld(soup: BeautifulSoup) -> list[dict]:
    items: list[dict] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            raw = json.loads(tag.string or "")
        except (json.JSONDecodeError, AttributeError):
            continue
        batch = raw if isinstance(raw, list) else [raw]
        for item in batch:
            if any(bt in item.get("@type", "") for bt in _BUSINESS_TYPES):
                items.append(item)
    return items


def jsonld_to_lead(item: dict, query: str) -> Lead:
    raw_addr = item.get("address", {})
    if isinstance(raw_addr, dict):
        address = ", ".join(filter(None, [
            raw_addr.get("streetAddress", ""),
            raw_addr.get("addressLocality", ""),
            raw_addr.get("addressRegion", ""),
            raw_addr.get("postalCode", ""),
        ]))
    else:
        address = str(raw_addr)

    btype = item.get("@type", "")
    lead = Lead(
        business_name = item.get("name", ""),
        phone_number  = item.get("telephone", ""),
        address       = address,
        website       = item.get("url", ""),
        business_type = btype,
    )
    lead.call_window = infer_call_window(btype, query)
    return lead


# ── Google Search Local Results ────────────────────────────────────────────────

def scrape_google_search(session: requests.Session, query: str) -> list[Lead]:
    """
    Fetch Google's local search tab (?tbm=lcl) and parse business listings.
    Tries JSON-LD structured data first, then falls back to HTML card parsing.
    """
    url = f"https://www.google.com/search?q={quote_plus(query)}&tbm=lcl&hl=en&gl=us&num=20"
    print(f"\n[Google Search] {url}")
    html = fetch(session, url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")

    # Strategy A — JSON-LD (cleanest when present)
    jsonld_items = parse_jsonld(soup)
    if jsonld_items:
        leads = [jsonld_to_lead(i, query) for i in jsonld_items if i.get("name")]
        print(f"  [json-ld] {len(leads)} businesses found")
        return leads

    # Strategy B — HTML card parsing with multiple selector fallbacks
    # Google's local result cards use obfuscated class names that change over time.
    CARD_SELECTORS = [
        "[data-cid]",
        ".VkpGBb",
        ".rllt__details",
        ".uMdZh",
        "div[class*='lqhpac']",
    ]
    cards: list = []
    for sel in CARD_SELECTORS:
        found = soup.select(sel)
        if found:
            cards = found
            print(f"  [html:{sel}] {len(cards)} cards found")
            break

    if not cards:
        # Last resort: elements with aria-label that look like business names
        cards = [
            el for el in soup.find_all(True, {"aria-label": True})
            if 3 < len(el.get("aria-label", "")) < 80
        ]
        print(f"  [html:aria-label fallback] {len(cards)} candidates")

    leads: list[Lead] = []
    seen: set[str] = set()

    for card in cards:
        text = card.get_text(" ", strip=True)

        # Name: heading role > h-tag > bold > aria-label
        name_el = (
            card.find(attrs={"role": "heading"})
            or card.find(["h3", "h2", "h1"])
            or card.find(["b", "strong"])
        )
        name = name_el.get_text(strip=True) if name_el else card.get("aria-label", "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())

        # Website: unwrap any Google redirect links in this card
        website = ""
        for a in card.find_all("a", href=True):
            w = unwrap_google_url(a["href"])
            if w:
                website = w
                break

        # Business type: short span with no digits
        btype = ""
        for span in card.find_all("span"):
            t = span.get_text(strip=True)
            if t and len(t) < 40 and not any(c.isdigit() for c in t):
                btype = t
                break

        lead = Lead(
            business_name = name,
            phone_number  = extract_phone(text),
            address       = extract_address(text),
            website       = website,
            business_type = btype,
        )
        lead.call_window = infer_call_window(btype, query)
        leads.append(lead)

    print(f"  [html] {len(leads)} businesses extracted")
    return leads


# ── Google Maps Search ─────────────────────────────────────────────────────────

def scrape_google_maps(session: requests.Session, query: str) -> list[Lead]:
    """
    Fetch the Google Maps search page and mine its initial HTML.
    Maps renders via JS, but the server-side HTML still includes aria-labels,
    embedded JSON, and raw text with phone/address/website data.
    """
    url = f"https://www.google.com/maps/search/{quote_plus(query)}/?hl=en"
    print(f"\n[Google Maps ] {url}")
    html = fetch(session, url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")

    # Strategy A — JSON-LD
    jsonld_items = parse_jsonld(soup)
    if jsonld_items:
        leads = [jsonld_to_lead(i, query) for i in jsonld_items if i.get("name")]
        print(f"  [json-ld] {len(leads)} businesses found")
        return leads

    # Strategy B — aria-label names + bulk regex on the full HTML body
    all_phones   = [m.group(0).strip() for m in _PHONE_RE.finditer(html)]
    all_addresses = _ADDR_RE.findall(html)
    all_websites  = [
        u for u in re.findall(r'https?://[^\s"\'<>]{4,120}', html)
        if "google." not in u and len(u) < 100
    ]

    leads: list[Lead] = []
    seen: set[str]    = set()
    idx = 0

    for el in soup.find_all(True, {"aria-label": True}):
        label = el.get("aria-label", "").strip()
        if not label or len(label) < 3 or len(label) > 80:
            continue
        if label.lower() in _SKIP_LABELS or label.lower() in seen:
            continue
        if not any(c.isalpha() for c in label):
            continue

        seen.add(label.lower())

        # Pull correlated data by sequential index (best-effort pairing)
        phone   = all_phones[idx]    if idx < len(all_phones)    else ""
        address = all_addresses[idx] if idx < len(all_addresses) else ""
        website = next((w for w in all_websites[idx: idx + 5]), "")

        # Supplement from element subtree text
        sub_text = el.get_text(" ", strip=True)
        if not phone:   phone   = extract_phone(sub_text)
        if not address: address = extract_address(sub_text)

        lead = Lead(
            business_name = label,
            phone_number  = phone,
            address       = address,
            website       = website,
        )
        lead.call_window = infer_call_window("", query)
        leads.append(lead)
        idx += 1

    print(f"  [aria-label] {len(leads)} business candidates found")
    return leads


# ── Per-Lead Enrichment ────────────────────────────────────────────────────────

def enrich_lead(session: requests.Session, lead: Lead, query: str) -> None:
    """
    Do a targeted Google search for this business name to fill missing fields.
    Google's knowledge panel for a specific business reliably surfaces phone,
    address, and website in the HTML even without JS.
    """
    if lead.phone_number and lead.address and lead.website:
        return

    city = query.lower().split(" in ")[-1].strip().title() if " in " in query.lower() else ""
    search_q = f"{lead.business_name} {city}".strip()
    html = fetch(session, f"https://www.google.com/search?q={quote_plus(search_q)}&hl=en&gl=us")
    if not html:
        return

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    if not lead.phone_number:  lead.phone_number  = extract_phone(text)
    if not lead.address:       lead.address        = extract_address(text)

    if not lead.website:
        for a in soup.find_all("a", href=True):
            w = unwrap_google_url(a["href"])
            if w and "google." not in w:
                lead.website = w
                break

    for item in parse_jsonld(soup):
        if not lead.phone_number:
            lead.phone_number = item.get("telephone", "")
        if not lead.address:
            raw = item.get("address", {})
            if isinstance(raw, dict):
                lead.address = ", ".join(filter(None, [
                    raw.get("streetAddress", ""),
                    raw.get("addressLocality", ""),
                    raw.get("addressRegion", ""),
                    raw.get("postalCode", ""),
                ]))
        if not lead.website:
            lead.website = item.get("url", "")
        if not lead.business_type:
            lead.business_type = item.get("@type", "")
            lead.call_window   = infer_call_window(lead.business_type, query)


# ── Deduplication ──────────────────────────────────────────────────────────────

def deduplicate(leads: list[Lead]) -> list[Lead]:
    merged: dict[str, Lead] = {}
    for lead in leads:
        k = lead.key
        if not k:
            continue
        if k in merged:
            merged[k].merge(lead)
        else:
            merged[k] = lead
    return list(merged.values())


# ── CSV Output ─────────────────────────────────────────────────────────────────

def save_csv(leads: list[Lead]) -> None:
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for lead in leads:
            writer.writerow(lead.to_row())
    print(f"\nSaved {len(leads)} leads → {OUTPUT_FILE}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Scrape Google Search + Google Maps for local business leads (no API key).\n\n"
            "Examples:\n"
            '  python surgevest_leads_scraper.py "med spas in Atlanta"\n'
            '  python surgevest_leads_scraper.py "restaurants in Miami" --enrich\n'
            '  python surgevest_leads_scraper.py "law firms in Dallas" --max 30\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("query", help='Search query, e.g. "med spas in Atlanta"')
    p.add_argument(
        "--enrich",
        action="store_true",
        help="Follow up with a per-lead Google search to fill missing phone/address/website (slower but more complete)",
    )
    p.add_argument(
        "--max",
        type=int,
        default=0,
        metavar="N",
        help="Cap results at N leads (default: no limit)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    session = make_session()
    all_leads: list[Lead] = []

    search_leads = scrape_google_search(session, args.query)
    all_leads.extend(search_leads)
    _sleep(2.0, 4.0)

    maps_leads = scrape_google_maps(session, args.query)
    all_leads.extend(maps_leads)

    leads = deduplicate(all_leads)
    leads = [l for l in leads if l.business_name]

    if args.max:
        leads = leads[: args.max]

    if not leads:
        print(
            "\nNo leads extracted.\n"
            "Google may be blocking the request or returned no local results.\n"
            "Tips: wait a few minutes and retry, try a different network, "
            "or run with --enrich for deeper per-business lookups."
        )
        sys.exit(1)

    if args.enrich:
        print(f"\n[Enriching] fetching details for {len(leads)} leads...")
        for i, lead in enumerate(leads, 1):
            print(f"  [{i}/{len(leads)}] {lead.business_name}")
            enrich_lead(session, lead, args.query)
            _sleep(2.0, 4.0)

    save_csv(leads)

    # Terminal preview (first 10 rows)
    col_w = 34
    print(f"\n{'Business Name':<{col_w}} {'Phone':<16} {'Call Window'}")
    print("─" * 80)
    for lead in leads[:10]:
        print(f"{lead.business_name[:col_w - 1]:<{col_w}} {lead.phone_number:<16} {lead.call_window}")
    if len(leads) > 10:
        print(f"  … and {len(leads) - 10} more — open {OUTPUT_FILE} to see all")


if __name__ == "__main__":
    main()
