"""
Discord Reminder + Quest bot
----------------------------
Reminders (per user):
  /remind <when> <message>   -> set a reminder (e.g. /remind 30m drink water)
  /reminders                 -> list your pending reminders
  /cancel <id>               -> cancel one of your reminders

Discord Quests (server announcements, admin-managed):
  /addquest <name> <ends> [url] [channel]  -> announce a quest + track its end
  /quests                                  -> list active quests with countdowns
  /delquest <id>                           -> remove a tracked quest

The bot announces each quest, shows a live countdown, and automatically pings
the channel when a quest is ~1 day and ~1 hour from ending.

Note: Discord has no bot-accessible API for Quests, so quests are added by an
admin (or by an optional JSON feed — see QUEST_FEED_URL below). Everything is
stored in SQLite so it survives restarts.
"""

import os
import re
import json
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "reminders.db")

# Optional automatic quest feed (see README). If unset, quests are added by hand.
QUEST_FEED_URL = os.getenv("QUEST_FEED_URL")
QUEST_FEED_CHANNEL_ID = os.getenv("QUEST_FEED_CHANNEL_ID")

# How long before a quest ends to ping the channel.
DAY_LEAD = int(os.getenv("QUEST_DAY_LEAD_SECONDS", str(24 * 3600)))
HOUR_LEAD = int(os.getenv("QUEST_HOUR_LEAD_SECONDS", str(3600)))

BRAND = 0x5865F2  # Discord blurple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("quest-bot")


# --------------------------------------------------------------------------- #
# Time parsing / formatting
# --------------------------------------------------------------------------- #
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_TOKEN_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_ABS_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def parse_duration(text: str) -> int | None:
    """Parse '30m', '2h', '1d', '1h30m', '90s' into seconds, else None."""
    text = text.strip().lower()
    matches = _TOKEN_RE.findall(text)
    if not matches:
        return None
    if _TOKEN_RE.sub("", text).strip():  # reject stray junk like '30mabc'
        return None
    total = sum(int(v) * _UNIT_SECONDS[u] for v, u in matches)
    return total if total > 0 else None


def parse_when(text: str) -> datetime | None:
    """Parse either a duration ('14d') or an absolute UTC date/time.

    Absolute forms (treated as UTC): '2026-07-20', '2026-07-20 18:00'.
    Returns a timezone-aware UTC datetime, or None.
    """
    secs = parse_duration(text)
    if secs is not None:
        return datetime.now(timezone.utc) + timedelta(seconds=secs)
    text = text.strip()
    for fmt in _ABS_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def human_delta(seconds: int) -> str:
    """Render seconds as a short human string, e.g. '1d 2h 5m'."""
    seconds = int(seconds)
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            qty, seconds = divmod(seconds, size)
            parts.append(f"{qty}{unit}")
    return " ".join(parts) if parts else "0s"


def iso(dt: datetime) -> str:
    return dt.isoformat()


def from_iso(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message    TEXT    NOT NULL,
                due_at     TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quests (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL,
                url           TEXT,
                channel_id    INTEGER NOT NULL,
                ends_at       TEXT    NOT NULL,
                notified_day  INTEGER NOT NULL DEFAULT 0,
                notified_hour INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self.conn.commit()

    # ----- reminders ----- #
    def add_reminder(self, user_id, channel_id, message, due_at) -> int:
        cur = self.conn.execute(
            "INSERT INTO reminders (user_id, channel_id, message, due_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, channel_id, message, iso(due_at)),
        )
        self.conn.commit()
        return cur.lastrowid

    def due_reminders(self, now) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reminders WHERE due_at <= ? ORDER BY due_at", (iso(now),)
        ).fetchall()

    def reminders_for(self, user_id) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reminders WHERE user_id = ? ORDER BY due_at", (user_id,)
        ).fetchall()

    def delete_reminder(self, rid, user_id) -> bool:
        cur = self.conn.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?", (rid, user_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete_reminder_by_id(self, rid) -> None:
        self.conn.execute("DELETE FROM reminders WHERE id = ?", (rid,))
        self.conn.commit()

    # ----- quests ----- #
    def add_quest(self, name, url, channel_id, ends_at) -> int:
        cur = self.conn.execute(
            "INSERT INTO quests (name, url, channel_id, ends_at) VALUES (?, ?, ?, ?)",
            (name, url, channel_id, iso(ends_at)),
        )
        self.conn.commit()
        return cur.lastrowid

    def active_quests(self, now) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM quests WHERE ends_at > ? ORDER BY ends_at", (iso(now),)
        ).fetchall()

    def all_quests(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM quests ORDER BY ends_at"
        ).fetchall()

    def quest_exists(self, name, ends_at) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM quests WHERE name = ? AND ends_at = ?",
            (name, iso(ends_at)),
        ).fetchone()
        return row is not None

    def mark_quest_notified(self, qid, *, day=False, hour=False) -> None:
        if day:
            self.conn.execute(
                "UPDATE quests SET notified_day = 1 WHERE id = ?", (qid,)
            )
        if hour:
            self.conn.execute(
                "UPDATE quests SET notified_hour = 1 WHERE id = ?", (qid,)
            )
        self.conn.commit()

    def delete_quest(self, qid) -> bool:
        cur = self.conn.execute("DELETE FROM quests WHERE id = ?", (qid,))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_expired_quests(self, now) -> None:
        self.conn.execute("DELETE FROM quests WHERE ends_at <= ?", (iso(now),))
        self.conn.commit()


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
class QuestBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.db = DB(DB_PATH)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        await self.tree.sync()
        self.check_reminders.start()
        self.check_quests.start()
        if QUEST_FEED_URL:
            self.poll_feed.start()
            log.info("Quest feed polling enabled: %s", QUEST_FEED_URL)
        log.info("Commands synced; loops started.")

    # ----- reminders loop ----- #
    @tasks.loop(seconds=15)
    async def check_reminders(self):
        now = datetime.now(timezone.utc)
        for row in self.db.due_reminders(now):
            await self._fire_reminder(row)
            self.db.delete_reminder_by_id(row["id"])

    @check_reminders.before_loop
    async def _before_reminders(self):
        await self.wait_until_ready()

    async def _fire_reminder(self, row):
        text = f"⏰ <@{row['user_id']}> reminder: **{row['message']}**"
        channel = self.get_channel(row["channel_id"])
        try:
            if channel is not None:
                await channel.send(text)
                return
        except discord.HTTPException:
            pass
        try:
            user = await self.fetch_user(row["user_id"])
            await user.send(text)
        except discord.HTTPException:
            log.warning("Could not deliver reminder %s", row["id"])

    # ----- quests loop ----- #
    @tasks.loop(seconds=60)
    async def check_quests(self):
        now = datetime.now(timezone.utc)
        for q in self.db.active_quests(now):
            secs_left = (from_iso(q["ends_at"]) - now).total_seconds()
            if secs_left <= DAY_LEAD and not q["notified_day"]:
                await self._ping_ending(q, "in about a day")
                self.db.mark_quest_notified(q["id"], day=True)
            if secs_left <= HOUR_LEAD and not q["notified_hour"]:
                await self._ping_ending(q, "very soon")
                self.db.mark_quest_notified(q["id"], hour=True)
        self.db.delete_expired_quests(now)

    @check_quests.before_loop
    async def _before_quests(self):
        await self.wait_until_ready()

    async def _ping_ending(self, q, phrase):
        channel = self.get_channel(q["channel_id"])
        if channel is None:
            return
        unix = int(from_iso(q["ends_at"]).timestamp())
        link = f"\n{q['url']}" if q["url"] else ""
        try:
            await channel.send(
                f"⏳ Discord Quest **{q['name']}** ends {phrase} — "
                f"<t:{unix}:R> (<t:{unix}:F>). Don't miss it!{link}"
            )
        except discord.HTTPException:
            log.warning("Could not ping ending quest %s", q["id"])

    async def announce_quest(self, channel, name, url, ends_at):
        unix = int(ends_at.timestamp())
        embed = discord.Embed(
            title="🎯 New Discord Quest!",
            description=f"**{name}**",
            color=BRAND,
        )
        embed.add_field(
            name="Ends", value=f"<t:{unix}:F> (<t:{unix}:R>)", inline=False
        )
        if url:
            embed.add_field(name="Link", value=url, inline=False)
        embed.set_footer(text="I'll ping here before it ends.")
        await channel.send(embed=embed)

    # ----- optional automatic feed ----- #
    @tasks.loop(minutes=15)
    async def poll_feed(self):
        """If QUEST_FEED_URL is set, fetch a JSON list of quests and add new ones.

        Expected JSON: a list of objects like
          {"name": "...", "ends_at": "2026-07-20T00:00:00+00:00", "url": "..."}
        """
        if not QUEST_FEED_CHANNEL_ID:
            log.warning("QUEST_FEED_URL set but QUEST_FEED_CHANNEL_ID is missing.")
            return
        channel = self.get_channel(int(QUEST_FEED_CHANNEL_ID))
        if channel is None:
            log.warning("Feed channel %s not found.", QUEST_FEED_CHANNEL_ID)
            return
        try:
            if self.session is None:
                self.session = aiohttp.ClientSession()
            async with self.session.get(QUEST_FEED_URL) as resp:
                resp.raise_for_status()
                data = json.loads(await resp.text())
        except Exception as exc:  # noqa: BLE001 - feed is best-effort
            log.warning("Quest feed fetch failed: %s", exc)
            return
        for item in data:
            try:
                name = item["name"]
                ends_at = from_iso(item["ends_at"])
            except (KeyError, ValueError, TypeError):
                continue
            if ends_at <= datetime.now(timezone.utc):
                continue
            if self.db.quest_exists(name, ends_at):
                continue
            url = item.get("url")
            self.db.add_quest(name, url, channel.id, ends_at)
            await self.announce_quest(channel, name, url, ends_at)
            log.info("Added quest from feed: %s", name)

    @poll_feed.before_loop
    async def _before_feed(self):
        await self.wait_until_ready()

    async def close(self):
        if self.session is not None:
            await self.session.close()
        await super().close()


client = QuestBot()


# --------------------------------------------------------------------------- #
# Reminder commands
# --------------------------------------------------------------------------- #
@client.tree.command(name="remind", description="Set a reminder, e.g. 30m or 1h30m")
@app_commands.describe(
    when="How long from now: 30s, 15m, 2h, 1d, 1h30m ...",
    message="What should I remind you about?",
)
async def remind(interaction: discord.Interaction, when: str, message: str):
    secs = parse_duration(when)
    if secs is None:
        await interaction.response.send_message(
            "❌ I couldn't understand that time. Try `30s`, `15m`, `2h`, `1d`, or `1h30m`.",
            ephemeral=True,
        )
        return
    if secs > 365 * 86400:
        await interaction.response.send_message(
            "❌ That's more than a year away — pick something shorter.", ephemeral=True
        )
        return
    due = datetime.now(timezone.utc) + timedelta(seconds=secs)
    rid = client.db.add_reminder(interaction.user.id, interaction.channel_id, message, due)
    unix = int(due.timestamp())
    await interaction.response.send_message(
        f"✅ I'll remind you in **{human_delta(secs)}** (<t:{unix}:R>).\n"
        f"Reminder `#{rid}`: {message}"
    )


@client.tree.command(name="reminders", description="List your pending reminders")
async def reminders(interaction: discord.Interaction):
    rows = client.db.reminders_for(interaction.user.id)
    if not rows:
        await interaction.response.send_message("You have no pending reminders. 🎉", ephemeral=True)
        return
    lines = [
        f"`#{r['id']}` <t:{int(from_iso(r['due_at']).timestamp())}:R> — {r['message']}"
        for r in rows
    ]
    await interaction.response.send_message(
        "**Your reminders:**\n" + "\n".join(lines), ephemeral=True
    )


@client.tree.command(name="cancel", description="Cancel a reminder by its id")
@app_commands.describe(id="The reminder id shown in /reminders")
async def cancel(interaction: discord.Interaction, id: int):
    if client.db.delete_reminder(id, interaction.user.id):
        await interaction.response.send_message(f"🗑️ Cancelled reminder `#{id}`.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"❌ No reminder `#{id}` found that belongs to you.", ephemeral=True
        )


# --------------------------------------------------------------------------- #
# Quest commands (admin-managed)
# --------------------------------------------------------------------------- #
@client.tree.command(name="addquest", description="Announce a Discord Quest and track when it ends")
@app_commands.describe(
    name="Quest name, e.g. 'Play Fortnite'",
    ends="When it ends: a date '2026-07-20' / '2026-07-20 18:00' (UTC), or a duration like '14d'",
    url="Optional link to the quest",
    channel="Optional channel to announce in (defaults to here)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def addquest(
    interaction: discord.Interaction,
    name: str,
    ends: str,
    url: str | None = None,
    channel: discord.TextChannel | None = None,
):
    ends_at = parse_when(ends)
    if ends_at is None:
        await interaction.response.send_message(
            "❌ I couldn't understand that end time. Use a date like `2026-07-20`, "
            "`2026-07-20 18:00` (UTC), or a duration like `14d`.",
            ephemeral=True,
        )
        return
    if ends_at <= datetime.now(timezone.utc):
        await interaction.response.send_message(
            "❌ That end time is already in the past.", ephemeral=True
        )
        return
    target = channel or interaction.channel
    qid = client.db.add_quest(name, url, target.id, ends_at)
    await client.announce_quest(target, name, url, ends_at)
    unix = int(ends_at.timestamp())
    where = "here" if target.id == interaction.channel_id else target.mention
    await interaction.response.send_message(
        f"✅ Quest `#{qid}` **{name}** announced in {where}. Ends <t:{unix}:R>.",
        ephemeral=True,
    )


@client.tree.command(name="quests", description="List active Discord Quests being tracked")
async def quests(interaction: discord.Interaction):
    rows = client.db.active_quests(datetime.now(timezone.utc))
    if not rows:
        await interaction.response.send_message("No active quests are being tracked right now.", ephemeral=True)
        return
    lines = []
    for q in rows:
        unix = int(from_iso(q["ends_at"]).timestamp())
        link = f" — {q['url']}" if q["url"] else ""
        lines.append(f"`#{q['id']}` **{q['name']}** — ends <t:{unix}:R>{link}")
    await interaction.response.send_message(
        "**Active Discord Quests:**\n" + "\n".join(lines), ephemeral=True
    )


@client.tree.command(name="delquest", description="Stop tracking a quest by its id")
@app_commands.describe(id="The quest id shown in /quests")
@app_commands.checks.has_permissions(manage_guild=True)
async def delquest(interaction: discord.Interaction, id: int):
    if client.db.delete_quest(id):
        await interaction.response.send_message(f"🗑️ Removed quest `#{id}`.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ No quest `#{id}` found.", ephemeral=True)


@addquest.error
@delquest.error
async def _quest_perm_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the **Manage Server** permission to manage quests.", ephemeral=True
        )
    else:
        log.exception("Quest command error", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)


@client.event
async def on_ready():
    log.info("Logged in as %s (id: %s)", client.user, client.user.id)


def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and add your token.")
    client.run(TOKEN)


if __name__ == "__main__":
    main()
