"""
discord_bot.py — Pokémon Lazada Alert Bot (Discord slash commands)

Commands:
  /track <url> <target_price>   — start tracking a Lazada product
  /list                         — show all tracked products
  /remove <id>                  — stop tracking a product
  /check <id>                   — force a price/stock refresh now
  /checkout <id>                — get a direct link to buy now
  /setalerts <channel>          — set which channel gets auto-alerts
  /help                         — show all commands
"""

import os
import asyncio
import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import tasks
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config from env ───────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
API_BASE = os.environ.get("API_BASE", "http://localhost:8000")
ALERT_CHANNEL_ID = int(os.environ.get("ALERT_CHANNEL_ID", "0"))  # set via /setalerts

POKE_RED   = 0xE3350D
POKE_GOLD  = 0xFFD700
POKE_GREEN = 0x4ade80
POKE_BLUE  = 0x3b82f6

# ── API client ────────────────────────────────────────────────────────────────
async def api_get(path: str):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API_BASE}{path}")
        r.raise_for_status()
        return r.json()

async def api_post(path: str, body: dict):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{API_BASE}{path}", json=body)
        r.raise_for_status()
        return r.json()

async def api_delete(path: str):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.delete(f"{API_BASE}{path}")
        return r.status_code

# ── Helpers ───────────────────────────────────────────────────────────────────
def stock_emoji(stock: str) -> str:
    return {"In Stock": "🟢", "Low Stock": "🟡", "Out of Stock": "🔴"}.get(stock, "⚪")

def price_str(p) -> str:
    return f"₱{p:,.0f}" if p else "—"

def product_embed(p: dict, colour: int = POKE_GOLD) -> discord.Embed:
    current = p.get("current_price")
    target  = p.get("target_price")
    stock   = p.get("stock", "Unknown")
    hit     = current and target and current <= target

    embed = discord.Embed(
        title=p.get("name", "Unknown Product")[:256],
        url=p.get("url", ""),
        colour=POKE_GREEN if hit else colour,
    )
    embed.add_field(name="💰 Current Price", value=price_str(current), inline=True)
    embed.add_field(name="🎯 Target Price",  value=price_str(target),  inline=True)
    embed.add_field(name=f"{stock_emoji(stock)} Stock", value=stock,   inline=True)
    embed.add_field(name="🆔 Product ID",   value=str(p["id"]),        inline=True)

    if hit:
        embed.add_field(name="🎉 Alert", value="TARGET PRICE REACHED!", inline=False)

    if p.get("price_history"):
        hist = p["price_history"][-5:]
        prices = " → ".join(f"₱{h['price']:,.0f}" for h in hist if h.get("price"))
        embed.add_field(name="📈 Price History", value=prices or "—", inline=False)

    if p.get("image"):
        embed.set_thumbnail(url=p["image"])

    last = p.get("last_checked")
    if last:
        embed.set_footer(text=f"Last checked {datetime.fromisoformat(last).strftime('%d %b %Y %H:%M')} UTC • Lazada Pokémon Bot 🎮")
    else:
        embed.set_footer(text="Not yet checked • Lazada Pokémon Bot 🎮")

    return embed

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

alert_channel_id: int = ALERT_CHANNEL_ID
seen_alert_ids: set[int] = set()   # avoid re-sending alerts

# ── Slash commands ─────────────────────────────────────────────────────────────

@tree.command(name="track", description="Track a Lazada Pokémon product and get alerted when it hits your price")
@app_commands.describe(
    url="Lazada product URL",
    target_price="Alert me when price drops to or below this (₱)",
    name="Optional custom name for this product",
)
async def cmd_track(interaction: discord.Interaction, url: str, target_price: float, name: str = ""):
    await interaction.response.defer(thinking=True)
    try:
        p = await api_post("/products", {"url": url, "target_price": target_price, "name": name or None})
        embed = discord.Embed(
            title="✅ Now tracking!",
            description=f"**{p.get('name', 'Product')}** added. I'll alert you when it hits {price_str(target_price)}.",
            colour=POKE_GOLD,
        )
        embed.add_field(name="Product ID", value=str(p["id"]), inline=True)
        embed.add_field(name="Target",     value=price_str(target_price), inline=True)
        embed.set_footer(text="Use /check <id> to refresh now • /list to see all tracked")
        if p.get("image"):
            embed.set_thumbnail(url=p["image"])
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to add product: `{e}`\nMake sure the URL is a valid Lazada product page.")

@tree.command(name="list", description="Show all tracked Pokémon products and their current prices")
async def cmd_list(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        products = await api_get("/products")
        if not products:
            await interaction.followup.send("📭 No products tracked yet. Use `/track <url> <price>` to add one!")
            return

        embeds = []
        for p in products[:10]:  # Discord allows max 10 embeds
            embeds.append(product_embed(p))

        header = discord.Embed(
            title=f"🎮 Tracking {len(products)} Pokémon product{'s' if len(products) != 1 else ''}",
            colour=POKE_GOLD,
        )
        await interaction.followup.send(embeds=[header] + embeds[:9])
    except Exception as e:
        await interaction.followup.send(f"❌ Couldn't fetch products: `{e}`")

@tree.command(name="check", description="Force a price & stock refresh for a tracked product right now")
@app_commands.describe(product_id="The product ID (get it from /list)")
async def cmd_check(interaction: discord.Interaction, product_id: int):
    await interaction.response.defer(thinking=True)
    try:
        p = await api_post(f"/products/{product_id}/refresh", {})
        embed = product_embed(p, colour=POKE_BLUE)
        embed.title = f"🔄 Refreshed — {embed.title}"
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Refresh failed: `{e}`\nCheck the product ID with `/list`.")

@tree.command(name="remove", description="Stop tracking a product")
@app_commands.describe(product_id="The product ID to remove (get it from /list)")
async def cmd_remove(interaction: discord.Interaction, product_id: int):
    await interaction.response.defer(thinking=True)
    try:
        products = await api_get("/products")
        p = next((x for x in products if x["id"] == product_id), None)
        name = p["name"] if p else f"Product #{product_id}"
        status = await api_delete(f"/products/{product_id}")
        if status == 204:
            await interaction.followup.send(f"🗑️ Stopped tracking **{name}**.")
        else:
            await interaction.followup.send(f"❌ Product ID `{product_id}` not found. Use `/list` to check IDs.")
    except Exception as e:
        await interaction.followup.send(f"❌ Remove failed: `{e}`")

@tree.command(name="checkout", description="Get a direct link to buy a tracked product on Lazada right now")
@app_commands.describe(product_id="The product ID (get it from /list)")
async def cmd_checkout(interaction: discord.Interaction, product_id: int):
    await interaction.response.defer(thinking=True)
    try:
        products = await api_get("/products")
        p = next((x for x in products if x["id"] == product_id), None)
        if not p:
            await interaction.followup.send(f"❌ Product ID `{product_id}` not found. Use `/list`.")
            return

        current = p.get("current_price")
        stock   = p.get("stock", "Unknown")

        if stock == "Out of Stock":
            colour = POKE_RED
            status_line = "⚠️ Currently out of stock — link included in case it restocks soon."
        elif stock == "Low Stock":
            colour = 0xFFA500
            status_line = "⚡ Low stock — grab it fast!"
        else:
            colour = POKE_GREEN
            status_line = "✅ In stock — ready to buy!"

        embed = discord.Embed(
            title=f"🛒 Buy Now — {p.get('name', 'Product')[:200]}",
            url=p.get("url", ""),
            description=status_line,
            colour=colour,
        )
        embed.add_field(name="💰 Current Price", value=price_str(current), inline=True)
        embed.add_field(name="🎯 Your Target",   value=price_str(p.get("target_price")), inline=True)
        embed.add_field(name=f"{stock_emoji(stock)} Stock", value=stock, inline=True)
        embed.add_field(
            name="🔗 Checkout Link",
            value=f"[Open on Lazada →]({p.get('url', '')})",
            inline=False,
        )
        if p.get("image"):
            embed.set_thumbnail(url=p["image"])
        embed.set_footer(text="Tap the link to go straight to the Lazada product page")

        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: `{e}`")

@tree.command(name="setalerts", description="Set which channel receives automatic price & stock alerts")
@app_commands.describe(channel="The channel to send alerts to")
@app_commands.checks.has_permissions(manage_channels=True)
async def cmd_setalerts(interaction: discord.Interaction, channel: discord.TextChannel):
    global alert_channel_id
    alert_channel_id = channel.id
    await interaction.response.send_message(
        f"✅ Automatic alerts will now be sent to {channel.mention}.\n"
        f"You'll be pinged whenever a tracked product hits your target price, drops 5%+, or comes back in stock.",
        ephemeral=True,
    )

@tree.command(name="help", description="Show all available commands")
async def cmd_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎮 Lazada Pokémon Alert Bot — Commands",
        colour=POKE_GOLD,
    )
    commands_list = [
        ("/track <url> <price>",   "Start tracking a Lazada product"),
        ("/list",                  "Show all tracked products & prices"),
        ("/check <id>",            "Force a refresh right now"),
        ("/checkout <id>",         "Get a direct buy link + stock status"),
        ("/remove <id>",           "Stop tracking a product"),
        ("/setalerts #channel",    "Set the channel for auto-alerts (admin)"),
        ("/help",                  "Show this message"),
    ]
    for cmd, desc in commands_list:
        embed.add_field(name=cmd, value=desc, inline=False)
    embed.set_footer(text="Auto-refreshes every 15 min • Powered by Railway 🚂")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── Background alert poller ───────────────────────────────────────────────────
@tasks.loop(seconds=30)
async def poll_alerts():
    """Check the API for new alerts and post them to the alert channel."""
    if not alert_channel_id:
        return
    channel = client.get_channel(alert_channel_id)
    if not channel:
        return
    try:
        new_alerts = await api_get("/alerts")
        for a in new_alerts:
            aid = a.get("id")
            if aid in seen_alert_ids:
                continue
            seen_alert_ids.add(aid)

            atype = a.get("type", "price")
            colour = {
                "price": POKE_GOLD,
                "drop":  POKE_GREEN,
                "stock": POKE_BLUE,
            }.get(atype, POKE_GOLD)

            embed = discord.Embed(
                title=a.get("message", "Alert"),
                colour=colour,
                timestamp=datetime.utcnow(),
            )
            pid = a.get("product_id")
            if pid:
                embed.add_field(name="Product ID", value=str(pid), inline=True)
                embed.add_field(name="🛒 Buy Now", value=f"Use `/checkout {pid}`", inline=True)
            embed.set_footer(text="Lazada Pokémon Alert Bot 🎮")

            await channel.send(embed=embed)
    except Exception as e:
        logger.warning(f"Alert poll error: {e}")

# ── Bot events ─────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    await tree.sync()
    poll_alerts.start()
    logger.info(f"✅ Bot ready as {client.user} — slash commands synced")

# ── Run ───────────────────────────────────────────────────────────────────────
client.run(DISCORD_BOT_TOKEN)
