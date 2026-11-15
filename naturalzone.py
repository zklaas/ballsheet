#!/usr/bin/env python3
"""Naturalzone tab: how fielders with more range than average might cover for neighbours with
less.  No Qt here.

**Baseline zones.**  The field is divided as Total Zone Rating divides it: by the Project
Scoresheet / Retrosheet batted-ball location diagram (https://www.retrosheet.org/location.htm),
which is drawn to scale for a park 330 ft down the lines and 405 ft to centre.  Its zones are
sectors cut by rays from home plate and arcs (``ZONES`` below, traced from the diagram).  A
zone's code says whose ground it is: one fielder's number (``6``, ``6S``, ``6D``, ``6M``) makes it
his; two (``56``, ``78XD``) make it the gap between them, and it's split down its middle, the
half nearer the foul line going to the fielder who plays nearer the line.  The foul zones
(``5F``, ``7LF``...) go with the fair ground beside them.  Each fielder's **average zone** is
the ground so assigned, and two positions are *contiguous* where their average zones meet --
the length of border they share is the contiguity matrix.

**Natural zones.**  **Natural zones.**  Each fielder's range factor against MLB's at his position (the Range tab's
"Player / MLB") then sets how much ground he covers.  Like the Thiessen (Voronoi) polygons of
the average zones, each natural zone is grown -- or shrunk -- by a *reach* (feet) at its
borders with the other fielders (never at the wall or the foul lines), until its area is the
average zone's area times the fielder's ratio: a 120% third baseman covers a fifth more ground
than usual, pushing into his neighbours' zones; an 85% shortstop gives up ground along his
borders.  The zones are fitted one by one, so they needn't meet: ground a weak fielder gives
up that no neighbour reaches is a **gap** -- balls there are likely to fall in -- and ground
two fielders now reach is an **overlap**.  Ground a weak fielder gives up that a neighbour's
extra range takes in is **made up** by that neighbour.  The pitcher's and catcher's range
factors are mostly strikeouts and comebackers rather than ground covered, so they can be held
at their average zones (``fixed``).

**What the figures are.**  The model assumes a uniform field size (the diagram's one park, for
every park) and a uniform spatial correspondence between normalised range factor and
normalised field space (a fielder's ratio is the share of his usual zone he covers, the same
everywhere along its borders).  Its areas, reaches, gaps and overlaps are indices under those
assumptions, not literal claims about ground covered or balls reached.

**Real parks.**  Every function that works on the field takes an optional ``park``: a real
park's fence as ``(angle, feet)`` points from the left-field line (-45) to the right-field line
(45), joined by straight lines (see :mod:`stadiums`).  The zones keep their rays and arcs, but
the deepest ones run out to that fence instead of the diagram's, and the foul strips run as far
as its lines do -- so a deeper park gives its outfielders more usual ground, a smaller one less,
and a fielder's natural zone is his ratio times his usual ground *in that park*.  ``None`` is
the diagram's own park.

Coordinates are feet, home plate at the origin, y toward centre field, x toward first base;
angles are degrees from the line to centre field, negative toward third base.
"""
from __future__ import annotations

import math
from functools import lru_cache

POSITIONS = ["P", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"]
NUMBER = {"1": "P", "2": "C", "3": "1B", "4": "2B", "5": "3B", "6": "SS", "7": "LF", "8": "CF", "9": "RF"}
# left to right across the field, for splitting a shared zone (the left half to the one on the left)
_ACROSS = {"LF": -3, "3B": -2, "SS": -1, "P": 0, "C": 0, "CF": 0, "2B": 1, "1B": 2, "RF": 3}
# where each fielder normally stands: the dots on the Project Scoresheet diagram
SITES = {"P": (0.0, 60.5), "C": (0.0, -2.0), "1B": (67.4, 79.1), "2B": (47.3, 134.8),
         "3B": (-67.4, 79.1), "SS": (-47.3, 134.8), "LF": (-150.5, 256.0), "CF": (0.0, 346.7),
         "RF": (150.5, 256.0)}
BASES = [(0.0, 0.0), (63.64, 63.64), (0.0, 127.28), (-63.64, 63.64)]
FOUL_WIDTH = 25.0   # how far into foul ground the F zones reach beside the lines
BEHIND_HOME = 40.0  # ... and behind home plate

# Radii traced from the diagram, as (|angle|, feet) points joined by straight lines.
_ARC_S = [(0, 106.0), (6, 105.6), (14, 105.0), (20, 99.0), (27, 95.3), (32, 92.7), (36, 91.4), (45, 89.0)]
_ARC_D = [(0, 159.4), (6, 159.4), (14, 156.8), (20, 153.6), (27, 149.7), (32, 145.8), (36, 141.9),
          (41, 137.4), (45, 133.0)]
_FENCE = [(0, 405.0), (3, 405.6), (6, 407.0), (9.3, 410.0), (12, 398.0), (15, 385.0), (18, 374.0),
          (21, 364.0), (24, 355.0), (27, 349.0), (30, 343.0), (33, 339.0), (36, 336.0), (39, 334.0),
          (42, 331.0), (45, 330.0)]
R_HOME, R_NEAR, R_MOUND, R_OF_S, R_OF, R_OF_D, R_XD = 24.6, 50.5, 70.0, 210.0, 260.0, 310.0, 356.0
_OPEN = 1e9  # no outer edge but the fence


def _interp(table, a: float) -> float:
    a = abs(a)
    for (a0, r0), (a1, r1) in zip(table, table[1:]):
        if a <= a1:
            return r0 + (r1 - r0) * (a - a0) / (a1 - a0)
    return table[-1][1]


def fence(angle: float) -> float:
    """The diagram's (uniform) park's fence at ``angle``."""
    return _interp(_FENCE, angle)


# the uniform park's fence in the same signed form as a real park's
UNIFORM_FENCE = tuple(sorted({(-a, r) for a, r in _FENCE} | set(_FENCE)))


def fence_at(park, angle: float) -> float:
    """A park's fence (feet from home) at ``angle``; ``park`` None is the uniform park."""
    if park is None:
        return fence(angle)
    a = max(park[0][0], min(park[-1][0], angle))
    for (a0, r0), (a1, r1) in zip(park, park[1:]):
        if a <= a1:
            return r0 + (r1 - r0) * (a - a0) / (a1 - a0) if a1 > a0 else r1
    return park[-1][1]


def _left_zones():
    """(code, angle from, angle to, inner radius fn, outer radius fn) for the third-base half and
    the middle; the first-base half is the mirror image."""
    const = lambda r: (lambda a: r)
    arc_s, arc_d = (lambda a: _interp(_ARC_S, a)), (lambda a: _interp(_ARC_D, a))
    # the deepest zones run to whatever park's fence: _locate cuts them off there
    xd_or_fence = lambda a: R_XD if abs(a) < 23.8 else _OPEN
    z = [("2", -45, 0, const(0), const(R_HOME)),
         ("25", -45, -23.8, const(R_HOME), const(R_NEAR)), ("1S", -23.8, 0, const(R_HOME), const(R_NEAR)),
         ("5S", -45, -37, const(R_NEAR), arc_s), ("15", -37, -23.8, const(R_NEAR), const(R_MOUND)),
         ("1", -23.8, 0, const(R_NEAR), const(R_MOUND)),
         ("56S", -37, -23.8, const(R_MOUND), arc_s), ("6S", -23.8, -9.3, const(R_MOUND), arc_s),
         ("6MS", -9.3, 0, const(R_MOUND), arc_s)]
    for suffix, inner, outer in (("", arc_s, arc_d), ("D", arc_d, const(R_OF_S))):
        z += [("5" + suffix, -45, -37, inner, outer), ("56" + suffix, -37, -23.8, inner, outer),
              ("6" + suffix, -23.8, -9.3, inner, outer), ("6M" + suffix, -9.3, 0, inner, outer)]
    for suffix, inner, outer in (("S", const(R_OF_S), const(R_OF)), ("", const(R_OF), const(R_OF_D)),
                                 ("D", const(R_OF_D), xd_or_fence)):
        z += [("7L" + suffix, -45, -37, inner, outer), ("7" + suffix, -37, -23.8, inner, outer),
              ("78" + suffix, -23.8, -9.3, inner, outer), ("8" + suffix, -9.3, 0, inner, outer)]
    z += [("78XD", -23.8, -9.3, const(R_XD), const(_OPEN)), ("8XD", -9.3, 0, const(R_XD), const(_OPEN))]
    return z


_MIRROR = str.maketrans("5634", "4365")  # third-base side -> first-base side; "7"/"9" handled below


def _mirror_code(code: str) -> str:
    if code in ("2", "1S", "1", "8", "8S", "8D", "8XD"):
        return code
    if code.startswith("6M"):
        return "4M" + code[2:]
    out = code.replace("78", "89").replace("7L", "9L").replace("7", "9")
    return out.translate(str.maketrans({"5": "3", "6": "4", "3": "5", "4": "6"})) if not out.startswith(("89", "9")) else out


def zone_list() -> list[tuple]:
    """Every fair zone as (code, angle from, angle to, inner radius fn, outer radius fn)."""
    out = []
    for code, a0, a1, rin, rout in _left_zones():
        if a1 <= 0 and a0 < 0:
            mirrored = (_mirror_code(code), -a1, -a0, rin, rout)
            # the middle zones that straddle 0 are listed once each side and merged by code
            out.append((code, a0, a1, rin, rout))
            out.append(mirrored)
    return out


def zone_fielders(code: str) -> list[str]:
    """The fielders a zone code names: "56D" -> ["3B", "SS"], "6M" -> ["SS"], "1S" -> ["P"]."""
    digits = [c for c in code if c.isdigit()]
    return [NUMBER[d] for d in dict.fromkeys(digits)]


# --------------------------------------------------------------------------- #
#  The grid                                                                   #
# --------------------------------------------------------------------------- #
CELL = 2.0  # feet
X0, X1, Y0, Y1 = -256.0, 256.0, -BEHIND_HOME - 2, 414.0  # the uniform park's grid


def bounds(park=None) -> tuple[float, float, float, float]:
    """(x0, x1, y0, y1) of a park's grid: the uniform park's, widened (on the same cells) to take
    in a bigger park's fence and foul strips -- so a park's grid always holds the uniform park's."""
    if park is None:
        return X0, X1, Y0, Y1
    wide, deep = 0.0, 0.0
    for step in range(-90, 91):
        a = step / 2
        r = fence_at(park, a)
        wide, deep = max(wide, abs(r * math.sin(math.radians(a)))), max(deep, r * math.cos(math.radians(a)))
    for line in (-45, 45):  # the foul strip's far corner
        wide = max(wide, (fence_at(park, line) + FOUL_WIDTH) / math.sqrt(2))
    x1 = max(X1, CELL * math.ceil((wide + 4) / CELL))
    y1 = max(Y1, Y0 + CELL * math.ceil((deep + 4 - Y0) / CELL))
    return -x1, x1, Y0, y1


def _polar(x: float, y: float) -> tuple[float, float]:
    return math.degrees(math.atan2(x, y)), math.hypot(x, y)


def _locate(x: float, y: float, zones, park=None) -> tuple[str, str] | None:
    """-> (zone code, fielder) of a point in ``park`` (None: the uniform one), or None off the field."""
    a, r = _polar(x, y)
    if abs(a) <= 45:
        if r > fence_at(park, a):
            return None
        for code, a0, a1, rin, rout in zones:
            if a0 <= a <= a1 and rin(a) <= r < rout(a):
                return code, _owner(code, a, a0, a1)
        return None
    # foul ground: within FOUL_WIDTH of a line, or close behind home
    line = -45 if a < 0 else 45
    beyond = math.radians(abs(a) - 45)
    along, off = r * math.cos(beyond), r * math.sin(beyond)
    if r <= BEHIND_HOME and (abs(a) > 90 or along < R_HOME):
        return "2F", "C"
    if abs(a) > 90 or off > FOUL_WIDTH or along > fence_at(park, line):
        return None
    side = "5" if a < 0 else "3"
    of = "7L" if a < 0 else "9L"
    if along < R_HOME:
        return "2F", "C"
    if along < R_NEAR:
        code = "25F" if a < 0 else "23F"
        return code, ("C" if along < (R_HOME + R_NEAR) / 2 else NUMBER[side])
    if along < _interp(_ARC_D, 45):
        return side + "F", NUMBER[side]
    if along < R_OF_S:
        return side + "DF", NUMBER[side]
    for edge, suffix in ((R_OF, "SF"), (R_OF_D, "F"), (1e9, "DF")):
        if along < edge:
            return of + suffix, NUMBER[of[0]]
    return None


def _owner(code: str, a: float, a0: float, a1: float) -> str:
    """Whose ground a point of a zone is: the zone's fielder, or for a shared zone, the one on
    its side of the zone's middle."""
    who = zone_fielders(code)
    if len(who) == 1:
        return who[0]
    left, right = sorted(who, key=lambda f: _ACROSS[f])
    return left if a < (a0 + a1) / 2 else right


@lru_cache(maxsize=8)
def grid(park=None) -> dict:
    """The field (``park``'s, or the uniform one) as square cells of ``CELL`` feet: each cell's zone
    code and average-zone fielder, and each fielder's signed distance from his average zone
    (negative inside it).  ``x0``..``y1`` are the grid's edges (see :func:`bounds`)."""
    zones = zone_list()
    X0, X1, Y0, Y1 = bounds(park)
    nx, ny = round((X1 - X0) / CELL), round((Y1 - Y0) / CELL)
    codes: list = [None] * (nx * ny)
    base: list = [-1] * (nx * ny)
    index = {p: i for i, p in enumerate(POSITIONS)}
    for j in range(ny):
        y = Y0 + (j + 0.5) * CELL
        for i in range(nx):
            got = _locate(X0 + (i + 0.5) * CELL, y, zones, park)
            if got:
                codes[j * nx + i], base[j * nx + i] = got[0], index[got[1]]
    field = [k for k, b in enumerate(base) if b >= 0]
    signed = [_signed_distance(base, nx, ny, f) for f in range(len(POSITIONS))]
    # the chamfer distances come in steps, so many cells tie; nearer the fielder's usual spot wins
    for f, pos in enumerate(POSITIONS):
        sx, sy = SITES[pos]
        sd = signed[f]
        for j in range(ny):
            y = Y0 + (j + 0.5) * CELL - sy
            for i in range(nx):
                k = j * nx + i
                sd[k] += 1e-4 * math.hypot(X0 + (i + 0.5) * CELL - sx, y)
    return {"nx": nx, "ny": ny, "codes": codes, "base": base, "field": field, "signed": signed,
            "x0": X0, "x1": X1, "y0": Y0, "y1": Y1, "park": park}


def _chamfer(seed: list, nx: int, ny: int) -> list:
    """Distance (feet, 3-4 chamfer) from every cell to the nearest seed cell."""
    big = 1e9
    d = [0.0 if s else big for s in seed]
    a, b = CELL, CELL * math.sqrt(2)
    for j in range(ny):
        row = j * nx
        for i in range(nx):
            k = row + i
            v = d[k]
            if v == 0.0:
                continue
            if i > 0 and d[k - 1] + a < v:
                v = d[k - 1] + a
            if j > 0:
                up = k - nx
                if d[up] + a < v:
                    v = d[up] + a
                if i > 0 and d[up - 1] + b < v:
                    v = d[up - 1] + b
                if i < nx - 1 and d[up + 1] + b < v:
                    v = d[up + 1] + b
            d[k] = v
    for j in range(ny - 1, -1, -1):
        row = j * nx
        for i in range(nx - 1, -1, -1):
            k = row + i
            v = d[k]
            if v == 0.0:
                continue
            if i < nx - 1 and d[k + 1] + a < v:
                v = d[k + 1] + a
            if j < ny - 1:
                down = k + nx
                if d[down] + a < v:
                    v = d[down] + a
                if i < nx - 1 and d[down + 1] + b < v:
                    v = d[down + 1] + b
                if i > 0 and d[down - 1] + b < v:
                    v = d[down - 1] + b
            d[k] = v
    return d


def _signed_distance(base: list, nx: int, ny: int, f: int) -> list:
    """Outside fielder ``f``'s average zone: the distance to it; inside: minus the distance to the
    nearest ground of another fielder (the field's own edge isn't a border with anyone)."""
    outside = _chamfer([b == f for b in base], nx, ny)
    inside = _chamfer([b >= 0 and b != f for b in base], nx, ny)
    return [(-inside[k] if base[k] == f else outside[k]) for k in range(len(base))]


# --------------------------------------------------------------------------- #
#  The model                                                                  #
# --------------------------------------------------------------------------- #
def average_zones(park=None) -> dict:
    """``{"areas": {position: sq ft}, "contiguity": {position: {position: feet of shared border}}}``
    for the average zones (in ``park``)."""
    g = grid(park)
    areas = {p: 0.0 for p in POSITIONS}
    for k in g["field"]:
        areas[POSITIONS[g["base"][k]]] += CELL * CELL
    return {"areas": areas, "contiguity": _borders(g["base"], g["nx"], g["ny"])}


def _borders(owner: list, nx: int, ny: int) -> dict:
    """Feet of border between every two fielders' ground in a grid of owners."""
    n = len(POSITIONS)
    edge = [[0.0] * n for _ in range(n)]
    for j in range(ny):
        for i in range(nx):
            k = j * nx + i
            o = owner[k]
            if o < 0:
                continue
            for nb in ((k + 1) if i < nx - 1 else -1, (k + nx) if j < ny - 1 else -1):
                if nb >= 0 and owner[nb] >= 0 and owner[nb] != o:
                    edge[o][owner[nb]] += CELL
                    edge[owner[nb]][o] += CELL
    return {POSITIONS[a]: {POSITIONS[b]: edge[a][b] for b in range(n)} for a in range(n)}


@lru_cache(maxsize=8)
def _sorted_distances(park=None) -> list:
    """Per fielder, the field cells' signed distances from his average zone, in order."""
    g = grid(park)
    return [sorted(g["signed"][f][k] for k in g["field"]) for f in range(len(POSITIONS))]


def reach_for(fielder: int, target_area: float, park=None) -> float:
    """How far (feet) a fielder's average zone must grow (or, negative, shrink) at its borders
    with the other fielders to cover ``target_area`` square feet -- as near as the grid allows."""
    ds = _sorted_distances(park)[fielder]
    n = max(1, min(len(ds), round(target_area / (CELL * CELL))))
    return ds[-1] if n >= len(ds) else (ds[n - 1] + ds[n]) / 2


def natural_zones(ratios: dict, fixed=(), park=None) -> dict:
    """Each fielder's natural zone, in ``park`` (None: the uniform park), for range ratios
    ``{position: ratio or None}`` (1.0 = MLB average): his average zone grown or shrunk at its borders with the others until its area is
    the average area x his ratio.  Positions in ``fixed``, and any without a ratio, keep their
    average zones.  Zones are fitted one by one, so they can overlap, and leave gaps -- ground
    no fielder is likely to reach.

    -> ``{"reach", "areas", "avg_areas", "targets", "contiguity", "cover", "coverage", "gap",
    "made_up", "overlap", "totals"}``:

    * ``cover``: per grid cell, the set of fielder indexes whose natural zone takes it in
      (``None`` off the field)
    * ``coverage[zone][fielder]``: the share (0..1) of ``zone``'s average zone inside
      ``fielder``'s natural zone
    * ``gap[zone]``: the share of ``zone``'s average zone nobody covers
    * ``made_up[zone]``: the share its own fielder gave up that someone else covers
    * ``made_up_by[zone][fielder]``: the share of it that ``fielder`` covers (two may cover the same
      ground, so these can add up to more than ``made_up``)
    * ``made_up_alone[zone][fielder]``: the share made up by ``fielder`` with no overlap -- no one
      else reaches it -- and ``made_up_shared[zone][(fielder, fielder, ...)]`` the share made up
      where those fielders' natural zones overlap; together they add up to ``made_up``
    * ``overlap[zone]``: the share two or more fielders cover
    * ``totals``: field-wide ``{"field", "gap", "overlap", "made_up_alone", "made_up_shared"}`` in
      square feet -- the last two: ground given up that one fielder alone makes up, and that
      fielders whose zones overlap make up (part of ``overlap``)
    * ``park``: the park it was worked out for"""
    g = grid(park)
    avg = average_zones(park)
    avg_areas = avg["areas"]
    n = len(POSITIONS)
    reach, targets = {}, {}
    for f, p in enumerate(POSITIONS):
        ratio = ratios.get(p)
        if p in fixed or not ratio or ratio <= 0:
            targets[p], reach[p] = avg_areas[p], 0.0
        else:
            targets[p] = avg_areas[p] * ratio
            reach[p] = reach_for(f, targets[p], park)
    reach_list = [reach[p] for p in POSITIONS]
    signed, base = g["signed"], g["base"]
    cover: list = [None] * len(base)
    areas = [0.0] * n
    within = [[0] * n for _ in range(n)]
    gap_cells, made_cells, over_cells, sizes = [0] * n, [0] * n, [0] * n, [0] * n
    made_by = [[0] * n for _ in range(n)]
    made_alone = [[0] * n for _ in range(n)]   # [zone][fielder]: made up by him and no one else
    made_shared: list[dict] = [{} for _ in range(n)]  # [zone]{fielders}: made up where two or more overlap
    gap_total = over_total = alone_total = shared_total = 0
    for k in g["field"]:
        who = frozenset(f for f in range(n) if signed[f][k] <= reach_list[f])
        cover[k] = who
        z = base[k]
        sizes[z] += 1
        for f in who:
            areas[f] += CELL * CELL
            within[z][f] += 1
        if not who:
            gap_cells[z] += 1
            gap_total += 1
        elif z not in who:
            made_cells[z] += 1
            for f in who:
                made_by[z][f] += 1
            if len(who) == 1:
                made_alone[z][next(iter(who))] += 1
                alone_total += 1
            else:
                shared_total += 1
                made_shared[z][who] = made_shared[z].get(who, 0) + 1
        if len(who) > 1:
            over_cells[z] += 1
            over_total += 1
    share = lambda count, z: count / sizes[z] if sizes[z] else 0.0
    return {
        "park": park, "reach": reach, "targets": targets, "avg_areas": avg_areas,
        "areas": {POSITIONS[f]: areas[f] for f in range(n)},
        "contiguity": avg["contiguity"], "cover": cover,
        "coverage": {POSITIONS[z]: {POSITIONS[f]: share(within[z][f], z) for f in range(n)} for z in range(n)},
        "gap": {POSITIONS[z]: share(gap_cells[z], z) for z in range(n)},
        "made_up": {POSITIONS[z]: share(made_cells[z], z) for z in range(n)},
        "made_up_by": {POSITIONS[z]: {POSITIONS[f]: share(made_by[z][f], z) for f in range(n) if made_by[z][f]}
                       for z in range(n)},
        "made_up_alone": {POSITIONS[z]: {POSITIONS[f]: share(made_alone[z][f], z) for f in range(n)
                                         if made_alone[z][f]} for z in range(n)},
        "made_up_shared": {POSITIONS[z]: {tuple(POSITIONS[f] for f in sorted(who)): share(count, z)
                                          for who, count in made_shared[z].items()} for z in range(n)},
        "overlap": {POSITIONS[z]: share(over_cells[z], z) for z in range(n)},
        "totals": {"field": len(g["field"]) * CELL * CELL, "gap": gap_total * CELL * CELL,
                   "overlap": over_total * CELL * CELL, "made_up_alone": alone_total * CELL * CELL,
                   "made_up_shared": shared_total * CELL * CELL},
    }


# --------------------------------------------------------------------------- #
#  For drawing                                                                #
# --------------------------------------------------------------------------- #
def borders(values: list, keep=lambda a, b: True, park=None) -> list:
    """The grid edges (feet: ((x0, y0), (x1, y1))) between neighbouring cells of ``park``'s grid whose
    ``values`` differ and for which ``keep(a, b)`` holds; runs along a row or column are joined."""
    g = grid(park)
    nx, ny, X0, Y0 = g["nx"], g["ny"], g["x0"], g["y0"]
    out = []
    # vertical edges, between a cell and the one to its right, joined up the column
    for i in range(nx - 1):
        run = None
        for j in range(ny + 1):
            on = False
            if j < ny:
                a, b = values[j * nx + i], values[j * nx + i + 1]
                on = a != b and keep(a, b)
            if on and run is None:
                run = j
            elif not on and run is not None:
                x = X0 + (i + 1) * CELL
                out.append(((x, Y0 + run * CELL), (x, Y0 + j * CELL)))
                run = None
    for j in range(ny - 1):
        run = None
        for i in range(nx + 1):
            on = False
            if i < nx:
                a, b = values[j * nx + i], values[(j + 1) * nx + i]
                on = a != b and keep(a, b)
            if on and run is None:
                run = i
            elif not on and run is not None:
                y = Y0 + (j + 1) * CELL
                out.append(((X0 + run * CELL, y), (X0 + i * CELL, y)))
                run = None
    return out


@lru_cache(maxsize=8)
def zone_drawing(park=None) -> dict:
    """What doesn't change from one defense to another: the Retrosheet zones' borders and where
    their codes go, the average zones' borders, and the field's edge (in ``park``)."""
    g = grid(park)
    codes, base, nx, X0, Y0 = g["codes"], g["base"], g["nx"], g["x0"], g["y0"]
    sums: dict = {}
    for k in g["field"]:
        s = sums.setdefault(codes[k], [0.0, 0.0, 0])
        s[0] += X0 + (k % nx + 0.5) * CELL
        s[1] += Y0 + (k // nx + 0.5) * CELL
        s[2] += 1
    labels = {code: (sx / n, sy / n, n * CELL * CELL) for code, (sx, sy, n) in sums.items()}
    both = lambda a, b: a is not None and b is not None
    return {"zone_borders": borders(codes, both, park), "labels": labels,
            "average_borders": borders(base, lambda a, b: a >= 0 and b >= 0, park),
            "edge": borders([b >= 0 for b in base], park=park)}


def natural_drawing(result: dict) -> dict:
    """Each fielder's natural zone's outline (feet segments), for :func:`natural_zones`' result."""
    cover, park = result["cover"], result.get("park")
    out = {}
    for f, pos in enumerate(POSITIONS):
        inside = [c is not None and f in c for c in cover]
        out[pos] = borders(inside, lambda a, b: True, park)
    return out


@lru_cache(maxsize=8)
def compare_park(park) -> dict:
    """A real park against the uniform one, on the park's grid (which holds both):

    * ``status``: per cell, 1 ground the park has that the uniform park hasn't (*gained*), -1
      ground the uniform park has that the park hasn't (*lost*), 0 ground both have, None neither
    * ``owner``: per cell, the index of the fielder whose usual ground it is (in the park, or for
      lost ground, in the uniform park), -1 for neither
    * ``gained`` / ``lost``: ``{position: sq ft}`` -- whose usual ground grows or shrinks
    * ``areas``: ``{"park", "uniform"}`` -- the whole field's sq ft in each
    * ``uniform_edge``: the uniform park's edge, as segments on this grid"""
    g = grid(park)
    nx, ny, X0, Y0 = g["nx"], g["ny"], g["x0"], g["y0"]
    zones = zone_list()
    index = {p: i for i, p in enumerate(POSITIONS)}
    status: list = [None] * (nx * ny)
    owner = list(g["base"])
    in_uniform = [False] * (nx * ny)
    gained, lost = {p: 0.0 for p in POSITIONS}, {p: 0.0 for p in POSITIONS}
    for j in range(ny):
        y = Y0 + (j + 0.5) * CELL
        for i in range(nx):
            k = j * nx + i
            was = _locate(X0 + (i + 0.5) * CELL, y, zones)
            in_uniform[k] = was is not None
            here = g["base"][k] >= 0
            if here and was:
                status[k] = 0
            elif here:
                status[k] = 1
                gained[POSITIONS[g["base"][k]]] += CELL * CELL
            elif was:
                status[k], owner[k] = -1, index[was[1]]
                lost[was[1]] += CELL * CELL
    area = CELL * CELL
    return {"status": status, "owner": owner, "gained": gained, "lost": lost,
            "areas": {"park": len(g["field"]) * area, "uniform": sum(in_uniform) * area},
            "uniform_edge": borders(in_uniform, park=park)}
