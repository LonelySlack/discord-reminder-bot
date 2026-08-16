# Discord Reminder + Quest bot

Two pieces you can run independently:

1. **`bot.py`** — a normal, ToS-safe Discord **bot**: personal reminders **and**
   an admin-managed Discord Quest announcer (announces quests, shows a live
   countdown, and auto-pings before a quest ends).
2. **`quest_notifier.py`** — an **automatic** new-quest notifier that watches
   Discord's quest list and posts to a webhook when a new quest appears.
   ⚠️ This one uses a **user account token** — see the big warning below.

---

## 1. The bot (`bot.py`) — safe

### Commands

| Command | What it does |
| --- | --- |
| `/remind <when> <message>` | Personal reminder. `when` = `30s`, `15m`, `2h`, `1d`, `1h30m`. |
| `/reminders` | List your pending reminders. |
| `/cancel <id>` | Cancel a reminder. |
| `/addquest <name> <ends> [url] [channel]` | *(Manage Server)* Announce a quest and track its end. `ends` = a date `2026-07-20` / `2026-07-20 18:00` (UTC) or a duration like `14d`. |
| `/quests` | List active quests with countdowns. |
| `/delquest <id>` | *(Manage Server)* Stop tracking a quest. |

When a quest is ~1 day and ~1 hour from ending, the bot automatically pings the
announcement channel. Everything persists in SQLite across restarts.

### Setup

```bash
cd discord-reminder-bot
pip install -r requirements.txt        # discord.py, aiohttp, python-dotenv
cp .env.example .env                    # add DISCORD_TOKEN
python bot.py
```

Create the bot at <https://discord.com/developers/applications> → **Bot** →
copy the token. Invite it with the `bot` + `applications.commands` scopes. No
privileged intents required.

---

## 2. The quest notifier (`quest_notifier.py`) — automatic, **ToS risk**

> ## ⚠️ READ THIS FIRST
> Discord has **no bot API for quests**. The only way to auto-detect new quests
> is to call `GET /quests/@me` with a **user account token**. Automating a user
> account ("self-botting") **violates Discord's Terms of Service**, and the
> account **can be banned**.
>
> - Use a **throwaway / alt account** — **never your main**.
> - Put the token in `.env` only. Never share or commit it.
> - You chose this route knowing the risk.

### What it does

Every ~10 minutes it checks your quest list. When a **new** quest appears it
posts an embed to a Discord **webhook** with the quest name, its reward, and
**when it ends** (`<t:...:R>` live countdown). Seen quests are remembered in
`seen_quests.json` so you're not pinged twice.

### Setup

1. **Make a webhook** for wherever you want notifications: *Server Settings →
   Integrations → Webhooks → New Webhook → Copy Webhook URL*.
2. **Get the alt account's user token** and put it, plus the webhook URL, in
   `.env` (`DISCORD_USER_TOKEN`, `QUEST_WEBHOOK_URL`).
3. Run it:
   ```bash
   python quest_notifier.py
   ```

### How to get a user token (alt account)

In a browser logged into the **alt** account: open DevTools (F12) → **Network**
tab → refresh → click any `discord.com/api` request → **Request Headers** →
copy the `authorization` value. That's the user token. (This is the account
you're risking — keep it an alt.)

---

## FAQ

- **Why not just a bot for quests?** Bots get `401` on `/quests/@me`; quests are
  a user-account feature only.
- **Will the notifier get my account banned?** It can. That's why it's an alt.
  It keeps requests infrequent and doesn't disguise itself.
- **Can the safe bot pull quests automatically?** Only if you give it a JSON feed
  (`QUEST_FEED_URL`) — e.g. point it at output you generate yourself. Otherwise
  admins add quests with `/addquest`.
