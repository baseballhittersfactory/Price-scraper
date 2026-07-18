"""
Baseball competitor price scraper - v7.

Changes in this version:
  - Baseball Outlet: full recursive crawl with a 500-page budget. Their menu
    alone has 200+ category pages, so earlier versions ran out before
    reaching balls, batting gloves, helmets and the other later sections.
  - Progress is logged every 50 pages so long runs are visibly alive.
"""

import csv
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

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
NOISE_RE = re.compile(
    r"\b(out of stock|free delivery|add to wish list|special price|"
    r"regular price|as low as|availability:.*|only \d+ left|sale|new)\b",
    re.IGNORECASE,
)
NON_PRODUCT_SLUGS = {
    "about_us", "about-us", "contact", "delivery", "terms", "faqs", "faq",
    "privacy_policy", "privacy-policy", "return_policy", "returns",
    "support_page", "payment_security", "payment_options", "sizing",
    "brands", "wishlist", "team-hub", "finance", "sitemap",
}

CATEGORY_RULES = [
    ("Batting Gloves", ["batting glove"]),
    ("Catchers Gear", ["catcher", "chest protector", "leg guard", "umpire"]),
    ("Helmets", ["helmet", "face guard", "faceguard", "jaw guard"]),
    ("Bats", ["bat ", " bats", "fungo", "slowpitch", "slow-pitch", "slow pitch",
              "fastpitch", "fast-pitch", "fast pitch", "wooden bat", "youth bat",
              "bbcor", "usssa", "tee ball", "teeball", "t-ball"]),
    ("Fielding Gloves", ["fielding glove", "baseball glove", "softball glove",
                         "mitt", "first base", "infield", "outfield", "glove"]),
    ("Balls", ["baseballs", "softballs", "training ball", "practice ball",
               "incrediball", "dozen", " ball", "rolb"]),
    ("Cleats", ["cleat", "spike", "turf shoe", "trainers", "footwear", "shoes"]),
    ("Training Equipment", ["training", "pitching machine", "batting tee",
                            "tee ", "net", "cage", "screen", "practice",
                            "agility", "swing trainer", "rebounder"]),
    ("Bags", ["bag", "backpack", "wheeled", "duffle", "duffel"]),
    ("Protection", ["elbow guard", "shin guard", "sliding mitt", "protective",
                    "wrist guard", "mouthguard", "evoshield", "cup", "guard"]),
    ("Clothing", ["pants", "jersey", "shirt", "jacket", "socks", "belt", "cap",
                  "hat", "beanie", "trousers", "shorts", "hoodie", "tee",
                  "compression", "sleeve", "uniform", "pullover"]),
    ("Field Equipment", ["base set", "bases", "home plate", "pitching rubber",
                         "line marker", "field", "scorebook", "strike zone"]),
    ("Accessories", ["grip", "tape", "pine tar", "eye black", "sunglasses",
                     "accessor", "glove care", "glove oil", "conditioner",
                     "voucher", "gift"]),
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


def clean_name(text):
    text = NOISE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" -\u00b7")
    words = text.split(" ")
    half = len(words) // 2
    if half >= 2 and words[:half] == words[half:half * 2] and len(words) % 2 == 0:
        text = " ".join(words[:half])
    return text.strip()


def get(url, log_status=False):
    for label, client in (("plain", PLAIN), ("stealth", STEALTH)):
        if client is None:
            continue
        try:
            resp = client.get(url, headers=HEADERS, timeout=30)
            time.sleep(DELAY)
            if resp.status_code == 429:
                if log_status:
                    print(f"  [{label}] HTTP 429 (rate limited) - waiting 30s and retrying")
                time.sleep(30)
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
# Generic Magento listing extractor (Comet Sports + Baseball Outlet)
# ---------------------------------------------------------------------------
def magento_products(soup, base_url):
    """Extract products from a Magento listing page.

    Tries the standard Magento product tiles first; if the theme is custom
    (like Comet's), falls back to a generic scan for root-level product
    links with a price next to them."""
    domain = urlparse(base_url).netloc
    best = {}

    # 1) standard Magento markup
    for link in soup.select("a.product-item-link"):
        href = link.get("href", "").split("?")[0]
        if not href:
            continue
        name = clean_name(link.get_text(" ", strip=True))
        if len(name) < 5:
            continue
        price, container = None, link
        for _ in range(5):
            container = container.parent
            if container is None:
                break
            price = clean_price(container.get_text(" ", strip=True))
            if price is not None:
                break
        if price is None:
            continue
        url = href if href.startswith("http") else base_url + href
        current = best.get(url)
        if current is None or len(name) < len(current[0]):
            best[url] = (name, price)
    if best:
        return [(n, p, u) for u, (n, p) in best.items()]

    # 2) generic fallback (custom themes)
    for link in soup.find_all("a", href=True):
        href = link["href"].split("?")[0]
        parsed = urlparse(href)
        if parsed.netloc and parsed.netloc != domain:
            continue
        path = parsed.path
        if not path.endswith(".html"):
            continue
        segments = [s for s in path.strip("/").split("/") if s]
        if len(segments) != 1:            # categories are multi-segment
            continue
        slug = segments[0][:-5]
        if slug in NON_PRODUCT_SLUGS:
            continue
        name = clean_name(link.get_text(" ", strip=True) or link.get("title") or "")
        if len(name) < 5 or name.startswith("£"):
            continue
        price, container = None, link
        for _ in range(5):
            container = container.parent
            if container is None:
                break
            price = clean_price(container.get_text(" ", strip=True))
            if price is not None:
                break
        if price is None:
            continue
        url = href if href.startswith("http") else base_url + path
        current = best.get(url)
        if current is None or len(name) < len(current[0]):
            best[url] = (name, price)
    return [(n, p, u) for u, (n, p) in best.items()]


def scrape_magento_categories(site_name, base_url, categories):
    rows = {}
    first = True

    def add(products, page_category):
        new = 0
        for name, price, url in products:
            if url in rows:
                continue
            own = categorise(name)
            rows[url] = {"site": site_name,
                         "category": own if own != "Other" else page_category,
                         "product": name, "price": price, "url": url}
            new += 1
        return new

    for category, path in categories:
        base_products = None
        for suffix in ("?product_list_limit=all", ""):
            resp = get(f"{base_url}{path}{suffix}", log_status=first)
            first = False
            if resp is None:
                continue
            products = magento_products(BeautifulSoup(resp.text, "html.parser"),
                                        base_url)
            if products:
                base_products = products
                break
        if not base_products:
            continue
        add(base_products, category)
        # extra pages in case the site ignored the show-all setting
        if len(base_products) >= 18:
            for page_num in range(2, 11):
                resp = get(f"{base_url}{path}?p={page_num}")
                if resp is None:
                    break
                products = magento_products(
                    BeautifulSoup(resp.text, "html.parser"), base_url)
                if not products or add(products, category) == 0:
                    break
    out = list(rows.values())
    print(f"  -> {len(out)} products")
    return out


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
    return scrape_magento_categories("Comet Sports", COMET_BASE, COMET_CATEGORIES)


OUTLET_BASE = "https://www.baseballoutlet.co.uk"
OUTLET_PAGE_BUDGET = 500


def outlet_category_links(soup):
    """Category links on a page: same-domain, multi-segment .html, excluding
    links that are product tiles on this page."""
    domain = urlparse(OUTLET_BASE).netloc
    product_hrefs = set()
    for a in soup.select("a.product-item-link"):
        href = a.get("href", "").split("?")[0]
        product_hrefs.add(href)
        product_hrefs.add(urlparse(href).path)
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0]
        parsed = urlparse(href)
        if parsed.netloc and parsed.netloc != domain:
            continue
        path = parsed.path
        segments = [s for s in path.strip("/").split("/") if s]
        if not path.endswith(".html") or len(segments) < 2:
            continue
        if href in product_hrefs or path in product_hrefs:
            continue
        links.add(path)
    return links


def scrape_baseball_outlet():
    print("Scraping Baseball Outlet (recursive crawl)...")
    resp = get(OUTLET_BASE + "/", log_status=True)
    if resp is None:
        print("  ! Baseball Outlet blocked the scraper - no data this run.")
        return []
    home = BeautifulSoup(resp.text, "html.parser")
    queue = sorted(outlet_category_links(home))
    if not queue:
        print("  ! Could not find any category pages in the menu.")
        return []
    print(f"  starting from {len(queue)} category pages found in the menu")

    rows, visited = {}, set()

    def add(products):
        new = 0
        for name, price, url in products:
            if url in rows:
                continue
            cat = categorise(name)
            rows[url] = {"site": "Baseball Outlet", "category": cat,
                         "product": name, "price": price, "url": url}
            new += 1
        return new

    while queue and len(visited) < OUTLET_PAGE_BUDGET:
        path = queue.pop(0)
        if path in visited:
            continue
        visited.add(path)
        if len(visited) % 50 == 0:
            print(f"  ...{len(visited)} pages crawled, "
                  f"{len(rows)} products so far, {len(queue)} pages queued")
        products, page_soup = None, None
        for suffix in ("?product_list_limit=all", ""):
            resp = get(f"{OUTLET_BASE}{path}{suffix}")
            if resp is None:
                continue
            page_soup = BeautifulSoup(resp.text, "html.parser")
            found = magento_products(page_soup, OUTLET_BASE)
            if found:
                products = found
                break
        if page_soup is not None:
            for link in outlet_category_links(page_soup):
                if link not in visited:
                    queue.append(link)
        if not products:
            continue
        add(products)
        if len(products) >= 18:
            for page_num in range(2, 11):
                resp = get(f"{OUTLET_BASE}{path}?p={page_num}")
                if resp is None:
                    break
                more = magento_products(
                    BeautifulSoup(resp.text, "html.parser"), OUTLET_BASE)
                if not more or add(more) == 0:
                    break
    out = list(rows.values())
    print(f"  crawled {len(visited)} pages")
    print(f"  -> {len(out)} products")
    return out


# ---------------------------------------------------------------------------
# Shopify sites (unchanged - both working)
# ---------------------------------------------------------------------------
def scrape_shopify(site_name, base_url):
    print(f"Scraping {site_name}...")
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
    print(f"  -> {len(rows)} products")
    return rows


# ---------------------------------------------------------------------------
# The Baseball Shop (unchanged - working)
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
    rows = list({(r["product"],): r for r in rows}.values())
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
