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

# Summarize captured network JSON (first 25 items)
@app.get("/debug/freshmarket_captured")
def debug_freshmarket_captured():
    p = DATA_DIR / "freshmarket_responses.json"
    if not p.exists():
        return {"ok": False, "error": "No captured JSON yet. Run /scrape/freshmarket first."}, 404
    try:
        blobs = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"read/parse error: {e}"}, 500

    summary = []
    for b in blobs[:25]:
        data = b.get("data")
        top_type = type(data).__name__
        top_keys = list(data.keys())[:10] if isinstance(data, dict) else None
        summary.append({
            "url": b.get("url"),
            "type": top_type,
            "top_keys": top_keys,
        })
    return {"ok": True, "count": len(blobs), "summary": summary}

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
    import traceback, json, re
    from playwright.async_api import async_playwright
    from scrapers.freshmarket import AD_URL, OUT_PATH, _extract_deals_from_html

    async def run_and_save_both():
        chromium_path = _find_chromium_executable()
        if not chromium_path:
            return {"ok": False, "error": "Chromium not found on server."}

        html = ""
        captured_json = []  # network JSON payloads

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
                    ct = (resp.headers.get("content-type") or "").lower()
                    url = resp.url
                    if "application/json" in ct or url.endswith(".json") or "graphql" in url or "/api/" in url:
                        data = await resp.json()
                        captured_json.append({"url": url, "data": data})
                except Exception:
                    pass
            page.on("response", on_response)

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

            # Also persist captured JSON for debugging/incremental tuning
            (DATA_DIR / "freshmarket_responses.json").write_text(
                json.dumps(captured_json, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

            await ctx.close(); await browser.close()

        # ---------- Extraction pipeline ----------
        from datetime import datetime, timedelta
        now = datetime.utcnow()

        def _parse_money_any(val):
            """
            Accept numbers, '$5.99', '99¢', '2 for $5', '2/$5', etc.
            Returns float or None.
            """
            if val is None:
                return None
            if isinstance(val, (int, float)):
                try:
                    return float(val)
                except Exception:
                    return None

            s = str(val).replace("\u00a0", " ").strip()

            # 2 for $5 / 2/$5
            m = re.search(r"(\d+)\s*(?:for|/)\s*\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", s, re.I)
            if m:
                qty = int(m.group(1))
                total = float(m.group(2))
                if qty > 0:
                    return round(total / qty, 2)

            # $5.99 / 5.99
            m = re.search(r"\$?\s*([0-9]+(?:\.[0-9]{1,2})?)", s)
            if m:
                try:
                    return float(m.group(1))
                except Exception:
                    pass

            # 99¢
            m = re.search(r"([0-9]+)\s*¢", s)
            if m:
                try:
                    return float(m.group(1)) / 100.0
                except Exception:
                    return None
            return None

        def _add_item(items_list, name, price, size_text=""):
            price_val = _parse_money_any(price)
            if price_val is None:
                return
            if not name or len(str(name).strip()) < 3:
                return
            items_list.append({
                "store_id": "fresh-market-24503",
                "item": str(name).strip()[:120],
                "size_text": (size_text or "").strip(),
                "price": float(price_val),
                "unit_qty": None,
                "unit": None,
                "start_date": now.date().isoformat(),
                "end_date": (now + timedelta(days=7)).date().isoformat(),
                "promo_text": "Weekly Features",
                "source": AD_URL,
                "fetched_at": now.isoformat() + "Z",
            })

        # Walk arbitrary JSON and try common shapes
        items_from_json: list[dict] = []

        def walk(obj):
            if isinstance(obj, dict):
                # --- candidate name fields (keep generous) ---
                name = (
                    obj.get("name") or obj.get("title") or obj.get("headline")
                    or obj.get("productName") or obj.get("description")
                    or obj.get("primaryText") or obj.get("tileHeadline")
                    or obj.get("eyebrow")
                )

                # --- candidate numeric/explicit price fields ---
                cand_prices = [
                    obj.get("price"), obj.get("salePrice"), obj.get("sale_price"),
                    obj.get("priceValue"), obj.get("priceText"), obj.get("formattedPrice"),
                    obj.get("amount"), obj.get("value"), obj.get("regularPrice"),
                    obj.get("finalPrice"), obj.get("wasPrice"), obj.get("nowPrice"),
                    obj.get("currentPrice"), obj.get("pricePerPound"), obj.get("price_per"),
                    obj.get("tilePrice"), obj.get("priceString"),
                ]

                # nested price object(s)
                p = obj.get("price")
                if isinstance(p, dict):
                    cand_prices += [p.get("amount"), p.get("value"), p.get("current"), p.get("sale")]

                # --- parse price from text-y fields, common in marketing tiles ---
                text_fields = [
                    obj.get("copy"), obj.get("body"), obj.get("text"),
                    obj.get("subtitle"), obj.get("blurb"), obj.get("richText"),
                    obj.get("html"), obj.get("content"), obj.get("secondaryText"),
                    obj.get("tileSubheadline"), obj.get("tileCopy"),
                ]
                for tf in text_fields:
                    parsed = _parse_money_any(tf)
                    if parsed is not None:
                        cand_prices.append(parsed)

                # --- unit/size hints ---
                size_text = (
                    obj.get("unit") or obj.get("uom") or obj.get("size") or obj.get("sizeText")
                    or obj.get("unitOfMeasure") or obj.get("unitText") or obj.get("priceUnit")
                )

                if name and any(cp is not None for cp in cand_prices):
                    for cp in cand_prices:
                        if cp is None:
                            continue
                        _add_item(items_from_json, name, cp, size_text)

                # recurse
                for v in obj.values():
                    walk(v)

            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        # A) Special-case: mine Next.js features payload
        features_blobs = [
            b for b in captured_json
            if "/_next/data/" in (b.get("url") or "") and "features/weekly-features" in b.get("url", "")
            and isinstance(b.get("data"), dict)
        ]
        for fb in features_blobs:
            data = fb["data"]
            page_props = data.get("pageProps", data)
            walk(page_props)

        # B) If still sparse, walk all captured JSON
        if len(items_from_json) < 3:
            for blob in captured_json:
                walk(blob["data"])

        # C) If still empty, mine embedded JSON from HTML (ld+json / __NEXT_DATA__/__NUXT__)
        if not items_from_json:
            try:
                embedded = []
                for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S|re.I):
                    try:
                        embedded.append(json.loads(m.group(1)))
                    except Exception:
                        pass
                for m in re.finditer(r'>(?:window\.__NEXT_DATA__|window\.__NUXT__)\s*=\s*(\{.*?\});?\s*<', html, re.S):
                    try:
                        embedded.append(json.loads(m.group(1)))
                    except Exception:
                        pass
                for root in embedded:
                    walk(root)
            except Exception:
                pass

        # D) Final fallback: heuristic HTML parser
        items = items_from_json if items_from_json else _extract_deals_from_html(html or "")

        # Deduplicate (name|price)
        seen = set()
        deduped = []
        for it in items:
            key = (it["item"].lower(), float(it["price"]))
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
            "saved_captured_json": str(DATA_DIR / "freshmarket_responses.json"),
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
