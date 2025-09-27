# scrapers/foodlion.py
import re
import json
import time
from pathlib import Path
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

AD_URL = "https://www.foodlion.com/savings/weekly-ad/grid-view"
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "deals_foodlion.json"

def _parse_price(text: str):
    """
    Parse prices like '$1.99', '1.99', '$5', etc.
    Returns float or None.
    """
    if not text:
        return None
    m = re.search(r"\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", text.replace(",", ""))
    return float(m.group(1)) if m else None

def fetch_foodlion_deals() -> list:
    """
    Very first-pass HTML parser for Food Lion weekly ad grid.
    NOTE: Sites change. This targets commonly found classes/text on the grid.
    If the page is JS-heavy, you may need Playwright later.
    """
    resp = requests.get(AD_URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    html = resp.text
    soup = BeautifulSoup(html, "lxml")

    deals = []
    # Heuristics: look for cards that contain both a name/description and a price
    # Common patterns: product title in <h3>/<h4>/<div>, price in elements with '$'
    product_cards = soup.find_all(["article", "div", "li"], recursive=True)

    for card in product_cards:
        text = " ".join(card.get_text(" ", strip=True).split())
        if "$" not in text:
            continue

        # Try to split name vs price heuristically
        price = _parse_price(text)
        if not price:
            continue

        # Guess item name as the first non-price phrase up to ~80 chars
        # You can refine by restricting to specific child tags/classes if needed.
        name_candidate = None
        for tag in card.find_all(["h2", "h3", "h4", "p", "div"], recursive=True):
            t = tag.get_text(" ", strip=True)
            if t and "$" not in t and len(t) > 2:
                name_candidate = t
                break
        if not name_candidate:
            # fallback: first words before the $ sign
            name_candidate = text.split("$", 1)[0].strip()
            if len(name_candidate) < 3:
                continue

        # Try to find unit/size hints
        size_text = None
        for unit_hint in ["per lb", "lb", "oz", "dozen", "each", "ea", "ct", "pk", "pack"]:
            if re.search(rf"\b{unit_hint}\b", text, re.I):
                size_text = unit_hint
                break

        deals.append({
            "store_id": "food-lion-24503",   # generic for MVP
            "item": name_candidate[:120],
            "size_text": size_text or "",
            "price": price,
            "unit_qty": None,                # normalize later
            "unit": None,
            "start_date": (datetime.utcnow()).date().isoformat(),
            "end_date": (datetime.utcnow() + timedelta(days=7)).date().isoformat(),
            "promo_text": "Weekly Ad",
            "source": AD_URL,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        })

    # de-dup by item name + price (simple MVP)
    seen = set()
    unique = []
    for d in deals:
        key = (d["item"].lower(), d["price"])
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique

def run_and_save() -> int:
    items = fetch_foodlion_deals()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    return len(items)

if __name__ == "__main__":
    n = run_and_save()
    print(f"Saved {n} Food Lion deals -> {OUT_PATH}")
