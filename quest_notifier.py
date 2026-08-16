"""
Discord Quest notifier (user-token, standalone)
===============================================

Polls Discord's internal quests endpoint and posts to a webhook whenever a NEW
quest appears, including when it ends.

⚠️  IMPORTANT — READ THIS ⚠️
This logs in with a USER account token to reach `GET /quests/@me`, because there
is no bot-accessible quest API. Automating a user account ("self-botting")
VIOLATES Discord's Terms of Service and the account CAN BE BANNED for it.

  * Use a THROWAWAY / ALT account here — never your main.
  * Keep the token in .env only. Never share it or commit it.

You accepted this tradeoff. This script keeps requests infrequent and does not
try to disguise itself as the real client.

Setup (.env):
  DISCORD_USER_TOKEN=<the alt account's user token>
  QUEST_WEBHOOK_URL=<a Discord webhook URL to post notifications to>
  QUEST_POLL_SECONDS=600          # optional, default 600 (10 min)
  QUEST_NOTIFY_EXISTING=true      # optional, notify about already-active quests on first run

Run:
  python quest_notifier.py
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv

load_dotenv()

USER_TOKEN = os.getenv("DISCORD_USER_TOKEN")
WEBHOOK_URL = os.getenv("QUEST_WEBHOOK_URL")
POLL_SECONDS = int(os.getenv("QUEST_POLL_SECONDS", "600"))
NOTIFY_EXISTING = os.getenv("QUEST_NOTIFY_EXISTING", "true").lower() == "true"
SEEN_FILE = os.getenv("QUEST_SEEN_FILE", "seen_quests.json")

QUESTS_URL = "https://discord.com/api/v9/quests/@me"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("quest-notifier")


# --------------------------------------------------------------------------- #
# Parsing Discord's quest payload (defensively — the schema shifts over time)
# --------------------------------------------------------------------------- #
def _dig(obj, *path, default=None):
    """Safely walk nested dict keys; return default if any step is missing."""
    for key in path:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key)
        if obj is None:
            return default
    return obj


def extract_quest(raw: dict) -> dict | None:
    """Pull the useful fields out of one quest object.

    Returns {id, name, reward, starts_at, expires_at} or None if unusable.
    """
    cfg = raw.get("config") or {}
    qid = raw.get("id") or cfg.get("id")
    if not qid:
        return None

    name = (
        _dig(cfg, "messages", "quest_name")
        or _dig(cfg, "messages", "game_title")
        or _dig(cfg, "application", "name")
        or "Unknown quest"
    )

    reward = _dig(cfg, "rewards_config", "rewards", default=None)
    reward_name = None
    if isinstance(reward, list) and reward:
        reward_name = _dig(reward[0], "messages", "name") or reward[0].get("sku_id")

    return {
        "id": str(qid),
        "name": name,
        "reward": reward_name,
        "starts_at": cfg.get("starts_at"),
        "expires_at": cfg.get("expires_at"),
    }


def parse_quests(payload: dict) -> list[dict]:
    """Turn the /quests/@me response into a clean list of quests."""
    quests = payload.get("quests") if isinstance(payload, dict) else None
    if not isinstance(quests, list):
        return []
    out = []
    for raw in quests:
        q = extract_quest(raw)
        if q:
            out.append(q)
    return out


def to_unix(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    try:
        return int(datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Seen-quest persistence
# --------------------------------------------------------------------------- #
def load_seen() -> set[str]:
    try:
        with open(SEEN_FILE) as fh:
            return set(json.load(fh))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen: set[str]) -> None:
    with open(SEEN_FILE, "w") as fh:
        json.dump(sorted(seen), fh)


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #
async def fetch_quests(session: aiohttp.ClientSession) -> list[dict] | None:
    headers = {"Authorization": USER_TOKEN, "User-Agent": USER_AGENT}
    try:
        async with session.get(QUESTS_URL, headers=headers) as resp:
            if resp.status == 401:
                log.error("401 Unauthorized — the user token is invalid or expired.")
                return None
            if resp.status == 429:
                retry = (await resp.json()).get("retry_after", 60)
                log.warning("Rate limited; backing off %ss.", retry)
                await asyncio.sleep(float(retry))
                return None
            resp.raise_for_status()
            return parse_quests(await resp.json())
    except aiohttp.ClientError as exc:
        log.warning("Request failed: %s", exc)
        return None


def build_embed(quest: dict) -> dict:
    ends_unix = to_unix(quest["expires_at"])
    ends = (
        f"<t:{ends_unix}:F> (<t:{ends_unix}:R>)" if ends_unix else "unknown"
    )
    fields = [{"name": "Ends", "value": ends, "inline": False}]
    if quest["reward"]:
        fields.append({"name": "Reward", "value": str(quest["reward"]), "inline": False})
    return {
        "content": "🎯 **New Discord Quest available!**",
        "embeds": [
            {
                "title": quest["name"],
                "color": 0x5865F2,
                "fields": fields,
            }
        ],
    }


async def notify(session: aiohttp.ClientSession, quest: dict) -> None:
    if not WEBHOOK_URL:
        log.info("New quest (no webhook set): %s ends %s", quest["name"], quest["expires_at"])
        return
    try:
        async with session.post(WEBHOOK_URL, json=build_embed(quest)) as resp:
            if resp.status >= 400:
                log.warning("Webhook post failed (%s): %s", resp.status, await resp.text())
    except aiohttp.ClientError as exc:
        log.warning("Webhook request failed: %s", exc)


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
async def run():
    seen = load_seen()
    first_run = not seen
    async with aiohttp.ClientSession() as session:
        while True:
            quests = await fetch_quests(session)
            if quests is not None:
                for q in quests:
                    if q["id"] in seen:
                        continue
                    seen.add(q["id"])
                    if first_run and not NOTIFY_EXISTING:
                        continue
                    log.info("New quest: %s (ends %s)", q["name"], q["expires_at"])
                    await notify(session, q)
                save_seen(seen)
                first_run = False
            await asyncio.sleep(POLL_SECONDS)


def main():
    if not USER_TOKEN:
        raise SystemExit(
            "DISCORD_USER_TOKEN is not set. Add an ALT account's token to .env "
            "(never your main account)."
        )
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
