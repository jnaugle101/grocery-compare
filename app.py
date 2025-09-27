import json
from pathlib import Path
from flask import Flask, jsonify, request
from utils.normalize import compute_unit_price

# at top:
import asyncio

import asyncio

@app.route("/scrape/foodlion", methods=["POST", "GET"])
def scrape_foodlion():
    try:
        from scrapers.foodlion import run_and_save_async
        saved = asyncio.run(run_and_save_async())
        return {"ok": True, "saved": saved}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

app = Flask(__name__)

DATA_DIR = Path(__file__).parent / "data"

def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/stores")
def stores():
    """Return stores by zip (defaults to 24503)"""
    zip_code = request.args.get("zip", "24503")
    stores = load_json(DATA_DIR / "stores_24503.json")
    # For MVP, we ignore other zips; later, filter by given zip or distance.
    return jsonify([s for s in stores if s["zip"] == zip_code or zip_code == "24503"])

@app.get("/deals")
def deals():
    """Return deals for zip (MVP: sample + any scraped files)"""
    _ = request.args.get("zip", "24503")
    merged = []

    # sample deals
    sample_path = DATA_DIR / "deals_sample_24503.json"
    if sample_path.exists():
        merged.extend(load_json(sample_path))

    # food lion scraped deals
    fl_path = DATA_DIR / "deals_foodlion.json"
    if fl_path.exists():
        merged.extend(load_json(fl_path))

    # compute unit_price if you like (simple if you have unit_qty)
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
    """
    MVP: simple greedy compare by item substring.
    Example: /compare?zip=24503&items=chicken,eggs
    """
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
# --- TEMP: manual scrape trigger for Food Lion (testing only) ---
@app.route("/scrape/foodlion", methods=["POST", "GET"])
def scrape_foodlion():
    from scrapers.foodlion import run_and_save
    try:
        saved = run_and_save()
        return {"ok": True, "saved": saved}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.get("/routes")
def list_routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))

if __name__ == "__main__":
    app.run(debug=True)


