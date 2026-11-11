# Memecoin Server Verification Bot

A two-gate Discord verification bot:

1. New members see **only** the gate channel. It has an **✅ I'm human** button.
2. Clicking it grants a `Verifying` role, which unlocks the captcha channel.
3. In the captcha channel they click **🔐 Start captcha**, read an image of
   random characters, and type them in.
4. On success they get the `Verified` role and lose `Verifying` — the two gate
   channels disappear and the real server appears.

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

- **`/setup`** — creates the `Verifying` and `Verified` roles, sets the two gate
  channels' permissions, and posts both panels.
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
