#!/usr/bin/env python3
"""Offline tests for BallSheet's data layer and xlsx writer.

No network: the API-shaped dicts below are trimmed real responses.
Run with:  python3 -m unittest -v   (or just  python3 test_core.py)
"""
import copy
import csv
import io
import json
import math
import os
import random
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

import formula
import infochart
import infoquery
import naturalzone
import stadiums
import mlbstats
import retrosheet
import simulator
import statcastview
import uptodate
from xlsxlite import Workbook, _col_letter

TEAMS_PAYLOAD = {
    "teams": [
        {"id": 147, "name": "New York Yankees", "abbreviation": "NYY",
         "teamName": "Yankees", "locationName": "New York", "firstYearOfPlay": "1903"},
        {"id": 133, "name": "Athletics", "abbreviation": "ATH",
         "teamName": "Athletics", "locationName": "", "firstYearOfPlay": "1901"},
        {"id": 999, "name": "Expansion Club", "abbreviation": "EXP",
         "teamName": "Club", "locationName": "Somewhere", "firstYearOfPlay": None},
    ]
}

TOTALS_PAYLOAD = {
    "stats": [{
        "group": {"displayName": "hitting"},
        "splits": [{"season": "1998", "stat": {
            "gamesPlayed": 162, "runs": 965, "homeRuns": 207,
            "avg": ".288", "obp": ".364", "ops": ".824"}}],
    }]
}

PLAYERS_PAYLOAD = {
    "stats": [{
        "group": {"displayName": "hitting"},
        "splits": [
            {"stat": {"plateAppearances": 78, "homeRuns": 1, "avg": ".380"},
             "player": {"id": 111792, "fullName": "Homer Bush"}},
            {"stat": {"plateAppearances": 700, "homeRuns": 28, "avg": ".339"},
             "player": {"id": 116539, "fullName": "Derek Jeter"}},
        ],
    }]
}

PITCHERS_PAYLOAD = {
    "stats": [{
        "group": {"displayName": "pitching"},
        "splits": [
            {"stat": {"inningsPitched": "12.1", "wins": 1},
             "player": {"id": 1, "fullName": "Short Reliever"}},
            {"stat": {"inningsPitched": "216.0", "wins": 20},
             "player": {"id": 2, "fullName": "David Cone"}},
            {"stat": {"inningsPitched": "12.2", "wins": 0},
             "player": {"id": 3, "fullName": "Mop Up"}},
        ],
    }]
}


FIELDERS_PAYLOAD = {
    "stats": [{
        "group": {"displayName": "fielding"},
        "splits": [
            {"stat": {"innings": "1044.2", "putOuts": 240, "errors": 10, "fielding": ".978"},
             "player": {"id": 10, "fullName": "Derek Jeter"},
             "position": {"abbreviation": "SS", "name": "Shortstop"}},
            {"stat": {"innings": "9.0", "putOuts": 1, "errors": 0, "fielding": "1.000"},
             "player": {"id": 10, "fullName": "Derek Jeter"},
             "position": {"abbreviation": "3B", "name": "Third Base"}},
            {"stat": {"innings": "132.1", "putOuts": 300, "errors": 2, "fielding": ".995"},
             "player": {"id": 11, "fullName": "Tino Martinez"},
             "position": {"abbreviation": "1B", "name": "First Base"}},
        ],
    }]
}


class ParseTeams(unittest.TestCase):
    def test_sorted_and_typed(self):
        teams = mlbstats.parse_teams(TEAMS_PAYLOAD)
        self.assertEqual([t["name"] for t in teams],
                         ["Athletics", "Expansion Club", "New York Yankees"])
        yanks = next(t for t in teams if t["id"] == 147)
        self.assertEqual(yanks["first_year"], 1903)
        self.assertEqual(yanks["abbrev"], "NYY")

    def test_missing_first_year_is_none(self):
        teams = mlbstats.parse_teams(TEAMS_PAYLOAD)
        exp = next(t for t in teams if t["id"] == 999)
        self.assertIsNone(exp["first_year"])

    def test_empty_payload(self):
        self.assertEqual(mlbstats.parse_teams({}), [])


TEAMS_1953 = {
    "teams": [
        {"id": 110, "name": "St. Louis Browns", "abbreviation": "SLB",
         "teamName": "Browns", "locationName": "St. Louis", "firstYearOfPlay": "1901"},
        {"id": 119, "name": "Brooklyn Dodgers", "abbreviation": "BRO",
         "teamName": "Dodgers", "locationName": "Brooklyn", "firstYearOfPlay": "1884"},
    ]
}


class HistoricalNames(unittest.TestCase):
    def setUp(self):
        self._orig_dir = mlbstats.CACHE_DIR
        self._orig_get = mlbstats._http_get
        mlbstats.CACHE_DIR = tempfile.mkdtemp()
        mlbstats._http_get = lambda url: TEAMS_1953

    def tearDown(self):
        mlbstats.CACHE_DIR = self._orig_dir
        mlbstats._http_get = self._orig_get

    def test_parse_gives_era_name(self):
        teams = {t["id"]: t for t in mlbstats.parse_teams(TEAMS_1953)}
        self.assertEqual(teams[110]["name"], "St. Louis Browns")
        self.assertEqual(teams[110]["abbrev"], "SLB")

    def test_get_team_map_keyed_by_franchise_id(self):
        era = mlbstats.get_team_map(1953, ttl=0)
        self.assertEqual(set(era), {110, 119})
        self.assertEqual(era[110]["name"], "St. Louis Browns")
        self.assertEqual(era[119]["nickname"], "Dodgers")


class ParseStats(unittest.TestCase):
    def test_team_totals(self):
        stat = mlbstats.parse_team_totals(TOTALS_PAYLOAD)
        self.assertEqual(stat["homeRuns"], 207)
        self.assertEqual(stat["avg"], ".288")

    def test_team_totals_empty(self):
        self.assertEqual(mlbstats.parse_team_totals({"stats": []}), {})

    def test_player_rows_carry_name_and_id(self):
        rows = mlbstats.parse_player_rows(PLAYERS_PAYLOAD)
        self.assertEqual(rows[0]["_name"], "Homer Bush")
        self.assertEqual(rows[0]["_id"], 111792)
        self.assertEqual(rows[1]["homeRuns"], 28)


class Fielding(unittest.TestCase):
    def test_pos_is_extracted_per_split(self):
        rows = mlbstats.parse_player_rows(FIELDERS_PAYLOAD)
        self.assertEqual([(r["_name"], r["_pos"]) for r in rows],
                         [("Derek Jeter", "SS"), ("Derek Jeter", "3B"),
                          ("Tino Martinez", "1B")])

    def test_batting_split_has_blank_pos(self):
        rows = mlbstats.parse_player_rows(PLAYERS_PAYLOAD)
        self.assertTrue(all(r["_pos"] == "" for r in rows))

    def test_fielding_group_registered(self):
        self.assertIn("fielding", mlbstats.COLUMNS)
        self.assertIn("fielding", mlbstats.TEAM_FIELDS)
        self.assertEqual(mlbstats.FIELDING_COLUMNS[0], ("Player", "_name"))
        self.assertEqual(mlbstats.FIELDING_COLUMNS[1], ("Pos", "_pos"))

    def test_get_player_stats_sorts_fielding_by_innings(self):
        payload = FIELDERS_PAYLOAD
        orig = mlbstats._http_get
        odir = mlbstats.CACHE_DIR
        mlbstats._http_get = lambda url: payload
        mlbstats.CACHE_DIR = tempfile.mkdtemp()
        try:
            rows = mlbstats.get_player_stats(147, 1998, "fielding", ttl=0)
        finally:
            mlbstats._http_get, mlbstats.CACHE_DIR = orig, odir
        self.assertEqual([r["_pos"] for r in rows], ["SS", "1B", "3B"])


class ValueHelpers(unittest.TestCase):
    def test_as_number(self):
        self.assertEqual(mlbstats.as_number(5), 5.0)
        self.assertAlmostEqual(mlbstats.as_number(".288"), 0.288)
        self.assertAlmostEqual(mlbstats.as_number("-.05"), -0.05)
        self.assertAlmostEqual(mlbstats.as_number("3.10"), 3.10)
        self.assertIsNone(mlbstats.as_number(""))
        self.assertIsNone(mlbstats.as_number("-.--"))
        self.assertIsNone(mlbstats.as_number("Homer Bush"))
        self.assertIsNone(mlbstats.as_number(True))

    def test_as_number_ip_is_plain_float(self):
        # thirds are the job of innings_to_outs, not as_number
        self.assertAlmostEqual(mlbstats.as_number("172.1"), 172.1)

    def test_innings_to_outs(self):
        self.assertEqual(mlbstats.innings_to_outs("12.1"), 37)
        self.assertEqual(mlbstats.innings_to_outs("216.0"), 648)
        self.assertEqual(mlbstats.innings_to_outs("0.2"), 2)
        self.assertEqual(mlbstats.innings_to_outs(""), 0)


class Sorting(unittest.TestCase):
    def test_pitchers_sorted_by_workload(self):
        rows = mlbstats.parse_player_rows(PITCHERS_PAYLOAD)
        rows.sort(key=lambda r: mlbstats.innings_to_outs(r.get("inningsPitched", 0)),
                  reverse=True)
        self.assertEqual([r["_name"] for r in rows],
                         ["David Cone", "Mop Up", "Short Reliever"])


class Cache(unittest.TestCase):
    def setUp(self):
        self._orig = mlbstats.CACHE_DIR
        self.tmp = tempfile.mkdtemp()
        mlbstats.CACHE_DIR = self.tmp

    def tearDown(self):
        mlbstats.CACHE_DIR = self._orig

    def test_ttl_none_serves_cache_without_network(self):
        def boom(url):
            raise AssertionError(f"network was hit for {url}")

        orig = mlbstats._http_get
        mlbstats._http_get = boom
        try:
            path = mlbstats._cache_path(
                "https://statsapi.mlb.com/api/v1/teams?season=1927&sportId=1")
            mlbstats._write_cache(path, "x", {"teams": [{"id": 1}]})
            got = mlbstats.api_get("/teams", {"season": 1927, "sportId": 1}, ttl=None)
            self.assertEqual(got, {"teams": [{"id": 1}]})
        finally:
            mlbstats._http_get = orig

    def test_stale_cache_used_when_network_fails(self):
        def boom(url):
            raise mlbstats.MLBStatsError("offline")

        orig = mlbstats._http_get
        mlbstats._http_get = boom
        try:
            path = mlbstats._cache_path(
                "https://statsapi.mlb.com/api/v1/seasons?sportId=1")
            mlbstats._write_cache(path, "x", {"seasons": [{"seasonId": "2020"}]})
            got = mlbstats.api_get("/seasons", {"sportId": 1}, ttl=0)
            self.assertEqual(got["seasons"][0]["seasonId"], "2020")
        finally:
            mlbstats._http_get = orig

    def test_clear_cache_counts_files(self):
        for i in range(3):
            mlbstats._write_cache(mlbstats._cache_path(str(i)), str(i), {"i": i})
        self.assertEqual(mlbstats.clear_cache(), 3)
        self.assertEqual(mlbstats.clear_cache(), 0)


class XlsxLite(unittest.TestCase):
    def test_col_letter(self):
        self.assertEqual(_col_letter(0), "A")
        self.assertEqual(_col_letter(25), "Z")
        self.assertEqual(_col_letter(26), "AA")

    def test_roundtrip_values_and_sheets(self):
        wb = Workbook()
        ws = wb.add_sheet("1998")
        ws.append(["Player", "HR", "AVG"], header=True)
        ws.append(["Derek Jeter", 19, 0.324])
        ws.append(["Tino Martinez", 28, 0.281])
        wb.add_sheet("1999")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.xlsx")
            wb.save(path)
            self.assertTrue(zipfile.is_zipfile(path))
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                self.assertIn("xl/worksheets/sheet1.xml", names)
                self.assertIn("xl/worksheets/sheet2.xml", names)
                sheet1 = zf.read("xl/worksheets/sheet1.xml").decode()
                wb_xml = zf.read("xl/workbook.xml").decode()
        self.assertIn('name="1998"', wb_xml)
        self.assertIn("Derek Jeter", sheet1)
        self.assertIn("<v>19</v>", sheet1)
        self.assertIn("0.324", sheet1)
        # header cells carry the bold style index
        root = ET.fromstring(sheet1)
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        first_row = root.find(f"{ns}sheetData/{ns}row")
        self.assertTrue(all(c.get("s") == "1" for c in first_row))

    def test_sheet_name_sanitised_and_unique(self):
        wb = Workbook()
        a = wb.add_sheet("A/B:C" * 10)
        b = wb.add_sheet("A/B:C" * 10)
        self.assertLessEqual(len(a.name), 31)
        self.assertNotEqual(a.name, b.name)
        for ch in "[]:*?/\\":
            self.assertNotIn(ch, a.name)

    def test_xml_escaped(self):
        wb = Workbook()
        ws = wb.add_sheet("s")
        ws.append(["Fic <A&M> \"x\""])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "e.xlsx")
            wb.save(path)
            with zipfile.ZipFile(path) as zf:
                xml = zf.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("&lt;A&amp;M&gt;", xml)
        ET.fromstring(xml)  # still well-formed


# Chris Bassitt, 2023 (a trimmed real pitching line -- the API's own opponent
# numbers are avg .235 / obp .299 / slg .403).
BASSITT_2023 = {
    "_id": 605135, "_name": "Chris Bassitt", "hits": 176, "atBats": 749, "baseOnBalls": 59,
    "hitBatsmen": 12, "sacFlies": 5, "totalBases": 302,
    "avg": ".235", "obp": ".299", "slg": ".403",
}

HERE = os.path.dirname(os.path.abspath(__file__))
AGAINST_FILES = {  # file -> (custom stat name, the API's own matching opponent field)
    "custom-batting-average-against.json": ("Batting Average Against", "avg"),
    "custom-on-base-average-against.json": ("On-Base Average Against", "obp"),
    "custom-slugging-average-against.json": ("Slugging Average Against", "slg"),
}


class AgainstStatFiles(unittest.TestCase):
    """The one-per-stat JSON files behind the Head-To-Head tab's pitcher numbers."""

    def _load(self, fname):
        with open(os.path.join(HERE, fname), encoding="utf-8") as fh:
            return json.load(fh)

    def test_each_file_is_one_player_level_definition(self):
        for fname, (name, _key) in AGAINST_FILES.items():
            d = self._load(fname)
            self.assertEqual(set(d), {"level", "name", "formula"}, fname)
            self.assertEqual((d["level"], d["name"]), ("player", name), fname)

    def test_formulas_use_only_known_player_variables(self):
        for fname in AGAINST_FILES:
            used = formula.formula_variables(self._load(fname)["formula"])
            self.assertTrue(used, fname)
            self.assertLessEqual(used, set(mlbstats.REGRESSION_VARIABLE_INDEX), fname)

    def test_formulas_reproduce_the_apis_opponent_numbers(self):
        groups = {"Pitching": mlbstats.group_by_player([BASSITT_2023])}
        for fname, (_name, key) in AGAINST_FILES.items():
            text = self._load(fname)["formula"]
            values = mlbstats.player_variable_lookup(formula.formula_variables(text), groups, 605135)
            self.assertAlmostEqual(formula.evaluate_formula(text, values),
                                   mlbstats.as_number(BASSITT_2023[key]), places=3, msg=fname)

    def test_missing_at_bats_gives_none_not_an_error(self):
        text = self._load("custom-batting-average-against.json")["formula"]
        groups = {"Pitching": mlbstats.group_by_player([{"_id": 1, "hits": 4}])}
        values = mlbstats.player_variable_lookup(formula.formula_variables(text), groups, 1)
        self.assertIsNone(formula.evaluate_formula(text, values))


class HeadToHead(unittest.TestCase):
    def test_average_of_for_and_against(self):
        self.assertAlmostEqual(mlbstats.head_to_head(0.264, 0.206), 0.235)

    def test_deflated_below_and_inflated_above_the_batters_own_number(self):
        self.assertLess(mlbstats.head_to_head(0.300, 0.220), 0.300)
        self.assertGreater(mlbstats.head_to_head(0.250, 0.290), 0.250)

    def test_missing_side_gives_none(self):
        self.assertIsNone(mlbstats.head_to_head(None, 0.2))
        self.assertIsNone(mlbstats.head_to_head(0.3, None))


def _split(season=None, team=None, stat=None, num_teams=None, pos=None):
    s = {"stat": stat or {}}
    if season: s["season"] = season
    if team: s["team"] = {"name": team}
    if num_teams: s["numTeams"] = num_teams
    if pos: s["position"] = {"abbreviation": pos}
    return s


# Shaped like the real /people/{id}/stats?stats=yearByYear,career responses (a
# traded pitcher: a line per team, then a team-less all-teams line, then career).
TRADED_PITCHER = {"stats": [
    {"type": {"displayName": "yearByYear"}, "splits": [
        _split("2021", "Washington Nationals", {"inningsPitched": "111.0"}),
        _split("2021", "Los Angeles Dodgers", {"inningsPitched": "68.1"}),
        _split("2021", None, {"inningsPitched": "179.1"}, num_teams=2),
        _split("2022", "New York Mets", {"inningsPitched": "145.1"}),
    ]},
    {"type": {"displayName": "career"}, "splits": [
        _split(None, None, {"inningsPitched": "3035.2"}, num_teams=7)]},
]}


class PersonStats(unittest.TestCase):
    def setUp(self):
        self.parsed = mlbstats.parse_person_splits(TRADED_PITCHER)

    def test_team_rows_totals_and_career_are_told_apart(self):
        kinds = [(r["_season"], r["_team"], r["_kind"]) for r in self.parsed["season"]]
        self.assertEqual(kinds, [
            ("2021", "Washington Nationals", "season"),
            ("2021", "Los Angeles Dodgers", "season"),
            ("2021", "2 Teams", "total"),
            ("2022", "New York Mets", "season")])
        career = self.parsed["career"]
        self.assertEqual(len(career), 1)
        self.assertEqual((career[0]["_season"], career[0]["_team"], career[0]["_kind"]),
                         ("Career", "7 Teams", "career"))

    def test_stat_values_are_kept(self):
        self.assertEqual(self.parsed["career"][0]["inningsPitched"], "3035.2")

    def test_a_role_the_player_never_played_is_just_empty(self):
        # The API answers with an empty "stats" list, not empty blocks.
        self.assertEqual(mlbstats.parse_person_splits({"stats": []}), {"season": [], "career": []})
        self.assertEqual(mlbstats.parse_person_splits({}), {"season": [], "career": []})

    def test_career_fielding_has_a_row_per_position(self):
        payload = {"stats": [{"type": {"displayName": "career"}, "splits": [
            _split(None, None, {"gamesPlayed": 356}, pos="1B"),
            _split(None, None, {"gamesPlayed": 242}, pos="LF")]}]}
        rows = mlbstats.parse_person_splits(payload)["career"]
        self.assertEqual([(r["_pos"], r["gamesPlayed"]) for r in rows], [("1B", 356), ("LF", 242)])

    def test_unrelated_stat_blocks_are_ignored(self):
        payload = {"stats": [{"type": {"displayName": "gameLog"}, "splits": [_split("2023")]}]}
        self.assertEqual(mlbstats.parse_person_splits(payload), {"season": [], "career": []})

    def test_advanced_sabermetrics_and_expected_blocks_are_read_too(self):
        for block, target in (("yearByYearAdvanced", "season"), ("careerAdvanced", "career"),
                              ("sabermetrics", "season"), ("expectedStatistics", "season")):
            payload = {"stats": [{"type": {"displayName": block},
                                  "splits": [_split("2023", "Toronto Blue Jays", {"x": 1})]}]}
            parsed = mlbstats.parse_person_splits(payload)
            self.assertEqual(len(parsed[target]), 1, block)

    def test_a_teamless_single_team_line_is_a_plain_season(self):
        # Expected stats come one per season, with no team and no numTeams.
        payload = {"stats": [{"type": {"displayName": "expectedStatistics"},
                              "splits": [_split("2023", None, {"woba": ".420"})]}]}
        row = mlbstats.parse_person_splits(payload)["season"][0]
        self.assertEqual((row["_season"], row["_team"], row["_kind"]), ("2023", "", "season"))

    def test_every_role_and_stat_set_has_columns_led_by_season_and_team(self):
        for _title, role in mlbstats.PERSON_ROLES:
            self.assertIn(mlbstats.PERSON_API_GROUP[role], ("hitting", "pitching", "fielding"))
            self.assertEqual(mlbstats.PERSON_SETS[role][0], "Standard")
            for set_name in mlbstats.PERSON_SETS[role]:
                cols = mlbstats.PERSON_SET_COLUMNS[(role, set_name)]
                self.assertEqual(cols[:2], [("Season", "_season"), ("Team", "_team")], (role, set_name))
                self.assertEqual(len({h for h, _k in cols}), len(cols), (role, set_name))  # no repeats

    def test_running_stats_live_under_running_not_batting(self):
        keys = lambda role, s: {k for _h, k in mlbstats.PERSON_SET_COLUMNS[(role, s)]}
        running_std = {"stolenBases", "caughtStealing", "stolenBasePercentage"}
        self.assertLessEqual(running_std, keys("running", "Standard"))
        self.assertFalse(running_std & keys("hitting", "Standard"))
        running_saber = {"baseRunning", "ubr", "wSb", "wGdp", "spd"}
        self.assertEqual(keys("running", "Sabermetrics") - {"_season", "_team"}, running_saber)
        self.assertFalse(running_saber & keys("hitting", "Sabermetrics"))
        # ...but the batting Sabermetrics still has what isn't running.
        self.assertLessEqual({"woba", "wRcPlus", "war"}, keys("hitting", "Sabermetrics"))


class PersonSeasonFetch(unittest.TestCase):
    """get_person_season_stats: one request per season, merged and ordered."""

    def _fake_api(self, calls):
        def api_get(path, params=None, ttl=None):
            calls.append((params["season"], params["stats"], ttl))
            season = str(params["season"])
            splits = [_split(season, "Team B", {"war": 1.0}), _split(season, "Team A", {"war": 2.0}),
                      _split(season, None, {"war": 3.0}, num_teams=2)]
            return {"stats": [{"type": {"displayName": "sabermetrics"}, "splits": splits}]}
        return api_get

    def test_one_request_per_season_ordered_with_totals_last(self):
        calls = []
        real = mlbstats.api_get
        mlbstats.api_get = self._fake_api(calls)
        try:
            out = mlbstats.get_person_season_stats(
                1, "hitting", "sabermetrics", ["2022", "2021"],
                ttl_for=lambda season: 600 if season >= 2022 else None)
        finally:
            mlbstats.api_get = real
        self.assertEqual(sorted(c[0] for c in calls), ["2021", "2022"])
        self.assertEqual({c[0]: c[2] for c in calls}, {"2021": None, "2022": 600})  # finished season: cached for good
        self.assertEqual([(r["_season"], r["_team"]) for r in out["season"]],
                         [("2021", "Team A"), ("2021", "Team B"), ("2021", "2 Teams"),
                          ("2022", "Team A"), ("2022", "Team B"), ("2022", "2 Teams")])
        self.assertEqual(out["career"], [])

    def test_no_seasons_means_no_requests(self):
        calls = []
        real = mlbstats.api_get
        mlbstats.api_get = self._fake_api(calls)
        try:
            out = mlbstats.get_person_season_stats(1, "hitting", "sabermetrics", [])
        finally:
            mlbstats.api_get = real
        self.assertEqual((calls, out), ([], {"season": [], "career": []}))


def _gl_split(date, stat, game_type="R", pk=1, game_number=1, team=(1, "Home Club"), opp=(2, "Visitors"),
              home=True, win=True, position=None, played=("CF",)):
    split = {"date": date, "gameType": game_type, "isHome": home, "isWin": win,
             "team": {"id": team[0], "name": team[1]}, "opponent": {"id": opp[0], "name": opp[1]},
             "game": {"gamePk": pk, "gameNumber": game_number}, "stat": dict(stat)}
    if position:
        split["position"] = {"abbreviation": position}
        split["stat"]["position"] = {"abbreviation": position}
    else:
        split["positionsPlayed"] = [{"abbreviation": p} for p in played]
    return split


class Dailies(unittest.TestCase):
    """parse_game_log + daily_lines: game rows, season-to-date rates, total lines."""

    HIT = {"stats": [{"splits": [
        _gl_split("2023-04-02", {"plateAppearances": 4, "atBats": 4, "hits": 1, "totalBases": 4, "homeRuns": 1,
                                 "baseOnBalls": 0, "stolenBases": 1, "caughtStealing": 0}, pk=12),
        _gl_split("2023-04-01", {"plateAppearances": 5, "atBats": 3, "hits": 2, "totalBases": 2,
                                 "baseOnBalls": 1, "hitByPitch": 1, "stolenBases": 0, "caughtStealing": 1},
                  pk=11, home=False, win=False, played=("P", "DH")),
        _gl_split("2023-10-04", {"plateAppearances": 4, "atBats": 4, "hits": 0, "totalBases": 0},
                  game_type="D", pk=13),
    ]}]}

    def test_parse_sorts_by_date_and_reads_positions(self):
        rows = mlbstats.parse_game_log(self.HIT)
        self.assertEqual([r["_pk"] for r in rows], [11, 12, 13])
        self.assertEqual(rows[0]["_pos"], "P-DH")
        self.assertEqual((rows[0]["_home"], rows[0]["_win"], rows[2]["_type"]), (False, False, "D"))
        self.assertEqual(mlbstats.parse_game_log({}), [])

    def test_batting_to_date_and_totals_split_by_postseason(self):
        rows = mlbstats.parse_game_log(self.HIT)
        games = {11: {"result": "L", "runs": 2, "opp_runs": 5}}
        lines = mlbstats.daily_lines("hitting", rows, {1: "HOM", 2: "VIS"}, games)
        self.assertEqual([l["_kind"] for l in lines], ["game", "game", "total", "game", "total"])
        first, second, tot = lines[:3]
        self.assertEqual((first["_team"], first["_opp"], first["_result"]), ("HOM", "@ VIS", "L 2\u20135"))
        self.assertEqual(second["_opp"], "vs VIS")
        self.assertEqual(second["_result"], "W")            # no schedule entry: just W/L
        self.assertEqual((first["_avg"], first["_obp"]), (".667", ".800"))   # 2/3; 4/5
        self.assertEqual((second["_avg"], second["_slg"]), (".429", ".857"))  # 3/7, 6/7 to date
        self.assertEqual((tot["_date"], tot["atBats"], tot["hits"], tot["_avg"]),
                         ("Regular season: 2 G", 7, 3, ".429"))
        post_game, post_tot = lines[3:]
        self.assertEqual(post_game["_date"], "2023-10-04 DS")
        self.assertEqual((post_game["_avg"], post_tot["_date"]), (".000", "Postseason: 1 G"))  # own count

    def test_running(self):
        lines = mlbstats.daily_lines("running", mlbstats.parse_game_log(self.HIT)[:2])
        self.assertEqual([l["_on_base"] for l in lines], [4, 1, 5])
        self.assertEqual((lines[1]["_sb"], lines[1]["_cs"], lines[1]["_sb_pct"]), (1, 1, ".500"))
        self.assertEqual(lines[-1]["_date"], "Season: 2 G")

    def test_pitching_era_whip_and_decisions(self):
        payload = {"stats": [{"splits": [
            _gl_split("2023-04-01", {"outs": 18, "inningsPitched": "6.0", "earnedRuns": 2, "hits": 4,
                                     "baseOnBalls": 2, "wins": 1, "gamesStarted": 1}),
            _gl_split("2023-04-07", {"outs": 2, "inningsPitched": "0.2", "earnedRuns": 1, "hits": 1,
                                     "baseOnBalls": 0, "blownSaves": 1, "losses": 1}, pk=2),
        ]}]}
        lines = mlbstats.daily_lines("pitching", mlbstats.parse_game_log(payload))
        self.assertEqual([l.get("_dec") for l in lines], ["W", "L, BS", "1 W, 1 L"])
        self.assertEqual((lines[0]["_era"], lines[0]["_whip"]), ("3.00", "1.00"))
        self.assertEqual((lines[1]["_era"], lines[2]["inningsPitched"]), ("4.05", "6.2"))  # 3 ER in 20 outs

    def test_fielding_one_total_per_position(self):
        payload = {"stats": [{"splits": [
            _gl_split("2023-04-01", {"innings": "9.0", "putOuts": 3, "assists": 0, "errors": 1}, position="CF"),
            _gl_split("2023-04-02", {"innings": "4.1", "putOuts": 1, "assists": 2, "errors": 0}, pk=2, position="SS"),
            _gl_split("2023-04-03", {"innings": "9.0", "putOuts": 2, "assists": 0, "errors": 0}, pk=3, position="CF"),
        ]}]}
        lines = mlbstats.daily_lines("fielding", mlbstats.parse_game_log(payload))
        self.assertEqual([(l["_kind"], l["_pos"]) for l in lines],
                         [("game", "CF"), ("game", "SS"), ("game", "CF"), ("total", "CF"), ("total", "SS")])
        self.assertEqual((lines[0]["_fld"], lines[2]["_fld"], lines[2]["_fld_td"]), (".750", "1.000", ".833"))
        self.assertEqual((lines[3]["innings"], lines[3]["putOuts"], lines[4]["innings"]), ("18.0", 5, "4.1"))

    def test_nothing_in(self):
        self.assertEqual(mlbstats.daily_lines("hitting", []), [])

    def test_get_game_log_asks_for_postseason_only_when_wanted(self):
        calls = []
        real = mlbstats.api_get
        mlbstats.api_get = lambda path, params=None, ttl=None: calls.append(dict(params)) or {}
        try:
            mlbstats.get_game_log(1, "hitting", 2023)
            mlbstats.get_game_log(1, "hitting", 2023, postseason=True)
        finally:
            mlbstats.api_get = real
        self.assertNotIn("gameType", calls[0])
        self.assertEqual((calls[0]["stats"], calls[1]["gameType"]), ("gameLog", "R,F,D,L,W"))

    def test_team_log_postseason_is_a_second_request_and_404_means_none(self):
        calls = []
        regular = {"stats": [{"splits": [_gl_split("2023-04-01", {"wins": 1, "holds": 2, "saves": 1}, pk=1)]}]}
        post = {"stats": [{"splits": [_gl_split("2023-10-04", {"losses": 1}, pk=2)]}]}

        def fake(path, params=None, ttl=None):
            calls.append(dict(params))
            if params.get("gameType") == "P":
                if params["season"] == 2022:
                    raise mlbstats.MLBStatsError("HTTP 404 for ...")
                return post
            return regular
        real = mlbstats.api_get
        mlbstats.api_get = fake
        try:
            rows = mlbstats.get_team_game_log(147, "pitching", 2023, postseason=True)
            none = mlbstats.get_team_game_log(147, "pitching", 2022, postseason=True)
            early = mlbstats.get_team_game_log(147, "pitching", 1899)
        finally:
            mlbstats.api_get = real
        self.assertEqual([r["_type"] for r in rows], ["R", "P"])
        self.assertEqual([r["_type"] for r in none], ["R"])
        self.assertEqual(early, [])
        self.assertEqual(len(calls), 4)  # nothing asked for before 1901
        lines = mlbstats.daily_lines("pitching", rows, games={2: {"type": "L", "result": "L", "runs": 1,
                                                                  "opp_runs": 2}}, team=True)
        self.assertEqual(lines[0]["_dec"], "W, SV")            # a club's line has no holds
        self.assertEqual(lines[2]["_date"], "2023-10-04 LCS")  # round named from the schedule
        self.assertEqual(lines[2]["_result"], "L 1\u20132")

    def test_team_columns_drop_player_only_ones(self):
        for role, cols in mlbstats.DAILY_TEAM_COLUMNS.items():
            keys = {k for _h, k in cols}
            self.assertFalse(keys & {"_team", "_pos", "gamesStarted", "summary"}, role)
            self.assertIn("_opp", keys)

    def test_columns_have_their_keys(self):
        for role, cols in mlbstats.DAILY_COLUMNS.items():
            self.assertEqual(cols[:4], mlbstats._DAILY_LEAD, role)
            self.assertEqual(len({h for h, _ in cols}), len(cols), role)  # no duplicate headers


class InfoQueries(unittest.TestCase):
    def test_a_bar_chart_has_one_value_per_subject_and_series_its_total(self):
        q = infoquery.parse_query({"name": "Bars", "source": "player-seasons", "subjects": ["A", "B"], "chart": "bars",
                                   "series": [{"name": "Runs", "formula": "{Batting: R}", "format": "int"},
                                              {"name": "Rate", "formula": "{Batting: R} / {Batting: G}", "format": "2"}]})
        self.assertEqual(infoquery.query_to_json(q)["chart"], "bars")
        seasons = {"A": [{"R": 100, "G": 150}, {"R": 50, "G": 100}], "B": [{"R": 10, "G": 20}]}
        real = infoquery._Fetcher.players, infoquery._Fetcher.player_seasons
        infoquery._Fetcher.players = lambda self, subject, season: (subject, subject)
        infoquery._Fetcher.player_seasons = lambda self, pid, names: [
            {"values": {"Batting: R": v["R"], "Batting: G": v["G"]}, "season": 2000 + i, "age": 25 + i, "detail": ""}
            for i, v in enumerate(seasons[pid])]
        try:
            result = infoquery.run_query(q, season_now=2026)
        finally:
            infoquery._Fetcher.players, infoquery._Fetcher.player_seasons = real
        headers, rows = infoquery.result_table(result)
        self.assertEqual(headers, ["Player", "Runs", "Rate"])
        self.assertEqual([[c[0] for c in row] for row in rows], [["A", "150", "0.60"], ["B", "10", "0.50"]])
        self.assertEqual(result["x_ticks"], [(1.0, "A"), (2.0, "B")])
        self.assertEqual({s["key"] for s in result["series"]}, {"Runs", "Rate"})  # coloured by series

    def test_series_given_one_stack_name_keep_it_through_the_file_and_the_result(self):
        q = infoquery.parse_query({"name": "S", "source": "player-seasons", "subjects": ["A"], "chart": "bars",
                                   "series": [{"name": "RP", "formula": "{Batting: R}"},
                                              {"name": "R", "formula": "{Batting: R}", "stack": "R + RBI"},
                                              {"name": "RBI", "formula": "{Batting: RBI}", "stack": "R + RBI"}]})
        self.assertEqual([s.get("stack") for s in infoquery.query_to_json(q)["series"]], [None, "R + RBI", "R + RBI"])

    def test_line_scores_give_each_team_the_innings_it_batted(self):
        def game(pk, away, home, a_score, h_score, state="Final"):
            n = max(len(away), len(home))
            inn = [{"num": i + 1, **({"away": {"runs": away[i]}} if i < len(away) else {"away": {}}),
                    **({"home": {"runs": home[i]}} if i < len(home) else {"home": {}})} for i in range(n)]
            return {"gamePk": pk, "gameType": "R", "officialDate": "1923-06-01",
                    "status": {"abstractGameState": state, "detailedState": state},
                    "teams": {"away": {"team": {"id": 1, "name": "A"}, "score": a_score},
                              "home": {"team": {"id": 2, "name": "H"}, "score": h_score}},
                    "linescore": {"innings": inn}}
        payload = {"dates": [{"games": [
            game(1, [1] * 9, [0] * 8, 9, 0),             # home team didn't need the ninth
            game(2, [0] * 8, [1, 0, 0, 0, 0, 0, 0, 0, 2], 0, 3),  # visitors didn't (home batted first)
            game(3, [1] * 9, [0] * 9, 8, 0),             # doesn't add up: left out
            dict(game(4, [], [], 0, 0), linescore={"innings": []})]}]}
        games, missing = mlbstats.parse_linescores(payload)
        self.assertEqual([(g["pk"], g["team"], len(g["innings"])) for g in games],
                         [(1, "H", 8), (1, "A", 9), (2, "H", 9), (2, "A", 8)])
        self.assertEqual(missing, [(1, 2), (1, 2)])
        self.assertEqual(games[1]["opp_innings"], [0] * 8)

    def test_a_ranking_of_team_games_keeps_the_ones_that_scored_every_inning(self):
        q = infoquery.parse_query({"name": "Every", "source": "league-games", "season": 1923, "rank_by": "{Game: Date}",
                                   "order": "lowest", "postseason": True,
                                   "where": ["{Innings: Scored} = {Innings: Batted}"],
                                   "series": [{"name": "Runs", "formula": "{Team: Runs}"}]})
        self.assertEqual(infoquery.parse_query(infoquery.query_to_json(q)), q)
        with self.assertRaises(infoquery.QueryError):
            infoquery.parse_query({"name": "x", "source": "league-games", "season": 1900, "rank_by": "1",
                                   "series": [{"name": "R", "formula": "{Team: Runs}"}]})
        def lines(season, post, ttl=None):
            mk = lambda date, team, inn, runs: {"pk": len(date), "date": date, "type": "R", "game_number": 1,
                                                "team_id": team, "team": f"T{team}", "opp_id": 9, "opponent": "Opp",
                                                "home": False, "runs": runs, "opp_runs": 0, "result": "W",
                                                "innings": inn, "opp_innings": [0] * 8, "length": 9}
            return [mk("1923-07-07", 5, [1] * 9, 9), mk("1923-06-01", 6, [2, 0] + [1] * 7, 9),
                    mk("1923-05-01", 7, [3] * 9, 27)], [(1, 2), (3, 4)]
        real = mlbstats.get_season_linescores, mlbstats.get_team_map
        mlbstats.get_season_linescores = lines
        mlbstats.get_team_map = lambda season, ttl=None: {1: {"league": "National League"}, 2: {"league": "American League"}}
        try:
            result = infoquery.run_query(q, season_now=2026)
        finally:
            mlbstats.get_season_linescores, mlbstats.get_team_map = real
        self.assertEqual([p["xlabel"] for p in result["series"][0]["points"]],
                         ["1. T7, May 1, 1923", "2. T5, Jul 7, 1923"])
        self.assertIn("by inning: 3 3 3", result["series"][0]["points"][0]["detail"])
        self.assertEqual(len(result["notes"]), 2)  # one AL/NL game and one Negro League game unjudged

    def test_a_ranked_list_can_be_drawn_as_bars(self):
        q = infoquery.parse_query({"name": "R", "source": "league-seasons", "season": 2000, "rank_by": "{Batting: HR}",
                                   "chart": "bars", "series": [{"name": "HR", "formula": "{Batting: HR}"}]})
        self.assertEqual(infoquery.query_to_json(q)["chart"], "bars")

    """infoquery: definitions checked, formulas over running totals, x axes, the table."""

    def setUp(self):
        self.real = {n: getattr(mlbstats, n) for n in ("search_people", "get_game_log", "get_team_map",
                                                       "get_person_stats", "get_team_games", "get_team_game_log",
                                                       "get_person")}
        mlbstats.search_people = lambda name, ttl=None: [
            {"id": 7, "name": "Joe Hitter", "debut": "1970-04-01"}, {"id": 8, "name": "Joe Hitter", "debut": "1990-04-01"}]
        mlbstats.get_team_map = lambda season, ttl=None: {
            1: {"id": 1, "name": "Home Club", "abbrev": "HOM", "league": "National League"},
            2: {"id": 2, "name": "Visitors", "abbrev": "VIS", "league": "National League"},
            3: {"id": 3, "name": "Elsewhere", "abbrev": "ELS", "league": "American League",
                "division": "American League East"}}
        log = {"stats": [{"splits": [
            _gl_split("1980-04-10", {"atBats": 4, "hits": 2, "homeRuns": 1}, pk=1),
            _gl_split("1980-04-11", {"atBats": 4, "hits": 0, "homeRuns": 0}, pk=2, home=False),
            _gl_split("1980-04-11", {"atBats": 2, "hits": 2, "homeRuns": 0}, pk=3, game_number=2)]}]}
        mlbstats.get_game_log = lambda pid, group, season, post=False, ttl=None: (
            mlbstats.parse_game_log(log) if group == "hitting" else [])
        mlbstats.get_person_stats = lambda pid, group, ttl=None, advanced=False: {"season": [
            {"_season": "1971", "_kind": "season", "_team": "A", "age": 21, "homeRuns": 10, "atBats": 300},
            {"_season": "1972", "_kind": "season", "_team": "A", "age": 22, "homeRuns": 5, "atBats": 100},
            {"_season": "1972", "_kind": "season", "_team": "B", "age": 22, "homeRuns": 7, "atBats": 150},
            {"_season": "1972", "_kind": "total", "_team": "2 Teams", "age": 22, "homeRuns": 12, "atBats": 250},
        ] if group == "hitting" else [], "career": []}

        def sched(tid, season, ttl=None):
            res = {1: ["W", "W", "L", "T"], 2: ["L", "L", "W", "T"]}[tid]
            return [{"pk": 10 + i, "date": f"1914-04-{14 + i}", "type": "R", "home": True, "opponent": "Visitors",
                     "state": "final", "runs": 3, "opp_runs": 2, "result": r, "game_number": 1}
                    for i, r in enumerate(res)]
        mlbstats.get_team_games = sched
        mlbstats.get_team_game_log = lambda *a, **k: []

    def tearDown(self):
        for n, f in self.real.items():
            setattr(mlbstats, n, f)

    def _q(self, **kw):
        base = {"name": "t", "source": "player-games", "subjects": ["Joe Hitter"], "season": 1980,
                "x": "game", "series": [{"name": "AVG", "formula": "{Batting: H} / {Batting: AB}"}]}
        base.update(kw)
        return infoquery.parse_query(base)

    def test_definition_checks(self):
        with self.assertRaises(infoquery.QueryError):
            infoquery.parse_query({"name": "x", "source": "nowhere", "subjects": ["a"], "series": []})
        with self.assertRaises(infoquery.QueryError):
            self._q(series=[{"name": "bad", "formula": "{Batting: H} +"}])
        with self.assertRaises(infoquery.QueryError):
            self._q(x="season")  # games have no season axis
        with self.assertRaises(infoquery.QueryError):
            self._q(source="team-games", season="career")  # a career is a player's
        with self.assertRaises(infoquery.QueryError):
            self._q(where=["{Batting: H}"])  # no comparison
        with self.assertRaises(infoquery.QueryError):
            infoquery.run_query(self._q(where=["{Batting: Nonsense} = 1"]), season_now=2026)
        with self.assertRaises(infoquery.QueryError):
            self._q(series=[{"formula": "{Batting: H}", "source": "player-seasons"}])  # can't mix x axes
        with self.assertRaises(infoquery.QueryError):
            infoquery.run_query(self._q(series=[{"formula": "{Batting: Nonsense}"}]), season_now=2026)
        q = self._q(running=True, reference_lines=[{"panel": 1, "value": 0.4, "label": ".400"}])
        self.assertEqual(infoquery.parse_query(infoquery.query_to_json(q)), q)  # round trip
        q = self._q(season="Career", where=["{Batting: H}==0"], x="age")
        self.assertEqual((q["season"], q["where"]), ("career", ["{Batting: H} = 0"]))
        self.assertEqual(infoquery.parse_query(infoquery.query_to_json(q)), q)

    def test_conditions(self):
        self.assertEqual(infoquery.split_condition("{A: x>=y} >= 2 * {B}"), ("{A: x>=y}", ">=", "2 * {B}"))
        env = {"H": 0, "Outs": 27, "R": None}
        self.assertTrue(infoquery._holds(["{H} = 0", "{Outs} >= 27", "{Outs} != 26"], env))
        self.assertFalse(infoquery._holds(["{H} = 0", "{Outs} > 27"], env))
        self.assertFalse(infoquery._holds(["{R} = 0"], env))  # a missing value fails...
        self.assertTrue(infoquery._holds(["{R} = 0"], env, soft=True))  # ...unless it's to be judged later

    def test_career_games_where_by_age_with_team_runs(self):
        mlbstats.get_person = lambda pid, ttl=None: {"birth_date": "1955-04-10"}
        mlbstats.get_person_stats = lambda pid, group, ttl=None, advanced=False: {"season": [
            {"_season": "1899", "_kind": "season"}, {"_season": "1980", "_kind": "season"}], "career": []}
        mlbstats.get_team_games = lambda tid, season, ttl=None: [
            {"pk": pk, "result": "W", "runs": runs, "opp_runs": 0} for pk, runs in ((1, 6), (2, 1), (3, 4))]
        q = self._q(season="career", x="age", where=["{Batting: H} = 2"],
                    series=[{"name": "Runs", "formula": "{Team: Runs}"}])
        r = infoquery.run_query(q, season_now=2026)
        (line,) = r["series"]
        self.assertEqual([p["y"] for p in line["points"]], [6, 4])  # the 0-hit game is dropped
        self.assertEqual(line["points"][0]["xlabel"], "Age 25, 0 d")  # his birthday
        self.assertAlmostEqual(line["points"][1]["x"], 25 + 1 / 365.25 + 0.001)  # the doubleheader's 2nd
        self.assertEqual(line["points"][0]["detail"], "Apr 10, 1980 vs VIS · W 6–0")
        self.assertTrue(any("no game logs before 1901" in n for n in r["notes"]))

    def test_running_average_by_game_and_date(self):
        r = infoquery.run_query(self._q(running=True), season_now=2026)
        (line,) = r["series"]
        self.assertEqual([p["y"] for p in line["points"]], [0.5, 0.25, 0.4])  # 2/4, 2/8, 4/10
        self.assertEqual([p["x"] for p in line["points"]], [1.0, 2.0, 3.0])
        self.assertEqual(r["notes"], [])  # only one of the two Joe Hitters had debuted by 1980
        later = infoquery.run_query(self._q(season=1995), season_now=2026)
        self.assertIn("2 players match", later["notes"][0])  # by 1995 both had: it says which it used
        self.assertEqual(line["points"][1]["detail"], "@ VIS")
        dated = infoquery.run_query(self._q(running=True, x="date"), season_now=2026)["series"][0]["points"]
        self.assertAlmostEqual(dated[2]["x"] - dated[1]["x"], 0.4)  # a doubleheader's 2nd game sits just after
        single = infoquery.run_query(self._q(), season_now=2026)["series"][0]["points"]
        self.assertEqual([p["y"] for p in single], [0.5, 0.0, 1.0])

    def test_league_seasons_ranked(self):
        def rows(season, view, ttl=None):
            bat = lambda pid, name, ab, h, pa, lg="AL", team=1, n=1: {
                "_id": pid, "_name": name, "_pos": "", "_league": lg, "_team_id": team, "_num_teams": n,
                "atBats": ab, "hits": h, "plateAppearances": pa}
            fld = lambda pid, pos, inn, po, a, g, lg="AL": {
                "_id": pid, "_name": "", "_pos": pos, "_league": lg, "innings": inn, "putOuts": po, "assists": a,
                "gamesPlayed": g}
            if view == "Batting":
                return [bat(1, "Glove Man", 500, 100, 520), bat(2, "Bat Man", 500, 175, 520),
                        bat(3, "Part Timer", 100, 15, 110), bat(4, "Traded Glove", 450, 95, 500, n=2),
                        bat(5, "Negro Leaguer", 50, 5, 55, lg="NNL", team=9)]
            return [fld(1, "SS", "1200", 250, 450, 140), fld(1, "2B", "100", 20, 30, 12),
                    fld(2, "SS", "1200", 200, 300, 140), fld(3, "SS", "300", 50, 90, 35),
                    fld(4, "SS", "1100", 220, 400, 130), fld(5, "SS", "400", 90, 150, 45, lg="NNL")]
        self.real["get_league_view_rows"] = mlbstats.get_league_view_rows
        self.real["get_team_games_played"] = mlbstats.get_team_games_played
        mlbstats.get_league_view_rows = rows
        mlbstats.get_team_games_played = lambda season, ttl=None: {1: 160, 9: 40}
        q = infoquery.parse_query({
            "name": "g", "source": "league-seasons", "season": 1980, "season_to": 1981, "distinct": True,
            "where": ["{Batting: PA} >= 3.1 * {Team: G}", "{Fielding: RF/9} > {MLB: RF/9}"],
            "rank_by": "{Fielding: RF/9} / {MLB: RF/9} / ({Batting: H} / {Batting: AB} / {MLB: AVG})",
            "series": [{"name": "Range", "formula": "{Fielding: RF/9} / {MLB: RF/9}"}]})
        self.assertEqual((q["subjects"], q["x"], q["top"], q["leagues"]), ([], "rank", 10, ["AL", "NL"]))
        self.assertEqual(infoquery.parse_query(infoquery.query_to_json(q)), q)
        r = infoquery.run_query(q, season_now=2026)
        (line,) = r["series"]
        # Bat Man is below average in the field, Part Timer short of 3.1 PA a game, the NNL left out;
        # one season each (distinct), though both seasons qualify
        self.assertEqual([p["xlabel"] for p in line["points"]], ["1. Glove Man, 1980", "2. Traded Glove, 1980"])
        self.assertEqual(line["label"], "Range")
        self.assertEqual(r["x_ticks"], [(1.0, "Man 1980"), (2.0, "Glove 1980")])
        self.assertEqual(line["points"][1]["detail"], "1980 2 teams · SS")
        self.assertGreater(line["points"][0]["y"], 1)
        with self.assertRaises(infoquery.QueryError):
            infoquery.parse_query(dict(infoquery.query_to_json(q), positions=["DH"]))

    def test_seasons_by_age_use_the_traded_total(self):
        q = infoquery.parse_query({"name": "a", "source": "player-seasons", "subjects": [{"id": 7, "name": "Joe"}],
                                   "x": "age", "series": [{"name": "HR", "formula": "{Batting: HR}"}]})
        (line,) = infoquery.run_query(q, season_now=2026)["series"]
        self.assertEqual([(p["x"], p["y"]) for p in line["points"]], [(21.0, 10), (22.0, 12)])
        self.assertEqual(line["points"][1]["detail"], "1972, A / B")

    def test_league_subject_and_games_over_500(self):
        q = infoquery.parse_query({"name": "race", "source": "team-games", "subjects": ["National League"],
                                   "season": 1914, "x": "game", "running": True,
                                   "series": [{"name": "Over .500", "formula": "{Team: W} - {Team: L}"}]})
        r = infoquery.run_query(q, season_now=2026)
        self.assertEqual([s["subject"] for s in r["series"]], ["Home Club", "Visitors"])  # not the AL club
        self.assertEqual([p["y"] for p in r["series"][0]["points"]], [1, 2, 1, 1])  # a tie changes nothing
        self.assertEqual({s["key"] for s in r["series"]}, {"Home Club", "Visitors"})  # colour by team
        headers, rows = infoquery.result_table(r)
        self.assertEqual(headers, ["Game", "Home Club", "Visitors"])
        self.assertEqual(rows[1][0][0], "Game 2")
        self.assertEqual([c[0] for c in rows[3][1:]], ["1", "-1"])

    def test_division_subject_long_or_short(self):
        mlbstats.get_team_games = lambda tid, season, ttl=None: []
        fetch = infoquery._Fetcher(2026, 600, [])
        for name in ("American League East", "AL East", "al east"):
            self.assertEqual(fetch.teams(name, 1990), [(3, "Elsewhere")])
        self.assertEqual(len(fetch.teams("NL", 1990)), 2)
        with self.assertRaisesRegex(infoquery.QueryError, "divisions began in 1969"):
            fetch.teams("NL Central", 1950)

    def test_custom_stats_usable_in_formulas(self):
        q = self._q(series=[{"name": "RP", "formula": "{Custom: Power} * 2"}])
        r = infoquery.run_query(q, custom_stats=[("Power", "{Batting: HR} + 1")], season_now=2026)
        self.assertEqual([p["y"] for p in r["series"][0]["points"]], [4, 2, 2])
        with self.assertRaises(infoquery.QueryError):
            infoquery.run_query(q, season_now=2026)  # no such custom stat defined

    def test_formats_and_ticks(self):
        self.assertEqual(infoquery.format_value(0.4, "rate"), ".400")
        self.assertEqual(infoquery.format_value(1.066, "rate"), "1.066")
        self.assertEqual(infoquery.format_value(175.0), "175")
        self.assertEqual(infochart.nice_ticks(0, 175), [0, 50, 100, 150, 200])
        self.assertEqual(infochart.nice_ticks(0.2, 0.45), [0.2, 0.25, 0.3, 0.35, 0.4, 0.45])

    def test_example_files_load(self):
        names = [f for f in os.listdir(HERE) if f.startswith("info-") and f.endswith(".json")]
        self.assertGreaterEqual(len(names), 3)
        for f in names:
            self.assertTrue(infoquery.read_queries(os.path.join(HERE, f)))


class VsPlayer(unittest.TestCase):
    """parse_vs_player: the batter's actual line against one pitcher, by season and career."""

    def test_seasons_and_career(self):
        payload = {"stats": [
            {"type": {"displayName": "vsPlayerTotal"}, "splits": [
                {"stat": {"plateAppearances": 61, "atBats": 48, "hits": 17, "avg": ".354", "obp": ".492", "slg": ".792"}}]},
            {"type": {"displayName": "vsPlayer"}, "splits": [
                {"season": "2024", "stat": {"plateAppearances": 10, "atBats": 8, "avg": ".500", "slg": "1.625"}},
                {"season": "2019", "stat": {"plateAppearances": 1, "atBats": 0, "avg": ".---", "obp": "1.000"}}]}]}
        out = mlbstats.parse_vs_player(payload)
        self.assertEqual(sorted(out["seasons"]), [2019, 2024])
        self.assertEqual((out["seasons"][2024]["avg"], out["seasons"][2024]["slg"]), (0.5, 1.625))
        self.assertIsNone(out["seasons"][2019]["avg"])  # a walk, no at-bats: no average
        self.assertEqual(out["career"]["obp"], 0.492)
        self.assertEqual(mlbstats.parse_vs_player({"stats": []}), {"seasons": {}, "career": None})


class PeopleSearch(unittest.TestCase):
    PAYLOAD = {"people": [
        {"id": 1, "fullName": "Zed Minor", "active": True, "primaryPosition": {"abbreviation": "P"}},
        {"id": 115223, "fullName": "Vladimir Guerrero", "mlbDebutDate": "1996-09-19",
         "birthDate": "1975-02-09", "primaryPosition": {"abbreviation": "RF"}},
        {"id": 665489, "fullName": "Vladimir Guerrero Jr.", "mlbDebutDate": "2019-04-26",
         "birthDate": "1999-03-16", "active": True, "primaryPosition": {"abbreviation": "1B"}},
        {"id": 2, "fullName": "Abe Minor", "primaryPosition": {"abbreviation": "OF"}},
        {"fullName": "No Id"},
    ]}

    def test_majors_first_most_recent_debut_first_then_the_rest_by_name(self):
        names = [p["name"] for p in mlbstats.parse_people(self.PAYLOAD)]
        self.assertEqual(names, ["Vladimir Guerrero Jr.", "Vladimir Guerrero", "Abe Minor", "Zed Minor"])

    def test_fields_and_missing_values(self):
        first = mlbstats.parse_people(self.PAYLOAD)[0]
        self.assertEqual((first["id"], first["position"], first["debut"], first["birth"], first["active"]),
                         (665489, "1B", "2019-04-26", "1999-03-16", True))
        minor = mlbstats.parse_people(self.PAYLOAD)[-1]
        self.assertIsNone(minor["debut"])

    def test_empty(self):
        self.assertEqual(mlbstats.parse_people({}), [])


class PersonBio(unittest.TestCase):
    def test_bio_facts(self):
        bio = mlbstats.parse_person({
            "id": 660271, "fullName": "Shohei Ohtani", "primaryPosition": {"abbreviation": "TWP"},
            "batSide": {"code": "L"}, "pitchHand": {"code": "R"}, "height": "6' 4\"", "weight": 210,
            "birthDate": "1994-07-05", "birthCity": "Oshu", "birthCountry": "Japan",
            "mlbDebutDate": "2018-03-29", "active": True})
        self.assertEqual((bio["name"], bio["position"], bio["bats"], bio["throws"]),
                         ("Shohei Ohtani", "TWP", "L", "R"))
        self.assertEqual(bio["birth_place"], "Oshu, Japan")   # no state -> no stray comma

    def test_sparse_person_does_not_raise(self):
        bio = mlbstats.parse_person({"fullName": "Old Timer"})
        self.assertEqual((bio["position"], bio["bats"], bio["birth_place"], bio["active"]), ("", "", "", False))


def _fld(pos, po, a, inn, g):
    return {"_pos": pos, "putOuts": po, "assists": a, "innings": inn, "gamesPlayed": g}


class RangeFactor(unittest.TestCase):
    def test_rf9_is_chances_per_nine_innings(self):
        f = mlbstats.rf_figures(4, 27, 4, 1)          # 4 chances in 9 innings (27 outs), 1 game
        self.assertAlmostEqual(f["rf_9"], 4.0)
        self.assertAlmostEqual(f["rf_game"], 4.0)
        f = mlbstats.rf_figures(300, 3 * 1000, 300, 130)   # 300 chances in 1000 innings, 130 games
        self.assertAlmostEqual(f["rf_9"], 2.7)
        self.assertAlmostEqual(f["rf_game"], 300 / 130)

    def test_partial_innings_are_exact_not_truncated(self):
        # "155.2" innings is 155 2/3, not 155 (the API's own rangeFactorPer9Inn drops the 2/3).
        f = mlbstats.fielding_row_figures(_fld("1B", 100, 42, "155.2", 20))
        self.assertAlmostEqual(f["rf_9"], 142 * 9 / (155 + 2 / 3))

    def test_a_tiny_sample_still_divides_sensibly(self):
        f = mlbstats.fielding_row_figures(_fld("1B", 1, 0, "0.1", 1))   # one out at first base
        self.assertAlmostEqual(f["rf_9"], 27.0)     # (the API reports 0.0 for this)

    def test_missing_innings_leaves_only_the_per_game_measure(self):
        for row in (_fld("SS", 90, 200, None, 100), _fld("SS", 90, 200, "0.0", 100)):
            f = mlbstats.fielding_row_figures(row)
            self.assertIsNone(f["rf_9"])
            self.assertAlmostEqual(f["rf_game"], 2.9)
            self.assertEqual(f["chances_inn"], 0)     # not counted toward a per-inning total

    def test_nothing_to_divide_by_gives_none(self):
        f = mlbstats.fielding_row_figures(_fld("SS", 0, 0, None, 0))
        self.assertEqual((f["rf_9"], f["rf_game"]), (None, None))


class LeagueRange(unittest.TestCase):
    ROWS = [
        _fld("SS", 100, 300, "1000.0", 120), _fld("SS", 50, 150, "500.0", 60),
        _fld("SS", 10, 20, None, 15),                 # no innings: per-game totals only
        _fld("CF", 300, 10, "900.0", 110),
        _fld("DH", 0, 0, "0.0", 30),                  # not one of the nine
        {"_pos": "OF", "putOuts": 5, "assists": 0, "innings": "9.0", "gamesPlayed": 1},  # nor is this
    ]

    def test_totals_per_position(self):
        ss = mlbstats.aggregate_league_positions(self.ROWS)["SS"]
        self.assertEqual((ss["chances_inn"], ss["outs"]), (600, 4500))            # rows with innings only
        self.assertEqual((ss["chances_game"], ss["games"]), (630, 195))           # every row with games
        self.assertAlmostEqual(ss["rf_9"], 27 * 600 / 4500)
        self.assertAlmostEqual(ss["rf_game"], 630 / 195)

    def test_every_position_is_present_even_when_empty(self):
        totals = mlbstats.aggregate_league_positions(self.ROWS)
        self.assertEqual([p for p, _n in mlbstats.RANGE_POSITIONS], list(totals))
        self.assertEqual(totals["RF"]["rf_9"], None)

    def test_league_fetch_asks_for_the_whole_league_and_rejects_a_truncated_list(self):
        calls = []
        def fake(path, params=None, ttl=None):
            calls.append((path, dict(params), ttl))
            splits = [{"stat": {"putOuts": 1, "assists": 2, "innings": "9.0", "gamesPlayed": 1},
                       "player": {"id": i, "fullName": "P"}, "position": {"abbreviation": "SS"}} for i in range(3)]
            return {"stats": [{"splits": splits, "totalSplits": self.total}]}
        real, mlbstats.api_get = mlbstats.api_get, fake
        try:
            self.total = 3
            totals = mlbstats.get_league_position_totals(2023, ttl=None)
            self.assertEqual(totals["SS"]["chances_inn"], 9)
            path, params, ttl = calls[0]
            self.assertEqual((path, params["season"], params["sportId"], params["limit"]),
                             ("/stats", 2023, 1, 5000))
            self.assertNotIn("teamId", params)
            self.total = 4                              # the API says there's more than it sent
            with self.assertRaises(mlbstats.MLBStatsError):
                mlbstats.get_league_position_totals(2023)
        finally:
            mlbstats.api_get = real


class RangeRatio(unittest.TestCase):
    def test_ratio_is_the_players_rf_over_the_leagues_on_the_same_measure(self):
        player = mlbstats.rf_figures(60, 3 * 100, 60, 12)     # RF/9 5.4, RF/G 5.0
        league = mlbstats.rf_figures(400, 3 * 1000, 400, 900)    # RF/9 3.6, RF/G .444
        measure, p, l, ratio = mlbstats.range_ratio(player, league)
        self.assertEqual(measure, "rf9")
        for got, want in zip((p, l, ratio), (5.4, 3.6, 1.5)):
            self.assertAlmostEqual(got, want)

    def test_falls_back_to_per_game_when_either_side_lacks_innings(self):
        with_inn = mlbstats.rf_figures(60, 300, 60, 12)
        no_inn = mlbstats.rf_figures(0, 0, 50, 10)            # RF/G 5.0, no RF/9
        for player, league in ((no_inn, with_inn), (with_inn, no_inn)):
            measure, _p, _l, _ratio = mlbstats.range_ratio(player, league)
            self.assertEqual(measure, "game")
        measure, p, l, ratio = mlbstats.range_ratio(no_inn, mlbstats.rf_figures(0, 0, 400, 100))
        for got, want in zip((p, l, ratio), (5.0, 4.0, 1.25)):
            self.assertAlmostEqual(got, want)

    def test_nothing_comparable_gives_none(self):
        empty = mlbstats.rf_figures(0, 0, 0, 0)
        self.assertIsNone(mlbstats.range_ratio(empty, mlbstats.rf_figures(4000, 3000, 4000, 900)))
        self.assertIsNone(mlbstats.range_ratio(mlbstats.rf_figures(60, 300, 60, 12), empty))

    def test_the_lineup_average_is_the_mean_of_the_proportions(self):
        ratios = [1.10, 0.90, 1.05]
        self.assertAlmostEqual(sum(ratios) / len(ratios), 1.0166666, places=5)


def _order_split(pid, code, games, pa, runs=None):
    return {"split": {"code": code}, "player": {"id": pid, "fullName": f"P{pid}"},
            "stat": {"gamesPlayed": games, "plateAppearances": pa, "runs": runs}}


class BattingOrder(unittest.TestCase):
    PAYLOAD = {"stats": [{"splits": [
        _order_split(1, "b1", 46, 200), _order_split(1, "b2", 10, 40),
        _order_split(2, "b1", 46, 190), _order_split(2, "b3", 68, 300),
        _order_split(3, "b1", 5, 20),
        _order_split(4, "b3", 4, 15),
        _order_split(5, "p3", 9, 9),                  # not a batting-order code
        _order_split(6, "b0", 9, 9), _order_split(6, "bx", 9, 9),
        {"split": {"code": "b4"}, "stat": {"gamesPlayed": 1}},   # no player
    ]}]}

    def test_parse_gives_games_and_plate_appearances_per_place(self):
        order = mlbstats.parse_batting_order(self.PAYLOAD)
        self.assertEqual(order[1], {1: {"games": 46, "pa": 200}, 2: {"games": 10, "pa": 40}})
        self.assertEqual(order[2][3], {"games": 68, "pa": 300})
        self.assertEqual(set(order), {1, 2, 3, 4})       # only real b1..b9 splits, with a player

    def test_parse_of_an_empty_season_is_empty(self):
        self.assertEqual(mlbstats.parse_batting_order({"stats": [{"splits": []}]}), {})
        self.assertEqual(mlbstats.parse_batting_order({}), {})

    def test_fetch_asks_for_all_nine_places_of_one_team(self):
        calls = []
        def fake(path, params=None, ttl=None):
            calls.append((path, dict(params), ttl))
            return self.PAYLOAD
        real, mlbstats.api_get = mlbstats.api_get, fake
        try:
            order = mlbstats.get_batting_order(147, 2023, ttl=5)
        finally:
            mlbstats.api_get = real
        path, params, ttl = calls[0]
        self.assertEqual((path, params["stats"], params["teamId"], params["season"], ttl),
                         ("/stats", "statSplits", 147, 2023, 5))
        self.assertEqual(params["sitCodes"], "b1,b2,b3,b4,b5,b6,b7,b8,b9")
        self.assertIn(1, order)

    ROSTER = [
        {"_id": 1, "_name": "Regular", "plateAppearances": 650},
        {"_id": 2, "_name": "Cleanup", "plateAppearances": 300},
        {"_id": 3, "_name": "Bench", "plateAppearances": 20},
        {"_id": 4, "_name": "Never", "plateAppearances": 500},
        {"_id": 7, "_name": "Also never", "plateAppearances": 500},
        {"_id": 8, "_name": "Twin never", "plateAppearances": 900},
    ]

    def test_rank_puts_the_most_games_in_that_place_first_then_the_rest_by_plate_appearances(self):
        order = mlbstats.parse_batting_order(self.PAYLOAD)
        here, others = mlbstats.rank_for_slot(self.ROSTER, order, 1)
        # 1 and 2 both have 46 G leading off: the one with fewer PA there follows
        self.assertEqual([r["_name"] for r in here], ["Regular", "Cleanup", "Bench"])
        self.assertEqual([r["_name"] for r in others], ["Twin never", "Also never", "Never"])
        here, others = mlbstats.rank_for_slot(self.ROSTER, order, 3)
        self.assertEqual([r["_name"] for r in here], ["Cleanup", "Never"])
        self.assertEqual([r["_name"] for r in others][:2], ["Twin never", "Regular"])

    def test_rank_with_no_order_data_is_all_by_plate_appearances(self):
        here, others = mlbstats.rank_for_slot(self.ROSTER, {}, 5)
        self.assertEqual(here, [])
        self.assertEqual([r["_name"] for r in others],
                         ["Twin never", "Regular", "Also never", "Never", "Cleanup", "Bench"])


class Production(unittest.TestCase):
    def test_without_batting_order_records_the_most_plate_appearances_fill_the_lineup(self):
        rows = [{"_id": i, "_name": f"P{i}", "plateAppearances": 100 + i} for i in range(12)]
        rows.append({"_id": 99, "_name": "Pitcher", "plateAppearances": 0})
        lineup = mlbstats.lineup_by_plate_appearances(rows)
        self.assertEqual([lineup[n]["_id"] for n in mlbstats.LINEUP_SLOTS], [11, 10, 9, 8, 7, 6, 5, 4, 3])

    """The Production tab's numbers: the "Runs Produced" custom stat from its JSON file."""

    def _definition(self):
        with open(os.path.join(HERE, "runs-produced-player.json"), encoding="utf-8") as fh:
            entries = json.load(fh)
        self.assertEqual(len(entries), 1)              # the file is a list holding the one definition
        return entries[0]

    def test_the_json_is_a_player_level_definition_using_known_variables(self):
        d = self._definition()
        self.assertEqual((d["level"], d["name"]), ("player", "Runs Produced"))
        self.assertLessEqual(formula.formula_variables(d["formula"]), set(mlbstats.REGRESSION_VARIABLE_INDEX))

    def test_runs_produced_is_runs_plus_rbi_minus_home_runs(self):
        text = self._definition()["formula"]
        groups = {"Batting": mlbstats.group_by_player([{"_id": 9, "runs": 94, "rbi": 82, "homeRuns": 25}])}
        self.assertEqual(mlbstats.production_values(text, groups, [9, 10]), {9: 151, 10: None})

    def test_rate_is_per_plate_appearance_and_none_without_either_figure(self):
        self.assertAlmostEqual(mlbstats.production_rate(151, 650), 151 / 650)
        self.assertIsNone(mlbstats.production_rate(None, 650))
        self.assertIsNone(mlbstats.production_rate(151, 0))
        self.assertIsNone(mlbstats.production_rate(151, None))

    def test_league_rate_leaves_out_players_who_cannot_be_measured(self):
        league = mlbstats.league_production({1: 100.0, 2: 50.0, 3: None, 4: 8.0},
                                            {1: 500, 2: 300, 3: 40, 4: 0})
        self.assertEqual((league["total"], league["pa"], league["players"]), (150.0, 800.0, 2))
        self.assertAlmostEqual(league["rate"], 150 / 800)
        self.assertIsNone(mlbstats.league_production({}, {})["rate"])

    def test_league_fetch_sums_the_formula_over_every_batter_and_rejects_a_truncated_list(self):
        text = self._definition()["formula"]
        calls = []
        def fake(path, params=None, ttl=None):
            calls.append((path, dict(params), ttl))
            splits = [{"player": {"id": i}, "stat": {"plateAppearances": 100, "runs": 10, "rbi": 12, "homeRuns": 2}}
                      for i in (1, 2, 3)]
            return {"stats": [{"splits": splits, "totalSplits": self.total}]}
        real, mlbstats.api_get = mlbstats.api_get, fake
        try:
            self.total = 3
            league = mlbstats.get_league_production(2023, text, ttl=None)
            self.assertEqual((league["total"], league["pa"], league["players"]), (60.0, 300.0, 3))
            self.assertAlmostEqual(league["rate"], 0.2)
            self.assertEqual(len(calls), 1)               # Batting is both the formula's view and the PA source
            path, params, _ttl = calls[0]
            self.assertEqual((path, params["season"], params["limit"]), ("/stats", 2023, 5000))
            self.assertNotIn("teamId", params)
            self.total = 4
            with self.assertRaises(mlbstats.MLBStatsError):
                mlbstats.get_league_production(2023, text)
        finally:
            mlbstats.api_get = real

    def test_lineup_score_is_the_mean_of_the_nine_proportions(self):
        rates = [0.26, 0.23, 0.30]
        ratios = [r / 0.2 for r in rates]
        self.assertAlmostEqual(sum(ratios) / len(ratios), (0.26 + 0.23 + 0.30) / 3 / 0.2)


def _standing(league, division, team_records):
    """One division of a /standings response; each team is (vs-league [(id, W, L)], vs-division [(id, name, W, L)])."""
    return {"league": {"id": league}, "division": {"id": division}, "teamRecords": [
        {"wins": sum(w for _i, w, _l in vs_league), "losses": sum(l for _i, _w, l in vs_league),
         "records": {
            "leagueRecords": [{"league": {"id": lid, "name": {103: "American League", 104: "National League"}[lid]},
                               "wins": w, "losses": l} for lid, w, l in vs_league],
            "divisionRecords": [{"division": {"id": did, "name": name}, "wins": w, "losses": l}
                                for did, name, w, l in vs_division]}}
        for vs_league, vs_division in team_records]}


class Leaders(unittest.TestCase):
    def test_a_board_reads_rank_player_season_team_league_and_value(self):
        payload = {"leagueLeaders": [{"leaderCategory": "homeRuns", "leaders": [
            {"rank": 1, "value": "73", "season": "2001", "person": {"id": 111188, "fullName": "Barry Bonds"},
             "team": {"name": "San Francisco Giants"}, "league": {"id": 104, "name": "National League"}},
            {"rank": 2, "value": "70", "person": {"id": 118743, "fullName": "Mark McGwire"}}]}]}
        board = mlbstats.parse_leaders(payload)
        self.assertEqual(board[0], {"rank": 1, "pid": 111188, "name": "Barry Bonds", "season": "2001",
                                    "team": "San Francisco Giants", "league_id": 104,
                                    "league": "National League", "value": "73"})
        self.assertEqual((board[1]["season"], board[1]["team"], board[1]["league_id"]), (None, "", None))
        self.assertEqual(mlbstats.parse_leaders({}), [])

    def test_ranking_shares_ties_keeps_ties_at_the_cut_and_knows_lower_is_better(self):
        def e(name, value):
            return {"name": name, "value": value}
        board = mlbstats._rank_board("homeRuns", [e("A", "40"), e("B", "50"), e("C", "40"), e("D", "30")], 2)
        self.assertEqual([(x["rank"], x["name"]) for x in board], [(1, "B"), (2, "A"), (2, "C")])
        board = mlbstats._rank_board("earnedRunAverage", [e("A", "2.50"), e("B", "1.90"), e("C", "-.--")], 5)
        self.assertEqual([x["name"] for x in board], ["B", "A"])
        board = mlbstats._rank_board("inningsPitched", [e("A", "200.1"), e("B", "200.2")], 5)
        self.assertEqual([x["name"] for x in board], ["B", "A"])

    def test_only_boards_mlb_cannot_give_are_added_up_here(self):
        self.assertFalse(mlbstats.leaders_assembled("season", [103], "modern"))
        self.assertFalse(mlbstats.leaders_assembled("single", [103, 104], None))   # MLB takes a league set
        self.assertTrue(mlbstats.leaders_assembled("single", None, "modern"))
        self.assertTrue(mlbstats.leaders_assembled("single", None, "early"))
        self.assertFalse(mlbstats.leaders_assembled("career", None, None))
        self.assertTrue(mlbstats.leaders_assembled("career", [103, 104], None))     # MLB splits careers by league

    def test_an_added_up_career_sums_its_leagues_less_the_national_leagues_early_seasons(self):
        def row(pid, name, **stat):
            return {"player": {"id": pid, "fullName": name}, "stat": stat}
        careers = {103: [row(1, "Both", homeRuns=20, plateAppearances=2000, atBats=1800, hits=600)],
                   104: [row(1, "Both", homeRuns=30, plateAppearances=2000, atBats=1800, hits=540),
                         row(2, "Early", homeRuns=40, plateAppearances=4000, atBats=3600, hits=1300)]}
        early = {1890: [dict(row(2, "Early", homeRuns=25, plateAppearances=1500, atBats=1350, hits=500),
                             league={"id": 104}),
                        dict(row(3, "Brotherhood", homeRuns=10, plateAppearances=600, atBats=550, hits=180),
                             league={"id": 105})]}

        def fake(path, params, ttl=None):
            if path == "/people":   # each player's record, for his last team
                return {"people": [{"id": 1, "stats": [{"splits": [
                    {"season": "1899", "team": {"name": "Old Club"}, "league": {"id": 104}},
                    {"season": "1905", "team": {"name": "AL Club"}, "league": {"id": 103}},
                    {"season": "1903", "team": {"name": "NL Club"}, "league": {"id": 104}}]}]}]}
            if params["stats"] == "career":
                rows = careers.get(params["leagueId"], [])
            else:
                rows = early.get(params["season"], [])
            return {"stats": [{"totalSplits": len(rows), "splits": rows if params["offset"] == 0 else []}]}
        real = mlbstats.api_get
        mlbstats.api_get = fake
        try:
            board = mlbstats.get_leaders("hitting", "homeRuns", "career", leagues=[103, 104], limit=5)
            self.assertEqual([(x["name"], x["value"], x["league"], x["team"]) for x in board],
                             [("Both", "50", "AL/NL", "AL Club"), ("Early", "40", "NL", "")])
            board = mlbstats.get_leaders("hitting", "homeRuns", "career", leagues=[104], limit=5)
            self.assertEqual({x["name"]: x["team"] for x in board}["Both"], "NL Club")   # last in the leagues counted
            board = mlbstats.get_leaders("hitting", "homeRuns", "career", leagues=[103, 104], limit=5, era="modern")
            self.assertEqual([(x["name"], x["value"]) for x in board], [("Both", "50"), ("Early", "15")])
            board = mlbstats.get_leaders("hitting", "battingAverage", "career", leagues=[103, 104], limit=5)
            self.assertEqual([(x["name"], x["value"]) for x in board], [("Early", ".361"), ("Both", ".317")])
            board = mlbstats.get_leaders("hitting", "battingAverage", "career", leagues=[103, 104], limit=5,
                                         era="modern")   # Early's 2,500 plate appearances from 1901 don't qualify
            self.assertEqual([x["name"] for x in board], ["Both"])
            board = mlbstats.get_leaders("hitting", "homeRuns", "career", limit=5, era="early")
            self.assertEqual([(x["name"], x["value"], x["league"]) for x in board],
                             [("Early", "25", "NL"), ("Brotherhood", "10", "PL")])
            board = mlbstats.get_leaders("hitting", "homeRuns", "single", leagues=[105], limit=5, era="early")
            self.assertEqual([(x["name"], x["season"]) for x in board], [("Brotherhood", None)])
            self.assertEqual(mlbstats.get_leaders("hitting", "homeRuns", "career", leagues=[103], era="early"), [])
        finally:
            mlbstats.api_get = real

    def test_catchers_caught_stealing_is_ranked_per_attempt_with_a_minimum_and_careers_added_up(self):
        def row(pid, name, pos, sb, cs, lid=103, season="1960"):
            return {"player": {"id": pid, "fullName": name}, "position": {"abbreviation": pos},
                    "season": season, "team": {"name": "Club"}, "league": {"id": lid},
                    "stat": {"stolenBases": sb, "caughtStealing": cs}}
        rows = [row(1, "Arm", "C", 30, 30), row(2, "Pitcher", "P", 0, 25), row(3, "Few", "C", 2, 18),
                row(4, "Busy", "C", 70, 20), row(5, "Steady", "C", 40, 20, lid=104), row(6, "Twin", "C", 40, 20)]
        careers = {103: [row(1, "Arm", "C", 300, 300)], 104: [row(1, "Arm", "C", 100, 50), row(7, "NL", "C", 300, 200)]}

        def fake(path, params, ttl=None):
            if path == "/people":
                return {"people": []}
            got = careers.get(params["leagueId"], []) if params["stats"] == "career" else rows
            return {"stats": [{"splits": got if params["offset"] == 0 else []}]}
        real = mlbstats.api_get
        mlbstats.api_get = fake
        try:
            board = mlbstats.get_leaders("fielding", mlbstats.CS_PERCENT, "season", season=1960, min_attempts=40)
            self.assertEqual([(x["rank"], x["name"], x["value"], x["att"]) for x in board],
                             [(1, "Arm", ".500", 60), (2, "Steady", ".333", 60), (2, "Twin", ".333", 60),
                              (4, "Busy", ".222", 90)])   # Few's 20 attempts and the pitcher left out
            board = mlbstats.get_leaders("fielding", mlbstats.CS_PERCENT, "season", season=1960, leagues=[103],
                                         min_attempts=40, limit=1)
            self.assertEqual([x["name"] for x in board], ["Arm"])
            board = mlbstats.get_leaders("fielding", mlbstats.CS_PERCENT, "career", leagues=[103, 104])
            self.assertEqual([(x["name"], x["cs"], x["att"], x["league"], x["value"]) for x in board],
                             [("Arm", 350, 750, "AL/NL", ".467"), ("NL", 200, 500, "NL", ".400")])
            self.assertEqual(mlbstats.get_leaders("fielding", mlbstats.CS_PERCENT, "career", era="early"), [])
        finally:
            mlbstats.api_get = real

    def test_a_one_year_board_asks_only_for_leagues_that_played_that_year(self):
        negro = [430, 427, 426, 429, 432, 431, 428]
        self.assertEqual(mlbstats.leagues_playing(negro, 1942), [431, 428])
        self.assertEqual(mlbstats.leagues_playing([103, 106], 1915), [103, 106])
        self.assertEqual(mlbstats.leagues_playing([100], 1901), [])
        asked = []
        real = mlbstats.api_get
        mlbstats.api_get = lambda path, params, ttl=None: asked.append(params) or {}
        try:
            mlbstats.get_leaders("hitting", "homeRuns", "season", season=1942, leagues=negro)
            self.assertEqual(asked[-1]["leagueIds"], "431,428")
            self.assertEqual(mlbstats.get_leaders("hitting", "homeRuns", "season", season=1960, leagues=negro), [])
            self.assertEqual(len(asked), 1)   # nothing asked for when none of them played
        finally:
            mlbstats.api_get = real

    def test_every_leader_stat_has_a_known_kind_and_fielding_percentage_needs_a_position(self):
        for group, stats in mlbstats.LEADER_STATS.items():
            for abbr, name, category, kinds in stats:
                self.assertTrue(kinds and kinds <= set(mlbstats.LEADER_KINDS), (group, abbr))
        self.assertIn("fieldingPercentage", mlbstats.LEADERS_NEED_POSITION)


class PeopleSearchFallback(unittest.TestCase):
    def test_a_player_mlbs_name_search_misses_is_found_in_the_career_lists(self):
        def fake(path, params, ttl=None):
            if path == "/people/search":
                return {"people": []}
            if path == "/people":
                return {"people": [{"id": int(i), "fullName": "Turkey Stearnes", "mlbDebutDate": "1923-04-29",
                                    "primaryPosition": {"abbreviation": "OF"}} for i in params["personIds"].split(",")]}
            rows = [{"player": {"id": 492584, "fullName": "Turkey Stearnes", "firstName": "Turkey",
                                "lastName": "Stearnes"}}] if params.get("leagueId") == 430 else []
            return {"stats": [{"totalSplits": len(rows), "splits": rows if params["offset"] == 0 else []}]}
        real, saved = mlbstats.api_get, (mlbstats._PEOPLE_INDEX, mlbstats._FULL_NAME_INDEX)
        mlbstats.api_get, mlbstats._PEOPLE_INDEX, mlbstats._FULL_NAME_INDEX = fake, None, None
        try:
            self.assertEqual([p["id"] for p in mlbstats.search_people("stearnes")], [492584])
            self.assertEqual([p["id"] for p in mlbstats.search_people("tur stea")], [492584])  # starts of names
            self.assertEqual(mlbstats.search_people("Nobody Atall"), [])
            mlbstats._FULL_NAME_INDEX = {492584: "turkey stearnes norman thomas stearnes"}
            self.assertEqual([p["id"] for p in mlbstats.search_people("Norman Stearnes")], [492584])
        finally:
            mlbstats.api_get = real
            mlbstats._PEOPLE_INDEX, mlbstats._FULL_NAME_INDEX = saved

    def test_a_nickname_mlb_cannot_match_leads_and_other_wikidata_names_follow(self):
        def fake(path, params, ttl=None):
            if path == "/people/search":
                return {"people": [{"id": 818878, "fullName": "Big Train Jackson"}]}
            names = {116635: "Walter Johnson", 2: "Bob Trainor", 3: "Big Trainer Smith"}
            return {"people": [{"id": int(i), "fullName": names[int(i)]} for i in params["personIds"].split(",")]}
        real = mlbstats.api_get
        saved = mlbstats._WIKIDATA_NAMES, mlbstats._WIKIDATA_RAW, mlbstats._PEOPLE_INDEX
        mlbstats.api_get = fake
        mlbstats._PEOPLE_INDEX = {818878: "big train jackson", 3: "big trainer smith", 4: "bigtrain jones"}
        mlbstats._WIKIDATA_RAW = {116635: ["Walter Johnson", "The Big Train"], 1: ["Bigtrainless"],
                                  2: ["Bob Trainor", "Big Bob Trainor"], 818878: ["Big Train Jackson"]}
        mlbstats._WIKIDATA_NAMES = {pid: mlbstats._search_key(" ".join(n)) for pid, n in mlbstats._WIKIDATA_RAW.items()}
        try:
            found = mlbstats.search_people("Big Train")
            # Johnson's nickname leads; then MLB's own match, a career-list player it missed, and
            # Trainor (only the start of a word); "Bigtrainless" and "Bigtrain" don't match (each
            # word must start a word)
            self.assertEqual([(p["name"], p.get("aka")) for p in found],
                             [("Walter Johnson", "The Big Train"), ("Big Train Jackson", None),
                              ("Big Trainer Smith", None), ("Bob Trainor", "Big Bob Trainor")])
        finally:
            mlbstats.api_get = real
            mlbstats._WIKIDATA_NAMES, mlbstats._WIKIDATA_RAW, mlbstats._PEOPLE_INDEX = saved


class ScoreFormulas(unittest.TestCase):
    def setUp(self):
        self.saved = dict(formula._CUSTOM)

    def tearDown(self):
        formula.set_custom_formulas(self.saved)

    def test_qualify_blanks_a_value_short_of_its_minimum_and_product_skips_blanks(self):
        ev = formula.evaluate_formula
        self.assertEqual(ev("qualify({PA}, 3.1 * {G}, {X})", {"PA": 502, "G": 162, "X": 1.2}), 1.2)
        self.assertIsNone(ev("qualify({PA}, 3.1 * {G}, {X})", {"PA": 501, "G": 162, "X": 1.2}))   # 502 is MLB's mark
        self.assertIsNone(ev("qualify({PA}, 3.1 * {G}, {X})", {"PA": None, "G": 162, "X": 1.2}))
        self.assertEqual(ev("product({a}, {b}, 1 / {c})", {"a": 2, "b": None, "c": 4}), 0.5)
        self.assertIsNone(ev("product({a}, {b})", {"a": None, "b": None}))
        self.assertEqual(ev("average({a}, {b}, 1 / {c})", {"a": 1.5, "b": None, "c": 2}), 1.0)
        self.assertIsNone(ev("average({a})", {"a": None}))
        for bad in ("qualify(1, 2)", "nope(1)", "product", "qualify(1, 2, 3"):
            with self.assertRaises(formula.FormulaError):
                formula.parse_formula(bad)

    def test_a_custom_stat_can_use_another_and_the_info_tab_can_leave_them_alone(self):
        formula.set_custom_formulas({"RP": "{R} + {RBI}", "Loop": "{Custom: Loop}"})
        self.assertEqual(formula.formula_variables("{Custom: RP} / {PA}"), {"R", "RBI", "PA"})
        self.assertEqual(formula.evaluate_formula("{Custom: RP} / {PA}", {"R": 60, "RBI": 40, "PA": 400}), 0.25)
        self.assertEqual(formula.formula_variables("{Custom: RP}", expand=False), {"Custom: RP"})
        with self.assertRaises(formula.FormulaError):
            formula.parse_formula("{Custom: Loop}")
        with self.assertRaises(formula.FormulaError):
            formula.parse_formula("{Custom: Missing}")

    def test_the_score_files_load_and_use_only_known_variables(self):
        import glob
        defs = {}
        for path in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "*-player.json")):
            with open(path, encoding="utf-8") as fh:
                for entry in json.load(fh):
                    defs[entry["name"]] = entry["formula"]
        formula.set_custom_formulas(defs)
        for name in ("Production Score", "Prevention Score", "Range Score", "Speed Score", "Overall Score"):
            used = formula.formula_variables(defs[name])
            self.assertTrue(used <= set(mlbstats.REGRESSION_VARIABLE_INDEX), (name, used - set(mlbstats.REGRESSION_VARIABLE_INDEX)))
        # Overall: what qualifies averaged; Prevention is MLB's runs per nine / his, so higher is better
        values = {v: None for v in formula.formula_variables(defs["Overall Score"])}
        values.update({"Pitching: Outs": 600, "Team: G": 162, "Pitching: R": 50, "MLB: Runs per Nine Innings": 4.5})
        self.assertAlmostEqual(formula.evaluate_formula(defs["Overall Score"], values), 4.5 / (50 * 27 / 600))
        # Prevention: MLB's runs per nine / his, so fewer runs allowed is above 1
        self.assertAlmostEqual(formula.evaluate_formula(defs["Prevention Score"], values), 4.5 / (50 * 27 / 600))

    def test_score_views_stay_out_of_the_roster_total_lists(self):
        for view in mlbstats.SCORE_VIEWS:
            self.assertNotIn(view, mlbstats.REGRESSION_VARIABLES)
            self.assertNotIn(view, mlbstats.PLAYER_VIEWS)
            self.assertIn(f"{view}: {mlbstats.SCORE_VARIABLES[view][0][0]}", mlbstats.REGRESSION_VARIABLE_INDEX)


class PlayerScores(unittest.TestCase):
    def test_a_traded_season_has_a_line_for_each_team_and_one_for_the_whole_season(self):
        stats = {"hitting": {"season": [
            {"_season": "2022", "_team": "Nationals", "_team_id": 120, "_kind": "season"},
            {"_season": "2022", "_team": "Padres", "_team_id": 135, "_kind": "season"},
            {"_season": "2022", "_team": "2 Teams", "_team_id": None, "_kind": "total"},
            {"_season": "2023", "_team": "Padres", "_team_id": 135, "_kind": "season"}]},
            "pitching": {"season": []}, "fielding": {"season": []}}
        stints = mlbstats.person_stints(stats)
        self.assertEqual(stints, [(2022, 120, "Nationals"), (2022, 135, "Padres"), (2023, 135, "Padres")])
        self.assertEqual(mlbstats.person_batted(stats), {(2022, 120), (2022, 135), (2023, 135)})
        # a season's teams in the order he played for them, not by team id (Kingman, 1977)
        kingman = {"hitting": {"season": [
            {"_season": "1977", "_team": t, "_team_id": i, "_kind": "season"}
            for t, i in (("Mets", 121), ("Padres", 135), ("Angels", 108), ("Yankees", 147))]},
            "pitching": {"season": [{"_season": "1977", "_team": "Mets", "_team_id": 121, "_kind": "season"},
                                    {"_season": "1977", "_team": "Pirates", "_team_id": 134, "_kind": "season"}]}}
        self.assertEqual([t for _s, _i, t in mlbstats.person_stints(kingman)],
                         ["Mets", "Pirates", "Padres", "Angels", "Yankees"])  # pitching's own team after the one it follows
        pa = {(120, 2022): 400, (135, 2022): 250, (135, 2023): 600}

        def fake(team_id, season, group, ttl=None, stat_type="season"):
            if group == "team-figures":
                return [{"_id": 7, "team_games": 162}]
            return [{"_id": 7, "plateAppearances": pa[(team_id, season)]}, {"_id": 8, "plateAppearances": 99}]
        real = mlbstats.get_player_stats
        mlbstats.get_player_stats = fake
        try:
            stat = [("Qualified PA", "qualify({Batting: PA}, 3.1 * {Team: G}, {Batting: PA})")]
            rows = mlbstats.person_custom_stats(7, stints, stat, workers=1)["rows"]
        finally:
            mlbstats.get_player_stats = real
        self.assertEqual([(r["season"], r["team"], r["values"]["Qualified PA"]) for r in rows],
                         [(2022, "Nationals", None), (2022, "Padres", None), (2022, "2 Teams", 650),
                          (2023, "Padres", 600)])
        # short of 3.1 PA a game: blank, but there for show
        self.assertEqual([r["unqualified"].get("Qualified PA") for r in rows], [400, 250, None, None])

    def test_qualify_can_be_set_aside(self):
        text = "qualify({Batting: PA}, 502, {Batting: PA} * 2)"
        self.assertIsNone(formula.evaluate_formula(text, {"Batting: PA": 400}))
        self.assertEqual(formula.evaluate_formula(text, {"Batting: PA": 400}, qualifying=False), 800)
        self.assertIsNone(formula.evaluate_formula(text, {"Batting: PA": None}, qualifying=False))

    def test_a_figure_is_marked_sampled_only_where_the_play_by_play_changes_it(self):
        marked = mlbstats.mark_sampled(1.25, None, 0.4)       # Speed: blank without Retrosheet
        self.assertIsInstance(marked, mlbstats.SampledValue)
        self.assertEqual((marked, marked.coverage), (1.25, 0.4))
        self.assertNotIsInstance(mlbstats.mark_sampled(1.25, 1.25, 0.4), mlbstats.SampledValue)  # Production
        self.assertNotIsInstance(mlbstats.mark_sampled(1.25, None, None), mlbstats.SampledValue)  # the whole season
        self.assertIsNone(mlbstats.mark_sampled(None, 1.0, 0.4))

    def test_career_scores_average_one_score_a_season_given_three(self):
        def row(season, teams, **values):
            return {"season": season, "team": "", "teams": teams, "values": values}
        rows = [row(2020, 1, A=1.2, B=0.9),
                row(2021, 1, A=None, B=1.1), row(2021, 1, A=0.8, B=None), row(2021, 2, A=None, B=1.3),
                row(2022, 1, A=1.0, B=None),
                row(2023, 1, A=None, B=1.0)]
        career = mlbstats.career_scores(rows, ("A", "B", "C"))
        # A: 2020 1.2, 2021 its team line 0.8 (the whole-season line's blank), 2022 1.0
        self.assertAlmostEqual(career["A"][0], 1.0)
        self.assertEqual(career["A"][1:], (3, False))
        # B: 2020 0.9, 2021 the whole-season 1.3 (not the team line's 1.1), 2023 1.0
        self.assertAlmostEqual(career["B"][0], (0.9 + 1.3 + 1.0) / 3)
        self.assertEqual(career["C"], (None, 0, False))
        self.assertEqual(mlbstats.career_scores(rows[:3], ("A",))["A"], (None, 2, False))  # fewer than three
        rows[2]["sampled"] = {"A": 0.4}   # 2021's team line for A rests on part of the season's play-by-play
        self.assertTrue(mlbstats.career_scores(rows, ("A",))["A"][2])
        self.assertFalse(mlbstats.career_scores(rows, ("B",))["B"][2])


class LeagueRecords(unittest.TestCase):
    AL, NL = "American League", "National League"
    PAYLOAD = {"records": [
        # AL West (200) listed before AL East (201) to prove the output is put in order
        _standing(103, 200, [([(103, 60, 40), (104, 10, 12)],
                              [(200, f"{AL} West", 20, 20), (201, f"{AL} East", 15, 10)])]),
        _standing(103, 201, [([(103, 50, 50), (104, 12, 10)],
                              [(201, f"{AL} East", 10, 15), (200, f"{AL} West", 20, 20)])]),
        _standing(104, 203, [([(104, 70, 70), (103, 12, 10)],
                              [(203, f"{NL} West", 30, 30), (204, f"{NL} East", 8, 6)])]),
        _standing(104, 204, [([(104, 70, 70), (103, 10, 12)],
                              [(204, f"{NL} East", 25, 25), (203, f"{NL} West", 6, 8)])]),
    ]}

    def test_interleague_totals_mirror_each_other(self):
        r = mlbstats.parse_league_records(self.PAYLOAD)
        self.assertEqual(r["interleague"], {103: (22, 22), 104: (22, 22)})
        self.assertEqual(r["leagues"], {103: "American League", 104: "National League"})

    def test_a_division_counts_its_teams_games_against_the_rest_of_its_league(self):
        r = mlbstats.parse_league_records(self.PAYLOAD)
        rows = [(d["name"], d["w"], d["l"]) for d in r["interdivision"]]
        # each division's games against its OWN division are left out; East before West, AL before NL
        self.assertEqual(rows, [("American League East", 20, 20), ("American League West", 15, 10),
                                ("National League East", 6, 8), ("National League West", 8, 6)])

    def test_a_division_also_counts_its_teams_games_against_the_other_league(self):
        r = mlbstats.parse_league_records(self.PAYLOAD)
        self.assertEqual([(d["name"], d["il_w"], d["il_l"]) for d in r["interdivision"]],
                         [("American League East", 12, 10), ("American League West", 10, 12),
                          ("National League East", 10, 12), ("National League West", 12, 10)])
        merged = mlbstats.merge_league_records(r, r)
        self.assertEqual((merged["interdivision"][0]["il_w"], merged["interdivision"][0]["il_l"]), (24, 20))

    def test_the_division_matrix_counts_each_game_once_from_both_sides(self):
        def game(pk, away, home, away_wins, state="Final"):
            return {"gamePk": pk, "status": {"abstractGameState": state}, "teams": {
                "away": {"team": {"id": away}, "isWinner": away_wins},
                "home": {"team": {"id": home}, "isWinner": not away_wins}}}
        teams = {1: (200, 2, 1), 2: (200, 2, 1), 3: (204, 0, 2)}
        payload = {"dates": [{"games": [game(10, 1, 2, True), game(11, 3, 1, False),
                                        game(10, 1, 2, True),               # listed again: counted once
                                        game(12, 2, 3, False, "Preview")]},  # not played
                             {"games": [game(13, 2, 3, True), game(14, 2, 1, True)]}]}
        m = mlbstats.parse_division_matrix(payload, teams)
        self.assertEqual(m["cells"], {(200, 200): (2, 2), (200, 204): (2, 0), (204, 200): (0, 2)})
        self.assertEqual(m["missing"], 0)
        teams[3] = (204, 0, 3)          # one game the schedule doesn't have
        self.assertEqual(mlbstats.parse_division_matrix(payload, teams)["missing"], 1)

    def test_an_empty_or_pre_interleague_response_is_empty(self):
        r = mlbstats.parse_league_records({"records": []})
        self.assertEqual((r["interleague"], r["interdivision"]), ({}, []))
        self.assertEqual(mlbstats.parse_league_records({})["interdivision"], [])

    def test_complete_records_are_not_flagged(self):
        self.assertEqual(mlbstats.parse_league_records(self.PAYLOAD)["incomplete"], 0)

    def test_a_team_whose_league_records_fall_short_of_its_overall_record_is_flagged(self):
        payload = json.loads(json.dumps(self.PAYLOAD))
        payload["records"][0]["teamRecords"][0]["wins"] += 5          # 5 wins the splits don't account for
        payload["records"][2]["teamRecords"][0]["losses"] += 1
        self.assertEqual(mlbstats.parse_league_records(payload)["incomplete"], 2)

    def test_two_halves_add_up(self):
        a = mlbstats.parse_league_records(self.PAYLOAD)
        merged = mlbstats.merge_league_records(a, a)
        self.assertEqual(merged["interleague"], {103: (44, 44), 104: (44, 44)})
        self.assertEqual([(d["name"], d["w"], d["l"]) for d in merged["interdivision"]][:2],
                         [("American League East", 40, 40), ("American League West", 30, 20)])
        self.assertEqual(merged["incomplete"], 0)

    def _split_season_fetch(self, whole_is_complete: bool, halves_are_complete: bool = True):
        good = self.PAYLOAD
        short = json.loads(json.dumps(good))
        short["records"][0]["teamRecords"][0]["wins"] += 9
        calls = []
        def fake(path, params=None, ttl=None):
            calls.append(params["standingsTypes"])
            if params["standingsTypes"] == "regularSeason":
                return good if whole_is_complete else short
            return good if halves_are_complete else short
        real, mlbstats.api_get = mlbstats.api_get, fake
        try:
            return mlbstats.get_league_records(1981), calls
        finally:
            mlbstats.api_get = real

    def test_a_complete_season_takes_one_request(self):
        records, calls = self._split_season_fetch(whole_is_complete=True)
        self.assertEqual((calls, records["halves"]), (["regularSeason"], False))

    def test_an_incomplete_season_is_rebuilt_from_its_two_halves(self):
        records, calls = self._split_season_fetch(whole_is_complete=False)
        self.assertEqual(calls, ["regularSeason", "firstHalf", "secondHalf"])
        self.assertTrue(records["halves"])
        self.assertEqual(records["interleague"], {103: (44, 44), 104: (44, 44)})

    def test_if_the_halves_are_no_better_the_whole_season_is_kept_with_its_warning(self):
        records, _calls = self._split_season_fetch(whole_is_complete=False, halves_are_complete=False)
        self.assertFalse(records["halves"])
        self.assertEqual(records["incomplete"], 1)

    def test_fetch_asks_for_both_leagues_regular_season(self):
        calls = []
        def fake(path, params=None, ttl=None):
            calls.append((path, dict(params), ttl))
            return self.PAYLOAD
        real, mlbstats.api_get = mlbstats.api_get, fake
        try:
            mlbstats.get_league_records(1997, ttl=7)
        finally:
            mlbstats.api_get = real
        path, params, ttl = calls[0]
        self.assertEqual((path, params["season"], params["leagueId"], params["standingsTypes"], ttl),
                         ("/standings", 1997, "103,104", "regularSeason", 7))
        self.assertEqual((mlbstats.INTERDIVISION_FIRST_SEASON, mlbstats.INTERLEAGUE_FIRST_SEASON), (1969, 1997))


def _team(tid, name, league, division=None):
    return {"id": tid, "name": name, "abbreviation": name[:3].upper(), "teamName": name,
            "locationName": "", "firstYearOfPlay": "1901",
            "league": {"id": league[0], "name": league[1]},
            **({"division": {"id": division[0], "name": division[1]}} if division else {})}


AL, NL, NNL = (103, "American League"), (104, "National League"), (430, "Negro National League (I)")
AL_E, AL_W, NL_E = (201, "American League East"), (200, "American League West"), (204, "National League East")


class TeamScopes(unittest.TestCase):
    SEASON = mlbstats.parse_teams({"teams": [
        _team(1, "Yankees", AL, AL_E), _team(2, "Red Sox", AL, AL_E), _team(3, "Mariners", AL, AL_W),
        _team(4, "Mets", NL, NL_E), _team(5, "Monarchs", NNL),          # a Negro league club: no division
    ]})
    TEAMS = {t["id"]: t for t in SEASON}

    def test_teams_carry_their_league_and_division(self):
        self.assertEqual((self.TEAMS[1]["league"], self.TEAMS[1]["division"], self.TEAMS[1]["division_id"]),
                         ("American League", "American League East", 201))
        self.assertEqual((self.TEAMS[5]["league"], self.TEAMS[5]["division"]), ("Negro National League (I)", None))

    def test_a_team_record_without_league_or_division_is_none(self):
        bare = mlbstats.parse_teams({"teams": [{"id": 9, "name": "X"}]})[0]
        self.assertEqual((bare["league"], bare["league_id"], bare["division"], bare["division_id"]),
                         (None, None, None, None))

    def test_league_scope_is_every_team_including_the_negro_leagues(self):
        self.assertEqual(set(mlbstats.teams_in_scope(self.TEAMS, "league", 1)), {1, 2, 3, 4, 5})

    def test_same_league_is_the_anchors_own_league(self):
        self.assertEqual(set(mlbstats.teams_in_scope(self.TEAMS, "same_league", 1)), {1, 2, 3})
        self.assertEqual(set(mlbstats.teams_in_scope(self.TEAMS, "same_league", 5)), {5})   # its own league

    def test_same_division_is_the_anchors_own_division(self):
        self.assertEqual(set(mlbstats.teams_in_scope(self.TEAMS, "same_division", 2)), {1, 2})
        self.assertEqual(set(mlbstats.teams_in_scope(self.TEAMS, "same_division", 3)), {3})

    def test_no_division_or_no_anchor_gives_nothing(self):
        self.assertEqual(mlbstats.teams_in_scope(self.TEAMS, "same_division", 5), {})   # Negro league: no divisions
        self.assertEqual(mlbstats.teams_in_scope(self.TEAMS, "same_league", 99), {})    # didn't play that season
        self.assertEqual(mlbstats.teams_in_scope(self.TEAMS, "same_division", 99), {})


def _entry(tid, name, w, l, rank, gb="-", ties=0, division_rank=None, league_rank=None, **extra):
    return {"team": {"id": tid, "name": name}, "wins": w, "losses": l,
            "winningPercentage": f".{round(w / (w + l) * 1000):03d}",
            "leagueRecord": {"wins": w, "losses": l, "ties": ties},
            "divisionRank": division_rank, "leagueRank": league_rank,
            "divisionGamesBack": gb, "leagueGamesBack": gb,
            "runsScored": 700, "runsAllowed": 650, "runDifferential": 50, "gamesPlayed": w + l + ties, **extra}


def _standing_group(league, division, entries):
    return {"league": {"id": league[0], "name": league[1]},
            "division": {"id": division[0], "name": division[1]} if division else {}, "teamRecords": entries}


class Standings(unittest.TestCase):
    PAYLOAD = {"records": [
        _standing_group((104, "National League"), (204, "National League East"),
                        [_entry(1, "Braves", 90, 72, 0, division_rank="1")]),
        _standing_group((427, "Eastern Colored League"), None,          # a Negro league: no divisions
                        [_entry(7, "Bacharach Giants", 26, 28, 0, gb="9.0", ties=2, league_rank="4"),
                         _entry(8, "Hilldale Daisies", 45, 13, 0, league_rank="1")]),
        _standing_group((103, "American League"), (200, "American League West"),
                        [_entry(3, "Mariners", 61, 101, 0, gb="29.0", division_rank="4"),
                         _entry(2, "Rangers", 90, 72, 0, division_rank="1")]),
        _standing_group((103, "American League"), (201, "American League East"),
                        [_entry(4, "Yankees", 95, 67, 0, division_rank="2", gb="1.0"),
                         _entry(5, "Rays", 96, 66, 0, division_rank="1")]),
    ]}

    def test_groups_come_in_league_then_division_order(self):
        names = [g["name"] for g in mlbstats.parse_standings(self.PAYLOAD)]
        self.assertEqual(names, ["American League East", "American League West",
                                 "National League East", "Eastern Colored League"])

    def test_teams_within_a_group_are_best_first(self):
        groups = {g["name"]: g for g in mlbstats.parse_standings(self.PAYLOAD)}
        self.assertEqual([t["name"] for t in groups["American League East"]["teams"]], ["Rays", "Yankees"])
        self.assertEqual([t["name"] for t in groups["American League West"]["teams"]], ["Rangers", "Mariners"])

    def test_a_divisional_group_uses_division_rank_and_games_back(self):
        rays, yankees = mlbstats.parse_standings(self.PAYLOAD)[0]["teams"]
        self.assertEqual((rays["rank"], rays["gb"], yankees["rank"], yankees["gb"]), (1, "-", 2, "1.0"))

    def test_a_league_without_divisions_uses_league_rank_and_ties_are_kept(self):
        group = mlbstats.parse_standings(self.PAYLOAD)[-1]
        self.assertEqual((group["division_id"], group["name"]), (None, "Eastern Colored League"))
        self.assertEqual([(t["name"], t["rank"], t["ties"]) for t in group["teams"]],
                         [("Hilldale Daisies", 1, 0), ("Bacharach Giants", 4, 2)])

    def test_run_figures_and_pct_come_through(self):
        team = mlbstats.parse_standings(self.PAYLOAD)[0]["teams"][0]
        self.assertEqual((team["wins"], team["losses"], team["pct"], team["rs"], team["ra"], team["diff"]),
                         (96, 66, ".593", 700, 650, 50))

    def test_empty_payload(self):
        self.assertEqual(mlbstats.parse_standings({}), [])

    def test_the_fetch_asks_for_every_league_with_team_and_division_names(self):
        calls = []
        def fake(path, params=None, ttl=None):
            calls.append((path, dict(params), ttl))
            return self.PAYLOAD
        real, mlbstats.api_get = mlbstats.api_get, fake
        try:
            mlbstats.get_standings(1925, ttl=3)
        finally:
            mlbstats.api_get = real
        path, params, ttl = calls[0]
        self.assertEqual((path, params["season"], params["standingsTypes"], params["hydrate"], ttl),
                         ("/standings", 1925, "regularSeason", "team,division,league", 3))
        ids = params["leagueId"].split(",")
        self.assertTrue({"103", "104", "430", "427", "100"} <= set(ids))     # AL NL, Negro leagues, 19th century
        self.assertFalse({"114", "115"} & set(ids))                          # not the spring-training leagues


class FranchiseHistory(unittest.TestCase):
    """One franchise id across seasons, under each season's own name."""

    NAMES = {1901: "Milwaukee Brewers", 1902: "St. Louis Browns", 1903: "St. Louis Browns",
             1954: "Baltimore Orioles"}

    def _fake(self, missing=(), failing=()):
        def fake(path, params=None, ttl=None):
            season = params["season"]
            if season in failing:
                raise mlbstats.MLBStatsError("boom")
            teams = [] if season in missing or season not in self.NAMES else \
                [_entry(110, self.NAMES[season], 70, 80, 0, league_rank="5")]
            return {"records": [_standing_group((103, "American League"), None, teams)]}
        return fake

    def _history(self, seasons, **kw):
        real, mlbstats.api_get = mlbstats.api_get, self._fake(**kw)
        try:
            return mlbstats.franchise_history(110, seasons)
        finally:
            mlbstats.api_get = real

    def test_each_season_carries_that_seasons_name(self):
        rows, failed = self._history([1954, 1901, 1902])
        self.assertEqual([(r["season"], r["name"]) for r in rows],
                         [(1901, "Milwaukee Brewers"), (1902, "St. Louis Browns"), (1954, "Baltimore Orioles")])
        self.assertEqual(failed, [])
        self.assertEqual((rows[0]["group"], rows[0]["league"]), ("American League", "American League"))

    def test_a_season_the_club_sat_out_is_absent_and_a_failed_one_is_reported(self):
        rows, failed = self._history(range(1900, 1904), missing=(1903,), failing=(1900,))
        self.assertEqual([r["season"] for r in rows], [1901, 1902])
        self.assertEqual(failed, [1900])

    def test_name_eras_group_consecutive_seasons_and_show_a_return_to_an_old_name(self):
        rows = [{"season": y, "name": n} for y, n in
                [(1935, "Boston Braves"), (1936, "Boston Bees"), (1937, "Boston Bees"), (1941, "Boston Braves")]]
        self.assertEqual(mlbstats.name_eras(rows), [("Boston Braves", 1935, 1935), ("Boston Bees", 1936, 1937),
                                                    ("Boston Braves", 1941, 1941)])
        self.assertEqual(mlbstats.name_eras([]), [])

    def test_ordinals(self):
        self.assertEqual([mlbstats.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101, 111)],
                         ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd", "101st", "111th"])


def _wc(tid, name, w, l, wc_rank=None, wc_gb="-", leader=None, clinched=False, indicator=None,
        div_rank=None, champ=False, ties=0):
    return {"team": {"id": tid, "name": name}, "wins": w, "losses": l, "winningPercentage": ".500",
            "leagueRecord": {"wins": w, "losses": l, "ties": ties},
            "wildCardRank": None if wc_rank is None else str(wc_rank), "wildCardGamesBack": wc_gb,
            "wildCardLeader": leader, "clinched": clinched, "clinchIndicator": indicator,
            "divisionRank": None if div_rank is None else str(div_rank), "divisionChamp": champ,
            "runsScored": 700, "runsAllowed": 650, "runDifferential": 50}


def _wc_record(kind, league, teams):
    return {"standingsType": kind, "league": {"id": league[0], "name": league[1]}, "teamRecords": teams}


class WildCardStandings(unittest.TestCase):
    AL_ = (103, "American League")
    PAYLOAD = {"records": [
        _wc_record("wildCard", (104, "National League"),
                   [_wc(20, "Phillies", 90, 72, 1, "+3.0", True, True, "w", 2)]),
        _wc_record("wildCard", AL_, [
            _wc(3, "Blue Jays", 89, 73, 3, "-", True, True, "w", 3),          # the last wild card spot
            _wc(1, "Rays", 99, 63, 1, "+10.0", True, True, "w", 2),
            _wc(2, "Rangers", 90, 72, 2, "+1.0", True, True, "w", 2),
            _wc(5, "Yankees", 82, 80, 5, "7.0", None, False, None, 4),
            _wc(4, "Mariners", 88, 74, 4, "1.0", None, False, None, 3)]),
        _wc_record("divisionLeaders", AL_, [_wc(10, "Twins", 87, 75, champ=True, clinched=True, indicator="y", div_rank=1)]),
        _wc_record("divisionLeaders", AL_, [_wc(11, "Orioles", 101, 61, champ=True, clinched=True, indicator="z", div_rank=1)]),
        _wc_record("divisionLeaders", AL_, [_wc(12, "Astros", 90, 72, champ=True, clinched=True, indicator="y", div_rank=1),
                                            _wc(13, "Angels", 60, 100, clinched=True, indicator="x", div_rank=2)]),  # 2020-style
    ]}

    def _american(self):
        return mlbstats.parse_wildcard(self.PAYLOAD)[0]

    def test_leagues_in_order(self):
        self.assertEqual([lg["league"] for lg in mlbstats.parse_wildcard(self.PAYLOAD)],
                         ["American League", "National League"])

    def test_division_leaders_come_first_best_record_first_then_wild_card_holders_then_the_rest(self):
        teams = self._american()["teams"]
        self.assertEqual([(t["name"], t["section"]) for t in teams], [
            ("Orioles", "division"), ("Astros", "division"), ("Twins", "division"), ("Angels", "division"),
            ("Rays", "wild_card"), ("Rangers", "wild_card"), ("Blue Jays", "wild_card"),
            ("Mariners", "other"), ("Yankees", "other")])

    def test_games_back_are_from_the_last_wild_card_spot_and_shown_only_for_the_rest(self):
        by_name = {t["name"]: t for t in self._american()["teams"]}
        self.assertEqual([by_name[n]["gb"] for n in ("Orioles", "Rays", "Rangers", "Blue Jays")], ["-"] * 4)
        self.assertEqual((by_name["Mariners"]["gb"], by_name["Yankees"]["gb"]), ("1.0", "7.0"))

    def test_a_wild_card_holders_margin_is_kept_separately(self):
        by_name = {t["name"]: t for t in self._american()["teams"]}
        self.assertEqual((by_name["Rays"]["ahead"], by_name["Rangers"]["ahead"], by_name["Blue Jays"]["ahead"]),
                         ("10.0", "1.0", ""))

    def test_status_uses_the_clinch_flags(self):
        by_name = {t["name"]: t for t in self._american()["teams"]}
        self.assertEqual(by_name["Orioles"]["status"], "Division champion")
        self.assertEqual(by_name["Rays"]["status"], "Wild card")
        self.assertEqual(by_name["Angels"]["status"], "Division runner-up, clinched a playoff spot")
        self.assertEqual(by_name["Mariners"]["status"], "")

    def test_a_race_still_on_is_not_called_clinched(self):
        payload = {"records": [
            _wc_record("wildCard", self.AL_, [_wc(1, "Sox", 84, 71, 2, "+5.0", True, False, None, 3)]),
            _wc_record("divisionLeaders", self.AL_, [_wc(2, "Guardians", 80, 75, div_rank=1),
                                                      _wc(3, "Rays", 94, 60, clinched=True, indicator="x", div_rank=1)])]}
        by_name = {t["name"]: t for t in mlbstats.parse_wildcard(payload)[0]["teams"]}
        self.assertEqual(by_name["Sox"]["status"], "Wild card position")
        self.assertEqual(by_name["Guardians"]["status"], "Division leader")
        self.assertEqual(by_name["Rays"]["status"], "Division leader, clinched a playoff spot")

    def test_empty_payload_and_the_request(self):
        self.assertEqual(mlbstats.parse_wildcard({}), [])
        calls = []
        def fake(path, params=None, ttl=None):
            calls.append((path, dict(params), ttl))
            return self.PAYLOAD
        real, mlbstats.api_get = mlbstats.api_get, fake
        try:
            mlbstats.get_wildcard_standings(2023, ttl=4)
        finally:
            mlbstats.api_get = real
        path, params, ttl = calls[0]
        self.assertEqual((path, params["season"], params["standingsTypes"], params["leagueId"], ttl),
                         ("/standings", 2023, "wildCardWithLeaders", "103,104", 4))
        self.assertEqual(mlbstats.WILD_CARD_FIRST_SEASON, 1994)


PYTH_FILES = {  # file -> (custom stat name, expected value for the line below)
    "pythagorean-win-loss-percentage-team.json": ("Pythagorean Win-Loss Percentage", 0.609756),
    "pythagorean-wins-team.json": ("Pythagorean Wins", 98.780488),
    "pythagorean-losses-team.json": ("Pythagorean Losses", 63.219512),
}
PYTH_LINE = {"wins": 99, "losses": 63, "rs": 500, "ra": 400}   # RS^2 / (RS^2 + RA^2) = 250000 / 410000


class PythagoreanStats(unittest.TestCase):
    """The three team-level JSON files behind the Standings tab's PW, PL and PPct columns."""

    def _definition(self, fname):
        with open(os.path.join(HERE, fname), encoding="utf-8") as fh:
            entries = json.load(fh)
        self.assertEqual(len(entries), 1, fname)
        return entries[0]

    def test_each_file_is_one_team_level_definition_using_known_team_variables(self):
        team_variables = {f"{view}: {label}" for view, _g, label, _k in mlbstats.DEPENDENT_VARIABLES}
        for fname, (name, _value) in PYTH_FILES.items():
            d = self._definition(fname)
            self.assertEqual((d["level"], d["name"]), ("team", name), fname)
            used = formula.formula_variables(d["formula"])
            self.assertLessEqual(used, team_variables, fname)              # real Team totals fields
            self.assertLessEqual(used, mlbstats.STANDINGS_VARIABLES, fname)  # ...that a standings line has

    def test_each_formula_gives_the_pythagorean_figure_for_a_standings_line(self):
        for fname, (_name, expected) in PYTH_FILES.items():
            got = mlbstats.standings_stat(self._definition(fname)["formula"], PYTH_LINE)
            self.assertAlmostEqual(got, expected, places=5, msg=fname)

    def test_wins_and_losses_add_up_to_the_games_with_a_decision(self):
        w = mlbstats.standings_stat(self._definition("pythagorean-wins-team.json")["formula"], PYTH_LINE)
        l = mlbstats.standings_stat(self._definition("pythagorean-losses-team.json")["formula"], PYTH_LINE)
        self.assertAlmostEqual(w + l, 99 + 63)

    def test_percentage_is_wins_over_decisions(self):
        pct = mlbstats.standings_stat(self._definition("pythagorean-win-loss-percentage-team.json")["formula"], PYTH_LINE)
        w = mlbstats.standings_stat(self._definition("pythagorean-wins-team.json")["formula"], PYTH_LINE)
        self.assertAlmostEqual(pct, w / 162)

    def test_missing_runs_give_none_and_a_formula_needing_more_is_unusable(self):
        formula_text = self._definition("pythagorean-wins-team.json")["formula"]
        self.assertIsNone(mlbstats.standings_stat(formula_text, dict(PYTH_LINE, rs=None)))
        self.assertIsNone(mlbstats.standings_stat("{Batting: Hits} / 2", PYTH_LINE))      # not on a standings line
        self.assertIsNone(mlbstats.standings_stat(formula_text, dict(PYTH_LINE, rs=0, ra=0)))  # 0 / 0


class ComputedStandings(unittest.TestCase):
    """Standings MLB doesn't publish (the 1892 National League, the Federal League) worked out
    from the clubs' season stats."""

    CLUBS = {1: "Chicago Whales", 2: "St. Louis Terriers", 3: "Pittsburgh Rebels", 4: "No Stats Club"}
    PITCHING = {1: {"wins": 86, "losses": 66, "gamesPlayed": 155, "runs": 538},
                2: {"wins": 87, "losses": 67, "gamesPlayed": 159, "runs": 528},
                3: {"wins": 86, "losses": 67, "gamesPlayed": 156, "runs": 524}}
    HITTING = {1: {"runs": 641}, 2: {"runs": 634}, 3: {"runs": 592}}

    def _group(self):
        return mlbstats.standings_from_team_stats(106, "Federal League", self.CLUBS, self.PITCHING, self.HITTING)

    def test_ranked_by_winning_percentage_not_by_wins(self):
        # 86-66 (.5658) beats 87-67 (.5649) even though the latter has more wins
        self.assertEqual([t["name"] for t in self._group()["teams"]],
                         ["Chicago Whales", "St. Louis Terriers", "Pittsburgh Rebels"])

    def test_percentage_games_back_and_run_figures(self):
        teams = self._group()["teams"]
        whales, terriers, rebels = teams
        self.assertEqual((whales["rank"], whales["pct"], whales["gb"]), (1, ".566", "-"))
        self.assertEqual((terriers["pct"], terriers["gb"]), (".565", "0.0"))      # (86-87 + 67-66) / 2
        self.assertEqual(rebels["gb"], "0.5")                                       # (86-86 + 67-66) / 2 -> 0.5
        self.assertEqual((whales["rs"], whales["ra"], whales["diff"]), (641, 538, 103))

    def test_ties_are_the_games_beyond_wins_and_losses(self):
        self.assertEqual([t["ties"] for t in self._group()["teams"]], [3, 5, 3])

    def test_a_club_without_stats_is_left_out_and_the_group_is_marked_computed(self):
        group = self._group()
        self.assertEqual(len(group["teams"]), 3)
        self.assertTrue(group["computed"])
        self.assertEqual((group["league_id"], group["name"], group["division_id"]), (106, "Federal League", None))

    def test_the_gap_table_names_exactly_the_league_seasons_found_missing(self):
        self.assertEqual(mlbstats.STANDINGS_GAPS, {1892: (104,), 1914: (106,), 1915: (106,)})

    def _fetch(self, season, standings_payload):
        team_map = {tid: {"id": tid, "name": name, "league_id": 106, "league": "Federal League"}
                    for tid, name in self.CLUBS.items()}
        calls = []
        def fake(path, params=None, ttl=None):
            calls.append((path, params.get("standingsTypes") or params.get("group")))
            if path == "/standings":
                return standings_payload
            block = self.PITCHING if params["group"] == "pitching" else self.HITTING
            return {"stats": [{"splits": [{"team": {"id": tid}, "stat": stat} for tid, stat in block.items()]}]}
        real_api, real_map = mlbstats.api_get, mlbstats.get_team_map
        mlbstats.api_get, mlbstats.get_team_map = fake, lambda season, ttl=None: team_map
        try:
            return mlbstats.get_standings(season), calls
        finally:
            mlbstats.api_get, mlbstats.get_team_map = real_api, real_map

    def test_a_gap_season_adds_the_computed_league_to_what_MLB_did_publish(self):
        published = {"records": [_standing_group((103, "American League"), None, [_entry(9, "Red Sox", 101, 50, 0, league_rank="1")])]}
        groups, calls = self._fetch(1915, published)
        self.assertEqual([(g["name"], bool(g.get("computed"))) for g in groups],
                         [("American League", False), ("Federal League", True)])
        self.assertIn(("/teams/stats", "pitching"), calls)

    def test_an_ordinary_season_makes_no_extra_requests(self):
        published = {"records": [_standing_group((103, "American League"), None, [_entry(9, "Red Sox", 101, 50, 0, league_rank="1")])]}
        groups, calls = self._fetch(1925, published)
        self.assertEqual(calls, [("/standings", "regularSeason")])
        self.assertFalse(any(g.get("computed") for g in groups))

    def test_a_league_MLB_does_publish_in_a_gap_year_is_left_alone(self):
        published = {"records": [_standing_group((106, "Federal League"), None, [_entry(1, "Whales", 86, 66, 0, league_rank="1")])]}
        groups, calls = self._fetch(1915, published)
        self.assertEqual(calls, [("/standings", "regularSeason")])
        self.assertFalse(groups[0].get("computed"))


def _pitcher(pid, name, gs, gf, outs, runs, earned, faced=100):
    return {"_id": pid, "_name": name, "gamesStarted": gs, "gamesFinished": gf, "outs": outs,
            "inningsPitched": mlbstats.outs_to_innings(outs), "runs": runs, "earnedRuns": earned,
            "battersFaced": faced}


PREVENTION_FILES = {  # file -> (custom stat name, value for the pitcher below: 50 R, 45 ER, 300 outs)
    "runs-per-nine-innings-player.json": ("Runs per Nine Innings", 4.5),
    "earned-run-average-player.json": ("Earned Run Average", 4.05),
    "unearned-runs-per-nine-innings-player.json": ("Unearned Runs per Nine Innings", 0.45),
}


class PreventionStatFiles(unittest.TestCase):
    """The three player-level JSON files behind the Prevention tab's per-nine-innings columns."""

    def _formula(self, fname):
        with open(os.path.join(HERE, fname), encoding="utf-8") as fh:
            entries = json.load(fh)
        self.assertEqual(len(entries), 1, fname)
        return entries[0]

    def test_each_file_is_one_player_level_definition_using_known_variables(self):
        for fname, (name, _value) in PREVENTION_FILES.items():
            d = self._formula(fname)
            self.assertEqual((d["level"], d["name"]), ("player", name), fname)
            self.assertLessEqual(formula.formula_variables(d["formula"]), set(mlbstats.REGRESSION_VARIABLE_INDEX), fname)

    def test_the_formulas_use_outs_so_a_third_of_an_inning_is_exact(self):
        # innings pitched "172.1" is read as 172.1 by the IP variable; outs are exact
        for fname in PREVENTION_FILES:
            self.assertIn("Pitching: Outs", formula.formula_variables(self._formula(fname)["formula"]), fname)

    def test_each_formula_gives_its_per_nine_figure(self):
        groups = {"Pitching": mlbstats.group_by_player([_pitcher(1, "P", 0, 0, 300, 50, 45)])}
        for fname, (_name, expected) in PREVENTION_FILES.items():
            got = mlbstats.production_values(self._formula(fname)["formula"], groups, [1])[1]
            self.assertAlmostEqual(got, expected, msg=fname)

    def test_games_finished_and_outs_are_variables(self):
        self.assertEqual(mlbstats.REGRESSION_VARIABLE_INDEX["Pitching: GF"], ("Pitching", "gamesFinished", "sum"))
        self.assertEqual(mlbstats.REGRESSION_VARIABLE_INDEX["Pitching: Outs"], ("Pitching", "outs", "sum"))


class PitcherRoles(unittest.TestCase):
    # 6 pitchers: 4 have started (average 37/4 = 9.25 GS), 4 have finished (38/4 = 9.5 GF); outs 250
    STAFF = [
        _pitcher(1, "Ace", 20, 0, 600, 60, 55),
        _pitcher(2, "Closer", 0, 20, 90, 10, 9),
        _pitcher(3, "Swingman", 10, 12, 300, 60, 55),     # above average in GS AND GF -> Rotation wins
        _pitcher(4, "Long Man", 2, 1, 300, 25, 23),       # below both averages, above average outs
        _pitcher(5, "Mop-up", 0, 0, 60, 8, 8),
        _pitcher(6, "At Average", 5, 5, 150, 15, 14),     # below every average -> Other
    ]

    def _roles(self):
        return {role: [r["_name"] for r in members]
                for role, members in mlbstats.classify_pitchers(self.STAFF)["roles"].items()}

    def test_the_averages_are_per_pitcher_on_the_team(self):
        a = mlbstats.classify_pitchers(self.STAFF)["averages"]
        self.assertEqual((a["pitchers"], a["starters"], a["finishers"]), (6, 4, 4))
        self.assertAlmostEqual(a["gs"], 37 / 4)                        # only the pitchers who started
        self.assertAlmostEqual(a["gf"], 38 / 4)                        # only the pitchers who finished a game
        self.assertAlmostEqual(a["outs"], 1500 / 6)

    def test_roles_follow_the_stated_rules(self):
        roles = self._roles()
        self.assertEqual(roles["Rotation"], ["Ace", "Swingman"])       # GS at or above the starters' 9.25; Swingman's GF doesn't matter
        self.assertEqual(roles["Closers"], ["Closer"])                 # GF 20 at or above the finishers' 9.5, not Rotation
        self.assertEqual(roles["Long Relief"], ["Long Man"])           # GS and GF below average, 300 outs above the 150 non-Rotation average
        self.assertEqual(roles["Other"], ["At Average", "Mop-up"])     # busiest first

    def test_long_relief_innings_are_measured_against_the_pitchers_outside_the_rotation(self):
        # three starters at 600 outs drag the staff average to ~295; the only relief pitcher near it
        # is well below, yet at 120 outs he is the longest of the relievers (their average is 67.5)
        staff = [_pitcher(1, "A", 30, 0, 600, 0, 0), _pitcher(2, "B", 30, 0, 600, 0, 0),
                 _pitcher(3, "C", 30, 0, 600, 0, 0), _pitcher(4, "Long", 0, 0, 120, 0, 0),
                 _pitcher(5, "Short", 0, 3, 60, 0, 0), _pitcher(6, "Closer", 0, 20, 60, 0, 0),
                 _pitcher(7, "Mop", 0, 1, 30, 0, 0)]
        c = mlbstats.classify_pitchers(staff)
        self.assertAlmostEqual(c["averages"]["outs"], 2070 / 7)          # the whole staff's average
        self.assertAlmostEqual(c["averages"]["relief_outs"], 270 / 4)    # what Long Relief is measured against
        roles = {role: [r["_name"] for r in members] for role, members in c["roles"].items()}
        self.assertEqual(roles["Long Relief"], ["Long"])
        self.assertEqual(roles["Closers"], ["Closer"])
        self.assertEqual(roles["Other"], ["Short", "Mop"])
        self.assertEqual(roles["Rotation"], ["A", "B", "C"])

    def test_long_relief_needs_a_game_in_relief(self):
        # Spot pitched long innings below the Rotation cutoff, but only ever as a starter
        staff = [_pitcher(1, "A", 30, 0, 600, 0, 0), _pitcher(2, "B", 30, 0, 600, 0, 0),
                 _pitcher(3, "Spot", 4, 0, 120, 0, 0), _pitcher(4, "Long", 0, 0, 120, 0, 0),
                 _pitcher(5, "Short", 0, 3, 30, 0, 0)]
        for row, games in zip(staff, (30, 30, 4, 25, 20)):
            row["gamesPitched"] = games
        roles = {role: [r["_name"] for r in members]
                 for role, members in mlbstats.classify_pitchers(staff)["roles"].items()}
        self.assertEqual(roles["Long Relief"], ["Long"])
        self.assertEqual(roles["Other"], ["Spot"])                    # (Short, the only finisher, closes)
        staff[2]["gamesPitched"] = 5  # one relief appearance is enough
        roles = {role: [r["_name"] for r in members]
                 for role, members in mlbstats.classify_pitchers(staff)["roles"].items()}
        self.assertEqual(roles["Long Relief"], ["Spot", "Long"])

    def test_relievers_zeros_do_not_drag_the_rotation_cutoff_down(self):
        # five starters plus two spot starters (8 and 4 GS) and fifteen relievers who never started:
        # averaged over everyone the cutoff would be 6.9 GS and the 8-start spot man would be
        # "Rotation"; averaged over those who started it is 21.7
        staff = [_pitcher(i, f"S{i}", gs, 0, 500, 0, 0) for i, gs in enumerate((32, 30, 28, 26, 24), start=1)]
        staff += [_pitcher(6, "Spot8", 8, 0, 120, 0, 0), _pitcher(7, "Spot4", 4, 0, 90, 0, 0)]
        staff += [_pitcher(10 + i, f"R{i}", 0, 0, 60, 0, 0) for i in range(15)]
        c = mlbstats.classify_pitchers(staff)
        self.assertAlmostEqual(c["averages"]["gs"], 152 / 7)
        self.assertEqual([r["_name"] for r in c["roles"]["Rotation"]], ["S1", "S2", "S3", "S4", "S5"])
        self.assertNotIn("Spot8", [r["_name"] for r in c["roles"]["Rotation"]])

    def test_relievers_zeros_do_not_drag_the_closer_cutoff_down_either(self):
        # two real closers (40, 30 GF), three occasional finishers (6, 5, 4) and fifteen who never finished:
        # averaged over everyone the cutoff would be 4.25 and the occasional finishers would all be
        # "Closers"; averaged over those who finished it is 17
        staff = [_pitcher(1, "C1", 0, 40, 60, 0, 0), _pitcher(2, "C2", 0, 30, 60, 0, 0),
                 _pitcher(3, "F6", 0, 6, 60, 0, 0), _pitcher(4, "F5", 0, 5, 60, 0, 0), _pitcher(5, "F4", 0, 4, 60, 0, 0)]
        staff += [_pitcher(10 + i, f"N{i}", 0, 0, 60, 0, 0) for i in range(15)]
        c = mlbstats.classify_pitchers(staff)
        self.assertAlmostEqual(c["averages"]["gf"], 85 / 5)
        self.assertEqual([r["_name"] for r in c["roles"]["Closers"]], ["C1", "C2"])

    def test_a_lone_finisher_or_finishers_who_all_tie_still_count_as_closers(self):
        lone = [_pitcher(1, "Only", 0, 30, 70, 0, 0), _pitcher(2, "Other", 0, 0, 60, 0, 0)]
        self.assertEqual([r["_name"] for r in mlbstats.classify_pitchers(lone)["roles"]["Closers"]], ["Only"])
        tied = [_pitcher(i, f"T{i}", 0, 20, 60, 0, 0) for i in (1, 2, 3)]
        self.assertEqual(len(mlbstats.classify_pitchers(tied)["roles"]["Closers"]), 3)

    def test_nobody_finishing_means_no_closers(self):
        staff = [_pitcher(1, "A", 30, 0, 500, 0, 0), _pitcher(2, "B", 0, 0, 60, 0, 0)]
        c = mlbstats.classify_pitchers(staff)
        self.assertEqual(c["roles"]["Closers"], [])
        self.assertEqual(c["averages"]["gf"], 0.0)

    def test_a_lone_starter_or_starters_who_all_tie_still_make_the_rotation(self):
        lone = [_pitcher(1, "Only", 60, 0, 1500, 0, 0), _pitcher(2, "Reliever", 0, 5, 100, 0, 0)]
        self.assertEqual([r["_name"] for r in mlbstats.classify_pitchers(lone)["roles"]["Rotation"]], ["Only"])
        tied = [_pitcher(i, f"T{i}", 30, 0, 500, 0, 0) for i in (1, 2, 3)]
        self.assertEqual(len(mlbstats.classify_pitchers(tied)["roles"]["Rotation"]), 3)

    def test_a_starter_sharing_a_turn_joins_the_rotation(self):
        # the 1970 Orioles: three 40-start pitchers lift the starters' average to 27 (with a 3-start
        # long man), out of reach of the two who shared the fourth turn
        def p(pid, name, gs, games, outs):
            return dict(_pitcher(pid, name, gs, 0, outs, 0, 0), gamesPitched=games)
        staff = [p(1, "Palmer", 39, 39, 915), p(2, "Cuellar", 40, 40, 893), p(3, "McNally", 40, 40, 888),
                 p(4, "Phoebus", 21, 27, 405), p(5, "Hardin", 19, 36, 436), p(6, "Lopez", 3, 25, 182),
                 p(7, "Watt", 0, 53, 165)]
        c = mlbstats.classify_pitchers(staff)
        self.assertEqual(c["averages"]["max_gs"], 40)
        self.assertEqual(sorted(r["_name"] for r in c["roles"]["Rotation"]),
                         ["Cuellar", "Hardin", "McNally", "Palmer", "Phoebus"])     # 19 and 21 >= 40% of 40

    def test_a_pitcher_who_mostly_started_needs_30_percent_of_the_busiest_starters_starts(self):
        def p(pid, name, gs, games):
            return dict(_pitcher(pid, name, gs, 0, gs * 15, 0, 0), gamesPitched=games)
        staff = [p(1, "Ace", 33, 33), p(2, "Two", 32, 32), p(3, "Three", 31, 31),
                 p(4, "Fill-in", 11, 11),     # 33% of 33, no relief: in by the 2:1 rule
                 p(5, "Swing", 11, 20),       # 11 starts but 9 relief: not twice as many
                 p(6, "Call-up", 3, 3)]       # all starts, but only 9% of the busiest's
        rotation = {r["_name"] for r in mlbstats.classify_pitchers(staff)["roles"]["Rotation"]}
        self.assertEqual(rotation, {"Ace", "Two", "Three", "Fill-in"})

    def test_a_team_where_nobody_started_has_no_rotation(self):
        staff = [_pitcher(1, "A", 0, 3, 100, 0, 0), _pitcher(2, "B", 0, 0, 60, 0, 0)]
        self.assertEqual(mlbstats.classify_pitchers(staff)["roles"]["Rotation"], [])
        self.assertEqual(mlbstats.classify_pitchers(staff)["averages"]["gs"], 0.0)

    def test_every_pitcher_lands_in_exactly_one_role_busiest_first(self):
        roles = self._roles()
        everyone = [n for members in roles.values() for n in members]
        self.assertEqual(sorted(everyone), sorted(r["_name"] for r in self.STAFF))
        self.assertEqual(roles["Rotation"], ["Ace", "Swingman"])       # 600 outs before 300

    def test_a_team_with_no_pitchers_has_empty_roles(self):
        c = mlbstats.classify_pitchers([])
        self.assertEqual(c["averages"]["pitchers"], 0)
        self.assertEqual(c["roles"], {role: [] for role in mlbstats.PITCHER_ROLES})

    def test_outs_fall_back_to_innings_pitched(self):
        row = {"_id": 1, "inningsPitched": "12.1", "gamesStarted": 1}
        self.assertEqual(mlbstats.pitcher_workload(row), (1, 0, 37))

    def test_innings_are_written_in_thirds(self):
        self.assertEqual([mlbstats.outs_to_innings(o) for o in (0, 1, 2, 3, 37, 517)], ["0.0", "0.1", "0.2", "1.0", "12.1", "172.1"])


class PreventionReport(unittest.TestCase):
    RA9 = "{Pitching: R}*27/{Pitching: Outs}"

    def _report(self):
        rows = PitcherRoles.STAFF
        groups = {"Pitching": mlbstats.group_by_player(rows)}
        return mlbstats.prevention_report(rows, groups, {"R/9": self.RA9})

    def test_a_pitchers_stat_is_worked_out_from_his_own_figures(self):
        ace = next(p for r in self._report()["roles"] for p in r["pitchers"] if p["name"] == "Ace")
        self.assertAlmostEqual(ace["stats"]["R/9"], 60 * 27 / 600)
        self.assertEqual((ace["gs"], ace["gf"], ace["outs"], ace["r"], ace["er"], ace["ur"]), (20, 0, 600, 60, 55, 5))

    def test_a_groups_stat_is_its_total_runs_over_its_total_outs_not_an_average_of_averages(self):
        rotation = next(r for r in self._report()["roles"] if r["role"] == "Rotation")["total"]
        self.assertAlmostEqual(rotation["stats"]["R/9"], (60 + 60) * 27 / (600 + 300))        # 3.6
        self.assertNotAlmostEqual(rotation["stats"]["R/9"], (60 * 27 / 600 + 60 * 27 / 300) / 2)  # not (2.7 + 5.4) / 2
        self.assertEqual((rotation["count"], rotation["gs"], rotation["outs"]), (2, 30, 900))

    def test_the_four_groups_add_up_to_the_whole_staff(self):
        report = self._report()
        for key in ("gs", "gf", "outs", "r", "er", "ur", "count"):
            self.assertEqual(sum(r["total"][key] for r in report["roles"]), report["all"][key], key)
        self.assertAlmostEqual(report["all"]["stats"]["R/9"], 178 * 27 / 1500)

    def test_an_empty_group_has_no_pitchers_and_no_stat(self):
        report = mlbstats.prevention_report([], {"Pitching": {}}, {"R/9": self.RA9})
        self.assertEqual([r["total"]["count"] for r in report["roles"]], [0, 0, 0, 0])
        self.assertIsNone(report["all"]["stats"]["R/9"])

    def test_a_pitcher_with_no_outs_has_no_rate(self):
        rows = [_pitcher(1, "Wild", 0, 0, 0, 3, 3)]
        report = mlbstats.prevention_report(rows, {"Pitching": mlbstats.group_by_player(rows)}, {"R/9": self.RA9})
        self.assertIsNone(report["roles"][3]["pitchers"][0]["stats"]["R/9"])


class LeaguePooled(unittest.TestCase):
    RA9 = "{Pitching: R}*27/{Pitching: Outs}"

    def _fetch(self, total):
        calls = []
        def fake(path, params=None, ttl=None):
            calls.append((path, dict(params)))
            splits = [{"player": {"id": i}, "stat": {"runs": 10, "outs": 60}} for i in (1, 2, 3)]
            return {"stats": [{"splits": splits, "totalSplits": total}]}
        real, mlbstats.api_get = mlbstats.api_get, fake
        try:
            return mlbstats.get_league_pooled(2023, [self.RA9], ttl=None), calls
        finally:
            mlbstats.api_get = real

    def test_the_league_figure_is_all_runs_over_all_outs(self):
        result, calls = self._fetch(3)
        self.assertAlmostEqual(result[self.RA9], 30 * 27 / 180)
        self.assertEqual(len(calls), 1)
        path, params = calls[0]
        self.assertEqual((path, params["group"], params["season"], params["limit"]), ("/stats", "pitching", 2023, 5000))
        self.assertNotIn("teamId", params)

    def test_a_truncated_league_list_is_refused(self):
        with self.assertRaises(mlbstats.MLBStatsError):
            self._fetch(4)


PLAY_COLUMNS = ["gid", "inning", "top_bot", "gametype", "batteam", "event", "pa", "outs_pre",
                "br1_pre", "br2_pre", "br3_pre", "br1_post", "br2_post", "br3_post", "run1", "run2", "run3",
                "sb2", "sb3", "sbh", "cs2", "cs3", "csh", "single", "double", "fly", "batout1"]


def _play(event="", pre=("", "", ""), post=("", "", ""), runs=("", "", ""), pa=1, outs=0, gid="G1", inning="1",
          team="AAA", gametype="regular", **flags):
    """A plays.csv row (a list matching PLAY_COLUMNS); ``flags`` set columns such as single="1"."""
    row = dict.fromkeys(PLAY_COLUMNS, "")
    row.update(gid=gid, inning=inning, top_bot="0", gametype=gametype, batteam=team, event=event, pa=str(pa),
               outs_pre=str(outs), br1_pre=pre[0], br2_pre=pre[1], br3_pre=pre[2], br1_post=post[0], br2_post=post[1],
               br3_post=post[2], run1=runs[0], run2=runs[1], run3=runs[2], batout1="0")
    for flag in ("sb2", "sb3", "sbh", "cs2", "cs3", "csh", "single", "double", "fly"):
        row[flag] = "0"
    row.update(flags)
    return [row[c] for c in PLAY_COLUMNS]


def _counts(*plays, steal_opps=False):
    """The per-runner counts of some plays as {(runner, team): {key: n}}, only the non-zero ones.  Steal
    opportunities (a runner on first with second open when a batter steps in) are left out unless
    asked for, so the other tests can stay about the hit or fly out they describe."""
    parsed = retrosheet.parse_plays([PLAY_COLUMNS, *plays])
    out = {who: {k: n for k, n in zip(retrosheet.KEYS, values) if n and (steal_opps or k != "opp_sb")}
           for who, values in parsed.items()}
    return out


class RetrosheetRunning(unittest.TestCase):
    def test_extra_bases_on_singles_and_doubles(self):
        counts = _counts(
            _play("S8.1-3", pre=("A", "", ""), post=("B", "", "A"), single="1", outs=0),                # A: 1st to 3rd
            _play("S7.1-H", pre=("C", "", ""), post=("D", "", ""), runs=("C", "", ""), single="1", gid="G2"),  # C: 1st to home
            _play("S9.2-H", pre=("", "E", ""), post=("F", "", ""), runs=("", "E", ""), single="1", gid="G3"),  # E: 2nd to home
            _play("D8.1-H", pre=("G", "", ""), post=("", "H", ""), runs=("G", "", ""), double="1", gid="G4"),  # G: 1st to home on a double
        )
        self.assertEqual(counts[("A", "AAA")], {"s13": 1, "opp_xb": 1})
        self.assertEqual(counts[("C", "AAA")], {"s1h": 1, "opp_xb": 1})
        self.assertEqual(counts[("E", "AAA")], {"s2h": 1, "opp_xb": 1})
        self.assertEqual(counts[("G", "AAA")], {"d1h": 1, "opp_xb": 1})

    def test_no_extra_base_where_none_can_be_taken(self):
        counts = _counts(
            _play("S8.3-H", pre=("", "", "A"), post=("B", "", ""), runs=("", "", "A"), single="1"),             # third: always scores
            _play("D7.2-H", pre=("", "C", ""), post=("", "D", ""), runs=("", "C", ""), double="1", gid="G2"),   # second on a double
        )
        self.assertNotIn(("A", "AAA"), [(k[0], k[1]) for k, v in counts.items() if v.get("opp_xb")])
        self.assertFalse(any(v.get("opp_xb") for v in counts.values()))

    def test_a_runner_who_takes_only_the_standard_base_or_is_out_gets_an_opportunity_and_nothing_else(self):
        counts = _counts(
            _play("S8.1-2", pre=("A", "", ""), post=("B", "A", ""), single="1"),
            _play("S8.1X3(85)", pre=("C", "", ""), post=("D", "", ""), single="1", gid="G2", outs=0),
        )
        self.assertEqual(counts[("A", "AAA")], {"opp_xb": 1})
        self.assertEqual(counts[("C", "AAA")], {"opp_xb": 1})

    def test_an_advance_marked_as_an_error_is_not_an_extra_base(self):
        counts = _counts(_play("S8.1-3(E8)", pre=("A", "", ""), post=("B", "", "A"), single="1"))
        self.assertEqual(counts[("A", "AAA")], {"opp_xb": 1})
        counts = _counts(_play("S8.1-3;B-2(E8)", pre=("A", "", ""), post=("B", "", "A"), single="1"))  # the batter's error, not his
        self.assertEqual(counts[("A", "AAA")], {"opp_xb": 1, "s13": 1})

    def test_tag_ups_on_fly_outs(self):
        counts = _counts(
            _play("8/SF/F.3-H", pre=("", "", "A"), post=("", "", ""), runs=("", "", "A"), fly="1", batout1="8"),
            _play("7/F.2-3", pre=("", "B", ""), post=("", "", "B"), fly="1", batout1="7", gid="G2"),
            _play("9/F.1-2", pre=("C", "", ""), post=("", "C", ""), fly="1", batout1="9", gid="G3"),
            _play("8/F.2-H", pre=("", "D", ""), post=("", "", ""), runs=("", "D", ""), fly="1", batout1="8", gid="G4"),
        )
        self.assertEqual(counts[("A", "AAA")], {"f3h": 1, "opp_tu": 1})
        self.assertEqual(counts[("B", "AAA")], {"f23": 1, "opp_tu": 1})
        self.assertEqual(counts[("C", "AAA")], {"f12": 1, "opp_tu": 1})
        self.assertEqual(counts[("D", "AAA")], {"f2h": 1, "opp_tu": 1})

    def test_a_home_run_or_a_fly_ball_hit_is_not_a_fly_out(self):
        # batout1 is "0" when the batter wasn't out: a fly-ball home run, single or error does not count
        counts = _counts(
            _play("HR/F9.1-H", pre=("A", "", ""), post=("", "", ""), runs=("A", "", ""), fly="1", batout1="0"),
            _play("S7/F.1-2", pre=("B", "", ""), post=("C", "B", ""), fly="1", single="1", batout1="0", gid="G2"),
            _play("E7/F.1-2", pre=("D", "", ""), post=("E", "D", ""), fly="1", batout1="0", gid="G3"),
        )
        self.assertFalse(any(v.get("f1h") or v.get("f12") or v.get("opp_tu") for v in counts.values()))

    def test_a_fly_out_with_two_out_or_a_blocked_base_is_no_tag_up_opportunity(self):
        counts = _counts(
            _play("8/F", pre=("", "", "A"), post=("", "", "A"), fly="1", batout1="8", outs=2),               # 3rd out: nothing to try
            _play("8/F", pre=("", "B", "C"), post=("", "B", "C"), fly="1", batout1="8", gid="G2"),           # 2nd is blocked by 3rd
        )
        self.assertNotIn("opp_tu", counts.get(("A", "AAA"), {}))
        self.assertNotIn("opp_tu", counts.get(("B", "AAA"), {}))
        self.assertEqual(counts[("C", "AAA")], {"opp_tu": 1})

    def test_stolen_bases_and_caught_stealing_go_to_the_runner_on_the_base_stolen_from(self):
        counts = _counts(
            _play("SB2", pre=("A", "", ""), post=("", "A", ""), sb2="1", pa=0),
            _play("SB3;SB2", pre=("B", "C", ""), post=("", "B", "C"), sb2="1", sb3="1", pa=0, gid="G2"),
            _play("CS2(26)", pre=("D", "", ""), post=("", "", ""), cs2="1", pa=0, gid="G3", outs=0),
            _play("SBH", pre=("", "", "E"), post=("", "", ""), sbh="1", pa=0, gid="G4"),
        )
        self.assertEqual(counts[("A", "AAA")]["sb"], 1)
        self.assertEqual((counts[("B", "AAA")]["sb"], counts[("C", "AAA")]["sb"]), (1, 1))
        self.assertEqual(counts[("D", "AAA")]["cs"], 1)
        self.assertEqual(counts[("E", "AAA")]["sb"], 1)

    def test_steal_opportunities_are_counted_once_per_plate_appearance_from_where_it_started(self):
        # A is on first with second open when the batter steps in; he steals, then the batter singles:
        # one opportunity (the steal chance), not two -- and once on second with third open, none until the next PA
        counts = _counts(
            _play("SB2", pre=("A", "", ""), post=("", "A", ""), sb2="1", pa=0),
            _play("S8", pre=("", "A", ""), post=("B", "A", ""), single="1", pa=1),
            _play("K", pre=("B", "A", ""), post=("B", "A", ""), pa=1, outs=0),        # next PA: B on first is blocked; A on second is open
            steal_opps=True,
        )
        self.assertEqual(counts[("A", "AAA")]["opp_sb"], 2)   # first PA (1st->2nd), next PA (2nd->3rd)
        self.assertNotIn("opp_sb", counts.get(("B", "AAA"), {}))

    def test_a_runner_on_third_has_a_steal_of_home_chance_each_plate_appearance(self):
        # home is always open, so every plate appearance that starts with a runner on third is a chance
        counts = _counts(
            _play("K", pre=("", "", "A"), post=("", "", "A"), pa=1, outs=0),
            _play("W", pre=("", "", "A"), post=("B", "", "A"), pa=1, outs=1),
            _play("SBH", pre=("B", "", "A"), post=("", "B", ""), sbh="1", pa=0),          # first event of a third PA
            _play("K", pre=("", "B", ""), post=("", "B", ""), pa=1, outs=1),               # the same PA: no second chance
            steal_opps=True,
        )
        self.assertEqual(counts[("A", "AAA")]["opp_sb"], 3)   # a chance in each of the three plate appearances
        self.assertEqual(counts[("A", "AAA")]["sb"], 1)       # the steal of home counts as a stolen base too

    def test_a_new_inning_starts_a_new_plate_appearance(self):
        counts = _counts(
            _play("CS2", pre=("A", "", ""), post=("", "", ""), cs2="1", pa=0, outs=2, inning="1"),
            _play("K", pre=("", "", ""), post=("", "", ""), pa=1, inning="2"),
            steal_opps=True,
        )
        self.assertEqual(counts[("A", "AAA")], {"opp_sb": 1, "cs": 1})

    def test_only_regular_season_games_count(self):
        counts = _counts(_play("SB2", pre=("A", "", ""), post=("", "A", ""), sb2="1", pa=0, gametype="worldseries"))
        self.assertEqual(counts, {})

    def test_the_team_is_the_batting_team(self):
        counts = _counts(_play("SB2", pre=("A", "", ""), post=("", "A", ""), sb2="1", pa=0, team="BBB"))
        self.assertIn(("A", "BBB"), counts)


class RetrosheetBattedIn(unittest.TestCase):
    COLUMNS = ["gametype", "batteam", "batter", "run_b", "run1", "run2", "run3", "rbi_b", "rbi1", "rbi2", "rbi3"]

    def _play(self, batter, runs=("", "", "", ""), rbi=("0", "0", "0", "0"), team="AAA", gametype="regular"):
        return [gametype, team, batter, *runs, *rbi]

    def test_runs_are_credited_to_the_batter_by_runner_and_starting_base(self):
        parsed = retrosheet.parse_batted_in([self.COLUMNS,
            self._play("B", runs=("", "", "R", "S"), rbi=("0", "0", "1", "1")),   # single: R from 2nd, S from 3rd
            self._play("B", runs=("B", "R", "", ""), rbi=("1", "1", "0", "0")),   # home run with R on first
            self._play("B", runs=("", "", "", "S"), rbi=("0", "0", "0", "0")),    # S scores on an error: no RBI
            self._play("B", runs=("", "", "", "S"), rbi=("0", "0", "0", "1"), gametype="worldseries"),
            self._play("C", runs=("", "", "", "B"), rbi=("0", "0", "0", "1"), team="AAA"),
        ])
        self.assertEqual(parsed, {"AAA": {"B": {"R": [0, 1, 1, 0], "S": [0, 0, 0, 1], "B": [1, 0, 0, 0]},
                                          "C": {"B": [0, 0, 0, 1]}}})


class SaveSituations(unittest.TestCase):
    def test_mlbs_three_ways_into_a_save_situation(self):
        sit = retrosheet.save_situation
        self.assertTrue(sit(3, 0, 8, 2))            # three-run lead, an inning or more to pitch
        self.assertTrue(sit(3, 0, 9, 0))            # ... from the start of the 9th
        self.assertFalse(sit(3, 0, 9, 1))           # 9th, one out, nobody on: not an inning left to pitch
        self.assertTrue(sit(3, 1, 9, 2))            # ... but the tying run on deck
        self.assertTrue(sit(5, 3, 7, 0))            # tying run at bat (bases loaded, up five)
        self.assertFalse(sit(5, 2, 7, 0))
        self.assertTrue(sit(9, 0, 6, 0, recorded=9))  # three innings with the lead
        self.assertFalse(sit(9, 0, 6, 0, recorded=8))
        self.assertFalse(sit(0, 3, 8, 0))           # tied: nothing to save

    def _staff(self, winner="st"):
        """A made-up game: the starter, a setup man up two in the 8th, a closer up six with two on in the 9th."""
        header = ["gid", "id", "team", "p_seq", "stattype", "p_ipouts", "p_gs", "p_gf", "save", "wp", "lp", "win", "gametype"]
        lines = [("st", "1", "21", "1", "", ""), ("cl", "3", "3", "", "1", "1"), ("su", "2", "3", "", "", "")]
        pitching = [header] + [["G1", pid, "AAA", seq, "value", outs, gs, gf, sv, "1" if pid == winner else "", "", "1", "regular"]
                               for pid, seq, outs, gs, gf, sv in lines]
        plays = [["gid", "gametype", "vis_home", "pitcher", "score_v", "score_h", "br1_pre", "br2_pre", "br3_pre",
                  "inning", "outs_pre", "runs"],
                 ["G1", "regular", "1", "st", "0", "0", "", "", "", "1", "0", "0"],
                 ["G1", "regular", "1", "su", "2", "0", "", "", "", "8", "0", "0"],
                 ["G1", "regular", "1", "cl", "6", "0", "x", "y", "", "9", "0", "0"]]
        game = retrosheet.parse_staff(pitching, plays)["AAA"][0]
        return game, [dict(zip(retrosheet.APPEARANCE_FIELDS, a)) for a in game["pitchers"]]

    def test_a_save_by_todays_rule(self):
        save = lambda lead, runners, outs, **kw: retrosheet.current_rule_save(
            lead, runners, outs, kw.get("finished", True), kw.get("team_won", True), kw.get("won_it", False),
            kw.get("kept_lead", True))
        self.assertTrue(save(3, 0, 3))                 # up three, an inning
        self.assertFalse(save(3, 0, 2))                # up three but not an inning, nobody on
        self.assertTrue(save(4, 2, 1))                 # tying run on deck
        self.assertFalse(save(8, 0, 3))                # Eddie Watt, 22 Sep 1970: a save then, not now
        self.assertTrue(save(8, 0, 9))                 # three innings
        self.assertFalse(save(2, 0, 3, finished=False))
        self.assertFalse(save(2, 0, 3, won_it=True))
        self.assertFalse(save(2, 0, 3, kept_lead=False))

    def test_appearances_in_order_with_the_situation_each_came_in_to(self):
        game, apps = self._staff()
        self.assertEqual([(a["pitcher"], a["save_sit"], a["lead"]) for a in apps],
                         [("st", 0, 0), ("su", 1, 2), ("cl", 0, 6)])   # up six with two on: the tying run isn't on deck
        self.assertEqual((game["won"], apps[2]["sv"], apps[2]["gf"]), (1, 1, 1))
        self.assertEqual([(a["hold"], a["blown"]) for a in apps], [(0, 0), (1, 0), (0, 0)])  # the setup man's hold

    def test_a_blown_save_is_a_blown_hold_when_he_handed_off_and_the_lead_came_back(self):
        split = lambda **a: (mlbstats._split_blown(a), (a["blown_holds"], a["blown_saves"]))[1]
        self.assertEqual(split(blown=1, gf=0, regained=1), (1, 0))   # handed off, lead won back: blown hold
        self.assertEqual(split(blown=1, gf=0, regained=0), (0, 1))   # never won back: blown save
        self.assertEqual(split(blown=1, gf=1, regained=1), (0, 1))   # he finished the game: blown save
        self.assertEqual(split(blown=0, gf=0, regained=1), (0, 0))

    def test_whether_the_lead_came_back_after_he_left(self):
        header = ["gid", "id", "team", "p_seq", "stattype", "p_ipouts", "p_gs", "p_gf", "save", "wp", "lp", "win", "gametype"]
        pitching = [header, ["G1", "st", "AAA", "1", "value", "21", "1", "", "", "", "", "1", "regular"],
                    ["G1", "su", "AAA", "2", "value", "2", "", "", "", "", "", "1", "regular"],
                    ["G1", "cl", "AAA", "3", "value", "4", "", "1", "", "1", "", "1", "regular"]]
        cols = ["gid", "gametype", "vis_home", "pitcher", "score_v", "score_h", "br1_pre", "br2_pre", "br3_pre",
                "inning", "outs_pre", "runs"]
        plays = [cols, ["G1", "regular", "1", "st", "0", "0", "", "", "", "1", "0", "0"],
                 ["G1", "regular", "1", "su", "1", "0", "", "", "", "8", "0", "2"],   # up 1 in the 8th: two score
                 ["G1", "regular", "1", "cl", "1", "2", "", "", "", "8", "2", "0"],
                 ["G1", "regular", "0", "xx", "1", "2", "", "", "", "9", "0", "2"],   # AAA bats in the 9th: up 3-2
                 ["G1", "regular", "1", "cl", "3", "2", "", "", "", "9", "0", "0"]]
        apps = [dict(zip(retrosheet.APPEARANCE_FIELDS, a)) for a in retrosheet.parse_staff(pitching, plays)["AAA"][0]["pitchers"]]
        self.assertEqual([(a["pitcher"], a["blown"], a["regained"]) for a in apps],
                         [("st", 0, 1), ("su", 1, 1), ("cl", 0, 0)])  # nothing came after the closer

    def test_the_winning_pitcher_gets_no_hold(self):
        _game, apps = self._staff(winner="su")
        self.assertEqual(apps[1]["hold"], 0)


class RetrosheetSeasons(unittest.TestCase):
    def _bundle(self, season=1999, plays=None):
        def text(header, rows):
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(header)
            writer.writerows(rows)
            return out.getvalue()
        plays = [_play("SB2", pre=("aaaa001", "", ""), post=("", "aaaa001", ""), sb2="1", pa=0)] if plays is None else plays
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr(f"{season}plays.csv", text(PLAY_COLUMNS, plays))
            z.writestr(f"{season}allplayers.csv", text(["id", "last", "first"], [["aaaa001", "Ace", "Alan"]]))
            z.writestr(f"{season}gameinfo.csv", text(
                ["gid", "visteam", "hometeam", "gametype", "vruns", "hruns"],
                [["G1", "AAA", "BBB", "regular", "5", "3"], ["G2", "BBB", "AAA", "regular", "2", "2"],
                 ["G3", "AAA", "BBB", "worldseries", "9", "0"], ["G4", "AAA", "BBB", "regular", "", ""]]))
        return buffer.getvalue()

    def test_a_bundle_becomes_a_small_summary(self):
        summary = retrosheet.build_season(1999, self._bundle())
        self.assertEqual(summary["runners"][0]["name"], "Alan Ace")
        self.assertEqual((summary["runners"][0]["sb"], summary["runners"][0]["team"]), (1, "AAA"))
        # AAA won G1 5-3 and tied G2 2-2 (a tie adds the runs but no win or loss); the post-season game
        # and the one without a score are not results at all
        self.assertEqual(summary["teams"]["AAA"], {"w": 1, "l": 0, "rs": 7, "ra": 5})
        self.assertEqual(summary["teams"]["BBB"], {"w": 0, "l": 1, "rs": 5, "ra": 7})

    def test_a_season_without_regular_season_plays_is_refused(self):
        with self.assertRaises(retrosheet.RetrosheetError):
            retrosheet.build_season(1999, self._bundle(plays=[_play("SB2", gametype="worldseries")]))

    def test_a_bundle_that_is_not_a_zip_is_refused(self):
        with self.assertRaises(retrosheet.RetrosheetError):
            retrosheet.build_season(1999, b"not a zip")

    def test_a_season_is_downloaded_once_then_read_from_the_cache(self):
        calls = []
        def download(season):
            calls.append(season)
            return self._bundle(season)
        with tempfile.TemporaryDirectory() as cache:
            self.assertFalse(retrosheet.is_cached(cache, 1999))
            first = retrosheet.load_season(1999, cache, download)
            self.assertTrue(retrosheet.is_cached(cache, 1999))
            second = retrosheet.load_season(1999, cache, download)
        self.assertEqual(calls, [1999])
        self.assertEqual(first, second)

    def test_a_cached_summary_from_an_older_version_is_rebuilt(self):
        calls = []
        def download(season):
            calls.append(season)
            return self._bundle(season)
        with tempfile.TemporaryDirectory() as cache:
            path = retrosheet.summary_path(cache, 1999)
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"version": 0, "runners": []}, fh)
            summary = retrosheet.load_season(1999, cache, download)
        self.assertEqual(calls, [1999])
        self.assertEqual(summary["version"], retrosheet.SUMMARY_VERSION)

    def test_teams_are_matched_by_their_results(self):
        retro = {"NYA": {"w": 99, "l": 63, "rs": 800, "ra": 700}, "BOS": {"w": 99, "l": 63, "rs": 750, "ra": 650},
                 "OAK": {"w": 60, "l": 102, "rs": 600, "ra": 800}}
        api = {147: {"w": 99, "l": 63, "rs": 800, "ra": 700}, 111: {"w": 99, "l": 63, "rs": 750, "ra": 650},
               133: {"w": 60, "l": 102, "rs": 600, "ra": 800}}
        self.assertEqual(retrosheet.match_teams(retro, api), {147: "NYA", 111: "BOS", 133: "OAK"})

    def test_teams_whose_results_differ_slightly_are_still_matched_but_not_wildly(self):
        retro = {"AAA": {"w": 70, "l": 80, "rs": 500, "ra": 600}, "ZZZ": {"w": 100, "l": 50, "rs": 900, "ra": 500}}
        api = {1: {"w": 71, "l": 79, "rs": 503, "ra": 598}, 2: {"w": 40, "l": 110, "rs": 300, "ra": 900}}
        self.assertEqual(retrosheet.match_teams(retro, api), {1: "AAA"})       # #2 resembles nothing
        self.assertEqual(retrosheet.match_teams(retro, api, close=False), {})  # exact only

    def test_teams_whose_results_disagree_are_matched_by_their_players(self):
        def keys(*names):
            return [{tuple(n.split())} for n in names]
        retro = {"HOM": {("j", "gibson"), ("b", "leonard"), ("v", "harris"), ("r", "brown")},
                 "NW2": {("m", "dihigo"), ("w", "wells"), ("r", "dandridge"), ("b", "leonard")},
                 "PTC": {("c", "bell"), ("x", "one")}}
        api = {1513: keys("j gibson", "b leonard", "v harris", "r brown", "s one"),   # the Grays
               1514: keys("w wells", "r dandridge", "l day"),                      # only two in common
               1536: keys("c bell", "x one", "y two", "z three")}                  # two isn't three
        self.assertEqual(retrosheet.match_rosters(retro, api), {1513: "HOM"})
        self.assertEqual(retrosheet.match_rosters(retro, api, minimum=2), {1513: "HOM", 1514: "NW2", 1536: "PTC"})


class RetrosheetView(unittest.TestCase):
    ROW = {"sb": 4, "s13": 2, "s1h": 1, "s2h": 3, "d1h": 1, "f12": 1, "f13": 0, "f1h": 0, "f23": 2, "f2h": 1, "f3h": 3,
           "opp_sb": 50, "opp_xb": 20, "opp_tu": 10}
    FILES = {"extra-bases-player.json": ("Extra Bases", 8),        # 2 + 2*1 + 3 + 1
             "tag-ups-player.json": ("Tag Ups", 8),                # 1 + 2*0 + 3*0 + 2 + 2*1 + 3
             "bases-gained-player.json": ("Bases Gained", 20),     # SB 4 + 8 + 8
             "opportunities-player.json": ("Opportunities", 80)}   # 50 + 20 + 10

    def _definition(self, fname):
        with open(os.path.join(HERE, fname), encoding="utf-8") as fh:
            entries = json.load(fh)
        self.assertEqual(len(entries), 1, fname)
        return entries[0]

    def test_the_view_is_registered_for_custom_stats_but_not_offered_on_the_stats_tab(self):
        self.assertIn(mlbstats.RETRO_VIEW, mlbstats.VIEW_SPECS)
        self.assertNotIn(mlbstats.RETRO_VIEW, mlbstats.PLAYER_VIEWS)
        self.assertEqual(mlbstats.VIEW_SPECS[mlbstats.RETRO_VIEW][0], "retrosheet")
        self.assertIn(f"{mlbstats.RETRO_VIEW}: SB", mlbstats.REGRESSION_VARIABLE_INDEX)
        self.assertEqual(len([v for v in mlbstats.REGRESSION_VARIABLE_INDEX if v.startswith(mlbstats.RETRO_VIEW)]),
                         len(retrosheet.RUNNING_VARIABLES))

    def test_each_file_is_one_player_level_definition_using_known_variables(self):
        for fname, (name, _value) in self.FILES.items():
            d = self._definition(fname)
            self.assertEqual((d["level"], d["name"]), ("player", name), fname)
            self.assertLessEqual(formula.formula_variables(d["formula"]), set(mlbstats.REGRESSION_VARIABLE_INDEX), fname)

    def test_each_formula_gives_its_figure_and_bases_gained_is_the_sum_of_the_parts(self):
        groups = {mlbstats.RETRO_VIEW: mlbstats.group_by_player([dict(self.ROW, _id=1)])}
        got = {}
        for fname, (name, expected) in self.FILES.items():
            got[name] = mlbstats.production_values(self._definition(fname)["formula"], groups, [1])[1]
            self.assertEqual(got[name], expected, name)
        self.assertEqual(got["Bases Gained"], self.ROW["sb"] + got["Extra Bases"] + got["Tag Ups"])

    def test_opportunities_add_up(self):
        self.assertEqual(retrosheet.opportunities(self.ROW), 80)

    def test_the_opportunities_file_agrees_with_the_built_in_count(self):
        groups = {mlbstats.RETRO_VIEW: mlbstats.group_by_player([dict(self.ROW, _id=1)])}
        formula_text = self._definition("opportunities-player.json")["formula"]
        self.assertEqual(mlbstats.production_values(formula_text, groups, [1])[1], retrosheet.opportunities(self.ROW))

    def test_runner_names_are_matched_to_mlb_players_by_first_initial_and_last_name(self):
        key = mlbstats._name_key
        self.assertEqual(key("Ronald", "Acuna"), key("Ronald", "Acu\u00f1a Jr."))
        self.assertEqual(key("J. T.", "Realmuto"), key("J.T.", "Realmuto"))
        self.assertNotEqual(key("Anthony", "Rizzo"), key("Anthony", "Volpe"))

    def test_a_runners_rows_carry_his_mlb_id_when_the_name_matches_and_his_retrosheet_id_when_not(self):
        summary = {"teams": {"NYA": {"w": 1, "l": 1, "rs": 1, "ra": 1}}, "runners": [
            {"id": "judga001", "name": "Aaron Judge", "first": "Aaron", "last": "Judge", "team": "NYA", "sb": 3},
            {"id": "nobod001", "name": "No Body", "first": "No", "last": "Body", "team": "NYA", "sb": 1},
            {"id": "other001", "name": "Other Guy", "first": "Other", "last": "Guy", "team": "BOS", "sb": 9}]}
        real = (mlbstats._retro_summary, mlbstats.retro_team_codes, mlbstats.get_player_stats, mlbstats.retro_ids)
        mlbstats.retro_ids = lambda pids, ttl=None: {}  # no cross-references: matched by name
        mlbstats._retro_summary = lambda season: summary
        mlbstats.retro_team_codes = lambda season, s, ttl=None: {147: "NYA"}
        mlbstats.get_player_stats = lambda team_id, season, group, ttl=None, stat_type="season": (
            [{"_id": 592450, "_name": "Aaron Judge"}] if group == "hitting" else [])
        try:
            rows = mlbstats.retro_team_rows(147, 2023)
            league = mlbstats.league_view_rows(2023, mlbstats.RETRO_VIEW, None)
        finally:
            mlbstats._retro_summary, mlbstats.retro_team_codes, mlbstats.get_player_stats, mlbstats.retro_ids = real
        self.assertEqual([(r["_id"], r["_name"]) for r in rows], [(592450, "Aaron Judge"), ("nobod001", "No Body")])
        self.assertEqual(len(league), 3)                                   # everyone, every team

    def _batted_in(self, roster, xrefs, pid, name):
        """get_batted_in over a made-up 1955 Pittsburgh, MLB's roster and cross-references given."""
        summary = {"batted_in": {"PIT": {
            "freeg101": {"clemr101": [0, 1, 0, 1]},               # George Freese: 2
            "freeg102": {"clemr101": [0, 0, 1, 0], "freeg102": [3, 0, 0, 0]},  # Gene Freese: 4, 3 his own
            "reesp101": {"soucb101": [0, 0, 0, 5]}}},             # Pee Wee Reese drove in Bud Souchock
            "names": {"freeg101": ["George", "Freese"], "freeg102": ["Gene", "Freese"], "clemr101": ["Roberto", "Clemente"],
                      "reesp101": ["Pee Wee", "Reese"], "soucb101": ["Bud", "Souchock"]}}
        real = (mlbstats.retrosheet.load_rbi_season, mlbstats._retro_summary, mlbstats.retro_team_codes,
                mlbstats.get_player_stats, mlbstats.retro_ids)
        mlbstats.retrosheet.load_rbi_season = lambda season, cache: summary
        mlbstats._retro_summary = lambda season: {}
        mlbstats.retro_team_codes = lambda season, s, ttl=None: {134: "PIT"}
        mlbstats.get_player_stats = lambda team_id, season, group, ttl=None, stat_type="season": roster
        mlbstats.retro_ids = lambda pids, ttl=None: {p: xrefs[p] for p in pids if p in xrefs}
        try:
            return mlbstats.get_batted_in(134, 1955, pid, name)
        finally:
            (mlbstats.retrosheet.load_rbi_season, mlbstats._retro_summary, mlbstats.retro_team_codes,
             mlbstats.get_player_stats, mlbstats.retro_ids) = real

    ROSTER = [{"_id": 1, "_name": "George Freese"}, {"_id": 2, "_name": "Gene Freese"},
              {"_id": 3, "_name": "Roberto Clemente"}, {"_id": 4, "_name": "Pee Wee Reese"},
              {"_id": 5, "_name": "Steve Souchock"}]

    def test_batted_in_finds_brothers_and_nicknames_by_mlbs_retrosheet_ids(self):
        xrefs = {1: "freeg101", 2: "freeg102", 3: "clemr101", 4: "reesp101", 5: "soucb101"}
        george = self._batted_in(self.ROSTER, xrefs, 1, "George Freese")
        gene = self._batted_in(self.ROSTER, xrefs, 2, "Gene Freese")
        reese = self._batted_in(self.ROSTER, xrefs, 4, "Pee Wee Reese")
        self.assertEqual((george["rbi"], gene["rbi"]), (2, 4))
        self.assertEqual([(r["name"], r["total"], r["self"]) for r in gene["rows"]],
                         [("Gene Freese", 3, True), ("Roberto Clemente", 1, False)])
        self.assertEqual([r["name"] for r in reese["rows"]], ["Steve Souchock"])  # MLB's name, not "Bud"

    def test_batted_in_without_cross_references_matches_a_two_word_first_name_but_not_a_brother(self):
        reese = self._batted_in(self.ROSTER, {}, 4, "Pee Wee Reese")
        self.assertEqual((reese["found"], reese["rbi"]), (True, 5))
        self.assertEqual(reese["rows"][0]["name"], "Bud Souchock")    # a nickname needs the cross-reference
        george = self._batted_in(self.ROSTER, {}, 1, "George Freese")
        self.assertFalse(george["found"])                              # G. Freese could be either brother


class RetrosheetCustomViews(unittest.TestCase):
    """The Relief, Batted In and Safe/Out (Retrosheet) variables, and the tab-derived custom stat files."""

    @staticmethod
    def _app(pitcher, outs=3, gs=0, gf=0, sv=0, sit=0, hold=0, blown=0, regained=0, w=0, l=0, sv_now=0):
        return [pitcher, outs, gs, gf, sv, sit, 1, hold, blown, regained, w, l, sv_now]

    def test_relief_counts_split_starts_from_relief_and_blown_holds_from_blown_saves(self):
        a = self._app
        games = [
            {"won": 1, "pitchers": [a("ace", 18, gs=1, w=1), a("setup", 3, sit=1, hold=1), a("closer", 3, gf=1, sv=1, sit=1)]},
            {"won": 0, "pitchers": [a("ace", 15, gs=1), a("setup", 2, sit=1, blown=1, regained=1), a("closer", 3, gf=1, l=1)]},
            {"won": 1, "pitchers": [a("other", 12, gs=1), a("setup", 3, sit=1, blown=1, gf=1, w=1)]},
        ]
        c = retrosheet.relief_counts(games)
        self.assertEqual((c["ace"]["gs"], c["ace"]["st_outs"], c["ace"]["st_w"], c["ace"]["st_l"]), (2, 33, 1, 0))
        self.assertEqual((c["ace"]["st_nd_w"], c["ace"]["st_nd_l"], c["ace"]["st_tw"], c["ace"]["st_tl"]), (0, 1, 1, 1))
        self.assertEqual(c["ace"]["rel"], 0)
        s = c["setup"]
        self.assertEqual((s["rel"], s["rel_outs"], s["sit"], s["hold"], s["sit_hold"], s["rel_w"]), (3, 8, 3, 1, 1, 1))
        self.assertEqual((s["blown"], s["blown_holds"], s["blown_saves"]), (2, 1, 1))  # the one he finished stays a blown save
        self.assertEqual((c["closer"]["sv"], c["closer"]["sit_sv"], c["closer"]["rel_gf"], c["closer"]["rel_l"]), (1, 1, 2, 1))
        self.assertEqual(set(c["ace"]), set(retrosheet.RELIEF_KEYS))

    def test_batted_in_counts_add_up_by_base(self):
        c = retrosheet.batted_in_counts({"bat": {"bat": [5, 0, 0, 0], "r1": [0, 2, 1, 3], "r2": [0, 0, 0, 1]}})["bat"]
        self.assertEqual(c, {"bi_total": 12, "bi_self": 5, "bi_1": 2, "bi_2": 1, "bi_3": 4, "bi_runners": 3})

    def test_the_views_are_formula_variables_but_not_stats_tab_views(self):
        for view, variables in ((mlbstats.RELIEF_VIEW, retrosheet.RELIEF_VARIABLES),
                                (mlbstats.BATTED_IN_VIEW, retrosheet.BATTED_IN_VARIABLES)):
            self.assertNotIn(view, mlbstats.PLAYER_VIEWS)
            self.assertEqual(len([v for v in mlbstats.REGRESSION_VARIABLE_INDEX if v.startswith(view + ":")]), len(variables))
        team = {f"{v}: {l}" for v, _g, l, _k in mlbstats.DEPENDENT_VARIABLES}
        self.assertIn("Safe/Out (Retrosheet): Scored", team)
        self.assertIn("Safe/Out Allowed (Retrosheet): Outs", team)
        self.assertIn("Relief (Retrosheet): Save Situations", team)
        self.assertIn("Pitching: HLD", mlbstats.REGRESSION_VARIABLE_INDEX)

    def test_team_lookup_fetches_retrosheet_only_for_the_variables_asked_for(self):
        asked = []
        real = mlbstats.get_team_totals
        mlbstats.get_team_totals = lambda team_id, season, group, ttl=None: asked.append(group) or (
            {"runs": 7, "outs": 27} if group.startswith("retrosheet") else {"runs": 5})
        try:
            got = mlbstats.team_totals_lookup(1, 2023, None, {"Safe/Out (Retrosheet): Scored"})
        finally:
            mlbstats.get_team_totals = real
        self.assertEqual(sorted(g for g in asked if g.startswith("retrosheet")), ["retrosheet-safe-out"])
        self.assertEqual((got["Safe/Out (Retrosheet): Scored"], got["Batting: Runs"]), (7, 5))
        self.assertIsNone(got["Relief (Retrosheet): Starts"])

    def test_every_tab_derived_file_uses_known_variables_at_its_level(self):
        import ballsheet
        team = {f"{v}: {l}" for v, _g, l, _k in mlbstats.DEPENDENT_VARIABLES}
        for fname in ballsheet.TAB_DERIVED_FILES:
            with open(os.path.join(HERE, fname), encoding="utf-8") as fh:
                for d in json.load(fh):
                    known = set(mlbstats.REGRESSION_VARIABLE_INDEX) if d["level"] == "player" else team
                    self.assertLessEqual(formula.formula_variables(d["formula"]), known, (fname, d["name"]))

    def test_safe_out_formulas_agree_with_the_tabs_rates(self):
        counts = {"outs": 4330, "reach1": 2176, "reach2": 1388, "reach3": 1004, "runs": 746, "placed": 31}
        rates = mlbstats.safe_out_rates(counts)
        values = {f"Safe/Out (Retrosheet): {label}": counts[key] for label, key in retrosheet.SAFE_OUT_VARIABLES}
        with open(os.path.join(HERE, "safe-out-team.json"), encoding="utf-8") as fh:
            defs = {d["name"]: d["formula"] for d in json.load(fh)}
        for name, key in (("Reached 1st per Inning", "on_base_inn"), ("Scored per Inning", "scored_inn"),
                          ("On Base to Outs", "on_base_outs"), ("Scored to On Base", "scored_on_base")):
            self.assertAlmostEqual(formula.evaluate_formula(defs[name], values), rates[key], places=9, msg=name)


class UsualLineup(unittest.TestCase):
    @staticmethod
    def _bat(pid, name):
        return {"_id": pid, "_name": name, "plateAppearances": 500}

    def test_each_slot_goes_to_the_batter_with_the_most_games_there(self):
        rows = [self._bat(1, "Leadoff"), self._bat(2, "Two"), self._bat(3, "Three")]
        order = {1: {1: {"games": 120, "pa": 500}, 2: {"games": 10, "pa": 40}},
                 2: {2: {"games": 100, "pa": 420}},
                 3: {3: {"games": 90, "pa": 380}, 2: {"games": 5, "pa": 20}}}
        chosen = mlbstats.assign_lineup(rows, order)
        self.assertEqual({slot: r["_name"] for slot, r in chosen.items()}, {1: "Leadoff", 2: "Two", 3: "Three"})

    def test_a_batter_is_used_once_and_where_he_batted_most(self):
        # Star hit third 100 games and second 60; the second slot's next best takes it
        rows = [self._bat(1, "Star"), self._bat(2, "Other")]
        order = {1: {3: {"games": 100, "pa": 450}, 2: {"games": 60, "pa": 270}},
                 2: {2: {"games": 50, "pa": 220}}}
        chosen = mlbstats.assign_lineup(rows, order)
        self.assertEqual({slot: r["_name"] for slot, r in chosen.items()}, {3: "Star", 2: "Other"})

    def test_a_slot_nobody_batted_from_is_left_empty_and_no_records_means_no_lineup(self):
        rows = [self._bat(1, "Only")]
        self.assertEqual(list(mlbstats.assign_lineup(rows, {1: {4: {"games": 30, "pa": 120}}})), [4])
        self.assertEqual(mlbstats.assign_lineup(rows, {}), {})                # before 1950
        self.assertEqual(mlbstats.assign_lineup([], {1: {1: {"games": 9, "pa": 9}}}), {})

    def test_ties_go_to_more_plate_appearances_then_name(self):
        rows = [self._bat(1, "Zed"), self._bat(2, "Abe"), self._bat(3, "Cy")]
        order = {1: {5: {"games": 80, "pa": 300}}, 2: {5: {"games": 80, "pa": 300}}, 3: {5: {"games": 80, "pa": 310}}}
        self.assertEqual(mlbstats.assign_lineup(rows, order)[5]["_name"], "Cy")
        del order[3]
        self.assertEqual(mlbstats.assign_lineup(rows, order)[5]["_name"], "Abe")


class UsualDefense(unittest.TestCase):
    @staticmethod
    def _fld(pid, name, pos, innings, games=100):
        return {"_id": pid, "_name": name, "_pos": pos, "innings": innings, "gamesPlayed": games}

    def test_each_position_goes_to_the_player_with_the_most_innings_there(self):
        rows = [self._fld(1, "Short", "SS", "1300.0"), self._fld(2, "Backup", "SS", "150.1"),
                self._fld(3, "Catch", "C", "1100.0"), self._fld(4, "Backup C", "C", "300.0")]
        chosen = mlbstats.assign_positions(rows)
        self.assertEqual({p: r["_name"] for p, r in chosen.items()}, {"SS": "Short", "C": "Catch"})

    def test_a_player_is_not_at_two_positions(self):
        # Utility man played more at first than in left, but the left fielder's backup is worse
        rows = [self._fld(1, "Utility", "1B", "900.0"), self._fld(1, "Utility", "LF", "600.0"),
                self._fld(2, "Left", "LF", "400.0")]
        chosen = mlbstats.assign_positions(rows)
        self.assertEqual({p: r["_name"] for p, r in chosen.items()}, {"1B": "Utility", "LF": "Left"})

    def test_games_decide_when_innings_werent_recorded_and_other_positions_are_ignored(self):
        rows = [self._fld(1, "A", "2B", None, games=90), self._fld(2, "B", "2B", None, games=60),
                self._fld(3, "Dh", "DH", "500.0"), self._fld(4, "Of", "OF", "800.0"), self._fld(None, "No id", "P", "9.0")]
        chosen = mlbstats.assign_positions(rows)
        self.assertEqual({p: r["_name"] for p, r in chosen.items()}, {"2B": "A"})


class RosterRange(unittest.TestCase):
    """The Range tab's whole-roster figure: chances accepted over what an average MLB fielder would have had."""
    LEAGUE = mlbstats.aggregate_league_positions([
        _fld("SS", 300, 300, "900.0", 100),      # MLB SS: 600 chances in 900 inn -> RF/9 6.0, RF/G 6.0
        _fld("C", 900, 60, "900.0", 100),        # MLB C: RF/9 9.6
    ])

    @staticmethod
    def _row(pid, pos, po, a, inn, g):
        return dict(_fld(pos, po, a, inn, g), _id=pid, _name=f"P{pid}")

    def test_it_is_actual_chances_over_expected_chances(self):
        rows = [self._row(1, "SS", 400, 350, "900.0", 100),   # 750 chances; expected 6.0 x 100 = 600
                self._row(2, "C", 480, 30, "450.0", 50)]      # 510 chances; expected 9.6 x 50 = 480
        got = mlbstats.roster_range(rows, self.LEAGUE)
        self.assertAlmostEqual(got["expected"], 1080)
        self.assertAlmostEqual(got["chances"], 1260)
        self.assertAlmostEqual(got["ratio"], 1260 / 1080)
        self.assertEqual(got["players"], 2)

    def test_playing_time_weights_each_row_rather_than_averaging_the_ratios(self):
        rows = [self._row(1, "SS", 400, 350, "900.0", 100),   # ratio 1.25
                self._row(2, "SS", 0, 0, "9.0", 1)]           # a one-game sub who accepted nothing: ratio 0
        got = mlbstats.roster_range(rows, self.LEAGUE)
        self.assertAlmostEqual(got["ratio"], 750 / (600 + 6.0))        # not (1.25 + 0) / 2
        self.assertGreater(got["ratio"], 1.2)

    def test_a_player_at_two_positions_counts_once_but_both_rows_count(self):
        rows = [self._row(1, "SS", 100, 100, "300.0", 40), self._row(1, "C", 300, 20, "300.0", 40)]
        got = mlbstats.roster_range(rows, self.LEAGUE)
        self.assertEqual(got["players"], 1)
        self.assertAlmostEqual(got["expected"], 6.0 * 300 / 9 + 9.6 * 300 / 9)

    def test_per_game_stands_in_where_innings_arent_recorded(self):
        rows = [self._row(1, "SS", 300, 300, None, 100)]                # RF/G 6.0 vs MLB's 6.0
        self.assertAlmostEqual(mlbstats.roster_range(rows, self.LEAGUE)["ratio"], 1.0)

    def test_positions_outside_the_nine_and_unmeasurable_rows_are_left_out(self):
        rows = [self._row(1, "DH", 0, 0, "500.0", 50), self._row(2, "OF", 90, 4, "800.0", 90),
                self._row(3, "SS", 0, 0, "0.0", 0)]
        self.assertIsNone(mlbstats.roster_range(rows, self.LEAGUE))
        self.assertIsNone(mlbstats.roster_range([], self.LEAGUE))


class RosterProduction(unittest.TestCase):
    ROWS = [{"_id": 1, "plateAppearances": 600}, {"_id": 2, "plateAppearances": 300},
            {"_id": 3, "plateAppearances": 0}, {"_id": 4, "plateAppearances": 20}, {"_id": None, "plateAppearances": 9}]

    def test_the_rate_is_total_production_over_total_plate_appearances(self):
        got = mlbstats.roster_production(self.ROWS, {1: 300, 2: 120, 3: 5, 4: 0})
        self.assertEqual((got["total"], got["pa"], got["players"]), (420, 920, 3))     # the 0-PA batter is out
        self.assertAlmostEqual(got["rate"], 420 / 920)                                # not the mean of 0.5, 0.4, 0.0

    def test_a_batter_with_no_value_is_left_out_of_both_sums(self):
        got = mlbstats.roster_production(self.ROWS, {1: 300, 2: None, 3: None, 4: 2})
        self.assertEqual((got["total"], got["pa"]), (302, 620))

    def test_nobody_measurable_gives_no_rate(self):
        self.assertIsNone(mlbstats.roster_production(self.ROWS, {})["rate"])


class PreventionLineupTotal(unittest.TestCase):
    RA9 = "{Pitching: R}*27/{Pitching: Outs}"

    def test_the_lineup_is_the_three_roles_together_and_the_rest_is_other(self):
        rows = PitcherRoles.STAFF
        report = mlbstats.prevention_report(rows, {"Pitching": mlbstats.group_by_player(rows)}, {"R/9": self.RA9})
        by_role = {r["role"]: r["total"] for r in report["roles"]}
        lineup = report["lineup"]
        for key in ("gs", "gf", "outs", "r", "er", "ur", "count"):
            self.assertEqual(lineup[key], by_role["Rotation"][key] + by_role["Long Relief"][key] + by_role["Closers"][key], key)
            self.assertEqual(lineup[key] + by_role["Other"][key], report["all"][key], key)
        self.assertNotIn("Other", mlbstats.PITCHER_LINEUP_ROLES)

    def test_its_rate_is_worked_out_from_the_combined_figures(self):
        rows = PitcherRoles.STAFF
        report = mlbstats.prevention_report(rows, {"Pitching": mlbstats.group_by_player(rows)}, {"R/9": self.RA9})
        lineup = report["lineup"]
        self.assertAlmostEqual(lineup["stats"]["R/9"], lineup["r"] * 27 / lineup["outs"])

    def test_no_lineup_pitchers_no_lineup_rate(self):
        report = mlbstats.prevention_report([], {"Pitching": {}}, {"R/9": self.RA9})
        self.assertEqual(report["lineup"]["count"], 0)
        self.assertIsNone(report["lineup"]["stats"]["R/9"])


class RunnerGroups(unittest.TestCase):
    LEAGUE_RATE = 0.10
    LEAGUE_OPPS = 150          # MLB's average opportunities per runner: the bench floor is a fifth of it, 30

    @staticmethod
    def _runner(name, opps, bg):
        return {"name": name, "opps": opps, "bg": bg}

    def _classify(self, runners, league_opps=LEAGUE_OPPS):
        return mlbstats.classify_runners(runners, self.LEAGUE_RATE, league_opps)

    def _groups(self, runners, league_opps=LEAGUE_OPPS):
        got = self._classify(runners, league_opps)["groups"]
        return {g: [r["name"] for r in members] for g, members in got.items()}

    def test_regulars_have_at_least_the_average_opportunities_and_are_split_by_speed(self):
        runners = [self._runner("Fast", 400, 60),      # 0.150 / 0.10 = 150%
                   self._runner("Average", 400, 40),   # 100%
                   self._runner("Slow", 400, 20),      # 50%
                   self._runner("Bench", 20, 5),       # very quick, but hardly ever on (under MLB's floor of 30)
                   self._runner("Sub", 10, 0)]
        self.assertEqual(self._groups(runners), {"Speedy regulars": ["Fast"], "Smart regulars": ["Average"],
                                                 "Slow regulars": ["Slow"], "Speedy bench": [], "Other": ["Bench", "Sub"]})

    def test_the_boundaries_are_125_and_75_percent_as_shown(self):
        runners = [self._runner("At top", 400, 50), self._runner("Just under top", 400, 49.9),   # 125.0%, 124.75 -> 124.8
                   self._runner("At bottom", 400, 30), self._runner("Just over bottom", 400, 30.4)]
        got = self._groups(runners)
        self.assertEqual(got["Speedy regulars"], ["At top"])
        self.assertEqual(got["Slow regulars"], ["At bottom"])
        self.assertEqual(sorted(got["Smart regulars"]), ["Just over bottom", "Just under top"])

    def test_the_average_is_over_every_runner_and_a_runner_exactly_at_it_is_a_regular(self):
        runners = [self._runner("A", 300, 30), self._runner("B", 100, 10), self._runner("C", 200, 20)]   # average 200
        result = self._classify(runners)
        self.assertEqual(result["average_opps"], 200)
        self.assertEqual([r["name"] for r in result["groups"]["Other"]], ["B"])
        self.assertEqual([r["name"] for r in result["groups"]["Smart regulars"]], ["A", "C"])

    def test_members_are_busiest_first_and_everyone_lands_in_one_group(self):
        runners = [self._runner("Low", 100, 5), self._runner("High", 900, 90),
                   self._runner("Mid", 500, 50), self._runner("Zed", 500, 50), self._runner("Bee", 500, 60)]
        groups = self._classify(runners)["groups"]
        # equal opportunities: more bases gained first, then name
        self.assertEqual([r["name"] for r in groups["Smart regulars"]], ["High", "Bee", "Mid", "Zed"])
        self.assertEqual(sum(len(m) for m in groups.values()), len(runners))
        self.assertEqual(list(groups), list(mlbstats.RUNNER_GROUPS))

    def test_a_fast_runner_below_the_regulars_cutoff_is_speedy_bench_if_he_had_enough_chances(self):
        # team average (400x3 + 128 + 60 + 4) / 6 = 232; MLB floor 30
        runners = [self._runner("R1", 400, 40), self._runner("R2", 400, 40), self._runner("R3", 400, 40),
                   self._runner("Pinch runner", 128, 39),    # 305% -- the Herb Washington case
                   self._runner("Slow sub", 128, 5),          # below the cutoff, enough chances, but not fast
                   self._runner("Lucky", 4, 1)]               # 250%, but 4 chances prove nothing
        got = self._groups(runners)
        self.assertEqual(got["Speedy bench"], ["Pinch runner"])
        self.assertEqual(got["Other"], ["Slow sub", "Lucky"])
        self.assertEqual(got["Smart regulars"], ["R1", "R2", "R3"])

    def test_the_bench_floor_is_a_fifth_of_mlbs_average_and_a_runner_exactly_at_it_counts(self):
        # team opportunities 500 + 300 + 100 + 60 + 30 = 990 -> team average 198; every runner gains 200% of MLB's rate
        runners = [self._runner("A", 500, 100), self._runner("B", 300, 60), self._runner("C", 100, 20),
                   self._runner("D", 60, 12), self._runner("At floor", 30, 6)]
        result = self._classify(runners)
        self.assertAlmostEqual(result["bench_floor"], 30)
        got = {g: [r["name"] for r in m] for g, m in result["groups"].items()}
        self.assertEqual(got["Speedy regulars"], ["A", "B"])                # regulars are never bench
        self.assertEqual(got["Speedy bench"], ["C", "D", "At floor"])
        runners[4] = self._runner("Under floor", 29, 6)
        self.assertEqual(self._groups(runners)["Other"], ["Under floor"])

    def test_the_floor_follows_mlbs_norm_not_the_teams(self):
        # the same team, judged in a league whose runners average 50 opportunities (floor 10) and one averaging 400 (floor 80)
        runners = [self._runner("A", 400, 40), self._runner("B", 400, 40), self._runner("Pinch", 40, 8)]   # Pinch 200%
        self.assertEqual(self._groups(runners, league_opps=50)["Speedy bench"], ["Pinch"])
        self.assertEqual(self._groups(runners, league_opps=400)["Speedy bench"], [])
        # and a team with a very high (or low) average of its own doesn't move the floor
        crowd = [self._runner("A", 900, 90), self._runner("B", 900, 90), self._runner("Pinch", 40, 8)]
        self.assertEqual(self._classify(crowd)["bench_floor"], self._classify(runners)["bench_floor"])
        self.assertEqual(self._groups(crowd)["Speedy bench"], ["Pinch"])

    def test_without_an_mlb_norm_there_is_no_bench(self):
        runners = [self._runner("A", 400, 40), self._runner("Pinch", 40, 8)]
        got = mlbstats.classify_runners(runners, self.LEAGUE_RATE)
        self.assertIsNone(got["bench_floor"])
        self.assertEqual([r["name"] for r in got["groups"]["Other"]], ["Pinch"])

    def test_without_a_rate_to_compare_with_nobody_is_a_regular_and_nothing_breaks(self):
        runners = [self._runner("A", 300, 30), self._runner("B", 100, None)]
        groups = mlbstats.classify_runners(runners, None, self.LEAGUE_OPPS)["groups"]
        self.assertEqual([len(groups[g]) for g in mlbstats.RUNNER_GROUPS], [0, 0, 0, 0, 2])
        self.assertEqual(mlbstats.classify_runners([], 0.1, self.LEAGUE_OPPS)["average_opps"], 0.0)


def _ps_team(tid, name, abbr, league=None):
    team = {"id": tid, "name": name, "abbreviation": abbr}
    if league:
        team["league"] = {"id": league}
    return team


def _ps_game(n, away, home, a_runs=None, h_runs=None, state="Final", best_of=5, desc="AL Division Series",
             tie=False, if_necessary="N"):
    final = a_runs is not None
    return {"seriesGameNumber": n, "gamesInSeries": best_of, "officialDate": f"2025-10-0{n}",
            "gameDate": f"2025-10-0{n}T20:00:00Z", "seriesDescription": desc, "isTie": tie,
            "ifNecessary": if_necessary, "venue": {"name": "Park"}, "description": f"Game {n}",
            "status": {"abstractGameState": "Final" if final else "Preview", "detailedState": state},
            "teams": {"away": {"team": away, "score": a_runs, "isWinner": final and a_runs > h_runs},
                      "home": {"team": home, "score": h_runs, "isWinner": final and h_runs > a_runs}}}


def _ps_series(sid, round_, games):
    return {"series": {"id": sid, "gameType": round_}, "games": games}


class Postseason(unittest.TestCase):
    TOR, NYY, BOS = _ps_team(141, "Toronto Blue Jays", "TOR", 103), _ps_team(147, "New York Yankees", "NYY", 103), \
        _ps_team(111, "Boston Red Sox", "BOS", 103)

    def test_series_wins_winner_and_order(self):
        games = [_ps_game(1, self.NYY, self.TOR, 1, 10), _ps_game(2, self.NYY, self.TOR, 7, 13),
                 _ps_game(3, self.TOR, self.NYY, 6, 9), _ps_game(4, self.TOR, self.NYY, 5, 2)]
        [s] = mlbstats.parse_postseason({"series": [_ps_series("D_1", "D", games)]})
        self.assertEqual([t["abbr"] for t in s["teams"]], ["TOR", "NYY"])  # game 1's home club first
        self.assertEqual(s["wins"], {141: 3, 147: 1})
        self.assertEqual(s["winner"], 141)
        self.assertEqual((s["league"], s["best_of"], s["slots"]), ("AL", 5, 5))

    def test_tie_postponed_and_tiebreakers(self):
        games = [_ps_game(1, self.NYY, self.TOR, 6, 6, tie=True, best_of=7, desc="World Series"),
                 _ps_game(2, self.NYY, self.TOR, state="Postponed", best_of=7, desc="World Series")]
        games += [_ps_game(n, self.NYY, self.TOR, 1, 2, best_of=7, desc="World Series") for n in range(2, 9)]
        payload = {"series": [_ps_series("W_1", "W", games),
                              _ps_series("R_1", "R", [_ps_game(1, self.NYY, self.BOS, 1, 2)])]}
        [s] = mlbstats.parse_postseason(payload)
        self.assertTrue(s["games"][0]["tie"])
        self.assertEqual(len(s["games"]), 8)  # postponed listing dropped, eight played
        self.assertEqual((s["best_of"], s["slots"]), (7, 8))
        self.assertEqual(s["winner"], 141)  # four wins decide it, the tie doesn't count
        self.assertIsNone(s["league"])

    def test_undecided_series_and_scheduled_games(self):
        games = [_ps_game(1, self.BOS, self.NYY, 3, 1, best_of=3, desc="AL Wild Card Series"),
                 _ps_game(2, self.BOS, self.NYY, best_of=3, desc="AL Wild Card Series"),
                 _ps_game(3, self.BOS, self.NYY, best_of=3, desc="AL Wild Card Series", if_necessary="Y")]
        [s] = mlbstats.parse_postseason({"series": [_ps_series("F_1", "F", games)]})
        self.assertIsNone(s["winner"])
        self.assertEqual([g["state"] for g in s["games"]], ["final", "scheduled", "scheduled"])
        self.assertTrue(s["games"][2]["if_necessary"])
        self.assertEqual(s["games"][1]["score"], {})

    def test_duplicate_listing_counted_once(self):
        # 1971: the API lists games 6 and 7 twice; Pittsburgh won in seven, not Baltimore in nine
        pit, bal = _ps_team(134, "Pittsburgh Pirates", "PIT", 104), _ps_team(110, "Baltimore Orioles", "BAL", 103)
        scores = [(pit, bal, 3, 5), (pit, bal, 3, 11), (bal, pit, 1, 5), (bal, pit, 3, 4), (bal, pit, 0, 4),
                  (pit, bal, 2, 3), (pit, bal, 2, 3), (pit, bal, 2, 1), (pit, bal, 2, 1)]
        numbers = [1, 2, 3, 4, 5, 6, 6, 7, 7]
        games = []
        for n, (away, home, a, h) in zip(numbers, scores):
            g = _ps_game(n, away, home, a, h, best_of=7, desc="World Series")
            g["gamePk"] = 67630 + n
            games.append(g)
        [s] = mlbstats.parse_postseason({"series": [_ps_series("W_1", "W", games)]})
        self.assertEqual((len(s["games"]), s["slots"]), (7, 7))
        self.assertEqual(s["wins"], {110: 3, 134: 4})
        self.assertEqual(s["winner"], 134)

    def test_two_series_under_one_id_are_split(self):
        # 2003: "D_1" holds both Twins-Yankees and Cubs-Braves
        chc, atl = _ps_team(112, "Chicago Cubs", "CHC", 104), _ps_team(144, "Atlanta Braves", "ATL", 104)
        games = [_ps_game(1, self.BOS, self.NYY, 3, 1), _ps_game(1, chc, atl, 4, 2, desc="NL Division Series"),
                 _ps_game(2, self.BOS, self.NYY, 1, 4), _ps_game(2, chc, atl, 3, 5, desc="NL Division Series")]
        for pk, g in enumerate(games):
            g["gamePk"] = pk
        series = mlbstats.parse_postseason({"series": [_ps_series("D_1", "D", games)]})
        self.assertEqual([(s["id"], s["league"], len(s["games"])) for s in series],
                         [("D_1", "AL", 2), ("D_1.2", "NL", 2)])

    def test_link_by_winner_and_placeholder(self):
        wc = _ps_series("F_1", "F", [_ps_game(1, self.BOS, self.NYY, 3, 1, best_of=3, desc="AL Wild Card Series")])
        slot = _ps_team(5529, "NYY/BOS", "NYY/BOS", 103)
        ds_a = _ps_series("D_1", "D", [_ps_game(1, self.TOR, _ps_team(139, "Tampa Bay Rays", "TB", 103))])
        ds_b = _ps_series("D_2", "D", [_ps_game(1, slot, _ps_team(114, "Cleveland Guardians", "CLE", 103))])
        lcs = _ps_series("L_1", "L", [_ps_game(1, _ps_team(5521, "AL Lower Seed", "AL Low", 103),
                                              _ps_team(5513, "AL Higher Seed", "AL High", 103),
                                              best_of=7, desc="AL Championship Series")])
        series = mlbstats.parse_postseason({"series": [wc, ds_a, ds_b, lcs]})
        self.assertTrue(next(t for s in series for t in s["teams"] if t["id"] == 5529)["placeholder"])
        links = mlbstats.link_postseason(series)
        self.assertEqual(links["D_2"], ["F_1"])  # "NYY/BOS" is the wild card series' slot
        self.assertEqual(links["D_1"], [])
        self.assertEqual(sorted(links["L_1"]), ["D_1", "D_2"])  # placeholders: the only ALCS


class EarlyPostseason(unittest.TestCase):
    """The bundled pre-1903 series (early_postseason.json)."""

    def test_every_series_adds_up(self):
        with open(mlbstats.EARLY_POSTSEASON_FILE, encoding="utf-8") as fh:
            entries = json.load(fh)["series"]
        self.assertEqual([e["season"] for e in entries],
                         [1884, 1885, 1886, 1887, 1888, 1889, 1890, 1892, 1894, 1895, 1896, 1897, 1900])
        for entry in entries:
            s = mlbstats.parse_early_postseason(entry)
            ties = sum(g["tie"] for g in s["games"])
            self.assertEqual(sum(s["wins"].values()) + ties, len(s["games"]), entry["season"])
            for g in entry["games"]:
                self.assertEqual(g["date"][:4], str(entry["season"]))
                self.assertLessEqual({g["score"][0], g["score"][2]}, set(entry["teams"]))
            if s["winner"] is None:  # 1885 and 1890 ended level
                self.assertEqual(len(set(s["wins"].values())), 1, entry["season"])
            else:
                self.assertEqual(s["wins"][s["winner"]], max(s["wins"].values()), entry["season"])

    def test_known_results(self):
        def result(season):
            [s] = mlbstats.early_postseason(season)
            names = {t["id"]: t["abbr"] for t in s["teams"]}
            return {names[k]: v for k, v in s["wins"].items()}, names.get(s["winner"]), len(s["games"])
        self.assertEqual(result(1887), ({"STL": 5, "DTN": 10}, "DTN", 15))
        self.assertEqual(result(1885), ({"STL": 3, "CHI": 3}, None, 7))
        self.assertEqual(result(1892), ({"CLV": 0, "BSN": 5}, "BSN", 6))
        self.assertEqual(result(1900), ({"PIT": 1, "BRO": 3}, "BRO", 4))
        self.assertEqual(mlbstats.get_postseason(1891), [])


class NegroPostseason(unittest.TestCase):
    """The bundled Negro league series (negro_postseason.json, built by make_negro_postseason.py)."""

    def test_winners_have_the_most_counted_wins(self):
        with open(mlbstats.NEGRO_POSTSEASON_FILE, encoding="utf-8") as fh:
            entries = json.load(fh)["series"]
        for entry in entries:
            s = mlbstats.parse_early_postseason(entry)
            self.assertIn(s["round"], ("D", "L", "W", "X"))
            if s["winner"] is not None:
                others = [n for tid, n in s["wins"].items() if tid != s["winner"]]
                self.assertGreater(s["wins"][s["winner"]], max(others), (entry["season"], entry["title"]))
            if not s["official"]:
                self.assertTrue(s["status"], (entry["season"], entry["title"]))  # says why

    def test_official_only_inside_the_recognized_years(self):
        for season in (1903, 1904, 1913, 1949):
            self.assertTrue(all(not s["official"] for s in mlbstats.negro_postseason(season)), season)
        self.assertTrue(all(s["official"] for s in mlbstats.negro_postseason(1948)))
        self.assertEqual(mlbstats.negro_postseason(1950), [])

    def test_1942_uncounted_games(self):
        [s] = mlbstats.negro_postseason(1942)
        names = {t["id"]: t["abbr"] for t in s["teams"]}
        self.assertEqual({names[k]: v for k, v in s["wins"].items()}, {"KCM": 4, "HOM": 0})
        self.assertEqual(sum(1 for g in s["games"] if not g["counted"]), 2)

    def test_bracket_links(self):
        series = [s for s in mlbstats.negro_postseason(1948) if s["round"] != "X"]
        links = mlbstats.link_postseason(series)
        self.assertEqual(len(links["W_1"]), 2)  # both league playoffs feed the World Series


class Denied(unittest.TestCase):
    """mlbstats.denied_report: the user's "ought to have won" rules on made-up seasons."""

    @staticmethod
    def team(tid, name, wins, losses, rank=None):
        return {"id": tid, "name": name, "wins": wins, "losses": losses, "ties": 0,
                "pct": f"{wins / (wins + losses):.3f}"[1:], "gb": "-", "rank": rank}

    @staticmethod
    def group(league_id, league, teams, division=""):
        return {"league_id": league_id, "league": league, "division_id": 1 if division else None,
                "division": division, "name": division or league, "teams": teams}

    @staticmethod
    def series(round_, title, a, b, winner, wins=(4, 2), best_of=7):
        loser = b if winner is a else a
        return {"round": round_, "title": title, "best_of": best_of,
                "teams": [{"id": a["id"], "name": a["name"]}, {"id": b["id"], "name": b["name"]}],
                "games": [{"state": "final"}], "wins": {winner["id"]: wins[0], loser["id"]: wins[1]},
                "winner": winner["id"]}

    def report(self, season, groups, series, negro=None):
        return {r["team"]: r for r in mlbstats.denied_report(season, groups, series, negro)}

    def test_best_record_beaten_by_underdog(self):
        cubs, sox, giants = self.team(1, "Chicago Cubs", 116, 36), self.team(2, "Chicago White Sox", 93, 58), \
            self.team(3, "New York Giants", 96, 56)
        rows = self.report(1906, [self.group(103, "American League", [sox]),
                                  self.group(104, "National League", [cubs, giants])],
                           [self.series("W", "World Series", cubs, sox, sox)])
        self.assertEqual(list(rows), ["Chicago Cubs"])  # only baseball's best before 1969
        self.assertEqual(rows["Chicago Cubs"]["denied"], "Denied:  Defeated by underdog in World Series")
        self.assertEqual(rows["Chicago Cubs"]["result"], "Lost World Series to Chicago White Sox (2–4)")

    def test_tied_best_and_no_postseason(self):
        a, b = self.team(1, "A", 92, 62), self.team(2, "B", 92, 62)
        groups = [self.group(103, "American League", [a]), self.group(104, "National League", [b])]
        rows = self.report(1958, groups, [self.series("W", "World Series", a, b, a, (4, 3))])
        self.assertEqual(rows["A"]["best_in"], ["MLB (tied)"])
        self.assertEqual(rows["A"]["denied"], "")
        self.assertEqual(rows["B"]["denied"], "")  # beaten by an equal: no upset
        self.assertTrue(rows["B"]["result"].endswith("not an upset: an equal record"))
        rows = self.report(1904, groups, [])
        self.assertEqual(rows["A"]["denied"], "Denied:  No postseason")

    def test_tiebreaker_loser(self):
        la, sf, nyy = self.team(1, "LA", 101, 61), self.team(2, "SF", 101, 61), self.team(3, "NYY", 96, 66)
        rows = self.report(1962, [self.group(103, "American League", [nyy]),
                                  self.group(104, "National League", [la, sf])],
                           [self.series("W", "World Series", nyy, sf, nyy, (4, 3))])
        self.assertEqual(rows["LA"]["denied"], "Denied:  Lost tiebreaker playoff")
        self.assertEqual(rows["SF"]["denied"], "Denied:  Defeated by underdog in World Series")

    def test_league_layer_and_split_season(self):
        reds, phi = self.team(1, "Reds", 66, 42, 1), self.team(2, "Phillies", 59, 48, 3)
        oak, nyy = self.team(3, "A's", 64, 45, 1), self.team(4, "Yankees", 59, 48, 2)
        groups = [self.group(103, "American League", [oak, nyy], "American League West"),
                  self.group(104, "National League", [reds, phi], "National League West")]
        rows = self.report(1981, groups, [self.series("D", "NL Division Series", phi, self.team(5, "Expos", 60, 48), phi, (3, 2), 5),
                                          self.series("L", "AL Championship Series", oak, nyy, nyy, (3, 0), 5)])
        self.assertEqual(rows["Reds"]["best_in"], ["MLB", "NL"])
        self.assertEqual(rows["Reds"]["denied"], "Denied:  Excluded by split-season format")
        self.assertEqual(rows["A's"]["denied"], "Denied:  Defeated by underdog in championship series")

    def test_division_and_wild_card_layers(self):
        nym, atl, phi = self.team(1, "Mets", 101, 61, 2), self.team(2, "Braves", 101, 61, 1), self.team(3, "Phillies", 87, 75, 3)
        stl, lad, sd = self.team(4, "Cardinals", 93, 69, 1), self.team(5, "Dodgers", 111, 51, 1), self.team(6, "Padres", 89, 73, 2)
        hou = self.team(7, "Astros", 106, 56, 1)
        groups = [self.group(103, "American League", [hou], "American League West"),
                  self.group(104, "National League", [atl, nym, phi], "National League East"),
                  self.group(104, "National League", [stl], "National League Central"),
                  self.group(104, "National League", [lad, sd], "National League West")]
        series = [self.series("F", "NL Wild Card Series", nym, sd, sd, (1, 2), 3),
                  self.series("F", "NL Wild Card Series", stl, phi, phi, (2, 0), 3),
                  self.series("D", "NL Division Series", lad, sd, sd, (3, 1), 5),
                  self.series("D", "NL Division Series", atl, phi, phi, (3, 1), 5),
                  self.series("L", "NL Championship Series", sd, phi, phi, (4, 1)),
                  self.series("W", "World Series", hou, phi, hou)]
        rows = self.report(2022, groups, series)
        self.assertEqual(rows["Dodgers"]["best_in"], ["MLB", "NL", "NL West"])
        self.assertEqual(rows["Dodgers"]["denied"], "Denied:  Defeated by underdog in division series")
        self.assertEqual(rows["Mets"]["best_in"], ["NL wild card"])
        self.assertEqual(rows["Mets"]["denied"], "Denied:  Defeated by underdog in wild card series")
        self.assertEqual(rows["Cardinals"]["denied"], "Denied:  Defeated by underdog in wild card series")
        self.assertEqual(rows["Astros"]["result"], "Won World Series")
        self.assertNotIn("Padres", rows)

    def test_beaten_by_a_better_record_is_no_denial(self):
        # 2025: the Cubs, the NL's best wild card, lost the division series to MLB's best, the Brewers
        mil, chc = self.team(1, "Brewers", 97, 65, 1), self.team(2, "Cubs", 92, 70, 2)
        lad, sd = self.team(3, "Dodgers", 93, 69, 1), self.team(4, "Padres", 90, 72, 2)
        groups = [self.group(104, "National League", [mil, chc], "National League Central"),
                  self.group(104, "National League", [lad, sd], "National League West")]
        series = [self.series("D", "NL Division Series", mil, chc, mil, (3, 2), 5),
                  self.series("L", "NL Championship Series", lad, mil, lad, (4, 0))]
        rows = self.report(2025, groups, series)
        self.assertEqual(rows["Cubs"]["best_in"], ["NL wild card"])
        self.assertEqual(rows["Cubs"]["denied"], "")
        self.assertEqual(rows["Cubs"]["result"],
                         "Lost NL Division Series to Brewers (2\u20133) \u2014 not an upset: a better record")
        self.assertEqual(rows["Brewers"]["denied"], "Denied:  Defeated by underdog in championship series")

    def test_early_final_matched_by_name(self):
        # MLB calls the AA's St. Louis Browns the Cardinals; the NL had a poor St. Louis Maroons too
        chi, mar = self.team(1, "Chicago White Stockings", 90, 34), self.team(2, "St. Louis Maroons", 43, 79)
        stl = self.team(3, "St. Louis Cardinals", 93, 46)
        groups = [self.group(104, "National League", [chi, mar]), self.group(100, "American Association", [stl])]
        early = {"round": "W", "title": "World's Series", "best_of": 6,
                 "teams": [{"id": -1, "name": "Chicago White Stockings"}, {"id": -2, "name": "St. Louis Browns"}],
                 "games": [{"state": "final"}], "wins": {-1: 2, -2: 4}, "winner": -2}
        rows = self.report(1886, groups, [early])
        self.assertEqual(list(rows), ["Chicago White Stockings"])
        self.assertEqual(rows["Chicago White Stockings"]["denied"], "Denied:  Defeated by underdog in final series (1886 World's Series)")
        self.assertEqual(rows["Chicago White Stockings"]["result"], "Lost World's Series to St. Louis Cardinals (2–4)")

    def test_negro_leagues_judged_apart(self):
        nyy, kcm = self.team(1, "Yankees", 97, 57), self.team(10, "Kansas City Monarchs", 57, 21)
        cag = self.team(11, "Chicago American Giants", 57, 23)
        groups = [self.group(103, "American League", [nyy]), self.group(430, "Negro National League (I)", [kcm, cag])]
        negro = [dict(self.series("L", "Championship Series", {"id": -1, "name": "Kansas City Monarchs"},
                                  {"id": -2, "name": "Chicago American Giants"},
                                  {"id": -2, "name": "Chicago American Giants"}, (4, 5), 9), official=True),
                 dict(self.series("W", "Colored World Series", {"id": -2, "name": "Chicago American Giants"},
                                  {"id": -3, "name": "Bacharach Giants"},
                                  {"id": -2, "name": "Chicago American Giants"}, (5, 4), 9), official=True)]
        rows = self.report(1926, groups, [], negro)
        self.assertEqual(rows["Yankees"]["denied"], "Denied:  No postseason")
        self.assertEqual(rows["Kansas City Monarchs"]["best_in"], ["Negro leagues"])
        self.assertEqual(rows["Kansas City Monarchs"]["track"], "Negro")
        self.assertEqual(rows["Kansas City Monarchs"]["denied"], "Denied:  Defeated by underdog in championship series")


class DeniedTiebreakers(unittest.TestCase):
    """Tiebreakers and how far each team got, on the Denied tab."""

    team, group, series = staticmethod(Denied.team), staticmethod(Denied.group), staticmethod(Denied.series)

    def test_left_out_games_added_and_better_than(self):
        # 1962: MLB's standings have both clubs 101-61, leaving the three-game playoff out
        la, sf, nyy = self.team(1, "LA", 101, 61), self.team(2, "SF", 101, 61), self.team(3, "NYY", 96, 66)
        games = [{"pk": 1, "date": "1962-10-01", "away": 1, "home": 2, "score": {1: 0, 2: 8}},
                 {"pk": 2, "date": "1962-10-02", "away": 2, "home": 1, "score": {2: 7, 1: 8}},
                 {"pk": 3, "date": "1962-10-03", "away": 2, "home": 1, "score": {2: 6, 1: 4}}]
        rows = {r["team"]: r for r in mlbstats.denied_report(
            1962, [self.group(103, "American League", [nyy]), self.group(104, "National League", [la, sf])],
            [self.series("W", "World Series", nyy, sf, nyy, (4, 3))], None, games)}
        self.assertEqual((rows["SF"]["wins"], rows["SF"]["losses"]), (103, 62))
        self.assertEqual(rows["SF"]["best_in"], ["MLB"])
        self.assertTrue(rows["SF"]["record_note"])
        self.assertEqual(rows["LA"]["best_in"], [])
        self.assertEqual(rows["LA"]["denied"], "")  # nobody ought to win a tiebreaker
        self.assertEqual(rows["LA"]["tiebreaker"], "Lost NL pennant tiebreaker vs SF (1\u20132)")
        self.assertEqual(rows["LA"]["stage"], mlbstats.DENIED_STAGES["T"])
        self.assertEqual(rows["LA"]["better_than"], ["NYY (.593, won World Series)"])
        self.assertNotIn("NYY", rows)  # neither best nor in a tiebreaker
        self.assertEqual(rows["SF"]["stage"], mlbstats.DENIED_STAGES["final"])
        self.assertFalse(rows["SF"]["champion"])

    def test_counted_games_left_alone_and_division_title(self):
        mil, chc = self.team(1, "Brewers", 96, 67, 1), self.team(2, "Cubs", 95, 68, 2)
        games = [{"pk": 1, "date": "2018-10-01", "away": 1, "home": 2, "score": {1: 3, 2: 1}}]
        rows = {r["team"]: r for r in mlbstats.denied_report(
            2018, [self.group(104, "National League", [mil, chc], "National League Central")],
            [self.series("F", "NL Wild Card Game", chc, self.team(3, "Rockies", 91, 72), self.team(3, "Rockies", 91, 72), (0, 1), 1)],
            None, games)}
        self.assertEqual((rows["Brewers"]["wins"], rows["Brewers"]["record_note"]), (96, ""))
        self.assertEqual(rows["Cubs"]["tiebreaker"], "Lost NL Central tiebreaker vs Brewers (0\u20131)")
        self.assertEqual(rows["Cubs"]["stage"], mlbstats.DENIED_STAGES["F"])
        self.assertEqual(rows["Cubs"]["better_than"], [])  # went on: not put out by it


class Poststats(unittest.TestCase):
    """Postseason-only statistics summed from box scores (the Poststats tab)."""

    @staticmethod
    def _g(pk, gtype, away, home, state="Final", tiebreaker="N"):
        return {"gamePk": pk, "gameType": gtype, "officialDate": "2000-10-01", "tiebreaker": tiebreaker,
                "status": {"abstractGameState": state, "detailedState": state},
                "teams": {"away": {"team": {"id": away, "name": f"T{away}"}},
                          "home": {"team": {"id": home, "name": f"T{home}"}}}}

    def test_games_rounds_and_tiebreakers(self):
        series = {"series": [{"games": [self._g(1, "W", 10, 20), self._g(1, "W", 10, 20),  # listed twice
                                        self._g(2, "L", 10, 30), self._g(9, "R", 10, 40)]}]}
        sched = {"dates": [{"games": [self._g(9, "R", 10, 40, tiebreaker="Y"),
                                      self._g(8, "R", 50, 60, tiebreaker="N")]}]}
        rounds = lambda season: {g["pk"]: g["round"] for g in mlbstats.parse_post_games(series, sched, season)}
        for season in (1951, 1978, 2018):  # a pennant playoff, a division tiebreaker, "Game 163"
            self.assertEqual(rounds(season), {1: "W", 2: "L", 9: "T"})

    def test_league_grouping(self):
        self.assertEqual(mlbstats._league_key({"KCM": "NAL", "HOM": "NNL"}), ("NNL-NAL", "NNL v NAL World Series"))
        self.assertEqual(mlbstats._league_key({"A": "NL", "B": "AA"}), ("NL-AA", "NL v AA World's Series"))
        self.assertEqual(mlbstats._league_key({"A": "NNL", "B": "NNL"}), ("NNL", "NNL postseason series"))
        self.assertEqual(mlbstats._league_key({"A": "NNL", "B": "independent"}), ("NNL-ind", "NNL v independent clubs"))
        self.assertEqual(mlbstats._league_key({"A": "independent", "B": "independent"}), ("ind", "Independent clubs"))

    def test_boxscore_lines_and_rates(self):
        player = lambda pid, pos, bat, pit=None, fld=None: {
            "person": {"id": pid, "fullName": f"P{pid}"}, "position": {"abbreviation": pos},
            "stats": {"batting": bat, "pitching": pit or {}, "fielding": fld or {}}}
        box = {"teams": {"away": {"team": {"id": 1, "name": "A"}, "players": {
                   "a": player(7, "SS", {"atBats": 4, "hits": 2, "doubles": 1, "baseOnBalls": 1,
                                         "plateAppearances": 5, "summary": "2-4"},
                               fld={"putOuts": 2, "assists": 3, "errors": 1}),
                   "b": player(8, "PH", {"atBats": 1, "hits": 0, "plateAppearances": 1})}},
               "home": {"team": {"id": 2, "name": "B"}, "players": {
                   "c": player(9, "P", {}, pit={"outs": 20, "earnedRuns": 2, "hits": 5, "baseOnBalls": 1,
                                                "strikeOuts": 6, "wins": 1})}}}}
        lines = mlbstats.boxscore_lines(box)
        self.assertEqual(sorted((ln["player"], ln["group"]) for ln in lines),
                         [(7, "batting"), (7, "fielding"), (8, "batting"), (9, "pitching")])  # no PH fielding
        [bat] = [r for r in mlbstats.aggregate_post_lines(lines + lines, "Batting") if r["_id"] == 7]
        self.assertEqual((bat["gamesPlayed"], bat["atBats"], bat["hits"], bat["avg"], bat["slg"], bat["obp"]),
                         (2, 8, 4, ".500", ".750", ".600"))
        [pit] = mlbstats.aggregate_post_lines(lines, "Pitching")
        self.assertEqual((pit["inningsPitched"], pit["era"], pit["whip"]), ("6.2", "2.70", "0.90"))
        [fld] = mlbstats.aggregate_post_lines(lines, "Fielding")
        self.assertEqual((fld["_pos"], fld["chances"], fld["fielding"]), ("SS", 6, ".833"))
        [team_a, team_b] = mlbstats.post_team_totals(lines, {1: 1, 2: 1})
        self.assertEqual((team_a["batting"]["hits"], team_b["pitching"]["wins"]), (2, 1))

    def test_unofficial_kinds_flagged(self):
        cats = {c["label"]: c for c in mlbstats.poststats_catalog(1930)}
        self.assertFalse(cats["Independent clubs (unofficial)"]["official"])
        self.assertTrue(cats["NNL postseason series"]["official"])

    def test_bundled_retrosheet_totals(self):
        cats = [c for c in mlbstats.poststats_catalog(1885)]
        self.assertTrue(cats and not cats[0]["available"] and "1885" in cats[0]["reason"])
        [cat] = mlbstats.poststats_catalog(1900)
        lines, games = mlbstats.poststats_lines(1900, [cat])
        self.assertEqual(games, {"R:BRO": 4, "R:PIT": 4})
        self.assertTrue(any(ln["name"] == "Fielder Jones" for ln in lines))


def _gd_sched_game(pk, date, home_id, away_id, home_score, away_score, state="Final", game_type="R", game_number=1):
    abstract = {"Final": "Final", "In Progress": "Live", "Postponed": "Final"}.get(state, "Preview")
    return {"gamePk": pk, "officialDate": date, "gameType": game_type, "gameNumber": game_number,
            "doubleHeader": "N", "seriesGameNumber": 2,
            "status": {"abstractGameState": abstract, "detailedState": state},
            "teams": {"home": {"team": {"id": home_id, "name": f"Team {home_id}"}, "score": home_score},
                      "away": {"team": {"id": away_id, "name": f"Team {away_id}"}, "score": away_score}}}


def _gd_pitch(n, balls, strikes, call, is_ball=False, is_strike=False, in_play=False, speed=None, hit=None):
    event = {"isPitch": True, "type": "pitch", "pitchNumber": n, "count": {"balls": balls, "strikes": strikes},
             "details": {"description": call, "isBall": is_ball, "isStrike": is_strike, "isInPlay": in_play,
                         "type": {"description": "Four-Seam Fastball"}}}
    if speed is not None:
        event["pitchData"] = {"startSpeed": speed, "zone": 5, "breaks": {"spinRate": 2300}}
    if hit is not None:
        event["hitData"] = hit
    return event


def _gd_play(index, inning, half, batter, event, events, runners=(), scoring=False, away=0, home=0, outs=1):
    return {"about": {"atBatIndex": index, "inning": inning, "halfInning": half, "isScoringPlay": scoring,
                      "isComplete": True},
            "result": {"event": event, "description": f"{batter} {event.lower()}.", "rbi": 1 if scoring else 0,
                       "awayScore": away, "homeScore": home},
            "count": {"balls": 1, "strikes": 2, "outs": outs},
            "matchup": {"batter": {"fullName": batter}, "batSide": {"code": "L"},
                        "pitcher": {"fullName": "Pitcher P"}, "pitchHand": {"code": "R"}},
            "playEvents": events, "runners": list(runners)}


def _gd_feed(plays, state="Final"):
    return {"gameData": {"game": {"pk": 1, "type": "W"}, "datetime": {"officialDate": "2024-10-25"},
                         "status": {"abstractGameState": state, "detailedState": state},
                         "teams": {"away": {"id": 147, "name": "New York Yankees", "abbreviation": "NYY"},
                                   "home": {"id": 119, "name": "Los Angeles Dodgers", "abbreviation": "LAD"}},
                         "venue": {"name": "Dodger Stadium"}},
            "liveData": {"linescore": {"scheduledInnings": 9,
                                       "innings": [{"num": 1, "away": {"runs": 0, "hits": 1}, "home": {"runs": 1}}],
                                       "teams": {"away": {"runs": 0, "hits": 1, "errors": 0},
                                                 "home": {"runs": 1, "hits": 1, "errors": 0}}},
                         "decisions": {"winner": {"fullName": "Win Ner"}, "loser": {"fullName": "Los Er"}},
                         "plays": {"allPlays": plays}}}


class Gameday(unittest.TestCase):
    def test_team_schedule(self):
        payload = {"dates": [
            {"date": "2024-04-01", "games": [_gd_sched_game(1, "2024-04-01", 147, 111, 5, 3),
                                             _gd_sched_game(2, "2024-04-01", 111, 147, None, None, state="Postponed")]},
            {"date": "2024-04-02", "games": [_gd_sched_game(3, "2024-04-02", 111, 147, 6, 2)]},
            {"date": "2024-10-25", "games": [_gd_sched_game(4, "2024-10-25", 119, 147, None, None,
                                                         state="Scheduled", game_type="W")]},
            {"date": "2024-04-01", "games": [_gd_sched_game(1, "2024-04-01", 147, 111, 5, 3)]},  # listed twice
        ]}
        games = mlbstats.parse_team_schedule(payload, 147)
        self.assertEqual([g["pk"] for g in games], [1, 3, 4])   # postponed dropped, duplicate kept once
        self.assertEqual((games[0]["home"], games[0]["result"], games[0]["opponent"]), (True, "W", "Team 111"))
        self.assertEqual((games[1]["home"], games[1]["runs"], games[1]["opp_runs"], games[1]["result"]),
                         (False, 2, 6, "L"))
        self.assertEqual((games[2]["state"], games[2]["round"], games[2]["result"]), ("scheduled", "World Series", ""))

    def test_game_feed(self):
        hr = {"launchSpeed": 109.8, "launchAngle": 30.0, "totalDistance": 413.0, "trajectory": "fly_ball",
              "location": "9"}
        plays = [
            _gd_play(0, 1, "top", "Aaron Judge", "Strikeout", [
                {"type": "action", "details": {"event": "Batter Timeout", "description": "Batter Timeout."}},
                _gd_pitch(1, 0, 1, "Called Strike", is_strike=True, speed=95.1),
                {"type": "action", "details": {"event": "Stolen Base 2B", "description": "Soto steals 2nd."}},
                _gd_pitch(2, 1, 1, "Ball", is_ball=True, speed=88.0),
                {"type": "no_pitch", "details": {"code": "N"}, "count": {"balls": 1, "strikes": 1}},
                _gd_pitch(3, 1, 2, "Swinging Strike", is_strike=True, speed=96.0),
            ]),
            _gd_play(1, 1, "bottom", "Freddie Freeman", "Home Run",
                  [_gd_pitch(1, 0, 0, "In play, run(s)", in_play=True, speed=92.2, hit=hr)],
                  runners=[{"movement": {"start": None, "end": "score"},
                            "details": {"runner": {"fullName": "Freddie Freeman"}, "event": "Home Run",
                                        "earned": True, "rbi": True}},
                           {"movement": {"start": "1B", "end": None, "isOut": True, "outBase": "2B"},
                            "details": {"runner": {"fullName": "Mookie Betts"}, "event": "Caught Stealing"}}],
                  scoring=True, home=1),
        ]
        game = mlbstats.parse_gameday(_gd_feed(plays))
        self.assertEqual((game["state"], game["round"], game["venue"]), ("final", "World Series", "Dodger Stadium"))
        self.assertEqual(game["decisions"], {"W": "Win Ner", "L": "Los Er"})
        self.assertEqual((game["away"]["abbr"], game["home"]["runs"]), ("NYY", 1))
        judge, freeman = game["plays"]
        self.assertEqual([q["n"] for q in judge["pitches"]], [1, 2, 3])  # the bare "N" marker is dropped
        self.assertEqual(judge["notes"], [(1, "Soto steals 2nd.")])     # the timeout is noise
        self.assertEqual(judge["pitches"][1]["count"], "1-1")
        self.assertEqual(freeman["hit"]["ev"], 109.8)
        self.assertEqual(freeman["hit"]["trajectory"], "fly ball")
        batter, runner = freeman["runners"]
        self.assertTrue(batter["batter"] and batter["scored"] and batter["rbi"])
        self.assertEqual((runner["start"], runner["out"], runner["out_base"]), ("1st", True, "2nd"))
        self.assertTrue(game["has_speed"] and game["has_statcast"])
        self.assertEqual(game["sequences"], "full")  # 4 pitches over 2 plate appearances

        headers, rows, pitch_headers, pitch_rows = mlbstats.gameday_rows(game)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(pitch_rows), 4)
        self.assertEqual(rows[1][headers.index("Exit velo")], 109.8)
        self.assertEqual(pitch_rows[0][pitch_headers.index("MPH")], 95.1)

    def test_who_is_on_base(self):
        def run(rid, name, start, end, at, out=False):
            return {"movement": {"start": start, "end": end, "isOut": out},
                    "details": {"runner": {"id": rid, "fullName": name}, "playIndex": at}}
        steal = {"type": "action", "index": 1, "details": {"event": "Stolen Base 2B", "description": "Ann steals 2nd.",
                                                           "awayScore": 0, "homeScore": 0}}
        plays = [
            _gd_play(0, 1, "top", "Ann A", "Single", [], runners=[run(1, "Ann A", None, "1B", 0)], outs=0),
            # Ann steals 2nd during Bea's at bat, then goes 2nd -> 3rd -> home on Bea's double
            _gd_play(1, 1, "top", "Bea B", "Double", [_gd_pitch(1, 0, 0, "Ball", is_ball=True), steal,
                                                      _gd_pitch(2, 1, 0, "In play", in_play=True)],
                     runners=[run(1, "Ann A", "1B", "2B", 1), run(2, "Bea B", None, "2B", 2),
                              run(1, "Ann A", "2B", "3B", 2), run(1, "Ann A", "3B", "score", 2)], outs=0),
            _gd_play(2, 1, "top", "Cat C", "Strikeout", [], runners=[run(3, "Cat C", None, None, 0, out=True)], outs=3),
            # extra innings: the automatic runner on 2nd turns up only in the runner movements
            _gd_play(3, 10, "bottom", "Dee D", "Single", [],
                     runners=[run(5, "Eve E", "2B", "3B", 0), run(4, "Dee D", None, "1B", 0)], outs=0),
        ]
        game = mlbstats.parse_gameday(_gd_feed(plays))
        ann, bea, cat, dee = game["plays"]
        self.assertEqual(ann["bases"], ("Ann A", "", ""))
        self.assertEqual(bea["note_bases"], [("", "Ann A", "")])     # after the steal
        self.assertEqual(bea["bases"], ("", "Bea B", ""))            # Ann scored
        self.assertEqual((bea["note_runs"], bea["play_runs"]), ([0], 1))  # she scored on the double, not the steal
        self.assertEqual(bea["note_scores"], [(0, 0)])
        self.assertEqual(bea["note_labels"], [""])                  # the steal moved a runner: no short name
        self.assertEqual(cat["bases"], ("", "", ""))                 # side retired, nobody stays on
        self.assertEqual(dee["bases_before"], ("", "Eve E", ""))
        self.assertEqual(dee["bases"], ("Dee D", "", "Eve E"))
        self.assertIsNone(game["live"])
        # MLB's own after-the-play runners win (here a pinch runner MLB never moved)
        plays[0]["matchup"]["postOnFirst"] = {"fullName": "Pinch Runner"}
        self.assertEqual(mlbstats.parse_gameday(_gd_feed(plays[:1]))["plays"][0]["bases"], ("Pinch Runner", "", ""))
        feed = _gd_feed(plays[:1], state="Live")
        feed["liveData"]["linescore"].update(outs=1, balls=2, strikes=1, offense={
            "first": {"fullName": "Ann A"}, "third": {"fullName": "Zed Z"}, "batter": {"fullName": "Bea B"}},
            defense={"pitcher": {"fullName": "Pat P"}})
        self.assertEqual(mlbstats.parse_gameday(feed)["live"],
                         {"bases": ("Ann A", "", "Zed Z"), "outs": 1, "balls": 2, "strikes": 1, "batter": "Bea B",
                          "pitcher": "Pat P"})

    def test_zone_location_in_the_batters_terms(self):
        self.assertEqual(mlbstats.zone_location(1, "R"), "Up & in")      # high, third-base side
        self.assertEqual(mlbstats.zone_location(1, "L"), "Up & away")
        self.assertEqual(mlbstats.zone_location(5, "R"), "Middle")
        self.assertEqual(mlbstats.zone_location(8, "L"), "Down")
        self.assertEqual(mlbstats.zone_location(6, "R"), "Away")
        self.assertEqual(mlbstats.zone_location(14, "R"), "Out: down & away")
        self.assertEqual(mlbstats.zone_location(11, "L"), "Out: up & away")
        self.assertEqual(mlbstats.zone_location(3, ""), "Up & 1B side")
        self.assertEqual(mlbstats.zone_location(None, "R"), "")

    def test_break_in_words(self):
        self.assertEqual(mlbstats.break_side(6.3, "R"), "6.3 arm side")      # right-hander's fastball
        self.assertEqual(mlbstats.break_side(-7.0, "R"), "7.0 glove side")   # right-hander's slider
        self.assertEqual(mlbstats.break_side(-6.7, "L"), "6.7 arm side")     # left-hander's fastball
        self.assertEqual(mlbstats.break_side(None, "R"), "")
        self.assertEqual(mlbstats.vertical_break(16.0), "16.0 up")
        self.assertEqual(mlbstats.vertical_break(-5.2), "5.2 down")

    def test_partial_and_missing_sequences(self):
        lone = lambda i: _gd_play(i, 1, "top", f"B{i}", "Batter Out", [_gd_pitch(1, 0, 0, "In play, out(s)", in_play=True)])
        full = _gd_play(9, 1, "top", "B9", "Walk", [_gd_pitch(n, n, 0, "Ball", is_ball=True) for n in range(1, 5)])
        game = mlbstats.parse_gameday(_gd_feed([lone(0), lone(1), lone(2), full]))  # 7 pitches / 4 PA
        self.assertEqual(game["sequences"], "partial")
        self.assertFalse(mlbstats.play_has_sequence(game, game["plays"][0]))
        self.assertTrue(mlbstats.play_has_sequence(game, game["plays"][3]))
        _h, rows, _ph, pitch_rows = mlbstats.gameday_rows(game)
        self.assertEqual(rows[0][8], "")          # no made-up 0-0 count
        self.assertEqual(len(pitch_rows), 4)      # only the walk's pitches
        game = mlbstats.parse_gameday(_gd_feed([]))  # before ~1950: line score only
        self.assertEqual((game["sequences"], game["plays"], game["innings"][0]["home"]["runs"]), ("none", [], 1))


class Teamstats(unittest.TestCase):
    """Team-level rows: MLB's own team figures, or (Sabermetrics, Expected) rolled up from players."""

    def setUp(self):
        self.real = {n: getattr(mlbstats, n) for n in ("api_get", "get_player_stats")}
        mlbstats._NO_TEAM_ROW.clear()

    def tearDown(self):
        for name, fn in self.real.items():
            setattr(mlbstats, name, fn)

    def test_columns_drop_the_player_name_and_position(self):
        cols = mlbstats.team_view_columns("Fielding")
        self.assertNotIn("_name", [k for _h, k in cols])
        self.assertNotIn("_pos", [k for _h, k in cols])
        self.assertEqual(cols[0], ("G", "gamesPlayed"))

    def test_league_rows_come_from_one_request_filtered_to_the_teams_asked(self):
        asked = []
        def api_get(path, params=None, ttl=None, base=None):
            asked.append(path)
            return {"stats": [{"splits": [
                {"team": {"id": 1, "name": "One"}, "stat": {"runs": 700}},
                {"team": {"id": 2, "name": "Two"}, "stat": {"runs": 650}},
                {"team": {"id": 9, "name": "Elsewhere"}, "stat": {"runs": 600}}]}]}
        mlbstats.api_get = api_get
        rows = mlbstats.get_league_team_rows(2025, "Batting", [2, 1])
        self.assertEqual(asked, ["/teams/stats"])
        self.assertEqual([(r["_team"], r["runs"], r["_season"]) for r in rows], [("Two", 650, 2025), ("One", 700, 2025)])

    def test_sabermetrics_is_rolled_up_weighted_by_playing_time(self):
        saber = [{"_id": 1, "war": 4.0, "woba": 0.400, "sd": 0}, {"_id": 2, "war": -1.0, "woba": 0.250, "sd": 0}]
        base = [{"_id": 1, "plateAppearances": 600}, {"_id": 2, "plateAppearances": 200}]
        mlbstats.get_player_stats = lambda tid, season, group, ttl=None, stat_type="season": \
            saber if stat_type == "sabermetrics" else base
        mlbstats.api_get = lambda path, params=None, ttl=None, base=None: {
            "stats": [{"splits": [{"team": {"id": 5, "name": "Fives"}, "stat": {"runs": 1}}]}]}
        row = mlbstats.get_team_view_row(5, 2025, "Batting (Sabermetrics)")
        self.assertAlmostEqual(row["war"], 3.0)                               # added up
        self.assertAlmostEqual(row["woba"], (0.400 * 600 + 0.250 * 200) / 800)  # weighted by PA
        self.assertEqual((row["_team"], row["_season"]), ("Fives", 2025))

    def test_expected_starts_in_2015_and_missing_seasons_are_skipped(self):
        self.assertIsNone(mlbstats.get_team_view_row(5, 2010, "Batting (Expected)"))
        def api_get(path, params=None, ttl=None, base=None):
            if params["season"] == 1950:
                raise mlbstats.MLBStatsError("HTTP 404")
            return {"stats": [{"splits": [{"team": {"id": 5, "name": f"Fives {params['season']}"},
                                           "stat": {"runs": 1}}]}]}
        mlbstats.api_get = api_get
        rows = mlbstats.get_team_history_rows(5, "Batting", [1949, 1950, 1951])
        self.assertEqual([r["_season"] for r in rows], [1949, 1951])
        self.assertIn((5, 1950, "hitting", "season"), mlbstats._NO_TEAM_ROW)  # not asked again this session


class PitchArsenalRanking(unittest.TestCase):
    """league-seasons with pitchers: the main pitch, lowest-first ranking, the best per pitch type."""

    def setUp(self):
        self.real = {n: getattr(mlbstats, n) for n in ("get_league_view_rows", "get_team_games_played",
                                                       "get_pitch_arsenal", "get_team_map")}
        def row(pid, name, runs, outs):
            return {"_id": pid, "_name": name, "_team_id": 1, "_num_teams": 1, "_league": "AL",
                    "runs": runs, "earnedRuns": runs, "outs": outs, "inningsPitched": f"{outs // 3}.0"}
        mlbstats.get_league_view_rows = lambda season, view, ttl=None: [
            row(1, "Ace Fastball", 40, 600), row(2, "Bob Fastball", 60, 600),
            row(3, "Cal Slider", 50, 600), row(4, "Dee Reliever", 5, 150)]
        mlbstats.get_team_games_played = lambda season, ttl=None: {1: 162}
        mix = {1: ("FF", "Four-seam FB"), 2: ("FF", "Four-seam FB"), 3: ("SL", "Slider"), 4: ("SL", "Slider")}
        self.asked = []
        def arsenal(pid, season, ttl=None):
            self.asked.append(pid)
            code, name = mix[pid]
            return [{"code": code, "name": name, "share": 0.5, "count": 100, "speed": 90.0}]
        mlbstats.get_pitch_arsenal = arsenal
        mlbstats.get_team_map = lambda season, ttl=None: {1: {"id": 1, "name": "Club"}}

    def tearDown(self):
        for n, f in self.real.items():
            setattr(mlbstats, n, f)

    def test_parse_arsenal_most_thrown_first(self):
        payload = {"stats": [{"splits": [
            {"stat": {"percentage": 0.2, "count": 20, "averageSpeed": 85, "type": {"code": "SL", "description": "Slider"}}},
            {"stat": {"percentage": 0.8, "count": 80, "averageSpeed": 95, "type": {"code": "FF", "description": "Four-seam FB"}}}]}]}
        got = mlbstats.parse_pitch_arsenal(payload)
        self.assertEqual([p["code"] for p in got], ["FF", "SL"])
        self.assertAlmostEqual(got[0]["share"], 0.8)

    def test_best_per_primary_pitch(self):
        q = infoquery.parse_query({
            "name": "t", "source": "league-seasons", "pitchers": True, "season": 2015,
            "where": ["{Pitching: IP} >= {Team: G}"], "rank_by": "{Pitching: R} * 27 / {Pitching: Outs}",
            "order": "lowest", "each": "primary pitch", "top": 1, "x": "rank",
            "series": [{"name": "R9", "formula": "{Pitching: R} * 27 / {Pitching: Outs}"},
                       {"name": "MLB", "formula": "{MLB: R/9}"}]})
        self.assertEqual(infoquery.query_to_json(q)["each"], "primary pitch")
        r = infoquery.run_query(q, season_now=2026)
        self.assertEqual([p["xlabel"] for p in r["series"][0]["points"]],
                         ["1. Four-seam FB: Ace Fastball, 2015", "2. Slider: Cal Slider, 2015"])
        self.assertAlmostEqual(r["series"][0]["points"][0]["y"], 1.8)
        self.assertAlmostEqual(r["series"][1]["points"][0]["y"], 155 * 27 / 1950)
        self.assertNotIn(4, self.asked)  # the non-qualifier's pitch mix is never fetched

    def test_each_needs_pitchers(self):
        with self.assertRaises(infoquery.QueryError):
            infoquery.parse_query({"name": "t", "source": "league-seasons", "season": 2015, "rank_by": "1",
                                   "each": "primary pitch", "series": [{"formula": "1"}]})


class NaturalZones(unittest.TestCase):
    """naturalzone: the Total Zone map of the field, and zones grown or shrunk by range."""

    def test_zone_codes_name_their_fielders(self):
        self.assertEqual(naturalzone.zone_fielders("56D"), ["3B", "SS"])
        self.assertEqual(naturalzone.zone_fielders("6MS"), ["SS"])
        self.assertEqual(naturalzone.zone_fielders("78XD"), ["LF", "CF"])
        codes = {z[0] for z in naturalzone.zone_list()}
        for code in ("5S", "56", "6M", "4MD", "34S", "3D", "7LS", "89XD", "9LD", "1S", "25", "23", "13"):
            self.assertIn(code, codes)

    def test_average_zones_mirror_and_border_each_other(self):
        avg = naturalzone.average_zones()
        a, c = avg["areas"], avg["contiguity"]
        self.assertAlmostEqual(a["3B"], a["1B"])
        self.assertAlmostEqual(a["SS"], a["2B"])
        self.assertAlmostEqual(a["LF"], a["RF"])
        for x in naturalzone.POSITIONS:
            for y in naturalzone.POSITIONS:
                self.assertAlmostEqual(c[x][y], c[y][x])
        self.assertGreater(c["3B"]["SS"], 0)
        self.assertGreater(c["SS"]["LF"], 0)
        self.assertEqual(c["3B"]["2B"], 0)
        self.assertEqual(c["LF"]["RF"], 0)

    def test_average_defense_reproduces_the_zones(self):
        r = naturalzone.natural_zones({p: 1.0 for p in naturalzone.POSITIONS})
        self.assertEqual(r["totals"]["gap"], 0)
        self.assertEqual(r["totals"]["overlap"], 0)
        self.assertAlmostEqual(r["coverage"]["SS"]["SS"], 1.0)

    def test_weak_and_strong_neighbours(self):
        r = naturalzone.natural_zones({"SS": 0.85, "3B": 1.2})
        self.assertAlmostEqual(r["areas"]["SS"] / r["avg_areas"]["SS"], 0.85, places=2)
        self.assertAlmostEqual(r["areas"]["3B"] / r["avg_areas"]["3B"], 1.2, places=2)
        self.assertGreater(r["coverage"]["SS"]["3B"], 0.05)   # the third baseman reaches into short
        self.assertGreater(r["made_up"]["SS"], 0)
        by = r["made_up_by"]["SS"]                             # and every bit made up is credited to someone
        self.assertIn("3B", by)
        self.assertGreaterEqual(sum(by.values()) + 1e-9, r["made_up"]["SS"])
        for zone in naturalzone.POSITIONS:
            self.assertEqual(bool(r["made_up"][zone]), bool(r["made_up_by"][zone]))
            # made up alone (no overlap) and where neighbours overlap: together, all of it
            together = sum(r["made_up_alone"][zone].values()) + sum(r["made_up_shared"][zone].values())
            self.assertAlmostEqual(together, r["made_up"][zone])
        self.assertGreater(r["gap"]["SS"], 0)                  # but not everywhere short gave up
        self.assertEqual(r["gap"]["3B"], 0)
        tot = r["totals"]
        self.assertGreater(tot["made_up_alone"], 0)
        made = sum(r["made_up"][z] * r["avg_areas"][z] for z in naturalzone.POSITIONS)
        self.assertAlmostEqual(tot["made_up_alone"] + tot["made_up_shared"], made, delta=1)
        self.assertLessEqual(tot["made_up_shared"], tot["overlap"])

    def test_a_weak_defense_leaves_gaps(self):
        r = naturalzone.natural_zones({p: 0.9 for p in naturalzone.POSITIONS}, fixed=("P", "C"))
        self.assertGreater(r["totals"]["gap"], 0.05 * r["totals"]["field"])
        self.assertEqual(r["reach"]["P"], 0.0)  # held
        free = naturalzone.natural_zones({p: 0.9 for p in naturalzone.POSITIONS})
        self.assertLess(free["reach"]["P"], 0.0)  # not held unless asked


class Stadiums(unittest.TestCase):
    def test_the_alternate_parks_list_each_wikipedia_era_then_mlbs_other_parks(self):
        parks = {"3": {"article": "Fenway Park", "revid": 1, "eras": [
            {"from": 1912, "to": 1933, "marks": [], "note": "before the rebuilding"},
            {"from": 1995, "to": 2100, "marks": [], "note": "1995"}]}}
        venues = [{"id": 3, "name": "Fenway Park"},
                  {"id": 15, "name": "Chase Field", "fieldInfo": {"leftLine": 330, "center": 407, "rightLine": 335}},
                  {"id": 99, "name": "No Distances", "fieldInfo": {}}]
        got = stadiums.parse_alternate_parks(parks, venues)
        self.assertEqual([p["label"] for p in got], ["Chase Field — MLB's posted distances",
                                                     "Fenway Park, 1912–1933 — before the rebuilding",
                                                     "Fenway Park, 1995 on"])
        self.assertEqual((got[1]["venue_id"], got[1]["season"]), (3, 1912))
        self.assertIsNone(got[0]["season"])

    """stadiums + naturalzone's parks: a real park's fence against the uniform park."""

    PAYLOAD = {"teams": [
        {"id": 2, "name": "B Club", "venue": {"id": 20, "name": "Big Park", "fieldInfo": {
            "leftLine": 340, "leftCenter": 385, "center": 430, "rightCenter": 385, "rightLine": 340}}},
        {"id": 1, "name": "A Club", "venue": {"id": 10, "name": "Old Park", "fieldInfo": {"capacity": 30000}}}]}

    def test_posted_distances_become_a_fence(self):
        parks = stadiums.parse_home_parks(self.PAYLOAD)
        self.assertEqual([p["team"] for p in parks], ["A Club", "B Club"])
        self.assertIsNone(parks[0]["fence"])  # no distances: no fence
        fence = parks[1]["fence"]
        self.assertEqual(fence, ((-45.0, 340.0), (-22.5, 385.0), (0.0, 430.0), (22.5, 385.0), (45.0, 340.0)))
        self.assertAlmostEqual(naturalzone.fence_at(fence, -33.75), 362.5)
        self.assertEqual(naturalzone.fence_at(fence, 50), 340.0)
        self.assertEqual([m[0] for m in stadiums.marks(parks[1]["field"])], ["LF line", "LCF", "CF", "RCF", "RF line"])

    def test_a_bigger_park_gives_the_outfield_more_ground(self):
        fence = stadiums.parse_home_parks(self.PAYLOAD)[1]["fence"]
        cmp = naturalzone.compare_park(fence)
        for pos in ("P", "C", "1B", "2B", "SS"):
            self.assertEqual(cmp["gained"][pos], 0)
        for pos in naturalzone.POSITIONS:
            self.assertEqual(cmp["lost"][pos], 0)  # deeper everywhere
        self.assertGreater(cmp["gained"]["CF"], 0)
        self.assertAlmostEqual(cmp["gained"]["LF"], cmp["gained"]["RF"])
        self.assertAlmostEqual(cmp["areas"]["park"] - cmp["areas"]["uniform"], sum(cmp["gained"].values()))
        here, uniform = naturalzone.average_zones(fence)["areas"], naturalzone.average_zones()["areas"]
        self.assertAlmostEqual(here["CF"] - uniform["CF"], cmp["gained"]["CF"])
        self.assertEqual(here["SS"], uniform["SS"])
        r = naturalzone.natural_zones({"CF": 1.1}, park=fence)
        self.assertEqual(r["avg_areas"]["CF"], here["CF"])
        self.assertAlmostEqual(r["areas"]["CF"] / here["CF"], 1.1, places=2)

    def test_wikipedia_eras_come_first_then_mlb(self):
        parks = {"20": {"article": "Big Park", "revid": 7, "eras": [
            {"from": 1990, "to": 1999, "marks": [["LF line", -45, 350], ["CF", 0, 440], ["RF line", 45, 350]], "note": "1990"}]}}
        pick = lambda season: stadiums.parse_home_parks(self.PAYLOAD, season, parks)[1]
        old = pick(1995)
        self.assertEqual(old["source"], "wikipedia")
        self.assertEqual(old["fence"], ((-45, 350), (0, 440), (45, 350)))
        self.assertEqual(old["wiki"]["url"], "https://en.wikipedia.org/wiki/Big_Park")
        self.assertIn("oldid=7", old["wiki"]["revision_url"])
        later = pick(2005)                       # no era for 2005: MLB's distances
        self.assertEqual(later["source"], "mlb")
        self.assertEqual(later["fence"][2], (0.0, 430.0))
        self.assertIsNone(stadiums.parse_home_parks(self.PAYLOAD, 1995, parks)[0]["source"])  # neither

    def test_the_wikipedia_file_is_well_formed(self):
        parks = stadiums.wikipedia_parks()
        self.assertGreater(len(parks), 50)
        for vid, park in parks.items():
            self.assertTrue(park["article"] and park["revid"])
            eras = sorted(park["eras"], key=lambda e: e["from"])
            for e, nxt in zip(eras, eras[1:]):
                self.assertLess(e["to"], nxt["from"], (vid, e))   # no overlaps
            for e in eras:
                angles = [m[1] for m in e["marks"]]
                self.assertEqual(angles, sorted(angles))
                self.assertEqual((angles[0], angles[-1]), (-45, 45))
                self.assertTrue(all(150 <= m[2] <= 650 for m in e["marks"]), (vid, e))

    def test_a_short_line_loses_ground(self):
        # the uniform park's own fence, but 310 ft down the left-field line
        fence = ((-45.0, 310.0),) + tuple(pt for pt in naturalzone.UNIFORM_FENCE if pt[0] > -45)
        cmp = naturalzone.compare_park(fence)
        self.assertGreater(cmp["lost"]["LF"], 0)
        self.assertEqual(cmp["lost"]["RF"], 0)
        self.assertEqual(naturalzone.bounds(fence), naturalzone.bounds())  # fits the uniform grid


class SafeOut(unittest.TestCase):
    """retrosheet.parse_team_running: reaching each base, however a player got there."""

    HEADER = ["gid", "inning", "top_bot", "gametype", "batteam", "pitteam", "batter", "outs_pre", "outs_post",
              "br1_pre", "br2_pre", "br3_pre", "br1_post", "br2_post", "br3_post", "run_b", "run1", "run2", "run3"]

    def play(self, inning, batter, outs, pre, post, runs=("", "", "", ""), top="0", gid="G1"):
        return [gid, str(inning), top, "regular", "AAA", "BBB", batter, str(outs[0]), str(outs[1]),
                *pre, *post, *runs]

    def test_an_inning(self):
        rows = [self.HEADER,
                # a doubles: reaches first and second
                self.play(1, "a", (0, 0), ("", "", ""), ("", "a", "")),
                # b grounds into a force at third: b is safe at first, a never reaches third
                self.play(1, "b", (0, 1), ("", "a", ""), ("b", "", "")),
                # c homers: c and b score -- c reaches every base, b second and third on the way
                self.play(1, "c", (1, 1), ("b", "", ""), ("", "", ""), runs=("c", "b", "", "")),
                # d and e strike out
                self.play(1, "d", (1, 2), ("", "", ""), ("", "", "")),
                self.play(1, "e", (2, 3), ("", "", ""), ("", "", ""))]
        got = retrosheet.parse_team_running(rows)["batting"]["AAA"]
        self.assertEqual((got["outs"], got["reach1"], got["reach2"], got["reach3"], got["runs"]), (3, 3, 3, 2, 2))
        rates = mlbstats.safe_out_rates(got)
        self.assertEqual(rates["outs_inn"], 3.0)
        self.assertAlmostEqual(rates["scored_on_base"], 2 / 3)
        self.assertAlmostEqual(rates["on_base_outs"], 1.0)
        # the same inning, from the side of the club in the field: what it allowed
        self.assertEqual(retrosheet.parse_team_running(rows)["fielding"]["BBB"], got)

    def test_extra_innings_runner_counts_only_from_third(self):
        rows = [self.HEADER,
                # the 10th starts with x put on second; y singles him to third, then a fly scores him
                self.play(10, "y", (0, 0), ("", "x", ""), ("y", "", "x")),
                self.play(10, "z", (0, 1), ("y", "", "x"), ("y", "", ""), runs=("", "", "", "x"))]
        got = retrosheet.parse_team_running(rows)["batting"]["AAA"]
        self.assertEqual((got["reach1"], got["reach2"], got["reach3"], got["runs"], got["placed"]), (1, 0, 1, 1, 1))

    def test_a_batter_twice_in_an_inning_is_two_trips(self):
        rows = [self.HEADER,
                self.play(1, "a", (0, 0), ("", "", ""), ("", "", ""), runs=("a", "", "", "")),  # homer
                self.play(1, "a", (0, 0), ("", "", ""), ("a", "", ""))]                         # later, a single
        got = retrosheet.parse_team_running(rows)["batting"]["AAA"]
        self.assertEqual((got["reach1"], got["runs"]), (2, 1))


def _box_player(pid, name, order=None, pos="CF", batting=None, pitching=None, current=False):
    p = {"person": {"id": pid, "fullName": name}, "position": {"abbreviation": pos},
         "allPositions": [{"abbreviation": x} for x in pos.split("-")],
         "stats": {"batting": batting or {}, "pitching": pitching or {}},
         "seasonStats": {"batting": {"avg": ".300", "ops": ".900"}, "pitching": {"era": "3.00"}},
         "gameStatus": {"isCurrentBatter": current, "isCurrentPitcher": current}}
    if order:
        p["battingOrder"] = order
    return p


class Boxscore(unittest.TestCase):
    def test_parse(self):
        bat = {"atBats": 4, "runs": 1, "hits": 2, "rbi": 1, "baseOnBalls": 0, "strikeOuts": 1, "leftOnBase": 2}
        side = {"batters": [3, 1, 2, 9],
                "pitchers": [9, 8],
                "players": {"ID1": _box_player(1, "Lead Off", "100", batting=bat),
                            "ID2": _box_player(2, "Pinch Hit", "101", "PH-LF", batting=dict(bat, atBats=1)),
                            "ID3": _box_player(3, "Clean Up", "400", "1B", batting=bat, current=True),
                            "ID9": _box_player(9, "Start Er", None, "P",
                                               pitching={"inningsPitched": "5.2", "hits": 3, "note": "(W, 1-0)"}),
                            "ID8": _box_player(8, "Re Liever", None, "P", pitching={"inningsPitched": "3.1", "hits": 1})},
                "note": [{"label": "a", "value": "Singled for Lead Off in the 8th."}],
                "info": [{"title": "BATTING", "fieldList": [{"label": "2B", "value": "Off, L (3)."}]}]}
        feed = {"liveData": {"boxscore": {"teams": {"away": side, "home": {}},
                                          "pitchingNotes": ["Er pitched to 1 batter in the 6th."],
                                          "info": [{"label": "Att", "value": "40,000."}, {"label": "May 1"}]}}}
        box = mlbstats.parse_boxscore(feed)
        away = box["away"]
        self.assertEqual([b["name"] for b in away["batters"]], ["Lead Off", "Pinch Hit", "Clean Up"])  # pitcher never batted
        self.assertEqual([(b["order"], b["sub"], b["pos"]) for b in away["batters"]],
                         [(1, False, "CF"), (1, True, "PH-LF"), (4, False, "1B")])
        self.assertTrue(away["batters"][2]["current"])
        self.assertEqual(away["batting_totals"]["atBats"], 9)
        self.assertEqual(away["pitching_totals"]["inningsPitched"], "9.0")  # 17 + 10 outs
        self.assertEqual(away["pitching_totals"]["hits"], 4)
        self.assertEqual(away["pitchers"][0]["note"], "(W, 1-0)")
        self.assertEqual(away["notes"], ["a-Singled for Lead Off in the 8th."])
        self.assertEqual(away["info"], [("BATTING", [("2B", "Off, L (3).")])])
        self.assertEqual(box["home"]["batters"], [])
        self.assertEqual(box["info"], [("Att", "40,000.")])  # the bare date line is dropped
        self.assertEqual(box["pitching_notes"], ["Er pitched to 1 batter in the 6th."])

    def test_gameday_carries_box(self):
        self.assertIn("box", mlbstats.parse_gameday(_gd_feed([])))

    def test_a_pitcher_listed_twice_among_the_batters_bats_once(self):
        # MLB's old box scores (pre-1950 or so) repeat the starting pitcher at the end of "batters"
        def man(pid, name, order, ab):
            return {"person": {"id": pid, "fullName": name}, "battingOrder": order,
                    "allPositions": [{"abbreviation": "P" if order == "900" else "SS"}],
                    "stats": {"batting": {"atBats": ab, "hits": 1}}}
        side = {"batters": [1, 2, 1], "pitchers": [1],
                "players": {"ID1": man(1, "Rosy Ryan", "900", 1), "ID2": man(2, "Dave Bancroft", "100", 5)}}
        box = mlbstats._box_side(side)
        self.assertEqual([b["name"] for b in box["batters"]], ["Dave Bancroft", "Rosy Ryan"])
        self.assertEqual(box["batting_totals"]["atBats"], 6)

    def test_ip(self):
        self.assertEqual((mlbstats.ip_outs("5.2"), mlbstats.ip_outs("0.0"), mlbstats.outs_ip(20)), (17, 0, "6.2"))


def _wp_play(index, inning, half, wp, added, batter="B", bid=1, pitcher="P", pid=2):
    return {"atBatIndex": index, "about": {"inning": inning, "halfInning": half, "isComplete": True},
            "result": {"event": "Single", "description": f"{batter} singles.", "awayScore": 0, "homeScore": 0},
            "matchup": {"batter": {"id": bid, "fullName": batter}, "pitcher": {"id": pid, "fullName": pitcher}},
            "homeTeamWinProbability": wp, "homeTeamWinProbabilityAdded": added, "leverageIndex": 1.2}


class WinProbability(unittest.TestCase):
    def test_parse_and_players(self):
        wp = mlbstats.parse_win_probability([
            _wp_play(0, 1, "top", 46.0, -4.0, "Away Bat", 10, "Home Arm", 20),   # visitor single: away +4
            _wp_play(1, 1, "bottom", 56.0, 10.0, "Home Bat", 30, "Away Arm", 40),  # home hit: home +10
            {"atBatIndex": 2, "about": {}},  # no figures: skipped
        ])
        self.assertEqual(wp["start"], 50.0)
        self.assertEqual([p["home_wp"] for p in wp["plays"]], [46.0, 56.0])
        rows = {r["name"]: r for r in mlbstats.wpa_by_player(wp)}
        self.assertEqual((rows["Away Bat"]["bat"], rows["Away Bat"]["side"]), (4.0, "away"))
        self.assertEqual((rows["Home Arm"]["pitch"], rows["Home Arm"]["side"]), (-4.0, "home"))
        self.assertEqual(rows["Home Bat"]["total"], 10.0)
        self.assertEqual(rows["Away Arm"]["pitch"], -10.0)
        self.assertEqual(mlbstats.wpa_by_player(wp)[0]["name"], "Home Bat")

    def test_empty(self):
        self.assertEqual(mlbstats.parse_win_probability({"messageNumber": 1}), {"start": 50.0, "plays": []})

    def test_layout(self):
        import wpachart
        plays = [{"inning": 1, "half": "top"}] * 3 + [{"inning": 1, "half": "bottom"}]
        self.assertEqual(wpachart.layout_points(plays, False), [1 / 3, 2 / 3, 1.0, 2.0])
        # a half-inning still being played leaves room for plays to come
        self.assertEqual(wpachart.layout_points(plays[:1], True), [0.25])


class StatcastWall(unittest.TestCase):
    """statcastview.wall_contact: a hit that stayed in the park stops at the fence."""

    @staticmethod
    def flight(z_at_fence, angle=-30.0, reach=360.0):
        """A straight-line flight out to ``reach`` ft, rising to ``z_at_fence`` at 330 ft."""
        pts = []
        for i in range(0, 101):
            r = reach * i / 100
            z = z_at_fence * r / 330
            pts.append((i * 0.05, (r * math.sin(math.radians(angle)), r * math.cos(math.radians(angle)), z)))
        return {"points": pts, "hang": 5.0, "landing": pts[-1][1], "angle": angle, "distance": reach, "air": True}

    def test_a_double_off_a_tall_wall(self):
        out = statcastview.wall_contact(self.flight(20.0), lambda a: 330.0, "Double")
        wall = out["wall"]
        self.assertTrue(wall["taller"])
        self.assertAlmostEqual(wall["z"], 20.0, places=1)
        self.assertEqual(wall["height"], 21.0)
        self.assertLessEqual(max(math.hypot(q[0], q[1]) for _t, q in out["points"]), 330.0 + 1e-6)
        self.assertEqual(out["landing"][2], 0.0)  # dropped back down
        self.assertTrue(statcastview.wall_text(wall).startswith("Model estimates double probably hit the wall at the 20 ft mark"))

    def test_low_off_the_wall_needs_no_taller_section(self):
        out = statcastview.wall_contact(self.flight(5.0), lambda a: 330.0, "Single")
        self.assertFalse(out["wall"]["taller"])

    def test_a_home_run_under_the_drawn_fence_clears_a_lower_one(self):
        hit = self.flight(5.3)
        out = statcastview.wall_contact(hit, lambda a: 330.0, "Home Run")
        wall = out["wall"]
        self.assertTrue(wall["lower"])
        self.assertFalse(wall["taller"])
        self.assertLess(wall["height"], wall["z"])
        self.assertEqual(wall["height"], 4.5)
        self.assertEqual(out["points"], hit["points"])  # still a home run: the flight goes on
        self.assertTrue(statcastview.wall_text(wall).startswith("Model estimates home run cleared the fence near the 5 ft mark"))
        high = self.flight(20.0)
        self.assertIs(statcastview.wall_contact(high, lambda a: 330.0, "Home Run"), high)  # cleared 8 ft

    def test_a_home_run_short_of_the_drawn_fence_pulls_it_in(self):
        hit = self.flight(30.0, reach=360.0)  # MLB's 360 ft, the drawn fence 400 ft
        hit["points"][-1] = (hit["points"][-1][0], hit["points"][-1][1][:2] + (0.0,))  # comes down at 360
        out = statcastview.wall_contact(hit, lambda a: 400.0, "Home Run")
        wall = out["wall"]
        self.assertTrue(wall["nearer"])
        self.assertEqual((wall["fence_r"], wall["distance"]), (400.0, 360.0))
        self.assertLess(wall["r"], 360.0)
        self.assertGreaterEqual(wall["z"], statcastview.WALL_H + 1)
        self.assertTrue(statcastview.wall_text(wall).startswith("MLB measured this home run at 360 ft"))

    def test_outs_and_short_hits_are_left_alone(self):
        for event in ("Flyout", "Lineout"):
            hit = self.flight(20.0, reach=320.0)
            self.assertIs(statcastview.wall_contact(hit, lambda a: 330.0, event), hit)
        short = self.flight(20.0, reach=300.0)
        self.assertNotIn("wall", statcastview.wall_contact(short, lambda a: 330.0, "Double"))
        self.assertIs(statcastview.wall_contact(hit, lambda a: 330.0, "Inside The Park Home Run"), hit)

    def test_an_out_beyond_the_drawn_fence_pushes_it_back(self):
        hit = dict(self.flight(20.0, angle=10.0, reach=395.0), fielded=403.0)  # the Trop, 2026-10-13: Bazzana
        out = statcastview.wall_contact(hit, lambda a: 389.0, "Flyout")
        wall = out["wall"]
        self.assertTrue(wall["farther"])
        self.assertEqual((wall["fence_r"], wall["distance"], wall["r"]), (389.0, 403.0, 408.0))
        self.assertTrue(wall["caught"])
        self.assertEqual(out["points"], hit["points"])  # the flight itself is left as it was
        self.assertTrue(statcastview.wall_text(wall).startswith("This flyout was caught about 403 ft out"))
        dropped = statcastview.wall_contact(self.flight(20.0, reach=345.0), lambda a: 330.0, "Field Error")
        self.assertEqual(dropped["wall"]["r"], 350.0)
        self.assertFalse(dropped["wall"]["caught"])  # no catch spot: where it came down


class Statcast(unittest.TestCase):
    # a real pitch (a changeup, 2025 World Series game 7) as MLB's feed has it
    PITCH = {"startSpeed": 86.6, "endSpeed": 79.4, "strikeZoneTop": 3.52, "strikeZoneBottom": 1.64,
             "extension": 6.47, "plateTime": 0.436,
             "coordinates": {"aY": 25.4128, "aZ": -28.3511, "pX": 0.2776, "pZ": 1.4215, "vX0": 11.0433,
                             "vY0": -125.5983, "vZ0": -4.1762, "x0": -2.8610, "y0": 50.0062, "z0": 5.4111,
                             "aX": -16.1766}}

    def test_track_and_heights(self):
        track = mlbstats._parse_track(self.PITCH)
        self.assertEqual((track["x0"], track["vy0"], track["sz_top"], track["extension"]), (-2.8610, -125.5983, 3.52, 6.47))
        self.assertIsNone(mlbstats._parse_track({"startSpeed": 90.0}))  # before PITCHf/x: no flight
        players = {"ID1": {"height": "6' 2\""}, "ID2": {"height": "5' 11\""}, "ID3": {}}
        self.assertEqual([mlbstats._height_inches(players, i) for i in (1, 2, 3, 4)], [74, 71, None, None])

    def test_pitch_flight(self):
        track = mlbstats._parse_track(self.PITCH)
        path = statcastview.pitch_path(track)
        t_end, (x, y, z) = path[-1]
        # the fit run to the front of the plate lands where MLB says it crossed, in about plateTime
        self.assertAlmostEqual(x, track["px"], delta=0.3)
        self.assertAlmostEqual(z, track["pz"], delta=0.3)
        self.assertAlmostEqual(y, statcastview.PLATE_FRONT, places=3)
        self.assertAlmostEqual(t_end, 0.41, delta=0.03)
        rx, ry, rz = statcastview.release_point(track)
        self.assertAlmostEqual(ry, 60.5 - 6.47, places=3)  # released at his extension
        self.assertTrue(rx < 0 and 4.5 < rz < 6.5)          # a right-hander: the third-base side

    def test_batted_ball(self):
        contact = (0.0, statcastview.PLATE_FRONT, 2.5)
        hr = {"ev": 110.1, "la": 24.0, "dist": 442.0, "trajectory": "fly ball", "spray": (110.0, 30.0)}
        flight = statcastview.batted_path(hr, contact)
        x, y, z = flight["landing"]
        self.assertAlmostEqual(math.hypot(x, y), 442, delta=3)  # comes down at MLB's distance
        self.assertLess(z, 0.5)
        self.assertTrue(flight["air"] and 3 < flight["hang"] < 6)
        self.assertLess(flight["angle"], 0)                     # left of center: toward left field
        self.assertLessEqual(len(flight["points"]), 90)
        # a ground ball's "distance" is its first bounce: it hops out to where it was fielded
        grounder = {"ev": 95.0, "la": -8.0, "dist": 12.0, "trajectory": "ground ball", "spray": (150.0, 150.0)}
        flight = statcastview.batted_path(grounder, contact)
        fielded = statcastview.SPRAY_FT * math.hypot(150 - statcastview.SPRAY_HOME[0], statcastview.SPRAY_HOME[1] - 150)
        self.assertFalse(flight["air"])
        self.assertAlmostEqual(math.hypot(*flight["landing"][:2]), fielded, delta=3)
        self.assertGreater(flight["angle"], 0)                  # toward right field
        self.assertIsNone(statcastview.batted_path({"ev": None, "la": None}, contact))

    def test_swung(self):
        self.assertTrue(statcastview.swung({"call": "Foul"}))
        self.assertTrue(statcastview.swung({"call": "Swinging Strike (Blocked)"}))
        self.assertTrue(statcastview.swung({"call": "In play, out(s)", "in_play": True}))
        self.assertFalse(statcastview.swung({"call": "Called Strike"}))
        self.assertFalse(statcastview.swung({"call": "Ball In Dirt"}))


SIM_COLUMNS = ["gid", "inning", "top_bot", "gametype", "pa", "outs_pre", "outs_post", "batter",
               "br1_pre", "br2_pre", "br3_pre", "br1_post", "br2_post", "br3_post", "run_b", "run1", "run2", "run3",
               "lob_id1", "lob_id2", "lob_id3", "sb2", "sb3", "sbh", "cs2", "cs3", "csh",
               "single", "double", "triple", "hr", "walk", "iw", "hbp", "k", "roe", "bip", "ground", "bunt", "sh",
               "vis_home", "score_v", "score_h"]


def _sim_play(batter="bat", pre=("", "", ""), post=("", "", ""), runs=("", "", "", ""), outs=0, outs_post=None,
              pa=1, lob=("", "", ""), inning="1", **flags):
    """A plays.csv row for the Simulator's tables; ``runs`` = (batter, from 1st, 2nd, 3rd)."""
    row = dict.fromkeys(SIM_COLUMNS, "0")
    row.update(gid="G1", inning=inning, top_bot="0", gametype="regular", pa=str(pa), outs_pre=str(outs),
               outs_post=str(outs if outs_post is None else outs_post), batter=batter,
               br1_pre=pre[0], br2_pre=pre[1], br3_pre=pre[2], br1_post=post[0], br2_post=post[1], br3_post=post[2],
               run_b=runs[0], run1=runs[1], run2=runs[2], run3=runs[3], lob_id1=lob[0], lob_id2=lob[1], lob_id3=lob[2])
    row.update(flags)
    return [row[c] for c in SIM_COLUMNS]


class WallHeights(unittest.TestCase):
    def test_fenway_has_its_left_field_wall_by_era(self):
        early, now = stadiums.wikipedia_era(3, 1920), stadiums.wikipedia_era(3, 2024)
        self.assertEqual(stadiums.wall_height(early["walls"], -30, 8.0), 25.0)
        self.assertEqual(stadiums.wall_height(now["walls"], -30, 8.0), 37.17)
        self.assertEqual(stadiums.wall_height(now["walls"], -10, 8.0), 8.0)  # right of the flagpole: not given
        self.assertEqual([a["article"] for a in now["also"]], ["Green Monster"])

    def test_a_ball_off_the_monster_is_not_a_taller_wall(self):
        # a liner reaching the fence 20 ft up is below the Monster's 37 ft: no taller section for the play
        hit = {"points": [(0.0, (0.0, 0.0, 3.0)), (1.0, (-200.0, 200.0, 20.0)), (1.5, (-260.0, 260.0, 20.0))],
               "angle": -45.0, "distance": 370.0}
        monster = lambda a: 37.17 if -45 <= a <= -15 else 8.0
        out = statcastview.wall_contact(hit, lambda a: 310.0, "Double", monster)
        self.assertFalse(out["wall"]["taller"])
        self.assertEqual(out["wall"]["height"], 37.17)
        plain = statcastview.wall_contact(hit, lambda a: 310.0, "Double")
        self.assertTrue(plain["wall"]["taller"])  # against a plain 8 ft fence it would have to be taller


class MlbSurnames(unittest.TestCase):
    def test_the_game_feed_gives_mlbs_surnames(self):
        feed = {"gameData": {"game": {"pk": 1}, "players": {
            "ID800543": {"fullName": "Josue De Paula", "lastName": "De Paula", "useLastName": "De Paula"},
            "ID665489": {"fullName": "Vladimir Guerrero Jr.", "lastName": "Guerrero", "useLastName": "Guerrero Jr."}}},
            "liveData": {}}
        self.assertEqual(mlbstats.parse_gameday(feed)["surnames"],
                         {"Josue De Paula": "De Paula", "Vladimir Guerrero Jr.": "Guerrero Jr."})

    def test_a_person_carries_his_surname(self):
        self.assertEqual(mlbstats.parse_person({"fullName": "Tommy La Stella", "lastName": "La Stella",
                                                "useLastName": "La Stella"})["surname"], "La Stella")


class SimulatorTables(unittest.TestCase):
    def _sim(self, *plays):
        return retrosheet.parse_sim([SIM_COLUMNS, *plays])

    def test_a_single_records_where_everyone_ended_up(self):
        t = self._sim(_sim_play(pre=("r1", "", "r3"), post=("bat", "", "r1"), runs=("", "", "", "r3"), single="1"))
        # runners on 1st and 3rd (1 + 4), none out -> batter on first, runner from first to third, from third scored
        self.assertEqual(t["trans"]["1B"], {"5.0": {"13-H": 1}})
        self.assertEqual(t["pa"], {"5.0": 1})

    def test_a_double_play_ground_ball(self):
        t = self._sim(_sim_play(pre=("r1", "", ""), outs=0, outs_post=2, bip="1", ground="1"))
        self.assertEqual(t["trans"]["GO"], {"1.0": {"XX--": 1}})

    def test_runners_left_on_by_the_third_out_are_not_put_out(self):
        # bases empty after the play (the inning is over): those listed as left on base held
        t = self._sim(_sim_play(pre=("r1", "r2", ""), outs=2, outs_post=3, lob=("r2", "r1", ""), bip="1", ground="1"))
        self.assertEqual(t["trans"]["GO"], {"3.2": {"X12-": 1}})

    def test_classes(self):
        cls = lambda **f: next(iter(k for k, v in self._sim(_sim_play(**f))["trans"].items() if v))
        self.assertEqual(cls(walk="1", post=("bat", "", "")), "BB")
        self.assertEqual(cls(k="1"), "K")
        self.assertEqual(cls(roe="1", bip="1", ground="1", post=("bat", "", "")), "ROE")
        self.assertEqual(cls(bip="1", bunt="1"), "GO")
        self.assertEqual(cls(bip="1"), "AO")
        self.assertEqual(cls(hr="1", runs=("bat", "", "", "")), "HR")

    def test_intentional_walks_and_sacrifices_by_situation(self):
        t = self._sim(
            # 8th inning, home batting down 1, runner on second, one out: walked intentionally
            _sim_play(pre=("", "r2", ""), post=("bat", "r2", ""), outs=1, inning="8", iw="1", vis_home="1",
                      score_v="3", score_h="2"),
            # 1st inning, visitors batting, runner on first, none out: sacrifice, runner to second
            _sim_play(pre=("r1", "", ""), post=("", "r1", ""), outs_post=1, bip="1", bunt="1", sh="1",
                      vis_home="0", score_v="0", score_h="0"))
        self.assertEqual(t["sit"]["2.1.l.-1"], [1, 1, 0])
        self.assertEqual(t["sit"]["1.0.e.0"], [1, 0, 1])
        self.assertEqual(t["sac"], {"1.0": {"X2--": 1}})
        self.assertEqual(t["trans"]["BB"], {})   # decided by the situation, not in the play tables
        self.assertEqual(t["trans"]["GO"], {})
        self.assertEqual(retrosheet.sim_situation(7, 2, 11, 5), "7.2.x.2")
        self.assertEqual(t["homefield"], {"1": {"BB": 1}, "0": {"GO": 1}})

    def test_where_a_ball_in_play_went(self):
        ix = {"loc": 0, "firstf": 1, "hittype": 2}
        zone = lambda loc, first, kind, cls="1B": retrosheet.field_zone([loc, first, kind], ix, cls)
        self.assertEqual(zone("56+", "7", "G"), "56")    # through the hole: the location says whose ground
        self.assertEqual(zone("8LXD+", "8", "F"), "8")
        self.assertEqual(zone("", "8", "G"), "46")        # no location: a grounder got past the middle infielders
        self.assertEqual(zone("", "9", ""), "349")        # of unknown kind: them and the outfielder
        self.assertEqual(zone("9+", "9", ""), "349")      # an older season's "location": only who picked it up
        self.assertEqual(zone("9+", "9", "F", "AO"), "9")
        self.assertEqual(zone("", "6", "G", "GO"), "6")
        self.assertIsNone(zone("", "", ""))

    def test_fielders_against_runners(self):
        cols = SIM_COLUMNS + ["firstf", "f6", "f9", "pitcher", "pitteam", "event", "loc", "hittype", "gdp"]
        def play(**f):
            extra = {k: f.pop(k) for k in list(f) if k in cols[len(SIM_COLUMNS):]}
            row = _sim_play(**f)
            return row + [extra.get(k, "") for k in cols[len(SIM_COLUMNS):]]
        t = retrosheet.parse_sim([cols,
            # single to right, the runner from first goes to third: Rios gave up an extra base
            play(pre=("r1", "", ""), post=("bat", "", "r1"), single="1", firstf="9", f9="riosa", pitteam="KCA",
                 event="S9/L9S.1-3", loc="9S", hittype="L"),
            # double-play grounder to short, turned
            play(pre=("r1", "", ""), outs=0, outs_post=2, bip="1", ground="1", firstf="6", f6="escoa",
                 pitteam="KCA", event="64(1)3/GDP", loc="6", hittype="G", gdp="1")])
        self.assertEqual(t["fielders"]["riosa KCA"]["9"], [1, 1, 0, 0, 0, 0])
        self.assertEqual(t["fielders"]["escoa KCA"]["6"], [0, 0, 0, 0, 1, 1])
        self.assertEqual(t["pick"]["1B"], {"9": {"9": 1}})
        self.assertEqual(t["fielding_league"]["9"][:2], [1, 1])

    def test_the_infielders_covering_a_steal(self):
        cols = SIM_COLUMNS + ["f4", "f5", "f6", "pitteam"]
        def play(**f):
            extra = {k: f.pop(k) for k in list(f) if k in cols[len(SIM_COLUMNS):]}
            return _sim_play(**f) + [extra.get(k, "") for k in cols[len(SIM_COLUMNS):]]
        on = dict(f4="secb", f5="thrb", f6="shst", pitteam="KCA")
        t = retrosheet.parse_sim([cols,
            play(pa=0, pre=("r1", "", ""), post=("", "r1", ""), sb2="1", **on),          # stole second
            play(pa=0, pre=("r1", "", ""), outs_post=1, cs2="1", inning="2", **on),       # caught at second
            play(pa=0, pre=("", "r2", ""), post=("", "", "r2"), sb3="1", inning="3", **on)])  # stole third
        self.assertEqual(t["tags"]["shst KCA"], {"6": [2, 1]})   # both tries at second, one caught
        self.assertEqual(t["tags"]["secb KCA"], {"4": [2, 1]})   # (second base shares them)
        self.assertEqual(t["tags"]["thrb KCA"], {"5": [1, 0]})
        self.assertEqual(t["tags_club"]["KCA"]["6"], [2, 1])

    def test_steal_chances_attempts_and_wild_pitches(self):
        t = self._sim(
            _sim_play(pa=0, pre=("r1", "", ""), post=("", "r1", ""), sb2="1"),            # steals second
            _sim_play(pre=("", "r1", ""), post=("", "", "r1"), bip="1"),                   # fly out, he tags to third
            _sim_play(pa=0, pre=("", "", "r1"), post=("", "", ""), runs=("", "", "", "r1"), inning="2"))  # wild pitch
        self.assertEqual(t["steal"]["1"], [1, 1, 1])
        self.assertEqual(t["steal"]["2"], [0, 0, 0])  # he stole during the plate appearance: no new chance from second
        self.assertEqual(t["steal"]["3"], [1, 0, 0])  # the next inning's first batter started with him on third
        self.assertEqual(t["adv"], {"4.0": {"---H": 1}})


class SimulatorEngine(unittest.TestCase):
    LEAGUE = {"K": 0.2, "BB": 0.08, "HBP": 0.01, "1B": 0.14, "2B": 0.045, "3B": 0.005, "HR": 0.03,
              "GO": 0.24, "AO": 0.25}
    LEAGUE_COUNTS = {k: round(v * 10000) for k, v in LEAGUE.items()}  # a season's road games: 10,000 PA

    def test_log5_of_league_average_players_is_the_environment(self):
        env = dict(self.LEAGUE, K=0.1, HR=0.01)
        total = sum(env.values())
        env = {k: v / total for k, v in env.items()}
        mixed = simulator.log5(self.LEAGUE, self.LEAGUE, self.LEAGUE, self.LEAGUE, env)
        for k in simulator.LOG5_CLASSES:
            self.assertAlmostEqual(mixed[k], env[k])

    def test_log5_odds_cancel(self):
        batter = dict(self.LEAGUE, BB=0.16, GO=0.16)       # walks twice as often
        pitcher = dict(self.LEAGUE, BB=0.04, GO=0.28)      # walks half as often
        mixed = simulator.log5(batter, self.LEAGUE, pitcher, self.LEAGUE, self.LEAGUE)
        self.assertAlmostEqual(mixed["BB"] / mixed["K"], self.LEAGUE["BB"] / self.LEAGUE["K"])

    ZONES = {"trans": {"ROE": {"0.0": {"1---": 2}}, "GO": {"0.0": {"X---": 49}}, "AO": {"0.0": {"X---": 49}}},
             "adv": {}, "pa": {"0.0": 100}, "steal": {},
             # half the balls to the shortstop, a quarter to the 5-6 hole, a quarter to center field
             "field": {k: {"6": 50, "56": 25, "8": 25} for k in ("1B", "2B", "3B", "GO", "AO")} | {"ROE": {"6": 2}}}

    def test_fielding_keeps_the_balls_in_play(self):
        tables = simulator.Tables(self.ZONES)
        p, where = simulator.apply_fielding(self.LEAGUE, tables, {z: (0.9, 1.5) for z in tables.zones})
        self.assertAlmostEqual(sum(p.values()), 1.0)
        bip = lambda q: sum(q.get(k, 0) for k in ("1B", "2B", "3B", "GO", "AO", "ROE"))
        self.assertAlmostEqual(bip(p), bip(self.LEAGUE))
        hits = lambda q: q["1B"] + q["2B"] + q["3B"]
        self.assertLess(hits(p), hits(self.LEAGUE))
        self.assertAlmostEqual(sum(c for _z, c in where["1B"]), p["1B"])
        league, lwhere = simulator.apply_fielding(self.LEAGUE, tables, {})
        at_short = lambda k: sum(c for z, c in lwhere[k] if z == "6")
        self.assertAlmostEqual(at_short("ROE") / (at_short("GO") + at_short("AO") + at_short("ROE")), 2 / 102)
        self.assertNotIn("8", [z for z, _c in lwhere["ROE"]])  # no errors in center field that season

    def test_a_fielder_counts_only_on_balls_to_his_ground(self):
        tables = simulator.Tables(self.ZONES)
        great_ss = simulator.zone_factors({"SS": (1.2, 1.0)}, tables.zones)
        self.assertEqual(great_ss["8"], (1.0, 1.0))  # center field's balls: not his
        self.assertLess(great_ss["6"][0], 1.0)
        self.assertAlmostEqual(great_ss["56"][0], great_ss["6"][0] ** 0.5)  # the hole: half his part
        p, where = simulator.apply_fielding(self.LEAGUE, tables, great_ss)
        league, lwhere = simulator.apply_fielding(self.LEAGUE, tables, {})
        to = lambda w, z: sum(c for zz, c in w["1B"] if zz == z)
        self.assertAlmostEqual(to(where, "8"), to(lwhere, "8"))
        self.assertLess(to(where, "6"), to(lwhere, "6"))

    def test_counts_with_missing_splits_use_the_league(self):
        c = simulator.line_counts({"plateAppearances": 100, "strikeOuts": 20, "baseOnBalls": 10, "hitByPitch": 0,
                                   "hits": 30, "homeRuns": 5, "groundOuts": None, "airOuts": None}, pitcher=False)
        counts = simulator.class_counts(c, self.LEAGUE)
        self.assertAlmostEqual(sum(counts.values()), 100)
        self.assertAlmostEqual(counts["GO"] / counts["AO"], self.LEAGUE["GO"] / self.LEAGUE["AO"])

    def test_shrinking_toward_the_league(self):
        c = {"K": 5, "BB": 0, "HBP": 0, "1B": 5, "2B": 0, "3B": 0, "HR": 0, "GO": 0, "AO": 0}
        r = simulator.rates_from_counts(c, 10, self.LEAGUE, 50)
        self.assertAlmostEqual(r["K"], (5 + 50 * 0.2) / 60)

    def test_intentional_walks_and_sacrifices_come_out_of_the_lines(self):
        row = {"plateAppearances": 100, "strikeOuts": 20, "baseOnBalls": 10, "intentionalWalks": 4, "sacBunts": 6,
               "hitByPitch": 0, "hits": 25, "homeRuns": 3, "doubles": 5, "triples": 1}
        c = simulator.line_counts(row, strip=(True, True))
        self.assertEqual((c["n"], c["BB"]), (90, 6))
        c = simulator.line_counts(row, strip=(False, True))
        self.assertEqual((c["n"], c["BB"]), (94, 10))

    def test_rest_days_follow_the_real_starts(self):
        from types import SimpleNamespace as NS
        regular, backup, other = NS(pid=1, name="Reg"), NS(pid=2, name="Backup", row={}), NS(pid=3, name="Other", row={})
        team = NS(fatigue=True, off=frozenset(), rest={1: 0.25}, bench=[backup, other],
                  fielding_by_player={2: {"SS": {"innings": "300.0"}}, 3: {"2B": {"innings": "900.0"}}})
        sat = 0
        for g in range(100):
            lineup, defense = [regular], {"SS": {"_id": 1}}
            out = simulator.Team.day_lineup(team, g, lineup, defense)
            if out:
                sat += 1
                self.assertEqual(out, [(0, regular, backup, "SS")])  # only the man who plays short can
                self.assertIs(lineup[0], backup)
                self.assertEqual(defense["SS"], {"innings": "300.0"})
        self.assertEqual(sat, 25)
        team.fatigue = False
        self.assertEqual(simulator.Team.day_lineup(team, 3, [regular], {"SS": {"_id": 1}}), [])

    def test_tired_relievers(self):
        from types import SimpleNamespace as NS
        team = NS(fatigue=True, off=frozenset(), pitchers=[NS(pid=7, bf_relief=4.0), NS(pid=8, bf_relief=4.0), NS(pid=9, bf_relief=6.0)])
        self.assertEqual(simulator.tired_relievers(team, []), frozenset())
        # 7 pitched both games; 9 went long (12 >= 2 x 6) yesterday; 8 only yesterday, briefly
        self.assertEqual(simulator.tired_relievers(team, [{7: 3}, {7: 4, 8: 5, 9: 12}]), {7, 9})
        team.off = frozenset({"tired_relievers"})  # the path analysis's switch
        self.assertEqual(simulator.tired_relievers(team, [{7: 3}, {7: 4}]), frozenset())
        team.off, team.fatigue = frozenset(), False
        self.assertEqual(simulator.tired_relievers(team, [{7: 3}, {7: 4}]), frozenset())

    def test_home_edge(self):
        self.assertIsNone(simulator.home_edge({"1": {"BB": 5}}))
        edge = simulator.home_edge({"1": {"K": 18000, "BB": 9000, "GO": 73000}, "0": {"K": 22000, "BB": 7000, "GO": 71000}})
        self.assertGreater(edge[True]["BB"], 1.0)
        self.assertLess(edge[False]["BB"], 1.0)
        self.assertLess(edge[True]["K"], 1.0)
        self.assertAlmostEqual(edge[True]["HR"], 1.0)  # never happened: no edge
        # pulled toward no edge with 20,000 plate appearances at the overall rate
        self.assertAlmostEqual(edge[True]["BB"], ((9000 + 20000 * 0.08) / 120000) / 0.08)

    def test_round_robin_bookkeeping(self):
        calls = []

        def fake(t1, t2, env, games, seed, progress=None, series=None, cancelled=None):
            calls.append((t1, t2, env, seed))
            # the first named team always wins 3 of 4, 5 runs to 2
            return {"wins": [3, 1], "ties": 0, "runs": ([5] * 4, [2] * 4), "games": 4}
        real, simulator.simulate = simulator.simulate, fake
        try:
            rr = simulator.round_robin(["A", "B", "C"], ["eA", "eB", "eC"], 4, 1)
        finally:
            simulator.simulate = real
        self.assertEqual([c[:3] for c in calls], [("A", "B", ("eA", "eB")), ("A", "C", ("eA", "eC")),
                                                  ("B", "C", ("eB", "eC"))])
        self.assertEqual(len({c[3] for c in calls}), 3)  # a seed per pair
        self.assertEqual([(r["W"], r["L"]) for r in rr["table"]], [(6, 2), (4, 4), (2, 6)])
        self.assertEqual(rr["h2h"][1], [1, 0, 3])
        self.assertEqual(rr["order"], [0, 1, 2])

    def test_uncertainty(self):
        low, high = simulator.win_interval(60, 100)
        self.assertAlmostEqual(low, 0.502, places=3)
        self.assertAlmostEqual(high, 0.691, places=3)
        self.assertIsNone(simulator.win_interval(0, 0))
        self.assertEqual(simulator.games_to_separate(0.6), 93)   # 1.96^2 x .24 / .01 = 92.2
        self.assertIsNone(simulator.games_to_separate(0.5))
        m, lo, hi = simulator.run_margin_interval({"runs": ([5, 3, 4], [2, 3, 4])})
        self.assertAlmostEqual(m, 1.0)
        self.assertLess(lo, 1.0)
        self.assertGreater(hi, 1.0)

    def test_win_expectancy(self):
        even = {(st, o): [50, 30, 20] + [0] * 8 for st in range(8) for o in range(3)}  # both teams alike
        we = simulator.WinExpectancy(({k: list(v) for k, v in even.items()}, {k: list(v) for k, v in even.items()}),
                                     1, False)
        self.assertAlmostEqual(we._value(0, 0), 0.5)                     # alike teams: even before the game
        self.assertEqual(we.home_chance(9, False, 1, 0, 1), 1.0)         # ahead in the bottom of the 9th: over
        self.assertAlmostEqual(we.home_chance(9, False, 3, 0, 0), 0.5)   # tied after nine: extra innings, even
        # up 3 going to the 9th, the visitors can score 2 at most in an inning here: a sure thing
        self.assertAlmostEqual(we._value(16, 3), 1.0)
        self.assertGreater(we.home_chance(5, True, 0, 0, 1), 0.5)
        # a team that scores more from bases loaded than from bases empty
        rich = {k: list(v) for k, v in even.items()}
        rich[(7, 0)] = [0, 0, 100] + [0] * 8
        we = simulator.WinExpectancy((rich, rich), 0, False)
        self.assertGreater(we.home_chance(3, False, 0, 7, 0), we.home_chance(3, False, 0, 0, 0))

    def test_series_home_games(self):
        self.assertEqual([simulator.series_host(7, k) for k in range(7)], [True, True, False, False, False, True, True])
        self.assertEqual([simulator.series_host(5, k) for k in range(5)], [True, True, False, False, True])

    def test_matchup_value(self):
        self.assertAlmostEqual(simulator.matchup_value({"K": 0.5, "HR": 0.1, "BB": 0.1, "GO": 0.3}), 0.279)
        self.assertEqual(simulator.matchup_value({"K": 1.0}), 0.0)

    def test_situations(self):
        sits = simulator.Situations({"1.0.e.0": [900, 0, 90], "1.0.l.0": [100, 0, 0], "2.1.l.-1": [100, 10, 0]},
                                    {"1.0": {"X2--": 9, "12--": 1}})
        ibb, sac = sits.chances(1, 0, 8, 0)  # pulled toward the base-out state's 9%
        self.assertGreater(sac, 0.0)
        self.assertLess(sac, 0.09)
        self.assertEqual(sits.chances(1, 0, 3, 5), (0.0, 0.09))  # never seen: the base-out state's
        self.assertAlmostEqual(sits.league[1], 90 / 1100)
        import random as _r
        got = {sits.draw_sac(1, 0, _r.Random(i))[0] for i in range(50)}
        self.assertEqual(got, {"X2--", "12--"})
        self.assertIsNone(sits.draw_sac(2, 0, _r.Random(1)))

    def test_odds_factor_and_tilt(self):
        self.assertAlmostEqual(simulator.odds_factor(10, 100, 0.1, 0), 1.0)
        f = simulator.odds_factor(20, 100, 0.1, 0)
        self.assertAlmostEqual(simulator.tilt(0.1, f), 0.2)
        self.assertGreater(simulator.odds_factor(20, 100, 0.1, 0), simulator.odds_factor(20, 100, 0.1, 100))

    def test_default_outcomes(self):
        self.assertEqual(simulator.default_outcome("BB", 5), "12-3")  # first is forced, third isn't
        self.assertEqual(simulator.default_outcome("BB", 7), "123H")
        self.assertEqual(simulator.default_outcome("2B", 1), "23--")
        self.assertEqual(simulator.default_outcome("HR", 4), "H--H")
        self.assertEqual(simulator.default_outcome("GO", 2), "X-2-")

    def test_park_factors(self):
        park = {"site": "DEN02", "home": dict(self.LEAGUE_COUNTS, **{"3B": 120, "HR": 260}), "road": self.LEAGUE_COUNTS}
        f = simulator.park_factors(park)
        self.assertGreater(f["3B"], 1.5)           # triples: 120 at home against 50 on the road
        self.assertLess(f["3B"], 120 / 50)          # ... pulled toward neutral
        self.assertAlmostEqual(f["K"], 1.0, delta=0.05)
        self.assertIsNone(simulator.park_factors(None))
        here = simulator.with_park(self.LEAGUE, f)
        self.assertAlmostEqual(sum(here[k] for k in simulator.LOG5_CLASSES), 1.0)
        self.assertGreater(here["3B"], self.LEAGUE["3B"])
        # a player's line with his half season there taken out, then a game there: back about where it was, but
        # only half way -- the other half of his season was on the road
        neutral = simulator.with_park(self.LEAGUE, f, -0.5)
        self.assertLess(neutral["3B"], self.LEAGUE["3B"])
        self.assertGreater(simulator.with_park(neutral, f)["3B"], self.LEAGUE["3B"])

    def test_steals_at_any_base(self):
        tables = simulator.Tables({"trans": {}, "adv": {}, "pa": {}, "steal": {"1": [600, 60, 42], "2": [300, 6, 4],
                                                                                "3": [100, 0, 0]}})
        tries, safe = tables.steal_rates()
        self.assertAlmostEqual(tries, 66 / 1000)        # the league's: all tries over all chances
        self.assertAlmostEqual(safe, (42 + 4) / 66)
        fast_tries, fast_safe = tables.steal_rates(2.0, 2.0)
        self.assertGreater(fast_tries, tries)
        self.assertGreater(fast_safe, safe)
        self.assertIsNone(simulator.Tables(None).steal_rates())

    def test_a_runner_tilts_the_draw(self):
        tables = simulator.Tables({"trans": {"1B": {"1.0": {"12--": 60, "13--": 40}}}, "adv": {}, "pa": {"1.0": 100},
                                   "steal": {"1": [10, 1, 1]}})
        rng = random.Random(1)
        fast = sum(tables.draw("1B", 1, 0, (3.0, 1.0, 1.0), 1.0, rng)[0] == "13--" for _ in range(4000)) / 4000
        self.assertAlmostEqual(fast, 40 * 3 / (60 + 40 * 3), delta=0.03)
        outcome, share, count, total = tables.draw("1B", 1, 0, (1.0, 1.0, 1.0), 1.0, rng)
        self.assertEqual(total, 100)

    def test_ground_and_air_outs_from_retrosheet_when_mlb_lacks_them(self):
        rows = [{"plateAppearances": 600, "strikeOuts": 100, "baseOnBalls": 50, "hitByPitch": 5, "hits": 150,
                 "homeRuns": 15, "doubles": 25, "triples": 3} for _ in range(10)]
        rows[0].update(groundOuts=200, airOuts=10)  # one stray line with the split is not the league's
        self.assertFalse(simulator.split_recorded(rows))
        league = simulator.league_rates(rows, {"GO": 300, "AO": 100})
        self.assertEqual(league["_split"], "Retrosheet")
        self.assertAlmostEqual(league["GO"] / league["AO"], 3.0)
        mine = simulator.player_rates(rows[0], league)
        self.assertAlmostEqual(mine["GO"] / mine["AO"], 3.0)  # his stray split isn't used either
        self.assertEqual(simulator.league_rates(rows)["_split"], "none")

    def test_ordinals(self):
        self.assertEqual([simulator._ordinal(n) for n in (1, 9, 10, 11, 12, 13, 21, 22, 23)],
                         ["first", "ninth", "10th", "11th", "12th", "13th", "21st", "22nd", "23rd"])

    def test_dh_seasons(self):
        self.assertFalse(simulator.uses_dh("AL", 1972))
        self.assertTrue(simulator.uses_dh("AL", 1973))
        self.assertTrue(simulator.uses_dh("NL", 2020))
        self.assertFalse(simulator.uses_dh("NL", 2021))
        self.assertTrue(simulator.uses_dh("NL", 2022))


def _sim_team_raw(team_id=1, season=2023, league="AL"):
    """A small but complete team-season for the Simulator: nine batters, five starters, four relievers."""
    def bat(pid, name, pa, h, hr, bb, k, go, ao):
        return {"_id": pid, "_name": name, "_league": league, "plateAppearances": pa, "atBats": pa - bb,
                "hits": h, "doubles": h // 5, "triples": 1, "homeRuns": hr, "baseOnBalls": bb, "hitByPitch": 2,
                "strikeOuts": k, "groundOuts": go, "airOuts": ao, "groundIntoDoublePlay": 10, "totalBases": h + 3 * hr,
                "runs": 70, "rbi": 70, "sacFlies": 3}
    hitting = [bat(100 + i, f"Batter {i}", 600 - 20 * i, 150 - 4 * i, 20 - i, 60, 120, 150, 160) for i in range(9)]

    def arm(pid, name, g, gs, gf, outs, bf, runs):
        return {"_id": pid, "_name": name, "_league": league, "gamesPitched": g, "gamesStarted": gs, "gamesFinished": gf,
                "outs": outs, "inningsPitched": f"{outs // 3}.{outs % 3}", "battersFaced": bf, "hits": bf // 4,
                "doubles": bf // 20, "triples": 1, "homeRuns": bf // 35, "baseOnBalls": bf // 12, "hitByPitch": 3,
                "strikeOuts": bf // 4, "groundOuts": bf // 5, "airOuts": bf // 5, "runs": runs}
    pitching = [arm(200 + i, f"Starter {i}", 32, 32, 0, 540, 760, 80) for i in range(5)] + \
               [arm(300, "Closer", 65, 0, 55, 195, 260, 20), arm(301, "Setup", 70, 0, 10, 200, 270, 25),
                arm(302, "Long Man", 40, 2, 5, 240, 330, 40), arm(303, "Mop", 30, 0, 8, 90, 140, 25)]
    positions = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"]
    fielding = [{"_id": 100 + i, "_name": f"Batter {i}", "_pos": pos, "putOuts": 300, "assists": 100, "errors": 8,
                 "innings": "1200.0", "gamesPlayed": 140} for i, pos in enumerate(positions)] + \
               [{"_id": 200, "_name": "Starter 0", "_pos": "P", "putOuts": 10, "assists": 20, "errors": 1,
                 "innings": "180.0", "gamesPlayed": 32}]
    order = {100 + i: {i + 1: {"games": 140, "pa": 600}} for i in range(9)}
    league_positions = {pos: mlbstats.rf_figures(4000.0, 3 * 14000.0, 4000.0, 1600.0) for pos in positions + ["P"]}
    league_hitting = [dict(r, _league=league) for r in hitting]
    return {"team_id": team_id, "season": season, "notes": [], "hitting": hitting, "pitching": pitching,
            "fielding": fielding, "order": order, "league_positions": league_positions,
            "league_hitting": league_hitting, "runners": None, "league_runners": None, "relief": None, "abbrev": "TST"}


class SimulatorGames(unittest.TestCase):
    def setUp(self):
        self.team = simulator.Team(_sim_team_raw(), "Testers 2023")
        self.env = simulator.Environment(self.team, simulator.Tables(None), None)

    def test_the_team_is_set_up_by_the_other_tabs_rules(self):
        t = self.team
        self.assertEqual([b.name for b in t.lineup], [f"Batter {i}" for i in range(9)])
        self.assertEqual(len(t.rotation), 5)
        self.assertEqual(t.closer.name, "Closer")
        self.assertEqual(t.defense["SS"]["_name"], "Batter 4")
        self.assertEqual(t.lineup_rows[8][2], "DH")  # the ninth batter isn't one of the eight fielders: the DH

    def test_no_dh_puts_the_pitcher_in_the_order(self):
        raw = _sim_team_raw(season=1965)
        raw["hitting"].append({"_id": 200, "_name": "Starter 0", "_league": "AL", "plateAppearances": 70, "hits": 8,
                               "homeRuns": 0, "baseOnBalls": 2, "strikeOuts": 30, "atBats": 66})
        raw["order"][200] = {9: {"games": 32, "pa": 70}}
        team = simulator.Team(raw, "Testers 1965")
        self.assertIsNone(team.lineup[8])
        self.assertTrue(any("pitcher bats ninth" in n for n in team.notes))

    def _two_way_team(self, season):
        """Batter 3 (third base, batting fourth) also pitches: 20 starts, 130 games in all."""
        raw = _sim_team_raw(season=season)
        for r in raw["hitting"]:
            r["gamesPlayed"] = 140
        raw["hitting"][3]["gamesPlayed"] = 130
        raw["pitching"].append({"_id": 103, "_name": "Batter 3", "_league": "AL", "gamesPitched": 20,
                                "gamesStarted": 20, "gamesFinished": 0, "outs": 400, "inningsPitched": "133.1",
                                "battersFaced": 560, "hits": 130, "doubles": 25, "triples": 2, "homeRuns": 8,
                                "baseOnBalls": 50, "hitByPitch": 3, "strikeOuts": 90, "groundOuts": 120,
                                "airOuts": 120, "runs": 60})
        if season < 1973:  # no DH: the pitchers bat ninth, and a bench player can play third
            raw["hitting"].append({"_id": 200, "_name": "Starter 0", "_league": "AL", "plateAppearances": 70,
                                   "hits": 8, "homeRuns": 0, "baseOnBalls": 2, "strikeOuts": 30, "atBats": 66,
                                   "gamesPlayed": 32})
            raw["order"][200] = {9: {"games": 32, "pa": 70}}
        raw["hitting"].append({"_id": 600, "_name": "Utility", "_league": "AL", "plateAppearances": 150, "hits": 35,
                               "homeRuns": 2, "baseOnBalls": 10, "strikeOuts": 30, "atBats": 140, "gamesPlayed": 60})
        raw["fielding"].append({"_id": 600, "_name": "Utility", "_pos": "3B", "putOuts": 40, "assists": 90,
                                "errors": 3, "innings": "300.0", "gamesPlayed": 40})
        return simulator.Team(raw, "Two-way")

    def test_a_two_way_player_without_the_dh(self):
        team = self._two_way_team(1965)
        self.assertEqual(team.two_way, {103})
        self.assertEqual([pos for _s, r, pos in team.lineup_rows if r["_id"] == 103], ["3B"])  # a batter, not a pitcher
        self.assertIn(103, [p.pid for p in team.rotation])
        star = next(p for p in team.pitchers if p.pid == 103)
        game = simulator.Game(self.env, (team, team), (star, team.rotation[0]), random.Random(1),
                              {"batting": ({}, {}), "pitching": ({}, {})})
        spot = team.lineup.index(None)  # the pitcher's spot: Utility bats there and plays third
        self.assertEqual(game.lineups[0][spot].name, "Utility")
        self.assertEqual(game.defense[0]["3B"]["_name"], "Utility")
        self.assertEqual(game.lineups[0][3].pid, 103)  # he still bats fourth
        game._bring_in(0, team.rotation[1])            # he leaves the mound: back to third, the reliever bats ninth
        self.assertEqual(game.defense[0]["3B"]["_id"], 103)
        self.assertIsNone(game.lineups[0][spot])

    def test_a_two_way_player_with_the_dh(self):
        before = self._two_way_team(2021)  # the club gives up the DH when he pitches, until 2022
        star = next(p for p in before.pitchers if p.pid == 103)
        game = simulator.Game(self.env, (before, before), (star, before.rotation[0]), random.Random(1),
                              {"batting": ({}, {}), "pitching": ({}, {})})
        game._bring_in(0, before.rotation[1])
        self.assertIsNone(game.lineups[0][3])  # whoever relieves him bats fourth
        after = self._two_way_team(2023)       # the Ohtani rule: he stays in the order
        star = next(p for p in after.pitchers if p.pid == 103)
        game = simulator.Game(self.env, (after, after), (star, after.rotation[0]), random.Random(1),
                              {"batting": ({}, {}), "pitching": ({}, {})})
        game._bring_in(0, after.rotation[1])
        self.assertEqual(game.lineups[0][3].pid, 103)

    def test_the_dh_rule_can_be_chosen(self):
        dh_club = simulator.Team(_sim_team_raw(), "DH club")  # 2023 AL: the ninth batter is the DH
        self.assertTrue(dh_club.uses_dh)
        no_dh = simulator.Team(_sim_team_raw(), "DH club", dh_rule="pitchers")
        self.assertFalse(no_dh.uses_dh)
        self.assertIsNone(no_dh.lineup[8])                       # the pitcher bats ninth ...
        self.assertNotIn(108, [b.pid for b in no_dh.lineup if b])  # ... and the DH sits
        self.assertTrue(no_dh.pitchers_batting["K"] > no_dh.league["K"])  # pitchers bat like pitchers
        old = _sim_team_raw(season=1965)
        old["hitting"].append({"_id": 200, "_name": "Starter 0", "_league": "AL", "plateAppearances": 70, "hits": 8,
                               "homeRuns": 0, "baseOnBalls": 2, "strikeOuts": 30, "atBats": 66})
        old["order"][200] = {9: {"games": 32, "pa": 70}}
        old["hitting"].append({"_id": 700, "_name": "Bench Bat", "_league": "AL", "plateAppearances": 300, "hits": 80,
                               "homeRuns": 10, "baseOnBalls": 30, "strikeOuts": 50, "atBats": 265})
        with_dh = simulator.Team(old, "1965 club", dh_rule="dh")
        self.assertTrue(with_dh.uses_dh)
        self.assertNotIn(None, with_dh.lineup)                   # no pitcher's spot
        self.assertIn(108, [b.pid for b in with_dh.lineup])      # Batter 8, whom the pitcher displaced, bats again

    def test_pinch_hitters_for_the_pitcher(self):
        raw = _sim_team_raw(season=1965)
        raw["hitting"].append({"_id": 200, "_name": "Starter 0", "_league": "AL", "plateAppearances": 70, "hits": 8,
                               "homeRuns": 0, "baseOnBalls": 2, "strikeOuts": 30, "atBats": 66})
        raw["order"][200] = {9: {"games": 32, "pa": 70}}
        raw["hitting"].append({"_id": 700, "_name": "Bench Bat", "_league": "AL", "plateAppearances": 300, "hits": 80,
                               "homeRuns": 10, "baseOnBalls": 30, "strikeOuts": 50, "atBats": 265})
        team = simulator.Team(raw, "1965 club")
        game = simulator.Game(self.env, (team, team), (team.rotation[0], team.rotation[0]), random.Random(1),
                              {"batting": ({}, {}), "pitching": ({}, {})})
        self.assertIsNone(game._pinch_hit(0))                    # the starter bats early on
        game.inning = 7
        ph = game._pinch_hit(0)
        self.assertEqual(ph.name, "Batter 8")                    # from the 7th he's pinch-hit for, by the bench's
        self.assertEqual(game.limit[0], game.faced[0])           # busiest hitter -- and is done
        self.assertEqual(game._pinch_hit(0).name, "Bench Bat")   # the next time, the next one
        self.assertIsNone(game._pinch_hit(0))                    # each once

    def test_card_battles(self):
        team = self.team
        batter, pitcher = team.lineup[0], team.rotation[0]
        pa = simulator.battle_plate_appearance(batter, team, pitcher, team, self.env)
        self.assertAlmostEqual(sum(r["final"] for r in pa["rows"]), 1.0)
        self.assertTrue(0 < pa["line"]["AVG"] < pa["line"]["SLG"])
        self.assertAlmostEqual(pa["line"]["OBP"], sum(r["final"] for r in pa["rows"]
                                                      if r["cls"] in ("1B", "2B", "3B", "HR", "BB", "HBP")))
        steps = simulator._odds_steps(0.4, [("runner", 2.0), ("arm", 0.5)])
        self.assertAlmostEqual(steps["rate"], 0.4)              # a fast runner and a strong arm cancel
        self.assertAlmostEqual(simulator._odds_steps(0.5, [("x", 3.0)])["rate"], 0.75)
        dp = simulator.battle_running("double_play", batter, team, team, self.env, team.defense["SS"]["_id"], "SS")
        self.assertEqual(dp["parts"][0][1]["steps"][0][0], "batter")
        with self.assertRaises(ValueError):
            simulator.battle_running("bunt", batter, team, team, self.env)

    def test_lineup_problems_and_a_suggestion(self):
        raw = _sim_team_raw()
        raw["order"][100] = {}                      # the catcher isn't in the order ...
        raw["order"][600] = {1: {"games": 150, "pa": 600}}
        raw["hitting"].append(dict(raw["hitting"][0], _id=600, _name="Backup", plateAppearances=650))
        raw["fielding"].append({"_id": 600, "_name": "Backup", "_pos": "C", "putOuts": 50, "assists": 5, "errors": 1,
                                "innings": "200.0", "gamesPlayed": 30})
        team = simulator.Team(raw, "T")
        problems = team.lineup_problems()
        # ... Backup leads off, so he's taken as the DH, and Batter 8, the DH, bats without a position
        self.assertEqual(problems["bats_only"], ["Batter 8"])
        self.assertEqual(problems["fields_only"], [("C", "Batter 0")])
        self.assertEqual(team.suggest_positions(), {600: "C", 108: "DH"})  # Backup caught: he catches
        fixed = simulator.Team(raw, "T", overrides={"defense": {"C": 600}})
        self.assertEqual(fixed.lineup_problems()["bats_only"], [])

    def test_games_finish_and_add_up(self):
        res = simulator.simulate(self.team, self.team, self.env, 60, 5)
        self.assertEqual(res["wins"][0] + res["wins"][1] + res["ties"], 60)
        runs = sum(res["runs"][0])
        scored = sum(st["R"] for st in res["stats"]["batting"][0].values())
        self.assertEqual(runs, scored)
        allowed = sum(st["R"] for st in res["stats"]["pitching"][1].values())
        self.assertEqual(allowed, runs)  # every run is charged to a pitcher
        starts = sum(st["GS"] for st in res["stats"]["pitching"][0].values())
        self.assertEqual(starts, 60)

    def test_a_game_replays_exactly(self):
        res = simulator.simulate(self.team, self.team, self.env, 8, 11)
        for index in (4, 5):  # Team 2 at home, then Team 1
            log, score, host = simulator.replay(self.team, self.team, self.env, 8, 11, index)
            self.assertEqual(score, (res["runs"][0][index], res["runs"][1][index]))
            self.assertEqual(host, res["hosts"][index])
        self.assertTrue(any(line["kind"] == "pa" for line in log))

    def test_the_teams_take_turns_at_home(self):
        other = simulator.Team(_sim_team_raw(team_id=2), "Others 2023")
        env2 = simulator.Environment(other, simulator.Tables(None), None)
        res = simulator.simulate(self.team, other, (self.env, env2), 10, 3)
        self.assertEqual(res["hosts"], [1, 0] * 5)  # Team 2 hosts the first game, then they take turns
        for t in (0, 1):
            self.assertEqual(sum(res["home"][t]) + sum(res["road"][t]), 10 - res["ties"])
            self.assertEqual(res["home"][t][0] + res["road"][t][0], res["wins"][t])
        self.assertEqual(res["home"][0][0], res["road"][1][1])  # Team 1's home wins are Team 2's road losses
        log, score, host = simulator.replay(self.team, other, (self.env, env2), 10, 3, 1)
        self.assertEqual(host, 0)
        self.assertEqual(score, (res["runs"][0][1], res["runs"][1][1]))
        top = next(line for line in log if line["kind"] == "pa")
        self.assertTrue(top["top"])
        self.assertEqual(top["side"], 1)  # Team 1 at home: Team 2 bats first

    def test_starts_follow_real_starts(self):
        raw = _sim_team_raw()
        raw["pitching"][0]["gamesStarted"] = 64  # twice the others' starts
        team = simulator.Team(raw, "T")
        given = [p.name for p in team.starters(120)]
        self.assertEqual(given.count("Starter 0"), 40)

    def test_player_cards(self):
        res = simulator.simulate(self.team, self.team, self.env, 20, 2)
        cards = simulator.player_cards(self.team, self.env, res, 1)
        kinds = [c["kind"] for c in cards]
        self.assertEqual(kinds.count("batter"), 9)
        self.assertEqual(kinds.count("pitcher"), 9)
        self.assertEqual(kinds.count("fielder"), 0)  # every one of the usual fielders bats
        self.assertEqual(kinds[0], "park")          # the home park leads
        self.assertTrue(cards[0]["used"][0].startswith("Park: neutral"))  # (no Retrosheet park for this one)
        first = cards[1]
        self.assertEqual((first["name"], first["slot"], first["pos"], first["side"]), ("Batter 0", 1, "C", 1))
        self.assertAlmostEqual(sum(first["chances"].values()), 1.0)
        self.assertEqual([tb["caption"].split(":")[0] for tb in first["tables"]], ["Running", "Fielding"])
        self.assertTrue(any(line.startswith("Fielding at C:") for line in first["reference"]))
        self.assertFalse(any(line.startswith(("Range", "Errors")) for line in first["used"]))
        self.assertTrue(first["caption"].startswith("Batting:"))
        self.assertTrue(any(line.startswith("Real:") for line in first["reference"]))
        self.assertTrue(first["result"])
        closer = next(c for c in cards if c["kind"] == "pitcher" and "closer" in c["tag"])
        self.assertEqual(closer["name"], "Closer")
        self.assertTrue(any(line.startswith("Usage: the closer") for line in closer["used"]))
        starter = next(c for c in cards if c["kind"] == "pitcher" and c["role"] == "Rotation")
        self.assertTrue(any(line.startswith("Usage: starts 20% of games") for line in starter["used"]))
        self.assertTrue(starter["caption"].startswith("Pitching:"))
        self.assertTrue(any(line.startswith("His line was pitched in front of") for line in starter["reference"]))
        self.assertEqual([tb["caption"].split(":")[0] for tb in starter["tables"]], ["Fielding"])
        labels = set(simulator.CATEGORY_LABELS.values())
        for card in cards:
            for row in (r for tb in card.get("tables", []) for r in tb["rows"]):
                self.assertIn(row["cat"], simulator.CATEGORY_LABELS)
                self.assertTrue(0 <= row["mine"] <= 1 and 0 <= row["league"] <= 1)
            for line in card["used"]:
                self.assertIn(line.partition(": ")[0], labels, line)
        bats_only = cards[9]  # the ninth batter isn't one of the usual fielders
        self.assertEqual([tb["caption"].split(":")[0] for tb in bats_only["tables"]], ["Running"])

    def test_a_fielder_who_does_not_bat_gets_a_card(self):
        raw = _sim_team_raw()
        raw["fielding"].append({"_id": 999, "_name": "Glove Man", "_pos": "SS", "putOuts": 300, "assists": 500,
                                "errors": 5, "innings": "1300.0", "gamesPlayed": 150})
        team = simulator.Team(raw, "T")
        cards = simulator.player_cards(team, self.env)
        glove = [c for c in cards if c["kind"] == "fielder"]
        self.assertEqual([(c["name"], c["pos"]) for c in glove], [("Glove Man", "SS")])
        self.assertTrue(glove[0]["reference"][0].startswith("Fielding at SS: range"))
        self.assertIn("5 E in 805 chances", glove[0]["reference"][0])
        rows = {r["cat"]: r for r in glove[0]["tables"][0]["rows"]}
        self.assertLess(rows["range"]["mine"], rows["range"]["league"])   # 800 chances in 1300 innings: a long reach
        self.assertAlmostEqual(rows["errors"]["mine"], rows["errors"]["league"])  # no MLB errors to set them against

    def test_fielders_against_runners_meet_the_runners_own(self):
        raw = _sim_team_raw()
        rf = raw["fielding"][7]["_id"]  # Batter 7 plays right
        raw["arms"] = {rf: {"RF": [400, 100, 200, 30, 0, 0]}}            # 25% extra bases, 15% tag-ups against him
        raw["arms_league"] = {"RF": [4000, 1800, 2000, 560, 0, 0], "SS": [600, 80, 700, 8, 2200, 1300]}
        raw["league_positions"]["C"].update(stolen_bases=2500.0, caught_stealing=1000.0)
        raw["fielding"][0].update(stolenBases=20, caughtStealing=30)    # the catcher throws runners out
        team = simulator.Team(raw, "T")
        xb, tu = team.arm(rf, "RF")
        self.assertLess(xb, 1.0)       # a strong arm: fewer extra bases than MLB's right fielders allowed
        self.assertLess(tu, 1.0)
        state = team.running_state(team.defense, team.rotation[0])
        self.assertEqual(state["RF"][:2], (xb, tu))
        att, safe = state["steal"]
        self.assertLess(safe, 1.0)     # runners are safe less often against him
        flat = simulator.neutralized(simulator.neutralized(team, "arms"), "stopping_steals")
        fstate = flat.running_state(flat.defense, flat.rotation[0])
        self.assertEqual(fstate["RF"][:2], (1.0, 1.0))
        self.assertEqual(fstate["steal"], (1.0, 1.0))
        zones = dict(SimulatorEngine.ZONES, steal={"1": [1000, 70, 49]},
                     field={k: {"6": 40, "56": 20, "8": 20, "9": 20} for k in ("1B", "2B", "3B", "GO", "AO")}
                     | {"ROE": {"6": 2, "9": 1}})
        env = simulator.Environment(team, simulator.Tables(zones), None)
        cards = simulator.player_cards(team, env)
        right = next(c for c in cards if c["pid"] == rf)
        arms = [r for r in right["tables"][1]["rows"] if r["cat"] == "arms"]
        self.assertEqual(len(arms), 2)
        self.assertTrue(all(r["mine"] < r["league"] and r["good"] == "lower" for r in arms))  # a strong arm
        self.assertIn("range", [r["cat"] for r in right["tables"][1]["rows"]])
        catcher = next(c for c in cards if c.get("pos") == "C")
        safe = [r for r in catcher["tables"][1]["rows"] if r["cat"] == "stopping_steals"][1]
        self.assertLess(safe["mine"], safe["league"])
        runner = cards[1]["tables"][0]["rows"]
        self.assertEqual(runner[0]["league"], 0.07)  # tries per chance from first, the league's

    def test_infielders_against_steals(self):
        raw = _sim_team_raw()
        ss, second = raw["fielding"][4]["_id"], raw["fielding"][2]["_id"]  # Batter 4 at short, Batter 2 at second
        raw["tags"] = {ss: {"SS": [100, 50]}, second: {"2B": [100, 25]}}  # half caught with him at short
        raw["tags_club"] = {"SS": [120, 30], "2B": [120, 30], "3B": [40, 10]}  # the club caught a quarter
        team = simulator.Team(raw, "T")
        self.assertLess(team.infielder_steals(ss, "SS"), 1.0)            # runners safe less often with him there
        self.assertAlmostEqual(team.infielder_steals(second, "2B"), 1.0, places=6)  # the club's rate: neutral
        tag = team.running_state(team.defense, team.rotation[0])["tag"]
        self.assertAlmostEqual(tag[1], team.infielder_steals(ss, "SS") ** 0.5)  # second: an equal share each
        self.assertEqual(tag[2], 1.0)                                    # third: no record for him
        flat = simulator.neutralized(team, "stopping_steals")
        self.assertEqual(flat.running_state(flat.defense, flat.rotation[0])["tag"], {1: 1.0, 2: 1.0})

    def test_a_bench_fielder_does_not_field_the_play(self):
        raw = _sim_team_raw()
        raw["fielding"].append({"_id": 999, "_name": "Bench Glove", "_pos": "SS", "putOuts": 30, "assists": 60,
                                "errors": 0, "innings": "120.0", "gamesPlayed": 20})
        bench = simulator.Team(raw, "T")
        plain = self.team
        p = plain.rotation[0]
        mine = dict((pos, (r, e)) for pos, r, e in bench.fielding_state(bench.defense, bench.rotation[0]))
        theirs = dict((pos, (r, e)) for pos, r, e in plain.fielding_state(plain.defense, p))
        # only the shortstop's range moves (the club's season at short now has him in it); the others don't
        self.assertEqual({pos: v for pos, v in mine.items() if pos != "SS"},
                         {pos: v for pos, v in theirs.items() if pos != "SS"})

    def test_your_lineup_defense_and_starter(self):
        default = self.team
        order = list(default.default_order)
        order[0], order[1] = order[1], order[0]
        raw = _sim_team_raw()
        raw["fielding"].append({"_id": 108, "_name": "Batter 8", "_pos": "SS", "putOuts": 50, "assists": 90,
                                "errors": 9, "innings": "300.0", "gamesPlayed": 40})
        team = simulator.Team(raw, "T", overrides={"order": order, "defense": {"SS": 108, "CF": 999},
                                                   "rotation": [301]})
        self.assertEqual([b.name for b in team.lineup][:2], ["Batter 1", "Batter 0"])
        self.assertEqual(team.defense["SS"]["_name"], "Batter 8")      # he played short
        self.assertEqual(team.defense["CF"]["_name"], "Batter 6")      # 999 never played center: unchanged
        self.assertEqual({p.name for p in team.starters(10)}, {"Setup"})  # a rotation of one: every game
        self.assertAlmostEqual(team.rotation[0].bf_start, team.rotation[0].bf_relief)
        five = simulator.Team(_sim_team_raw(), "T", overrides={"rotation": [204, 202, 200]})
        self.assertEqual([p.name for p in five.starters(5)],
                         ["Starter 4", "Starter 2", "Starter 0", "Starter 4", "Starter 2"])  # strict turn
        self.assertEqual(five.start_shares()[204], 1 / 3)
        cards = simulator.player_cards(five, self.env)
        left_out = next(c for c in cards if c["name"] == "Starter 1")
        self.assertTrue(any("not in your rotation" in line for line in left_out["used"]))
        same = simulator.Team(_sim_team_raw(), "T", overrides={"rotation": list(default.default_rotation)})
        self.assertTrue(same.custom_rotation)  # the default pitchers entered as yours go in strict turn
        self.assertFalse(simulator.Team(_sim_team_raw(), "T", overrides={"rotation": []}).custom_rotation)
        self.assertTrue(any("chosen by you" in n for n in team.notes))
        with_spot = list(default.default_order)
        with_spot[8] = "P"
        self.assertIsNone(simulator.Team(_sim_team_raw(), "T", overrides={"order": with_spot}).lineup[8])

    def _pinch_team(self):
        """Batter 2 (who plays 2B) is a Slow regular; on the bench, Speedster (Speedy bench, never played the field)
        and Glove (Smart regulars, a better second baseman than anyone)."""
        raw = _sim_team_raw()
        raw["hitting"] += [{"_id": 500, "_name": "Speedster", "_league": "AL", "plateAppearances": 0, "gamesPlayed": 90},
                           {"_id": 501, "_name": "Glove", "_league": "AL", "plateAppearances": 120, "hits": 25,
                            "homeRuns": 0, "baseOnBalls": 8, "strikeOuts": 25, "atBats": 110, "groundOuts": 40,
                            "airOuts": 30}]
        raw["fielding"].append({"_id": 501, "_name": "Glove", "_pos": "2B", "putOuts": 150, "assists": 250,
                                "errors": 2, "innings": "400.0", "gamesPlayed": 60})
        speed = {102: {"group": "Slow regulars", "rate": 0.05}, 500: {"group": "Speedy bench", "rate": 0.3},
                 501: {"group": "Smart regulars", "rate": 0.12}}
        return simulator.Team(raw, "T", speed)

    def test_pinch_runners_are_lined_up_by_the_speed_tab(self):
        team = self._pinch_team()
        self.assertEqual(team.slow, {102})
        self.assertEqual([b.name for b in team.pinch_runners], ["Speedster", "Glove"])
        self.assertGreater(team.defense_rating(501, "2B"), team.defense_rating(102, "2B"))
        self.assertIsNone(team.defense_rating(500, "2B"))

    def test_a_pinch_runner_then_the_best_glove(self):
        team = self._pinch_team()
        game = simulator.Game(self.env, (team, team), (team.rotation[0], team.rotation[0]), random.Random(1),
                              {"batting": ({}, {}), "pitching": ({}, {})}, [])
        game.inning = 8
        slow = team.lineup[2]
        bases = [simulator._Runner(slow.pid, slow.name, slow.running, team.rotation[0]), None, None]
        game._pinch_run(0, bases)
        self.assertEqual(bases[0].name, "Speedster")
        self.assertEqual(game.lineups[0][2].name, "Speedster")
        self.assertEqual(game.stats["batting"][0][500]["PR"], 1)
        game._settle_defense(0)  # Speedster never played second: Glove takes it and his place in the order
        self.assertEqual(game.lineups[0][2].name, "Glove")
        self.assertEqual(game.defense[0]["2B"]["_name"], "Glove")
        self.assertEqual(game.stats["batting"][0][501]["DEF"], 1)
        before = team.fielding_state(team.defense, team.rotation[0])
        after = team.fielding_state(game.defense[0], team.rotation[0])
        self.assertNotEqual(dict((p, r) for p, r, _e in after)["2B"], dict((p, r) for p, r, _e in before)["2B"])

    def test_no_pinch_runner_early_or_in_a_rout(self):
        team = self._pinch_team()
        game = simulator.Game(self.env, (team, team), (team.rotation[0], team.rotation[0]), random.Random(1),
                              {"batting": ({}, {}), "pitching": ({}, {})})
        slow = team.lineup[2]
        for inning, score in ((7, [0, 0]), (8, [5, 0])):
            game.inning, game.score = inning, score
            bases = [simulator._Runner(slow.pid, slow.name, slow.running, team.rotation[0]), None, None]
            game._pinch_run(0, bases)
            self.assertEqual(bases[0].name, slow.name)

    def test_pinch_running_shows_on_the_cards(self):
        team = self._pinch_team()
        cards = simulator.player_cards(team, self.env)
        slow = next(c for c in cards if c["kind"] == "batter" and c["name"] == "Batter 2")
        self.assertTrue(any("Slow regular" in line for line in slow["used"]))
        bench = [c for c in cards if c["kind"] == "bench"]
        self.assertEqual([c["name"] for c in bench], ["Speedster", "Glove"])
        self.assertTrue(bench[0]["used"][0].startswith("Pinch running: first in line to run (Speedy bench"))
        self.assertTrue(any(line.startswith("Pinch running: can take 2B") for line in bench[1]["used"]))

    def test_path_analysis_leaves_the_teams_alone(self):
        before = dict(self.team.lineup[0].rates)
        pa = simulator.path_analysis(self.team, self.team, self.env, 20, 3)
        self.assertEqual(self.team.lineup[0].rates, before)
        # the chain from outs to runs: the counts agree with the run's own lines, and a part that changed nothing
        # changed no link
        run = simulator.simulate(self.team, self.team, self.env, 20, 3)
        for t in (0, 1):
            lines = run["lines"][t]
            self.assertEqual(sum(lines["PA"]), simulator._team_totals(run, t)["PA"])
            self.assertEqual([r + o for r, o in zip(lines["reached"], lines["outs_plate"])], list(lines["PA"]))
            outs = [a + b for a, b in zip(lines["outs_plate"], lines["outs_bases"])]
            self.assertTrue(all(20 <= o <= 3 * simulator.MAX_INNINGS for o in outs), outs)
        same = simulator.chain_effect(run, run, 0)
        self.assertEqual({k: e["d"] for k, e in same.items()}, dict.fromkeys(same, 0.0))
        self.assertIn("chain", pa["effects"][0]["fielding"])
        self.assertEqual(set(pa["effects"][0]["managing"]["chain"]), {"bat", "field"})
        self.assertEqual(set(pa["chain_base"][0]), {k for k, _l, _m in simulator.CHAIN_STAGES})
        # a newer run replacing this one stops it part way
        asked = []
        with self.assertRaises(simulator.Cancelled):
            simulator.simulate(self.team, self.team, self.env, 200, 3, cancelled=lambda: asked.append(1) or len(asked) > 3)
        self.assertEqual(len(asked), 4)  # asked every 1% of the games, stopped at the first yes
        with self.assertRaises(simulator.Cancelled):
            simulator.path_analysis(self.team, self.team, self.env, 20, 3, cancelled=lambda: True)
        # the run itself as the baseline (the same games): the same analysis, one replay fewer
        run = simulator.simulate(self.team, self.team, self.env, 20, 3)
        reused = simulator.path_analysis(self.team, self.team, self.env, 20, 3, base=run)
        self.assertIs(reused["base"], run)
        self.assertEqual(reused["effects"], pa["effects"])
        cats = {c for p in simulator.PATH_PARTS for c in simulator.PATH_CATEGORIES[p]}
        self.assertEqual(set(pa["effects"][0]), set(simulator.PATH_PARTS) | cats | {"managing", "home"}
                         | set(simulator.MANAGING_CATEGORIES) | set(simulator.HOME_CATEGORIES))
        self.assertTrue(all(pa["effects"][0][k]["margin"] for k in simulator.MANAGING_CATEGORIES))
        # each managing row switches off just its own decision, for that team; "managing" all of them
        self.assertEqual(simulator.neutralized(self.team, "sacrifices").off, {"sacrifices"})
        self.assertEqual(simulator.neutralized(self.team, "managing").off, set(simulator.MANAGING_CATEGORIES))
        self.assertEqual(self.team.off, frozenset())
        self.assertTrue(self.env.without_edge().edge_off)
        self.assertFalse(self.env.edge_off)
        steals = simulator.neutralized(self.team, "steals")
        r0, r1 = self.team.lineup[0].running, steals.lineup[0].running
        self.assertEqual((r1["att"], r1["succ"]), (1.0, 1.0))
        self.assertEqual((r1["xb"], r1["tu"]), (r0["xb"], r0["tu"]))
        self.assertEqual(steals.lineup[0].gdp, self.team.lineup[0].gdp)
        state = lambda t: t.fielding_state(t.defense, t.rotation[0])
        errors = simulator.neutralized(self.team, "errors")
        self.assertTrue(all(e == 1.0 for _p, _r, e in state(errors)))
        self.assertEqual([r for _p, r, _e in state(errors)], [r for _p, r, _e in state(self.team)])
        fielding = simulator.neutralized(self.team, "fielding")
        club, vol = self.team.club_range, self.team.bip_volume
        self.assertTrue(all(abs(r - (vol / club[p] if club.get(p) else 1.0)) < 1e-12 for p, r, _e in state(fielding)))
        # league pitchers and MLB-average fielders: nothing to take out, nothing to add
        flat = simulator.neutralized(simulator.neutralized(self.team, "fielding"), "pitching")
        self.assertTrue(all(r == 1.0 and e == 1.0 for _p, r, e in state(flat)))
        alt = simulator.neutralized(self.team, "pitching")
        self.assertEqual(alt.rotation[0].rates, self.team.league)
        self.assertIsNot(alt.rotation[0], self.team.rotation[0])
        self.assertIn(alt.closer, alt.pitchers)


# --------------------------------------------------------------------------- #
#  Uptodate: this year's play-by-play from MLB's feed, in Retrosheet's layout  #
# --------------------------------------------------------------------------- #
_UP_POS = {1: "8", 2: "6", 3: "9", 4: "3", 5: "10", 6: "7", 7: "5", 8: "4", 9: "2"}  # lineup spot -> position


def _up_person(pid):
    return {"id": pid, "fullName": f"Player {pid}", "lastName": f"Last{pid}", "useName": f"First{pid}",
            "birthDate": "1990-01-01", "batSide": {"code": "R"}, "pitchHand": {"code": "R"}}


def _up_pitch(code, balls, strikes, going=False, hit=None):
    ev = {"isPitch": True, "type": "pitch", "count": {"balls": balls, "strikes": strikes},
          "details": {"call": {"code": code}, "code": code, "isInPlay": code in ("X", "D", "E")}}
    if going:
        ev["details"]["runnerGoing"] = True
    if hit:
        ev["hitData"] = hit
    return ev


def _up_runner(pid, start, end, event, index, out=False, out_base=None, out_number=None, credits=(),
               reason=None, rbi=False, earned=True):
    return {"movement": {"originBase": start, "start": start, "end": None if out else end, "outBase": out_base,
                         "isOut": out, "outNumber": out_number},
            "details": {"event": event, "eventType": event, "movementReason": reason, "runner": {"id": pid},
                        "responsiblePitcher": None, "isScoringEvent": end == "score", "rbi": rbi,
                        "earned": earned, "teamUnearned": False, "playIndex": index},
            "credits": [{"player": {"id": 0}, "position": {"code": c}, "credit": kind} for c, kind in credits]}


def _up_play(inning, top, batter, pitcher, event, events, runners, outs, desc="", away=0, home=0):
    return {"result": {"type": "atBat", "event": event, "eventType": event, "description": desc,
                       "awayScore": away, "homeScore": home},
            "about": {"inning": inning, "halfInning": "top" if top else "bottom", "isTopInning": top,
                      "isComplete": True, "startTime": "2026-04-01T20:00:00Z"},
            "count": {"outs": outs}, "matchup": {"batter": {"id": batter}, "batSide": {"code": "R"},
                                                  "pitcher": {"id": pitcher}, "pitchHand": {"code": "R"}},
            "playEvents": events, "runners": runners}


def _up_feed():
    """A one-inning game: the visitors single, steal, strike out, walk and hit into a 6-4-3 double play;
    the home side homers, singles (and a pinch runner comes in), flies out, strikes out and grounds out."""
    ground_cf = {"trajectory": "ground_ball", "hardness": "medium", "coordinates": {"coordX": 125.7, "coordY": 80.0}}
    ground_ss = {"trajectory": "ground_ball", "hardness": "medium", "coordinates": {"coordX": 110.0, "coordY": 150.0}}
    fly_hr = {"trajectory": "fly_ball", "hardness": "hard", "coordinates": {"coordX": 125.7, "coordY": 20.0}}
    line_lf = {"trajectory": "line_drive", "hardness": "medium", "coordinates": {"coordX": 80.0, "coordY": 110.0}}
    fly_rf = {"trajectory": "fly_ball", "hardness": "medium", "coordinates": {"coordX": 175.0, "coordY": 95.0}}
    ground_3b = {"trajectory": "ground_ball", "hardness": "soft", "coordinates": {"coordX": 100.0, "coordY": 170.0}}
    plays = [
        _up_play(1, True, 1, 200, "single", [_up_pitch("B", 1, 0), _up_pitch("D", 1, 0, hit=ground_cf)],
                 [_up_runner(1, None, "1B", "single", 1, credits=[("8", "f_fielded_ball")])], 0),
        _up_play(1, True, 2, 200, "strikeout",
                 [_up_pitch("C", 0, 1, going=True),
                  {"isPitch": False, "type": "action", "details": {"eventType": "stolen_base_2b"}, "count": {}},
                  _up_pitch("B", 1, 1), _up_pitch("S", 1, 2), _up_pitch("S", 1, 3)],
                 [_up_runner(1, "1B", "2B", "stolen_base_2b", 1, reason="r_stolen_base_2b"),
                  _up_runner(2, None, None, "strikeout", 4, out=True, out_base="1B", out_number=1,
                             credits=[("2", "f_putout")])], 1),
        _up_play(1, True, 3, 200, "walk", [_up_pitch("B", 1, 0), _up_pitch("B", 2, 0), _up_pitch("B", 3, 0),
                                           _up_pitch("B", 4, 0)],
                 [_up_runner(3, None, "1B", "walk", 3)], 1),
        _up_play(1, True, 4, 200, "grounded_into_double_play", [_up_pitch("X", 0, 0, hit=ground_ss)],
                 [_up_runner(3, "1B", None, "grounded_into_double_play", 0, out=True, out_base="2B", out_number=2,
                             credits=[("6", "f_assist"), ("4", "f_putout")], reason="r_force_out"),
                  _up_runner(4, None, None, "grounded_into_double_play", 0, out=True, out_base="1B", out_number=3,
                             credits=[("4", "f_assist"), ("3", "f_putout")])], 3),
        _up_play(1, False, 11, 100, "home_run", [_up_pitch("E", 0, 0, hit=fly_hr)],
                 [_up_runner(11, None, "score", "home_run", 0, rbi=True)], 0, away=0, home=1),
        _up_play(1, False, 12, 100, "single", [_up_pitch("D", 0, 0, hit=line_lf)],
                 [_up_runner(12, None, "1B", "single", 0, credits=[("7", "f_fielded_ball")])], 0),
        _up_play(1, False, 13, 100, "field_out",
                 [{"isPitch": False, "type": "action", "isSubstitution": True, "player": {"id": 30},
                   "position": {"code": "12"}, "battingOrder": "201", "replacedPlayer": {"id": 12},
                   "details": {"eventType": "offensive_substitution"}, "count": {}},
                  _up_pitch("X", 0, 0, hit=fly_rf)],
                 [_up_runner(13, None, None, "field_out", 1, out=True, out_base="1B", out_number=1,
                             credits=[("9", "f_putout")])], 1),
        _up_play(1, False, 14, 100, "strikeout", [_up_pitch("C", 0, 1), _up_pitch("C", 0, 2), _up_pitch("C", 0, 3)],
                 [_up_runner(14, None, None, "strikeout", 2, out=True, out_base="1B", out_number=2,
                             credits=[("2", "f_putout")])], 2),
        _up_play(1, False, 15, 100, "field_out", [_up_pitch("X", 0, 0, hit=ground_3b)],
                 [_up_runner(15, None, None, "field_out", 0, out=True, out_base="1B", out_number=3,
                             credits=[("5", "f_assist"), ("3", "f_putout")])], 3),
    ]

    def side(first, pitcher):
        players = {f"ID{first + i - 1}": {"person": {"id": first + i - 1}, "battingOrder": f"{i}00",
                                          "allPositions": [{"code": _UP_POS[i]}]} for i in range(1, 10)}
        players[f"ID{pitcher}"] = {"person": {"id": pitcher}, "allPositions": [{"code": "1"}]}
        return {"players": players, "pitchers": [pitcher], "teamStats": {"batting": {}}}

    home = side(11, 100 + 100)
    away = side(1, 100)
    home["players"]["ID30"] = {"person": {"id": 30}, "battingOrder": "201", "allPositions": [{"code": "12"}]}
    people = [_up_person(pid) for pid in list(range(1, 10)) + list(range(11, 20)) + [30, 100, 200]]
    return {"gameData": {"datetime": {"officialDate": "2026-04-01", "originalDate": "2026-04-01", "time": "7:10",
                                      "ampm": "PM", "dayNight": "night"},
                         "teams": {"away": {"id": 147}, "home": {"id": 111}},
                         "players": {f"ID{p['id']}": p for p in people},
                         "venue": {"id": 3, "name": "Fenway Park", "timeZone": {"offsetAtGameTime": -4}},
                         "weather": {"condition": "Partly Cloudy", "temp": "55", "wind": "8 mph, Out To CF"},
                         "gameInfo": {"attendance": 30000, "gameDurationMinutes": 30}},
            "liveData": {"plays": {"allPlays": plays},
                         "linescore": {"scheduledInnings": 9, "teams": {"away": {"runs": 0}, "home": {"runs": 1}},
                                       "innings": [{"num": 1, "away": {"runs": 0}, "home": {"runs": 1}}]},
                         "boxscore": {"teams": {"away": away, "home": home},
                                      "officials": [{"official": {"id": 427344, "fullName": "Bill Miller"},
                                                     "officialType": "Home Plate"}]},
                         "decisions": {"winner": {"id": 100}, "loser": {"id": 200}}}}


def _up_rid(pid):
    return f"p{pid:03d}"


def _up_game():
    return {"pk": 1, "date": "2026-04-01", "home": 111, "away": 147, "doubleheader": "N", "game_number": 1,
            "type": "R", "venue": 3, "venue_name": "Fenway Park", "officials": []}


class UptodatePlays(unittest.TestCase):
    def setUp(self):
        self.conv = uptodate.GameConverter(_up_feed(), "BOS202604010", _up_rid, "BOS07",
                                           {"umphome": "millb901"}, "regular")
        self.rows = self.conv.convert_plays()

    def test_rows_are_split_as_retrosheet_splits_them(self):
        self.assertEqual([r["event"] for r in self.rows],
                         ["S8/G6M", "SB2", "K", "W", "64(1)3/GDP/G6", "HR/F8XD+", "S7/L7", "NP", "9/F9", "K", "53/G5-"])

    def test_pitches_count_and_steal_row(self):
        sb, k = self.rows[1], self.rows[2]
        self.assertEqual((sb["pitches"], sb["count"], sb["pa"], sb["sb2"]), (">C", "0", "0", "1"))
        self.assertEqual((sb["br1_pre"], sb["br2_post"]), ("p001", "p001"))
        self.assertEqual((k["pitches"], k["count"], k["nump"], k["pa"], k["k"]), (">C.BSS", "12", "3", "1", "1"))

    def test_runners_outs_and_left_on_base(self):
        walk, gdp = self.rows[3], self.rows[4]
        self.assertEqual((walk["br1_post"], walk["br2_post"]), ("p003", "p001"))
        self.assertEqual((gdp["outs_pre"], gdp["outs_post"], gdp["gdp"], gdp["dpopp"], gdp["pivot"]), ("1", "3", "1", "1", "4"))
        self.assertEqual((gdp["lob_id1"], gdp["br2_post"]), ("p001", ""))
        self.assertEqual((gdp["po4"], gdp["po3"], gdp["a6"], gdp["a4"], gdp["fseq"], gdp["batout1"]),
                         ("1", "1", "1", "1", "643", "6"))

    def test_home_run_scores_and_drives_in(self):
        hr = self.rows[5]
        self.assertEqual((hr["run_b"], hr["prun_b"], hr["rbi_b"], hr["runs"], hr["rbi"], hr["er"], hr["bip"], hr["fly"]),
                         ("p011", "p100", "1", "1", "1", "1", "0", "1"))
        self.assertEqual(self.rows[6]["score_h"], "1")

    def test_pinch_runner_gets_an_np_row_then_is_on_base(self):
        np_row, fly = self.rows[7], self.rows[8]
        self.assertEqual((np_row["br1_pre"], np_row["br1_post"], np_row["pa"]), ("p012", "p012", "0"))
        self.assertEqual((fly["br1_pre"], fly["lf2"], fly["pitches"]), ("p030", "12", ".X"))

    def test_lineup_fielders_and_hands(self):
        first = self.rows[0]
        self.assertEqual((first["l1"], first["lf1"], first["lf5"], first["f2"], first["f8"]), ("p001", "8", "10", "p019", "p011"))
        self.assertEqual((first["bathand"], first["umphome"], first["site"], first["gametype"]), ("R", "millb901", "BOS07", "regular"))

    def test_box_score_files(self):
        box = self.conv.convert_box()
        bat = {r["id"]: r for r in box["batting"]}
        self.assertEqual((bat["p011"]["b_hr"], bat["p011"]["b_r"], bat["p011"]["b_rbi"]), ("1", "1", "1"))
        self.assertEqual((bat["p001"]["b_sb"], bat["p004"]["b_gdp"], bat["p030"]["pr"], bat["p005"]["dh"]), ("1", "1", "1", "1"))
        self.assertEqual(bat["p100"]["b_lp"], "")  # a pitcher who didn't bat is listed, with no lineup spot
        pit = {r["id"]: r for r in box["pitching"]}
        self.assertEqual((pit["p200"]["p_ipouts"], pit["p200"]["p_bfp"], pit["p200"]["p_k"], pit["p200"]["lp"]),
                         ("3", "4", "1", "1"))
        self.assertEqual((pit["p100"]["p_r"], pit["p100"]["p_er"], pit["p100"]["wp"], pit["p100"]["p_cg"]), ("1", "1", "1", "1"))
        team = {r["team"]: r for r in box["teamstats"]}
        # left on base as the box score proves it: plate appearances - runs - the other side's putouts
        self.assertEqual((team["NYA"]["lob"], team["BOS"]["lob"]), ("1", "1"))
        self.assertEqual((team["BOS"]["inn1"], team["BOS"]["inn2"], team["BOS"]["start_f10"]), ("1", "", "p015"))
        fld = {(r["id"], r["d_pos"]): r for r in box["fielding"]}
        self.assertEqual((fld[("p012", "6")]["d_dp"], fld[("p012", "6")]["d_a"], fld[("p012", "6")]["d_ifouts"]),
                         ("1", "1", "3"))
        self.assertEqual(box["fielding"][0]["id"], "p100")  # the visitors, pitchers first, as Retrosheet lists them

    def test_gameinfo(self):
        info = self.conv.convert_gameinfo()
        self.assertEqual((info["visteam"], info["hometeam"], info["starttime"], info["sky"], info["winddir"],
                          info["windspeed"], info["wp"], info["wteam"], info["usedh"], info["ump1b"]),
                         ("NYA", "BOS", "7:10PM", "cloudy", "tocf", "8", "p100", "BOS", "true", "(none)"))


class UptodateParts(unittest.TestCase):
    def test_made_up_ids_look_like_retrosheets_and_stay_clear_of_theirs(self):
        self.assertEqual(uptodate.make_id("Núñez", "Renato", set()), "nuner999")
        self.assertEqual(uptodate.make_id("Li", "Bo", {"li--b999"}), "li--b998")
        self.assertEqual(uptodate.make_id("Smith", "Al", set(), umpire=True), "smita999")

    def test_hit_locations_on_retrosheets_diagram(self):
        self.assertEqual(uptodate.hit_location({"coordX": 125.7, "coordY": 20.0}, home_run=True), "8XD")
        self.assertEqual(uptodate.hit_location({"coordX": 110.0, "coordY": 150.0}, ground=True), "6")
        # a ground ball picked up in the outfield is placed where it went through the infield
        self.assertEqual(uptodate.hit_location({"coordX": 125.7, "coordY": 80.0}, ground=True), "6M")
        self.assertEqual(uptodate.hit_location(None), "")

    def test_game_ids(self):
        self.assertEqual(uptodate.game_id(_up_game()), "BOS202604010")
        self.assertEqual(uptodate.game_id(dict(_up_game(), doubleheader="Y", game_number=2)), "BOS202604012")
        self.assertEqual(uptodate.game_id(_up_game(), "2026-03-31"), "BOS202603310")

    def test_all_players_counts_games_by_position(self):
        files = {"batting": [{"id": "a", "team": "BOS", "gid": "g1", "date": "20260401", "dh": "1", "ph": "", "pr": ""},
                             {"id": "a", "team": "BOS", "gid": "g2", "date": "20260402", "dh": "", "ph": "1", "pr": ""}],
                 "fielding": [{"id": "a", "team": "BOS", "gid": "g2", "date": "20260402", "d_pos": "7"}],
                 "pitching": []}
        row = uptodate.all_players(files, {"a": ["Last", "First", "L", "R"]})[0]
        self.assertEqual((row["g"], row["g_dh"], row["g_ph"], row["g_lf"], row["g_of"], row["first_g"], row["last_g"]),
                         ("2", "1", "1", "1", "1", "20260401", "20260402"))


class UptodateFile(unittest.TestCase):
    """update(): the season's file is made, then only new games are added."""

    def _fetch(self, games):
        feed = _up_feed()

        def fetch(url):
            if "/schedule" in url:
                return {"dates": [{"date": g["date"], "games": [
                    {"gamePk": g["pk"], "gameType": "R", "officialDate": g["date"], "doubleHeader": "N", "gameNumber": 1,
                     "status": {"codedGameState": "F"},
                     "teams": {"away": {"team": {"id": 147}}, "home": {"team": {"id": 111}}},
                     "venue": {"id": 3, "name": "Fenway Park"}}]} for g in games]}
            if "/people" in url:
                ids = url.split("personIds=")[1].split("&")[0].split(",")
                return {"people": [dict(_up_person(int(i)), xrefIds=[{"xrefType": "retrosheet", "xrefId": f"r{int(i):03d}"}])
                                   for i in ids]}
            if "/coaches" in url:
                return {"roster": [{"job": "Manager", "person": {"id": 900, "fullName": "Manny Ager"}}]}
            if "/feed/live" in url:
                pk = int(url.split("/game/")[1].split("/")[0])
                f = json.loads(json.dumps(feed))
                date = next(g["date"] for g in games if g["pk"] == pk)
                f["gameData"]["datetime"].update(officialDate=date, originalDate=date)
                return f
            raise AssertionError(url)
        return fetch

    def test_made_then_appended_without_downloading_games_again(self):
        games = [{"pk": 1, "date": "2026-04-01"}, {"pk": 2, "date": "2026-04-02"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "2026csvs.zip")
            seen = []
            fetch = self._fetch(games[:1])
            first = uptodate.update(2026, path, fetch=lambda u: (seen.append(u), fetch(u))[1], bio=b"")
            self.assertEqual((first["added"], first["games"]), (1, 1))
            fetch = self._fetch(games)
            seen.clear()
            progress = []
            second = uptodate.update(2026, path, fetch=lambda u: (seen.append(u), fetch(u))[1], bio=b"",
                                     progress=lambda d, t, text: progress.append((d, t)))
            self.assertEqual((second["added"], second["games"]), (1, 2))
            self.assertEqual(sum("/feed/live" in u for u in seen), 1)  # only the new game
            self.assertIn((1, 1), progress)
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                self.assertEqual(names, {f"2026{n}.csv" for n in uptodate.FILES} | {"uptodate.json"})
                header = z.read("2026plays.csv").decode().splitlines()[0]
            self.assertEqual(header.split(","), uptodate.HEADERS["plays"])
            files, meta = uptodate.read_bundle(path)
            self.assertEqual([r["gid"] for r in files["gameinfo"]], ["BOS202604010", "BOS202604020"])
            self.assertEqual({r["mgr"] for r in files["teamstats"]}, {"r900"})
            self.assertEqual(uptodate.file_summary(path)["games"], 2)
            # the bundle reads as Retrosheet's does
            with open(path, "rb") as fh:
                teams = retrosheet.build_team_season(2026, fh.read())
            self.assertEqual(teams["teams"]["BOS"]["w"], 2)
            uptodate.delete_file(path)
            self.assertIsNone(uptodate.file_summary(path))

    def test_retrosheet_falls_back_to_the_file_until_it_publishes(self):
        games = [{"pk": 1, "date": "2026-04-01"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "2026csvs.zip")
            uptodate.update(2026, path, fetch=self._fetch(games), bio=b"")
            old_path, old_open, old_pub = uptodate.default_path, retrosheet.urllib.request.urlopen, dict(retrosheet._PUBLISHED)

            def not_published(*_a, **_k):
                raise retrosheet.urllib.error.HTTPError("u", 404, "no", {}, io.BytesIO(b""))
            try:
                uptodate.default_path = lambda season: path if season == 2026 else os.path.join(tmp, "none.zip")
                retrosheet.urllib.request.urlopen = not_published
                retrosheet._PUBLISHED[2026] = False
                data = retrosheet.download_season(2026)
                self.assertIsInstance(data, retrosheet.LocalBundle)
                cache = os.path.join(tmp, "cache")
                teams = retrosheet.load_team_season(2026, cache)
                self.assertEqual(teams["teams"]["BOS"]["w"], 1)
                self.assertTrue(retrosheet.is_team_cached(cache, 2026))
                os.utime(path, (1, 1))  # the file changed: its summaries are made again
                self.assertFalse(retrosheet.is_team_cached(cache, 2026))
                retrosheet.load_team_season(2026, cache)
                retrosheet._PUBLISHED[2026] = True  # Retrosheet's own is out: the file's summaries are dropped
                self.assertFalse(retrosheet.is_team_cached(cache, 2026))
                os.remove(path)
                with self.assertRaises(retrosheet.RetrosheetError):
                    retrosheet.download_season(2026)
            finally:
                uptodate.default_path, retrosheet.urllib.request.urlopen = old_path, old_open
                retrosheet._PUBLISHED.clear()
                retrosheet._PUBLISHED.update(old_pub)


def _platoon_cells(scale=None):
    """Retrosheet-style plate appearances by bat side and pitch hand; ``scale`` {(side, hand): {result: factor}}."""
    base = {"K": 200, "BB": 80, "HBP": 10, "1B": 150, "2B": 45, "3B": 5, "HR": 30, "ROE": 10, "GO": 220, "AO": 250}
    out = {}
    for s in "LR":
        for h in "LR":
            f = (scale or {}).get((s, h), {})
            out[s + h] = {k: round(v * f.get(k, 1.0)) * (3 if s == "R" else 1) for k, v in base.items()}
    return out


class SimulatorPlatoon(unittest.TestCase):
    # the opposite hand hits more home runs and strikes out less
    EDGE = {("L", "R"): {"HR": 1.3, "K": 0.85}, ("R", "L"): {"HR": 1.3, "K": 0.85}}

    def _team(self, cells=None, hands=None, splits=None, team_id=1):
        raw = _sim_team_raw(team_id=team_id)
        raw["platoon_cells"] = _platoon_cells(self.EDGE) if cells is None else cells
        raw["hands"] = hands if hands is not None else {
            **{100 + i: {"bats": "LRS"[i % 3], "throws": "R"} for i in range(9)},
            **{p["_id"]: {"bats": "R", "throws": "L" if p["_id"] % 2 else "R"} for p in raw["pitching"]}}
        raw["platoon"] = splits
        return simulator.Team(raw, f"Platoon {team_id}")

    def test_ratios_and_hands(self):
        got = simulator.platoon_ratios(_platoon_cells(self.EDGE))
        self.assertGreater(got["ratio"][("L", "R")]["HR"], 1.0)
        self.assertLess(got["ratio"][("L", "L")]["HR"], 1.0)
        self.assertEqual(got["n"][("R", "R")], 3 * got["n"][("L", "L")])
        self.assertIsNone(simulator.platoon_ratios({"LL": {"K": 1}}))
        flat = simulator.platoon_ratios(_platoon_cells())
        self.assertTrue(all(abs(x - 1.0) < 1e-9 for r in flat["ratio"].values() for x in r.values()))
        self.assertEqual(simulator.matchup_hands("S", "L"), ("R", "L"))   # a switch hitter bats away from him
        self.assertEqual(simulator.matchup_hands("L", "S"), ("L", "L"))   # a switch pitcher matches the batter
        self.assertEqual(simulator.matchup_hands("S", "S"), ("L", "R"))
        self.assertIsNone(simulator.matchup_hands("", "R"))

    def test_flat_cells_and_no_splits_play_as_before(self):
        plain = simulator.Team(_sim_team_raw(), "Plain")
        flat = self._team(cells=_platoon_cells())
        self.assertIsNotNone(flat.platoon)
        env, env0 = simulator.Environment(flat, simulator.Tables(None), None), \
            simulator.Environment(plain, simulator.Tables(None), None)
        for b, b0 in zip(flat.lineup, plain.lineup):
            for o in "LR":
                for k in simulator.LOG5_CLASSES:
                    self.assertAlmostEqual(b.platoon[o][k], b0.rates[k])
        p = flat.rotation[0]
        got = simulator.platoon_matchup(flat.lineup[0].bats, flat.lineup[0].rates, flat.lineup[0].platoon, p, flat,
                                        flat, env)
        mixed = simulator.log5(got[0], flat.league, got[1], flat.league, got[2])
        before = simulator.log5(plain.lineup[0].rates, plain.league, plain.rotation[0].rates, plain.league, env0.rates)
        for k in simulator.LOG5_CLASSES:
            self.assertAlmostEqual(mixed[k], before[k])

    def test_the_edge_is_counted_once(self):
        team = self._team()
        env = simulator.Environment(team, simulator.Tables(None), None)
        lefty = next(b for b in team.lineup if b.bats == "L")
        rhp = next(p for p in team.pitchers if p.throws == "R")
        lhp = copy.copy(rhp)  # the same pitcher's figures, throwing left
        lhp.throws = "L"
        hr = lambda p: simulator.log5(*(lambda g: (g[0], team.league, g[1], team.league, g[2]))(
            simulator.platoon_matchup(lefty.bats, lefty.rates, lefty.platoon, p, team, team, env)))["HR"]
        ratio = team.platoon["ratio"]
        edge = ratio[("L", "R")]["HR"] / ratio[("L", "L")]["HR"]  # the league's edge (1.3, per plate appearance)
        self.assertAlmostEqual(hr(rhp) / hr(lhp), edge, delta=0.02)  # counted once
        # the same season line is worse from a right-hander, who faced mostly right-handed batters (no edge for them)
        real_lhp = next(p for p in team.pitchers if p.throws == "L" and p.rates == rhp.rates)
        self.assertGreater(rhp.platoon["R"]["HR"], real_lhp.platoon["R"]["HR"])
        # without anyone's hand known: no platoon at all
        none = self._team(hands={})
        self.assertIsNone(none.platoon)
        self.assertIsNone(none.lineup[0].platoon)

    def test_splits_move_a_player_a_little(self):
        pid = 101  # bats right
        row = next(r for r in _sim_team_raw()["hitting"] if r["_id"] == pid)
        vs_l = dict(row, plateAppearances=150, atBats=130, hits=60, homeRuns=12, baseOnBalls=20, strikeOuts=15)
        vs_r = dict(row, plateAppearances=430, atBats=390, hits=80, homeRuns=7, baseOnBalls=40, strikeOuts=105)
        team = self._team(splits={"hitting": {pid: {"L": vs_l, "R": vs_r}}, "pitching": {}})
        flat = self._team()
        b = next(x for x in team.lineup if x.pid == pid)
        b0 = next(x for x in flat.lineup if x.pid == pid)
        self.assertGreater(b.platoon["L"]["HR"], b0.platoon["L"]["HR"])
        self.assertLess(b.platoon["R"]["HR"], b0.platoon["R"]["HR"])
        # ... but only a little: 150 PA against 2,200 of his whole season
        self.assertLess(b.platoon["L"]["HR"] / b0.platoon["L"]["HR"], 1.25)
        # league-average for the path analysis keeps the hand effect, drops his own split
        n = simulator.neutralized(team, "platoon_batting")
        nb = next(x for x in n.lineup if x.pid == pid)
        for k in simulator.LOG5_CLASSES:
            self.assertAlmostEqual(nb.platoon["L"][k], b0.platoon["L"][k])
        self.assertIsNot(b.platoon, nb.platoon)

    def test_parse_platoon_splits(self):
        payload = {"stats": [{"splits": [
            {"split": {"code": "vl"}, "player": {"id": 7}, "stat": {"plateAppearances": 100}},
            {"split": {"code": "vr"}, "player": {"id": 7}, "stat": {"plateAppearances": 300}},
            {"split": {"code": "b1"}, "player": {"id": 7}, "stat": {"plateAppearances": 9}}]}]}
        got = mlbstats.parse_platoon_splits(payload)
        self.assertEqual(got, {7: {"L": {"plateAppearances": 100}, "R": {"plateAppearances": 300}}})

    def test_games_and_cards_play_with_platoon(self):
        a, b = self._team(), self._team(team_id=2)
        envs = (simulator.Environment(a, simulator.Tables(None), None),
                simulator.Environment(b, simulator.Tables(None), None))
        res = simulator.simulate(a, b, envs, 20, 5)
        self.assertEqual(sum(res["wins"]) + res["ties"], 20)
        log, _score, _host = simulator.replay(a, b, envs, 20, 5, 0)
        self.assertTrue(any(line.get("hands") for line in log if line["kind"] == "pa"))
        cards = simulator.player_cards(a, envs[0], res, 0)
        batter = next(c for c in cards if c["kind"] == "batter")
        self.assertEqual(batter["tables"][0]["rows"][0]["cat"], "platoon_batting")
        pitcher = next(c for c in cards if c["kind"] == "pitcher")
        self.assertEqual(pitcher["tables"][0]["rows"][0]["cat"], "platoon_pitching")


if __name__ == "__main__":
    unittest.main(verbosity=2)
