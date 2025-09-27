# scrapers/freshmarket.py
import re
import json
from pathlib import Path
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import asyncio

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

AD_URL = "https://www.thefreshmarket.com/features/weekly-features"
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
OUT_PATH = DATA_DIR / "deals_freshmarket.json"
DEBUG_HTML = DATA_DIR / "debug_freshmarket.html"

def _parse_price(text: str):
    if not text:
        return None
    m = re.search(r"\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", text.replace(",", ""))
    return float(m.group(1)) if m else None

def _extract_deals_from_html(html: str) -> list:
    """
    Very loose first pass. We mostly want to prove the flow works.
    Tighten selectors once we see the real HTML in DEBUG_HTML.
    """
    soup = BeautifulSoup(html, "lxml")
    deals = []

    # Heuristic scan for price-y cards
    cards = soup.find_all(["article", "div", "li"], recursive=True)
    for c in cards:
        text = " ".join(c.get_text(" ", strip=True).split())
        if "$" not in text:
            continue
        price = _parse_price(text)
        if not price:
            continue

        name = None
        for tag in c.find_all(["h2", "h3", "h4", "p", "div"], recursive=True):
            t = tag.get_text(" ", strip=True)
            if t and "$" not in t and len(t) > 2:
                name = t
                break
        if not name:
            name = text.split("$", 1)[0].strip()
            if len(name) < 3:
                continue

        size_text = None
        for hint in ["per lb", "lb", "oz", "dozen", "each", "ea", "ct", "pk", "pack"]:
            if re.search(rf"\b{hint}\b", text, re.I):
                size_text = hint
                break

        deals.append({
            "store_id": "fresh-market-boonsboro",  # adjust later
            "item": name[:120],
            "size_text": size_text or "",
            "price": price,
            "unit_qty": None,
            "unit": None,
            "start_date": datetime.utcnow().date().isoformat(),
            "end_date": (datetime.utcnow() + timedelta(days=7)).date().isoformat(),
            "promo_text": "Weekly Features",
            "source": AD_URL,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        })

    # De-dup
    seen, unique = set(), []
    for d in deals:
        key = (d["item"].lower(), d["price"])
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique

def _find_chromium_executable() -> str | None:
    import os, glob
    for base in ("/opt/render/project/src/.playwright", "/opt/render/.cache/ms-playwright"):
        hits = sorted(glob.glob(os.path.join(base, "chromium-*", "chrome-linux", "chrome")))
        if hits:
            return hits[-1]
    return None

async def _fetch_html() -> str:
    chromium_path = _find_chromium_executable()
    if not chromium_path:
        raise RuntimeError("Chromium not found on server.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=chromium_path)
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            locale="en-US",
        )
        page = await ctx.new_page()
        await page.goto(AD_URL, wait_until="networkidle", timeout=45000)

        # Gentle nudge for lazy loads
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1200)

        html = await page.content()
        await ctx.close()
        await browser.close()
        return html

def run_and_save() -> int:
    """Synchronous entrypoint used by /scrape/freshmarket."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        html = asyncio.run(_fetch_html())
    except Exception as e:
        # Write a clue to disk for /debug/files
        DEBUG_HTML.write_text(f"ERROR fetching Fresh Market: {e}", encoding="utf-8")
        # Save empty JSON so endpoint still responds cleanly
        OUT_PATH.write_text("[]", encoding="utf-8")
        return 0

    # Save snapshot for inspection
    DEBUG_HTML.write_text(html, encoding="utf-8")

    # Parse and save JSON
    items = _extract_deals_from_html(html)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    return len(items)
