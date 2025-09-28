# app.py
import json
import glob
import os
import asyncio
from pathlib import Path

from flask import Flask, jsonify, request, Response
from utils.normalize import compute_unit_price

app = Flask(__name__)
DATA_DIR = Path(__file__).parent / "data"

# -------------------- helpers --------------------
def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def _find_chromium_executable():
    # Prefer build-installed path; fall back to default cache.
    for base in ("/opt/render/project/src/.playwright", "/opt/render/.cache/ms-playwright"):
        hits = sorted(glob.glob(os.path.join(base, "chromium-*", "chrome-linux", "chrome")))
        if hits:
            return hits[-1]
    return None

# -------------------- debug: saved HTML views --------------------
@app.get("/debug/foodlion")
def debug_foodlion_page():
    p = DATA_DIR / "debug_foodlion.html"
    if not p.exists():
        return {"ok": False, "error": "No debug HTML found yet. Run /scrape/foodlion first."}, 404
    return Response(p.read_text(encoding="utf-8", errors="ignore"), mimetype="text/html")

@app.get("/debug/freshmarket")
def debug_freshmarket_page():
    p = DATA_DIR / "debug_freshmarket.html"
    if not p.exists():
        return {"ok": False, "error": "No Fresh Market debug HTML yet. Run /scrape/freshmarket first."}, 404
    return Response(p.read_text(encoding="utf-8", errors="ignore"), mimetype="text/html")

@app.get("/debug/freshmarket.png")
def debug_freshmarket_png():
    p = DATA_DIR / "debug_freshmarket.png"
    if not p.exists():
        return {"ok": False, "error": "No Fresh Market screenshot yet. Run /scrape/freshmarket first."}, 404
    return Response(p.read_bytes(), mimetype="image/png")

# -------------------- debug: playwright sanity --------------------
@app.get("/debug/playwright")
def debug_playwright():
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        return {"ok": False, "where": "import", "error": str(e)}, 500

    async def probe():
        chromium_path = _find_chromium_executable()
        if not chromium_path:
            return {"ok": False, "where": "resolve", "error": "Chromium not found in expected paths."}
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, executable_path=chromium_path)
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.goto("https://example.com", timeout=20000)
            html = await page.content()
            await ctx.close(); await browser.close()
            return {"ok": True, "html_len": len(html), "chromium_path": chromium_path}

    try:
        return asyncio.run(probe())
    except Exception as e:
        return {"ok": False, "where": "run", "error": str(e)}, 500

# -------------------- basics --------------------
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/routes")
def list_routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))

@app.get("/debug/files")
def list_data_files():
    try:
        files = []
        if DATA_DIR.exists():
            for p in sorted(DATA_DIR.glob("*")):
                files.append({"name": p.name, "size": p.stat().st_size})
        return jsonify({"dir": str(DATA_DIR), "files": files})
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# -------------------- API: stores / deals / compare --------------------
@app.get("/stores")
def stores():
    """Return stores by zip (defaults to 24503)"""
    zip_code = request.args.get("zip", "24503")
    stores = load_json(DATA_DIR / "stores_24503.json")
    return jsonify([s for s in stores if s["zip"] == zip_code or zip_code == "24503"])

@app.get("/deals")
def deals():
    """Return deals (sample + any scraped files)"""
    _ = request.args.get("zip", "24503")  # reserved for future filter
    merged = []

    for fname in ("deals_sample_24503.json", "deals_foodlion.json", "deals_freshmarket.json"):
        p = DATA_DIR / fname
        if p.exists() and p.stat().st_size > 0:
            merged.extend(load_json(p))

    for d in merged:
        qty = d.get("unit_qty")
        price = d.get("price")
        try:
            if qty and price and float(qty) > 0:
                d["unit_price"] = round(float(price) / float(qty), 4)
            else:
                d["unit_price"] = None
        except Exception:
            d["unit_price"] = None

    return jsonify(merged)

@app.get("/compare")
def compare():
    """MVP: simple greedy compare by item substring."""
    items_q = request.args.get("items", "")
    if not items_q:
        return jsonify({"error": "missing items param"}), 400
    wanted = [x.strip().lower() for x in items_q.split(",") if x.strip()]

    deals = load_json(DATA_DIR / "deals_sample_24503.json")
    for d in deals:
        d["unit_price"] = compute_unit_price(d.get("price"), d.get("unit_qty"))

    picks = []
    for w in wanted:
        candidates = [d for d in deals if w in d["item"].lower()]
        if candidates:
            best = sorted(
                candidates,
                key=lambda x: (x.get("unit_price") if x.get("unit_price") is not None else 9e9, x["price"])
            )[0]
            picks.append(best)

    total_cost = sum([p["price"] for p in picks]) if picks else 0.0
    store_breakdown = {}
    for p in picks:
        store_breakdown.setdefault(p["store_id"], {"items": [], "subtotal": 0.0})
        store_breakdown[p["store_id"]]["items"].append(p["item"])
        store_breakdown[p["store_id"]]["subtotal"] += p["price"]

    return jsonify({
        "requested_items": wanted,
        "picks": picks,
        "total_items_found": len(picks),
        "estimated_total": round(total_cost, 2),
        "by_store": store_breakdown
    })

# -------------------- scrape: Food Lion --------------------
@app.route("/scrape/foodlion", methods=["POST", "GET"])
def scrape_foodlion():
    import traceback
    from playwright.async_api import async_playwright
    from scrapers.foodlion import AD_URL, OUT_PATH, _extract_deals_from_html

    async def run_and_save_both():
        chromium_path = _find_chromium_executable()
        if not chromium_path:
            return {"ok": False, "error": "Chromium not found on server."}

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
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)
            html = await page.content()
            await ctx.close(); await browser.close()

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "debug_foodlion.html").write_text(html, encoding="utf-8")

        items = _extract_deals_from_html(html)
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUT_PATH.open("w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

        return {"ok": True, "saved_items": len(items),
                "saved_html": str(DATA_DIR / "debug_foodlion.html"),
                "saved_json": str(OUT_PATH)}

    try:
        return asyncio.run(run_and_save_both()), 200
    except Exception:
        err = traceback.format_exc()
        (DATA_DIR / "foodlion_error.txt").write_text(err, encoding="utf-8")
        return {"ok": False, "error": err}, 500

# -------------------- scrape: Fresh Market --------------------
@app.route("/scrape/freshmarket", methods=["POST", "GET"])
def scrape_freshmarket():
    import traceback
    from playwright.async_api import async_playwright
    from scrapers.freshmarket import AD_URL, OUT_PATH, _extract_deals_from_html

    async def run_and_save_both():
        chromium_path = _find_chromium_executable()
        if not chromium_path:
            return {"ok": False, "error": "Chromium not found on server."}

        html = ""
        js_items = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                executable_path=chromium_path,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            ctx = await browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"),
                locale="en-US",
                viewport={"width": 1366, "height": 900},
            )
            page = await ctx.new_page()
            page.set_default_timeout(60000)

            # Don’t use networkidle; these sites keep sockets open.
            await page.goto(AD_URL, wait_until="domcontentloaded", timeout=60000)

            # Try to nudge lazy content & close common banners
            for sel in [
                '#onetrust-accept-btn-handler',
                'button:has-text("Accept")',
                'button:has-text("Allow All")',
                'button:has-text("I Agree")',
                '[aria-label="Accept"]',
            ]:
                try:
                    await page.locator(sel).first.click(timeout=1500)
                    break
                except Exception:
                    pass
            for sel in [
                '[aria-label="Close"]',
                'button[aria-label="Close"]',
                'button:has-text("Close")',
                '.modal [data-dismiss="modal"]',
                '.mfp-close',
            ]:
                try:
                    await page.locator(sel).first.click(timeout=1500)
                    break
                except Exception:
                    pass

            try:
                await page.wait_for_timeout(800)
                await page.evaluate("""
                    const sleep = (ms)=>new Promise(r=>setTimeout(r,ms));
                    (async () => {
                      for (let y=0; y<=document.body.scrollHeight; y+=800) {
                        window.scrollTo(0, y);
                        await sleep(200);
                      }
                      window.scrollTo(0, document.body.scrollHeight);
                    })();
                """)
                await page.wait_for_timeout(1200)
            except Exception:
                pass

            # In-page extraction (may include items missing price)
            js_items = await page.evaluate(r"""
            () => {
              const items = [];
              const clean = s => (s || '').replace(/\s+/g,' ').replace(/\u00A0/g,' ').trim();
              const firstMoney = txt => {
                const m = (txt || '').match(/\$\s*\d+(?:\.\d{1,2})?/);
                return m ? parseFloat(m[0].replace(/[^0-9.]/g,'')) : null;
              };
              const sizeFrom = txt => {
                const hints = ['per lb','per-lb','lb','oz','dozen','each','ea','ct','pk','pack'];
                const t = (txt || '').toLowerCase();
                for (const h of hints) if (t.includes(h)) return h.replace('-','-');
                return '';
              };
              const cards = new Set();
              [
                'article, li, .card, .c-card, .product, .product-card, .tile, .teaser, .feature, .grid__item',
                '[class*="feature"] [class*="card"], [class*="weekly"] [class*="card"]'
              ].forEach(sel => document.querySelectorAll(sel).forEach(n => cards.add(n)));

              const pushMaybe = (title, explicitPrice, contextText) => {
                const item = clean(title);
                const price = typeof explicitPrice === 'number' ? explicitPrice : firstMoney(contextText);
                if (!item || item.length < 3) return;
                items.push({ item: item.slice(0,120), price, size_text: sizeFrom(contextText || item) });
              };

              for (const card of cards) {
                const text = clean(card.textContent || '');
                if (!/\$\s*\d/.test(text)) continue;
                const titleEl = card.querySelector('h1,h2,h3,h4,.title,.card__title,.product__title,.teaser__title,strong');
                const title = titleEl ? titleEl.textContent : text.split('$', 1)[0];
                pushMaybe(title, null, text);
              }

              const priceNodes = Array.from(document.querySelectorAll('[class*="price"], .price, [data-price]'));
              for (const n of priceNodes) {
                const p = firstMoney(n.textContent);
                if (p == null) continue;
                let container = n;
                for (let i=0; i<4 && container && container.parentElement; i++) {
                  container = container.parentElement;
                  const tEl = container.querySelector('h1,h2,h3,h4,.title,.card__title,.product__title,.teaser__title,strong');
                  if (tEl) { pushMaybe(tEl.textContent, p, container.textContent); break; }
                }
              }

              const seen = new Set();
              return items.filter(x => {
                const key = (x.item || '').toLowerCase() + '|' + (x.price ?? 'none');
                if (seen.has(key)) return false; seen.add(key); return true;
              });
            }
            """)

            # Save artifacts
            html = await page.content()
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / "debug_freshmarket.html").write_text(html or "", encoding="utf-8")
            await page.screenshot(path=str(DATA_DIR / "debug_freshmarket.png"), full_page=True)

            await ctx.close(); await browser.close()

        # Prefer JS results; skip items with missing/invalid price; else fallback to soup
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        items = []

        if js_items:
            for i in js_items:
                price_raw = i.get("price")
                try:
                    price_val = float(price_raw) if price_raw is not None else None
                except Exception:
                    price_val = None
                if price_val is None:
                    continue  # drop items w/o usable price

                items.append({
                    "store_id": "fresh-market-24503",
                    "item": (i.get("item") or "")[:120],
                    "size_text": i.get("size_text") or "",
                    "price": price_val,
                    "unit_qty": None,
                    "unit": None,
                    "start_date": now.date().isoformat(),
                    "end_date": (now + timedelta(days=7)).date().isoformat(),
                    "promo_text": "Weekly Features",
                    "source": AD_URL,
                    "fetched_at": now.isoformat() + "Z",
                })
        if not items:
            # Fallback to soup parser from scrapers/freshmarket.py
            items = _extract_deals_from_html((DATA_DIR / "debug_freshmarket.html").read_text(encoding="utf-8"))

        # Save JSON
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUT_PATH.open("w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

        return {
            "ok": True,
            "saved_items": len(items),
            "js_extracted": bool(js_items),
            "saved_html": str(DATA_DIR / "debug_freshmarket.html"),
            "saved_png": str(DATA_DIR / "debug_freshmarket.png"),
            "saved_json": str(OUT_PATH),
        }

    try:
        return asyncio.run(run_and_save_both()), 200
    except Exception:
        err = traceback.format_exc()
        (DATA_DIR / "freshmarket_error.txt").write_text(err, encoding="utf-8")
        return {"ok": False, "error": err}, 500

# -------------------- main --------------------
if __name__ == "__main__":
    app.run(debug=True)
