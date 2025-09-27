import json
from pathlib import Path
from flask import Flask, jsonify, request
from utils.normalize import compute_unit_price
import asyncio
from flask import Response

app = Flask(__name__)
DATA_DIR = Path(__file__).parent / "data"

def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)
@app.get("/debug/foodlion")
def debug_foodlion_page():
    debug_path = DATA_DIR / "debug_foodlion.html"
    if not debug_path.exists():
        return {"ok": False, "error": "No debug HTML found yet. Run /scrape/foodlion first."}, 404
    html = debug_path.read_text(encoding="utf-8", errors="ignore")
    return Response(html, mimetype="text/html")
@app.get("/debug/playwright")
def debug_playwright():
    import asyncio, os, glob

    # Prefer the path we used during build; fall back to default cache.
    PW_DIRS = [
        "/opt/render/project/src/.playwright",   # where we installed in Build Command
        "/opt/render/.cache/ms-playwright",      # default cache path
    ]

    def find_chromium_executable():
        for base in PW_DIRS:
            candidates = sorted(glob.glob(os.path.join(base, "chromium-*", "chrome-linux", "chrome")))
            if candidates:
                return candidates[-1]  # newest
        return None

    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        return {"ok": False, "where": "import", "error": str(e)}, 500

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

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/stores")
def stores():
    """Return stores by zip (defaults to 24503)"""
    zip_code = request.args.get("zip", "24503")
    stores = load_json(DATA_DIR / "stores_24503.json")
    return jsonify([s for s in stores if s["zip"] == zip_code or zip_code == "24503"])

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

# --- TEMP: manual scrape trigger for Food Lion (async) ---
@app.route("/scrape/foodlion", methods=["POST", "GET"])
def scrape_foodlion():
    import traceback
    from scrapers.foodlion import run_and_save_async
    try:
        saved = asyncio.run(run_and_save_async())
        return {"ok": True, "saved": saved}, 200
    except Exception:
        err = traceback.format_exc()
        # also drop to a file so we can view later if needed
        (DATA_DIR / "foodlion_error.txt").write_text(err, encoding="utf-8")
        return {"ok": False, "error": err}, 500


@app.get("/routes")
def list_routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))

if __name__ == "__main__":
    app.run(debug=True)
