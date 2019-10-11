# Copyright (c) 2011-2019 Eric Froemling
"""Implements football games (both co-op and teams varieties)."""

# bs_meta require api 6
# (see bombsquadgame.com/apichanges)

from __future__ import annotations

import random
from typing import TYPE_CHECKING

import ba
from bastd.actor import bomb as stdbomb
from bastd.actor import flag as stdflag
from bastd.actor import playerspaz
from bastd.actor import spazbot
from bastd.actor.scoreboard import Scoreboard

if TYPE_CHECKING:
    from typing import (Any, List, Tuple, Type, Dict, Sequence, Optional,
                        Union)
    from bastd.actor.spaz import Spaz


class FootballFlag(stdflag.Flag):
    """Custom flag class for football games."""

    def __init__(self, position: Sequence[float]):
        super().__init__(position=position,
                         dropped_timeout=20,
                         color=(1.0, 1.0, 0.3))
        assert self.node
        self.last_holding_player: Optional[ba.Player] = None
        self.node.is_area_of_interest = True
        self.respawn_timer: Optional[ba.Timer] = None
        self.scored = False
        self.held_count = 0
        self.light = ba.newnode('light',
                                owner=self.node,
                                attrs={
                                    'intensity': 0.25,
                                    'height_attenuated': False,
                                    'radius': 0.2,
                                    'color': (0.9, 0.7, 0.0)
                                })
        self.node.connectattr('position', self.light, 'position')


# bs_meta export game
class FootballTeamGame(ba.TeamGameActivity):
    """Football game for teams mode."""

    @classmethod
    def get_name(cls) -> str:
        return 'Football'

    @classmethod
    def supports_session_type(cls, sessiontype: Type[ba.Session]) -> bool:
        # We only support two-team play.
        return issubclass(sessiontype, ba.TeamsSession)

    @classmethod
    def get_description(cls, sessiontype: Type[ba.Session]) -> str:
        return 'Get the flag to the enemy end zone.'

    @classmethod
    def get_supported_maps(cls, sessiontype: Type[ba.Session]) -> List[str]:
        return ba.getmaps('football')

    @classmethod
    def get_settings(cls, sessiontype: Type[ba.Session]
                     ) -> List[Tuple[str, Dict[str, Any]]]:
        return [
            ("Score to Win", {
                'min_value': 7,
                'default': 21,
                'increment': 7
            }),
            ("Time Limit", {
                'choices': [('None', 0), ('1 Minute', 60), ('2 Minutes', 120),
                            ('5 Minutes', 300), ('10 Minutes', 600),
                            ('20 Minutes', 1200)],
                'default': 0
            }),
            ("Respawn Times", {
                'choices': [('Shorter', 0.25), ('Short', 0.5), ('Normal', 1.0),
                            ('Long', 2.0), ('Longer', 4.0)],
                'default': 1.0
            })
        ]  # yapf: disable

    def __init__(self, settings: Dict[str, Any]):
        super().__init__(settings)
        self._scoreboard: Optional[Scoreboard] = Scoreboard()

        # Load some media we need.
        self._cheer_sound = ba.getsound("cheer")
        self._chant_sound = ba.getsound("crowdChant")
        self._score_sound = ba.getsound("score")
        self._swipsound = ba.getsound("swip")
        self._whistle_sound = ba.getsound("refWhistle")

        self.score_region_material = ba.Material()
        self.score_region_material.add_actions(
            conditions=("they_have_material",
                        stdflag.get_factory().flagmaterial),
            actions=(("modify_part_collision", "collide",
                      True), ("modify_part_collision", "physical", False),
                     ("call", "at_connect", self._handle_score)))
        self._flag_spawn_pos: Optional[Sequence[float]] = None
        self._score_regions: List[ba.Actor] = []
        self._flag: Optional[FootballFlag] = None
        self._flag_respawn_timer: Optional[ba.Timer] = None
        self._flag_respawn_light: Optional[ba.Actor] = None

    def get_instance_description(self) -> Union[str, Sequence]:
        touchdowns = self.settings['Score to Win'] / 7
        if touchdowns > 1:
            return 'Score ${ARG1} touchdowns.', touchdowns
        return 'Score a touchdown.'

    def get_instance_scoreboard_description(self) -> Union[str, Sequence]:
        touchdowns = self.settings['Score to Win'] / 7
        if touchdowns > 1:
            return 'score ${ARG1} touchdowns', touchdowns
        return 'score a touchdown'

    # noinspection PyMethodOverriding
    def on_transition_in(self) -> None:  # type: ignore
        # FIXME: Unify these args.
        # pylint: disable=arguments-differ
        ba.TeamGameActivity.on_transition_in(self, music='Football')

    def on_begin(self) -> None:
        ba.TeamGameActivity.on_begin(self)
        self.setup_standard_time_limit(self.settings['Time Limit'])
        self.setup_standard_powerup_drops()
        self._flag_spawn_pos = (self.map.get_flag_position(None))
        self._spawn_flag()
        defs = self.map.defs
        self._score_regions.append(
            ba.Actor(
                ba.newnode('region',
                           attrs={
                               'position': defs.boxes['goal1'][0:3],
                               'scale': defs.boxes['goal1'][6:9],
                               'type': 'box',
                               'materials': (self.score_region_material, )
                           })))
        self._score_regions.append(
            ba.Actor(
                ba.newnode('region',
                           attrs={
                               'position': defs.boxes['goal2'][0:3],
                               'scale': defs.boxes['goal2'][6:9],
                               'type': 'box',
                               'materials': (self.score_region_material, )
                           })))
        self._update_scoreboard()
        ba.playsound(self._chant_sound)

    def on_team_join(self, team: ba.Team) -> None:
        team.gamedata['score'] = 0
        self._update_scoreboard()

    def _kill_flag(self) -> None:
        self._flag = None

    def _handle_score(self) -> None:
        """A point has been scored."""

        # Our flag might stick around for a second or two
        # make sure it doesn't score again.
        assert self._flag is not None
        if self._flag.scored:
            return
        region = ba.get_collision_info("source_node")
        i = None
        for i in range(len(self._score_regions)):
            if region == self._score_regions[i].node:
                break
        for team in self.teams:
            if team.get_id() == i:
                team.gamedata['score'] += 7

                # Tell all players to celebrate.
                for player in team.players:
                    if player.actor is not None and player.actor.node:
                        try:
                            # Note: celebrate message is milliseconds
                            # (for historical reasons).
                            player.actor.node.handlemessage('celebrate', 2000)
                        except Exception:
                            ba.print_exception('Error on celebrate')

                # If someone on this team was last to touch it,
                # give them points.
                assert self._flag is not None
                if (self._flag.last_holding_player
                        and team == self._flag.last_holding_player.team):
                    self.stats.player_scored(self._flag.last_holding_player,
                                             50,
                                             big_message=True)
                # end game if we won
                if team.gamedata['score'] >= self.settings['Score to Win']:
                    self.end_game()
        ba.playsound(self._score_sound)
        ba.playsound(self._cheer_sound)
        assert self._flag
        self._flag.scored = True

        # Kill the flag (it'll respawn shortly).
        ba.timer(1.0, self._kill_flag)
        light = ba.newnode('light',
                           attrs={
                               'position': ba.get_collision_info('position'),
                               'height_attenuated': False,
                               'color': (1, 0, 0)
                           })
        ba.animate(light, 'intensity', {0.0: 0, 0.5: 1, 1.0: 0}, loop=True)
        ba.timer(1.0, light.delete)
        ba.cameraflash(duration=10.0)
        self._update_scoreboard()

    def end_game(self) -> None:
        results = ba.TeamGameResults()
        for team in self.teams:
            results.set_team_score(team, team.gamedata['score'])
        self.end(results=results, announce_delay=0.8)

    def _update_scoreboard(self) -> None:
        win_score = self.settings['Score to Win']
        assert self._scoreboard is not None
        for team in self.teams:
            self._scoreboard.set_team_value(team, team.gamedata['score'],
                                            win_score)

    def handlemessage(self, msg: Any) -> Any:
        if isinstance(msg, stdflag.FlagPickedUpMessage):
            assert isinstance(msg.flag, FootballFlag)
            try:
                player = msg.node.getdelegate().getplayer()
                if player:
                    msg.flag.last_holding_player = player
                msg.flag.held_count += 1
            except Exception:
                ba.print_exception("exception in Football FlagPickedUpMessage;"
                                   " this shouldn't happen")

        elif isinstance(msg, stdflag.FlagDroppedMessage):
            assert isinstance(msg.flag, FootballFlag)
            msg.flag.held_count -= 1

        # Respawn dead players if they're still in the game.
        elif isinstance(msg, playerspaz.PlayerSpazDeathMessage):
            # Augment standard behavior.
            super().handlemessage(msg)
            self.respawn_player(msg.spaz.player)

        # Respawn dead flags.
        elif isinstance(msg, stdflag.FlagDeathMessage):
            if not self.has_ended():
                self._flag_respawn_timer = ba.Timer(3.0, self._spawn_flag)
                self._flag_respawn_light = ba.Actor(
                    ba.newnode('light',
                               attrs={
                                   'position': self._flag_spawn_pos,
                                   'height_attenuated': False,
                                   'radius': 0.15,
                                   'color': (1.0, 1.0, 0.3)
                               }))
                assert self._flag_respawn_light.node
                ba.animate(self._flag_respawn_light.node,
                           "intensity", {
                               0.0: 0,
                               0.25: 0.15,
                               0.5: 0
                           },
                           loop=True)
                ba.timer(3.0, self._flag_respawn_light.node.delete)

        else:
            # Augment standard behavior.
            super().handlemessage(msg)

    def _flash_flag_spawn(self) -> None:
        light = ba.newnode('light',
                           attrs={
                               'position': self._flag_spawn_pos,
                               'height_attenuated': False,
                               'color': (1, 1, 0)
                           })
        ba.animate(light, 'intensity', {0: 0, 0.25: 0.25, 0.5: 0}, loop=True)
        ba.timer(1.0, light.delete)

    def _spawn_flag(self) -> None:
        ba.playsound(self._swipsound)
        ba.playsound(self._whistle_sound)
        self._flash_flag_spawn()
        assert self._flag_spawn_pos is not None
        self._flag = FootballFlag(position=self._flag_spawn_pos)


class FootballCoopGame(ba.CoopGameActivity):
    """
    Co-op variant of football
    """

    tips = ['Use the pick-up button to grab the flag < ${PICKUP} >']

    @classmethod
    def get_name(cls) -> str:
        return 'Football'

    @classmethod
    def get_score_info(cls) -> Dict[str, Any]:
        return {'score_type': 'milliseconds', 'score_version': 'B'}

    # FIXME: Need to update co-op games to use get_score_info.
    def get_score_type(self) -> str:
        return 'time'

    def get_instance_description(self) -> Union[str, Sequence]:
        touchdowns = self._score_to_win / 7
        if touchdowns > 1:
            return 'Score ${ARG1} touchdowns.', touchdowns
        return 'Score a touchdown.'

    def get_instance_scoreboard_description(self) -> Union[str, Sequence]:
        touchdowns = self._score_to_win / 7
        if touchdowns > 1:
            return 'score ${ARG1} touchdowns', touchdowns
        return 'score a touchdown'

    def __init__(self, settings: Dict[str, Any]):
        settings['map'] = 'Football Stadium'
        super().__init__(settings)
        self._preset = self.settings.get('preset', 'rookie')

        # Load some media we need.
        self._cheer_sound = ba.getsound("cheer")
        self._boo_sound = ba.getsound("boo")
        self._chant_sound = ba.getsound("crowdChant")
        self._score_sound = ba.getsound("score")
        self._swipsound = ba.getsound("swip")
        self._whistle_sound = ba.getsound("refWhistle")
        self._score_to_win = 21
        self._score_region_material = ba.Material()
        self._score_region_material.add_actions(
            conditions=("they_have_material",
                        stdflag.get_factory().flagmaterial),
            actions=(("modify_part_collision", "collide",
                      True), ("modify_part_collision", "physical", False),
                     ("call", "at_connect", self._handle_score)))
        self._powerup_center = (0, 2, 0)
        self._powerup_spread = (10, 5.5)
        self._player_has_dropped_bomb = False
        self._player_has_punched = False
        self._scoreboard: Optional[Scoreboard] = None
        self._flag_spawn_pos: Optional[Sequence[float]] = None
        self.score_regions: List[ba.Actor] = []
        self._exclude_powerups: List[str] = []
        self._have_tnt = False
        self._bot_types_initial: Optional[List[Type[spazbot.SpazBot]]] = None
        self._bot_types_7: Optional[List[Type[spazbot.SpazBot]]] = None
        self._bot_types_14: Optional[List[Type[spazbot.SpazBot]]] = None
        self._bot_team: Optional[ba.Team] = None
        self._starttime_ms: Optional[int] = None
        self._time_text: Optional[ba.Actor] = None
        self._time_text_input: Optional[ba.Actor] = None
        self._tntspawner: Optional[stdbomb.TNTSpawner] = None
        self._bots = spazbot.BotSet()
        self._bot_spawn_timer: Optional[ba.Timer] = None
        self._powerup_drop_timer: Optional[ba.Timer] = None
        self.scoring_team: Optional[ba.Team] = None
        self._final_time_ms: Optional[int] = None
        self._time_text_timer: Optional[ba.Timer] = None
        self._flag_respawn_light: Optional[ba.Actor] = None
        self._flag: Optional[FootballFlag] = None

    # noinspection PyMethodOverriding
    def on_transition_in(self) -> None:  # type: ignore
        # FIXME: Unify these args.
        # pylint: disable=arguments-differ
        ba.CoopGameActivity.on_transition_in(self, music='Football')
        self._scoreboard = Scoreboard()
        self._flag_spawn_pos = self.map.get_flag_position(None)
        self._spawn_flag()

        # Set up the two score regions.
        defs = self.map.defs
        self.score_regions.append(
            ba.Actor(
                ba.newnode('region',
                           attrs={
                               'position': defs.boxes['goal1'][0:3],
                               'scale': defs.boxes['goal1'][6:9],
                               'type': 'box',
                               'materials': [self._score_region_material]
                           })))
        self.score_regions.append(
            ba.Actor(
                ba.newnode('region',
                           attrs={
                               'position': defs.boxes['goal2'][0:3],
                               'scale': defs.boxes['goal2'][6:9],
                               'type': 'box',
                               'materials': [self._score_region_material]
                           })))
        ba.playsound(self._chant_sound)

    def on_begin(self) -> None:
        # FIXME: Split this up a bit.
        # pylint: disable=too-many-statements
        from bastd.actor import controlsguide
        ba.CoopGameActivity.on_begin(self)

        # Show controls help in kiosk mode.
        if ba.app.kiosk_mode:
            controlsguide.ControlsGuide(delay=3.0, lifespan=10.0,
                                        bright=True).autoretain()
        assert self.initial_player_info is not None
        abot: Type[spazbot.SpazBot]
        bbot: Type[spazbot.SpazBot]
        cbot: Type[spazbot.SpazBot]
        if self._preset in ['rookie', 'rookie_easy']:
            self._exclude_powerups = ['curse']
            self._have_tnt = False
            abot = (spazbot.BrawlerBotLite
                    if self._preset == 'rookie_easy' else spazbot.BrawlerBot)
            self._bot_types_initial = [abot] * len(self.initial_player_info)
            bbot = (spazbot.BomberBotLite
                    if self._preset == 'rookie_easy' else spazbot.BomberBot)
            self._bot_types_7 = (
                [bbot] * (1 if len(self.initial_player_info) < 3 else 2))
            cbot = (spazbot.BomberBot
                    if self._preset == 'rookie_easy' else spazbot.TriggerBot)
            self._bot_types_14 = (
                [cbot] * (1 if len(self.initial_player_info) < 3 else 2))
        elif self._preset == 'tournament':
            self._exclude_powerups = []
            self._have_tnt = True
            self._bot_types_initial = (
                [spazbot.BrawlerBot] *
                (1 if len(self.initial_player_info) < 2 else 2))
            self._bot_types_7 = (
                [spazbot.TriggerBot] *
                (1 if len(self.initial_player_info) < 3 else 2))
            self._bot_types_14 = (
                [spazbot.ChargerBot] *
                (1 if len(self.initial_player_info) < 4 else 2))
        elif self._preset in ['pro', 'pro_easy', 'tournament_pro']:
            self._exclude_powerups = ['curse']
            self._have_tnt = True
            self._bot_types_initial = [spazbot.ChargerBot] * len(
                self.initial_player_info)
            abot = (spazbot.BrawlerBot
                    if self._preset == 'pro' else spazbot.BrawlerBotLite)
            typed_bot_list: List[Type[spazbot.SpazBot]] = []
            self._bot_types_7 = (
                typed_bot_list + [abot] + [spazbot.BomberBot] *
                (1 if len(self.initial_player_info) < 3 else 2))
            bbot = (spazbot.TriggerBotPro
                    if self._preset == 'pro' else spazbot.TriggerBot)
            self._bot_types_14 = (
                [bbot] * (1 if len(self.initial_player_info) < 3 else 2))
        elif self._preset in ['uber', 'uber_easy']:
            self._exclude_powerups = []
            self._have_tnt = True
            abot = (spazbot.BrawlerBotPro
                    if self._preset == 'uber' else spazbot.BrawlerBot)
            bbot = (spazbot.TriggerBotPro
                    if self._preset == 'uber' else spazbot.TriggerBot)
            typed_bot_list_2: List[Type[spazbot.SpazBot]] = []
            self._bot_types_initial = (typed_bot_list_2 + [spazbot.StickyBot] +
                                       [abot] * len(self.initial_player_info))
            self._bot_types_7 = (
                [bbot] * (1 if len(self.initial_player_info) < 3 else 2))
            self._bot_types_14 = (
                [spazbot.ExplodeyBot] *
                (1 if len(self.initial_player_info) < 3 else 2))
        else:
            raise Exception()

        self.setup_low_life_warning_sound()

        self._drop_powerups(standard_points=True)
        ba.timer(4.0, self._start_powerup_drops)

        # Make a bogus team for our bots.
        bad_team_name = self.get_team_display_string('Bad Guys')
        self._bot_team = ba.Team(1, bad_team_name, (0.5, 0.4, 0.4))

        for team in [self.teams[0], self._bot_team]:
            team.gamedata['score'] = 0

        self.update_scores()

        # Time display.
        starttime_ms = ba.time(timeformat=ba.TimeFormat.MILLISECONDS)
        assert isinstance(starttime_ms, int)
        self._starttime_ms = starttime_ms
        self._time_text = ba.Actor(
            ba.newnode('text',
                       attrs={
                           'v_attach': 'top',
                           'h_attach': 'center',
                           'h_align': 'center',
                           'color': (1, 1, 0.5, 1),
                           'flatness': 0.5,
                           'shadow': 0.5,
                           'position': (0, -50),
                           'scale': 1.3,
                           'text': ''
                       }))
        self._time_text_input = ba.Actor(
            ba.newnode('timedisplay', attrs={'showsubseconds': True}))
        ba.sharedobj('globals').connectattr('time', self._time_text_input.node,
                                            'time2')
        assert self._time_text_input.node
        assert self._time_text.node
        self._time_text_input.node.connectattr('output', self._time_text.node,
                                               'text')

        # Our TNT spawner (if applicable).
        if self._have_tnt:
            self._tntspawner = stdbomb.TNTSpawner(position=(0, 1, -1))

        self._bots = spazbot.BotSet()
        self._bot_spawn_timer = ba.Timer(1.0, self._update_bots, repeat=True)

        for bottype in self._bot_types_initial:
            self._spawn_bot(bottype)

    def _on_got_scores_to_beat(self, scores: List[Dict[str, Any]]) -> None:
        self._show_standard_scores_to_beat_ui(scores)

    def _on_bot_spawn(self, spaz: spazbot.SpazBot) -> None:
        # We want to move to the left by default.
        spaz.target_point_default = ba.Vec3(0, 0, 0)

    def _spawn_bot(self,
                   spaz_type: Type[spazbot.SpazBot],
                   immediate: bool = False) -> None:
        assert self._bot_team is not None
        pos = self.map.get_start_position(self._bot_team.get_id())
        self._bots.spawn_bot(spaz_type,
                             pos=pos,
                             spawn_time=0.001 if immediate else 3.0,
                             on_spawn_call=self._on_bot_spawn)

    def _update_bots(self) -> None:
        bots = self._bots.get_living_bots()
        for bot in bots:
            bot.target_flag = None

        # If we're waiting on a continue, stop here so they don't keep scoring.
        if self.is_waiting_for_continue():
            self._bots.stop_moving()
            return

        # If we've got a flag and no player are holding it, find the closest
        # bot to it, and make them the designated flag-bearer.
        assert self._flag is not None
        if self._flag.node:
            for player in self.players:
                try:
                    assert player.actor is not None and player.actor.node
                    if (player.actor.is_alive() and
                            player.actor.node.hold_node == self._flag.node):
                        return
                except Exception:
                    ba.print_exception("exception checking hold node")

            flagpos = ba.Vec3(self._flag.node.position)
            closest_bot = None
            closest_dist = None
            for bot in bots:
                # If a bot is picked up, he should forget about the flag.
                if bot.held_count > 0:
                    continue
                assert bot.node
                botpos = ba.Vec3(bot.node.position)
                botdist = (botpos - flagpos).length()
                if closest_bot is None or botdist < closest_dist:
                    closest_dist = botdist
                    closest_bot = bot
            if closest_bot is not None:
                closest_bot.target_flag = self._flag

    def _drop_powerup(self, index: int, poweruptype: str = None) -> None:
        from bastd.actor import powerupbox
        if poweruptype is None:
            poweruptype = (powerupbox.get_factory().get_random_powerup_type(
                excludetypes=self._exclude_powerups))
        powerupbox.PowerupBox(position=self.map.powerup_spawn_points[index],
                              poweruptype=poweruptype).autoretain()

    def _start_powerup_drops(self) -> None:
        self._powerup_drop_timer = ba.Timer(3.0,
                                            self._drop_powerups,
                                            repeat=True)

    def _drop_powerups(self,
                       standard_points: bool = False,
                       poweruptype: str = None) -> None:
        """Generic powerup drop."""
        from bastd.actor import powerupbox
        if standard_points:
            spawnpoints = self.map.powerup_spawn_points
            for i, _point in enumerate(spawnpoints):
                ba.timer(1.0 + i * 0.5,
                         ba.Call(self._drop_powerup, i, poweruptype))
        else:
            point = (self._powerup_center[0] + random.uniform(
                -1.0 * self._powerup_spread[0], 1.0 * self._powerup_spread[0]),
                     self._powerup_center[1],
                     self._powerup_center[2] + random.uniform(
                         -self._powerup_spread[1], self._powerup_spread[1]))

            # Drop one random one somewhere.
            powerupbox.PowerupBox(
                position=point,
                poweruptype=powerupbox.get_factory().get_random_powerup_type(
                    excludetypes=self._exclude_powerups)).autoretain()

    def _kill_flag(self) -> None:
        try:
            assert self._flag is not None
            self._flag.handlemessage(ba.DieMessage())
        except Exception:
            ba.print_exception('error in _kill_flag')

    def _handle_score(self) -> None:
        """ a point has been scored """
        # FIXME tidy this up
        # pylint: disable=too-many-branches
        # pylint: disable=too-many-nested-blocks

        # Our flag might stick around for a second or two;
        # we don't want it to be able to score again.
        assert self._flag is not None
        if self._flag.scored:
            return

        # See which score region it was.
        region = ba.get_collision_info("source_node")
        i = None
        for i in range(len(self.score_regions)):
            if region == self.score_regions[i].node:
                break

        for team in [self.teams[0], self._bot_team]:
            assert team is not None
            if team.get_id() == i:
                team.gamedata['score'] += 7

                # Tell all players (or bots) to celebrate.
                if i == 0:
                    for player in team.players:
                        try:
                            # Note: celebrate message is milliseconds
                            # (for historical reasons).
                            if player.actor is not None and player.actor.node:
                                player.actor.node.handlemessage(
                                    'celebrate', 2000)
                        except Exception:
                            ba.print_exception()
                else:
                    self._bots.celebrate(2000)

        # If the good guys scored, add more enemies.
        if i == 0:
            if self.teams[0].gamedata['score'] == 7:
                assert self._bot_types_7 is not None
                for bottype in self._bot_types_7:
                    self._spawn_bot(bottype)
            elif self.teams[0].gamedata['score'] == 14:
                assert self._bot_types_14 is not None
                for bottype in self._bot_types_14:
                    self._spawn_bot(bottype)

        ba.playsound(self._score_sound)
        if i == 0:
            ba.playsound(self._cheer_sound)
        else:
            ba.playsound(self._boo_sound)

        # Kill the flag (it'll respawn shortly).
        self._flag.scored = True

        ba.timer(0.2, self._kill_flag)

        self.update_scores()
        light = ba.newnode('light',
                           attrs={
                               'position': ba.get_collision_info('position'),
                               'height_attenuated': False,
                               'color': (1, 0, 0)
                           })
        ba.animate(light, 'intensity', {0: 0, 0.5: 1, 1.0: 0}, loop=True)
        ba.timer(1.0, light.delete)
        if i == 0:
            ba.cameraflash(duration=10.0)

    def end_game(self) -> None:
        ba.setmusic(None)
        self._bots.final_celebrate()
        ba.timer(0.001, ba.Call(self.do_end, 'defeat'))

    def on_continue(self) -> None:
        # Subtract one touchdown from the bots and get them moving again.
        assert self._bot_team is not None
        self._bot_team.gamedata['score'] -= 7
        self._bots.start_moving()
        self.update_scores()

    def update_scores(self) -> None:
        """ update scoreboard and check for winners """
        # FIXME: tidy this up
        # pylint: disable=too-many-nested-blocks
        have_scoring_team = False
        win_score = self._score_to_win
        for team in [self.teams[0], self._bot_team]:
            assert team is not None
            assert self._scoreboard is not None
            self._scoreboard.set_team_value(team, team.gamedata['score'],
                                            win_score)
            if team.gamedata['score'] >= win_score:
                if not have_scoring_team:
                    self.scoring_team = team
                    if team is self._bot_team:
                        self.continue_or_end_game()
                    else:
                        ba.setmusic('Victory')

                        # Completion achievements.
                        assert self._bot_team is not None
                        if self._preset in ['rookie', 'rookie_easy']:
                            self._award_achievement('Rookie Football Victory',
                                                    sound=False)
                            if self._bot_team.gamedata['score'] == 0:
                                self._award_achievement(
                                    'Rookie Football Shutout', sound=False)
                        elif self._preset in ['pro', 'pro_easy']:
                            self._award_achievement('Pro Football Victory',
                                                    sound=False)
                            if self._bot_team.gamedata['score'] == 0:
                                self._award_achievement('Pro Football Shutout',
                                                        sound=False)
                        elif self._preset in ['uber', 'uber_easy']:
                            self._award_achievement('Uber Football Victory',
                                                    sound=False)
                            if self._bot_team.gamedata['score'] == 0:
                                self._award_achievement(
                                    'Uber Football Shutout', sound=False)
                            if (not self._player_has_dropped_bomb
                                    and not self._player_has_punched):
                                self._award_achievement('Got the Moves',
                                                        sound=False)
                        self._bots.stop_moving()
                        self.show_zoom_message(ba.Lstr(resource='victoryText'),
                                               scale=1.0,
                                               duration=4.0)
                        self.celebrate(10.0)
                        assert self._starttime_ms is not None
                        self._final_time_ms = int(
                            ba.time(timeformat=ba.TimeFormat.MILLISECONDS) -
                            self._starttime_ms)
                        self._time_text_timer = None
                        assert (self._time_text_input is not None
                                and self._time_text_input.node)
                        self._time_text_input.node.timemax = (
                            self._final_time_ms)

                        # FIXME: Does this still need to be deferred?
                        ba.pushcall(ba.Call(self.do_end, 'victory'))

    def do_end(self, outcome: str) -> None:
        """End the game with the specified outcome."""
        if outcome == 'defeat':
            self.fade_to_red()
        assert self._final_time_ms is not None
        scoreval = (None if outcome == 'defeat' else int(self._final_time_ms //
                                                         10))
        self.end(delay=3.0,
                 results={
                     'outcome': outcome,
                     'score': scoreval,
                     'score_order': 'decreasing',
                     'player_info': self.initial_player_info
                 })

    def handlemessage(self, msg: Any) -> Any:
        """ handle high-level game messages """
        if isinstance(msg, playerspaz.PlayerSpazDeathMessage):
            from bastd.actor import respawnicon

            # Respawn dead players.
            player = msg.spaz.player
            self.stats.player_lost_spaz(player)
            assert self.initial_player_info is not None
            respawn_time = 2.0 + len(self.initial_player_info) * 1.0

            # Respawn them shortly.
            player.gamedata['respawn_timer'] = ba.Timer(
                respawn_time, ba.Call(self.spawn_player_if_exists, player))
            player.gamedata['respawn_icon'] = respawnicon.RespawnIcon(
                player, respawn_time)

            # Augment standard behavior.
            super().handlemessage(msg)

        elif isinstance(msg, spazbot.SpazBotDeathMessage):

            # Every time a bad guy dies, spawn a new one.
            ba.timer(3.0, ba.Call(self._spawn_bot, (type(msg.badguy))))

        elif isinstance(msg, spazbot.SpazBotPunchedMessage):
            if self._preset in ['rookie', 'rookie_easy']:
                if msg.damage >= 500:
                    self._award_achievement('Super Punch')
            elif self._preset in ['pro', 'pro_easy']:
                if msg.damage >= 1000:
                    self._award_achievement('Super Mega Punch')

        # Respawn dead flags.
        elif isinstance(msg, stdflag.FlagDeathMessage):
            assert isinstance(msg.flag, FootballFlag)
            msg.flag.respawn_timer = ba.Timer(3.0, self._spawn_flag)
            self._flag_respawn_light = ba.Actor(
                ba.newnode('light',
                           attrs={
                               'position': self._flag_spawn_pos,
                               'height_attenuated': False,
                               'radius': 0.15,
                               'color': (1.0, 1.0, 0.3)
                           }))
            assert self._flag_respawn_light.node
            ba.animate(self._flag_respawn_light.node,
                       "intensity", {
                           0: 0,
                           0.25: 0.15,
                           0.5: 0
                       },
                       loop=True)
            ba.timer(3.0, self._flag_respawn_light.node.delete)
        else:
            super().handlemessage(msg)

    def _handle_player_dropped_bomb(self, player: Spaz,
                                    bomb: ba.Actor) -> None:
        del player, bomb  # Unused.
        self._player_has_dropped_bomb = True

    def _handle_player_punched(self, player: Spaz) -> None:
        del player  # Unused.
        self._player_has_punched = True

    def spawn_player(self, player: ba.Player) -> ba.Actor:
        spaz = self.spawn_player_spaz(player,
                                      position=self.map.get_start_position(
                                          player.team.get_id()))
        if self._preset in ['rookie_easy', 'pro_easy', 'uber_easy']:
            spaz.impact_scale = 0.25
        spaz.add_dropped_bomb_callback(self._handle_player_dropped_bomb)
        spaz.punch_callback = self._handle_player_punched
        return spaz

    def _flash_flag_spawn(self) -> None:
        light = ba.newnode('light',
                           attrs={
                               'position': self._flag_spawn_pos,
                               'height_attenuated': False,
                               'color': (1, 1, 0)
                           })
        ba.animate(light, 'intensity', {0: 0, 0.25: 0.25, 0.5: 0}, loop=True)
        ba.timer(1.0, light.delete)

    def _spawn_flag(self) -> None:
        ba.playsound(self._swipsound)
        ba.playsound(self._whistle_sound)
        self._flash_flag_spawn()
        assert self._flag_spawn_pos is not None
        self._flag = FootballFlag(position=self._flag_spawn_pos)