import os
import re
import json
import time
import random
import asyncio
import logging
from datetime import datetime
from typing import Optional

import httpx
import asyncpg
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Lazada Alert Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

scheduler = AsyncIOScheduler()
db: asyncpg.Pool = None

# ── DB helpers ────────────────────────────────────────────────────────────────
async def get_db() -> asyncpg.Pool:
    return db

async def init_db():
    global db
    db = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    await db.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            url TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT 'Loading...',
            target_price FLOAT NOT NULL,
            current_price FLOAT,
            stock TEXT DEFAULT 'Unknown',
            price_history JSONB DEFAULT '[]',
            last_checked TIMESTAMPTZ,
            scrape_error TEXT,
            image TEXT,
            target_alerted BOOLEAN DEFAULT FALSE,
            added_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id BIGINT PRIMARY KEY,
            product_id INT,
            product_name TEXT,
            type TEXT,
            message TEXT,
            ts TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    logger.info("Database initialized")

async def db_get_products():
    rows = await db.fetch("SELECT * FROM products ORDER BY id")
    return [dict(r) for r in rows]

async def db_get_product(pid: int):
    row = await db.fetchrow("SELECT * FROM products WHERE id = $1", pid)
    return dict(row) if row else None

async def db_save_product(p: dict):
    await db.execute("""
        UPDATE products SET
            name=$1, current_price=$2, stock=$3,
            price_history=$4, last_checked=$5,
            scrape_error=$6, image=$7, target_alerted=$8
        WHERE id=$9
    """,
        p["name"], p.get("current_price"), p.get("stock"),
        json.dumps(p.get("price_history", [])),
        datetime.utcnow() if p.get("last_checked") else None,
        p.get("scrape_error"), p.get("image"),
        p.get("target_alerted", False), p["id"]
    )

async def db_get_alerts(limit=50):
    rows = await db.fetch("SELECT * FROM alerts ORDER BY ts DESC LIMIT $1", limit)
    return [dict(r) for r in rows]

async def db_save_alert(a: dict):
    await db.execute("""
        INSERT INTO alerts (id, product_id, product_name, type, message, ts)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (id) DO NOTHING
    """, a["id"], a.get("product_id"), a.get("product_name"), a.get("type"), a.get("message"), datetime.utcnow())

# ── Models ────────────────────────────────────────────────────────────────────
class ProductAdd(BaseModel):
    url: str
    target_price: float = 99999
    name: Optional[str] = None

class ProductUpdate(BaseModel):
    target_price: Optional[float] = None
    name: Optional[str] = None

# ── Scraper ───────────────────────────────────────────────────────────────────
HEADERS_POOL = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Connection": "keep-alive",
    },
]

async def scrape_lazada(url: str) -> dict:
    headers = random.choice(HEADERS_POOL)
    await asyncio.sleep(random.uniform(1.5, 3.5))
    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, "html.parser")

        # Price
        price = None
        for script in soup.find_all("script"):
            txt = script.string or ""
            if "skuBase" in txt or "pdpProtocol" in txt or "window.pageData" in txt:
                m = re.search(r'"price"\s*:\s*"?([\d.]+)"?', txt)
                if m: price = float(m.group(1)); break
                m = re.search(r'"originalPrice"\s*:\s*"?([\d.]+)"?', txt)
                if m: price = float(m.group(1)); break
        if price is None:
            for sel in ['span[data-spm-anchor-id*="price"]', '.pdp-price_type_normal', '.pdp-price', '[class*="price"] span']:
                el = soup.select_one(sel)
                if el:
                    raw = re.sub(r"[^\d.]", "", el.get_text())
                    if raw: price = float(raw); break

        # Name
        name = None
        for sel in ["h1.pdp-product-title", "h1[class*='title']", "h1"]:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                name = el.get_text(strip=True)[:120]; break

        # Stock
        page_text = soup.get_text().lower()
        if any(h in page_text for h in ["out of stock", "sold out", "unavailable", "habis"]):
            stock = "Out of Stock"
        elif any(h in page_text for h in ["hurry", "only", "left", "limited"]):
            stock = "Low Stock"
        elif price is not None:
            stock = "In Stock"
        else:
            stock = "Unknown"

        # Image
        image = None
        og = soup.find("meta", property="og:image")
        if og: image = og.get("content")

        return {"name": name or "Unknown Product", "price": price, "stock": stock, "image": image, "scraped_at": datetime.utcnow().isoformat(), "error": None}

    except httpx.HTTPStatusError as e:
        return {"error": f"HTTP {e.response.status_code}", "price": None, "stock": "Unknown", "scraped_at": datetime.utcnow().isoformat()}
    except Exception as e:
        return {"error": str(e), "price": None, "stock": "Unknown", "scraped_at": datetime.utcnow().isoformat()}

# ── Refresh logic ─────────────────────────────────────────────────────────────
async def refresh_product(pid: int):
    p = await db_get_product(pid)
    if not p: return

    logger.info(f"Refreshing product {pid}: {p['url']}")
    result = await scrape_lazada(p["url"])

    old_price = p.get("current_price")
    old_stock = p.get("stock")
    new_price = result.get("price") or old_price
    new_stock = result.get("stock", old_stock)

    history = json.loads(p.get("price_history") or "[]")
    if new_price:
        history = (history + [{"price": new_price, "ts": result["scraped_at"]}])[-10:]

    new_alerts = []
    if new_price and p.get("target_price") and new_price <= p["target_price"] and not p.get("target_alerted"):
        new_alerts.append({"id": int(time.time() * 1000), "product_id": pid, "product_name": p["name"], "type": "price",
            "message": f"🎉 {p['name']} hit your target! Now S${new_price:,.2f}"})
        p["target_alerted"] = True
    elif new_price and p.get("target_price") and new_price > p["target_price"]:
        p["target_alerted"] = False

    if old_stock == "Out of Stock" and new_stock == "In Stock":
        new_alerts.append({"id": int(time.time() * 1000) + 1, "product_id": pid, "product_name": p["name"],
            "type": "stock", "message": f"📦 {p['name']} is back IN STOCK!"})

    if new_price and old_price and new_price < old_price:
        drop = old_price - new_price
        pct = (drop / old_price) * 100
        if pct >= 5:
            new_alerts.append({"id": int(time.time() * 1000) + 2, "product_id": pid, "product_name": p["name"],
                "type": "drop", "message": f"📉 {p['name']} dropped S${drop:,.2f} ({pct:.0f}% off)!"})

    p.update({
        "current_price": new_price,
        "stock": new_stock,
        "price_history": history,
        "last_checked": result["scraped_at"],
        "scrape_error": result.get("error"),
        **({"image": result["image"]} if result.get("image") else {}),
        **({"name": result["name"]} if result.get("name") and result["name"] != "Unknown Product" else {}),
    })
    await db_save_product(p)

    for alert in new_alerts:
        await db_save_alert(alert)

async def refresh_all():
    products = await db_get_products()
    logger.info(f"Auto-refresh: {len(products)} products")
    for p in products:
        await refresh_product(p["id"])
        await asyncio.sleep(2)

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    await init_db()
    scheduler.add_job(refresh_all, "interval", minutes=5, id="auto_refresh")
    scheduler.start()
    logger.info("Scheduler started — auto-refresh every 5 minutes")

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
    await db.close()

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "message": "Lazada Alert Bot API 🎮"}

@app.get("/products")
async def list_products():
    products = await db_get_products()
    for p in products:
        if isinstance(p.get("price_history"), str):
            p["price_history"] = json.loads(p["price_history"])
    return products

@app.post("/products", status_code=201)
async def add_product(body: ProductAdd, background_tasks: BackgroundTasks):
    row = await db.fetchrow("""
        INSERT INTO products (url, name, target_price)
        VALUES ($1, $2, $3)
        RETURNING *
    """, body.url, body.name or "Loading...", body.target_price)
    p = dict(row)
    p["price_history"] = []
    background_tasks.add_task(refresh_product, p["id"])
    return p

@app.get("/products/{pid}")
async def get_product(pid: int):
    p = await db_get_product(pid)
    if not p: raise HTTPException(404, "Product not found")
    if isinstance(p.get("price_history"), str):
        p["price_history"] = json.loads(p["price_history"])
    return p

@app.patch("/products/{pid}")
async def update_product(pid: int, body: ProductUpdate):
    p = await db_get_product(pid)
    if not p: raise HTTPException(404, "Product not found")
    if body.target_price is not None:
        await db.execute("UPDATE products SET target_price=$1, target_alerted=FALSE WHERE id=$2", body.target_price, pid)
    if body.name is not None:
        await db.execute("UPDATE products SET name=$1 WHERE id=$2", body.name, pid)
    return await db_get_product(pid)

@app.delete("/products/{pid}", status_code=204)
async def delete_product(pid: int):
    result = await db.execute("DELETE FROM products WHERE id=$1", pid)
    if result == "DELETE 0":
        raise HTTPException(404, "Product not found")

@app.post("/products/{pid}/refresh")
async def manual_refresh(pid: int):
    p = await db_get_product(pid)
    if not p: raise HTTPException(404, "Product not found")
    await refresh_product(pid)
    p = await db_get_product(pid)
    if isinstance(p.get("price_history"), str):
        p["price_history"] = json.loads(p["price_history"])
    return p

@app.get("/alerts")
async def list_alerts():
    return await db_get_alerts()

@app.delete("/alerts", status_code=204)
async def clear_alerts():
    await db.execute("DELETE FROM alerts")

@app.get("/health")
async def health():
    products = await db_get_products()
    alerts = await db_get_alerts()
    return {"status": "healthy", "products": len(products), "alerts": len(alerts)}
