# scrapers/freshmarket.py
import re
import json
from pathlib import Path
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

AD_URL = "https://www.thefreshmarket.com/features/weekly-features"
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "deals_freshmarket.json"
DEBUG_HTML = Path(__file__).resolve().parents[1] / "data" / "debug_freshmarket.html"
STORE_ID = "fresh-market-24503"  # MVP tag

def _parse_price(text: str):
    if not text:
        return None
    m = re.search(r"\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", text.replace(",", ""))
    return float(m.group(1)) if m else None

def _extract_deals_from_html(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    deals = []

    # Heuristic sweep: look for elements that contain a $ price and nearby title text.
    # This is intentionally loose so we get *something* to inspect on first pass.
    cards = soup.find_all(["article", "div", "li", "section"], recursive=True)

    for card in cards:
        text = " ".join(card.get_text(" ", strip=True).split())
        if "$" not in text:
            continue

        price = _parse_price(text)
        if not price:
            continue

        name_candidate = None
        for tag in card.find_all(["h1", "h2", "h3", "h4", "p", "span", "div"], recursive=True):
            t = tag.get_text(" ", strip=True)
            if t and "$" not in t and len(t) > 2:
                name_candidate = t
                break
        if not name_candidate:
            # fallback: text before first $
            name_candidate = text.split("$", 1)[0].strip()
            if len(name_candidate) < 3:
                continue

        size_text = None
        for unit_hint in ["per lb", "lb", "oz", "dozen", "each", "ea", "ct", "pk", "pack"]:
            if re.search(rf"\b{unit_hint}\b", text, re.I):
                size_text = unit_hint
                break

        deals.append({
            "store_id": STORE_ID,
            "item": name_candidate[:120],
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

    # de-dup by (name, price)
    seen, out = set(), []
    for d in deals:
        key = (d["item"].lower(), d["price"])
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out

async def _fetch_page_html() -> str:
    """
    On Render we can’t run 'playwright install --with-deps' at runtime,
    so we rely on the Chromium downloaded during build and let Playwright
    auto-find the executable (our app’s /debug/playwright already verified that).
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)  # auto-resolve chromium we installed at build
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            locale="en-US",
        )
        page = await ctx.new_page()
        await page.goto(AD_URL, wait_until="networkidle", timeout=45000)

        # Nudge lazy content; Fresh Market often lazy-loads tiles
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1200)

        html = await page.content()
        await ctx.close()
        await browser.close()
    return html

def run_and_save() -> int:
    """
    Synchronous entry: fetch with Playwright, save debug HTML,
    parse, save JSON, return count.
    """
    # Run Playwright in a tiny event loop for sync call
    import asyncio
    html = asyncio.run(_fetch_page_html())

    DEBUG_HTML.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_HTML.write_text(html, encoding="utf-8")

    items = _extract_deals_from_html(html)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    return len(items)
