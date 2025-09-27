# scrapers/foodlion.py
import re
import json
from pathlib import Path
from datetime import datetime, timedelta
import time

import requests
from requests.adapters import HTTPAdapter, Retry
from bs4 import BeautifulSoup

AD_URL = "https://www.foodlion.com/savings/weekly-ad/grid-view"
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "deals_foodlion.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.foodlion.com/",
    "Connection": "keep-alive",
}

def _session():
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.8, status_forcelist=[403, 429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update(HEADERS)
    return s

def _parse_price(text: str):
    if not text:
        return None
    m = re.search(r"\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", text.replace(",", ""))
    return float(m.group(1)) if m else None

def fetch_foodlion_deals() -> list:
    """
    First-pass HTML parser. If the page is JS-rendered or blocks bots by IP,
    this may still fail on Render. In that case, move to Playwright.
    """
    s = _session()
    resp = s.get(AD_URL, timeout=20)
    resp.raise_for_status()
    html = resp.text
    soup = BeautifulSoup(html, "lxml")

    deals = []
    product_cards = soup.find_all(["article", "div", "li"], recursive=True)

    for card in product_cards:
        text = " ".join(card.get_text(" ", strip=True).split())
        if "$" not in text:
            continue

        price = _parse_price(text)
        if not price:
            continue

        name_candidate = None
        for tag in card.find_all(["h2", "h3", "h4", "p", "div"], recursive=True):
            t = tag.get_text(" ", strip=True)
            if t and "$" not in t and len(t) > 2:
                name_candidate = t
                break
        if not name_candidate:
            name_candidate = text.split("$", 1)[0].strip()
            if len(name_candidate) < 3:
                continue

        size_text = None
        for unit_hint in ["per lb", "lb", "oz", "dozen", "each", "ea", "ct", "pk", "pack"]:
            if re.search(rf"\b{unit_hint}\b", text, re.I):
                size_text = unit_hint
                break

        deals.append({
            "store_id": "food-lion-24503",
            "item": name_candidate[:120],
            "size_text": size_text or "",
            "price": price,
            "unit_qty": None,
            "unit": None,
            "start_date": (datetime.utcnow()).date().isoformat(),
            "end_date": (datetime.utcnow() + timedelta(days=7)).date().isoformat(),
            "promo_text": "Weekly Ad",
            "source": AD_URL,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        })

    # de-dup
    seen = set()
    unique = []
    for d in deals:
        key = (d["item"].lower(), d["price"])
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique

def run_and_save() -> int:
    try:
        items = fetch_foodlion_deals()
    except requests.HTTPError as e:
        # Log and return 0 so your API endpoint still returns JSON
        print(f"[foodlion] HTTPError: {e}")
        items = []
    except Exception as e:
        print(f"[foodlion] Error: {e}")
        items = []

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"[foodlion] Saved {len(items)} items -> {OUT_PATH}")
    return len(items)

if __name__ == "__main__":
    n = run_and_save()
    print(f"Saved {n} Food Lion deals -> {OUT_PATH}")
