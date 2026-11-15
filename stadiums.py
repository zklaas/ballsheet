#!/usr/bin/env python3
"""Stadiums tab: each club's home park's fence against the Naturalzone tab's uniform park.  No Qt.

**Where the dimensions come from.**  First ``stadium_dimensions.json``: fence distances by season
for 82 parks, transcribed by hand from English Wikipedia (each park names its article and the
revision read; CC BY-SA 4.0), at the angles each mark stands at.  A season none of its eras
covers falls back to MLB's posted distances.  MLB's Stats API gives a venue's posted distances (its
``fieldInfo``): down each line, to the left- and right-centre alleys, to centre, and for some
parks a "left" and "right" mark between line and alley.  ``/teams?hydrate=venue(fieldInfo)``
names each club's home park for a season.  MLB keeps one set of distances per park, today's or
its last, so a park whose fences moved shows the same distances every year; older parks often
have none at all.

**From marks to a fence.**  Each posted distance is put at a set angle (degrees from the line
to centre, negative toward third): the lines at +-45, the alleys at +-22.5 (halfway), the
"left"/"right" marks halfway between alley and line (+-33.75), centre at 0.  The fence between
marks is a straight line in (angle, distance), as the uniform park's own fence is drawn
(:mod:`naturalzone`), so it's the posted distances' outline, not the real wall's every corner.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from functools import lru_cache

import mlbstats

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stadium_dimensions.json")
WIKI = "https://en.wikipedia.org/wiki/"

# fieldInfo key -> (label, angle)
MARKS = [("leftLine", "LF line", -45.0), ("left", "LF", -33.75), ("leftCenter", "LCF", -22.5),
         ("center", "CF", 0.0), ("rightCenter", "RCF", 22.5), ("right", "RF", 33.75),
         ("rightLine", "RF line", 45.0)]
REQUIRED = ("leftLine", "center", "rightLine")


def fence_points(field: dict) -> tuple | None:
    """A venue's ``fieldInfo`` -> its fence as ``((angle, feet), ...)`` from the left-field line to
    the right-field line, or None without both lines and centre."""
    def feet(key):
        try:
            v = float(field.get(key))
        except (TypeError, ValueError):
            return None
        return v if 150 <= v <= 650 else None
    if any(feet(k) is None for k in REQUIRED):
        return None
    return tuple((angle, feet(key)) for key, _label, angle in MARKS if feet(key) is not None)


def marks(field: dict) -> list[tuple[str, float, float]]:
    """The posted distances a venue has: ``[(label, angle, feet)]``, left to right."""
    out = []
    for key, label, angle in MARKS:
        try:
            out.append((label, angle, float(field[key])))
        except (KeyError, TypeError, ValueError):
            pass
    return out


@lru_cache(maxsize=1)
def wikipedia_parks() -> dict:
    """``{venue id (str): {"article", "revid", "eras": [{"from", "to", "marks", "note"}]}}`` from
    ``stadium_dimensions.json`` (empty if it's missing)."""
    try:
        with open(DATA_FILE, encoding="utf-8") as fh:
            return json.load(fh).get("parks", {})
    except (OSError, ValueError):
        return {}


def wikipedia_era(venue_id, season: int, parks: dict | None = None) -> dict | None:
    """The Wikipedia era covering ``season`` at a venue, with the article's links, or None:
    ``{"marks": [(label, angle, feet)], "note", "article", "url", "revision_url", "walls", "also"}`` --
    ``walls`` the sections of fence whose height the articles give (``{"from", "to"`` (degrees, as the marks),
    ``"height"`` (ft), ``"label", "note"}``; elsewhere the height isn't known), ``also`` further articles cited."""
    park = (wikipedia_parks() if parks is None else parks).get(str(venue_id))
    if not park:
        return None
    for era in park["eras"]:
        if era["from"] <= season <= era["to"]:
            title = park["article"]
            slug = urllib.parse.quote(title.replace(" ", "_"))
            also = []
            for other in park.get("also", []):  # further articles the era's figures (e.g. wall heights) come from
                oslug = urllib.parse.quote(other["article"].replace(" ", "_"))
                also.append({"article": other["article"], "url": WIKI + oslug,
                             "revision_url": f"https://en.wikipedia.org/w/index.php?title={oslug}&oldid={other['revid']}"})
            return {"marks": [tuple(m) for m in era["marks"]], "note": era.get("note", ""), "article": title,
                    "url": WIKI + slug,
                    "revision_url": f"https://en.wikipedia.org/w/index.php?title={slug}&oldid={park['revid']}",
                    "walls": [dict(w) for w in era.get("walls", [])], "also": also}
    return None


def parse_home_parks(payload: dict, season: int | None = None, parks: dict | None = None) -> list[dict]:
    """``/teams?hydrate=venue(fieldInfo)`` -> ``[{team_id, team, venue_id, venue, field, marks, fence,
    source, wiki}]`` by team name: Wikipedia's distances for that season where
    ``stadium_dimensions.json`` has them (``source`` "wikipedia", ``wiki`` its era), else MLB's
    (``source`` "mlb"); ``fence`` None (``source`` None) where neither has enough."""
    out = []
    for team in payload.get("teams", []):
        venue = team.get("venue") or {}
        park = venue_park(venue.get("id"), venue.get("name", "?"), venue.get("fieldInfo") or {}, season, parks)
        out.append({"team_id": team.get("id"), "team": team.get("name", "?"), **park})
    out.sort(key=lambda p: p["team"])
    return out


def venue_park(venue_id, name: str, field: dict, season: int | None, parks: dict | None = None) -> dict:
    """One venue's distances for a season: ``{venue_id, venue, field, marks, fence, source, wiki, walls}`` --
    Wikipedia's era for the season if there is one (``source`` "wikipedia"), else MLB's ``field``
    (its fieldInfo; ``source`` "mlb"), else no fence (``source`` None)."""
    wiki = wikipedia_era(venue_id, season, parks) if season is not None else None
    if wiki:
        ms, fence, source = wiki["marks"], tuple((a, ft) for _l, a, ft in wiki["marks"]), "wikipedia"
    else:
        ms, fence = marks(field), fence_points(field)
        source = "mlb" if fence else None
    return {"venue_id": venue_id, "venue": name, "field": field, "marks": ms, "fence": fence,
            "source": source, "wiki": wiki, "walls": (wiki or {}).get("walls", [])}


def wall_height(walls, angle: float, default: float):
    """The height (ft) of a park's fence at ``angle``: the section of ``walls`` it falls in, else ``default``."""
    for w in walls or ():
        if w["from"] <= angle <= w["to"]:
            return w["height"]
    return default


def get_home_parks(season: int, ttl: float | None = None) -> list[dict]:
    """Each club's home park that season, with its dimensions (see :func:`parse_home_parks`)."""
    payload = mlbstats.api_get("/teams", {"sportId": mlbstats.SPORT_ID, "season": season,
                                          "hydrate": "venue(fieldInfo)"}, ttl=ttl)
    return parse_home_parks(payload, season)


OPEN_ERA = 2100  # an era "to" this year runs on to today


def parse_alternate_parks(parks: dict, venues: list[dict]) -> list[dict]:
    """Every park a user may put a game in: each Wikipedia era (``parks``, as
    :func:`wikipedia_parks`), then each MLB venue with posted distances that Wikipedia doesn't
    cover (``venues``, ``/venues`` records with ``fieldInfo``).  Each
    ``{"label", "search", "venue_id", "venue", "season", "field"}``, by name and then years;
    :func:`venue_park` of its ``venue_id``/``venue``/``field``/``season`` gives the park."""
    name_of = {str(v.get("id")): v.get("name") for v in venues if v.get("name")}
    out = []
    for vid, park in parks.items():
        # the article's title is the park's best-known name; MLB's (often its last) is searched too
        name = park.get("article") or name_of.get(vid) or f"Venue {vid}"
        for era in park.get("eras", []):
            years = (str(era["from"]) if era["from"] == era["to"] else f"{era['from']} on"
                     if era["to"] >= OPEN_ERA else f"{era['from']}–{era['to']}")
            note = era.get("note", "")
            note = "" if note.isdigit() else note  # a bare year says no more than the era does
            out.append({"label": f"{name}, {years}" + (f" — {note}" if note else ""),
                        "search": f"{name} {name_of.get(vid, '')} {years} {note}".casefold(),
                        "venue_id": int(vid), "venue": name, "season": era["from"], "field": {},
                        "sort": (name.casefold(), era["from"])})
    for v in venues:
        vid, field = v.get("id"), v.get("fieldInfo") or {}
        if str(vid) in parks or fence_points(field) is None:
            continue
        out.append({"label": f"{v['name']} — MLB's posted distances", "search": v["name"].casefold(),
                    "venue_id": vid, "venue": v["name"], "season": None, "field": field,
                    "sort": (v["name"].casefold(), 0)})
    out.sort(key=lambda p: p.pop("sort"))
    return out


def get_alternate_parks() -> list[dict]:
    """The parks of :func:`parse_alternate_parks`: Wikipedia's eras, named as MLB names the venue,
    and MLB's own parks with posted distances (two requests, cached a week)."""
    parks = wikipedia_parks()
    week = 7 * 24 * 3600
    venues = mlbstats.api_get("/venues", {"sportIds": mlbstats.SPORT_ID, "hydrate": "fieldInfo"},
                              ttl=week).get("venues", [])
    known = {str(v.get("id")) for v in venues}
    others = sorted(set(parks) - known, key=int)
    if others:  # the Wikipedia parks MLB's list of today's venues leaves out (long gone, mostly)
        venues = venues + mlbstats.api_get("/venues", {"venueIds": ",".join(others)},
                                           ttl=week).get("venues", [])
    return parse_alternate_parks(parks, venues)
