#!/usr/bin/env python3
"""Uptodate tab: this year's play-by-play, built from the MLB Stats API in the shape of Retrosheet's
per-season CSV bundle (``YYYYcsvs.zip``: allplayers, batting, fielding, gameinfo, pitching, plays and
teamstats), so the tabs that read Retrosheet can use the current season before Retrosheet publishes it
(it does so after the season ends).  No Qt here.

The MLB feed of each finished game (``/api/v1.1/game/{pk}/feed/live``) is turned into Retrosheet's rows:

* **plays** -- one row per event as Retrosheet splits them: every plate appearance, every runner event
  between pitches (steals, caught stealing, pickoffs, wild pitches, passed balls, balks, defensive
  indifference, other advances) and an ``NP`` row for each pinch runner.  The counting columns (hits,
  outs, runners before and after, who scored and who drove them in, earned runs, putouts and assists by
  position, the batting team's lineup and the fielders) come straight from MLB's structured data; the
  ``event`` text is written in Retrosheet's notation from the same data, and ``loc`` (where the ball
  went) from MLB's hit coordinates on Retrosheet's hit-location diagram (:mod:`naturalzone`);
* **batting / pitching / fielding / teamstats** -- added up from those rows (fielding per position);
* **gameinfo** -- from the feed's game data and box score;
* **allplayers** -- one row per player per team, games by position.

Ids are Retrosheet's: players' from MLB's own cross-reference (``/people?hydrate=xrefId``), teams',
parks' and umpires' from tables matched against Retrosheet's 2024-25 files (an unknown park is looked up
in Retrosheet's ballparks list).  A player MLB has no Retrosheet id for yet (a rookie) gets one made the
way Retrosheet makes them -- four letters of the last name, the first initial, a number -- from 9 down,
so it can't be confused with one of Retrosheet's (which count up from 001).

The file is a zip with Retrosheet's file names (``2026plays.csv`` ...) plus ``uptodate.json`` (which MLB
games it holds, the ids made up).  Updating it downloads only the finished games it doesn't have yet.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import sys
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import naturalzone

API = "https://statsapi.mlb.com/api/v1"
API_V11 = "https://statsapi.mlb.com/api/v1.1"
_UA = "BallSheet/1.0 (personal use)"
_TIMEOUT = 60
WORKERS = 6
FORMAT_VERSION = 1

HEADERS = {
    "allplayers": "id,last,first,bat,throw,team,g,g_p,g_sp,g_rp,g_c,g_1b,g_2b,g_3b,g_ss,g_lf,g_cf,g_rf,g_of,g_dh,"
                  "g_ph,g_pr,first_g,last_g",
    "batting": "gid,id,team,b_lp,b_seq,stattype,b_pa,b_ab,b_r,b_h,b_d,b_t,b_hr,b_rbi,b_sh,b_sf,b_hbp,b_w,b_iw,b_k,"
               "b_sb,b_cs,b_gdp,b_xi,b_roe,dh,ph,pr,date,number,site,vishome,opp,win,loss,tie,gametype,box,pbp",
    "fielding": "gid,id,team,d_seq,d_pos,stattype,d_ifouts,d_po,d_a,d_e,d_dp,d_tp,d_pb,d_wp,d_sb,d_cs,d_gs,date,"
                "number,site,vishome,opp,win,loss,tie,gametype,box,pbp",
    "gameinfo": "gid,visteam,hometeam,site,date,number,starttime,daynight,innings,tiebreaker,usedh,htbf,timeofgame,"
                "attendance,fieldcond,precip,sky,temp,winddir,windspeed,oscorer,forfeit,suspend,umphome,ump1b,ump2b,"
                "ump3b,umplf,umprf,wp,lp,save,gametype,vruns,hruns,wteam,lteam,line,batteries,lineups,box,pbp,season",
    "pitching": "gid,id,team,p_seq,stattype,p_ipouts,p_noout,p_bfp,p_h,p_d,p_t,p_hr,p_r,p_er,p_w,p_iw,p_k,p_hbp,"
                "p_wp,p_bk,p_sh,p_sf,p_sb,p_cs,p_pb,wp,lp,save,p_gs,p_gf,p_cg,date,number,site,vishome,opp,win,loss,"
                "tie,gametype,box,pbp",
    "plays": "gid,event,inning,top_bot,vis_home,site,batteam,pitteam,score_v,score_h,batter,pitcher,lp,bat_f,"
             "bathand,pithand,balls,strikes,count,pitches,nump,pa,ab,single,double,triple,hr,sh,sf,hbp,walk,k,xi,"
             "roe,fc,othout,noout,oth,bip,bunt,ground,fly,line,iw,gdp,othdp,tp,fle,wp,pb,bk,oa,di,sb2,sb3,sbh,cs2,"
             "cs3,csh,pko1,pko2,pko3,k_safe,e1,e2,e3,e4,e5,e6,e7,e8,e9,outs_pre,outs_post,br1_pre,br2_pre,br3_pre,"
             "br1_post,br2_post,br3_post,lob_id1,lob_id2,lob_id3,pr1_pre,pr2_pre,pr3_pre,pr1_post,pr2_post,"
             "pr3_post,run_b,run1,run2,run3,prun_b,prun1,prun2,prun3,ur_b,ur1,ur2,ur3,rbi_b,rbi1,rbi2,rbi3,runs,"
             "rbi,er,tur,l1,l2,l3,l4,l5,l6,l7,l8,l9,lf1,lf2,lf3,lf4,lf5,lf6,lf7,lf8,lf9,f2,f3,f4,f5,f6,f7,f8,f9,"
             "po0,po1,po2,po3,po4,po5,po6,po7,po8,po9,a1,a2,a3,a4,a5,a6,a7,a8,a9,fseq,batout1,batout2,batout3,"
             "brout_b,brout1,brout2,brout3,firstf,loc,hittype,dpopp,pivot,pn,umphome,ump1b,ump2b,ump3b,umplf,"
             "umprf,date,gametype,pbp",
    "teamstats": "gid,team," + ",".join(f"inn{i}" for i in range(1, 29)) + ",lob,mgr,stattype,b_pa,b_ab,b_r,b_h,"
                 "b_d,b_t,b_hr,b_rbi,b_sh,b_sf,b_hbp,b_w,b_iw,b_k,b_sb,b_cs,b_gdp,b_xi,b_roe,p_ipouts,p_noout,p_bfp,"
                 "p_h,p_d,p_t,p_hr,p_r,p_er,p_w,p_iw,p_k,p_hbp,p_wp,p_bk,p_sh,p_sf,p_sb,p_cs,p_pb,d_po,d_a,d_e,d_dp,"
                 "d_tp,d_pb,d_wp,d_sb,d_cs," + ",".join(f"start_l{i}" for i in range(1, 10)) + ","
                 + ",".join(f"start_f{i}" for i in range(1, 11)) + ",date,number,site,vishome,opp,win,loss,tie,"
                 "gametype,box,pbp",
}
HEADERS = {name: line.split(",") for name, line in HEADERS.items()}
FILES = tuple(HEADERS)
META = "uptodate.json"

# MLB game types kept, as Retrosheet names them (spring training and exhibitions aren't in its files)
GAME_TYPES = {"R": "regular", "F": "wildcard", "D": "divisionseries", "L": "lcs", "W": "worldseries",
              "A": "allstar"}

# MLB team id -> Retrosheet team code (matched against Retrosheet's 2025 files)
TEAM_CODES = {108: "ANA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHN", 113: "CIN", 114: "CLE", 115: "COL",
              116: "DET", 117: "HOU", 118: "KCA", 119: "LAN", 120: "WAS", 121: "NYN", 133: "ATH", 134: "PIT",
              135: "SDN", 136: "SEA", 137: "SFN", 138: "SLN", 139: "TBA", 140: "TEX", 141: "TOR", 142: "MIN",
              143: "PHI", 144: "ATL", 145: "CHA", 146: "MIA", 147: "NYA", 158: "MIL", 159: "ALS", 160: "NLS"}

# MLB venue id -> Retrosheet park code (matched against Retrosheet's 2024-25 files); others are looked up
# by name in Retrosheet's ballparks list
SITES = {1: "ANA01", 2: "BAL12", 3: "BOS07", 4: "CHI12", 5: "CLE08", 7: "KAN06", 10: "OAK01", 12: "STP01",
         14: "TOR02", 15: "PHO01", 17: "CHI11", 19: "DEN02", 22: "LOS03", 31: "PIT08", 32: "MIL06", 680: "SEA03",
         2392: "HOU03", 2394: "DET05", 2395: "SFO03", 2397: "TOK01", 2523: "TAM02", 2529: "SAC01", 2602: "CIN09",
         2680: "SAN02", 2681: "PHI13", 2735: "WIL02", 2889: "STL10", 3289: "NYC20", 3309: "WAS11",
         3312: "MIN04", 3313: "NYC21", 3949: "BIR01", 4169: "MIA02", 4705: "ATL03", 5150: "SEO01",
         5325: "ARL03", 5340: "MEX02", 5381: "LON01", 6130: "BST01"}
PARKS_URL = "https://www.retrosheet.org/ballparks.zip"

# MLB umpire id -> Retrosheet umpire id (matched against Retrosheet's 2024-25 files)
UMPIRES = {427013: "barkl901", 427044: "buckc901", 427053: "carlm901", 427093: "cuzzp901", 427113: "diazl901",
           427128: "drakr901", 427129: "drecb901", 427139: "eddid901", 427164: "fleta901", 427197: "guccc901",
           427220: "herna901", 427243: "hudsm901", 427248: "iassd901", 427286: "knigb901", 427292: "kulpr901",
           427315: "marqa901", 427344: "millb901", 427366: "onorb901", 427413: "randt901", 427520: "vanol901",
           427533: "wegnm901", 427537: "wendh902", 427554: "wolfj901", 428442: "hoyej901", 429805: "ticht901",
           431232: "fairc901", 482608: "barrs901", 482620: "conrc901", 482631: "estam901", 482641: "johna901",
           482663: "rackd901", 482666: "reybd901", 483561: "barrl901", 483564: "belld901", 483569: "carav901",
           483630: "tumpj901", 483912: "muchm901", 483919: "rippm901", 484183: "blasc901", 484198: "porta901",
           484499: "gonzm901", 485856: "parrj901", 486200: "arrid901", 489985: "whitc901", 490319: "bakej902",
           503077: "hamaa901", 503490: "segac901", 503493: "barbs901", 503502: "blakr901", 503586: "gibsh902",
           504381: "littw901", 511890: "wolcq901", 521051: "mahrn901", 521251: "may-b901", 521889: "sches901",
           544728: "lentn901", 545402: "bacoj901", 547380: "torrc901", 554242: "mosce901", 573597: "morag901",
           573769: "riggj901", 575629: "lives901", 577468: "ortir901", 594151: "dejer901", 596740: "viscj901",
           596805: "libkj901", 596809: "addir901", 598411: "cejan901", 605670: "merzd901", 605671: "ramoc901",
           605672: "rehaj901", 605673: "tosia901", 605674: "willr901", 607884: "clemp901", 608093: "valej901",
           608158: "tomln901", 621660: "jimee901", 623938: "bacce901", 623945: "macka901", 623946: "moorm901",
           623949: "vondc901", 639723: "millb903", 644760: "becka901", 658082: "metzj901", 658292: "thomd901",
           658325: "jonet901", 658536: "jonea901", 664263: "jimee902", 665281: "walsb901", 665297: "hanat901",
           665298: "kelld901", 665576: "ballb901", 668921: "pawoj901", 676580: "jascs901", 676581: "jeanj901",
           691042: "trayw901"}
UMPIRE_POSTS = {"Home Plate": "umphome", "First Base": "ump1b", "Second Base": "ump2b", "Third Base": "ump3b",
                "Left Field": "umplf", "Right Field": "umprf"}


class UptodateError(Exception):
    """The season's games couldn't be had, or the file couldn't be read or written."""


# --------------------------------------------------------------------------- #
#  Ids                                                                        #
# --------------------------------------------------------------------------- #
def _ascii(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if c.isascii())


def make_id(last: str, first: str, taken, umpire: bool = False) -> str:
    """A Retrosheet-style id for someone with none: the first four letters of the last name (padded
    with "-"), the first initial, and a number not in ``taken`` -- 999 down for players (Retrosheet's
    count up from 001), 999 down from the 900s for umpires (Retrosheet's are 901, 902 ...)."""
    stem = re.sub(r"[^a-z]", "", _ascii(last).lower())[:4].ljust(4, "-")
    stem += (re.sub(r"[^a-z]", "", _ascii(first).lower())[:1] or "x")
    for n in range(999, 900 if umpire else 1, -1):
        candidate = f"{stem}{n:03d}"
        if candidate not in taken:
            return candidate
    raise UptodateError(f"no id left for {first} {last}")


def _plain_name(name: str) -> str:
    """A name as Retrosheet writes it: no accents, no "Jr."."""
    name = re.sub(r",?\s+(Jr\.?|Sr\.?|II|III|IV)$", "", _ascii(name).strip())
    return name


def split_name(person: dict) -> tuple[str, str]:
    """(last, first) as Retrosheet writes them (``useName`` first: "Shohei", not "Shohei")."""
    last = person.get("useLastName") or person.get("lastName") or person.get("fullName", "").split(" ")[-1]
    first = person.get("useName") or person.get("firstName") or ""
    return last, first


# --------------------------------------------------------------------------- #
#  Hit locations                                                              #
# --------------------------------------------------------------------------- #
_ZONES = naturalzone.zone_list()
HOME_X, HOME_Y, FEET = 125.7, 201.5, 2.55  # MLB's hit chart: home plate and feet per unit (fitted to Retrosheet's 2025 locations)


INFIELD_DEPTH = 120.0  # feet: where a ground ball that got through the infield is placed (between the arcs)
OUTFIELD_GROUND = 180.0  # feet: a ground ball picked up farther out than this got through the infield


def hit_location(coordinates: dict | None, home_run: bool = False, ground: bool = False) -> str:
    """Retrosheet's hit-location code (``"56"``, ``"8XD"``, ``"7LSF"``) of MLB's hit coordinates, on the
    Project Scoresheet diagram (:mod:`naturalzone`'s tracing); a home run is placed at the deepest zone
    on its line.  ``""`` if MLB has no coordinates."""
    if not coordinates or coordinates.get("coordX") is None or coordinates.get("coordY") is None:
        return ""
    x = (coordinates["coordX"] - HOME_X) * FEET
    y = (HOME_Y - coordinates["coordY"]) * FEET
    a, r = math.degrees(math.atan2(x, y)), math.hypot(x, y)
    if ground:
        # a ground ball in play was fair; one that got through to the outfield is placed where it went
        # through the infield, not where it was picked up
        a = max(-44.5, min(44.5, a))
        if r > OUTFIELD_GROUND:
            r = INFIELD_DEPTH
        x, y = r * math.sin(math.radians(a)), r * math.cos(math.radians(a))
    if home_run or (abs(a) <= 45 and r >= naturalzone.fence(a)):
        a = max(-44.9, min(44.9, a))
        r = naturalzone.fence(a) - 1
        x, y = r * math.sin(math.radians(a)), r * math.cos(math.radians(a))
    found = naturalzone._locate(x, y, _ZONES)
    if found is None:  # beyond the foul strips: the nearest strip
        if abs(a) > 45:
            return ("7LDF" if a < 0 else "9LDF") if r > 200 else ("5DF" if a < 0 else "3DF")
        return ""
    return found[0]


HIT_TYPES = {"ground_ball": "G", "fly_ball": "F", "line_drive": "L", "popup": "P", "bunt_grounder": "BG",
             "bunt_popup": "BP", "bunt_line_drive": "BL"}
_LOC_IN_EVENT = re.compile(r"/(?:BG|BP|BL|G|F|L|P)(\d[0-9A-Z]*?)[+-]?(?=/|$)")
HARDNESS = {"hard": "+", "soft": "-"}  # MLB's hardness -> Retrosheet's mark after a location ("medium": none)
# actions Retrosheet marks with a "." in the pitch sequence ("play not involving the batter")
DOT_ACTIONS = {"batter_timeout", "pitching_substitution", "offensive_substitution", "defensive_substitution",
               "defensive_switch", "runner_placed"}


# --------------------------------------------------------------------------- #
#  One game                                                                   #
# --------------------------------------------------------------------------- #
BASE_NUM = {"1B": 1, "2B": 2, "3B": 3, "score": 4, "4B": 4}
BASE_CH = {0: "B", 1: "1", 2: "2", 3: "3", 4: "H"}

# runner events that make their own row (Retrosheet's "SB2", "WP" ...): MLB event type -> notation
RUNNER_EVENTS = {
    "stolen_base_2b": "SB2", "stolen_base_3b": "SB3", "stolen_base_home": "SBH",
    "caught_stealing_2b": "CS2", "caught_stealing_3b": "CS3", "caught_stealing_home": "CSH",
    "pickoff_1b": "PO1", "pickoff_2b": "PO2", "pickoff_3b": "PO3",
    "pickoff_error_1b": "PO1", "pickoff_error_2b": "PO2", "pickoff_error_3b": "PO3",
    "pickoff_caught_stealing_2b": "POCS2", "pickoff_caught_stealing_3b": "POCS3",
    "pickoff_caught_stealing_home": "POCSH",
    "wild_pitch": "WP", "passed_ball": "PB", "balk": "BK", "forced_balk": "BK", "defensive_indiff": "DI",
    "other_advance": "OA", "error": "OA", "runner_double_play": "OA", "other_out": "OA",
    "caught_stealing": "CS2", "stolen_base": "SB2",
}
# the base a steal / caught stealing / pickoff notation is about
_EVENT_BASE = {"SB2": 2, "SB3": 3, "SBH": 4, "CS2": 2, "CS3": 3, "CSH": 4, "POCS2": 2, "POCS3": 3, "POCSH": 4,
               "PO1": 1, "PO2": 2, "PO3": 3}
# MLB pitch call codes -> Retrosheet's pitch codes
PITCH_CODES = {"B": "B", "*B": "*B", "C": "C", "S": "S", "W": "*S", "F": "F", "T": "T", "L": "L", "M": "M",
               "O": "T", "X": "X", "D": "X", "E": "X", "H": "H", "P": "P", "Q": "Q", "R": "R", "I": "I",
               "V": "V", "VP": "V", "VC": "V", "VB": "V", "AB": "A", "AC": "A", "A": "A", "VS": "A", "N": "N"}
_PITCHES = set("ABCFHIKLMOPQRSTUXY")  # what Retrosheet's nump counts (not automatic balls)
_BATTER_SAFE_DEFAULT = {"single": 1, "double": 2, "triple": 3, "home_run": 4, "walk": 1, "intent_walk": 1,
                        "hit_by_pitch": 1, "catcher_interf": 1, "field_error": 1, "fielders_choice": 1,
                        "fielders_choice_out": 1, "force_out": 1, "sac_bunt": 1, "sac_fly": 1, "strikeout": 1}


def _blank_row() -> dict:
    return {}


class _Runner:
    """One player's movement on one play, MLB's segments joined."""
    __slots__ = ("pid", "start", "end", "out", "out_base", "reasons", "credits", "scored", "rbi", "earned",
                 "team_unearned", "responsible", "events", "segments")

    def __init__(self, pid):
        self.pid = pid
        self.start = None   # 0 batter, 1-3
        self.end = None     # 1-3, 4 scored, None out
        self.out = False
        self.out_base = None
        self.reasons, self.credits, self.events, self.segments = [], [], [], []
        self.scored = self.rbi = self.earned = self.team_unearned = False
        self.responsible = None


def _join_runners(movements) -> list[_Runner]:
    """MLB's runner segments of one play event -> one :class:`_Runner` each, in order of base (third
    first, the batter last)."""
    out: dict[int, _Runner] = {}
    for m in movements:
        mv, dt = m["movement"], m["details"]
        pid = dt["runner"]["id"]
        r = out.get(pid)
        if r is None:
            r = out[pid] = _Runner(pid)
            r.start = BASE_NUM.get(mv.get("start") or mv.get("originBase"), 0)
        r.segments.append(m)
        r.events.append(dt.get("eventType"))
        if dt.get("movementReason"):
            r.reasons.append(dt["movementReason"])
        r.credits += m.get("credits", [])
        if mv.get("isOut"):
            r.out, r.end = True, None
            r.out_base = BASE_NUM.get(mv.get("outBase"))
        elif mv.get("end"):
            r.end = BASE_NUM.get(mv["end"])
        if mv.get("end") == "score" and not mv.get("isOut"):
            r.scored = True
            r.rbi = bool(dt.get("rbi"))
            r.earned = bool(dt.get("earned"))
            r.team_unearned = bool(dt.get("teamUnearned"))
            if dt.get("responsiblePitcher"):
                r.responsible = dt["responsiblePitcher"]["id"]
    return sorted(out.values(), key=lambda r: -r.start if r.start else 1)


def _squeeze(seq: str) -> str:
    """A fielding sequence with repeats in a row taken out ("9942" -> "942")."""
    return "".join(ch for i, ch in enumerate(seq) if i == 0 or ch != seq[i - 1])


def _digits(credits, kinds=("f_assist", "f_assist_of", "f_putout")) -> str:
    return _squeeze("".join(c["position"]["code"] for c in credits if c.get("credit") in kinds
                            and c["position"]["code"].isdigit() and len(c["position"]["code"]) == 1))


def _errors(credits) -> list[tuple[str, bool]]:
    """[(position, throwing?)] of the errors in ``credits``."""
    return [(c["position"]["code"], c["credit"] == "f_throwing_error") for c in credits
            if c.get("credit") in ("f_fielding_error", "f_throwing_error", "f_error_dropped_ball")]


class GameConverter:
    """One MLB game feed -> Retrosheet rows (dicts by column) for every file but allplayers."""

    def __init__(self, feed: dict, gid: str, rid, site: str, umpires: dict, gametype: str):
        self.feed = feed
        self.gid = gid
        self.rid = rid            # MLB person id -> Retrosheet id
        self.site = site
        self.umpires = umpires    # column -> Retrosheet id
        self.gametype = gametype
        gd = feed["gameData"]
        dt = gd["datetime"]
        # a suspended game keeps the date it began; its rows after the resumption carry the later date
        self.date = (dt.get("originalDate") or dt.get("officialDate", "")).replace("-", "")
        official = dt.get("officialDate", "").replace("-", "")
        self.resumed = official if official and official != self.date else ""
        self.offset = (gd.get("venue", {}).get("timeZone") or {}).get("offsetAtGameTime") or 0
        self.managers = {}
        self.team = {side: TEAM_CODES.get(gd["teams"][side]["id"], gd["teams"][side].get("abbreviation", "???"))
                     for side in ("away", "home")}
        self.box = feed["liveData"]["boxscore"]["teams"]
        self.players = gd.get("players", {})
        self.plays: list[dict] = []
        self._play_date = self.date
        self._seg = 0
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ helpers
    def hand(self, pid, kind) -> str:
        p = self.players.get(f"ID{pid}", {})
        return (p.get(kind) or {}).get("code", "")

    def _start_side(self, side: str):
        """Starting lineup and fielders of one side from the box score."""
        lineup, pos, fielders = {}, {}, {}
        players = self.box[side]["players"]
        for key, p in players.items():
            order = p.get("battingOrder")
            if order and int(order) % 100 == 0:
                spot = int(order) // 100
                codes = [a.get("code", "") for a in (p.get("allPositions") or [p.get("position", {})])]
                lineup[spot] = p["person"]["id"]
                # a two-way player (pitcher and DH) is in the lineup as the DH
                pos[p["person"]["id"]] = 10 if "10" in codes else _pos_num(codes[0] if codes else "")
        for pid, f in pos.items():
            if 1 <= f <= 9:
                fielders[f] = pid
        pitchers = self.box[side].get("pitchers") or []
        if pitchers:
            fielders[1] = pitchers[0]
            pos.setdefault(pitchers[0], 1)
        return {"lineup": lineup, "pos": pos, "fielders": fielders, "starters": dict(lineup),
                "start_fielders": dict(fielders), "dh": any(v == 10 for v in pos.values())}

    # ------------------------------------------------------------------ the play-by-play
    def convert_plays(self) -> list[dict]:
        sides = {"away": self._start_side("away"), "home": self._start_side("home")}
        self.sides = sides
        self.appear = {"away": [], "home": []}   # (pid, slot, kind) in order of appearance
        for side in ("away", "home"):
            for slot, pid in sorted(sides[side]["lineup"].items()):
                self.appear[side].append((pid, slot, "start"))
        self.pitch_order = {"away": list(self.box["away"].get("pitchers") or []),
                            "home": list(self.box["home"].get("pitchers") or [])}
        self.fielding_spells = {"away": [], "home": []}  # [pid, pos, outs, started] as they happen
        self.dh_seen = {"away": set(), "home": set()}
        for side in ("away", "home"):
            for f, pid in sorted(sides[side]["fielders"].items()):
                self.fielding_spells[side].append([pid, f, 0, True])
        score = {"away": 0, "home": 0}
        self.pn = 0
        half_key = None
        bases = {1: None, 2: None, 3: None}
        resp = {1: None, 2: None, 3: None}
        outs = 0
        all_plays = self.feed["liveData"]["plays"]["allPlays"]
        for play in all_plays:
            about = play["about"]
            key = (about["inning"], about["isTopInning"])
            if key != half_key:
                half_key = key
                bases, resp, outs = {1: None, 2: None, 3: None}, {1: None, 2: None, 3: None}, 0
                self._settle_dh("home" if about["isTopInning"] else "away")
            bat_side = "away" if about["isTopInning"] else "home"
            fld_side = "home" if bat_side == "away" else "away"
            self._play_date = self.date
            if self.resumed and about.get("startTime"):
                try:
                    when = datetime.strptime(about["startTime"][:19], "%Y-%m-%dT%H:%M:%S") + timedelta(hours=self.offset)
                    if when.strftime("%Y%m%d") >= self.resumed:
                        self._play_date = self.resumed
                except ValueError:
                    pass
            st = {"bases": bases, "resp": resp, "outs": outs, "score": score, "inning": about["inning"],
                  "top": about["isTopInning"], "bat": bat_side, "fld": fld_side}
            self._play(play, st)
            outs = st["outs"]
            if outs >= 3:
                bases, resp = {1: None, 2: None, 3: None}, {1: None, 2: None, 3: None}
                st["bases"], st["resp"] = bases, resp
        if self.plays and self.plays[-1]["outs_post"] != "3":  # a walk-off: whoever is still on base is left
            last = self.plays[-1]
            left = [last[f"br{b}_post"] for b in (3, 2, 1) if last[f"br{b}_post"]]
            for b in (1, 2, 3):
                last[f"lob_id{b}"] = left[b - 1] if b <= len(left) else ""
                last[f"br{b}_post"] = last[f"pr{b}_post"] = ""
        return self.plays

    def _settle_dh(self, side: str):
        """When a side takes the field, a pinch hitter or runner who batted for the DH and has no position
        is the DH now."""
        s = self.sides[side]
        if not s["dh"] or any(s["pos"].get(pid) == 10 for pid in s["lineup"].values()):
            return
        for pid in s["lineup"].values():
            if s["pos"].get(pid) in (11, 12) and pid not in s["fielders"].values():
                s["pos"][pid] = 10
                self.dh_seen[side].add(pid)
                return

    def _play(self, play: dict, st: dict):
        events = play.get("playEvents", [])
        result = play.get("result", {})
        matchup = play.get("matchup", {})
        batter = matchup.get("batter", {}).get("id")
        pitcher = matchup.get("pitcher", {}).get("id")
        groups: dict[int, list] = {}
        for m in play.get("runners", []):
            groups.setdefault(m["details"].get("playIndex", len(events) - 1), []).append(m)
        batter_index = None
        for idx, movements in groups.items():
            if any(m["details"]["runner"]["id"] == batter and not m["movement"].get("originBase")
                   and not m["movement"].get("start") and m["details"].get("eventType") not in RUNNER_EVENTS
                   for m in movements):
                batter_index = idx
        if result.get("type") != "atBat" or not play.get("about", {}).get("isComplete", True):
            pass
        self._seg = 0          # where the current play's pitches start (after a runner play's ".")
        self._mid_ph = None    # (batter replaced, balls, strikes) when a pinch hitter came in mid-count
        pitches = []           # Retrosheet pitch tokens so far
        count = [0, 0]         # balls, strikes before the latest pitch
        before_last = (0, 0)
        last_was_pitch = False
        for i, ev in enumerate(events):
            det = ev.get("details", {})
            kind = ev.get("type")
            etype = det.get("eventType")
            if ev.get("isPitch"):
                code = (det.get("call") or {}).get("code") or det.get("code", "")
                token = PITCH_CODES.get(code, code[:1] or "U")
                if det.get("runnerGoing"):
                    token = ">" + token
                pitches.append(token)
                before_last = tuple(count)
                c = ev.get("count", {})
                count = [c.get("balls", count[0]), c.get("strikes", count[1])]
                last_was_pitch = True
            elif kind == "no_pitch":
                code = det.get("code", "")
                token = PITCH_CODES.get(code, "")
                if token:
                    pitches.append(token)
                    before_last = tuple(count)
                    c = ev.get("count", {})
                    count = [c.get("balls", count[0]), c.get("strikes", count[1])]
                    last_was_pitch = True
            elif kind == "pickoff":
                code = det.get("code", "")
                if code in ("1", "2", "3"):
                    pitches.append(("+" if det.get("fromCatcher") else "") + code)
                    last_was_pitch = False
            elif kind == "action":
                if ev.get("isSubstitution") or etype in ("pitching_substitution", "offensive_substitution",
                                                         "defensive_substitution", "defensive_switch"):
                    self._substitute(ev, st, pitches, count, batter, pitcher)
                elif etype == "runner_placed":
                    base = ev.get("base") or 2
                    pid = ev.get("player", {}).get("id")
                    st["bases"][base] = pid
                    st["resp"][base] = self.sides[st["fld"]]["fielders"].get(1)
                elif etype == "error" and i not in groups:  # a dropped foul: the batter stays up
                    who = ev.get("player", {}).get("id")
                    fpos = next((f for f, x in self.sides[st["fld"]]["fielders"].items() if x == who), 0)
                    cnt = before_last if last_was_pitch else tuple(count)
                    row = self._base_row(st, batter, self.sides[st["fld"]]["fielders"].get(1) or pitcher,
                                         "".join(pitches), cnt)
                    row["event"], row["fle"], row["outs_post"] = f"FLE{fpos}", "1", row["outs_pre"]
                    if fpos:
                        row[f"e{fpos}"] = "1"
                    for b in (1, 2, 3):
                        row[f"br{b}_post"], row[f"pr{b}_post"] = row[f"br{b}_pre"], row[f"pr{b}_pre"]
                    self.plays.append(row)
                    pitches.append(".")
                    self._seg = len("".join(pitches))
                    last_was_pitch = False
                if etype in DOT_ACTIONS and i not in groups:
                    pitches.append(".")
                    last_was_pitch = False
            if i in groups and i != batter_index:
                cnt = before_last if last_was_pitch else tuple(count)
                self._emit(play, groups[i], st, "".join(pitches), cnt, batter_pa=False, batter=batter,
                           pitcher=self.sides[st["fld"]]["fielders"].get(1) or pitcher, event_index=i)
                pitches.append(".")
                self._seg = len("".join(pitches))
                last_was_pitch = False
        if batter_index is not None:
            cnt = before_last if last_was_pitch else tuple(count)
            if self._mid_ph:
                first, balls, strikes = self._mid_ph
                btype = result.get("eventType", "")
                if (btype.startswith("strikeout") and strikes == 2) or (
                        btype == "walk" and balls >= 2 and balls > strikes):
                    batter = first
            self._emit(play, groups[batter_index], st, "".join(pitches), cnt, batter_pa=True, batter=batter,
                       pitcher=pitcher, event_index=batter_index)

    # ------------------------------------------------------------------ substitutions
    def _substitute(self, ev, st, pitches, count, batter, pitcher):
        det = ev.get("details", {})
        etype = det.get("eventType")
        pid = ev.get("player", {}).get("id")
        if pid is None:
            return
        pos_code = (ev.get("position") or {}).get("code", "")
        newpos = _pos_num(pos_code) if pos_code else None
        order = ev.get("battingOrder")
        replaced = (ev.get("replacedPlayer") or {}).get("id")
        side = "away" if f"ID{pid}" in self.box["away"]["players"] else "home"
        s = self.sides[side]
        if etype == "pitching_substitution":
            newpos = 1
            if pid not in self.pitch_order[side]:
                self.pitch_order[side].append(pid)
        if order:
            slot = int(order) // 100
            old = s["lineup"].get(slot)
            if old != pid:
                s["lineup"][slot] = pid
                kind = "ph" if newpos == 11 else "pr" if newpos == 12 else "sub"
                self.appear[side].append((pid, slot, kind))
                if old is not None and old in s["pos"] and old != pid:
                    oldpos = s["pos"].pop(old)
                    if s["fielders"].get(oldpos) == old and newpos not in (11, 12):
                        pass
        if newpos == 11 and replaced and any(ch in _PITCHES for ch in "".join(pitches)):
            # a pinch hitter in the middle of a count: a strikeout with two strikes already on the batter
            # he replaced is charged to that batter, and so is a walk with the count in the batter's
            # favour (rule 9.15(b)); otherwise an NP row closes the first batter's part
            self._mid_ph = (replaced, count[0], count[1])
            if count[1] < 2:
                self._emit_np(st, "".join(pitches), tuple(count), replaced, pitcher)
        if newpos == 12 and replaced:  # a pinch runner: an NP row, then he's on base
            for b in (1, 2, 3):
                if st["bases"][b] == replaced:
                    self._emit_np(st, "".join(pitches), tuple(count), batter, pitcher)
                    st["bases"][b] = pid
        if newpos is not None:
            prev = s["pos"].get(pid)
            s["pos"][pid] = newpos
            if 1 <= newpos <= 9:
                holder = s["fielders"].get(newpos)
                for f, who in list(s["fielders"].items()):
                    if who == pid and f != newpos:
                        del s["fielders"][f]
                s["fielders"][newpos] = pid
                self.fielding_spells[side].append([pid, newpos, 0, False])
                if newpos == 1 and pid not in self.pitch_order[side]:
                    self.pitch_order[side].append(pid)
            elif newpos == 10:
                self.dh_seen[side].add(pid)
                for f, who in list(s["fielders"].items()):
                    if who == pid:
                        del s["fielders"][f]
            if replaced and replaced in s["pos"] and replaced != pid:
                if s["pos"].get(replaced) == newpos or newpos in (11, 12):
                    pass
            if prev == 10 and newpos != 10:
                pass
        # the DH is lost when the pitcher bats or a DH takes the field
        if newpos == 1 and order:
            s["dh_lost"] = True

    # ------------------------------------------------------------------ rows
    def _base_row(self, st, batter, pitcher, pitches, cnt):
        since = self._seg
        bat, fld = st["bat"], st["fld"]
        s_bat, s_fld = self.sides[bat], self.sides[fld]
        self.pn += 1
        lp = next((slot for slot, who in s_bat["lineup"].items() if who == batter), "")
        bat_f = 1 if batter == s_bat["fielders"].get(1) else s_bat["pos"].get(batter, "")
        row = {name: "0" for name in HEADERS["plays"]}
        nump = sum(1 for ch in pitches[since:] if ch in _PITCHES) if pitches else ""
        row.update({
            "gid": self.gid, "inning": str(st["inning"]), "top_bot": "0" if st["top"] else "1",
            "vis_home": "0" if st["top"] else "1", "site": self.site, "batteam": self.team[bat],
            "pitteam": self.team[fld], "score_v": str(st["score"]["away"]), "score_h": str(st["score"]["home"]),
            "batter": self.rid(batter), "pitcher": self.rid(pitcher), "lp": str(lp), "bat_f": str(bat_f),
            "bathand": self._bat_hand(batter, pitcher), "pithand": self.hand(pitcher, "pitchHand"),
            "balls": str(cnt[0]), "strikes": str(cnt[1]), "count": str(cnt[0] * 10 + cnt[1]),
            "pitches": pitches, "nump": str(nump), "outs_pre": str(st["outs"]),
            "pn": str(self.pn), "date": self._play_date, "gametype": self.gametype, "pbp": "full", "loc": "",
            "hittype": "", "fseq": "", "event": "",
        })
        for b in (1, 2, 3):
            row[f"br{b}_pre"] = self.rid(st["bases"][b]) if st["bases"][b] else ""
            row[f"pr{b}_pre"] = self.rid(st["resp"][b]) if st["bases"][b] and st["resp"][b] else ""
        for col in ("lob_id1", "lob_id2", "lob_id3", "run_b", "run1", "run2", "run3", "prun_b", "prun1",
                    "prun2", "prun3"):
            row[col] = ""
        for n in range(1, 10):
            who = s_bat["lineup"].get(n)
            row[f"l{n}"] = self.rid(who) if who else ""
            row[f"lf{n}"] = str(s_bat["pos"].get(who, "")) if who else ""
        for f in range(2, 10):
            who = s_fld["fielders"].get(f)
            row[f"f{f}"] = self.rid(who) if who else ""
        for col, ump in self.umpires.items():
            row[col] = "" if ump == "(none)" else ump
        return row

    def _bat_hand(self, batter, pitcher) -> str:
        side = self.hand(batter, "batSide")
        return "B" if side == "S" else side

    def _emit_np(self, st, pitches, cnt, batter, pitcher):
        row = self._base_row(st, batter, self.sides[st["fld"]]["fielders"].get(1) or pitcher, pitches, cnt)
        row["event"] = "NP"
        row["outs_post"] = row["outs_pre"]
        for b in (1, 2, 3):
            row[f"br{b}_post"] = row[f"br{b}_pre"]
            row[f"pr{b}_post"] = row[f"pr{b}_pre"]
        row["batout1"] = row["batout2"] = row["batout3"] = "0"
        self.plays.append(row)

    def _emit(self, play, movements, st, pitches, cnt, batter_pa, batter, pitcher, event_index):
        runners = _join_runners(movements)
        result = play.get("result", {})
        events = play.get("playEvents", [])
        ev = events[event_index] if 0 <= event_index < len(events) else {}
        hit = ev.get("hitData") or {}
        bat_side, fld_side = st["bat"], st["fld"]
        row = self._base_row(st, batter, pitcher, pitches, cnt)
        etype = result.get("eventType", "") if batter_pa else (
            (ev.get("details") or {}).get("eventType") or (runners[0].events[0] if runners else ""))
        # runner-only event types inside the batter's play (K+SB2, W+WP)
        extra_types = []
        if batter_pa:
            for r in runners:
                if (r.start and r.events and r.events[0] in RUNNER_EVENTS and r.events[0] != etype
                        and r.events[0] not in ("error", "other_out", "runner_double_play")):
                    extra_types.append(r.events[0])
        flags = {}

        def flag(name, value=1):
            flags[name] = str(int(flags.get(name, "0")) + value)

        # -------- the runners
        pre = dict(st["bases"])
        post = dict(st["bases"])
        resp_post = dict(st["resp"])
        current_pitcher = pitcher
        scored = {}
        outs_made = 0
        batter_r = None
        for r in runners:
            if r.start == 0:
                batter_r = r
        for r in runners:
            if r.start in (1, 2, 3):
                if pre.get(r.start) != r.pid:
                    # MLB has him somewhere we don't: trust MLB
                    for b in (1, 2, 3):
                        if pre.get(b) == r.pid:
                            pre[b] = None
                            post[b] = None
                    if st["bases"].get(r.start) not in (None, r.pid):
                        self.warnings.append(f"{self.gid} {play['about']['inning']}: base {r.start} mismatch")
                    pre[r.start] = r.pid
                    post[r.start] = r.pid
                post[r.start] = None if post.get(r.start) == r.pid else post.get(r.start)
        # resp for runners already on base
        start_resp = {b: st["resp"].get(b) for b in (1, 2, 3)}
        moved_resp = {}
        for r in runners:
            if r.start in (1, 2, 3):
                moved_resp[r.pid] = start_resp.get(r.start) if st["bases"].get(r.start) == r.pid else (
                    start_resp.get(r.start) or current_pitcher)
            else:
                moved_resp[r.pid] = current_pitcher
        # a batter who reaches on a force play takes the place of the runner put out (rule 9.16(g))
        forced_out = [r for r in runners if r.start in (1, 2, 3) and r.out]
        if batter_r and not batter_r.out and forced_out and etype in ("force_out", "fielders_choice_out",
                                                                       "grounded_into_double_play"):
            moved_resp[batter_r.pid] = moved_resp[forced_out[0].pid]
        for b in (1, 2, 3):
            if post[b] is None:
                resp_post[b] = None
        for r in sorted(runners, key=lambda r: -(r.end or 0)):
            if r.out:
                outs_made += 1
            elif r.end in (1, 2, 3):
                post[r.end] = r.pid
                resp_post[r.end] = moved_resp.get(r.pid)
            elif r.end == 4 or r.scored:
                scored[r.start] = r
        for b in (1, 2, 3):
            row[f"br{b}_pre"] = self.rid(pre[b]) if pre[b] else ""
        outs_post = st["outs"] + outs_made
        row["outs_post"] = str(outs_post)
        if outs_post >= 3:
            left = [self.rid(post[b]) for b in (3, 2, 1) if post[b]]
            for b in (1, 2, 3):
                row[f"lob_id{b}"] = left[b - 1] if b <= len(left) else ""
                row[f"br{b}_post"] = ""
                row[f"pr{b}_post"] = ""
        else:
            for b in (1, 2, 3):
                row[f"br{b}_post"] = self.rid(post[b]) if post[b] else ""
                row[f"pr{b}_post"] = self.rid(resp_post[b]) if post[b] and resp_post[b] else ""
        # -------- runs
        runs = er = tur = rbi = 0
        for start, r in scored.items():
            col = "b" if start == 0 else str(start)
            row[f"run_{col}" if start == 0 else f"run{col}"] = self.rid(r.pid)
            who = r.responsible or moved_resp.get(r.pid) or current_pitcher
            row["prun_b" if start == 0 else f"prun{col}"] = self.rid(who)
            row["ur_b" if start == 0 else f"ur{col}"] = "0" if r.earned else "1"
            row["rbi_b" if start == 0 else f"rbi{col}"] = "1" if r.rbi else "0"
            runs += 1
            er += 1 if r.earned else 0
            tur += 1 if (r.team_unearned and r.earned) else 0
            rbi += 1 if r.rbi else 0
        row["runs"], row["er"], row["tur"], row["rbi"] = str(runs), str(er), str(tur), str(rbi)
        # -------- fielding credits
        po = [0] * 10
        a = [0] * 10
        errs = []
        all_credits = []
        for r in runners:
            for c in r.credits:
                code = c.get("position", {}).get("code", "")
                n = int(code) if code.isdigit() and 1 <= int(code) <= 9 else None
                kind = c.get("credit")
                if kind == "f_putout":
                    po[n or 0] += 1
                elif kind in ("f_assist", "f_assist_of"):
                    if n:
                        a[n] += 1
                elif kind in ("f_fielding_error", "f_throwing_error", "f_error_dropped_ball", "f_interference"):
                    if n:
                        errs.append((n, kind == "f_throwing_error", r))
                all_credits.append(c)
        a = [min(x, 1) for x in a]  # one assist a fielder per play (rule 9.10)
        for n in range(10):
            row[f"po{n}"] = str(po[n])
        for n in range(1, 10):
            row[f"a{n}"] = str(a[n])
        for n, _th, _r in errs:
            flag(f"e{n}")
        # -------- the batter's outcome
        btype = etype if batter_pa else ""
        traj = hit.get("trajectory", "")
        hittype = HIT_TYPES.get(traj, "")
        in_play = bool((ev.get("details") or {}).get("isInPlay")) and batter_pa
        hr = btype == "home_run"
        inside_hr = hr and batter_r is not None and any(c.get("credit") == "f_fielded_ball" for c in batter_r.credits)
        if batter_pa:
            flag("pa")
            if btype in ("single",):
                flag("single")
            elif btype == "double":
                flag("double")
            elif btype == "triple":
                flag("triple")
            elif hr:
                flag("hr")
            elif btype in ("walk", "intent_walk"):
                flag("walk")
                if btype == "intent_walk":
                    flag("iw")
            elif btype == "hit_by_pitch":
                flag("hbp")
            elif btype.startswith("strikeout"):
                flag("k")
                if batter_r and not batter_r.out:
                    flag("k_safe")
            elif btype in ("sac_bunt", "sac_bunt_double_play"):
                flag("sh")
            elif btype in ("sac_fly", "sac_fly_double_play"):
                flag("sf")
            elif btype in ("catcher_interf", "batter_interference_reached"):
                flag("xi")
            elif btype == "field_error":
                flag("roe")
            elif btype == "fielders_choice" or (btype == "fielders_choice_out" and batter_r and not batter_r.out
                                                and not any(r.out for r in runners)):
                flag("fc")
            else:
                flag("othout")
                flag("oth")
            if btype not in ("walk", "intent_walk", "hit_by_pitch", "sac_bunt", "sac_fly", "catcher_interf",
                             "sac_bunt_double_play", "sac_fly_double_play"):
                flag("ab")
            if in_play and (not hr or inside_hr):
                flag("bip")
            if in_play:
                if hittype.startswith("B"):
                    flag("bunt")
                elif hittype == "G":
                    flag("ground")
                elif hittype in ("F", "P"):
                    flag("fly")
                elif hittype == "L":
                    flag("line")
            if btype == "grounded_into_double_play":
                flag("gdp")
            elif outs_made == 2 and btype != "grounded_into_double_play":
                flag("othdp")
            elif outs_made == 3:
                flag("tp")
        row["hittype"] = hittype if (batter_pa and in_play) else ""
        # -------- runner events
        types = (list(dict.fromkeys([etype] + [r.events[0] for r in runners if r.events and r.start]))
                 if not batter_pa else []) + extra_types
        notation = []
        for t in types:
            n = RUNNER_EVENTS.get(t)
            if n is None:
                continue
            if n == "WP":
                flag("wp")
            elif n == "PB":
                flag("pb")
            elif n == "BK":
                flag("bk")
            elif n == "DI":
                flag("di")
            elif n == "OA":
                flag("oa")
            elif n.startswith("SB"):
                flag({"SB2": "sb2", "SB3": "sb3", "SBH": "sbh"}[n])
            elif n.startswith("CS") or n.startswith("POCS"):
                flag({"2": "cs2", "3": "cs3", "H": "csh"}[n[-1]])
            elif n.startswith("PO"):
                flag(f"pko{n[-1]}")
        # -------- notation
        row["event"] = self._notation(play, btype, etype, extra_types, runners, batter_r, hit, hittype, in_play,
                                      batter_pa)
        if batter_pa and btype.startswith("strikeout"):  # K+WP, K+PB found in MLB's description
            plus = row["event"].split("/")[0].split(".")[0]
            if "+WP" in plus and "wild_pitch" not in extra_types:
                flag("wp")
            if "+PB" in plus and "passed_ball" not in extra_types:
                flag("pb")
        # -------- fielding sequence columns
        out_runners = sorted([r for r in runners if r.out], key=lambda r: self._out_number(r))
        seqs = []
        for r in out_runners:
            seqs.append(_digits(r.credits) or "0")
        fseq = ""
        for s in seqs:
            if fseq and s and s[0] == fseq[-1]:
                s = s[1:]
            fseq += s
        row["fseq"] = fseq
        bat_outs = [r for r in out_runners if r.start == 0 or (batter_pa and btype not in RUNNER_EVENTS)]
        for k in range(1, 4):
            row[f"batout{k}"] = "0"
        bo = [(_digits(r.credits) or "0")[0] for r in out_runners] if batter_pa and in_play else []
        for k, d in enumerate(bo[:3], 1):
            row[f"batout{k}"] = d
        for r in out_runners:
            col = "brout_b" if r.start == 0 else f"brout{r.start}"
            if batter_pa and r.start == 0:
                continue
            row[col] = "0"
        first = ""
        if out_runners and batter_pa and in_play:
            first = (_digits(out_runners[0].credits) or "")[:1]
        if not first and batter_pa and in_play and batter_r is not None:
            for c in batter_r.credits:
                code = c.get("position", {}).get("code", "")
                if code.isdigit() and len(code) == 1:
                    first = code
                    break
            if not first:
                for r in runners:
                    for c in r.credits:
                        code = c.get("position", {}).get("code", "")
                        if code.isdigit() and len(code) == 1:
                            first = code
                            break
                    if first:
                        break
        row["firstf"] = first or "0"
        if batter_pa and in_play:
            row["_hit"] = hit  # (not a column: kept for checking locations)
            found = _LOC_IN_EVENT.search(row["event"].split(".")[0])  # the location the notation gives
            loc = found.group(1) if found else ""
            row["loc"] = loc + ("-" if hit.get("hardness") == "soft" else "+") if loc else ""
        dpopp = (batter_pa and in_play and hittype == "G" and pre[1] is not None and st["outs"] < 2
                 and btype not in ("single", "double", "triple", "home_run", "sac_bunt"))
        row["dpopp"] = "1" if dpopp else "0"
        row["pivot"] = "0"
        if dpopp:
            forced = [r for r in runners if r.start == 1 and r.out]
            if forced:
                pos = _digits(forced[0].credits, ("f_putout",))
                row["pivot"] = pos[:1] or row["firstf"]
            else:
                row["pivot"] = row["firstf"]
        for name, value in flags.items():
            row[name] = value
        # -------- the state after the play
        for start, r in scored.items():
            side = bat_side
            st["score"][side] += 1
        st["outs"] = outs_post
        for b in (1, 2, 3):
            st["bases"][b] = post[b] if outs_post < 3 else None
            st["resp"][b] = resp_post[b] if outs_post < 3 else None
        # outs for the fielders' innings
        if outs_made:
            for spell in self._current_spells(fld_side):
                spell[2] += outs_made
        self.plays.append(row)

    def _current_spells(self, side):
        s = self.sides[side]
        current = {(pid, f) for f, pid in s["fielders"].items()}
        latest = {}
        for spell in self.fielding_spells[side]:
            latest[(spell[0], spell[1])] = spell
        return [latest[k] for k in current if k in latest]

    @staticmethod
    def _out_number(r: _Runner) -> int:
        for m in r.segments:
            n = m["movement"].get("outNumber")
            if n:
                return n
        return 9

    # ------------------------------------------------------------------ notation
    def _notation(self, play, btype, etype, extra_types, runners, batter_r, hit, hittype, in_play,
                  batter_pa) -> str:
        """The play in Retrosheet's event notation: the main play, its modifiers, then the runners'
        advances that the main play doesn't already say ("S8/L8D.2-H;1-3")."""
        mods, post_mods = [], []
        implied = {}    # runner's start (0 the batter) -> where the main notation puts him (None: out)
        noted = set()   # runners whose error is already in the main notation
        in_advances = set()  # runners put out in the advances rather than the main notation
        if not batter_pa:
            parts = []
            for r in runners:
                code = RUNNER_EVENTS.get(r.events[0] if r.events else etype, RUNNER_EVENTS.get(etype, "OA"))
                e = _errors(r.credits)
                if code in ("SB2", "SB3", "SBH"):
                    parts.append({1: "SB2", 2: "SB3", 3: "SBH"}.get(r.start, code))
                    implied[r.start] = r.start + 1
                elif code.startswith(("CS", "POCS")):
                    code = code[:-1] + {1: "2", 2: "3", 3: "H"}.get(r.start, code[-1])
                    seq = _digits(r.credits)
                    if not r.out and e:
                        seq += f"E{e[0][0]}"
                        noted.add(r.pid)
                    parts.append(f"{code}({seq or '0'})")
                    implied[r.start] = None if r.out else r.start + 1
                elif code.startswith("PO"):
                    code = f"PO{r.start}"
                    if r.out:
                        parts.append(f"{code}({_digits(r.credits) or '0'})")
                        implied[r.start] = None
                    else:
                        parts.append(f"{code}(E{e[0][0]}{'/TH' if e[0][1] else ''})" if e else code)
                        noted.add(r.pid)
                        implied[r.start] = r.start
                elif not parts:
                    parts.append(code)
            parts = list(dict.fromkeys(parts))
            if parts and parts[0].startswith("SB"):
                parts.sort(key=lambda x: {"SBH": 0, "SB3": 1, "SB2": 2}.get(x, 3))
            main = ";".join(parts) or RUNNER_EVENTS.get(etype, "OA")
        else:
            t = btype
            desc = play.get("result", {}).get("description", "")
            fielded = "".join(c["position"]["code"] for c in (batter_r.credits if batter_r else [])
                              if c.get("credit") in ("f_fielded_ball", "f_deflection")
                              and c["position"]["code"].isdigit())
            outs = sorted([r for r in runners if r.out], key=self._out_number)
            if t in ("single", "double", "triple"):
                if t == "double" and "ground-rule" in desc:
                    main = "DGR"
                else:
                    main = {"single": "S", "double": "D", "triple": "T"}[t] + _squeeze(fielded)
                implied[0] = {"single": 1, "double": 2, "triple": 3}[t]
            elif t == "home_run":
                main = "HR" + (_squeeze(fielded) if fielded else "")
                implied[0] = 4
            elif t in ("walk", "intent_walk", "hit_by_pitch"):
                main = {"walk": "W", "intent_walk": "IW", "hit_by_pitch": "HP"}[t]
                implied[0] = 1
            elif t.startswith("strikeout"):
                main = "K"
                if batter_r is not None and batter_r.out and _digits(batter_r.credits) in ("", "2"):
                    implied[0] = None
            elif t == "catcher_interf":
                e = [c["position"]["code"] for r in runners for c in r.credits if c.get("credit") == "f_interference"]
                main = f"C/E{e[0] if e else '2'}"
            elif t == "field_error":
                e = _errors(batter_r.credits) if batter_r else []
                main = f"{_digits(batter_r.credits, ('f_assist',)) if batter_r else ''}E{e[0][0] if e else ''}"
                if e and e[0][1]:
                    mods.append("TH")
                if batter_r is not None:
                    noted.add(batter_r.pid)
            elif t in ("sac_bunt", "sac_fly") and batter_r is not None and not batter_r.out:
                e = _errors(batter_r.credits)
                if e:  # safe on an error
                    main = f"E{e[0][0]}"
                    noted.add(batter_r.pid)
                    post_mods.append("SH" if t == "sac_bunt" else "SF")
                else:
                    main = "FC"
                    mods.append("SH" if t == "sac_bunt" else "SF")
                in_advances.update(r.pid for r in outs)
            elif t in ("fielders_choice", "fielders_choice_out") and not (batter_r and batter_r.out):
                main = "FC" + (_squeeze(fielded)[:1] if t == "fielders_choice" and not outs else "")
                in_advances.update(r.pid for r in outs)
            else:
                # outs: the batter's (and, on a force play, the forced runners') in the main notation,
                # the others in the advances
                force = t in ("force_out", "grounded_into_double_play", "grounded_into_triple_play",
                              "triple_play") or (hittype in ("G", "BG") and t in ("double_play",))
                main_outs = [r for r in outs if r.start == 0 or (force and (
                    "r_force_out" in r.reasons or t in ("grounded_into_double_play", "triple_play",
                                                        "grounded_into_triple_play")))]
                if not main_outs and outs:
                    main_outs = outs[:1]
                in_advances.update(r.pid for r in outs if r not in main_outs)
                text = ""
                last = ""
                for k, r in enumerate(main_outs):
                    seq = _digits(r.credits) or "0"
                    if text and seq[:1] == last:
                        seq = seq[1:] or seq
                    tag = "" if (r.start == 0 and k == len(main_outs) - 1) else f"({BASE_CH[r.start]})"
                    text += seq + tag
                    last = (_digits(r.credits) or "0")[-1:]
                    implied[r.start] = None
                main = text
                if t == "force_out":
                    mods.append("FO")
                if t in ("sac_bunt", "sac_bunt_double_play"):
                    mods.append("SH")
                if t in ("sac_fly", "sac_fly_double_play"):
                    mods.append("SF")
                if t == "grounded_into_double_play":
                    mods.append("GDP")
                elif t in ("double_play", "sac_fly_double_play", "sac_bunt_double_play") or len(outs) == 2:
                    post_mods.append("DP")
                elif t in ("triple_play", "grounded_into_triple_play") or len(outs) == 3:
                    post_mods.append("TP")
            if in_play and hittype:
                ground = hittype in ("G", "BG")
                loc = hit_location(hit.get("coordinates"), home_run=(t == "home_run"), ground=ground)
                # foul or fair as MLB's description has it (only a caught fly can be foul)
                foul = "foul territory" in desc and not ground
                if loc.endswith("F") and not foul:
                    loc = loc[:-1]
                elif foul and loc and not loc.endswith("F"):
                    loc += "F"
                mods.append(hittype + loc + (HARDNESS.get(hit.get("hardness"), "") if loc else ""))
                if foul:
                    post_mods.append("FL")
            # runner events on the same pitch: K+SB2, W+WP
            plus = []
            for x in extra_types:
                n = RUNNER_EVENTS.get(x)
                rr = [r for r in runners if r.events and r.events[0] == x and r.start]
                if n and n.startswith("SB"):
                    for r in rr:
                        plus.append({1: "SB2", 2: "SB3", 3: "SBH"}[r.start])
                        implied[r.start] = r.start + 1
                elif n and n.startswith(("CS", "PO")):
                    for r in rr:
                        base = {1: "2", 2: "3", 3: "H"}[r.start]
                        if n.startswith("POCS"):
                            plus.append(f"POCS{base}({_digits(r.credits) or '0'})")
                        elif n.startswith("CS"):
                            plus.append(f"CS{base}({_digits(r.credits) or '0'})")
                        else:
                            plus.append(f"PO{r.start}({_digits(r.credits) or '0'})")
                        implied[r.start] = None if r.out else r.start + 1
                        in_advances.discard(r.pid)
                elif n:
                    plus.append(n)
            if t.startswith("strikeout") and batter_r is not None and not batter_r.out:
                low = desc.lower()
                plus += ["WP"] if "wild pitch" in low else ["PB"] if "passed ball" in low else []
            plus = list(dict.fromkeys(plus))
            if plus:
                main += "+" + ";".join(plus)
                if t.startswith("strikeout") and any(x.startswith(("CS", "PO")) for x in plus):
                    if "DP" not in post_mods:
                        post_mods.append("DP")
        text = main + "".join("/" + m for m in mods + post_mods)
        # -------- advances
        adv, batter_adv = [], []
        for r in sorted(runners, key=lambda r: -(r.start or 0)):
            start = r.start or 0
            if r.out:
                if start in implied and implied[start] is None and r.pid not in in_advances:
                    continue
                to = r.out_base if r.out_base else min(start + 1, 4)
                note = f"{BASE_CH[start]}X{BASE_CH[min(to, 4)]}({_digits(r.credits) or '0'})"
            else:
                end = 4 if r.scored else r.end
                if end is None or (end == start and start):
                    continue
                note = ""
                e = _errors(r.credits)
                if e and r.pid not in noted:
                    note += f"(E{e[0][0]}{'/TH' if e[0][1] else ''})"
                if r.scored:
                    if not r.rbi:
                        note += "(NR)"
                    if not r.earned:
                        note += "(UR)"
                    elif r.team_unearned:
                        note += "(TUR)"
                if implied.get(start, -1) == end and not note:
                    continue
                note = f"{BASE_CH[start]}-{BASE_CH[min(end, 4)]}{note}"
            (batter_adv if start == 0 else adv).append(note)
        if adv or batter_adv:
            text += "." + ";".join(adv + batter_adv)
        return text


    # ------------------------------------------------------------------ box-score files
    def _game_columns(self, side: str) -> dict:
        """The columns every box-score row ends with: date, number, site, v/h, opponent, result."""
        other = "home" if side == "away" else "away"
        ls = self.feed["liveData"].get("linescore", {}).get("teams", {})
        mine, theirs = ls.get(side, {}).get("runs", 0), ls.get(other, {}).get("runs", 0)
        return {"date": self.date, "number": self.gid[-1], "site": self.site, "vishome": "v" if side == "away" else "h",
                "opp": self.team[other], "win": str(int(mine > theirs)), "loss": str(int(mine < theirs)),
                "tie": str(int(mine == theirs)), "gametype": self.gametype, "box": "y", "pbp": "y"}

    def convert_box(self) -> dict[str, list[dict]]:
        """batting, pitching, fielding and teamstats rows, added up from the play-by-play."""
        plays = [r for r in self.plays]
        out = {"batting": [], "pitching": [], "fielding": [], "teamstats": []}
        decisions = self.feed["liveData"].get("decisions", {})
        dec = {key: self.rid(decisions[key]["id"]) for key in ("winner", "loser", "save") if decisions.get(key)}
        for side in ("away", "home"):
            tcode = self.team[side]
            tail = self._game_columns(side)
            bat_rows = [r for r in plays if r["batteam"] == tcode]
            fld_rows = [r for r in plays if r["pitteam"] == tcode]
            # ---- batting: everyone who appeared, by lineup spot and order, then pitchers who didn't bat
            seen, order = set(), []
            per_slot = {}
            for pid, slot, kind in self.appear[side]:
                rid = self.rid(pid)
                if rid in seen:
                    continue
                seen.add(rid)
                per_slot[slot] = per_slot.get(slot, 0) + 1
                order.append((rid, str(slot), str(per_slot[slot]), kind, pid))
            for pid in self.pitch_order[side]:
                rid = self.rid(pid)
                if rid not in seen:
                    seen.add(rid)
                    order.append((rid, "", "", "pitcher", pid))
            order.sort(key=lambda o: (int(o[1]) if o[1] else 99, int(o[2]) if o[2] else 0))
            dh_ids = {self.rid(pid) for pid, f in self._positions_held(side) if f == 10}
            totals = {}
            for rid, slot, seq, kind, pid in order:
                b = self._batting(rid, bat_rows)
                row = {"gid": self.gid, "id": rid, "team": tcode, "b_lp": slot, "b_seq": seq, "stattype": "value"}
                row.update(b)
                row["dh"] = "1" if rid in dh_ids else ""
                row["ph"] = "1" if kind == "ph" else ""
                row["pr"] = "1" if kind == "pr" else ""
                row.update(tail)
                out["batting"].append(row)
                for k, v in b.items():
                    totals[k] = totals.get(k, 0) + int(v)
            # ---- pitching
            ptot = {}
            pitchers = self.pitch_order[side]
            for n, pid in enumerate(pitchers, 1):
                rid = self.rid(pid)
                p = self._pitching(rid, fld_rows, last_pitcher=n == len(pitchers))
                row = {"gid": self.gid, "id": rid, "team": tcode, "p_seq": str(n), "stattype": "value"}
                row.update(p)
                row["wp"] = "1" if dec.get("winner") == rid else ""
                row["lp"] = "1" if dec.get("loser") == rid else ""
                row["save"] = "1" if dec.get("save") == rid else ""
                row["p_gs"] = "1" if n == 1 else ""
                row["p_gf"] = "1" if n == len(pitchers) and n > 1 else ""
                row["p_cg"] = "1" if n == 1 and len(pitchers) == 1 else ""
                row.update(tail)
                out["pitching"].append(row)
                for k, v in p.items():
                    ptot[k] = ptot.get(k, 0) + int(v)
            # ---- fielding: one row per player per position
            ftot = {}
            spells = {}
            for pid, pos, outs, started in self.fielding_spells[side]:
                key = (self.rid(pid), pos)
                if key not in spells:
                    spells[key] = [0, started]
                spells[key][0] += outs
                spells[key][1] = spells[key][1] or started
            seqs = {}
            for (rid, pos), (outs, started) in spells.items():
                seqs[rid] = seqs.get(rid, 0) + 1
                f = self._fielding(rid, pos, fld_rows)
                row = {"gid": self.gid, "id": rid, "team": tcode, "d_seq": str(seqs[rid]), "d_pos": str(pos),
                       "stattype": "value", "d_ifouts": str(outs)}
                row.update(f)
                row["d_gs"] = "1" if started else "0"
                row.update(tail)
                out["fielding"].append(row)
                for k, v in f.items():
                    if v != "":
                        ftot[k] = ftot.get(k, 0) + int(v)
            # pitchers first (in order), then the others in batting order, as Retrosheet lists them
            spot = {rid: (int(slot) if slot else 99, int(seq) if seq else 0) for rid, slot, seq, _k, _p in order}
            ptime = {self.rid(pid): i for i, pid in enumerate(pitchers)}
            rows_here = [r for r in out["fielding"] if r["team"] == tcode]
            rest = [r for r in out["fielding"] if r["team"] != tcode]
            rows_here.sort(key=lambda r: (0, ptime.get(r["id"], 99), 0) if r["d_pos"] == "1" and spot.get(
                r["id"], (99, 0))[0] == 99 else
                           (1,) + spot.get(r["id"], (99, 0)) + (int(r["d_seq"]),))
            out["fielding"] = rest + rows_here
            # ---- team line
            ls = self.feed["liveData"].get("linescore", {})
            row = {"gid": self.gid, "team": tcode}
            for i in range(1, 29):
                row[f"inn{i}"] = ""
            for inn in ls.get("innings", []):
                n = inn.get("num", 0)
                if 1 <= n <= 28 and "runs" in inn.get(side, {}):
                    row[f"inn{n}"] = str(inn[side]["runs"])
            tstats = self.box[side].get("teamStats", {}).get("batting", {})
            row["lob"] = ""  # (filled in below, from both sides' figures)
            row["mgr"] = self.managers.get(side, "")
            row["stattype"] = "value"
            for k in HEADERS["batting"][6:25]:
                row[k] = str(totals.get(k, 0))
            for k in HEADERS["pitching"][5:25]:
                row[k] = str(ptot.get(k, 0))
            # batters faced in the team's last inning in the field, if it got nobody out (a walk-off)
            last = (fld_rows[-1]["inning"], fld_rows[-1]["top_bot"]) if fld_rows else None
            spell = [r for r in fld_rows if (r["inning"], r["top_bot"]) == last]
            row["p_noout"] = "0" if any(r["outs_post"] != r["outs_pre"] for r in spell) else str(
                sum(1 for r in spell if r["pa"] == "1"))
            # the team's earned runs count every error in the inning (Retrosheet's "team earned runs")
            row["p_er"] = str(sum(int(r["er"]) - int(r["tur"]) for r in fld_rows))
            for k in ("d_po", "d_a", "d_e", "d_dp", "d_tp", "d_pb", "d_wp", "d_sb", "d_cs"):
                row[k] = str(ftot.get(k, 0))
            row["d_dp"] = str(sum(1 for r in fld_rows if r["gdp"] == "1" or r["othdp"] == "1"))
            row["d_tp"] = str(sum(1 for r in fld_rows if r["tp"] == "1"))
            starters = self.sides[side]["starters"]
            for i in range(1, 10):
                row[f"start_l{i}"] = self.rid(starters[i]) if starters.get(i) else ""
            sf = self.sides[side]["start_fielders"]
            for i in range(1, 10):
                row[f"start_f{i}"] = self.rid(sf[i]) if sf.get(i) else ""
            dh = [pid for pid in starters.values() if self._start_pos(side, pid) == 10]
            row["start_f10"] = self.rid(dh[0]) if dh else ""
            row.update(tail)
            out["teamstats"].append(row)
        # left on base as the box score proves it: plate appearances - runs - the other side's putouts
        if len(out["teamstats"]) == 2:
            for me, them in (out["teamstats"], out["teamstats"][::-1]):
                me["lob"] = str(int(me["b_pa"]) - int(me["b_r"]) - int(them["d_po"]))
        return out

    def _start_pos(self, side, pid):
        for p in self.box[side]["players"].values():
            if p["person"]["id"] == pid:
                codes = [a.get("code", "") for a in (p.get("allPositions") or [{}])]
                return 10 if "10" in codes and int(p.get("battingOrder") or 1) % 100 == 0 else _pos_num(codes[0])
        return 0

    def _positions_held(self, side):
        held = [(pid, self._start_pos(side, pid)) for pid in self.sides[side]["starters"].values()]
        held += [(pid, f) for pid, f, _o, _s in self.fielding_spells[side]]
        held += [(pid, 10) for pid in self.dh_seen[side]]
        return held

    @staticmethod
    def _batting(rid: str, rows) -> dict:
        n = lambda col: sum(1 for r in rows if r["batter"] == rid and r[col] == "1")
        sb = cs = 0
        for r in rows:
            for col, base in (("sb2", 1), ("sb3", 2), ("sbh", 3)):
                if r[col] == "1" and r[f"br{base}_pre"] == rid:
                    sb += 1
            for col, base in (("cs2", 1), ("cs3", 2), ("csh", 3)):
                if r[col] == "1" and r[f"br{base}_pre"] == rid:
                    cs += 1
        runs = sum(1 for r in rows for c in ("run_b", "run1", "run2", "run3") if r[c] == rid)
        h = sum(n(c) for c in ("single", "double", "triple", "hr"))
        return {"b_pa": str(n("pa")), "b_ab": str(n("ab")), "b_r": str(runs), "b_h": str(h), "b_d": str(n("double")),
                "b_t": str(n("triple")), "b_hr": str(n("hr")),
                "b_rbi": str(sum(int(r["rbi"]) for r in rows if r["batter"] == rid and r["pa"] == "1")),
                "b_sh": str(n("sh")), "b_sf": str(n("sf")), "b_hbp": str(n("hbp")), "b_w": str(n("walk")),
                "b_iw": str(n("iw")), "b_k": str(n("k")), "b_sb": str(sb), "b_cs": str(cs), "b_gdp": str(n("gdp")),
                "b_xi": str(n("xi")),
                # Retrosheet counts a fielder's choice with an error on the play as reached on error too
                "b_roe": str(n("roe") + sum(1 for r in rows if r["batter"] == rid and r["fc"] == "1"
                                            and any(r[f"e{k}"] != "0" for k in range(1, 10))))}

    @staticmethod
    def _pitching(rid: str, rows, last_pitcher: bool = False) -> dict:
        mine = [r for r in rows if r["pitcher"] == rid]
        n = lambda col: sum(1 for r in mine if r[col] == "1")
        outs = sum(int(r["outs_post"]) - int(r["outs_pre"]) for r in mine)
        runs = er = 0
        for r in rows:
            for col, ur in (("prun_b", "ur_b"), ("prun1", "ur1"), ("prun2", "ur2"), ("prun3", "ur3")):
                if r[col] == rid:
                    runs += 1
                    er += r[ur] != "1"
        noout = 0
        if mine and not last_pitcher:  # batters faced in his last inning, if he left without an out in it
            last = (mine[-1]["inning"], mine[-1]["top_bot"])
            spell = [r for r in mine if (r["inning"], r["top_bot"]) == last]
            if not any(int(r["outs_post"]) > int(r["outs_pre"]) for r in spell):
                noout = sum(1 for r in spell if r["pa"] == "1")
        return {"p_ipouts": str(outs), "p_noout": str(noout), "p_bfp": str(n("pa")),
                "p_h": str(sum(n(c) for c in ("single", "double", "triple", "hr"))), "p_d": str(n("double")),
                "p_t": str(n("triple")), "p_hr": str(n("hr")), "p_r": str(runs), "p_er": str(er),
                "p_w": str(n("walk")), "p_iw": str(n("iw")), "p_k": str(n("k")), "p_hbp": str(n("hbp")),
                "p_wp": str(n("wp")), "p_bk": str(n("bk")), "p_sh": str(n("sh")), "p_sf": str(n("sf")),
                "p_sb": str(n("sb2") + n("sb3") + n("sbh")), "p_cs": str(n("cs2") + n("cs3") + n("csh")),
                "p_pb": str(n("pb"))}

    @staticmethod
    def _fielding(rid: str, pos: int, rows) -> dict:
        at = [r for r in rows if (r["pitcher"] == rid if pos == 1 else r.get(f"f{pos}") == rid)]
        po = sum(int(r[f"po{pos}"]) for r in at)
        a = sum(int(r[f"a{pos}"]) for r in at)
        e = sum(int(r[f"e{pos}"]) for r in at)
        took_part = lambda r: r[f"po{pos}"] != "0" or r[f"a{pos}"] != "0"
        dp = sum(1 for r in at if (r["gdp"] == "1" or r["othdp"] == "1") and took_part(r))
        tp = sum(1 for r in at if r["tp"] == "1" and took_part(r))
        row = {"d_po": str(po), "d_a": str(a), "d_e": str(e), "d_dp": str(dp), "d_tp": str(tp),
               "d_pb": "", "d_wp": "", "d_sb": "", "d_cs": ""}
        if pos == 2:
            row.update({"d_pb": str(sum(1 for r in at if r["pb"] == "1")),
                        "d_wp": str(sum(1 for r in at if r["wp"] == "1")),
                        "d_sb": str(sum(int(r["sb2"]) + int(r["sb3"]) + int(r["sbh"]) for r in at)),
                        "d_cs": str(sum(int(r["cs2"]) + int(r["cs3"]) + int(r["csh"]) for r in at))})
        return row

    # ------------------------------------------------------------------ gameinfo
    def convert_gameinfo(self, oscorer: str = "") -> dict:
        gd, ld = self.feed["gameData"], self.feed["liveData"]
        dt = gd.get("datetime", {})
        weather = gd.get("weather", {})
        info = gd.get("gameInfo", {})
        cond = (weather.get("condition") or "").lower()
        sky = ("dome" if cond in ("dome", "roof closed") else "sunny" if cond in ("sunny", "clear") else
               "overcast" if cond == "overcast" else "cloudy" if "cloud" in cond else
               "night" if cond == "night" else "unknown")
        precip = ("drizzle" if "drizzle" in cond else "rain" if "rain" in cond else "snow" if "snow" in cond else
                  "none")
        wind = weather.get("wind") or ""
        speed = re.match(r"\s*(\d+)", wind)
        direction = wind.split(",", 1)[1].strip().lower() if "," in wind else ""
        winddir = {"out to cf": "tocf", "out to lf": "tolf", "out to rf": "torf", "in from cf": "fromcf",
                   "in from lf": "fromlf", "in from rf": "fromrf", "l to r": "ltor", "r to l": "rtol"}.get(
                       direction, "unknown")
        ls = ld.get("linescore", {}).get("teams", {})
        vr, hr = ls.get("away", {}).get("runs", 0), ls.get("home", {}).get("runs", 0)
        dec = ld.get("decisions", {})
        rid = lambda key: self.rid(dec[key]["id"]) if dec.get(key) else ""
        start = (dt.get("time", "") + dt.get("ampm", "")) if dt.get("time") else ""
        # the scheduled time, as Retrosheet gives it -- but the real first pitch for a doubleheader's
        # second game (whose scheduled time is only "after game 1")
        first = info.get("firstPitch") or dt.get("dateTime")
        offset = (gd.get("venue", {}).get("timeZone") or {}).get("offsetAtGameTime")
        if (not start or self.gid.endswith("2")) and first and offset is not None:
            try:
                local = datetime.strptime(first[:19], "%Y-%m-%dT%H:%M:%S") + timedelta(hours=offset)
                start = local.strftime("%I:%M%p").lstrip("0")
            except ValueError:
                pass
        uses_dh = any(v == 10 for s in ("away", "home") for v in self.sides[s]["pos"].values()) \
            if hasattr(self, "sides") else True
        row = {"gid": self.gid, "visteam": self.team["away"], "hometeam": self.team["home"], "site": self.site,
               "date": self.date, "number": self.gid[-1], "starttime": start,
               "daynight": dt.get("dayNight", ""), "innings": str(ld.get("linescore", {}).get("scheduledInnings", 9)),
               "tiebreaker": "2" if self.gametype == "regular" else "", "usedh": "true" if uses_dh else "false",
               "htbf": "", "timeofgame": str(info.get("gameDurationMinutes", "")),
               "attendance": str(info.get("attendance", "")), "fieldcond": "unknown", "precip": precip,
               "sky": sky, "temp": str(weather.get("temp", "")), "winddir": winddir,
               "windspeed": speed.group(1) if speed else "", "oscorer": oscorer, "forfeit": "",
               "suspend": self.resumed, "wp": rid("winner"), "lp": rid("loser"), "save": rid("save"),
               "gametype": self.gametype, "vruns": str(vr), "hruns": str(hr),
               "wteam": self.team["away"] if vr > hr else self.team["home"] if hr > vr else "",
               "lteam": self.team["home"] if vr > hr else self.team["away"] if hr > vr else "",
               "line": "y", "batteries": "both", "lineups": "y", "box": "y", "pbp": "y",
               "season": self.date[:4]}
        for col in UMPIRE_POSTS.values():
            row[col] = self.umpires.get(col) or "(none)"
        if self.gametype == "allstar":
            row["wteam"] = row["lteam"] = "" if False else row["wteam"]
        return row

def _pos_num(code: str) -> int:
    code = str(code)
    if code.isdigit():
        return int(code)
    return {"P": 1, "C": 2, "1B": 3, "2B": 4, "3B": 5, "SS": 6, "LF": 7, "CF": 8, "RF": 9, "DH": 10, "PH": 11,
            "PR": 12, "O": 7}.get(code, 0)


# --------------------------------------------------------------------------- #
#  The network                                                                #
# --------------------------------------------------------------------------- #
def fetch_json(url: str, tries: int = 5) -> dict:
    """GET JSON, gzip-compressed on the wire (a game's feed is ~800 KB, ~125 KB compressed); waits and
    tries again when MLB says too many requests or has a server error."""
    import gzip
    delay = 2.0
    for attempt in range(tries):
        request = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                data = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return json.loads(data)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                wait = float(exc.headers.get("Retry-After") or delay) if exc.headers else delay
                time.sleep(min(wait, 60))
                delay *= 2
                continue
            raise UptodateError(f"MLB returned HTTP {exc.code} for {url}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt < tries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise UptodateError(f"cannot reach statsapi.mlb.com ({exc})") from exc
        except ValueError as exc:
            raise UptodateError(f"MLB sent something that isn't JSON for {url}") from exc
    raise UptodateError(f"no answer from {url}")


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise UptodateError(f"cannot download {url} ({exc})") from exc


def finished_games(season: int, fetch=fetch_json) -> list[dict]:
    """The season's finished MLB games (regular season, postseason, All-Star) in date order:
    ``{"pk", "date", "home", "away", "doubleheader", "game_number", "type", "venue"}``."""
    url = (f"{API}/schedule?sportId=1&season={season}&gameType={','.join(GAME_TYPES)}"
           "&hydrate=officials&fields=dates,date,games,gamePk,gameType,officialDate,doubleHeader,gameNumber,"
           "status,codedGameState,teams,away,home,team,id,venue,id,name,resumeDate,resumedFrom,rescheduleDate,"
           "originalDate,officials,official,fullName,officialType")
    data = fetch(url)
    games, seen = [], set()
    for day in data.get("dates", []):
        for g in day.get("games", []):
            if g.get("status", {}).get("codedGameState") != "F" or g["gamePk"] in seen:
                continue
            seen.add(g["gamePk"])
            games.append({"pk": g["gamePk"], "date": g.get("officialDate", day.get("date")),
                          "home": g["teams"]["home"]["team"]["id"], "away": g["teams"]["away"]["team"]["id"],
                          "doubleheader": g.get("doubleHeader", "N"), "game_number": g.get("gameNumber", 1),
                          "type": g.get("gameType"), "venue": (g.get("venue") or {}).get("id"),
                          "venue_name": (g.get("venue") or {}).get("name", ""),
                          "officials": [(o.get("officialType"), o.get("official", {}).get("id"),
                                         o.get("official", {}).get("fullName", ""))
                                        for o in g.get("officials", [])]})
    games.sort(key=lambda g: (g["date"], g["pk"]))
    return games


def game_id(game: dict, original_date: str | None = None) -> str:
    """Retrosheet's game id: home team, date (the day it began), 0 or the game of a doubleheader."""
    home = TEAM_CODES.get(game["home"], "UNK")
    number = "0" if game["doubleheader"] == "N" else str(game["game_number"])
    return f"{home}{(original_date or game['date']).replace('-', '')}{number}"


# --------------------------------------------------------------------------- #
#  Retrosheet ids                                                             #
# --------------------------------------------------------------------------- #
BIO_URL = "https://www.retrosheet.org/downloads/biodata.zip"


class Ids:
    """MLB people, parks and umpires -> Retrosheet ids, remembered in the file's ``uptodate.json``."""

    def __init__(self, known: dict | None = None, bio: bytes | None = None, fetch=fetch_json):
        known = known or {}
        self.people = {int(k): v for k, v in known.get("people", {}).items()}       # MLB id -> rid
        self.names = dict(known.get("names", {}))                                   # rid -> [last, first, bat, throw]
        self.made = set(known.get("made", []))                                       # rids made up here
        self.umpires = {int(k): v for k, v in known.get("umpires", {}).items()}
        self.sites = {int(k): v for k, v in known.get("sites", {}).items()}
        self.managers = {k: v for k, v in known.get("managers", {}).items()}         # "team date" -> rid
        self.fetch = fetch
        self.lock = threading.Lock()
        self._bio = None
        self._bio_bytes = bio

    def as_json(self) -> dict:
        return {"people": {str(k): v for k, v in self.people.items()}, "names": self.names,
                "made": sorted(self.made), "umpires": {str(k): v for k, v in self.umpires.items()},
                "sites": {str(k): v for k, v in self.sites.items()}, "managers": self.managers}

    # ---- Retrosheet's biographical files (players, managers, umpires, parks), once a session
    def bio(self) -> dict:
        if self._bio is None:
            out = {"players": [], "managers": [], "umpires": [], "parks": []}
            try:
                data = self._bio_bytes if self._bio_bytes is not None else fetch_bytes(BIO_URL)
                z = zipfile.ZipFile(io.BytesIO(data))
                for name in z.namelist():
                    key = ("players" if name.startswith("biofile") else "managers" if name.startswith("managers")
                           else "umpires" if name.startswith("umpires") else "parks" if name.startswith("ballparks")
                           else None)
                    if key:
                        text = z.read(name).decode("latin-1")
                        out[key] = list(csv.DictReader(io.StringIO(text)))
            except (UptodateError, zipfile.BadZipFile, KeyError):
                pass  # no biographical files: fall back to made-up ids
            self._bio = out
        return self._bio

    def taken(self) -> set:
        bio = self.bio()
        return ({r.get("id", "") for r in bio["players"]} | {r.get("id", "") for r in bio["managers"]}
                | {r.get("id", "") for r in bio["umpires"]} | set(self.people.values()) | set(self.umpires.values()))

    # ---- players (and managers, who are looked up the same way)
    def resolve_people(self, people: list[dict]):
        """Give every person in ``people`` (MLB person dicts: id, names, birthDate, batSide, pitchHand) a
        Retrosheet id: MLB's cross-reference, else Retrosheet's biographical file (last name and
        birth date), else one made up."""
        need = [p for p in people if p.get("id") and p["id"] not in self.people]
        need = list({p["id"]: p for p in need}.values())
        xref, full = {}, {}
        for i in range(0, len(need), 100):
            ids = ",".join(str(p["id"]) for p in need[i:i + 100])
            try:
                data = self.fetch(f"{API}/people?personIds={ids}&hydrate=xrefId")
            except UptodateError:
                data = {}
            for person in data.get("people", []):
                full[person["id"]] = person
                for x in person.get("xrefIds", []):
                    if x.get("xrefType") == "retrosheet" and x.get("xrefId"):
                        xref[person["id"]] = x["xrefId"]
        bio = None
        for p in need:
            person = {**p, **full.get(p["id"], {})}
            rid = xref.get(p["id"])
            if not rid:
                bio = bio or self._bio_index()
                rid = self._from_bio(person, bio)
            with self.lock:
                if not rid:
                    last, first = split_name(person)
                    rid = make_id(last, first, self.taken())
                    self.made.add(rid)
                self.people[p["id"]] = rid
        for p in people:
            rid = self.people.get(p.get("id"))
            if rid and rid not in self.names:
                person = {**p, **full.get(p["id"], {})}
                bio_row = self._bio_by_id().get(rid)
                if bio_row:  # Retrosheet's own spelling ("Acuna", "J. P.")
                    last, first = bio_row.get("lastname", ""), bio_row.get("usename", "")
                else:
                    last, first = (_plain_name(x) for x in split_name(person))
                bat = (person.get("batSide") or {}).get("code", "")
                self.names[rid] = [last, first, "B" if bat == "S" else bat,
                                   (person.get("pitchHand") or {}).get("code", "")]

    def _bio_by_id(self) -> dict:
        if not hasattr(self, "_by_id"):
            self._by_id = {r.get("id"): r for r in self.bio()["players"]}
        return self._by_id

    def _bio_index(self) -> dict:
        index = {}
        for r in self.bio()["players"]:
            index.setdefault(r.get("birthdate", ""), []).append(r)
        return index

    @staticmethod
    def _from_bio(person: dict, index: dict) -> str:
        born = (person.get("birthDate") or "").replace("-", "")
        if not born:
            return ""
        last = _ascii(person.get("lastName") or person.get("useLastName") or "").lower()
        hits = [r for r in index.get(born, []) if _ascii(r.get("lastname", "")).lower() == last]
        if len(hits) != 1:
            alt = _ascii(person.get("useLastName") or "").lower()
            hits = [r for r in index.get(born, []) if _ascii(r.get("lastname", "")).lower() in (last, alt)]
        return hits[0]["id"] if len(hits) == 1 else ""

    def rid(self, pid) -> str:
        if pid is None:
            return ""
        rid = self.people.get(pid)
        if rid is None:  # someone the feed didn't list among the game's players (rare): made up now
            with self.lock:
                rid = self.people.get(pid) or make_id(f"p{pid}", "x", self.taken())
                self.people[pid] = rid
                self.made.add(rid)
        return rid

    # ---- umpires
    def umpire(self, mlb_id, name: str) -> str:
        if not mlb_id:
            return "(none)"
        if mlb_id in UMPIRES:
            return UMPIRES[mlb_id]
        with self.lock:
            if mlb_id in self.umpires:
                return self.umpires[mlb_id]
            parts = name.split()
            first, last = (parts[0], " ".join(parts[1:])) if len(parts) > 1 else ("", name)
            hits = [r["id"] for r in self.bio()["umpires"]
                    if _ascii(r.get("lastname", "")).lower() == _ascii(last).lower()
                    and _ascii(r.get("firstname", "")).lower() == _ascii(first).lower()]
            rid = hits[-1] if len(hits) >= 1 else make_id(last, first, self.taken(), umpire=True)
            if not hits:
                self.made.add(rid)
            self.umpires[mlb_id] = rid
            return rid

    # ---- parks
    def site(self, venue_id, name: str, home_team: int) -> str:
        if venue_id in SITES:
            return SITES[venue_id]
        with self.lock:
            if venue_id in self.sites:
                return self.sites[venue_id]
            norm = lambda t: re.sub(r"[^a-z0-9]", "", _ascii(t).lower())
            hits = [r for r in self.bio()["parks"] if norm(r.get("name", "")) == norm(name)]
            if hits:
                code = sorted(hits, key=lambda r: r.get("last_g", ""))[-1].get("site", "")
            else:
                # Retrosheet's way: three letters of the city and a number, made up here
                code = (TEAM_CODES.get(home_team, "UNK")[:3] + "99")
                self.made.add(code)
            self.sites[venue_id] = code
            return code

    # ---- managers
    def manager(self, team_id: int, date: str) -> str:
        """The team's manager on ``date`` (MLB's coaches list; interim managers count)."""
        key = f"{team_id} {date}"
        if key in self.managers:
            return self.managers[key]
        try:
            data = self.fetch(f"{API}/teams/{team_id}/coaches?date={date}")
        except UptodateError:
            return ""
        roster = data.get("roster", [])
        boss = [c for c in roster if c.get("job") == "Manager"] or [c for c in roster if c.get("job") == "Interim Manager"]
        rid = ""
        if boss:
            person = boss[0]["person"]
            self.resolve_people([person])
            rid = self.people.get(person["id"], "")
            if rid in self.made:  # not a player: Retrosheet's managers' list, else a manager's made-up id
                parts = person.get("fullName", "").split()
                hits = [r["id"] for r in self.bio()["managers"]
                        if parts and _ascii(r.get("lastname", "")).lower() == _ascii(parts[-1]).lower()
                        and _ascii(r.get("firstname", "")).lower() == _ascii(parts[0]).lower()]
                if hits:
                    with self.lock:
                        self.made.discard(rid)
                        rid = self.people[person["id"]] = hits[-1]
        with self.lock:
            self.managers[key] = rid
        return rid


# --------------------------------------------------------------------------- #
#  One game end to end                                                        #
# --------------------------------------------------------------------------- #
def convert_game(feed: dict, game: dict, ids: Ids) -> dict[str, list[dict]]:
    """A finished game's MLB feed -> its rows for every file but allplayers."""
    gd = feed["gameData"]
    people = list(gd.get("players", {}).values())
    ids.resolve_people(people)
    original = (gd.get("datetime") or {}).get("originalDate")
    gid = game_id(game, original)
    venue = gd.get("venue", {})
    site = ids.site(venue.get("id"), venue.get("name", ""), game["home"])
    umps = {}
    for o in feed["liveData"]["boxscore"].get("officials", []):
        col = UMPIRE_POSTS.get(o.get("officialType"))
        if col:
            umps[col] = ids.umpire(o.get("official", {}).get("id"), o.get("official", {}).get("fullName", ""))
    for col in UMPIRE_POSTS.values():
        umps.setdefault(col, "(none)")
    gametype = GAME_TYPES.get(game["type"], "regular")
    conv = GameConverter(feed, gid, ids.rid, site, umps, gametype)
    plays = conv.convert_plays()
    if game["type"] != "A":
        conv.managers = {side: ids.manager(gd["teams"][side]["id"], game["date"]) for side in ("away", "home")}
    out = conv.convert_box()
    out["plays"] = [{k: r.get(k, "") for k in HEADERS["plays"]} for r in plays]
    out["gameinfo"] = [conv.convert_gameinfo()]
    return out


def all_players(files: dict[str, list[dict]], names: dict) -> list[dict]:
    """allplayers: one row per player per team, games by position, over every game in the file --
    the way Retrosheet's is made (every game type counts)."""
    games = {}
    for r in files.get("batting", []):
        key = (r["id"], r["team"])
        g = games.setdefault(key, {"g": set(), "dates": set(), "pos": {}})
        g["g"].add(r["gid"])
        g["dates"].add(r["date"])
        for col in ("dh", "ph", "pr"):
            if r.get(col) == "1":
                g["pos"].setdefault(col, set()).add(r["gid"])
    for r in files.get("fielding", []):
        key = (r["id"], r["team"])
        g = games.setdefault(key, {"g": set(), "dates": set(), "pos": {}})
        g["g"].add(r["gid"])
        g["dates"].add(r["date"])
        g["pos"].setdefault(r["d_pos"], set()).add(r["gid"])
    for r in files.get("pitching", []):
        if r.get("p_gs") == "1":
            games.setdefault((r["id"], r["team"]), {"g": set(), "dates": set(), "pos": {}})["pos"].setdefault(
                "sp", set()).add(r["gid"])
    out = []
    for (rid, team), g in sorted(games.items()):
        pos = g["pos"]
        n = lambda k: len(pos.get(k, ()))
        last, first, bat, throw = (names.get(rid) or ["", "", "", ""])[:4]
        out.append({"id": rid, "last": last, "first": first, "bat": bat, "throw": throw, "team": team,
                    "g": str(len(g["g"])), "g_p": str(n("1")), "g_sp": str(n("sp")), "g_rp": str(n("1") - n("sp")),
                    "g_c": str(n("2")), "g_1b": str(n("3")), "g_2b": str(n("4")), "g_3b": str(n("5")),
                    "g_ss": str(n("6")), "g_lf": str(n("7")), "g_cf": str(n("8")), "g_rf": str(n("9")),
                    "g_of": str(len(pos.get("7", set()) | pos.get("8", set()) | pos.get("9", set()))),
                    "g_dh": str(n("dh")), "g_ph": str(n("ph")), "g_pr": str(n("pr")),
                    "first_g": min(g["dates"]) if g["dates"] else "", "last_g": max(g["dates"]) if g["dates"] else ""})
    return out


# --------------------------------------------------------------------------- #
#  The file                                                                   #
# --------------------------------------------------------------------------- #
def data_dir() -> str:
    """Data folder: %APPDATA% / %LOCALAPPDATA% on Windows, ~/Library on macOS,
    the usual XDG folder on Linux."""
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "BallSheet")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "BallSheet")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "ballsheet")


def default_path(season: int) -> str:
    """Where the season's file is kept: ``~/.local/share/ballsheet/2026csvs.zip``."""
    return os.path.join(data_dir(), f"{season}csvs.zip")


_BOX_FILES = ("batting", "fielding", "pitching")  # the ones allplayers is added up from


def _gid_of(line: str) -> str:
    return line[:line.find(",")]


def read_lines(path: str) -> tuple[dict[str, list[str]], dict]:
    """(each file's CSV lines without the header, the ``uptodate.json`` meta) of a file made here; empty
    if there's none.  Lines stay text: a season's plays are never parsed just to be written out again."""
    lines = {name: [] for name in FILES}
    if not os.path.exists(path):
        return lines, {}
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            meta = json.loads(z.read(META)) if META in names else {}
            for name in FILES:
                member = next((n for n in names if n.endswith(f"{name}.csv")), None)
                if member:
                    text = z.read(member).decode("utf-8").splitlines(keepends=True)
                    if text and text[0].rstrip("\r\n").split(",") != HEADERS[name]:
                        meta["format"] = -1  # another layout: made again from scratch
                    lines[name] = text[1:]
    except (zipfile.BadZipFile, OSError, ValueError, KeyError) as exc:
        raise UptodateError(f"can't read {path} ({exc})") from exc
    return lines, meta


def read_bundle(path: str) -> tuple[dict[str, list[dict]], dict]:
    """(rows by file, as dicts, the ``uptodate.json`` meta) of a file made here; empty if there's none."""
    lines, meta = read_lines(path)
    return {name: _parse(name, rows) for name, rows in lines.items()}, meta


def _parse(name: str, lines: list[str]) -> list[dict]:
    return list(csv.DictReader(io.StringIO("".join(lines)), fieldnames=HEADERS[name]))


def _to_lines(name: str, rows: list[dict]) -> list[str]:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    cols = HEADERS[name]
    w.writerows([row.get(c, "") for c in cols] for row in rows)
    return buf.getvalue().splitlines(keepends=True)


def _sort_key(gid: str) -> tuple:
    return gid[3:11], gid


def write_bundle(path: str, season: int, files: dict[str, list], meta: dict):
    """Write the file (atomically: a half-written file never replaces a good one).  ``files`` holds
    each file's rows as dicts or as CSV lines; games are put in date order, each keeping its own."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".part"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for name in FILES:
                rows = files.get(name, [])
                lines = rows if not rows or isinstance(rows[0], str) else _to_lines(name, rows)
                if name != "allplayers":
                    order = {gid: i for i, gid in enumerate(sorted({_gid_of(x) for x in lines}, key=_sort_key))}
                    lines = sorted(lines, key=lambda x: order[_gid_of(x)])  # stable
                z.writestr(f"{season}{name}.csv", ",".join(HEADERS[name]) + "\n" + "".join(lines))
            z.writestr(META, json.dumps(meta, indent=1))
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise UptodateError(f"can't write {path} ({exc})") from exc


def file_summary(path: str) -> dict | None:
    """What a file holds, read from its meta only: ``{"season", "games", "last_date", "updated"}``; None if
    there's no file."""
    if not os.path.exists(path):
        return None
    try:
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read(META))
    except (zipfile.BadZipFile, OSError, ValueError, KeyError):
        return {"season": None, "games": 0, "last_date": "", "updated": "", "broken": True}
    games = meta.get("games", {})
    dates = [g.get("date", "") for g in games.values()]
    return {"season": meta.get("season"), "games": len(games), "last_date": max(dates) if dates else "",
            "updated": meta.get("updated", ""), "size": os.path.getsize(path), "made": len(meta.get("ids", {}).get("made", []))}


def delete_file(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise UptodateError(f"can't delete {path} ({exc})") from exc


def retrosheet_has(season: int) -> bool:
    """Whether Retrosheet has published the season's own file yet (a HEAD request)."""
    request = urllib.request.Request(f"https://www.retrosheet.org/downloads/{season}/{season}csvs.zip",
                                     headers={"User-Agent": _UA}, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False


def update(season: int, path: str | None = None, progress=None, cancelled=lambda: False,
           fetch=fetch_json, bio: bytes | None = None, save_every: int = 500) -> dict:
    """Bring the season's file up to date: every finished game it doesn't have yet is downloaded and
    added (a new file is made if there's none).  ``progress(done, total, text)`` is called as games come
    in; ``cancelled()`` stops it early (what's been done so far is kept).  -> a summary dict."""
    path = path or default_path(season)
    lines, meta = read_lines(path)
    if meta and meta.get("season") not in (None, season):
        raise UptodateError(f"{path} holds {meta.get('season')}, not {season}")
    if meta.get("format", FORMAT_VERSION) != FORMAT_VERSION:
        lines, meta = {name: [] for name in FILES}, {}   # an older layout: start again
    have = meta.get("games", {})
    have_gids = {_gid_of(x) for x in lines.get("gameinfo", [])}
    box = {name: _parse(name, lines[name]) for name in _BOX_FILES}  # for allplayers
    if progress:
        progress(0, 0, f"Getting MLB's {season} schedule")
    games = finished_games(season, fetch)
    todo = [g for g in games if str(g["pk"]) not in have]
    total = len(todo)
    ids = Ids(meta.get("ids"), bio=bio, fetch=fetch)
    if progress:
        progress(0, total, f"{len(have)} games already in the file; {total} to download")
    added, failed = 0, []
    lock = threading.Lock()

    def save():
        lines["allplayers"] = _to_lines("allplayers", all_players(box, ids.names))
        meta.update({"format": FORMAT_VERSION, "season": season, "games": have, "ids": ids.as_json(),
                     "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                     "source": "MLB Stats API (statsapi.mlb.com) game feeds, laid out as Retrosheet's CSV files"})
        write_bundle(path, season, lines, meta)

    def work(game):
        if cancelled():
            return game, None, None
        try:
            feed = fetch(f"{API_V11}/game/{game['pk']}/feed/live")
            return game, convert_game(feed, game, ids), None
        except (UptodateError, KeyError, IndexError, TypeError, ValueError) as exc:
            return game, None, exc

    done = 0
    since_save = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for game, rows, error in pool.map(work, todo):
            done += 1
            if rows is not None:
                gid = rows["gameinfo"][0]["gid"]
                with lock:
                    if gid in have_gids:  # the same game id twice (shouldn't happen): keep the first
                        failed.append((game["pk"], f"duplicate game id {gid}"))
                    else:
                        for name, new in rows.items():
                            lines[name].extend(_to_lines(name, new))
                            if name in box:
                                box[name].extend(new)
                        have_gids.add(gid)
                        have[str(game["pk"])] = {"gid": gid, "date": game["date"]}
                        added += 1
                        since_save += 1
            elif error is not None:
                failed.append((game["pk"], str(error)))
            if progress:
                progress(done, total, f"{done} of {total} games downloaded, {total - done} left")
            if since_save >= save_every:
                save()
                since_save = 0
            if cancelled():
                break
    if added or not os.path.exists(path):
        save()
    return {"path": path, "added": added, "failed": failed, "games": len(have), "cancelled": cancelled(),
            "made": len(ids.made), "last_date": max((g["date"] for g in have.values()), default="")}
