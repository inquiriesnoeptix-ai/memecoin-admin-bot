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
# Each option is (button label, emoji, role). The role is a role ID (number,
# recommended: survives renames) or an exact role name (text).
# Max 24 options per question.
#
# "multi": False -> member picks exactly ONE, then moves on automatically.
# "multi": True  -> member toggles as many as they like, then taps Done.
#                   "none_option" is the "no thanks" role: tapping it (or
#                   tapping Done with nothing picked) gives that role and
#                   moves them on.
# ==========================================================================
QUESTIONS = [
    {
        "title": "🌍 Where are you based?",
        "multi": False,
        "options": [
            ("US", "🇺🇸", 1570451704766275595),
            ("EU", "🇪🇺", 1570451708830810192),
            ("GB / UK", "🇬🇧", 1570451711980740638),
            ("Asia", "🌏", 1570451715604615380),
            ("AU", "🇦🇺", 1570451718578245766),
            ("Other", "🌍", 1570451721799340183),
        ],
    },
    {
        "title": "📈 What kind of trader are you?",
        "multi": False,
        "options": [
            ("Newbie Trader (1-10 SOL)", "🐣", 1570451725658235022),
            ("Novice Trader (10-20 SOL)", "📊", 1570451728535527544),
            ("Experienced Trader (20-100 SOL)", "🧠", 1570452389499244574),
            ("Pro Trader / KOL (100+ SOL)", "🐋", 1570453252598669463),
        ],
    },
    {
        "title": "🔔 What do you want to be pinged for?",
        "multi": True,
        "options": [
            ("Calls", "📞", 1570453310966599811),
            ("Announcements", "📢", 1570453315156709508),
            ("Live Trading", "🔴", 1570453318617010236),
            ("Giveaways (coming soon)", "🎁", 1570453322023047258),
            ("Scans", "📊", 1570453771505631323),
        ],
        "none_option": ("No pings", "🔕", 1570453773640400947),
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


def resolve_role(guild: discord.Guild, ref):
    """A QUESTIONS role reference: int = role ID, str = exact name, None = unset."""
    if ref is None:
        return None
    if isinstance(ref, int):
        return guild.get_role(ref)
    return role_by_name(guild, ref)


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
    how = "Pick as many as you want, then tap **Done**." if q.get("multi") else "Pick one."
    return discord.Embed(
        title=q["title"],
        description=f"{how} Question {qi + 1} of {len(QUESTIONS)}.",
        color=BRAND_COLOR,
    )


def build_question_view(qi: int, member: discord.Member) -> discord.ui.View:
    if QUESTIONS[qi].get("multi"):
        return MultiQuestionView(qi, {r.id for r in member.roles})
    return QuestionView(qi)


async def send_step(interaction: discord.Interaction, step: int):
    """Send question `step`, or the 'proceed to verify' popup after the last one.
    Uses a followup, so the interaction must already be responded to/deferred."""
    if step < len(QUESTIONS):
        await interaction.followup.send(
            embed=question_embed(step), view=build_question_view(step, interaction.user), ephemeral=True
        )
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
    chosen = resolve_role(guild, options[oi][2])
    if chosen is None:
        await interaction.response.send_message(
            "That option isn't set up yet. Pick another one, or let a mod know.", ephemeral=True
        )
        return

    await interaction.response.defer()
    # Only one answer per question: drop any other role from this question.
    others = []
    for _label, _emoji, ref in options:
        role = resolve_role(guild, ref)
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


def _none_role(guild: discord.Guild, qi: int):
    none_opt = QUESTIONS[qi].get("none_option")
    return resolve_role(guild, none_opt[2]) if none_opt else None


async def handle_toggle(interaction: discord.Interaction, qi: int, oi: int):
    """Multi-pick question: add/remove one role and redraw the buttons."""
    guild = interaction.guild
    member = interaction.user
    role = resolve_role(guild, QUESTIONS[qi]["options"][oi][2])
    if role is None:
        await interaction.response.send_message(
            "That option isn't set up yet. Let a mod know.", ephemeral=True
        )
        return

    await interaction.response.defer()
    selected = {r.id for r in member.roles}
    none_role = _none_role(guild, qi)
    try:
        if role.id in selected:
            await member.remove_roles(role, reason="Onboarding toggle off")
            selected.discard(role.id)
        else:
            await member.add_roles(role, reason="Onboarding toggle on")
            selected.add(role.id)
            if none_role and none_role.id in selected:  # picked something -> not "no pings"
                await member.remove_roles(none_role, reason="Picked a ping role")
                selected.discard(none_role.id)
    except discord.Forbidden:
        await interaction.followup.send(
            "I couldn't change that role. An admin needs to move my role above the ping roles.",
            ephemeral=True,
        )
        return
    await interaction.edit_original_response(view=MultiQuestionView(qi, selected))


async def handle_none(interaction: discord.Interaction, qi: int):
    """'No pings': clear this question's roles, give the none role, move on."""
    guild = interaction.guild
    member = interaction.user
    await interaction.response.defer()
    picked = [
        role for role in (resolve_role(guild, ref) for _l, _e, ref in QUESTIONS[qi]["options"])
        if role and role in member.roles
    ]
    none_role = _none_role(guild, qi)
    try:
        if picked:
            await member.remove_roles(*picked, reason="Chose no pings")
        if none_role and none_role not in member.roles:
            await member.add_roles(none_role, reason="Chose no pings")
    except discord.Forbidden:
        await interaction.followup.send(
            "I couldn't change your roles. An admin needs to move my role above the ping roles.",
            ephemeral=True,
        )
        return
    await delete_source(interaction)
    await send_step(interaction, qi + 1)


async def handle_done(interaction: discord.Interaction, qi: int):
    """Done on a multi-pick question. Nothing picked counts as 'no pings'."""
    # Read what's picked from the buttons themselves (green = picked); that's
    # always in sync with what the member saw, even if role caches lag.
    prefix = f"onb:{qi}:"
    any_picked = False
    for row in (interaction.message.components if interaction.message else []):
        for child in getattr(row, "children", []):
            cid = getattr(child, "custom_id", "") or ""
            if cid.startswith(prefix) and cid[len(prefix):].isdigit() and child.style == discord.ButtonStyle.success:
                any_picked = True
    if not any_picked:
        await handle_none(interaction, qi)
        return
    await interaction.response.defer()
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
            await interaction.response.send_message(
                embed=question_embed(0), view=build_question_view(0, interaction.user), ephemeral=True
            )
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


class MultiQuestionView(discord.ui.View):
    """Toggle buttons (green = picked), plus 'none' and Done."""

    def __init__(self, qi: int, selected_ids: set[int] = frozenset()):
        super().__init__(timeout=None)
        q = QUESTIONS[qi]
        for oi, (label, emoji, ref) in enumerate(q["options"]):
            picked = isinstance(ref, int) and ref in selected_ids
            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.success if picked else discord.ButtonStyle.secondary,
                custom_id=f"onb:{qi}:{oi}",
            )
            button.callback = self._wrap(handle_toggle, qi, oi)
            self.add_item(button)

        if q.get("none_option"):
            label, emoji, _ref = q["none_option"]
            button = discord.ui.Button(
                label=label, emoji=emoji, style=discord.ButtonStyle.danger, custom_id=f"onb:{qi}:none", row=4
            )
            button.callback = self._wrap(handle_none, qi)
            self.add_item(button)

        done = discord.ui.Button(
            label="Done", emoji="\N{BLACK RIGHTWARDS ARROW}", style=discord.ButtonStyle.primary,
            custom_id=f"onb:{qi}:done", row=4,
        )
        done.callback = self._wrap(handle_done, qi)
        self.add_item(done)

    @staticmethod
    def _wrap(handler, *args):
        async def callback(interaction: discord.Interaction):
            await handler(interaction, *args)
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

    # Check every onboarding role exists (they're your roles; the bot doesn't create them).
    missing = []
    for q in QUESTIONS:
        entries = list(q["options"]) + ([q["none_option"]] if q.get("none_option") else [])
        for label, _emoji, ref in entries:
            if resolve_role(guild, ref) is None:
                missing.append(f"{label} ({ref if ref is not None else 'no ID set'})")

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

    roles_line = (
        "\N{WARNING SIGN}\N{VARIATION SELECTOR-16} These onboarding roles weren't found: "
        + ", ".join(missing) + ". Those buttons won't work until the role IDs in bot.py are fixed."
        if missing else "All onboarding roles found."
    )
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
    for qi, q in enumerate(QUESTIONS):
        bot.add_view(MultiQuestionView(qi) if q.get("multi") else QuestionView(qi))
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
        _limit = 20 if _q.get("multi") else 25  # multi needs a row for None/Done
        assert len(_q["options"]) <= _limit, f"Too many options in {_q['title']!r} (max {_limit})"
    bot.run(TOKEN)
