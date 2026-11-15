#!/usr/bin/env python3
"""The Simulator tab's engine: plays games between two team-seasons, plate appearance by plate appearance.

No Qt here.  Each side takes the field the way BallSheet's other tabs would set it up by default:

* **batting order** -- the Production tab's: whoever batted from each place most games (from 1950;
  before that the nine with the most plate appearances, most first);
* **pitching staff** -- the Prevention tab's roles (Rotation, Long Relief, Closers, Other);
* **defense** -- the Range tab's: whoever played each position the most innings;
* **running** -- the Speed tab's per-runner counts from Retrosheet (and its Speedy / Smart / Slow labels).

How one plate appearance is decided
-----------------------------------
1.  **What the batter does, against what the pitcher allows.**  Each batter's line (MLB's season
    figures) becomes the share of his plate appearances that ended in each of nine results -- strikeout,
    walk, hit by pitch, single, double, triple, home run, ground-ball out, air out -- and the same for each
    pitcher's batters faced.  Each is first pulled a little toward his league's rates (as if he had
    ``SHRINK_PA`` more plate appearances at the league average), so a ten-at-bat cameo can't bat .600.
2.  **Odds-ratio ("log5") combination.**  For each result ``i``::

        p_i  proportional to  (batter_i / batter's league_i) x (pitcher_i / pitcher's league_i) x environment_i

    and the nine are scaled to add up to 1.  A batter who walks twice as often as his league, facing a
    pitcher who walks twice as few, walks at the environment's rate.  This is Bill James's log5 method
    extended to several outcomes; the environment is the home team's league that season, in its park
    (:func:`park_factors`; each player's own line has half his home park taken out first), with the season's edge of
    batting at home -- the home batters' rates against the visitors' in the same games (:func:`home_edge`) --
    for whichever side is batting.
    **Platoon**: with both hands known, the batter's rates are his against the pitcher's hand and the pitcher's
    his against the batter's side (:func:`platoon_rates`: the league's platoon effect divided out of his season,
    his own MLB splits from 1950 moved in, heavily pulled toward his season), and the environment is the league's
    for that matchup of hands (:func:`platoon_ratios`, from Retrosheet) -- the effect counted once
    (:func:`platoon_matchup`).
3.  **Fielding.**  Each ball in play goes where that kind of ball went in the environment season
    (Retrosheet's hit locations, :func:`retrosheet.field_zone`): one fielder's ground or a hole or gap
    two share.  Only the fielders whose ground it is count on it (:func:`apply_fielding`,
    :func:`zone_factors`).  The pitchers' own lines already include their club's fielders all season, so
    each fielder's range is taken against the club's season at his position, and the odds of a hit on his
    ground are scaled by ``(his range / the club's) ^ -DEFENSE_ELASTICITY``.  ``DEFENSE_ELASTICITY`` was
    measured over 168 team-seasons (1975-2023): a club whose fielders reached 1% more balls than MLB's, per
    ball in play, allowed about 1.27% fewer hits on them in odds (r = -0.52).  Reaching on an error is the
    league's share of the outs on that ground, scaled by the fielder's errors per chance against MLB's.
4.  **What happens to the runners** is drawn from what really happened in the environment season
    (Retrosheet's play-by-play, :func:`retrosheet.parse_sim`): for that kind of result in that base-out
    state, each outcome -- where the batter and each runner ended up -- comes up as often as it did in the
    majors.  Runner and fielder meet as batter and pitcher do: each against his own league, multiplied in
    odds.  Each runner tilts the outcomes by his own record times the fielder's (exponential tilting: an
    outcome where he takes ``x`` extra bases is weighted by ``factor ^ x``): on singles and doubles his extra
    bases taken times the arm of the fielder who picked up the ball (who, in that zone, from Retrosheet), on
    air outs his tag-ups times the catcher of the fly's; on ground outs that make two outs, the batter's
    double-play rate times the fielder's double plays turned.
5.  **Intentional walks and sacrifice bunts** are decided before the pitch, by the situation: Retrosheet's rate of
    each in that base-out state, stage of the game and margin (:class:`Situations`), scaled to the home league's
    rate and tilted by the batter's own per plate appearance against his league's, in odds.  A sacrifice moves
    the runners as that season's sacrifices did.  They are taken out of every line (and of the play tables) first,
    so the plate appearances that are played out are the rest.
6.  **Between plate appearances**: a runner with the next base open may try to steal -- the league's attempt
    and success rates from that base, tilted by his own times the catcher's (against MLB's catchers) and the
    pitcher's (against his own staff, who threw to the same catchers); and wild pitches, passed balls, balks,
    pickoffs happen as often as they did in that base-out state.

Managing the game
-----------------
Starters take turns in proportion to their real starts and pitch until they have faced their average
batters per start; relievers go for their average batters per relief appearance.  The relief role is picked
by the situation: the Closers' busiest finisher (most GF) for a lead of 1-3 runs from the 9th inning on --
coming in at the start of the inning if whoever is pitching would run out during it -- the other Closers for
the 7th and 8th with the score close (down 1 to up 3), Long Relief when the starter goes before the 6th,
Other and Long Relief otherwise; within a role, relievers are chosen in proportion to their relief work.
From 2020 on (the home team's season) extra innings start with a runner on second.  Without the DH the pitcher
is pinch-hit for (a reliever always, the starter from the 7th), and late in close games a Slow regular on base is
pinch-run for, the best glove on the bench then taking his position; from the 7th, with his side 3 behind to 1
ahead, a regular is pinch-hit for by a bench hitter worth ``PH_EDGE`` runs a plate appearance more against that
pitcher (:meth:`Game._pinch_hit_regular`: platoon and all); and from the 8th, with a lead of 1-3, a clearly better
glove on the bench goes in on defense (:meth:`Game._defensive_changes`).  Relievers aren't chosen for their hand.  Intentional walks and sacrifices follow the situation (point
5).  With ``fatigue`` regulars rest as often as they really didn't start (:meth:`Team.day_lineup`), and a reliever
who pitched the last two games, or long in the last, sits out (:func:`tired_relievers`)."""
from __future__ import annotations

import bisect
import math
import time
from array import array
import random

import mlbstats
import retrosheet
import stadiums

LOG5_CLASSES = ("K", "BB", "HBP", "1B", "2B", "3B", "HR", "GO", "AO")
CLASSES = retrosheet.SIM_CLASSES  # the nine above plus ROE (reached on error)
CLASS_NAMES = {"K": "Strikeout", "BB": "Walk", "HBP": "Hit by pitch", "1B": "Single", "2B": "Double",
               "3B": "Triple", "HR": "Home run", "ROE": "Reached on error", "GO": "Ground-ball out",
               "AO": "Air out (fly, liner, pop)", "IBB": "Intentional walk", "SH": "Sacrifice bunt"}
SHRINK_PA = 50            # league-average plate appearances added to every batter's and pitcher's line
DEFENSE_ELASTICITY = 1.27  # hits on balls in play (odds) per unit of fielders' range per ball in play (log-log)
SHRINK_ERROR_CHANCES = 200  # league-average chances added to a fielder's errors per chance
# Fielders against runners are pulled toward the league's the same way: this many league-average chances added.
SHRINK_ARM_EXTRA_BASE = 40      # runners who could take an extra base on hits he picked up
SHRINK_ARM_TAG_UP = 30          # runners who could tag up on fly balls he caught
SHRINK_TURNING_DPS = 40         # double-play grounders he fielded
SHRINK_CATCHER_INNINGS = 150    # a catcher's steal attempts per inning
SHRINK_CATCHER_ATTEMPTS = 30    # ... and how many he threw out
SHRINK_PITCHER_BATTERS = 300    # a pitcher's steal attempts per batter faced, against his staff's
SHRINK_PITCHER_ATTEMPTS = 15    # ... and how many were caught
SHRINK_TAG_ATTEMPTS = 60        # an infielder's steal attempts covered, against his club's
STEAL_COVER = {1: ("2B", "SS"), 2: ("3B",)}  # who takes the throw and tags: second (shared), third
# Runners' figures are pulled toward the league's the same way: this many league-average chances added.
SHRINK_STEAL_CHANCES = 40
SHRINK_STEAL_ATTEMPTS = 10
SHRINK_EXTRA_BASE = 20
SHRINK_TAG_UP = 15
SHRINK_GROUND_OUTS = 40   # for the batter's double-play rate (GIDP per ground out)
PINCH_RUN_FROM_INNING = 8   # a Slow regular on base is run for from this inning ...
PINCH_RUN_MARGIN = 2        # ... when the game is within this many runs either way
# Defensive replacements: from this inning, with the fielding side ahead by DEF_LEAD runs, a bench man whose record at
# a position (``Team.defense_rating``) beats the fielder's there by DEF_EDGE, on at least DEF_MIN_INNINGS, takes it --
# one change a half-inning, the biggest gap first (range factors are noisy: a smaller edge sent in two or three)
DEF_FROM_INNING = 8
DEF_LEAD = (1, 3)
DEF_EDGE = 0.20
DEF_MIN_INNINGS = 100
PINCH_HIT_FROM_INNING = 7   # the starting pitcher is pinch-hit for from this inning (a reliever always) ...
# ... and a regular from this inning too, with his side between PH_LEAD runs (behind, ahead), when a bench hitter's
# value in this very matchup (LINEAR_WEIGHTS) beats his by PH_EDGE runs a plate appearance
PH_LEAD = (-3, 1)
PH_EDGE = 0.070  # (2015 AL parks: 0.35 such pinch hitters a team-game, as Retrosheet's; 1975 AL 0.50 real)
PH_MIN_PA = 100  # a manager doesn't send up a bench hitter on fewer real plate appearances than this
# runs a result is worth (Tango, Lichtman and Dolphin's *The Book*, roughly; outs 0), to compare two batters
LINEAR_WEIGHTS = {"BB": 0.69, "HBP": 0.72, "1B": 0.89, "2B": 1.27, "3B": 1.62, "HR": 2.10, "ROE": 0.90}
# Pitchers at bat against the league, for a season in which no pitchers batted (the universal DH): MLB's pitchers'
# rates over the league's, 2012-2019 (they hardly moved: strikeouts 1.85-1.90, home runs 0.13-0.17)
PITCHER_AT_BAT = {"K": 1.88, "BB": 0.37, "HBP": 0.24, "1B": 0.61, "2B": 0.35, "3B": 0.18, "HR": 0.15, "GO": 1.43,
                  "AO": 0.49}
DH_RULES = ("own", "dh", "pitchers")  # each team's league's rule that season; the DH for both; pitchers bat for both
PINCH_RUNNER_GROUPS = ("Speedy bench", "Speedy regulars", "Smart regulars")  # who may run for him, in this order
DEFENSE_SHRINK_INNINGS = 50  # league-average innings added to a fielder's record when comparing glove men
PLACED_RUNNER_FROM = 2020  # MLB's regular-season extra-inning runner on second
TWO_WAY_GAMES = 20         # games not pitched (batting, fielding) that make a pitcher a two-way player
SHRINK_PARK_PA = 3000      # neutral plate appearances added to a park's home games (about half a season's)
OHTANI_RULE_FROM = 2022    # a starting pitcher who is also the DH stays DH after he leaves the mound
MAX_INNINGS = 25           # a game still tied after this many innings is called a tie
# The matchups' chances a run remembers (per home team's environment), by batter, pitcher and fielders on the field.
# Rest days and substitutions make thousands of fielding alignments, so when it's this full the older half goes (what
# repeats is mostly recent).  Unbounded it grew by about 90 MB per 1,000 games (11 MB with entries stored compactly);
# at 20,000 a run stays near 200 MB at about three-quarters of the unbounded speed (2015 NYY-TOR, 12,000 games).
MATCHUP_CACHE_MAX = 20000
_ZONE_LISTS: dict = {}  # each list of zones once, shared by every entry that has it


def trim_cache(cache: dict):
    """A full cache's older half dropped (a dict keeps the order things went in)."""
    for key in list(cache)[:len(cache) // 2]:
        del cache[key]
# Fatigue: a reliever who pitched in each of his club's last two games sits out the next; so does one who faced at
# least TIRED_LONG_STINT times his usual relief batters (and TIRED_MIN_BATTERS) in the last one
TIRED_LONG_STINT = 2.0
TIRED_MIN_BATTERS = 8
# A split against one hand is pulled toward his whole season (with the league's platoon effect) as if it had this
# many more plate appearances: most of a split is luck.  Batters by the side they bat from, after Tango, Lichtman
# and Dolphin's *The Book* (2007): about 2,200 for right-handed batters and 1,000 for left-handed (switch hitters as
# right-handed); pitchers the same 1,000 either way.
SHRINK_PLATOON_PA = {"R": 2200, "L": 1000, "S": 2200}
SHRINK_PLATOON_BF = 1000
# Intentional walks and sacrifice bunts are decided by the situation (Retrosheet's rate there, ``Situations``), tilted
# by the batter's own rate per plate appearance against his league's, in odds, with these many league-average plate
# appearances added; a situation's rate is pulled toward its base-out state's with ``SHRINK_SITUATION_PA``.
SHRINK_IBB_PA = 300
SHRINK_SAC_PA = 200
SHRINK_SITUATION_PA = 300
SHRINK_HOME_PA = 20000  # each side's plate appearances at the overall rates added, for the home edge
# Pitchers at bat against their league, in odds, for a season in which none batted: MLB 2012-2019 (sacrifices 14.7
# to 21.9 times the league's; intentional walks: none in those eight seasons)
PITCHER_SAC = 17.0
PITCHER_IBB = 0.05
OUTFIELD = ("1B", "2B", "3B", "SS", "LF", "CF", "RF")  # the seven fielders behind the battery
FIELDERS = ("C",) + OUTFIELD
ZONE_POSITIONS = {"1": "P", "2": "C", "3": "1B", "4": "2B", "5": "3B", "6": "SS", "7": "LF", "8": "CF", "9": "RF"}
POSITION_ZONES = {pos: z for z, pos in ZONE_POSITIONS.items()}
FIELDED = retrosheet.SIM_FIELDED          # the balls in play: hits other than home runs, errors, outs
EVERYWHERE = "3456789"                    # every fielder's ground, where the play tables don't say where balls went


class SimError(Exception):
    pass


class Cancelled(Exception):
    """A run stopped part way because a newer one replaced it (``cancelled`` said so)."""


N = mlbstats.as_number


def _num(row: dict, key: str) -> float:
    return N(row.get(key)) or 0.0


# --------------------------------------------------------------------------- #
#  Rates                                                                      #
# --------------------------------------------------------------------------- #
def line_counts(rows, pitcher: bool = False, split: bool = True, strip: tuple | None = None) -> dict:
    """One or more MLB stat lines (batting, or pitching with ``pitcher``) added up -> counts of the
    plate appearances (``n``) and of each result.  Splits the API doesn't have are ``None``: a pitcher's
    doubles and triples before 1974, ground and air outs where they weren't recorded.  ``strip`` =
    ``(intentional walks, sacrifice bunts)``: those taken out (the games decide them by the situation)."""
    if isinstance(rows, dict):
        rows = [rows]
    n_key = "battersFaced" if pitcher else "plateAppearances"
    c = {"n": sum(_num(r, n_key) for r in rows)}
    for key, field in (("K", "strikeOuts"), ("BB", "baseOnBalls"), ("HBP", "hitByPitch"), ("H", "hits"),
                       ("HR", "homeRuns"), ("GIDP", "groundIntoDoublePlay")):
        c[key] = sum(_num(r, field) for r in rows)
    if strip:
        ibb = sum(_num(r, "intentionalWalks") for r in rows) if strip[0] else 0.0
        sh = sum(_num(r, "sacBunts") for r in rows) if strip[1] else 0.0
        c["BB"] -= ibb
        c["n"] -= ibb + sh
    for key, field in (("2B", "doubles"), ("3B", "triples"), ("go", "groundOuts"), ("ao", "airOuts")):
        values = [N(r.get(field)) for r in rows]
        c[key] = None if any(v is None for v in values) or not rows else sum(values)
    if not split or (c["go"] is not None and c["ao"] is not None and c["go"] + c["ao"] <= 0):
        c["go"] = c["ao"] = None
    return c


def class_counts(c: dict, league: dict | None = None) -> dict[str, float]:
    """:func:`line_counts` -> ``{LOG5_CLASSES: count}``.  Where a split is missing the league's own
    proportions (``league``, a rates dict) are used: non-home-run hits into singles/doubles/triples,
    outs on balls in play into ground and air outs."""
    out = {k: c[k] for k in ("K", "BB", "HBP", "HR")}
    hits = max(c["H"] - c["HR"], 0.0)
    if c.get("2B") is not None and c.get("3B") is not None:
        out["2B"], out["3B"] = c["2B"], c["3B"]
        out["1B"] = max(hits - c["2B"] - c["3B"], 0.0)
    else:
        share = league or {"1B": 0.78, "2B": 0.19, "3B": 0.03}
        total = share["1B"] + share["2B"] + share["3B"]
        for k in ("1B", "2B", "3B"):
            out[k] = hits * share[k] / total
    bip_outs = max(c["n"] - c["K"] - c["BB"] - c["HBP"] - c["H"], 0.0)
    if c.get("go") is not None and c.get("ao") is not None:
        go_share = c["go"] / (c["go"] + c["ao"])
    elif league:
        go_share = league["GO"] / (league["GO"] + league["AO"])
    else:
        go_share = 0.5
    out["GO"], out["AO"] = bip_outs * go_share, bip_outs * (1 - go_share)
    return out


def rates_from_counts(counts: dict[str, float], n: float, league: dict | None, shrink: float) -> dict[str, float]:
    """Counts of each result over ``n`` plate appearances -> shares, with ``shrink`` league-average plate
    appearances added (no league: the shares as they are)."""
    if league is None:
        total = sum(counts.values())
        return {k: counts[k] / total for k in LOG5_CLASSES} if total > 0 else None
    raw = {k: (counts[k] + shrink * league[k]) / (n + shrink) for k in LOG5_CLASSES}
    total = sum(raw.values())
    return {k: v / total for k, v in raw.items()}


def split_recorded(rows) -> bool:
    """Does MLB have ground outs and air outs for most of these batting lines?  (Its records have the split for
    a few dozen players in the early 1970s and nearly everyone from the mid-1970s on; a scattering isn't a
    league's split.)"""
    batted = [r for r in rows if _num(r, "plateAppearances") > 0]
    have = sum(1 for r in batted if _num(r, "groundOuts") + _num(r, "airOuts") > 0)
    return bool(batted) and have >= 0.5 * len(batted)


def league_rates(rows, retro_split: dict | None = None, strip: tuple | None = None) -> dict[str, float]:
    """A whole league's batting rows -> its rates of each result.  The ground/air split of outs on balls in play
    comes from MLB's lines where it has them for most players, else from Retrosheet's play-by-play of the season
    (``retro_split`` = ``{"GO": n, "AO": n}``), else half and half.  ``_split`` says which.  ``strip`` (as
    :func:`line_counts`) is kept as ``_strip``, for every player measured against this league."""
    split = split_recorded(rows)
    c = line_counts(rows, split=split, strip=strip)
    source = "MLB"
    if split and c["go"] is None:
        with_split = [r for r in rows if N(r.get("groundOuts")) is not None and N(r.get("airOuts")) is not None]
        go, ao = sum(_num(r, "groundOuts") for r in with_split), sum(_num(r, "airOuts") for r in with_split)
        c["go"], c["ao"] = (go, ao) if go + ao > 0 else (None, None)
    if not split:
        if retro_split and retro_split.get("GO", 0) + retro_split.get("AO", 0) > 0:
            c["go"], c["ao"], source = retro_split["GO"], retro_split["AO"], "Retrosheet"
        else:
            source = "none"
    rates = rates_from_counts(class_counts(c), c["n"], None, 0)
    if rates is None:
        raise SimError("no league batting figures")
    rates["_gidp_go"] = (c["GIDP"] / c["go"]) if split and c.get("go") else None
    rates["_split"] = source
    rates["_strip"] = strip
    return rates


def player_rates(rows, league: dict, pitcher: bool = False) -> dict[str, float]:
    """A player's rates; where his league's lines don't record ground and air outs (``league["_split"]`` isn't
    "MLB") his outs on balls in play are split as the league's."""
    c = line_counts(rows, pitcher, split=league.get("_split") == "MLB", strip=league.get("_strip"))
    if c["n"] <= 0:
        return {k: v for k, v in league.items() if k in LOG5_CLASSES}
    return rates_from_counts(class_counts(c, league), c["n"], league, SHRINK_PA)


def park_factors(park: dict | None) -> dict[str, float] | None:
    """A club's park from Retrosheet (``mlbstats.retro_park``: its home games' plate appearances by result, both
    teams batting, and its road games') -> each result's rate at home against on the road, with
    ``SHRINK_PARK_PA`` road-rate plate appearances added to the home games so a season's luck counts for less;
    None without the counts."""
    if not park:
        return None
    home, road = park.get("home") or {}, park.get("road") or {}
    h, r = sum(home.get(k, 0) for k in LOG5_CLASSES), sum(road.get(k, 0) for k in LOG5_CLASSES)
    if not h or not r:
        return None
    out = {}
    for k in LOG5_CLASSES:
        road_rate = road.get(k, 0) / r
        out[k] = (home.get(k, 0) + SHRINK_PARK_PA * road_rate) / (h + SHRINK_PARK_PA) / road_rate if road_rate else 1.0
    return out


def with_park(rates: dict, factors: dict | None, share: float = 1.0) -> dict:
    """``rates`` with a park's ``factors`` applied (``share`` < 0 takes a part of them out), the nine scaled back to
    their total: ``share`` 1 is a game there; -0.5 takes out the half of a player's season he played there."""
    if not factors:
        return rates
    out = dict(rates)
    scale = {k: (1 + share * (factors[k] - 1)) if share > 0 else 1 / (1 - share * (factors[k] - 1)) for k in LOG5_CLASSES}
    total = sum(rates[k] for k in LOG5_CLASSES)
    raw = {k: rates[k] * scale[k] for k in LOG5_CLASSES}
    norm = sum(raw.values())
    for k in LOG5_CLASSES:
        out[k] = raw[k] / norm * total if norm else rates[k]
    return out


def pitchers_at_bat(league: dict) -> dict:
    """A league's rates as its pitchers would bat them (``PITCHER_AT_BAT``), for a season with none at bat."""
    raw = {k: league[k] * PITCHER_AT_BAT[k] for k in LOG5_CLASSES}
    total = sum(raw.values())
    return dict(league, **{k: v / total for k, v in raw.items()})


HANDS = ("L", "R")


def platoon_ratios(cells: dict | None) -> dict | None:
    """Retrosheet's plate appearances by the batter's side and the pitcher's hand (``retrosheet.parse_sim``'s
    ``platoon``) -> ``{"ratio": {(side, hand): {result: rate in that matchup / rate in all}}, "n": {(side, hand):
    plate appearances}}``, or None without all four.  MLB's lines count reaching on an error as an out, so here it
    is a ground-ball out."""
    if not cells or any(not cells.get(s + h) for s in HANDS for h in HANDS):
        return None
    counts = {}
    for s in HANDS:
        for h in HANDS:
            c = cells[s + h]
            counts[(s, h)] = {k: c.get(k, 0) + (c.get("ROE", 0) if k == "GO" else 0) for k in LOG5_CLASSES}
    n = {key: sum(c.values()) for key, c in counts.items()}
    total = {k: sum(c[k] for c in counts.values()) for k in LOG5_CLASSES}
    n_all = sum(n.values())
    ratio = {key: {k: (c[k] / n[key]) / (total[k] / n_all) if total[k] and n[key] else 1.0 for k in LOG5_CLASSES}
             for key, c in counts.items()}
    return {"ratio": ratio, "n": n}


def home_edge(counts: dict | None) -> dict | None:
    """Retrosheet's plate appearances by the home and visiting batters in the same games (``parse_sim``'s
    ``homefield``) -> ``{True: {result: home batters' rate / all}, False: the visitors'}``, each side pulled toward
    the overall with ``SHRINK_HOME_PA``; None without both.  Reaching on an error counts as a ground-ball out (as in
    MLB's lines).  The parks' factors compare home games with road games, both teams batting, so this is beyond them:
    the edge of batting at home."""
    if not counts or not counts.get("1") or not counts.get("0"):
        return None
    sides = {}
    for key in ("1", "0"):
        c = counts[key]
        sides[key] = {k: c.get(k, 0) + (c.get("ROE", 0) if k == "GO" else 0) for k in LOG5_CLASSES}
    total = {k: sides["1"][k] + sides["0"][k] for k in LOG5_CLASSES}
    n_all = sum(total.values())
    out = {}
    for key, home in (("1", True), ("0", False)):
        n = sum(sides[key].values())
        out[home] = {k: ((sides[key][k] + SHRINK_HOME_PA * total[k] / n_all) / (n + SHRINK_HOME_PA))
                     / (total[k] / n_all) if total[k] else 1.0 for k in LOG5_CLASSES}
    return out


def matchup_hands(bats: str, throws: str) -> tuple[str, str] | None:
    """The side a batter bats from and the hand the pitcher throws with -> ``(side, hand)``, or None if either is
    unknown.  A switch hitter bats from the side opposite the pitcher's hand; a switch pitcher (Pat Venditte) throws
    with the batter's hand, and against a switch hitter (who chooses after him) right-handed."""
    if bats not in ("L", "R", "S") or throws not in ("L", "R", "S"):
        return None
    hand = throws if throws != "S" else ("R" if bats == "S" else bats)
    side = bats if bats != "S" else ("R" if hand == "L" else "L")
    return side, hand


def platoon_rates(rates: dict, splits: dict | None, own: str, platoon: dict, league: dict, pitcher: bool = False,
                  park: dict | None = None) -> dict | None:
    """A player's rates against each hand on the other side, with the league's platoon effect taken out ->
    ``{"L": rates, "R": rates}``: a batter's against left- and right-handed pitchers, a pitcher's against batters
    batting from the left and from the right.  None if his hand (``own``: a batter's bats, a pitcher's throws) is
    unknown.

    ``rates`` is his whole season (already pulled toward his league and with his park's half taken out), so it has
    each matchup in it as often as he had them: divided by those matchups' ratios (``platoon_ratios``; weighed by
    his own split plate appearances where the API has them, else by the league's) it is his rate against an average
    mix of hands.  ``splits`` (``mlbstats.parse_platoon_splits``'s, for him) moves each side from there, by its
    plate appearances against ``SHRINK_PLATOON_PA`` (batters) or ``SHRINK_PLATOON_BF`` (pitchers) of the whole
    season's -- most of a split is luck.  In a game the matchup's ratio is put back once, in the environment."""
    def cell(other: str):  # (side, hand) when he faces ``other``
        return matchup_hands(other, own) if pitcher else matchup_hands(own, other)
    if cell("L") is None:
        return None
    n = platoon["n"]
    if pitcher:  # how often a pitcher of his hand faced batters from each side
        hand = own if own != "S" else None
        mix = {o: (n[(o, hand)] if hand else n[(o, "L")] + n[(o, "R")]) for o in HANDS}
    else:  # how often a batter of his hand faced pitchers of each hand
        mix = {o: (n[(own, o)] if own != "S" else n[("L", o)] + n[("R", o)]) for o in HANDS}
    split_rows = {o: (splits or {}).get(o) for o in HANDS}
    split_n = {o: _num(split_rows[o], "battersFaced" if pitcher else "plateAppearances") if split_rows[o] else 0.0
               for o in HANDS}
    weights = split_n if sum(split_n.values()) > 0 else mix
    total = sum(weights.values()) or 1.0
    mean = {k: sum(weights[o] / total * platoon["ratio"][cell(o)][k] for o in HANDS) for k in LOG5_CLASSES}
    prior = {k: rates[k] / mean[k] if mean[k] else rates[k] for k in LOG5_CLASSES}
    if pitcher:
        shrink = SHRINK_PLATOON_BF
    else:
        shrink = SHRINK_PLATOON_PA.get(own, SHRINK_PLATOON_PA["S"])
    split = league.get("_split") == "MLB"
    out = {}
    for o in HANDS:
        ratio = platoon["ratio"][cell(o)]
        c = line_counts(split_rows[o], pitcher, split=split, strip=league.get("_strip")) if split_rows[o] else None
        if not c or c["n"] <= 0:
            out[o] = dict(prior)
            continue
        seen = class_counts(c, league)
        seen = {k: v / c["n"] for k, v in seen.items()}
        if park:
            seen = with_park(seen, park, -0.5)
        out[o] = {k: (c["n"] * seen[k] / ratio[k] + shrink * prior[k]) / (c["n"] + shrink) if ratio[k] else prior[k]
                  for k in LOG5_CLASSES}
    return out


def log5(batter: dict, b_league: dict, pitcher: dict, p_league: dict, env: dict) -> dict[str, float]:
    """The odds-ratio combination of a batter's and a pitcher's rates (each against his own league) in
    the environment's league -> shares of the nine results, adding up to 1."""
    raw = {}
    for k in LOG5_CLASSES:
        bl, pl = b_league[k], p_league[k]
        raw[k] = (batter[k] / bl if bl > 0 else 1.0) * (pitcher[k] / pl if pl > 0 else 1.0) * env[k]
    total = sum(raw.values())
    return {k: v / total for k, v in raw.items()}


def platoon_matchup(bats: str, b_rates: dict, b_platoon: dict | None, pitcher: "Pitcher", batting: "Team",
                    fielding: "Team", env: "Environment") -> tuple[dict, dict, dict, tuple | None]:
    """What a plate appearance is played from -> ``(batter's rates, pitcher's rates, environment's rates, (side,
    hand) or None)``.  With the platoon figures for both teams and the game, and both hands known: the batter's
    rates against the pitcher's hand, the pitcher's against the batter's side, and the environment's for that
    matchup (:meth:`Environment.cell_rates`) -- the platoon effect counted once.  Otherwise the season lines and
    the environment's rates, as without platoon figures."""
    hands = matchup_hands(bats, pitcher.throws) if (env.platoon and batting.platoon and fielding.platoon
                                                   and b_platoon and pitcher.platoon) else None
    if hands is None:
        return b_rates, pitcher.rates, env.rates, None
    side, hand = hands
    return b_platoon[hand], pitcher.platoon[side], env.cell_rates(side, hand), hands


def matchup_value(chances: dict) -> float:
    """A plate appearance's chances -> its value in runs (``LINEAR_WEIGHTS``), to compare batters with."""
    return sum(chances.get(k, 0.0) * w for k, w in LINEAR_WEIGHTS.items())


def apply_fielding(p: dict, tables: "Tables", zone_factors: dict) -> tuple[dict, dict]:
    """The fielders' part.  The balls in play (``p``'s singles, doubles, triples and outs) go where the
    environment season's went (``tables.zone_share``: each kind of ball by zone, e.g. ``"6"``, ``"56"``);
    in each zone ``zone_factors[zone] = (hits, errors)`` -- from the fielders whose ground that is -- scale
    the odds of a hit, and the zone's league share of outs that became errors.  Zones not in
    ``zone_factors`` are left as the league's.  -> ``(the ten results' chances, {class: [(zone, chance),
    ...]})``: what comes up, and where each kind of ball in play went."""
    out = {k: p.get(k, 0.0) for k in ("K", "BB", "HBP", "HR")}
    for k in FIELDED:
        out[k] = 0.0
    where = {k: [] for k in FIELDED}
    for zone in tables.zones:
        part = {k: p.get(k, 0.0) * tables.zone_share[k].get(zone, 0.0) for k in ("1B", "2B", "3B", "GO", "AO")}
        hit_f, err_f = zone_factors.get(zone, (1.0, 1.0))
        roe = min(tables.zone_roe.get(zone, tables.roe_share) * err_f, 0.5)
        part["ROE"] = (part["GO"] + part["AO"]) * roe
        part["GO"] *= 1 - roe
        part["AO"] *= 1 - roe
        hits = part["1B"] + part["2B"] + part["3B"]
        outs = part["GO"] + part["AO"] + part["ROE"]
        if hits > 0 and outs > 0 and hit_f != 1.0:
            bip = hits + outs
            odds = hits / outs * hit_f
            new_hits = bip * odds / (1 + odds)
            for k in ("1B", "2B", "3B"):
                part[k] *= new_hits / hits
            for k in ("GO", "AO", "ROE"):
                part[k] *= (bip - new_hits) / outs
        for k in FIELDED:
            if part[k] > 0:
                out[k] += part[k]
                where[k].append((zone, part[k]))
    return out, where


def zone_factors(fielders: dict, zones) -> dict:
    """Each zone's ``(hits, errors)`` factors from the fielders whose ground it is: ``fielders`` is
    ``{position: (range against what the pitchers' lines had there, errors against MLB's)}``.  A zone two
    or three fielders share takes each one's part equally (the geometric mean); range is
    ``range ^ -DEFENSE_ELASTICITY`` in the odds of a hit.  The pitcher's and catcher's range isn't
    measured (1)."""
    out = {}
    for zone in zones:
        hit = err = 1.0
        for d in zone:
            rng, e = fielders.get(ZONE_POSITIONS[d], (1.0, 1.0))
            hit *= rng ** (-DEFENSE_ELASTICITY / len(zone))
            err *= e ** (1 / len(zone))
        out[zone] = (hit, err)
    return out


def odds_factor(count: float, chances: float, league_rate: float | None, shrink: float) -> float:
    """How a player's rate compares with the league's, in odds, after ``shrink`` league-average chances
    are added to his: 1 = average."""
    if not league_rate or league_rate <= 0 or league_rate >= 1 or chances is None:
        return 1.0
    rate = (count + shrink * league_rate) / (chances + shrink)
    rate = min(max(rate, 1e-6), 1 - 1e-6)
    return (rate / (1 - rate)) / (league_rate / (1 - league_rate))


def tilt(p: float, factor: float) -> float:
    """A probability with its odds multiplied by ``factor``."""
    if factor == 1.0 or p <= 0 or p >= 1:
        return p
    o = p / (1 - p) * factor
    return o / (1 + o)


# --------------------------------------------------------------------------- #
#  Baserunning                                                                #
# --------------------------------------------------------------------------- #
XB_KEYS = ("s13", "s1h", "s2h", "d1h")
TU_KEYS = ("f12", "f13", "f1h", "f23", "f2h", "f3h")


def runner_totals(rows) -> dict:
    keys = ("sb", "cs", "opp_sb", "opp_xb", "opp_tu") + XB_KEYS + TU_KEYS
    return {k: sum(N(r.get(k)) or 0 for r in rows) for k in keys}


def league_running(rows) -> dict:
    t = runner_totals(rows)
    att = t["sb"] + t["cs"]
    return {"att": att / t["opp_sb"] if t["opp_sb"] else None,
            "succ": t["sb"] / att if att else None,
            "xb": sum(t[k] for k in XB_KEYS) / t["opp_xb"] if t["opp_xb"] else None,
            "tu": sum(t[k] for k in TU_KEYS) / t["opp_tu"] if t["opp_tu"] else None}


def runner_factors(rows, league: dict) -> dict:
    """A runner's Retrosheet counts against his league's -> odds factors for trying to steal, stealing
    safely, taking extra bases on hits and tagging up (1 = average)."""
    t = runner_totals(rows)
    att = t["sb"] + t["cs"]
    return {"att": odds_factor(att, t["opp_sb"], league.get("att"), SHRINK_STEAL_CHANCES),
            "succ": odds_factor(t["sb"], att, league.get("succ"), SHRINK_STEAL_ATTEMPTS),
            "xb": odds_factor(sum(t[k] for k in XB_KEYS), t["opp_xb"], league.get("xb"), SHRINK_EXTRA_BASE),
            "tu": odds_factor(sum(t[k] for k in TU_KEYS), t["opp_tu"], league.get("tu"), SHRINK_TAG_UP),
            "counts": t}


AVERAGE_RUNNER = {"att": 1.0, "succ": 1.0, "xb": 1.0, "tu": 1.0, "counts": None}
_DEST_BASE = {"1": 1, "2": 2, "3": 3, "H": 4}


class Tables:
    """The environment season's play tables (:func:`retrosheet.parse_sim`) ready to draw from -- or, with
    none, plain textbook advances (every runner moves up as many bases as the batter, walks force)."""

    def __init__(self, summary: dict | None):
        self.summary = summary
        self.trans: dict = {}
        self.adv: dict = {}
        self.adv_rate: dict = {}
        self.steal = {}
        self.steal_chances: dict = {}  # how often each base was a steal chance, to weigh the bases together
        # where balls in play went: each kind's zones (shares adding up to 1), and each zone's outs that were errors
        self.zone_share = {k: {EVERYWHERE: 1.0} for k in ("1B", "2B", "3B", "GO", "AO")}
        self.roe_share = 0.02
        self.zone_roe: dict = {}
        self.zones = (EVERYWHERE,)
        self.pick: dict = {}  # {(class, zone): (positions picking the ball up, cumulative shares)}
        self.situations = Situations.from_summary(summary)  # intentional walks and sacrifices (None: not decided)
        self.home_edge = home_edge((summary or {}).get("homefield"))  # batting at home (None: no edge)
        if summary is None:
            return
        counts = {c: sum(sum(v.values()) for v in summary["trans"].get(c, {}).values()) for c in ("ROE", "GO", "AO")}
        total = counts["ROE"] + counts["GO"] + counts["AO"]
        self.roe_share = counts["ROE"] / total if total and counts["ROE"] else 0.02
        field = summary.get("field") or {}
        if all(field.get(k) for k in ("1B", "GO", "AO")):
            for k in self.zone_share:
                n = sum((field.get(k) or {}).values())
                if n:
                    self.zone_share[k] = {z: c / n for z, c in field[k].items()}
            zones = set().union(*(set(v) for v in self.zone_share.values()))
            for z in zones:
                outs = sum((field.get(k) or {}).get(z, 0) for k in ("GO", "AO", "ROE"))
                if outs:
                    self.zone_roe[z] = (field.get("ROE") or {}).get(z, 0) / outs
            self.zones = tuple(sorted(zones))
        for k, by_zone in (summary.get("pick") or {}).items():
            for z, who in by_zone.items():
                n = sum(who.values())
                if not n:
                    continue
                firsts, cum, acc = [], [], 0.0
                for first, c in sorted(who.items()):
                    acc += c / n
                    firsts.append(first)
                    cum.append(acc)
                self.pick[(k, z)] = (firsts, cum)

        for cls, cells in summary["trans"].items():
            for cell, outcomes in cells.items():
                state, outs = (int(x) for x in cell.split("."))
                self.trans[(cls, state, outs)] = [self._entry(cls, o, n) for o, n in
                                                  sorted(outcomes.items(), key=lambda kv: -kv[1])]
        for cell, outcomes in summary["adv"].items():
            state, outs = (int(x) for x in cell.split("."))
            entries = [self._entry("ADV", o, n) for o, n in sorted(outcomes.items(), key=lambda kv: -kv[1])]
            self.adv[(state, outs)] = entries
            pa = summary["pa"].get(cell, 0)
            if pa:
                self.adv_rate[(state, outs)] = sum(e[1] for e in entries) / pa
        for base, (chances, attempts, stolen) in summary["steal"].items():
            self.steal[int(base)] = (attempts / chances if chances else 0.0, stolen / attempts if attempts else 0.7)
            self.steal_chances[int(base)] = chances

    def steal_rates(self, att: float = 1.0, safe: float = 1.0, by_base: dict | None = None) -> tuple[float, float] | None:
        """Steals at any base, as the games play them: tries per chance and safe per try, with odds factors
        ``att`` and ``safe`` applied to each base's league rate and the bases weighed by how often each was a
        chance (and tries by how often each was tried) -> ``(tries, safe)``; ``None`` without the tables."""
        chances = {b: self.steal_chances.get(b, 0) for b in self.steal}
        total = sum(chances.values())
        if not total:
            return None
        tries = {b: chances[b] * tilt(self.steal[b][0], att) for b in self.steal}
        tried = sum(tries.values())
        by_base = by_base or {}  # (a base's own factor too: the infielders covering it)
        safe_rate = sum(tries[b] * tilt(self.steal[b][1], safe * by_base.get(b, 1.0)) for b in self.steal) / tried \
            if tried else 0.0
        return tried / total, safe_rate

    def picked_up(self, cls: str, zone: str, u: float) -> str:
        """Which position (1-9) picked up a ball of kind ``cls`` in ``zone``, chosen by ``u`` (0-1) in proportion
        to the season's; "" if not known."""
        got = self.pick.get((cls, zone))
        if not got:
            return zone if len(zone) == 1 else ""
        firsts, cum = got
        return firsts[min(bisect.bisect_right(cum, u * cum[-1]), len(firsts) - 1)]

    @staticmethod
    def _entry(cls: str, outcome: str, count: int):
        """(outcome, count, extra bases per runner on 1st/2nd/3rd, outs made, runs scored)"""
        extra = []
        for b in (1, 2, 3):
            d = outcome[b]
            if d in "-X":
                extra.append(0)
                continue
            standard = {"1B": b + 1, "2B": b + 2}.get(cls, b)
            extra.append(max(0, _DEST_BASE[d] - min(standard, 4)))
        return (outcome, count, tuple(extra), outcome.count("X"), outcome.count("H"))

    def draw(self, cls: str, state: int, outs: int, factors, gdp: float, rng: random.Random):
        """An outcome for a plate appearance of kind ``cls`` -> ``(outcome, weight share, count, cell total)``.
        ``factors`` are the runners' tilts on 1st/2nd/3rd for this kind of play."""
        entries = self.trans.get((cls, state, outs))
        if not entries:
            return default_outcome(cls, state), 1.0, 0, 0
        weights = []
        for outcome, count, extra, made, _runs in entries:
            w = count
            for f, x in zip(factors, extra):
                if x and f != 1.0:
                    w *= f ** x
            if gdp != 1.0 and cls == "GO" and made >= 2:
                w *= gdp
            weights.append(w)
        total = sum(weights)
        pick = rng.random() * total
        for (outcome, count, *_rest), w in zip(entries, weights):
            pick -= w
            if pick < 0:
                return outcome, w / total, count, sum(e[1] for e in entries)
        last = entries[-1]
        return last[0], weights[-1] / total, last[1], sum(e[1] for e in entries)


class Situations:
    """Intentional walks and sacrifice bunts by the situation (``retrosheet.parse_sim``'s ``sit`` and ``sac``):
    how often each happened in a base-out state, stage of the game and margin (:func:`retrosheet.sim_situation`),
    pulled toward the base-out state's rate with ``SHRINK_SITUATION_PA`` plate appearances; and what became of the
    runners on a sacrifice."""

    def __init__(self, sit: dict, sac: dict | None):
        by_state: dict = {}
        for key, (n, ibb, sh) in sit.items():
            got = by_state.setdefault(key.rsplit(".", 2)[0], [0, 0, 0])
            for i, x in enumerate((n, ibb, sh)):
                got[i] += x
        total = [sum(v[i] for v in sit.values()) for i in range(3)]
        self.league = (total[1] / total[0], total[2] / total[0]) if total[0] else (0.0, 0.0)  # (IBB, SH) per PA
        self.rates: dict = {}
        for key, (n, ibb, sh) in sit.items():
            pn, pi, ps = by_state[key.rsplit(".", 2)[0]]
            k = SHRINK_SITUATION_PA
            self.rates[key] = ((ibb + k * pi / pn) / (n + k), (sh + k * ps / pn) / (n + k)) if pn else (0.0, 0.0)
        self.state_rates = {key: (i / n, sh / n) for key, (n, i, sh) in by_state.items() if n}
        self.sac: dict = {}
        for cell, outcomes in (sac or {}).items():
            state, outs = (int(x) for x in cell.split("."))
            self.sac[(state, outs)] = [Tables._entry("SH", o, n) for o, n in
                                       sorted(outcomes.items(), key=lambda kv: -kv[1])]

    @classmethod
    def from_summary(cls, summary: dict | None) -> "Situations | None":
        return cls(summary["sit"], summary.get("sac")) if summary and summary.get("sit") else None

    def chances(self, state: int, outs: int, inning: int, margin: int) -> tuple[float, float]:
        """(intentional walk, sacrifice bunt) per plate appearance in this situation, the league's; a situation
        never seen is its base-out state's."""
        key = retrosheet.sim_situation(state, outs, inning, margin)
        got = self.rates.get(key)
        return got if got is not None else self.state_rates.get(key.rsplit(".", 2)[0], (0.0, 0.0))

    def draw_sac(self, state: int, outs: int, rng: random.Random):
        """What became of the runners on a sacrifice in this base-out state, drawn as often as it happened ->
        ``(outcome, share, count, total)``, or None if there was none."""
        entries = self.sac.get((state, outs))
        if not entries:
            return None
        total = sum(e[1] for e in entries)
        pick = rng.random() * total
        for outcome, count, *_ in entries:
            pick -= count
            if pick < 0:
                return outcome, count / total, count, total
        return entries[-1][0], entries[-1][1] / total, entries[-1][1], total


def sit_factor(count: float, n: float, league_rate: float, shrink: float) -> float:
    """A batter's intentional walks (or sacrifices) per plate appearance against the league's, in odds."""
    return odds_factor(count, n, league_rate, shrink) if league_rate and 0 < league_rate < 1 else 1.0


def default_outcome(cls: str, state: int) -> str:
    """Textbook advances, for a base-out state the play tables never saw (or no tables at all)."""
    occupied = [bool(state & 1), bool(state & 2), bool(state & 4)]
    if cls in ("BB", "HBP", "ROE"):
        dests = []
        forced = True
        for b in (1, 2, 3):
            if not occupied[b - 1]:
                dests.append("-")
                forced = False
                continue
            if forced:
                dests.append("H" if b == 3 else str(b + 1))
            else:
                dests.append(str(b))
        return "1" + "".join(dests)
    move = {"1B": 1, "2B": 2, "3B": 3, "HR": 4}.get(cls)
    if move:
        batter = "H" if move == 4 else str(move)
        return batter + "".join((("H" if b + move >= 4 else str(b + move)) if occupied[b - 1] else "-")
                                for b in (1, 2, 3))
    return "X" + "".join(str(b) if occupied[b - 1] else "-" for b in (1, 2, 3))


# --------------------------------------------------------------------------- #
#  Building a side                                                            #
# --------------------------------------------------------------------------- #
def fetch_team(team_id: int, season: int, ttl: float | None = None) -> dict:
    """Everything the Simulator needs about one team-season, straight from the sources (MLB's API and
    Retrosheet's summaries, both cached).  Retrosheet's parts are ``None`` where it has no play-by-play
    (the current season, the 19th century) -- with ``notes`` saying so."""
    raw = {"team_id": team_id, "season": season, "notes": []}
    raw["hitting"] = [r for r in mlbstats.get_player_stats(team_id, season, "hitting", ttl) if r.get("_id") is not None]
    raw["pitching"] = [r for r in mlbstats.get_player_stats(team_id, season, "pitching", ttl) if r.get("_id") is not None]
    raw["fielding"] = [r for r in mlbstats.get_player_stats(team_id, season, "fielding", ttl) if r.get("_id") is not None]
    if not raw["hitting"] or not raw["pitching"]:
        raise SimError(f"MLB has no batting or pitching lines for that team in {season}")
    try:
        raw["order"] = mlbstats.get_batting_order(team_id, season, ttl)
    except mlbstats.MLBStatsError:
        raw["order"] = {}
    raw["league_positions"] = mlbstats.get_league_position_totals(season, ttl)
    try:
        raw["abbrev"] = (mlbstats.get_team_map(season, ttl).get(team_id) or {}).get("abbrev")
    except mlbstats.MLBStatsError:
        raw["abbrev"] = None
    raw["league_hitting"] = mlbstats.get_league_view_rows(season, "Batting", ttl)
    raw["retro_split"] = None
    if not split_recorded(raw["league_hitting"]):  # MLB lacks ground/air outs: the season's play-by-play has them
        try:
            tables = retrosheet.load_sim_season(season, mlbstats.CACHE_DIR)
            raw["retro_split"] = {c: sum(sum(v.values()) for v in tables["trans"][c].values()) for c in ("GO", "AO")}
        except retrosheet.RetrosheetError:
            raw["notes"].append(f"{season}: neither MLB nor Retrosheet splits outs into ground and air outs -- "
                                "half and half")
    try:
        raw["runners"] = mlbstats.get_player_stats(team_id, season, "retrosheet", ttl)
        raw["league_runners"] = mlbstats.league_view_rows(season, mlbstats.RETRO_VIEW, ttl)
    except mlbstats.MLBStatsError as exc:
        raw["runners"] = raw["league_runners"] = None
        raw["notes"].append(f"{season}: no Retrosheet running records ({exc}) -- every runner is league-average")
    try:
        raw["tags"], raw["tags_club"] = mlbstats.retro_team_tags(team_id, season, ttl)
    except mlbstats.MLBStatsError:
        raw["tags"] = raw["tags_club"] = None
    try:
        raw["park"] = mlbstats.retro_park(team_id, season, ttl)
    except mlbstats.MLBStatsError as exc:
        raw["park"] = None
        raw["notes"].append(f"{season}: no Retrosheet home and road games for the park ({exc}) -- a neutral park")
    try:  # the park's name and distances, for its card
        raw["venue"] = next((p for p in stadiums.get_home_parks(season, ttl) if p["team_id"] == team_id), None)
    except mlbstats.MLBStatsError:
        raw["venue"] = None
    try:
        raw["arms"], raw["arms_league"] = mlbstats.retro_team_fielders(team_id, season, ttl)
    except mlbstats.MLBStatsError as exc:
        raw["arms"] = raw["arms_league"] = None
        raw["notes"].append(f"{season}: no Retrosheet fielding against runners ({exc}) -- every arm and double-play "
                            "turn is league-average")
    try:
        raw["relief"] = mlbstats.get_player_stats(team_id, season, "retrosheet-relief", ttl)
    except mlbstats.MLBStatsError:
        raw["relief"] = None
    # platoon: who bats and throws with which hand, each player's splits (MLB, from 1950), and the league's
    # plate appearances by hand against hand (Retrosheet: this season, or the one before while it isn't published)
    try:
        raw["hands"] = mlbstats.get_hands([r["_id"] for r in raw["hitting"] + raw["pitching"]])
    except mlbstats.MLBStatsError:
        raw["hands"] = {}
    try:
        raw["platoon"] = {g: mlbstats.get_platoon_splits(team_id, season, g, ttl) for g in ("hitting", "pitching")}
    except mlbstats.MLBStatsError:
        raw["platoon"] = None
    raw["platoon_cells"] = raw["platoon_season"] = raw["situations"] = None
    for s in (season, season - 1):
        try:
            summary = retrosheet.load_sim_season(s, mlbstats.CACHE_DIR)
        except retrosheet.RetrosheetError:
            continue
        cells = summary.get("platoon")
        if summary.get("sit") and not raw["situations"]:  # intentional walks and sacrifices by the situation
            raw["situations"] = {"sit": summary["sit"], "sac": summary.get("sac"), "season": s}
        if cells:
            raw["platoon_cells"], raw["platoon_season"] = cells, s
            break
    if not raw["platoon_cells"]:
        raw["notes"].append(f"{season}: Retrosheet has no batters' and pitchers' hands -- no platoon (left/right) "
                            "effect")
    elif not raw["hands"]:
        raw["notes"].append(f"{season}: MLB didn't say who bats and throws with which hand -- no platoon effect")
    elif season >= mlbstats.PLATOON_FROM and not raw["platoon"]:
        raw["notes"].append(f"{season}: no left/right splits from MLB -- the league's platoon effect only")
    return raw


def load_tables(season: int, last_complete: int) -> tuple[dict | None, int | None, str | None]:
    """The play tables for ``season``, or the nearest season Retrosheet has (the one before, then after) ->
    ``(summary, season used, note)``.  A season after ``last_complete`` (Retrosheet hasn't published it) is used
    when the Uptodate tab has made its file."""
    tried = []
    for s in (season, season - 1, season + 1, season - 2):
        if (s > last_complete and not retrosheet.uptodate_file(s)) or s < 1901 or s in tried:
            continue
        tried.append(s)
        try:
            summary = retrosheet.load_sim_season(s, mlbstats.CACHE_DIR)
        except retrosheet.RetrosheetError:
            continue
        if s == season:
            note = (f"{season}: play-by-play from the Uptodate tab's file (MLB's games so far), not yet Retrosheet's"
                    if summary.get("uptodate") else None)
        else:
            note = f"Retrosheet has no {season} play-by-play: the runners move as they did in {s}"
        return summary, s, note
    return None, None, (f"Retrosheet has no play-by-play near {season}: runners advance by textbook rules "
                        "(as many bases as the batter, walks force)")


def _main_league(rows) -> str:
    counts = {}
    for r in rows:
        if r.get("_league"):
            counts[r["_league"]] = counts.get(r["_league"], 0) + 1
    return max(counts, key=counts.get) if counts else ""


def _workloads(pitching: list[dict], relief_rows) -> dict:
    """``{pitcher id: (batters faced per start, batters faced per relief appearance)}``.  Outs per start and
    per relief appearance come from Retrosheet where it has the season (exact); otherwise a pitcher who only
    started or only relieved is exact, and one who did both is assumed to have relieved for as long as the
    team's pure relievers did on average."""
    relief = {r["_id"]: r for r in relief_rows or [] if isinstance(r.get("_id"), int)}
    pure = [r for r in pitching if _num(r, "gamesStarted") == 0 and _num(r, "gamesPitched") > 0]
    pure_g = sum(_num(r, "gamesPitched") for r in pure)
    relief_bf = sum(_num(r, "battersFaced") for r in pure) / pure_g if pure_g else 4.3
    out = {}
    for r in pitching:
        bf, g, gs = _num(r, "battersFaced"), _num(r, "gamesPitched"), _num(r, "gamesStarted")
        outs = mlbstats.pitcher_workload(r)[2]
        per_out = bf / outs if outs else 4.3 / 3
        rel = relief.get(r["_id"])
        start = rel_app = None
        if rel is not None:
            if rel.get("gs"):
                start = rel["st_outs"] / rel["gs"] * per_out
            if rel.get("rel"):
                rel_app = rel["rel_outs"] / rel["rel"] * per_out
        relief_games = max(g - gs, 0)
        if start is None and gs > 0:
            start = bf / gs if relief_games == 0 else max((bf - relief_games * relief_bf) / gs, relief_bf)
        if rel_app is None and relief_games > 0:
            rel_app = bf / g if gs == 0 else relief_bf
        out[r["_id"]] = (max(start or 0.0, 3.0) if gs > 0 else None, max(rel_app or relief_bf, 1.0))
    return out


def speed_groups(runners, league_runners, bg_formula: str | None, opp_formula: str | None) -> dict:
    """The Speed tab's groups (Speedy / Smart / Slow regulars, Speedy bench, Other) for a team's runners,
    worked out the way that tab does -> ``{player id: {"group", "rate"}}`` (rate = bases gained per opportunity);
    empty without the Bases Gained stat."""
    if not runners or not league_runners or not bg_formula:
        return {}
    view = mlbstats.RETRO_VIEW

    def opportunities(groups, pids, rows):
        if opp_formula:
            return mlbstats.pooled_stat(opp_formula, groups, pids) or 0
        return sum(retrosheet.opportunities(r) for r in rows)

    groups = {view: mlbstats.group_by_player(runners)}
    league_groups = {view: {"league": league_runners}}
    league_bg = mlbstats.pooled_stat(bg_formula, league_groups, ["league"])
    league_opps = opportunities(league_groups, ["league"], league_runners)
    if league_bg is None or not league_opps:
        return {}
    each = {view: {i: [row] for i, row in enumerate(league_runners)}}
    per_runner = [n for n in (opportunities(each, [i], [row]) for i, row in enumerate(league_runners)) if n >= 1]
    average = sum(per_runner) / len(per_runner) if per_runner else None
    listed = []
    for pid, rows in groups[view].items():
        opps = opportunities(groups, [pid], rows)
        if opps >= 1:
            listed.append({"id": pid, "name": rows[0]["_name"], "opps": opps,
                           "bg": mlbstats.pooled_stat(bg_formula, groups, [pid])})
    classified = mlbstats.classify_runners(listed, league_bg / league_opps, average)
    return {r["id"]: {"group": label, "rate": (r["bg"] / r["opps"]) if r.get("bg") is not None and r["opps"] else None}
            for label, members in classified["groups"].items() for r in members}


def uses_dh(league_name: str, season: int) -> bool:
    """Did this league bat a designated hitter that season?  The AL from 1973, the NL in 2020 and from 2022."""
    if league_name in ("AL", "American League"):
        return season >= 1973
    if league_name in ("NL", "National League"):
        return season == 2020 or season >= 2022
    return False


def _bip_per_out(rows) -> float | None:
    """Balls put in play per out made, from batting or pitching lines (the same fields in both)."""
    bip = sum(_num(r, "atBats") - _num(r, "strikeOuts") - _num(r, "homeRuns") + _num(r, "sacFlies")
              + _num(r, "sacBunts") for r in rows)
    outs = sum(_num(r, "atBats") - _num(r, "hits") + _num(r, "sacFlies") + _num(r, "sacBunts")
               + _num(r, "groundIntoDoublePlay") + _num(r, "caughtStealing") for r in rows)
    return bip / outs if bip > 0 and outs > 0 else None


def _ordinal(n: int) -> str:
    words = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh",
             8: "eighth", 9: "ninth"}
    if n in words:
        return words[n]
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


class Batter:
    def __init__(self, pid, name, rates, league, running, gdp, row, group=""):
        self.pid, self.name, self.rates, self.league = pid, name, rates, league
        self.running, self.gdp, self.row, self.group = running, gdp, row, group
        self.speed_rate = None  # bases gained per opportunity (Speed tab), where known
        self.ibb = self.sac = None  # intentional walks and sacrifices per PA against the league's, in odds (None: not
                                    # decided by the situation)
        self.bats = ""          # "L", "R", "S" (switch), "" unknown
        self.platoon = None     # his rates against left- and right-handed pitchers (:func:`platoon_rates`)


class Pitcher:
    def __init__(self, pid, name, role, rates, league, bf_start, bf_relief, row, bat_rates, gf, relief_work):
        self.pid, self.name, self.role, self.rates, self.league = pid, name, role, rates, league
        self.bf_start, self.bf_relief, self.row, self.bat_rates = bf_start, bf_relief, row, bat_rates
        self.gf, self.relief_work = gf, relief_work
        self.bats = self.throws = ""
        self.platoon = None      # his rates against batters from the left and from the right (:func:`platoon_rates`)
        self.bat_platoon = None  # his batting (in the pitcher's spot) against each hand: the league's effect only


class Team:
    """One side, set up by the other tabs' defaults (see the module docstring)."""

    def __init__(self, raw: dict, name: str, speed: dict | None = None, overrides: dict | None = None,
                 dh_rule: str = "own", fatigue: bool = True):
        """``overrides`` replaces the defaults where given: ``{"order": [player id or "P" (the pitcher's spot)
        for places 1-9], "defense": {position: player id}, "rotation": [pitcher id, ...] (they start in strict
        turn, in that order; one pitcher = every game)}``.  ``dh_rule`` (``DH_RULES``): the team's own league's
        rule that season, or the DH, or pitchers batting, whatever its league did.  ``fatigue``: regulars rest as
        often as they really did (:meth:`day_lineup`) and tired relievers sit out (:func:`tired_relievers`); without
        it everyone plays every game fresh."""
        self.name, self.season, self.team_id = name, raw["season"], raw["team_id"]
        self.fatigue = fatigue
        # the path analysis's switches (each turns one of the team's decisions off: ``MANAGING_CATEGORIES``)
        self.off: frozenset = frozenset()
        self.raw = raw
        self.overrides = overrides or {}
        self.short = f"{raw.get('abbrev') or name.split()[0]} {raw['season']}"
        self.notes = list(raw["notes"])
        self.league_name = _main_league(raw["hitting"])
        own = [r for r in raw["league_hitting"] if r.get("_league") == self.league_name] or raw["league_hitting"]
        # intentional walks and sacrifices: decided in the games by the situation (Retrosheet's, this season or the
        # one before) where MLB has them, and so taken out of every line here
        self.situations = Situations.from_summary(raw.get("situations"))
        total = lambda rows, key: sum(_num(r, key) for r in rows)
        self.decides_ibb = bool(self.situations) and total(own, "intentionalWalks") > 0
        self.decides_sac = bool(self.situations) and total(own, "sacBunts") > 0
        strip = (self.decides_ibb, self.decides_sac) if self.situations else None
        own_pa = total(own, "plateAppearances")
        self.league_ibb = total(own, "intentionalWalks") / own_pa if own_pa else 0.0
        self.league_sac = total(own, "sacBunts") / own_pa if own_pa else 0.0

        def sit_factors(rows, shrink: bool = True) -> tuple:
            """(intentional walks, sacrifices) per PA against the league's, in odds; None where not decided."""
            n = total(rows, "plateAppearances")
            return (sit_factor(total(rows, "intentionalWalks"), n, self.league_ibb, SHRINK_IBB_PA if shrink else 0)
                    if self.decides_ibb else None,
                    sit_factor(total(rows, "sacBunts"), n, self.league_sac, SHRINK_SAC_PA if shrink else 0)
                    if self.decides_sac else None)
        self.league = league_rates(own, raw.get("retro_split"), strip)
        # the league's position players, and MLB's pitchers at bat (league-average for each kind of batter)
        hitters = [r for r in own if r.get("_pos") != "P"]
        arms = [r for r in raw["league_hitting"] if r.get("_pos") == "P" and _num(r, "plateAppearances") > 0]
        self.league_hitters = league_rates(hitters, raw.get("retro_split"), strip) if hitters else self.league
        self.hitters_sit = sit_factors(hitters, shrink=False) if hitters else (1.0, 1.0)
        if sum(_num(r, "plateAppearances") for r in arms) >= 500:
            self.league_pitchers_batting = league_rates(arms, raw.get("retro_split"), strip)
            self.league_pitchers_sit = sit_factors(arms, shrink=False)
        else:
            self.league_pitchers_batting = pitchers_at_bat(self.league)
            self.league_pitchers_sit = (PITCHER_IBB if self.decides_ibb else None,
                                        PITCHER_SAC if self.decides_sac else None)
        if not self.situations:
            self.notes.append(f"{name}: Retrosheet has no {self.season} play-by-play -- intentional walks and "
                              "sacrifices stay in each line as walks and outs, not decided by the situation")
        elif not self.decides_ibb:
            self.notes.append(f"{name}: MLB has no intentional walks for {self.season} -- they stay in the walks")
        # its home park (Retrosheet), and the venue (MLB: name and distances)
        self.park = park_factors(raw.get("park"))
        self.park_counts = raw.get("park")
        self.venue = raw.get("venue")
        self.flat_park = False
        self.league_name = self.league_name or "MLB"
        # --- running
        self.league_running = league_running(raw["league_runners"]) if raw.get("league_runners") else None
        runner_rows = mlbstats.group_by_player(raw["runners"]) if raw.get("runners") else {}
        self.speed_groups = {pid: (v if isinstance(v, dict) else {"group": v, "rate": None})
                             for pid, v in (speed or {}).items()}
        # --- defense (the Range tab's usual nine)
        self.defense = mlbstats.assign_positions(raw["fielding"])
        self.default_defense = {pos: row["_id"] for pos, row in self.defense.items()}
        for pos, pid in (self.overrides.get("defense") or {}).items():
            row = next((r for r in raw["fielding"] if r.get("_id") == pid and r.get("_pos") == pos), None)
            if row is not None:  # (only someone who played the position can be put there)
                self.defense[pos] = row
        if self.overrides.get("defense") and any(self.default_defense.get(pos) != row["_id"]
                                                 for pos, row in self.defense.items()):
            self.notes.append(f"{name}: fielders chosen by you, not the Range tab's")
        self.fielding_rows = raw["fielding"]
        lg = self.league_positions = raw["league_positions"]
        self.fielding_by_player: dict = {}
        for r in raw["fielding"]:
            if r.get("_id") is not None and r.get("_pos"):
                self.fielding_by_player.setdefault(r["_id"], {})[r["_pos"]] = r
        # what the pitchers' lines had behind them at each position: everyone the club played there all season
        self.club_range = {}
        for pos in OUTFIELD:
            got = mlbstats.roster_range([r for r in raw["fielding"] if r.get("_pos") == pos], lg)
            self.club_range[pos] = got["ratio"] if got else None
        # a range factor counts chances, and a staff that allows more balls in play per out gives its fielders
        # more: the club's balls in play per out against its league's, to tell its fielders' skill from that
        self.bip_volume = _bip_per_out(raw["pitching"]) / _bip_per_out(own) \
            if _bip_per_out(raw["pitching"]) and _bip_per_out(own) else 1.0
        # the path analysis's league-average fielders, and pitchers (whose lines then have no club in them)
        self.flat_range = self.flat_errors = self.flat_pitching = False
        # fielders against runners: Retrosheet's arms and double plays, MLB's catchers and pitchers against steals
        self.arms, self.arms_league = raw.get("arms") or {}, raw.get("arms_league") or {}
        self.tags, self.tags_club = raw.get("tags") or {}, raw.get("tags_club") or {}
        self.flat_arms = self.flat_dps = self.flat_steals = False
        self._running_cache: dict = {}
        staff = [r for r in raw["pitching"] if _num(r, "battersFaced") > 0]
        self.staff_steals = (sum(_num(r, "stolenBases") for r in staff), sum(_num(r, "caughtStealing") for r in staff),
                             sum(_num(r, "battersFaced") for r in staff))
        self._state_cache: dict = {}
        if not any(self.club_range.values()):
            self.notes.append(f"{name}: fielders' range couldn't be measured -- every fielder counted as the "
                              "club's season at his position")
        # --- pitchers (the Prevention tab's roles)
        pitching = [r for r in raw["pitching"] if _num(r, "battersFaced") > 0]
        roles = mlbstats.classify_pitchers(pitching)["roles"]
        loads = _workloads(pitching, raw.get("relief"))
        hitting_by_id = {r["_id"]: r for r in raw["hitting"]}
        pitching_by_id = {r["_id"]: r for r in pitching}

        def is_pitcher(row) -> bool:
            """A pitcher who batted only as one: he faced more batters than he came to bat, and played fewer
            than ``TWO_WAY_GAMES`` games without pitching.  A two-way player (Ruth 1919: 130 games, 17
            pitched; Ohtani) is a batter who also pitches."""
            p = pitching_by_id.get(row["_id"])
            if p is None:
                return False
            not_pitching = _num(row, "gamesPlayed") - _num(p, "gamesPitched")
            return not_pitching < TWO_WAY_GAMES and _num(p, "battersFaced") > _num(row, "plateAppearances")
        # batters who also pitch: in the order as themselves, and on the staff
        self.two_way = {pid for pid in pitching_by_id if pid in hitting_by_id and not is_pitcher(hitting_by_id[pid])
                        and _num(hitting_by_id[pid], "gamesPlayed") - _num(pitching_by_id[pid], "gamesPitched")
                        >= TWO_WAY_GAMES}
        staff_batting = [hitting_by_id[p] for p in pitching_by_id if p in hitting_by_id and is_pitcher(hitting_by_id[p])]
        split = self.league["_split"] == "MLB"
        pooled = line_counts(staff_batting, split=split, strip=strip) if staff_batting else None
        toward = self.league_pitchers_batting or self.league  # a few at-bats: pulled toward MLB's pitchers
        if pooled and pooled["n"] > 0:
            self.pitchers_batting = rates_from_counts(class_counts(pooled, self.league), pooled["n"], toward, SHRINK_PA)
            # the staff's intentional walks and sacrifices at bat, pulled toward MLB's pitchers'
            n = total(staff_batting, "plateAppearances")
            self.pitchers_sit = tuple(
                None if lg is None else
                odds_factor(total(staff_batting, key), n, tilt(rate, lg), shrink) * lg if rate and 0 < rate < 1 else lg
                for key, rate, lg, shrink in (("intentionalWalks", self.league_ibb, self.league_pitchers_sit[0],
                                               SHRINK_IBB_PA),
                                              ("sacBunts", self.league_sac, self.league_pitchers_sit[1], SHRINK_SAC_PA)))
        else:
            self.pitchers_batting = self.league_pitchers_batting
            self.pitchers_sit = self.league_pitchers_sit
        self.pitchers: list[Pitcher] = []
        self.roles = {}
        for role in mlbstats.PITCHER_ROLES:
            members = []
            for r in roles[role]:
                start, rel = loads[r["_id"]]
                bat = None
                if r["_id"] in hitting_by_id and self.pitchers_batting:
                    c = line_counts(hitting_by_id[r["_id"]], split=split, strip=strip)
                    if c["n"] > 0:
                        bat = rates_from_counts(class_counts(c, self.league), c["n"], self.pitchers_batting, SHRINK_PA)
                g, gs = _num(r, "gamesPitched"), _num(r, "gamesStarted")
                p = Pitcher(r["_id"], r.get("_name", "?"), role, player_rates(r, self.league, pitcher=True),
                            self.league, start, rel, r, bat or self.pitchers_batting, _num(r, "gamesFinished"),
                            max(g - gs, 0) * (rel or 0))
                members.append(p)
                self.pitchers.append(p)
            self.roles[role] = members
        self.rotation = [p for p in self.roles["Rotation"] if p.bf_start]
        if not self.rotation:  # (a season with no starts recorded)
            self.rotation = sorted(self.pitchers, key=lambda p: -_num(p.row, "battersFaced"))[:4]
            for p in self.rotation:
                p.bf_start = p.bf_start or 27.0
        closers = self.roles["Closers"]
        self.closer = max(closers, key=lambda p: (p.gf, p.relief_work)) if closers else None
        # --- batting order (the Production tab's)
        hitting = [r for r in raw["hitting"] if _num(r, "plateAppearances") > 0]
        chosen = mlbstats.assign_lineup(hitting, raw["order"]) if raw.get("order") else {}
        self.order_source = "most games batting from each place (Production tab)"
        if len(chosen) < 9:
            if not chosen:
                self.order_source = "the nine with the most plate appearances, most first (no batting-order records)"
                chosen = mlbstats.lineup_by_plate_appearances(hitting)
            used = {r["_id"] for r in chosen.values()}
            rest = [r for r in sorted(hitting, key=lambda r: -_num(r, "plateAppearances")) if r["_id"] not in used]
            for slot in mlbstats.LINEUP_SLOTS:
                if slot not in chosen and rest:
                    chosen[slot] = rest.pop(0)
        own_rule = uses_dh(self.league_name, self.season)
        self.uses_dh = {"dh": True, "pitchers": False}.get(dh_rule, own_rule)
        self.dh_rule = dh_rule
        in_field = {row["_id"] for pos, row in self.defense.items() if pos != "P"}
        if not self.uses_dh and not any(is_pitcher(r) for r in chosen.values()):
            # No DH: the pitcher bats.  His place is where the team's pitchers batted most games (the ninth before
            # batting-order records, or with none on record).
            by_slot = {slot: 0 for slot in mlbstats.LINEUP_SLOTS}
            for pid, places in (raw.get("order") or {}).items():
                if pid in pitching_by_id and pid not in {r["_id"] for r in chosen.values()}:
                    for slot, info in places.items():
                        by_slot[slot] += info["games"]
            spot = max(by_slot, key=lambda k: (by_slot[k], k))
            if by_slot[spot] == 0:
                spot = 9
            pitcher_spot = {"_id": "P", "_name": "Pitcher's spot"}
            dh_slot = next((slot for slot in mlbstats.LINEUP_SLOTS if slot in chosen
                            and chosen[slot]["_id"] not in in_field), None) if own_rule else None
            if dh_slot is not None:  # a DH club told to let its pitchers bat: the DH sits, those below move up
                rest = [chosen[slot] for slot in mlbstats.LINEUP_SLOTS if slot in chosen and slot != dh_slot]
                rest.insert(spot - 1, pitcher_spot)
                self.notes.append(f"{name}: pitchers bat (your choice; the {self.league_name} used the DH in "
                                  f"{self.season}) -- {chosen[dh_slot].get('_name', '?')}, the DH, sits and the "
                                  f"pitcher bats {_ordinal(spot)}")
                chosen = dict(zip(mlbstats.LINEUP_SLOTS, rest))
            elif slot_row := chosen.get(spot):
                self.notes.append(f"{name}: no DH" + (" (your choice)" if dh_rule == "pitchers" and own_rule else
                                                      f" in the {self.league_name} in {self.season}")
                                  + f" -- the pitcher bats {_ordinal(spot)} (where the club's pitchers batted most), "
                                  f"in place of {slot_row.get('_name', '?')}")
                chosen[spot] = pitcher_spot
        elif self.uses_dh and not own_rule:
            # a club whose pitchers batted, told to use the DH: the bench's busiest hitter bats in the pitcher's place
            used = {r["_id"] for r in chosen.values()} | in_field
            bench_bats = sorted((r for r in hitting if r["_id"] not in used and not is_pitcher(r)),
                                key=lambda r: -_num(r, "plateAppearances"))
            for slot in mlbstats.LINEUP_SLOTS:
                row = chosen.get(slot)
                if row is not None and (row["_id"] == "P" or is_pitcher(row)) and bench_bats:
                    dh = bench_bats.pop(0)
                    self.notes.append(f"{name}: the DH (your choice; the {self.league_name}'s pitchers batted in "
                                      f"{self.season}) -- {dh.get('_name', '?')}, the bench's busiest hitter, bats "
                                      f"{_ordinal(slot)} in the pitcher's place")
                    chosen[slot] = dh
        self.default_order = [("P" if is_pitcher(chosen[slot]) else chosen[slot]["_id"]) if slot in chosen else None
                              for slot in mlbstats.LINEUP_SLOTS]
        self.eligible_batters = sorted((r for r in hitting if not is_pitcher(r)),
                                       key=lambda r: -_num(r, "plateAppearances"))
        order = self.overrides.get("order")
        if order and list(order) != self.default_order:
            by_id = {r["_id"]: r for r in hitting}
            chosen = {}
            for slot, pid in zip(mlbstats.LINEUP_SLOTS, order):
                if pid == "P":
                    chosen[slot] = {"_id": "P", "_name": "Pitcher's spot"}
                elif pid in by_id:
                    chosen[slot] = by_id[pid]
            self.order_source = "your batting order"
            self.notes.append(f"{name}: batting order chosen by you, not the Production tab's")
        position = {}
        for pos, row in self.defense.items():
            position[row["_id"]] = pos
        def make_batter(row) -> Batter:
            rows = runner_rows.get(row["_id"])
            running = runner_factors(rows, self.league_running) if rows and self.league_running else AVERAGE_RUNNER
            gidp = odds_factor(_num(row, "groundIntoDoublePlay"), N(row.get("groundOuts")), gidp_league,
                               SHRINK_GROUND_OUTS) if split and N(row.get("groundOuts")) is not None else 1.0
            speed_info = self.speed_groups.get(row["_id"]) or {}
            b = Batter(row["_id"], row.get("_name", "?"), player_rates(row, self.league), self.league,
                       running, gidp, row, speed_info.get("group", ""))
            b.speed_rate = speed_info.get("rate")
            b.ibb, b.sac = sit_factors([row])
            return b

        self.lineup: list[Batter | None] = []  # None = the pitcher's spot
        self.lineup_rows = []
        gidp_league = self.league.get("_gidp_go")
        for slot in mlbstats.LINEUP_SLOTS:
            row = chosen.get(slot)
            if row is None:
                continue
            if row["_id"] == "P" or is_pitcher(row):
                self.lineup.append(None)  # he pitched more than he batted: whoever pitches bats here
                self.lineup_rows.append((slot, row, "pitcher's spot"))
                continue
            self.lineup.append(make_batter(row))
            pos = position.get(row["_id"], "bats only")
            self.lineup_rows.append((slot, row, "bats only" if pos == "P" else pos))  # (a two-way player: whoever
        if len(self.lineup) < 9:                                                   # pitches fields there)
            raise SimError(f"{name}: only {len(self.lineup)} batters could be found for a lineup")
        if self.uses_dh:  # the first who bats but doesn't field is the designated hitter
            for i, (slot, row, pos) in enumerate(self.lineup_rows):
                if pos == "bats only":
                    self.lineup_rows[i] = (slot, row, "DH")
                    break
        # --- the bench: pinch runners (the Speed tab's groups) and defensive replacements
        in_order = {b.pid for b in self.lineup if b is not None}
        in_order |= {row["_id"] for pos, row in self.defense.items() if pos != "P"}  # fielders are in the game too
        # (everyone who batted or ran: a pure pinch runner like Herb Washington, 1974, never came to the plate)
        bench_rows = [r for r in raw["hitting"] if r.get("_id") not in in_order and not is_pitcher(r)
                      and (_num(r, "plateAppearances") > 0 or r["_id"] in runner_rows or r["_id"] in self.speed_groups)]
        self.bench = [make_batter(r) for r in bench_rows]
        self.slow = {b.pid for b in self.lineup if b is not None and b.group == "Slow regulars"}
        rank = {g: i for i, g in enumerate(PINCH_RUNNER_GROUPS)}
        self.pinch_runners = sorted((b for b in self.bench if b.group in rank),
                                    key=lambda b: (rank[b.group], -(b.speed_rate or 0), b.name))
        if None in self.lineup and not self.pitchers_batting:
            self.pitchers_batting = dict(self.league_pitchers_batting or self.league)  # (none on record)
            for p in self.pitchers:
                p.bat_rates = p.bat_rates or self.pitchers_batting
        # --- rest days: each regular starts the share of the club's games he really started (fielding records,
        # the DH's included), the bench man who played his position most taking his place on the others
        team_games = sum(_num(r, "gamesStarted") for r in raw["pitching"]) or \
            max((_num(r, "gamesPlayed") for r in raw["hitting"]), default=0)
        started: dict = {}
        for r in raw["fielding"]:
            if r.get("_id") is not None and r.get("_pos") != "P":
                started[r["_id"]] = started.get(r["_id"], 0) + _num(r, "gamesStarted")
        self.rest = {}  # {player id: share of games he sits}
        for b in self.lineup:
            if b is None or b.pid in self.two_way or not team_games:
                continue
            starts = started.get(b.pid) or _num(b.row, "gamesPlayed")
            if 0 < starts < team_games:
                self.rest[b.pid] = 1 - starts / team_games
        self.default_rotation = [p.pid for p in self.rotation]
        self.custom_rotation = False
        by_pid = {p.pid: p for p in self.pitchers}
        chosen_rotation = [by_pid[pid] for pid in (self.overrides.get("rotation") or []) if pid in by_pid]
        if chosen_rotation:  # (yours even if it names the default pitchers: then they go in strict turn)
            for p in chosen_rotation:
                if not p.bf_start:  # a reliever starting goes as long as he does in relief
                    p.bf_start = p.bf_relief
            self.rotation = chosen_rotation
            self.custom_rotation = True
            self.notes.append(f"{name}: your rotation, in turn: " + ", ".join(p.name for p in chosen_rotation))
        # a player's line was half made in his home park: that half comes out, so a park counts only in its games
        if self.park:
            for b in [b for b in self.lineup if b is not None] + self.bench:
                b.rates = with_park(b.rates, self.park, -0.5)
            for p in self.pitchers:
                p.rates = with_park(p.rates, self.park, -0.5)
                if p.bat_rates is not None and p.bat_rates is not self.pitchers_batting:
                    p.bat_rates = with_park(p.bat_rates, self.park, -0.5)
            if self.pitchers_batting:
                neutral = with_park(self.pitchers_batting, self.park, -0.5)
                for p in self.pitchers:
                    if p.bat_rates is self.pitchers_batting:
                        p.bat_rates = neutral
                self.pitchers_batting = neutral
        # --- platoon: each player against each hand on the other side (the league's effect put back in the games)
        self.platoon = platoon_ratios(raw.get("platoon_cells")) if raw.get("hands") else None
        self.platoon_season = raw.get("platoon_season")
        self.hands = raw.get("hands") or {}
        splits = raw.get("platoon") or {}
        self.platoon_splits = {g: splits.get(g) or {} for g in ("hitting", "pitching")}
        for b in [b for b in self.lineup if b is not None] + self.bench:
            b.bats = (self.hands.get(b.pid) or {}).get("bats", "")
            b.platoon = self.platoon_of(b.rates, b.pid, b.bats)
        for p in self.pitchers:
            hands = self.hands.get(p.pid) or {}
            p.bats, p.throws = hands.get("bats", ""), hands.get("throws", "")
            p.platoon = self.platoon_of(p.rates, p.pid, p.throws, pitcher=True)
            p.bat_platoon = self.platoon_of(p.bat_rates, None, p.bats) if p.bat_rates else None

    def platoon_of(self, rates: dict, pid, hand: str, pitcher: bool = False) -> dict | None:
        """:func:`platoon_rates` for one of the team's players (``pid`` None: no splits of his own, the league's
        platoon effect only); None without the league's figures or his hand."""
        if not self.platoon or not hand:
            return None
        splits = self.platoon_splits["pitching" if pitcher else "hitting"].get(pid) if pid is not None else None
        return platoon_rates(rates, splits, hand, self.platoon, self.league, pitcher, self.park)

    def lineup_problems(self) -> dict:
        """What a real lineup card wouldn't allow: batters in the order who don't field (beyond the DH), and
        the fielders who don't bat because of them -> ``{"bats_only": [names], "fields_only": [(position,
        name)]}`` (both empty when the lineup is sound)."""
        bats_only = [row.get("_name", "?") for _slot, row, pos in self.lineup_rows if pos == "bats only"]
        batting = {b.pid for b in self.lineup if b is not None}
        fields_only = [(pos, row.get("_name", "?")) for pos, row in self.defense.items()
                       if pos != "P" and row["_id"] not in batting] if bats_only else []
        return {"bats_only": bats_only, "fields_only": fields_only}

    def suggest_positions(self) -> dict:
        """A way to resolve :meth:`lineup_problems`: the batters who don't field go, most plate appearances
        first, to the open position -- one whose fielder doesn't bat -- each played most that season; then the
        DH, if a position he played is still open; then the first left over, the DH first, is the DH where there
        is one -> ``{player id: position or "DH"}`` for those it could place (perhaps not all)."""
        batting = [b for b in self.lineup if b is not None]
        fielding = {row["_id"] for q, row in self.defense.items() if q != "P"}
        dh = [row["_id"] for _slot, row, pos in self.lineup_rows if pos == "DH"]
        open_positions = {q for q, row in self.defense.items() if q != "P" and row["_id"] not in {b.pid for b in batting}}
        out = {}
        left = []
        # the DH last: he moves to the field only if a place is still open once the others have theirs
        for b in sorted((b for b in batting if b.pid not in fielding),
                        key=lambda b: (b.pid in dh, -_num(b.row, "plateAppearances"))):
            played = sorted(((mlbstats.innings_to_outs(row.get("innings", 0)), q)
                             for q, row in (self.fielding_by_player.get(b.pid) or {}).items() if q in open_positions),
                            reverse=True)
            if played:
                out[b.pid] = played[0][1]
                open_positions.discard(played[0][1])
            else:
                left.append(b.pid)
        if self.uses_dh and left:
            out[next((pid for pid in left if pid in dh), left[0])] = "DH"
        return out

    def range_against_club(self, pid, pos: str) -> float:
        """A fielder's range at ``pos`` (:meth:`defense_rating`) against the club's whole season there, which
        the pitchers' lines already have in them: 1 = no different.  The pitcher's and catcher's isn't
        measured (1).  With ``flat_range`` he's an MLB-average fielder and with ``flat_pitching`` the pitchers'
        lines are the league's, which have MLB-average fielding in them: for those two, each side's range is
        taken as skill, the club's balls in play per out (``bip_volume``) divided out."""
        club = self.club_range.get(pos)
        if not club:
            return 1.0
        mine = 1.0 if self.flat_range else (self.defense_rating(pid, pos) or club) / self.bip_volume
        behind = 1.0 if self.flat_pitching else club / self.bip_volume
        return mine / behind

    def error_factor(self, pid, pos: str) -> float:
        """A fielder's errors per chance at ``pos`` against MLB's there, in odds, with
        ``SHRINK_ERROR_CHANCES`` league-average chances added (1 = average, and with ``flat_errors``)."""
        lg = self.league_positions.get(pos) or {}
        row = (self.fielding_by_player.get(pid) or {}).get(pos)
        if self.flat_errors or row is None or not lg.get("total_chances"):
            return 1.0
        chances = _num(row, "putOuts") + _num(row, "assists") + _num(row, "errors")
        return odds_factor(_num(row, "errors"), chances, lg["errors"] / lg["total_chances"], SHRINK_ERROR_CHANCES)

    def _arm_counts(self, pid, pos: str, i: int) -> tuple[float, float, float | None]:
        """His count and chances (``retrosheet.ARM_KEYS`` pair at ``i``: 0 extra bases, 2 tag-ups, 4 double
        plays) at ``pos``, and MLB's rate there."""
        mine = (self.arms.get(pid) or {}).get(pos) or [0] * 6
        lg = self.arms_league.get(pos) or [0] * 6
        return mine[i + 1], mine[i], (lg[i + 1] / lg[i] if lg[i] else None)

    def arm(self, pid, pos: str) -> tuple[float, float]:
        """A fielder's arm at ``pos`` against MLB's there, in odds (1 = average): runners taking an extra base
        on hits he picks up, and tagging up on fly balls he catches."""
        if self.flat_arms:
            return 1.0, 1.0
        xb, xb_opp, xb_lg = self._arm_counts(pid, pos, 0)
        tu, tu_opp, tu_lg = self._arm_counts(pid, pos, 2)
        return (odds_factor(xb, xb_opp, xb_lg, SHRINK_ARM_EXTRA_BASE),
                odds_factor(tu, tu_opp, tu_lg, SHRINK_ARM_TAG_UP))

    def turning_dps(self, pid, pos: str) -> float:
        """How often double-play grounders he fields are turned, against MLB's at ``pos``, in odds."""
        if self.flat_dps:
            return 1.0
        dp, opp, lg = self._arm_counts(pid, pos, 4)
        return odds_factor(dp, opp, lg, SHRINK_TURNING_DPS)

    def catcher_steals(self, pid) -> tuple[float, float]:
        """A catcher against steals, against MLB's catchers: (attempts per inning, in rate; runners safe, in
        odds).  His figures have his pitchers in them, as MLB's catchers' have theirs."""
        row = (self.fielding_by_player.get(pid) or {}).get("C")
        lg = self.league_positions.get("C") or {}
        lg_att = (lg.get("stolen_bases") or 0) + (lg.get("caught_stealing") or 0)
        if self.flat_steals or row is None or not lg_att or not lg.get("outs"):
            return 1.0, 1.0
        innings = mlbstats.innings_to_outs(row.get("innings", 0)) / 3
        sb, cs = _num(row, "stolenBases"), _num(row, "caughtStealing")
        per_inning = lg_att / (lg["outs"] / 3)
        att = (sb + cs + SHRINK_CATCHER_INNINGS * per_inning) / (innings + SHRINK_CATCHER_INNINGS) / per_inning
        return att, odds_factor(sb, sb + cs, lg["stolen_bases"] / lg_att, SHRINK_CATCHER_ATTEMPTS)

    def club_catcher_steals(self) -> tuple[float, float]:
        """All the club's catchers together against MLB's, as :meth:`catcher_steals` (no shrinking)."""
        rows = [r for r in self.fielding_rows if r.get("_pos") == "C"]
        lg = self.league_positions.get("C") or {}
        lg_att = (lg.get("stolen_bases") or 0) + (lg.get("caught_stealing") or 0)
        innings = sum(mlbstats.innings_to_outs(r.get("innings", 0)) for r in rows) / 3
        if not rows or not lg_att or not lg.get("outs") or not innings:
            return 1.0, 1.0
        sb, cs = sum(_num(r, "stolenBases") for r in rows), sum(_num(r, "caughtStealing") for r in rows)
        att = (sb + cs) / innings / (lg_att / (lg["outs"] / 3))
        return att, odds_factor(sb, sb + cs, lg["stolen_bases"] / lg_att, 0)

    def pitcher_steals(self, p) -> tuple[float, float]:
        """A pitcher against steals, against his own staff's (who threw to the same catchers): (attempts per
        batter faced, in rate; runners safe, in odds)."""
        sb_all, cs_all, bf_all = self.staff_steals
        if self.flat_steals or not bf_all or not (sb_all + cs_all):
            return 1.0, 1.0
        sb, cs, bf = _num(p.row, "stolenBases"), _num(p.row, "caughtStealing"), _num(p.row, "battersFaced")
        per_batter = (sb_all + cs_all) / bf_all
        att = (sb + cs + SHRINK_PITCHER_BATTERS * per_batter) / (bf + SHRINK_PITCHER_BATTERS) / per_batter
        return att, odds_factor(sb, sb + cs, sb_all / (sb_all + cs_all), SHRINK_PITCHER_ATTEMPTS)

    def infielder_steals(self, pid, pos: str) -> float:
        """An infielder taking throws on steals at ``pos`` (second base or short: steals of second; third base: of
        third), against his own club's (the same catchers and pitchers throwing): runners safe, in odds."""
        mine = (self.tags.get(pid) or {}).get(pos)
        club = self.tags_club.get(pos)
        if self.flat_steals or not mine or not club or not club[0] or club[1] >= club[0]:
            return 1.0
        return odds_factor(mine[0] - mine[1], mine[0], (club[0] - club[1]) / club[0], SHRINK_TAG_ATTEMPTS)

    def running_state(self, defense: dict, pitcher) -> dict:
        """The fielders on the field against the runners -> ``{position: (extra bases, tag-ups, double plays),
        "steal": (attempts, safe), "tag": {base: safe}}`` -- the pitcher's (position "P") and the catcher's with
        him; and the infielders covering a steal of second (second base and short, an equal share each) or third."""
        key = (tuple((pos, row["_id"]) for pos, row in sorted(defense.items()) if pos != "P"),
               getattr(pitcher, "pid", None))
        got = self._running_cache.get(key)
        if got is None:
            got = {}
            for pos, pid in key[0]:
                got[pos] = self.arm(pid, pos) + (self.turning_dps(pid, pos),)
            att = safe = 1.0
            catcher = defense.get("C")
            if catcher is not None:
                att, safe = self.catcher_steals(catcher["_id"])
            if pitcher is not None:
                got["P"] = self.arm(pitcher.pid, "P") + (self.turning_dps(pitcher.pid, "P"),)
                p_att, p_safe = self.pitcher_steals(pitcher)
                att, safe = att * p_att, safe * p_safe
            got["steal"] = (att, safe)
            ids = dict(key[0])
            got["tag"] = {}
            for base, cover in STEAL_COVER.items():
                f = 1.0
                for pos in cover:
                    f *= self.infielder_steals(ids.get(pos), pos) ** (1 / len(cover))
                got["tag"][base] = f
            if len(self._running_cache) >= MATCHUP_CACHE_MAX:
                trim_cache(self._running_cache)
            self._running_cache[key] = got
        return got

    def fielding_state(self, defense: dict, pitcher) -> tuple:
        """The fielders on the field -> ``((position, range, errors), ...)`` for :func:`zone_factors`: each
        one's range against the club's season at his position (:meth:`range_against_club`) and errors
        against MLB's (:meth:`error_factor`), the pitcher's errors too."""
        key = (tuple((pos, row["_id"]) for pos, row in sorted(defense.items()) if pos != "P"),
               getattr(pitcher, "pid", None))
        got = self._state_cache.get(key)
        if got is None:
            got = tuple((pos, self.range_against_club(pid, pos), self.error_factor(pid, pos)) for pos, pid in key[0])
            if pitcher is not None:
                got += (("P", 1.0, self.error_factor(pitcher.pid, "P")),)
            if len(self._state_cache) >= MATCHUP_CACHE_MAX:
                trim_cache(self._state_cache)
            self._state_cache[key] = got
        return got

    def defense_rating(self, pid, pos: str) -> float | None:
        """A fielder's record at ``pos`` as a share of MLB's chances there, with ``DEFENSE_SHRINK_INNINGS`` of
        league-average fielding added so a few lucky innings don't win the job (None if never measured there)."""
        row = (self.fielding_by_player.get(pid) or {}).get(pos)
        lg = self.league_positions.get(pos)
        if row is None or not lg:
            return None
        f = mlbstats.fielding_row_figures(row)
        if f["rf_9"] is not None and lg.get("rf_9"):
            chances, expected = f["chances_inn"], lg["rf_9"] * f["outs"] / 27
            pad = lg["rf_9"] * DEFENSE_SHRINK_INNINGS / 9
        elif f["rf_game"] is not None and lg.get("rf_game"):
            chances, expected = f["chances_game"], lg["rf_game"] * f["games"]
            pad = lg["rf_game"] * DEFENSE_SHRINK_INNINGS / 9
        else:
            return None
        return (chances + pad) / (expected + pad)

    def day_lineup(self, g: int, lineup: list, defense: dict) -> list[tuple]:
        """Who sits in game ``g`` (0-based), and who plays for him -> ``[(place in the order, regular, his
        replacement, position or "DH")]``; ``lineup`` and ``defense`` (the game's own copies) are changed to match.
        Each regular sits on a fixed schedule, spread through the run, as often as he didn't start (``rest``); the
        places' schedules are staggered so they don't all sit together.  His replacement is the bench man with the
        most innings at his position (the DH: the bench's busiest hitter); with nobody to replace him he plays."""
        if not self.fatigue or not self.rest or "rest_days" in self.off:
            return []
        out = []
        used = {b.pid for b in lineup if b is not None}
        for i, b in enumerate(lineup):
            share = self.rest.get(b.pid) if b is not None else None
            if not share:
                continue
            phase = (i * 0.618) % 1.0
            if math.floor((g + 1) * share + phase) == math.floor(g * share + phase):
                continue  # he plays today
            pos = next((q for q, row in defense.items() if q != "P" and row["_id"] == b.pid), None)
            options = [c for c in self.bench if c.pid not in used]
            if pos:
                options = [c for c in options if pos in (self.fielding_by_player.get(c.pid) or {})]
                sub = max(options, key=lambda c: (mlbstats.innings_to_outs(
                    self.fielding_by_player[c.pid][pos].get("innings", 0)), c.name), default=None)
            else:
                sub = max(options, key=lambda c: (_num(c.row, "plateAppearances"), c.name), default=None)
            if sub is None:
                continue
            lineup[i] = sub
            used.add(sub.pid)
            if pos:
                defense[pos] = self.fielding_by_player[sub.pid][pos]
            out.append((i, b, sub, pos or "DH"))
        return out

    def start_shares(self) -> dict:
        """``{pitcher id: share of the starts}`` as :meth:`starters` hands them out."""
        if self.custom_rotation:
            counts = {}
            for p in self.rotation:
                counts[p.pid] = counts.get(p.pid, 0) + 1 / len(self.rotation)
            return counts
        total = sum(_num(p.row, "gamesStarted") or 1 for p in self.rotation)
        return {p.pid: (_num(p.row, "gamesStarted") or 1) / total for p in self.rotation}

    def starters(self, games: int) -> list[Pitcher]:
        """Who starts each game: each Rotation pitcher in proportion to his real starts, handed out in
        turn (whoever is furthest behind his share goes next) -- or, with your rotation, strictly in your order."""
        if self.custom_rotation:
            return [self.rotation[g % len(self.rotation)] for g in range(games)]
        total = sum(_num(p.row, "gamesStarted") or 1 for p in self.rotation)
        shares = [(_num(p.row, "gamesStarted") or 1) / total for p in self.rotation]
        given = [0] * len(self.rotation)
        out = []
        for g in range(games):
            i = max(range(len(self.rotation)), key=lambda j: (shares[j] * (g + 1) - given[j], shares[j]))
            given[i] += 1
            out.append(self.rotation[i])
        return out


class Environment:
    """The rules and league the games are played in: the home team's season."""

    def __init__(self, home: Team, tables: Tables, tables_season: int | None, park: bool = True):
        self.season, self.league, self.tables, self.tables_season = home.season, home.league, tables, tables_season
        self.home = home
        # the home park's factors (None: neutral), and the league's rates there -- what a game here is played in
        self.park = home.park if park else None
        self.rates = with_park(home.league, self.park)
        self.placed_runner = home.season >= PLACED_RUNNER_FROM
        self.roe_share = tables.roe_share
        self._zone_cache: dict = {}
        self.platoon = home.platoon  # the league's plate appearances by hand against hand (None: no platoon)
        self._cell_cache: dict = {}
        self._edge_cache: dict = {}
        self.edge_off = False  # the path analysis: the home batters' edge taken away

    def cell_rates(self, side: str, hand: str) -> dict:
        """The environment's rates for a batter batting from ``side`` against a pitcher throwing with ``hand``:
        the league's (in this park) times that matchup's ratio (:func:`platoon_ratios`), scaled back to their
        total."""
        got = self._cell_cache.get((side, hand))
        if got is None:
            ratio = self.platoon["ratio"][(side, hand)]
            raw = {k: self.rates[k] * ratio[k] for k in LOG5_CLASSES}
            total, norm = sum(self.rates[k] for k in LOG5_CLASSES), sum(raw.values())
            got = self._cell_cache[(side, hand)] = dict(self.rates, **{k: v / norm * total for k, v in raw.items()})
        return got

    def with_edge(self, rates: dict, home: bool) -> dict:
        """``rates`` (the environment's, or its rates for a matchup of hands) for the home batters (``home``) or the
        visitors: times the season's edge of batting at home (:func:`home_edge`), scaled back to their total."""
        edge = self.tables.home_edge
        if not edge or self.edge_off:
            return rates
        key = (id(rates), home)
        got = self._edge_cache.get(key)
        if got is None:
            raw = {k: rates[k] * edge[home][k] for k in LOG5_CLASSES}
            total, norm = sum(rates[k] for k in LOG5_CLASSES), sum(raw.values())
            got = self._edge_cache[key] = (rates, dict(rates, **{k: v / norm * total for k, v in raw.items()}))
        return got[1]

    def without_edge(self) -> "Environment":
        """The same environment without the edge of batting at home (the path analysis)."""
        env = Environment(self.home, self.tables, self.tables_season, park=self.park is not None)
        env.edge_off = True
        return env

    def without_park(self) -> "Environment":
        """The same environment in a neutral park (the path analysis)."""
        return Environment(self.home, self.tables, self.tables_season, park=False)

    def zone_factors(self, fielders: tuple) -> dict:
        """:func:`zone_factors` for a fielding side's ``Team.fielding_state``, remembered."""
        got = self._zone_cache.get(fielders)
        if got is None:
            if len(self._zone_cache) >= MATCHUP_CACHE_MAX:
                trim_cache(self._zone_cache)
            got = self._zone_cache[fielders] = zone_factors({pos: (r, e) for pos, r, e in fielders},
                                                            self.tables.zones)
        return got


# --------------------------------------------------------------------------- #
#  Playing                                                                    #
# --------------------------------------------------------------------------- #
class _Runner:
    __slots__ = ("pid", "name", "running", "pitcher")

    def __init__(self, pid, name, running, pitcher):
        self.pid, self.name, self.running, self.pitcher = pid, name, running, pitcher


def _blank_batting():
    """A batter's simulated line; PR = times he went in as a pinch runner, DEF = times he came in on defense,
    PH = times he pinch-hit (for the pitcher or a regular), GS = games he started.  BB counts the intentional walks (IBB) too, as
    MLB's do."""
    return {"GS": 0, "PA": 0, "AB": 0, "H": 0, "1B": 0, "2B": 0, "3B": 0, "HR": 0, "BB": 0, "IBB": 0, "HBP": 0, "K": 0,
            "SF": 0, "SH": 0, "ROE": 0, "R": 0, "RBI": 0, "SB": 0, "CS": 0, "GIDP": 0, "PR": 0, "DEF": 0, "PH": 0}


def _blank_pitching():
    return {"G": 0, "GS": 0, "BF": 0, "outs": 0, "R": 0, "H": 0, "BB": 0, "IBB": 0, "K": 0, "HR": 0}


class Game:
    """One game.  ``log`` (a list) collects a line for every event with the numbers behind it."""

    def __init__(self, env: Environment, teams: tuple[Team, Team], starters: tuple[Pitcher, Pitcher],
                 rng: random.Random, stats: dict, log: list | None = None, cache: dict | None = None,
                 index: int = 0, tired: tuple | None = None, rest: bool = True, rest_runs: tuple | None = None):
        """``cache`` (the matchups' chances) can be shared by the games of a run: the same teams throughout.
        ``index``: which game of the run (who rests, :meth:`Team.day_lineup`); ``tired``: per side, the relievers
        who sit out (:func:`tired_relievers`); ``rest``: regulars may rest (not in a series).  ``rest_runs``: per side,
        ``{(bases, outs): [half-innings that scored 0, 1, ... 10+ runs from there on]}`` to add this game's to (the
        run's win expectancy, :class:`WinExpectancy`)."""
        self.env, self.teams, self.rng, self.stats, self.log = env, teams, rng, stats, log
        self.score = [0, 0]
        self.order = [0, 0]
        self.pitcher = list(starters)
        self.faced = [0, 0]        # batters faced by the pitcher now in, per fielding side
        self.limit = [starters[0].bf_start, starters[1].bf_start]
        self.starting = tuple(starters)
        self.used = [{starters[0].pid}, {starters[1].pid}]
        self.cache: dict = {} if cache is None else cache
        self.inning = 1
        self.last_batter = [None, None]
        # this game's batting orders and fielders: pinch runners and defensive replacements change them
        self.lineups = [list(teams[0].lineup), list(teams[1].lineup)]
        self.defense = [dict(teams[0].defense), dict(teams[1].defense)]
        self._on_field = [None, None]  # per fielding side: (pitcher, defense version, fielding state, running state)
        self._defense_version = [0, 0]
        self.gone = [set(), set()]      # players who have left the game, and bench men already used
        self.pending = [[], []]         # (position, batting place) to be settled when the side next takes the field
        self._two_way = [None, None]    # what a two-way starter changed, undone when he leaves the mound
        self.tired = tired or (frozenset(), frozenset())
        self.rest_runs = rest_runs
        self.relief_bf = [{}, {}]       # batters each reliever faced in this game, per side
        # per side at bat: plate appearances, batters who reached (on base or scored), outs at the plate, outs on the
        # bases (caught stealing, picked off, thrown out, the second out of a double play) -- the path analysis's chain
        self.tally = [[0, 0, 0, 0], [0, 0, 0, 0]]
        for side in (0, 1):             # rest days: today's regulars who sit, and who plays for them
            for place, regular, sub, pos in (teams[side].day_lineup(index, self.lineups[side], self.defense[side])
                                             if rest else ()):
                self.gone[side].add(regular.pid)
                self._say(kind="note", text=f"{regular.name} rests ({teams[side].short}); {sub.name} plays {pos} and "
                                            f"bats {_ordinal(place + 1)}",
                          detail=f"he started {1 - teams[side].rest[regular.pid]:.0%} of his club's games")
        for side in (0, 1):
            self._two_way_start(side, starters[side])
            for b in self.lineups[side]:
                if b is not None:
                    self._bstats(side, b.pid, b.name)["GS"] += 1
        # how each side took the field (for a box score)
        self.start_lineups = [list(self.lineups[0]), list(self.lineups[1])]
        self.start_defense = [dict(self.defense[0]), dict(self.defense[1])]
        for side in (0, 1):
            pst = self._pstats(side, starters[side])
            pst["G"] += 1
            pst["GS"] += 1

    # --- bookkeeping
    def _bstats(self, side, pid, name):
        return self.stats["batting"][side].setdefault(pid, dict(_blank_batting(), name=name))

    def _pstats(self, side, p: Pitcher):
        return self.stats["pitching"][side].setdefault(p.pid, dict(_blank_pitching(), name=p.name, role=p.role))

    def _say(self, **line):
        if self.log is not None:
            line.setdefault("inning", self.inning)
            line.setdefault("score", tuple(self.score))
            self.log.append(line)

    # --- two-way players
    def _two_way_start(self, side: int, p: Pitcher):
        """A two-way player starting on the mound keeps his own place in the order.  With the DH before the
        Ohtani rule (``OHTANI_RULE_FROM``) the club gives up the DH, so whoever relieves him bats there.  With
        no DH his place in the field goes to the bench's best fielder there (or its busiest hitter), who bats
        in the pitcher's spot; when he leaves the mound he goes back to his position and the reliever bats in
        the pitcher's spot."""
        team = self.teams[side]
        if p.pid not in team.two_way:
            return
        lineup = self.lineups[side]
        slot = next((i for i, b in enumerate(lineup) if b is not None and b.pid == p.pid), None)
        if slot is None:
            return  # not in the order: he starts like any pitcher
        if team.uses_dh:
            if team.season < OHTANI_RULE_FROM:
                self._two_way[side] = ("dh", slot, None, None)
                self._say(kind="note", text=f"{p.name} pitches and bats {_ordinal(slot + 1)}: {team.short} give up "
                                            "the DH, so whoever relieves him bats there")
            return
        spot = next((i for i, b in enumerate(lineup) if b is None), None)
        if spot is None:
            return
        pos = next((q for q, row in self.defense[side].items() if q != "P" and row["_id"] == p.pid), None)
        sub = None
        if pos:
            options = [(team.defense_rating(b.pid, pos) or 0.0, b) for b in self._bench_fielders(side, pos)]
            sub = max(options, key=lambda o: o[0])[1] if options else None
        if sub is None:
            in_game = {b.pid for b in lineup if b is not None}
            rest = [b for b in team.bench if b.pid not in self.gone[side] and b.pid not in in_game]
            sub = max(rest, key=lambda b: _num(b.row, "plateAppearances"), default=None)
        if sub is None:
            return
        own_row = self.defense[side].get(pos) if pos else None
        lineup[spot] = sub
        self.gone[side].add(sub.pid)
        if pos and pos in (team.fielding_by_player.get(sub.pid) or {}):
            self.defense[side][pos] = team.fielding_by_player[sub.pid][pos]
            self._defense_version[side] += 1
        self._two_way[side] = ("field", spot, pos, own_row)
        self._say(kind="note", text=f"{p.name} pitches and bats {_ordinal(slot + 1)}; {sub.name} "
                                    + (f"plays {pos} and " if pos else "") + f"bats {_ordinal(spot + 1)}")

    def _two_way_leaves(self, side: int, p: Pitcher, reliever: Pitcher):
        """Undo what :meth:`_two_way_start` changed, as the two-way starter leaves the mound."""
        change = self._two_way[side]
        self._two_way[side] = None
        kind, spot, pos, own_row = change
        if kind == "dh":
            self.lineups[side][spot] = None
            return
        if own_row is not None:
            sub = self.lineups[side][spot]
            self.defense[side][pos] = own_row
            self._defense_version[side] += 1
            self.lineups[side][spot] = None
            self._say(kind="note", text=f"{p.name} goes back to {pos} for {sub.name}; {reliever.name} bats "
                                        f"{_ordinal(spot + 1)}")

    # --- pitching changes
    def _choose_reliever(self, fside: int, lead: int, reason: str):
        team = self.teams[fside]
        used = self.used[fside]
        batting = {b.pid for b in self.lineups[fside] if b is not None}  # (a two-way player in the order stays there)
        tired = self.tired[fside]
        rested = lambda ps: [p for p in ps if p.pid not in used and p not in team.rotation and p.pid not in batting]
        fresh = lambda ps: [p for p in rested(ps) if p.pid not in tired]
        candidates, why = [], ""
        if self.inning >= 9 and 0 < lead <= 3 and team.closer and team.closer.pid not in used \
                and team.closer.pid not in tired:
            candidates, why = [team.closer], "closer: lead of 1-3 from the 9th"
        if not candidates and self.inning >= 7 and -1 <= lead <= 3:
            candidates, why = [p for p in fresh(team.roles["Closers"]) if p is not team.closer], \
                "other Closers: 7th inning on, score close"
        if not candidates and self.inning <= 5:
            candidates, why = fresh(team.roles["Long Relief"]), "Long Relief: starter gone before the 6th"
        if not candidates:
            candidates, why = fresh(team.roles["Other"] + team.roles["Long Relief"]), "Other / Long Relief"
        if not candidates:
            candidates, why = fresh(team.pitchers), "anyone left in the bullpen"
        if not candidates:
            candidates, why = rested(team.pitchers), "a tired reliever: nobody else left"
        if not candidates:
            return None
        weights = [max(p.relief_work, 1.0) for p in candidates]
        pick = self.rng.random() * sum(weights)
        for p, w in zip(candidates, weights):
            pick -= w
            if pick < 0:
                break
        self._say(kind="change", side=fside, pitcher=p.name, role=p.role, why=f"{reason}; {why}",
                  stint=p.bf_relief)
        return p

    def _bring_in(self, fside: int, p: Pitcher):
        if self._two_way[fside] is not None:
            self._two_way_leaves(fside, self.pitcher[fside], p)
        self.pitcher[fside] = p
        self.faced[fside] = 0
        self.limit[fside] = p.bf_relief
        self.used[fside].add(p.pid)
        self.relief_bf[fside].setdefault(p.pid, 0)
        self._pstats(fside, p)["G"] += 1

    def _check_pitcher(self, fside: int, start_of_inning: bool):
        bside = 1 - fside
        lead = self.score[fside] - self.score[bside]
        team = self.teams[fside]
        now = self.pitcher[fside]
        if start_of_inning and self.inning >= 9 and 0 < lead <= 3 and team.closer and now is not team.closer \
                and team.closer.pid not in self.used[fside] and team.closer.pid not in self.tired[fside] \
                and self.limit[fside] - self.faced[fside] < 4.3:
            self._say(kind="change", side=fside, pitcher=team.closer.name, role="Closers",
                      why=f"{now.name} would run out during the inning; closer for a lead of {lead}",
                      stint=team.closer.bf_relief)
            self._bring_in(fside, team.closer)
            self.limit[fside] = max(team.closer.bf_relief, 4.3)
            return
        if self.faced[fside] >= self.limit[fside]:
            p = self._choose_reliever(fside, lead, f"{now.name} reached {self.limit[fside]:.1f} batters")
            if p is not None:
                self._bring_in(fside, p)
            else:
                self.limit[fside] += now.bf_relief  # nobody left: he stays in

    # --- one plate appearance
    def _fielders(self, fside: int) -> tuple:
        """The fielding side's ``Team.fielding_state`` and ``Team.running_state`` for who is on the field now."""
        pitcher, version = self.pitcher[fside], self._defense_version[fside]
        got = self._on_field[fside]
        if got is None or got[0] is not pitcher or got[1] != version:
            team = self.teams[fside]
            got = self._on_field[fside] = (pitcher, version, team.fielding_state(self.defense[fside], pitcher),
                                           team.running_state(self.defense[fside], pitcher))
        return got[2], got[3]

    def _probabilities(self, bside: int, batter, pitcher: Pitcher):
        """The chances of each result -> ``(chances, where)`` (see :func:`apply_fielding`)."""
        fielders = self._fielders(1 - bside)[0] if pitcher is self.pitcher[1 - bside] else \
            self.teams[1 - bside].fielding_state(self.defense[1 - bside], pitcher)
        key = (bside, batter.pid if batter is not None else ("P", self.pitcher[bside].pid), pitcher.pid, fielders)
        p = self.cache.get(key)
        if p is None:
            bteam, fteam = self.teams[bside], self.teams[1 - bside]
            if batter is None:
                own = self.pitcher[bside]
                bats, b_rates, b_platoon = own.bats, own.bat_rates or bteam.league, own.bat_platoon
            else:
                bats, b_rates, b_platoon = batter.bats, batter.rates, batter.platoon
            b_rates, p_rates, env_rates, _hands = platoon_matchup(bats, b_rates, b_platoon, pitcher, bteam, fteam,
                                                                  self.env)
            env_rates = self.env.with_edge(env_rates, bside == 1)
            mixed = log5(b_rates, bteam.league, p_rates, fteam.league, env_rates)
            chances, where = apply_fielding(mixed, self.env.tables, self.env.zone_factors(fielders))
            spread = {}  # each kind of ball in play's zones and their cumulative chances, to find one by bisection
            for cls, spots in where.items():
                cum, acc = array("d"), 0.0
                for _z, c in spots:
                    acc += c
                    cum.append(acc)
                zones = tuple(z for z, _c in spots)
                spread[cls] = (_ZONE_LISTS.setdefault(zones, zones), cum)
            if len(self.cache) >= MATCHUP_CACHE_MAX:
                trim_cache(self.cache)
            p = self.cache[key] = (chances, spread)
        return p

    def _fielded_by(self, cls: str, spread: dict, u: float) -> tuple[str, str]:
        """Where a ball in play of kind ``cls`` went and who picked it up, chosen by ``u`` (0-1) in proportion to
        the chances -> (zone, position 1-9 or "")."""
        zones, cum = spread.get(cls) or ((), ())
        if not zones:
            return "", ""
        x = u * cum[-1]
        i = min(bisect.bisect_right(cum, x), len(zones) - 1)
        low = cum[i - 1] if i else 0.0
        within = (x - low) / (cum[i] - low) if cum[i] > low else 0.0
        return zones[i], self.env.tables.picked_up(cls, zones[i], min(within, 0.999999))

    def _fielder_name(self, fside: int, digit: str) -> str:
        pos = ZONE_POSITIONS[digit]
        name = self.pitcher[fside].name if pos == "P" else (self.defense[fside].get(pos) or {}).get("_name", "?")
        return f"{pos} {name}"

    def _advance(self, bside, bases, outcome, cls, batter_runner, cap):
        """Move the runners as ``outcome`` says -> (new bases, runs, outs, scorers)."""
        new = [None, None, None]
        scorers, outs = [], 0
        movers = [batter_runner] + bases
        # runners from third first, so a walk-off cap counts the lead runner
        for idx in (3, 2, 1, 0):
            d = outcome[idx]
            who = movers[idx]
            if d == "-" or who is None:
                continue
            if d == "X":
                outs += 1
            elif d == "H":
                if cap is not None and len(scorers) >= cap:
                    new_base = None  # the game is already over
                else:
                    scorers.append(who)
            else:
                new[int(d) - 1] = who
        return new, scorers, outs

    def _score(self, bside, scorers, rbi_to=None):
        fside = 1 - bside
        for who in scorers:
            self.score[bside] += 1
            self._bstats(bside, who.pid, who.name)["R"] += 1
            if who.pitcher is not None:
                self.stats["pitching"][fside].setdefault(
                    who.pitcher.pid, dict(_blank_pitching(), name=who.pitcher.name, role=who.pitcher.role))["R"] += 1
        if rbi_to is not None and scorers:
            rbi_to["RBI"] += len(scorers)

    def _walkoff_cap(self, bside, cls):
        """How many more runs count: in the bottom of the 9th or later the game ends with the winning run
        (all of them on a home run)."""
        if bside == 1 and self.inning >= 9 and cls != "HR":
            return max(self.score[0] - self.score[1] + 1, 0)
        return None

    def _between(self, bside, bases, outs):
        """Steal attempts and wild pitches/balks/pickoffs before the pitch that ends the plate appearance."""
        env, rng = self.env, self.rng
        fside = 1 - bside
        for b in (1, 2, 3):
            runner = bases[b - 1]
            if runner is None or (b < 3 and bases[b] is not None) or b not in env.tables.steal:
                continue
            att, succ = env.tables.steal[b]
            # the runner against the catcher and the pitcher holding him, each against his league, in odds
            against = self._fielders(fside)[1]
            f_att, f_succ = against["steal"]
            f_tag = against["tag"].get(b, 1.0)  # the infielders covering the base
            p_att = tilt(att, runner.running["att"] * f_att)
            if rng.random() >= p_att:
                continue
            p_succ = tilt(succ, runner.running["succ"] * f_succ * f_tag)
            st = self._bstats(bside, runner.pid, runner.name)
            if rng.random() < p_succ:
                st["SB"] += 1
                bases[b - 1] = None
                if b == 3:
                    cap = self._walkoff_cap(bside, "SB")
                    if cap is None or cap > 0:
                        self._score(bside, [runner])
                else:
                    bases[b] = runner
                self._say(kind="steal", text=f"{runner.name} steals {('second', 'third', 'home')[b - 1]}",
                          detail=f"tries {p_att:.1%} of the time from {('first', 'second', 'third')[b - 1]}, "
                                 f"safe {p_succ:.1%} (his own \u00d7{runner.running['att']:.2f} and "
                                 f"\u00d7{runner.running['succ']:.2f}; the battery's \u00d7{f_att:.2f} and "
                                 f"\u00d7{f_succ:.2f}; the infielders covering \u00d7{f_tag:.2f})")
            else:
                st["CS"] += 1
                self.tally[bside][3] += 1
                bases[b - 1] = None
                outs += 1
                self._pstats(fside, self.pitcher[fside])["outs"] += 1
                self._say(kind="steal", text=f"{runner.name} caught stealing {('second', 'third', 'home')[b - 1]}",
                          detail=f"tries {p_att:.1%}, safe {p_succ:.1%} (his own \u00d7{runner.running['att']:.2f} "
                                 f"and \u00d7{runner.running['succ']:.2f}; the battery's \u00d7{f_att:.2f} and "
                                 f"\u00d7{f_succ:.2f}; the infielders covering \u00d7{f_tag:.2f})")
            return bases, outs
        state = (1 if bases[0] else 0) | (2 if bases[1] else 0) | (4 if bases[2] else 0)
        rate = env.tables.adv_rate.get((state, outs))
        if state and rate and rng.random() < rate:
            entries = env.tables.adv[(state, outs)]
            total = sum(e[1] for e in entries)
            pick = rng.random() * total
            for outcome, count, *_ in entries:
                pick -= count
                if pick < 0:
                    break
            new, scorers, made = self._advance(bside, bases, outcome, "ADV", None, self._walkoff_cap(bside, "ADV"))
            self.tally[bside][3] += made
            self._score(bside, scorers)
            self._pstats(fside, self.pitcher[fside])["outs"] += made
            self._say(kind="adv", text="Wild pitch / passed ball / balk / pickoff: " + _describe(outcome, bases, None),
                      detail=f"happens {rate:.2%} of plate appearances in this base-out state; this outcome "
                             f"{count} of {total} times")
            return new, outs + made
        return bases, outs

    # --- intentional walks and sacrifice bunts
    def _situational(self, bside: int, batter, state: int, outs: int) -> tuple[str | None, float]:
        """Before the pitch: is the batter walked intentionally, or does he sacrifice?  The league's rate in this
        situation (base-out state, stage of the game, margin; :class:`Situations`) tilted by his own against his
        league's, in odds -> ``("IBB" or "SH" or None, the chance it had)``.  A sacrifice only with a runner on
        first or second and fewer than two out."""
        team = self.teams[bside]
        sits = self.env.tables.situations or team.situations
        if sits is None:
            return None, 0.0
        ibb_f, sac_f = (batter.ibb, batter.sac) if batter is not None else team.pitchers_sit
        p_ibb, p_sac = sits.chances(state, outs, self.inning, self.score[bside] - self.score[1 - bside])
        # the table is all MLB's: scaled to the home league's own rates (each player is against his league, then
        # placed in the home team's)
        home = self.env.home if self.env.tables.situations else team
        lg_ibb, lg_sac = sits.league
        p_ibb *= home.league_ibb / lg_ibb if lg_ibb and home.decides_ibb else 1.0
        p_sac *= home.league_sac / lg_sac if lg_sac and home.decides_sac else 1.0
        if ibb_f is not None and team.decides_ibb and p_ibb > 0 \
                and "intentional_walks" not in self.teams[1 - bside].off:  # (the fielding side's call)
            p = tilt(p_ibb, ibb_f)
            if self.rng.random() < p:
                return "IBB", p
        if sac_f is not None and team.decides_sac and "sacrifices" not in team.off and p_sac > 0 and outs < 2 and state & 3 and (state, outs) in sits.sac:
            p = tilt(p_sac, sac_f)
            if self.rng.random() < p:
                return "SH", p
        return None, 0.0

    # --- pinch hitters
    def _pinch_hit(self, bside: int):
        """The pitcher's spot comes up: a reliever is pinch-hit for, and so is the starter from the
        ``PINCH_HIT_FROM_INNING`` (he then leaves the game) -- by the bench's busiest hitter not yet used, as long
        as there is one -> him, or None for the pitcher to bat.  (In the real no-DH game about half the pitcher's
        spot's plate appearances went to pinch hitters.)  The relief pitcher stays on the mound: a double switch,
        in effect."""
        own = self.pitcher[bside]
        starter = own is self.starting[bside]
        if (starter and self.inning < PINCH_HIT_FROM_INNING) or "pinch_hitting" in self.teams[bside].off:
            return None
        team = self.teams[bside]
        in_game = {b.pid for b in self.lineups[bside] if b is not None}
        options = [b for b in team.bench if b.pid not in self.gone[bside] and b.pid not in in_game]
        if not options:
            return None
        ph = max(options, key=lambda b: (_num(b.row, "plateAppearances"), b.name))
        self.gone[bside].add(ph.pid)
        if starter:
            self.limit[bside] = self.faced[bside]  # his day is done: a reliever takes over next half-inning
        self._bstats(bside, ph.pid, ph.name)["PH"] += 1
        self._say(kind="note", text=f"Pinch hitter: {ph.name} bats for {own.name}"
                                    + ("" if not starter else f" ({_ordinal(self.inning)} inning: the starter is done)"))
        return ph

    def _pinch_hit_regular(self, bside: int, slot: int, batter, pitcher: Pitcher):
        """From the ``PINCH_HIT_FROM_INNING``, with his side between ``PH_LEAD`` runs behind and ahead, a batter is
        pinch-hit for by the bench hitter not yet used whose value against this pitcher (:func:`matchup_value` of
        the games' own chances: platoon, park, fielders and all) beats his by ``PH_EDGE`` -- if somebody can play
        his position afterwards (the best glove there then takes it, as after a pinch runner) -> who bats."""
        lead = self.score[bside] - self.score[1 - bside]
        if self.inning < PINCH_HIT_FROM_INNING or not PH_LEAD[0] <= lead <= PH_LEAD[1] \
                or "pinch_hitting" in self.teams[bside].off \
                or batter.pid == self.pitcher[bside].pid:
            return batter
        team = self.teams[bside]
        in_game = {b.pid for b in self.lineups[bside] if b is not None}
        options = [b for b in team.bench if b.pid not in self.gone[bside] and b.pid not in in_game
                   and _num(b.row, "plateAppearances") >= PH_MIN_PA]
        if not options:
            return batter
        mine = matchup_value(self._probabilities(bside, batter, pitcher)[0])
        value, ph = max(((matchup_value(self._probabilities(bside, b, pitcher)[0]), b) for b in options),
                        key=lambda t: (t[0], t[1].name))
        if value - mine < PH_EDGE:
            return batter
        pos = next((q for q, row in self.defense[bside].items() if q != "P" and row["_id"] == batter.pid), None)
        if pos and pos not in (team.fielding_by_player.get(ph.pid) or {}) and \
                not [x for x in self._bench_fielders(bside, pos) if x is not ph]:
            return batter  # nobody could take his position afterwards
        self.lineups[bside][slot] = ph
        self.gone[bside].update((batter.pid, ph.pid))
        if pos:
            self.pending[bside].append((pos, slot, batter.name))
        self._bstats(bside, ph.pid, ph.name)["PH"] += 1
        self._say(kind="note", text=f"Pinch hitter: {ph.name} bats for {batter.name}",
                  detail=f"{_ordinal(self.inning)} inning, {'up' if lead > 0 else 'down' if lead < 0 else 'tied'}"
                         f"{f' {abs(lead)}' if lead else ''}: worth {value:.3f} runs a plate appearance against "
                         f"{pitcher.name}, {batter.name} {mine:.3f}")
        return ph

    # --- pinch runners and defensive replacements
    def _bench_fielders(self, side: int, pos: str) -> list:
        """Bench players not yet used who played ``pos`` that season."""
        team = self.teams[side]
        in_game = {b.pid for b in self.lineups[side] if b is not None}
        return [b for b in team.bench if b.pid not in self.gone[side] and b.pid not in in_game
                and pos in (team.fielding_by_player.get(b.pid) or {})]

    def _pinch_run(self, bside: int, bases: list):
        """From the 8th inning, with the game within two runs, a Slow regular on base gives way to the first bench
        runner available in the Speed tab's order -- if somebody can play his position afterwards."""
        if self.inning < PINCH_RUN_FROM_INNING or abs(self.score[0] - self.score[1]) > PINCH_RUN_MARGIN:
            return
        team = self.teams[bside]
        if not team.slow or not team.pinch_runners:
            return
        lineup = self.lineups[bside]
        for b in range(3):
            runner = bases[b]
            if runner is None or runner.pid not in team.slow or runner.pid in self.gone[bside]:
                continue
            slot = next((i for i, x in enumerate(lineup) if x is not None and x.pid == runner.pid), None)
            if slot is None:
                continue
            in_game = {x.pid for x in lineup if x is not None}
            sub = next((p for p in team.pinch_runners if p.pid not in self.gone[bside] and p.pid not in in_game), None)
            if sub is None:
                return
            pos = next((pos for pos, row in self.defense[bside].items() if row["_id"] == runner.pid), None)
            if pos == "P":
                continue
            if pos and pos not in (team.fielding_by_player.get(sub.pid) or {}) and \
                    not [x for x in self._bench_fielders(bside, pos) if x is not sub]:
                continue  # nobody could take his position afterwards
            bases[b] = _Runner(sub.pid, sub.name, sub.running, runner.pitcher)
            lineup[slot] = sub
            self.gone[bside].update((runner.pid, sub.pid))
            if pos:
                self.pending[bside].append((pos, slot, runner.name))
            self._bstats(bside, sub.pid, sub.name)["PR"] += 1
            self._say(kind="note", text=f"Pinch runner: {sub.name} ({sub.group}) runs for {runner.name} "
                                        f"(Slow regular) on {('first', 'second', 'third')[b]}",
                      detail=f"8th inning or later, game within {PINCH_RUN_MARGIN} runs")

    def _settle_defense(self, side: int):
        """A side taking the field after pinch-running: at each position the pinch runner's man played, whoever
        has the best fielding record there -- the pinch runner, or a bench player who played it -- plays it."""
        team = self.teams[side]
        for pos, slot, replaced in self.pending[side]:
            runner = self.lineups[side][slot]
            options = [(team.defense_rating(b.pid, pos), b) for b in self._bench_fielders(side, pos)]
            if pos in (team.fielding_by_player.get(runner.pid) or {}):
                options.append((team.defense_rating(runner.pid, pos), runner))
            if not options:
                continue
            rating, best = max(options, key=lambda o: (o[0] is not None, o[0] or 0, o[1] is runner))
            mine = next((r for r, b in options if b is runner), None)
            if best is not runner:
                self.lineups[side][slot] = best
                self.gone[side].add(best.pid)
                self._bstats(side, best.pid, best.name)["DEF"] += 1
                self._say(kind="note", text=f"Defensive replacement: {best.name} takes {pos} (and {runner.name}'s "
                                            f"place in the order)",
                          detail=f"his record at {pos}: {_pct(rating)} of MLB's chances; {runner.name}: "
                                 + (_pct(mine) if mine is not None else f"never played {pos}"))
            else:
                self._say(kind="note", text=f"{runner.name} stays in at {pos} for {replaced}",
                          detail=f"his record there, {_pct(rating)} of MLB's chances, is the best on the bench")
            self.defense[side][pos] = team.fielding_by_player[best.pid][pos]
            self._defense_version[side] += 1
        self.pending[side] = []

    def _defensive_changes(self, side: int):
        """Taking the field from the ``DEF_FROM_INNING`` with a lead of ``DEF_LEAD``: the bench man with the best
        record at some position (:meth:`Team.defense_rating`, on ``DEF_MIN_INNINGS`` or more) replaces the fielder
        there if he beats him by ``DEF_EDGE`` of MLB's chances -- the biggest such gap, one a half-inning -- and bats
        in his place."""
        lead = self.score[side] - self.score[1 - side]
        if self.inning < DEF_FROM_INNING or not DEF_LEAD[0] <= lead <= DEF_LEAD[1] \
                or "defensive_subs" in self.teams[side].off:
            return
        team = self.teams[side]
        lineup = self.lineups[side]
        moves = []
        for pos in OUTFIELD + ("C",):
            row = self.defense[side].get(pos)
            slot = next((i for i, b in enumerate(lineup) if b is not None and row is not None and b.pid == row["_id"]),
                        None)
            if slot is None or row["_id"] == self.pitcher[side].pid:
                continue
            mine = team.defense_rating(row["_id"], pos)
            options = [(team.defense_rating(b.pid, pos), b) for b in self._bench_fielders(side, pos)
                       if mlbstats.innings_to_outs(team.fielding_by_player[b.pid][pos].get("innings", 0)) / 3
                       >= DEF_MIN_INNINGS]
            options = [(r, b) for r, b in options if r is not None]
            if mine is None or not options:
                continue
            rating, best = max(options, key=lambda o: (o[0], o[1].name))
            if rating - mine >= DEF_EDGE:
                moves.append((rating - mine, pos, slot, row, mine, rating, best))
        if moves:
            _gap, pos, slot, row, mine, rating, best = max(moves, key=lambda m: (m[0], m[1]))
            lineup[slot] = best
            self.gone[side].update((row["_id"], best.pid))
            self.defense[side][pos] = team.fielding_by_player[best.pid][pos]
            self._defense_version[side] += 1
            self._bstats(side, best.pid, best.name)["DEF"] += 1
            self._say(kind="note", text=f"Defensive replacement: {best.name} takes {pos} for {row.get('_name', '?')}, "
                                        f"batting {_ordinal(slot + 1)}",
                      detail=f"up {lead} in the {_ordinal(self.inning)}: his record at {pos} {_pct(rating)} of MLB's "
                             f"chances, {row.get('_name', '?')}'s {_pct(mine)}")

    def half_inning(self, bside: int) -> bool:
        """Play one half-inning -> whether the game is over (walk-off)."""
        fside = 1 - bside
        team = self.teams[bside]
        bases = [None, None, None]
        outs = 0
        if self.pending[fside]:
            self._settle_defense(fside)
        self._defensive_changes(fside)
        self._check_pitcher(fside, True)
        seen = []  # (bases, outs, runs so far) before each plate appearance, for the run's win expectancy
        start_runs = self.score[bside]
        if self.inning > 9 and self.env.placed_runner:
            prev = self.last_batter[bside]
            if prev is not None:
                bases[1] = _Runner(prev[0], prev[1], prev[2], self.pitcher[fside])
                self._say(kind="note", text=f"{prev[1]} placed on second (extra-inning rule)")
        while outs < 3:
            self._check_pitcher(fside, False)
            self._pinch_run(bside, bases)
            bases, outs = self._between(bside, bases, outs)
            if outs >= 3 or (bside == 1 and self.inning >= 9 and self.score[1] > self.score[0]):
                break
            slot = self.order[bside]
            batter = self.lineups[bside][slot]
            pitcher = self.pitcher[fside]
            if batter is None:
                batter = self._pinch_hit(bside)
            else:
                batter = self._pinch_hit_regular(bside, slot, batter, pitcher)
            if batter is None:
                own = self.pitcher[bside]
                pid, name, running = own.pid, own.name, AVERAGE_RUNNER
                gdp = 1.0
            else:
                pid, name, running, gdp = batter.pid, batter.name, batter.running, batter.gdp
            score_before = tuple(self.score)
            state = (1 if bases[0] else 0) | (2 if bases[1] else 0) | (4 if bases[2] else 0)
            if self.rest_runs is not None and (bside == 0 or self.inning < 9):  # (a walk-off cuts the half short)
                seen.append((state, outs, self.score[bside]))
            special, chance = self._situational(bside, batter, state, outs)
            zone = first = ""
            arm = turn = 1.0
            factors = (1.0, 1.0, 1.0)
            if special == "IBB":
                cls, probs, roll = special, {"IBB": chance}, None
                outcome, share, count, total = default_outcome("BB", state), 1.0, 0, 0
            elif special == "SH":
                sits = self.env.tables.situations or self.teams[bside].situations
                cls, probs, roll = special, {"SH": chance}, None
                outcome, share, count, total = sits.draw_sac(state, outs, self.rng)
            else:
                probs, spread = self._probabilities(bside, batter, pitcher)
                roll = self.rng.random()
                acc = 0.0
                cls = CLASSES[-1]
                for c in CLASSES:
                    acc += probs.get(c, 0.0)
                    if roll < acc:
                        cls = c
                        break
                # where the ball went and who picked it up: from where in the class's share of the roll it fell
                if cls in FIELDED and probs.get(cls):
                    zone, first = self._fielded_by(cls, spread, min(max((roll - acc + probs[cls]) / probs[cls], 0.0),
                                                                    0.999999))
                # the runners against him, each side against its league, in odds (as batter and pitcher are)
                against = self._fielders(fside)[1].get(ZONE_POSITIONS.get(first)) if first else None
                key = {"1B": "xb", "2B": "xb", "AO": "tu"}.get(cls)
                arm = (against[0] if key == "xb" else against[1]) if (against and key) else 1.0
                factors = tuple(b.running[key] * arm if (b is not None and key) else 1.0 for b in bases)
                turn = against[2] if (against and cls == "GO") else 1.0
                outcome, share, count, total = self.env.tables.draw(cls, state, outs, factors, gdp * turn, self.rng)
            batter_runner = _Runner(pid, name, running, pitcher)
            cap = self._walkoff_cap(bside, cls)
            before = list(bases)
            new, scorers, made = self._advance(bside, bases, outcome, cls, batter_runner, cap)
            # statistics
            st = self._bstats(bside, pid, name)
            pst = self._pstats(fside, pitcher)
            st["PA"] += 1
            batter_out = outcome[0] == "X"
            tally = self.tally[bside]
            tally[0] += 1
            tally[1 if not batter_out else 2] += 1
            tally[3] += made - batter_out
            pst["BF"] += 1
            self.faced[fside] += 1
            if pitcher.pid in self.relief_bf[fside]:
                self.relief_bf[fside][pitcher.pid] += 1
            sac_fly = cls == "AO" and outcome[0] == "X" and scorers and outs < 2
            if cls in ("1B", "2B", "3B", "HR"):
                st["H"] += 1
                st[cls] += 1
                pst["H"] += 1
                if cls == "HR":
                    pst["HR"] += 1
            elif cls in ("BB", "HBP"):
                st[cls] += 1
                if cls == "BB":
                    pst["BB"] += 1
            elif cls == "IBB":
                st["BB"] += 1
                st["IBB"] += 1
                pst["BB"] += 1
                pst["IBB"] += 1
            elif cls == "SH":
                st["SH"] += 1
            elif cls == "K":
                st["K"] += 1
                pst["K"] += 1
            elif cls == "ROE":
                st["ROE"] += 1
            if sac_fly:
                st["SF"] += 1
            if cls not in ("BB", "HBP", "IBB", "SH") and not sac_fly:
                st["AB"] += 1
            double_play = cls == "GO" and made >= 2
            if double_play:
                st["GIDP"] += 1
            self._score(bside, scorers, None if (cls == "ROE" or double_play) else st)
            pst["outs"] += made
            outs += made
            self.order[bside] = (slot + 1) % 9
            self.last_batter[bside] = (pid, name, running)
            if self.log is not None:
                bats = batter.bats if batter is not None else self.pitcher[bside].bats
                hands = matchup_hands(bats, pitcher.throws) \
                    if self.env.platoon and all(t.platoon for t in self.teams) else None
                self._say(kind="pa", side=bside, slot=slot + 1, batter=name, pitcher=pitcher.name, hands=hands,
                          zone=zone, fielders=[self._fielder_name(fside, d) for d in zone],
                          picked=self._fielder_name(fside, first) if first else "", arm=arm, turn=turn,
                          outs=outs - made, bases=before, probs=probs, roll=roll, cls=cls, outcome=outcome,
                          share=share, count=count, total=total, factors=factors, gdp=gdp,
                          text=_describe(outcome, before, name), runs=len(scorers), score=score_before)
            bases = new
            if bside == 1 and self.inning >= 9 and self.score[1] > self.score[0]:
                return True
        if seen:
            book = self.rest_runs[bside]
            end = self.score[bside]
            for state, outs_then, so_far in seen:
                book.setdefault((state, outs_then), [0] * 11)[min(end - so_far, 10)] += 1
        return False

    def play(self):
        while True:
            self.half_inning(0)
            if self.inning >= 9 and self.score[1] > self.score[0]:
                break  # the home team doesn't need its half
            if self.half_inning(1):
                break
            if self.inning >= 9 and self.score[0] != self.score[1]:
                break
            if self.inning >= MAX_INNINGS:
                break
            self.inning += 1
        return self.score


def _describe(outcome: str, bases, batter: str | None) -> str:
    words = {"1": "first", "2": "second", "3": "third", "H": "scores", "X": "out"}
    parts = []
    if batter is not None and outcome[0] != "-":
        d = outcome[0]
        parts.append(f"{batter} " + ("out" if d == "X" else "scores" if d == "H" else f"to {words[d]}"))
    for i, b in enumerate((1, 2, 3), start=1):
        runner = bases[b - 1]
        d = outcome[i]
        if runner is None or d == "-":
            continue
        if d == str(b):
            parts.append(f"{runner.name} holds {words[d]}")
        elif d in "HX":
            parts.append(f"{runner.name} {words[d]}")
        else:
            parts.append(f"{runner.name} to {words[d]}")
    return "; ".join(parts)


def game_seed(seed: int, index: int) -> int:
    return seed * 1_000_003 + index


def host_of(g: int) -> int:
    """Which team (0 = Team 1, 1 = Team 2) is at home in game ``g`` (0-based): they take turns, Team 2 first, so an
    even number of games is half and half."""
    return 1 if g % 2 == 0 else 0


def tired_relievers(team: Team, recent: list) -> frozenset:
    """The relievers who sit out a team's next game: ``recent`` is its last games' ``Game.relief_bf`` (batters each
    reliever faced), oldest first.  One who pitched in both of the last two, or faced ``TIRED_LONG_STINT`` times his
    usual relief batters (and at least ``TIRED_MIN_BATTERS``) in the last."""
    if not team.fatigue or not recent or "tired_relievers" in team.off:
        return frozenset()
    out = set(recent[-1]) & set(recent[-2]) if len(recent) >= 2 else set()
    stint = {p.pid: p.bf_relief for p in team.pitchers}
    out |= {pid for pid, bf in recent[-1].items() if bf >= max(TIRED_MIN_BATTERS, TIRED_LONG_STINT * stint.get(pid, 4.3))}
    return frozenset(out)


def _environments(env) -> tuple:
    """``env`` as (the environment when Team 1 hosts, when Team 2 hosts): one Environment serves both."""
    return env if isinstance(env, tuple) else (env, env)


CHAIN_COUNTS = ("PA", "reached", "outs_plate", "outs_bases")  # a team's game at bat, counted (``Game.tally``)
SERIES_FORMATS = {7: "2-3-2", 5: "2-2-1"}  # best of N: the higher seed's home games in its pattern
SERIES_ROTATION = 4  # starters a club goes through in a series (its busiest, in turn)


def series_host(best_of: int, k: int) -> bool:
    """Is game ``k`` (0-based) of a best-of-``best_of`` series at the higher seed's park?  2-3-2: games 1, 2, 6, 7;
    2-2-1: games 1, 2, 5."""
    blocks = [int(x) for x in SERIES_FORMATS[best_of].split("-")]
    at_home, i = True, k
    for b in blocks:
        if i < b:
            return at_home
        i -= b
        at_home = not at_home
    return at_home


def higher_seed(team1: Team, team2: Team) -> int:
    """Which team (0 or 1) has home field in a series: the better real record (its pitchers' wins and losses);
    Team 2 if they're level."""
    pct = []
    for t in (team1, team2):
        w = sum(_num(r, "wins") for r in t.raw["pitching"])
        lost = sum(_num(r, "losses") for r in t.raw["pitching"])
        pct.append(w / (w + lost) if w + lost else 0.5)
    return 0 if pct[0] > pct[1] else 1


def series_rotation(team: Team) -> list:
    """Who starts a series' games in turn: your rotation as you set it, else the ``SERIES_ROTATION`` busiest of
    the Rotation by real starts."""
    if team.custom_rotation:
        return list(team.rotation)
    return sorted(team.rotation, key=lambda p: (-_num(p.row, "gamesStarted"), p.name))[:SERIES_ROTATION]


def simulate(team1: Team, team2: Team, env, games: int, seed: int, progress=None, series: int | None = None,
             cancelled=None) -> dict:
    """Play ``games`` games, the two teams taking turns at home (:func:`host_of`), each game in its home team's
    environment (``env``: one for both, or a pair -- when Team 1 hosts, when Team 2 hosts) -> totals, each by team
    (0 = Team 1, 1 = Team 2): wins, runs, every batter's and pitcher's line, run distributions, and ``hosts`` (who
    was at home in each game), ``home`` and ``road`` records, ``starter_ids`` (each game's two starters), ``tired``.

    With ``series`` (``SERIES_FORMATS``: 7 or 5) ``games`` is a number of best-of-``series`` series instead: the
    better record (:func:`higher_seed`) at home in its pattern (:func:`series_host`), each club's
    :func:`series_rotation` in turn from game 1, no rest days, and tired relievers carried through a series, rested
    by each travel day (a change of park).  ``series`` in the result: ``{"best_of", "count", "seed_team", "wins":
    [series won], "lengths": {(winner, games): count}}``; ``games`` is then the games played.

    ``cancelled`` (a function), asked every 1% of the games: once it says so the run stops with :class:`Cancelled`
    (a newer run has replaced it, and nobody will see this one)."""
    envs = _environments(env)
    teams = (team1, team2)
    stats = {"batting": ({}, {}), "pitching": ({}, {})}
    wins = [0, 0]
    ties = extra = 0
    runs = ([], [])
    innings_total = 0
    margin = {}       # Team 2's runs less Team 1's
    starter_record = ({}, {})  # {pitcher id: [team wins, team losses]} in his starts
    shutouts = [0, 0]
    hosts, starter_ids = [], []
    home, road = ([0, 0], [0, 0]), ([0, 0], [0, 0])  # per team: [wins, losses] at home, on the road
    caches: tuple = ({}, {})  # the matchups' chances, per home team (its environment)
    recent: tuple = ([], [])  # each team's last two games' relievers (fatigue)
    tired_log = []            # per game, each team's relievers who sat out tired
    rest_runs: tuple = ({}, {})  # per team, runs to the end of the half-inning from each base-out state
    lines = tuple({k: array("i") for k in CHAIN_COUNTS} for _t in (0, 1))  # per team at bat, game by game

    def play(g: int, host: int, pitchers: tuple, rest: bool) -> int | None:
        """One game -> the winning team (None: a tie)."""
        nonlocal ties, extra, innings_total
        # Step aside for anyone waiting for the interpreter, once a game.  Left to itself this thread kept the window's
        # thread waiting for the interpreter lock for 1-3 s at a time (Python 3.14 on Linux), long enough with a path
        # analysis's minutes of replays for the desktop to call the app unresponsive; a zero-length sleep releases it.
        time.sleep(0)
        order = (1 - host, host)  # (visitor, home) as team indices
        tired = tuple(tired_relievers(teams[t], recent[t]) for t in (0, 1))
        tired_log.append(tired)
        game_stats = {k: (stats[k][order[0]], stats[k][order[1]]) for k in ("batting", "pitching")}
        game = Game(envs[host], (teams[order[0]], teams[order[1]]), (pitchers[order[0]], pitchers[order[1]]),
                    random.Random(game_seed(seed, g)), game_stats, cache=caches[host], index=g,
                    tired=(tired[order[0]], tired[order[1]]), rest=rest,
                    rest_runs=(rest_runs[order[0]], rest_runs[order[1]]))
        v, h = game.play()
        for i, t in enumerate(order):
            recent[t].append(game.relief_bf[i])
            del recent[t][:-2]
        score = {order[0]: v, order[1]: h}
        hosts.append(host)
        for i, t in enumerate(order):
            for k, x in zip(CHAIN_COUNTS, game.tally[i]):
                lines[t][k].append(x)
        starter_ids.append((pitchers[0].pid, pitchers[1].pid))
        for t in (0, 1):
            mine, theirs = score[t], score[1 - t]
            rec = starter_record[t].setdefault(pitchers[t].pid, [0, 0])
            if mine != theirs:
                rec[0 if mine > theirs else 1] += 1
                where = home if t == host else road
                where[t][0 if mine > theirs else 1] += 1
                if mine > theirs:
                    wins[t] += 1
            if theirs == 0:
                shutouts[t] += 1
            runs[t].append(mine)
        if score[0] == score[1]:
            ties += 1
        innings_total += game.inning
        if game.inning > 9:
            extra += 1
        margin[score[1] - score[0]] = margin.get(score[1] - score[0], 0) + 1
        return None if score[0] == score[1] else (0 if score[0] > score[1] else 1)

    step = max(1, games // 100)  # report progress every 1% of the games (or series)
    played = 0
    out_series = None
    if not series:
        starters = (team1.starters(games), team2.starters(games))
        for g in range(games):
            play(g, host_of(g), (starters[0][g], starters[1][g]), True)
            if (g + 1) % step == 0 and cancelled is not None and cancelled():
                raise Cancelled("stopped: a newer run replaced it")
            if progress is not None and ((g + 1) % step == 0 or g + 1 == games):
                progress(g + 1)
        played = games
    else:
        seeded = higher_seed(team1, team2)
        rotations = (series_rotation(team1), series_rotation(team2))
        need = series // 2 + 1
        won_series, lengths = [0, 0], {}
        for n in range(games):
            won, k, last_host = [0, 0], 0, None
            for t in (0, 1):
                recent[t].clear()  # days off before a series
            while max(won) < need and k < 3 * series:  # (a tie is replayed: it doesn't count)
                host = seeded if series_host(series, min(k, series - 1)) else 1 - seeded
                if last_host is not None and host != last_host:
                    for t in (0, 1):
                        recent[t].clear()  # a travel day
                winner = play(played, host, tuple(rotations[t][k % len(rotations[t])] for t in (0, 1)), False)
                if winner is not None:
                    won[winner] += 1
                last_host = host
                k += 1
                played += 1
            if max(won) >= need:
                w = 0 if won[0] > won[1] else 1
                won_series[w] += 1
                lengths[(w, won[0] + won[1])] = lengths.get((w, won[0] + won[1]), 0) + 1
            if (n + 1) % step == 0 and cancelled is not None and cancelled():
                raise Cancelled("stopped: a newer run replaced it")
            if progress is not None and ((n + 1) % step == 0 or n + 1 == games):
                progress(n + 1)
        out_series = {"best_of": series, "count": games, "seed_team": seeded, "wins": won_series, "lengths": lengths}
    return {"games": played, "wins": wins, "ties": ties, "extra": extra, "runs": runs,
            "innings": innings_total / played if played else 0, "margin": margin, "stats": stats, "seed": seed,
            "starter_record": starter_record, "shutouts": shutouts, "hosts": hosts, "home": home, "road": road,
            "tired": tired_log, "starter_ids": starter_ids, "series": out_series, "rest_runs": rest_runs,
            "lines": lines}


class WinExpectancy:
    """The home team's chance of winning from any point of a game, worked out from a run's own scoring:
    ``rest_runs`` (the run's, per team) gives how many runs each team scored from each base-out state to the end
    of the half-inning; a fresh half-inning is bases empty, none out (from 2020, extra innings a runner on second).
    From there it's a sum over the half-innings left; a game tied after nine wins in extra innings with ``P(home
    outscores the visitors in an inning) / (1 - P(they tie it))``.  Bottom halves of the 9th and later aren't
    counted (a walk-off cuts them short)."""
    MIN_HALVES = 30  # fewer than this from a base-out state: both teams' together

    def __init__(self, rest_runs: tuple, host: int, placed_runner: bool):
        self.rest_runs, self.host, self.placed = rest_runs, host, placed_runner
        self._memo: dict = {}
        away, home = 1 - host, host
        self.fresh = (self.dist(away, 0, 0), self.dist(home, 0, 0))
        extra = (self.dist(away, 2, 0), self.dist(home, 2, 0)) if placed_runner else self.fresh
        self.extra = extra
        win = sum(pa * pb for a, pa in enumerate(extra[0]) for b, pb in enumerate(extra[1]) if b > a)
        tie = sum(pa * extra[1][a] for a, pa in enumerate(extra[0]))
        self.p_extra = win / (1 - tie) if tie < 1 else 0.5

    def dist(self, team: int, state: int, outs: int) -> list[float]:
        """Shares of half-innings scoring 0..10+ runs from here on for ``team`` (0 = Team 1)."""
        mine = self.rest_runs[team].get((state, outs))
        if not mine or sum(mine) < self.MIN_HALVES:
            both = [sum(x) for x in zip(*(r.get((state, outs)) or [0] * 11 for r in self.rest_runs))]
            mine = both if sum(both) else None
        if not mine:  # never seen: about half a run, Poisson
            return [math.exp(-0.5) * 0.5 ** k / math.factorial(k) for k in range(11)]
        n = sum(mine)
        return [c / n for c in mine]

    def _value(self, h: int, lead: int) -> float:
        """The home team's chance with half-inning ``h`` (0 = top of the 1st) about to start and the home team
        ``lead`` runs ahead."""
        lead = max(min(lead, 30), -30)
        if h >= 18 and h % 2 == 0:  # after nine innings, or a full extra inning
            return 1.0 if lead > 0 else 0.0 if lead < 0 else self.p_extra
        key = (h, lead)
        got = self._memo.get(key)
        if got is None:
            top = h % 2 == 0
            d = (self.extra if h >= 18 else self.fresh)[0 if top else 1]
            got = sum(p * self._value(h + 1, lead - r if top else lead + r) for r, p in enumerate(d))
            self._memo[key] = got
        return got

    def home_chance(self, inning: int, top: bool, outs: int, state: int, lead: int) -> float:
        """The home team's chance with the bases (``state``) and outs as they are, the home team ``lead`` ahead."""
        h = (inning - 1) * 2 + (0 if top else 1)
        if not top and inning >= 9 and lead > 0:
            return 1.0  # a walk-off
        if outs >= 3:
            return self._value(h + 1, lead)
        team = (1 - self.host) if top else self.host
        d = self.dist(team, state, outs)
        return sum(p * self._value(h + 1, lead - r if top else lead + r) for r, p in enumerate(d))


def game_win_probability(log: list, final: tuple, host: int, res: dict, placed_runner: bool) -> dict:
    """A replayed game's plate appearances (``replay``'s log; ``final`` its score by team) with the home team's chance
    of winning after each, for :class:`wpachart.WPAChart`: ``{"start": before the first pitch, "plays": [{"index",
    "inning", "half", "home_wp", "added", "batter", "pitcher", "description", "event", "away_score", "home_score",
    "li"}]}`` (chances in percent; ``added`` the home team's change).  The chance before a plate appearance comes from :class:`WinExpectancy` over the run; after it,
    the next one's before (steals and wild pitches before that pitch go with it), or the final result."""
    we = WinExpectancy(res["rest_runs"], host, placed_runner)
    pas = [line for line in log if line["kind"] == "pa"]
    befores = []
    for line in pas:
        home_score, away_score = line["score"][host], line["score"][1 - host]
        state = sum(bit for bit, b in zip((1, 2, 4), line["bases"]) if b is not None)
        befores.append(100 * we.home_chance(line["inning"], line["top"], line["outs"], state, home_score - away_score))
    end = 100.0 if final[host] > final[1 - host] else 0.0 if final[host] < final[1 - host] else 50.0
    plays = []
    first = 100 * we._value(0, 0)
    for i, line in enumerate(pas):
        after = befores[i + 1] if i + 1 < len(pas) else end
        before = befores[i] if i else first
        after_score = pas[i + 1]["score"] if i + 1 < len(pas) else final
        plays.append({"index": i, "inning": line["inning"], "half": "top" if line["top"] else "bottom", "home_wp": after,
                      "added": after - before, "batter": line["batter"], "pitcher": line["pitcher"],
                      "description": line["text"], "event": CLASS_NAMES.get(line["cls"], line["cls"]),
                      "away_score": after_score[1 - host], "home_score": after_score[host], "li": None})
    return {"start": first, "plays": plays}


BOX_BATTING_KEYS = (("atBats", "AB"), ("runs", "R"), ("hits", "H"), ("rbi", "RBI"), ("baseOnBalls", "BB"),
                    ("strikeOuts", "K"))
BOX_NOTES = (("2B", "2B"), ("3B", "3B"), ("HR", "HR"), ("SF", "SF"), ("SH", "SH"), ("IBB", "IBB"), ("HBP", "HBP"),
             ("GIDP", "GIDP"))


def box_score(log: list, out: dict, teams: tuple, host: int, final: tuple) -> dict:
    """A replayed game (``replay``'s log and ``out``) as a box score in the Boxscore tab's form ->
    ``{"line": {"innings": n, "runs": (Team 1's by inning, Team 2's), "hits": (...), "errors": (...)},
    "sides": (Team 1's, Team 2's)}``, each side as ``mlbstats``'s: ``{"batters": [{"name", "pos", "sub", "stats",
    ...}], "batting_totals", "notes", "info", "pitchers": [...], "pitching_totals"}``.  Batters by their place in the
    order, each place's replacements under it (PH, PR, or the position they came in at); pitchers in the order they
    pitched.  Errors are the times the other side reached on one."""
    game, stats = out["game"], out["stats"]
    pas = [line for line in log if line.get("kind") == "pa"]
    sides = []
    for t in (0, 1):
        gside = 1 if t == host else 0
        team = teams[t]
        bat = stats["batting"][gside]
        by_name = {st["name"]: pid for pid, st in bat.items()}
        start = game.start_lineups[gside]
        start_pos = {row["_id"]: pos for pos, row in game.start_defense[gside].items() if pos != "P"}
        final_pos = {row["_id"]: pos for pos, row in game.defense[gside].items() if pos != "P"}
        places = [[] for _ in range(9)]  # each place's batters, in the order they came in (player ids)
        for slot, b in enumerate(start):
            if b is not None:
                places[slot].append(b.pid)
        for line in pas:
            if line["side"] == t:
                pid = by_name.get(line["batter"])
                if pid is not None and pid not in places[line["slot"] - 1]:
                    places[line["slot"] - 1].append(pid)
        placed = {pid for p in places for pid in p}
        final_ids = [b.pid if b is not None else None for b in game.lineups[gside]]
        for pid, st in bat.items():  # came in to run or field and never batted
            if pid not in placed and (st["PR"] or st["DEF"]):
                slot = final_ids.index(pid) if pid in final_ids else 8
                places[slot].append(pid)
        batters = []
        totals = {key: 0 for key, _k in BOX_BATTING_KEYS}
        for slot, members in enumerate(places):
            for i, pid in enumerate(members):
                st = bat.get(pid)
                if st is None:
                    continue
                if i == 0 and start[slot] is not None:
                    pos = start_pos.get(pid, "DH" if team.uses_dh else "")
                elif start[slot] is None and not st["PH"]:
                    pos = "P"
                else:
                    pos = "PH" if st["PH"] else "PR" if st["PR"] else final_pos.get(pid, "")
                row = {"name": st["name"], "pos": pos, "sub": i > 0,
                       "current": False, "stats": {key: st[k] for key, k in BOX_BATTING_KEYS}, "avg": "", "ops": ""}
                batters.append(row)
                for key, k in BOX_BATTING_KEYS:
                    totals[key] += st[k]
        notes_fields = []
        for label, k in BOX_NOTES + (("SB", "SB"), ("CS", "CS")):
            who = [(st["name"], st[k]) for st in bat.values() if st.get(k)]
            if who:
                notes_fields.append((label, ", ".join(f"{n}{f' {c}' if c > 1 else ''}" for n, c in who)))
        pitchers = []
        ptotals = {"inningsPitched": 0, "hits": 0, "runs": 0, "baseOnBalls": 0, "strikeOuts": 0, "homeRuns": 0}
        outs_total = 0
        for pid, st in stats["pitching"][gside].items():
            pitchers.append({"name": st["name"], "note": "", "current": False, "era": "",
                             "stats": {"inningsPitched": mlbstats.outs_to_innings(st["outs"]), "hits": st["H"],
                                       "runs": st["R"], "baseOnBalls": st["BB"], "strikeOuts": st["K"],
                                       "homeRuns": st["HR"]}})
            outs_total += st["outs"]
            for key, k in (("hits", "H"), ("runs", "R"), ("baseOnBalls", "BB"), ("strikeOuts", "K"), ("homeRuns", "HR")):
                ptotals[key] += st[k]
        ptotals["inningsPitched"] = mlbstats.outs_to_innings(outs_total)
        sides.append({"batters": batters, "batting_totals": totals, "notes": [],
                      "info": [("Batting", notes_fields)] if notes_fields else [], "pitchers": pitchers,
                      "pitching_totals": ptotals})
    # the line score: each half-inning's runs, from the score as each half began
    starts = []  # (inning, top, score by team) at each half's first plate appearance
    for line in pas:
        if not starts or (starts[-1][0], starts[-1][1]) != (line["inning"], line["top"]):
            starts.append((line["inning"], line["top"], line["score"]))
    innings = max((s[0] for s in starts), default=9)
    runs = ([None] * innings, [None] * innings)
    for i, (inning, top, score) in enumerate(starts):
        after = starts[i + 1][2] if i + 1 < len(starts) else final
        t = (1 - host) if top else host
        runs[t][inning - 1] = after[t] - score[t]
    hits = tuple(sides[t]["batting_totals"]["hits"] for t in (0, 1))
    errors = tuple(sum(st["ROE"] for st in stats["batting"][1 if (1 - t) == host else 0].values()) for t in (0, 1))
    return {"line": {"innings": innings, "runs": runs, "hits": hits, "errors": errors}, "sides": tuple(sides)}


def round_robin(teams: list, envs: list, games: int, seed: int, progress=None, cancelled=None) -> dict:
    """Every pair of ``teams`` plays ``games`` games, taking turns at home as :func:`simulate` (``envs[i]``: the
    environment when team ``i`` hosts) -> ``{"table": [{"team", "W", "L", "T", "RS", "RA", "G"}] in the teams'
    order, "h2h": [[wins of row against column]], "order": team indices best first (win share, then run margin),
    "games", "seed"}``.  Each pair has its own seed (from ``seed`` and the pair's place in the schedule), so adding a
    team doesn't change the other pairs' games."""
    n = len(teams)
    table = [{"team": t, "W": 0, "L": 0, "T": 0, "RS": 0, "RA": 0, "G": 0} for t in teams]
    h2h = [[0] * n for _ in range(n)]
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    for k, (i, j) in enumerate(pairs):
        res = simulate(teams[i], teams[j], (envs[i], envs[j]), games, seed * 1009 + k, cancelled=cancelled)
        for me, other, side in ((i, j, 0), (j, i, 1)):
            row = table[me]
            row["W"] += res["wins"][side]
            row["L"] += res["wins"][1 - side]
            row["T"] += res["ties"]
            row["RS"] += sum(res["runs"][side])
            row["RA"] += sum(res["runs"][1 - side])
            row["G"] += res["games"]
            h2h[me][other] += res["wins"][side]
        if progress is not None:
            progress(k + 1, len(pairs))
    share = lambda r: (r["W"] + r["T"] / 2) / r["G"] if r["G"] else 0.0
    order = sorted(range(n), key=lambda i: (-share(table[i]), -(table[i]["RS"] - table[i]["RA"]), i))
    return {"table": table, "h2h": h2h, "order": order, "games": games, "seed": seed}


def replay(team1: Team, team2: Team, env, games: int, seed: int, index: int, tired: tuple | None = None,
           res: dict | None = None, out: dict | None = None) -> tuple[list, tuple, int]:
    """Game ``index`` of a run of ``games`` played again with every event written down (the same game: it
    has its own random numbers; ``tired``, by team, the relievers who sat out in it -- the run's ``tired``) ->
    ``(log lines, (Team 1's runs, Team 2's), home team)``.  The log's sides and scores are by team, as the run's;
    ``top`` says whether it was the visitors batting.  ``res`` (the run) gives who hosted and started, as in a
    series.  ``out``, if given, receives the ``game`` and its own ``stats`` (by side: visitors, home) for
    :func:`box_score`."""
    envs = _environments(env)
    teams = (team1, team2)
    in_series = bool(res and res.get("series"))
    host = res["hosts"][index] if res and res.get("hosts") else host_of(index)
    order = (1 - host, host)
    if res and res.get("starter_ids"):
        ids = res["starter_ids"][index]
        by_pid = [{p.pid: p for p in t.pitchers} for t in teams]
        sv, sh = by_pid[order[0]][ids[order[0]]], by_pid[order[1]][ids[order[1]]]
    else:
        sv, sh = teams[order[0]].starters(index + 1)[index], teams[order[1]].starters(index + 1)[index]
    log = []
    stats = {"batting": ({}, {}), "pitching": ({}, {})}
    tired = tired or (frozenset(), frozenset())
    game = Game(envs[host], (teams[order[0]], teams[order[1]]), (sv, sh), random.Random(game_seed(seed, index)),
                stats, log, index=index, tired=(tired[order[0]], tired[order[1]]), rest=not in_series)
    for t in order:
        if tired[t]:
            names = {p.pid: p.name for p in teams[t].pitchers}
            log.append({"kind": "note", "inning": 1, "score": (0, 0), "text": f"Tired, sitting out ({teams[t].short}): "
                        + ", ".join(sorted(names.get(pid, "?") for pid in tired[t]))})
    log.insert(0, {"kind": "start", "inning": 1, "score": (0, 0), "text": f"{teams[order[0]].name} at "
                f"{teams[order[1]].name}: {sv.name} ({sv.bf_start:.1f} batters a start) vs {sh.name} "
                f"({sh.bf_start:.1f})"})
    v, h = game.play()
    if out is not None:
        out["game"], out["stats"] = game, stats
    by_team = lambda pair: (pair[0], pair[1]) if host == 1 else (pair[1], pair[0])  # (visitor, home) -> by team
    for line in log:
        if "side" in line:
            line["top"] = line["side"] == 0
            line["side"] = order[line["side"]]
        if "score" in line:
            line["score"] = by_team(line["score"])
    return log, by_team((v, h)), host


def matchup_table(batting: Team, slot: int, fielding: Team, pitcher: Pitcher, env: Environment) -> list[dict]:
    """The worked example: each result's rate for the batter, his league, the pitcher, the pitcher's league,
    the environment (``park``: the home league in its park; ``env``: that for this matchup of hands, ``hands`` --
    the same without platoon figures), the log5 mix and the final figure after the fielders."""
    batter = batting.lineup[slot]
    if batter is not None:
        bats, b_rates, b_platoon = batter.bats, batter.rates, batter.platoon
    else:
        own = batting.rotation[0]
        bats, b_rates, b_platoon = own.bats, own.bat_rates or batting.league, own.bat_platoon
    b_rates, p_rates, env_rates, hands = platoon_matchup(bats, b_rates, b_platoon, pitcher, batting, fielding, env)
    env_rates = env.with_edge(env_rates, env.home is batting)
    mixed = log5(b_rates, batting.league, p_rates, fielding.league, env_rates)
    final, _where = apply_fielding(mixed, env.tables,
                                   env.zone_factors(fielding.fielding_state(fielding.defense, pitcher)))
    rows = []
    for k in CLASSES:
        rows.append({"cls": k, "name": CLASS_NAMES[k], "batter": b_rates.get(k), "b_league": batting.league.get(k),
                     "pitcher": p_rates.get(k), "p_league": fielding.league.get(k), "env": env_rates.get(k),
                     "park": env.rates.get(k), "log5": mixed.get(k), "final": final.get(k), "hands": hands})
    return rows


# --------------------------------------------------------------------------- #
#  Card Battle: one matchup worked out from the cards, as the games do it     #
# --------------------------------------------------------------------------- #
def battle_plate_appearance(batter, batting: Team, pitcher: Pitcher, fielding: Team, env: Environment) -> dict:
    """A batter (a ``Batter``) against a pitcher, with the fielding team's usual fielders behind him, in ``env``
    (whose park) -> ``{"rows": matchup rows as :func:`matchup_table`'s, "line": {"AVG", "OBP", "SLG", "K", "BB"},
    "fielders": the fielders' state}``: what the games draw from for this plate appearance."""
    b_rates, p_rates, env_rates, hands = platoon_matchup(batter.bats, batter.rates, batter.platoon, pitcher,
                                                         batting, fielding, env)
    env_rates = env.with_edge(env_rates, env.home is batting)
    mixed = log5(b_rates, batting.league, p_rates, fielding.league, env_rates)
    state = fielding.fielding_state(fielding.defense, pitcher)
    final, _where = apply_fielding(mixed, env.tables, env.zone_factors(state))
    rows = [{"cls": k, "name": CLASS_NAMES[k], "batter": b_rates.get(k), "b_league": batting.league.get(k),
             "pitcher": p_rates.get(k), "p_league": fielding.league.get(k), "env": env_rates.get(k),
             "park": env.rates.get(k), "league": env.league.get(k), "log5": mixed.get(k), "final": final.get(k)}
            for k in CLASSES]
    hits = sum(final[k] for k in ("1B", "2B", "3B", "HR"))
    walks = final["BB"] + final["HBP"]
    at_bats = 1 - walks
    bases = final["1B"] + 2 * final["2B"] + 3 * final["3B"] + 4 * final["HR"]
    line = {"AVG": hits / at_bats if at_bats else 0.0, "OBP": hits + walks, "SLG": bases / at_bats if at_bats else 0.0,
            "K": final["K"], "BB": final["BB"], "HR": final["HR"], "ROE": final["ROE"]}
    return {"rows": rows, "line": line, "fielders": state, "hands": hands}


def _odds_steps(base: float, factors: list[tuple[str, float]]) -> dict:
    """A rate turned to odds, each side's factor multiplied in, and back -> ``{"base", "steps": [(who, factor,
    odds after)], "odds", "rate"}`` (as the games combine a runner and his fielders)."""
    odds = base / (1 - base) if 0 < base < 1 else 0.0
    steps = []
    for who, f in factors:
        odds *= f
        steps.append((who, f, odds))
    return {"base": base, "base_odds": base / (1 - base) if 0 < base < 1 else 0.0, "steps": steps, "odds": odds,
            "rate": odds / (1 + odds)}


BATTLE_PLAYS = ("steal", "extra_base", "tag_up", "double_play")


def battle_running(play: str, runner, running: Team, fielding: Team, env: Environment, fielder_pid=None,
                   position: str | None = None, pitcher: Pitcher | None = None) -> dict:
    """A runner (a ``Batter``: his running, or for a double play his ground balls) against the fielding team's
    fielders on one kind of play (``BATTLE_PLAYS``), in ``env`` -> ``{"play", "parts": [(label, base rate, steps
    as :func:`_odds_steps`)], "per_base": [...] for a steal}``.  Each side is against its own league; the
    environment's league rate is the base the factors multiply, in odds -- as in the games, where the same factors
    tilt the season's play tables."""
    lg_run = (env.home.league_running or running.league_running or {})
    if play == "steal":
        catcher = fielding.defense.get("C")
        c_att, c_safe = fielding.catcher_steals(catcher["_id"]) if catcher is not None else (1.0, 1.0)
        p_att, p_safe = fielding.pitcher_steals(pitcher) if pitcher is not None else (1.0, 1.0)
        r = runner.running
        tags = fielding.running_state(fielding.defense, pitcher)["tag"]
        cover = lambda base: " and ".join(f"{p} {(fielding.defense.get(p) or {}).get('_name', '?')}"
                                          for p in STEAL_COVER[base])
        league = env.tables.steal_rates() or (lg_run.get("att") or 0.05, lg_run.get("succ") or 0.7)
        second = env.tables.steal.get(1, (league[0], league[1]))
        tries = _odds_steps(league[0], [("runner", r["att"]), ("catcher", c_att), ("pitcher", p_att)])
        safe = _odds_steps(second[1], [("runner", r["succ"]), ("catcher", c_safe), ("pitcher", p_safe),
                                       (f"covering ({cover(1)})", tags.get(1, 1.0))])
        both = env.tables.steal_rates(r["att"] * c_att * p_att, r["succ"] * c_safe * p_safe, tags)
        per_base = []
        for b in sorted(env.tables.steal):
            att, succ = env.tables.steal[b]
            per_base.append({"base": ("second", "third", "home")[b - 1], "tries": tilt(att, r["att"] * c_att * p_att),
                             "league_tries": att, "safe": tilt(succ, r["succ"] * c_safe * p_safe * tags.get(b, 1.0)),
                             "league_safe": succ, "covering": cover(b) if b in STEAL_COVER else "the catcher"})
        return {"play": play, "parts": [("tries to steal, per chance (any base)", tries),
                                        ("is safe stealing second", safe)], "per_base": per_base,
                "any_base": both, "catcher": catcher.get("_name") if catcher else None}
    if play in ("extra_base", "tag_up"):
        key, i = ("xb", 0) if play == "extra_base" else ("tu", 1)
        arm = fielding.arm(fielder_pid, position)[i] if fielder_pid is not None else 1.0
        base = lg_run.get(key) or (0.38 if key == "xb" else 0.24)
        label = "takes an extra base on a single or double" if key == "xb" else "tags up and advances on a fly out"
        return {"play": play, "parts": [(label, _odds_steps(base, [("runner", runner.running[key]),
                                                                    (f"{position} arm", arm)]))]}
    if play == "double_play":
        turn = fielding.turning_dps(fielder_pid, position) if fielder_pid is not None else 1.0
        base = env.home.league.get("_gidp_go") or running.league.get("_gidp_go") or 0.08
        return {"play": play, "parts": [("grounds into a double play, per ground out",
                                         _odds_steps(base, [("batter", runner.gdp), (f"{position} turning", turn)]))]}
    raise ValueError(f"unknown play {play!r}")


def export_tables(teams: tuple, res: dict) -> list[tuple[str, list, list]]:
    """A run as plain tables for a spreadsheet -> ``[(sheet name, header, rows)]``: the result per team (and the
    series, in series mode), every game, and each team's batting and pitching lines in the games beside the real
    season's."""
    n = res["games"] or 1
    out = []
    header = ["Team", "W", "L", "T", "Win share", "Home W", "Home L", "Road W", "Road L", "Runs/G", "Allowed/G",
              "Shutouts", "Pythagorean"]
    rows = []
    for side, t in enumerate(teams):
        rs, ra = sum(res["runs"][side]) / n, sum(res["runs"][1 - side]) / n
        rows.append([t.name, res["wins"][side], res["wins"][1 - side], res["ties"],
                     round((res["wins"][side] + res["ties"] / 2) / n, 4), *res["home"][side], *res["road"][side],
                     round(rs, 3), round(ra, 3), res["shutouts"][side],
                     round(rs ** 2 / (rs ** 2 + ra ** 2), 4) if rs + ra else None])
    out.append(("Result", header, rows))
    sr = res.get("series")
    if sr:
        need = sr["best_of"] // 2 + 1
        header = ["Team", "Home field", "Series won", "Share"] + [f"Won in {k}" for k in range(need, sr["best_of"] + 1)]
        out.append(("Series", header, [[t.name, "yes" if side == sr["seed_team"] else "", sr["wins"][side],
                                         round(sr["wins"][side] / sr["count"], 4)]
                                        + [sr["lengths"].get((side, k), 0) for k in range(need, sr["best_of"] + 1)]
                                        for side, t in enumerate(teams)]))
    names = [{p.pid: p.name for p in t.pitchers} for t in teams]
    rows = []
    for g in range(res["games"]):
        a, b = res["runs"][0][g], res["runs"][1][g]
        ids = res["starter_ids"][g] if res.get("starter_ids") else (None, None)
        rows.append([g + 1, teams[res["hosts"][g]].name, a, b, names[0].get(ids[0], ""), names[1].get(ids[1], ""),
                     teams[0].name if a > b else teams[1].name if b > a else "tie"])
    out.append(("Games", ["Game", "Home team", f"{teams[0].short} runs", f"{teams[1].short} runs",
                          f"{teams[0].short} starter", f"{teams[1].short} starter", "Winner"], rows))
    bat_keys = ("GS", "PA", "AB", "H", "2B", "3B", "HR", "BB", "IBB", "HBP", "K", "SF", "SH", "ROE", "R", "RBI", "SB",
                "CS", "GIDP", "PH", "PR", "DEF")
    pit_keys = ("G", "GS", "BF", "H", "BB", "IBB", "K", "HR", "R")
    for side, t in enumerate(teams):
        real_rows = {r["_id"]: r for r in t.raw["hitting"]}
        rows = []
        for pid, st in sorted(res["stats"]["batting"][side].items(), key=lambda kv: -kv[1]["PA"]):
            sim = summarize_batting(st)
            real = real_batting(real_rows[pid]) if pid in real_rows else {}
            rows.append([st["name"]] + [st[k] for k in bat_keys]
                        + [round(sim[k], 3) if sim[k] is not None else None for k in ("AVG", "OBP", "SLG")]
                        + [round(real[k], 3) if real.get(k) is not None else None for k in ("AVG", "OBP", "SLG")])
        out.append((f"Team {side + 1} batting", ["Batter"] + list(bat_keys) + ["AVG", "OBP", "SLG", "Real AVG",
                                                                                "Real OBP", "Real SLG"], rows))
        real_p = {p.pid: p.row for p in t.pitchers}
        rows = []
        for pid, st in sorted(res["stats"]["pitching"][side].items(), key=lambda kv: -kv[1]["outs"]):
            row = real_p.get(pid)
            outs = mlbstats.pitcher_workload(row)[2] if row else 0
            rows.append([st["name"], st.get("role", "")] + [st[k] for k in pit_keys]
                        + [mlbstats.outs_to_innings(st["outs"]), round(27 * st["R"] / st["outs"], 2) if st["outs"] else None,
                           round(27 * _num(row, "runs") / outs, 2) if outs else None])
        out.append((f"Team {side + 1} pitching", ["Pitcher", "Role"] + list(pit_keys) + ["IP", "R/9", "Real R/9"],
                    rows))
    return out


def _fmt3(x) -> str:
    """.312 style, blank for nothing."""
    if x is None:
        return ""
    return f"{x:.3f}"[1:] if 0 <= x < 1 else f"{x:.3f}"


def summarize_batting(st: dict) -> dict:
    ab, h = st["AB"], st["H"]
    tb = st["1B"] + 2 * st["2B"] + 3 * st["3B"] + 4 * st["HR"]
    obp_d = ab + st["BB"] + st["HBP"] + st["SF"]
    return {"AVG": h / ab if ab else None, "OBP": (h + st["BB"] + st["HBP"]) / obp_d if obp_d else None,
            "SLG": tb / ab if ab else None,
            "RP/PA": (st["R"] + st["RBI"] - st["HR"]) / st["PA"] if st["PA"] else None}


def real_batting(row: dict) -> dict:
    ab, h = _num(row, "atBats"), _num(row, "hits")
    pa = _num(row, "plateAppearances")
    tb = _num(row, "totalBases")
    obp_d = ab + _num(row, "baseOnBalls") + _num(row, "hitByPitch") + _num(row, "sacFlies")
    return {"AVG": h / ab if ab else None,
            "OBP": (h + _num(row, "baseOnBalls") + _num(row, "hitByPitch")) / obp_d if obp_d else None,
            "SLG": tb / ab if ab else None,
            "RP/PA": (_num(row, "runs") + _num(row, "rbi") - _num(row, "homeRuns")) / pa if pa else None}


# --------------------------------------------------------------------------- #
#  Path analysis: what each part of each team did to the result               #
# --------------------------------------------------------------------------- #
PATH_PARTS = ("batting", "running", "pitching", "fielding")
PATH_LABELS = {"batting": "Batting order", "running": "Baserunning", "pitching": "Pitching staff",
               "fielding": "Fielding"}
# Each part's categories, named as the player cards tag their lines.  Batting and pitching are one each (the
# cards' chances); fielding and running are measured category by category as well as together.
PATH_CATEGORIES = {"batting": ("batting", "platoon_batting"), "pitching": ("pitching", "platoon_pitching"),
                   "fielding": ("range", "errors", "arms", "turning_dps", "stopping_steals"),
                   "running": ("steals", "extra_bases", "tag_ups", "double_plays", "pinch_running")}
CATEGORY_LABELS = {"batting": "Batting", "pitching": "Pitching", "platoon_batting": "Platoon",
                   "platoon_pitching": "Platoon", "range": "Range", "errors": "Errors",
                   "steals": "Steals", "extra_bases": "Extra bases", "tag_ups": "Tag-ups",
                   "double_plays": "Double plays", "pinch_running": "Pinch running",
                   "arms": "Arms", "turning_dps": "Turning DPs", "stopping_steals": "Stopping steals",
                   "order": "Order", "usage": "Usage", "managing": "Managing",
                   "sacrifices": "Sacrifices", "intentional_walks": "Intentional walks",
                   "defensive_subs": "Defensive subs", "rest_days": "Rest days", "tired_relievers": "Tired relievers",
                   "home_edge": "Home-field edge", "pinch_hitting": "Pinch hitting", "running": "Running", "fielding": "Fielding", "park": "Park"}
_RUNNING_KEYS = {"steals": ("att", "succ"), "extra_bases": ("xb",), "tag_ups": ("tu",)}
# The managers' decisions and the home field, each measured by its effect on the team's run margin (they act on both
# its batting and its fielding): the same games with that decision of this team's switched off -- or its home park
# made neutral, or its home batters' edge taken away.
MANAGING_CATEGORIES = ("sacrifices", "intentional_walks", "pinch_hitting", "defensive_subs", "rest_days",
                       "tired_relievers")
HOME_CATEGORIES = ("park", "home_edge")


def win_interval(wins: float, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """A 95% (``z``) Wilson interval for a share of wins (``wins`` of ``n``, a tie half) -> (low, high)."""
    if n <= 0:
        return None
    p = wins / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(centre - half, 0.0), min(centre + half, 1.0)


def games_to_separate(p: float, z: float = 1.96) -> int | None:
    """How many games a run needs before a team winning ``p`` of them is told apart from an even match (its 95%
    interval clear of .500) -> a count, or None for an even match."""
    if abs(p - 0.5) < 1e-9 or not 0 < p < 1:
        return None
    return math.ceil(z * z * p * (1 - p) / (p - 0.5) ** 2)


def run_margin_interval(res: dict, z: float = 1.96) -> tuple[float, float, float]:
    """Team 1's run margin a game, with a 95% interval from the games' spread -> (mean, low, high)."""
    diffs = [a - b for a, b in zip(res["runs"][0], res["runs"][1])]
    m, se = _mean(diffs), _se(diffs)
    return m, m - z * se, m + z * se


def path_replays() -> int:
    """How many times a path analysis plays the games: once as they were, then per side once for each part and
    each category of a part that has several."""
    per_side = len(PATH_PARTS) + sum(len(c) for c in PATH_CATEGORIES.values() if len(c) > 1)
    per_side += 1 + len(MANAGING_CATEGORIES) + 1 + len(HOME_CATEGORIES)  # managing (all, each); home (all, each)
    return 1 + 2 * per_side


def neutralized(team: Team, part: str) -> Team:
    """A copy of ``team`` with one part (``PATH_PARTS``) or one card category (``PATH_CATEGORIES``) made exactly
    league-average: its batters' (and pitchers') hitting, its pitchers, its batters' or pitchers' own platoon
    splits (the league's platoon effect stays), its fielders' range or errors, or its runners' steals, extra bases,
    tag-ups, double plays or pinch running.  Everything else -- the same people,
    roles and workloads -- stays as it was."""
    import copy
    cats = set(PATH_CATEGORIES.get(part, (part,)))

    def even(rates: dict, hand: str) -> dict | None:
        """A league-average player of his hand: the league's rates against each hand before the matchup's ratio
        (which the games put back), so he does as an average player batting or throwing from his side does."""
        return {o: dict(rates) for o in HANDS} if team.platoon and hand else None

    def running(r: dict) -> dict:
        keys = [k for c in cats for k in _RUNNING_KEYS.get(c, ())]
        return dict(r, **{k: 1.0 for k in keys}) if keys else r

    t = copy.copy(team)
    t.off = team.off | (set(MANAGING_CATEGORIES) if part == "managing" else cats & set(MANAGING_CATEGORIES))
    pmap = {}
    for p in team.pitchers:
        q = copy.copy(p)
        if "pitching" in cats:
            q.rates = dict(team.league)
            q.platoon = even(team.league, q.throws)
        elif "platoon_pitching" in cats:  # the league's platoon effect only, none of his own
            q.platoon = team.platoon_of(q.rates, None, q.throws, pitcher=True)
        if "batting" in cats:
            q.bat_rates = dict(team.league_pitchers_batting or team.league)  # a league-average pitcher at bat
            q.bat_platoon = even(q.bat_rates, q.bats)
        pmap[id(p)] = q
    if "batting" in cats:
        t.pitchers_sit = team.league_pitchers_sit
    t.pitchers = [pmap[id(p)] for p in team.pitchers]
    t.roles = {role: [pmap[id(p)] for p in members] for role, members in team.roles.items()}
    t.rotation = [pmap.get(id(p), p) for p in team.rotation]
    t.closer = pmap.get(id(team.closer)) if team.closer is not None else None
    lineup = []
    for b in team.lineup:
        if b is None:
            lineup.append(None)
            continue
        c = copy.copy(b)
        if "batting" in cats:
            c.rates = dict(team.league_hitters)  # a league-average position player
            c.platoon = even(team.league_hitters, c.bats)
            c.ibb, c.sac = team.hitters_sit
        elif "platoon_batting" in cats:  # the league's platoon effect only, none of his own
            c.platoon = team.platoon_of(c.rates, None, c.bats)
        c.running = running(c.running)
        if "double_plays" in cats:
            c.gdp = 1.0
        lineup.append(c)
    t.lineup = lineup
    if cats & {"range", "errors", "pitching"}:  # MLB-average fielders, whoever is out there; league pitchers
        t.flat_range = team.flat_range or "range" in cats
        t.flat_errors = team.flat_errors or "errors" in cats
        t.flat_pitching = team.flat_pitching or "pitching" in cats
        t._state_cache = {}
    if cats & {"arms", "turning_dps", "stopping_steals"}:  # MLB-average fielders against the runners
        t.flat_arms = team.flat_arms or "arms" in cats
        t.flat_dps = team.flat_dps or "turning_dps" in cats
        t.flat_steals = team.flat_steals or "stopping_steals" in cats
        t._running_cache = {}
    bench = []
    for b in team.bench:
        c = copy.copy(b)
        if "batting" in cats:
            c.rates = dict(team.league_hitters)  # a league-average position player
            c.platoon = even(team.league_hitters, c.bats)
            c.ibb, c.sac = team.hitters_sit
        elif "platoon_batting" in cats:  # the league's platoon effect only, none of his own
            c.platoon = team.platoon_of(c.rates, None, c.bats)
        c.running = running(c.running)
        if "double_plays" in cats:
            c.gdp = 1.0
        bench.append(c)
    t.bench = bench
    t.pinch_runners = [c for c in bench if c.pid in {b.pid for b in team.pinch_runners}]
    if "pinch_running" in cats:
        t.slow, t.pinch_runners = set(), []  # no pinch running (nor the defensive changes that follow it)
    return t


def _team_totals(res: dict, side: int) -> dict:
    tot = _blank_batting()
    for st in res["stats"]["batting"][side].values():
        for k in tot:
            tot[k] += st[k]
    return tot


def path_stages(res: dict, side: int) -> dict:
    """The links of one side's chain from plate appearances to runs and wins, measured in a run:
    on-base and slugging (what the plate appearances produced), the share of baserunners who scored
    (what the running and the runner tables made of them), runs per game and the win share."""
    n = res["games"]
    t = _team_totals(res, side)
    on_base = t["H"] + t["BB"] + t["HBP"] + t["ROE"]
    summary = summarize_batting(t)
    return {"PA/G": t["PA"] / n, "OBP": summary["OBP"], "SLG": summary["SLG"],
            "on_base/G": on_base / n, "scored": t["R"] / (on_base) if on_base else None,
            "R/G": sum(res["runs"][side]) / n, "win": res["wins"][side] / n}


def _won(res: dict, g: int, side: int) -> float:
    mine, theirs = res["runs"][side][g], res["runs"][1 - side][g]
    return 1.0 if mine > theirs else 0.0 if mine < theirs else 0.5


def _mean(xs) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _se(xs) -> float:
    """Standard error of the mean of paired differences (game by game)."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1) / n)


# The chain from outs to runs (Bill James: runs come from baserunners, advanced, before the outs run out): each link's
# label and whether more of it is good for the side at bat.  27 outs a game are the clock -- outs at the plate (per
# plate appearance) and outs on the bases decide how many batters come up, of whom so many reach and so many of those
# score.
CHAIN_STAGES = (("out_rate", "Outs at the plate", False), ("outs_bases", "Outs on the bases", False),
                ("pa", "Plate appearances", True), ("reached", "Batters reaching", True),
                ("scored", "Reachers who score", True), ("runs", "Runs", True))


def _ratio_diff(bn, bd, an, ad) -> tuple[float, float]:
    """The change in a rate (sum of ``bn`` / sum of ``bd`` in the run, against the replay's ``an`` / ``ad``), game by
    game paired -> (difference, standard error), the error by linearizing each game's part of each rate."""
    sb, sa = sum(bd), sum(ad)
    if not sb or not sa:
        return 0.0, 0.0
    rb, ra = sum(bn) / sb, sum(an) / sa
    mb, ma = sb / len(bd), sa / len(ad)
    z = [(x - rb * y) / mb - (u - ra * v) / ma for x, y, u, v in zip(bn, bd, an, ad)]
    return rb - ra, _se(z)


def chain_values(res: dict, team: int) -> dict:
    """A team's chain at bat in a run, per game (rates as shares)."""
    lines, n = res["lines"][team], res["games"] or 1
    runs = sum(res["runs"][team])
    return {"out_rate": sum(lines["outs_plate"]) / max(sum(lines["PA"]), 1),
            "outs_bases": sum(lines["outs_bases"]) / n, "pa": sum(lines["PA"]) / n,
            "reached": sum(lines["reached"]) / n, "scored": runs / max(sum(lines["reached"]), 1), "runs": runs / n}


def chain_effect(base: dict, alt: dict, team: int) -> dict:
    """What a part did to each link of ``team``'s chain at bat: the run against the replay without it ->
    ``{stage: {"d": change, "se": its standard error}}`` (rates as shares, the rest per game)."""
    b, a = base["lines"][team], alt["lines"][team]
    out = {}
    d, se = _ratio_diff(b["outs_plate"], b["PA"], a["outs_plate"], a["PA"])
    out["out_rate"] = {"d": d, "se": se}
    for stage, key in (("outs_bases", "outs_bases"), ("pa", "PA"), ("reached", "reached")):
        diffs = [x - y for x, y in zip(b[key], a[key])]
        out[stage] = {"d": _mean(diffs), "se": _se(diffs)}
    d, se = _ratio_diff(base["runs"][team], b["reached"], alt["runs"][team], a["reached"])
    out["scored"] = {"d": d, "se": se}
    diffs = [x - y for x, y in zip(base["runs"][team], alt["runs"][team])]
    out["runs"] = {"d": _mean(diffs), "se": _se(diffs)}
    return out


def path_analysis(team1: Team, team2: Team, env, games: int, seed: int, progress=None, base: dict | None = None,
                  cancelled=None) -> dict:
    """The run as played (the teams taking turns at home, ``env`` as :func:`simulate`), then once more with each part of each team made league-average, on the same random
    numbers (so the difference is that part and not luck).  An effect is the real part's contribution: runs
    per game the team scored (batting, running) or kept off the board (pitching, fielding), and its win share.
    The managers' decisions (``MANAGING_CATEGORIES``, all together as "managing") and the home field
    (``HOME_CATEGORIES``, together as "home") act on both sides of a game, so theirs (``margin``: True) are the
    team's run margin a game; ``inactive``: the replay came out exactly the same (the decision never came up).
    Parts aren't exactly additive -- a good lineup's runners matter more when it gets on base more.  Each effect
    has a standard error from the game-by-game differences: once one thing goes differently the rest of that game's
    random numbers fall differently, so the same seed narrows the luck but doesn't remove it.  ``base``: the run
    itself, when it was exactly these games (single games, ``games`` of them, this seed) -- it isn't played again.
    ``cancelled``: as :func:`simulate`'s, for every replay."""
    if base is None or base["games"] != games or base.get("series") or base["seed"] != seed:
        base = simulate(team1, team2, env, games, seed, cancelled=cancelled)
    effects = {0: {}, 1: {}}
    steps = 0
    # each part, then each category of a part that has several (a one-category part is its category)
    runs = list(PATH_PARTS) + [c for p in PATH_PARTS for c in PATH_CATEGORIES[p] if len(PATH_CATEGORIES[p]) > 1]
    owner_part = {c: p for p in PATH_PARTS for c in PATH_CATEGORIES[p]}
    total = path_replays() - 1
    for side in (0, 1):
        for part in runs:
            teams = [team1, team2]
            teams[side] = neutralized(teams[side], part)
            alt = simulate(teams[0], teams[1], env, games, seed, cancelled=cancelled)
            lane = side if owner_part.get(part, part) in ("batting", "running") else 1 - side
            sign = 1 if lane == side else -1  # runs the part added to its own team's, or kept off the board
            diffs = [sign * (b - a) for b, a in zip(base["runs"][lane], alt["runs"][lane])]
            wins = [_won(base, g, side) - _won(alt, g, side) for g in range(games)]
            effects[side][part] = {"runs": _mean(diffs), "runs_se": _se(diffs),
                                   "win": _mean(wins), "win_se": _se(wins),
                                   "chain": chain_effect(base, alt, lane), "lane": lane}
            steps += 1
            if progress is not None:
                progress(steps, total)
        # the managers' decisions and the home field: what each did to the team's run margin and wins
        margin = lambda r, g: r["runs"][side][g] - r["runs"][1 - side][g]
        envs0 = _environments(env)
        for key in ("managing",) + MANAGING_CATEGORIES + ("home",) + HOME_CATEGORIES:
            teams, envs = [team1, team2], list(envs0)
            if key == "managing" or key in MANAGING_CATEGORIES:
                teams[side] = neutralized(teams[side], key)
            else:  # its home games: the park neutral, the home batters' edge taken away, or both
                envs[side] = envs[side].without_park() if key in ("home", "park") else envs[side].without_edge()
                if key == "home":
                    envs[side].edge_off = True
            alt = simulate(teams[0], teams[1], tuple(envs), games, seed, cancelled=cancelled)
            diffs = [margin(base, g) - margin(alt, g) for g in range(games)]
            wins = [_won(base, g, side) - _won(alt, g, side) for g in range(games)]
            effects[side][key] = {"runs": _mean(diffs), "runs_se": _se(diffs), "win": _mean(wins),
                                  "win_se": _se(wins), "margin": True,
                                  "inactive": alt["runs"] == base["runs"],  # (the decision never came up)
                                  # both of its chains: at bat, and the other side's against it
                                  "chain": {"bat": chain_effect(base, alt, side),
                                            "field": chain_effect(base, alt, 1 - side)}}
            if key == "park":
                effects[side][key]["known"] = bool(envs0[side].park)
            elif key == "home_edge":
                effects[side][key]["known"] = bool(envs0[side].tables.home_edge)
            steps += 1
            if progress is not None:
                progress(steps, total)
        for part in PATH_PARTS:
            for c in PATH_CATEGORIES[part]:
                effects[side].setdefault(c, effects[side][part])
    return {"base": base, "effects": effects, "stages": (path_stages(base, 0), path_stages(base, 1)),
            "chain_base": (chain_values(base, 0), chain_values(base, 1)),
            "games": games, "seed": seed, "names": (team1.name, team2.name),
            "short": (team1.short, team2.short)}


# --------------------------------------------------------------------------- #
#  Player cards: what the Simulator plays each player with                    #
# --------------------------------------------------------------------------- #
def _fielding_lines(team: Team, pid, prefer: str | None = None) -> list[dict]:
    """A player's fielding at each position he played (busiest first; ``prefer`` first if given) ->
    ``[{"pos", "ratio", "errors", "chances", "innings"}]`` with range as a share of MLB's at the position."""
    out = []
    for row in team.fielding_rows:
        if row.get("_id") != pid or row.get("_pos") not in team.league_positions:
            continue
        r = mlbstats.range_ratio(mlbstats.fielding_row_figures(row), team.league_positions[row["_pos"]])
        out.append({"pos": row["_pos"], "ratio": r[3] if r else None, "errors": int(_num(row, "errors")),
                    "chances": int(_num(row, "putOuts") + _num(row, "assists") + _num(row, "errors")),
                    "innings": row.get("innings", ""), "outs": mlbstats.innings_to_outs(row.get("innings", 0))})
    out.sort(key=lambda f: (f["pos"] != prefer, -f["outs"]))
    return out


def _pct(x) -> str:
    return f"{x:.0%}" if x is not None else "?"


BATTER_CAPTION = "Batting: his plate appearances, against an average pitcher and fielders, end"
PITCHER_CAPTION = "Pitching: batters he faces (average hitters, average fielders) end"
PARK_CAPTION = "Park: plate appearances here, an average batter against an average pitcher and fielders, end"
RUNNER_CAPTION = "Running: against an average battery, arms and infielders, he"
FIELDER_CAPTION = "Fielding: against him, an average batter or runner"


def _tag(category: str, text: str) -> str:
    """A card line under its category (``CATEGORY_LABELS``), the name the path analysis measures it by."""
    return f"{CATEGORY_LABELS[category]}: {text}"


def player_cards(team: Team, env: Environment, res: dict | None = None, side: int = 0) -> list[dict]:
    """One card per player the Simulator uses -> dicts for drawing.  ``chances`` are the player against an
    average opponent in the environment (league-average pitcher -- or batter -- and fielders), beside the
    league's.  ``tables`` are his running (against average fielders) and fielding (against an average batter or
    runner) the same way: ``{"caption", "rows": [{"cat", "label", "mine", "league", "good"}]}``, each row how
    often, his beside the league's.  ``used`` are the other things the games are decided with (his place in the
    order, a pitcher's usage, pinch running), ``reference`` things shown for
    comparison that the games don't use, ``result`` what he did in the simulated games -- each a list of lines.

    Kinds: "batter" (the batting order, in order), "pitcher" (the staff by role) and "fielder" (fielders
    who don't bat)."""
    league_field = lambda p: apply_fielding(p, env.tables, {})[0]
    base = league_field(env.league)
    bat = res["stats"]["batting"][side] if res else {}
    pit = res["stats"]["pitching"][side] if res else {}
    games = res["games"] if res else 0
    position = {row["_id"]: pos for pos, row in team.defense.items()}
    # how much of the balls in play each zone gets, in this environment (league-average batter and pitcher)
    in_play = sum(base[k] for k in ("1B", "2B", "3B", "GO", "AO"))
    zone_part = {z: sum(base[k] * env.tables.zone_share[k].get(z, 0.0) for k in ("1B", "2B", "3B", "GO", "AO"))
                 / in_play for z in env.tables.zones} if in_play else {}

    def ground(pos: str) -> str:
        """Where balls to ``pos`` went: his alone, and shared with whom."""
        d = POSITION_ZONES[pos]
        if env.tables.zones == (EVERYWHERE,):
            return "the play tables don't say where balls went: every ball in play is everyone's"
        pct = lambda x: f"{x:.0%}" if x >= 0.01 else f"{x:.1%}"
        alone = zone_part.get(d, 0.0)
        shared = sorted(((p, z) for z, p in zone_part.items() if d in z and len(z) > 1 and p >= 0.005), reverse=True)
        text = f"his alone {pct(alone)} of balls in play"
        if shared:
            text += "; shared " + ", ".join(f"{'-'.join(ZONE_POSITIONS[c] for c in z)} {pct(p)}" for p, z in shared[:3])
        return text

    def rate(cat: str, label: str, mine: float, league: float, good: str | None) -> dict:
        """One line of a running or fielding table: how often, his against the league's; ``good`` says which
        way is better for him ("higher", "lower" or None)."""
        return {"cat": cat, "label": label, "mine": mine, "league": league, "good": good}

    league_steals = env.tables.steal_rates()  # (tries per chance, safe) at any base, the league's
    lg_run = team.league_running or {}
    lg_dp = team.league.get("_gidp_go")

    def running_table(r: dict, gdp: float | None) -> dict:
        """What he does on the bases against average fielders, beside the league -- as a batter's chances are
        against an average pitcher."""
        rows = []
        if league_steals:
            tries, safe = env.tables.steal_rates(r["att"], r["succ"])
            rows.append(rate("steals", "tries to steal, per chance (any base)", tries, league_steals[0], None))
            rows.append(rate("steals", "is safe", safe, league_steals[1], "higher"))
        if lg_run.get("xb"):
            rows.append(rate("extra_bases", "takes an extra base on a single or double", tilt(lg_run["xb"], r["xb"]),
                             lg_run["xb"], "higher"))
        if lg_run.get("tu"):
            rows.append(rate("tag_ups", "tags up and advances on a fly out", tilt(lg_run["tu"], r["tu"]),
                             lg_run["tu"], "higher"))
        if gdp is not None and lg_dp:
            rows.append(rate("double_plays", "grounds into a double play, per ground out", tilt(lg_dp, gdp), lg_dp,
                             "lower"))
        caption = RUNNER_CAPTION + ("" if r.get("counts") else " (no Retrosheet record: as the league)")
        return {"caption": caption, "rows": rows}

    def platoon_table(own: str, rates_by_hand: dict | None, pitcher: bool = False) -> dict | None:
        """His plate appearances against each hand on the other side (an average opponent of that hand, average
        fielders, a neutral park), beside an average player of his hand against that hand -- what the games play
        him with (:func:`platoon_rates`)."""
        if not rates_by_hand or not team.platoon:
            return None
        cat = "platoon_pitching" if pitcher else "platoon_batting"
        rows = []
        for o in HANDS:
            side, hand = matchup_hands(o, own) if pitcher else matchup_hands(own, o)
            ratio = team.platoon["ratio"][(side, hand)]
            raw = {k: env.league[k] * ratio[k] for k in LOG5_CLASSES}
            norm = sum(raw.values())
            cell = dict(env.league, **{k: v / norm * sum(env.league[k] for k in LOG5_CLASSES) for k, v in raw.items()})
            if pitcher:
                mine = league_field(log5(env.league, env.league, rates_by_hand[o], team.league, cell))
            else:
                mine = league_field(log5(rates_by_hand[o], team.league, env.league, env.league, cell))
            avg = league_field(cell)
            on = lambda c: sum(c[k] for k in ("BB", "HBP", "1B", "2B", "3B", "HR", "ROE"))
            who = o + ("HB" if pitcher else "HP")
            better = "lower" if pitcher else "higher"
            rows.append(rate(cat, f"on base vs {who}", on(mine), on(avg), better))
            rows.append(rate(cat, f"home run vs {who}", mine["HR"], avg["HR"], better))
            rows.append(rate(cat, f"strikeout vs {who}", mine["K"], avg["K"], "higher" if pitcher else "lower"))
        if pitcher:
            hand = {"L": "left", "R": "right", "S": "with either hand"}.get(own, own)
            caption = (f"Platoon (throws {hand}): batters he faces from each side (average hitters and fielders) end, "
                       "beside an average pitcher of his hand")
        else:
            hand = {"L": "left", "R": "right", "S": "from both sides, away from the pitcher"}.get(own, own)
            caption = (f"Platoon (bats {hand}): against average pitchers of each hand and average fielders, beside an "
                       "average batter batting from the same side, he ends")
        return {"caption": caption, "rows": rows}

    def managing(ibb, sac, row=None) -> list[str]:
        """His intentional walks and sacrifices as the games decide them (by the situation, tilted by his own)."""
        bits = []
        if ibb is not None and team.decides_ibb:
            bits.append(f"walked intentionally \u00d7{ibb:.2f}")
        if sac is not None and team.decides_sac:
            bits.append(f"sacrifices \u00d7{sac:.2f}")
        if not bits:
            return []
        real = (f" (real: {int(_num(row, 'intentionalWalks'))} IBB, {int(_num(row, 'sacBunts'))} SH in "
                f"{int(_num(row, 'plateAppearances'))} PA)") if row is not None else ""
        return [_tag("managing", " and ".join(bits) + " the league's odds, in each situation (base-out state, "
                                                      "inning, margin)" + real)]

    def platoon_reference(pid, pitcher: bool = False) -> list[str]:
        """His real season split by the other side's hand (MLB, from 1950)."""
        mine = team.platoon_splits["pitching" if pitcher else "hitting"].get(pid) or {}
        out = []
        for o in HANDS:
            row = mine.get(o)
            if not row:
                continue
            real = real_batting(row)
            n = int(_num(row, "battersFaced" if pitcher else "plateAppearances"))
            who = ("LHB" if o == "L" else "RHB") if pitcher else ("LHP" if o == "L" else "RHP")
            out.append(f"Real vs {who}: {_fmt3(real['AVG'])}/{_fmt3(real['OBP'])}/{_fmt3(real['SLG'])} "
                       f"in {n} {'BF' if pitcher else 'PA'}")
        if out:
            k = SHRINK_PLATOON_BF if pitcher else SHRINK_PLATOON_PA.get(
                (team.hands.get(pid) or {}).get("bats", ""), SHRINK_PLATOON_PA["S"])
            out.append(f"Splits pulled toward his whole season as if each had {k:,} more "
                       f"{'batters faced' if pitcher else 'plate appearances'}")
        return out

    def zone_rates(pos: str, hit_f: float, err_f: float):
        """Hits and errors on balls to ``pos``'s own ground (a league-average batter and pitcher), with those
        factors there -> (hits, errors) as shares of the balls in play there, or None."""
        d = POSITION_ZONES[pos]
        if d not in env.tables.zones:
            if env.tables.zones != (EVERYWHERE,) or d not in EVERYWHERE:
                return None
            # no locations: every ball is everyone's, and his part of it a seventh, as in play
            d = EVERYWHERE
            hit_f, err_f = hit_f ** (1 / len(EVERYWHERE)), err_f ** (1 / len(EVERYWHERE))
        _p, where = apply_fielding(env.league, env.tables, {d: (hit_f, err_f)})
        mass = {k: sum(c for z, c in where[k] if z == d) for k in FIELDED}
        bip = sum(mass.values())
        return ((mass["1B"] + mass["2B"] + mass["3B"]) / bip, mass["ROE"] / bip) if bip else None

    def fielding_table(pid, pos: str, pitcher=None) -> dict:
        """What happens against him, for an average batter or runner, beside the league -- as a pitcher's
        chances are for an average hitter."""
        rows = []
        league = zone_rates(pos, 1.0, 1.0)
        if league:
            hit_f = 1.0
            if pos in OUTFIELD and team.club_range.get(pos):
                mine = team.defense_rating(pid, pos)
                hit_f = (mine / team.bip_volume) ** -DEFENSE_ELASTICITY if mine else 1.0
            # each row his own part only (better range makes more outs, and so more chances of an error)
            if pos in OUTFIELD:
                rows.append(rate("range", "balls to his ground fall for hits", zone_rates(pos, hit_f, 1.0)[0],
                                 league[0], "lower"))
            rows.append(rate("errors", "balls to his ground are errors",
                             zone_rates(pos, 1.0, team.error_factor(pid, pos))[1], league[1], "lower"))
        if team.arms and pos in ("LF", "CF", "RF"):
            _x, _o, xb_lg = team._arm_counts(pid, pos, 0)
            _t, _u, tu_lg = team._arm_counts(pid, pos, 2)
            a_xb, a_tu = team.arm(pid, pos)
            if xb_lg is not None:
                rows.append(rate("arms", "runners take an extra base on hits he picks up", tilt(xb_lg, a_xb), xb_lg,
                                 "lower"))
            if tu_lg is not None:
                rows.append(rate("arms", "runners tag up and advance on his catches", tilt(tu_lg, a_tu), tu_lg,
                                 "lower"))
        elif team.arms and pos != "C":
            _d, _o, dp_lg = team._arm_counts(pid, pos, 4)
            if dp_lg is not None:
                rows.append(rate("turning_dps", "double-play grounders he fields are turned",
                                 tilt(dp_lg, team.turning_dps(pid, pos)), dp_lg, "higher"))
        steals = None
        if pos == "C":
            steals = team.catcher_steals(pid), ""
        elif pitcher is not None:  # his own figures are against his staff: with the club's catchers, against MLB's
            p_att, p_safe = team.pitcher_steals(pitcher)
            c_att, c_safe = team.club_catcher_steals()
            steals = (p_att * c_att, p_safe * c_safe), " (with his club's catchers)"
        for base, cover in STEAL_COVER.items():
            if pos in cover and team.tags.get(pid, {}).get(pos) and base in env.tables.steal:
                lg = env.tables.steal[base][1]
                rows.append(rate("stopping_steals", f"runners stealing {('second', 'third')[base - 1]} are safe "
                                                    "(against his club's other infielders)",
                                 tilt(lg, team.infielder_steals(pid, pos)), lg, "lower"))
        if steals and league_steals and steals[0] != (1.0, 1.0):
            (att, safe), note = steals
            tries, safe_rate = env.tables.steal_rates(att, safe)
            rows.append(rate("stopping_steals", f"runners try to steal, per chance (any base){note}",
                             tries, league_steals[0], "lower"))
            rows.append(rate("stopping_steals", "are safe", safe_rate, league_steals[1], "lower"))
        return {"caption": FIELDER_CAPTION, "rows": rows}

    def fielding_record(pid, pos: str) -> list[str]:
        """His real fielding at ``pos``, which the table above is worked out from."""
        line = next((f for f in _fielding_lines(team, pid, pos) if f["pos"] == pos), None)
        bits = []
        if line and pos in OUTFIELD:
            bits.append(f"range {_pct(line['ratio'])} of MLB's chances (the club's {pos}s: "
                        f"{_pct(team.club_range.get(pos))})")
        if line:
            bits.append(f"{line['errors']} E in {line['chances']} chances")
        if team.arms:
            if pos in ("LF", "CF", "RF"):
                xb, xb_opp, _l = team._arm_counts(pid, pos, 0)
                tu, tu_opp, _l = team._arm_counts(pid, pos, 2)
                bits.append(f"{xb} extra bases taken in {xb_opp} chances and {tu} tag-ups in {tu_opp} against him")
            elif pos != "C":
                dp, opp, _l = team._arm_counts(pid, pos, 4)
                bits.append(f"{dp} of {opp} double-play grounders turned")
        covered = (team.tags.get(pid) or {}).get(pos)
        if covered:
            club = team.tags_club.get(pos) or [0, 0]
            bits.append(f"{covered[1]} of {covered[0]} runners caught stealing on throws he took (the club: "
                        + (f"{club[1] / club[0]:.0%})" if club[0] else "?)"))
        row = (team.fielding_by_player.get(pid) or {}).get(pos)
        if pos == "C" and row is not None:
            bits.append(f"{int(_num(row, 'stolenBases'))} SB, {int(_num(row, 'caughtStealing'))} CS in "
                        f"{row.get('innings', '?')} innings")
        out = [f"Fielding at {pos}: " + "; ".join(bits)] if bits else []
        return out + [f"His ground: {ground(pos)}"]

    def fielding_reference(pid, skip) -> list[str]:
        return [f"{f['pos']}: range {_pct(f['ratio'])} of MLB, {f['errors']} E / {f['chances']} ch"
                for f in _fielding_lines(team, pid) if f["pos"] != skip][:1]

    behind = ", ".join(f"{pos} {_pct(team.club_range.get(pos))}" for pos in OUTFIELD)
    # a pitcher's line had his club's whole season of fielders behind it: that's what comes out
    behind_him = (f"His line was pitched in front of the club's fielders all season ({behind} of MLB's chances): "
                  "in play that's taken out, and each ball is judged by the fielders whose ground it goes to")

    def park_card() -> dict:
        """The team's home park as a card: how plate appearances end there (an average batter and pitcher), beside
        the league's -- what a game there is played in."""
        venue = team.venue or {}
        name = venue.get("venue") or f"{team.short} home park"
        counts = team.park_counts or {}
        used, reference = [], []
        if env.park:
            chances = league_field(env.rates)
            used.append(_tag("park", f"every game at {name} is played in these rates (the {team.league_name}'s, "
                                     f"times the park's factors); {team.short}'s players have half of it taken out of "
                                     "their own lines, for the half of their season played here"))
            h = sum(counts.get("home", {}).get(k, 0) for k in LOG5_CLASSES)
            r = sum(counts.get("road", {}).get(k, 0) for k in LOG5_CLASSES)
            reference.append(f"From Retrosheet: {h:,} plate appearances at {counts.get('site', '?')} in "
                             f"{team.season} (both teams batting) against {r:,} in {team.short}'s road games; "
                             f"{SHRINK_PARK_PA:,} neutral ones added")
            notable = [f"{CLASS_NAMES[k].split(' (')[0].lower()} \u00d7{env.park[k]:.2f}" for k in LOG5_CLASSES
                       if abs(env.park[k] - 1) >= 0.03]
            reference.append("Factors: " + (", ".join(notable) if notable else "all within 3% of neutral"))
        else:
            chances = base
            used.append(_tag("park", "neutral: Retrosheet has no home and road games for it"))
        marks = venue.get("marks") or []
        if marks:
            reference.append("Fences: " + ", ".join(f"{label} {ft:.0f} ft" for label, _a, ft in marks))
        result = []
        if res:
            w, l = res["home"][side]
            result.append(f"{team.short} at home: {w:,}\u2013{l:,}")
        return {"kind": "park", "team": team.name, "short": team.short, "side": side, "pid": None, "name": name,
                "tag": "home park", "chances": chances, "caption": PARK_CAPTION, "league": base, "used": used,
                "reference": reference, "result": result}

    split_source = team.league.get("_split")
    split_note = [] if split_source == "MLB" else [
        "ground/air outs split as the league's" + (" (Retrosheet)" if split_source == "Retrosheet" else " (half and half)")
        + f": MLB has no players' split for {team.season}"]
    cards = [park_card()]
    for (slot, row, pos), batter in zip(team.lineup_rows, team.lineup):
        if batter is None:
            continue  # the pitcher's spot: the pitchers' cards carry their batting
        r = batter.running
        used = [_tag("order", f"bats {_ordinal(slot)}" + {"bats only": "; doesn't field",
                                                          "DH": "; the designated hitter"}.get(pos, ""))]
        if batter.pid in team.two_way:
            starts = team.start_shares().get(batter.pid, 0)
            used.append(_tag("usage", f"also pitches: starts {starts:.0%} of games, batting in his own place"
                             + ("" if team.uses_dh else "; his position goes to the bench's best fielder there, "
                                "who bats in the pitcher's spot, until he leaves the mound")
                             + (" (and the club gives up the DH)" if team.uses_dh and team.season < OHTANI_RULE_FROM
                                else "")))
        used += [_tag("batting", n) for n in split_note]
        used += managing(batter.ibb, batter.sac, row)
        tables = [t for t in (platoon_table(batter.bats, batter.platoon), running_table(r, batter.gdp)) if t]
        reference = platoon_reference(batter.pid)
        if batter.pid in team.slow:
            used.append(_tag("pinch_running", f"a Slow regular (Speed tab): run for on base from the "
                                              f"{_ordinal(PINCH_RUN_FROM_INNING)}, game within {PINCH_RUN_MARGIN} runs"
                             + ("" if team.pinch_runners else " (no bench runner to send)")))
        elif batter.group:
            reference.append(f"Speed tab: {batter.group}")
        fpos = position.get(batter.pid)
        if fpos and fpos != "P":
            tables.append(fielding_table(batter.pid, fpos))
        real = real_batting(row)
        reference += fielding_record(batter.pid, fpos) if fpos and fpos != "P" else []
        reference += fielding_reference(batter.pid, fpos)
        reference.append(f"Real: {_fmt3(real['AVG'])}/{_fmt3(real['OBP'])}/{_fmt3(real['SLG'])}, "
                         f"{int(_num(row, 'homeRuns'))} HR, {int(_num(row, 'stolenBases'))} SB in "
                         f"{int(_num(row, 'plateAppearances'))} PA")
        st = bat.get(batter.pid)
        result = []
        if st and games:
            sim = summarize_batting(st)
            result.append(f"{_fmt3(sim['AVG'])}/{_fmt3(sim['OBP'])}/{_fmt3(sim['SLG'])}, "
                          f"{st['HR'] / games * 162:.0f} HR and {st['SB'] / games * 162:.0f} SB per 162")
        cards.append({"kind": "batter", "team": team.name, "short": team.short, "side": side, "pid": batter.pid,
                      "name": batter.name, "tag": f"#{slot} \u00b7 {pos}", "slot": slot, "pos": pos,
                      "chances": league_field(log5(batter.rates, team.league, env.league, env.league, env.league)),
                      "caption": BATTER_CAPTION, "league": base, "tables": tables, "used": used,
                      "reference": reference, "result": result})
    starts = team.start_shares()
    pitcher_spot = None in team.lineup
    fresh = lambda ps: [p for p in ps if p not in team.rotation]
    pools = [("setup", [p for p in fresh(team.roles["Closers"]) if p is not team.closer],
              "of the 7th-8th picks, score close (down 1 to up 3)"),
             ("long", fresh(team.roles["Long Relief"]), "of the picks when the starter goes before the 6th"),
             ("other", fresh(team.roles["Other"] + team.roles["Long Relief"]), "of the other relief picks")]
    for role in mlbstats.PITCHER_ROLES:
        for p in team.roles[role]:
            used = []
            if p.pid in starts:
                used.append(_tag("usage", f"starts {starts[p.pid]:.0%} of games; goes {p.bf_start:.1f} batters"))
            if p is team.closer:
                used.append(_tag("usage", f"the closer (most GF among Closers, {int(_num(p.row, 'gamesFinished'))}): "
                                          "a lead of 1-3 from the 9th"))
                used.append(_tag("usage", f"goes {max(p.bf_relief, 4.3):.1f} batters"))
            elif role == "Rotation" and p.pid not in starts:
                used.append(_tag("usage", "not in your rotation: relieves only when the bullpen is empty"))
            for _key, members, when in pools:
                if p in members:
                    work = sum(max(m.relief_work, 1.0) for m in members)
                    used.append(_tag("usage", f"{max(p.relief_work, 1.0) / work:.0%} {when}"))
            if p not in team.rotation and p is not team.closer and role != "Rotation":
                used.append(_tag("usage", f"goes {p.bf_relief:.1f} batters in relief"))
            used += [_tag("pitching", n) for n in split_note]
            if p.pid in team.two_way and any(b is not None and b.pid == p.pid for b in team.lineup):
                slot = next(i for i, b in enumerate(team.lineup) if b is not None and b.pid == p.pid) + 1
                used.append(_tag("batting", f"a two-way player: bats {_ordinal(slot)} as himself (see his batting "
                                            "card)"))
            elif pitcher_spot and p.bat_rates:
                b = league_field(log5(p.bat_rates, team.league, env.league, env.league, env.league))
                on = sum(b[k] for k in ("BB", "HBP", "1B", "2B", "3B", "HR", "ROE"))
                used.append(_tag("batting", f"in the pitcher's spot: on base {on:.0%}, K {b['K']:.0%}, HR {b['HR']:.1%}"))
                used += managing(*team.pitchers_sit)
            outs = mlbstats.pitcher_workload(p.row)[2]
            per9 = lambda key: f"{27 * _num(p.row, key) / outs:.2f}" if outs else "\u2013"
            reference = [f"Real: {int(_num(p.row, 'gamesPitched'))} G, {int(_num(p.row, 'gamesStarted'))} GS, "
                         f"{mlbstats.outs_to_innings(outs)} IP, R/9 {per9('runs')}, K/9 {per9('strikeOuts')}"]
            reference += platoon_reference(p.pid, pitcher=True)
            reference += [behind_him] + fielding_record(p.pid, "P")[:-1]
            st = pit.get(p.pid)
            result = []
            if st and games and st["outs"]:
                result.append(f"R/9 {27 * st['R'] / st['outs']:.2f}, K/9 {27 * st['K'] / st['outs']:.2f}, "
                              f"{st['outs'] / 3 / games * 162:.0f} IP per 162")
            elif games:
                result.append("didn't pitch")
            sub = []
            if p.bf_start and p.pid in starts:
                sub.append(f"{p.bf_start:.1f} BF a start")
            if _num(p.row, "gamesPitched") > _num(p.row, "gamesStarted"):
                sub.append(f"{p.bf_relief:.1f} a relief app.")
            cards.append({"kind": "pitcher", "team": team.name, "short": team.short, "side": side, "pid": p.pid,
                          "name": p.name, "role": role, "tag": role + (" \u00b7 closer" if p is team.closer else ""),
                          "sub": ", ".join(sub),
                          "chances": league_field(log5(env.league, env.league, p.rates, team.league, env.league)),
                          "caption": PITCHER_CAPTION, "league": base,
                          "tables": [t for t in (platoon_table(p.throws, p.platoon, pitcher=True),
                                                 fielding_table(p.pid, "P", p)) if t],
                          "used": used, "reference": reference, "result": result})
    # the bench: pinch runners in the order they're sent, and who could come in on defense
    runner_rank = {b.pid: i for i, b in enumerate(team.pinch_runners)}
    # with the pitcher batting, the bench pinch-hits for him: busiest hitter first (``Game._pinch_hit``)
    hitter_rank = {b.pid: i for i, b in enumerate(sorted(team.bench, key=lambda b: (-_num(b.row, "plateAppearances"),
                                                                                  b.name)))} \
        if None in team.lineup else {}
    starting_positions = {pos for pos, row in team.defense.items() if pos != "P"}
    for b in sorted(team.bench, key=lambda b: (b.pid not in runner_rank, runner_rank.get(b.pid, 0), b.name)):
        positions = sorted(((team.defense_rating(b.pid, pos), pos) for pos in (team.fielding_by_player.get(b.pid) or {})
                            if pos in starting_positions), key=lambda t: -(t[0] or 0))
        hits_late = _num(b.row, "plateAppearances") >= PH_MIN_PA
        if b.pid not in runner_rank and not positions and b.pid not in hitter_rank and not hits_late:
            continue
        used = []
        if hits_late:
            used.append(_tag("pinch_hitting", f"bats for a regular from the {_ordinal(PINCH_HIT_FROM_INNING)}, his side "
                                              f"{-PH_LEAD[0]} behind to {PH_LEAD[1]} ahead, when worth {PH_EDGE:.2f} runs "
                                              "a plate appearance more against that pitcher (platoon and all)"))
        if b.pid in hitter_rank:
            used.append(_tag("pinch_hitting", f"{_ordinal(hitter_rank[b.pid] + 1)} in line to bat for the pitcher "
                                              f"(a reliever, or the starter from the {_ordinal(PINCH_HIT_FROM_INNING)})"))
        if b.pid in runner_rank:
            used.append(_tag("pinch_running", f"{_ordinal(runner_rank[b.pid] + 1)} in line to run ({b.group}"
                             + (f", {b.speed_rate:.3f} bases gained per opportunity)" if b.speed_rate is not None else ")")))
        for rating, pos in positions[:3]:
            used.append(_tag("pinch_running", f"can take {pos} after it: record {_pct(rating)} of MLB's chances "
                                              f"(with {DEFENSE_SHRINK_INNINGS} average innings added)"))
            starter = team.defense.get(pos)
            theirs = team.defense_rating(starter["_id"], pos) if starter is not None else None
            if rating is not None and theirs is not None and rating - theirs >= DEF_EDGE and \
                    mlbstats.innings_to_outs(team.fielding_by_player[b.pid][pos].get("innings", 0)) / 3 >= DEF_MIN_INNINGS:
                used.append(_tag("defensive_subs", f"replaces {starter.get('_name', '?')} at {pos} from the "
                                             f"{_ordinal(DEF_FROM_INNING)} with a lead of {DEF_LEAD[0]}-{DEF_LEAD[1]} "
                                             f"(his record {_pct(theirs)})"))
        if _num(b.row, "plateAppearances") == 0:
            used.append(_tag("batting", "at the league's rates if he stays in (no plate appearances)"))
        elif b.pid in hitter_rank or hits_late:
            used += managing(b.ibb, b.sac, b.row)
        st = bat.get(b.pid)
        result = []
        if st and games:
            line = f"{st['PH']:,} times as pinch hitter, {st['PR']:,} as pinch runner, {st['DEF']:,} on defense"
            if st["PA"]:
                sim = summarize_batting(st)
                line += f"; {st['PA']:,} PA, {_fmt3(sim['AVG'])}/{_fmt3(sim['OBP'])}/{_fmt3(sim['SLG'])}"
            if st["SB"] or st["CS"]:
                line += f"; {st['SB']} SB, {st['CS']} CS"
            result.append(line)
        elif games:
            result.append("didn't get in")
        real = real_batting(b.row)
        reference = platoon_reference(b.pid) + [f"Real: {_fmt3(real['AVG'])}/{_fmt3(real['OBP'])}/{_fmt3(real['SLG'])} in "
                     f"{int(_num(b.row, 'plateAppearances'))} PA, {int(_num(b.row, 'stolenBases'))} SB, "
                     f"{int(_num(b.row, 'gamesPlayed'))} G"]
        if b.group and b.pid not in runner_rank:
            reference.insert(0, f"Speed tab: {b.group}")
        cards.append({"kind": "bench", "team": team.name, "short": team.short, "side": side, "pid": b.pid,
                      "name": b.name, "tag": "bench", "pos": positions[0][1] if positions else "",
                      "chances": league_field(log5(b.rates, team.league, env.league, env.league, env.league)),
                      "caption": BATTER_CAPTION, "league": base, "used": used, "reference": reference, "result": result,
                      "tables": [t for t in (platoon_table(b.bats, b.platoon),
                                             running_table(b.running, None) if b.pid in runner_rank else None) if t]})
    batting_ids = {b.pid for b in team.lineup if b is not None}
    for pos, row in team.defense.items():
        if pos == "P" or row["_id"] in batting_ids:
            continue
        cards.append({"kind": "fielder", "team": team.name, "short": team.short, "side": side, "pid": row["_id"],
                      "name": row.get("_name", "?"), "tag": f"{pos} \u00b7 doesn't bat", "pos": pos,
                      "tables": [fielding_table(row["_id"], pos)], "used": [],
                      "reference": fielding_record(row["_id"], pos) + fielding_reference(row["_id"], pos),
                      "result": []})
    return cards
