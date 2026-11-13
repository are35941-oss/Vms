import asyncio
import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vps-bot")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
API_KEY = os.getenv("CLOUD_API_KEY", "").strip()
SSH_KEY = os.getenv("SSH_KEY_ID", "").strip()
try:
    ADMIN_ID = int(os.getenv("ADMIN_USER_ID", ""))
except ValueError:
    raise SystemExit("ADMIN_USER_ID must be a numeric Discord user ID")
if not TOKEN or not API_KEY or not SSH_KEY:
    raise SystemExit("DISCORD_TOKEN, CLOUD_API_KEY and SSH_KEY_ID are required")

BASE_URL = "https://api.digitalocean.com/v2"
TIMEOUT = aiohttp.ClientTimeout(total=30)


class CloudError(Exception):
    pass


class CloudClient:
    def __init__(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )

    async def close(self):
        await self.session.close()

    async def request(self, method, path, **kwargs):
        for attempt in range(4):
            try:
                async with self.session.request(method, BASE_URL + path, **kwargs) as response:
                    if response.status == 429:
                        if attempt == 3:
                            raise CloudError("Cloud API rate limit exceeded; try again later.")
                        try:
                            delay = min(max(float(response.headers.get("Retry-After", "5")), 1), 30)
                        except ValueError:
                            delay = 5
                        await asyncio.sleep(delay)
                        continue
                    if response.status == 204:
                        return {}
                    try:
                        data = await response.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError):
                        raise CloudError(f"Cloud API returned an invalid response (HTTP {response.status}).")
                    if response.status >= 400:
                        message = data.get("message", "Request failed") if isinstance(data, dict) else "Request failed"
                        raise CloudError(f"Cloud API error (HTTP {response.status}): {message}")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # A timed-out mutation may already have succeeded; never retry it.
                if method != "GET" or attempt == 3:
                    raise CloudError("Cloud API network error or timeout. For changes, check /list before retrying.") from exc
                await asyncio.sleep(2 ** attempt)
        raise CloudError("Cloud request failed.")

    async def droplets(self):
        droplets = []
        page = 1
        while True:
            data = await self.request("GET", "/droplets", params={"per_page": 200, "page": page})
            droplets.extend(data.get("droplets", []))
            if not data.get("links", {}).get("pages", {}).get("next"):
                return droplets
            page += 1


def ipv4(droplet):
    return next((network["ip_address"] for network in droplet.get("networks", {}).get("v4", [])
                 if network.get("type") == "public"), None)


def instance_id(value):
    if not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise CloudError("Instance ID must be a positive numeric DigitalOcean droplet ID.")
    return str(int(value))


class VPSBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.cloud = None

    async def setup_hook(self):
        self.cloud = CloudClient()
        await self.tree.sync()

    async def on_ready(self):
        log.info("Connected as %s", self.user)
        print("KODARI_READY", flush=True)

    async def close(self):
        if self.cloud:
            await self.cloud.close()
        await super().close()


bot = VPSBot()


async def admin_only(interaction: discord.Interaction):
    return interaction.user.id == ADMIN_ID


bot.tree.interaction_check = admin_only


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(error, app_commands.CheckFailure):
        message = "You are not authorized to use this bot."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"Please retry in {error.retry_after:.0f} seconds."
    elif isinstance(original, CloudError):
        message = str(original)
    else:
        log.error("Command failed", exc_info=(type(original), original, original.__traceback__))
        message = "An unexpected error occurred. Check the bot logs."
    if interaction.response.is_done():
        await interaction.followup.send(message[:1900], ephemeral=True)
    else:
        await interaction.response.send_message(message[:1900], ephemeral=True)


@bot.tree.command(description="Provision a DigitalOcean droplet")
@app_commands.describe(provider="digitalocean", region="Region slug, e.g. nyc3", size="Size slug, e.g. s-1vcpu-1gb", os="Image slug, e.g. ubuntu-24-04-x64")
@app_commands.checks.cooldown(1, 10)
async def deploy(interaction: discord.Interaction, provider: str, region: str, size: str, os: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if provider.lower() != "digitalocean":
        raise CloudError("Only the digitalocean provider is supported.")
    try:
        dm = await interaction.user.create_dm()
        await dm.send("Your VPS deployment is starting. The result will be sent here.")
    except discord.HTTPException as exc:
        raise CloudError("Could not DM you. Enable DMs from server members and try again; no VPS was created.") from exc

    data = await bot.cloud.request("POST", "/droplets", json={
        "name": f"discord-vps-{interaction.id}", "region": region, "size": size,
        "image": os, "ssh_keys": [SSH_KEY], "backups": False,
        "ipv6": False, "monitoring": False,
    })
    droplet = data["droplet"]
    droplet_id = droplet["id"]
    await interaction.followup.send(f"Created droplet `{droplet_id}`. Waiting up to 10 minutes for its public IPv4; charges may apply.", ephemeral=True)
    try:
        deadline = asyncio.get_running_loop().time() + 600
        while asyncio.get_running_loop().time() < deadline:
            droplet = (await bot.cloud.request("GET", f"/droplets/{droplet_id}"))["droplet"]
            address = ipv4(droplet)
            if droplet.get("status") == "active" and address:
                image = droplet.get("image") or {}
                await dm.send(f"VPS ready\nID: `{droplet_id}`\nIPv4: `{address}`\nOS: {image.get('distribution', '')} {image.get('name', os)}\nSSH key: `{SSH_KEY}` (installed by DigitalOcean; no root password supplied).")
                return
            await asyncio.sleep(10)
        await dm.send(f"Droplet `{droplet_id}` was created but did not become ready within 10 minutes. Check /list; it may still incur charges.")
    except (CloudError, discord.HTTPException) as exc:
        log.warning("Deployment follow-up for droplet %s failed: %s", droplet_id, exc)
        await interaction.followup.send(f"Droplet `{droplet_id}` was created, but status polling or DM delivery failed. Check /list; it may still incur charges.", ephemeral=True)


@bot.tree.command(name="list", description="List DigitalOcean droplets")
@app_commands.checks.cooldown(1, 5)
async def list_instances(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    droplets = await bot.cloud.droplets()
    if not droplets:
        await interaction.followup.send("No droplets found.", ephemeral=True)
        return
    for start in range(0, len(droplets), 10):
        embed = discord.Embed(title="DigitalOcean droplets", color=discord.Color.blurple())
        for droplet in droplets[start:start + 10]:
            region = droplet.get("region") or {}
            embed.add_field(name=f"{droplet.get('name', 'Droplet')[:200]} · {droplet['id']}",
                            value=f"IP: {ipv4(droplet) or 'Pending'}\nStatus: {droplet.get('status', 'unknown')}\nRegion: {region.get('slug', 'unknown')}", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(description="Start, stop, or reboot a droplet")
@app_commands.choices(action=[app_commands.Choice(name=x, value=x) for x in ("start", "stop", "reboot")])
@app_commands.checks.cooldown(1, 3)
async def power(interaction: discord.Interaction, instance_id: str, action: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True, thinking=True)
    droplet_id = globals()["instance_id"](instance_id)
    action_type = {"start": "power_on", "stop": "power_off", "reboot": "reboot"}[action.value]
    result = await bot.cloud.request("POST", f"/droplets/{droplet_id}/actions", json={"type": action_type})
    await interaction.followup.send(f"Action `{action.value}` submitted for `{droplet_id}` (action ID `{result['action']['id']}`).", ephemeral=True)


class DeleteConfirmation(discord.ui.View):
    def __init__(self, droplet_id):
        super().__init__(timeout=60)
        self.droplet_id = droplet_id
        self.used = False

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != ADMIN_ID:
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Permanently delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.used:
            await interaction.response.send_message("This confirmation has already been used.", ephemeral=True)
            return
        self.used = True
        self.stop()
        await interaction.response.edit_message(content=f"Deleting droplet `{self.droplet_id}`...", view=None)
        try:
            await bot.cloud.request("DELETE", f"/droplets/{self.droplet_id}")
            await interaction.followup.send(f"Deletion requested for droplet `{self.droplet_id}`.", ephemeral=True)
        except CloudError as exc:
            await interaction.followup.send(str(exc)[:1900], ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.used = True
        self.stop()
        await interaction.response.edit_message(content="Deletion cancelled.", view=None)


@bot.tree.command(description="Permanently delete a droplet after confirmation")
@app_commands.checks.cooldown(1, 3)
async def delete(interaction: discord.Interaction, instance_id: str):
    droplet_id = globals()["instance_id"](instance_id)
    await interaction.response.send_message(f"Permanently destroy droplet `{droplet_id}`? This cannot be undone. Confirmation expires in 60 seconds.", view=DeleteConfirmation(droplet_id), ephemeral=True)


bot.run(TOKEN, log_handler=None)