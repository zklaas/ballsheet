#!/usr/bin/env python3
"""Thin, standard-library-only client for the MLB Stats API.

The MLB Stats API (https://statsapi.mlb.com) is the official feed.  It needs no
key, speaks plain JSON over HTTPS, and updates within minutes of a game going
final -- which is exactly what BallSheet wants.

Nothing in this module imports Qt, so it can be used from a script or a test.
The network calls live in ``get_*`` functions; the ``parse_*`` helpers are pure
and are what the tests exercise.

Terms of use: MLB's data is for personal, non-commercial use.  See the copyright
string returned in every payload.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import sys
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction

import formula
import retrosheet

API = "https://statsapi.mlb.com/api/v1"
API_V11 = "https://statsapi.mlb.com/api/v1.1"  # the game feed (Gameday) lives only here
SPORT_ID = 1  # 1 = Major League Baseball

def _cache_dir() -> str:
    """Cache folder: %APPDATA% / %LOCALAPPDATA% on Windows, ~/Library on macOS,
    the usual XDG folder on Linux."""
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "BallSheet", "cache")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Caches", "BallSheet")
    return os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "ballsheet")


CACHE_DIR = _cache_dir()
_UA = "BallSheet/1.0 (+https://statsapi.mlb.com; personal use)"
_TIMEOUT = 20


class MLBStatsError(RuntimeError):
    """Any failure talking to the API (network, HTTP, or malformed JSON)."""


# --------------------------------------------------------------------------- #
#  Column definitions -- (header, stat-key) pairs, in display order.          #
# --------------------------------------------------------------------------- #
HITTING_COLUMNS = [
    ("Player", "_name"), ("G", "gamesPlayed"), ("PA", "plateAppearances"),
    ("AB", "atBats"), ("R", "runs"), ("H", "hits"), ("2B", "doubles"),
    ("3B", "triples"), ("HR", "homeRuns"), ("RBI", "rbi"), ("BB", "baseOnBalls"),
    ("SO", "strikeOuts"), ("SB", "stolenBases"), ("CS", "caughtStealing"),
    ("AVG", "avg"), ("OBP", "obp"), ("SLG", "slg"), ("OPS", "ops"),
]

PITCHING_COLUMNS = [
    ("Player", "_name"), ("W", "wins"), ("L", "losses"), ("ERA", "era"),
    ("G", "gamesPlayed"), ("GS", "gamesStarted"), ("CG", "completeGames"),
    ("SHO", "shutouts"), ("SV", "saves"), ("IP", "inningsPitched"),
    ("H", "hits"), ("R", "runs"), ("ER", "earnedRuns"), ("HR", "homeRuns"),
    ("BB", "baseOnBalls"), ("SO", "strikeOuts"), ("WHIP", "whip"),
    ("K/9", "strikeoutsPer9Inn"), ("BB/9", "walksPer9Inn"),
]

# Fielding is per position: a player who saw time at two spots has two rows.
FIELDING_COLUMNS = [
    ("Player", "_name"), ("Pos", "_pos"), ("G", "gamesPlayed"),
    ("GS", "gamesStarted"), ("Inn", "innings"), ("PO", "putOuts"),
    ("A", "assists"), ("E", "errors"), ("DP", "doublePlays"),
    ("FLD%", "fielding"), ("RF/9", "rangeFactorPer9Inn"),
    ("PB", "passedBall"), ("CS", "caughtStealing"),
]

# For the "Team totals" view: which aggregate numbers to show, and their labels.
TEAM_HITTING_FIELDS = [
    ("Games", "gamesPlayed"), ("Runs", "runs"), ("Hits", "hits"),
    ("Doubles", "doubles"), ("Triples", "triples"), ("Home runs", "homeRuns"),
    ("RBI", "rbi"), ("Walks", "baseOnBalls"), ("Strikeouts", "strikeOuts"),
    ("Stolen bases", "stolenBases"), ("Batting avg", "avg"),
    ("On-base pct", "obp"), ("Slugging", "slg"), ("OPS", "ops"),
    ("Left on base", "leftOnBase"),
]
TEAM_PITCHING_FIELDS = [
    ("Wins", "wins"), ("Losses", "losses"), ("ERA", "era"),
    ("Games", "gamesPlayed"), ("Complete games", "completeGames"),
    ("Shutouts", "shutouts"), ("Saves", "saves"),
    ("Innings pitched", "inningsPitched"), ("Hits allowed", "hits"),
    ("Runs allowed", "runs"), ("Earned runs", "earnedRuns"),
    ("Home runs allowed", "homeRuns"), ("Walks", "baseOnBalls"),
    ("Strikeouts", "strikeOuts"), ("WHIP", "whip"),
]
TEAM_FIELDING_FIELDS = [
    ("Fielding pct", "fielding"), ("Errors", "errors"), ("Assists", "assists"),
    ("Putouts", "putOuts"), ("Chances", "chances"),
    ("Double plays", "doublePlays"), ("Triple plays", "triplePlays"),
    ("Passed balls", "passedBall"), ("Wild pitches", "wildPitches"),
    ("Pickoffs", "pickoffs"), ("Caught stealing", "caughtStealing"),
    ("Stolen bases allowed", "stolenBases"), ("Innings", "innings"),
]

# "Advanced" (stats=seasonAdvanced): batted-ball detail beyond the box score.
HITTING_ADVANCED_COLUMNS = [
    ("Player", "_name"), ("PA", "plateAppearances"), ("TB", "totalBases"),
    ("XBH", "extraBaseHits"), ("ISO", "iso"), ("BABIP", "babip"),
    ("HBP", "hitByPitch"), ("SF", "sacFlies"), ("SH", "sacBunts"),
    ("GIDP", "gidp"), ("P/PA", "pitchesPerPlateAppearance"),
    ("BB/K", "walksPerStrikeout"), ("Pitches", "numberOfPitches"),
    ("Swings", "totalSwings"), ("Whiffs", "swingAndMisses"),
]

PITCHING_ADVANCED_COLUMNS = [
    ("Player", "_name"), ("QS", "qualityStarts"), ("BF", "battersFaced"),
    ("K/9", "strikeoutsPer9"), ("BB/9", "baseOnBallsPer9"),
    ("HR/9", "homeRunsPer9"), ("H/9", "hitsPer9"),
    ("K/BB", "strikesoutsToWalks"), ("BABIP", "babip"),
    ("OPS-against", "ops"), ("Whiff%", "whiffPercentage"),
    ("FB%", "flyBallPercentage"), ("Balks", "balks"), ("WP", "wildPitches"),
]

# Sabermetrics (stats=sabermetrics): the licensed WAR/wOBA-family model.
HITTING_SABERMETRICS_COLUMNS = [
    ("Player", "_name"), ("wOBA", "woba"), ("wRC+", "wRcPlus"),
    ("wRAA", "wRaa"), ("WAR", "war"), ("RAR", "rar"),
    ("Batting", "batting"), ("Fielding", "fielding"),
    ("BsR", "baseRunning"), ("Spd", "spd"), ("UBR", "ubr"),
    ("wSB", "wSb"), ("wGDP", "wGdp"),
]

PITCHING_SABERMETRICS_COLUMNS = [
    ("Player", "_name"), ("WAR", "war"), ("RAR", "rar"), ("FIP", "fip"),
    ("xFIP", "xfip"), ("FIP-", "fipMinus"), ("ERA-", "eraMinus"),
    ("RA9-WAR", "ra9War"), ("SD", "sd"), ("MD", "md"), ("pLI", "pli"),
]

# Expected stats (stats=expectedStatistics): Statcast-modelled "should have".
HITTING_EXPECTED_COLUMNS = [
    ("Player", "_name"), ("xAVG", "avg"), ("xSLG", "slg"),
    ("xwOBA", "woba"), ("xwOBAcon", "wobaCon"),
]

PITCHING_EXPECTED_COLUMNS = [
    ("Player", "_name"), ("xAVG", "avg"), ("xSLG", "slg"),
    ("xwOBA", "woba"), ("xwOBAcon", "wobaCon"),
]

COLUMNS = {
    "hitting": HITTING_COLUMNS,
    "pitching": PITCHING_COLUMNS,
    "fielding": FIELDING_COLUMNS,
}

# Player-list views: display name -> (API group, API statType, columns).
# Every entry here is queried the same way ("give me this team, this season,
# this group") via get_player_stats/get_team_totals -- just with a different
# ``stats=`` value.  A few other statTypes the MLB API exposes (catching,
# running, game, team and streak *groups*; outsAboveAverage; pitch-arsenal
# metrics) were tried and dropped: they either come back empty for a
# team+season query, ignore the teamId filter entirely, or (advanced
# fielding) silently fall back to plain "season" data -- so there is nothing
# real to show for them here.
VIEW_SPECS = {
    "Batting": ("hitting", "season", HITTING_COLUMNS),
    "Pitching": ("pitching", "season", PITCHING_COLUMNS),
    "Fielding": ("fielding", "season", FIELDING_COLUMNS),
    "Batting (Advanced)": ("hitting", "seasonAdvanced", HITTING_ADVANCED_COLUMNS),
    "Pitching (Advanced)": ("pitching", "seasonAdvanced", PITCHING_ADVANCED_COLUMNS),
    "Batting (Sabermetrics)": ("hitting", "sabermetrics", HITTING_SABERMETRICS_COLUMNS),
    "Pitching (Sabermetrics)": ("pitching", "sabermetrics", PITCHING_SABERMETRICS_COLUMNS),
    "Batting (Expected)": ("hitting", "expectedStatistics", HITTING_EXPECTED_COLUMNS),
    "Pitching (Expected)": ("pitching", "expectedStatistics", PITCHING_EXPECTED_COLUMNS),
}
# Baserunning worked out from Retrosheet's play-by-play (see retrosheet.py): a view of its own, whose
# rows come from there rather than the MLB API.  Its variables are the counts custom stats such as
# Extra Bases, Tag Ups and Bases Gained are built from.
RETRO_VIEW = "Running (Retrosheet)"
RETRO_COLUMNS = [("Player", "_name")] + list(retrosheet.RUNNING_VARIABLES)
VIEW_SPECS[RETRO_VIEW] = ("retrosheet", "season", RETRO_COLUMNS)
# Two more from the Bullpen Support and Players Batted In tabs' Retrosheet summaries: each pitcher's starts,
# relief appearances, save situations, holds and saves, and whom each batter drove in from where.
RELIEF_VIEW = "Relief (Retrosheet)"
VIEW_SPECS[RELIEF_VIEW] = ("retrosheet-relief", "season", [("Player", "_name")] + list(retrosheet.RELIEF_VARIABLES))
BATTED_IN_VIEW = "Batted In (Retrosheet)"
VIEW_SPECS[BATTED_IN_VIEW] = ("retrosheet-rbi", "season", [("Player", "_name")] + list(retrosheet.BATTED_IN_VARIABLES))
RETRO_VIEWS = (RETRO_VIEW, RELIEF_VIEW, BATTED_IN_VIEW)
# Views for player-level custom stats only (the Score stats): what MLB as a whole did that season, a
# fielder's range measured as on the Range tab, and his team's games -- one row per player.
SCORE_VIEWS = {
    "MLB": ("league-figures", [("Runs Produced per Plate Appearance", "rp_pa"),
                               ("Runs per Nine Innings", "r9"),
                               ("Bases Gained per Opportunity", "bg_opp")]),
    "Range": ("range-figures", [("Chances", "chances"), ("Expected Chances", "expected"), ("Innings", "innings")]),
    "Team": ("team-figures", [("G", "team_games")]),
}
for _view, (_group, _cols) in SCORE_VIEWS.items():
    VIEW_SPECS[_view] = (_group, "season", [("Player", "_name")] + _cols)
PLAYER_VIEWS = [view for view, spec in VIEW_SPECS.items()
                if not spec[0].startswith("retrosheet") and view not in SCORE_VIEWS]  # the Stats tab's views
TEAM_FIELDS = {
    "hitting": TEAM_HITTING_FIELDS,
    "pitching": TEAM_PITCHING_FIELDS,
    "fielding": TEAM_FIELDING_FIELDS,
}

# Personal tab: one player's year-by-year lines, in four roles.  "Running" is drawn
# from the same hitting data as "Batting" -- the API has no separate running feed
# (its "running" group comes back empty) -- but shown as its own category: the
# base-running numbers (SB, CS, SB%, and Sabermetrics' BsR / UBR / wSB / wGDP /
# Spd) live there instead of in Batting.
PERSON_ROLES = [("Batting", "hitting"), ("Running", "running"),
                ("Pitching", "pitching"), ("Fielding", "fielding")]
PERSON_API_GROUP = {"hitting": "hitting", "running": "hitting",
                    "pitching": "pitching", "fielding": "fielding"}

# The stat sets each role offers (the ones the Stats tab has as views).  Standard
# and Advanced come back for every season in one request; Sabermetrics and
# Expected only one season per request, with no career line (Expected also only
# exists from the Statcast era).  Fielding has no advanced tier, and Running's
# only sets are its standard line and the Sabermetrics running metrics.
PERSON_SETS = {
    "hitting": ["Standard", "Advanced", "Sabermetrics", "Expected"],
    "running": ["Standard", "Sabermetrics"],
    "pitching": ["Standard", "Advanced", "Sabermetrics", "Expected"],
    "fielding": ["Standard"],
}
EXPECTED_FROM_SEASON = 2015

_RUNNING_STANDARD_KEYS = ("stolenBases", "caughtStealing", "stolenBasePercentage")
# In Sabermetrics: BsR (base-running runs) is UBR + wSB + wGDP; Spd is speed score.
_RUNNING_SABERMETRIC_KEYS = ("baseRunning", "ubr", "wSb", "wGdp", "spd")

_SEASON_TEAM = [("Season", "_season"), ("Team", "_team")]
_HITTING_STANDARD = [
    ("Age", "age"), ("G", "gamesPlayed"), ("PA", "plateAppearances"), ("AB", "atBats"),
    ("R", "runs"), ("H", "hits"), ("2B", "doubles"), ("3B", "triples"), ("HR", "homeRuns"),
    ("RBI", "rbi"), ("BB", "baseOnBalls"), ("SO", "strikeOuts"),
    ("AVG", "avg"), ("OBP", "obp"), ("SLG", "slg"), ("OPS", "ops"),
]
_RUNNING_STANDARD = [("Age", "age"), ("G", "gamesPlayed"), ("SB", "stolenBases"),
                     ("CS", "caughtStealing"), ("SB%", "stolenBasePercentage")]
_PITCHING_STANDARD = [
    ("Age", "age"), ("W", "wins"), ("L", "losses"), ("ERA", "era"), ("G", "gamesPlayed"),
    ("GS", "gamesStarted"), ("CG", "completeGames"), ("SHO", "shutouts"),
    ("SV", "saves"), ("IP", "inningsPitched"), ("H", "hits"), ("R", "runs"),
    ("ER", "earnedRuns"), ("HR", "homeRuns"), ("BB", "baseOnBalls"),
    ("SO", "strikeOuts"), ("WHIP", "whip"), ("K/9", "strikeoutsPer9Inn"),
    ("BB/9", "walksPer9Inn"),
]
# Fielding is one row per position per season.
_FIELDING_STANDARD = [
    ("Pos", "_pos"), ("Age", "age"), ("G", "gamesPlayed"), ("GS", "gamesStarted"),
    ("Inn", "innings"), ("PO", "putOuts"), ("A", "assists"), ("E", "errors"),
    ("DP", "doublePlays"), ("FLD%", "fielding"), ("RF/9", "rangeFactorPer9Inn"),
    ("PB", "passedBall"), ("CS", "caughtStealing"),
]


def _person_columns(view_columns: list, keep=None, drop=()) -> list:
    """A Stats-tab view's columns, re-led with Season and Team instead of Player.
    ``keep`` limits (and orders) the columns by stat key; ``drop`` removes some."""
    by_key = {key: (header, key) for header, key in view_columns if key != "_name"}
    keys = list(keep) if keep is not None else [k for _h, k in by_key.values()]
    return _SEASON_TEAM + [by_key[k] for k in keys if k in by_key and k not in drop]


PERSON_COLUMNS = {  # the Standard set of each role
    "hitting": _SEASON_TEAM + _HITTING_STANDARD,
    "running": _SEASON_TEAM + _RUNNING_STANDARD,
    "pitching": _SEASON_TEAM + _PITCHING_STANDARD,
    "fielding": _SEASON_TEAM + _FIELDING_STANDARD,
}
PERSON_SET_COLUMNS = {
    **{(role, "Standard"): cols for role, cols in PERSON_COLUMNS.items()},
    ("hitting", "Advanced"): _person_columns(HITTING_ADVANCED_COLUMNS),
    ("pitching", "Advanced"): _person_columns(PITCHING_ADVANCED_COLUMNS),
    ("hitting", "Sabermetrics"): _person_columns(HITTING_SABERMETRICS_COLUMNS,
                                                 drop=_RUNNING_SABERMETRIC_KEYS),
    ("running", "Sabermetrics"): _person_columns(HITTING_SABERMETRICS_COLUMNS,
                                                 keep=_RUNNING_SABERMETRIC_KEYS),
    ("pitching", "Sabermetrics"): _person_columns(PITCHING_SABERMETRICS_COLUMNS),
    ("hitting", "Expected"): _person_columns(HITTING_EXPECTED_COLUMNS),
    ("pitching", "Expected"): _person_columns(PITCHING_EXPECTED_COLUMNS),
}

# Dailies tab: one player's season game by game, in the same four roles.  The API's
# ``gameLog`` stat type gives one line per game (fielding: per game per position) from
# 1901 on -- nothing earlier.  Rate columns marked * are season to date, through that
# game, worked out here from the counting stats (the API's own rates are to-date too,
# but would run the regular season and postseason together).
DAILY_FROM_SEASON = 1901
_DAILY_LEAD = [("Date", "_date"), ("Team", "_team"), ("Opp", "_opp"), ("Result", "_result")]
DAILY_COLUMNS = {
    "hitting": _DAILY_LEAD + [
        ("Pos", "_pos"), ("PA", "plateAppearances"), ("AB", "atBats"), ("R", "runs"),
        ("H", "hits"), ("2B", "doubles"), ("3B", "triples"), ("HR", "homeRuns"), ("RBI", "rbi"),
        ("BB", "baseOnBalls"), ("IBB", "intentionalWalks"), ("SO", "strikeOuts"),
        ("HBP", "hitByPitch"), ("SH", "sacBunts"), ("SF", "sacFlies"), ("GIDP", "groundIntoDoublePlay"),
        ("LOB", "leftOnBase"), ("AVG*", "_avg"), ("OBP*", "_obp"), ("SLG*", "_slg"), ("OPS*", "_ops"),
        ("Line", "summary"),
    ],
    "running": _DAILY_LEAD + [
        ("On base", "_on_base"), ("SB", "stolenBases"), ("CS", "caughtStealing"), ("R", "runs"),
        ("GIDP", "groundIntoDoublePlay"), ("SB*", "_sb"), ("CS*", "_cs"), ("SB%*", "_sb_pct"),
    ],
    "pitching": _DAILY_LEAD + [
        ("Dec", "_dec"), ("GS", "gamesStarted"), ("IP", "inningsPitched"), ("H", "hits"), ("R", "runs"),
        ("ER", "earnedRuns"), ("HR", "homeRuns"), ("BB", "baseOnBalls"), ("SO", "strikeOuts"),
        ("HBP", "hitBatsmen"), ("WP", "wildPitches"), ("BF", "battersFaced"), ("Pit", "numberOfPitches"),
        ("Str", "strikes"), ("ERA*", "_era"), ("WHIP*", "_whip"), ("Line", "summary"),
    ],
    "fielding": _DAILY_LEAD + [
        ("Pos", "_pos"), ("GS", "gamesStarted"), ("Inn", "innings"), ("Ch", "chances"),
        ("PO", "putOuts"), ("A", "assists"), ("E", "errors"), ("DP", "doublePlays"),
        ("FLD%", "_fld"), ("FLD%*", "_fld_td"),
    ],
}
_DAILY_ROUND = {"F": "WC", "D": "DS", "L": "LCS", "W": "WS", "P": "Post"}  # postseason games' tag after the date
# A team's own game log: the same columns less the ones that only mean something for one player.
_DAILY_PLAYER_ONLY = ("_team", "_pos", "gamesStarted", "summary")
DAILY_TEAM_COLUMNS = {role: [(h, k) for h, k in cols if k not in _DAILY_PLAYER_ONLY]
                      for role, cols in DAILY_COLUMNS.items()}
DAILY_TEXT_KEYS = ("_date", "_team", "_opp", "_result", "_pos", "_dec", "_round", "summary")

# --------------------------------------------------------------------------- #
#  Regression variable registry.                                              #
# --------------------------------------------------------------------------- #
# Dependent-variable candidates: any "Team totals" number (view, group, label, key).
TEAM_RELIEF_FIELDS = [("Holds", "holds"), ("Blown saves", "blownSaves"), ("Save opportunities", "saveOpportunities"),
                      ("Inherited runners", "inheritedRunners"), ("Inherited runners scored", "inheritedRunnersScored")]
SAFE_OUT_VIEW = "Safe/Out (Retrosheet)"                  # a club's batters and runners
SAFE_OUT_ALLOWED_VIEW = "Safe/Out Allowed (Retrosheet)"  # what it allowed its opponents
DEPENDENT_VARIABLES = (
    [("Batting", "hitting", label, key) for label, key in TEAM_HITTING_FIELDS]
    + [("Pitching", "pitching", label, key) for label, key in TEAM_PITCHING_FIELDS]
    + [("Fielding", "fielding", label, key) for label, key in TEAM_FIELDING_FIELDS]
    # MLB's official relief figures (holds and blown saves from 1999; nothing before)
    + [("Pitching", "pitching", label, key) for label, key in TEAM_RELIEF_FIELDS]
    # from Retrosheet's play-by-play (the Safe/Out and Bullpen Support tabs' summaries), one club at a time
    + [(SAFE_OUT_VIEW, "retrosheet-safe-out", label, key) for label, key in retrosheet.SAFE_OUT_VARIABLES]
    + [(SAFE_OUT_ALLOWED_VIEW, "retrosheet-safe-out-allowed", label, key) for label, key in retrosheet.SAFE_OUT_VARIABLES]
    + [(RELIEF_VIEW, "retrosheet-relief", label, key) for label, key in retrosheet.RELIEF_VARIABLES]
)

# Independent-variable candidates: every non-identifier column of every
# player-list view, tagged with how to roll many player-rows up into one
# team-season number:
#   "sum" -- a counting stat; add it across the roster (this is exactly how
#            the Team totals numbers themselves are built, for the plain
#            hitting/pitching/fielding groups).
#   "avg" -- a rate/scale stat (a batting average, a WAR-family rate, an
#            ERA-minus); combined as a playing-time-weighted mean (weighted
#            by whichever of plateAppearances/battersFaced/innings(Pitched)
#            is present on the row, else an unweighted mean).
# WAR/RAR and their components (batting/fielding/baseRunning/wSB/wGDP/UBR
# runs, shutdowns/meltdowns) are additive by construction, so they are
# summed even though they "look like" saber stats.
REGRESSION_VARIABLES = {
    "Batting": [
        ("G", "gamesPlayed", "sum"), ("PA", "plateAppearances", "sum"),
        ("AB", "atBats", "sum"), ("R", "runs", "sum"), ("H", "hits", "sum"),
        ("2B", "doubles", "sum"), ("3B", "triples", "sum"), ("HR", "homeRuns", "sum"),
        ("RBI", "rbi", "sum"), ("BB", "baseOnBalls", "sum"), ("SO", "strikeOuts", "sum"),
        ("SB", "stolenBases", "sum"), ("CS", "caughtStealing", "sum"),
        ("AVG", "avg", "avg"), ("OBP", "obp", "avg"), ("SLG", "slg", "avg"),
        ("OPS", "ops", "avg"),
    ],
    "Pitching": [
        ("W", "wins", "sum"), ("L", "losses", "sum"), ("ERA", "era", "avg"),
        ("G", "gamesPlayed", "sum"), ("GS", "gamesStarted", "sum"), ("GF", "gamesFinished", "sum"),
        ("CG", "completeGames", "sum"), ("SHO", "shutouts", "sum"), ("SV", "saves", "sum"),
        # IP is read as a plain decimal ("172.1" -> 172.1, not 172 1/3); Outs is exact.
        ("IP", "inningsPitched", "sum"), ("Outs", "outs", "sum"), ("H", "hits", "sum"), ("R", "runs", "sum"),
        ("ER", "earnedRuns", "sum"), ("HR", "homeRuns", "sum"), ("BB", "baseOnBalls", "sum"),
        ("SO", "strikeOuts", "sum"), ("WHIP", "whip", "avg"),
        ("K/9", "strikeoutsPer9Inn", "avg"), ("BB/9", "walksPer9Inn", "avg"),
        # What opposing hitters did -- the pieces a "...against" custom stat
        # (batting/on-base/slugging average against) is calculated from.
        ("AB", "atBats", "sum"), ("HBP", "hitBatsmen", "sum"),
        ("SF", "sacFlies", "sum"), ("TB", "totalBases", "sum"),
        # MLB's official relief figures (holds and blown saves are in its records from 1999)
        ("HLD", "holds", "sum"), ("BS", "blownSaves", "sum"), ("SVO", "saveOpportunities", "sum"),
        ("IR", "inheritedRunners", "sum"), ("IRS", "inheritedRunnersScored", "sum"),
    ],
    "Fielding": [
        ("G", "gamesPlayed", "sum"), ("GS", "gamesStarted", "sum"),
        ("Inn", "innings", "sum"), ("PO", "putOuts", "sum"), ("A", "assists", "sum"),
        ("E", "errors", "sum"), ("DP", "doublePlays", "sum"), ("FLD%", "fielding", "avg"),
        ("RF/9", "rangeFactorPer9Inn", "avg"), ("PB", "passedBall", "sum"),
        ("CS", "caughtStealing", "sum"),
    ],
    "Batting (Advanced)": [
        ("PA", "plateAppearances", "sum"), ("TB", "totalBases", "sum"),
        ("XBH", "extraBaseHits", "sum"), ("ISO", "iso", "avg"), ("BABIP", "babip", "avg"),
        ("HBP", "hitByPitch", "sum"), ("SF", "sacFlies", "sum"), ("SH", "sacBunts", "sum"),
        ("GIDP", "gidp", "sum"), ("P/PA", "pitchesPerPlateAppearance", "avg"),
        ("BB/K", "walksPerStrikeout", "avg"), ("Pitches", "numberOfPitches", "sum"),
        ("Swings", "totalSwings", "sum"), ("Whiffs", "swingAndMisses", "sum"),
    ],
    "Pitching (Advanced)": [
        ("QS", "qualityStarts", "sum"), ("BF", "battersFaced", "sum"),
        ("K/9", "strikeoutsPer9", "avg"), ("BB/9", "baseOnBallsPer9", "avg"),
        ("HR/9", "homeRunsPer9", "avg"), ("H/9", "hitsPer9", "avg"),
        ("K/BB", "strikesoutsToWalks", "avg"), ("BABIP", "babip", "avg"),
        ("OPS-against", "ops", "avg"), ("Whiff%", "whiffPercentage", "avg"),
        ("FB%", "flyBallPercentage", "avg"), ("Balks", "balks", "sum"),
        ("WP", "wildPitches", "sum"),
    ],
    "Batting (Sabermetrics)": [
        ("wOBA", "woba", "avg"), ("wRC+", "wRcPlus", "avg"), ("wRAA", "wRaa", "sum"),
        ("WAR", "war", "sum"), ("RAR", "rar", "sum"), ("Batting", "batting", "sum"),
        ("Fielding", "fielding", "sum"), ("BsR", "baseRunning", "sum"),
        ("Spd", "spd", "avg"), ("UBR", "ubr", "sum"), ("wSB", "wSb", "sum"),
        ("wGDP", "wGdp", "sum"),
    ],
    "Pitching (Sabermetrics)": [
        ("WAR", "war", "sum"), ("RAR", "rar", "sum"), ("FIP", "fip", "avg"),
        ("xFIP", "xfip", "avg"), ("FIP-", "fipMinus", "avg"), ("ERA-", "eraMinus", "avg"),
        ("RA9-WAR", "ra9War", "sum"), ("SD", "sd", "sum"), ("MD", "md", "sum"),
        ("pLI", "pli", "avg"),
    ],
    "Batting (Expected)": [
        ("xAVG", "avg", "avg"), ("xSLG", "slg", "avg"), ("xwOBA", "woba", "avg"),
        ("xwOBAcon", "wobaCon", "avg"),
    ],
    "Pitching (Expected)": [
        ("xAVG", "avg", "avg"), ("xSLG", "slg", "avg"), ("xwOBA", "woba", "avg"),
        ("xwOBAcon", "wobaCon", "avg"),
    ],
    # counts from Retrosheet's play-by-play (the retrosheet module), by runner
    RETRO_VIEW: [(label, key, "sum") for label, key in retrosheet.RUNNING_VARIABLES],
    RELIEF_VIEW: [(label, key, "sum") for label, key in retrosheet.RELIEF_VARIABLES],
    BATTED_IN_VIEW: [(label, key, "sum") for label, key in retrosheet.BATTED_IN_VARIABLES],
}

# "View: Label" (exactly how the predictor list and the per-player
# dependent-variable dropdown display each one) -> (view, key, agg), for
# resolving a per-player custom-stat formula's variables.
REGRESSION_VARIABLE_INDEX = {
    f"{view}: {label}": (view, key, agg)
    for view, columns in REGRESSION_VARIABLES.items()
    for label, key, agg in columns
}
# the Score views' variables: known to player-level formulas, but not offered as roster totals
# on the Correlation, Regression and Factor Analysis tabs (MLB's figure or a team's games added
# up over a roster means nothing)
# (a player's own Range figures add up over two teams' rows; MLB's figure and his teams' games are the
# same on every row, or near enough, so they're averaged -- one row is just its value)
SCORE_VARIABLES = {view: [(label, key, "sum" if view == "Range" else "avg") for label, key in cols]
                   for view, (_g, cols) in SCORE_VIEWS.items()}
REGRESSION_VARIABLE_INDEX.update({f"{view}: {label}": (view, key, agg)
                                  for view, columns in SCORE_VARIABLES.items() for label, key, agg in columns})

# Factor Analysis tab: which REGRESSION_VARIABLES views its predictor list
# offers for each "Batters"/"Pitchers"/"All players" grouping choice.
# Fielding is common to both roles (a pitcher fields his position same as
# anyone else), so it's offered under either one; "All players" is simply
# every view, same universe the Regression tab's predictor list uses.
FACTOR_GROUPING_VIEWS = {
    "Batters": ["Batting", "Batting (Advanced)", "Batting (Sabermetrics)",
                "Batting (Expected)", "Fielding", BATTED_IN_VIEW],
    "Pitchers": ["Pitching", "Pitching (Advanced)", "Pitching (Sabermetrics)",
                 "Pitching (Expected)", "Fielding", RELIEF_VIEW],
    "All players": list(REGRESSION_VARIABLES.keys()),
}


def aggregate_rows(rows: list[dict], key: str, agg: str) -> float | None:
    """Roll a list of player-season rows up into one team-season number.

    ``agg="sum"`` adds the stat across the roster.  ``agg="avg"`` takes a
    playing-time-weighted mean, weighted by whichever of plateAppearances /
    battersFaced / inningsPitched / innings is present on each row (falling
    back to an unweighted mean if none is).  Rows missing ``key`` are skipped;
    ``None`` comes back if no row had a usable value.
    """
    total = 0.0
    weight_total = 0.0
    seen = False
    for row in rows:
        value = as_number(row.get(key))
        if value is None:
            continue
        seen = True
        if agg == "sum":
            total += value
            continue
        weight_key = next(
            (k for k in ("plateAppearances", "battersFaced", "inningsPitched", "innings")
             if k in row), None)
        if weight_key in ("inningsPitched", "innings"):
            weight = innings_to_outs(row.get(weight_key))
        elif weight_key is not None:
            weight = as_number(row.get(weight_key))
        else:
            weight = None
        weight = weight if weight else 1.0
        total += value * weight
        weight_total += weight
    if not seen:
        return None
    return total if agg == "sum" else (total / weight_total if weight_total else None)


# Range tab: the nine fielding positions (a DH doesn't field).
RANGE_POSITIONS = [("P", "Pitcher"), ("C", "Catcher"), ("1B", "First base"),
                   ("2B", "Second base"), ("3B", "Third base"), ("SS", "Shortstop"),
                   ("LF", "Left field"), ("CF", "Center field"), ("RF", "Right field")]


def rf_figures(chances_inn: float, outs: float, chances_game: float, games: float) -> dict:
    """Range factor from raw totals, both ways it can be had: per nine innings
    (RF/9 = (PO + A) x 9 / innings, preferred -- it is a rate over a fixed amount
    of play, whereas the length of a game and how long a player stayed in vary)
    and per game (RF/G = (PO + A) / games -- how many innings that was isn't
    known).  ``outs`` is innings x 3.  Each measure is ``None`` when its
    denominator is zero.  The chances counted per measure are kept separate
    because a group can have some rows with innings and some without."""
    return {
        "chances_inn": chances_inn, "outs": outs,
        "chances_game": chances_game, "games": games,
        "rf_9": 27.0 * chances_inn / outs if outs > 0 else None,
        "rf_game": chances_game / games if games > 0 else None,
    }


def fielding_row_figures(row: dict) -> dict:
    """:func:`rf_figures` for one player's fielding row at one position."""
    chances = (as_number(row.get("putOuts")) or 0) + (as_number(row.get("assists")) or 0)
    outs = innings_to_outs(row.get("innings", 0))
    games = as_number(row.get("gamesPlayed")) or 0
    return rf_figures(chances if outs > 0 else 0, outs, chances if games > 0 else 0, games)


def aggregate_league_positions(rows: list[dict]) -> dict[str, dict]:
    """Every fielding row of a whole league-season -> ``{position: rf_figures}``
    for the nine positions: what all of MLB did at each spot, with its ``errors`` and
    ``total_chances`` (putouts, assists and errors), and the ``stolen_bases`` and ``caught_stealing``
    against it (a catcher's)."""
    sums = {pos: [0, 0, 0, 0] for pos, _name in RANGE_POSITIONS}
    errors = {pos: [0, 0, 0, 0] for pos in sums}  # errors, total chances (PO + A + E), stolen bases, caught stealing
    for row in rows:
        s = sums.get(row.get("_pos"))
        if s is None:
            continue
        f = fielding_row_figures(row)
        s[0] += f["chances_inn"]
        s[1] += f["outs"]
        s[2] += f["chances_game"]
        s[3] += f["games"]
        e = as_number(row.get("errors")) or 0
        errors[row["_pos"]][0] += e
        errors[row["_pos"]][1] += (as_number(row.get("putOuts")) or 0) + (as_number(row.get("assists")) or 0) + e
        errors[row["_pos"]][2] += as_number(row.get("stolenBases")) or 0
        errors[row["_pos"]][3] += as_number(row.get("caughtStealing")) or 0
    return {pos: dict(rf_figures(*s), errors=errors[pos][0], total_chances=errors[pos][1],
                      stolen_bases=errors[pos][2], caught_stealing=errors[pos][3]) for pos, s in sums.items()}


def range_ratio(player: dict, league: dict):
    """A player's range factor against all of MLB's at that position, on the same
    measure for both -- RF/9 if both have innings, else RF/G.
    -> ``(measure, player_rf, league_rf, ratio)`` with measure "rf9" or "game",
    or ``None`` if neither measure works for both."""
    for measure, key in (("rf9", "rf_9"), ("game", "rf_game")):
        p, l = player.get(key), league.get(key)
        if p is not None and l:
            return measure, p, l, p / l
    return None


def roster_range(rows: list[dict], league: dict[str, dict]) -> dict | None:
    """The range of every fielder a team used, taken together: all the chances they accepted
    divided by the chances an average MLB fielder would have had in the same playing time at
    the same positions.  ``rows`` are the team's fielding rows (one per player per position),
    ``league`` is :func:`aggregate_league_positions`' result.

    Each row is judged like a lineup slot on the Range tab -- RF/9 (expected chances = MLB's RF/9
    x innings / 9) where both sides have innings, else RF/G (MLB's RF/G x games) -- but the rows
    are added up rather than averaged, so a utility man's few innings weigh what they are worth.
    Rows at a position outside the nine (DH, a bare "OF") are left out.

    -> ``{"ratio", "chances", "expected", "players"}`` (``players`` counts distinct fielders that
    were measured), or ``None`` if no row could be measured."""
    chances = expected = 0.0
    players = set()
    for row in rows:
        lg = league.get(row.get("_pos"))
        if not lg:
            continue
        f = fielding_row_figures(row)
        if f["rf_9"] is not None and lg.get("rf_9"):
            chances += f["chances_inn"]
            expected += lg["rf_9"] * f["outs"] / 27.0
        elif f["rf_game"] is not None and lg.get("rf_game"):
            chances += f["chances_game"]
            expected += lg["rf_game"] * f["games"]
        else:
            continue
        players.add(row.get("_id") or id(row))
    if not players or expected <= 0:
        return None
    return {"ratio": chances / expected, "chances": chances, "expected": expected, "players": len(players)}


# Production tab: the nine places in a batting order.  The API's situation codes
# b1..b9 ("Batting First" .. "Batting Ninth") split a player's season by the lineup
# place he batted from.
LINEUP_SLOTS = range(1, 10)
BATTING_ORDER_SITCODES = ",".join(f"b{n}" for n in LINEUP_SLOTS)


def parse_batting_order(payload: dict) -> dict[int, dict[int, dict]]:
    """A team-season's ``statSplits`` response for the b1..b9 situation codes ->
    ``{player id: {lineup place: {"games": G, "pa": PA}}}`` -- how often each player
    batted from each place.  A place he never batted from has no entry."""
    out: dict[int, dict[int, dict]] = {}
    for section in payload.get("stats", []):
        for split in section.get("splits", []):
            code = (split.get("split") or {}).get("code") or ""
            pid = (split.get("player") or {}).get("id")
            if pid is None or len(code) != 2 or code[0] != "b" or not code[1].isdigit():
                continue
            slot = int(code[1])
            if slot not in LINEUP_SLOTS:
                continue
            stat = split.get("stat") or {}
            place = out.setdefault(pid, {}).setdefault(slot, {"games": 0, "pa": 0})
            place["games"] += int(as_number(stat.get("gamesPlayed")) or 0)
            place["pa"] += int(as_number(stat.get("plateAppearances")) or 0)
    return out


def rank_for_slot(rows: list[dict], order: dict, slot: int) -> tuple[list[dict], list[dict]]:
    """Split a roster's batting rows for one lineup place -> ``(batted there, others)``.
    Those who batted from ``slot`` come first, most games there first (then most plate
    appearances there, then name); the others follow, by season plate appearances.
    ``order`` is :func:`parse_batting_order`'s result; with no data for the season
    (it starts in 1950) everyone is in "others", so the whole list is by plate appearances."""
    def season_pa(row):
        return as_number(row.get("plateAppearances")) or 0

    here, others = [], []
    for row in rows:
        (here if slot in order.get(row.get("_id"), {}) else others).append(row)
    here.sort(key=lambda r: (-order[r["_id"]][slot]["games"], -order[r["_id"]][slot]["pa"],
                             r.get("_name", "")))
    others.sort(key=lambda r: (-season_pa(r), r.get("_name", "")))
    return here, others


def production_values(ftext: str, groups_by_view: dict, pids) -> dict:
    """A player-level custom stat's formula evaluated for each of ``pids`` ->
    ``{player id: value or None}``.  ``groups_by_view`` is ``{view: {player id: rows}}``
    for every view the formula names (see :func:`player_variable_lookup`)."""
    variables = formula.formula_variables(ftext)
    return {pid: formula.evaluate_formula(ftext, player_variable_lookup(variables, groups_by_view, pid))
            for pid in pids}


def production_rate(value: float | None, plate_appearances: float | None) -> float | None:
    """A production total per plate appearance, or ``None`` if either is missing."""
    if value is None or not plate_appearances:
        return None
    return value / plate_appearances


def league_production(values: dict, plate_appearances: dict) -> dict:
    """What all of MLB did: ``values`` is ``{player id: production value or None}`` and
    ``plate_appearances`` ``{player id: PA}`` -> ``{"total", "pa", "players", "rate"}``.
    A player with no value or no plate appearances is left out of both sums, so the
    rate is the production per plate appearance of everyone who could be measured."""
    total = pa_total = 0.0
    players = 0
    for pid, value in values.items():
        pa = plate_appearances.get(pid)
        if value is None or not pa:
            continue
        total += value
        pa_total += pa
        players += 1
    return {"total": total, "pa": pa_total, "players": players,
            "rate": production_rate(total, pa_total)}


def roster_production(rows: list[dict], values: dict) -> dict:
    """Every batter a team used, taken together: ``rows`` are the team's batting rows and ``values``
    ``{player id: production value or None}`` (see :func:`production_values`) -> what
    :func:`league_production` gives for them -- total, plate appearances, ``players`` measured and
    ``rate`` per plate appearance -- so it is comparable with MLB's rate.  Anyone without a plate
    appearance is left out, pitchers who batted are in."""
    return league_production(
        {row["_id"]: values.get(row["_id"]) for row in rows if row.get("_id") is not None},
        {row["_id"]: as_number(row.get("plateAppearances")) for row in rows if row.get("_id") is not None})


def _assign_greedily(pairs: list[tuple]) -> dict:
    """``pairs`` are ``(sort key, place, row)``, best first after sorting: give each place its best
    remaining player, no player taking two places -> ``{place: row}``.  Whoever is best anywhere is
    placed first, so a man who is the busiest at two places gets the one he did most and the other
    goes to the next best."""
    chosen: dict = {}
    used = set()
    for _key, place, row in sorted(pairs, key=lambda t: t[0]):
        if place in chosen or row["_id"] in used:
            continue
        chosen[place] = row
        used.add(row["_id"])
    return chosen


def assign_lineup(rows: list[dict], order: dict) -> dict[int, dict]:
    """A team's usual batting order -> ``{slot 1-9: batting row}``: the batter who was in each
    slot for the most games (then the most plate appearances there), no batter in two slots.
    ``order`` is :func:`parse_batting_order`'s result; a slot nobody is known to have batted from
    (or every slot, before its records begin in 1950) is left out."""
    pairs = []
    for row in rows:
        for slot, info in (order.get(row.get("_id")) or {}).items():
            if info["games"] > 0:
                pairs.append(((-info["games"], -info["pa"], row.get("_name", ""), slot), slot, row))
    return _assign_greedily(pairs)


def lineup_by_plate_appearances(rows: list[dict]) -> dict[int, dict]:
    """A lineup for a season without batting-order records (before 1950) -> ``{slot 1-9: batting
    row}``: the nine with the most plate appearances, most first (a batter higher in the order
    comes up more often), ties by name."""
    ranked = sorted((r for r in rows if r.get("_id") is not None and (as_number(r.get("plateAppearances")) or 0) > 0),
                    key=lambda r: (-(as_number(r.get("plateAppearances")) or 0), r.get("_name", "")))
    return {slot: row for slot, row in zip(LINEUP_SLOTS, ranked)}


def assign_positions(rows: list[dict]) -> dict[str, dict]:
    """A team's usual defense -> ``{position: fielding row}`` for the nine positions: the player who
    played each for the most innings (else the most games, when innings weren't recorded), no
    player at two positions.  ``rows`` are the team's fielding rows, one per player per position."""
    codes = {code for code, _name in RANGE_POSITIONS}
    pairs = []
    for row in rows:
        pos = row.get("_pos")
        if pos in codes and row.get("_id") is not None:
            pairs.append(((-innings_to_outs(row.get("innings", 0)), -(as_number(row.get("gamesPlayed")) or 0),
                           row.get("_name", ""), pos), pos, row))
    return _assign_greedily(pairs)


def head_to_head(batter_for: float | None, pitcher_against: float | None) -> float | None:
    """A batter's "for" number and a pitcher's matching "against" number
    averaged -- the matchup estimate.  Below the batter's own number, the
    pitcher deflates it; above, inflates it.  ``None`` if either is missing."""
    if batter_for is None or pitcher_against is None:
        return None
    return (batter_for + pitcher_against) / 2.0



# Batter-vs-pitcher actual results come from play-by-play, which the source has from about 1950.
VS_PLAYER_FROM_SEASON = 1950


def parse_vs_player(payload: dict) -> dict:
    """A ``vsPlayer`` payload -> ``{"seasons": {season (int): stat}, "career": stat or None}``;
    each stat dict has numbers for the counting stats and ``avg``/``obp``/``slg`` as floats
    (None when there were no at-bats / plate appearances to divide by)."""
    out = {"seasons": {}, "career": None}
    for block in payload.get("stats", []):
        kind = (block.get("type") or {}).get("displayName", "")
        for split in block.get("splits", []):
            stat = dict(split.get("stat", {}))
            for key in ("avg", "obp", "slg", "ops"):
                stat[key] = as_number(stat.get(key))
            if kind == "vsPlayerTotal":
                out["career"] = stat
            elif kind == "vsPlayer" and str(split.get("season", "")).isdigit():
                out["seasons"][int(split["season"])] = stat
    return out


def get_vs_player(batter_id: int, pitcher_id: int, ttl: float | None = None) -> dict:
    """What the batter actually did against the pitcher, regular season, by season and in
    all -- see :func:`parse_vs_player`."""
    payload = api_get(f"/people/{batter_id}/stats",
                      {"stats": "vsPlayer", "group": "hitting", "opposingPlayerId": pitcher_id,
                       "sportId": SPORT_ID}, ttl=ttl)
    return parse_vs_player(payload)

def group_by_player(rows: list[dict]) -> dict[int, list[dict]]:
    """Split a player-list view's rows by player id.

    Almost every row is one player's only row; Fielding is the exception
    (one row per position), which is exactly why callers feed a player's
    group through :func:`aggregate_rows` rather than using a row directly.
    """
    groups: dict[int, list[dict]] = {}
    for row in rows:
        pid = row.get("_id")
        if pid is not None:
            groups.setdefault(pid, []).append(row)
    return groups


def team_totals_lookup(team_id: int, season: int, ttl: float | None = None,
                       names=None, errors: dict | None = None) -> dict[str, float | None]:
    """``{"Batting: Runs": 750.0, "Pitching: ERA": 3.45, ...}`` -- every Team
    totals field for one team-season, keyed exactly as the dependent-variable
    dropdown labels them.  This is what a custom-stat formula's ``{...}``
    variables resolve against; a field with no value for this team-season
    (or a team-season the API 404s on) comes back as ``None``, not a
    KeyError, so a formula referencing it just evaluates to ``None`` too.
    ``names`` (the variables the formulas use) limits the Retrosheet groups fetched to the ones they
    need -- each is a season's download the first time; the MLB groups are always fetched.
    ``errors``, if given, gets ``{view: why}`` for each Retrosheet view that couldn't be had (a
    season Retrosheet hasn't published yet, say), so a caller can say why its figures are blank.
    """
    by_group = {}
    wanted = {group for view, group, label, _key in DEPENDENT_VARIABLES
              if names is None or f"{view}: {label}" in names or not group.startswith("retrosheet")}
    for group in dict.fromkeys(g for _v, g, _l, _k in DEPENDENT_VARIABLES):
        if group not in wanted:
            by_group[group] = {}
            continue
        try:
            by_group[group] = get_team_totals(team_id, season, group, ttl)
        except MLBStatsError as exc:
            by_group[group] = {}
            if errors is not None and group.startswith("retrosheet"):
                view = next(v for v, g, _l, _k in DEPENDENT_VARIABLES if g == group)
                errors.setdefault(view, str(exc))
    return {f"{view}: {label}": as_number(by_group[group].get(key))
            for view, group, label, key in DEPENDENT_VARIABLES}


def player_variable_lookup(variable_names, groups_by_view: dict[str, dict[int, list[dict]]],
                           pid) -> dict[str, float | None]:
    """``{"Batting: HR": 3.0, ...}`` for one player: resolves each of
    ``variable_names`` (``"View: Label"`` tokens, matching
    :data:`REGRESSION_VARIABLE_INDEX`) against that player's rows within
    ``groups_by_view`` (``{view: {player_id: [row, ...]}}``, one entry per
    view a custom-stat formula touches).  A view/player combination with no
    rows resolves to ``None``, same as a missing Team totals field.
    """
    out = {}
    for name in variable_names:
        view, key, agg = REGRESSION_VARIABLE_INDEX[name]
        rows = groups_by_view.get(view, {}).get(pid)
        out[name] = aggregate_rows(rows, key, agg) if rows else None
    return out


# --------------------------------------------------------------------------- #
#  Low-level fetch with an on-disk cache.                                     #
# --------------------------------------------------------------------------- #
def _cache_path(url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, digest + ".json")


def _read_cache(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_cache(path: str, url: str, data) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"url": url, "fetched": time.time(), "data": data}, fh)
        os.replace(tmp, path)
    except OSError:
        pass  # a broken cache must never break a lookup


def _http_get(url: str):
    """GET JSON, announced to ``retrosheet.DOWNLOAD_HOOKS`` (the status bar) while it lasts."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    parts = urllib.parse.urlsplit(url)
    what = "MLB's Stats API" if "statsapi" in parts.netloc else parts.netloc
    try:
        retrosheet.notify_download(what)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        finally:
            retrosheet.notify_download(what, done=True)
    except urllib.error.HTTPError as exc:
        raise MLBStatsError(f"HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MLBStatsError(f"cannot reach statsapi.mlb.com ({exc})") from exc
    except ValueError as exc:
        raise MLBStatsError(f"bad JSON from {url}") from exc


def api_get(path: str, params: dict | None = None, ttl: float | None = None, base: str = API):
    """GET ``base + path`` with ``params`` (``base`` is the v1 API unless given).

    ``ttl`` controls the cache:
      * ``None``  -> use any cached copy forever (right for finished seasons)
      * a number  -> refetch if the cached copy is older than ``ttl`` seconds
    A network error falls back to a stale cache entry when one exists.
    """
    query = urllib.parse.urlencode(sorted((params or {}).items()))
    url = f"{base}{path}?{query}" if query else f"{base}{path}"
    path_on_disk = _cache_path(url)
    cached = _read_cache(path_on_disk)

    if cached is not None and (ttl is None or time.time() - cached.get("fetched", 0) <= ttl):
        return cached["data"]

    try:
        data = _http_get(url)
    except MLBStatsError:
        if cached is not None:
            return cached["data"]
        raise
    _write_cache(path_on_disk, url, data)
    return data


def clear_cache() -> int:
    """Delete every cached response.  Returns the number of files removed."""
    removed = 0
    try:
        for name in os.listdir(CACHE_DIR):
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(CACHE_DIR, name))
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


# --------------------------------------------------------------------------- #
#  Pure parsing helpers.                                                      #
# --------------------------------------------------------------------------- #
def parse_teams(payload: dict) -> list[dict]:
    """-> list of {id, name, abbrev, nickname, location, first_year, league, league_id,
    division, division_id} -- the league and division are that season's: a division only
    from 1969 on, and the Negro leagues (1920-1948) count as leagues of their own."""
    out = []
    for team in payload.get("teams", []):
        try:
            first = int(team.get("firstYearOfPlay") or 0) or None
        except (TypeError, ValueError):
            first = None
        out.append({
            "id": team.get("id"),
            "name": team.get("name", "?"),
            "abbrev": team.get("abbreviation", ""),
            "nickname": team.get("teamName", ""),
            "location": team.get("locationName", ""),
            "first_year": first,
            "league": (team.get("league") or {}).get("name"),
            "league_id": (team.get("league") or {}).get("id"),
            "division": (team.get("division") or {}).get("name"),
            "division_id": (team.get("division") or {}).get("id"),
        })
    out.sort(key=lambda t: t["name"])
    return out


def _first_split_stat(payload: dict) -> dict:
    stats = payload.get("stats") or []
    if not stats:
        return {}
    splits = stats[0].get("splits") or []
    return splits[0].get("stat", {}) if splits else {}


def parse_team_totals(payload: dict) -> dict:
    """The single aggregate stat block for a team-season, or {}."""
    return _first_split_stat(payload)


def parse_player_rows(payload: dict) -> list[dict]:
    """One dict per split: the ``stat`` block plus ``_name``, ``_id``, ``_pos``, ``_team_id``,
    ``_num_teams`` (a league-wide row of a traded player covers all his clubs) and ``_league``
    (its short name: AL, NL, FL, NNL ...).

    For fielding a player has one split per position, so ``_pos`` distinguishes
    them; for batting/pitching the API sends no position and ``_pos`` is "".
    """
    rows = []
    for section in payload.get("stats", []):
        for split in section.get("splits", []):
            stat = dict(split.get("stat", {}))
            player = split.get("player", {})
            stat["_name"] = player.get("fullName", "?")
            stat["_id"] = player.get("id")
            stat["_pos"] = split.get("position", {}).get("abbreviation", "")
            stat["_team_id"] = (split.get("team") or {}).get("id")
            stat["_num_teams"] = split.get("numTeams") or 1
            stat["_league"] = (split.get("league") or {}).get("name", "")
            rows.append(stat)
    return rows


def parse_people(payload: dict) -> list[dict]:
    """People-search results -> ``{id, name, position, debut, birth, active}`` dicts.

    Players who reached the majors come first, most recent debut first (the
    likelier target of a search -- "Vladimir Guerrero" puts Jr. above Sr.);
    everyone without an MLB debut (minor leaguers) follows, by name.
    """
    people = []
    for p in payload.get("people", []):
        if p.get("id") is None:
            continue
        people.append({
            "id": p["id"],
            "name": p.get("fullName", "?"),
            "position": (p.get("primaryPosition") or {}).get("abbreviation", ""),
            "debut": p.get("mlbDebutDate") or None,
            "birth": p.get("birthDate") or None,
            "active": bool(p.get("active")),
        })
    debuted = sorted((p for p in people if p["debut"]), key=lambda p: p["debut"], reverse=True)
    others = sorted((p for p in people if not p["debut"]), key=lambda p: p["name"])
    return debuted + others


def parse_person(person: dict) -> dict:
    """One ``people[]`` entry -> the handful of bio facts the Personal tab shows."""
    place = ", ".join(x for x in (person.get("birthCity"), person.get("birthStateProvince"),
                                  person.get("birthCountry")) if x)
    return {
        "id": person.get("id"),
        "name": person.get("fullName", "?"),
        "surname": person.get("useLastName") or person.get("lastName") or "",
        "position": (person.get("primaryPosition") or {}).get("abbreviation", ""),
        "bats": (person.get("batSide") or {}).get("code", ""),
        "throws": (person.get("pitchHand") or {}).get("code", ""),
        "height": person.get("height", ""),
        "weight": person.get("weight"),
        "birth_date": person.get("birthDate", ""),
        "birth_place": place,
        "debut": person.get("mlbDebutDate", ""),
        "active": bool(person.get("active")),
    }


def parse_person_splits(payload: dict) -> dict[str, list[dict]]:
    """One player's ``yearByYear`` and ``career`` blocks -> ``{"season": [...],
    "career": [...]}``, each row the ``stat`` dict plus ``_season``, ``_team``,
    ``_pos`` and ``_kind``.

    ``_kind`` is "season" (one team's line), "total" (the all-teams line the API
    adds for a traded player's season -- it carries no team, only ``numTeams``)
    or "career".  Also reads the Advanced/Sabermetrics/Expected block types.  A career fielding block has one row per position.
    A role the player never played comes back as an empty ``stats`` list, so
    this just yields empty lists for it.
    """
    out: dict[str, list[dict]] = {"season": [], "career": []}
    for block in payload.get("stats", []):
        kind = (block.get("type") or {}).get("displayName", "")
        target = {"yearByYear": "season", "yearByYearAdvanced": "season",
                  "sabermetrics": "season", "expectedStatistics": "season",
                  "career": "career", "careerAdvanced": "career"}.get(kind)
        if target is None:
            continue
        for split in block.get("splits", []):
            row = dict(split.get("stat", {}))
            row["_pos"] = (split.get("position") or {}).get("abbreviation", "")
            row["_team_id"] = (split.get("team") or {}).get("id")
            num_teams = split.get("numTeams") or 0
            multi = f"{num_teams} Teams" if num_teams > 1 else ""
            team = (split.get("team") or {}).get("name", "")
            if target == "career":
                row.update(_season="Career", _team=multi, _kind="career")
            elif team:
                row.update(_season=str(split.get("season", "")), _team=team, _kind="season")
            elif num_teams > 1:
                row.update(_season=str(split.get("season", "")), _team=multi, _kind="total")
            else:  # e.g. an Expected line: one per season, not broken out by team
                row.update(_season=str(split.get("season", "")), _team="", _kind="season")
            out[target].append(row)
    return out


def person_batted(stats: dict[str, dict]) -> set[tuple[int, int]]:
    """``{(season, team id), ...}`` where a player has a batting line (pinch runners included, at
    0 plate appearances) -- where he can have baserunning -- from :func:`get_person_stats` results."""
    return {(int(r["_season"]), r["_team_id"]) for r in stats.get("hitting", {}).get("season", [])
            if r.get("_kind") == "season" and r.get("_team_id") and str(r.get("_season", "")).isdigit()}


def person_stints(stats: dict[str, dict]) -> list[tuple[int, int, str]]:
    """``[(season, team id, team name), ...]`` -- every team-season of a player's career, in order,
    from :func:`get_person_stats` results by group (``{"hitting": ..., "pitching": ..., ...}``).  A
    season's teams are in the order he played for them, as MLB lists them; a team found in only one
    group goes in after the team it follows there."""
    names: dict[tuple[int, int], str] = {}
    order: dict[int, list[int]] = {}
    for data in stats.values():
        previous: dict[int, int | None] = {}
        for row in data.get("season", []):
            if row.get("_kind") != "season" or not row.get("_team_id") or not str(row.get("_season", "")).isdigit():
                continue
            season, tid = int(row["_season"]), row["_team_id"]
            teams = order.setdefault(season, [])
            if tid not in teams:
                after = previous.get(season)
                teams.insert(teams.index(after) + 1 if after in teams else 0, tid)
                names[(season, tid)] = row.get("_team", "")
            previous[season] = tid
    return [(season, tid, names[(season, tid)]) for season in sorted(order) for tid in order[season]]


def person_custom_stats(pid: int, stints, stats, ttl_for=lambda season: None, workers: int = 4,
                        batted=None, progress=None) -> dict:
    """Player-level custom stats (``[(name, formula), ...]``) for each of a player's team-seasons
    ``stints`` (:func:`person_stints`), worked out as the Custom tab does -- the team-season's views
    fetched, then his rows looked up -- plus, for a season with more than one team, his whole season
    (his rows from every team taken together).  ``ttl_for(season)`` picks the cache lifetime.
    ``batted`` (a set of (season, team id), or None for all) says where he batted or ran:
    Retrosheet's play-by-play, several MB a season to download, is only fetched for those (a
    pitcher who never came to bat has no baserunning to find).  ``progress(done, total, stint)``,
    if given, is called (from a worker thread) as each team-season is finished.

    -> ``{"rows": [{"season", "team", "teams", "values": {name: value}, "unqualified": {name: value},
    "sampled": {name: coverage}}, ...], "unavailable": {view: reason}}``, a season's teams in order and
    then its total (``teams`` > 1).  ``unqualified`` has what a stat blank for want of playing time
    (its ``qualify``) would otherwise be -- for show only; ``values`` leaves them out.
    ``sampled`` names the values that rest on Retrosheet's play-by-play of only part of the team-season
    (:func:`retro_sample`; for a whole season, the smallest part's) -- those that come out otherwise
    without it."""
    parsed = [(name, text, formula.formula_variables(text)) for name, text in stats]
    views = sorted({REGRESSION_VARIABLE_INDEX[v][0] for _n, _t, fvars in parsed for v in fvars})
    unavailable: dict[str, str] = {}

    def fetch(stint):
        season, team_id, _name = stint
        mine = {}
        sample = None
        for view in views:
            group, stat_type, _cols = VIEW_SPECS[view]
            if group == "retrosheet" and batted is not None and (season, team_id) not in batted:
                mine[view] = []
                continue
            try:
                rows = get_player_stats(team_id, season, group, ttl_for(season), stat_type=stat_type)
            except MLBStatsError as exc:
                unavailable.setdefault(view, str(exc))
                rows = []
            mine[view] = [r for r in rows if r.get("_id") == pid]
            if group.startswith("retrosheet") and mine[view] and sample is None:
                sample = retro_sample(team_id, season, ttl_for(season))
        return mine, sample

    fetched = [None] * len(stints)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, stint): i for i, stint in enumerate(stints)}
        for done, future in enumerate(as_completed(futures), start=1):
            fetched[futures[future]] = future.result()
            if progress is not None:
                progress(done, len(stints), stints[futures[future]])

    def values(per_view: dict, qualifying: bool = True) -> dict:
        groups = {view: {pid: rows} for view, rows in per_view.items() if rows}
        return {name: formula.evaluate_formula(text, player_variable_lookup(fvars, groups, pid), qualifying=qualifying)
                for name, text, fvars in parsed}

    def row(season, team, teams, per_view: dict, sample) -> dict:
        got = values(per_view)
        loose = values(per_view, qualifying=False)
        unqualified = {name: value for name, value in loose.items() if got[name] is None and value is not None}
        sampled = {}
        if sample is not None:  # which values the play-by-play changes
            bare = {view: rows for view, rows in per_view.items() if view not in RETRO_VIEWS}
            without, loose_without = values(bare), values(bare, qualifying=False)
            sampled = {name: sample for name, value in got.items() if value is not None and value != without[name]}
            sampled.update({name: sample for name, value in unqualified.items() if value != loose_without[name]})
        return {"season": season, "team": team, "teams": teams, "values": got, "unqualified": unqualified,
                "sampled": sampled}

    out = []
    by_season: dict[int, list] = {}
    for (season, team_id, team), (mine, sample) in zip(stints, fetched):
        by_season.setdefault(season, []).append((mine, sample))
        out.append(row(season, team, 1, mine, sample))
        later = [s for s, _t, _n in stints if s == season]
        if len(by_season[season]) == len(later) > 1:   # his last team that season: add the season's total
            parts = by_season[season]
            combined = {view: [r for part, _s in parts for r in part[view]] for view in views}
            samples = [s for _part, s in parts if s is not None]
            out.append(row(season, f"{len(later)} Teams", len(later), combined, min(samples) if samples else None))
    return {"rows": out, "unavailable": unavailable}


def career_scores(rows: list[dict], names, minimum: int = 3) -> dict:
    """Each score in ``names`` averaged over the seasons that have one, from :func:`person_custom_stats`'s
    ``rows`` -> ``{name: (average or None, seasons counted, whether any of them is ``sampled``)}``.  A
    season counts once: a split season by its whole-season line, or where that's blank, by its team
    lines' scores averaged.  Fewer than ``minimum`` seasons with the score gives no average."""
    seasons: dict[int, dict] = {}
    for row in rows:
        part = seasons.setdefault(row["season"], {"total": None, "teams": []})
        if row["teams"] > 1:
            part["total"] = row
        else:
            part["teams"].append(row)
    out = {}
    for name in names:
        yearly, sampled = [], False
        for part in seasons.values():
            total = part["total"]
            if total is not None and total["values"].get(name) is not None:
                value, sampled = total["values"][name], sampled or name in total.get("sampled", {})
            else:
                got = [r for r in part["teams"] if r["values"].get(name) is not None]
                value = sum(r["values"][name] for r in got) / len(got) if got else None
                sampled = sampled or any(name in r.get("sampled", {}) for r in got)
            if value is not None:
                yearly.append(value)
        out[name] = (sum(yearly) / len(yearly) if len(yearly) >= minimum else None, len(yearly), sampled)
    return out


# --------------------------------------------------------------------------- #
#  Small value helpers (shared with the GUI and the xlsx export).            #
# --------------------------------------------------------------------------- #
def as_number(value):
    """Best-effort float for sorting / spreadsheets.  ``None`` if not numeric.

    Understands the API's dot-prefixed rate strings (``".288"`` -> 0.288).
    Innings pitched ("172.1") parses as the plain float 172.1; callers that
    need thirds use :func:`innings_to_outs` instead.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or set(text) <= set("-.*") or text[-1] in "-*":
        return None
    if text[0] == "." or text[:2] == "-.":
        text = "-0." + text[2:] if text[0] == "-" else "0" + text
    try:
        return float(text)
    except ValueError:
        return None


def innings_to_outs(ip_str) -> int:
    """``"12.1"`` -> 37 outs, for sorting pitchers by workload."""
    try:
        whole, _, frac = str(ip_str).partition(".")
        return int(whole or 0) * 3 + (int(frac) if frac else 0)
    except ValueError:
        return 0


# --------------------------------------------------------------------------- #
#  Public, network-backed API.                                               #
# --------------------------------------------------------------------------- #
def current_season() -> int:
    """The season MLB considers current (falls back to the calendar year)."""
    try:
        data = api_get("/seasons", {"sportId": SPORT_ID}, ttl=6 * 3600)
        seasons = data.get("seasons") or []
        if seasons:
            return int(seasons[0]["seasonId"])
    except (MLBStatsError, KeyError, ValueError, IndexError):
        pass
    today = _dt.date.today()
    return today.year - 1 if today.month < 3 else today.year


def get_teams(season: int, ttl: float | None = None) -> list[dict]:
    payload = api_get("/teams", {"sportId": SPORT_ID, "season": season}, ttl=ttl)
    return parse_teams(payload)


def teams_in_scope(teams: dict[int, dict], kind: str, anchor_id) -> dict[int, dict]:
    """The teams of one season's ``get_team_map`` that a sample scope covers: ``"league"``
    is all of them; ``"same_league"`` and ``"same_division"`` are the ones in the same
    league or division as team ``anchor_id`` that season.  Empty if the anchor didn't
    play then, or (for a division) had none -- divisions began in 1969."""
    if kind == "league":
        return dict(teams)
    anchor = teams.get(anchor_id)
    key = {"same_league": "league_id", "same_division": "division_id"}[kind]
    if anchor is None or anchor.get(key) is None:
        return {}
    return {tid: rec for tid, rec in teams.items() if rec.get(key) == anchor[key]}


def get_team_map(season: int, ttl: float | None = None) -> dict[int, dict]:
    """``{franchise_id: record}`` for teams *as they were known in* ``season``.

    Franchise ids are stable across relocations and renames, so id 110 maps to
    "St. Louis Browns" for 1953 and "Baltimore Orioles" for 1954+.  Teams that
    had not yet been founded in ``season`` are simply absent.
    """
    return {t["id"]: t for t in get_teams(season, ttl=ttl)}


def get_team_totals(team_id: int, season: int, group: str,
                    ttl: float | None = None, stat_type: str = "season") -> dict:
    if group.startswith("retrosheet"):
        return retro_team_totals(team_id, season, group, ttl)
    payload = api_get(
        f"/teams/{team_id}/stats",
        {"stats": stat_type, "group": group, "season": season, "sportId": SPORT_ID},
        ttl=ttl,
    )
    return parse_team_totals(payload)


def get_player_stats(team_id: int, season: int, group: str,
                     ttl: float | None = None, stat_type: str = "season") -> list[dict]:
    if group == "retrosheet":
        return retro_team_rows(team_id, season, ttl)
    if group == "retrosheet-relief":
        return retro_relief_rows(team_id, season, ttl)
    if group == "retrosheet-rbi":
        return retro_batted_in_rows(team_id, season, ttl)
    if group in ("league-figures", "range-figures", "team-figures"):
        return score_view_rows(team_id, season, group, ttl)
    payload = api_get(
        "/stats",
        {
            "stats": stat_type, "group": group, "season": season,
            "sportId": SPORT_ID, "teamId": team_id,
            "playerPool": "all", "limit": 2000,
        },
        ttl=ttl,
    )
    rows = parse_player_rows(payload)
    # Sort by whichever workload/production stat this statType actually carries
    # (advanced/sabermetric/expected views don't all share the same fields).
    if rows and "inningsPitched" in rows[0]:
        rows.sort(key=lambda r: innings_to_outs(r.get("inningsPitched", 0)), reverse=True)
    elif rows and "innings" in rows[0]:
        # busiest position first, then the player's other positions under it
        rows.sort(key=lambda r: (innings_to_outs(r.get("innings", 0)), r.get("_name", "")),
                  reverse=True)
    elif rows and "plateAppearances" in rows[0]:
        rows.sort(key=lambda r: as_number(r.get("plateAppearances")) or 0, reverse=True)
    elif rows and "war" in rows[0]:
        rows.sort(key=lambda r: as_number(r.get("war")) or 0, reverse=True)
    else:
        rows.sort(key=lambda r: r.get("_name", ""))
    return rows



def parse_game_log(payload: dict) -> list[dict]:
    """A ``gameLog`` payload -> one row per split (a game, or for fielding a game at one
    position): the ``stat`` dict plus ``_pk``, ``_date``, ``_game_number``, ``_type`` (R F D L
    W), ``_team_id``, ``_team``, ``_opp_id``, ``_opp``, ``_home`` (bool), ``_win`` (bool) and
    ``_pos`` (the fielding position, or every position played that game for batting)."""
    rows = []
    for block in payload.get("stats", []):
        for split in block.get("splits", []):
            row = dict(split.get("stat", {}))
            row.pop("position", None)
            game = split.get("game") or {}
            if split.get("position"):
                pos = split["position"].get("abbreviation", "")
            else:
                pos = "-".join(p.get("abbreviation", "") for p in split.get("positionsPlayed") or [])
            row.update(
                _pk=game.get("gamePk"), _date=split.get("date", ""),
                _game_number=game.get("gameNumber", 1), _type=split.get("gameType", "R"),
                _team_id=(split.get("team") or {}).get("id"), _team=(split.get("team") or {}).get("name", ""),
                _opp_id=(split.get("opponent") or {}).get("id"),
                _opp=(split.get("opponent") or {}).get("name", ""),
                _home=bool(split.get("isHome")), _win=bool(split.get("isWin")), _pos=pos)
            rows.append(row)
    rows.sort(key=lambda r: (r["_date"], r["_game_number"]))
    return rows


def get_game_log(person_id: int, group: str, season: int, postseason: bool = False,
                 ttl: float | None = None) -> list[dict]:
    """One player's games in ``season`` for one stat ``group`` -- see :func:`parse_game_log`.
    Regular season only unless ``postseason``.  Empty before :data:`DAILY_FROM_SEASON`."""
    params = {"stats": "gameLog", "group": group, "season": season, "sportId": SPORT_ID}
    if postseason:
        params["gameType"] = GAMEDAY_GAME_TYPES
    return parse_game_log(api_get(f"/people/{person_id}/stats", params, ttl=ttl))


def get_team_game_log(team_id: int, group: str, season: int, postseason: bool = False,
                      ttl: float | None = None) -> list[dict]:
    """A team's own game-by-game lines in ``season`` for one stat ``group``, shaped like
    :func:`get_game_log` (``_pos`` is blank).  The team log takes one game type per request
    and its rows don't say which, so the postseason (``gameType=P``) is a second request
    whose rows are tagged ``_type`` "P" -- :func:`daily_lines` names the round from the
    schedule.  Empty before :data:`DAILY_FROM_SEASON` (the API answers 404 there)."""
    if season < DAILY_FROM_SEASON:
        return []
    params = {"stats": "gameLog", "group": group, "season": season, "sportId": SPORT_ID}
    rows = parse_game_log(api_get(f"/teams/{team_id}/stats", params, ttl=ttl))
    if postseason:
        try:
            post = parse_game_log(api_get(f"/teams/{team_id}/stats", dict(params, gameType="P"), ttl=ttl))
        except MLBStatsError as exc:
            if not str(exc).startswith("HTTP 404"):  # 404 = no postseason that year
                raise
            post = []
        for row in post:
            row["_type"] = "P"
        rows += post
    return rows


def _daily_ip(outs: int) -> str:
    return f"{outs // 3}.{outs % 3}"


def _daily_rate(num, den, digits=3):
    """``num / den`` as the API writes rates (".288", "3.45"), or "" with no denominator."""
    if not den:
        return ""
    text = f"{num / den:.{digits}f}"
    return text[1:] if digits == 3 and text.startswith("0.") else text


def _daily_n(row: dict, key: str) -> float:
    return as_number(row.get(key)) or 0


def daily_lines(role: str, rows: list[dict], abbrevs: dict | None = None,
                games: dict | None = None, team: bool = False) -> list[dict]:
    """A role's game-log ``rows`` (from :func:`get_game_log`; Running uses the hitting log)
    -> display rows for :data:`DAILY_COLUMNS`, with season-to-date rates, and after each
    stretch of games -- the regular season, then the postseason -- a total line
    (``_kind`` "total"; fielding has one per position).  Game rows have ``_kind`` "game".

    ``abbrevs`` maps team id -> abbreviation for the Team/Opp columns; ``games`` maps
    game pk -> a :func:`parse_team_schedule` entry, for the score in Result.  ``team``: the
    rows are a team's own log (:func:`get_team_game_log`) -- its decisions leave out holds,
    which are a reliever's, not the club's."""
    abbrevs, games = abbrevs or {}, games or {}
    out: list[dict] = []
    for post in (False, True):
        part = [r for r in rows if (r["_type"] != "R") == post]
        if not part:
            continue
        label = "Postseason" if post else ("Regular season" if any(r["_type"] != "R" for r in rows)
                                            else "Season")
        acc: dict = {}
        fld: dict = {}  # position -> its own running sums

        def add(key, value):
            acc[key] = acc.get(key, 0) + value

        for r in part:
            line = dict(r)
            line["_kind"] = "game"
            team = abbrevs.get(r["_team_id"]) or r["_team"]
            opp = abbrevs.get(r["_opp_id"]) or r["_opp"] or (games.get(r["_pk"]) or {}).get("opponent", "")
            line["_team"] = team
            line["_opp"] = ("vs " if r["_home"] else "@ ") + opp
            line["_date"] = r["_date"] + (f" ({r['_game_number']})" if r["_game_number"] != 1 else "")
            g = games.get(r["_pk"])
            if r["_type"] != "R":
                kind = (g or {}).get("type") if r["_type"] == "P" else r["_type"]
                line["_date"] += " " + _DAILY_ROUND.get(kind or "P", kind)
            if g and g.get("result"):
                line["_result"] = f"{g['result']} {g['runs']}\u2013{g['opp_runs']}"
            else:
                line["_result"] = "W" if r["_win"] else "L"
            if role == "hitting":
                for k in ("atBats", "hits", "baseOnBalls", "hitByPitch", "sacFlies", "totalBases"):
                    add(k, _daily_n(r, k))
                _batting_rates(line, acc)
            elif role == "running":
                line["_on_base"] = int(_daily_n(r, "hits") + _daily_n(r, "baseOnBalls") + _daily_n(r, "hitByPitch"))
                add("sb", _daily_n(r, "stolenBases"))
                add("cs", _daily_n(r, "caughtStealing"))
                _running_rates(line, acc)
            elif role == "pitching":
                line["_dec"] = ", ".join(tag for tag, key in (("W", "wins"), ("L", "losses"), ("SV", "saves"),
                                                              ("H", "holds"), ("BS", "blownSaves"))
                                         if _daily_n(r, key) and not (team and tag == "H"))
                outs = int(_daily_n(r, "outs")) or innings_to_outs(r.get("inningsPitched", 0))
                add("outs", outs)
                for k in ("earnedRuns", "hits", "baseOnBalls"):
                    add(k, _daily_n(r, k))
                _pitching_rates(line, acc)
            else:  # fielding
                po, a, e = _daily_n(r, "putOuts"), _daily_n(r, "assists"), _daily_n(r, "errors")
                line["_fld"] = _daily_rate(po + a, po + a + e)
                pos_acc = fld.setdefault(r["_pos"], {})
                for k, v in (("po", po), ("a", a), ("e", e)):
                    pos_acc[k] = pos_acc.get(k, 0) + v
                line["_fld_td"] = _daily_rate(pos_acc["po"] + pos_acc["a"], pos_acc["po"] + pos_acc["a"] + pos_acc["e"])
            out.append(line)

        # the stretch's total line(s)
        if role == "fielding":
            for pos in dict.fromkeys(r["_pos"] for r in part):  # in order of first appearance
                these = [r for r in part if r["_pos"] == pos]
                tot = _sum_lines(these, ("gamesStarted", "chances", "putOuts", "assists", "errors",
                                         "doublePlays"))
                tot["innings"] = _daily_ip(sum(innings_to_outs(r.get("innings", 0)) for r in these)) \
                    if any(r.get("innings") for r in these) else ""
                po, a, e = (tot.get(k, 0) for k in ("putOuts", "assists", "errors"))
                tot["_fld"] = tot["_fld_td"] = _daily_rate(po + a, po + a + e)
                tot.update(_date=f"{label}: {len(these)} G", _pos=pos, _kind="total")
                out.append(tot)
            continue
        keys = {
            "hitting": ("plateAppearances", "atBats", "runs", "hits", "doubles", "triples", "homeRuns", "rbi",
                        "baseOnBalls", "intentionalWalks", "strikeOuts", "hitByPitch", "sacBunts", "sacFlies",
                        "groundIntoDoublePlay", "leftOnBase"),
            "running": ("stolenBases", "caughtStealing", "runs", "groundIntoDoublePlay"),
            "pitching": ("gamesStarted", "hits", "runs", "earnedRuns", "homeRuns", "baseOnBalls", "strikeOuts",
                         "hitBatsmen", "wildPitches", "battersFaced", "numberOfPitches", "strikes"),
        }[role]
        tot = _sum_lines(part, keys)
        tot.update(_date=f"{label}: {len(part)} G", _kind="total")
        if role == "hitting":
            _batting_rates(tot, acc)
        elif role == "running":
            tot["_on_base"] = sum(r["_on_base"] for r in out if r["_kind"] == "game" and (r["_type"] != "R") == post)
            _running_rates(tot, acc)
        else:
            tot["inningsPitched"] = _daily_ip(int(acc.get("outs", 0)))
            tot["_dec"] = ", ".join(f"{int(n)} {tag}" for tag, n in (
                ("W", sum(_daily_n(r, "wins") for r in part)), ("L", sum(_daily_n(r, "losses") for r in part)),
                ("SV", sum(_daily_n(r, "saves") for r in part))) if n)
            _pitching_rates(tot, acc)
        out.append(tot)
    return out


def _sum_lines(rows: list[dict], keys) -> dict:
    """Column sums over ``rows``, left blank for a stat none of them carries (an era without it)."""
    tot = {}
    for k in keys:
        if any(k in r for r in rows):
            tot[k] = int(sum(_daily_n(r, k) for r in rows))
    return tot


def _batting_rates(line: dict, acc: dict):
    ab, h, bb, hbp, sf, tb = (acc.get(k, 0) for k in ("atBats", "hits", "baseOnBalls", "hitByPitch",
                                                      "sacFlies", "totalBases"))
    line["_avg"] = _daily_rate(h, ab)
    line["_obp"] = _daily_rate(h + bb + hbp, ab + bb + hbp + sf)
    line["_slg"] = _daily_rate(tb, ab)
    if ab and (ab + bb + hbp + sf):
        line["_ops"] = _daily_rate((h + bb + hbp) / (ab + bb + hbp + sf) + tb / ab, 1)


def _running_rates(line: dict, acc: dict):
    sb, cs = int(acc.get("sb", 0)), int(acc.get("cs", 0))
    line["_sb"], line["_cs"], line["_sb_pct"] = sb, cs, _daily_rate(sb, sb + cs)


def _pitching_rates(line: dict, acc: dict):
    outs = acc.get("outs", 0)
    line["_era"] = _daily_rate(acc.get("earnedRuns", 0) * 27, outs, 2)
    line["_whip"] = _daily_rate((acc.get("hits", 0) + acc.get("baseOnBalls", 0)) * 3, outs, 2)

def search_people(name: str, ttl: float | None = 6 * 3600) -> list[dict]:
    """Players whose name matches ``name`` (any era), see :func:`parse_people`: MLB's name search
    and the players of :func:`_people_index` it missed, then anyone Wikidata knows by that name
    and neither offered (:func:`_wikidata_names`: "Pete
    Alexander", "The Big Train" -- each word the start of a word of the name).  MLB's search
    misses some players it has on file (Turkey Stearnes, Oscar Charleston and "Tris" Speaker among
    them), so when neither finds anybody, :func:`_people_index` -- everyone in MLB's major-league
    career lists -- is searched (each word part of a name); and if that finds nobody either,
    :func:`_full_name_index`, which has everyone's given and middle names and MLB's nicknames
    ("Grover Cleveland Alexander", "Norman Stearnes", "Three Finger Brown")."""
    people = parse_people(api_get("/people/search", {"names": name, "sportIds": SPORT_ID}, ttl=ttl))
    words = _search_key(name).split()
    if not words:
        return people
    shown = {p["id"] for p in people}
    known_as = _known_as(words, shown)
    # players MLB's search missed though their names match (it gives Turkey Gross for "Turkey",
    # not Turkey Stearnes): from everyone in the career lists, each word starting a word of the name
    missed = [pid for pid, key in _people_index().items()
              if pid not in shown and pid not in known_as
              and all(any(part.startswith(word) for part in key.split()) for word in words)][:25]
    if missed:
        people += parse_people(api_get("/people", {"personIds": ",".join(map(str, sorted(missed)))}, ttl=ttl))
    if people or known_as:
        if known_as:
            found = parse_people(api_get("/people", {"personIds": ",".join(map(str, known_as))}, ttl=ttl))
            for person in found:
                person["aka"] = known_as[person["id"]]
            # a nickname MLB's search can't match ("Pete" for Grover Alexander) leads the list; the
            # other names Wikidata matched (a middle name Peter, for "Pete") follow MLB's own matches
            def nickname(person) -> bool:
                name_words = _search_key(person["name"]).split()
                in_name = all(any(part.startswith(word) for part in name_words) for word in words)
                return not in_name and all(word in _search_key(person["aka"]).split() for word in words)
            people = [p for p in found if nickname(p)] + people + [p for p in found if not nickname(p)]
        return people
    found = [pid for pid, key in _people_index().items() if all(word in key for word in words)][:50]
    if not found:
        found = [pid for pid, key in _full_name_index().items() if all(word in key for word in words)][:50]
    if not found:
        return []
    return parse_people(api_get("/people", {"personIds": ",".join(map(str, sorted(found)))}, ttl=ttl))


def _search_key(text: str) -> str:
    """``text`` lower-cased without accents, for matching names ("José" finds "Jose")."""
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)).lower()


_PEOPLE_INDEX: dict | None = None


def _people_index(ttl: float | None = 7 * 86400) -> dict[int, str]:
    """``{person id: searchable name}`` for everyone who batted or pitched in a league MLB counts
    as major, from each league's career lists (fetched once, then kept a week) -- the full name
    and the given first and last names ("Turkey Stearnes norman stearnes")."""
    global _PEOPLE_INDEX
    if _PEOPLE_INDEX is None:
        index = {}
        for group in ("hitting", "pitching"):
            for lid, _abbr, _name in LEADER_LEAGUES:
                for row in _bulk_rows({"stats": "career", "group": group, "sportId": SPORT_ID,
                                       "playerPool": "ALL", "leagueId": lid}, ttl):
                    person = row.get("player") or {}
                    if person.get("id") is not None and person["id"] not in index:
                        index[person["id"]] = _search_key(" ".join(
                            person.get(k) or "" for k in ("fullName", "firstName", "lastName")))
        _PEOPLE_INDEX = index
    return _PEOPLE_INDEX


def _known_as(words: list[str], shown=()) -> dict[int, str]:
    """``{MLB person id: the Wikidata name that matched}`` for up to 25 people Wikidata knows by
    a name whose words start with ``words`` (whole words preferred), leaving out ``shown``."""
    names = _wikidata_names()
    raw = _WIKIDATA_RAW or {}
    out = []
    for pid, key in names.items():
        if pid in shown or not all(any(part.startswith(word) for part in key.split()) for word in words):
            continue
        matching = [n for n in raw.get(pid, []) if all(
            any(part.startswith(word) for part in _search_key(n).split()) for word in words)]
        aka = min(matching, key=lambda n: (not all(word in _search_key(n).split() for word in words), len(n))) \
            if matching else ""
        out.append((not all(word in _search_key(aka).split() for word in words), pid, aka))
    out.sort()
    return {pid: aka for _partial, pid, aka in out[:25]}


_FULL_NAME_INDEX: dict | None = None
WIKIDATA_NAMES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wikidata_names.json")
_WIKIDATA_NAMES: dict | None = None
_WIKIDATA_RAW: dict | None = None     # {MLB person id: [his English name, aliases, nicknames]}


def _wikidata_names() -> dict[int, str]:
    """``{MLB person id: searchable names}`` from ``wikidata_names.json`` -- the English name,
    aliases and nicknames of every Wikidata person with an MLB player ID, collected once
    (Wikidata is CC0, public domain).  Empty if the file is missing."""
    global _WIKIDATA_NAMES, _WIKIDATA_RAW
    if _WIKIDATA_NAMES is None:
        try:
            with open(WIKIDATA_NAMES_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        _WIKIDATA_RAW = {int(mlb): [n for n in [entry.get("label")] + entry.get("names", []) if n]
                         for mlb, entry in data.items() if str(mlb).isdigit()}
        _WIKIDATA_NAMES = {pid: _search_key(" ".join(names)) for pid, names in _WIKIDATA_RAW.items()}
    return _WIKIDATA_NAMES


def _full_name_index(ttl: float | None = 7 * 86400) -> dict[int, str]:
    """:func:`_people_index` with each player's names as his biography gives them -- given,
    middle and used names ("Norman Thomas Stearnes", "Grover Cleveland Alexander") -- read 600
    players to a request, side by side (fetched once, then kept a week)."""
    global _FULL_NAME_INDEX
    if _FULL_NAME_INDEX is None:
        base = _people_index(ttl)
        ids = sorted(base)
        batches = [ids[i:i + 600] for i in range(0, len(ids), 600)]
        index = dict(base)
        with ThreadPoolExecutor(max_workers=6) as pool:
            for payload in pool.map(lambda batch: api_get(
                    "/people", {"personIds": ",".join(map(str, batch))}, ttl=ttl), batches):
                for person in payload.get("people") or []:
                    if person.get("id") in index:
                        index[person["id"]] += " " + _search_key(" ".join(
                            person.get(k) or "" for k in ("fullFMLName", "useName", "nickName")))
        _FULL_NAME_INDEX = index
    return _FULL_NAME_INDEX


def get_person(person_id: int, ttl: float | None = None) -> dict:
    payload = api_get(f"/people/{person_id}", {}, ttl=ttl)
    people = payload.get("people") or []
    return parse_person(people[0]) if people else {}


def get_person_stats(person_id: int, group: str, ttl: float | None = None,
                     advanced: bool = False) -> dict[str, list[dict]]:
    """One player's MLB year-by-year and career lines for one stat ``group``
    ("hitting", "pitching" or "fielding") -- see :func:`parse_person_splits`.
    ``advanced`` asks for the Advanced set instead of the standard one (there is
    none for fielding).  The current season is in the response, so callers
    should pass a ``ttl``."""
    types = "yearByYearAdvanced,careerAdvanced" if advanced else "yearByYear,career"
    payload = api_get(
        f"/people/{person_id}/stats",
        {"stats": types, "group": group, "sportId": SPORT_ID},
        ttl=ttl,
    )
    return parse_person_splits(payload)


def get_person_season_stats(person_id: int, group: str, stat_type: str, seasons: list,
                            ttl_for=lambda season: None, workers: int = 6) -> dict[str, list[dict]]:
    """A stat set the API only serves one season at a time ("sabermetrics",
    "expectedStatistics"): one request per season in ``seasons`` (fetched
    ``workers`` at a time), merged into ``{"season": [...], "career": []}`` --
    there is no career line for these.  ``ttl_for(season)`` picks each request's
    cache lifetime, so finished seasons can be cached for good."""
    def one(season):
        payload = api_get(
            f"/people/{person_id}/stats",
            {"stats": stat_type, "group": group, "season": season, "sportId": SPORT_ID},
            ttl=ttl_for(int(season)),
        )
        return parse_person_splits(payload)["season"]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        per_season = list(pool.map(one, seasons))
    rows = [row for chunk in per_season for row in chunk]
    # By season; within one, team lines before the all-teams total.
    rows.sort(key=lambda r: (r["_season"], r["_kind"] == "total", r["_team"]))
    return {"season": rows, "career": []}


def get_league_position_totals(season: int, ttl: float | None = None) -> dict[str, dict]:
    """What all of MLB did at each of the nine positions in ``season`` --
    ``{position: rf_figures}`` (see :func:`aggregate_league_positions`).  One
    request returns the whole league's fielding rows; it's checked against the
    reported total so a truncated list can't quietly skew the figures."""
    payload = api_get(
        "/stats",
        {"stats": "season", "group": "fielding", "season": season, "sportId": SPORT_ID,
         "playerPool": "all", "limit": 5000},
        ttl=ttl,
    )
    rows = parse_player_rows(payload)
    stats = payload.get("stats") or [{}]
    total = stats[0].get("totalSplits")
    if total is not None and len(rows) < total:
        raise MLBStatsError(f"the {season} league fielding list came back truncated "
                            f"({len(rows)} of {total} rows)")
    return aggregate_league_positions(rows)


def get_batting_order(team_id: int, season: int, ttl: float | None = None) -> dict[int, dict[int, dict]]:
    """How often each of a team's players batted from each lineup place in ``season`` --
    see :func:`parse_batting_order`.  One request covers all nine places.  The API
    has these splits from 1950 on; before that the result is empty."""
    payload = api_get(
        "/stats",
        {"stats": "statSplits", "group": "hitting", "season": season, "sportId": SPORT_ID,
         "teamId": team_id, "playerPool": "all", "sitCodes": BATTING_ORDER_SITCODES,
         "limit": 5000},
        ttl=ttl,
    )
    return parse_batting_order(payload)


# The Simulator's platoon splits: the API's situation codes vl / vr ("vs Left" / "vs Right") split a batter's season
# by the pitcher's hand and a pitcher's by the batter's side.  The API has them from 1950 on.
PLATOON_SITCODES = "vl,vr"
PLATOON_FROM = 1950


def parse_platoon_splits(payload: dict) -> dict:
    """A team-season's ``statSplits`` response for the vl / vr codes -> ``{player id: {"L": stat row, "R": stat
    row}}`` -- a batter's line against left- and right-handed pitchers, or a pitcher's against left- and
    right-side batters.  A side he never faced has no entry."""
    out: dict = {}
    for section in payload.get("stats", []):
        for split in section.get("splits", []):
            code = (split.get("split") or {}).get("code") or ""
            pid = (split.get("player") or {}).get("id")
            if pid is None or code not in ("vl", "vr"):
                continue
            out.setdefault(pid, {})[code[1].upper()] = dict(split.get("stat") or {})
    return out


def get_platoon_splits(team_id: int, season: int, group: str, ttl: float | None = None) -> dict:
    """Each of a team's players' lines split by the other side's hand (``group`` "hitting" or "pitching") --
    see :func:`parse_platoon_splits`; empty before 1950."""
    if season < PLATOON_FROM:
        return {}
    payload = api_get(
        "/stats",
        {"stats": "statSplits", "group": group, "season": season, "sportId": SPORT_ID,
         "teamId": team_id, "playerPool": "all", "sitCodes": PLATOON_SITCODES, "limit": 5000},
        ttl=ttl,
    )
    return parse_platoon_splits(payload)


def get_hands(person_ids, ttl: float | None = 30 * 86400) -> dict:
    """``{player id: {"bats": "L"/"R"/"S", "throws": "L"/"R"/"S"}}`` (an empty string where unknown), 600 players
    to a request."""
    ids = sorted({int(p) for p in person_ids if isinstance(p, int)})
    out = {}
    for i in range(0, len(ids), 600):
        payload = api_get("/people", {"personIds": ",".join(map(str, ids[i:i + 600]))}, ttl=ttl)
        for person in payload.get("people") or []:
            if person.get("id") is not None:
                out[person["id"]] = {"bats": (person.get("batSide") or {}).get("code", ""),
                                     "throws": (person.get("pitchHand") or {}).get("code", "")}
    return out


def _formula_views(ftext: str) -> set[str]:
    """The player-list views a custom-stat formula's variables come from."""
    return {REGRESSION_VARIABLE_INDEX[v][0] for v in formula.formula_variables(ftext)}


def formula_views(ftexts, always=()) -> set[str]:
    """The views needed by several formulas together, plus any in ``always``."""
    views = set(always)
    for ftext in ftexts:
        views |= _formula_views(ftext)
    return views


def team_view_groups(team_id: int, season: int, views, ttl: float | None = None) -> dict:
    """``{view: {player id: rows}}`` for one team-season, one request per view."""
    groups = {}
    for view in views:
        group, stat_type, _cols = VIEW_SPECS[view]
        groups[view] = group_by_player(get_player_stats(team_id, season, group, ttl, stat_type=stat_type))
    return groups


def get_team_production(team_id: int, season: int, ftext: str,
                        ttl: float | None = None) -> dict[int, float | None]:
    """A player-level custom stat evaluated for every player of one team-season
    -> ``{player id: value or None}``."""
    groups = {}
    for view in _formula_views(ftext):
        group, stat_type, _cols = VIEW_SPECS[view]
        groups[view] = group_by_player(get_player_stats(team_id, season, group, ttl,
                                                        stat_type=stat_type))
    return production_values(ftext, groups, {pid for g in groups.values() for pid in g})


def get_league_production(season: int, ftext: str, ttl: float | None = None) -> dict:
    """The same custom stat over all of MLB in ``season``, per plate appearance -- see
    :func:`league_production`.  One league-wide request per view the formula uses (plus
    Batting, for the plate appearances); each is checked against the reported total so
    a truncated list can't quietly skew the average."""
    groups = {view: group_by_player(_league_view_rows(season, view, ttl))
              for view in _formula_views(ftext) | {"Batting"}}
    batting = groups["Batting"]
    return league_production(
        production_values(ftext, groups, batting),
        {pid: aggregate_rows(rows, "plateAppearances", "sum") for pid, rows in batting.items()})


# Intergroup tab: the leagues split into divisions in 1969, and regular-season play
# between the leagues began in 1997.
INTERDIVISION_FIRST_SEASON = 1969
INTERLEAGUE_FIRST_SEASON = 1997
_DIVISION_ORDER = {"East": 0, "Central": 1, "West": 2}


def parse_league_records(payload: dict) -> dict:
    """A season's ``/standings`` response -> how the leagues played each other and how each
    division did against the rest of its own league.  Every team's record carries its
    win-loss against each league (``leagueRecords``) and against each division of its own
    league (``divisionRecords``); summing those over the teams gives:

    * ``"leagues"``       -- ``{league id: name}``, in id order (the American League first)
    * ``"interleague"``   -- ``{league id: (W, L)}``, that league's teams against the other league
    * ``"interdivision"`` -- ``[{"id", "name", "league", "w", "l", "il_w", "il_l"}, ...]``,
                             each division's teams against the *other* divisions of their
                             league (``w``, ``l`` -- their games against their own division
                             are left out) and against the other league's divisions
                             (``il_w``, ``il_l`` -- MLB splits those only by league, not by
                             the other league's divisions), East to West, American League first
    * ``"teams"``         -- ``{team id: (division id, W, L)}``, each team's division and
                             overall record
    * ``"incomplete"``    -- how many teams' league records don't add up to their overall
                             record (MLB's splits for 1981, the strike-split season, are
                             missing games for 14 teams), so the totals undercount

    (A league's interdivision games as a whole would always come to W == L, every game being
    counted from both sides; a single division's don't.)"""
    leagues: dict[int, str] = {}
    interleague: dict[int, list] = {}
    divisions: dict[int, dict] = {}
    teams: dict[int, tuple] = {}
    incomplete = 0
    for record in payload.get("records", []):
        league_id = (record.get("league") or {}).get("id")
        division_id = (record.get("division") or {}).get("id")
        entry = divisions.setdefault(division_id, {
            "id": division_id, "name": "", "league": league_id, "w": 0, "l": 0, "il_w": 0, "il_l": 0})
        for team in record.get("teamRecords", []):
            records = team.get("records") or {}
            team_id = (team.get("team") or {}).get("id")
            if team_id is not None:
                teams[team_id] = (division_id, int(team.get("wins") or 0), int(team.get("losses") or 0))
            by_league = records.get("leagueRecords") or []
            if team.get("wins") is not None and (
                    sum(int(x.get("wins") or 0) for x in by_league) != int(team["wins"])
                    or sum(int(x.get("losses") or 0) for x in by_league) != int(team.get("losses") or 0)):
                incomplete += 1
            for item in by_league:
                other = item.get("league") or {}
                if other.get("id") is None:
                    continue
                leagues.setdefault(other["id"], other.get("name", ""))
                if other["id"] != league_id:
                    w_l = interleague.setdefault(league_id, [0, 0])
                    w_l[0] += int(item.get("wins") or 0)
                    w_l[1] += int(item.get("losses") or 0)
                    entry["il_w"] += int(item.get("wins") or 0)
                    entry["il_l"] += int(item.get("losses") or 0)
            for item in records.get("divisionRecords") or []:
                division = item.get("division") or {}
                if division.get("id") is None:
                    continue
                if division["id"] == division_id:
                    entry["name"] = division.get("name", "")
                else:
                    entry["w"] += int(item.get("wins") or 0)
                    entry["l"] += int(item.get("losses") or 0)
    ordered = sorted((d for d in divisions.values() if d["name"]), key=lambda d: (
        d["league"] or 0, _DIVISION_ORDER.get(d["name"].split()[-1], 3), d["name"]))
    return {
        "leagues": dict(sorted(leagues.items())),
        "interleague": {k: tuple(v) for k, v in sorted(interleague.items())},
        "interdivision": ordered,
        "teams": teams,
        "incomplete": incomplete,
    }


def merge_league_records(first: dict, second: dict) -> dict:
    """Two :func:`parse_league_records` results added together (the two halves of a split season)."""
    interleague = {}
    for part in (first["interleague"], second["interleague"]):
        for lid, (w, l) in part.items():
            old_w, old_l = interleague.get(lid, (0, 0))
            interleague[lid] = (old_w + w, old_l + l)
    divisions: dict[int, dict] = {}
    for part in (first["interdivision"], second["interdivision"]):
        for d in part:
            entry = divisions.setdefault(d["id"], dict(d, w=0, l=0, il_w=0, il_l=0))
            for key in ("w", "l", "il_w", "il_l"):
                entry[key] += d[key]
    return {
        "leagues": dict(sorted({**first["leagues"], **second["leagues"]}.items())),
        "interleague": dict(sorted(interleague.items())),
        "interdivision": sorted(divisions.values(), key=lambda d: (
            d["league"] or 0, _DIVISION_ORDER.get(d["name"].split()[-1], 3), d["name"])),
        "teams": {tid: (div, w + second.get("teams", {}).get(tid, (div, 0, 0))[1],
                        l + second.get("teams", {}).get(tid, (div, 0, 0))[2])
                  for tid, (div, w, l) in first.get("teams", {}).items()},
        "incomplete": first["incomplete"] + second["incomplete"],
    }


def get_league_records(season: int, ttl: float | None = None) -> dict:
    """:func:`parse_league_records` for ``season`` -- one standings request.  In 1981, the
    strike-split season, the whole-season records against each league and division are
    missing games for 14 teams; the two halves' standings are complete, so when the whole
    season doesn't add up they're fetched and added together instead (``"halves": True``)."""
    def standings(kind: str) -> dict:
        return parse_league_records(api_get(
            "/standings",
            {"leagueId": "103,104", "season": season, "standingsTypes": kind}, ttl=ttl))

    records = standings("regularSeason")
    records["halves"] = False
    if records["incomplete"]:
        try:
            merged = merge_league_records(standings("firstHalf"), standings("secondHalf"))
        except MLBStatsError:
            return records
        if not merged["incomplete"]:
            merged["halves"] = True
            return merged
    return records


def parse_division_matrix(payload: dict, teams: dict) -> dict:
    """A season's regular-season ``/schedule`` response -> how every division did against every
    other, across both leagues -- something the standings can't say, splitting a team's games
    against the other league only by league.  ``teams`` is :func:`parse_league_records`'s
    ``{team id: (division id, W, L)}``.  Returns

    * ``"cells"``   -- ``{(division id, opponent's division id): (W, L)}`` from the first
                       division's side, so a division's row adds up to its teams' record and its
                       games against itself come out even
    * ``"missing"`` -- how many team-games the standings have that the schedule doesn't (MLB's
                       1981 schedule is missing one), so the cells undercount by that much

    Games are counted once each (a suspended game can be listed again when it's resumed);
    postponed, cancelled, unfinished and tied games aren't counted."""
    cells: dict[tuple, list] = {}
    seen = set()
    counted: dict[int, int] = {}
    for day in payload.get("dates", []):
        for g in day.get("games", []):
            if (g.get("status") or {}).get("abstractGameState") != "Final" or g.get("gamePk") in seen:
                continue
            sides = [(g.get("teams") or {}).get(side) or {} for side in ("away", "home")]
            winners = [side for side in sides if side.get("isWinner")]
            if len(winners) != 1:
                continue
            winner = winners[0]
            loser = sides[1] if winner is sides[0] else sides[0]
            w_id, l_id = (winner.get("team") or {}).get("id"), (loser.get("team") or {}).get("id")
            if w_id not in teams or l_id not in teams:
                continue
            seen.add(g.get("gamePk"))
            w_div, l_div = teams[w_id][0], teams[l_id][0]
            cells.setdefault((w_div, l_div), [0, 0])[0] += 1
            cells.setdefault((l_div, w_div), [0, 0])[1] += 1
            for tid in (w_id, l_id):
                counted[tid] = counted.get(tid, 0) + 1
    missing = sum(max(0, w + l - counted.get(tid, 0)) for tid, (_d, w, l) in teams.items())
    return {"cells": {k: tuple(v) for k, v in cells.items()}, "missing": missing}


def get_division_matrix(season: int, records: dict, ttl: float | None = None) -> dict:
    """:func:`parse_division_matrix` for ``season``, ``records`` being its :func:`get_league_records`."""
    return parse_division_matrix(api_get(
        "/schedule", {"sportId": SPORT_ID, "season": season, "gameType": "R"}, ttl=ttl), records["teams"])


# Leaders tab: MLB's own leaderboards (``/stats/leaders``) for the statistics the Stats tab
# shows -- a season's leaders, the best single seasons ever, and career leaders.  Each entry is
# (Stats tab header, name, MLB's leader category, the kinds of board it works for): "season"
# (one year), "single" (all-time single seasons) and "career".  Left out where MLB's boards
# don't hold up: extra-base hits have no all-time boards, fielding innings and range factor
# are only recorded for recent seasons, so their all-time boards are wrong, and fielding
# percentage has no playing-time minimum except within one season at one position.
LEADER_KINDS = ("season", "single", "career")
_ALL_KINDS = frozenset(LEADER_KINDS)
_ONE_SEASON = frozenset({"season"})
LEADER_STATS = {
    "hitting": [
        ("G", "Games", "gamesPlayed", _ALL_KINDS),
        ("PA", "Plate appearances", "totalPlateAppearances", _ALL_KINDS),
        ("AB", "At bats", "atBats", _ALL_KINDS),
        ("R", "Runs", "runs", _ALL_KINDS),
        ("H", "Hits", "hits", _ALL_KINDS),
        ("2B", "Doubles", "doubles", _ALL_KINDS),
        ("3B", "Triples", "triples", _ALL_KINDS),
        ("HR", "Home runs", "homeRuns", _ALL_KINDS),
        ("RBI", "Runs batted in", "runsBattedIn", _ALL_KINDS),
        ("BB", "Walks", "walks", _ALL_KINDS),
        ("SO", "Strikeouts", "strikeouts", _ALL_KINDS),
        ("SB", "Stolen bases", "stolenBases", _ALL_KINDS),
        ("CS", "Caught stealing", "caughtStealing", _ALL_KINDS),
        ("AVG", "Batting average", "battingAverage", _ALL_KINDS),
        ("OBP", "On-base percentage", "onBasePercentage", _ALL_KINDS),
        ("SLG", "Slugging percentage", "sluggingPercentage", _ALL_KINDS),
        ("OPS", "On-base plus slugging", "onBasePlusSlugging", _ALL_KINDS),
        ("TB", "Total bases", "totalBases", _ALL_KINDS),
        ("XBH", "Extra-base hits", "extraBaseHits", _ONE_SEASON),
        ("HBP", "Hit by pitch", "hitByPitches", _ALL_KINDS),
        ("SF", "Sacrifice flies", "sacrificeFlies", _ALL_KINDS),
        ("SH", "Sacrifice bunts", "sacrificeBunts", _ALL_KINDS),
        ("GIDP", "Grounded into double plays", "groundIntoDoublePlays", _ALL_KINDS),
        ("Pitches", "Pitches seen", "numberOfPitches", _ALL_KINDS),
    ],
    "pitching": [
        ("W", "Wins", "wins", _ALL_KINDS),
        ("L", "Losses", "losses", _ALL_KINDS),
        ("ERA", "Earned run average", "earnedRunAverage", _ALL_KINDS),
        ("G", "Games", "gamesPlayed", _ALL_KINDS),
        ("GS", "Games started", "gamesStarted", _ALL_KINDS),
        ("CG", "Complete games", "completeGames", _ALL_KINDS),
        ("SHO", "Shutouts", "shutouts", _ALL_KINDS),
        ("SV", "Saves", "saves", _ALL_KINDS),
        ("IP", "Innings pitched", "inningsPitched", _ALL_KINDS),
        ("H", "Hits allowed", "hits", _ALL_KINDS),
        ("R", "Runs allowed", "runs", _ALL_KINDS),
        ("ER", "Earned runs", "earnedRun", _ALL_KINDS),
        ("HR", "Home runs allowed", "homeRuns", _ALL_KINDS),
        ("BB", "Walks", "walks", _ALL_KINDS),
        ("SO", "Strikeouts", "strikeouts", _ALL_KINDS),
        ("WHIP", "Walks and hits per inning", "walksAndHitsPerInningPitched", _ALL_KINDS),
        ("K/9", "Strikeouts per nine innings", "strikeoutsPer9Inn", _ALL_KINDS),
        ("BB/9", "Walks per nine innings", "walksPer9Inn", _ALL_KINDS),
        ("H/9", "Hits per nine innings", "hitsPer9Inn", _ALL_KINDS),
        ("K/BB", "Strikeouts per walk", "strikeoutWalkRatio", _ALL_KINDS),
        ("BF", "Batters faced", "totalBattersFaced", _ALL_KINDS),
        ("WP", "Wild pitches", "wildPitch", _ALL_KINDS),
        ("Balks", "Balks", "balk", _ALL_KINDS),
    ],
    "fielding": [
        ("G", "Games", "gamesPlayed", _ALL_KINDS),
        ("GS", "Games started", "gamesStarted", _ALL_KINDS),
        ("Inn", "Innings", "innings", _ONE_SEASON),
        ("PO", "Putouts", "putOuts", _ALL_KINDS),
        ("A", "Assists", "assists", _ALL_KINDS),
        ("E", "Errors", "errors", _ALL_KINDS),
        ("DP", "Double plays", "doublePlays", _ALL_KINDS),
        ("FLD%", "Fielding percentage", "fieldingPercentage", _ONE_SEASON),
        ("RF/9", "Range factor per nine innings", "rangeFactorPer9Inn", _ONE_SEASON),
        ("PB", "Passed balls", "passedBalls", _ALL_KINDS),
        ("CS", "Runners caught stealing", "caughtStealing", _ALL_KINDS),
        ("CS%", "Catchers\u2019 caught stealing", "caughtStealingPercentage", _ALL_KINDS),
    ],
}
# fielding percentage means something only at one position (else pitchers with a handful of
# chances and no errors top it)
LEADERS_NEED_POSITION = {"fieldingPercentage"}
# The leagues MLB counts as major, as the Leaders tab abbreviates them: (id, abbreviation, name),
# the American and National first, then the 19th-century majors, the Federal League and the
# Negro leagues.  MLB's own abbreviations, except that its two Negro National Leagues ("NNL",
# "NN2") are told apart as I and II.
LEADER_LEAGUES = (
    (103, "AL", "American League"), (104, "NL", "National League"),
    (100, "AA", "American Association"), (101, "UA", "Union Association"), (105, "PL", "Players' League"),
    (106, "FL", "Federal League"),
    (430, "NNL I", "Negro National League (I)"), (427, "ECL", "Eastern Colored League"),
    (426, "ANL", "American Negro League"), (429, "EWL", "East-West League"),
    (432, "NSL", "Negro Southern League"), (431, "NNL II", "Negro National League (II)"),
    (428, "NAL", "Negro American League"),
)
LEAGUE_ABBREVIATIONS = {lid: abbr for lid, abbr, _name in LEADER_LEAGUES}
# The seasons each league played (from MLB's standings, the Federal League's from its history):
# a one-year board asked for with a league that didn't play that year fails at MLB's end.
LEAGUE_SEASONS = {103: (1901, None), 104: (1876, None), 100: (1882, 1891), 101: (1884, 1884),
                  105: (1890, 1890), 106: (1914, 1915), 430: (1920, 1931), 427: (1923, 1928),
                  426: (1929, 1929), 429: (1932, 1932), 432: (1932, 1932), 431: (1933, 1948),
                  428: (1937, 1948)}


def leagues_in_era(leagues, era: str | None) -> list[int]:
    """Those of ``leagues`` (None: every major league) that played in ``era`` (None: any)."""
    ids = [lid for lid, _a, _n in LEADER_LEAGUES] if leagues is None else leagues
    if era == "modern":
        return [lid for lid in ids if lid not in _PRE_1901_LEAGUES]
    if era == "early":
        return [lid for lid in ids if lid in _EARLY_LEAGUES]
    return list(ids)


def leagues_playing(leagues, season: int) -> list[int]:
    """Those of ``leagues`` (None: every major league) that played in ``season``."""
    ids = [lid for lid, _a, _n in LEADER_LEAGUES] if leagues is None else leagues
    return [lid for lid in ids if lid in LEAGUE_SEASONS and LEAGUE_SEASONS[lid][0] <= season
            and (LEAGUE_SEASONS[lid][1] is None or season <= LEAGUE_SEASONS[lid][1])]
MODERN_FROM = 1901                      # the "1901 on" era starts here, the AL's first season
LEADER_ERAS = (None, "modern", "early")  # all years, 1901 on, before 1901
_PRE_1901_LEAGUES = frozenset({100, 101, 105})   # leagues that played only before 1901
_EARLY_LEAGUES = frozenset({104, 100, 101, 105})  # the leagues that played before 1901
CAREER_MIN_PA = 3000                    # a career average needs this many plate appearances ...
CAREER_MIN_OUTS = 3000                  # ... a career pitching rate this many outs (1,000 innings)
# A leader category's field in a /stats row, whether lower is better, and -- for an average or
# rate -- how to work it out again from a career's added-up totals.
_LEADER_FIELDS = {
    "totalPlateAppearances": "plateAppearances", "runsBattedIn": "rbi", "walks": "baseOnBalls",
    "strikeouts": "strikeOuts", "battingAverage": "avg", "onBasePercentage": "obp",
    "sluggingPercentage": "slg", "onBasePlusSlugging": "ops", "hitByPitches": "hitByPitch",
    "sacrificeFlies": "sacFlies", "sacrificeBunts": "sacBunts", "groundIntoDoublePlays": "groundIntoDoublePlay",
    "earnedRunAverage": "era", "earnedRun": "earnedRuns", "walksAndHitsPerInningPitched": "whip",
    "totalBattersFaced": "battersFaced", "wildPitch": "wildPitches", "balk": "balks", "passedBalls": "passedBall",
}
_LOWER_IS_BETTER = {"earnedRunAverage", "walksAndHitsPerInningPitched", "walksPer9Inn", "hitsPer9Inn"}


def _ratio(top, bottom, per=1.0):
    return per * top / bottom if bottom else None


_CAREER_RATES = {   # category -> (value from totals t, digits shown, needs plate appearances (else outs))
    "battingAverage": (lambda t: _ratio(t["hits"], t["atBats"]), 3, True),
    "onBasePercentage": (lambda t: _ratio(t["hits"] + t["baseOnBalls"] + t["hitByPitch"],
                                          t["atBats"] + t["baseOnBalls"] + t["hitByPitch"] + t["sacFlies"]), 3, True),
    "sluggingPercentage": (lambda t: _ratio(t["totalBases"], t["atBats"]), 3, True),
    "onBasePlusSlugging": (lambda t: None if not t["atBats"] else
                           _CAREER_RATES["onBasePercentage"][0](t) + _CAREER_RATES["sluggingPercentage"][0](t), 3, True),
    "earnedRunAverage": (lambda t: _ratio(t["earnedRuns"], t["outs"], 27), 2, False),
    "walksAndHitsPerInningPitched": (lambda t: _ratio(t["baseOnBalls"] + t["hits"], t["outs"], 3), 2, False),
    "strikeoutsPer9Inn": (lambda t: _ratio(t["strikeOuts"], t["outs"], 27), 2, False),
    "walksPer9Inn": (lambda t: _ratio(t["baseOnBalls"], t["outs"], 27), 2, False),
    "hitsPer9Inn": (lambda t: _ratio(t["hits"], t["outs"], 27), 2, False),
    "strikeoutWalkRatio": (lambda t: _ratio(t["strikeOuts"], t["baseOnBalls"]), 2, False),
}
_RATE_CATEGORIES = set(_CAREER_RATES) | {"fieldingPercentage", "rangeFactorPer9Inn"}
# a fielder's games are counted at each position he played, so an all-positions career total of
# them would count a game twice: added-up fielding careers need a position for these
CAREER_FIELDING_NEED_POSITION = {"gamesPlayed", "gamesStarted"}
LEADER_POSITIONS = ("P", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF")
LEADER_LIMIT = 100   # the most MLB will rank on one board (ties at the bottom can add a few)
_LEADER_STAT_TYPES = {"season": "season", "single": "statsSingleSeason", "career": "career"}


def parse_leaders(payload: dict) -> list[dict]:
    """A ``/stats/leaders`` response for one category -> its board, best first:
    ``{"rank", "pid", "name", "season", "team", "league_id", "league", "value"}``.  ``season`` is None
    on a career board, whose ``team`` is the player's last; ``value`` is MLB's text (".372",
    "208.2").  Tied players share a rank."""
    boards = payload.get("leagueLeaders") or []
    out = []
    for leader in (boards[0].get("leaders") or []) if boards else []:
        person = leader.get("person") or {}
        out.append({
            "rank": leader.get("rank"),
            "pid": person.get("id"),
            "name": person.get("fullName") or "?",
            "season": leader.get("season"),
            "team": (leader.get("team") or {}).get("name") or "",
            "league_id": (leader.get("league") or {}).get("id"),
            "league": (leader.get("league") or {}).get("name") or "",
            "value": leader.get("value") or "",
        })
    return out


def leaders_assembled(kind: str, leagues, era: str | None) -> bool:
    """Whether a board has to be put together here rather than taken from MLB's: MLB's boards
    can't keep to one era (1901 on, or before 1901), and its career boards can't leave out
    leagues (it ranks each league's careers separately rather than adding them up)."""
    return kind != "season" and (era is not None or (kind == "career" and leagues is not None))


def get_leaders(group: str, category: str, kind: str, season: int | None = None,
                leagues=None, position: str | None = None, limit: int = 25,
                ttl: float | None = None, era: str | None = None,
                min_attempts: int | None = None) -> list[dict]:
    """One leaderboard, best first, as :func:`parse_leaders` gives it: ``group``
    hitting/pitching/fielding, ``category`` one of :data:`LEADER_STATS`'s, ``kind`` one of
    :data:`LEADER_KINDS` (``season`` needing the year); ``leagues`` the league ids to count (None:
    every league MLB counts as major); fielding optionally at one ``position``; ``era`` keeps to
    the seasons from 1901 on ("modern") or before it ("early"), None for all.  Regular season only.  MLB's own board where it has one
    (:func:`leaders_assembled`); otherwise :func:`_assembled_single` or
    :func:`_assembled_career`; catchers' caught stealing percentage from :func:`get_catcher_cs`,
    for those tried against ``min_attempts`` times."""
    limit = max(1, min(LEADER_LIMIT, limit))
    if category == CS_PERCENT:
        return get_catcher_cs(kind, season, leagues, limit, min_attempts, ttl, era)
    if leagues is not None:
        leagues = [lid for lid, _a, _n in LEADER_LEAGUES if lid in set(leagues)]
    if not leaders_assembled(kind, leagues, era):
        params = {"leaderCategories": category, "statGroup": group, "statType": _LEADER_STAT_TYPES[kind],
                  "sportId": SPORT_ID, "limit": limit}
        if kind == "season":
            params["season"] = season
            if leagues is not None:   # (asking for a league that didn't play that year fails)
                leagues = leagues_playing(leagues, season)
                if not leagues:
                    return []
        if leagues is not None:
            params["leagueIds"] = ",".join(map(str, leagues))
        if position:
            params["position"] = position
        board = parse_leaders(api_get("/stats/leaders", params, ttl=ttl))
        if kind == "career":
            # MLB's career boards don't give the last team (Hank Aaron is a Brave there, Pete Rose a
            # Phillie, and Negro and 19th-century careers often have none): it's taken from each
            # player's season-by-season record, MLB's only where that has none
            teams = _last_teams(group, [e["pid"] for e in board if e["pid"] is not None], ttl=ttl)
            board = [dict(e, team=teams.get(e["pid"]) or e["team"]) for e in board]
        return board
    if leagues is None:
        leagues = [lid for lid, _a, _n in LEADER_LEAGUES]
    leagues = leagues_in_era(leagues, era)
    if not leagues:
        return []
    if kind == "single":
        if era == "early":
            return _early_single(group, category, leagues, position, limit)
        return _assembled_single(group, category, leagues, position, limit, ttl)
    return _assembled_career(group, category, leagues, era, position, limit, ttl)


def _last_teams(group: str, pids, leagues=None, era: str | None = None,
                ttl: float | None = None) -> dict[int, str]:
    """``{person id: the last team he played for}`` -- counting only ``leagues`` (None: any) and
    ``era``'s seasons -- from each player's season-by-season record, 100 players to a request."""
    pids = list(dict.fromkeys(pids))
    out = {}
    for i in range(0, len(pids), 100):
        payload = api_get("/people", {"personIds": ",".join(map(str, pids[i:i + 100])),
                                      "hydrate": f"stats(group=[{group}],type=[yearByYear])"}, ttl=ttl)
        for person in payload.get("people") or []:
            best = None
            for block in person.get("stats") or []:
                for order, split in enumerate(block.get("splits") or []):
                    team = (split.get("team") or {}).get("name")
                    season = int(split.get("season") or 0)
                    if not team or (leagues is not None and (split.get("league") or {}).get("id") not in leagues):
                        continue
                    if (era == "modern" and season < MODERN_FROM) or (era == "early" and season >= MODERN_FROM):
                        continue
                    if best is None or (season, order) >= best[0]:
                        best = ((season, order), team)
            if best:
                out[person["id"]] = best[1]
    return out


def _leader_value(category: str, text) -> float | None:
    """A board value as a number, for ranking ("208.2" innings is 208 2/3)."""
    if text in (None, "", "-.--", ".---"):
        return None
    if category == "inningsPitched":
        whole, _dot, thirds = str(text).partition(".")
        return int(whole or 0) + int(thirds or 0) / 3
    try:
        return float(text)
    except ValueError:
        return None


def _rank_board(category: str, entries: list[dict], limit: int) -> list[dict]:
    """``entries`` (each with "value") best first, cut to ``limit`` -- and anyone tied with the
    last -- with tied players sharing a rank, as MLB's boards do."""
    low = category in _LOWER_IS_BETTER
    entries = [e for e in entries if _leader_value(category, e["value"]) is not None]
    entries.sort(key=lambda e: ((1 if low else -1) * _leader_value(category, e["value"]), e["name"]))
    out, previous, rank = [], None, 0
    for i, entry in enumerate(entries, start=1):
        value = _leader_value(category, entry["value"])
        if value != previous:
            if i > limit:
                break
            rank = i
        out.append(dict(entry, rank=rank))
        previous = value
    return out


def _bulk_rows(params: dict, ttl: float | None, pages: int | None = None) -> list[dict]:
    """Every row of a ``/stats`` list, 1,000 to a request (the most it gives) -- the later pages
    fetched side by side -- or just the first ``pages``."""
    first = api_get("/stats", dict(params, limit=1000, offset=0), ttl=ttl)
    block = (first.get("stats") or [{}])[0]
    rows = list(block.get("splits") or [])
    total = int(block.get("totalSplits") or len(rows))
    offsets = list(range(1000, total, 1000))[:None if pages is None else max(0, pages - 1)]
    if offsets:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for page in pool.map(lambda o: api_get("/stats", dict(params, limit=1000, offset=o), ttl=ttl), offsets):
                rows += ((page.get("stats") or [{}])[0].get("splits") or [])
    return rows


def _row_entry(row: dict, value, leagues: str | None = None) -> dict:
    person = row.get("player") or {}
    league = row.get("league") or {}
    return {"rank": None, "pid": person.get("id"), "name": person.get("fullName") or "?",
            "season": row.get("season"), "team": (row.get("team") or {}).get("name") or "",
            "league_id": league.get("id"), "league": leagues if leagues is not None else league.get("name") or "",
            "value": value}


def _assembled_single(group, category, leagues, position, limit, ttl) -> list[dict]:
    """The best single seasons from 1901 on in ``leagues``: each league's seasons, ranked by MLB
    (its own playing-time minimums for the rates), read until enough from 1901 on are found."""
    key = _LEADER_FIELDS.get(category, category)
    entries = []
    for lid in leagues:
        params = {"stats": "statsSingleSeason", "group": group, "sportId": SPORT_ID, "leagueId": lid,
                  "playerPool": "QUALIFIED" if category in _RATE_CATEGORIES else "ALL",
                  "sortStat": category, "order": "asc" if category in _LOWER_IS_BETTER else "desc"}
        kept, offset = 0, 0
        while offset < 30000:
            page = api_get("/stats", dict(params, limit=1000, offset=offset), ttl=ttl)
            rows = (page.get("stats") or [{}])[0].get("splits") or []
            for row in rows:
                if int(row.get("season") or 0) < MODERN_FROM:
                    continue
                if position and (row.get("position") or {}).get("abbreviation") != position:
                    continue
                value = (row.get("stat") or {}).get(key)
                if value is not None:
                    entries.append(_row_entry(row, str(value)))
                    kept += 1
            if kept > limit or len(rows) < 1000:
                break
            offset += 1000
    return _rank_board(category, entries, limit)


def _early_seasons(group: str, pool: str, leagues) -> list[dict]:
    """Every player's row of every season before 1901 in ``leagues`` (one request a season, all
    leagues at once -- a finished era, so kept for good)."""
    rows = []
    for year in range(1876, MODERN_FROM):
        rows += [row for row in _bulk_rows({"stats": "season", "season": year, "group": group,
                                            "sportId": SPORT_ID, "playerPool": pool}, None)
                 if (row.get("league") or {}).get("id") in leagues]
    return rows


def _early_single(group, category, leagues, position, limit) -> list[dict]:
    """The best single seasons before 1901 in ``leagues``, from MLB's lists of each season (the
    qualified players, by its own minimums, for the rates)."""
    key = _LEADER_FIELDS.get(category, category)
    pool = "QUALIFIED" if category in _RATE_CATEGORIES else "ALL"
    entries = [_row_entry(row, str(row["stat"][key])) for row in _early_seasons(group, pool, leagues)
               if (row.get("stat") or {}).get(key) is not None
               and not (position and (row.get("position") or {}).get("abbreviation") != position)]
    return _rank_board(category, entries, limit)


def _assembled_career(group, category, leagues, era, position, limit, ttl) -> list[dict]:
    """Career leaders counting only ``leagues`` and ``era``'s seasons: each player's career in
    each of those leagues added up -- from 1901 on, less his National League seasons before 1901
    (the only league to cross 1901); before 1901, his seasons then added up -- with the averages
    and rates worked out again from the totals, for careers of :data:`CAREER_MIN_PA` plate
    appearances or :data:`CAREER_MIN_OUTS` outs."""
    key = _LEADER_FIELDS.get(category, category)
    totals: dict = {}
    names: dict = {}
    played: dict = {}

    def add(rows, sign, lid=None):
        for row in rows:
            if position and (row.get("position") or {}).get("abbreviation") != position:
                continue
            pid = (row.get("player") or {}).get("id")
            if pid is None:
                continue
            names[pid] = (row.get("player") or {}).get("fullName") or "?"
            if lid is not None:
                played.setdefault(pid, []).append(lid)
            t = totals.setdefault(pid, {})
            for field, value in (row.get("stat") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    t[field] = t.get(field, 0) + sign * value

    if era == "early":
        for row in _early_seasons(group, "ALL", leagues):
            add([row], 1, (row.get("league") or {}).get("id"))
    else:
        base = {"group": group, "sportId": SPORT_ID, "playerPool": "ALL"}
        for lid in leagues:
            add(_bulk_rows(dict(base, stats="career", leagueId=lid), ttl), 1, lid)
        if era == "modern" and 104 in leagues:
            add(_early_seasons(group, "ALL", {104}), -1)
    rate = _CAREER_RATES.get(category)
    entries = []
    for pid, t in totals.items():
        if pid not in played:
            continue
        t = dict(t)
        for field in ("hits", "atBats", "baseOnBalls", "hitByPitch", "sacFlies", "totalBases",
                      "earnedRuns", "outs", "strikeOuts", "plateAppearances"):
            t.setdefault(field, 0)
        if rate:
            fn, digits, batting = rate
            if (t["plateAppearances"] if batting else t["outs"]) < (CAREER_MIN_PA if batting else CAREER_MIN_OUTS):
                continue
            value = fn(t)
            if value is None:
                continue
            text = f"{value:.{digits}f}"
            if digits == 3 and text.startswith("0"):
                text = text[1:]
        elif category == "inningsPitched":
            if t["outs"] <= 0:
                continue
            text = f"{int(t['outs']) // 3}.{int(t['outs']) % 3}"
        else:
            value = t.get(key, 0)
            if value <= 0:
                continue
            text = str(int(value))
        abbrs = "/".join(abbr for lid, abbr, _n in LEADER_LEAGUES if lid in set(played[pid]))
        entries.append({"rank": None, "pid": pid, "name": names[pid], "season": None, "team": "",
                        "league_id": None, "league": abbrs, "value": text})
    board = _rank_board(category, entries, limit)
    teams = _last_teams(group, [e["pid"] for e in board], set(leagues), era, ttl)
    return [dict(e, team=teams.get(e["pid"], "")) for e in board]


# Catchers' caught stealing percentage: the runners a catcher threw out for each steal tried
# against him, CS / (SB + CS).  MLB keeps no board of it, so it's put together here from its
# lists of fielding rows -- the catchers' rows carry the steals tried against them, from the
# 1950s on (next to none before, and none before 1901) -- read most runners caught first, and
# only as far as anyone further down could still make the board.  A catcher needs a minimum of
# attempts against him, by default these for each kind of board.
CS_PERCENT = "caughtStealingPercentage"
CS_MIN_ATTEMPTS = {"season": 40, "single": 60, "career": 500}


def _cs_pages(params: dict, ttl: float | None):
    """Pages of a ``/stats`` fielding list, most runners caught first: (its catchers' rows with
    a runner caught, the runners caught on the page's last row), until no rows are left with one."""
    params = dict(params, group="fielding", sportId=SPORT_ID, playerPool="ALL",
                  sortStat="caughtStealing", order="desc")
    offset = 0
    while True:
        rows = (api_get("/stats", dict(params, limit=1000, offset=offset), ttl=ttl).get("stats")
                or [{}])[0].get("splits") or []
        last = (rows[-1].get("stat") or {}).get("caughtStealing") if rows else None
        yield [r for r in rows if (r.get("position") or {}).get("abbreviation") == "C"
               and (r.get("stat") or {}).get("caughtStealing")], last or 0
        if len(rows) < 1000 or not last:
            return
        offset += 1000


def _rank_cs(entries: list[dict], limit: int) -> list[dict]:
    """``entries`` (each with "sb", "cs" and "att") best percentage first -- the more attempts
    first among equals -- cut to ``limit`` and anyone tied with the last, ties sharing a rank."""
    entries = sorted(entries, key=lambda e: (-e["cs"] / e["att"], -e["att"], e["name"]))
    out, previous, rank = [], None, 0
    for i, entry in enumerate(entries, start=1):
        if previous is None or entry["cs"] * previous["att"] != previous["cs"] * entry["att"]:
            if i > limit:
                break
            rank = i
        value = f"{entry['cs'] / entry['att']:.3f}"
        out.append(dict(entry, rank=rank, value=value[1:] if value.startswith("0") else value))
        previous = entry
    return out


def get_catcher_cs(kind: str, season: int | None = None, leagues=None, limit: int = 25,
                   min_attempts: int | None = None, ttl: float | None = None,
                   era: str | None = None) -> list[dict]:
    """Catchers' caught stealing percentage, best first, as :func:`get_leaders` gives a board
    (with ``"sb"``, ``"cs"`` and ``"att"``, the steals tried against him, too), for catchers tried
    against ``min_attempts`` times (None: :data:`CS_MIN_ATTEMPTS`'s for ``kind``).  A season's or
    the best single seasons are read only until no one further down could make the board; a
    career adds up each league's career, every league's read through."""
    limit = max(1, min(LEADER_LIMIT, limit))
    least = max(1, CS_MIN_ATTEMPTS[kind] if min_attempts is None else min_attempts)
    if era == "early":
        return []
    ids = [lid for lid, _a, _n in LEADER_LEAGUES if leagues is None or lid in set(leagues)]
    if kind == "career":
        totals: dict = {}
        for lid in ids:
            for rows, _last in _cs_pages({"stats": "career", "leagueId": lid}, ttl):
                for row in rows:
                    pid = (row.get("player") or {}).get("id")
                    if pid is None:
                        continue
                    stat = row["stat"]
                    t = totals.setdefault(pid, {"name": (row.get("player") or {}).get("fullName") or "?",
                                                "sb": 0, "cs": 0, "played": set()})
                    t["sb"] += stat.get("stolenBases") or 0
                    t["cs"] += stat["caughtStealing"]
                    t["played"].add(lid)
        entries = [{"rank": None, "pid": pid, "name": t["name"], "season": None, "team": "",
                    "league_id": None, "sb": t["sb"], "cs": t["cs"], "att": t["sb"] + t["cs"],
                    "league": "/".join(abbr for lid, abbr, _n in LEADER_LEAGUES if lid in t["played"])}
                   for pid, t in totals.items() if t["sb"] + t["cs"] >= least]
        board = _rank_cs(entries, limit)
        teams = _last_teams("fielding", [e["pid"] for e in board], set(ids), era, ttl)
        return [dict(e, team=teams.get(e["pid"], "")) for e in board]
    params = {"stats": "season", "season": season} if kind == "season" else {"stats": "statsSingleSeason"}
    if leagues is not None and len(ids) == 1:
        params["leagueId"] = ids[0]
    entries = []
    for rows, last in _cs_pages(params, ttl):
        for row in rows:
            stat = row["stat"]
            sb, cs = stat.get("stolenBases") or 0, stat["caughtStealing"]
            if sb + cs < least or (leagues is not None and (row.get("league") or {}).get("id") not in ids):
                continue
            if era == "modern" and int(row.get("season") or 0) < MODERN_FROM:
                continue
            entries.append(dict(_row_entry(row, ""), sb=sb, cs=cs, att=sb + cs))
        # anyone further down caught at most ``last``, so out of ``least`` attempts or more he
        # can't reach the board's last percentage once it's full
        if len(entries) >= limit:
            cut = sorted((e["cs"] / e["att"] for e in entries), reverse=True)[limit - 1]
            if last < cut * least:
                break
    return _rank_cs(entries, limit)


# Standings tab: every league MLB keeps standings for -- the American and National Leagues,
# the Negro leagues (1920-1948, recognised as major leagues in 2020) and the 19th-century
# majors -- but not the spring-training leagues.  Asking for all of them at once returns
# just the ones that played that season.
STANDINGS_LEAGUE_IDS = (103, 104, 431, 430, 429, 428, 427, 426, 432, 100, 105, 101, 106)


def _standings_int(value) -> int | None:
    number = as_number(value)
    return None if number is None else int(number)


def parse_standings(payload: dict) -> list[dict]:
    """A season's ``/standings`` response (asked for with ``hydrate=team,division,league``) ->
    one group per division -- or per league, before divisions began in 1969 and for the
    Negro and 19th-century leagues:

    ``{"league_id", "league", "division_id", "division", "name", "teams": [...]}``, the
    American League first, divisions East to West, and each group's teams best first.
    A team is ``{"id", "name", "wins", "losses", "ties", "pct", "gb", "rank", "rs", "ra",
    "diff", "played"}``: ``name`` is the one it had that season (so a renamed franchise
    shows under each of its names), ``rank`` and ``gb`` are within the division (or league),
    and ``gb`` is the API's text -- ``"-"`` for a leader."""
    groups = []
    for record in payload.get("records", []):
        league = record.get("league") or {}
        division = record.get("division") or {}
        divisional = division.get("id") is not None
        teams = []
        for entry in record.get("teamRecords", []):
            team = entry.get("team") or {}
            overall = entry.get("leagueRecord") or {}
            teams.append({
                "id": team.get("id"),
                "name": team.get("name") or team.get("shortName") or "?",
                "wins": _standings_int(entry.get("wins")) or 0,
                "losses": _standings_int(entry.get("losses")) or 0,
                "ties": _standings_int(overall.get("ties")) or 0,
                "pct": entry.get("winningPercentage") or overall.get("pct") or "",
                "gb": entry.get("divisionGamesBack" if divisional else "leagueGamesBack") or "-",
                "rank": _standings_int(entry.get("divisionRank" if divisional else "leagueRank")),
                "rs": _standings_int(entry.get("runsScored")),
                "ra": _standings_int(entry.get("runsAllowed")),
                "diff": _standings_int(entry.get("runDifferential")),
                "played": _standings_int(entry.get("gamesPlayed")),
            })
        teams.sort(key=lambda t: (t["rank"] is None, t["rank"] or 0, -t["wins"], t["name"]))
        groups.append({
            "league_id": league.get("id"), "league": league.get("name") or "",
            "division_id": division.get("id"), "division": division.get("name") or "",
            "name": (division.get("name") if divisional else league.get("name")) or "?",
            "teams": teams,
        })
    return _sort_standings_groups(groups)


# League-seasons MLB recognizes -- the API has their clubs and season records -- but whose
# standings the API doesn't serve: the National League's split season of 1892 and the Federal
# League (1914-1915).  Found by comparing every season's team list with its standings
# (1876-2026); those standings are computed from team season stats.
STANDINGS_GAPS = {1892: (104,), 1914: (106,), 1915: (106,)}


def _sort_standings_groups(groups: list[dict]) -> list[dict]:
    def league_position(league_id):
        return (STANDINGS_LEAGUE_IDS.index(league_id) if league_id in STANDINGS_LEAGUE_IDS
                else len(STANDINGS_LEAGUE_IDS))
    return sorted(groups, key=lambda g: (league_position(g["league_id"]),
                                         _DIVISION_ORDER.get(g["division"].split()[-1] if g["division"] else "", 3),
                                         g["name"]))


def standings_from_team_stats(league_id: int, league_name: str, clubs: dict[int, str],
                              pitching: dict[int, dict], hitting: dict[int, dict]) -> dict:
    """One league's standings group worked out from its clubs' season stats: ``clubs`` is
    ``{team id: name that season}``, ``pitching`` and ``hitting`` are ``{team id: stat block}``
    (wins, losses and runs allowed; runs scored).  Ranked by winning percentage, games back
    from the leader as the API works them out, ties (games played beyond wins and losses)
    kept.  A club without stats is left out.  ``"computed": True`` marks the group."""
    rows = []
    for tid, name in clubs.items():
        p, h = pitching.get(tid), hitting.get(tid)
        if not p or not h or _standings_int(p.get("wins")) is None or _standings_int(p.get("losses")) is None:
            continue
        wins, losses = _standings_int(p["wins"]), _standings_int(p["losses"])
        played = _standings_int(p.get("gamesPlayed"))
        rs, ra = _standings_int(h.get("runs")), _standings_int(p.get("runs"))
        rows.append({"id": tid, "name": name, "wins": wins, "losses": losses,
                     "ties": max(0, played - wins - losses) if played else 0,
                     "played": played, "rs": rs, "ra": ra,
                     "diff": rs - ra if rs is not None and ra is not None else None})
    rows.sort(key=lambda t: (-(t["wins"] / (t["wins"] + t["losses"]) if t["wins"] + t["losses"] else 0),
                             -t["wins"], t["name"]))
    for rank, team in enumerate(rows, start=1):
        decisions = team["wins"] + team["losses"]
        team["pct"] = _rate_text(team["wins"] / decisions) if decisions else ""
        team["rank"] = rank
        if rank == 1:
            team["gb"] = "-"
        else:
            back = ((rows[0]["wins"] - team["wins"]) + (team["losses"] - rows[0]["losses"])) / 2
            team["gb"] = f"{back:.1f}"
    return {"league_id": league_id, "league": league_name, "division_id": None, "division": "",
            "name": league_name, "teams": rows, "computed": True}


def _rate_text(value: float) -> str:
    """A winning percentage the way MLB writes it: .588 (1.000 for a perfect record)."""
    text = f"{value:.3f}"
    return text[1:] if text.startswith("0") else text


def _team_stat_blocks(season: int, group: str, ttl) -> dict[int, dict]:
    payload = api_get("/teams/stats", {"stats": "season", "group": group, "season": season,
                                       "sportId": SPORT_ID}, ttl=ttl)
    blocks = {}
    for section in payload.get("stats", []):
        for split in section.get("splits", []):
            team_id = (split.get("team") or {}).get("id")
            if team_id is not None:
                blocks[team_id] = split.get("stat") or {}
    return blocks


def get_standings(season: int, ttl: float | None = None) -> list[dict]:
    """:func:`parse_standings` for ``season`` -- one request for every league that played it.
    For the few league-seasons the API serves no standings for (:data:`STANDINGS_GAPS`) the group is
    computed from the clubs' season stats instead (:func:`standings_from_team_stats`)."""
    payload = api_get(
        "/standings",
        {"leagueId": ",".join(map(str, STANDINGS_LEAGUE_IDS)), "season": season,
         "standingsTypes": "regularSeason", "hydrate": "team,division,league"},
        ttl=ttl,
    )
    groups = parse_standings(payload)
    missing = [lid for lid in STANDINGS_GAPS.get(season, ()) if not any(g["league_id"] == lid for g in groups)]
    if missing:
        teams = get_team_map(season, ttl)
        pitching, hitting = _team_stat_blocks(season, "pitching", ttl), _team_stat_blocks(season, "hitting", ttl)
        for league_id in missing:
            clubs = {tid: t["name"] for tid, t in teams.items() if t.get("league_id") == league_id}
            league_name = next((t["league"] for t in teams.values() if t.get("league_id") == league_id), "")
            group = standings_from_team_stats(league_id, league_name, clubs, pitching, hitting)
            if group["teams"]:
                groups.append(group)
    return _sort_standings_groups(groups)


def franchise_history(team_id: int, seasons, ttl_for=lambda season: None,
                      workers: int = 6) -> tuple[list[dict], list[int]]:
    """One franchise's standings line in each of ``seasons`` -> ``(rows, failed)``.  Franchise
    ids are stable across moves and renames, so this follows the club through its names: each
    row is the team dict of :func:`parse_standings` (its name that season) plus ``"season"``,
    ``"group"`` (its division, or league before 1969) and ``"league"``.  A season the club
    didn't play is simply absent; ``failed`` lists seasons that couldn't be fetched.  One
    request per season (``workers`` at a time); finished seasons are cached for good."""
    def one(season):
        try:
            groups = get_standings(season, ttl_for(season))
        except MLBStatsError:
            return season, None, True
        for group in groups:
            for team in group["teams"]:
                if team["id"] == team_id:
                    return season, dict(team, season=season, group=group["name"], league=group["league"],
                                        computed=bool(group.get("computed"))), False
        return season, None, False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, list(seasons)))
    rows = sorted((row for _s, row, _f in results if row), key=lambda r: r["season"])
    return rows, [season for season, _row, failed in results if failed]


def name_eras(rows: list[dict]) -> list[tuple[str, int, int]]:
    """A franchise history's names in order -> ``[(name, first season, last season), ...]``."""
    eras: list[list] = []
    for row in sorted(rows, key=lambda r: r["season"]):
        if eras and eras[-1][0] == row["name"]:
            eras[-1][2] = row["season"]
        else:
            eras.append([row["name"], row["season"], row["season"]])
    return [tuple(era) for era in eras]


def ordinal(n: int) -> str:
    """1 -> ``"1st"``, 12 -> ``"12th"``, 23 -> ``"23rd"``."""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# Standings tab's wild card view.  The format began with the 1994 season (which the strike
# ended before any wild card was awarded); MLB's standings will "compute" a wild card race for
# earlier seasons too, but there was none.
WILD_CARD_FIRST_SEASON = 1994


def _wildcard_status(entry: dict, section: str) -> str:
    """How a team stands in the playoff picture, from MLB's own flags (they carry the clinches)."""
    indicator = entry.get("clinchIndicator")
    if section == "division":
        if entry.get("divisionChamp"):
            return "Division champion"
        rank = _standings_int(entry.get("divisionRank"))
        base = "Division runner-up" if rank and rank > 1 else "Division leader"  # 2020: two per division
        return base + (", clinched a playoff spot" if entry.get("clinched") else "")
    if section == "wild_card":
        if indicator == "w":
            return "Wild card"
        return "Wild card position" + (", clinched a playoff spot" if entry.get("clinched") else "")
    return ""


def parse_wildcard(payload: dict) -> list[dict]:
    """A season's ``standingsTypes=wildCardWithLeaders`` response -> one entry per league,
    ``{"league_id", "league", "teams": [...]}`` (the American League first).  Each league's
    teams come in playoff order:

    1. those leading their division (``section`` ``"division"``; in 2020 also the runners-up,
       who qualified too), best record first;
    2. those holding a wild card spot (``"wild_card"``), in wild card rank order;
    3. everyone else (``"other"``), in wild card rank order, with ``gb`` their games behind
       the last wild card spot.

    Leaders and wild card holders have ``gb`` ``"-"``: for a wild card holder MLB gives the
    margin it is *ahead* of the last spot (``"+10.0"``), kept in ``ahead``.  A team is
    ``{"id", "name", "wins", "losses", "ties", "pct", "gb", "ahead", "section", "status",
    "rs", "ra", "diff"}``."""
    leagues: dict[int, dict] = {}
    for record in payload.get("records", []):
        league = record.get("league") or {}
        league_id = league.get("id")
        entry = leagues.setdefault(league_id, {"league_id": league_id, "league": league.get("name") or "",
                                               "division": [], "wild_card": [], "other": []})
        entry["league"] = entry["league"] or league.get("name") or ""
        kind = record.get("standingsType")
        for team in record.get("teamRecords", []):
            if kind == "divisionLeaders":
                section = "division"
            elif kind == "wildCard":
                section = "wild_card" if team.get("wildCardLeader") else "other"
            else:
                continue
            info = team.get("team") or {}
            overall = team.get("leagueRecord") or {}
            gb_text = team.get("wildCardGamesBack") or "-"
            entry[section].append({
                "id": info.get("id"), "name": info.get("name") or info.get("shortName") or "?",
                "wins": _standings_int(team.get("wins")) or 0, "losses": _standings_int(team.get("losses")) or 0,
                "ties": _standings_int(overall.get("ties")) or 0,
                "pct": team.get("winningPercentage") or overall.get("pct") or "",
                "gb": gb_text if section == "other" else "-",
                "ahead": gb_text[1:] if section == "wild_card" and gb_text.startswith("+") else "",
                "section": section, "status": _wildcard_status(team, section),
                "rs": _standings_int(team.get("runsScored")), "ra": _standings_int(team.get("runsAllowed")),
                "diff": _standings_int(team.get("runDifferential")),
                "_wc_rank": _standings_int(team.get("wildCardRank")),
                "_div_rank": _standings_int(team.get("divisionRank")),
            })
    out = []
    for league_id in sorted(leagues, key=lambda i: STANDINGS_LEAGUE_IDS.index(i) if i in STANDINGS_LEAGUE_IDS else 99):
        entry = leagues[league_id]
        entry["division"].sort(key=lambda t: (t["_div_rank"] or 99, -t["wins"], t["losses"], t["name"]))
        for key in ("wild_card", "other"):
            entry[key].sort(key=lambda t: (t["_wc_rank"] is None, t["_wc_rank"] or 0, -t["wins"], t["name"]))
        teams = entry["division"] + entry["wild_card"] + entry["other"]
        for team in teams:
            team.pop("_wc_rank"), team.pop("_div_rank")
        out.append({"league_id": league_id, "league": entry["league"], "teams": teams})
    return out


def get_wildcard_standings(season: int, ttl: float | None = None) -> list[dict]:
    """:func:`parse_wildcard` for ``season`` -- one request, both leagues."""
    payload = api_get(
        "/standings",
        {"leagueId": "103,104", "season": season, "standingsTypes": "wildCardWithLeaders",
         "hydrate": "team,league"},
        ttl=ttl,
    )
    return parse_wildcard(payload)


# What a team-level custom stat may use when the Standings tab evaluates it for a standings
# line: the four numbers such a line has.
STANDINGS_VARIABLES = {"Batting: Runs", "Pitching: Runs allowed", "Pitching: Wins", "Pitching: Losses"}


def standings_stat(ftext: str, team: dict) -> float | None:
    """A team-level custom stat -- a Pythagorean win percentage, or the wins or losses that
    percentage implies over the team's games -- evaluated for one standings line (a team dict
    of :func:`parse_standings`: ``rs``, ``ra``, ``wins``, ``losses``).  ``None`` if a figure is
    missing or the formula needs anything beyond :data:`STANDINGS_VARIABLES`."""
    values = {"Batting: Runs": team.get("rs"), "Pitching: Runs allowed": team.get("ra"),
              "Pitching: Wins": team.get("wins"), "Pitching: Losses": team.get("losses")}
    try:
        return formula.evaluate_formula(ftext, values)
    except formula.FormulaError:
        return None


# --------------------------------------------------------------------------- #
#  Prevention tab: a team's pitchers by role, and pooled figures for each group #
# --------------------------------------------------------------------------- #
PITCHER_ROLES = ("Rotation", "Long Relief", "Closers", "Other")
PITCHER_LINEUP_ROLES = ("Rotation", "Long Relief", "Closers")  # the staff proper; "Other" is the rest of the roster


def outs_to_innings(outs) -> str:
    """172 1/3 innings' worth of outs -> ``"172.1"``, the way baseball writes innings."""
    outs = int(round(outs))
    return f"{outs // 3}.{outs % 3}"


def pitcher_workload(row: dict) -> tuple[float, float, float]:
    """``(games started, games finished, outs recorded)`` of a pitching row; a missing figure
    counts as none.  Outs come from the row's own count, else from its innings pitched."""
    outs = as_number(row.get("outs"))
    if outs is None:
        outs = innings_to_outs(row.get("inningsPitched", 0))
    return (as_number(row.get("gamesStarted")) or 0, as_number(row.get("gamesFinished")) or 0, outs)


ROTATION_SHARE = 0.4        # of the busiest starter's starts: enough for the Rotation on its own
STARTER_RATIO = 2           # starts per relief appearance for a pitcher who mostly started ...
STARTER_RATIO_SHARE = 0.3   # ... and the share of the busiest starter's starts he needs as well


def classify_pitchers(rows: list[dict]) -> dict:
    """A team's pitchers (its pitching rows) sorted into roles by how each compares with the
    team average per pitcher.  The games-started and games-finished averages are taken only over
    pitchers who have any -- the zeros of everyone else would drag a whole-staff average so low
    that spot starters counted as Rotation (8 per team in 2023) and anyone with a handful of
    finishes as a Closer:

    * **Rotation** -- any of: at or above the average games started of the team's starters; at
      least :data:`ROTATION_SHARE` (40%) of the starts of the team's busiest starter (a starter
      sharing a turn in the rotation, like Jim Hardin and Tom Phoebus for the 1970 Orioles, when
      three 40-start pitchers lift the average out of reach); or a pitcher who mostly started
      (:data:`STARTER_RATIO`: at least twice as many starts as relief appearances) with at least
      :data:`STARTER_RATIO_SHARE` (30%) of the busiest starter's starts -- shares, not numbers, so
      short seasons (60 games in 2020) work the same;
    * **Closers** -- at or above the average games finished of the team's finishers, and not in
      the Rotation;
    * **Long Relief** -- fewer games started than the starters' average *and* fewer games
      finished than the finishers' average, but more innings than the average of the pitchers
      *outside the Rotation* (measured against the whole staff, the starters' innings pull the
      average up so far that hardly anyone outside the Rotation can exceed it) -- and at least
      one game in relief (a spot starter who never relieved is Other);
    * **Other** -- everyone else (including anyone exactly at the innings average).

    "At or above" so a lone starter or finisher, or several who tie, still count.

    -> ``{"averages": {"gs", "gf", "outs", "relief_outs", "starters", "finishers", "pitchers", "max_gs"},
    "roles": {role: [row, ...]}}``: ``gs`` is the starters' average, ``gf`` the finishers',
    ``outs`` the whole staff's innings average and ``relief_outs`` the non-Rotation one Long Relief
    is measured against.  Each role's pitchers are busiest first (most outs, then most starts,
    then name)."""
    loads = [pitcher_workload(r) for r in rows]
    n = len(rows)
    starts = [load[0] for load in loads if load[0] > 0]
    finishes = [load[1] for load in loads if load[1] > 0]
    avg_gs = sum(starts) / len(starts) if starts else 0.0
    avg_gf = sum(finishes) / len(finishes) if finishes else 0.0
    avg_outs = sum(load[2] for load in loads) / n if n else 0.0
    max_gs = max(starts) if starts else 0

    def rotation(row, gs) -> bool:
        if gs <= 0:
            return False
        if gs >= avg_gs or gs >= ROTATION_SHARE * max_gs:
            return True
        games = as_number(row.get("gamesPitched"))
        return games is not None and gs >= STARTER_RATIO * (games - gs) and gs >= STARTER_RATIO_SHARE * max_gs
    def relief_games(row, gs):
        """Games pitched in relief, or None when the row doesn't say how many games he pitched."""
        games = as_number(row.get("gamesPitched"))
        return None if games is None else max(games - gs, 0)
    in_rotation = [rotation(row, load[0]) for row, load in zip(rows, loads)]
    outside = [load for load, rot in zip(loads, in_rotation) if not rot]
    relief_outs = sum(load[2] for load in outside) / len(outside) if outside else 0.0
    roles: dict[str, list[dict]] = {role: [] for role in PITCHER_ROLES}
    for row, (gs, gf, outs), rot in zip(rows, loads, in_rotation):
        if rot:
            role = "Rotation"
        elif gf > 0 and gf >= avg_gf:
            role = "Closers"
        elif gs < avg_gs and gf < avg_gf and outs > relief_outs and relief_games(row, gs) != 0:
            role = "Long Relief"  # (never a pitcher who only started: his innings aren't relief)
        else:
            role = "Other"
        roles[role].append(row)
    for members in roles.values():
        members.sort(key=lambda r: (-pitcher_workload(r)[2], -pitcher_workload(r)[0], r.get("_name", "")))
    return {"averages": {"gs": avg_gs, "gf": avg_gf, "outs": avg_outs, "relief_outs": relief_outs,
                         "starters": len(starts), "finishers": len(finishes), "pitchers": n, "max_gs": max_gs},
            "roles": roles}


def pooled_stat(ftext: str, groups_by_view: dict, pids) -> float | None:
    """A player-level custom stat for several players taken together -- the formula applied to
    their combined figures (counting stats added up, rate stats weighted by playing time), so
    a per-nine-innings stat is the group's total runs over its total outs, not an average of
    averages.  ``groups_by_view`` is ``{view: {player id: rows}}`` as for
    :func:`player_variable_lookup`."""
    values = {}
    for name in formula.formula_variables(ftext):
        view, key, agg = REGRESSION_VARIABLE_INDEX[name]
        rows = [row for pid in pids for row in groups_by_view.get(view, {}).get(pid, [])]
        values[name] = aggregate_rows(rows, key, agg) if rows else None
    return formula.evaluate_formula(ftext, values)


def prevention_report(rows: list[dict], groups_by_view: dict, stats: dict[str, str]) -> dict:
    """A team's pitchers by role with the player-level custom stats in ``stats`` (``{key: formula}``)
    for each pitcher, each role and all together.  ``rows`` are the team's pitching rows,
    ``groups_by_view`` the views the formulas need (see :func:`pooled_stat`).

    -> ``{"averages": ..., "roles": [{"role", "pitchers": [...], "total": {...}}, ...], "lineup": {...},
    "all": {...}}`` where ``lineup`` is the total of the :data:`PITCHER_LINEUP_ROLES` together
    where a pitcher is ``{"id", "name", "gs", "gf", "outs", "r", "er", "ur", "stats": {key: value}}``
    and a total is the same figures added up (``"count"`` pitchers) with each stat worked out
    from the combined figures."""
    classified = classify_pitchers(rows)

    def figures(members: list[dict]) -> dict:
        gs = sum(pitcher_workload(r)[0] for r in members)
        gf = sum(pitcher_workload(r)[1] for r in members)
        outs = sum(pitcher_workload(r)[2] for r in members)
        runs = sum(as_number(r.get("runs")) or 0 for r in members)
        earned = sum(as_number(r.get("earnedRuns")) or 0 for r in members)
        return {"gs": gs, "gf": gf, "outs": outs, "r": runs, "er": earned, "ur": runs - earned}

    def total(members: list[dict]) -> dict:
        pids = [r["_id"] for r in members]
        return dict(figures(members), count=len(members),
                    stats={key: pooled_stat(ftext, groups_by_view, pids) for key, ftext in stats.items()})

    out_roles = []
    for role in PITCHER_ROLES:
        members = classified["roles"][role]
        pitchers = []
        for row in members:
            values = {key: pooled_stat(ftext, groups_by_view, [row["_id"]]) for key, ftext in stats.items()}
            pitchers.append(dict(figures([row]), id=row["_id"], name=row.get("_name", "?"), stats=values))
        out_roles.append({"role": role, "pitchers": pitchers, "total": total(members)})
    lineup = [r for role in PITCHER_LINEUP_ROLES for r in classified["roles"][role]]
    return {"averages": classified["averages"], "roles": out_roles, "lineup": total(lineup), "all": total(rows)}


def _league_view_rows(season: int, view: str, ttl: float | None) -> list[dict]:
    """Every player's row of one player-list view for all of MLB in ``season`` (one request,
    checked against the reported total so a truncated list can't skew a figure)."""
    group, stat_type, _cols = VIEW_SPECS[view]
    if group == "retrosheet":
        return [dict(r, _id=r["id"], _name=r["name"], _pos="") for r in _retro_summary(season)["runners"]]
    if group in ("retrosheet-relief", "retrosheet-rbi"):
        return retro_league_rows(season, group)
    payload = api_get(
        "/stats",
        {"stats": stat_type, "group": group, "season": season, "sportId": SPORT_ID,
         "playerPool": "all", "limit": 5000},
        ttl=ttl,
    )
    rows = parse_player_rows(payload)
    total = (payload.get("stats") or [{}])[0].get("totalSplits")
    if total is not None and len(rows) < total:
        raise MLBStatsError(f"the {season} league {view} list came back truncated "
                            f"({len(rows)} of {total} rows)")
    return rows


# The MLB figures the Score stats compare a player with: each the league-wide value of the custom stat
# of that name (as the Production, Prevention and Speed tabs use them), or of this formula if there's none.
LEAGUE_FIGURE_DEFAULTS = {
    "Runs Produced per Plate Appearance": "({Batting: R}+{Batting: RBI}-{Batting: HR})/{Batting: PA}",
    "Runs per Nine Innings": "{Pitching: R}*27/{Pitching: Outs}",
    "Bases Gained per Opportunity": None,   # its formula is long: bases-gained-per-opportunity-player.json
}
_SCORE_MEMO: dict = {}


def _memo(key, ttl, compute):
    """``compute()``, remembered for this run (a finished season's for good, else ``ttl`` seconds)."""
    hit = _SCORE_MEMO.get(key)
    if hit is not None and (ttl is None or time.time() - hit[0] < ttl):
        return hit[1]
    value = compute()
    _SCORE_MEMO[key] = (time.time(), value)
    return value


def league_figures(season: int, ttl: float | None = None) -> dict[str, float | None]:
    """``{"rp_pa", "r9", "bg_opp"}``: MLB's figures in ``season`` for :data:`LEAGUE_FIGURE_DEFAULTS`,
    each worked out on the whole league's combined figures; one that can't be had (Retrosheet's
    baserunning before it publishes the season) is None, without spoiling the others."""
    out = {}
    for label, key in SCORE_VIEWS["MLB"][1]:
        ftext = formula.custom_formula(label) or LEAGUE_FIGURE_DEFAULTS.get(label)
        if not ftext:
            out[key] = None
            continue
        try:
            out[key] = _memo(("league", season, ftext), ttl, lambda f=ftext: get_league_pooled(season, [f], ttl)[f])
        except (MLBStatsError, formula.FormulaError, KeyError):
            out[key] = None
    return out


def score_view_rows(team_id: int, season: int, group: str, ttl: float | None = None) -> list[dict]:
    """One row per player who batted, pitched or fielded for the team that season, for a Score view:
    ``league-figures`` (MLB's figures that season, :func:`league_figures`), ``range-figures`` (his
    ``chances``, ``expected`` chances and ``innings`` at the nine positions, measured as the Range
    tab does -- :func:`roster_range` on his own fielding rows) or ``team-figures`` (``team_games``)."""
    names: dict[int, str] = {}
    fielding = []
    for api_group in ("hitting", "pitching", "fielding"):
        rows = get_player_stats(team_id, season, api_group, ttl)
        for row in rows:
            if row.get("_id") is not None:
                names.setdefault(row["_id"], row.get("_name", "?"))
        if api_group == "fielding":
            fielding = rows
    if group == "league-figures":
        figures = league_figures(season, ttl)
        return [dict(figures, _id=pid, _name=name) for pid, name in names.items()]
    if group == "team-figures":
        games = _memo(("games", season), ttl, lambda: get_team_games_played(season, ttl)).get(team_id)
        return [{"_id": pid, "_name": name, "team_games": games} for pid, name in names.items()]
    league = _memo(("positions", season), ttl,
                   lambda: aggregate_league_positions(_league_view_rows(season, "Fielding", ttl)))
    by_player = group_by_player(fielding)
    out = []
    for pid, name in names.items():
        rows = [r for r in by_player.get(pid, []) if r.get("_pos") in league]
        measured = roster_range(rows, league) if rows else None
        outs = sum(innings_to_outs(r.get("innings", 0)) for r in rows)
        out.append({"_id": pid, "_name": name,
                    "chances": measured["chances"] if measured else None,
                    "expected": measured["expected"] if measured else None,
                    "innings": outs / 3 if rows else None})
    return out


def get_league_view_rows(season: int, view: str, ttl: float | None = None) -> list[dict]:
    """Every player's row of one player-list view ("Batting", "Fielding", ...) for all of
    MLB in ``season`` -- one request, checked against the reported total."""
    return _league_view_rows(season, view, ttl)


def get_team_games_played(season: int, ttl: float | None = None) -> dict[int, int]:
    """``{team id: games played}`` for every MLB club in ``season`` (one request)."""
    payload = api_get("/teams/stats", {"stats": "season", "group": "hitting", "season": season,
                                       "sportIds": SPORT_ID}, ttl=ttl)
    out = {}
    for block in payload.get("stats", []):
        for split in block.get("splits", []):
            tid = (split.get("team") or {}).get("id")
            games = as_number((split.get("stat") or {}).get("gamesPlayed"))
            if tid is not None and games:
                out[tid] = int(games)
    return out


# --------------------------------------------------------------------------- #
#  Teamstats: the Stats tab's views with one row per team-season.            #
# --------------------------------------------------------------------------- #
# Standard and Advanced come straight from MLB's team figures -- for the whole league in one
# request (``/teams/stats``).  For Sabermetrics and Expected MLB has no team figure (its team
# endpoint answers with the players' rows, or every player in the league), so those are rolled up
# from the team's players: counting stats (WAR, RAR, wRAA ...) added up, rates weighted by playing
# time from the players' standard lines -- plate appearances for batters; innings for pitchers'
# FIP family, which makes team FIP and xFIP exact; batters faced for pitchers' expected stats.
TEAM_STAT_VIEWS = list(PLAYER_VIEWS)
_LEAGUE_WIDE_STAT_TYPES = ("season", "seasonAdvanced")
_TEAM_ONLY_KEYS = ("_name", "_pos")  # player columns with no team-level meaning
_NO_TEAM_ROW: set = set()  # (team, season, group, stat type) MLB had nothing for, this session


def team_view_columns(view: str) -> list[tuple[str, str]]:
    """A Stats-tab view's (header, key) columns less the player's name and position."""
    return [(h, k) for h, k in VIEW_SPECS[view][2] if k not in _TEAM_ONLY_KEYS]


def _team_splits(payload: dict, season: int) -> dict[int, dict]:
    """A team-stats payload -> ``{team id: row}``, each row the ``stat`` block plus ``_team_id``,
    ``_team`` (the club's name that season) and ``_season``.  Teams with no figures are left out."""
    out = {}
    for block in payload.get("stats", []):
        for split in block.get("splits", []):
            team = split.get("team") or {}
            stat = split.get("stat") or {}
            if team.get("id") is None or not stat:
                continue
            out[team["id"]] = dict(stat, _team_id=team["id"], _team=team.get("name", "?"), _season=season)
    return out


def _rolled_up_team_row(team_id: int, season: int, view: str, ttl: float | None = None) -> dict | None:
    """A Sabermetrics or Expected view's team row, rolled up from the team's players (see above)."""
    group, stat_type, _cols = VIEW_SPECS[view]
    try:
        players = get_player_stats(team_id, season, group, ttl, stat_type=stat_type)
        base = get_player_stats(team_id, season, group, ttl) if players else []
    except MLBStatsError:
        return None
    if not players:
        return None
    weights: dict = {}
    for r in base:
        if group == "hitting":
            w = as_number(r.get("plateAppearances"))
        elif stat_type == "sabermetrics":
            w = innings_to_outs(r.get("inningsPitched"))
        else:
            w = as_number(r.get("battersFaced"))
        weights[r.get("_id")] = weights.get(r.get("_id"), 0) + (w or 0)
    row = {}
    for _label, key, agg in REGRESSION_VARIABLES[view]:
        if agg == "sum":
            value = aggregate_rows(players, key, "sum")
            # a count (shutdowns, meltdowns) as a whole number; run values (WAR, RAR) as they come
            row[key] = int(value) if value is not None and float(value).is_integer() else value
            continue
        total = weight = 0.0
        for r in players:
            value, w = as_number(r.get(key)), weights.get(r.get("_id"), 0)
            if value is not None and w:
                total += value * w
                weight += w
        row[key] = total / weight if weight else None
    if not any(v is not None for v in row.values()):
        return None
    named = get_team_view_row(team_id, season, "Batting" if group == "hitting" else "Pitching", ttl)
    return dict(row, _team_id=team_id, _team=(named or {}).get("_team", "?"), _season=season)


def get_team_view_row(team_id: int, season: int, view: str, ttl: float | None = None) -> dict | None:
    """One team-season's row of ``view`` (see :func:`_team_splits`), or None when MLB has none."""
    group, stat_type, _cols = VIEW_SPECS[view]
    if stat_type == "expectedStatistics" and season < EXPECTED_FROM_SEASON:
        return None
    if stat_type not in _LEAGUE_WIDE_STAT_TYPES:
        return _rolled_up_team_row(team_id, season, view, ttl)
    missed = (team_id, season, group, stat_type)
    if ttl is None and missed in _NO_TEAM_ROW:
        return None
    try:
        payload = api_get(f"/teams/{team_id}/stats",  # the same request as get_team_totals: one cache
                          {"stats": stat_type, "group": group, "season": season, "sportId": SPORT_ID}, ttl=ttl)
    except MLBStatsError:
        if ttl is None:
            _NO_TEAM_ROW.add(missed)  # (a club's seasons before or after its time: MLB answers 404, uncached)
        return None
    row = _team_splits(payload, season).get(team_id)
    if row is not None and not any(as_number(v) is not None for k, v in row.items() if not k.startswith("_")):
        return None  # a block of blanks (Sabermetrics before MLB has the inputs)
    return row


def get_league_team_rows(season: int, view: str, team_ids, ttl: float | None = None,
                         workers: int = 8) -> list[dict]:
    """``view``'s row for each of ``team_ids`` in ``season``: one league-wide request where the
    view allows it, else (and for any team it leaves out) one request a team."""
    group, stat_type, _cols = VIEW_SPECS[view]
    wanted = list(team_ids)
    found: dict[int, dict] = {}
    if stat_type in _LEAGUE_WIDE_STAT_TYPES:
        try:
            payload = api_get("/teams/stats", {"stats": stat_type, "group": group, "season": season,
                                               "sportIds": SPORT_ID}, ttl=ttl)
            found = {tid: row for tid, row in _team_splits(payload, season).items() if tid in wanted}
        except MLBStatsError:
            found = {}
    missing = [tid for tid in wanted if tid not in found]
    if missing:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for tid, row in zip(missing, pool.map(lambda t: get_team_view_row(t, season, view, ttl), missing)):
                if row is not None:
                    found[tid] = row
    return [found[tid] for tid in wanted if tid in found]


def get_team_history_rows(team_id: int, view: str, seasons, ttl_for=lambda season: None,
                          workers: int = 8) -> list[dict]:
    """``view``'s row for one franchise in each of ``seasons`` it has one, oldest first;
    ``ttl_for(season)`` gives each season's cache lifetime (None for a finished season)."""
    seasons = list(seasons)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = pool.map(lambda y: get_team_view_row(team_id, y, view, ttl_for(y)), seasons)
        return [row for row in rows if row is not None]


STATCAST_FIRST_SEASON = 2015  # pitch tracking began in 2008 (PITCHf/x); Statcast took over in 2015


def parse_pitch_arsenal(payload: dict) -> list[dict]:
    """A ``stats=pitchArsenal`` response -> the pitcher's pitch types that season, most thrown
    first: ``[{"code", "name", "share", "count", "speed"}, ...]`` (``share`` 0..1)."""
    out = []
    for block in payload.get("stats", []):
        for split in block.get("splits", []):
            stat = split.get("stat") or {}
            kind = stat.get("type") or {}
            if not kind.get("code"):
                continue
            out.append({"code": kind["code"], "name": kind.get("description") or kind["code"],
                        "share": as_number(stat.get("percentage")) or 0.0,
                        "count": int(as_number(stat.get("count")) or 0),
                        "speed": as_number(stat.get("averageSpeed"))})
    out.sort(key=lambda p: (-p["count"], p["code"]))
    return out


def get_pitch_arsenal(person_id: int, season: int, ttl: float | None = None) -> list[dict]:
    """:func:`parse_pitch_arsenal` for one pitcher's regular season (pitch-tracked, 2008 on)."""
    return parse_pitch_arsenal(api_get(f"/people/{person_id}/stats",
                                       {"stats": "pitchArsenal", "group": "pitching", "season": season},
                                       ttl=ttl))


def get_league_pooled(season: int, ftexts, ttl: float | None = None) -> dict[str, float | None]:
    """Player-level custom stats over all of MLB in ``season``, each formula applied to
    everyone's combined figures (so a per-nine-innings stat is the whole league's runs over its
    outs) -> ``{formula: value}``.  One league-wide request per view the formulas use."""
    ftexts = list(ftexts)
    views = set().union(*(_formula_views(f) for f in ftexts)) if ftexts else set()
    rows_by_view = {view: _league_view_rows(season, view, ttl) for view in views}
    out = {}
    for ftext in ftexts:
        values = {}
        for name in formula.formula_variables(ftext):
            view, key, agg = REGRESSION_VARIABLE_INDEX[name]
            rows = rows_by_view[view]
            values[name] = aggregate_rows(rows, key, agg) if rows else None
        out[ftext] = formula.evaluate_formula(ftext, values)
    return out


# --------------------------------------------------------------------------- #
#  Baserunning from Retrosheet's play-by-play (rows for the RETRO_VIEW)         #
# --------------------------------------------------------------------------- #
# Speed tab: runners sorted into groups.  A regular has "lots of opportunities" -- at least the team's
# average per runner (about ten a team: the lineup and its first few substitutes).  Among regulars the
# runner's bases gained per opportunity is set against MLB's: at or above 125% he is Speedy, at or below
# 75% Slow, in between Smart (he gets his bases by judgment more than by legs).  Measured over 1955-2023
# that gives roughly 2.5 / 4.6 / 3.0 regulars a team.
# A Speedy Bench runner (a pinch-running specialist such as Herb Washington, 1974 Athletics: 128 opportunities
# against a team average of 141, 283% of MLB's rate) is below the regular cut-off but has at least
# BENCH_SHARE of the MLB norm -- the average opportunities of all MLB's runners that season, so the floor is the
# same for every club -- enough chances that the rate isn't luck, and is Speedy by the same 125% test.
# Without that floor a runner with 4 opportunities and one extra base would read 232%.
RUNNER_GROUPS = ("Speedy regulars", "Smart regulars", "Slow regulars", "Speedy bench", "Other")
SPEED_BAND = 0.25
BENCH_SHARE = 0.2


def classify_runners(runners: list[dict], league_rate: float | None, league_average_opps: float | None = None,
                     band: float = SPEED_BAND, bench_share: float = BENCH_SHARE) -> dict:
    """Sort a team's runners (dicts with ``opps``, ``bg`` -- bases gained -- and ``name``) into
    :data:`RUNNER_GROUPS`.  Regulars are those with at least the average opportunities of the
    team's runners; each is Speedy, Smart or Slow by ``bases gained per opportunity / league_rate``
    (judged on the percentage as shown, one decimal).  Below that average, a runner with at least
    ``bench_share`` of ``league_average_opps`` (MLB's average opportunities per runner that season) who
    is Speedy by the same test is Speedy Bench; with no MLB norm there is no bench.  Everyone else --
    and anyone whose rate can't be worked out -- is Other.  Members are listed busiest first (most
    opportunities, then most bases gained, then name).

    -> ``{"average_opps", "bench_floor", "groups": {group: [runner, ...]}}`` (``bench_floor`` is ``None``
    without a norm)"""
    average = sum(r["opps"] for r in runners) / len(runners) if runners else 0.0
    floor = bench_share * league_average_opps if league_average_opps else None
    fast_pct, slow_pct = round((1 + band) * 100, 1), round((1 - band) * 100, 1)
    groups: dict[str, list[dict]] = {g: [] for g in RUNNER_GROUPS}
    for runner in runners:
        group = "Other"
        if runner["opps"] > 0 and runner.get("bg") is not None and league_rate:
            pct = round(runner["bg"] / runner["opps"] / league_rate * 100, 1)
            if runner["opps"] >= average:
                group = "Speedy regulars" if pct >= fast_pct else "Slow regulars" if pct <= slow_pct else "Smart regulars"
            elif floor is not None and runner["opps"] >= floor and pct >= fast_pct:
                group = "Speedy bench"
        groups[group].append(runner)
    for members in groups.values():
        members.sort(key=lambda r: (-r["opps"], -(r.get("bg") or 0), r.get("name", "")))
    return {"average_opps": average, "bench_floor": floor, "groups": groups}


league_view_rows = _league_view_rows  # public name: every player's row of a view for all of MLB in a season
_RETRO_TEAM_CODES: dict[int, dict[int, str]] = {}


def _retro_summary(season: int) -> dict:
    """Retrosheet's summary for ``season``, downloaded and built the first time (a few seconds)."""
    try:
        return retrosheet.load_season(season, CACHE_DIR)
    except retrosheet.RetrosheetError as exc:
        raise MLBStatsError(str(exc)) from exc


def retro_is_cached(season: int) -> bool:
    """Is Retrosheet's ``season`` already on disk (else the first use downloads 4-10 MB)?"""
    return retrosheet.is_cached(CACHE_DIR, season)


def retro_sim_is_cached(season: int) -> bool:
    """Are the Simulator's play tables for ``season`` on disk, in the current version (else the first Play
    downloads Retrosheet's season again)?"""
    return retrosheet.is_sim_cached(CACHE_DIR, season)


def retro_team_codes(season: int, summary: dict, ttl: float | None = None) -> dict[int, str]:
    """``{MLB team id: Retrosheet team code}`` for ``season``: clubs whose wins, losses and runs are
    the same in the two sources (see :func:`retrosheet.match_teams`), then the rest by the players
    they share (:func:`retrosheet.match_rosters`) -- the Negro Leagues' games are counted differently
    by the two, so their records seldom agree.  Only a club whose MLB roster can't be had is paired by
    a close record instead."""
    codes = _RETRO_TEAM_CODES.get(season)
    if codes is None:
        api = {team["id"]: {"w": team["wins"], "l": team["losses"], "rs": team["rs"] or 0, "ra": team["ra"] or 0}
               for group in get_standings(season, ttl) for team in group["teams"]}
        codes = retrosheet.match_teams(summary["teams"], api, close=False)
        left = [tid for tid in api if tid not in codes]
        free = set(summary["teams"]) - set(codes.values())
        unread = []
        if left and free:
            try:
                runners = _retro_summary(season)["runners"]
            except MLBStatsError:
                runners = []
            retro: dict[str, set] = {}
            for runner in runners:
                if runner["team"] in free:
                    retro.setdefault(runner["team"], set()).add(_name_key(runner["first"], runner["last"]))
            players = {}
            for tid in left:
                try:
                    rows = get_player_stats(tid, season, "hitting", ttl)
                except MLBStatsError:
                    rows = []
                if rows:
                    players[tid] = [_full_name_keys(row.get("_name", "")) for row in rows]
                else:
                    unread.append(tid)
            codes.update(retrosheet.match_rosters(retro, players) if retro else {})
            unread += [] if retro else list(players)
        if unread:
            free = {c: summary["teams"][c] for c in set(summary["teams"]) - set(codes.values())}
            codes.update(retrosheet.match_teams(free, {tid: api[tid] for tid in unread}))
        if codes:
            _RETRO_TEAM_CODES[season] = codes
    return codes


# Below this share of a club's outs in the field (MLB's innings pitched) Retrosheet's play-by-play is a
# sample of its season, not the whole of it -- the Negro Leagues, mostly: a third or so of their games.
RETRO_FULL_COVERAGE = 0.98


def retro_coverage(team_id: int, season: int, ttl: float | None = None) -> float | None:
    """How much of a team-season Retrosheet's play-by-play has: the outs its fielders made there, as a
    share of MLB's innings pitched for the club (1.0 for an AL or NL club; less where only some games'
    accounts survive).  None where it can't be told (no play-by-play, or the club isn't in it).  Only
    a Negro Leagues club can be short; for the others the season's team summary is used if it's on
    hand but never downloaded just for this (the AL's and NL's play-by-play is complete)."""
    def compute() -> dict[int, float]:
        try:
            negro = any(g["league_id"] in NEGRO_LEAGUE_IDS for g in get_standings(season, ttl))
            if not negro and not retrosheet.is_team_cached(CACHE_DIR, season):
                return {}
            summary = retrosheet.load_team_season(season, CACHE_DIR)
            codes = retro_team_codes(season, summary, ttl)
            pitching = _team_stat_blocks(season, "pitching", ttl)
        except (retrosheet.RetrosheetError, MLBStatsError):
            return {}
        out = {}
        for tid, code in codes.items():
            mlb_outs = innings_to_outs(pitching.get(tid, {}).get("inningsPitched") or 0)
            outs = (summary.get("allowed", {}).get(code) or {}).get("outs")
            if mlb_outs and outs is not None:
                out[tid] = outs / mlb_outs
        return out
    return _memo(("coverage", season), ttl, compute).get(team_id)


def retro_sample(team_id: int, season: int, ttl: float | None = None) -> float | None:
    """:func:`retro_coverage` where Retrosheet has only a sample of the team-season (below
    :data:`RETRO_FULL_COVERAGE`), else None -- its figures then cover only that share of the games."""
    coverage = retro_coverage(team_id, season, ttl)
    return coverage if coverage is not None and coverage < RETRO_FULL_COVERAGE else None


class SampledValue(float):
    """A figure that rests on Retrosheet's play-by-play of only ``coverage`` of its team-season
    (:func:`retro_sample`) -- shown with a *."""

    def __new__(cls, value: float, coverage: float):
        made = super().__new__(cls, value)
        made.coverage = coverage
        return made


RETRO_TEAM_VARIABLES = frozenset(f"{view}: {label}" for view, group, label, _key in DEPENDENT_VARIABLES
                                 if group.startswith("retrosheet"))


def mark_sampled(value, without, coverage: float | None):
    """``value`` as a :class:`SampledValue` when ``coverage`` says the team-season's play-by-play is a
    sample and ``without`` (the same figure worked out with no Retrosheet figures) differs -- so it
    rests on that sample; else ``value`` unchanged."""
    if coverage is None or value is None or value == without or isinstance(value, bool):
        return value
    return SampledValue(value, coverage) if isinstance(value, (int, float)) else value


def sample_note(coverage: float, team: str, season: int) -> str:
    """The footnote for figures marked * (Retrosheet's play-by-play of only part of a season)."""
    return (f"* From Retrosheet\u2019s play-by-play, which has only {coverage:.0%} of the innings the {season} "
            f"{team} played in MLB\u2019s records: a sample of the season, not the whole of it.")


def _no_retro_team(team_id: int, season: int, ttl: float | None) -> MLBStatsError:
    """The error for a team-season not found among Retrosheet's clubs, naming the club."""
    try:
        name = next((t["name"] for g in get_standings(season, ttl) for t in g["teams"] if t["id"] == team_id), None)
    except MLBStatsError:
        name = None
    return MLBStatsError(f"couldn't find the {season} {name or f'team {team_id}'} in Retrosheet's play-by-play"
                         + (" (it may not have that club's games)" if name else ""))


def retro_team_is_cached(season: int) -> bool:
    """Is the Safe/Out tab's Retrosheet summary for ``season`` on disk?"""
    return retrosheet.is_team_cached(CACHE_DIR, season)


def safe_out_rates(counts: dict) -> dict:
    """Per-inning rates and the three comparison ratios from a team's (or MLB's) Safe/Out counts.
    Innings are counted as pitchers' are, outs / 3, so outs per inning is always 3."""
    outs = counts.get("outs") or 0
    innings = outs / 3
    per = lambda key: counts[key] / innings if innings else None
    on_base, scored = per("reach1"), per("runs")
    return {"innings": innings, "outs_inn": 3.0 if innings else None, "on_base_inn": on_base,
            "second_inn": per("reach2"), "third_inn": per("reach3"), "scored_inn": scored,
            "on_base_outs": on_base / 3 if on_base is not None else None,
            "scored_outs": scored / 3 if scored is not None else None,
            "scored_on_base": counts["runs"] / counts["reach1"] if counts.get("reach1") else None}


def get_safe_out(season: int, ttl: float | None = None) -> dict:
    """Every club's Safe/Out counts for ``season`` from Retrosheet's play-by-play (see
    :func:`retrosheet.parse_team_running`), with its MLB name and league, and MLB's totals: what its
    batters did (``teams``) and what it allowed its opponents (``allowed``; ``official_runs`` there
    is runs allowed).  ``sample`` is :func:`retro_sample` -- set where the play-by-play has only part
    of a club's season.  MLB's totals are the same either way -- every play is one club's at bat and
    another's in the field.
    -> ``{"teams": [{"id", "name", "league", "official_runs", <SAFE_OUT_KEYS>, **rates}, ...],
    "allowed": [...], "mlb": {...}}``"""
    try:
        summary = retrosheet.load_team_season(season, CACHE_DIR)
    except retrosheet.RetrosheetError as exc:
        raise MLBStatsError(str(exc)) from exc
    codes = retro_team_codes(season, summary, ttl)
    info = {}
    for group in get_standings(season, ttl):
        for team in group["teams"]:
            info[team["id"]] = (team["name"], group["league"])
    try:
        abbrevs = {tid: t.get("abbrev") for tid, t in get_team_map(season, ttl).items()}
    except MLBStatsError:
        abbrevs = {}
    by_code = {code: tid for tid, code in codes.items()}

    def lines(side: str, official: str) -> list[dict]:
        rows = []
        for code, counts in summary[side].items():
            tid = by_code.get(code)
            name, league = info.get(tid, (code, ""))
            short_league = "".join(w[0] for w in league.split()[:-1]) + "L" if league.endswith("League") else league
            row = {"id": tid, "code": code, "name": name, "league": league, "lg": short_league,
                   "abbrev": abbrevs.get(tid) or code,
                   "official_runs": summary["teams"].get(code, {}).get(official),
                   "sample": retro_sample(tid, season, ttl) if tid is not None else None}
            row.update(counts)
            row.update(safe_out_rates(counts))
            rows.append(row)
        return rows
    rows = lines("running", "rs")
    total = {k: sum(r[k] for r in rows) for k in retrosheet.SAFE_OUT_KEYS}
    total["official_runs"] = sum(r["official_runs"] or 0 for r in rows)
    total.update(safe_out_rates(total), name="MLB", league="", lg="", abbrev="MLB")
    return {"teams": rows, "allowed": lines("allowed", "ra"), "mlb": total}


_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _name_key(first: str, last: str) -> tuple[str, str]:
    """A name reduced for matching: first initial and last name, lower case, no accents,
    punctuation or generational suffix."""
    import unicodedata

    def plain(text: str) -> str:
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        return "".join(ch for ch in text.lower() if ch.isalpha() or ch == " ").strip()

    words = [w for w in plain(last).split() if w not in _NAME_SUFFIXES]
    return (plain(first)[:1], "".join(words))


def _full_name_keys(full: str) -> set[tuple[str, str]]:
    """Every :func:`_name_key` a full name could have, split anywhere between words -- MLB gives one
    string, and a first name can be two words ("Pee Wee Reese" is Retrosheet's Pee Wee / Reese)."""
    words = full.split()
    return {_name_key(" ".join(words[:i]), " ".join(words[i:])) for i in range(1, len(words))} or {_name_key(full, "")}


def retro_ids(pids, ttl: float | None = None) -> dict[int, str]:
    """``{MLB player id: Retrosheet player id}`` for ``pids``, from MLB's cross-references
    (``/people?hydrate=xrefId``, a hundred players a request).  Players MLB has no Retrosheet id
    for are left out; so is everyone if the request fails (the callers fall back to names)."""
    pids = sorted({int(p) for p in pids if isinstance(p, int)})
    out: dict[int, str] = {}
    for i in range(0, len(pids), 100):
        try:
            data = api_get("/people", {"personIds": ",".join(map(str, pids[i:i + 100])), "hydrate": "xrefId"}, ttl)
        except MLBStatsError:
            continue
        for person in data.get("people", []):
            for xref in person.get("xrefIds", []):
                if xref.get("xrefType") == "retrosheet" and xref.get("xrefId"):
                    out[person["id"]] = xref["xrefId"]
                    break
    return out


class _RetroRoster:
    """A team-season's MLB players, to be found by Retrosheet id: by MLB's cross-reference where it
    has one, else by name -- every way the full name could split (:func:`_full_name_keys`), and only
    where it fits one player (two brothers on one team share a first initial)."""

    def __init__(self, team_id: int, season: int, groups, ttl: float | None = None):
        self.rows: dict[int, dict] = {}
        for group in groups:
            try:
                for row in get_player_stats(team_id, season, group, ttl):
                    if row.get("_id") is not None:
                        self.rows.setdefault(row["_id"], row)
            except MLBStatsError:
                pass
        self.by_retro = {retro: self.rows[pid] for pid, retro in retro_ids(self.rows, ttl).items()}
        self.xref_ids = {row["_id"] for row in self.by_retro.values()}
        self.by_name: dict[tuple[str, str], list[dict]] = {}
        for row in self.rows.values():
            for key in _full_name_keys(row.get("_name", "")):
                self.by_name.setdefault(key, []).append(row)

    def find(self, retro_id: str, first: str, last: str) -> dict | None:
        row = self.by_retro.get(retro_id)
        if row is not None:
            return row
        matches = [m for m in self.by_name.get(_name_key(first, last), [])
                   if m["_id"] not in self.xref_ids]  # not someone known by another Retrosheet id
        return matches[0] if len(matches) == 1 else None


def retro_team_rows(team_id: int, season: int, ttl: float | None = None) -> list[dict]:
    """One row per runner of a team-season, from Retrosheet: the counts of :data:`RETRO_VIEW`
    keyed as :data:`retrosheet.KEYS`, plus ``_name`` and ``_id``.  A runner is given his MLB id
    where he is found on the team's MLB roster (:class:`_RetroRoster`), so these rows join up with
    the other views; otherwise his Retrosheet id stands in."""
    summary = _retro_summary(season)
    code = retro_team_codes(season, summary, ttl).get(team_id)
    if code is None:
        raise _no_retro_team(team_id, season, ttl)
    roster = _RetroRoster(team_id, season, ("hitting", "pitching"), ttl)
    rows = []
    for runner in summary["runners"]:
        if runner["team"] != code:
            continue
        mlb = roster.find(runner["id"], runner["first"], runner["last"])
        if mlb is not None:
            rows.append(dict(runner, _id=mlb["_id"], _name=mlb["_name"], _pos=""))
        else:
            rows.append(dict(runner, _id=runner["id"], _name=runner["name"], _pos=""))
    return rows


def retro_team_fielders(team_id: int, season: int, ttl: float | None = None) -> tuple[dict, dict]:
    """A team-season's fielders against runners, from Retrosheet's play-by-play (the Simulator's summary,
    :func:`retrosheet.parse_sim`) -> ``({MLB id: {position: counts}}, {position: MLB's counts})``, positions
    as "P", "C", "1B" ... and counts as :data:`retrosheet.ARM_KEYS`.  A fielder not found on the team's MLB
    roster (:class:`_RetroRoster`) is left out."""
    try:
        summary = retrosheet.load_sim_season(season, CACHE_DIR)
    except retrosheet.RetrosheetError as exc:
        raise MLBStatsError(str(exc)) from exc
    if not summary.get("fielders"):
        raise MLBStatsError(f"Retrosheet's {season} summary has no fielders")
    code = retro_team_codes(season, summary, ttl).get(team_id)
    if code is None:
        raise _no_retro_team(team_id, season, ttl)
    position = dict(zip("123456789", ("P", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF")))
    roster = _RetroRoster(team_id, season, ("hitting", "pitching", "fielding"), ttl)
    out: dict = {}
    for f in summary["fielders"]:
        if f["team"] != code:
            continue
        mlb = roster.find(f["id"], f["first"], f["last"])
        if mlb is not None:
            mine = out.setdefault(mlb["_id"], {})
            for digit, counts in f["pos"].items():
                mine[position[digit]] = [a + b for a, b in zip(mine.get(position[digit], [0] * len(counts)), counts)]
    league = {position[d]: c for d, c in summary["fielding_league"].items()}
    return out, league


def retro_team_tags(team_id: int, season: int, ttl: float | None = None) -> tuple[dict, dict]:
    """A team-season's infielders covering on steals, from Retrosheet (the Simulator's summary) -> ``({MLB id:
    {position: [attempts, caught]}}, {position: the club's [attempts, caught]})``, positions "2B", "SS" (steals
    of second) and "3B" (of third)."""
    try:
        summary = retrosheet.load_sim_season(season, CACHE_DIR)
    except retrosheet.RetrosheetError as exc:
        raise MLBStatsError(str(exc)) from exc
    if "tags" not in summary:
        raise MLBStatsError(f"Retrosheet's {season} summary has no steal coverage")
    code = retro_team_codes(season, summary, ttl).get(team_id)
    if code is None:
        raise _no_retro_team(team_id, season, ttl)
    position = {"4": "2B", "5": "3B", "6": "SS"}
    names = {f["id"]: (f["first"], f["last"]) for f in summary.get("fielders", [])}
    roster = _RetroRoster(team_id, season, ("hitting", "pitching", "fielding"), ttl)
    out: dict = {}
    for key, by_pos in summary["tags"].items():
        who, team = key.split(" ", 1)
        if team != code:
            continue
        first, last = names.get(who, ("", who))
        mlb = roster.find(who, first, last)
        if mlb is not None:
            mine = out.setdefault(mlb["_id"], {})
            for digit, (att, cs) in by_pos.items():
                old = mine.get(position[digit], [0, 0])
                mine[position[digit]] = [old[0] + att, old[1] + cs]
    club = {position[d]: c for d, c in (summary["tags_club"].get(code) or {}).items()}
    return out, club


def retro_park(team_id: int, season: int, ttl: float | None = None) -> dict:
    """A club's home park that season from Retrosheet's play-by-play (the Simulator's summary,
    :func:`retrosheet.parse_sim`): the park it played most of its home games in -> ``{"site", "home": {class:
    plate appearances there, both teams batting}, "road": {class: the same in its road games}}``."""
    try:
        summary = retrosheet.load_sim_season(season, CACHE_DIR)
    except retrosheet.RetrosheetError as exc:
        raise MLBStatsError(str(exc)) from exc
    if not summary.get("parks"):
        raise MLBStatsError(f"Retrosheet's {season} summary has no parks")
    code = retro_team_codes(season, summary, ttl).get(team_id)
    if code is None:
        raise _no_retro_team(team_id, season, ttl)
    mine = [(sum(c.values()), key.split(" ")[0], c) for key, c in summary["parks"].items() if key.endswith(f" {code}")]
    if not mine or code not in summary["road"]:
        raise MLBStatsError(f"no home and road games for {code} in Retrosheet's {season} play-by-play")
    _n, site, home = max(mine, key=lambda m: m[0])
    return {"site": site, "home": home, "road": summary["road"][code]}


def retro_rbi_is_cached(season: int) -> bool:
    """Is the Players Batted In tab's Retrosheet summary for ``season`` on disk?"""
    return retrosheet.is_rbi_cached(CACHE_DIR, season)


def get_batted_in(team_id: int, season: int, pid: int, name: str, ttl: float | None = None) -> dict:
    """Whom one batter drove in over a regular season with one team, from Retrosheet's play-by-play
    (see :func:`retrosheet.parse_batted_in`).  The batter (MLB id ``pid``) is found among the team's
    Retrosheet batters by MLB's cross-reference to his Retrosheet id, else by ``name``; the runners he
    drove in are named as on the team's MLB roster where found there (:class:`_RetroRoster`), else
    as Retrosheet has them.
    -> ``{"rbi", "found", "rows": [{"name", "self", "own", "from1", "from2", "from3", "total"}, ...]}``,
    the rows most runs first (the batter himself, for his home runs, among them); ``found`` is False
    when the batter isn't among those Retrosheet credits with an RBI for the team."""
    try:
        summary = retrosheet.load_rbi_season(season, CACHE_DIR)
    except retrosheet.RetrosheetError as exc:
        raise MLBStatsError(str(exc)) from exc
    code = retro_team_codes(season, _retro_summary(season), ttl).get(team_id)
    if code is None:
        raise _no_retro_team(team_id, season, ttl)
    batters = summary["batted_in"].get(code, {})
    names = summary["names"]
    roster = _RetroRoster(team_id, season, ("hitting",), ttl)
    batter = retro_ids([pid], ttl).get(pid)
    if batter is None:  # no cross-reference: by name, and only if just one of the team's batters has it
        want = _full_name_keys(name)
        found = [b for b in batters if _name_key(*names.get(b, ("", b))) in want]
        batter = found[0] if len(found) == 1 else None
    if batter not in batters:  # no runs batted in -- or not to be told apart from a teammate
        return {"rbi": 0, "rows": [], "found": False}
    rows = []
    for runner, (own, from1, from2, from3) in batters[batter].items():
        f, l = names.get(runner, ("", runner))
        mlb = roster.find(runner, f, l)
        rows.append({"name": mlb["_name"] if mlb else f"{f} {l}".strip(), "self": runner == batter,
                     "own": own, "from1": from1, "from2": from2, "from3": from3,
                     "total": own + from1 + from2 + from3})
    rows.sort(key=lambda r: (-r["total"], -r["from3"], r["name"]))
    return {"rbi": sum(r["total"] for r in rows), "rows": rows, "found": True}


def _retro_staff(season: int) -> dict:
    try:
        return retrosheet.load_staff_season(season, CACHE_DIR)
    except retrosheet.RetrosheetError as exc:
        raise MLBStatsError(str(exc)) from exc


def _retro_rbi(season: int) -> dict:
    try:
        return retrosheet.load_rbi_season(season, CACHE_DIR)
    except retrosheet.RetrosheetError as exc:
        raise MLBStatsError(str(exc)) from exc


def _retro_code(team_id: int, season: int, ttl: float | None) -> str:
    code = retro_team_codes(season, _retro_summary(season), ttl).get(team_id)
    if code is None:
        raise _no_retro_team(team_id, season, ttl)
    return code


def _retro_counts(season: int, group: str) -> tuple[dict, dict]:
    """``({team code: {retro id: counts}}, {retro id: (first, last)})`` of a Retrosheet view's
    group ("retrosheet-relief" or "retrosheet-rbi") for ``season``."""
    if group == "retrosheet-relief":
        staff = _retro_staff(season)
        return ({code: retrosheet.relief_counts(games) for code, games in staff["games"].items()},
                staff["names"])
    summary = _retro_rbi(season)
    return ({code: retrosheet.batted_in_counts(batters) for code, batters in summary["batted_in"].items()},
            summary["names"])


def _retro_view_rows(team_id: int, season: int, group: str, roster_groups, ttl: float | None) -> list[dict]:
    """One row per player of a team-season from a Retrosheet view's summary, given his MLB id and
    name where he is found on the team's MLB roster (:class:`_RetroRoster`), so the rows join up
    with the other views; otherwise his Retrosheet id and name stand in."""
    counts, names = _retro_counts(season, group)
    mine = counts.get(_retro_code(team_id, season, ttl), {})
    roster = _RetroRoster(team_id, season, roster_groups, ttl)
    rows = []
    for retro_id, values in mine.items():
        first, last = names.get(retro_id, ("", retro_id))
        mlb = roster.find(retro_id, first, last)
        if mlb is not None:
            rows.append(dict(values, _id=mlb["_id"], _name=mlb["_name"], _pos=""))
        else:
            rows.append(dict(values, _id=retro_id, _name=f"{first} {last}".strip(), _pos=""))
    return rows


def retro_relief_rows(team_id: int, season: int, ttl: float | None = None) -> list[dict]:
    """The :data:`RELIEF_VIEW` rows of a team-season: each pitcher's counts of
    :data:`retrosheet.RELIEF_VARIABLES` from Retrosheet's game records."""
    return _retro_view_rows(team_id, season, "retrosheet-relief", ("pitching",), ttl)


def retro_batted_in_rows(team_id: int, season: int, ttl: float | None = None) -> list[dict]:
    """The :data:`BATTED_IN_VIEW` rows of a team-season: each batter's counts of
    :data:`retrosheet.BATTED_IN_VARIABLES` from Retrosheet's play-by-play."""
    return _retro_view_rows(team_id, season, "retrosheet-rbi", ("hitting",), ttl)


def retro_league_rows(season: int, group: str) -> list[dict]:
    """Every player-team row of a Retrosheet view's group for all of MLB in ``season``, keyed by
    Retrosheet id (league-wide figures only need them added up)."""
    counts, names = _retro_counts(season, group)
    return [dict(values, _id=retro_id, _name=" ".join(names.get(retro_id, ("", retro_id))).strip(), _pos="")
            for team in counts.values() for retro_id, values in team.items()]


def retro_team_totals(team_id: int, season: int, group: str, ttl: float | None = None) -> dict:
    """One club's Team-level Retrosheet figures for ``season``: its Safe/Out counts
    ("retrosheet-safe-out"), what it allowed ("retrosheet-safe-out-allowed"), or its staff's
    :data:`retrosheet.RELIEF_VARIABLES` added up ("retrosheet-relief")."""
    if group == "retrosheet-relief":
        games = _retro_staff(season)["games"].get(_retro_code(team_id, season, ttl), [])
        total = {k: 0 for k in retrosheet.RELIEF_KEYS}
        for counts in retrosheet.relief_counts(games).values():
            for k, v in counts.items():
                total[k] += v
        return total
    try:
        summary = retrosheet.load_team_season(season, CACHE_DIR)
    except retrosheet.RetrosheetError as exc:
        raise MLBStatsError(str(exc)) from exc
    code = retro_team_codes(season, summary, ttl).get(team_id)
    if code is None:
        raise _no_retro_team(team_id, season, ttl)
    side = {"retrosheet-safe-out": "running", "retrosheet-safe-out-allowed": "allowed"}[group]
    return dict(summary[side].get(code, {}))


def retro_staff_is_cached(season: int) -> bool:
    """Is the Bullpen Support tab's Retrosheet summary for ``season`` on disk?"""
    return retrosheet.is_staff_cached(CACHE_DIR, season)


def _relief_figures(apps: list[dict]) -> dict:
    """A reliever's (or a role's) relief appearances -> counts and averages: ``apps``, ``outs`` and
    ``avg_outs`` per appearance; ``sit`` appearances begun in a save situation (MLB's definition, see
    :func:`retrosheet.save_situation`), ``sit_outs`` / ``avg_sit_outs`` in them; MLB's official
    ``holds``, ``saves`` and ``blown`` saves -- the last split into ``blown_holds`` and ``blown_saves``
    (:func:`_split_blown`) -- and how many of each came out of those situations (``sit_holds`` ...)."""
    sit = [a for a in apps if a["save_sit"]]
    out = {"apps": len(apps), "outs": sum(a["outs"] for a in apps), "sit": len(sit),
           "sit_outs": sum(a["outs"] for a in sit)}
    for key in ("holds", "saves", "blown", "blown_holds", "blown_saves", "sv_mlb"):
        out[key] = sum(a[key] for a in apps)
        out[f"sit_{key}"] = sum(a[key] for a in sit)
    out["avg_outs"] = out["outs"] / out["apps"] if out["apps"] else None
    out["avg_sit_outs"] = out["sit_outs"] / out["sit"] if out["sit"] else None
    return out


def _split_blown(a: dict):
    """A blown hold -- the Bullpen Support tab's own stat, not MLB's -- is a blown save by a reliever who
    handed the game on to another (didn't finish it), when his team later took the lead back; every
    other blown save stays a blown save.  The two always add up to MLB's blown saves."""
    a["blown_holds"] = a["blown"] if a["blown"] and not a["gf"] and a["regained"] else 0
    a["blown_saves"] = a["blown"] - a["blown_holds"]


CURRENT_SAVE_RULE_FROM = 1975  # today's save rule 9.19; earlier official saves were credited under looser ones
OFFICIAL_HOLDS_FROM = 1999  # MLB's records have holds and blown saves from this season on (saves always)


def _official_relief(pid: int, season: int, ttl: float | None) -> dict:
    """A pitcher's official holds, saves and blown saves game by game, from MLB's game log ->
    ``{(date "YYYYMMDD", game number 1 or 2): (holds, saves, blown saves)}``."""
    out = {}
    for r in get_game_log(pid, "pitching", season, ttl=ttl):
        key = ((r.get("_date") or "").replace("-", ""), r.get("_game_number") or 1)
        out[key] = tuple(int(as_number(r.get(k)) or 0) for k in ("holds", "saves", "blownSaves"))
    return out


def holds_saves_report(team_id: int, season: int, rows: list[dict], ttl: float | None = None) -> dict:
    """The Bullpen Support tab's figures for a team-season: its pitchers sorted into the Prevention
    tab's roles (:func:`classify_pitchers` over ``rows``, the team's MLB pitching rows), with what
    Retrosheet's game records say of each (:func:`retrosheet.parse_staff`).

    -> ``{"rotation": [{"id", "name", "starts", "outs", "avg_outs", "dw", "dl", "nd_w", "nd_l", "w", "l",
    "followers": [...]}, ...], "other_starters" (the same over the starts by pitchers outside the
    Rotation, with their ``names``), "all_starts" (every game), "totals" (Rotation's the same),
    "long_relief": [{"id", "name", **relief figures}, ...], "closers": [...], "other": [...],
    "totals": {role: figures},
    "unmatched": [names]}``.  A starter's ``dw`` / ``dl`` are his own wins and losses in his starts (the
    official scorer's decisions), ``nd_w`` / ``nd_l`` his no-decisions the team won / lost, ``w`` / ``l``
    the team's record in his starts -- so ``w`` = ``dw`` + ``nd_w`` and ``l`` = ``dl`` + ``nd_l``.  His
    ``followers`` carry the same for each reliever in those games -- his own decisions ``dw`` / ``dl``
    and no-decisions ``nd_w`` / ``nd_l``, the team's ``w`` / ``l`` with him -- and are every pitcher who
    pitched after him in his starts, with his Prevention ``role`` (Other included, so their decisions
    and the starter's no-decisions balance).  A follower's ``nd_w`` / ``nd_l`` leave out the games he had a
    hold, save or blown save (blown holds included) without a decision -- those are in his hold and save columns --
    so they are only the games he pitched in with neither a decision nor a hold or save situation: ``direct`` (came in right after him), ``later``, ``games``
    (either), ``outs`` / ``avg_outs`` they recorded in those games, and the team's ``w`` / ``l`` in
    them, most games first, with the ``holds``, ``saves`` and ``blown`` saves they had in them.
    Relief figures are :func:`_relief_figures` over relief appearances only.  The saves are by today's
    rule: official from :data:`CURRENT_SAVE_RULE_FROM` on, and counted by it before (when saves were
    credited under earlier rules); ``sv_mlb`` beside them are MLB's own, by the rule of their time
    (before 1969, MLB's retroactive saves by the 1969 rule), from its game logs; the holds and blown saves are MLB's official ones (its game logs) from
    :data:`OFFICIAL_HOLDS_FROM` on, and before that -- MLB has none -- counted from Retrosheet's
    play-by-play by MLB's definitions (``"official"`` in the result says which).  The save
    situations and innings are Retrosheet's."""
    try:
        staff = retrosheet.load_staff_season(season, CACHE_DIR)
    except retrosheet.RetrosheetError as exc:
        raise MLBStatsError(str(exc)) from exc
    code = retro_team_codes(season, _retro_summary(season), ttl).get(team_id)
    if code is None:
        raise _no_retro_team(team_id, season, ttl)
    roles = classify_pitchers(rows)["roles"]
    role_of = {r["_id"]: role for role, members in roles.items() for r in members}
    roster = _RetroRoster(team_id, season, ("pitching",), ttl)
    names = staff["names"]
    games = []
    to_mlb: dict[str, dict] = {}
    for game in staff["games"].get(code, []):
        apps = []
        for values in game["pitchers"]:
            a = dict(zip(retrosheet.APPEARANCE_FIELDS, values))
            if a["pitcher"] not in to_mlb:
                to_mlb[a["pitcher"]] = roster.find(a["pitcher"], *names.get(a["pitcher"], ("", a["pitcher"])))
            mlb = to_mlb[a["pitcher"]]
            a["mlb"] = mlb["_id"] if mlb else None
            a["role"] = role_of.get(a["mlb"])
            apps.append(a)
        games.append((game["won"], apps))
    name_of = {r["_id"]: r.get("_name", "?") for r in rows}
    official_era = season >= OFFICIAL_HOLDS_FROM
    official = {}  # each reliever's official holds, saves and blown saves game by game (MLB's game logs, 1901 on)
    if season >= DAILY_FROM_SEASON:
        pids = sorted({a["mlb"] for _won, apps in games for a in apps if not a["gs"] and a["mlb"] is not None})
        with ThreadPoolExecutor(max_workers=6) as pool:
            for pid, log in zip(pids, pool.map(lambda p: _official_relief(p, season, ttl), pids)):
                official[pid] = log
    for game, (_won, apps) in zip(staff["games"].get(code, []), games):
        key = (game["gid"][3:11], 2 if game["gid"][-1] == "2" else 1)  # Retrosheet: 0 one game, 1 / 2 a doubleheader's
        for a in apps:
            got = official.get(a["mlb"], {}).get(key)
            a["sv_mlb"] = got[1] if got else a["sv"]  # MLB's save, by the rule of the time
            if official_era:
                a["holds"], a["saves"], a["blown"] = got or (0, 0, 0)
            else:  # holds and blown saves by MLB's definitions; the save as officially credited
                a["holds"], a["saves"], a["blown"] = a["hold"], a["sv"], a["blown"]
    if season < CURRENT_SAVE_RULE_FROM:  # saves by today's rule, like everything else here
        for _won, apps in games:
            for a in apps:
                a["saves"] = a["sv_now"]
    for _won, apps in games:
        for a in apps:
            _split_blown(a)

    def starter_figures(starts: list) -> dict:
        """A set of games ``[(won, apps), ...]`` -> the starters' figures in them and the pitchers who
        followed (see the docstring above)."""
        outs = sum(apps[0]["outs"] for _won, apps in starts)
        followers: dict = {}
        for won, apps in starts:
            seen = set()
            for i, a in enumerate(apps[1:], start=1):
                if a["mlb"] in seen:
                    continue
                seen.add(a["mlb"])
                f = followers.setdefault(a["mlb"], {"id": a["mlb"], "role": a["role"] or "?",
                                                    "name": name_of.get(a["mlb"]) or " ".join(names.get(a["pitcher"], ("", a["pitcher"]))),
                                                    "direct": 0, "later": 0, "games": 0, "outs": 0, "w": 0, "l": 0,
                                                    "holds": 0, "saves": 0, "blown": 0, "blown_holds": 0, "blown_saves": 0,
                                                    "sv_mlb": 0,
                                                    "dw": 0, "dl": 0, "nd_w": 0, "nd_l": 0})
                f["dw"] += a["w"]
                f["dl"] += a["l"]
                if not a["w"] and not a["l"] and not (a["holds"] or a["saves"] or a["blown"]):
                    f["nd_w" if won else "nd_l"] += 1  # a hold or save situation is counted in those columns instead
                for k in ("holds", "saves", "blown", "blown_holds", "blown_saves", "sv_mlb"):
                    f[k] += a[k]
                f["direct" if i == 1 else "later"] += 1
                f["games"] += 1
                f["outs"] += a["outs"]
                f["w" if won else "l"] += 1
        for f in followers.values():
            f["avg_outs"] = f["outs"] / f["games"]
        nd = [won for won, apps in starts if not apps[0]["w"] and not apps[0]["l"]]
        return {"starts": len(starts), "outs": outs, "avg_outs": outs / len(starts) if starts else None,
                "dw": sum(apps[0]["w"] for _won, apps in starts), "dl": sum(apps[0]["l"] for _won, apps in starts),
                "nd_w": sum(1 for won in nd if won), "nd_l": sum(1 for won in nd if not won),
                "w": sum(1 for won, _a in starts if won), "l": sum(1 for won, _a in starts if not won),
                "followers": sorted(followers.values(), key=lambda f: (-f["games"], -f["direct"], f["name"]))}

    played = [(won, apps) for won, apps in games if apps]
    rotation_ids = {row["_id"] for row in roles["Rotation"]}
    rotation = [dict(starter_figures([g for g in played if g[1][0]["mlb"] == row["_id"]]),
                     id=row["_id"], name=row.get("_name", "?")) for row in roles["Rotation"]]
    rot_total = starter_figures([g for g in played if g[1][0]["mlb"] in rotation_ids])
    other_starters = starter_figures([g for g in played if g[1][0]["mlb"] not in rotation_ids])
    other_starters["names"] = sorted({name_of.get(g[1][0]["mlb"]) or " ".join(names.get(g[1][0]["pitcher"], ("", "?")))
                                      for g in played if g[1][0]["mlb"] not in rotation_ids})
    all_starts = starter_figures(played)

    def decisions(apps: list[tuple[bool, dict]]) -> dict:
        """Relief appearances ``[(team won, app), ...]`` -> his own ``dw`` / ``dl``, his ``nd_w`` / ``nd_l``
        (no decision and no hold or save situation, as for a starter's followers) and the team's ``w`` / ``l``."""
        out = {"dw": 0, "dl": 0, "nd_w": 0, "nd_l": 0, "w": 0, "l": 0}
        for won, a in apps:
            out["dw"] += a["w"]
            out["dl"] += a["l"]
            if not a["w"] and not a["l"] and not (a["holds"] or a["saves"] or a["blown"]):
                out["nd_w" if won else "nd_l"] += 1
            out["w" if won else "l"] += 1
        return out

    def relievers(role: str) -> tuple[list[dict], dict]:
        out, every = [], []
        for row in roles[role]:
            mine = [(won, a) for won, game in games for a in game if a["mlb"] == row["_id"]]
            apps = [(won, a) for won, a in mine if not a["gs"]]
            every += apps
            out.append(dict(_relief_figures([a for _w, a in apps]), **decisions(apps), id=row["_id"],
                            name=row.get("_name", "?"), gs=sum(a["gs"] for _w, a in mine)))
        return out, dict(_relief_figures([a for _w, a in every]), **decisions(every), gs=sum(f["gs"] for f in out))

    long_relief, lr_total = relievers("Long Relief")
    closers, cl_total = relievers("Closers")
    others, ot_total = relievers("Other")
    unmatched = sorted({names.get(p, ("", p))[1] or p for p, m in to_mlb.items() if m is None})
    return {"rotation": rotation, "long_relief": long_relief, "closers": closers, "other": others,
            "totals": {"Rotation": rot_total, "Long Relief": lr_total, "Closers": cl_total, "Other": ot_total},
            "other_starters": other_starters, "all_starts": all_starts,
            "unmatched": unmatched,
            "official": official_era}


# --------------------------------------------------------------------------- #
#  Postseason bracket.                                                        #
# --------------------------------------------------------------------------- #
# The rounds by the API's game type, earliest first: the wild card round (a single game
# 2012-2019, a series since 2020), the division series (1981, then from 1995), the league
# championship series (from 1969) and the World Series (from 1903; none in 1904 or 1994).
# Tiebreaker games ("R") are regular-season games and are left out.
POSTSEASON_ROUNDS = ("F", "D", "L", "W")
POSTSEASON_FIRST_SEASON = 1903
POSTSEASON_LEAGUES = {103: "AL", 104: "NL"}
_DROPPED_STATES = ("Postponed", "Cancelled")  # a postponed game is listed again, rescheduled


def _is_placeholder(team: dict) -> bool:
    """A not-yet-decided slot in an upcoming round: "NYY/BOS", "AL Higher Seed",
    "Lower Seed League Champion"..."""
    name, abbr = team.get("name", ""), team.get("abbreviation", "")
    return "/" in abbr or "Seed" in name or "Champion" in name


def parse_postseason(payload: dict) -> list[dict]:
    """The ``/schedule/postseason/series`` payload as a list of series, each::

        {"id": "D_1", "round": "D", "title": "AL Division Series", "league": "AL" | "NL" | None,
         "best_of": 5, "slots": 5, "teams": [team, team], "games": [game, ...],
         "wins": {team_id: n}, "winner": team_id | None}

    ``teams`` puts game 1's home club first; a team is ``{"id", "name", "abbr",
    "placeholder"}``.  A game is ``{"number", "date", "state": "final" | "live" | "scheduled",
    "tie", "if_necessary", "away", "home", "score": {team_id: runs}, "venue",
    "description"}``, in series order.  Postponed listings are dropped (the game appears
    again when it was played), and so are repeat listings of the same game."""
    out = []
    for entry in payload.get("series", []):
        round_ = entry.get("series", {}).get("gameType")
        # the API sometimes lists a game twice (1951, 1959 and 1971 World Series, 1991 ALCS,
        # 2011's suspended ALDS game 1): one entry per gamePk, the last listing kept
        unique = {g.get("gamePk", id(g)): g for g in entry.get("games", [])
                  if g.get("status", {}).get("detailedState") not in _DROPPED_STATES}
        if round_ not in POSTSEASON_ROUNDS or not unique:
            continue
        # ...and sometimes lumps two series under one id (2003 and 2004's division series each
        # hold an AL and an NL series): one series per pair of clubs
        matchups: dict[frozenset, list[dict]] = {}
        for g in unique.values():
            pair = frozenset(g["teams"][side]["team"]["id"] for side in ("home", "away"))
            matchups.setdefault(pair, []).append(g)
        sid = entry["series"]["id"]
        for n, games in enumerate(matchups.values(), start=1):
            out.append(_parse_series(sid if n == 1 else f"{sid}.{n}", round_, games))
    return out


def _parse_series(sid: str, round_: str, games: list[dict]) -> dict:
    """One series of :func:`parse_postseason` from its games (one pair of clubs)."""
    games.sort(key=lambda g: (g.get("seriesGameNumber") or 0, g.get("gameDate", "")))
    teams = {}
    for g in games:
        for side in ("home", "away"):
            t = g["teams"][side]["team"]
            teams.setdefault(t["id"], t)
    first = games[0]["teams"]
    order = [first["home"]["team"]["id"], first["away"]["team"]["id"]]
    league = None
    for t in teams.values():
        league = league or POSTSEASON_LEAGUES.get((t.get("league") or {}).get("id"))
    if round_ == "W":
        league = None
    elif league is None:
        desc = games[0].get("seriesDescription", "")
        league = desc[:2] if desc[:2] in ("AL", "NL") else None

    best_of = max(g.get("gamesInSeries") or 0 for g in games) or len(games)
    need = best_of // 2 + 1
    parsed_games, wins, winner_id = [], {tid: 0 for tid in order}, None
    for g in games:
        abstract = g.get("status", {}).get("abstractGameState")
        state = {"Final": "final", "Live": "live"}.get(abstract, "scheduled")
        home, away = g["teams"]["home"], g["teams"]["away"]
        score = {}
        if state != "scheduled":
            score = {home["team"]["id"]: home.get("score", 0), away["team"]["id"]: away.get("score", 0)}
        tie = bool(g.get("isTie")) or (state == "final" and len(set(score.values())) == 1)
        if state == "final" and not tie:
            winner = home if home.get("isWinner") else away if away.get("isWinner") else (
                home if score[home["team"]["id"]] > score[away["team"]["id"]] else away)
            wins[winner["team"]["id"]] = wins.get(winner["team"]["id"], 0) + 1
            if winner_id is None and wins[winner["team"]["id"]] >= need:
                winner_id = winner["team"]["id"]  # the first to clinch, in game order
        parsed_games.append({
            "number": g.get("seriesGameNumber") or len(parsed_games) + 1,
            "date": g.get("officialDate") or g.get("gameDate", "")[:10],
            "state": state, "tie": tie, "if_necessary": g.get("ifNecessary") == "Y",
            "away": away["team"]["id"], "home": home["team"]["id"], "score": score,
            "venue": (g.get("venue") or {}).get("name", ""),
            "description": g.get("description", ""),
        })
    return {
        "id": sid, "round": round_,
        "title": games[0].get("seriesDescription", ""), "league": league, "best_of": best_of,
        "slots": max(best_of, len(parsed_games)),  # a tied game is replayed: 1912 took eight
        "teams": [{"id": tid, "name": teams[tid].get("name", ""),
                   "abbr": teams[tid].get("abbreviation", ""),
                   "placeholder": _is_placeholder(teams[tid])} for tid in order],
        "games": parsed_games, "wins": wins, "winner": winner_id,
    }


def _feeds(child: dict, parent: dict) -> bool:
    """Does the winner of ``child`` (or, while it's undecided, either of its teams) go on
    to ``parent`` -- by club, or by abbreviation in a placeholder like "NYY/BOS"?"""
    ids = [child["winner"]] if child["winner"] else [t["id"] for t in child["teams"]]
    abbrs = {t["abbr"] for t in child["teams"] if t["id"] in ids}
    for t in parent["teams"]:
        if t["id"] in ids:
            return True
        if t["placeholder"] and abbrs & set(t["abbr"].split("/")):
            return True
    return False


def link_postseason(series: list[dict]) -> dict[str, list[str]]:
    """``{series id: [ids of the series that feed it]}`` -- each series of one round is
    joined to the series of the next round (same league; any league for the World Series)
    its winner went on to.  A series that can't be traced that way (the next round is still
    placeholders like "AL Higher Seed") goes to whichever series of the next round has the
    fewest feeders so far, in id order."""
    by_round = {r: sorted((s for s in series if s["round"] == r), key=lambda s: s["id"])
                for r in POSTSEASON_ROUNDS}
    children = {s["id"]: [] for s in series}
    for i, r in enumerate(POSTSEASON_ROUNDS[:-1]):
        later = next((by_round[n] for n in POSTSEASON_ROUNDS[i + 1:] if by_round[n]), [])
        for child in by_round[r]:
            parents = [p for p in later if p["league"] is None or p["league"] == child["league"]]
            if not parents:
                continue
            parent = next((p for p in parents if _feeds(child, p)), None)
            if parent is None:
                parent = min(parents, key=lambda p: (len(children[p["id"]]), p["id"]))
            children[parent["id"]].append(child["id"])
    return children


# Before 1903 the API has no postseason: the 1884-1890 World's Series (NL v AA champions),
# 1892's split-season championship, the Temple Cup (1894-1897) and the 1900 Chronicle-Telegraph
# Cup come from a file shipped with the app, transcribed from Wikipedia and Retrosheet.
EARLY_POSTSEASON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "early_postseason.json")
EARLY_POSTSEASON_FIRST_SEASON = 1884
_early_cache: dict | None = None


def parse_early_postseason(entry: dict, sid: str = "W_1") -> dict:
    """One series of ``early_postseason.json`` (or ``negro_postseason.json``) in
    :func:`parse_postseason`'s shape, plus ``"format"`` (how the series was decided, e.g.
    "15-game tour"), ``"note"``, ``"winner_label"`` ("champions", "Temple Cup winners"...),
    ``"official"`` and ``"status"`` (a Negro league series that doesn't count, and why), and
    each game's ``"note"`` and ``"counted"`` (False: played but thrown out -- not in the wins).
    The winner is the file's -- not worked out from the wins: 1885 and 1890 ended level,
    and 1887 and 1888 went on after the winner had clinched.  Clubs get made-up negative ids."""
    abbrs = list(entry["teams"])
    ids = {abbr: -(i + 1) for i, abbr in enumerate(abbrs)}
    wins = {ids[a]: 0 for a in abbrs}
    games = []
    for n, g in enumerate(entry["games"], start=1):
        away, a_runs, home, h_runs = g["score"]
        counted = g.get("counted", True)
        if a_runs != h_runs and counted:
            wins[ids[away if a_runs > h_runs else home]] += 1
        games.append({
            "number": n, "date": g["date"], "state": "final", "tie": a_runs == h_runs,
            "if_necessary": False, "away": ids[away], "home": ids[home],
            "score": {ids[away]: a_runs, ids[home]: h_runs}, "venue": g.get("venue", ""),
            "description": f"Game {n}", "note": g.get("note", ""), "counted": counted,
        })
    first_home = entry["games"][0]["score"][2]
    order = [first_home] + [a for a in abbrs if a != first_home]
    return {
        "id": sid, "round": entry.get("round", "W"), "title": entry["title"], "league": entry.get("league"),
        "best_of": len(games), "slots": len(games), "format": entry.get("format", ""),
        "teams": [{"id": ids[a], "name": entry["teams"][a], "abbr": a, "placeholder": False} for a in order],
        "games": games, "wins": wins, "winner": ids[entry["winner"]] if entry.get("winner") else None,
        "winner_label": entry.get("winner_label", "champions"), "note": entry.get("note", ""),
        "source": entry.get("source", ""), "official": entry.get("official", True),
        "status": entry.get("status", ""), "leagues": entry.get("leagues", []),
    }


# The Negro leagues' postseason series, 1903-1949 -- league playoffs, the Colored/Negro World
# Series and the unofficial series around them -- built from Retrosheet by make_negro_postseason.py.
NEGRO_POSTSEASON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "negro_postseason.json")
_negro_cache: dict | None = None


def negro_postseason(season: int) -> list[dict]:
    """The Negro league series of ``season`` (none for most seasons), each in
    :func:`parse_early_postseason`'s shape.  Rounds F/D/L/W make one bracket (a league's
    playoffs and the World Series between leagues); round "X" is a one-off series."""
    global _negro_cache
    if _negro_cache is None:
        with open(NEGRO_POSTSEASON_FILE, encoding="utf-8") as fh:
            by_season: dict[int, list] = {}  # filled before it's published: other threads may be reading
            for entry in json.load(fh)["series"]:
                by_season.setdefault(entry["season"], []).append(entry)
        _negro_cache = by_season
    out = []
    for n, entry in enumerate(_negro_cache.get(season, []), start=1):
        sid = "W_1" if entry["round"] == "W" else f"{entry['round']}_{n}"
        out.append(parse_early_postseason(entry, sid))
    return out


def early_postseason(season: int) -> list[dict]:
    """The series of a season before 1903 (none for most of them)."""
    global _early_cache
    if _early_cache is None:
        with open(EARLY_POSTSEASON_FILE, encoding="utf-8") as fh:
            _early_cache = {e["season"]: e for e in json.load(fh)["series"]}
    entry = _early_cache.get(season)
    return [parse_early_postseason(entry)] if entry else []


def get_postseason(season: int, ttl: float | None = None) -> list[dict]:
    """:func:`parse_postseason` for ``season`` -- one request -- or, before 1903,
    :func:`early_postseason`."""
    if season < POSTSEASON_FIRST_SEASON:
        return early_postseason(season)
    return parse_postseason(api_get(
        "/schedule/postseason/series",
        {"season": season, "sportId": SPORT_ID, "hydrate": "team(league)"}, ttl=ttl))


# --------------------------------------------------------------------------- #
#  Denied: the teams that "ought to have won" in the postseason but didn't.   #
# --------------------------------------------------------------------------- #
# The user's rules.  Every season: the best record in MLB -- the AL, NL and 19th-century majors (several clubs if tied), who
# ought to win it all.  From 1969 also the best record in each league, who ought to win the
# championship series; from 1995 also the best in each division and among each league's wild
# cards (the "wild card division"), who ought to get through the wild card and division
# rounds.  A team is denied when it goes out in a round it ought to have won -- or never gets
# there: its league had no postseason, or (1981) the split-season format shut it out.  The
# Negro leagues (1920-1948) are a track of their own: their best record overall, judged by
# their own official postseason (the Colored/Negro World Series, or a league's championship
# series in a year without one).  Kept apart because baseball was segregated then -- the AL and
# NL barred Black players -- not because their records are any less a part of the history of
# today's integrated game (MLB has recognized them as major leagues since 2020).
NEGRO_LEAGUE_IDS = (431, 430, 429, 428, 427, 426, 432)
DENIED_LEAGUE_FROM = 1969
DENIED_DIVISION_FROM = 1995
DENIED_PREFIX = "Denied:  "
_LEAGUE_SHORT = {103: "AL", 104: "NL"}


def _pct_key(team: dict) -> Fraction:
    decisions = team["wins"] + team["losses"]
    return Fraction(team["wins"], decisions) if decisions else Fraction(0)


def _best_teams(teams: list[dict]) -> list[dict]:
    """The team(s) with the best winning percentage -- exactly, so a tie is a tie."""
    if not teams:
        return []
    top = max(_pct_key(t) for t in teams)
    return [t for t in teams if _pct_key(t) == top]


def _short_group(name: str) -> str:
    return name.replace("American League", "AL").replace("National League", "NL")


_NAME_STOP = {"the", "club", "of"}


def _name_tokens(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", name.lower()) if w not in _NAME_STOP]


def _match_club(name: str, candidates: list[dict]) -> dict | None:
    """The standings club a postseason file's club name means: the same name, else the most
    words in common, the nickname counting most ("Hilldale Club" = Hilldale Daisies, "Bacharach
    Giants" = Atlantic City Bacharach Giants); a draw goes to the better record (MLB calls the
    AA's 1885 St. Louis Browns the Cardinals -- the city's other club, the Maroons, was poor)."""
    words = _name_tokens(name)
    if not words:
        return None
    best, best_key = None, None
    for team in candidates:
        theirs = _name_tokens(team["name"])
        score = (100 if theirs == words else 0) + (3 if theirs and theirs[-1] == words[-1] else 0) \
            + len(set(theirs) & set(words))
        if score == 0:
            continue
        key = (score, _pct_key(team))
        if best_key is None or key > best_key:
            best, best_key = team, key
    return best


def _series_entries(series: list[dict], clubs: list[dict], by_name: bool) -> list[dict]:
    """Each series as ``{"round", "title", "best_of", "final", "teams": [standings id, id],
    "names", "wins", "winner"}`` -- the clubs as standings ids (API series carry them; the
    bundled files' clubs are matched by name among ``clubs``)."""
    out = []
    for s in series:
        ids, names = [], []
        for t in s["teams"]:
            if by_name:
                match = _match_club(t["name"], clubs)
                ids.append(match["id"] if match else None)
                names.append(match["name"] if match else t["name"])
            else:
                ids.append(t["id"])
                names.append(t["name"])
        remap = dict(zip((t["id"] for t in s["teams"]), ids))
        finished = all(g["state"] == "final" for g in s["games"]) and not any(
            t.get("placeholder") for t in s["teams"])
        out.append({"round": s["round"], "title": s["title"], "best_of": s.get("best_of") or 0,
                    "teams": ids, "names": names, "finished": finished,
                    "wins": {remap[k]: v for k, v in s["wins"].items() if k in remap},
                    "winner": remap.get(s["winner"]) if s.get("winner") is not None else None})
    return out


def _loss_text(season: int, entry: dict, final: bool, modern_ws: bool, underdog: bool,
               league: str) -> str:
    """What a series a best team lost to an underdog (a club with a worse record) is called in
    its "Denied" label -- ``underdog`` False when the winner's record isn't known."""
    by = "by underdog " if underdog else ""
    if final and modern_ws:
        return f"Defeated {by}in World Series"
    if final:
        where = f"{season} {entry['title']}" + (f", {league}" if entry["round"] == "L" and league else "")
        return f"Defeated {by}in final series ({where})"
    if entry["round"] == "L":
        return f"Defeated {by}in championship series"
    if entry["round"] == "F":
        return f"Defeated {by}in wild card game" if entry["best_of"] == 1 else f"Defeated {by}in wild card series"
    if entry["round"] == "D" and modern_ws:
        return f"Defeated {by}in division series"
    return f"Defeated {by}in {entry['title'].lower()}"


# How far a team got, for the tab's "Got as far as" filter: the furthest round it reached (a
# champion's is the final), 0 when it never reached the postseason.
DENIED_STAGES = {"none": 0, "T": 1, "F": 2, "D": 3, "L": 4, "final": 5}


def parse_tiebreakers(payload: dict) -> list[dict]:
    """The tiebreaker games in a ``/schedule`` payload (regular-season games MLB flags
    ``tiebreaker == "Y"``: pennant playoffs, division and wild card "Game 163"s), finished
    ones only: ``{"pk", "date", "away", "home", "score": {team id: runs}}``."""
    seen, out = set(), []
    for day in payload.get("dates", []):
        for g in day.get("games", []):
            if g.get("tiebreaker") != "Y" or g.get("status", {}).get("abstractGameState") != "Final":
                continue
            if g.get("gamePk") in seen:
                continue
            seen.add(g.get("gamePk"))
            t = g["teams"]
            away, home = t["away"]["team"]["id"], t["home"]["team"]["id"]
            out.append({"pk": g.get("gamePk"), "date": g.get("officialDate", ""), "away": away, "home": home,
                        "score": {away: t["away"].get("score", 0), home: t["home"].get("score", 0)}})
    return sorted(out, key=lambda g: g["date"])


def get_tiebreakers(season: int, ttl: float | None = None) -> list[dict]:
    """:func:`parse_tiebreakers` for ``season`` (the same request as :func:`get_post_games`)."""
    return parse_tiebreakers(api_get(
        "/schedule", {"sportId": SPORT_ID, "season": season, "gameType": "R",
                      "startDate": f"{season}-09-15", "endDate": f"{season}-10-20"}, ttl=ttl))


def _tiebreak_series(games: list[dict]) -> list[dict]:
    """Tiebreaker games grouped by pair of clubs: ``{"teams": [id, id], "wins": {id: n},
    "record": {id: (wins, losses)}, "winner": id | None}``."""
    pairs: dict[frozenset, dict] = {}
    for g in games:
        a, b = g["away"], g["home"]
        s = pairs.setdefault(frozenset((a, b)), {"teams": [a, b], "wins": {a: 0, b: 0}})
        if g["score"][a] != g["score"][b]:
            s["wins"][a if g["score"][a] > g["score"][b] else b] += 1
    for s in pairs.values():
        a, b = s["teams"]
        s["record"] = {a: (s["wins"][a], s["wins"][b]), b: (s["wins"][b], s["wins"][a])}
        s["winner"] = a if s["wins"][a] > s["wins"][b] else b if s["wins"][b] > s["wins"][a] else None
    return list(pairs.values())


def _tiebreak_title(season: int, ids, league_of: dict, record: dict) -> str:
    """What a tiebreaker between the clubs ``ids`` decided: "NL pennant tiebreaker" (before
    1969), "AL East tiebreaker" (a division), "NL wild card tiebreaker"."""
    a, b = ids
    ga, gb = league_of.get(a), league_of.get(b)
    short = _LEAGUE_SHORT.get((ga or {}).get("league_id"), (ga or {}).get("league", ""))
    if season < DENIED_LEAGUE_FROM or ga is None or gb is None:
        scope = f"{short} pennant"
    elif any(record[i]["rank"] == 1 for i in (a, b)):
        scope = _short_group(ga["name"]) if ga is gb else f"{short} division"
    else:
        scope = f"{short} wild card"
    return f"{scope} tiebreaker"


TIEBREAK_NOTE = ("A tiebreaker, not a postseason series: its games count in the regular-season "
                 "standings and statistics.")


def get_tiebreak_series(season: int, ttl: float | None = None) -> list[dict]:
    """``season``'s tiebreakers as series in :func:`parse_postseason`'s shape (round "T"),
    each titled by what it decided (:func:`_tiebreak_title`) and carrying ``"note"``
    (:data:`TIEBREAK_NOTE`) and ``"winner_label"``.  The National League settled a tied pennant
    with a best-of-three playoff (1946, 1951, 1959, 1962), everything else with one game -- the
    API's own series numbers can't be trusted here (1980's game is listed as game 4 of 4)."""
    if season < POSTSEASON_FIRST_SEASON:
        return []
    payload = api_get("/schedule", {"sportId": SPORT_ID, "season": season, "gameType": "R",
                                    "startDate": f"{season}-09-15", "endDate": f"{season}-10-20"}, ttl=ttl)
    pairs: dict[frozenset, list[dict]] = {}
    seen = set()
    for day in payload.get("dates", []):
        for g in day.get("games", []):
            if g.get("tiebreaker") != "Y" or g.get("gamePk") in seen \
                    or g.get("status", {}).get("detailedState") in _DROPPED_STATES:
                continue
            seen.add(g.get("gamePk"))
            pair = frozenset(g["teams"][side]["team"]["id"] for side in ("home", "away"))
            pairs.setdefault(pair, []).append(g)
    if not pairs:
        return []
    groups = get_standings(season, ttl)
    league_of = {t["id"]: g for g in groups for t in g["teams"]}
    record = {t["id"]: t for g in groups for t in g["teams"]}
    out = []
    for n, games in enumerate(pairs.values(), start=1):
        games.sort(key=lambda g: g.get("gameDate", ""))
        ids = list(dict.fromkeys(g["teams"][side]["team"]["id"] for g in games for side in ("home", "away")))
        league_id = (league_of.get(ids[0]) or {}).get("league_id")
        best_of = 3 if league_id == 104 and season < DENIED_LEAGUE_FROM and season >= 1946 else 1
        games = [dict(g, seriesGameNumber=i, gamesInSeries=best_of) for i, g in enumerate(games, start=1)]
        series = _parse_series(f"T_{n}", "T", games)
        title = _tiebreak_title(season, ids[:2], league_of, record) if all(i in record for i in ids[:2]) \
            else "Tiebreaker"
        series.update(title=title[0].upper() + title[1:], league=None, note=TIEBREAK_NOTE,
                      winner_label=f"winners of the {title}")
        out.append(series)
    return out


def denied_report(season: int, groups: list[dict], series: list[dict],
                  negro_series: list[dict] | None = None,
                  tiebreak_games: list[dict] | None = None) -> list[dict]:
    """One season's best teams by the user's rules and what became of them.  ``groups`` is
    :func:`get_standings`, ``series`` :func:`get_postseason` (the bundled file's before 1903),
    ``negro_series`` :func:`negro_postseason` (only the official series count),
    ``tiebreak_games`` :func:`get_tiebreakers`.  One row per team, in order -- MLB's best,
    then each league's, division's and wild card's, then the Negro leagues', then any other club
    that played a tiebreaker -- each ``{"season", "id", "team", "league", "wins", "losses",
    "pct", "best_in": [labels], "track": "MLB" | "Negro", "result": what happened, "denied":
    label or "", "stage": furthest round (:data:`DENIED_STAGES`), "champion": bool,
    "tiebreaker": what happened in one or "", "better_than": [later series winners with a
    worse record -- for a club put out by a tiebreaker], "record_note": ""}``.

    Tiebreaker games count in the regular season.  MLB's standings include them, except in a
    few seasons (1946, 1962) where both clubs are still level; there they are added in."""
    games = tiebreak_games or []
    tiebreaks = _tiebreak_series(games)
    groups = [dict(g, teams=[dict(t) for t in g["teams"]]) for g in groups]
    record = {t["id"]: t for g in groups for t in g["teams"]}
    for tb in tiebreaks:
        a, b = (record.get(i) for i in tb["teams"])
        if a and b and (a["wins"], a["losses"]) == (b["wins"], b["losses"]):  # games left out
            for team in (a, b):
                won, lost = tb["record"][team["id"]]
                team["wins"] += won
                team["losses"] += lost
                team["pct"] = _rate_text(team["wins"] / (team["wins"] + team["losses"]))
                team["record_note"] = "tiebreaker games added: MLB's standings leave them out this season"

    mlb_groups = [g for g in groups if g["league_id"] not in NEGRO_LEAGUE_IDS]
    negro_groups = [g for g in groups if g["league_id"] in NEGRO_LEAGUE_IDS]
    league_of = {t["id"]: g for g in groups for t in g["teams"]}
    mlb_teams = [t for g in mlb_groups for t in g["teams"]]
    negro_teams = [t for g in negro_groups for t in g["teams"]]
    modern = season >= POSTSEASON_FIRST_SEASON
    mlb_entries = _series_entries(series, mlb_teams, by_name=not modern)
    for e in mlb_entries:
        e["final"] = e["round"] == "W"
    official = [s for s in (negro_series or []) if s.get("official", True) and s["round"] != "X"]
    negro_entries = _series_entries(official, negro_teams, by_name=True)
    has_ws = any(e["round"] == "W" for e in negro_entries)
    for e in negro_entries:
        e["final"] = e["round"] == "W" or (e["round"] == "L" and not has_ws)

    for tb in tiebreaks:  # what each decided
        tb["title"] = _tiebreak_title(season, tb["teams"], league_of, record)

    # pools: (label, members, rounds a member ought to win -- None: every round)
    pools = []
    if mlb_teams:
        best = _best_teams(mlb_teams)
        pools.append(("MLB" + (" (tied)" if len(best) > 1 else ""), best, None, "MLB"))
    if mlb_entries and season >= DENIED_LEAGUE_FROM:
        for lid, short in _LEAGUE_SHORT.items():
            league_teams = [t for g in mlb_groups if g["league_id"] == lid for t in g["teams"]]
            best = _best_teams(league_teams)
            if best:
                pools.append((short + (" (tied)" if len(best) > 1 else ""), best, {"F", "D", "L"}, "MLB"))
    if mlb_entries and season >= DENIED_DIVISION_FROM:
        for lid, short in _LEAGUE_SHORT.items():
            divisions = [g for g in mlb_groups if g["league_id"] == lid]
            for g in divisions:
                leaders = [t for t in g["teams"] if t["rank"] == 1] or _best_teams(g["teams"])
                pools.append((_short_group(g["name"]), leaders[:1], {"F", "D"}, "MLB"))
            others = [t for g in divisions for t in g["teams"] if t["rank"] != 1]
            best = _best_teams(others)
            if best:
                pools.append((f"{short} wild card" + (" (tied)" if len(best) > 1 else ""), best, {"F", "D"}, "MLB"))
    if negro_teams:
        best = _best_teams(negro_teams)
        pools.append(("Negro leagues" + (" (tied)" if len(best) > 1 else ""), best, None, "Negro"))
    # the clubs of each tiebreaker too, best or not
    pools.append(("", [record[i] for tb in tiebreaks for i in tb["teams"] if i in record], set(), "MLB"))

    rows, by_id = [], {}
    for label, members, rounds, track in pools:
        for team in members:
            row = by_id.get(team["id"])
            if row is None:
                row = by_id[team["id"]] = {
                    "season": season, "id": team["id"], "team": team["name"],
                    "league": league_of[team["id"]]["league"], "wins": team["wins"],
                    "losses": team["losses"], "pct": team["pct"], "best_in": [], "track": track,
                    "record_note": team.get("record_note", ""), "_rounds": set()}
                rows.append(row)
            if label:
                row["best_in"].append(label)
            row["_rounds"] |= set(POSTSEASON_ROUNDS) if rounds is None else rounds

    # every club that won a postseason series, with the furthest one it won
    furthest_win: dict = {}
    for e in mlb_entries:
        if e["winner"] is not None:
            if e["winner"] not in furthest_win or e["final"] or (
                    POSTSEASON_ROUNDS.index(e["round"]) > POSTSEASON_ROUNDS.index(furthest_win[e["winner"]]["round"])):
                furthest_win[e["winner"]] = e

    for row in rows:
        entries = mlb_entries if row["track"] == "MLB" else negro_entries
        tid, league = row["id"], row["league"]
        played = [e for e in entries if tid in e["teams"]]
        tb = next((t for t in tiebreaks if tid in t["teams"]), None)
        row["tiebreaker"], row["better_than"] = "", []
        if tb is not None:
            opp = next(i for i in tb["teams"] if i != tid)
            won, lost = tb["record"][tid]
            verb = "Won" if tb["winner"] == tid else "Lost" if tb["winner"] is not None else "Split"
            row["tiebreaker"] = f"{verb} {tb['title']} vs {record[opp]['name'] if opp in record else '?'} ({won}–{lost})"
        result, denied, stage, champion = "", "", 0, False
        if not played:
            league_in = any(i is not None and league_of.get(i, {}).get("league") == league
                            for e in entries for i in e["teams"])
            if tb is not None and tb["winner"] not in (None, tid):
                result, stage = "Put out by the tiebreaker", DENIED_STAGES["T"]  # nobody ought to win one
                row["better_than"] = [
                    f"{record[w]['name']} ({record[w]['pct']}, won {e['title']})"
                    for w, e in furthest_win.items() if w in record and _pct_key(record[w]) < _pct_key(record[tid])]
            elif not entries:
                result = "No postseason" if row["track"] == "MLB" or not negro_series else "No official postseason"
                denied = "No postseason"
            elif not league_in:
                result, denied = f"No postseason in the {league}", "No postseason"
            elif any(i is not None and i != tid and _pct_key(record[i]) == _pct_key(record[tid])
                     and league_of[i]["league"] == league for e in entries for i in e["teams"] if i in record):
                result, denied = "Tied, lost the tiebreaker", "Lost tiebreaker playoff"
            elif season == 1981:
                result = "Did not qualify (won neither half of the split season)"
                denied = "Excluded by split-season format"
            else:
                result, denied = "Did not qualify", "Did not qualify for the postseason"
            if not row["best_in"]:
                denied = ""
        else:
            stage = max(DENIED_STAGES["final"] if e["final"] else DENIED_STAGES[e["round"]] for e in played)
            lost = next((e for e in played if e["winner"] is not None and e["winner"] != tid), None)
            if lost is not None:
                opp = next(i for i in lost["teams"] if i != tid)
                opp_name = lost["names"][lost["teams"].index(opp)]
                score = f"{lost['wins'].get(tid, 0)}–{lost['wins'].get(opp, 0)}"
                result = f"Lost {lost['title']} to {opp_name} ({score})"
                if lost["round"] in row["_rounds"]:
                    # denied only by an underdog: losing to an equal or better record is no upset
                    opp_team = record.get(opp)
                    if opp_team is None or _pct_key(opp_team) < _pct_key(record[tid]):
                        denied = _loss_text(season, lost, lost["final"], modern and row["track"] == "MLB",
                                            opp_team is not None, league)
                    else:
                        same = _pct_key(opp_team) == _pct_key(record[tid])
                        result += f" \u2014 not an upset: {'an equal' if same else 'a better'} record"
            else:
                last = played[-1]
                for e in played:
                    if e["final"]:
                        last = e
                if last["winner"] == tid and last["final"]:
                    result, champion = f"Won {last['title']}", True
                elif last["winner"] is None and last["finished"]:
                    opp = next((i for i in last["teams"] if i != tid), None)
                    result = (f"{last['title']} ended level "
                              f"({last['wins'].get(tid, 0)}–{last['wins'].get(opp, 0)})")
                else:
                    result = f"Still playing ({last['title']})" if last["winner"] is None else f"Won {last['title']}"
        row.update(result=result, denied=DENIED_PREFIX + denied if denied else "", stage=stage, champion=champion)
        del row["_rounds"]
    return rows


def get_denied(season: int, ttl: float | None = None) -> list[dict]:
    """:func:`denied_report` for ``season``: its standings, postseason and tiebreaker games
    (three requests, all cached) plus the bundled Negro league file."""
    groups = get_standings(season, ttl)
    series = get_postseason(season, ttl) if season >= EARLY_POSTSEASON_FIRST_SEASON else []
    negro = negro_postseason(season) if any(g["league_id"] in NEGRO_LEAGUE_IDS for g in groups) else []
    tiebreaks = get_tiebreakers(season, ttl) if season >= POSTSEASON_FIRST_SEASON else []
    return denied_report(season, groups, series, negro, tiebreaks)


# --------------------------------------------------------------------------- #
#  Poststats: player statistics for chosen kinds of postseason series.       #
# --------------------------------------------------------------------------- #
# The Stats API's own postseason splits come one round at a time (gameType=W, L, D or F; asking
# for several falls back to the regular season) and leave out tiebreaker games, which count as
# regular-season games.  So Poststats adds up box scores instead: the MLB Stats API's for the
# American and National Leagues from 1903, Retrosheet's (via negro_postseason.json and
# early_poststats.json) for the Negro leagues and the 1900 Chronicle-Telegraph Cup.
EARLY_POSTSTATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "early_poststats.json")
MLB_SECTION = "American and National Leagues"
POST_ROUNDS = [  # (key, label, what it takes in)
    ("W", "World Series", "The World Series, 1903 on."),
    ("L", "Championship series", "League championship series, 1969 on."),
    ("D", "Division series", "Division series: 1981, and 1995 on."),
    ("F", "Wild card", "Wild card games and series, 2012 on."),
    ("T", "Tiebreakers", "Tiebreaker games and playoffs for a first place (or, from 1995, a wild card "
                         "spot) left tied at the end of the season: for the pennant before 1969 "
                         "(1946, 1948, 1951, 1959, 1962), for a division from 1969 to 1994 (1978, "
                         "1980), and from 1995 the one-game “Game 163”. Traditionally their "
                         "statistics count with the regular season, not the postseason."),
]
POST_BATTING_KEYS = ("plateAppearances", "atBats", "runs", "hits", "doubles", "triples", "homeRuns",
                     "rbi", "baseOnBalls", "intentionalWalks", "strikeOuts", "stolenBases",
                     "caughtStealing", "hitByPitch", "sacFlies", "sacBunts", "groundIntoDoublePlay",
                     "leftOnBase")
POST_PITCHING_KEYS = ("wins", "losses", "saves", "gamesStarted", "completeGames", "shutouts", "outs",
                      "battersFaced", "hits", "runs", "earnedRuns", "homeRuns", "baseOnBalls",
                      "strikeOuts", "hitBatsmen", "wildPitches")
POST_FIELDING_KEYS = ("gamesStarted", "outs", "putOuts", "assists", "errors", "doublePlays",
                      "triplePlays", "passedBall", "stolenBases", "caughtStealing")
POST_COLUMNS = {
    "Batting": HITTING_COLUMNS,
    "Pitching": PITCHING_COLUMNS,
    "Fielding": [("Player", "_name"), ("Pos", "_pos"), ("G", "gamesPlayed"), ("GS", "gamesStarted"),
                 ("Inn", "innings"), ("PO", "putOuts"), ("A", "assists"), ("E", "errors"),
                 ("DP", "doublePlays"), ("FLD%", "fielding"), ("PB", "passedBall"),
                 ("SB", "stolenBases"), ("CS", "caughtStealing")],
}
POST_GROUP = {"Batting": "batting", "Pitching": "pitching", "Fielding": "fielding"}
# Why a column can be blank: the sources don't all record everything.
POST_BLANKS_MLB = "MLB's box scores give no fielder's innings or double plays"
POST_BLANKS_RETRO = ("Retrosheet's box scores (the Negro leagues, 1900) often lack caught stealing, "
                     "passed balls and complete games, and give no shutouts or runners left on base")


def post_blanks(lines: list[dict]) -> str:
    """Why columns may be blank, for the sources behind ``lines``."""
    parts = []
    if any(isinstance(ln["team"], int) for ln in lines):
        parts.append(POST_BLANKS_MLB)
    if any(isinstance(ln["team"], str) for ln in lines):
        parts.append(POST_BLANKS_RETRO)
    return f"A blank means the source doesn't record it: {'; '.join(parts)}." if parts else ""


def tiebreaker_round(season: int) -> str:
    """Tiebreaker games are a kind of their own ("T") in every era -- they decided a pennant
    before 1969, a division until 1994, and a division or wild card spot ("Game 163") since."""
    return "T"


def parse_post_games(series_payload: dict, tiebreak_payload: dict, season: int) -> list[dict]:
    """The MLB postseason games of a season -- ``{"pk", "round", "final", "away": (id, name),
    "home": (id, name)}`` -- from the postseason-series payload (duplicate listings and
    postponements dropped) plus the tiebreaker games in the regular-season schedule."""
    seen, out = set(), []

    def add(g, round_):
        pk = g.get("gamePk")
        if pk in seen or g.get("status", {}).get("detailedState") in _DROPPED_STATES:
            return
        seen.add(pk)
        t = g["teams"]
        out.append({"pk": pk, "round": round_, "date": g.get("officialDate", ""),
                    "final": g.get("status", {}).get("abstractGameState") == "Final",
                    "away": (t["away"]["team"]["id"], t["away"]["team"].get("name", "")),
                    "home": (t["home"]["team"]["id"], t["home"]["team"].get("name", ""))})

    for entry in series_payload.get("series", []):
        for g in entry.get("games", []):
            if g.get("gameType") in ("F", "D", "L", "W"):
                add(g, g["gameType"])
    for day in tiebreak_payload.get("dates", []):
        for g in day.get("games", []):
            if g.get("tiebreaker") == "Y":
                add(g, tiebreaker_round(season))
    return out


def get_post_games(season: int, ttl: float | None = None) -> list[dict]:
    series = api_get("/schedule/postseason/series", {"season": season, "sportId": SPORT_ID}, ttl=ttl)
    tiebreaks = api_get("/schedule", {"sportId": SPORT_ID, "season": season, "gameType": "R",
                                      "startDate": f"{season}-09-15", "endDate": f"{season}-10-20"},
                        ttl=ttl)
    return parse_post_games(series, tiebreaks, season)


def boxscore_lines(box: dict) -> list[dict]:
    """One line per player per stat group from an MLB box score, in the shape Poststats sums:
    ``{"team", "team_name", "player", "name", "group", "pos", <stat>: n, "gamesPlayed": 1}``.
    A fielder's line goes to the position he started at (the box score doesn't split a game
    between positions); pinch-hitters, pinch-runners and designated hitters have none."""
    out = []
    for side in ("away", "home"):
        team = box.get("teams", {}).get(side, {})
        tid, tname = team.get("team", {}).get("id"), team.get("team", {}).get("name", "")
        for p in team.get("players", {}).values():
            stats = p.get("stats", {})
            base = {"team": tid, "team_name": tname, "player": p["person"]["id"],
                    "name": p["person"].get("fullName", "")}
            for group, keys in (("batting", POST_BATTING_KEYS), ("pitching", POST_PITCHING_KEYS)):
                if stats.get(group):
                    line = dict(base, group=group, pos="", gamesPlayed=1)
                    for k in keys:
                        v = stats[group].get(k)
                        line[k] = v if isinstance(v, int) else None
                    out.append(line)
            positions = p.get("allPositions") or ([p["position"]] if p.get("position") else [])
            pos = positions[0].get("abbreviation", "") if positions else ""
            if stats.get("fielding") and pos not in ("", "PH", "PR", "DH"):
                line = dict(base, group="fielding", pos=pos, gamesPlayed=1)
                for k in POST_FIELDING_KEYS:
                    v = stats["fielding"].get(k)
                    line[k] = v if isinstance(v, int) else None
                out.append(line)
    return out


def retro_lines(players: dict, team_names: dict, prefix: str = "R:") -> list[dict]:
    """Retrosheet series totals (from the bundled files) as Poststats lines; clubs keyed
    "R:<Retrosheet code>" so they can't collide with MLB ids."""
    out = []
    for group, lines in players.items():
        for line in lines:
            row = {k: v for k, v in line.items() if k not in ("id", "name", "team", "pos")}
            row.update(team=prefix + line["team"], team_name=team_names.get(line["team"], line["team"]),
                       player=line["id"], name=line["name"], group=group, pos=line.get("pos", ""))
            out.append(row)
    return out


def _league_key(club_leagues: dict) -> tuple[str, str]:
    """A category for a non-MLB series from the leagues of its two clubs: ("N:NNL-NAL",
    "NNL v NAL World Series"), ("N:NNL", "NNL postseason series"), ("N:NNL-ind", "NNL v
    independent clubs"), ("N:ind", "Independent clubs")."""
    order = ["NL", "AA", "NNL", "ECL", "NAL", "NSL"]
    leagues = sorted({lg for lg in club_leagues.values() if lg and lg != "independent"},
                     key=lambda lg: order.index(lg) if lg in order else 99)
    independent = "independent" in club_leagues.values()
    if len(leagues) == 2:
        a, b = leagues
        name = "World's Series" if {a, b} == {"NL", "AA"} else "World Series"
        return f"{a}-{b}", f"{a} v {b} {name}"
    if leagues and independent:
        return f"{leagues[0]}-ind", f"{leagues[0]} v independent clubs"
    if leagues:
        return leagues[0], f"{leagues[0]} postseason series"
    return "ind", "Independent clubs"


def poststats_catalog(season: int, ttl: float | None = None) -> list[dict]:
    """The kinds of postseason series ``season`` had, each ``{"key", "label", "section", "tip",
    "available", "reason", "teams": {team key: name}, "games": [...], "series": [...]}`` --
    the MLB rounds (1903 on), then the other series grouped by the leagues involved: the
    19th-century World's Series and NL series, and the Negro leagues'.  A kind with no player
    statistics says why in "reason"."""
    cats = []
    if season >= POSTSEASON_FIRST_SEASON:
        games = get_post_games(season, ttl)
        for key, label, tip in POST_ROUNDS:
            mine = [g for g in games if g["round"] == key]
            if not mine:
                continue
            teams = {}
            for g in mine:
                for tid, name in (g["away"], g["home"]):
                    teams[tid] = name
            final = [g for g in mine if g["final"]]
            cats.append({"key": key, "label": label, "section": MLB_SECTION, "tip": tip,
                         "available": bool(final), "games": mine, "series": [], "teams": teams,
                         "reason": "" if final else "None of these games has been played yet."})
    early = {}
    try:
        with open(EARLY_POSTSTATS_FILE, encoding="utf-8") as fh:
            early = json.load(fh)["seasons"]
    except (OSError, ValueError):
        pass
    if season < POSTSEASON_FIRST_SEASON:
        for s in early_postseason(season):
            data = early.get(str(season))
            leagues = data["club_leagues"] if data else (
                {s["teams"][0]["abbr"]: "NL", s["teams"][1]["abbr"]: "AA"} if season <= 1890
                else {t["abbr"]: "NL" for t in s["teams"]})
            key, label = _league_key(leagues)
            cats.append({
                "key": "E:" + key, "label": label, "section": "Before 1903", "series": [s], "games": [],
                "tip": f"{season} {s['title']}", "available": bool(data),
                "teams": {"R:" + t["abbr"]: t["name"] for t in s["teams"]},
                "retro": data, "reason": "" if data else (
                    f"No player statistics exist for the {season} {s['title']}: neither MLB's data nor "
                    "Retrosheet has box scores for postseason series before 1900.")})
    negro = {}
    try:
        with open(NEGRO_POSTSEASON_FILE, encoding="utf-8") as fh:
            for entry in json.load(fh)["series"]:
                if entry["season"] == season:
                    key, label = _league_key(entry.get("club_leagues", {}))
                    negro.setdefault(key, (label, []))[1].append(entry)
    except (OSError, ValueError):
        pass
    for key, (label, entries) in negro.items():
        unofficial = [e for e in entries if not e.get("official", True)]
        if len(unofficial) == len(entries):
            label += " (unofficial)"
        tip = "\n".join(f"{e['title']}" + ("" if e.get("official", True) else f" — unofficial: {e['status']}")
                        for e in entries)
        teams = {"R:" + code: name for e in entries for code, name in e["teams"].items()}
        cats.append({"key": "N:" + key, "label": label, "section": "Negro leagues", "tip": tip,
                     "official": len(unofficial) < len(entries),
                     "available": any(e.get("players", {}).get("batting") for e in entries),
                     "reason": "", "teams": teams, "games": [], "series": entries})
    return cats


def poststats_lines(season: int, cats: list[dict], team=None, ttl: float | None = None,
                    workers: int = 6) -> tuple[list[dict], dict]:
    """Every player line for the chosen kinds of series (and one club, when ``team`` is
    given): MLB box scores fetched (cached for good once a game is final) and Retrosheet's
    series totals.  Returns (lines, {team key: games played})."""
    from concurrent.futures import ThreadPoolExecutor
    lines, games = [], {}
    mlb = [g for c in cats for g in c["games"] if g["final"]
           and (team is None or team in (g["away"][0], g["home"][0]))]
    for g in mlb:
        for tid in (g["away"][0], g["home"][0]):
            games[tid] = games.get(tid, 0) + 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        boxes = list(pool.map(lambda g: api_get(f"/game/{g['pk']}/boxscore", ttl=None), mlb))
    for box in boxes:
        lines += boxscore_lines(box)
    for c in cats:
        if c.get("retro"):
            lines += retro_lines(c["retro"]["players"], c["retro"]["teams"])
            for code in c["retro"]["teams"]:
                games["R:" + code] = games.get("R:" + code, 0) + c["retro"]["games"]
        for entry in c["series"] if c["key"].startswith("N:") else []:
            lines += retro_lines(entry.get("players", {}), entry["teams"])
            counted = sum(1 for g in entry["games"] if g.get("counted", True))
            for code in entry["teams"]:
                games["R:" + code] = games.get("R:" + code, 0) + counted
    if team is not None:
        lines = [ln for ln in lines if ln["team"] == team]
        games = {team: games.get(team, 0)}
    return lines, games


def _outs_to_ip(outs):
    return None if outs is None else f"{outs // 3}.{outs % 3}"


def _rate(value, digits=3):
    if value is None:
        return None
    text = f"{value:.{digits}f}"
    return text[1:] if digits == 3 and text.startswith("0.") else text


def _div(a, b):
    return a / b if a is not None and b else None


def _derive(group: str, t: dict) -> dict:
    """Rates from summed counts: AVG/OBP/SLG/OPS, ERA/WHIP/K9/BB9/IP, FLD%/innings."""
    g = lambda k: t.get(k)  # noqa: E731
    if group == "batting":
        h, ab = g("hits"), g("atBats")
        tb = None if h is None else h + (g("doubles") or 0) + 2 * (g("triples") or 0) + 3 * (g("homeRuns") or 0)
        on = None if h is None else h + (g("baseOnBalls") or 0) + (g("hitByPitch") or 0)
        pa_obp = None if ab is None else ab + (g("baseOnBalls") or 0) + (g("hitByPitch") or 0) + (g("sacFlies") or 0)
        # as the Stats API does: .000 for a hitless 0-for-0, and OPS the sum of the rounded OBP and SLG
        avg = None if h is None else (h / ab if ab else 0.0)
        obp = None if on is None else (on / pa_obp if pa_obp else 0.0)
        slg = None if tb is None else (tb / ab if ab else 0.0)
        ops = None if obp is None or slg is None else round(obp, 3) + round(slg, 3)
        t.update(avg=_rate(avg), obp=_rate(obp), slg=_rate(slg), ops=_rate(ops), totalBases=tb)
    elif group == "pitching":
        ip = None if g("outs") is None else g("outs") / 3
        t.update(inningsPitched=_outs_to_ip(g("outs")),
                 era=_rate(_div(9 * g("earnedRuns"), ip) if g("earnedRuns") is not None else None, 2),
                 whip=_rate(_div((g("baseOnBalls") or 0) + (g("hits") or 0), ip), 2),
                 strikeoutsPer9Inn=_rate(_div(9 * (g("strikeOuts") or 0), ip), 2),
                 walksPer9Inn=_rate(_div(9 * (g("baseOnBalls") or 0), ip), 2))
    else:
        po, a, e = g("putOuts"), g("assists"), g("errors")
        ch = None if po is None or a is None else po + a + (e or 0)
        t.update(chances=ch, fielding=_rate(_div(None if ch is None else po + a, ch)),
                 innings=_outs_to_ip(g("outs")))
    return t


def _sum_into(total: dict, line: dict, keys) -> None:
    for k in keys:
        v = line.get(k)
        if v is None:
            total.setdefault(k, None)
        else:
            total[k] = (total.get(k) or 0) + v


def aggregate_post_lines(lines: list[dict], view: str) -> list[dict]:
    """Player rows for a Poststats view (Batting, Pitching, Fielding): one per player and
    club (and position, for fielding), counts summed and rates worked out from them, keyed
    like the Stats tab's rows ("_name", "_team", "_pos" ...), most active first."""
    group = POST_GROUP[view]
    keys = {"batting": POST_BATTING_KEYS, "pitching": POST_PITCHING_KEYS,
            "fielding": POST_FIELDING_KEYS}[group] + ("gamesPlayed",)
    totals: dict[tuple, dict] = {}
    for ln in lines:
        if ln["group"] != group:
            continue
        t = totals.setdefault((ln["team"], ln["player"], ln.get("pos", "")),
                              {"_name": ln["name"], "_team": ln["team_name"], "_pos": ln.get("pos", ""),
                               "_id": ln["player"]})
        _sum_into(t, ln, keys)
    rows = [_derive(group, t) for t in totals.values()]
    work = {"batting": "plateAppearances", "pitching": "outs", "fielding": "gamesPlayed"}[group]
    rows.sort(key=lambda r: (-(r.get(work) or 0), r["_name"]))
    return rows


def post_team_totals(lines: list[dict], games: dict) -> list[dict]:
    """One row per club: its batting, pitching and fielding summed over the chosen series
    (the Stats tab's Team totals fields), with "gamesPlayed" the games it played."""
    teams: dict = {}
    for ln in lines:
        t = teams.setdefault(ln["team"], {"batting": {}, "pitching": {}, "fielding": {},
                                          "_team": ln["team_name"], "_key": ln["team"]})
        keys = {"batting": POST_BATTING_KEYS, "pitching": POST_PITCHING_KEYS,
                "fielding": POST_FIELDING_KEYS}[ln["group"]]
        _sum_into(t[ln["group"]], ln, keys)
    out = []
    for key, t in teams.items():
        row = {"_team": t["_team"], "_key": key}
        for group in ("batting", "pitching", "fielding"):
            row[group] = _derive(group, dict(t[group]))
            row[group]["gamesPlayed"] = games.get(key)
        row["pitching"]["wildPitches"] = t["pitching"].get("wildPitches")
        out.append(row)
    out.sort(key=lambda r: r["_team"])
    return out


# --------------------------------------------------------------------------- #
#  Gameday: one game's play-by-play.                                          #
# --------------------------------------------------------------------------- #
# The game feed (``/api/v1.1/game/{pk}/feed/live``) is what MLB's own Gameday draws from:
# line score, decisions and every plate appearance.  How much a play carries depends on the
# era -- measured on sampled games: nothing before about 1950 (line score and decisions only);
# one line per plate appearance from then, with the ball/strike sequence patchy until the late
# 1980s; every pitch's speed and location from 2008 (PITCHf/x); exit velocity, launch angle and
# distance from 2015 (Statcast).  A finished game's feed is ~0.5-0.8 MB and is cached forever.
GAMEDAY_GAME_TYPES = "R,F,D,L,W"  # regular season + the four postseason rounds (no spring, exhibitions)
GAME_TYPE_LABELS = {"F": "Wild Card", "D": "Division Series", "L": "LCS", "W": "World Series"}
GAMEDAY_LIVE_TTL = 15  # seconds before a game in progress is refetched
# Non-pitch events that are only noise in a play list.
_GAMEDAY_QUIET_EVENTS = ("Batter Timeout", "Mound Visit", "Game Advisory")
_BASE_NAMES = {None: "home", "1B": "1st", "2B": "2nd", "3B": "3rd", "4B": "home", "score": "home"}


def _game_state(status: dict) -> str:
    abstract = (status or {}).get("abstractGameState", "")
    return {"Final": "final", "Live": "live"}.get(abstract, "scheduled")


def parse_team_schedule(payload: dict, team_id: int) -> list[dict]:
    """A ``/schedule`` payload for one team as its games in date order, each::

        {"pk", "date" (YYYY-MM-DD), "type" (R F D L W), "round" (label or ""), "series_game",
         "home" (bool: the team was at home), "opponent", "state" (final/live/scheduled),
         "detailed", "runs", "opp_runs", "result" ("W"/"L"/"T"/""), "game_number"}

    Postponed and cancelled listings are dropped (the game is listed again when it is played);
    a game listed twice (a suspended game resumed another day) is kept once, on its last date."""
    by_pk: dict = {}
    for day in payload.get("dates") or []:
        for g in day.get("games") or []:
            status = g.get("status") or {}
            if status.get("detailedState") in _DROPPED_STATES:
                continue
            teams = g.get("teams") or {}
            home = ((teams.get("home") or {}).get("team") or {}).get("id") == team_id
            us, them = (teams.get("home"), teams.get("away")) if home else (teams.get("away"), teams.get("home"))
            us, them = us or {}, them or {}
            state = _game_state(status)
            runs, opp_runs = us.get("score"), them.get("score")
            result = ""
            if state == "final" and runs is not None and opp_runs is not None:
                result = "W" if runs > opp_runs else "L" if runs < opp_runs else "T"
            game_type = g.get("gameType", "R")
            by_pk[g.get("gamePk")] = {
                "pk": g.get("gamePk"),
                "date": g.get("officialDate") or day.get("date", ""),
                "type": game_type,
                "round": GAME_TYPE_LABELS.get(game_type, ""),
                "series_game": g.get("seriesGameNumber"),
                "home": home,
                "opponent": (them.get("team") or {}).get("name", "?"),
                "state": state,
                "detailed": status.get("detailedState", ""),
                "runs": runs,
                "opp_runs": opp_runs,
                "result": result,
                "game_number": g.get("gameNumber", 1),
                "double_header": g.get("doubleHeader", "N") != "N",
            }
    games = [g for g in by_pk.values() if g["pk"] is not None]
    games.sort(key=lambda g: (g["date"], g["game_number"]))
    return games


def get_team_games(team_id: int, season: int, ttl: float | None = None) -> list[dict]:
    """One team's games in ``season``: the regular season and the postseason."""
    payload = api_get("/schedule", {"sportId": SPORT_ID, "teamId": team_id, "season": season,
                                    "gameType": GAMEDAY_GAME_TYPES}, ttl=ttl)
    return parse_team_schedule(payload, team_id)


LINESCORE_SUMMARY_VERSION = 1
# only what a line score needs: a season with every game's innings is ~1.3 MB instead of ~7
_LINESCORE_FIELDS = ("dates,date,games,gamePk,gameType,officialDate,gameNumber,status,detailedState,"
                     "abstractGameState,teams,away,home,team,id,name,score,linescore,innings,num,runs")


def parse_linescores(payload: dict) -> tuple[list[dict], list[tuple]]:
    """A league-wide ``/schedule`` payload hydrated with line scores -> (each finished game
    once per team that played it, the (away, home) team ids of each finished game with no line
    score).  Each::

        {"pk", "date", "type", "game_number", "team_id", "team", "opp_id", "opponent", "home",
         "runs", "opp_runs", "result", "innings" (its runs in each inning it batted),
         "opp_innings", "length" (the game's innings)}

    A team that didn't need its last turn at bat hasn't batted that inning (usually the home team;
    early in the 1900s the home club sometimes chose to bat first).  A line score
    that doesn't add up to the final score, or skips an inning, counts as having none."""
    rows, missing = _linescore_rows(payload)
    return _linescore_games(rows), missing


def _linescore_rows(payload: dict) -> tuple[list[list], list[tuple]]:
    """The checked line scores of a ``/schedule`` payload, one compact row per game:
    [pk, date, type, game_number, away_id, away, home_id, home, away_runs, home_runs, innings]."""
    rows, missing, seen = [], [], set()
    for day in payload.get("dates") or []:
        for g in day.get("games") or []:
            status = g.get("status") or {}
            if status.get("detailedState") in _DROPPED_STATES or _game_state(status) != "final":
                continue
            pk = g.get("gamePk")
            if pk is None or pk in seen:
                continue
            seen.add(pk)
            teams = g.get("teams") or {}
            innings = (g.get("linescore") or {}).get("innings") or []
            runs = {side: [(i.get(side) or {}).get("runs") for i in innings] for side in ("away", "home")}
            batted = {side: [r for r in got if r is not None] for side, got in runs.items()}
            score = {side: (teams.get(side) or {}).get("score") for side in ("away", "home")}
            if (not innings or [i.get("num") for i in innings] != list(range(1, len(innings) + 1))
                    # a side that didn't need its last turn (in the early 1900s the home club might bat first)
                    or any(len(b) < len(innings) - 1 or None in runs[side][:len(b)] for side, b in batted.items())
                    or max(len(b) for b in batted.values()) != len(innings)
                    or any(sum(b) != score[side] for side, b in batted.items())):
                missing.append(tuple(((teams.get(side) or {}).get("team") or {}).get("id")
                                     for side in ("away", "home")))
                continue
            team = {side: (teams.get(side) or {}).get("team") or {} for side in ("away", "home")}
            rows.append([pk, g.get("officialDate") or day.get("date", ""), g.get("gameType", "R"),
                         g.get("gameNumber") or 1, team["away"].get("id"), team["away"].get("name", "?"),
                         team["home"].get("id"), team["home"].get("name", "?"),
                         batted["away"], batted["home"], len(innings)])
    return rows, missing


def _linescore_games(rows: list[list]) -> list[dict]:
    """Compact line-score rows -> one record per team per game (see :func:`parse_linescores`)."""
    games = []
    for pk, date, gtype, number, away_id, away, home_id, home, away_runs, home_runs, length in rows:
        sides = ((away_id, away, away_runs, False), (home_id, home, home_runs, True))
        for (tid, name, inn, at_home), (oid, oname, opp, _h) in (sides, sides[::-1]):
            r, o = sum(inn), sum(opp)
            games.append({"pk": pk, "date": date, "type": gtype, "game_number": number,
                          "team_id": tid, "team": name, "opp_id": oid, "opponent": oname,
                          "home": at_home, "runs": r, "opp_runs": o,
                          "result": "W" if r > o else "L" if r < o else "T",
                          "innings": inn, "opp_innings": opp, "length": length})
    games.sort(key=lambda t: (t["date"], t["game_number"], t["pk"], not t["home"]))
    return games


def get_season_linescores(season: int, postseason: bool = False,
                          ttl: float | None = None) -> tuple[list[dict], list[tuple]]:
    """Every finished game of ``season`` inning by inning (line scores begin in 1901), once per
    team -- see :func:`parse_linescores`.  The postseason too if asked.

    A finished season (``ttl`` None) is kept as a compact summary of its line scores (a few
    hundred KB, quick to read) in place of MLB's whole schedule response."""
    path = os.path.join(CACHE_DIR, f"linescores-{season}-{'RP' if postseason else 'R'}.json")
    if ttl is None:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if saved.get("version") == LINESCORE_SUMMARY_VERSION:
                return _linescore_games(saved["rows"]), [tuple(m) for m in saved["missing"]]
        except (OSError, ValueError, KeyError, AttributeError):
            pass
    params = {"sportId": SPORT_ID, "season": season, "hydrate": "linescore",
              "gameType": GAMEDAY_GAME_TYPES if postseason else "R", "fields": _LINESCORE_FIELDS}
    rows, missing = _linescore_rows(api_get("/schedule", params, ttl=ttl))
    if ttl is None:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": LINESCORE_SUMMARY_VERSION, "rows": rows, "missing": missing},
                          fh, separators=(",", ":"))
            os.replace(tmp, path)
            # the summary stands in for the full response from now on
            os.remove(_cache_path(f"{API}/schedule?{urllib.parse.urlencode(sorted(params.items()))}"))
        except OSError:
            pass
    return _linescore_games(rows), missing


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _parse_pitch(event: dict) -> dict:
    details = event.get("details") or {}
    pdata = event.get("pitchData") or {}
    breaks = pdata.get("breaks") or {}
    count = event.get("count") or {}
    return {
        "n": event.get("pitchNumber"),
        "count": f"{count.get('balls', 0)}-{count.get('strikes', 0)}",
        "call": details.get("description") or (details.get("call") or {}).get("description", ""),
        "pitch": (details.get("type") or {}).get("description", ""),
        "mph": _num(pdata.get("startSpeed")),
        "spin": _num(breaks.get("spinRate")),
        "ivb": _num(breaks.get("breakVerticalInduced")),
        "hb": _num(breaks.get("breakHorizontal")),
        "zone": _num(pdata.get("zone")),
        "in_play": bool(details.get("isInPlay")),
        "strike": bool(details.get("isStrike")),
        "ball": bool(details.get("isBall")),
        "code": (details.get("type") or {}).get("code", ""),
        "track": _parse_track(pdata),
    }


# A pitch's flight as tracked (PITCHf/x from 2008, Statcast from 2015): MLB fits each pitch with
# constant acceleration, position(t) = (x0, y0, z0) + v0 t + a t^2 / 2, in feet and seconds.  x is
# toward the catcher's right (the first-base side), y from the point of home plate toward the
# pitcher, z up; (x0, y0, z0) is where the fit starts, 50 ft out.  "px"/"pz" are where the pitch
# crossed the front of the plate; "sz_top"/"sz_bot" the batter's strike zone.
_TRACK_KEYS = (("x0", "x0"), ("y0", "y0"), ("z0", "z0"), ("vx0", "vX0"), ("vy0", "vY0"), ("vz0", "vZ0"),
               ("ax", "aX"), ("ay", "aY"), ("az", "aZ"), ("px", "pX"), ("pz", "pZ"))


def _parse_track(pdata: dict) -> dict | None:
    coords = pdata.get("coordinates") or {}
    track = {key: _num(coords.get(raw)) for key, raw in _TRACK_KEYS}
    if any(v is None for v in track.values()):
        return None
    track.update(sz_top=_num(pdata.get("strikeZoneTop")), sz_bot=_num(pdata.get("strikeZoneBottom")),
                 extension=_num(pdata.get("extension")), plate_time=_num(pdata.get("plateTime")),
                 end_mph=_num(pdata.get("endSpeed")))
    return track


def _parse_hit(event: dict) -> dict | None:
    hit = event.get("hitData") or {}
    out = {
        "ev": _num(hit.get("launchSpeed")),
        "la": _num(hit.get("launchAngle")),
        "dist": _num(hit.get("totalDistance")),
        "trajectory": (hit.get("trajectory") or "").replace("_", " "),
        "hardness": hit.get("hardness") or "",
        "location": hit.get("location") or "",
        # where it was fielded (or landed), in the pixels of MLB's Gameday field drawing
        "spray": (lambda c: (c["coordX"], c["coordY"]) if _num(c.get("coordX")) is not None
                  and _num(c.get("coordY")) is not None else None)(hit.get("coordinates") or {}),
    }
    return out if any(v not in (None, "") for v in out.values()) else None


_BASE_SLOTS = ("1B", "2B", "3B")
_MATCHUP_BASES = ("postOnFirst", "postOnSecond", "postOnThird")


def _advance_bases(bases: tuple, runners: list, subs: list = (), upto: float = math.inf) -> tuple:
    """Who is on (1st, 2nd, 3rd) -- each a name or "" -- after a play's runner movements and
    pinch runners (``subs``: (event index, base 1-3, name)) up to play event ``upto`` (all of
    them by default).  A runner's moves at one event are taken together (his first start, his
    last end), so "1B -> 2B, 2B -> 3B" on one hit leaves him on 3rd alone; every runner leaves
    before anyone arrives, so 2nd -> 3rd and 1st -> 2nd don't collide."""
    groups: dict = {}
    for r in runners:
        move, det = r.get("movement") or {}, r.get("details") or {}
        at = det.get("playIndex")
        at = math.inf if at is None else at
        if at > upto:
            continue
        who = det.get("runner") or {}
        key = who.get("id") or who.get("fullName")
        moves = groups.setdefault(at, {})
        if key in moves:
            moves[key]["end"], moves[key]["out"] = move.get("end"), bool(move.get("isOut"))
        else:
            moves[key] = {"name": who.get("fullName", "?"), "start": move.get("start"), "end": move.get("end"),
                          "out": bool(move.get("isOut"))}
    on = dict(zip(_BASE_SLOTS, bases))
    events = [(at, 1, moves) for at, moves in groups.items()]
    events += [(at, 0, (base, name)) for at, base, name in subs if at <= upto]
    for at, kind, what in sorted(events, key=lambda e: e[:2]):
        if kind == 0:  # a pinch runner takes the base (before anything else at that moment)
            base, name = what
            on[_BASE_SLOTS[base - 1]] = name
            continue
        for m in what.values():
            if m["start"] in on:
                on[m["start"]] = ""
        for m in what.values():
            if m["end"] in on and not m["out"]:
                on[m["end"]] = m["name"]
    return tuple(on[b] for b in _BASE_SLOTS)


def _height_inches(players: dict, pid) -> int | None:
    """A player's height from the feed's roster ("6' 2\"") in inches, or None."""
    m = re.match(r"\s*(\d+)\s*'\s*(\d+)?", (players.get(f"ID{pid}") or {}).get("height") or "")
    return int(m.group(1)) * 12 + int(m.group(2) or 0) if m else None


def parse_gameday(feed: dict) -> dict:
    """A ``/game/{pk}/feed/live`` payload trimmed to what the Gameday tab shows::

        {"pk", "date", "type", "round", "state", "detailed", "venue", "current" ("Top 5th" live),
         "away"/"home": {"id", "name", "abbr", "runs", "hits", "errors", "lob"},
         "innings": [{"num", "away": {"runs", "hits", "errors", "lob"}, "home": {...}}],
         "decisions": {"W", "L", "S"} (names, when known), "plays": [...],
         "sequences" (full/partial/none, see :func:`play_has_sequence`), "has_pitches",
         "has_speed", "has_statcast"}

    A play: {"index", "inning", "half" ("top"/"bottom"), "batter", "bat_side", "pitcher",
    "pitch_hand", "event", "description", "rbi", "away_score", "home_score", "outs",
    "scoring", "complete", "count", "pitches": [...], "notes": [(pitches before, text)],
    "hit": {...} or None, "runners": [{"name", "start", "end", "out", "scored", "event",
    "earned", "rbi"}], "bases_before", "bases" (after the play), "note_bases" (after each
    note), "note_outs", "note_runs" (runs scored at each note: a wild pitch, a steal of home),
    "note_scores" ((away, home) after each note, or None), "note_labels" (MLB's short name for a
    note that isn't a play -- "Defensive Switch", "Ejection" -- or "" for one that moves runners),
    "play_runs" (the rest of the play's runs)}.  Bases are (1st, 2nd, 3rd), each a runner's
    name or "".

    A game in progress also has "live": {"bases", "outs", "balls", "strikes", "batter", "pitcher"} -- the
    situation right now."""
    game_data = feed.get("gameData") or {}
    live = feed.get("liveData") or {}
    status = game_data.get("status") or {}
    game = game_data.get("game") or {}
    linescore = live.get("linescore") or {}
    line_teams = linescore.get("teams") or {}

    def team(side):
        t = (game_data.get("teams") or {}).get(side) or {}
        totals = line_teams.get(side) or {}
        return {"id": t.get("id"), "name": t.get("name", side.title()),
                "abbr": t.get("abbreviation") or t.get("teamName") or t.get("name", side.title()),
                "runs": totals.get("runs"), "hits": totals.get("hits"),
                "errors": totals.get("errors"), "lob": totals.get("leftOnBase")}

    innings = []
    for inn in linescore.get("innings") or []:
        innings.append({"num": inn.get("num"),
                        "away": {k: (inn.get("away") or {}).get(k) for k in ("runs", "hits", "errors", "leftOnBase")},
                        "home": {k: (inn.get("home") or {}).get(k) for k in ("runs", "hits", "errors", "leftOnBase")}})
    decisions = {}
    for key, label in (("winner", "W"), ("loser", "L"), ("save", "S")):
        who = (live.get("decisions") or {}).get(key)
        if who and who.get("fullName"):
            decisions[label] = who["fullName"]

    plays = []
    bases, half = ("", "", ""), None
    players = game_data.get("players") or {}
    all_plays = (live.get("plays") or {}).get("allPlays") or []
    # MLB's own "who's on after the play" (matchup.postOn*), in every feed with play-by-play
    # sampled; without it the runner movements are replayed
    has_post = any(k in (raw.get("matchup") or {}) for raw in all_plays for k in _MATCHUP_BASES)
    for raw in all_plays:
        about, result = raw.get("about") or {}, raw.get("result") or {}
        matchup = raw.get("matchup") or {}
        if (about.get("inning"), about.get("halfInning")) != half:
            half, bases = (about.get("inning"), about.get("halfInning")), ("", "", "")
        moves = raw.get("runners") or []
        subs = []  # pinch runners: (event index, base, name)
        for at, event in enumerate(raw.get("playEvents") or []):
            if (event.get("details") or {}).get("eventType") == "offensive_substitution" and event.get("base") in (1, 2, 3):
                pid = (event.get("player") or {}).get("id")
                name = (players.get(f"ID{pid}") or {}).get("fullName")
                if name:
                    subs.append((event.get("index", at), event["base"], name))
        # anyone a runner moves off (or MLB has standing on after the play) without our having
        # put him there was on before it: extra innings' automatic runner, a lost pinch runner
        before, seen = list(bases), set()
        for r in moves:
            who = (r.get("details") or {}).get("runner") or {}
            start = (r.get("movement") or {}).get("start")
            if who.get("id") in seen:
                continue  # only where a runner began the play counts, not his later legs
            seen.add(who.get("id"))
            if start in _BASE_SLOTS and not before[_BASE_SLOTS.index(start)] and who.get("fullName") not in before:
                before[_BASE_SLOTS.index(start)] = who.get("fullName", "?")
        replayed = _advance_bases(tuple(before), moves, subs)
        posted = tuple((matchup.get(k) or {}).get("fullName", "") for k in _MATCHUP_BASES) if has_post else replayed
        for i, name in enumerate(posted):
            moved = {((r.get("details") or {}).get("runner") or {}).get("fullName") for r in moves}
            if name and not before[i] and name not in moved and name not in before \
                    and name not in (n for _a, _b, n in subs):
                before[i] = name
        bases = tuple(before)
        pitches, notes, hit = [], [], None
        note_bases, note_outs, note_runs, note_scores, note_labels = [], [], [], [], []
        scored_at = [(r.get("details") or {}).get("playIndex") for r in moves
                     if (r.get("movement") or {}).get("end") == "score"]
        for at, event in enumerate(raw.get("playEvents") or []):
            etype = event.get("type")
            details = event.get("details") or {}
            if event.get("isPitch") or etype == "no_pitch":
                pitch = _parse_pitch(event)
                if etype == "no_pitch" and not pitch["call"]:
                    continue  # a bare "N" marker (seen around balks) -- not a ball or strike
                if etype == "no_pitch":
                    pitch["n"] = None
                pitches.append(pitch)
                found = _parse_hit(event)
                if found:
                    hit = found
            elif etype == "action":
                label = details.get("event") or ""
                text = details.get("description") or label
                if text and not any(label.startswith(q) for q in _GAMEDAY_QUIET_EVENTS):
                    notes.append((len(pitches), text))
                    note_bases.append(_advance_bases(bases, moves, subs, event.get("index", at)))
                    note_outs.append((event.get("count") or {}).get("outs"))
                    note_runs.append(scored_at.count(event.get("index", at)))
                    note_scores.append((details["awayScore"], details["homeScore"])
                                       if "awayScore" in details and "homeScore" in details else None)
                    moved = any((r.get("details") or {}).get("playIndex") == event.get("index", at) for r in moves)
                    note_labels.append("" if event.get("isBaseRunningPlay") or moved else label)
        runners = []
        for r in raw.get("runners") or []:
            move, det = r.get("movement") or {}, r.get("details") or {}
            runners.append({
                "name": (det.get("runner") or {}).get("fullName", "?"),
                "start": _BASE_NAMES.get(move.get("start"), move.get("start") or ""),
                "batter": move.get("start") is None,
                "end": _BASE_NAMES.get(move.get("end"), move.get("end") or "") if move.get("end") else "",
                "out": bool(move.get("isOut")),
                "out_base": _BASE_NAMES.get(move.get("outBase"), move.get("outBase")) if move.get("outBase") else "",
                "scored": move.get("end") == "score",
                "event": det.get("event") or "",
                "earned": bool(det.get("earned")),
                "rbi": bool(det.get("rbi")),
            })
        count = raw.get("count") or {}
        after = posted if has_post else _advance_bases(bases, moves, subs)
        if count.get("outs") == 3 and about.get("isComplete", True):
            after = ("", "", "")  # the side is retired: nobody stays on
        plays.append({
            "index": about.get("atBatIndex", len(plays)),
            "inning": about.get("inning"),
            "half": about.get("halfInning", "top"),
            "batter": (matchup.get("batter") or {}).get("fullName", "?"),
            "bat_side": (matchup.get("batSide") or {}).get("code", ""),
            "batter_height": _height_inches(players, (matchup.get("batter") or {}).get("id")),
            "pitcher": (matchup.get("pitcher") or {}).get("fullName", "?"),
            "pitch_hand": (matchup.get("pitchHand") or {}).get("code", ""),
            "pitcher_height": _height_inches(players, (matchup.get("pitcher") or {}).get("id")),
            "event": result.get("event") or "",
            "description": result.get("description") or "",
            "rbi": result.get("rbi") or 0,
            "away_score": result.get("awayScore"),
            "home_score": result.get("homeScore"),
            "outs": count.get("outs"),
            "count": f"{count.get('balls', 0)}-{count.get('strikes', 0)}",
            "scoring": bool(about.get("isScoringPlay")),
            "complete": bool(about.get("isComplete", True)),
            "pitches": pitches,
            "notes": notes,
            "hit": hit,
            "runners": runners,
            "bases_before": bases,
            "bases": after,
            "note_bases": note_bases,
            "note_outs": note_outs,
            "note_runs": note_runs,
            "note_scores": note_scores,
            "note_labels": note_labels,
            "play_runs": len(scored_at) - sum(note_runs),
        })
        bases = after

    state = _game_state(status)
    current = ""
    if state == "live" and linescore.get("currentInningOrdinal"):
        current = f"{linescore.get('inningState') or linescore.get('inningHalf', '')} {linescore['currentInningOrdinal']}".strip()
    game_type = game.get("type", "R")
    return {
        "pk": game.get("pk"),
        "date": (game_data.get("datetime") or {}).get("officialDate", ""),
        "type": game_type,
        "round": GAME_TYPE_LABELS.get(game_type, ""),
        "state": state,
        "detailed": status.get("detailedState", ""),
        "venue": (game_data.get("venue") or {}).get("name", ""),
        "venue_id": (game_data.get("venue") or {}).get("id"),
        "venue_field": (game_data.get("venue") or {}).get("fieldInfo") or {},
        "current": current,
        "scheduled_innings": linescore.get("scheduledInnings") or 9,
        "away": team("away"),
        "home": team("home"),
        "innings": innings,
        "decisions": decisions,
        "plays": plays,
        # MLB's own surname for each player in the game ("De Paula", "La Stella", "Guerrero Jr."): the last word of
        # the full name would be "Paula"
        "surnames": {v["fullName"]: (v.get("useLastName") or v.get("lastName"))
                     for v in players.values() if v.get("fullName") and (v.get("useLastName") or v.get("lastName"))},
        # Before the late 1980s MLB has the ball-strike sequence for only some plate appearances;
        # the rest carry a lone "in play" pitch at 0-0 that says nothing.  "full" when the game
        # averages 2+ pitches a plate appearance (a modern game ~3.9, 1995 ~4.2, 1975 ~1.7).
        "sequences": _sequences(plays),
        "has_pitches": any(len(p["pitches"]) > 1 for p in plays),
        "has_speed": any(q["mph"] is not None for p in plays for q in p["pitches"]),
        "has_statcast": any(p["hit"] and p["hit"]["ev"] is not None for p in plays),
        "box": parse_boxscore(feed),
        "live": _live_situation(linescore) if state == "live" else None,
    }


def _live_situation(linescore: dict) -> dict:
    """A game in progress right now, from its line score: who is on, the outs and count."""
    offense = linescore.get("offense") or {}
    return {"bases": tuple((offense.get(k) or {}).get("fullName", "") for k in ("first", "second", "third")),
            "outs": linescore.get("outs"), "balls": linescore.get("balls"), "strikes": linescore.get("strikes"),
            "batter": (offense.get("batter") or {}).get("fullName", ""),
            "pitcher": ((linescore.get("defense") or {}).get("pitcher") or {}).get("fullName", "")}


def _sequences(plays: list[dict]) -> str:
    """How much of a game's pitch-by-pitch record there is: "full", "partial" or "none"."""
    if not any(len(p["pitches"]) > 1 for p in plays):
        return "none"
    pitched = sum(1 for p in plays for q in p["pitches"] if q["n"] is not None)
    return "full" if pitched >= 2 * len(plays) else "partial"


def play_has_sequence(game: dict, play: dict) -> bool:
    """Whether ``play``'s pitches are a real record: always in a "full" game; in a "partial"
    one, only a plate appearance with more than one pitch (a lone 0-0 "in play" is a stand-in)."""
    if game["sequences"] == "full":
        return bool(play["pitches"])
    return game["sequences"] == "partial" and len(play["pitches"]) > 1


def get_gameday(game_pk: int, force: bool = False) -> dict:
    """One game's play-by-play (see :func:`parse_gameday`).  A finished game comes from the
    cache when it's there; a game still in progress (or cached while it was) is refetched once
    the copy is more than ``GAMEDAY_LIVE_TTL`` seconds old."""
    path = f"/game/{game_pk}/feed/live"
    feed = api_get(path, ttl=0 if force else None, base=API_V11)
    if not force and _game_state((feed.get("gameData") or {}).get("status")) != "final":
        feed = api_get(path, ttl=GAMEDAY_LIVE_TTL, base=API_V11)
    return parse_gameday(feed)


# Gameday zones, from the catcher's view (checked against pitch coordinates): 1-9 the strike zone
# in a 3x3 grid, 1 = top row on the third-base side; 11-14 outside it (11 high 3B side, 12 high
# 1B side, 13 low 3B side, 14 low 1B side).  A right-handed batter stands on the 3B side, so for
# him that side is "in"; for a left-handed batter it is "away".
_ZONE_GRID = {1: ("up", "3b"), 2: ("up", ""), 3: ("up", "1b"), 4: ("", "3b"), 5: ("", ""),
              6: ("", "1b"), 7: ("down", "3b"), 8: ("down", ""), 9: ("down", "1b"),
              11: ("up", "3b"), 12: ("up", "1b"), 13: ("down", "3b"), 14: ("down", "1b")}


def zone_location(zone, bat_side: str) -> str:
    """A Gameday zone in the batter's terms: 1 -> "Up & in" for a right-handed batter ("Up &
    away" for a left-handed one), 5 -> "Middle", 14 -> "Out: down & away" (right-handed)."""
    try:
        height, side = _ZONE_GRID[int(zone)]
    except (KeyError, TypeError, ValueError):
        return ""
    if side and bat_side in ("R", "L"):
        side = "in" if (side == "3b") == (bat_side == "R") else "away"
    elif side:
        side = "3B side" if side == "3b" else "1B side"
    text = " & ".join(t for t in (height, side) if t) or "middle"
    text = text[0].upper() + text[1:]
    return f"Out: {text[0].lower() + text[1:]}" if int(zone) >= 11 else text


def break_side(horizontal, pitch_hand: str) -> str:
    """MLB's horizontal break (inches; positive toward the third-base side, checked against
    right-handers' fastballs at about +6 and sliders at about -7) as "6.3 arm side" or
    "7.0 glove side" -- the same words for either hand."""
    if horizontal is None:
        return ""
    if pitch_hand not in ("R", "L") or not horizontal:
        return f"{abs(horizontal):.1f}"
    arm = (horizontal > 0) == (pitch_hand == "R")
    return f"{abs(horizontal):.1f} {'arm' if arm else 'glove'} side"


def vertical_break(induced) -> str:
    """Induced vertical break (inches of lift from spin, gravity left out) as "16.0 up" or
    "5.2 down"."""
    if induced is None:
        return ""
    return f"{abs(induced):.1f} {'up' if induced >= 0 else 'down'}"


def gameday_rows(game: dict) -> tuple[list, list, list, list]:
    """A parsed game (:func:`parse_gameday`) as spreadsheet rows: ``(play headers, play rows,
    pitch headers, pitch rows)`` -- one row per plate appearance, and one per pitch."""
    play_headers = ["Inning", "Half", "Batter", "Bats", "Pitcher", "Throws", "Event", "Description",
                    "Count", "Outs", "RBI", "Away", "Home", "Scoring", "Exit velo", "Launch angle",
                    "Distance", "Trajectory"]
    pitch_headers = ["Inning", "Half", "Batter", "Bats", "Pitcher", "Throws", "Pitch #", "Count", "Result",
                     "Pitch", "MPH", "Spin (rpm)", "Induced vertical break (in)",
                     "Horizontal break (in, + = 3B side)", "Break side", "Zone", "Location"]
    plays, pitches = [], []
    for p in game["plays"]:
        hit = p["hit"] or {}
        known = play_has_sequence(game, p)
        plays.append([p["inning"], p["half"], p["batter"], p["bat_side"], p["pitcher"], p["pitch_hand"],
                      p["event"], p["description"], p["count"] if known else "", p["outs"], p["rbi"], p["away_score"],
                      p["home_score"], "yes" if p["scoring"] else "", hit.get("ev"), hit.get("la"),
                      hit.get("dist"), hit.get("trajectory", "")])
        for q in p["pitches"] if known else []:
            side = break_side(q["hb"], p["pitch_hand"]).partition(" ")[2]
            pitches.append([p["inning"], p["half"], p["batter"], p["bat_side"], p["pitcher"], p["pitch_hand"],
                            q["n"], q["count"], q["call"], q["pitch"], q["mph"], q["spin"], q["ivb"], q["hb"],
                            side, q["zone"], zone_location(q["zone"], p["bat_side"])])
    return play_headers, plays, pitch_headers, pitches


# --------------------------------------------------------------------------- #
#  Boxscore: the box score that rides along in the Gameday feed.              #
# --------------------------------------------------------------------------- #
# ``liveData.boxscore`` in the game feed is MLB's box score: every game from 1901 (the batting
# and pitching lines come from Retrosheet's box scores for games before play-by-play), filled in
# as a game goes on.  A game in progress has the lines so far; notes such as pitches-strikes
# and umpires appear as MLB adds them.
BOX_BATTING = [("AB", "atBats"), ("R", "runs"), ("H", "hits"), ("RBI", "rbi"), ("BB", "baseOnBalls"),
               ("SO", "strikeOuts"), ("LOB", "leftOnBase")]
BOX_PITCHING = [("IP", "inningsPitched"), ("H", "hits"), ("R", "runs"), ("ER", "earnedRuns"),
                ("BB", "baseOnBalls"), ("SO", "strikeOuts"), ("HR", "homeRuns"), ("NP", "numberOfPitches")]


def ip_outs(ip) -> int:
    """Innings pitched as written ("5.2") in outs (17)."""
    try:
        whole, _, part = str(ip).partition(".")
        return int(whole or 0) * 3 + int(part or 0)
    except ValueError:
        return 0


def outs_ip(outs: int) -> str:
    return f"{outs // 3}.{outs % 3}"


def _box_side(side: dict) -> dict:
    players = side.get("players") or {}

    def player(pid):
        return players.get(f"ID{pid}") or {}

    batters = []
    # MLB's list repeats the starting pitcher at its end in many old games (pre-1950 or so): once each
    for pid in dict.fromkeys(side.get("batters") or []):
        p = player(pid)
        order = p.get("battingOrder")
        if not order:
            continue  # a pitcher who never came to bat (DH games) is listed only with the pitchers
        stats = (p.get("stats") or {}).get("batting") or {}
        season = (p.get("seasonStats") or {}).get("batting") or {}
        positions = [x.get("abbreviation", "") for x in p.get("allPositions") or []] \
            or [(p.get("position") or {}).get("abbreviation", "")]
        batters.append({
            "id": pid,
            "name": (p.get("person") or {}).get("fullName", "?"),
            "pos": "-".join(x for x in positions if x),
            "order": int(order) // 100,
            "sub": int(order) % 100 != 0,
            "_seq": int(order),
            "current": bool((p.get("gameStatus") or {}).get("isCurrentBatter")),
            "stats": {key: _num(stats.get(key)) for _label, key in BOX_BATTING},
            "avg": season.get("avg", ""),
            "ops": season.get("ops", ""),
        })
    batters.sort(key=lambda b: b["_seq"])
    for b in batters:
        del b["_seq"]
    pitchers = []
    for pid in side.get("pitchers") or []:
        p = player(pid)
        stats = (p.get("stats") or {}).get("pitching") or {}
        season = (p.get("seasonStats") or {}).get("pitching") or {}
        line = {key: _num(stats.get(key)) for _label, key in BOX_PITCHING if key != "inningsPitched"}
        line["inningsPitched"] = stats.get("inningsPitched") or "0.0"
        pitchers.append({
            "id": pid,
            "name": (p.get("person") or {}).get("fullName", "?"),
            "note": stats.get("note") or "",
            "current": bool((p.get("gameStatus") or {}).get("isCurrentPitcher")),
            "stats": line,
            "strikes": _num(stats.get("strikes")),
            "era": season.get("era", ""),
        })
    totals_bat = {key: sum(b["stats"][key] or 0 for b in batters) for _label, key in BOX_BATTING}
    totals_pit = {key: sum(p["stats"][key] or 0 for p in pitchers) for _label, key in BOX_PITCHING
                  if key != "inningsPitched"}
    totals_pit["inningsPitched"] = outs_ip(sum(ip_outs(p["stats"]["inningsPitched"]) for p in pitchers))
    return {
        "batters": batters,
        "pitchers": pitchers,
        "batting_totals": totals_bat,
        "pitching_totals": totals_pit,
        "notes": [f"{n.get('label', '')}-{n.get('value', '')}" for n in side.get("note") or []],
        "info": [(block.get("title", ""), [(f.get("label", ""), f.get("value", ""))
                                          for f in block.get("fieldList") or []])
                 for block in side.get("info") or []],
    }


def parse_boxscore(feed: dict) -> dict:
    """The game feed's box score (``liveData.boxscore``)::

        {"away"/"home": {"batters": [{"id", "name", "pos" ("PH-LF"), "order" (1-9), "sub",
                                      "current", "stats": {AB, R... by API key}, "avg", "ops"}],
                         "pitchers": [{"id", "name", "note" ("(W, 2-0)"), "current", "stats",
                                       "strikes", "era"}],
                         "batting_totals", "pitching_totals", "notes" (substitutes),
                         "info": [(title, [(label, value)])]},
         "pitching_notes": [str], "info": [(label, value)]}

    ``avg``/``ops``/``era`` are the player's season to date, as MLB's box score prints them.
    Totals are summed from the players' lines (MLB's own team totals are wrong in some old games)."""
    box = (feed.get("liveData") or {}).get("boxscore") or {}
    teams = box.get("teams") or {}
    return {
        "away": _box_side(teams.get("away") or {}),
        "home": _box_side(teams.get("home") or {}),
        "pitching_notes": list(box.get("pitchingNotes") or []),
        "info": [(i.get("label", ""), i.get("value", "")) for i in box.get("info") or [] if i.get("value")],
    }


# --------------------------------------------------------------------------- #
#  Win probability: MLB's own per-play figures, for the WPA tab.              #
# --------------------------------------------------------------------------- #
# ``/game/{pk}/winProbability`` gives, for each finished plate appearance, the home team's chance
# of winning after it and how much the play changed that chance (the play's Win Probability
# Added).  It exists for games with play-by-play (from about 1950) -- a 404 before that -- and
# for a game in progress it covers the plate appearances finished so far.
_WP_FIELDS = ("atBatIndex,homeTeamWinProbability,homeTeamWinProbabilityAdded,leverageIndex,"
              "about,inning,halfInning,isComplete,result,event,description,awayScore,homeScore,"
              "matchup,batter,pitcher,id,fullName")


def parse_win_probability(payload) -> dict:
    """``/game/{pk}/winProbability`` as ``{"start": home win % before the first pitch,
    "plays": [{"index", "inning", "half", "batter", "batter_id", "pitcher", "pitcher_id",
    "event", "description", "away_score", "home_score", "home_wp" (after the play),
    "added" (home team's gain, percentage points), "li" (leverage index)}]}``."""
    plays = []
    for raw in payload if isinstance(payload, list) else []:
        wp = _num(raw.get("homeTeamWinProbability"))
        added = _num(raw.get("homeTeamWinProbabilityAdded"))
        if wp is None or added is None:
            continue
        about, result, matchup = raw.get("about") or {}, raw.get("result") or {}, raw.get("matchup") or {}
        batter, pitcher = matchup.get("batter") or {}, matchup.get("pitcher") or {}
        plays.append({
            "index": raw.get("atBatIndex", about.get("atBatIndex")),
            "inning": about.get("inning"),
            "half": about.get("halfInning", "top"),
            "batter": batter.get("fullName", "?"), "batter_id": batter.get("id"),
            "pitcher": pitcher.get("fullName", "?"), "pitcher_id": pitcher.get("id"),
            "event": result.get("event") or "",
            "description": result.get("description") or "",
            "away_score": result.get("awayScore"), "home_score": result.get("homeScore"),
            "home_wp": round(wp, 1),
            "added": round(added, 1),
            "li": _num(raw.get("leverageIndex")),
        })
    start = round(plays[0]["home_wp"] - plays[0]["added"], 1) if plays else 50.0
    return {"start": start, "plays": plays}


def get_win_probability(game_pk: int, final: bool, complete_plays: int = 0, force: bool = False) -> dict:
    """MLB's win probability for one game (see :func:`parse_win_probability`); an empty play
    list when MLB has none.  A finished game is cached for good once the copy covers all
    ``complete_plays`` of it; a game in progress is refetched after ``GAMEDAY_LIVE_TTL`` s."""
    path, params = f"/game/{game_pk}/winProbability", {"fields": _WP_FIELDS}
    try:
        ttl = 0 if force else (None if final else GAMEDAY_LIVE_TTL)
        result = parse_win_probability(api_get(path, params, ttl=ttl))
        if final and not force and len(result["plays"]) < complete_plays:  # cached while the game was on
            result = parse_win_probability(api_get(path, params, ttl=0))
    except MLBStatsError as exc:
        if "HTTP 404" in str(exc):
            return {"start": 50.0, "plays": []}
        raise
    return result


def wpa_by_player(wp: dict) -> list[dict]:
    """Each player's Win Probability Added over the game, in percentage points: a batter is
    credited with his plate appearances' change in his team's chances, a pitcher with the
    change in his team's (so a strikeout is + for the pitcher, - for the batter).  Rows
    ``{"id", "name", "side" (away/home), "bat", "pitch", "total"}``, biggest total first."""
    rows: dict = {}

    def row(pid, name, side):
        return rows.setdefault((pid, name), {"id": pid, "name": name, "side": side,
                                             "bat": 0.0, "pitch": 0.0, "pa": 0, "bf": 0})
    for p in wp["plays"]:
        batting = "away" if p["half"] == "top" else "home"
        fielding = "home" if batting == "away" else "away"
        gain = p["added"] if batting == "home" else -p["added"]
        b = row(p["batter_id"], p["batter"], batting)
        b["bat"] += gain
        b["pa"] += 1
        q = row(p["pitcher_id"], p["pitcher"], fielding)
        q["pitch"] -= gain
        q["bf"] += 1
    out = list(rows.values())
    for r in out:
        r["bat"], r["pitch"] = round(r["bat"], 1), round(r["pitch"], 1)
        r["total"] = round(r["bat"] + r["pitch"], 1)
    out.sort(key=lambda r: -r["total"])
    return out
