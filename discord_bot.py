"""
discord_bot.py — Pokémon Lazada Alert Bot (py-cord, Python 3.13 compatible)
"""

import os
import logging
from datetime import datetime

import discord
from discord.ext import tasks
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
API_BASE          = os.environ.get("API_BASE", "http://localhost:8000")

POKE_GOLD  = 0xFFD700
POKE_GREEN = 0x4ade80
POKE_BLUE  = 0x3b82f6
POKE_RED   = 0xE3350D

async def api_get(path):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API_BASE}{path}")
        r.raise_for_status()
        return r.json()

async def api_post(path, body={}):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{API_BASE}{path}", json=body)
        r.raise_for_status()
        return r.json()

async def api_delete(path):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.delete(f"{API_BASE}{path}")
        return r.status_code

def stock_emoji(stock):
    return {"In Stock": "🟢", "Low Stock": "🟡", "Out of Stock": "🔴"}.get(stock, "⚪")

def price_str(p):
    return f"₱{p:,.0f}" if p else "—"

def product_embed(p, colour=POKE_GOLD):
    current = p.get("current_price")
    target  = p.get("target_price")
    stock   = p.get("stock", "Unknown")
    hit     = current and target and current <= target
    embed   = discord.Embed(title=p.get("name", "Unknown")[:256], url=p.get("url",""), colour=POKE_GREEN if hit else colour)
    embed.add_field(name="💰 Current", value=price_str(current), inline=True)
    embed.add_field(name="🎯 Target",  value=price_str(target),  inline=True)
    embed.add_field(name=f"{stock_emoji(stock)} Stock", value=stock, inline=True)
    embed.add_field(name="🆔 ID",      value=str(p["id"]),       inline=True)
    if hit:
        embed.add_field(name="🎉", value="TARGET PRICE REACHED!", inline=False)
    if p.get("price_history"):
        prices = " → ".join(f"₱{h['price']:,.0f}" for h in p["price_history"][-5:] if h.get("price"))
        if prices:
            embed.add_field(name="📈 History", value=prices, inline=False)
    if p.get("image"):
        embed.set_thumbnail(url=p["image"])
    embed.set_footer(text="Lazada Pokémon Bot 🎮")
    return embed

bot = discord.Bot()
alert_channel_id = int(os.environ.get("ALERT_CHANNEL_ID", "0"))
seen_alert_ids   = set()

@bot.slash_command(name="track", description="Track a Lazada Pokémon product")
async def cmd_track(ctx,
    url: discord.Option(str, "Lazada product URL"),
    target_price: discord.Option(float, "Alert when price drops to this (₱)"),
    name: discord.Option(str, "Custom name", required=False, default=""),
):
    await ctx.defer()
    try:
        p = await api_post("/products", {"url": url, "target_price": target_price, "name": name or None})
        embed = discord.Embed(title="✅ Now tracking!", colour=POKE_GOLD,
            description=f"**{p.get('name','Product')}** added. Alert when it hits {price_str(target_price)}.")
        embed.add_field(name="ID", value=str(p["id"]), inline=True)
        embed.add_field(name="Target", value=price_str(target_price), inline=True)
        if p.get("image"): embed.set_thumbnail(url=p["image"])
        await ctx.respond(embed=embed)
    except Exception as e:
        await ctx.respond(f"❌ Failed: `{e}`")

@bot.slash_command(name="list", description="Show all tracked products")
async def cmd_list(ctx):
    await ctx.defer()
    try:
        products = await api_get("/products")
        if not products:
            await ctx.respond("📭 Nothing tracked yet. Use `/track <url> <price>`!")
            return
        header = discord.Embed(title=f"🎮 Tracking {len(products)} product(s)", colour=POKE_GOLD)
        await ctx.respond(embeds=[header] + [product_embed(p) for p in products[:9]])
    except Exception as e:
        await ctx.respond(f"❌ Error: `{e}`")

@bot.slash_command(name="check", description="Force a refresh right now")
async def cmd_check(ctx, product_id: discord.Option(int, "Product ID from /list")):
    await ctx.defer()
    try:
        p = await api_post(f"/products/{product_id}/refresh")
        embed = product_embed(p, POKE_BLUE)
        embed.title = f"🔄 Refreshed — {embed.title}"
        await ctx.respond(embed=embed)
    except Exception as e:
        await ctx.respond(f"❌ Error: `{e}`")

@bot.slash_command(name="remove", description="Stop tracking a product")
async def cmd_remove(ctx, product_id: discord.Option(int, "Product ID from /list")):
    await ctx.defer()
    try:
        products = await api_get("/products")
        p    = next((x for x in products if x["id"] == product_id), None)
        name = p["name"] if p else f"Product #{product_id}"
        code = await api_delete(f"/products/{product_id}")
        if code == 204:
            await ctx.respond(f"🗑️ Stopped tracking **{name}**.")
        else:
            await ctx.respond(f"❌ ID `{product_id}` not found.")
    except Exception as e:
        await ctx.respond(f"❌ Error: `{e}`")

@bot.slash_command(name="checkout", description="Get a direct buy link")
async def cmd_checkout(ctx, product_id: discord.Option(int, "Product ID from /list")):
    await ctx.defer()
    try:
        products = await api_get("/products")
        p = next((x for x in products if x["id"] == product_id), None)
        if not p:
            await ctx.respond(f"❌ ID `{product_id}` not found.")
            return
        stock  = p.get("stock", "Unknown")
        colour = POKE_GREEN if stock == "In Stock" else (0xFFA500 if stock == "Low Stock" else POKE_RED)
        status = {"In Stock": "✅ In stock — ready to buy!", "Low Stock": "⚡ Low stock — grab it fast!", "Out of Stock": "⚠️ Out of stock."}.get(stock, "⚪ Unknown.")
        embed  = discord.Embed(title=f"🛒 {p.get('name','Product')[:200]}", url=p.get("url",""), description=status, colour=colour)
        embed.add_field(name="💰 Price",  value=price_str(p.get("current_price")), inline=True)
        embed.add_field(name="🎯 Target", value=price_str(p.get("target_price")),  inline=True)
        embed.add_field(name=f"{stock_emoji(stock)} Stock", value=stock, inline=True)
        embed.add_field(name="🔗 Link", value=f"[Open on Lazada →]({p.get('url','')})", inline=False)
        if p.get("image"): embed.set_thumbnail(url=p["image"])
        await ctx.respond(embed=embed)
    except Exception as e:
        await ctx.respond(f"❌ Error: `{e}`")

@bot.slash_command(name="setalerts", description="Set the auto-alert channel (admin)")
@discord.default_permissions(manage_channels=True)
async def cmd_setalerts(ctx, channel: discord.Option(discord.TextChannel, "Channel for alerts")):
    global alert_channel_id
    alert_channel_id = channel.id
    await ctx.respond(f"✅ Alerts will go to {channel.mention}.", ephemeral=True)

@bot.slash_command(name="help", description="Show all commands")
async def cmd_help(ctx):
    embed = discord.Embed(title="🎮 Lazada Pokémon Bot — Commands", colour=POKE_GOLD)
    for cmd, desc in [
        ("/track <url> <price>", "Start tracking a product"),
        ("/list",                "Show all tracked products"),
        ("/check <id>",          "Force a refresh now"),
        ("/checkout <id>",       "Get a direct buy link"),
        ("/remove <id>",         "Stop tracking"),
        ("/setalerts #channel",  "Set alert channel (admin)"),
        ("/help",                "Show this message"),
    ]:
        embed.add_field(name=cmd, value=desc, inline=False)
    embed.set_footer(text="Auto-refreshes every 15 min • Railway 🚂")
    await ctx.respond(embed=embed, ephemeral=True)

@tasks.loop(seconds=30)
async def poll_alerts():
    if not alert_channel_id:
        return
    channel = bot.get_channel(alert_channel_id)
    if not channel:
        return
    try:
        new_alerts = await api_get("/alerts")
        for a in new_alerts:
            aid = a.get("id")
            if aid in seen_alert_ids:
                continue
            seen_alert_ids.add(aid)
            colour = {"price": POKE_GOLD, "drop": POKE_GREEN, "stock": POKE_BLUE}.get(a.get("type"), POKE_GOLD)
            embed  = discord.Embed(title=a.get("message","Alert"), colour=colour)
            pid    = a.get("product_id")
            if pid:
                embed.add_field(name="ID",      value=str(pid),           inline=True)
                embed.add_field(name="🛒 Buy",  value=f"`/checkout {pid}`", inline=True)
            embed.set_footer(text="Lazada Pokémon Bot 🎮")
            await channel.send(embed=embed)
    except Exception as e:
        logger.warning(f"Poll error: {e}")

@bot.event
async def on_ready():
    poll_alerts.start()
    logger.info(f"✅ Bot ready as {bot.user}")

bot.run(DISCORD_BOT_TOKEN)
