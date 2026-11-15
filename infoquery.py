#!/usr/bin/env python3
"""Info-tab queries: a range of values -- one per game or per season, for one or more
players or teams -- worked out from formula "series" over the data the other tabs show,
ready to be tabled (Info tab) or graphed (Infograph tab).  No Qt here.

A query is a JSON object (a file may hold one, or a list of them), e.g.::

    {
      "name": "George Brett's 1980 chase for .400",
      "source": "player-games",            # player-games | player-seasons | team-games
      "subjects": ["George Brett"],        # players or teams; see resolve_* below
      "season": 1980,                      # the games sources only; "career" (player-games)
      "postseason": false,                 #   = every season he played, from 1901
      "where": ["{Batting: H} >= 1"],      # keep only the games (or seasons) where all hold
      "x": "date",                         # date | game | age (player-games), game | date
                                           #   (team-games),  age | season  (player-seasons)
      "running": true,                     # each formula over the running totals so far
      "series": [
        {"name": "Hits", "formula": "{Batting: H}", "panel": 1},
        {"name": "Batting average", "formula": "{Batting: H} / {Batting: AB}", "panel": 2,
         "format": "rate"}
      ],
      "panels": {"2": {"min": 0.2, "max": 0.45}},
      "reference_lines": [{"panel": 2, "value": 0.4, "label": ".400"}]
    }

Formulas are the Custom tab's (``formula.py``), over the same variable names as a
Player-level custom stat -- ``{Batting: H}``, ``{Pitching: SO}``, ``{Fielding: PO}`` -- plus
``{Team: W}`` / ``L`` / ``T`` / ``G`` / ``Runs`` / ``Runs allowed`` (the team's result
that game), ``{Player: Age}`` / ``{Player: Season}`` (season by season), and
``{Custom: <name>}`` for any Player-level custom stat.  A series can override the
query's ``source``, ``subjects``, ``season`` and ``postseason`` -- so one graph can
combine, say, a player's games with his team's -- as long as every series shares the
query's x axis.

``league-seasons`` is every player's season in all of MLB over ``season`` .. ``season_to``,
each judged at his main position (the one he played most innings at; only those listed in
``positions`` are kept) against MLB's figures that year -- MLB meaning the ``leagues`` listed,
by default the AL and NL (the Negro Leagues' records are too fragmentary to rank by rate):
``{MLB: AVG}``, ``{MLB: RF/9}`` at his position, the leagues' batting totals ``{MLB: PA}``,
``{MLB: RBI}`` ... -- then ranked by the ``rank_by`` formula, highest first, and the
``top`` few kept (``distinct``: a player's best season only).  It needs no subjects; its x
axis is the rank.

With ``"pitchers": true`` it's every pitcher's season instead (no positions): his pitching line,
``{Team: G}``, ``{Player: Season}`` and MLB's ``{MLB: R/9}`` / ``{MLB: ERA}`` that season, plus
his most-thrown pitch from pitch tracking (2008 on; Statcast from 2015) -- ``{Pitch: Primary %}``
of his pitches, at ``{Pitch: Primary mph}``.  ``"order": "lowest"`` ranks the lowest ``rank_by``
first (runs allowed, say), and ``"each": "primary pitch"`` keeps the ``top`` few for each pitch
type separately -- the best pitcher-season by each kind of main pitch.  The pitch mix is fetched
only for seasons that pass ``where`` (one request per pitcher-season, cached).

``"chart": "bars"`` draws the query as bars instead of lines: one value per subject for each
series -- the formula over all his games or seasons together (as ``running`` totals end up) --
grouped by subject in the order the subjects are listed, a bar per series -- or, for series given
the same ``"stack"`` name, one bar with each piled on the one before.  A ranked list (``league-seasons``)
is already one value per season: drawn as bars, it's a group per rank.  The x axis is
then unused (it names the subjects), and the table has a row per subject.

``where`` conditions are two formulas (or a formula and a number) joined by one of
``= != < <= > >=``; a game or season is kept only when every condition holds, and
``running`` totals, ``x: game`` numbering and graphs then see the kept ones alone --
``{Pitching: H} = 0`` and ``{Pitching: Outs} >= 27`` pick out a pitcher's no-hitters.
"""
from __future__ import annotations

import datetime as _dt
import functools
import json
import re
from concurrent.futures import ThreadPoolExecutor

import formula
import mlbstats

SOURCES = {
    "player-games": "Player, game by game (Dailies)",
    "player-seasons": "Player, season by season (Personal)",
    "team-games": "Team, game by game (Dailies, Team level)",
    "league-seasons": "All MLB player seasons, ranked",
    "league-games": "All MLB team-games, ranked",
}
RANKED = ("league-seasons", "league-games")  # every player's season / every team's game, ranked
X_CHOICES = {"player-games": ["date", "game", "age"], "team-games": ["game", "date"],
             "player-seasons": ["age", "season"], "league-seasons": ["rank"],
             "league-games": ["rank"]}
X_LABELS = {"date": "Date", "game": "Game", "age": "Age", "season": "Season", "rank": "Rank"}
FORMATS = ("auto", "int", "rate", "1", "2", "3")  # rate = .400 style
MAX_PANELS = 4
CAREER = "career"  # a player-games "season": every season he played
CHARTS = ("lines", "bars")
_COMPARE = {"=": lambda a, b: abs(a - b) < 1e-9, "!=": lambda a, b: abs(a - b) >= 1e-9,
            "<": lambda a, b: a < b, "<=": lambda a, b: a <= b + 1e-9,
            ">": lambda a, b: a > b, ">=": lambda a, b: a >= b - 1e-9}
_COMPARE_RE = re.compile(r"(<=|>=|!=|==|=|<|>)")

_GROUPS = {"Batting": "hitting", "Pitching": "pitching", "Fielding": "fielding"}
# (variable name, API group, stat key, how many rows combine: "sum" or "avg")
STAT_VARIABLES = [(f"{view}: {label}", _GROUPS[view], key, how)
                  for view in _GROUPS
                  for label, key, how in mlbstats.REGRESSION_VARIABLES[view]]
_seen: set = set()
STAT_VARIABLES = [v for v in STAT_VARIABLES if not (v[0] in _seen or _seen.add(v[0]))]
TEAM_VARIABLES = ["Team: G", "Team: W", "Team: L", "Team: T", "Team: Runs", "Team: Runs allowed"]
PLAYER_VARIABLES = ["Player: Age", "Player: Season"]
# league-seasons: what all of MLB did that season (RF at his main position), his team's games
# (all MLB's average for a traded player), and range factor worked out from exact innings
LEAGUE_VARIABLES = ["MLB: AVG", "MLB: OBP", "MLB: SLG", "MLB: RF/9", "MLB: RF/G",
                    "Team: G", "Player: Season", "Fielding: RF/G"]
# ...and the leagues' totals of each batting count ({MLB: PA}, {MLB: RBI} ...), for league rates
_LEAGUE_TOTALS = [(name.replace("Batting: ", "MLB: "), key) for name, g, key, how in STAT_VARIABLES
                  if g == "hitting" and how == "sum" and name != "Batting: G"]
LEAGUE_VARIABLES += [name for name, _key in _LEAGUE_TOTALS if name not in LEAGUE_VARIABLES]
# league-seasons with "pitchers": MLB's runs and earned runs per nine that season, and his main pitch
PITCH_VARIABLES = ["Pitch: Primary %", "Pitch: Primary mph"]
PITCHER_LEAGUE_VARIABLES = ["MLB: R/9", "MLB: ERA", "Team: G", "Player: Season"] + PITCH_VARIABLES
EACH_CHOICES = ("primary pitch",)
# league-games: one team's game, from the line score -- how it batted inning by inning
LINESCORE_FROM_SEASON = 1901
INNING_VARIABLES = [f"Inning: {n}" for n in range(1, 10)] + ["Inning: Extra"]
GAME_VARIABLES = ["Team: W", "Team: L", "Team: T", "Team: Runs", "Team: Runs allowed", "Team: Home",
                  "Innings: Batted", "Innings: Scored", "Innings: Scoreless", "Innings: Most runs",
                  "Innings: Fewest runs", "Innings: Opponent batted", "Innings: Opponent scored",
                  "Innings: Game", "Game: Season", "Game: Date", "Game: Postseason"] + INNING_VARIABLES
POSITIONS = [pos for pos, _name in mlbstats.RANGE_POSITIONS]
MAX_TOP = 100
LEAGUES = ["AL", "NL"]
_SUMMED = {name for name, _g, _k, how in STAT_VARIABLES if how == "sum"} | set(TEAM_VARIABLES)
_INNINGS_KEYS = ("inningsPitched", "innings")  # "172.1" means 172 1/3


class QueryError(ValueError):
    """A query definition is malformed, or names something that can't be found."""



def _formula_variables(text: str) -> set[str]:
    """:func:`formula.formula_variables`, with ``{Custom: ...}`` left a variable: Info works
    custom stats out itself, over its own variables."""
    return formula.formula_variables(text, expand=False)


def _evaluate_formula(text: str, env: dict):
    return formula.evaluate_formula(text, env, expand=False)

def variables_for(source: str, pitchers: bool = False) -> list[str]:
    """The variable names a formula can use with ``source`` (for the Insert list);
    ``pitchers``: a league-seasons query of pitchers."""
    names = [name for name, *_ in STAT_VARIABLES] + TEAM_VARIABLES
    if source == "player-seasons":
        names = [name for name, *_ in STAT_VARIABLES] + PLAYER_VARIABLES
    if source == "league-seasons":
        if pitchers:
            names = [name for name, g, *_ in STAT_VARIABLES if g == "pitching"] + PITCHER_LEAGUE_VARIABLES
        else:
            names = [name for name, g, *_ in STAT_VARIABLES if g in ("hitting", "fielding")] + LEAGUE_VARIABLES
    if source == "league-games":
        names = list(GAME_VARIABLES)
    return names


# --------------------------------------------------------------------------- #
#  Definitions                                                                #
# --------------------------------------------------------------------------- #
def _series_source(q: dict, s: dict) -> str:
    return s.get("source") or q["source"]


def parse_query(obj) -> dict:
    """Check one query object and fill in the defaults -> a normalised copy.
    Raises :class:`QueryError` saying what's wrong."""
    if not isinstance(obj, dict):
        raise QueryError("a query must be a JSON object")
    q = {"name": str(obj.get("name", "")).strip(), "description": str(obj.get("description", "")),
         "title": str(obj.get("title", "")).strip(), "source": obj.get("source", "")}
    if not q["name"]:
        raise QueryError("the query has no name")
    if q["source"] not in SOURCES:
        raise QueryError(f"{q['name']}: source must be one of {', '.join(SOURCES)}")
    league = q["source"] in RANKED
    q["subjects"] = [] if league else _subjects(obj.get("subjects"), q["name"])
    q["season"] = _season(obj.get("season"), q["name"], needed=q["source"] != "player-seasons",
                          career=q["source"] == "player-games")
    if league:
        q["season_to"] = _season(obj.get("season_to"), q["name"], needed=False) or q["season"]
        if q["season_to"] < q["season"]:
            raise QueryError(f"{q['name']}: season_to comes before season")
        q["rank_by"] = str(obj.get("rank_by", "")).strip()
        try:
            _formula_variables(q["rank_by"])
        except formula.FormulaError as exc:
            raise QueryError(f"{q['name']}: rank_by: {exc}") from exc
        try:
            q["top"] = int(obj.get("top", 10))
        except (TypeError, ValueError):
            raise QueryError(f"{q['name']}: top must be a number") from None
        if not 1 <= q["top"] <= MAX_TOP:
            raise QueryError(f"{q['name']}: top must be 1 to {MAX_TOP}")
    if q["source"] == "league-games" and q["season"] < LINESCORE_FROM_SEASON:
        raise QueryError(f"{q['name']}: MLB's line scores begin in {LINESCORE_FROM_SEASON}")
    if league:
        q["positions"] = [str(p).upper() for p in _as_list(obj.get("positions"))] or list(POSITIONS)
        bad = [p for p in q["positions"] if p not in POSITIONS]
        if bad:
            raise QueryError(f"{q['name']}: positions are {' '.join(POSITIONS)} (not {bad[0]})")
        q["distinct"] = bool(obj.get("distinct", False))
        q["leagues"] = [str(x).strip().upper() for x in _as_list(obj.get("leagues"))] or list(LEAGUES)
        q["pitchers"] = bool(obj.get("pitchers", False))
        q["order"] = str(obj.get("order") or "highest").strip().lower()
        if q["order"] not in ("highest", "lowest"):
            raise QueryError(f"{q['name']}: order must be highest or lowest")
        q["each"] = str(obj.get("each") or "").strip().lower() or None
        if q["each"] is not None and q["each"] not in EACH_CHOICES:
            raise QueryError(f"{q['name']}: each must be {' or '.join(EACH_CHOICES)}")
        if q["each"] == "primary pitch" and not q["pitchers"]:
            raise QueryError(f"{q['name']}: each primary pitch needs \"pitchers\": true")
        if q["source"] == "league-games" and (q["pitchers"] or q["each"]):
            raise QueryError(f"{q['name']}: pitchers and each are for a ranking of player seasons")
    q["where"] = [_condition(c, q["name"]) for c in _as_list(obj.get("where"))]
    q["postseason"] = bool(obj.get("postseason", False))
    q["running"] = bool(obj.get("running", False))
    q["chart"] = str(obj.get("chart") or "lines").strip().lower()
    if q["chart"] not in CHARTS:
        raise QueryError(f"{q['name']}: chart must be {' or '.join(CHARTS)}")
    q["x"] = obj.get("x") or X_CHOICES[q["source"]][0]
    if q["x"] not in X_CHOICES[q["source"]]:
        raise QueryError(f"{q['name']}: x for {q['source']} must be {' or '.join(X_CHOICES[q['source']])}")
    series = obj.get("series")
    if not isinstance(series, list) or not series:
        raise QueryError(f"{q['name']}: needs at least one series")
    q["series"] = []
    for i, s in enumerate(series, 1):
        if not isinstance(s, dict):
            raise QueryError(f"{q['name']}: series {i} must be an object")
        out = {"name": str(s.get("name", "")).strip() or f"Series {i}",
               "formula": str(s.get("formula", "")).strip()}
        try:
            _formula_variables(out["formula"])
        except formula.FormulaError as exc:
            raise QueryError(f"{q['name']}: series “{out['name']}”: {exc}") from exc
        try:
            out["panel"] = int(s.get("panel", 1))
        except (TypeError, ValueError):
            raise QueryError(f"{q['name']}: series “{out['name']}”: panel must be a number") from None
        if not 1 <= out["panel"] <= MAX_PANELS:
            raise QueryError(f"{q['name']}: panels are numbered 1 to {MAX_PANELS}")
        out["format"] = str(s.get("format", "auto"))
        if out["format"] not in FORMATS:
            raise QueryError(f"{q['name']}: format must be one of {', '.join(FORMATS)}")
        if "running" in s:
            out["running"] = bool(s["running"])
        if s.get("stack"):  # bars: series sharing a stack name are piled into one bar
            out["stack"] = str(s["stack"]).strip()
        src = s.get("source")
        if src is not None:
            if src not in SOURCES:
                raise QueryError(f"{q['name']}: series “{out['name']}”: unknown source {src!r}")
            if src != q["source"] and (src in RANKED or q["source"] in RANKED):
                raise QueryError(f"{q['name']}: series “{out['name']}”: a ranked list of MLB's "
                                 "seasons or games can't share an x axis with other data")
            if (src == "player-seasons") != (q["source"] == "player-seasons"):
                raise QueryError(f"{q['name']}: series “{out['name']}” can't put season-by-season "
                                 "and game-by-game data on one x axis")
            out["source"] = src
        if "subjects" in s:
            out["subjects"] = _subjects(s["subjects"], q["name"])
        if "season" in s:
            out["season"] = _season(s["season"], q["name"], needed=False,
                                    career=(src or q["source"]) == "player-games")
        if "postseason" in s:
            out["postseason"] = bool(s["postseason"])
        q["series"].append(out)
    for s in q["series"]:
        if q["x"] == "date" and _series_source(q, s) == "player-seasons":
            raise QueryError(f"{q['name']}: season-by-season data has no dates")
    q["panels"] = {}
    for key, spec in (obj.get("panels") or {}).items():
        try:
            n = int(key)
        except (TypeError, ValueError):
            raise QueryError(f"{q['name']}: panels are keyed by number") from None
        spec = spec if isinstance(spec, dict) else {}
        q["panels"][n] = {k: spec[k] for k in ("title", "min", "max") if spec.get(k) is not None}
    q["reference_lines"] = []
    for ref in obj.get("reference_lines") or []:
        try:
            q["reference_lines"].append({"panel": int(ref.get("panel", 1)), "value": float(ref["value"]),
                                         "label": str(ref.get("label", ""))})
        except (TypeError, ValueError, KeyError, AttributeError):
            raise QueryError(f"{q['name']}: a reference line needs a numeric value") from None
    return q


def _subjects(raw, name: str) -> list:
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise QueryError(f"{name}: needs at least one subject (a player or team)")
    out = []
    for s in raw:
        if isinstance(s, str) and s.strip():
            out.append(s.strip())
        elif isinstance(s, dict) and (s.get("id") or s.get("name")):
            out.append({k: s[k] for k in ("id", "name") if s.get(k)})
        else:
            raise QueryError(f"{name}: a subject is a name, or {{\"id\": ..., \"name\": ...}}")
    return out


def _season(raw, name: str, needed: bool, career: bool = False):
    if raw in (None, ""):
        if needed:
            raise QueryError(f"{name}: needs a season")
        return None
    if isinstance(raw, str) and raw.strip().casefold() == CAREER:
        if not career:
            raise QueryError(f"{name}: a whole career (season \"career\") is for player-games only")
        return CAREER
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise QueryError(f"{name}: season must be a year") from None


def _as_list(raw) -> list:
    if raw in (None, ""):
        return []
    return [raw] if isinstance(raw, str) else list(raw)


@functools.lru_cache(maxsize=1024)
def split_condition(text: str) -> tuple[str, str, str]:
    """``"{Pitching: H} = 0"`` -> ("{Pitching: H}", "=", "0").  Raises FormulaError."""
    outside = re.sub(r"\{[^{}]*\}", lambda m: " " * len(m.group()), text)  # names may hold < > =
    ops = list(_COMPARE_RE.finditer(outside))
    if len(ops) != 1:
        raise formula.FormulaError("a condition is two formulas joined by one of = != < <= > >=")
    m = ops[0]
    left, right = text[:m.start()].strip(), text[m.end():].strip()
    if not left or not right:
        raise formula.FormulaError("a condition needs something on both sides of " + m.group())
    return left, "=" if m.group() == "==" else m.group(), right


def _condition(raw, name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise QueryError(f"{name}: each “where” condition is a string such as \"{{Pitching: H}} = 0\"")
    try:
        left, op, right = split_condition(raw)
        _formula_variables(left)
        _formula_variables(right)
    except formula.FormulaError as exc:
        raise QueryError(f"{name}: condition “{raw.strip()}”: {exc}") from exc
    return f"{left} {op} {right}"


def condition_variables(conditions: list[str]) -> set:
    out = set()
    for c in conditions:
        left, _op, right = split_condition(c)
        out |= set(_formula_variables(left)) | set(_formula_variables(right))
    return out


def _holds(conditions: list[str], env: dict, soft: bool = False) -> bool:
    """Every condition true over ``env``.  A side that can't be worked out (a value missing)
    fails the condition -- unless ``soft``, when it's let through to be judged later."""
    for c in conditions:
        left, op, right = split_condition(c)
        a, b = _evaluate_formula(left, env), _evaluate_formula(right, env)
        if a is None or b is None:
            if soft:
                continue
            return False
        if not _COMPARE[op](a, b):
            return False
    return True


def query_to_json(q: dict) -> dict:
    """A normalised query back to its file form (defaults left out)."""
    out = {"name": q["name"]}
    for key in ("title", "description"):
        if q.get(key):
            out[key] = q[key]
    out["source"] = q["source"]
    if q["subjects"]:
        out["subjects"] = q["subjects"]
    if q.get("season") is not None:
        out["season"] = q["season"]
    if q["source"] in RANKED:
        if q["season_to"] != q["season"]:
            out["season_to"] = q["season_to"]
        out["rank_by"], out["top"] = q["rank_by"], q["top"]
        if q["positions"] != POSITIONS and q["source"] == "league-seasons":
            out["positions"] = q["positions"]
        if q["distinct"]:
            out["distinct"] = True
        if q["leagues"] != LEAGUES and q["source"] == "league-seasons":
            out["leagues"] = q["leagues"]
        if q.get("pitchers"):
            out["pitchers"] = True
        if q.get("order", "highest") != "highest":
            out["order"] = q["order"]
        if q.get("each"):
            out["each"] = q["each"]
    if q.get("postseason"):
        out["postseason"] = True
    if q.get("where"):
        out["where"] = q["where"]
    out["x"] = q["x"]
    if q.get("chart", "lines") != "lines":
        out["chart"] = q["chart"]
    if q.get("running"):
        out["running"] = True
    out["series"] = []
    for s in q["series"]:
        item = {"name": s["name"], "formula": s["formula"], "panel": s["panel"]}
        if s.get("format", "auto") != "auto":
            item["format"] = s["format"]
        for key in ("running", "stack", "source", "subjects", "season", "postseason"):
            if key in s:
                item[key] = s[key]
        out["series"].append(item)
    if q.get("panels"):
        out["panels"] = {str(n): spec for n, spec in sorted(q["panels"].items()) if spec}
    if q.get("reference_lines"):
        out["reference_lines"] = q["reference_lines"]
    return out


def read_queries(path: str) -> list[dict]:
    """The queries in one JSON file (an object or a list of them), each checked."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    items = data if isinstance(data, list) else [data]
    return [parse_query(item) for item in items]


# --------------------------------------------------------------------------- #
#  Running a query                                                            #
# --------------------------------------------------------------------------- #
def format_value(value, fmt: str = "auto") -> str:
    if value is None:
        return ""
    if fmt == "int":
        return f"{value:,.0f}"
    if fmt == "rate":
        text = f"{value:.3f}"
        return text[1:] if text.startswith("0.") else text.replace("-0.", "-.")
    if fmt in ("1", "2", "3"):
        return f"{value:,.{fmt}f}"
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.3f}".rstrip("0").rstrip(".")


def _stat_values(row: dict, group: str) -> dict:
    out = {}
    for name, g, key, _how in STAT_VARIABLES:
        if g != group or key not in row:
            continue
        if key in _INNINGS_KEYS:
            out[name] = mlbstats.innings_to_outs(row.get(key) or 0) / 3
        else:
            out[name] = mlbstats.as_number(row.get(key))
    return out


def _merge(into: dict, values: dict):
    """Fold one row's values into a game's or season's (several fielding positions)."""
    for name, v in values.items():
        if v is None:
            continue
        if name in _SUMMED and into.get(name) is not None:
            into[name] += v
        elif into.get(name) is None:
            into[name] = v


def _needed(q: dict, custom: dict) -> dict:
    """source -> the variable names its series use (custom stats expanded)."""
    need: dict[str, set] = {}
    checks = [(s["name"], _series_source(q, s), set(_formula_variables(s["formula"])))
              for s in q["series"]]
    if q.get("where"):
        checks.append(("where", q["source"], condition_variables(q["where"])))
    if q.get("rank_by"):
        checks.append(("rank_by", q["source"], set(_formula_variables(q["rank_by"]))))
    for label, source, start in checks:
        names = set()
        todo = list(start)
        while todo:
            v = todo.pop()
            if v in names:
                continue
            names.add(v)
            if v.startswith("Custom: "):
                cname = v[len("Custom: "):]
                if cname not in custom:
                    raise QueryError(f"{_what(label)} uses {{{v}}}, but there's no "
                                     "Player-level custom stat by that name (Custom tab)")
                todo.extend(_formula_variables(custom[cname]))
        known = set(variables_for(source, q.get("pitchers", False))) | {f"Custom: {c}" for c in custom}
        unknown = sorted(names - known)
        if unknown:
            raise QueryError(f"{_what(label)}: unknown variable {{{unknown[0]}}} for "
                             f"{SOURCES[source].split(' (')[0].lower()} data")
        need.setdefault(source, set()).update(names)
    return need


def _what(label: str) -> str:
    return {"where": "the “where” conditions", "rank_by": "rank_by"}.get(label, f"series “{label}”")


def _groups(names: set) -> set:
    by_name = {name: g for name, g, _k, _h in STAT_VARIABLES}
    return {by_name[n] for n in names if n in by_name}


class _Fetcher:
    """Fetches (once per run) what the series need: subjects resolved to ids, and their
    records -- one per game or season, each ``{"x_date", "game_number", "season", "age",
    "detail", "values"}``."""

    def __init__(self, season_now: int, live_ttl: float, notes: list):
        self.now, self.live, self.notes = season_now, live_ttl, notes
        self._cache: dict = {}

    def _ttl(self, season):
        return self.live if season is None or season >= self.now else None

    def _once(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # -- subjects ----------------------------------------------------------
    def players(self, subject, season) -> tuple[int, str]:
        if isinstance(subject, dict) and subject.get("id"):
            return int(subject["id"]), subject.get("name") or str(subject["id"])
        name = subject["name"] if isinstance(subject, dict) else subject
        people = self._once(("search", name.casefold()), lambda: mlbstats.search_people(name))
        if not people:
            raise QueryError(f"no player found called “{name}”")
        exact = [p for p in people if p["name"].casefold() == name.casefold()] or people
        if season is not None:
            played = [p for p in exact if p["debut"] and int(p["debut"][:4]) <= season]
            exact = played or exact
        pick = exact[0]
        if len(exact) > 1:
            self.notes.append(f"{len(exact)} players match “{name}” — used {pick['name']} "
                              f"(debut {(pick['debut'] or '?')[:4]}, id {pick['id']}); give "
                              "{\"id\": ...} to choose another.")
        return pick["id"], pick["name"]

    def teams(self, subject, season) -> list[tuple[int, str]]:
        tmap = self.team_map(season)
        if isinstance(subject, dict) and subject.get("id"):
            rec = tmap.get(int(subject["id"]))
            return [(int(subject["id"]), subject.get("name") or (rec or {}).get("name") or str(subject["id"]))]
        name = _expand_league(subject["name"] if isinstance(subject, dict) else subject).casefold()
        for field in ("league", "division"):  # a whole league or division (divisions from 1969)
            group = sorted((t for t in tmap.values() if (t.get(field) or "").casefold() == name),
                           key=lambda t: t["name"])
            if group:
                return [(t["id"], t["name"]) for t in group]
        for test in (lambda t: t["name"].casefold() == name,
                     lambda t: name in t["name"].casefold() or name == (t.get("abbrev") or "").casefold()):
            hits = [t for t in tmap.values() if test(t)]
            if hits:
                return [(hits[0]["id"], hits[0]["name"])]
        raise QueryError(f"no team, league or division called “{subject}” in {season}"
                         + (" (divisions began in 1969)" if season < 1969 else ""))

    def team_map(self, season):
        return self._once(("teams", season), lambda: mlbstats.get_team_map(season, self._ttl(season)))

    def schedule(self, team_id, season):
        return self._once(("sched", team_id, season),
                          lambda: mlbstats.get_team_games(team_id, season, self._ttl(season)))

    # -- records -----------------------------------------------------------
    def birth(self, pid):
        """His birth date, or None if the source doesn't know it."""
        def get():
            text = mlbstats.get_person(pid).get("birth_date") or ""
            try:
                return _dt.date.fromisoformat(text[:10])
            except ValueError:
                return None
        return self._once(("birth", pid), get)

    def career_games(self, pid, pname, post, names) -> list[dict]:
        """Every game of his career that has a game log (1901 on), all seasons together."""
        groups = sorted(_groups(names) or {"hitting", "pitching"})
        seasons: set = set()
        for group in {g for g in groups if g in ("hitting", "pitching")} or {"hitting", "pitching"}:
            data = self._once(("pstats", pid, group), lambda g=group: mlbstats.get_person_stats(pid, g, self.live))
            seasons |= {int(r["_season"]) for r in data["season"]}
        early = sorted(y for y in seasons if y < mlbstats.DAILY_FROM_SEASON)
        if early:
            self.notes.append(f"{pname}: no game logs before {mlbstats.DAILY_FROM_SEASON}, so his "
                              f"{early[0]}–{early[-1]} games aren't counted." if len(early) > 1 else
                              f"{pname}: no game logs before {mlbstats.DAILY_FROM_SEASON}, so his "
                              f"{early[0]} games aren't counted.")
        years = sorted(y for y in seasons if y >= mlbstats.DAILY_FROM_SEASON)
        with ThreadPoolExecutor(max_workers=6) as pool:
            per_year = list(pool.map(lambda y: self.player_games(pid, y, post, names), years))
        return [rec for chunk in per_year for rec in chunk]

    def add_team_values(self, records: list[dict]):
        """Fill in each game's ``Team: ...`` values (his team's result) from the schedules."""
        by_pk: dict = {}
        for tid, season in {(r["team_id"], r["season"]) for r in records if r["team_id"] and r["season"]}:
            try:
                by_pk.update((g["pk"], g) for g in self.schedule(tid, season))
            except mlbstats.MLBStatsError:
                pass
        for rec in records:
            g = by_pk.get(rec["pk"])
            rec["values"].update(_team_values(g))
            if g and g.get("result"):
                rec["detail"] += f" · {g['result']} {g['runs']}–{g['opp_runs']}"

    def player_games(self, pid, season, post, names) -> list[dict]:
        groups = _groups(names) or {"hitting", "pitching"}
        games: dict = {}
        abbrevs = {tid: t.get("abbrev") or t["name"] for tid, t in self.team_map(season).items()}
        for group in sorted(groups):
            rows = self._once(("plog", pid, group, season, post),
                              lambda g=group: mlbstats.get_game_log(pid, g, season, post, self._ttl(season)))
            for r in rows:
                rec = games.setdefault(r["_pk"], self._game_record(r, abbrevs))
                _merge(rec["values"], _stat_values(r, group))
        return sorted(games.values(), key=lambda r: (r["date"], r["game_number"]))

    def team_games(self, tid, season, post, names) -> list[dict]:
        sched = [g for g in self.schedule(tid, season)
                 if g["state"] == "final" and (post or g["type"] == "R")]
        abbrev_of = {rec["name"]: rec.get("abbrev") or rec["name"] for rec in self.team_map(season).values()}
        games = {}
        for g in sched:
            games[g["pk"]] = {
                "date": g["date"], "game_number": g["game_number"], "type": g["type"], "team_id": tid,
                "season": season, "age": None,
                "detail": ("vs " if g["home"] else "@ ") + abbrev_of.get(g["opponent"], g["opponent"])
                          + (f" · {g['result']} {g['runs']}–{g['opp_runs']}" if g["result"] else ""),
                "values": _team_values(g)}
        for group in sorted(_groups(names)):
            rows = self._once(("tlog", tid, group, season, post),
                              lambda g=group: mlbstats.get_team_game_log(tid, g, season, post, self._ttl(season)))
            for r in rows:
                if r["_pk"] in games:
                    _merge(games[r["_pk"]]["values"], _stat_values(r, group))
        return sorted(games.values(), key=lambda r: (r["date"], r["game_number"]))

    def player_seasons(self, pid, names) -> list[dict]:
        groups = _groups(names) or {"hitting"}
        groups |= {"hitting"} if not groups & {"hitting", "pitching"} else set()  # for the age
        seasons: dict = {}
        for group in sorted(groups):
            data = self._once(("pstats", pid, group),
                              lambda g=group: mlbstats.get_person_stats(pid, g, self.live))
            by_season: dict = {}
            for r in data["season"]:
                by_season.setdefault(r["_season"], []).append(r)
            for season, rows in by_season.items():
                # a traded player's season: the all-teams line, not each team's
                use = [r for r in rows if r["_kind"] == "total"] or rows
                rec = seasons.setdefault(season, {"date": "", "game_number": 1, "type": "R", "team_id": None,
                                                  "season": int(season), "age": None,
                                                  "detail": "", "values": {}})
                for r in use:
                    if rec["age"] is None and mlbstats.as_number(r.get("age")) is not None:
                        rec["age"] = int(mlbstats.as_number(r["age"]))
                    _merge(rec["values"], _stat_values(r, group))
                teams = sorted({r["_team"] for r in rows if r["_kind"] == "season" and r["_team"]})
                if teams and not rec["detail"]:
                    rec["detail"] = f"{season}, " + " / ".join(teams)
        for season, rec in seasons.items():
            rec["detail"] = rec["detail"] or season
            rec["values"]["Player: Age"] = rec["age"]
            rec["values"]["Player: Season"] = rec["season"]
        return [seasons[s] for s in sorted(seasons)]

    def league_season(self, season: int, positions: list, leagues: list) -> list[dict]:
        """Every player's season in ``leagues``, one record each: his batting line, his
        fielding at his main position (only those in ``positions``), and the leagues' figures."""
        ttl = self._ttl(season)
        keep = lambda rows: [r for r in rows if (r.get("_league") or "").upper() in leagues]
        batting = keep(mlbstats.get_league_view_rows(season, "Batting", ttl))
        fielding = keep(mlbstats.get_league_view_rows(season, "Fielding", ttl))
        team_g = mlbstats.get_team_games_played(season, ttl)
        ours = [team_g[t] for t in {r.get("_team_id") for r in batting if r.get("_num_teams", 1) == 1} if t in team_g]
        avg_g = sum(ours) / len(ours) if ours else None  # the leagues' own clubs: a traded player's
        tot = lambda key: sum(mlbstats.as_number(r.get(key)) or 0 for r in batting)
        ab, h, bb, hbp, sf = (tot(k) for k in ("atBats", "hits", "baseOnBalls", "hitByPitch", "sacFlies"))
        league = {"MLB: AVG": h / ab if ab else None,
                  "MLB: OBP": (h + bb + hbp) / (ab + bb + hbp + sf) if ab + bb + hbp + sf else None,
                  "MLB: SLG": tot("totalBases") / ab if ab else None}
        league.update((name, tot(key)) for name, key in _LEAGUE_TOTALS)
        rf = mlbstats.aggregate_league_positions(fielding)
        main: dict = {}  # player -> his row at the position he played most
        for r in fielding:
            if r.get("_pos") not in rf or r.get("_id") is None:
                continue
            weight = (mlbstats.innings_to_outs(r.get("innings") or 0), mlbstats.as_number(r.get("gamesPlayed")) or 0)
            if r["_id"] not in main or weight > main[r["_id"]][0]:
                main[r["_id"]] = (weight, r)
        out = []
        for b in batting:
            got = main.get(b.get("_id"))
            if got is None or got[1]["_pos"] not in positions:
                continue
            f = got[1]
            figs = mlbstats.fielding_row_figures(f)
            values = _stat_values(b, "hitting")
            values.update(_stat_values(f, "fielding"))
            values.update(league)
            values.update({"MLB: RF/9": rf[f["_pos"]]["rf_9"], "MLB: RF/G": rf[f["_pos"]]["rf_game"],
                           "Fielding: RF/9": figs["rf_9"], "Fielding: RF/G": figs["rf_game"],
                           "Player: Season": season,
                           "Team: G": team_g.get(b.get("_team_id")) if b.get("_num_teams", 1) == 1 else avg_g})
            out.append({"season": season, "pid": b["_id"], "name": b["_name"], "pos": f["_pos"],
                        "team_id": b.get("_team_id"), "teams": b.get("_num_teams", 1), "values": values})
        return out

    def league_pitching_season(self, season: int, leagues: list) -> list[dict]:
        """Every pitcher's season in ``leagues``, one record each: his pitching line, his team's
        games, and MLB's runs and earned runs per nine innings."""
        ttl = self._ttl(season)
        rows = [r for r in mlbstats.get_league_view_rows(season, "Pitching", ttl)
                if (r.get("_league") or "").upper() in leagues and r.get("_id") is not None]
        team_g = mlbstats.get_team_games_played(season, ttl)
        ours = [team_g[t] for t in {r.get("_team_id") for r in rows if r.get("_num_teams", 1) == 1} if t in team_g]
        avg_g = sum(ours) / len(ours) if ours else None
        tot = lambda key: sum(mlbstats.as_number(r.get(key)) or 0 for r in rows)
        outs = tot("outs")
        league = {"MLB: R/9": 27 * tot("runs") / outs if outs else None,
                  "MLB: ERA": 27 * tot("earnedRuns") / outs if outs else None}
        out = []
        for r in rows:
            values = _stat_values(r, "pitching")
            values.update(league)
            values.update({"Player: Season": season,
                           "Team: G": team_g.get(r.get("_team_id")) if r.get("_num_teams", 1) == 1 else avg_g})
            out.append({"season": season, "pid": r["_id"], "name": r["_name"], "pos": "P",
                        "team_id": r.get("_team_id"), "teams": r.get("_num_teams", 1), "values": values})
        return out

    def add_primary_pitches(self, records: list[dict]) -> list[dict]:
        """Each record's most-thrown pitch (``rec["pitch"]``, and the Pitch: variables) -- the
        ones with no pitch-tracking data are dropped, with a note."""
        def arsenal(rec):
            return mlbstats.get_pitch_arsenal(rec["pid"], rec["season"], self._ttl(rec["season"]))
        with ThreadPoolExecutor(max_workers=8) as pool:
            mixes = list(pool.map(arsenal, records))
        kept, missing = [], 0
        for rec, mix in zip(records, mixes):
            if not mix:
                missing += 1
                continue
            rec["pitch"] = mix[0]
            rec["values"].update({"Pitch: Primary %": mix[0]["share"], "Pitch: Primary mph": mix[0]["speed"]})
            kept.append(rec)
        if missing:
            self.notes.append(f"{missing} pitcher-season{'s' if missing != 1 else ''} had no pitch-tracking "
                              f"data (it begins in 2008) and {'were' if missing != 1 else 'was'} left out.")
        return kept

    def league_ranking(self, q: dict, custom: dict) -> list[dict]:
        """The ``top`` player-seasons of ``q`` by its ``rank_by``, best first (for each primary
        pitch, with ``each``)."""
        years = list(range(q["season"], q["season_to"] + 1))
        pitchers = q.get("pitchers", False)
        load = ((lambda y: self.league_pitching_season(y, q["leagues"])) if pitchers else
                (lambda y: self.league_season(y, q["positions"], q["leagues"])))
        with ThreadPoolExecutor(max_workers=6) as pool:
            per_year = list(pool.map(load, years))
        # the conditions on the pitch mix wait until it's fetched -- for the seasons that pass the rest
        uses_pitch = lambda text: any(v in PITCH_VARIABLES for v in condition_variables([text]))
        early = [c for c in q["where"] if not uses_pitch(c)]
        late = [c for c in q["where"] if uses_pitch(c)]
        records = [rec for rec in (r for chunk in per_year for r in chunk)
                   if not early or _holds(early, _env("league-seasons", rec["values"], custom))]
        need_pitch = pitchers and (q.get("each") or late or any(
            v in PITCH_VARIABLES for v in _formula_variables(q["rank_by"])) or any(
            v in PITCH_VARIABLES for s in q["series"] for v in _formula_variables(s["formula"])))
        if need_pitch:
            records = self.add_primary_pitches(records)
        scored = []
        for rec in records:
            env = _env("league-seasons", rec["values"], custom)
            if late and not _holds(late, env):
                continue
            score = _evaluate_formula(q["rank_by"], env)
            if score is not None:
                scored.append((score, rec))
        sign = 1 if q.get("order", "highest") == "lowest" else -1
        scored.sort(key=lambda sr: (sign * sr[0], sr[1]["season"], sr[1]["name"]))
        what = "pitcher-season" if pitchers else "player-season"
        if not scored:
            self.notes.append(f"No {what} meets the conditions.")
        groups: dict = {}   # group name -> its scored records, best first
        for score, rec in scored:
            key = rec["pitch"]["name"] if q.get("each") == "primary pitch" else None
            groups.setdefault(key, []).append(rec)
        # the most common group first (the pitch the most qualifiers throw most)
        order = sorted(groups, key=lambda k: (-len(groups[k]), k or ""))
        top = []
        for key in order:
            chosen, seen = [], set()
            for rec in groups[key]:
                if q["distinct"] and rec["pid"] in seen:
                    continue
                seen.add(rec["pid"])
                chosen.append(dict(rec, group=key, group_size=len(groups[key])))
                if len(chosen) == q["top"]:
                    break
            top += chosen
        for rank, rec in enumerate(top, 1):
            tmap = self.team_map(rec["season"])
            team = (f"{rec['teams']} teams" if rec["teams"] > 1 else
                    (tmap.get(rec["team_id"]) or {}).get("name", ""))
            where = rec["pos"]
            if rec.get("pitch"):
                where = f"{rec['pitch']['name']} {rec['pitch']['share']:.0%}"
                if rec.get("group"):
                    where += f" · best of {rec['group_size']} {what}s with it as the main pitch"
            short = f"{_surname(rec['name'])} {rec['season']}"
            rec.update(rank=rank, date="", game_number=1, age=None,
                       detail=f"{rec['season']} {team} · {where}".replace("  ", " "),
                       short=f"{rec['group']}\n{short}" if rec.get("group") else short)
        return top

    def league_games(self, season: int, post: bool) -> list[dict]:
        """Every team's game in ``season`` with a line score, one record each."""
        games, unscored = mlbstats.get_season_linescores(season, post, self._ttl(season))
        tmap = self.team_map(season)
        major = lambda tid: (tmap.get(tid) or {}).get("league") in ("American League", "National League")
        missing = [all(major(t) for t in pair) for pair in unscored]  # True: an AL/NL game
        out = []
        for g in games:
            inn, opp = g["innings"], g["opp_innings"]
            n, scoreless = len(inn), inn.count(0)
            result = g["result"]
            values = dict(zip(INNING_VARIABLES, inn[:9] + [None] * (9 - n) if n < 9 else inn[:9]))
            values.update({"Team: W": int(result == "W"), "Team: L": int(result == "L"),
                           "Team: T": int(result == "T"), "Team: Runs": g["runs"],
                           "Team: Runs allowed": g["opp_runs"], "Team: Home": int(g["home"]),
                           "Innings: Batted": n, "Innings: Scored": n - scoreless, "Innings: Scoreless": scoreless,
                           "Innings: Most runs": max(inn) if inn else None,
                           "Innings: Fewest runs": min(inn) if inn else None,
                           "Innings: Opponent batted": len(opp), "Innings: Opponent scored": len(opp) - opp.count(0),
                           "Innings: Game": g["length"], "Game: Season": season,
                           "Game: Date": int(g["date"].replace("-", "")) if g["date"] else None,
                           "Game: Postseason": int(g["type"] != "R"),
                           "Inning: Extra": sum(inn[9:]) if n > 9 else None})
            g["season"], g["values"] = season, values
            out.append(g)
        return out, missing

    def league_game_ranking(self, q: dict, custom: dict) -> list[dict]:
        """The ``top`` team-games of ``q`` by its ``rank_by``, best first (ties in date order)."""
        years = list(range(q["season"], q["season_to"] + 1))
        with ThreadPoolExecutor(max_workers=6) as pool:
            per_year = list(pool.map(lambda y: self.league_games(y, q["postseason"]), years))
        major = sum(sum(m) for _games, m in per_year)
        negro = sum(len(m) for _games, m in per_year) - major
        span = "these seasons" if len(years) > 1 else str(years[0])
        if major:
            self.notes.append(f"{major:,} AL/NL game{'s' if major != 1 else ''} in {span} "
                              f"{'have' if major != 1 else 'has'} no inning-by-inning line score in MLB's "
                              "data and couldn't be judged.")
        if negro:
            self.notes.append(f"The {negro:,} Negro League game{'s' if negro != 1 else ''} in {span} "
                              "have no line scores in MLB's data, so only AL and NL games are judged.")
        scored = []
        for rec in (r for games, _m in per_year for r in games):
            env = _env("league-games", rec["values"], custom)
            if q["where"] and not _holds(q["where"], env):
                continue
            score = _evaluate_formula(q["rank_by"], env)
            if score is not None:
                scored.append((score, rec))
        sign = 1 if q.get("order", "highest") == "lowest" else -1
        scored.sort(key=lambda sr: (sign * sr[0], sr[1]["date"], sr[1]["game_number"], sr[1]["team"]))
        if not scored:
            self.notes.append("No team-game meets the conditions.")
        top, seen = [], set()
        for _score, rec in scored:
            if q["distinct"] and rec["team_id"] in seen:
                continue
            seen.add(rec["team_id"])
            top.append(rec)
            if len(top) == q["top"]:
                break
        for rank, rec in enumerate(top, 1):
            tmap = self.team_map(rec["season"])
            abbrev = lambda tid, name: (tmap.get(tid) or {}).get("abbrev") or name
            day = _short_date(rec["date"]) + (f" (game {rec['game_number']})" if rec["game_number"] > 1 else "")
            line = " ".join(str(r) for r in rec["innings"])
            post = f" · {mlbstats.GAME_TYPE_LABELS.get(rec['type'], 'postseason')}" if rec["type"] != "R" else ""
            rec.update(rank=rank, name=rec["team"], age=None,
                       label=f"{rec['team']}, {day}",
                       detail=f"{day} · {rec['team']} {'vs' if rec['home'] else '@'} "
                              f"{abbrev(rec['opp_id'], rec['opponent'])} · {rec['result']} "
                              f"{rec['runs']}–{rec['opp_runs']}{post} · by inning: {line}",
                       # "game 2" beside the team, not the date, so a doubleheader's name is no wider
                       short=f"{abbrev(rec['team_id'], rec['team'])}"
                             f"{', game ' + str(rec['game_number']) if rec['game_number'] > 1 else ''}"
                             f"\n{_short_date(rec['date'])}")
        return top

    @staticmethod
    def _game_record(r: dict, abbrevs: dict) -> dict:
        opp = abbrevs.get(r["_opp_id"]) or r["_opp"]
        return {"pk": r["_pk"], "date": r["_date"], "game_number": r["_game_number"], "type": r["_type"],
                "team_id": r["_team_id"], "season": int(r["_date"][:4]) if r["_date"] else None, "age": None,
                "detail": ("vs " if r["_home"] else "@ ") + opp, "values": {}}


def _surname(name: str) -> str:
    words = [w for w in name.split() if w.rstrip(".").casefold() not in ("jr", "sr", "ii", "iii", "iv")]
    return words[-1] if words else name


def _env(source: str, vals: dict, custom: dict) -> dict:
    """What a formula sees for one game or season: every variable (None if missing),
    the Player-level custom stats worked out over them."""
    env = dict(_blank_env(source))
    env.update(vals)
    for cname, cformula in custom.items():
        try:
            env[f"Custom: {cname}"] = _evaluate_formula(cformula, env)
        except formula.FormulaError:
            env[f"Custom: {cname}"] = None
    return env


@functools.lru_cache(maxsize=None)
def _blank_env(source: str) -> dict:
    """Every variable ``source`` has, each None (copied for each game or season)."""
    env = {name: None for name in variables_for(source)}
    if source == "league-seasons":
        env.update((name, None) for name in variables_for(source, pitchers=True))
    return env


def _date_players_games(records: list[dict], birth, with_date: bool):
    """His age on each game's day (``birth`` known), and the date in front of the detail."""
    for rec in records:
        if birth and rec["date"]:
            day = _dt.date.fromisoformat(rec["date"])
            years = day.year - birth.year - ((day.month, day.day) < (birth.month, birth.day))
            last = birth.replace(year=birth.year + years) if not (birth.month == 2 and birth.day == 29) \
                else _dt.date(birth.year + years, 3, 1)
            rec["age"] = (years, (day - last).days)
        if with_date and rec["date"]:
            rec["detail"] = f"{_short_date(rec['date'])} {rec['detail']}"


def _expand_league(name: str) -> str:
    """"AL East" -> "American League East", "NL" -> "National League"; anything else as is."""
    words = name.split()
    short = {"al": "American League", "nl": "National League"}
    if words and words[0].casefold() in short:
        return " ".join([short[words[0].casefold()]] + words[1:])
    return name


def _team_values(g: dict | None) -> dict:
    if not g or not g.get("result"):
        return {name: None for name in TEAM_VARIABLES}
    return {"Team: G": 1, "Team: W": int(g["result"] == "W"), "Team: L": int(g["result"] == "L"),
            "Team: T": int(g["result"] == "T"), "Team: Runs": g["runs"], "Team: Runs allowed": g["opp_runs"]}


def _x_of(q: dict, rec: dict, index: int):
    """-> (x as a number, its label) for one record."""
    if q["x"] == "rank":
        if rec.get("label"):
            return float(rec["rank"]), f"{rec['rank']}. {rec['label']}"
        group = f"{rec['group']}: " if rec.get("group") else ""
        return float(rec["rank"]), f"{rec['rank']}. {group}{rec['name']}, {rec['season']}"
    if q["x"] == "game":
        return float(index), f"Game {index}" + (f" ({_short_date(rec['date'])})" if rec["date"] else "")
    if q["x"] == "date":
        day = _dt.date.fromisoformat(rec["date"])
        return day.toordinal() + 0.4 * (rec["game_number"] - 1), \
            _short_date(rec["date"]) + (f" (game {rec['game_number']})" if rec["game_number"] != 1 else "")
    if q["x"] == "age":
        if rec["age"] is None:
            return None, ""
        if isinstance(rec["age"], tuple):  # a game: (years, days since his birthday)
            years, days = rec["age"]
            return years + days / 365.25 + 0.001 * (rec["game_number"] - 1), f"Age {years}, {days} d"
        return float(rec["age"]), f"Age {rec['age']}"
    return float(rec["season"]), str(rec["season"])


def _short_date(iso: str) -> str:
    day = _dt.date.fromisoformat(iso)
    return f"{day.strftime('%b')} {day.day}, {day.year}"


def run_query(q: dict, custom_stats: list[tuple[str, str]] = (), season_now: int | None = None,
              live_ttl: float = 600) -> dict:
    """Fetch and compute a query -> ``{"title", "x", "x_kind", "series", "panels", "notes"}``.
    Each series item is one subject's line: ``{"label", "subject", "name", "panel", "format",
    "key", "points": [{"x", "xlabel", "y", "detail"}]}``; ``key`` is what the graph colours
    by (the subject when there are several, else the series).  Raises QueryError, or
    mlbstats.MLBStatsError if the data can't be fetched."""
    custom = dict(custom_stats)
    need = _needed(q, custom)
    used = set().union(*need.values())  # work out only the custom stats the query uses
    custom = {name: f for name, f in custom.items() if f"Custom: {name}" in used}
    now = season_now or mlbstats.current_season()
    notes: list[str] = []
    fetch = _Fetcher(now, live_ttl, notes)
    out_series = []
    for s in q["series"]:
        source = _series_source(q, s)
        subjects = s.get("subjects", q["subjects"])
        season = s.get("season", q["season"])
        post = s.get("postseason", q["postseason"])
        running = s.get("running", q["running"]) or (q["chart"] == "bars" and source not in RANKED)  # a bar is the total
        names = need[source]
        career = season == CAREER
        if source in ("player-games", "team-games") and not career and season is not None \
                and season < mlbstats.DAILY_FROM_SEASON:  # (the ranked source reads season lines, not game logs)
            raise QueryError(f"there are no game logs before {mlbstats.DAILY_FROM_SEASON} (asked for {season})")
        lines = []  # (subject label, records)
        if source == "league-seasons":
            ranking = fetch._once(("ranking",), lambda: fetch.league_ranking(q, custom))
            lines.append(("MLB", [dict(r) for r in ranking]))
        if source == "league-games":
            ranking = fetch._once(("ranking",), lambda: fetch.league_game_ranking(q, custom))
            lines.append(("MLB", [dict(r) for r in ranking]))
        for subject in subjects:
            if source == "team-games":
                for tid, tname in fetch.teams(subject, season):
                    lines.append((tname, fetch.team_games(tid, season, post, names)))
            else:
                pid, pname = fetch.players(subject, None if career else season)
                if source == "player-games":
                    records = (fetch.career_games(pid, pname, post, names) if career
                               else fetch.player_games(pid, season, post, names))
                    _date_players_games(records, fetch.birth(pid) if q["x"] == "age" else None,
                                        with_date=career or q["x"] == "age")
                    lines.append((pname, records))
                else:
                    lines.append((pname, fetch.player_seasons(pid, names)))
        where = q["where"] if source == q["source"] and source not in RANKED else []  # (ranked already)
        team_vars = source == "player-games" and any(n.startswith("Team: ") for n in names)
        for subject_label, records in lines:
            if where:  # the stat conditions first, so only the games kept need their schedules
                records = [r for r in records if _holds(where, _env(source, r["values"], custom), soft=team_vars)]
            if team_vars:
                fetch.add_team_values(records)
            if where:
                records = [r for r in records if _holds(where, _env(source, r["values"], custom))]
            if not records:
                span = ("" if source in ("player-seasons", "league-games") else
                        " in his career" if career else f" in {season}")
                notes.append(f"{subject_label}: no {'games' if source != 'player-seasons' else 'seasons'}"
                             f"{' that meet the conditions' if where else ''}{span}.")
            points = []
            acc: dict = {}
            for i, rec in enumerate(records, 1):
                vals = rec["values"]
                if running:
                    for name, v in vals.items():
                        if name in _SUMMED:
                            if v is not None:
                                acc[name] = (acc.get(name) or 0) + v
                            else:
                                acc.setdefault(name, None)
                        else:
                            acc[name] = v
                    vals = acc
                y = _evaluate_formula(s["formula"], _env(source, vals, custom))
                x, xlabel = _x_of(q, rec, i)
                if x is None:
                    continue
                points.append({"x": x, "xlabel": xlabel, "y": y, "detail": rec["detail"]})
            label = (s["name"] if source in RANKED else
                     subject_label if len(q["series"]) == 1 else f"{subject_label} — {s['name']}")
            out_series.append({"label": label, "stack": s.get("stack"),
                               "subject": subject_label, "name": s["name"], "panel": s["panel"],
                               "format": s["format"], "points": points})
    subjects = list(dict.fromkeys(item["subject"] for item in out_series))
    for item in out_series:
        item["key"] = item["subject"] if len(subjects) > 1 else item["name"]
    ticks = []
    if q["chart"] == "bars" and q["source"] not in RANKED:  # each line becomes its last value, at its subject's place
        for item in out_series:
            last = next((pt for pt in reversed(item["points"]) if pt["y"] is not None), None)
            place = subjects.index(item["subject"]) + 1
            item["key"] = item["name"]  # coloured by series: the subjects are named on the x axis
            item["label"] = item["name"]
            item["points"] = [] if last is None else [
                {"x": float(place), "xlabel": item["subject"], "y": last["y"], "detail": ""}]
        ticks = [(float(i), _bar_name(name)) for i, name in enumerate(subjects, 1)]
    panels = []
    for n in sorted({item["panel"] for item in out_series}):
        members = [item for item in out_series if item["panel"] == n]
        spec = q["panels"].get(n, {})
        names = list(dict.fromkeys(item["name"] for item in members))
        panels.append({"n": n, "title": spec.get("title") or " / ".join(names),
                       "min": spec.get("min"), "max": spec.get("max"),
                       "format": members[0]["format"],
                       "refs": [(r["value"], r["label"]) for r in q["reference_lines"] if r["panel"] == n]})
    if q["source"] in RANKED:  # the graph names each rank on its x axis
        ticks = [(float(r["rank"]), r["short"]) for r in fetch._cache.get(("ranking",), [])]
    bars = q["chart"] == "bars" and q["source"] not in RANKED  # bars by subject: the x axis just names them
    return {"title": q["title"] or q["name"], "x": q["x"], "x_label": "" if bars else X_LABELS[q["x"]],
            "x_ticks": ticks, "x_kind": "date" if q["x"] == "date" and not bars else "number", "chart": q["chart"],
            "subject_kind": "Team" if q["source"] in ("team-games", "league-games") else "Player",
            "series": out_series, "panels": panels, "notes": list(dict.fromkeys(notes))}


def _bar_name(subject: str) -> str:
    """A subject's name under its bars: a player's on two lines (first names over the surname),
    so ten of them fit side by side."""
    first, _, last = subject.rpartition(" ")
    return f"{first}\n{last}" if first else subject


def result_table(result: dict) -> tuple[list[str], list[list]]:
    """A run query as a table: one row per x value, one column per series line, each cell
    ``(text, detail)``.  -> (headers, rows); the first cell of a row is the x label.  Drawn as
    bars, it's one row per subject and one column per series."""
    series = result["series"]
    if result.get("chart") == "bars" and result["x"] != "rank":
        names = list(dict.fromkeys(item["name"] for item in series))
        subjects = list(dict.fromkeys(item["subject"] for item in series))
        cell = {(item["subject"], item["name"]): item for item in series}
        rows = []
        for subject in subjects:
            row = [(subject, "")]
            for name in names:
                item = cell.get((subject, name))
                pt = item["points"][0] if item and item["points"] else None
                row.append(("", "") if pt is None else (format_value(pt["y"], item["format"]), ""))
            rows.append(row)
        return [result.get("subject_kind", "")] + names, rows
    xs: dict = {}
    for item in series:
        for p in item["points"]:
            xs.setdefault(p["x"], p["xlabel"])
    if result["x"] == "game":
        xs = {x: f"Game {int(x)}" for x in xs}  # each team's own dates differ; they're in the details
    headers = [result["x_label"]] + [item["label"] for item in series]
    lookup = [{p["x"]: p for p in item["points"]} for item in series]
    rows = []
    for x in sorted(xs):
        row = [(xs[x], "")]
        for item, pts in zip(series, lookup):
            p = pts.get(x)
            row.append(("", "") if p is None else (format_value(p["y"], item["format"]), p["detail"]))
        rows.append(row)
    return headers, rows
