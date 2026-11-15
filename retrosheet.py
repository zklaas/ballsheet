#!/usr/bin/env python3
"""Baserunning from Retrosheet's parsed play-by-play (the ``plays.csv`` of each season's CSV bundle).

The information used here was obtained free of charge from and is copyrighted by Retrosheet.
Interested parties may contact Retrosheet at 20 Sunset Rd., Newark, DE 19711 (retrosheet.org).

For every play the file lists the runner on each base before and after it and who scored, so each
base a runner gains can be credited to him without interpreting the event notation.  Counted per
runner and team, for regular-season games:

* **stolen bases** (and times caught stealing) -- from the steal flags, the runner being the one
  on the base stolen from;
* **extra bases on hits** -- a runner taking more bases than the hit: first to third on a single,
  first to home on a single or double, second to home on a single (a runner on third, or on second
  on a double, can take none).  Advances marked as errors are left out;
* **tag-ups** -- runners advancing on a fly-ball out (errors again left out);
* **opportunities** for each: a runner on first with second open, on second with third open, or on
  third (home is always open), at the start of a plate appearance (steals); a runner on first or
  second on a single, or on first on a double (extra bases); a runner on third, or with the next
  base open, on a fly out with fewer than two out (tag-ups).  A steal of home is rare, but it is a
  chance like the others, and MLB's figure counts it the same way.

A season's bundle is 4-10 MB; it is read once and only a small per-runner summary is kept.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile

URL = "https://www.retrosheet.org/downloads/{y}/{y}csvs.zip"
_UA = "BallSheet/1.0 (personal use; retrosheet.org)"
_TIMEOUT = 120
SUMMARY_VERSION = 3  # 2: tag-ups no longer count home runs and other non-outs as fly outs; 3: a runner on third is a steal chance


class RetrosheetError(Exception):
    """The season's play-by-play couldn't be had or read."""


# (label used as a custom-stat variable, key in a runner's row).  All counts, added across games.
RUNNING_VARIABLES = [
    ("SB", "sb"), ("CS", "cs"),
    ("1st to 3rd on Single", "s13"), ("1st to Home on Single", "s1h"),
    ("2nd to Home on Single", "s2h"), ("1st to Home on Double", "d1h"),
    ("1st to 2nd on Fly Out", "f12"), ("1st to 3rd on Fly Out", "f13"), ("1st to Home on Fly Out", "f1h"),
    ("2nd to 3rd on Fly Out", "f23"), ("2nd to Home on Fly Out", "f2h"), ("3rd to Home on Fly Out", "f3h"),
    ("Steal Opportunities", "opp_sb"), ("Extra Base Opportunities", "opp_xb"),
    ("Tag Up Opportunities", "opp_tu"),
]
KEYS = [key for _label, key in RUNNING_VARIABLES]
_INDEX = {key: i for i, key in enumerate(KEYS)}


def opportunities(row: dict) -> int:
    """Steal, extra-base and tag-up opportunities together."""
    return int(row.get("opp_sb", 0)) + int(row.get("opp_xb", 0)) + int(row.get("opp_tu", 0))


def _error_advance(event: str, start: int) -> bool:
    """Was the advance of the runner who started on base ``start`` marked as an error's doing
    (``1-3(E8)``)?  Retrosheet lists advances after the period, ``;`` apart."""
    for token in event.partition(".")[2].split(";"):
        if token[:1] == str(start) and "(E" in token:
            return True
    return False


def parse_plays(rows) -> dict[tuple[str, str], list[int]]:
    """A ``plays.csv``'s rows (the header first) -> ``{(runner id, batting team): counts}``,
    the counts in the order of :data:`KEYS`.  Only regular-season games count."""
    it = iter(rows)
    header = next(it)
    ix = {name: i for i, name in enumerate(header)}
    (i_gid, i_inn, i_tb, i_type, i_team, i_ev, i_pa, i_outs) = (
        ix["gid"], ix["inning"], ix["top_bot"], ix["gametype"], ix["batteam"], ix["event"], ix["pa"], ix["outs_pre"])
    i_pre = (ix["br1_pre"], ix["br2_pre"], ix["br3_pre"])
    i_post = (ix["br1_post"], ix["br2_post"], ix["br3_post"])
    i_run = (ix["run1"], ix["run2"], ix["run3"])
    i_sb = (ix["sb2"], ix["sb3"], ix["sbh"])
    i_cs = (ix["cs2"], ix["cs3"], ix["csh"])
    i_single, i_double = ix["single"], ix["double"]
    i_fly, i_batout = ix["fly"], ix["batout1"]
    K = _INDEX
    counts: dict[tuple[str, str], list[int]] = {}

    def bump(runner: str, team: str, key: str, amount: int = 1):
        row = counts.get((runner, team))
        if row is None:
            row = counts[(runner, team)] = [0] * len(KEYS)
        row[K[key]] += amount

    inning_key = None
    new_pa = True
    for r in it:
        if r[i_type] != "regular":
            continue
        key = (r[i_gid], r[i_inn], r[i_tb])
        if key != inning_key:
            inning_key, new_pa = key, True
        team = r[i_team]
        pre = (r[i_pre[0]], r[i_pre[1]], r[i_pre[2]])
        pa = r[i_pa] == "1"

        if new_pa:  # the first event of a plate appearance: who could try to steal?
            if pre[0] and not pre[1]:
                bump(pre[0], team, "opp_sb")
            if pre[1] and not pre[2]:
                bump(pre[1], team, "opp_sb")
            if pre[2]:  # home is always open
                bump(pre[2], team, "opp_sb")
        new_pa = pa

        for base in range(3):
            if pre[base]:
                if r[i_sb[base]] == "1":
                    bump(pre[base], team, "sb")
                if r[i_cs[base]] == "1":
                    bump(pre[base], team, "cs")

        hit_bases = 1 if r[i_single] == "1" else 2 if r[i_double] == "1" else 0
        fly_out = r[i_fly] == "1" and r[i_batout] not in ("", "0")  # batout1 is "0" when the batter wasn't out
        if not hit_bases and not fly_out:
            continue
        post = (r[i_post[0]], r[i_post[1]], r[i_post[2]])
        event = r[i_ev]
        outs_before = int(r[i_outs] or 0)
        for start in (1, 2, 3):
            runner = pre[start - 1]
            if not runner:
                continue
            if hit_bases:
                standard = min(start + hit_bases, 4)
                if standard >= 4:
                    continue  # already scores on the hit: no extra base to take
                bump(runner, team, "opp_xb")
            else:
                if outs_before >= 2:
                    continue  # the fly out ends the inning
                if start == 3 or not pre[start]:  # a runner on third, or the next base is open
                    bump(runner, team, "opp_tu")
            if r[i_run[start - 1]]:
                end = 4
            elif runner in post:
                end = post.index(runner) + 1
            else:
                continue  # out, or otherwise gone
            if _error_advance(event, start):
                continue
            if hit_bases:
                if end > standard:
                    if hit_bases == 2:
                        label = "d1h"  # on a double only a runner from first can take more
                    elif start == 1:
                        label = "s13" if end == 3 else "s1h"
                    else:
                        label = "s2h"
                    bump(runner, team, label)
            elif end > start:
                bump(runner, team, f"f{start}{'h' if end == 4 else end}")
    return counts


TEAM_SUMMARY_VERSION = 2  # 2: also what each club allowed (by the fielding team)
SAFE_OUT_KEYS = ("outs", "reach1", "reach2", "reach3", "runs", "placed")


def parse_team_running(rows) -> dict[str, dict[str, dict]]:
    """A ``plays.csv``'s rows (the header first) -> ``{"batting": {team: counts}, "fielding": {team:
    counts}}`` over the regular season, for the Safe/Out tab: what each club's batters and runners
    did, and what each club allowed its opponents' (counted by the team in the field; its outs are
    the outs it recorded, so its innings are innings in the field).  The counts:

    * ``outs`` -- outs made (``outs_post`` - ``outs_pre`` on each play);
    * ``reach1``, ``reach2``, ``reach3`` -- times a player reached first, second, third: each trip
      round the bases counts once at every base up to the furthest it got, however it got there --
      a hit, walk, error, fielder's choice or force play (the batter safe at first on a force has
      reached it; the runner forced out at second hasn't reached second) -- so a double counts at
      first and second, and a home run at all three;
    * ``runs`` -- times a player scored (each also counts as reaching every base);
    * ``placed`` -- runners put on base without reaching it (the extra-innings runner on second,
      2020 on, or a runner already aboard when an inning's record begins): counted only from the
      base after the one they were put on.

    A pinch runner takes over his man's trip where it stands."""
    it = iter(rows)
    header = next(it)
    ix = {name: i for i, name in enumerate(header)}
    i_gid, i_inn, i_tb, i_type, i_team = ix["gid"], ix["inning"], ix["top_bot"], ix["gametype"], ix["batteam"]
    i_field = ix["pitteam"]
    i_bat, i_opre, i_opost = ix["batter"], ix["outs_pre"], ix["outs_post"]
    i_pre = (ix["br1_pre"], ix["br2_pre"], ix["br3_pre"])
    i_post = (ix["br1_post"], ix["br2_post"], ix["br3_post"])
    i_run = (ix["run1"], ix["run2"], ix["run3"])
    i_runb = ix["run_b"]
    teams: dict[str, dict] = {}
    allowed: dict[str, dict] = {}
    inning_key, trips, sides = None, {}, ()   # trips: runner on base -> the furthest base his trip reached

    def add(key: str, amount: int = 1):
        for counts in sides:
            counts[key] += amount

    def credit(runner: str, base: int):
        reached = trips.get(runner, 0)
        for b in range(reached + 1, base + 1):
            add(f"reach{b}" if b <= 3 else "runs")
        if base > reached:
            trips[runner] = base

    for r in it:
        if r[i_type] != "regular":
            continue
        key = (r[i_gid], r[i_inn], r[i_tb])
        if key != inning_key:
            inning_key, trips = key, {}
        batting = teams.get(r[i_team])
        if batting is None:
            batting = teams[r[i_team]] = {k: 0 for k in SAFE_OUT_KEYS}
        fielding = allowed.get(r[i_field])
        if fielding is None:
            fielding = allowed[r[i_field]] = {k: 0 for k in SAFE_OUT_KEYS}
        sides = (batting, fielding)
        pre = [r[i] for i in i_pre]
        for base, runner in enumerate(pre, start=1):
            if runner and runner not in trips:  # put there (or a pinch runner): his trip starts at that base
                trips[runner] = base
                if r[i_opre] in ("", "0") and r[i_pre[1]] == runner and key[1] and int(key[1]) > 9:
                    add("placed")
        try:
            add("outs", max(0, int(r[i_opost] or 0) - int(r[i_opre] or 0)))
        except ValueError:
            pass
        post = [r[i] for i in i_post]
        for base, runner in enumerate(post, start=1):
            if runner:
                credit(runner, base)
        for start, i in enumerate(i_run, start=1):
            if r[i]:
                credit(r[i], 4)
        if r[i_runb]:
            credit(r[i_runb], 4)
        trips = {runner: trips.get(runner, base) for base, runner in enumerate(post, start=1) if runner}
    return {"batting": teams, "fielding": allowed}


RBI_SUMMARY_VERSION = 1


def parse_batted_in(rows) -> dict[str, dict[str, dict[str, list[int]]]]:
    """A ``plays.csv``'s rows (the header first) -> ``{batting team: {batter: {runner: counts}}}`` over
    the regular season, for the Players Batted In tab: every run a batter was credited with driving
    in, by the runner who scored it.  ``counts`` is ``[himself, from 1st, from 2nd, from 3rd]`` --
    the base the runner started the play on (the batter's own run being a home run, or an
    inside-the-park one).  A run without an RBI (an error, a double play, a wild pitch) isn't
    anyone's to claim.  The runner is the one who crossed the plate, a pinch runner included."""
    it = iter(rows)
    header = next(it)
    ix = {name: i for i, name in enumerate(header)}
    i_type, i_team, i_bat = ix["gametype"], ix["batteam"], ix["batter"]
    i_run = (ix["run_b"], ix["run1"], ix["run2"], ix["run3"])
    i_rbi = (ix["rbi_b"], ix["rbi1"], ix["rbi2"], ix["rbi3"])
    out: dict[str, dict[str, dict[str, list[int]]]] = {}
    for r in it:
        if r[i_type] != "regular":
            continue
        for start in range(4):
            runner = r[i_run[start]]
            if runner and r[i_rbi[start]] == "1":
                batters = out.setdefault(r[i_team], {})
                counts = batters.setdefault(r[i_bat], {}).setdefault(runner, [0, 0, 0, 0])
                counts[start] += 1
    return out


STAFF_SUMMARY_VERSION = 8  # 5: holds and blown saves by MLB's definitions; 6: whether the lead came back; 7: decisions;
                            # 8: saves by the current rule
# One appearance in a staff summary's game: these fields, in this order.
APPEARANCE_FIELDS = ("pitcher", "outs", "gs", "gf", "sv", "save_sit", "lead", "hold", "blown", "regained", "w", "l",
                     "sv_now")


def current_rule_save(lead: int, runners: int, outs: int, finished: bool, team_won: bool, won_it: bool,
                      kept_lead: bool) -> bool:
    """Is a relief appearance a save by today's rule 9.19 (in force since 1975; saves before then were
    credited under earlier, looser rules)?  He finished a game his team won, wasn't the winning
    pitcher, recorded at least one out, came in with the lead and never gave it up, and either came
    in leading by three runs or fewer and pitched at least an inning, or came in with the tying run
    on base, at bat or on deck, or pitched at least three innings."""
    if not (finished and team_won and not won_it and outs >= 1 and lead > 0 and kept_lead):
        return False
    return (lead <= 3 and outs >= 3) or lead <= runners + 2 or outs >= 9


def save_situation(lead: int, runners: int, inning: int, outs: int, recorded: int = 0) -> bool:
    """Was a reliever's appearance a save situation (MLB's definition, after rule 9.19)?  His team
    led when he came in, and either the lead was three runs or fewer and he had the chance to pitch
    at least an inning (an inning's outs were left: before the 9th, or at the start of the 9th or an
    extra inning), or the tying run was on base, at bat or on deck, or he went on to pitch at least
    three innings (``recorded`` 9 outs or more)."""
    if lead <= 0:
        return False
    can_pitch_an_inning = inning < 9 or outs == 0
    return (lead <= 3 and can_pitch_an_inning) or lead <= runners + 2 or recorded >= 9


def parse_staff(pitching_rows, play_rows) -> dict[str, list[dict]]:
    """A season's ``pitching.csv`` and ``plays.csv`` rows (each its header first) -> ``{team: [game,
    ...]}`` over the regular season, for the Bullpen Support tab.  A game is ``{"gid", "won",
    "pitchers": [appearance, ...]}``, the team's pitchers in the order they pitched; an appearance is
    a list in the order of :data:`APPEARANCE_FIELDS`:

    * ``pitcher``, ``outs`` recorded, ``gs`` / ``gf`` / ``sv`` -- started, finished, credited with
      the save (1 or 0);
    * ``save_sit`` -- 1 if his relief appearance was a save situation (:func:`save_situation`: the score,
      runners, inning and outs when he faced his first batter, and the outs he went on to record), 0 if
      not, None if he faced no batter; ``lead`` -- his team's lead then (negative when behind);
    * ``hold`` and ``blown`` -- a hold and a blown save by MLB's definitions, for seasons MLB's records
      have none of (before 1999): a hold for a reliever in a save situation who records an out and
      leaves never having given up the lead, without finishing the game or getting the save or the
      win (a pitcher gets one or the other, as MLB's own records do); a blown save when the tying
      run scores after he came in to a save situation;
    * ``regained`` -- 1 if his team was ahead at some point after he left the game (for the Holds and
      Saves tab's blown holds: a lost lead won back later);
    * ``w`` / ``l`` -- the official scorer's winning / losing pitcher (1 or 0);
    * ``sv_now`` -- a save by today's rule (:func:`current_rule_save`), for seasons before 1975 whose
      official saves were credited under earlier rules.

    The save is the official scorer's, as Retrosheet records it."""
    it = iter(play_rows)
    header = next(it)
    ix = {name: i for i, name in enumerate(header)}
    i_gid, i_type, i_vh, i_pit, i_runs = ix["gid"], ix["gametype"], ix["vis_home"], ix["pitcher"], ix["runs"]
    i_inn, i_outs = ix["inning"], ix["outs_pre"]
    i_sv, i_sh = ix["score_v"], ix["score_h"]
    i_pre = (ix["br1_pre"], ix["br2_pre"], ix["br3_pre"])
    stints: dict[tuple[str, str], dict] = {}  # (gid, pitcher) -> the score, runners, inning and outs he came
                                              # in to, whether his team stayed ahead while he pitched, his last play
    margins: dict[str, list[int]] = {}  # gid -> the visitors' lead after each play, in order
    for r in it:
        if r[i_type] != "regular":
            continue
        try:
            v, h, runs = int(r[i_sv] or 0), int(r[i_sh] or 0), int(r[i_runs] or 0)
        except ValueError:
            continue
        visitors_field = r[i_vh] == "1"
        lead = (v - h) if visitors_field else (h - v)  # the fielding team's, before the play
        margin = margins.setdefault(r[i_gid], [])
        margin.append((v - h) - runs if visitors_field else (v - h) + runs)  # the visitors' lead after it
        key = (r[i_gid], r[i_pit])
        stint = stints.get(key)
        if stint is None:  # the first batter he faced: the situation he came in to
            stint = stints[key] = {"lead": lead, "runners": sum(1 for i in i_pre if r[i]), "ahead": True,
                                   "inning": int(r[i_inn] or 0), "outs": int(r[i_outs] or 0),
                                   "sign": 1 if visitors_field else -1}
        stint["last"] = len(margin) - 1
        if lead - runs <= 0:
            stint["ahead"] = False  # the tying run scored

    it = iter(pitching_rows)
    header = next(it)
    ix = {name: i for i, name in enumerate(header)}
    games: dict[tuple[str, str], dict] = {}
    for r in it:
        if r[ix["gametype"]] != "regular" or r[ix["stattype"]] != "value":
            continue
        team, gid = r[ix["team"]], r[ix["gid"]]
        game = games.setdefault((team, gid), {"gid": gid, "won": 1 if r[ix["win"]] == "1" else 0, "pitchers": []})
        flag = lambda name: 1 if r[ix[name]] == "1" else 0
        pid = r[ix["id"]]
        outs = int(r[ix["p_ipouts"]] or 0)
        gs, gf, sv, win = flag("p_gs"), flag("p_gf"), flag("save"), flag("wp")
        stint = stints.get((gid, pid))
        sit = None if stint is None else int(not gs and save_situation(stint["lead"], stint["runners"],
                                                                       stint["inning"], stint["outs"], outs))
        regained = 0 if stint is None else int(any(stint["sign"] * m > 0 for m in margins[gid][stint["last"] + 1:]))
        sv_now = int(stint is not None and not gs and current_rule_save(
            stint["lead"], stint["runners"], outs, bool(gf), game["won"] == 1, bool(win), stint["ahead"]))
        game["pitchers"].append((int(r[ix["p_seq"]] or 0),
                                 [pid, outs, gs, gf, sv, sit, None if stint is None else stint["lead"],
                                  int(bool(sit) and stint["ahead"] and outs >= 1 and not gf and not sv_now and not win),
                                  int(bool(sit) and not stint["ahead"]), regained, win, flag("lp"), sv_now]))
    out: dict[str, list[dict]] = {}
    for (team, _gid), game in sorted(games.items(), key=lambda kv: kv[0]):
        game["pitchers"] = [a for _seq, a in sorted(game["pitchers"], key=lambda sa: sa[0])]
        out.setdefault(team, []).append(game)
    return out


def parse_results(rows) -> dict[str, dict]:
    """A ``gameinfo.csv``'s rows -> ``{team: {"w", "l", "rs", "ra"}}`` over the regular season
    (games without a recorded score are skipped)."""
    it = iter(rows)
    header = next(it)
    ix = {name: i for i, name in enumerate(header)}
    teams: dict[str, dict] = {}
    for r in it:
        if r[ix["gametype"]] != "regular":
            continue
        try:
            away_runs, home_runs = int(r[ix["vruns"]]), int(r[ix["hruns"]])
        except ValueError:
            continue
        for team, scored, allowed in ((r[ix["visteam"]], away_runs, home_runs), (r[ix["hometeam"]], home_runs, away_runs)):
            t = teams.setdefault(team, {"w": 0, "l": 0, "rs": 0, "ra": 0})
            t["rs"] += scored
            t["ra"] += allowed
            if scored > allowed:
                t["w"] += 1
            elif scored < allowed:
                t["l"] += 1
    return teams


def parse_names(rows) -> dict[str, tuple[str, str]]:
    """An ``allplayers.csv``'s rows -> ``{player id: (first name, last name)}``."""
    it = iter(rows)
    header = next(it)
    ix = {name: i for i, name in enumerate(header)}
    return {r[ix["id"]]: (r[ix["first"]], r[ix["last"]]) for r in it}


def build_season(season: int, data: bytes) -> dict:
    """A season's downloaded CSV bundle -> the small summary kept on disk:
    ``{"version", "season", "teams": {code: {w, l, rs, ra}}, "runners": [{"id", "name", "first", "last",
    "team", <KEYS>}, ...]}``."""
    rows = _bundle_reader(season, data)
    counts = parse_plays(rows("plays"))
    if not counts:
        raise RetrosheetError(f"Retrosheet has no regular-season play-by-play for {season}")
    names = parse_names(rows("allplayers"))
    runners = []
    for (runner, team), values in sorted(counts.items()):
        first, last = names.get(runner, ("", runner))
        row = {"id": runner, "name": f"{first} {last}".strip(), "first": first, "last": last, "team": team}
        row.update(zip(KEYS, values))
        runners.append(row)
    return {"version": SUMMARY_VERSION, "season": season, "teams": parse_results(rows("gameinfo")),
            "runners": runners}


def build_team_season(season: int, data: bytes) -> dict:
    """A season's downloaded CSV bundle -> the Safe/Out tab's summary kept on disk:
    ``{"version", "season", "teams": {code: {w, l, rs, ra}}, "running": {code: {SAFE_OUT_KEYS}},
    "allowed": {code: {SAFE_OUT_KEYS}}}`` -- what each club's batters did, and what it allowed."""
    rows = _bundle_reader(season, data)
    try:
        running = parse_team_running(rows("plays"))
    except (KeyError, ValueError, IndexError) as exc:
        raise RetrosheetError(f"Retrosheet's {season} play-by-play lacks what Safe/Out needs ({exc})") from exc
    if not running["batting"]:
        raise RetrosheetError(f"Retrosheet has no regular-season play-by-play for {season}")
    return {"version": TEAM_SUMMARY_VERSION, "season": season, "teams": parse_results(rows("gameinfo")),
            "running": running["batting"], "allowed": running["fielding"]}


def build_rbi_season(season: int, data: bytes) -> dict:
    """A season's downloaded CSV bundle -> the Players Batted In tab's summary kept on disk:
    ``{"version", "season", "batted_in": {team: {batter: {runner: [himself, 1st, 2nd, 3rd]}}},
    "names": {player id: [first, last]}}`` (names only of those who appear in it)."""
    rows = _bundle_reader(season, data)
    try:
        batted_in = parse_batted_in(rows("plays"))
    except (KeyError, ValueError, IndexError) as exc:
        raise RetrosheetError(f"Retrosheet's {season} play-by-play lacks who batted in whom ({exc})") from exc
    if not batted_in:
        raise RetrosheetError(f"Retrosheet has no regular-season runs batted in for {season}")
    names = parse_names(rows("allplayers"))
    used = {pid for batters in batted_in.values() for batter, runners in batters.items() for pid in (batter, *runners)}
    return {"version": RBI_SUMMARY_VERSION, "season": season, "batted_in": batted_in,
            "names": {pid: list(names.get(pid, ("", pid))) for pid in sorted(used)}}


def build_staff_season(season: int, data: bytes) -> dict:
    """A season's downloaded CSV bundle -> the Bullpen Support tab's summary kept on disk:
    ``{"version", "season", "games": {team: [game, ...]}, "names": {player id: [first, last]}}``
    (see :func:`parse_staff`)."""
    rows = _bundle_reader(season, data)
    try:
        games = parse_staff(rows("pitching"), rows("plays"))
    except (KeyError, ValueError, IndexError) as exc:
        raise RetrosheetError(f"Retrosheet's {season} records lack the pitchers' appearances ({exc})") from exc
    if not games:
        raise RetrosheetError(f"Retrosheet has no regular-season pitching records for {season}")
    names = parse_names(rows("allplayers"))
    used = {a[0] for team in games.values() for g in team for a in g["pitchers"]}
    return {"version": STAFF_SUMMARY_VERSION, "season": season, "games": games,
            "names": {pid: list(names.get(pid, ("", pid))) for pid in sorted(used)}}


# --------------------------------------------------------------------------- #
#  The Simulator tab's league tables: what happened to the runners on each kind of play  #
# --------------------------------------------------------------------------- #
# A plate appearance ends in one of these.  The batter's and pitcher's MLB lines give how often each
# happens; this play-by-play gives what each does to the runners and outs, base-out state by base-out state.
SIM_SUMMARY_VERSION = 8  # 2: where each ball in play went (``field``); 3: older seasons' outfielder-only hit
#                           locations; 4: who picked each ball up (``pick``), fielders against runners (``fielders``);
#                           5: each club's plate appearances at home, by park, and on the road (``parks``, ``road``);
#                           6: the infielders covering on steal attempts (``tags``, ``tags_club``);
#                           7: plate appearances by the batter's side and the pitcher's hand (``platoon``);
#                           8: intentional walks and sacrifice bunts by situation (``sit``, ``sac``), out of ``trans``;
#                              plate appearances by the home and the visiting batters (``homefield``)
# who covers the base on a steal: second, the second baseman and shortstop (shared); third, the third baseman
STEAL_COVER = {1: ("4", "6"), 2: ("5",)}
# a fielder's counts against runners, per position: extra bases runners could take on hits he picked up and
# took; tag-ups runners could make on fly balls he caught and made; double-play grounders he fielded, turned
ARM_KEYS = ("xb_opp", "xb", "tu_opp", "tu", "dp_opp", "dp")
SIM_FIELDED = ("1B", "2B", "3B", "ROE", "GO", "AO")  # the classes that are balls in play (a home run isn't fielded)
# a ground ball that got through to an outfielder, with no location recorded: the infielders it got past
_GOT_PAST = {"7": "56", "8": "46", "9": "34"}
SIM_CLASSES = ("K", "BB", "HBP", "1B", "2B", "3B", "HR", "ROE", "GO", "AO")
SIM_DESTS = "123HX"  # a runner's (or the batter's) base after the play: 1st, 2nd, 3rd, Home (scored), X (out)


def sim_situation(state: int, outs: int, inning: int, margin: int) -> str:
    """A plate appearance's situation for intentional walks and sacrifices -> ``"<state>.<outs>.<stage>.<margin>"``:
    the stage of the game ``e`` (innings 1-6), ``l`` (7-8) or ``x`` (9 on), and the batting side's margin as
    -2 (behind by 3 or more), -1 (behind by 1-2), 0, 1 (ahead by 1-2) or 2 (ahead by 3 or more)."""
    stage = "e" if inning <= 6 else "l" if inning <= 8 else "x"
    m = -2 if margin <= -3 else -1 if margin < 0 else 0 if margin == 0 else 1 if margin <= 2 else 2
    return f"{state}.{min(outs, 2)}.{stage}.{m}"


def _sim_class(r, ix) -> str | None:
    """Which :data:`SIM_CLASSES` a plate appearance's event row is (``None`` for the rare others:
    catcher's interference and the like)."""
    flag = lambda name: r[ix[name]] == "1"
    if flag("single"):
        return "1B"
    if flag("double"):
        return "2B"
    if flag("triple"):
        return "3B"
    if flag("hr"):
        return "HR"
    if flag("walk") or flag("iw"):
        return "BB"
    if flag("hbp"):
        return "HBP"
    if flag("k"):
        return "K"
    if flag("roe"):
        return "ROE"
    if flag("bip"):
        return "GO" if flag("ground") or flag("bunt") else "AO"
    return None


def field_zone(r, ix, cls: str) -> str | None:
    """Where a ball in play went, as the positions (1-9) whose ground it was, in order: ``"6"`` for the
    shortstop's, ``"56"`` for the hole between third and short, ``"78"`` for the left-center gap.  From
    Retrosheet's hit location (``loc``) where it was recorded; else the first fielder to handle it
    (``firstf``) -- for a ground-ball hit picked up by an outfielder, the infielders it got past instead,
    and for a hit of unknown kind picked up by an outfielder, both (older seasons' "location" for such a hit
    is only the outfielder's number, so it's treated the same way).  ``None`` if neither is known."""
    get = lambda name: r[ix[name]] if name in ix else ""
    digits = "".join(sorted({ch for ch in get("loc") if ch in "123456789"}))
    first = get("firstf").strip()
    # older seasons give a hit's "location" as just the outfielder who picked it up, of unknown kind: that's
    # no location at all
    if digits and not (cls in ("1B", "2B", "3B") and digits == first and first in _GOT_PAST and not get("hittype")):
        return digits
    if first not in tuple("123456789"):
        return None
    if cls in ("1B", "2B", "3B") and first in _GOT_PAST:
        kind = get("hittype")
        if kind in ("G", "BG"):
            return _GOT_PAST[first]
        if not kind:
            return "".join(sorted(_GOT_PAST[first] + first))
    return first


def sim_state(pre) -> int:
    """Runners on base as a number 0-7: 1 = first, 2 = second, 4 = third, added up."""
    return (1 if pre[0] else 0) | (2 if pre[1] else 0) | (4 if pre[2] else 0)


def parse_sim(rows) -> dict:
    """A ``plays.csv``'s rows (the header first) -> the league tables the Simulator plays from:

    * ``trans``: ``{class: {"<state>.<outs>": {outcome: count}}}`` -- for each kind of plate appearance
      (:data:`SIM_CLASSES`) and each base-out state before it, how often each outcome happened.  An
      outcome is four characters, where the batter, the runner on first, on second and on third ended up
      (:data:`SIM_DESTS`, or "-" for an empty base), e.g. ``"1-3H"``: batter on first, runner from first to
      third, runner from third scored.  Read straight from each row's runners before and after the play
      and who scored, so errors, throws and outs on the bases are in it as they happened.  When a play
      makes the third out Retrosheet lists the bases after it as empty and names who was left on, so a
      runner left on is put down as holding his base (and a batter left on as on first) -- only who
      scored and who was out matters once the inning is over;
    * ``adv``: the same for plays between plate appearances other than steals -- wild pitches, passed
      balls, balks, defensive indifference, pickoffs, other advances (the batter's place is always "-");
    * ``pa``: ``{"<state>.<outs>": plate appearances}`` -- how often each base-out state came up, the
      denominator for ``adv``;
    * ``steal``: ``{"1"/"2"/"3": [chances, attempts, stolen]}`` -- a runner on that base with the next one
      open (on third: always) at the start of a plate appearance, as on the Speed tab; an attempt is any
      stolen base or caught stealing by him before the plate appearance ended;
    * ``field``: ``{class: {zone: count}}`` for the balls in play (:data:`SIM_FIELDED`) -- where each went
      (:func:`field_zone`), so the Simulator can put the fielders whose ground it was on the play;
    * ``pick``: ``{class: {zone: {position: count}}}`` -- who picked the ball up (Retrosheet's first fielder,
      1-9), so the Simulator knows whose arm the runners ran against;
    * ``fielders``: ``{"<fielder id> <fielding team>": {position: counts}}`` and ``fielding_league``:
      ``{position: counts}`` -- each fielder against the runners, counts as :data:`ARM_KEYS`: on hits he
      picked up, runners who could take an extra base (as on the Speed tab) and those who did; on fly balls
      he caught with fewer than two out, runners who could tag up and those who advanced; ground balls he
      fielded with a runner on first and fewer than two out, and the double plays;
    * ``parks``: ``{"<park> <home team>": {class: count}}`` -- every plate appearance (both teams batting) in a
      club's home games, by the park they were played in; ``road``: ``{team: {class: count}}`` -- the same in its
      road games.  A park's factors are its home games against the same club's road games;
    * ``tags``: ``{"<fielder id> <fielding team>": {position: [steal attempts, caught]}}`` -- every attempt to
      steal a base he would cover (``STEAL_COVER``) while he was at that position, and how many were caught;
      ``tags_club``: ``{team: {position: [attempts, caught]}}``, the same for the club;
    * ``platoon``: ``{"<bat side><pitch hand>": {class: count}}`` -- every plate appearance by the side the batter
      batted from (a switch hitter's actual side) and the pitcher's hand: ``"LL"``, ``"LR"``, ``"RL"``, ``"RR"``.
      Plate appearances with either unknown aren't counted;
    * ``sit``: ``{situation: [plate appearances, intentional walks, sacrifice bunts]}`` by :func:`sim_situation`;
      ``sac``: ``{"<state>.<outs>": {outcome: count}}`` -- what became of the runners on sacrifice bunts.  The
      Simulator decides these by the situation, so they are left out of ``trans`` (an intentional walk is a walk
      and a sacrifice a ground out there);
    * ``homefield``: ``{"1": {class: count}, "0": {class: count}}`` -- every plate appearance by the home batters
      ("1") and the visitors ("0"), in the same games: the edge of batting at home, beyond the park.

    Only regular-season games count."""
    it = iter(rows)
    header = next(it)
    ix = {name: i for i, name in enumerate(header)}
    i_type, i_pa, i_outs = ix["gametype"], ix["pa"], ix["outs_pre"]
    i_gid, i_inn, i_tb, i_bat = ix["gid"], ix["inning"], ix["top_bot"], ix["batter"]
    i_pre = (ix["br1_pre"], ix["br2_pre"], ix["br3_pre"])
    i_post = (ix["br1_post"], ix["br2_post"], ix["br3_post"])
    i_run = (ix["run1"], ix["run2"], ix["run3"])
    i_run_b = ix["run_b"]
    i_outs_post, i_lob = ix["outs_post"], (ix["lob_id1"], ix["lob_id2"], ix["lob_id3"])
    i_sb = (ix["sb2"], ix["sb3"], ix["sbh"])
    i_cs = (ix["cs2"], ix["cs3"], ix["csh"])
    trans = {c: {} for c in SIM_CLASSES}
    adv: dict[str, dict[str, int]] = {}
    pa_count: dict[str, int] = {}
    steal = {b: [0, 0, 0] for b in ("1", "2", "3")}
    field = {c: {} for c in SIM_FIELDED}
    pick = {c: {} for c in SIM_FIELDED}
    fielders: dict[str, dict[str, list[int]]] = {}
    fielding_league: dict[str, list[int]] = {}
    parks: dict[str, dict[str, int]] = {}
    road: dict[str, dict[str, int]] = {}
    tags: dict[str, dict[str, list[int]]] = {}
    tags_club: dict[str, dict[str, list[int]]] = {}
    platoon: dict[str, dict[str, int]] = {}
    sit: dict[str, list[int]] = {}
    sac: dict[str, dict[str, int]] = {}
    homefield: dict[str, dict[str, int]] = {}
    get = lambda r, name: r[ix[name]] if name in ix else ""

    def dest(who: str, post, scored: bool, start: str, stranded) -> str:
        if scored:
            return "H"
        if who in post:
            return str(post.index(who) + 1)
        if who in stranded:  # the play made the third out: the bases after it are empty, those left on are listed
            return start
        return "X"

    inning_key, new_pa = None, True
    tried: set = set()  # bases a steal was tried from in this plate appearance
    chances: set = set()  # bases that were a steal chance at its start
    for r in it:
        if r[i_type] != "regular":
            continue
        key = (r[i_gid], r[i_inn], r[i_tb])
        if key != inning_key:
            inning_key, new_pa = key, True
        pre = (r[i_pre[0]], r[i_pre[1]], r[i_pre[2]])
        post = (r[i_post[0]], r[i_post[1]], r[i_post[2]])
        if new_pa:
            tried, chances = set(), set()
            for b in (1, 2, 3):
                if pre[b - 1] and (b == 3 or not pre[b]):
                    chances.add(b)
                    steal[str(b)][0] += 1
        is_pa = r[i_pa] == "1"
        new_pa = is_pa
        cell = f"{sim_state(pre)}.{min(int(r[i_outs] or 0), 2)}"
        stranded = {r[i] for i in i_lob if r[i]} if r[i_outs_post] == "3" else ()
        runners = "".join(dest(pre[b], post, bool(r[i_run[b]]), str(b + 1), stranded) if pre[b] else "-"
                          for b in range(3))
        stole = [b for b in (1, 2, 3) if pre[b - 1] and (r[i_sb[b - 1]] == "1" or r[i_cs[b - 1]] == "1")]
        for b in stole:  # every attempt counts against the infielders covering the base
            caught = r[i_cs[b - 1]] == "1"
            team = get(r, "pitteam")
            for digit in STEAL_COVER.get(b, ()):
                who = get(r, f"f{digit}")
                if not who:
                    continue
                mine = tags.setdefault(f"{who} {team}", {}).setdefault(digit, [0, 0])
                club = tags_club.setdefault(team, {}).setdefault(digit, [0, 0])
                for c in (mine, club):
                    c[0] += 1
                    c[1] += caught
        for b in stole:
            if b in chances and b not in tried:
                tried.add(b)
                steal[str(b)][1] += 1
                if r[i_sb[b - 1]] == "1":
                    steal[str(b)][2] += 1
        if is_pa:
            pa_count[cell] = pa_count.get(cell, 0) + 1
            cls = _sim_class(r, ix)
            if cls is None:
                continue
            outcome = dest(r[i_bat], post, bool(r[i_run_b]), "1", stranded) + runners
            if "vis_home" in ix:
                side = homefield.setdefault(r[ix["vis_home"]], {})
                side[cls] = side.get(cls, 0) + 1
            if "score_v" in ix and "vis_home" in ix:
                v, h = int(get(r, "score_v") or 0), int(get(r, "score_h") or 0)
                margin = h - v if r[ix["vis_home"]] == "1" else v - h
                here = sit.setdefault(sim_situation(sim_state(pre), int(r[i_outs] or 0), int(r[i_inn] or 1),
                                                    margin), [0, 0, 0])
                here[0] += 1
                if get(r, "iw") == "1":
                    here[1] += 1
                elif get(r, "sh") == "1":
                    here[2] += 1
                    table = sac.setdefault(cell, {})
                    table[outcome] = table.get(outcome, 0) + 1
            if get(r, "iw") != "1" and get(r, "sh") != "1":
                table = trans[cls].setdefault(cell, {})
                table[outcome] = table.get(outcome, 0) + 1
            hands = get(r, "bathand") + get(r, "pithand")
            if hands in ("LL", "LR", "RL", "RR"):
                mine = platoon.setdefault(hands, {})
                mine[cls] = mine.get(cls, 0) + 1
            if "site" in ix and "vis_home" in ix:
                batting_home = r[ix["vis_home"]] == "1"
                home = get(r, "batteam") if batting_home else get(r, "pitteam")
                away = get(r, "pitteam") if batting_home else get(r, "batteam")
                here = parks.setdefault(f"{r[ix['site']]} {home}", {})
                here[cls] = here.get(cls, 0) + 1
                there = road.setdefault(away, {})
                there[cls] = there.get(cls, 0) + 1
            if cls in field:
                zone = field_zone(r, ix, cls)
                first = get(r, "firstf").strip()
                if zone:
                    field[cls][zone] = field[cls].get(zone, 0) + 1
                    picked = pick[cls].setdefault(zone, {})
                    picked[first] = picked.get(first, 0) + 1
                if first in POSITION_DIGITS:
                    _against_runners(r, ix, cls, first, pre, post, stranded, fielders, fielding_league)
        elif not stole and runners != "".join(str(b + 1) if pre[b] else "-" for b in range(3)):
            table = adv.setdefault(cell, {})  # something moved (or someone was put out) between pitches
            outcome = "-" + runners
            table[outcome] = table.get(outcome, 0) + 1
    return {"trans": trans, "adv": adv, "pa": pa_count, "steal": steal, "field": field, "pick": pick,
            "fielders": fielders, "fielding_league": fielding_league, "parks": parks, "road": road,
            "tags": tags, "tags_club": tags_club, "platoon": platoon, "sit": sit, "sac": sac,
            "homefield": homefield}


POSITION_DIGITS = tuple("123456789")


def _against_runners(r, ix, cls: str, first: str, pre, post, stranded, fielders: dict, league: dict):
    """Count a ball in play against the fielder who picked it up (position ``first``): see ``parse_sim``."""
    get = lambda name: r[ix[name]] if name in ix else ""
    who = get("pitcher") if first == "1" else get(f"f{first}")
    if not who:
        return
    mine = fielders.setdefault(f"{who} {get('pitteam')}", {}).setdefault(first, [0] * len(ARM_KEYS))
    lg = league.setdefault(first, [0] * len(ARM_KEYS))

    def bump(i: int):
        mine[i] += 1
        lg[i] += 1

    def reached(start: int) -> int | None:  # where the runner from ``start`` ended up, or None (out)
        runner = pre[start - 1]
        if get(f"run{start}"):
            return 4
        if runner in post:
            return post.index(runner) + 1
        return start if runner in stranded else None

    outs = int(get("outs_pre") or 0)
    event = get("event")
    hit_bases = {"1B": 1, "2B": 2}.get(cls, 0)
    if hit_bases:
        for start in (1, 2, 3):
            if not pre[start - 1] or start + hit_bases >= 4:
                continue  # nobody there, or he scores anyway
            bump(0)
            end = reached(start)
            if end is not None and end > start + hit_bases and not _error_advance(event, start):
                bump(1)
    elif cls == "AO" and outs < 2:
        for start in (1, 2, 3):
            if not pre[start - 1] or not (start == 3 or not pre[start]):
                continue  # a tag-up chance: on third, or the next base open
            bump(2)
            end = reached(start)
            if end is not None and end > start and not _error_advance(event, start):
                bump(3)
    elif cls == "GO" and pre[0] and outs < 2:
        bump(4)
        if get("gdp") == "1":
            bump(5)


def build_sim_season(season: int, data: bytes) -> dict:
    """A season's downloaded CSV bundle -> the Simulator tab's league tables (:func:`parse_sim`)."""
    rows = _bundle_reader(season, data)
    try:
        tables = parse_sim(rows("plays"))
    except (KeyError, ValueError, IndexError) as exc:
        raise RetrosheetError(f"Retrosheet's {season} play-by-play lacks what the Simulator needs ({exc})") from exc
    if not tables["pa"]:
        raise RetrosheetError(f"Retrosheet has no regular-season play-by-play for {season}")
    names = parse_names(rows("allplayers"))
    fielders = []
    for key, positions in sorted(tables["fielders"].items()):
        who, team = key.split(" ", 1)
        first, last = names.get(who, ("", who))
        fielders.append({"id": who, "team": team, "name": f"{first} {last}".strip(), "first": first, "last": last,
                         "pos": positions})
    return dict(tables, fielders=fielders, teams=parse_results(rows("gameinfo")), version=SIM_SUMMARY_VERSION,
                season=season)


def _bundle_reader(season: int, data: bytes):
    """-> ``rows(name)``: a CSV reader over one file of a season's bundle."""
    try:
        bundle = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise RetrosheetError(f"Retrosheet's {season} download isn't a zip file") from exc

    def rows(name: str):
        try:
            handle = bundle.open(f"{season}{name}.csv")
        except KeyError as exc:
            raise RetrosheetError(f"Retrosheet's {season} bundle has no {name} file") from exc
        return csv.reader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
    return rows


class LocalBundle(bytes):
    """This year's bundle made on the Uptodate tab from MLB's games, standing in for Retrosheet's until
    Retrosheet publishes the season; ``stamp`` says which version of the file it was."""
    stamp = ""


def uptodate_file(season: int) -> str | None:
    """The Uptodate tab's file for ``season``, if there is one."""
    try:
        import uptodate
    except ImportError:
        return None
    path = uptodate.default_path(season)
    return path if os.path.exists(path) else None


def _stamp(path: str) -> str:
    st = os.stat(path)
    return f"{int(st.st_mtime)}-{st.st_size}"


_PUBLISHED: dict[int, bool] = {}  # season -> has Retrosheet published it (asked once a session)


def _stale(cached: dict | None, season: int | None) -> bool:
    """A summary built from the Uptodate file is out of date once the file has changed or gone, or
    Retrosheet has published the season itself (whose file is then used instead)."""
    if not cached or not cached.get("uptodate") or season is None:
        return False
    local = uptodate_file(season)
    if local is None or _stamp(local) != cached["uptodate"]:
        return True
    if season not in _PUBLISHED:
        try:
            import uptodate
            _PUBLISHED[season] = uptodate.retrosheet_has(season)
        except ImportError:
            _PUBLISHED[season] = False
    return _PUBLISHED[season]


# Who wants to know about downloads (the window's status bar): each is called as ``hook(what, got, total, done)``
# -- what is being downloaded, the bytes so far, the size if the server gave it (else None), and whether it has
# finished (or failed).  Called from whatever thread is downloading; a hook must not raise.
DOWNLOAD_HOOKS: list = []


def notify_download(what: str, got: int = 0, total: int | None = None, done: bool = False):
    for hook in list(DOWNLOAD_HOOKS):
        try:
            hook(what, got, total, done)
        except Exception:  # (a closed window's hook) -- a download must never fail over its notice
            pass


def _read_reporting(response, what: str) -> bytes:
    """A response's body, read in pieces with :func:`notify_download` told how far it has got."""
    total = int(response.headers.get("Content-Length") or 0) or None
    chunks, got, last = [], 0, time.monotonic()
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
        if time.monotonic() - last >= 0.5:
            notify_download(what, got, total)
            last = time.monotonic()
    return b"".join(chunks)


def download_season(season: int) -> bytes:
    """One season's CSV bundle from retrosheet.org (4-10 MB) -- or, for a season Retrosheet hasn't
    published yet, the Uptodate tab's file if one has been made (a :class:`LocalBundle`).  The download is
    announced to :data:`DOWNLOAD_HOOKS`."""
    request = urllib.request.Request(URL.format(y=season), headers={"User-Agent": _UA})
    what = f"Retrosheet's {season} play-by-play"
    try:
        notify_download(what)
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                return _read_reporting(response, what)
        finally:
            notify_download(what, done=True)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            local = uptodate_file(season)
            if local:
                try:
                    with open(local, "rb") as fh:
                        bundle = LocalBundle(fh.read())
                    bundle.stamp = _stamp(local)
                    return bundle
                except OSError:
                    pass
            hint = ""
            try:
                import uptodate  # noqa: F401
                from datetime import date
                if season >= date.today().year:
                    hint = "; the Uptodate tab can make this year's from MLB's games"
            except ImportError:
                pass
            raise RetrosheetError(f"Retrosheet has no play-by-play for {season} (yet){hint}") from exc
        raise RetrosheetError(f"Retrosheet returned HTTP {exc.code} for {season}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RetrosheetError(f"cannot reach retrosheet.org ({exc})") from exc


def summary_path(cache_dir: str, season: int) -> str:
    return os.path.join(cache_dir, "retrosheet", f"{season}.json")


def is_cached(cache_dir: str, season: int) -> bool:
    return _read(summary_path(cache_dir, season), SUMMARY_VERSION, season) is not None


def team_summary_path(cache_dir: str, season: int) -> str:
    return os.path.join(cache_dir, "retrosheet", f"{season}-teams.json")


def is_team_cached(cache_dir: str, season: int) -> bool:
    return _read(team_summary_path(cache_dir, season), TEAM_SUMMARY_VERSION, season) is not None


def rbi_summary_path(cache_dir: str, season: int) -> str:
    return os.path.join(cache_dir, "retrosheet", f"{season}-rbi.json")


def is_rbi_cached(cache_dir: str, season: int) -> bool:
    return _read(rbi_summary_path(cache_dir, season), RBI_SUMMARY_VERSION, season) is not None


def staff_summary_path(cache_dir: str, season: int) -> str:
    return os.path.join(cache_dir, "retrosheet", f"{season}-staff.json")


def is_staff_cached(cache_dir: str, season: int) -> bool:
    return _read(staff_summary_path(cache_dir, season), STAFF_SUMMARY_VERSION, season) is not None


def sim_summary_path(cache_dir: str, season: int) -> str:
    return os.path.join(cache_dir, "retrosheet", f"{season}-sim.json")


def is_sim_cached(cache_dir: str, season: int) -> bool:
    return _read(sim_summary_path(cache_dir, season), SIM_SUMMARY_VERSION, season) is not None


def _read(path: str, version: int, season: int | None = None) -> dict | None:
    """A cached summary, or None if there's none, it's an older version, or (``season`` given) it was
    built from an Uptodate file that has since changed."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        return None
    if cached.get("version") != version or _stale(cached, season):
        return None
    return cached


def _save_file(path: str, summary: dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(summary, fh)
        os.replace(tmp, path)
    except OSError:
        pass  # a broken cache must never break a lookup


def _download_and_build(season: int, cache_dir: str, download) -> tuple[dict, dict | None, dict | None, dict | None]:
    """Download a season once and keep all its summaries -- the runners' (Speed tab), the teams'
    (Safe/Out tab), who batted in whom (Players Batted In tab), the pitchers' appearances (Holds
    and Saves tab) and the league's play tables (Simulator tab) -- so no tab has to download it again."""
    data = download(season)
    stamp = getattr(data, "stamp", "")  # an Uptodate file's version, kept with each summary made from it

    def _save(path, summary):
        if stamp and isinstance(summary, dict):
            summary = dict(summary, uptodate=stamp)
        _save_file(path, summary)
    runners = build_season(season, data)
    _save(summary_path(cache_dir, season), runners)
    try:
        teams = build_team_season(season, data)
        _save(team_summary_path(cache_dir, season), teams)
    except RetrosheetError:
        teams = None
    try:
        batted_in = build_rbi_season(season, data)
        _save(rbi_summary_path(cache_dir, season), batted_in)
    except RetrosheetError:
        batted_in = None
    try:
        staff = build_staff_season(season, data)
        _save(staff_summary_path(cache_dir, season), staff)
    except RetrosheetError:
        staff = None
    try:
        _save(sim_summary_path(cache_dir, season), build_sim_season(season, data))
    except RetrosheetError:
        pass  # (load_sim_season says so when it's wanted)
    return runners, teams, batted_in, staff


def load_season(season: int, cache_dir: str, download=download_season) -> dict:
    """A season's runner summary, from the cache or -- once -- built from Retrosheet's download and
    saved.  (Nothing about a finished season changes, so a cached one is kept for good.)"""
    cached = _read(summary_path(cache_dir, season), SUMMARY_VERSION, season)
    return cached if cached is not None else _download_and_build(season, cache_dir, download)[0]


def load_team_season(season: int, cache_dir: str, download=download_season) -> dict:
    """A season's team summary (:func:`build_team_season`), from the cache or built from Retrosheet's
    download.  Seasons the Speed tab fetched before this summary existed are downloaded once more."""
    cached = _read(team_summary_path(cache_dir, season), TEAM_SUMMARY_VERSION, season)
    if cached is not None:
        return cached
    _runners, teams, _batted_in, _staff = _download_and_build(season, cache_dir, download)
    if teams is None:
        raise RetrosheetError(f"Retrosheet has no regular-season play-by-play for {season}")
    return teams


def load_rbi_season(season: int, cache_dir: str, download=download_season) -> dict:
    """A season's who-batted-in-whom summary (:func:`build_rbi_season`), from the cache or built from
    Retrosheet's download.  Seasons fetched before this summary existed are downloaded once more."""
    cached = _read(rbi_summary_path(cache_dir, season), RBI_SUMMARY_VERSION, season)
    if cached is not None:
        return cached
    _runners, _teams, batted_in, _staff = _download_and_build(season, cache_dir, download)
    if batted_in is None:
        raise RetrosheetError(f"Retrosheet has no regular-season runs batted in for {season}")
    return batted_in


def load_staff_season(season: int, cache_dir: str, download=download_season) -> dict:
    """A season's pitchers' appearances (:func:`build_staff_season`), from the cache or built from
    Retrosheet's download.  Seasons fetched before this summary existed are downloaded once more."""
    cached = _read(staff_summary_path(cache_dir, season), STAFF_SUMMARY_VERSION, season)
    if cached is not None:
        return cached
    _runners, _teams, _batted_in, staff = _download_and_build(season, cache_dir, download)
    if staff is None:
        raise RetrosheetError(f"Retrosheet has no regular-season pitching records for {season}")
    return staff


def load_sim_season(season: int, cache_dir: str, download=download_season) -> dict:
    """A season's league play tables (:func:`build_sim_season`), from the cache or built from Retrosheet's
    download.  Seasons fetched before these tables existed are downloaded once more."""
    cached = _read(sim_summary_path(cache_dir, season), SIM_SUMMARY_VERSION, season)
    if cached is not None:
        return cached
    _download_and_build(season, cache_dir, download)
    cached = _read(sim_summary_path(cache_dir, season), SIM_SUMMARY_VERSION, season)
    if cached is None:
        raise RetrosheetError(f"Retrosheet has no regular-season play-by-play for {season}")
    return cached


def match_teams(retro: dict[str, dict], api: dict[int, dict], close: bool = True) -> dict[int, str]:
    """Which Retrosheet team code is which MLB team -- ``{team id: code}`` -- from the season's
    results: wins, losses, runs scored and allowed (``retro`` is :func:`parse_results`, ``api``
    ``{team id: {"w", "l", "rs", "ra"}}``).  Exact matches first; then, with ``close``, for what is
    left, the closest result if it is close (a season whose accounts are partly reconstructed can
    differ by a game or a few runs)."""
    def distance(a: dict, b: dict) -> float:
        return abs(a["w"] - b["w"]) + abs(a["l"] - b["l"]) + (abs(a["rs"] - b["rs"]) + abs(a["ra"] - b["ra"])) / 10

    out: dict[int, str] = {}
    free_codes, free_ids = set(retro), set(api)
    for tid in list(free_ids):
        exact = [c for c in free_codes if all(retro[c][k] == api[tid][k] for k in ("w", "l", "rs", "ra"))]
        if len(exact) == 1:
            out[tid] = exact[0]
            free_codes.discard(exact[0])
            free_ids.discard(tid)
    if not close:
        return out
    pairs = sorted((distance(retro[c], api[t]), t, c) for t in free_ids for c in free_codes)
    for dist, tid, code in pairs:
        if dist <= 30 and tid in free_ids and code in free_codes:
            out[tid] = code
            free_ids.discard(tid)
            free_codes.discard(code)
    return out


def match_rosters(retro: dict[str, set], api: dict[int, list[set]], minimum: int = 3) -> dict[int, str]:
    """Which Retrosheet team code is which MLB team -- ``{team id: code}`` -- by the players they
    share: ``retro`` is ``{code: {name key, ...}}`` and ``api`` ``{team id: [{the name keys one
    player could have}, ...]}``.  For when the results can't say -- the Negro Leagues, whose games
    MLB and Retrosheet count differently.  A pair needs ``minimum`` players in common, and at least
    half of the smaller roster; the strongest pairs are made first, each team and code once."""
    pairs = []
    for tid, players in api.items():
        for code, names in retro.items():
            shared = sum(1 for keys in players if keys & names)
            if shared >= minimum and 2 * shared >= min(len(players), len(names)):
                pairs.append((-shared, tid, code))
    out: dict[int, str] = {}
    for _shared, tid, code in sorted(pairs):
        if tid not in out and code not in out.values():
            out[tid] = code
    return out


# --------------------------------------------------------------------------- #
#  Custom-stat variables from the other summaries                             #
# --------------------------------------------------------------------------- #
# The Bullpen Support tab's game records (:func:`build_staff_season`) per pitcher and team: (label used as
# a custom-stat variable, key in his row).  All counts, added across games.  Holds and blown saves here
# are counted by MLB's definitions (MLB's own records have them only from 1999 -- its official figures are
# the Pitching view's HLD and BS); saves are the official scorer's, as Retrosheet records them.
RELIEF_VARIABLES = [
    ("Starts", "gs"), ("Outs in Starts", "st_outs"), ("Wins in Starts", "st_w"), ("Losses in Starts", "st_l"),
    ("No Decisions, Team Won", "st_nd_w"), ("No Decisions, Team Lost", "st_nd_l"),
    ("Team Wins in Starts", "st_tw"), ("Team Losses in Starts", "st_tl"),
    ("Relief Appearances", "rel"), ("Outs in Relief", "rel_outs"), ("Relief Wins", "rel_w"),
    ("Relief Losses", "rel_l"), ("Games Finished in Relief", "rel_gf"),
    ("Save Situations", "sit"), ("Outs in Save Situations", "sit_outs"),
    ("Holds by Definition", "hold"), ("Saves", "sv"), ("Saves by Current Rule", "sv_now"),
    ("Blown Saves by Definition", "blown"), ("Blown Holds", "blown_holds"),
    ("Blown Saves Excluding Blown Holds", "blown_saves"),
    ("Holds from Save Situations", "sit_hold"), ("Saves from Save Situations", "sit_sv"),
]
RELIEF_KEYS = [key for _label, key in RELIEF_VARIABLES]


def relief_counts(games: list[dict]) -> dict[str, dict[str, int]]:
    """One team's games from a staff summary -> ``{pitcher: {RELIEF_KEYS}}``.  A start's decisions
    and the team's result go to the starter; relief appearances' outs, decisions, save situations,
    holds and saves to each reliever.  A blown hold is a blown save by a reliever who didn't finish
    the game when his team later took the lead back (as on the Bullpen Support tab); the rest stay
    blown saves, so the two add up to all blown saves."""
    out: dict[str, dict[str, int]] = {}
    for game in games:
        won = game["won"]
        for values in game["pitchers"]:
            a = dict(zip(APPEARANCE_FIELDS, values))
            c = out.get(a["pitcher"])
            if c is None:
                c = out[a["pitcher"]] = {k: 0 for k in RELIEF_KEYS}
            if a["gs"]:
                c["gs"] += 1
                c["st_outs"] += a["outs"]
                c["st_w"] += a["w"]
                c["st_l"] += a["l"]
                if not a["w"] and not a["l"]:
                    c["st_nd_w" if won else "st_nd_l"] += 1
                c["st_tw" if won else "st_tl"] += 1
                continue
            c["rel"] += 1
            c["rel_outs"] += a["outs"]
            c["rel_w"] += a["w"]
            c["rel_l"] += a["l"]
            c["rel_gf"] += a["gf"]
            blown_hold = a["blown"] if a["blown"] and not a["gf"] and a["regained"] else 0
            c["hold"] += a["hold"]
            c["sv"] += a["sv"]
            c["sv_now"] += a["sv_now"]
            c["blown"] += a["blown"]
            c["blown_holds"] += blown_hold
            c["blown_saves"] += a["blown"] - blown_hold
            if a["save_sit"]:
                c["sit"] += 1
                c["sit_outs"] += a["outs"]
                c["sit_hold"] += a["hold"]
                c["sit_sv"] += a["sv"]
    return out


# The Players Batted In tab's summary (:func:`build_rbi_season`) per batter and team.
BATTED_IN_VARIABLES = [
    ("Runs Batted In", "bi_total"), ("Drove In Himself", "bi_self"), ("Drove In from 1st", "bi_1"),
    ("Drove In from 2nd", "bi_2"), ("Drove In from 3rd", "bi_3"), ("Different Runners Driven In", "bi_runners"),
]
BATTED_IN_KEYS = [key for _label, key in BATTED_IN_VARIABLES]


def batted_in_counts(batters: dict[str, dict[str, list[int]]]) -> dict[str, dict[str, int]]:
    """One team's ``{batter: {runner: [himself, from 1st, from 2nd, from 3rd]}}`` -> ``{batter:
    {BATTED_IN_KEYS}}``: his runs batted in by the base each runner started from, and how many
    different runners (himself included, for his home runs) they were."""
    out = {}
    for batter, runners in batters.items():
        own, from1, from2, from3 = (sum(counts[i] for counts in runners.values()) for i in range(4))
        out[batter] = {"bi_total": own + from1 + from2 + from3, "bi_self": own, "bi_1": from1, "bi_2": from2,
                       "bi_3": from3, "bi_runners": sum(1 for counts in runners.values() if any(counts))}
    return out


# The Safe/Out tab's summary (:func:`build_team_season`) per club, at Team level: what its batters did
# ("running") and what it allowed ("allowed").
SAFE_OUT_VARIABLES = [
    ("Outs", "outs"), ("Reached 1st", "reach1"), ("Reached 2nd", "reach2"), ("Reached 3rd", "reach3"),
    ("Scored", "runs"), ("Placed Runners", "placed"),
]
