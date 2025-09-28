# scrapers/freshmarket.py
import re
import json
from pathlib import Path
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# Fresh Market "Weekly Features" page (marketing page, markup may change)
AD_URL = "https://www.thefreshmarket.com/features/weekly-features"
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "deals_freshmarket.json"


# ---------- Price & unit parsing helpers ----------
def _parse_price(text: str):
    """
    Extract a single comparable item price from text.

    Handles:
      - $5.99 / $ 5 / 5.99/lb
      - 2 for $5  -> 2.50
      - 99¢       -> 0.99
    """
    if not text:
        return None
    t = text.replace("\u00a0", " ")

    # e.g., "2 for $5", "3 for $10"
    m = re.search(r"(\d+)\s*(?:for|/\s*for)\s*\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", t, re.I)
    if m:
        qty = int(m.group(1))
        total = float(m.group(2))
        if qty > 0:
            return round(total / qty, 2)

    # e.g., "$5.99", "$ 5", "5.99/lb"
    m = re.search(r"\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", t)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass

    # e.g., "99¢"
    m = re.search(r"([0-9]+)\s*¢", t)
    if m:
        try:
            return float(m.group(1)) / 100.0
        except Exception:
            pass

    return None


_UNIT_PATTERNS = [
    (r"(\d+(?:\.\d+)?)\s*(oz)\b", "oz"),
    (r"(\d+(?:\.\d+)?)\s*(lb|lbs)\b", "lb"),
    (r"(\d+(?:\.\d+)?)\s*(ct|count)\b", "ct"),
    (r"(\d+(?:\.\d+)?)\s*(pk|pack|pkg)\b", "ct"),
]
_SIZE_HINTS = ["per lb", "lb", "oz", "dozen", "each", "ea", "ct", "pk", "pack"]


def _extract_unit_info(text: str):
    """
    Try to extract a (quantity, unit) pair from text.
    Falls back to unit-only (e.g., per lb) when qty isn't explicit.
    """
    if not text:
        return None, None
    t = text.lower().replace("\u00a0", " ")

    # explicit qty + unit (e.g., "32 oz", "1 lb", "12 ct")
    for pat, unit in _UNIT_PATTERNS:
        m = re.search(pat, t)
        if m:
            try:
                return float(m.group(1)), unit
            except Exception:
                pass

    # "dozen" ~ 12 ct
    if re.search(r"\bdozen\b", t):
        return 12.0, "ct"

    # "each"/"ea" → 1 ct
    if re.search(r"\b(each|ea)\b", t):
        return 1.0, "ct"

    # unit-only hints (e.g., "per lb")
    if re.search(r"\b(per\s*lb|lb)\b", t):
        return None, "lb"

    return None, None


# ---------- HTML extraction ----------
def _extract_deals_from_html(html: str) -> list:
    """
    Heuristic parser that scans common Fresh Market marketing card structures.
    Tune/expand selectors based on what's saved in /debug_freshmarket.html.
    """
    soup = BeautifulSoup(html or "", "lxml")
    items = []

    # Most TFM marketing blocks render into card-like containers.
    card_selectors = [
        # more specific, likely real product tiles
        ".c-card, .card, .grid__item, .product, .product-card, .tile, .teaser, .feature",
        # fallback sweep
        "article, li, div, section",
    ]

    seen = set()

    for sel in card_selectors:
        for card in soup.select(sel):
            txt = " ".join((card.get_text(" ", strip=True) or "").split())
            if "$" not in txt and "¢" not in txt:
                continue

            price = _parse_price(txt)
            if price is None:
                continue

            # Prefer explicit title-ish nodes; fallback to text before first $/¢.
            name = None
            for tag in card.select("h1,h2,h3,h4,strong,.title,.card__title,.product__title,.teaser__title"):
                t = tag.get_text(" ", strip=True)
                if t and "$" not in t and "¢" not in t and len(t) > 2:
                    name = t
                    break
            if not name:
                if "$" in txt:
                    name = txt.split("$", 1)[0].strip()
                elif "¢" in txt:
                    name = txt.split("¢", 1)[0].strip()
                else:
                    name = txt
                if len(name) < 3:
                    continue

            # Size hint string (for readability) + numeric unit extraction (for comparison)
            size_text = None
            for unit_hint in _SIZE_HINTS:
                if re.search(rf"\b{re.escape(unit_hint)}\b", txt, re.I):
                    size_text = unit_hint
                    break
            unit_qty, unit = _extract_unit_info(txt)

            key = (name.lower(), float(price))
            if key in seen:
                continue
            seen.add(key)

            now = datetime.utcnow()
            items.append({
                "store_id": "fresh-market-24503",
                "item": name[:120],
                "size_text": size_text or "",
                "price": float(price),
                "unit_qty": unit_qty,
                "unit": unit,
                "start_date": now.date().isoformat(),
                "end_date": (now + timedelta(days=7)).date().isoformat(),
                "promo_text": "Weekly Features",
                "source": AD_URL,
                "fetched_at": now.isoformat() + "Z",
            })

        if items:
            break  # stop after first selector that produced data

    return items


# ---------- Optional: local helper to dump parsed items from saved HTML ----------
def _parse_saved_debug_html_to_json(debug_html_path: Path = None) -> int:
    """
    Convenience for local testing:
      Reads DATA/debug_freshmarket.html, parses, writes OUT_PATH.
    """
    debug_html_path = debug_html_path or (Path(__file__).resolve().parents[1] / "data" / "debug_freshmarket.html")
    html = debug_html_path.read_text(encoding="utf-8", errors="ignore")
    items = _extract_deals_from_html(html)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(items)
