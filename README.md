# Memecoin Server Verification Bot

An onboarding + verification bot across two channels:

1. **Read-me channel**: new members see only this. They tap **✅ I've read &
   understood**, get the `Verifying` role, and the read-me channel closes while
   the verify channel opens.
2. **Verify channel**: they tap **🔐 Start verification**. Everything from here
   is a private popup only they see, and each popup deletes itself before the
   next one appears:
   - 🌍 country (pick one)
   - 📈 trader level (pick one)
   - 🔔 pings (pick any, then **Done**; **No pings** or nothing picked gives the
     no-pings role)
   - **✅ Proceed to verify**
   - 🔐 image captcha (a wrong or expired code swaps in a fresh one)
3. They pass and get `Verified`, `Verifying` is removed, the verify channel
   closes and the rest of the server opens.

To change the questions, answers or role names, edit the `QUESTIONS` block at
the top of `bot.py`, push, then run `/setup` again (it reports any role IDs it can't find).

The bot manages **roles**; Discord **channel permissions** (tied to those roles)
control what each person can see.

---

## 1. Create the Discord application & bot

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. Open the **Bot** tab → **Reset Token** → copy the token (you'll paste it into
   Railway later). Keep it secret.
3. Still on the **Bot** tab, scroll to **Privileged Gateway Intents** and turn on
   **Server Members Intent**. Save.

## 2. Invite the bot to your server

On the **OAuth2 → URL Generator** tab:

- **Scopes:** `bot` and `applications.commands`
- **Bot Permissions:** `Manage Roles`, `Manage Channels`, `View Channels`,
  `Send Messages`, `Embed Links`, `Manage Messages`, `Read Message History`
  (or just tick **Administrator** for simplicity).

Copy the generated URL, open it, and add the bot to your server.

> After it joins, open **Server Settings → Roles** and drag the bot's role
> **above** where `Verifying` and `Verified` will sit (near the top). A bot can
> only manage roles below its own.

## 3. Push this folder to GitHub

```bash
git init
git add .
git commit -m "Verification bot"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

The `.gitignore` already keeps `.env` out of the repo.

## 4. Deploy on Railway

1. <https://railway.app> → **New Project → Deploy from GitHub repo** → pick this repo.
2. Open the service → **Variables** tab → add:

   | Variable | Value |
   |---|---|
   | `DISCORD_TOKEN` | your bot token from step 1 |
   | `GUILD_ID` | your server ID (Developer Mode → right-click server → Copy Server ID) |
   | `GATE_CHANNEL_ID` | `1570053267621417060` |
   | `CAPTCHA_CHANNEL_ID` | `1569526459176067072` |

   (The role names and `CAPTCHA_TTL` have sensible defaults — only set them to override.)
3. Railway builds and runs `python bot.py` automatically. Check the **Deploy logs**
   for `Logged in as ...`.

## 5. Configure the server

In Discord, run these slash commands (you need Administrator):

- **`/setup`** — sets both channels' permissions, posts the read-me and verify
  panels, and checks every onboarding role ID exists.
- **`/postrules`** — posts the rules embed in the rules channel.
- **`/lockdown`** *(optional)* — hides every other channel from `@everyone` and
  shows them to `Verified`. Asks for confirmation first. Admins are unaffected.

If you'd rather configure the rest of the server by hand instead of `/lockdown`:
for each members-only category, set `@everyone` → **View Channel: ❌** and
`Verified` → **View Channel: ✅**.

That's it — test it with an alt account or by temporarily removing your `Verified`
role.

---

## Local testing (optional)

```bash
pip install -r requirements.txt
cp .env.example .env    # then fill in DISCORD_TOKEN and GUILD_ID
python bot.py
```

## Troubleshooting

- **Slash commands don't show up** → make sure `GUILD_ID` is set (global sync can
  take up to an hour). Re-deploy after setting it.
- **"I couldn't assign your role"** → the bot's role is below `Verifying`/`Verified`,
  or it's missing **Manage Roles**. Move its role up.
- **Gate/captcha channels not configured** → the bot needs **Manage Channels** and
  its role must be high enough to edit those channels.
- **Captcha image doesn't appear** → check deploy logs; `Pillow`/`captcha` should
  install from `requirements.txt`.
