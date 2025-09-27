import json
from pathlib import Path
from flask import Flask, jsonify, request, Response
from utils.normalize import compute_unit_price
import asyncio

app = Flask(__name__)
DATA_DIR = Path(__file__).parent / "data"


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------- Debug: view latest saved Food Lion HTML ----------
@app.get("/debug/foodlion")
def debug_foodlion_page():
    debug_path = DATA_DIR / "debug_foodlion.html"
    if not debug_path.exists():
        return {"ok": False, "error": "No debug HTML found yet. Run /scrape/foodlion first."}, 404
    html = debug_path.read_text(encoding="utf-8", errors="ignore")
    return Response(html, mimetype="text/html")


# ---------- Debug: verify Playwright & Chromium ----------
@app.get("/debug/playwright")
def debug_playwright():
    import os, glob
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        return {"ok": False, "where": "import", "error": str(e)}, 500

    def find_chromium_executable():
        # Prefer build-installed path; fall back to default cache.
        for base in ("/opt/render/project/src/.playwright", "/opt/render/.cache/ms-playwright"):
            candidates = sorted(glob.glob(os.path.join(base, "chromium-*", "chrome-linux", "chrome")))
            if candidates:
                return candidates[-1]
        return None

    async def probe():
        chromium_path = find_chromium_executable()
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
    # fresh market scraped deals
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


# ---------- Scrape Food Lion (force-snapshot for inspection) ----------
@app.route("/scrape/foodlion", methods=["POST", "GET"])
def scrape_foodlion():
    import os, glob, traceback
    from playwright.async_api import async_playwright
    from scrapers.foodlion import AD_URL

    def find_chromium_executable():
        for base in ("/opt/render/project/src/.playwright", "/opt/render/.cache/ms-playwright"):
            hits = sorted(glob.glob(os.path.join(base, "chromium-*", "chrome-linux", "chrome")))
            if hits:
                return hits[-1]
        return None

    async def run_and_debug():
        chromium_path = find_chromium_executable()
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

            # Nudge lazy-loaded content a bit
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1200)

            html = await page.content()
            await ctx.close()
            await browser.close()

        # Always write a stable filename so /debug/foodlion works
        snap = DATA_DIR / "debug_foodlion.html"
        snap.write_text(html, encoding="utf-8")
        return {"ok": True, "debug_html": str(snap)}

    try:
        return asyncio.run(run_and_debug()), 200
    except Exception:
        err = traceback.format_exc()
        (DATA_DIR / "foodlion_error.txt").write_text(err, encoding="utf-8")
        return {"ok": False, "error": err}, 500

@app.route("/scrape/freshmarket", methods=["POST", "GET"])
def scrape_freshmarket():
    try:
        from scrapers.freshmarket import run_and_save
        saved = run_and_save()
        return {"ok": True, "saved": saved}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# ---------- Debug: import probe for the scraper ----------
@app.get("/debug/import_foodlion")
def debug_import_foodlion():
    import traceback, importlib
    try:
        mod = importlib.import_module("scrapers.foodlion")
        has_async = hasattr(mod, "run_and_save_async")
        return {"ok": True, "module": str(mod), "has_run_and_save_async": has_async}
    except Exception:
        return {"ok": False, "error": traceback.format_exc()}, 500


# ---------- Routes list ----------
@app.get("/routes")
def list_routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))


if __name__ == "__main__":
    app.run(debug=True)
