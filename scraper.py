"""
Baseball competitor price scraper - v2.

Fixes in this version:
  - Any site that refuses the first request is retried with cloudscraper
    (bypasses basic bot detection).
  - Shopify sites: if the products.json feed fails, falls back to reading
    the /collections/all shop pages directly.
  - Coach Carter's: removed the keyword filter that was dropping products.
  - Clearer logging: prints the HTTP status for each site's first page so
    failures are easy to spot in the run log.
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
    STEALTH = cloudscraper.create_scraper()
except Exception:
    STEALTH = None

PLAIN = requests.Session()
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
DELAY = 1.5
PRICE_RE = re.compile(r"£\s*(\d{1,4}(?:,\d{3})?\.\d{2})")

CATEGORY_RULES = [
    ("Batting Gloves", ["batting glove"]),
    ("Catchers Gear", ["catcher", "chest protector", "leg guard", "umpire"]),
    ("Helmets", ["helmet", "face guard", "faceguard", "jaw guard"]),
    ("Bats", ["bat ", " bats", "fungo", "slugger bat", "slowpitch", "slow-pitch",
              "slow pitch", "fastpitch", "fast-pitch", "fast pitch", "wooden bat",
              "youth bat", "-13", "-12", "-11", "-10", "-8", "-5", "-3", "bbcor",
              "usssa", "end loaded", "endloaded"]),
    ("Fielding Gloves", ["fielding glove", "baseball glove", "softball glove",
                         "mitt", "first base", "infield", "outfield", "glove"]),
    ("Balls", ["baseballs", "softballs", "training ball", "practice ball",
               "incrediball", "dozen", " ball", "rolb", "tattered"]),
    ("Cleats", ["cleat", "spike", "turf shoe", "trainers", "footwear", "shoes",
                "molded", "metal low", "metal mid"]),
    ("Training Equipment", ["training", "pitching machine", "batting tee",
                            "tee ", "net", "cage", "screen", "practice",
                            "agility", "swing trainer", "rebounder"]),
    ("Bags", ["bag", "backpack", "wheeled", "duffle", "duffel", "catch all"]),
    ("Protection", ["elbow guard", "shin guard", "sliding mitt", "protective",
                    "wrist guard", "mouthguard", "evoshield", "cup", "guard"]),
    ("Clothing", ["pants", "jersey", "shirt", "jacket", "socks", "belt", "cap",
                  "hat", "beanie", "trousers", "shorts", "hoodie", "tee",
                  "compression", "sleeve", "uniform", "pullover"]),
    ("Field Equipment", ["base set", "bases", "home plate", "pitching rubber",
                         "line marker", "field", "scorebook", "strike zone"]),
    ("Accessories", ["grip", "tape", "pine tar", "eye black", "sunglasses",
                     "accessor", "glove care", "glove oil", "conditioner",
                     "voucher", "gift", "keychain", "lanyard"]),
]


def categorise(name, hint=""):
    text = f"{name} {hint}".lower()
    for category, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in text:
                return category
    return "Other"


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


def get(url, log_status=False):
    """Fetch a URL; if the plain request fails, retry in stealth mode."""
    for label, client in (("plain", PLAIN), ("stealth", STEALTH)):
        if client is None:
            continue
        try:
            resp = client.get(url, headers=HEADERS, timeout=30)
            time.sleep(DELAY)
            if resp.status_code == 200:
                if log_status:
                    print(f"  [{label}] 200 OK  {url}")
                return resp
            if log_status:
                print(f"  [{label}] HTTP {resp.status_code}  {url}")
        except Exception as exc:
            if log_status:
                print(f"  [{label}] error: {exc}  {url}")
    return None


# ---------------------------------------------------------------------------
# Shopify sites (JSON feed with an HTML fallback)
# ---------------------------------------------------------------------------
def scrape_shopify(site_name, base_url):
    print(f"Scraping {site_name}...")
    rows = shopify_json(site_name, base_url)
    if not rows:
        print(f"  products.json gave nothing - falling back to shop pages")
        rows = shopify_html(site_name, base_url)
    print(f"  -> {len(rows)} products")
    return rows


def shopify_json(site_name, base_url):
    rows = []
    page = 1
    while True:
        resp = get(f"{base_url}/products.json?limit=250&page={page}",
                   log_status=(page == 1))
        if resp is None:
            break
        try:
            products = resp.json().get("products", [])
        except (json.JSONDecodeError, ValueError):
            print(f"  ! {site_name}: products.json response was not JSON")
            break
        if not products:
            break
        for p in products:
            name = p.get("title", "").strip()
            hint = f"{p.get('product_type', '')} {' '.join(p.get('tags', []))}"
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
        if page > 40:
            break
    return rows


def shopify_html(site_name, base_url):
    rows, seen = [], set()
    for page_num in range(1, 41):
        resp = get(f"{base_url}/collections/all?page={page_num}",
                   log_status=(page_num == 1))
        if resp is None:
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        found = 0
        for link in soup.select("a[href*='/products/']"):
            href = link.get("href", "").split("?")[0]
            if not href or href in seen:
                continue
            name = (link.get_text(" ", strip=True) or link.get("title") or "").strip()
            if len(name) < 4:
                continue
            container, price = link, None
            for _ in range(4):
                container = container.parent
                if container is None:
                    break
                price = clean_price(container.get_text(" ", strip=True))
                if price is not None:
                    break
            if price is None:
                continue
            seen.add(href)
            full = href if href.startswith("http") else base_url + href
            rows.append({"site": site_name, "category": categorise(name),
                         "product": name, "price": price, "url": full})
            found += 1
        if found == 0:
            break
    return rows


# ---------------------------------------------------------------------------
# Comet Sports (Magento category pages)
# ---------------------------------------------------------------------------
COMET_BASE = "https://www.cometsports.co.uk"
COMET_CATEGORIES = [
    ("Bats", "/baseball-softball-shop-uk/bats.html"),
    ("Balls", "/baseball-softball-shop-uk/balls.html"),
    ("Batting Gloves", "/baseball-softball-shop-uk/batting-gloves.html"),
    ("Fielding Gloves", "/baseball-softball-shop-uk/baseball-gloves-mitts.html"),
    ("Helmets", "/baseball-softball-shop-uk/helmets.html"),
    ("Catchers Gear", "/baseball-softball-shop-uk/catcher-equipment.html"),
    ("Catchers Gear", "/baseball-softball-shop-uk/baseball-umpire.html"),
    ("Cleats", "/baseball-softball-shop-uk/shoes.html"),
    ("Training Equipment", "/baseball-softball-shop-uk/training-equipment.html"),
    ("Bags", "/baseball-softball-shop-uk/bags.html"),
    ("Accessories", "/baseball-softball-shop-uk/accessories.html"),
    ("Clothing", "/baseball-softball-shop-uk/clothing-apparel.html"),
    ("Field Equipment", "/baseball-softball-shop-uk/field-equipment.html"),
]


def scrape_comet():
    print("Scraping Comet Sports...")
    rows = []
    first = True
    for category, path in COMET_CATEGORIES:
        for page_num in range(1, 11):
            url = f"{COMET_BASE}{path}" + (f"?p={page_num}" if page_num > 1 else "")
            resp = get(url, log_status=first)
            first = False
            if resp is None:
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            found = 0
            for item in soup.select("li.product-item, .product-item-info"):
                link = (item.select_one("a.product-item-link")
                        or item.select_one("strong a")
                        or item.select_one("a[href$='.html']"))
                if not link:
                    continue
                name = link.get_text(strip=True)
                price_el = (item.select_one("[data-price-type='finalPrice'] .price")
                            or item.select_one(".special-price .price")
                            or item.select_one(".price"))
                price = clean_price(price_el.get_text(strip=True) if price_el
                                    else item.get_text(" ", strip=True))
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
                break
    rows = list({(r["product"], r["url"]): r for r in rows}.values())
    print(f"  -> {len(rows)} products")
    return rows


# ---------------------------------------------------------------------------
# The Baseball Shop (Visualsoft category pages)
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
    results = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for block in (data if isinstance(data, list) else [data]):
            if not isinstance(block, dict):
                continue
            for entry in block.get("itemListElement", []):
                item = entry.get("item", entry) if isinstance(entry, dict) else {}
                if not isinstance(item, dict):
                    continue
                name = item.get("name", "")
                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = clean_price(offers.get("price") or offers.get("lowPrice"))
                if name and price is not None:
                    results.append((name.strip(), price, item.get("url", "")))
    return results


def scrape_tbs():
    print("Scraping The Baseball Shop...")
    rows = []
    first = True
    for category, path in TBS_CATEGORIES:
        for page_num in range(1, 11):
            url = f"{TBS_BASE}{path}" + (f"?page={page_num}" if page_num > 1 else "")
            resp = get(url, log_status=first)
            first = False
            if resp is None:
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            found = 0
            for name, price, purl in extract_from_jsonld(soup):
                rows.append({"site": "The Baseball Shop", "category": category,
                             "product": name, "price": price, "url": purl or url})
                found += 1
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
    rows = list({(r["product"],) : r for r in rows}.values())
    print(f"  -> {len(rows)} products")
    return rows


# ---------------------------------------------------------------------------
# Baseball Outlet (bot-protected, best effort)
# ---------------------------------------------------------------------------
def scrape_baseball_outlet():
    print("Scraping Baseball Outlet (bot-protected, best effort)...")
    rows = shopify_json("Baseball Outlet", "https://www.baseballoutlet.co.uk")
    if not rows:
        rows = shopify_html("Baseball Outlet", "https://www.baseballoutlet.co.uk")
    if not rows:
        print("  ! Baseball Outlet blocked the scraper - no data this run.")
    else:
        print(f"  -> {len(rows)} products")
    return rows


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

    for path in (out_dir / f"prices_{today}.csv", out_dir / "latest_prices.csv"):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    history_path = out_dir / "price_history.csv"
    new_file = not history_path.exists()
    with open(history_path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} total rows to the data folder.")


def main():
    all_rows = []
    all_rows += scrape_shopify("Baseball & Softball Shop",
                               "https://www.baseballandsoftball.co.uk")
    all_rows += scrape_shopify("Coach Carter's",
                               "https://coachcarterssports.co.uk")
    all_rows += scrape_comet()
    all_rows += scrape_tbs()
    all_rows += scrape_baseball_outlet()

    if not all_rows:
        print("No data collected at all - something is wrong upstream.")
        sys.exit(1)
    write_output(all_rows)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
