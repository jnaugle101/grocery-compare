# scrapers/freshmarket.py
import re
import json
from pathlib import Path
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# Fresh Market "Weekly Features" (public page; may change)
AD_URL = "https://www.thefreshmarket.com/features/weekly-features"
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "deals_freshmarket.json"

def _parse_price(text: str):
    if not text:
        return None
    m = re.search(r"\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", text.replace(",", ""))
    return float(m.group(1)) if m else None

def _extract_deals_from_html(html: str) -> list:
    """
    Heuristic parser. Tune selectors after inspecting /debug/freshmarket HTML.
    """
    soup = BeautifulSoup(html, "lxml")
    items = []

    # Try a few common card containers used on TFM marketing pages
    card_selectors = [
        # marketing grid cards (e.g., product tiles)
        ".c-card, .card, .grid__item",
        # fallback: list items with a price inside
        "li, article, div",
    ]

    seen = set()

    for sel in card_selectors:
        for card in soup.select(sel):
            txt = " ".join(card.get_text(" ", strip=True).split())
            if "$" not in txt:
                continue
            price = _parse_price(txt)
            if not price:
                continue

            name = None
            # Prefer headings or strong labels
            for tag in card.select("h1,h2,h3,h4,strong,.title,.card__title"):
                t = tag.get_text(" ", strip=True)
                if t and "$" not in t and len(t) > 2:
                    name = t
                    break
            if not name:
                # fallback: first words before the first $
                name = txt.split("$", 1)[0].strip()
                if len(name) < 3:
                    continue

            size_text = None
            for unit_hint in ["per lb", "lb", "oz", "dozen", "each", "ea", "ct", "pk", "pack"]:
                if re.search(rf"\b{unit_hint}\b", txt, re.I):
                    size_text = unit_hint
                    break

            key = (name.lower(), price)
            if key in seen:
                continue
            seen.add(key)

            items.append({
                "store_id": "fresh-market-24503",
                "item": name[:120],
                "size_text": size_text or "",
                "price": float(price),
                "unit_qty": None,
                "unit": None,
                "start_date": datetime.utcnow().date().isoformat(),
                "end_date": (datetime.utcnow() + timedelta(days=7)).date().isoformat(),
                "promo_text": "Weekly Features",
                "source": AD_URL,
                "fetched_at": datetime.utcnow().isoformat() + "Z",
            })

        if items:
            break  # stop after first selector that worked

    return items
