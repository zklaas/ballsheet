#!/usr/bin/env python3
"""Build negro_postseason.json -- the Negro league postseason series the Postseason tab
shows under the MLB bracket -- from Retrosheet's Negro league game file and ballpark list.

    python3 make_negro_postseason.py [negroleagues.zip] [ballparks.zip]

Both are downloaded from retrosheet.org (into ~/.cache/ballsheet) when not given.
SERIES below says which games make up each series, what it was called, whether it counts
as official, and why not; the games themselves (dates, scores, ballparks) come from
Retrosheet.  Titles, results and status follow Wikipedia's "List of Negro league baseball
postseason games", "Negro World Series" and the league articles.

The information used here was obtained free of charge from and is copyrighted by
Retrosheet.  Interested parties may contact Retrosheet at "www.retrosheet.org".
"""
import csv
import io
import json
import os
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "negro_postseason.json")
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "ballsheet")
EARLY_OUT = os.path.join(HERE, "early_poststats.json")
URLS = {"negroleagues.zip": "https://www.retrosheet.org/downloads/negroleagues.zip",
        "ballparks.zip": "https://www.retrosheet.org/ballparks.zip",
        "1900csvs.zip": "https://www.retrosheet.org/downloads/1900/1900csvs.zip"}
# Before 1903 Retrosheet has box scores for one postseason series only: the 1900 Chronicle-Telegraph
# Cup.  Its player totals go to early_poststats.json for the Poststats tab.
EARLY_RETRO = [dict(season=1900, zip="1900csvs.zip", prefix="1900",
                    club_leagues={"BRO": "NL", "PIT": "NL"}, teams={"BRO": "Brooklyn Superbas",
                                                                    "PIT": "Pittsburgh Pirates"})]

TEAMS = {
    "CUX": "Cuban X-Giants", "PHG": "Philadelphia Giants", "CAG": "Chicago American Giants",
    "NYL": "New York Lincoln Giants", "HIL": "Hilldale Club", "KCM": "Kansas City Monarchs",
    "SSN": "St. Louis Stars", "SSA": "St. Louis Stars", "ACY": "Bacharach Giants",
    "BIR": "Birmingham Black Barons", "DT1": "Detroit Stars", "HOM": "Homestead Grays",
    "NSH": "Nashville Elite Giants", "PTC": "Pittsburgh Crawfords", "PH5": "Philadelphia Stars",
    "NY6": "New York Cubans", "WEG": "Washington Elite Giants", "NW2": "Newark Eagles",
    "ATN": "Atlanta Black Crackers", "MEM": "Memphis Red Sox", "BLG": "Baltimore Elite Giants",
    "CVB": "Cleveland Buckeyes",
}

PRE_1920 = ("Before the organized Negro leagues: MLB recognizes them as major leagues only from "
            "1920 to 1948.")

POSTSEASON_TYPES = ("championship", "lcs", "divisionseries", "playoff")

# One entry per series.  "games": (first date, last date) -- the games between the two clubs in
# that window that Retrosheet files as postseason games (not the exhibitions they played along
# the way); "any_type": True takes every game in the window, and "extra_games" names others
# ("YYYYMMDD#n").  "round": F/D/L/W for a league's playoffs and
# the World Series (drawn as a bracket), X for a one-off series (drawn on its own).
# "official": False, with the reason in "status".  "game_notes": {"YYYYMMDD" or "YYYYMMDD#n": note};
# a note starting "not counted" leaves the game out of the series standings.
SERIES = [
    dict(season=1903, teams=("CUX", "PHG"), games=("19030912", "19030925"), round="X",
         title="Colored Championship", official=False, status=PRE_1920, winner="CUX", club_leagues={"CUX": "independent", "PHG": "independent"}),
    dict(season=1904, teams=("PHG", "CUX"), games=("19040901", "19040903"), round="X",
         title="Colored Championship", official=False, status=PRE_1920, winner="PHG", club_leagues={"CUX": "independent", "PHG": "independent"}),
    dict(season=1913, teams=("NYL", "CAG"), games=("19130718", "19130813"), round="X",
         title="Championship Series", official=False, winner="NYL",
         club_leagues={"NYL": "independent", "CAG": "independent"},
         status=PRE_1920 + " The best clubs of the East and the West met over three weeks.",
         game_notes={"19130719": "tie", "19130812#2": "tie"}),
    dict(season=1921, teams=("CAG", "HIL"), games=("19211004", "19211015"), round="X",
         title="Championship Series", official=False, winner="HIL",
         club_leagues={"CAG": "NNL", "HIL": "independent"},
         status="A challenge series: NNL champion Chicago against Hilldale, then an independent "
                "club (it joined the Eastern Colored League in 1923).",
         game_notes={"19211012": "tie"}),
    dict(season=1924, teams=("KCM", "HIL"), games=("19241003", "19241020"), round="W",
         title="Colored World Series", leagues=("NNL", "ECL"), format="best of 9", winner="KCM",
         game_notes={"19241005": "tie, 13 innings"}),
    dict(season=1925, teams=("KCM", "SSN"), games=("19250919", "19250928"), round="L", league="NNL",
         title="Championship Series", format="best of 7", winner="KCM",
         note="First-half champion Kansas City against second-half champion St. Louis; agreed as "
              "best of 9, cut to 7 for scheduling. The Monarchs played their home games in Chicago."),
    dict(season=1925, teams=("HIL", "KCM"), games=("19251001", "19251010"), round="W",
         title="Colored World Series", leagues=("ECL", "NNL"), format="best of 9", winner="HIL"),
    dict(season=1926, teams=("CAG", "KCM"), games=("19260918", "19260929"), round="L", league="NNL",
         title="Championship Series", format="best of 9", winner="CAG",
         note="First-half champion Kansas City against second-half champion Chicago.",
         game_notes={"19260929#2": "5 innings, the second game of the day"}),
    dict(season=1926, teams=("CAG", "ACY"), games=("19261001", "19261014"), round="W",
         title="Colored World Series", leagues=("NNL", "ECL"), format="best of 9", winner="CAG",
         game_notes={"19261001": "tie", "19261004": "tie"}),
    dict(season=1927, teams=("CAG", "BIR"), games=("19270919", "19270925"), round="L", league="NNL",
         title="Championship Series", winner="CAG",
         note="First-half champion Chicago against second-half champion Birmingham."),
    dict(season=1927, teams=("CAG", "ACY"), games=("19271001", "19271013"), round="W",
         title="Colored World Series", leagues=("NNL", "ECL"), format="best of 9", winner="CAG",
         game_notes={"19271010": "tie, 10 innings"}),
    dict(season=1928, teams=("SSN", "CAG"), games=("19280922", "19281005"), round="L", league="NNL",
         title="Championship Series", format="best of 9", winner="SSN",
         note="First-half champion St. Louis against second-half champion Chicago. The Eastern "
              "Colored League folded before its season ended, so there was no World Series."),
    dict(season=1930, teams=("SSN", "DT1"), games=("19300913", "19300922"), round="L", league="NNL",
         title="Championship Series", winner="SSN",
         note="First-half champion St. Louis against second-half champion Detroit."),
    dict(season=1930, teams=("HOM", "NYL"), games=("19300920", "19300928"), round="X",
         title="East Coast Championship Series", official=False, format="best of 10", winner="HOM",
         club_leagues={"HOM": "independent", "NYL": "independent"},
         status="Between two independent clubs: the East had no league in 1930."),
    dict(season=1930, teams=("HOM", "SSN"), games=("19300419", "19300903"), round="X", any_type=True,
         club_leagues={"HOM": "independent", "SSN": "NNL"},
         title="East–West Challenge Series", official=False, winner=None,
         status="Challenge games between the independent Homestead Grays and the NNL's St. Louis "
                "Stars, not a league series. St. Louis won four of the seven.",
         game_notes={"19300903": "11 innings"}),
    dict(season=1930, teams=("HOM", "DT1"), games=("19300821", "19300828"), round="X",
         club_leagues={"HOM": "independent", "DT1": "NNL"},
         title="East–West Challenge Series", official=False, winner=None,
         status="Challenge games between the independent Homestead Grays and the NNL's Detroit "
                "Stars, not a league series. Homestead won three of the four."),
    dict(season=1932, teams=("CAG", "NSH"), games=("19320903", "19321006"), round="L", league="NSL",
         title="Championship Series", winner="CAG",
         note="The Negro Southern League was a major league for 1932 only. First-half champion "
              "Chicago against second-half champion Nashville."),
    dict(season=1933, teams=("PTC", "CAG"), games=("19331006", "19331008"), round="X",
         title="Games between the pennant claimants", official=False, winner=None, league="NNL",
         status="Historians dispute whether 1933 had any postseason. Chicago claimed the first "
                "half, but the NNL awarded the pennant to Pittsburgh, so these games decided nothing.",
         game_notes={"19331008": "tie"}),
    dict(season=1934, teams=("PH5", "CAG"), games=("19340911", "19341002"), round="L", league="NNL",
         title="Championship Series", winner="PH5",
         note="Second-half champion Philadelphia against first-half champion Chicago.",
         game_notes={"19341001": "tie"}),
    dict(season=1935, teams=("PTC", "NY6"), games=("19350913", "19350921"), round="L", league="NNL",
         title="Championship Series", winner="PTC",
         note="First-half champion Pittsburgh came back from 3–1 down against second-half "
              "champion New York."),
    dict(season=1936, teams=("WEG", "PH5"), games=("19360917", "19360917"), round="D", league="NNL",
         title="Half-season playoffs", official=False, format="one game", winner="WEG",
         status="Disputed: Washington won this one-game playoff for the first-half title."),
    dict(season=1936, teams=("PTC", "NW2"), games=("19360912", "19360915"), round="D", league="NNL",
         any_type=True,
         title="Half-season playoffs", official=False, winner="PTC",
         status="Disputed: either a second-half playoff or a regular-season series that became one.",
         game_notes={"19360912#2": "tie"}),
    dict(season=1936, teams=("WEG", "PTC"), games=("19360921", "19360927"), round="L", league="NNL",
         title="Championship Series", official=False, winner=None,
         status="Abandoned with Washington ahead 2–1, the only year with no clear winner. "
                "Pittsburgh, with the league's best record, is generally considered the champion."),
    dict(season=1937, teams=("KCM", "CAG"), games=("19370908", "19370917"), round="L", league="NAL",
         title="Championship Series", winner="KCM",
         note="The first Negro American League season: first-place Kansas City against Chicago.",
         game_notes={"19370912": "tie, 17 innings"}),
    dict(season=1938, teams=("MEM", "ATN"), games=("19380918", "19380919"), round="L", league="NAL",
         title="Championship Series", format="2 games played", winner="MEM",
         note="First-half champion Memphis against second-half champion Atlanta. Stopped after two "
              "games when Atlanta couldn't find a ballpark for its home games; Memphis was named "
              "champion two months later."),
    dict(season=1938, teams=("PTC", "PH5"), games=("19380908", "19380913"), round="X",
         title="NNL playoff games", official=False, winner=None, league="NNL",
         status="Retrosheet lists these as playoff games, but the NNL awarded its 1938 pennant on "
                "the standings (to the Homestead Grays). No league playoff is recorded."),
    dict(season=1938, teams=("BLG", "PTC"), games=("19380911", "19380911"), round="X",
         title="NNL playoff game", official=False, winner=None, format="one game", league="NNL",
         status="Retrosheet lists this as a playoff game, but the NNL awarded its 1938 pennant on "
                "the standings (to the Homestead Grays). No league playoff is recorded."),
    dict(season=1939, teams=("BLG", "NW2"), games=("19390906", "19390910"), round="D", league="NNL",
         title="Semifinals", format="best of 5", winner="BLG"),
    dict(season=1939, teams=("HOM", "PH5"), games=("19390909", "19390914"), round="D", league="NNL",
         title="Semifinals", format="best of 5", winner="HOM"),
    dict(season=1939, teams=("BLG", "HOM"), games=("19390916", "19390924"), round="L", league="NNL",
         title="Championship Series", winner="BLG",
         note="The NNL's top four clubs played off for the pennant; third-place Baltimore won it.",
         game_notes={"19390917#2": "tie"}),
    dict(season=1939, teams=("KCM", "SSA"), games=("19390831", "19390904"), round="L", league="NAL",
         title="Championship Series", winner="KCM",
         game_notes={"19390903#2": "7 innings"}),
    dict(season=1941, teams=("HOM", "NY6"), games=("19410907", "19410921"), round="L", league="NNL",
         title="Championship Series", winner="HOM",
         note="First-half champion Homestead against second-half champion New York."),
    dict(season=1942, teams=("KCM", "HOM"), games=("19420908", "19420929"), round="W",
         title="Negro World Series", leagues=("NAL", "NNL"), format="best of 7", winner="KCM",
         extra_games=("19420913#2",),
         note="Kansas City swept the four games that counted; the two shown in brackets didn't.",
         game_notes={"19420913#2": "not counted, by prior agreement (the second game of the day, "
                                   "7 innings)",
                     "19420920": "not counted: thrown out on Kansas City's protest after Homestead "
                                 "used players borrowed from other NNL clubs"}),
    dict(season=1943, teams=("BIR", "CAG"), games=("19430913", "19430919"), round="L", league="NAL",
         title="Championship Series", winner="BIR",
         note="First-half champion Birmingham against second-half champion Chicago."),
    dict(season=1943, teams=("HOM", "BIR"), games=("19430921", "19431005"), round="W",
         title="Negro World Series", leagues=("NNL", "NAL"), format="best of 7", winner="HOM",
         game_notes={"19430923": "tie, 12 innings"}),
    dict(season=1944, teams=("HOM", "BIR"), games=("19440917", "19440924"), round="W",
         title="Negro World Series", leagues=("NNL", "NAL"), format="best of 7", winner="HOM"),
    dict(season=1945, teams=("CVB", "HOM"), games=("19450913", "19450920"), round="W",
         title="Negro World Series", leagues=("NAL", "NNL"), format="best of 7", winner="CVB"),
    dict(season=1946, teams=("NW2", "KCM"), games=("19460917", "19460929"), round="W",
         title="Negro World Series", leagues=("NNL", "NAL"), format="best of 7", winner="NW2"),
    dict(season=1947, teams=("NY6", "CVB"), games=("19470919", "19470927"), round="W",
         title="Negro World Series", leagues=("NNL", "NAL"), format="best of 7", winner="NY6",
         note="Second-half champion New York went to the World Series over first-half champion "
              "Newark without a playoff.",
         game_notes={"19470919": "tie, 6 innings"}),
    dict(season=1948, teams=("BIR", "KCM"), games=("19480911", "19480923"), round="L", league="NAL",
         title="Championship Series", winner="BIR",
         game_notes={"19480920": "tie, 5 innings"}),
    dict(season=1948, teams=("HOM", "BLG"), games=("19480914", "19480919"), round="L", league="NNL",
         title="Championship Series", winner="HOM",
         note="Game 3 was called 4–4 by an 11 p.m. curfew after Homestead scored four in the "
              "top of the 9th. On the Grays' protest the league ordered it resumed with them 8–4 "
              "up; Baltimore forfeited instead, and with it the series.",
         game_notes={"19480917": "tie by curfew; protested (see the note below the bracket)"}),
    dict(season=1948, teams=("HOM", "BIR"), games=("19480926", "19481005"), round="W",
         title="Negro World Series", leagues=("NNL", "NAL"), format="best of 7", winner="HOM",
         game_notes={"19481005": "10 innings"}),
    dict(season=1949, teams=("BLG", "CAG"), games=("19490916", "19490922"), round="L", league="NAL",
         title="Championship Series", official=False, winner="BLG",
         status="After 1948: MLB's recognition of the Negro leagues as major leagues ends with the "
                "1948 season, after which the NNL folded and its clubs joined the NAL."),
]


def fetch(name: str, given: str | None) -> bytes:
    if given:
        with open(given, "rb") as fh:
            return fh.read()
    path = os.path.join(CACHE, name)
    if not os.path.exists(path):
        os.makedirs(CACHE, exist_ok=True)
        req = urllib.request.Request(URLS[name], headers={"User-Agent": "BallSheet/1.0 (personal use)"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as fh:
            fh.write(resp.read())
    with open(path, "rb") as fh:
        return fh.read()


def read_csv(blob: bytes, member: str) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(member))
        return list(csv.DictReader(io.TextIOWrapper(zf.open(name), encoding="latin-1")))


def _num(value: str):
    return int(value) if value not in ("", None) else None


def _add(total: dict, key: str, value):
    """Sum into ``total[key]``; a blank (unknown) leaves an existing figure alone."""
    if value is None:
        total.setdefault(key, None)
    else:
        total[key] = (total.get(key) or 0) + value


# Retrosheet's per-game columns -> the MLB Stats API names the app aggregates with
BATTING = {"b_pa": "plateAppearances", "b_ab": "atBats", "b_r": "runs", "b_h": "hits",
           "b_d": "doubles", "b_t": "triples", "b_hr": "homeRuns", "b_rbi": "rbi",
           "b_sh": "sacBunts", "b_sf": "sacFlies", "b_hbp": "hitByPitch", "b_w": "baseOnBalls",
           "b_iw": "intentionalWalks", "b_k": "strikeOuts", "b_sb": "stolenBases",
           "b_cs": "caughtStealing", "b_gdp": "groundIntoDoublePlay"}
PITCHING = {"p_ipouts": "outs", "p_bfp": "battersFaced", "p_h": "hits", "p_hr": "homeRuns",
            "p_r": "runs", "p_er": "earnedRuns", "p_w": "baseOnBalls", "p_k": "strikeOuts",
            "p_hbp": "hitBatsmen", "p_wp": "wildPitches", "p_gs": "gamesStarted",
            "p_cg": "completeGames"}
FIELDING = {"d_ifouts": "outs", "d_po": "putOuts", "d_a": "assists", "d_e": "errors",
            "d_dp": "doublePlays", "d_tp": "triplePlays", "d_pb": "passedBall",
            "d_sb": "stolenBases", "d_cs": "caughtStealing", "d_gs": "gamesStarted"}
POSITIONS = {"1": "P", "2": "C", "3": "1B", "4": "2B", "5": "3B", "6": "SS", "7": "LF", "8": "CF",
             "9": "RF", "10": "DH"}


def player_lines(blob: bytes, series_gids: list[set], prefix: str = "") -> list[dict]:
    """For each set of games in ``series_gids``, ``{"batting": [...], "pitching": [...],
    "fielding": [...]}`` -- each player's totals over those games (one line per player, and
    per position for fielding), keyed with the MLB Stats API's stat names, plus "id", "name",
    "team" (Retrosheet's code) and for fielding "pos".  Only Retrosheet's firm figures
    ("value"), not its estimated ranges.  Reads each of Retrosheet's files once."""
    which = {gid: i for i, gids in enumerate(series_gids) for gid in gids}
    names = {}
    for r in read_csv(blob, prefix + "allplayers.csv"):
        names[r["id"]] = f"{r['first']} {r['last']}".strip()
    outs = [{"batting": {}, "pitching": {}, "fielding": {}} for _ in series_gids]
    for group, member, columns in (("batting", "batting.csv", BATTING), ("pitching", "pitching.csv", PITCHING),
                                   ("fielding", "fielding.csv", FIELDING)):
        for r in read_csv(blob, prefix + member):
            if r["gid"] not in which or r["stattype"] != "value":
                continue
            out = outs[which[r["gid"]]]
            pos = POSITIONS.get(r.get("d_pos", ""), r.get("d_pos", "")) if group == "fielding" else ""
            key = (r["id"], r["team"], pos)
            line = out[group].setdefault(key, {"id": r["id"], "name": names.get(r["id"], r["id"]),
                                               "team": r["team"], "gamesPlayed": 0, "_games": set()})
            if pos:
                line["pos"] = pos
            if r["gid"] not in line["_games"]:
                line["_games"].add(r["gid"])
                line["gamesPlayed"] += 1
            for col, name in columns.items():
                _add(line, name, _num(r.get(col, "")))
            if group == "pitching":
                for flag, name in (("wp", "wins"), ("lp", "losses"), ("save", "saves")):
                    _add(line, name, 1 if r.get(flag) == "1" else 0)
    results = []
    for out in outs:
        result = {}
        for group, lines in out.items():
            result[group] = []
            for line in lines.values():
                line.pop("_games")
                result[group].append(line)
        results.append(result)
    return results


def build(games_zip: bytes, parks_zip: bytes) -> dict:
    rows = read_csv(games_zip, "gameinfo.csv")
    parks = {p["PARKID"]: f"{p['NAME']}, {p['CITY']}" for p in read_csv(parks_zip, "ballparks.csv")}
    out = []
    for spec in SERIES:
        a, b = spec["teams"]
        first, last = spec["games"]
        extra = set(spec.get("extra_games", ()))
        picked = sorted((r for r in rows if {r["visteam"], r["hometeam"]} == {a, b}
                         and first <= r["date"] <= last
                         and (spec.get("any_type") or r["gametype"] in POSTSEASON_TYPES
                              or f"{r['date']}#{r['number']}" in extra)),
                        key=lambda r: (r["date"], r["number"]))
        if not picked:
            raise SystemExit(f"no games for {spec['season']} {a}-{b}")
        notes = spec.get("game_notes", {})
        games, wins = [], {a: 0, b: 0}
        for r in picked:
            if r["number"] in ("0", ""):  # a single game; "1"/"2" are a doubleheader's games
                note = notes.get(r["date"], "")
            else:
                note = notes.get(f"{r['date']}#{r['number']}", "")
            v, h = int(r["vruns"]), int(r["hruns"])
            counted = not note.startswith("not counted")
            if counted and v != h:
                wins[r["visteam"] if v > h else r["hometeam"]] += 1
            games.append({"date": f"{r['date'][:4]}-{r['date'][4:6]}-{r['date'][6:8]}",
                          "score": [r["visteam"], v, r["hometeam"], h],
                          "venue": parks.get(r["site"], r["site"]), "note": note, "counted": counted})
        winner = spec.get("winner")
        if winner:  # the file's winner must be the club with more counted wins
            assert wins[winner] > max(n for t, n in wins.items() if t != winner), (spec["season"], wins)
        title = spec["title"]
        official = spec.get("official", True)
        if "club_leagues" in spec:
            club_leagues = spec["club_leagues"]
        elif spec.get("leagues"):
            club_leagues = dict(zip(spec["teams"], spec["leagues"]))
        else:
            club_leagues = {t: spec.get("league") for t in spec["teams"]}
        counted_gids = {r["gid"] for r, g in zip(picked, games) if g["counted"]}
        out.append({
            "season": spec["season"], "round": spec["round"], "title": title,
            "league": spec.get("league"), "leagues": list(spec.get("leagues", ())),
            "official": official, "status": spec.get("status", ""), "note": spec.get("note", ""),
            "format": spec.get("format") or f"{len(games)} games",
            "teams": {t: TEAMS[t] for t in spec["teams"]}, "winner": winner,
            "winner_label": ("champions" if spec["round"] == "W" and official
                             else f"{title} winners"),
            "games": games, "source": "Retrosheet (games); Wikipedia (series, status, notes)",
            "club_leagues": club_leagues, "_gids": sorted(counted_gids),
        })
    # each series' player totals, over the games that counted
    for entry, players in zip(out, player_lines(games_zip, [set(e["_gids"]) for e in out])):
        entry.pop("_gids")
        entry["players"] = players
    return {"_about": __doc__.strip(), "series": out}


def main():
    games_zip = fetch("negroleagues.zip", sys.argv[1] if len(sys.argv) > 1 else None)
    parks_zip = fetch("ballparks.zip", sys.argv[2] if len(sys.argv) > 2 else None)
    data = build(games_zip, parks_zip)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    print(f"wrote {len(data['series'])} series to {OUT}")
    early = {}
    for spec in EARLY_RETRO:
        blob = fetch(spec["zip"], None)
        gids = {r["gid"] for r in read_csv(blob, spec["prefix"] + "gameinfo.csv") if r["gametype"] == "championship"}
        [players] = player_lines(blob, [gids], prefix=spec["prefix"])
        early[str(spec["season"])] = {"club_leagues": spec["club_leagues"], "teams": spec["teams"],
                                      "games": len(gids), "players": players}
    with open(EARLY_OUT, "w", encoding="utf-8") as fh:
        json.dump({"_about": "Player totals for the pre-1903 series Retrosheet has box scores for; "
                             "built by make_negro_postseason.py.", "seasons": early}, fh, indent=1)
        fh.write("\n")
    print(f"wrote {len(early)} early series to {EARLY_OUT}")


if __name__ == "__main__":
    main()
