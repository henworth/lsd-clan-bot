import asyncio
import backoff
import logging
import pickle
import pydest

from peewee import DoesNotExist, fn, IntegrityError
from seraphsix import constants
from seraphsix.cogs.utils.helpers import bungie_date_as_utc
from seraphsix.database import ClanGame as ClanGameDb, ClanMember, Game, GameMember, Guild, Member
from seraphsix.errors import MaintenanceError
from seraphsix.models.destiny import ClanGame
from ratelimit import limits, RateLimitException

logging.getLogger(__name__)


def parse_platform(member_db, platform_id):
    if platform_id == constants.PLATFORM_BUNGIE:
        member_id = member_db.bungie_id
        member_username = member_db.bungie_username
    elif platform_id == constants.PLATFORM_PSN:
        member_id = member_db.psn_id
        member_username = member_db.psn_username
    elif platform_id == constants.PLATFORM_XBOX:
        member_id = member_db.xbox_id
        member_username = member_db.xbox_username
    elif platform_id == constants.PLATFORM_BLIZZARD:
        member_id = member_db.blizzard_id
        member_username = member_db.blizzard_username
    elif platform_id == constants.PLATFORM_STEAM:
        member_id = member_db.steam_id
        member_username = member_db.steam_username
    elif platform_id == constants.PLATFORM_STADIA:
        member_id = member_db.stadia_id
        member_username = member_db.stadia_username
    return member_id, member_username


@backoff.on_exception(
    backoff.expo,
    (pydest.pydest.PydestPrivateHistoryException, pydest.pydest.PydestMaintenanceException),
    max_tries=1, logger=None)
@backoff.on_exception(backoff.expo, pydest.pydest.PydestException, max_time=10)
@backoff.on_exception(backoff.expo, asyncio.TimeoutError, max_tries=1)
@backoff.on_exception(backoff.expo, RateLimitException, max_time=10, logger=None)
@limits(calls=25, period=1)
async def execute_pydest(function, redis):
    is_maintenance = await redis.get('global-bungie-maintenance')
    if is_maintenance and eval(is_maintenance):
        await function
        raise MaintenanceError
    try:
        return await asyncio.create_task(function)
    except pydest.pydest.PydestMaintenanceException as e:
        await redis.set('global-bungie-maintenance', str(True), expire=constants.TIME_MIN_SECONDS)
        logging.error(e)
        raise MaintenanceError


async def get_data(redis, key, function):
    redis_data = await redis.get(key)
    if redis_data:
        data = pickle.loads(redis_data)
    else:
        data = await execute_pydest(function, redis)
    return data


async def set_data(redis, key, data, expire=constants.TIME_HOUR_SECONDS):
    try:
        await redis.set(key, pickle.dumps(data), expire=expire)
    except Exception as e:
        logging.exception("Error setting data in redis")
        return


async def get_activity_history(destiny, redis, platform_id, member_id, char_id, mode_id, count):
    redis_key = f'{member_id}-activity-history'
    redis_data = await redis.get(redis_key)
    if redis_data:
        activities = pickle.loads(redis_data)
    else:
        function = destiny.api.get_activity_history(platform_id, member_id, char_id, mode=mode_id, count=count)
        data = await execute_pydest(function, redis)
        try:
            activities = data['Response']['activities']
        except KeyError:
            return None
        await set_data(redis, redis_key, activities, expire=constants.TIME_MIN_SECONDS * 45)
    return activities


async def get_pgcr(destiny, redis, activity_id):
    redis_key = f'{activity_id}-pcgr'
    redis_data = await redis.get(redis_key)
    if redis_data:
        pgcr = pickle.loads(redis_data)
    else:
        function = destiny.api.get_post_game_carnage_report(activity_id)
        data = await execute_pydest(function, redis)
        pgcr = data['Response']
        await set_data(redis, redis_key, pgcr)
    return pgcr


async def get_characters(destiny, redis, member_id, platform_id):
    redis_key = f'{member_id}-profile'
    redis_data = await redis.get(redis_key)
    if redis_data:
        characters = pickle.loads(redis_data)
    else:
        function = destiny.api.get_profile(platform_id, member_id, [constants.COMPONENT_CHARACTERS])
        data = await execute_pydest(function, redis)
        characters = data['Response']['characters']['data']
        await set_data(redis, redis_key, characters)
    return characters


async def decode_activity(destiny, redis, reference_id):
    await execute_pydest(destiny.update_manifest())
    function = destiny.decode_hash(reference_id, 'DestinyActivityDefinition')
    return await execute_pydest(function, redis)


async def get_activity_list(destiny, redis, platform_id, member_id, char_ids, mode_id, count=30):
    all_activity_ids = []
    for char_id in char_ids:
        function = get_activity_history(destiny, redis, platform_id, member_id, char_id, mode_id, count)
        try:
            activities = await function
        except RuntimeError:
            try:
                activities = await function
            except Exception:
                continue

        if not activities:
            continue

        all_activity_ids.extend([
            activity['activityDetails']['instanceId']
            for activity in activities
        ])
    return all_activity_ids


async def get_last_active(destiny, redis, member_db):
    platform_id = member_db.clanmember.platform_id
    member_id, _ = parse_platform(member_db, platform_id)

    acct_last_active = None
    try:
        characters = await get_characters(destiny, redis, member_id, platform_id)
        characters = characters.items()
    except AttributeError:
        logging.error(f"Could not get character data for {platform_id}-{member_id}")
        return acct_last_active

    for _, character in characters:
        char_last_active = bungie_date_as_utc(character['dateLastPlayed'])
        if not acct_last_active or char_last_active > acct_last_active:
            acct_last_active = char_last_active
            logging.debug(f"Found last active date for {platform_id}-{member_id}: {acct_last_active}")
    return acct_last_active


async def store_last_active(database, destiny, redis, member_db):
    last_active = await get_last_active(destiny, redis, member_db)
    member_db.clanmember.last_active = last_active
    await database.update(member_db.clanmember)


async def get_game_counts(database, game_mode, member_db=None):
    counts = {}
    base_query = Game.select()
    for mode_id in constants.SUPPORTED_GAME_MODES.get(game_mode):
        if member_db:
            query = base_query.join(GameMember).join(Member).join(ClanMember).where(
                (Member.id == member_db.id) &
                (ClanMember.clan_id == member_db.clanmember.clan_id) &
                (Game.mode_id << [mode_id])
            )
        else:
            query = base_query.where(Game.mode_id << [mode_id])
        try:
            count = await database.count(query.distinct())
        except DoesNotExist:
            continue
        else:
            counts[constants.MODE_MAP[mode_id]['title']] = count
    return counts


async def get_sherpa_time_played(database, member_db):
    clan_sherpas = Member.select(Member.id).join(ClanMember).where((ClanMember.is_sherpa) & (Member.id != member_db.id))

    full_list = list(constants.SUPPORTED_GAME_MODES.values())
    mode_list = list(set([mode for sublist in full_list for mode in sublist]))

    games = Game.select().join(GameMember).where(
        (GameMember.member_id == member_db.id) & (Game.mode_id << mode_list)
    )

    game_sherpas = Game.select(Game.id.distinct()).join(GameMember).join(Member).join(ClanMember).where(
        (Game.id << games) & (Member.id << clan_sherpas)
    )

    game_members = Member.select(Member.id.distinct()).join(GameMember).join(Game).where(
        (Game.id << games) & (Member.id << clan_sherpas)
    )

    try:
        await database.execute(game_sherpas)
    except DoesNotExist:
        return None

    query = GameMember.select(fn.SUM(GameMember.time_played).alias('sum')).where(
        (GameMember.member_id == member_db.id) & (GameMember.game_id << game_sherpas)
    )
    time_played = await database.execute(query)

    game_members_db = await database.execute(game_members)
    sherpa_members_db = await database.execute(clan_sherpas)

    game_member_set = set([member.id for member in game_members_db])
    clan_sherpa_set = set([sherpa.id for sherpa in sherpa_members_db])

    game_sherpas_unique = list(game_member_set.intersection(clan_sherpa_set))

    return (time_played[0].sum, game_sherpas_unique)


async def store_member_history(member_dbs, bot, member_db, game_mode):  # noqa TODO
    platform_id = member_db.clanmember.platform_id

    member_id, member_username = parse_platform(member_db, platform_id)

    try:
        characters = await get_characters(bot.destiny, bot.redis, member_id, platform_id)
        char_ids = characters.keys()
    except (KeyError, TypeError):
        logging.error(f"Could not get character data for {member_db.clanmember.platform_id}-{member_id}")
        return

    all_activity_ids = []
    for game_mode_id in constants.SUPPORTED_GAME_MODES.get(game_mode):
        activity_ids = await get_activity_list(
            bot.destiny, bot.redis, platform_id, member_id, char_ids, game_mode_id
        )
        if activity_ids:
            all_activity_ids.extend(activity_ids)

    mode_count = 0
    for activity_id in all_activity_ids:
        try:
            pgcr = await get_pgcr(bot.destiny, bot.redis, activity_id)
        except RuntimeError:
            try:
                pgcr = await get_pgcr(bot.destiny, bot.redis, activity_id)
            except Exception:
                continue

        if not pgcr:
            logging.error(f"{member_username}: {pgcr}")
            continue

        game = ClanGame(pgcr, member_dbs)

        game_mode_details = constants.MODE_MAP[game.mode_id]

        # Check if player count is below the threshold, or if the game
        # occurred before Forsaken released (ie. Season 4), or if the game
        # occurred before a configured cutoff date or if the member joined
        # before game time. If any of those apply, the game is not eligible.
        if (len(game.clan_players) < game_mode_details['threshold'] or
                game.date < constants.FORSAKEN_RELEASE or
                game.date < bot.config.activity_cutoff or
                game.date < member_db.clanmember.join_date):
            continue

        game_title = game_mode_details['title'].title()

        try:
            game_db = await bot.database.get(Game, instance_id=game.instance_id)
        except DoesNotExist:
            game_db = await bot.database.create(Game, **vars(game))
            logging.info(f"{game_title} game id {activity_id} created")
            mode_count += 1

        try:
            await bot.database.get(ClanGameDb, clan=member_db.clanmember.clan_id, game=game_db.id)
        except DoesNotExist:
            await bot.database.create(ClanGameDb, clan=member_db.clanmember.clan_id, game=game_db.id)
            for player in game.clan_players:
                try:
                    player_db = await bot.database.get_clan_member_by_platform(
                        player.membership_id, player.membership_type, member_db.clanmember.clan_id)
                except DoesNotExist:
                    logging.info((player.membership_id, player.membership_type, member_db.clanmember.clan_id))
                    raise
                try:
                    # Create the game members
                    await bot.database.create(
                        GameMember, member=player_db.id, game=game_db.id,
                        completed=player.completed, time_played=player.time_played)
                except IntegrityError:
                    # If one already exists, increment the time played and set the completion flag
                    game_member_db = await bot.database.get(GameMember, game=game_db.id, member=player_db.id)
                    game_member_db.time_played += player.time_played
                    if not game_member_db.completed or game_member_db.completed != player.completed:
                        game_member_db.completed = player.completed
                    await bot.database.update(game_member_db)
                    continue

    if mode_count:
        logging.debug(
            f"Found {mode_count} {game_mode} games for {member_username}")
        return mode_count


async def store_all_games(bot, game_mode, guild_id):
    guild_db = await bot.database.get(Guild, guild_id=guild_id)

    try:
        clan_dbs = await bot.database.get_clans_by_guild(guild_id)
    except DoesNotExist:
        return

    logging.info(
        f"Finding all {game_mode} games for members of server {guild_id} active in the last hour")

    tasks = []
    member_dbs = []
    for clan_db in clan_dbs:
        if not clan_db.activity_tracking:
            logging.info(f"Clan activity tracking disabled for Clan {clan_db.name}, skipping")
            continue

        active_members = await bot.database.get_clan_members_active(clan_db.id, days=7)
        if guild_db.aggregate_clans:
            member_dbs.extend(active_members)
        else:
            member_dbs = active_members

        tasks.extend([
            store_member_history(member_dbs, bot, member_db, game_mode)
            for member_db in member_dbs
        ])

    results = await asyncio.gather(*tasks)

    logging.info(
        f"Found {sum(filter(None, results))} {game_mode} games for members "
        f"of server {guild_id} active in the last hour"
    )
