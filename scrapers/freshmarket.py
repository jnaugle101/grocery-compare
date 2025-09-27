# scrapers/freshmarket.py
import re, json
from pathlib import Path
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import requests

AD_URL = "https://www.thefreshmarket.com/features/weekly-features"
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "deals_freshmarket.json"

def _parse_price(text: str):
    if not text:
        return None
    m = re.search(r"\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", text.replace(",", ""))
    return float(m.group(1)) if m else None

def _extract(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    deals = []
    cards = soup.find_all(["article","div","li"])
    for card in cards:
        text = " ".join(card.get_text(" ", strip=True).split())
        if "$" not in text:
            continue
        price = _parse_price(text)
        if not price:
            continue

        name = None
        for tag in card.find_all(["h2","h3","h4","p"], recursive=True):
            t = tag.get_text(" ", strip=True)
            if t and "$" not in t and len(t) > 2:
                name = t
                break
        if not name:
            continue

        deals.append({
            "store_id": "fresh-market-24503",
            "item": name[:120],
            "price": price,
            "unit_qty": None,
            "unit": None,
            "start_date": datetime.utcnow().date().isoformat(),
            "end_date": (datetime.utcnow()+timedelta(days=7)).date().isoformat(),
            "promo_text": "Weekly Ad",
            "source": AD_URL,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        })
    return deals

def run_and_save() -> int:
    r = requests.get(AD_URL, timeout=30)
    items = _extract(r.text)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"[freshmarket] Saved {len(items)} items -> {OUT_PATH}")
    return len(items)
