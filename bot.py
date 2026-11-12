"""
Discord onboarding + verification bot.

Flow (everything after step 1 happens in private popups only the member sees,
and each popup deletes itself before the next one appears):
  1. New member sees only the GATE channel and taps "I've read & understood".
  2. The bot asks each question in QUESTIONS (country, experience, ...).
     Tapping an answer gives that role, deletes the question, shows the next.
  3. "You're done" -> tap "Proceed to verify" -> that popup deletes itself.
  4. An image captcha pops up. Pass it -> VERIFIED role -> the gate channel
     disappears and the rest of the server opens up.

Admin commands:
  /setup      create the roles, lock the gate channel, post the gate panel
  /lockdown   hide every other channel from @everyone, show it to Verified
  /postrules  post the rules embed
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

# ==========================================================================
# ONBOARDING QUESTIONS — edit these freely.
# Each option is (button label, emoji, role name). The bot creates any role
# that doesn't exist yet when you run /setup. Max 25 options per question.
# Members can pick only ONE option per question.
# ==========================================================================
QUESTIONS = [
    {
        "title": "🌍 Where are you based?",
        "options": [
            ("USA", "🇺🇸", "USA"),
            ("Canada", "🇨🇦", "Canada"),
            ("UK", "🇬🇧", "UK"),
            ("Europe", "🇪🇺", "Europe"),
            ("Australia / NZ", "🇦🇺", "Australia/NZ"),
            ("Asia", "🌏", "Asia"),
            ("South America", "🌎", "South America"),
            ("Africa / Middle East", "🌍", "Africa/Middle East"),
        ],
    },
    {
        "title": "📈 How experienced are you with memecoins?",
        "options": [
            ("New to memecoins", "🐣", "Beginner"),
            ("A few months in", "📈", "Intermediate"),
            ("Experienced", "🧠", "Experienced"),
            ("Full-time trader", "🐋", "Full-time Trader"),
        ],
    },
]

# --------------------------------------------------------------------------
# Config (environment variables)
# --------------------------------------------------------------------------
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None
GATE_CHANNEL_ID = int(os.getenv("GATE_CHANNEL_ID", "1570053267621417060"))
# Old captcha channel. No longer used by the flow; kept so /lockdown skips it.
CAPTCHA_CHANNEL_ID = int(os.getenv("CAPTCHA_CHANNEL_ID", "1569526459176067072"))
RULES_CHANNEL_ID = int(os.getenv("RULES_CHANNEL_ID", "1570027184855457792"))
VERIFYING_ROLE_NAME = os.getenv("VERIFYING_ROLE_NAME", "Verifying")
VERIFIED_ROLE_NAME = os.getenv("VERIFIED_ROLE_NAME", "Verified")
# Optional: use existing roles by ID (takes priority over name lookup).
VERIFYING_ROLE_ID = int(os.getenv("VERIFYING_ROLE_ID", "0")) or None
VERIFIED_ROLE_ID = int(os.getenv("VERIFIED_ROLE_ID", "0")) or None
CAPTCHA_TTL = int(os.getenv("CAPTCHA_TTL", "300"))  # seconds a code stays valid

# Characters used in the captcha (ambiguous ones like 0/O/1/I removed).
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 5

# Purple sidebar on the embeds.
BRAND_COLOR = discord.Color(0x9146FF)

# In-memory store of pending codes: user_id -> (code, expires_at).
# Lost on restart, which is fine — the member just gets a fresh code.
pending: dict[int, tuple[str, float]] = {}

intents = discord.Intents.default()
intents.members = True  # needed to assign roles / read member roles

bot = commands.Bot(command_prefix="!noop ", intents=intents, help_command=None)


def gen_code() -> str:
    return "".join(random.choices(CODE_CHARS, k=CODE_LEN))


def role_by_name(guild: discord.Guild, name: str):
    return discord.utils.get(guild.roles, name=name)


def get_verifying_role(guild: discord.Guild):
    """Prefer the configured role ID; fall back to lookup by name."""
    if VERIFYING_ROLE_ID:
        role = guild.get_role(VERIFYING_ROLE_ID)
        if role:
            return role
    return discord.utils.get(guild.roles, name=VERIFYING_ROLE_NAME)


def get_verified_role(guild: discord.Guild):
    if VERIFIED_ROLE_ID:
        role = guild.get_role(VERIFIED_ROLE_ID)
        if role:
            return role
    return discord.utils.get(guild.roles, name=VERIFIED_ROLE_NAME)


# --------------------------------------------------------------------------
# Rules embed (/postrules)
# --------------------------------------------------------------------------
def build_rules_embed() -> discord.Embed:
    embed = discord.Embed(
        title="\N{SCROLL} Community Rules",
        description="Welcome to the trenches. Follow these to keep the server safe and worth being in.",
        color=BRAND_COLOR,
    )
    rules = [
        ("\N{NO ONE UNDER EIGHTEEN SYMBOL} 16+ only", "You must be 16 or older to be here."),
        ("\N{NO ENTRY SIGN} No scams or drainers",
         "Scams, phishing, wallet drainers, fake giveaways, impersonation or malicious links = instant permanent ban."),
        ("\N{CLOSED LOCK WITH KEY} Protect your wallet",
         "Never share your seed phrase or private keys. Staff will **never** DM you first or ask for them."),
        ("\N{DESKTOP COMPUTER} Never screenshare “Dryflip”",
         "It's a drainer. Don't download, open or bookmark anything someone sends you."),
        ("\N{SPEECH BALLOON} No unsolicited DMs",
         "Don't DM members with calls, investments, paid groups or services unless they asked first."),
        ("\N{LEFT-POINTING MAGNIFYING GLASS} DYOR",
         "Nothing here is financial advice. Do your own research and trade at your own risk."),
        ("\N{CHART WITH UPWARDS TREND} No manipulation",
         "No coordinated pump-and-dumps, wash trading, fake volume or intentionally misleading calls."),
        ("\N{HANDSHAKE} Respect everyone",
         "No harassment, hate speech, discrimination, threats or doxxing."),
        ("\N{PUBLIC ADDRESS LOUDSPEAKER} No spam or shilling",
         "No message/emoji/mention spam. Promote projects only in the right channels, with permission."),
        ("\N{SHIELD} Staff decisions are final",
         "Disagree with a decision? Open a ticket instead of arguing in public."),
    ]
    for name, value in rules:
        embed.add_field(name=name, value=value, inline=False)
    embed.add_field(
        name="\N{WARNING SIGN}\N{VARIATION SELECTOR-16} Disclaimer",
        value="Memecoins are extremely volatile \N{EM DASH} you can lose some or all of your money. "
        "You alone are responsible for your trades and your funds.",
        inline=False,
    )
    embed.set_footer(text="DYOR • Don't FOMO • Clear comms • Stay safe \U0001FAE1")
    return embed


# --------------------------------------------------------------------------
# Onboarding flow helpers
# --------------------------------------------------------------------------
async def delete_source(interaction: discord.Interaction):
    """Delete the popup the member just clicked on. Call after response.defer().
    Falls back to blanking it if Discord refuses the delete."""
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        try:
            await interaction.edit_original_response(
                content="\N{WHITE HEAVY CHECK MARK}", embed=None, view=None, attachments=[]
            )
        except discord.HTTPException:
            pass


def question_embed(qi: int) -> discord.Embed:
    q = QUESTIONS[qi]
    return discord.Embed(
        title=q["title"],
        description=f"Pick one. Question {qi + 1} of {len(QUESTIONS)}.",
        color=BRAND_COLOR,
    )


async def send_step(interaction: discord.Interaction, step: int):
    """Send question `step`, or the 'proceed to verify' popup after the last one.
    Uses a followup, so the interaction must already be responded to/deferred."""
    if step < len(QUESTIONS):
        await interaction.followup.send(embed=question_embed(step), view=QuestionView(step), ephemeral=True)
    else:
        embed = discord.Embed(
            title="\N{WHITE HEAVY CHECK MARK} You're all set",
            description="Last step: tap **Proceed to verify** and solve a quick captcha to unlock the server.",
            color=BRAND_COLOR,
        )
        await interaction.followup.send(embed=embed, view=ProceedView(), ephemeral=True)


async def send_captcha(interaction: discord.Interaction, note: str = ""):
    """Generate a fresh captcha for this member and send it as a popup."""
    if ImageCaptcha is None:
        await interaction.followup.send("Captcha engine isn't installed on the host. Tell an admin.", ephemeral=True)
        return
    code = gen_code()
    pending[interaction.user.id] = (code, time.time() + CAPTCHA_TTL)
    buf = ImageCaptcha(width=300, height=100).generate(code)  # BytesIO, seeked to 0
    await interaction.followup.send(
        content=f"{note}\N{CLOSED LOCK WITH KEY} Type the characters in the image (not case-sensitive), "
        "then tap **Enter code**. It expires in 5 minutes.",
        file=discord.File(buf, filename="captcha.png"),
        view=EnterCodeView(),
        ephemeral=True,
    )


async def handle_answer(interaction: discord.Interaction, qi: int, oi: int):
    guild = interaction.guild
    member = interaction.user
    options = QUESTIONS[qi]["options"]
    chosen = role_by_name(guild, options[oi][2])
    if chosen is None:
        await interaction.response.send_message(
            "That role doesn't exist yet. An admin needs to run **/setup**.", ephemeral=True
        )
        return

    await interaction.response.defer()
    # Only one answer per question: drop any other role from this question.
    others = []
    for _label, _emoji, role_name in options:
        role = role_by_name(guild, role_name)
        if role and role != chosen and role in member.roles:
            others.append(role)
    try:
        if others:
            await member.remove_roles(*others, reason="Onboarding answer changed")
        if chosen not in member.roles:
            await member.add_roles(chosen, reason="Onboarding answer")
    except discord.Forbidden:
        await interaction.followup.send(
            "I couldn't give you that role. An admin needs to move my role above the onboarding roles.",
            ephemeral=True,
        )
        return

    await delete_source(interaction)
    await send_step(interaction, qi + 1)


# --------------------------------------------------------------------------
# Views. Static custom_ids make the buttons keep working after a restart.
# --------------------------------------------------------------------------
class GateView(discord.ui.View):
    """Public panel in the gate channel. Never deleted by clicks."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="I've read & understood",
        style=discord.ButtonStyle.success,
        emoji="\N{WHITE HEAVY CHECK MARK}",
        custom_id="verify_gate",
    )
    async def verify_gate(self, interaction: discord.Interaction, button: discord.ui.Button):
        verified = get_verified_role(interaction.guild)
        if verified and verified in interaction.user.roles:
            await interaction.response.send_message("You're already verified. \U0001F389", ephemeral=True)
            return
        if QUESTIONS:
            await interaction.response.send_message(embed=question_embed(0), view=QuestionView(0), ephemeral=True)
        else:
            await interaction.response.defer(ephemeral=True)
            await send_step(interaction, 0)


class QuestionView(discord.ui.View):
    def __init__(self, qi: int):
        super().__init__(timeout=None)
        for oi, (label, emoji, _role_name) in enumerate(QUESTIONS[qi]["options"]):
            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=f"onb:{qi}:{oi}",
            )
            button.callback = self._callback(qi, oi)
            self.add_item(button)

    @staticmethod
    def _callback(qi: int, oi: int):
        async def callback(interaction: discord.Interaction):
            await handle_answer(interaction, qi, oi)
        return callback


class ProceedView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Proceed to verify",
        style=discord.ButtonStyle.success,
        emoji="\N{WHITE HEAVY CHECK MARK}",
        custom_id="onb:proceed",
    )
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await delete_source(interaction)
        await send_captcha(interaction)


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
        await interaction.response.send_modal(CaptchaModal())


class CaptchaModal(discord.ui.Modal, title="Captcha verification"):
    answer = discord.ui.TextInput(
        label="Enter the code from the image",
        placeholder="ABC23",
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        data = pending.get(member.id)

        await interaction.response.defer()
        await delete_source(interaction)  # remove the old captcha popup either way

        if not data or data[1] < time.time():
            pending.pop(member.id, None)
            await send_captcha(interaction, note="\N{HOURGLASS} That code expired, here's a new one.\n")
            return
        if str(self.answer.value).strip().upper() != data[0].upper():
            pending.pop(member.id, None)
            await send_captcha(interaction, note="\N{CROSS MARK} Wrong code, try this one.\n")
            return

        pending.pop(member.id, None)
        verified = get_verified_role(guild)
        if verified is None:
            await interaction.followup.send("Setup isn't finished. An admin needs to run **/setup**.", ephemeral=True)
            return

        # Say welcome before the role lands: the gate channel (and this popup)
        # disappears for them as soon as they're Verified.
        await interaction.followup.send("\N{PARTY POPPER} Verified! Opening the server\N{HORIZONTAL ELLIPSIS}", ephemeral=True)
        try:
            await member.add_roles(verified, reason="Passed captcha")
            verifying = get_verifying_role(guild)
            if verifying and verifying in member.roles:  # leftover from the old flow
                await member.remove_roles(verifying, reason="Verification complete")
        except discord.Forbidden:
            await interaction.followup.send(
                "You passed, but I couldn't give you the Verified role. Tell an admin to raise my role.",
                ephemeral=True,
            )


# --------------------------------------------------------------------------
# Admin commands
# --------------------------------------------------------------------------
async def _purge_bot_messages(channel: discord.abc.Messageable):
    """Best-effort cleanup of the bot's own old panel messages so re-running a
    command doesn't stack duplicates. Silently ignores missing permissions."""
    try:
        await channel.purge(limit=20, check=lambda m: m.author.id == bot.user.id)
    except Exception:
        pass


@bot.tree.command(name="setup", description="Create the roles, lock the gate channel, and post the gate panel.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild

    verified = get_verified_role(guild) or await guild.create_role(
        name=VERIFIED_ROLE_NAME, reason="verification bot setup"
    )

    created = []
    for q in QUESTIONS:
        for _label, _emoji, role_name in q["options"]:
            if role_by_name(guild, role_name) is None:
                await guild.create_role(name=role_name, reason="onboarding role")
                created.append(role_name)

    gate = guild.get_channel(GATE_CHANNEL_ID)
    if gate is None:
        await interaction.followup.send(
            f"Couldn't find the gate channel. Check GATE_CHANNEL_ID ({GATE_CHANNEL_ID}).", ephemeral=True
        )
        return

    try:
        # Everyone can see + read but not chat; hidden once Verified.
        await gate.set_permissions(
            guild.default_role, view_channel=True, read_message_history=True, send_messages=False
        )
        await gate.set_permissions(verified, view_channel=False)
    except discord.Forbidden:
        await interaction.followup.send(
            "I need **Manage Channels** permission (and my role above the channel) to configure it.",
            ephemeral=True,
        )
        return

    gate_embed = discord.Embed(
        title="\N{WHITE HEAVY CHECK MARK} Confirm you've read the rules",
        description="Read the rules above, then tap **\N{WHITE HEAVY CHECK MARK} I've read & understood** to get started.",
        color=discord.Color.green(),
    )
    await _purge_bot_messages(gate)
    await gate.send(embed=gate_embed, view=GateView())

    roles_line = f"Created roles: {', '.join(created)}" if created else "All onboarding roles already existed."
    await interaction.followup.send(
        "**Setup complete.**\n"
        f"- Gate panel posted in <#{GATE_CHANNEL_ID}>\n"
        f"- {roles_line}\n\n"
        "Make sure my role sits **above** the onboarding roles and "
        f"`{VERIFIED_ROLE_NAME}` in Server Settings → Roles.\n"
        f"The old captcha channel <#{CAPTCHA_CHANNEL_ID}> isn't used anymore; you can delete it.",
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
    verified = get_verified_role(guild)
    if verified is None:
        await interaction.response.send_message("Run **/setup** first to create the roles.", ephemeral=True)
        return

    view = ConfirmLockdownView(interaction.user.id)
    await interaction.response.send_message(
        "This will set **every channel except the gate channel** so that `@everyone` can't see them "
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


@bot.tree.command(name="postrules", description="Post (or refresh) the rules embed in the rules channel.")
@app_commands.describe(channel="Where to post the rules (defaults to the configured rules channel).")
@app_commands.checks.has_permissions(administrator=True)
async def postrules_cmd(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    target = channel or guild.get_channel(RULES_CHANNEL_ID)
    if target is None:
        await interaction.followup.send(
            f"Couldn't find the rules channel ({RULES_CHANNEL_ID}). Set RULES_CHANNEL_ID or pass a channel.",
            ephemeral=True,
        )
        return
    try:
        await _purge_bot_messages(target)
        await target.send(embed=build_rules_embed())
    except discord.Forbidden:
        await interaction.followup.send(
            f"I need permission to send messages in {target.mention}.", ephemeral=True
        )
        return
    await interaction.followup.send(f"Rules posted in {target.mention}. \N{WHITE HEAVY CHECK MARK}", ephemeral=True)


@setup_cmd.error
@lockdown_cmd.error
@postrules_cmd.error
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
    # Register persistent views so buttons keep working after a restart.
    bot.add_view(GateView())
    for qi in range(len(QUESTIONS)):
        bot.add_view(QuestionView(qi))
    bot.add_view(ProceedView())
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
    for _q in QUESTIONS:
        assert len(_q["options"]) <= 25, "Discord allows at most 25 buttons per question"
    bot.run(TOKEN)
