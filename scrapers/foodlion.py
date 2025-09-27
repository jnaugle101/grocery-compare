# scrapers/foodlion.py
import re
import json
from pathlib import Path
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from flask import Response

AD_URL = "https://www.foodlion.com/savings/weekly-ad/grid-view"
OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "deals_foodlion.json"
ZIP = "24503"

def _parse_price(text: str):
    if not text:
        return None
    m = re.search(r"\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", text.replace(",", ""))
    return float(m.group(1)) if m else None

def _extract_deals_from_html(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    deals = []
    # Heuristic: scan containers; tighten once you inspect the DOM
    cards = soup.find_all(["article", "div", "li"], recursive=True)
    for card in cards:
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
            "start_date": datetime.utcnow().date().isoformat(),
            "end_date": (datetime.utcnow() + timedelta(days=7)).date().isoformat(),
            "promo_text": "Weekly Ad",
            "source": AD_URL,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        })

    # de-dup
    seen, unique = set(), []
    for d in deals:
        key = (d["item"].lower(), d["price"])
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique

async def _set_zip_and_select_store(page):
    # Cookie banner (try common buttons)
    for selector in [
        'button:has-text("Accept All")',
        'button:has-text("Accept")',
        'button[aria-label="Accept all"]',
        'button:has-text("I Accept")',
    ]:
        try:
            await page.locator(selector).click(timeout=1500)
            break
        except PWTimeout:
            pass

    # Try to open "Change/Set Store" flow
    # We attempt several likely buttons/links—site markup can change.
    store_openers = [
        'button:has-text("Change Store")',
        'button:has-text("Set My Store")',
        'a:has-text("Change Store")',
        'a:has-text("Set My Store")',
        '[data-testid="change-store"]',
    ]
    opened = False
    for sel in store_openers:
        try:
            await page.locator(sel).first.click(timeout=2500)
            opened = True
            break
        except PWTimeout:
            continue

    # If no explicit opener, sometimes a store picker is inline—try focusing the input directly.
    search_inputs = [
        'input[placeholder*="ZIP"]',
        'input[aria-label*="ZIP"]',
        'input[placeholder*="zip"]',
        'input[aria-label*="zip"]',
        'input[type="search"]',
    ]
    for sel in search_inputs:
        try:
            inp = page.locator(sel).first
            await inp.fill(ZIP, timeout=2500)
            await inp.press("Enter")
            break
        except PWTimeout:
            continue

    # Pick the first store result if a list appears
    possible_store_cards = [
        '[data-testid="store-card"] button:has-text("Select")',
        'button:has-text("Make This My Store")',
        'button:has-text("Select Store")',
    ]
    for sel in possible_store_cards:
        try:
            await page.locator(sel).first.click(timeout=4000)
            break
        except PWTimeout:
            continue

    # Give the ad grid time to reload for the chosen store
    await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_timeout(1500)

async def fetch_foodlion_deals_async() -> list:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            locale="en-US",
        )
        page = await context.new_page()
        await page.goto(AD_URL, wait_until="networkidle", timeout=45000)

        # Try to ensure ZIP is set so deals load
        try:
            await _set_zip_and_select_store(page)
        except Exception as e:
            print(f"[foodlion] ZIP/store setup warning: {e}")

        # Nudge lazy-loaded content
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1500)

        html = await page.content()
        await context.close()
        await browser.close()
    return _extract_deals_from_html(html)

# scrapers/foodlion.py (only this function needs replacing)
async def run_and_save_async() -> int:
    debug_path = OUT_PATH.parent / "debug_foodlion.html"
    try:
        items = await fetch_foodlion_deals_async()
        # Save last-rendered HTML for inspection if nothing parsed
        # (fetch_foodlion_deals_async returns the HTML via internal call; adjust to return both if needed)
    except Exception as e:
        print(f"[foodlion] Error: {e}")
        items = []

    # If you want to always keep a snapshot from the last run, change the scraper to return html too.
    # Quick workaround: call the renderer again and save its content only for debugging:
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"),
                locale="en-US",
            )
            page = await context.new_page()
            await page.goto(AD_URL, wait_until="networkidle", timeout=45000)
            # try to set ZIP/store again quickly (non-fatal if fails)
            try:
                await _set_zip_and_select_store(page)
            except Exception as e:
                print(f"[foodlion] ZIP/store setup warning (debug snapshot): {e}")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(800)
            html = await page.content()
            await context.close()
            await browser.close()

        debug_path.parent.mkdir(parents=True, exist_ok=True)
        with debug_path.open("w", encoding="utf-8") as f:
            f.write(html)
        print(f"[foodlion] Wrote debug HTML -> {debug_path}")
    except Exception as e:
        print(f"[foodlion] Failed to write debug HTML: {e}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"[foodlion] Saved {len(items)} items -> {OUT_PATH}")
    return len(items)
