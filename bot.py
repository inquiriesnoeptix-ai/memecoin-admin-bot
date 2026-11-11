"""
Discord two-gate verification bot.

Flow:
  1. New member sees only the GATE channel. It has an "I'm human" button.
  2. Clicking it grants the VERIFYING role -> unlocks the CAPTCHA channel.
  3. In the captcha channel they solve an image captcha.
  4. On success they get the VERIFIED role and lose VERIFYING, so both gate
     channels disappear and the rest of the server becomes visible.

The bot manages ROLES. Discord channel permissions (tied to those roles)
control visibility. /setup configures the two gate channels and posts the
panels. /lockdown (optional) hides every other channel from @everyone and
shows it to Verified.
"""

import os
import time
import random
import logging

import discord
from discord import app_commands
from discord.ext import commands

try:
    from captcha.image import ImageCaptcha
except ImportError:  # captcha lib missing
    ImageCaptcha = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("verifybot")

# --------------------------------------------------------------------------
# Config (environment variables)
# --------------------------------------------------------------------------
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None
GATE_CHANNEL_ID = int(os.getenv("GATE_CHANNEL_ID", "1570053267621417060"))
CAPTCHA_CHANNEL_ID = int(os.getenv("CAPTCHA_CHANNEL_ID", "1569526459176067072"))
VERIFYING_ROLE_NAME = os.getenv("VERIFYING_ROLE_NAME", "Verifying")
VERIFIED_ROLE_NAME = os.getenv("VERIFIED_ROLE_NAME", "Verified")
CAPTCHA_TTL = int(os.getenv("CAPTCHA_TTL", "300"))  # seconds a code stays valid

# Characters used in the captcha (ambiguous ones like 0/O/1/I removed).
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 5

# In-memory store of pending codes: user_id -> (code, expires_at).
# Lost on restart, which is fine — the user just clicks "Start captcha" again.
pending: dict[int, tuple[str, float]] = {}

intents = discord.Intents.default()
intents.members = True  # needed to assign roles / read member roles

bot = commands.Bot(command_prefix="!noop ", intents=intents, help_command=None)


def gen_code() -> str:
    return "".join(random.choices(CODE_CHARS, k=CODE_LEN))


def role_by_name(guild: discord.Guild, name: str):
    return discord.utils.get(guild.roles, name=name)


# --------------------------------------------------------------------------
# Persistent views (buttons survive bot restarts via static custom_ids)
# --------------------------------------------------------------------------
class GateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="I'm human — Verify",
        style=discord.ButtonStyle.success,
        emoji="\N{WHITE HEAVY CHECK MARK}",
        custom_id="verify_gate",
    )
    async def verify_gate(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user
        verifying = role_by_name(guild, VERIFYING_ROLE_NAME)
        verified = role_by_name(guild, VERIFIED_ROLE_NAME)

        if verified and verified in member.roles:
            await interaction.response.send_message("You're already verified. \U0001F389", ephemeral=True)
            return
        if not verifying:
            await interaction.response.send_message(
                "Setup isn't finished yet — an admin needs to run **/setup**.", ephemeral=True
            )
            return
        try:
            await member.add_roles(verifying, reason="Passed gate button")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I couldn't assign your role. An admin needs to move my role above "
                "`Verifying`/`Verified` and give me **Manage Roles**.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Step 1 complete! Now head to <#{CAPTCHA_CHANNEL_ID}> and solve the captcha to finish.",
            ephemeral=True,
        )


class EnterCodeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Enter code",
        style=discord.ButtonStyle.primary,
        emoji="\N{KEYBOARD}",
        custom_id="captcha_enter",
    )
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = pending.get(interaction.user.id)
        if not data or data[1] < time.time():
            await interaction.response.send_message(
                "Your captcha expired. Click **Start captcha** again.", ephemeral=True
            )
            return
        await interaction.response.send_modal(CaptchaModal())


class CaptchaStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Start captcha",
        style=discord.ButtonStyle.success,
        emoji="\N{CLOSED LOCK WITH KEY}",
        custom_id="verify_captcha_start",
    )
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user
        verified = role_by_name(guild, VERIFIED_ROLE_NAME)

        if verified and verified in member.roles:
            await interaction.response.send_message("You're already verified. \U0001F389", ephemeral=True)
            return
        if ImageCaptcha is None:
            await interaction.response.send_message(
                "Captcha engine isn't installed on the host. Check the deploy logs.", ephemeral=True
            )
            return

        code = gen_code()
        pending[member.id] = (code, time.time() + CAPTCHA_TTL)

        image = ImageCaptcha(width=300, height=100)
        buf = image.generate(code)  # BytesIO, already seeked to 0
        file = discord.File(buf, filename="captcha.png")

        await interaction.response.send_message(
            content="Type the characters shown in the image below (not case-sensitive). "
            "It expires in 5 minutes.",
            file=file,
            view=EnterCodeView(),
            ephemeral=True,
        )


class CaptchaModal(discord.ui.Modal, title="Captcha verification"):
    answer = discord.ui.TextInput(
        label="Enter the code from the image",
        placeholder="e.g. ABC23",
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        data = pending.get(member.id)

        if not data or data[1] < time.time():
            pending.pop(member.id, None)
            await interaction.response.send_message(
                "Captcha expired. Click **Start captcha** again.", ephemeral=True
            )
            return

        code = data[0]
        if str(self.answer.value).strip().upper() != code.upper():
            pending.pop(member.id, None)
            await interaction.response.send_message(
                "\N{CROSS MARK} Incorrect. Click **Start captcha** to get a fresh code.", ephemeral=True
            )
            return

        pending.pop(member.id, None)
        verifying = role_by_name(guild, VERIFYING_ROLE_NAME)
        verified = role_by_name(guild, VERIFIED_ROLE_NAME)
        try:
            if verified:
                await member.add_roles(verified, reason="Passed captcha")
            if verifying and verifying in member.roles:
                await member.remove_roles(verifying, reason="Verification complete")
        except discord.Forbidden:
            await interaction.response.send_message(
                "You passed, but I couldn't update your roles. An admin needs to raise my role "
                "and give me **Manage Roles**.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "\N{PARTY POPPER} Verified! Welcome to the server.", ephemeral=True
        )


# --------------------------------------------------------------------------
# Admin commands
# --------------------------------------------------------------------------
async def _purge_bot_messages(channel: discord.abc.Messageable):
    """Best-effort cleanup of the bot's own old panel messages so /setup
    doesn't stack duplicates. Silently ignores missing permissions."""
    try:
        await channel.purge(limit=20, check=lambda m: m.author.id == bot.user.id)
    except Exception:
        pass


@bot.tree.command(name="setup", description="Create roles, configure the gate channels, and post the panels.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild

    verifying = role_by_name(guild, VERIFYING_ROLE_NAME) or await guild.create_role(
        name=VERIFYING_ROLE_NAME, reason="verification bot setup"
    )
    verified = role_by_name(guild, VERIFIED_ROLE_NAME) or await guild.create_role(
        name=VERIFIED_ROLE_NAME, reason="verification bot setup"
    )

    gate = guild.get_channel(GATE_CHANNEL_ID)
    captcha = guild.get_channel(CAPTCHA_CHANNEL_ID)
    if gate is None or captcha is None:
        await interaction.followup.send(
            f"Couldn't find the channels. Check GATE_CHANNEL_ID ({GATE_CHANNEL_ID}) and "
            f"CAPTCHA_CHANNEL_ID ({CAPTCHA_CHANNEL_ID}).",
            ephemeral=True,
        )
        return

    everyone = guild.default_role
    try:
        # Gate channel: everyone can see + read but not chat; hidden once Verified.
        await gate.set_permissions(everyone, view_channel=True, read_message_history=True, send_messages=False)
        await gate.set_permissions(verified, view_channel=False)
        # Captcha channel: hidden from everyone, shown to Verifying, hidden once Verified.
        await captcha.set_permissions(everyone, view_channel=False)
        await captcha.set_permissions(verifying, view_channel=True, read_message_history=True, send_messages=False)
        await captcha.set_permissions(verified, view_channel=False)
    except discord.Forbidden:
        await interaction.followup.send(
            "I need **Manage Channels** permission (and my role above the channels) to configure visibility.",
            ephemeral=True,
        )
        return

    gate_embed = discord.Embed(
        title="Welcome \N{WAVING HAND SIGN}  Verify to enter",
        description="Click the button below to confirm you're human.\n"
        f"That unlocks the next step in <#{CAPTCHA_CHANNEL_ID}>.",
        color=discord.Color.green(),
    )
    captcha_embed = discord.Embed(
        title="Step 2 — Solve the captcha",
        description="Click **Start captcha**, read the characters in the image, then enter them.\n"
        "Once you pass, you get full access to the server.",
        color=discord.Color.blurple(),
    )

    await _purge_bot_messages(gate)
    await _purge_bot_messages(captcha)
    await gate.send(embed=gate_embed, view=GateView())
    await captcha.send(embed=captcha_embed, view=CaptchaStartView())

    await interaction.followup.send(
        "**Setup complete.**\n"
        f"- Roles ready: `{VERIFYING_ROLE_NAME}`, `{VERIFIED_ROLE_NAME}`\n"
        f"- Gate panel posted in <#{GATE_CHANNEL_ID}>\n"
        f"- Captcha panel posted in <#{CAPTCHA_CHANNEL_ID}>\n\n"
        "Next: make sure the rest of your server is hidden from `@everyone` and visible to "
        f"`{VERIFIED_ROLE_NAME}`. You can do that per-category by hand, or run **/lockdown** to do it automatically.",
        ephemeral=True,
    )


class ConfirmLockdownView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.value = False

    @discord.ui.button(label="Yes, lock it down", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who ran the command can confirm.", ephemeral=True)
            return
        self.value = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who ran the command can confirm.", ephemeral=True)
            return
        self.value = False
        self.stop()
        await interaction.response.defer()


@bot.tree.command(
    name="lockdown",
    description="Hide every other channel from @everyone and show them to Verified.",
)
@app_commands.checks.has_permissions(administrator=True)
async def lockdown_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    verified = role_by_name(guild, VERIFIED_ROLE_NAME)
    if verified is None:
        await interaction.response.send_message("Run **/setup** first to create the roles.", ephemeral=True)
        return

    view = ConfirmLockdownView(interaction.user.id)
    await interaction.response.send_message(
        "This will set **every channel except the two gate channels** so that `@everyone` can't see them "
        f"and `{VERIFIED_ROLE_NAME}` can. Admins are unaffected. Continue?",
        view=view,
        ephemeral=True,
    )
    await view.wait()
    if not view.value:
        await interaction.edit_original_response(content="Cancelled. Nothing changed.", view=None)
        return

    everyone = guild.default_role
    changed, failed = 0, 0
    skip_ids = {GATE_CHANNEL_ID, CAPTCHA_CHANNEL_ID}
    for channel in guild.channels:
        if channel.id in skip_ids:
            continue
        try:
            await channel.set_permissions(everyone, view_channel=False)
            await channel.set_permissions(verified, view_channel=True)
            changed += 1
        except Exception:
            failed += 1

    await interaction.edit_original_response(
        content=f"Lockdown done. Updated **{changed}** channels"
        + (f", **{failed}** failed (check my permissions / role position)." if failed else "."),
        view=None,
    )


@setup_cmd.error
@lockdown_cmd.error
async def admin_cmd_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "You need **Administrator** to run this."
    else:
        log.exception("Command error", exc_info=error)
        msg = "Something went wrong running that command."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
@bot.event
async def setup_hook():
    # Register persistent views so buttons work after a restart.
    bot.add_view(GateView())
    bot.add_view(CaptchaStartView())
    bot.add_view(EnterCodeView())

    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        log.info("Synced commands to guild %s", GUILD_ID)
    else:
        await bot.tree.sync()
        log.info("Synced commands globally (may take up to 1 hour to appear)")


@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN environment variable is not set.")
    bot.run(TOKEN)
