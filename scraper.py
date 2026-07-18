"""
Baseball competitor price scraper.

Scrapes every baseball-related product and price from 5 UK retailers,
assigns each product to a category, and writes the results to CSV files
in the data/ folder.

Sites:
  - baseballandsoftball.co.uk   (Shopify - uses the products.json API)
  - coachcarterssports.co.uk    (Shopify - uses the products.json API)
  - cometsports.co.uk           (Magento - parses category listing pages)
  - thebaseballshop.co.uk       (Visualsoft - parses category listing pages)
  - baseballoutlet.co.uk        (bot-protected; best-effort via cloudscraper)

Run:  python scraper.py
"""

import csv
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    import cloudscraper
    SCRAPER = cloudscraper.create_scraper()
except Exception:
    SCRAPER = requests.Session()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}
DELAY = 1.5  # polite pause between requests, in seconds
PRICE_RE = re.compile(r"£\s*(\d{1,4}(?:,\d{3})?\.\d{2})")

# ---------------------------------------------------------------------------
# Category normalisation - maps keywords to your product categories
# ---------------------------------------------------------------------------
CATEGORY_RULES = [
    ("Bats", ["bat ", " bats", "bat)", "fungo", "slugger", "slowpitch", "slow-pitch",
              "fastpitch", "fast-pitch", "wooden bat", "youth bat"]),
    ("Batting Gloves", ["batting glove"]),
    ("Fielding Gloves", ["fielding glove", "baseball glove", "softball glove", "mitt",
                         "first base", "infield glove", "outfield glove", "glove"]),
    ("Balls", ["baseball)", "baseballs", "softball)", "softballs", "training ball",
               "practice ball", " ball", "rolb", "dot ("]),
    ("Helmets", ["helmet", "face guard", "faceguard", "jaw guard"]),
    ("Catchers Gear", ["catcher", "chest protector", "leg guard", "umpire"]),
    ("Cleats", ["cleat", "spike", "turf shoe", "trainers", "footwear", "shoes"]),
    ("Training Equipment", ["training", "pitching machine", "batting tee", "tee ",
                            "net", "cage", "screen", "practice", "agility"]),
    ("Bags", ["bag", "backpack", "wheeled"]),
    ("Protection", ["elbow", "shin", "sliding", "protective", "guard", "cup ",
                    "evoshield", "wrist guard", "mouthguard"]),
    ("Clothing", ["pants", "jersey", "shirt", "jacket", "socks", "belt", "cap",
                  "hat", "beanie", "trousers", "shorts", "hoodie", "tee"]),
    ("Field Equipment", ["base ", "bases", "plate", "line marker", "field"]),
    ("Accessories", ["grip", "tape", "pine tar", "eye black", "sunglasses",
                     "accessor", "care", "scorebook", "voucher"]),
]

BASEBALL_KEYWORDS = [
    "baseball", "softball", "bat", "glove", "mitt", "catcher", "cleat",
    "helmet", "pitching", "batting", "fungo", "slowpitch", "fastpitch",
    "rawlings", "marucci", "easton", "wilson", "louisville", "mizuno",
    "victus", "worth", "miken", "evoshield", "teammate", "kr3",
]


def categorise(name, hint=""):
    text = f"{name} {hint}".lower()
    for category, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in text:
                return category
    return "Other"


def looks_baseball_related(name, hint=""):
    text = f"{name} {hint}".lower()
    return any(kw in text for kw in BASEBALL_KEYWORDS)


def clean_price(value):
    if value is None:
        return None
    m = PRICE_RE.search(str(value))
    if m:
        return float(m.group(1).replace(",", ""))
    try:
        return round(float(str(value).replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def get(url, session=None):
    session = session or requests
    try:
        resp = (session or requests).get(url, headers=HEADERS, timeout=30)
        time.sleep(DELAY)
        if resp.status_code == 200:
            return resp
        print(f"  ! {url} returned HTTP {resp.status_code}")
    except Exception as exc:
        print(f"  ! {url} failed: {exc}")
    return None


# ---------------------------------------------------------------------------
# Shopify sites - clean JSON API, no HTML parsing needed
# ---------------------------------------------------------------------------
def scrape_shopify(site_name, base_url, baseball_only=False, session=None):
    print(f"Scraping {site_name} (Shopify API)...")
    rows = []
    page = 1
    while True:
        url = f"{base_url}/products.json?limit=250&page={page}"
        resp = get(url, session=session)
        if resp is None:
            break
        try:
            products = resp.json().get("products", [])
        except json.JSONDecodeError:
            print(f"  ! {site_name}: response was not JSON (possibly blocked)")
            break
        if not products:
            break
        for p in products:
            name = p.get("title", "").strip()
            hint = f"{p.get('product_type', '')} {' '.join(p.get('tags', []))}"
            if baseball_only and not looks_baseball_related(name, hint):
                continue
            variants = p.get("variants", [])
            price = clean_price(variants[0].get("price")) if variants else None
            if name and price is not None:
                rows.append({
                    "site": site_name,
                    "category": categorise(name, hint),
                    "product": name,
                    "price": price,
                    "url": f"{base_url}/products/{p.get('handle', '')}",
                })
        page += 1
        if page > 40:  # safety stop
            break
    print(f"  -> {len(rows)} products")
    return rows


# ---------------------------------------------------------------------------
# Comet Sports - Magento category listing pages
# ---------------------------------------------------------------------------
COMET_BASE = "https://www.cometsports.co.uk"
COMET_CATEGORIES = [
    ("Bats", "/baseball-softball-shop-uk/bats.html"),
    ("Balls", "/baseball-softball-shop-uk/balls.html"),
    ("Batting Gloves", "/baseball-softball-shop-uk/batting-gloves.html"),
    ("Fielding Gloves", "/baseball-softball-shop-uk/baseball-gloves-mitts.html"),
    ("Helmets", "/baseball-softball-shop-uk/helmets.html"),
    ("Catchers Gear", "/baseball-softball-shop-uk/catcher-equipment.html"),
    ("Cleats", "/baseball-softball-shop-uk/shoes.html"),
    ("Training Equipment", "/baseball-softball-shop-uk/training-equipment.html"),
    ("Bags", "/baseball-softball-shop-uk/bags.html"),
    ("Accessories", "/baseball-softball-shop-uk/accessories.html"),
    ("Clothing", "/baseball-softball-shop-uk/clothing-apparel.html"),
    ("Field Equipment", "/baseball-softball-shop-uk/field-equipment.html"),
]


def scrape_comet():
    print("Scraping Comet Sports (Magento pages)...")
    rows = []
    for category, path in COMET_CATEGORIES:
        for page_num in range(1, 11):
            url = f"{COMET_BASE}{path}?p={page_num}" if page_num > 1 else f"{COMET_BASE}{path}"
            resp = get(url)
            if resp is None:
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select("li.product-item, .product-item")
            found = 0
            for item in items:
                link = item.select_one("a.product-item-link") or item.select_one("a[href]")
                price_el = item.select_one("[data-price-type='finalPrice'] .price, .special-price .price, .price")
                if not link:
                    continue
                name = link.get_text(strip=True)
                price = clean_price(price_el.get_text(strip=True) if price_el else None)
                if name and price is not None:
                    rows.append({
                        "site": "Comet Sports",
                        "category": category,
                        "product": name,
                        "price": price,
                        "url": link.get("href", ""),
                    })
                    found += 1
            if found == 0:
                break  # no products on this page -> end of category
    # de-duplicate (sale + regular price rows)
    rows = list({(r["site"], r["product"], r["url"]): r for r in rows}.values())
    print(f"  -> {len(rows)} products")
    return rows


# ---------------------------------------------------------------------------
# The Baseball Shop - Visualsoft category pages
# ---------------------------------------------------------------------------
TBS_BASE = "https://www.thebaseballshop.co.uk"
TBS_CATEGORIES = [
    ("Bats", "/baseball-bats-c11"),
    ("Balls", "/baseballs-c27"),
    ("Fielding Gloves", "/baseball-gloves-c6"),
    ("Batting Gloves", "/baseball-batting-gloves-c4"),
    ("Bags", "/baseball-bags-c18"),
    ("Clothing", "/baseball-clothing-c71"),
    ("Cleats", "/baseball-cleats-c77"),
    ("Protection", "/baseball-protectives-c1"),
    ("Accessories", "/baseball-accessories-c8"),
    ("Field Equipment", "/baseball-field-equipment-c70"),
]


def extract_from_jsonld(soup):
    """Many storefronts embed product lists as JSON-LD structured data."""
    results = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for block in candidates:
            if not isinstance(block, dict):
                continue
            items = block.get("itemListElement", [])
            for entry in items:
                item = entry.get("item", entry) if isinstance(entry, dict) else {}
                if not isinstance(item, dict):
                    continue
                name = item.get("name", "")
                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = clean_price(offers.get("price") or offers.get("lowPrice"))
                url = item.get("url", "")
                if name and price is not None:
                    results.append((name.strip(), price, url))
    return results


def scrape_tbs():
    print("Scraping The Baseball Shop (category pages)...")
    rows = []
    for category, path in TBS_CATEGORIES:
        for page_num in range(1, 11):
            url = f"{TBS_BASE}{path}" + (f"?page={page_num}" if page_num > 1 else "")
            resp = get(url)
            if resp is None:
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            found = 0

            # 1) structured data if present
            for name, price, purl in extract_from_jsonld(soup):
                rows.append({"site": "The Baseball Shop", "category": category,
                             "product": name, "price": price,
                             "url": purl or url})
                found += 1

            # 2) fallback: product tiles (name link + nearby £ price)
            if found == 0:
                for tile in soup.select("[class*=product]"):
                    link = tile.select_one("a[href*='-p']") or tile.select_one("a[title]")
                    if not link:
                        continue
                    name = (link.get("title") or link.get_text(" ", strip=True)).strip()
                    price = clean_price(tile.get_text(" ", strip=True))
                    if name and price is not None and len(name) > 3:
                        href = link.get("href", "")
                        if href.startswith("/"):
                            href = TBS_BASE + href
                        rows.append({"site": "The Baseball Shop",
                                     "category": category, "product": name,
                                     "price": price, "url": href})
                        found += 1

            if found == 0:
                break
    rows = list({(r["site"], r["product"]): r for r in rows}.values())
    print(f"  -> {len(rows)} products")
    return rows


# ---------------------------------------------------------------------------
# Baseball Outlet - bot-protected, best effort
# ---------------------------------------------------------------------------
def scrape_baseball_outlet():
    print("Scraping Baseball Outlet (bot-protected, best effort)...")
    base = "https://www.baseballoutlet.co.uk"
    # Try the Shopify API route first in case the shop exposes it
    rows = scrape_shopify("Baseball Outlet", base, session=SCRAPER)
    if rows:
        return rows
    print("  ! Baseball Outlet blocked the scraper - no data this run.")
    return []


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_output(rows):
    out_dir = Path(__file__).parent / "data"
    out_dir.mkdir(exist_ok=True)
    today = date.today().isoformat()
    fieldnames = ["date", "site", "category", "product", "price", "url"]

    rows = sorted(rows, key=lambda r: (r["category"], r["product"].lower(), r["site"]))
    for r in rows:
        r["date"] = today

    daily_path = out_dir / f"prices_{today}.csv"
    latest_path = out_dir / "latest_prices.csv"
    for path in (daily_path, latest_path):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {daily_path.name} and latest_prices.csv")

    # Also append to a running history file for trend analysis
    history_path = out_dir / "price_history.csv"
    new_file = not history_path.exists()
    with open(history_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def main():
    all_rows = []
    all_rows += scrape_shopify("Baseball & Softball Shop",
                               "https://www.baseballandsoftball.co.uk")
    all_rows += scrape_shopify("Coach Carter's",
                               "https://coachcarterssports.co.uk",
                               baseball_only=True)
    all_rows += scrape_comet()
    all_rows += scrape_tbs()
    all_rows += scrape_baseball_outlet()

    if not all_rows:
        print("No data collected - check your internet connection.")
        sys.exit(1)
    write_output(all_rows)


if __name__ == "__main__":
    main()
