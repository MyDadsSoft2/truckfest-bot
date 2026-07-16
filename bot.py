import logging
import os
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()


def parse_id_set(value: str) -> set[int]:
    """Convert a comma-separated list of Discord IDs into a set of integers."""
    ids: set[int] = set()

    for item in value.split(","):
        item = item.strip()
        if not item:
            continue

        try:
            ids.add(int(item))
        except ValueError as exc:
            raise RuntimeError(f"Invalid Discord ID in configuration: {item!r}") from exc

    return ids


def parse_optional_id(value: str) -> Optional[int]:
    value = value.strip()
    if not value:
        return None

    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid Discord ID in configuration: {value!r}") from exc


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

CONTESTANT_ROLE_ID = int(
    os.getenv("CONTESTANT_ROLE_ID", "1527449582907162784")
)

ELIGIBLE_ROLE_IDS = parse_id_set(
    os.getenv(
        "ELIGIBLE_ROLE_IDS",
        ",".join(
            [
                "1527449733558173706",
                "1527449738125775050",
                "1527449743289094254",
                "1527449747017826444",
                "1527449751568650310",
                "1527449756518060092",
                "1527449760229883995",
                "1527449774150651924",
                "1527449779301253183",
                "1527449790445649941",
            ]
        ),
    )
)

ALLOWED_CHANNEL_IDS = parse_id_set(os.getenv("ALLOWED_CHANNEL_IDS", ""))
LOG_CHANNEL_ID = parse_optional_id(os.getenv("LOG_CHANNEL_ID", ""))

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("truckfest-bot")


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)


def attachment_is_image(attachment: discord.Attachment) -> bool:
    """Accept images using MIME type, with a filename-extension fallback."""
    if attachment.content_type:
        return attachment.content_type.lower().startswith("image/")

    return Path(attachment.filename).suffix.lower() in IMAGE_EXTENSIONS


def configured_channel_id(channel: discord.abc.GuildChannel | discord.Thread) -> int:
    """Treat a thread or forum post as belonging to its parent channel."""
    if isinstance(channel, discord.Thread) and channel.parent_id is not None:
        return channel.parent_id

    return channel.id


async def send_log(guild: discord.Guild, text: str) -> None:
    if LOG_CHANNEL_ID is None:
        return

    channel = guild.get_channel(LOG_CHANNEL_ID)
    if not isinstance(channel, discord.abc.Messageable):
        logger.warning("LOG_CHANNEL_ID does not point to a messageable channel.")
        return

    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        logger.exception("Could not send a message to the configured log channel.")


async def safely_react(message: discord.Message, emoji: str) -> None:
    try:
        await message.add_reaction(emoji)
    except (discord.Forbidden, discord.HTTPException):
        # Role assignment should still work if the bot cannot add reactions.
        pass


@bot.event
async def on_ready() -> None:
    assert bot.user is not None
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id)

    if not ALLOWED_CHANNEL_IDS:
        logger.warning(
            "No ALLOWED_CHANNEL_IDS are configured. "
            "Add the competition channel IDs to your .env file."
        )


@bot.event
async def on_message(message: discord.Message) -> None:
    # Ignore DMs, bots, and webhook messages.
    if message.guild is None or message.author.bot or message.webhook_id is not None:
        return

    channel_id = configured_channel_id(message.channel)
    if channel_id not in ALLOWED_CHANNEL_IDS:
        await bot.process_commands(message)
        return

    if not isinstance(message.author, discord.Member):
        await bot.process_commands(message)
        return

    image_attachments = [
        attachment
        for attachment in message.attachments
        if attachment_is_image(attachment)
    ]

    if not image_attachments:
        await bot.process_commands(message)
        return

    contestant_role = message.guild.get_role(CONTESTANT_ROLE_ID)
    if contestant_role is None:
        logger.error(
            "Contestant role %s was not found in guild %s.",
            CONTESTANT_ROLE_ID,
            message.guild.id,
        )
        await send_log(
            message.guild,
            f"⚠️ Contestant role `{CONTESTANT_ROLE_ID}` could not be found.",
        )
        await bot.process_commands(message)
        return

    # Do not repeatedly add a role the member already has.
    if contestant_role in message.author.roles:
        await safely_react(message, "✅")
        await bot.process_commands(message)
        return

    try:
        await message.author.add_roles(
            contestant_role,
            reason=(
                f"Uploaded {len(image_attachments)} image(s) in "
                f"competition channel {channel_id}; message {message.id}"
            ),
        )
    except discord.Forbidden:
        logger.exception(
            "Missing permission or role hierarchy prevents assigning the role."
        )
        await safely_react(message, "⚠️")
        await send_log(
            message.guild,
            (
                "⚠️ I could not give the Contestant role to "
                f"{message.author.mention}. Check **Manage Roles** and make sure "
                "the bot's role is above the Contestant role."
            ),
        )
    except discord.HTTPException:
        logger.exception("Discord rejected the role-assignment request.")
        await safely_react(message, "⚠️")
        await send_log(
            message.guild,
            f"⚠️ Discord returned an error while assigning a role to {message.author.mention}.",
        )
    else:
        logger.info(
            "Assigned Contestant role to %s (%s).",
            message.author,
            message.author.id,
        )
        await safely_react(message, "✅")
        await send_log(
            message.guild,
            (
                f"✅ Gave **{contestant_role.name}** to {message.author.mention} "
                f"after an image upload in <#{channel_id}>. "
                f"[Open upload]({message.jump_url})"
            ),
        )

    await bot.process_commands(message)


@bot.command(name="botstatus")
@commands.has_permissions(manage_guild=True)
async def bot_status(ctx: commands.Context) -> None:
    """Moderator-only configuration check."""
    contestant_role = ctx.guild.get_role(CONTESTANT_ROLE_ID) if ctx.guild else None

    await ctx.reply(
        "\n".join(
            [
                "✅ Truckfest Scotland bot is online.",
                f"Contestant role found: **{'Yes' if contestant_role else 'No'}**",
                f"Eligible uploader roles configured: **{len(ELIGIBLE_ROLE_IDS)}**",
                f"Competition channels configured: **{len(ALLOWED_CHANNEL_IDS)}**",
            ]
        ),
        mention_author=False,
    )


@bot_status.error
async def bot_status_error(
    ctx: commands.Context,
    error: commands.CommandError,
) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply(
            "You need the **Manage Server** permission to use this command.",
            mention_author=False,
        )
        return

    raise error


def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN is missing. Copy .env.example to .env and add the token."
        )

    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
