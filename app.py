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

            # Close any promo/video modal if present
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

            # Small wait + incremental scroll to trigger lazy sections
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

            # In-page extraction (may return some items without price)
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
                // it's OK if price is null here; we'll filter on the server
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

            await ctx.close()
            await browser.close()

        # Prefer JS-extracted; else fallback to soup
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        items = []

        if js_items and len(js_items) > 0:
            for i in js_items:
                # --- Guard against missing/invalid price ---
                price_raw = i.get("price")
                try:
                    price_val = float(price_raw) if price_raw is not None else None
                except Exception:
                    price_val = None

                # If price is missing, SKIP the item (or keep it by storing None if you prefer)
                if price_val is None:
                    continue

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
