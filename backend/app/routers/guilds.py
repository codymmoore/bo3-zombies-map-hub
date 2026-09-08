from fastapi import APIRouter
from sqlalchemy import select

from app import schemas
from app.deps import DbDep, MembershipDep, UserDep
from app.errors import DiscordUnavailable, bad_request, forbidden, not_found
from app.models import GuildConfig

router = APIRouter(prefix="/guilds", tags=["guilds"])


def _require_admin(membership, guild_id: str, user_id: str) -> None:
    if not membership.is_admin(guild_id, user_id):
        raise forbidden("Server admin required", code="admin_required")


@router.get("", response_model=list[schemas.GuildOut])
def list_guilds(db: DbDep, membership: MembershipDep, user: UserDep) -> list[schemas.GuildOut]:
    """Configured guilds the caller is a member of. Resolved live per guild;
    fine at this scale (see CLAUDE.md)."""
    out: list[schemas.GuildOut] = []
    for config in db.scalars(select(GuildConfig).order_by(GuildConfig.created_at)).all():
        try:
            if not membership.is_member(config.guild_id, user.discord_id):
                continue
            guild = membership.get_guild(config.guild_id)
            is_admin = membership.is_admin(config.guild_id, user.discord_id)
        except DiscordUnavailable:
            continue  # reads degrade gracefully; the guild just doesn't show up
        out.append(
            schemas.GuildOut(
                guild_id=config.guild_id,
                name=guild.name if guild else config.guild_id,
                icon_url=guild.icon_url if guild else None,
                notification_channel_id=config.notification_channel_id,
                is_admin=is_admin,
            )
        )
    return out


@router.get("/{guild_id}/channels", response_model=list[schemas.ChannelOut])
def list_channels(guild_id: str, membership: MembershipDep, user: UserDep) -> list[schemas.ChannelOut]:
    """Text channels the bot can see, for the setup page's channel picker. Admin only."""
    _require_admin(membership, guild_id, user.discord_id)
    return [schemas.ChannelOut(channel_id=c.channel_id, name=c.name) for c in membership.get_text_channels(guild_id)]


@router.put("/{guild_id}/config", response_model=schemas.GuildConfigOut)
def put_config(
    guild_id: str, body: schemas.GuildConfigUpdate, db: DbDep, membership: MembershipDep, user: UserDep
) -> schemas.GuildConfigOut:
    _require_admin(membership, guild_id, user.discord_id)
    if membership.get_guild(guild_id) is None:
        raise not_found("Guild (is the bot in this server?)")
    channel_ids = {c.channel_id for c in membership.get_text_channels(guild_id)}
    if body.notification_channel_id not in channel_ids:
        raise bad_request("invalid_channel", "That channel is not a text channel in this server, or the bot can't see it")

    config = db.get(GuildConfig, guild_id)
    if config is None:
        config = GuildConfig(guild_id=guild_id, notification_channel_id=body.notification_channel_id)
        db.add(config)
    else:
        config.notification_channel_id = body.notification_channel_id
    db.commit()
    db.refresh(config)
    return schemas.GuildConfigOut.model_validate(config)
