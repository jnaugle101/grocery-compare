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
# --- paste over your existing /scrape/freshmarket route in app.py ---

@app.route("/scrape/freshmarket", methods=["POST", "GET"])
def scrape_freshmarket():
    import traceback, json, re
    from playwright.async_api import async_playwright
    from scrapers.freshmarket import AD_URL, OUT_PATH, _extract_deals_from_html

    async def run_and_save_both():
        chromium_path = _find_chromium_executable()
        if not chromium_path:
            return {"ok": False, "error": "Chromium not found on server."}

        html = ""
        js_items = []
        captured_json = []  # network JSON payloads we’ll mine

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                executable_path=chromium_path,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
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

            # Capture JSON responses while navigating
            async def on_response(resp):
                try:
                    ct = resp.headers.get("content-type", "")
                    url = resp.url
                    if "application/json" in ct or url.endswith(".json") or "graphql" in url or "api" in url:
                        data = await resp.json()
                        captured_json.append({"url": url, "data": data})
                except Exception:
                    pass
            page.on("response", on_response)

            # Go (avoid networkidle)
            await page.goto(AD_URL, wait_until="domcontentloaded", timeout=60000)

            # Try common banners
            for sel in [
                '#onetrust-accept-btn-handler',
                'button:has-text("Accept")',
                '[aria-label="Accept"]',
                'button:has-text("Allow All")',
                'button:has-text("I Agree")',
            ]:
                try:
                    await page.locator(sel).first.click(timeout=1500)
                    break
                except Exception:
                    pass

            # Soft scroll to trigger lazy sections
            try:
                await page.evaluate("""
                  const sleep = ms => new Promise(r => setTimeout(r, ms));
                  (async () => {
                    for (let y = 0; y <= document.body.scrollHeight; y += 800) {
                      window.scrollTo(0, y);
                      await sleep(200);
                    }
                    window.scrollTo(0, document.body.scrollHeight);
                  })();
                """)
                await page.wait_for_timeout(1200)
            except Exception:
                pass

            # Save artifacts
            html = await page.content()
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / "debug_freshmarket.html").write_text(html or "", encoding="utf-8")
            await page.screenshot(path=str(DATA_DIR / "debug_freshmarket.png"), full_page=True)

            await ctx.close(); await browser.close()

        # ---------- Pull items from captured JSON / embedded JSON ----------
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        items: list[dict] = []

        def add_item(name, price, size_text=""):
            try:
                price_val = float(price)
            except Exception:
                return
            if not name or len(name.strip()) < 3:
                return
            items.append({
                "store_id": "fresh-market-24503",
                "item": name.strip()[:120],
                "size_text": (size_text or "").strip(),
                "price": price_val,
                "unit_qty": None,
                "unit": None,
                "start_date": now.date().isoformat(),
                "end_date": (now + timedelta(days=7)).date().isoformat(),
                "promo_text": "Weekly Features",
                "source": AD_URL,
                "fetched_at": now.isoformat() + "Z",
            })

        # A) Mine captured JSON responses
        def walk(obj, path=""):
            if isinstance(obj, dict):
                # Heuristic: a dict with name/title & price fields
                keys = set(k.lower() for k in obj.keys())
                cand_name = obj.get("name") or obj.get("title") or obj.get("headline")
                cand_price = obj.get("price") or obj.get("salePrice") or obj.get("sale_price") or obj.get("amount") or obj.get("value")
                if cand_name and (cand_price is not None):
                    add_item(str(cand_name), cand_price, obj.get("unit") or obj.get("uom") or obj.get("size"))
                # Recurse
                for k, v in obj.items():
                    walk(v, f"{path}.{k}" if path else k)
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    walk(v, f"{path}[{i}]")

        for blob in captured_json:
            walk(blob["data"])

        # B) Mine embedded JSON from HTML: __NEXT_DATA__, __NUXT__, ld+json
        if not items:
            try:
                import re, json
                embedded = []
                for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S|re.I):
                    try:
                        embedded.append(json.loads(m.group(1)))
                    except Exception:
                        pass
                # NEXT_DATA / NUXT state
                for m in re.finditer(r'>(?:window\.__NEXT_DATA__|window\.__NUXT__)\s*=\s*(\{.*?\});?\s*<', html, re.S):
                    try:
                        embedded.append(json.loads(m.group(1)))
                    except Exception:
                        pass
                for root in embedded:
                    walk(root)
            except Exception:
                pass

        # C) Fallback to your heuristic HTML parser
        if not items:
            items = _extract_deals_from_html(html or "")

        # Deduplicate (name|price)
        seen = set()
        deduped = []
        for it in items:
            key = (it["item"].lower(), it["price"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(it)
        items = deduped

        # Save JSON
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUT_PATH.open("w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

        return {
            "ok": True,
            "saved_items": len(items),
            "captured_json_count": len(captured_json),
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
