import os
import json
import time
import random
import logging
from datetime import datetime
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Lazada Pokémon Alert Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory store (replace with DB for persistence) ──────────────────────
products: dict[int, dict] = {}
alerts: list[dict] = []
next_id = 1
scheduler = AsyncIOScheduler()

# ── Models ──────────────────────────────────────────────────────────────────
class ProductAdd(BaseModel):
    url: str
    target_price: float
    name: Optional[str] = None

class ProductUpdate(BaseModel):
    target_price: Optional[float] = None
    name: Optional[str] = None

# ── Lazada scraper ───────────────────────────────────────────────────────────
HEADERS_POOL = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Connection": "keep-alive",
    },
]

async def scrape_lazada(url: str) -> dict:
    """Scrape product info from a Lazada product page."""
    headers = random.choice(HEADERS_POOL)
    # Small random delay to be polite
    await asyncio.sleep(random.uniform(1.5, 3.5))

    try:
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=20.0,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, "html.parser")

        # ── Price ─────────────────────────────────────────────────────────
        price = None
        price_selectors = [
            # Lazada PH / SEA inline JSON
            None,  # handled below via __js_parsed_data
            'span[data-spm-anchor-id*="price"]',
            '.pdp-price_type_normal',
            '.pdp-price',
            '[class*="price"] span',
        ]

        # Lazada embeds product data as JSON in a <script> tag
        for script in soup.find_all("script"):
            txt = script.string or ""
            if "skuBase" in txt or "pdpProtocol" in txt or "window.pageData" in txt:
                # Try to extract price from JSON blob
                import re
                m = re.search(r'"price"\s*:\s*"?([\d.]+)"?', txt)
                if m:
                    price = float(m.group(1))
                    break
                m = re.search(r'"originalPrice"\s*:\s*"?([\d.]+)"?', txt)
                if m:
                    price = float(m.group(1))
                    break

        if price is None:
            for sel in price_selectors[1:]:
                el = soup.select_one(sel)
                if el:
                    import re
                    raw = re.sub(r"[^\d.]", "", el.get_text())
                    if raw:
                        price = float(raw)
                        break

        # ── Name ─────────────────────────────────────────────────────────
        name = None
        for sel in ["h1.pdp-product-title", "h1[class*='title']", "h1", '[class*="title"] h1']:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                name = el.get_text(strip=True)[:120]
                break

        # ── Stock / availability ──────────────────────────────────────────
        stock = "Unknown"
        out_of_stock_hints = ["out of stock", "sold out", "unavailable", "habis"]
        low_stock_hints = ["hurry", "only", "left", "limited"]
        page_text = soup.get_text().lower()
        if any(h in page_text for h in out_of_stock_hints):
            stock = "Out of Stock"
        elif any(h in page_text for h in low_stock_hints):
            stock = "Low Stock"
        elif price is not None:
            stock = "In Stock"

        # ── Image ─────────────────────────────────────────────────────────
        image = None
        og = soup.find("meta", property="og:image")
        if og:
            image = og.get("content")

        return {
            "name": name or "Unknown Product",
            "price": price,
            "stock": stock,
            "image": image,
            "scraped_at": datetime.utcnow().isoformat(),
            "error": None,
        }

    except httpx.HTTPStatusError as e:
        logger.warning(f"HTTP error scraping {url}: {e}")
        return {"error": f"HTTP {e.response.status_code}", "price": None, "stock": "Unknown", "scraped_at": datetime.utcnow().isoformat()}
    except Exception as e:
        logger.error(f"Scrape error for {url}: {e}")
        return {"error": str(e), "price": None, "stock": "Unknown", "scraped_at": datetime.utcnow().isoformat()}


import asyncio

async def refresh_product(pid: int):
    """Fetch latest data for a product and check alert conditions."""
    global products, alerts

    p = products.get(pid)
    if not p:
        return

    logger.info(f"Refreshing product {pid}: {p['url']}")
    result = await scrape_lazada(p["url"])

    old_price = p.get("current_price")
    old_stock = p.get("stock")
    new_price = result.get("price") or old_price
    new_stock = result.get("stock", old_stock)

    # Price history (keep last 10)
    history = p.get("price_history", [])
    if new_price:
        history = (history + [{"price": new_price, "ts": result["scraped_at"]}])[-10:]

    # Alert conditions
    new_alerts = []
    if new_price and p.get("target_price") and new_price <= p["target_price"]:
        if not p.get("target_alerted"):
            new_alerts.append({
                "id": int(time.time() * 1000),
                "product_id": pid,
                "product_name": p["name"],
                "type": "price",
                "message": f"🎉 {p['name']} hit your target! Now ₱{new_price:,.0f} (target ₱{p['target_price']:,.0f})",
                "ts": datetime.utcnow().isoformat(),
            })
            products[pid]["target_alerted"] = True
    else:
        products[pid]["target_alerted"] = False

    if old_stock == "Out of Stock" and new_stock == "In Stock":
        new_alerts.append({
            "id": int(time.time() * 1000) + 1,
            "product_id": pid,
            "product_name": p["name"],
            "type": "stock",
            "message": f"📦 {p['name']} is back IN STOCK!",
            "ts": datetime.utcnow().isoformat(),
        })

    if new_price and old_price and new_price < old_price:
        drop = old_price - new_price
        pct = (drop / old_price) * 100
        if pct >= 5:
            new_alerts.append({
                "id": int(time.time() * 1000) + 2,
                "product_id": pid,
                "product_name": p["name"],
                "type": "drop",
                "message": f"📉 {p['name']} dropped ₱{drop:,.0f} ({pct:.0f}% off)! Now ₱{new_price:,.0f}",
                "ts": datetime.utcnow().isoformat(),
            })

    alerts[:0] = new_alerts  # prepend
    alerts[:] = alerts[:50]  # cap at 50

    products[pid].update({
        "current_price": new_price,
        "stock": new_stock,
        "price_history": history,
        "last_checked": result["scraped_at"],
        "scrape_error": result.get("error"),
        **({"image": result["image"]} if result.get("image") else {}),
        **({"name": result["name"]} if result.get("name") and result["name"] != "Unknown Product" else {}),
    })


async def refresh_all():
    """Background job: refresh all tracked products."""
    logger.info(f"Auto-refresh: {len(products)} products")
    for pid in list(products.keys()):
        await refresh_product(pid)
        await asyncio.sleep(2)  # stagger requests


# ── Startup / shutdown ───────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    scheduler.add_job(refresh_all, "interval", minutes=15, id="auto_refresh")
    scheduler.start()
    logger.info("Scheduler started — auto-refresh every 15 minutes")

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "message": "Lazada Pokémon Alert Bot API 🎮"}

@app.get("/products")
def list_products():
    return list(products.values())

@app.post("/products", status_code=201)
async def add_product(body: ProductAdd, background_tasks: BackgroundTasks):
    global next_id
    pid = next_id
    next_id += 1

    products[pid] = {
        "id": pid,
        "url": body.url,
        "name": body.name or "Loading…",
        "target_price": body.target_price,
        "current_price": None,
        "stock": "Unknown",
        "price_history": [],
        "last_checked": None,
        "scrape_error": None,
        "image": None,
        "target_alerted": False,
        "added_at": datetime.utcnow().isoformat(),
    }

    background_tasks.add_task(refresh_product, pid)
    return products[pid]

@app.get("/products/{pid}")
def get_product(pid: int):
    p = products.get(pid)
    if not p:
        raise HTTPException(404, "Product not found")
    return p

@app.patch("/products/{pid}")
def update_product(pid: int, body: ProductUpdate):
    p = products.get(pid)
    if not p:
        raise HTTPException(404, "Product not found")
    if body.target_price is not None:
        products[pid]["target_price"] = body.target_price
        products[pid]["target_alerted"] = False
    if body.name is not None:
        products[pid]["name"] = body.name
    return products[pid]

@app.delete("/products/{pid}", status_code=204)
def delete_product(pid: int):
    if pid not in products:
        raise HTTPException(404, "Product not found")
    del products[pid]

@app.post("/products/{pid}/refresh")
async def manual_refresh(pid: int):
    if pid not in products:
        raise HTTPException(404, "Product not found")
    await refresh_product(pid)
    return products[pid]

@app.get("/alerts")
def list_alerts():
    return alerts

@app.delete("/alerts", status_code=204)
def clear_alerts():
    alerts.clear()

@app.get("/health")
def health():
    return {"status": "healthy", "products": len(products), "alerts": len(alerts)}
