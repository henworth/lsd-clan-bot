import pydest
import backoff

from datetime import datetime, timedelta, timezone
from peewee import DoesNotExist, IntegrityError, InternalError

from members import Game

PLATFORM_XBOX = 1

COMPONENT_CHARACTERS = 200

MODE_ALLPVP = 5
MODE_GAMBIT = 63
MODE_RAID = 4

MODE_PVP_CONTROL = 10
MODE_PVP_CLASH = 12
MODE_PVP_MAYHEM = 25
MODE_PVP_SUPREMACY = 31
MODE_PVP_SURVIVAL = 37
MODE_PVP_COUNTDOWN = 38
MODE_PVP_IRONBANNER_CONTROL = 43
MODE_PVP_IRONBANNER_CLASH = 44
MODE_PVP_DOUBLES = 50
MODE_PVP_BREAKTHROUGH = 65


MODES_PVP_QUICK = [
    MODE_PVP_CONTROL, MODE_PVP_CLASH, MODE_PVP_MAYHEM,
    MODE_PVP_SUPREMACY, MODE_PVP_DOUBLES,
    MODE_PVP_IRONBANNER_CONTROL, MODE_PVP_IRONBANNER_CLASH
]

MODES_PVP_COMP = [
    MODE_PVP_SURVIVAL, MODE_PVP_COUNTDOWN, MODE_PVP_BREAKTHROUGH,
]

PLAYER_COUNT = {
    MODE_GAMBIT: 4, MODE_RAID: 6, MODE_PVP_DOUBLES: 2,
    MODE_PVP_CONTROL: 6, MODE_PVP_CLASH: 6, MODE_PVP_MAYHEM: 6,
    MODE_PVP_SUPREMACY: 6, MODE_PVP_SURVIVAL: 4, MODE_PVP_COUNTDOWN: 4,
    MODE_PVP_BREAKTHROUGH: 4, MODE_PVP_IRONBANNER_CONTROL: 6,
    MODE_PVP_IRONBANNER_CLASH: 6
}

FORSAKEN_RELEASE = datetime.strptime('2018-09-04T18:00:00Z', '%Y-%m-%dT%H:%M:%S%z')


@backoff.on_exception(backoff.expo, pydest.pydest.PydestException, max_time=60)
async def get_activity_list(destiny, member_id, char_id, mode_id, count=5):
    return await destiny.api.get_activity_history(
        PLATFORM_XBOX, member_id, char_id, mode=mode_id, count=count
    )


@backoff.on_exception(backoff.expo, pydest.pydest.PydestException, max_time=60)
async def get_activity(destiny, activity_id):
    return await destiny.api.get_post_game_carnage_report(activity_id)


@backoff.on_exception(backoff.expo, pydest.pydest.PydestException, max_time=60)
async def decode_activity(destiny, reference_id):
    await destiny.update_manifest()
    return await destiny.decode_hash(reference_id, 'DestinyActivityDefinition')


@backoff.on_exception(backoff.expo, pydest.pydest.PydestException, max_time=60)
async def get_profile(destiny, member_id):
    return await destiny.api.get_profile(PLATFORM_XBOX, member_id, [COMPONENT_CHARACTERS])


async def get_member_history(database, destiny, member_name, game_mode, check_date=True):
    
    if game_mode == 'gambit':
        game_mode_id = MODE_GAMBIT
    elif game_mode == 'raid':
        game_mode_id = MODE_RAID
    elif game_mode == 'pvp':
        game_mode_id = MODES_PVP_COMP + MODES_PVP_QUICK
    elif game_mode == 'pvp-quick':
        game_mode_id = MODES_PVP_QUICK
    elif game_mode == 'pvp-comp':
        game_mode_id = MODES_PVP_COMP

    if not isinstance(game_mode_id, list):
        game_mode_list = [game_mode_id]
    else:
        game_mode_list = game_mode_id

    members = [member.xbox_username for member in await database.get_members()]

    member_db = await database.get_member(member_name)
    member_id = member_db.bungie_id
    member_join_date = member_db.join_date

    profile = await get_profile(destiny, member_id)
    char_ids = list(profile['Response']['characters']['data'].keys())

    total_game_count = 0
    for mode_id in game_mode_list:
        try:
            game_session = await database.get_game_session(member_name, mode_id)
        except DoesNotExist:
            pass
        else:
            if game_session.last_updated > (datetime.now(timezone.utc) - timedelta(hours = 1)) and check_date:
                total_game_count += game_session.count
                continue

        player_threshold = int(PLAYER_COUNT[mode_id] / 2)
        if player_threshold < 2:
            player_threshold = 2
        mode_count = 0

        for char_id in char_ids:
            activity = await get_activity_list(
                destiny, member_id, char_id, mode_id
            )

            try:
                activities = activity['Response']['activities']
            except KeyError:
                continue

            activity_ids = [
                activity['activityDetails']['instanceId']
                for activity in activities
            ]

            for activity_id in activity_ids:
                pgcr = await get_activity(destiny, activity_id)
                game = Game(pgcr['Response'])
                
                # Loop through all players to find any members that completed
                # the game session. Also check if the member joined before
                # the game time.
                players = []
                for player in game.players:
                    if player['completed'] and player['name'] in members:
                        member_db = await database.get_member(player['name'])
                        if game.date > member_db.join_date:
                            players.append(player['name'])

                # Check if player count is below the threshold, or if the game
                # occurred after Forsaken released (ie. Season 4) or if the
                # member joined before game time. If any of those apply, the
                # game is not eligible.
                if (len(players) < player_threshold or
                        game.date < FORSAKEN_RELEASE or 
                        game.date < member_join_date):
                    continue

                game_details = {
                    'date': game.date,
                    'mode_id': game.mode_id,
                    'instance_id': activity_id,
                }

                try:
                    await database.create_game(game_details, players)
                except IntegrityError:
                    pass
                else:
                    # Loop though all players and create/update their
                    # count as needed.
                    for player in players:
                        try:
                            await database.create_game_session(
                                player,
                                {
                                    "game_mode_id": mode_id,
                                    "count": 1,
                                }
                            )
                        except (IntegrityError, InternalError):
                            await database.update_game_session(player, mode_id, 1)
                        mode_count += 1

        # Increment the total counter and update the date stamp in the database
        # This happens with a count of zero so as to track update times 
        # appropriately. If the update fails, there is no record and the
        # member has yet to play that game mode.
        total_game_count += mode_count
        try:
            await database.update_game_session(member_name, mode_id, 0)
        except DoesNotExist:
            continue
    
    return total_game_count
