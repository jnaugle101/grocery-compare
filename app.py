import json
from pathlib import Path
from flask import Flask, jsonify, request
from utils.normalize import compute_unit_price

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
    """Return deals for zip (MVP: static sample deals + computed unit price)"""
    _ = request.args.get("zip", "24503")
    deals = load_json(DATA_DIR / "deals_sample_24503.json")
    for d in deals:
        d["unit_price"] = compute_unit_price(d.get("price"), d.get("unit_qty"))
    return jsonify(deals)

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

if __name__ == "__main__":
    app.run(debug=True)
