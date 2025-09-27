import json
from pathlib import Path
from flask import Flask, jsonify, request, Response
from utils.normalize import compute_unit_price
import asyncio
import os, glob

app = Flask(__name__)
DATA_DIR = Path(__file__).parent / "data"


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


# ---------- Debug: view latest saved Food Lion HTML ----------
@app.get("/debug/foodlion")
def debug_foodlion_page():
    debug_path = DATA_DIR / "debug_foodlion.html"
    if not debug_path.exists():
        return {"ok": False, "error": "No debug HTML found yet. Run /scrape/foodlion first."}, 404
    html = debug_path.read_text(encoding="utf-8", errors="ignore")
    return Response(html, mimetype="text/html")


# ---------- Debug: view latest saved Fresh Market HTML ----------
@app.get("/debug/freshmarket")
def debug_freshmarket_page():
    debug_path = DATA_DIR / "debug_freshmarket.html"
    if not debug_path.exists():
        return {"ok": False, "error": "No Fresh Market debug HTML yet. Run /scrape/freshmarket first."}, 404
    html = debug_path.read_text(encoding="utf-8", errors="ignore")
    return Response(html, mimetype="text/html")


# ---------- Debug: verify Playwright & Chromium ----------
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
            await ctx.close()
            await browser.close()
            return {"ok": True, "html_len": len(html), "chromium_path": chromium_path}

    try:
        result = asyncio.run(probe())
        return result if isinstance(result, dict) else {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "where": "run", "error": str(e)}, 500


# ---------- Health ----------
@app.get("/health")
def health():
    return {"ok": True}


# ---------- Stores ----------
@app.get("/stores")
def stores():
    """Return stores by zip (defaults to 24503)"""
    zip_code = request.args.get("zip", "24503")
    stores = load_json(DATA_DIR / "stores_24503.json")
    return jsonify([s for s in stores if s["zip"] == zip_code or zip_code == "24503"])


# ---------- Deals (sample + any scraped) ----------
@app.get("/deals")
def deals():
    """Return deals for zip (MVP: sample + any scraped files)"""
    _ = request.args.get("zip", "24503")
    merged = []

    sample_path = DATA_DIR / "deals_sample_24503.json"
    if sample_path.exists():
        merged.extend(load_json(sample_path))

    fl_path = DATA_DIR / "deals_foodlion.json"
    if fl_path.exists():
        merged.extend(load_json(fl_path))

    fm_path = DATA_DIR / "deals_freshmarket.json"
    if fm_path.exists():
        merged.extend(load_json(fm_path))

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


# ---------- Compare ----------
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


# ---------- Debug: list files in /data ----------
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


# ---------- Scrape Food Lion (save HTML + parsed JSON) ----------
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
            await ctx.close()
            await browser.close()

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        debug_path = DATA_DIR / "debug_foodlion.html"
        debug_path.write_text(html, encoding="utf-8")

        items = _extract_deals_from_html(html)
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUT_PATH.open("w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

        return {"ok": True, "saved_items": len(items), "saved_html": str(debug_path), "saved_json": str(OUT_PATH)}

    try:
        return asyncio.run(run_and_save_both()), 200
    except Exception:
        err = traceback.format_exc()
        (DATA_DIR / "foodlion_error.txt").write_text(err, encoding="utf-8")
        return {"ok": False, "error": err}, 500


@app.route("/scrape/freshmarket", methods=["POST", "GET"])
def scrape_freshmarket():
    import traceback, json
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

            # Try dismissing cookie banners / modals commonly used on TFM
            for sel in [
                '#onetrust-accept-btn-handler',          # OneTrust
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

            # Close any promo/video modal if present
            for sel in [
                '[aria-label="Close"]',
                'button[aria-label="Close"]',
                'button:has-text("Close")',
                '.modal [data-dismiss="modal"]',
                '.mfp-close',  # Magnific Popup
            ]:
                try:
                    await page.locator(sel).first.click(timeout=1500)
                    break
                except Exception:
                    pass

            # Small wait for content to settle
            try:
                await page.wait_for_timeout(800)
            except Exception:
                pass

            # Incremental scroll to trigger lazy sections
            try:
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

            # In-page extraction: try multiple structures before falling back to regex-only
            js_items = await page.evaluate(r"""
            () => {
              const items = [];

              const clean = s => (s || '')
                .replace(/\s+/g, ' ')
                .replace(/\u00A0/g, ' ')   // nbsp
                .trim();

              const firstMoney = (txt) => {
                const m = (txt || '').match(/\$\s*\d+(?:\.\d{1,2})?/);
                return m ? parseFloat(m[0].replace(/[^0-9.]/g,'')) : null;
              };

              const sizeFrom = (txt) => {
                const hints = ['per lb','per-lb','lb','oz','dozen','each','ea','ct','pk','pack'];
                const t = (txt || '').toLowerCase();
                for (const h of hints) if (t.includes(h)) return h.replace('-','-');
                return '';
              };

              // Strategy A: obvious product/feature tiles
              const cardSelectors = [
                // generic card patterns
                'article, li, .card, .c-card, .product, .product-card, .tile, .teaser, .feature, .grid__item',
                // TFM often uses BEM-ish classes
                '[class*="feature"] [class*="card"], [class*="weekly"] [class*="card"]'
              ];
              const cards = new Set();
              for (const sel of cardSelectors) {
                document.querySelectorAll(sel).forEach(n => cards.add(n));
              }

              const pushIfValid = (title, price, contextText) => {
                const item = clean(title);
                const p = typeof price === 'number' ? price : firstMoney(contextText);
                if (!item || item.length < 3 || !isFinite(p)) return;
                items.push({
                  item: item.slice(0, 120),
                  price: p,
                  size_text: sizeFrom(contextText || item)
                });
              };

              // Try to pick title + price for each card-ish container
              for (const card of cards) {
                const text = clean(card.textContent || '');
                if (!/\$\s*\d/.test(text)) continue;

                let titleEl = card.querySelector('h1,h2,h3,h4,.title,.card__title,.product__title,.teaser__title,strong');
                let title = titleEl ? titleEl.textContent : text.split('$', 1)[0];

                pushIfValid(title, null, text);
              }

              // Strategy B: any visible node that *is* a price + look upward for a title-ish sibling
              const priceNodes = Array.from(document.querySelectorAll('[class*="price"], .price, [data-price]'));
              for (const n of priceNodes) {
                const price = firstMoney(n.textContent);
                if (!isFinite(price)) continue;

                // climb up to find a reasonable container
                let container = n;
                for (let i=0; i<4 && container && container.parentElement; i++) {
                  container = container.parentElement;
                  const titleEl = container.querySelector('h1,h2,h3,h4,.title,.card__title,.product__title,.teaser__title,strong');
                  if (titleEl) {
                    pushIfValid(titleEl.textContent, price, container.textContent);
                    break;
                  }
                }
              }

              // Strategy C: as last resort, scan big sections for $ and take the nearest heading above it
              if (items.length < 2) {
                const blocks = Array.from(document.querySelectorAll('section, .section, .content, main, .container'));
                for (const b of blocks) {
                  const text = clean(b.textContent || '');
                  if (!/\$\s*\d/.test(text)) continue;

                  let titleEl = b.querySelector('h2,h3,h4,strong,.title');
                  pushIfValid(titleEl ? titleEl.textContent : text.split('$',1)[0], null, text);
                }
              }

              // de-dup by (title+price)
              const seen = new Set();
              return items.filter(x => {
                const key = x.item.toLowerCase() + '|' + x.price;
                if (seen.has(key)) return false;
                seen.add(key);
                return true;
              });
            }
            """)

            #Save artifacts for inspection
            html = await page.content()
            from pathlib import Path
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / "debug_freshmarket.html").write_text(html or "", encoding="utf-8")
            await page.screenshot(path=str(DATA_DIR / "debug_freshmarket.png"), full_page=True)

            await ctx.close()
            await browser.close()

        # Prefer JS-extracted items; fallback to soup if still empty
        if js_items and len(js_items) > 0:
            from datetime import datetime, timedelta
            now = datetime.utcnow()
            items = [{
                "store_id": "fresh-market-24503",
                "item": i["item"],
                "size_text": i.get("size_text") or "",
                "price": float(i["price"]),
                "unit_qty": None,
                "unit": None,
                "start_date": now.date().isoformat(),
                "end_date": (now + timedelta(days=7)).date().isoformat(),
                "promo_text": "Weekly Features",
                "source": AD_URL,
                "fetched_at": now.isoformat() + "Z",
            } for i in js_items]
        else:
            items = _extract_deals_from_html(html or "")

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



# ---------- Routes list ----------
@app.get("/routes")
def list_routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))


if __name__ == "__main__":
    app.run(debug=True)
