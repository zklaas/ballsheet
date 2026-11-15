#!/usr/bin/env python3
"""The Statcast tab's diorama: a pitcher and a batter on a regulation field, drawn in 3D with
QPainter, with each pitch's flight from the pitcher's hand to the plate and the batted ball's
flight off the bat.

World coordinates are MLB's own pitch-tracking frame, in feet: x toward the catcher's right
(the first-base side), y from the point of home plate out toward the pitcher and center field,
z up.  A pitch's path is MLB's fit of it (constant acceleration, see ``mlbstats._parse_track``),
run back to the release point and on to the plate.  A batted ball has only its launch (exit
velocity, launch angle, direction) and where it ended up, so its flight is modelled: a ball in
the air with drag, the drag tuned until the ball lands at the distance MLB measured; a ground
ball or low liner hops and rolls out to where it was fielded.

The fence is the game's park's when it's known (``set_park``: Wikipedia's or MLB's distances,
see :mod:`stadiums`), drawn 8 ft high except where the park's Wikipedia articles give a section's
height (Fenway's Green Monster: 37 ft 2 in from the left-field pole to the flagpole).  A
hit that wasn't caught and didn't leave the park stops at the fence and drops; where it reached
the fence higher than the fence there, the wall must be taller, so a section of wall that tall is
drawn for that play only; a home run that crossed the fence lower than the fence there must have
cleared a lower one, so the fence gives way there to a section low enough to clear; and a home run
whose distance (MLB's measurement, to where it would have come down at field level) falls short
of the drawn fence shows the fence there must be nearer, so it's pulled in for that play.  The other
way round, a ball that stayed in the park -- caught, or an error, a fielder's choice -- but came down
or was caught beyond the drawn fence (a fence drawn straight between the park's posted distances
cuts inside a curved one) shows the fence there must be farther, so it's pushed back for that play
(``wall_contact``).

The renderer is a small painter's-algorithm one: the field is drawn flat first, then
everything standing on it (the figures, the arcs, the strike-zone grid) back to front.  The
figures are "thick stick" silhouettes -- each limb a 3D segment drawn as a round-capped line as
wide as the limb would be at that distance -- so they read as solid silhouettes from any angle.
Drag to orbit, right-drag (or Shift+drag) to pan, the wheel to zoom.
"""
from __future__ import annotations

import math

from PyQt6.QtCore import QElapsedTimer, QLineF, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPalette,
                         QPen, QPolygonF)
from PyQt6.QtWidgets import QSizePolicy, QWidget

import naturalzone

# --------------------------------------------------------------------------- #
#  Field and physics                                                          #
# --------------------------------------------------------------------------- #
RUBBER_Y = 60.5            # front of the pitcher's rubber from the point of home plate
MOUND_Y = 59.0             # center of the mound's 18 ft circle
MOUND_R = 9.0
MOUND_TOP_R = 2.5          # the flat top around the rubber
MOUND_H = 10 / 12          # the rubber is 10 in above home plate
PLATE_FRONT = 17 / 12      # where a pitch's plate location (px, pz) is measured
PLATE_HALF = 8.5 / 12      # the plate is 17 in wide
BALL_R = 1.45 / 12         # a baseball is ~2.9 in across
BASE = 90.0
G = 32.174                 # ft/s^2
MPH = 5280 / 3600          # ft/s per mph
DRAG_K = 0.0019            # rho Cd A / 2m for a baseball at sea level, per ft
SPRAY_HOME = (125.42, 198.27)  # home plate in MLB's Gameday field-drawing pixels
SPRAY_FT = 2.5                 # feet per pixel of that drawing
DEFAULT_HEIGHT = 74            # inches, when the feed has none

# where a ball hit to each fielder goes, when the feed has no spray coordinates: degrees from
# straight-away center, + toward right field
FIELDER_ANGLE = {"1": 0, "2": 0, "3": 38, "4": 18, "5": -38, "6": -18, "7": -30, "8": 0, "9": 30}


def mound_height(x: float, y: float) -> float:
    """The ground's height at (x, y): 0 but on the mound, which rises to MOUND_H at its top."""
    r = math.hypot(x, y - MOUND_Y)
    if r >= MOUND_R:
        return 0.0
    if r <= MOUND_TOP_R:
        return MOUND_H
    return MOUND_H * (MOUND_R - r) / (MOUND_R - MOUND_TOP_R)


def fence_distance(angle_deg: float) -> float:
    """A generic outfield fence: 330 ft down the lines, 400 ft to center (no real park's)."""
    return 330 + 70 * math.cos(math.radians(2 * max(-45.0, min(45.0, angle_deg))))


def _time_at_y(track: dict, y: float) -> float:
    """When the pitch's fitted path is at depth ``y`` (negative = before the fit's start)."""
    a, b, c = 0.5 * track["ay"], track["vy0"], track["y0"] - y
    if abs(a) < 1e-9:
        return -c / b
    disc = b * b - 4 * a * c
    if disc < 0:
        return -c / b
    root = math.sqrt(disc)
    return min(((-b - root) / (2 * a), (-b + root) / (2 * a)), key=abs)


def pitch_point(track: dict, t: float) -> tuple:
    return (track["x0"] + track["vx0"] * t + 0.5 * track["ax"] * t * t,
            track["y0"] + track["vy0"] * t + 0.5 * track["ay"] * t * t,
            track["z0"] + track["vz0"] * t + 0.5 * track["az"] * t * t)


def release_y(track: dict) -> float:
    ext = track.get("extension")
    return RUBBER_Y - (ext if ext is not None and 3 < ext < 9 else 6.0)


def pitch_path(track: dict, end_y: float = PLATE_FRONT, steps: int = 30) -> list[tuple]:
    """The pitch from release to ``end_y`` as [(seconds since release, (x, y, z)), ...]."""
    t0, t1 = _time_at_y(track, release_y(track)), _time_at_y(track, end_y)
    return [((t1 - t0) * i / steps, pitch_point(track, t0 + (t1 - t0) * i / steps)) for i in range(steps + 1)]


def release_point(track: dict) -> tuple:
    return pitch_point(track, _time_at_y(track, release_y(track)))


def fielded_distance(hit: dict) -> float | None:
    """How far from home plate the ball was fielded (caught, for a ball caught in the air), from the
    spray coordinates (ft), or None."""
    if not hit.get("spray"):
        return None
    return SPRAY_FT * math.hypot(hit["spray"][0] - SPRAY_HOME[0], SPRAY_HOME[1] - hit["spray"][1])


def spray_angle(hit: dict) -> float | None:
    """Degrees from straight-away center field, + toward right field."""
    if hit.get("spray"):
        dx, dy = hit["spray"][0] - SPRAY_HOME[0], SPRAY_HOME[1] - hit["spray"][1]
        if math.hypot(dx, dy) > 2:
            return math.degrees(math.atan2(dx, dy))
    return FIELDER_ANGLE.get(hit.get("location") or "")


def grounder(hit: dict) -> bool:
    return "ground" in (hit.get("trajectory") or "") or (hit.get("la") is not None and hit["la"] < 5)


def batted_distance(hit: dict) -> float | None:
    """How far the ball went, in feet.  MLB's distance is where a ball in the air came down (or
    was caught); for a ground ball it is only its first bounce, so there it is where the ball
    was fielded, from the spray coordinates."""
    fielded = fielded_distance(hit)
    if grounder(hit) and fielded:
        return max(fielded, float(hit.get("dist") or 0))
    return float(hit["dist"]) if hit.get("dist") else fielded


def _fly(v: list, p: list, k: float, until: float, dt: float = 0.01, limit: float = 15.0) -> list[tuple]:
    """Fly a ball with drag ``k`` until it comes down to the ground or is ``until`` ft from home
    (horizontally).  [(t, (x, y, z)), ...]"""
    out, t = [(0.0, tuple(p))], 0.0
    p, v = list(p), list(v)
    while t < limit:
        speed = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
        acc = (-k * speed * v[0], -k * speed * v[1], -k * speed * v[2] - G)
        v = [v[i] + acc[i] * dt for i in range(3)]
        q = [p[i] + v[i] * dt for i in range(3)]
        t += dt
        if q[2] <= 0 and v[2] < 0:  # down: finish on the ground
            f = p[2] / (p[2] - q[2]) if p[2] != q[2] else 1.0
            q = [p[i] + (q[i] - p[i]) * f for i in range(3)]
            q[2] = 0.0
            out.append((t - dt * (1 - f), tuple(q)))
            return out
        if math.hypot(q[0], q[1]) >= until:  # caught (or met) in the air
            out.append((t, tuple(q)))
            return out
        p = q
        out.append((t, tuple(p)))
    return out


def _reach(path: list[tuple]) -> float:
    x, y, _z = path[-1][1]
    return math.hypot(x, y)


def batted_path(hit: dict, contact: tuple) -> dict | None:
    """A batted ball's modelled flight from ``contact``::

        {"points": [(t, (x, y, z)), ...], "hang": seconds in the air before it first came
         down (or was caught), "landing": (x, y, z) where it ended, "angle": spray degrees,
         "distance": ft, "air": True when it was caught or landed before touching the ground}

    The launch is MLB's; the drag is tuned until the flight ends at MLB's distance (what a fly
    ball carries, what a liner does in the air).  A ball that can't reach it in the air -- a
    grounder, a low liner -- hops and rolls the rest of the way to where it was fielded."""
    if not hit or hit.get("ev") is None or hit.get("la") is None:
        return None
    out = _batted_path(hit, contact)
    out["fielded"] = None if grounder(hit) else fielded_distance(hit)
    return out


def _batted_path(hit: dict, contact: tuple) -> dict:
    angle, dist = spray_angle(hit), batted_distance(hit)
    if angle is None:
        angle = 0.0
    if dist is None or dist < 1:
        dist = max(10.0, (hit["ev"] * MPH) ** 2 * math.sin(math.radians(2 * max(hit["la"], 1))) / G * 0.5)
    speed, la, phi = hit["ev"] * MPH, math.radians(hit["la"]), math.radians(angle)
    v0 = [speed * math.cos(la) * math.sin(phi), speed * math.cos(la) * math.cos(phi), speed * math.sin(la)]
    p0 = list(contact)
    in_air = not grounder(hit)

    def landing(k):  # how far it carries before coming down, on drag k
        return _reach(_fly(v0, p0, k, math.inf))

    def solve(lo, hi):  # the drag that brings it down at the distance (landing falls as drag rises)
        for _ in range(22):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if landing(mid) > dist else (lo, mid)
        return (lo + hi) / 2

    if landing(DRAG_K) >= dist:
        # it carries at least that far: more drag until it comes down at the distance -- or,
        # still up there on the most drag allowed, it was caught (or met the wall) in the air
        k = DRAG_K * 6 if landing(DRAG_K * 6) > dist else solve(DRAG_K, DRAG_K * 6)
        return _finish(_fly(v0, p0, k, dist), angle, dist, True)
    if in_air and landing(0.0) >= dist:
        # a fly ball or liner carrying farther than plain drag allows (its backspin holds it up)
        return _finish(_fly(v0, p0, solve(0.0, DRAG_K), dist), angle, dist, True)
    base = _fly(v0, p0, DRAG_K, dist)
    # comes down short: hop and roll out to the distance
    path = list(base)
    t, p = path[-1]
    vx = (path[-1][1][0] - path[-2][1][0]) / max(path[-1][0] - path[-2][0], 1e-6) if len(path) > 1 else v0[0]
    vy = (path[-1][1][1] - path[-2][1][1]) / max(path[-1][0] - path[-2][0], 1e-6) if len(path) > 1 else v0[1]
    vz = (path[-1][1][2] - path[-2][1][2]) / max(path[-1][0] - path[-2][0], 1e-6) if len(path) > 1 else -1.0
    hang = t
    dt = 0.01
    p = list(p)
    for _bounce in range(12):
        vz = -vz * 0.35  # a hop off the infield keeps about a third of its drop
        vx, vy = vx * 0.8, vy * 0.8
        if vz < 3:
            break
        while True:
            vz -= G * dt
            q = [p[0] + vx * dt, p[1] + vy * dt, p[2] + vz * dt]
            t += dt
            if math.hypot(q[0], q[1]) >= dist:
                path.append((t, tuple(q)))
                return _finish(path, angle, dist, False, hang)
            if q[2] <= 0:
                q[2] = 0.0
                p = q
                path.append((t, tuple(p)))
                break
            p = q
            path.append((t, tuple(p)))
    # roll (slowing, but never stopping short of where it was fielded)
    ground = math.hypot(vx, vy) or 1.0
    ux, uy = vx / ground, vy / ground
    while math.hypot(p[0], p[1]) < dist and t < 20:
        ground = max(ground - 9 * dt, 15.0)
        p = [p[0] + ux * ground * dt, p[1] + uy * ground * dt, 0.0]
        t += dt
        if len(path) % 4 == 0 or math.hypot(p[0], p[1]) >= dist:
            path.append((t, tuple(p)))
    return _finish(path, angle, dist, False, hang)


def _finish(path, angle, dist, air, hang=None) -> dict:
    step = max(1, -(-len(path) // 80))  # flown every 0.01 s; 80-odd points draw it smoothly
    path = path[::step] + ([path[-1]] if (len(path) - 1) % step else [])
    return {"points": path, "hang": path[-1][0] if hang is None else hang, "landing": path[-1][1],
            "angle": angle, "distance": dist, "air": air}


WALL_H = 8.0                                # the fence as drawn where its height isn't known
WALL_HITS = {"Single", "Double", "Triple"}  # in play, not caught, not out of the park
WALL_SPAN = 3.0                             # degrees either side of where a taller wall is drawn
FARTHER_ROOM = 5.0                          # ft between a catch at the wall and the fence pushed back for it


def wall_contact(hit: dict, fence_at, event: str, height_at=None) -> dict:
    """``hit`` (a :func:`batted_path`) for a play whose result was ``event``, against the fence
    (``fence_at(angle)`` gives its distance, ``height_at(angle)`` its height -- default 8 ft):

    * a hit that wasn't caught and stayed in the park can't have gone through the wall, so its
      flight ends where it met it and drops back down; ``hit["wall"]`` is ``{"angle", "r", "z",
      "height", "taller", "lower"}`` -- where, how high up it met the wall, and when that's above
      the drawn 8 ft (``taller``), how tall a section of wall to draw there for this play;
    * a home run that crossed the fence line lower than 8 ft must have cleared a lower fence: its
      flight is left as it was, and ``hit["wall"]`` has ``lower`` and the ``height`` (a foot or so
      under where it crossed) to draw the fence there for this play;
    * a home run whose distance falls short of the drawn fence (MLB's distance is to where it would
      have come down at field level, so the fence must be nearer than that): ``hit["wall"]`` has
      ``nearer``, the drawn fence's distance (``fence_r``), MLB's (``distance``), and ``r``, where
      to draw the fence for this play -- the last of the flight still a foot over the 8 ft fence;
    * any other ball in play (an out, an error, a fielder's choice: it stayed in the park) that came
      down, or was caught, beyond the drawn fence: the fence there must be farther, so
      ``hit["wall"]`` has ``farther``, the drawn fence's distance (``fence_r``), how far out the ball
      went (``distance``: MLB's, or where it was caught if farther) and ``r``, where to draw the fence
      for this play (``FARTHER_ROOM`` beyond).

    Anything else comes back as it was."""
    height_at = height_at or (lambda _a: WALL_H)
    if hit and event == "Home Run":
        return _cleared(hit, fence_at, height_at)
    if hit and event not in WALL_HITS and event != "Inside The Park Home Run":
        return _kept_in(hit, fence_at, height_at, event)
    if not hit or event not in WALL_HITS:
        return hit
    pts = hit["points"]
    for i in range(1, len(pts)):
        (t0, a), (t1, b) = pts[i - 1], pts[i]
        ang = math.degrees(math.atan2(b[0], b[1]))
        if abs(ang) > 45:
            continue  # foul ground: no fence
        fence = fence_at(ang)
        r0, r1 = math.hypot(a[0], a[1]), math.hypot(b[0], b[1])
        if r1 < fence or r0 >= fence:
            continue
        f = (fence - r0) / (r1 - r0) if r1 > r0 else 1.0
        at = tuple(a[k] + (b[k] - a[k]) * f for k in range(3))
        t = t0 + (t1 - t0) * f
        dt = max(t1 - t0, 1e-6)
        v = [(b[k] - a[k]) / dt for k in range(3)]
        # off the wall: a third of its pace back toward the infield, falling under gravity
        path = pts[:i] + [(t, at)]
        p, vx, vy, vz = list(at), -v[0] * 0.3, -v[1] * 0.3, min(v[2], 0.0) * 0.5
        while p[2] > 0 and t < 20:
            t += 0.02
            vz -= G * 0.02
            p = [p[0] + vx * 0.02, p[1] + vy * 0.02, max(0.0, p[2] + vz * 0.02)]
            path.append((t, tuple(p)))
        z = at[2]
        here = height_at(ang)
        out = dict(hit, points=path, landing=path[-1][1])
        out["wall"] = {"angle": ang, "r": fence, "z": z, "taller": z > here, "lower": False, "event": event,
                       "height": float(max(here, math.ceil(z + 1)))}
        return out
    return hit


def _kept_in(hit: dict, fence_at, height_at, event: str) -> dict:
    """A ball that stayed in the park without hitting the wall as a hit: if it came down (or was
    caught) beyond the drawn fence, the fence there must be farther."""
    ang = hit.get("angle") or 0.0
    if abs(ang) > 45:
        return hit
    reach = max(max((math.hypot(q[0], q[1]) for _t, q in hit["points"]), default=0.0),
                hit.get("fielded") or 0.0)
    fence = fence_at(ang)
    if reach < fence - 1:
        return hit
    return dict(hit, wall={"angle": ang, "r": reach + FARTHER_ROOM, "z": 0.0, "taller": False, "lower": False,
                           "farther": True, "height": height_at(ang), "fence_r": fence, "distance": reach,
                           "caught": bool(hit.get("fielded")) and hit["fielded"] >= reach - 0.5,
                           "event": event})


def _fence_crossing(points, fence_at):
    """Where a flight first reaches the fence: (angle, fence ft, height ft), or None."""
    for (_t0, a), (_t1, b) in zip(points, points[1:]):
        ang = math.degrees(math.atan2(b[0], b[1]))
        if abs(ang) > 45:
            continue
        fence = fence_at(ang)
        r0, r1 = math.hypot(a[0], a[1]), math.hypot(b[0], b[1])
        if r0 < fence <= r1:
            f = (fence - r0) / (r1 - r0)
            return ang, fence, a[2] + (b[2] - a[2]) * f
    return None


def _cleared(hit: dict, fence_at, height_at) -> dict:
    """A home run: if MLB's distance falls short of the drawn fence, how near the fence must be;
    if it crossed the fence line under the drawn 8 ft, how low the fence there must have been
    (half a foot under where it crossed, at least a foot)."""
    dist, ang = hit.get("distance"), hit.get("angle") or 0.0
    if dist and abs(ang) <= 45 and dist < fence_at(ang):
        here = height_at(ang)
        r, z = None, here
        for _t, q in hit["points"]:
            if q[2] >= here + 1:
                r, z = math.hypot(q[0], q[1]), q[2]
        r = min(r if r is not None else dist - 5, dist - 1)
        return dict(hit, wall={"angle": ang, "r": r, "z": z, "taller": False, "lower": False, "nearer": True,
                               "height": here, "fence_r": fence_at(ang), "distance": dist, "event": "Home Run"})
    got = _fence_crossing(hit["points"], fence_at)
    if not got or got[2] >= height_at(got[0]):
        return hit
    ang, fence, z = got
    height = max(1.0, math.floor(max(z - 0.5, 0.0) * 2) / 2)
    return dict(hit, wall={"angle": ang, "r": fence, "z": z, "taller": False, "lower": True, "height": height,
                           "event": "Home Run"})


def wall_text(wall: dict) -> str:
    """What a ``wall_contact`` section says, as the model estimate it is."""
    what = (wall.get("event") or "hit").lower()
    mark = max(1, round(wall["z"]))
    if wall.get("farther"):
        where = "was caught" if wall.get("caught") else "came down"
        return (f"This {what} {where} about {wall['distance']:.0f} ft out and stayed in the park, beyond the "
                f"drawn fence ({wall['fence_r']:.0f} ft, a straight line between the park's posted distances), "
                f"so the fence here must be farther. It's drawn for this play at about {wall['r']:.0f} ft "
                "(an estimate).")
    if wall.get("nearer"):
        return (f"MLB measured this home run at {wall['distance']:.0f} ft, short of the drawn fence "
                f"({wall['fence_r']:.0f} ft), so the fence here must be nearer than {wall['distance']:.0f} ft. "
                f"It's drawn for this play at about {wall['r']:.0f} ft, where the model's flight still clears "
                "an 8 ft fence (an estimate).")
    if wall.get("lower"):
        return (f"Model estimates {what} cleared the fence near the {mark} ft mark (the 8 ft fence is drawn "
                f"lower there, {wall['height']:g} ft, for this play)")
    if wall.get("taller"):
        return (f"Model estimates {what} probably hit the wall at the {mark} ft mark (a {wall['height']:.0f} ft "
                "section of wall is drawn there for this play)")
    return f"Model estimates {what} hit the wall about {mark} ft up"


def swung(pitch: dict) -> bool:
    call = (pitch.get("call") or "").lower()
    return bool(pitch.get("in_play")) or any(w in call for w in ("swinging", "foul", "bunt", "in play"))


# --------------------------------------------------------------------------- #
#  The figures                                                                #
# --------------------------------------------------------------------------- #
def _add(a, b, s=1.0):
    return (a[0] + b[0] * s, a[1] + b[1] * s, a[2] + b[2] * s)


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _len(a):
    return math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2)


def _norm(a):
    n = _len(a) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _elbow(shoulder, hand, upper, fore, pole):
    """Two-bone reach: where the elbow (knee) goes for a limb from ``shoulder`` to ``hand``,
    bending toward ``pole``."""
    d = _sub(hand, shoulder)
    n = min(_len(d), upper + fore - 1e-3)
    axis = _norm(d)
    along = (upper * upper - fore * fore + n * n) / (2 * n) if n > 1e-6 else 0.0
    up = math.sqrt(max(upper * upper - along * along, 0.0))
    side = _sub(pole, _add((0, 0, 0), axis, _dot(pole, axis)))
    side = _norm(side) if _len(side) > 1e-6 else (0.0, 0.0, -1.0)
    return _add(_add(shoulder, axis, along), side, up)


def _limb(segs, a, b, r):
    segs.append((a, b, r))


def pitcher_figure(release: tuple, hand: str, height_in) -> tuple[list, list]:
    """A pitcher at the moment of release, his hand at the release point: (segments [(a, b,
    radius)], balls [(center, radius)]).  A right-hander's arm is on the third-base side."""
    s = (height_in or 75) / 75
    a = 1.0 if hand == "L" else -1.0  # the throwing arm's side, in x
    ry = release[1]
    fy = min(ry + 0.9 * s, RUBBER_Y - 3.0)
    front = (-a * 0.35 * s, fy, mound_height(-a * 0.35 * s, fy))
    back = (a * 0.35 * s, RUBBER_Y + 0.3, mound_height(a * 0.35, RUBBER_Y) + 0.45 * s)
    pelvis = (0.0, fy + 1.45 * s, front[2] + 2.65 * s)
    neck = _add(pelvis, (0.0, -1.0 * s, 1.45 * s))
    shoulder = _add(neck, (a * 0.62 * s, 0.0, -0.15 * s))
    upper, fore = 1.05 * s, 1.45 * s
    gap = _len(_sub(release, shoulder))
    if gap > upper + fore - 0.05:  # lean the body toward a far release
        shift = _add((0, 0, 0), _norm(_sub(release, shoulder)), gap - (upper + fore - 0.05))
        pelvis, neck, shoulder = _add(pelvis, shift, 0.6), _add(neck, shift), _add(shoulder, shift)
    glove_sh = _add(neck, (-a * 0.62 * s, 0.0, -0.15 * s))
    head = _add(neck, (0.0, -0.3 * s, 0.5 * s))
    segs = []
    fknee = _elbow(_add(pelvis, (-a * 0.3 * s, 0, 0)), front, 1.55 * s, 1.55 * s, (0, -1, 0.3))
    bknee = _elbow(_add(pelvis, (a * 0.3 * s, 0, 0)), back, 1.55 * s, 1.55 * s, (a * 0.3, -0.6, -1))
    for hip, knee, foot in ((_add(pelvis, (-a * 0.3 * s, 0, 0)), fknee, front),
                            (_add(pelvis, (a * 0.3 * s, 0, 0)), bknee, back)):
        _limb(segs, hip, knee, 0.27 * s)
        _limb(segs, knee, foot, 0.2 * s)
        _limb(segs, foot, _add(foot, (0, -0.7 * s if foot is front else -0.3 * s, -0.05 * s)), 0.15 * s)
    _limb(segs, pelvis, neck, 0.52 * s)
    _limb(segs, _add(pelvis, (-a * 0.3 * s, 0, 0)), _add(pelvis, (a * 0.3 * s, 0, 0)), 0.36 * s)
    _limb(segs, shoulder, glove_sh, 0.3 * s)
    _limb(segs, neck, head, 0.17 * s)
    elbow = _elbow(shoulder, release, upper, fore, (a, 0.8, -0.3))
    _limb(segs, shoulder, elbow, 0.2 * s)
    _limb(segs, elbow, release, 0.16 * s)
    glove = _add(neck, (-a * 0.3 * s, -0.55 * s, -0.55 * s))
    gelbow = _elbow(glove_sh, glove, 1.05 * s, 1.1 * s, (-a, 0.2, -1))
    _limb(segs, glove_sh, gelbow, 0.2 * s)
    _limb(segs, gelbow, glove, 0.16 * s)
    cap = _add(head, (0, -0.28 * s, 0.12 * s))
    _limb(segs, _add(head, (0, 0, 0.12 * s)), cap, 0.14 * s)  # the cap's bill
    return segs, [(head, 0.38 * s), (glove, 0.3 * s)]


def batter_figure(side: str, height_in, swing: bool, contact: tuple | None) -> tuple[list, list]:
    """A batter in the box: in his stance for a pitch taken, through the zone (the bat at the
    pitch) for one swung at.  A right-handed batter stands on the third-base side (x < 0)."""
    s = (height_in or DEFAULT_HEIGHT) / DEFAULT_HEIGHT
    b = 1.0 if side == "L" else -1.0
    x = b * 2.9
    back_foot, front_foot = (x, 0.1, 0.0), (x + b * 0.05, 0.1 + 2.5 * s, 0.0)
    segs = []
    if not swing:
        pelvis = (x, 0.1 + 1.3 * s, 2.95 * s)
        neck = (x - b * 0.32 * s, 0.1 + 1.15 * s, 4.85 * s)
        head = (x - b * 0.42 * s, 0.1 + 1.35 * s, 5.45 * s)
        front_sh = _add(neck, (0.0, 0.62 * s, -0.12 * s))
        back_sh = _add(neck, (0.0, -0.62 * s, -0.12 * s))
        hands = (x - b * 0.6 * s, 0.1 + 0.55 * s, 4.85 * s)
        bat_dir = _norm((b * 0.15, -0.55, 0.82))
        poles = ((-b * 0.2, 0.3, -1), (b * 0.6, -0.4, -0.4))
    else:
        pelvis = (x - b * 0.15 * s, 0.1 + 1.45 * s, 2.85 * s)
        neck = (x - b * 0.3 * s, 0.1 + 1.0 * s, 4.75 * s)
        head = (x - b * 0.42 * s, 0.1 + 1.2 * s, 5.35 * s)
        front_sh = _add(neck, (b * 0.4 * s, 0.45 * s, 0.0))
        back_sh = _add(neck, (-b * 0.4 * s, -0.45 * s, -0.2 * s))
        c = contact or (0.0, PLATE_FRONT, 2.5)
        hands = (x - b * 1.0 * s, 0.1 + 2.1 * s, max(2.4 * s, min(4.3 * s, c[2] + 0.9)))
        target = (c[0], max(c[1], 0.8), c[2])
        bat_dir = _norm(_sub(target, hands))
        if _dot(bat_dir, (-b, 0, 0)) < 0.2:  # keep the barrel heading across the plate
            bat_dir = _norm(_add(bat_dir, (-b, 0, 0), 0.8))
        poles = ((0, -0.3, -1), (b * 0.3, 0.2, -1))
    hips = (_add(pelvis, (0, 0.3 * s, 0)), _add(pelvis, (0, -0.3 * s, 0)))
    for hip, foot, pole in ((hips[0], front_foot, (-b * 0.6, 0.6, 0)), (hips[1], back_foot, (-b * 0.8, -0.2, 0))):
        knee = _elbow(hip, foot, 1.55 * s, 1.55 * s, pole)
        _limb(segs, hip, knee, 0.27 * s)
        _limb(segs, knee, foot, 0.2 * s)
        _limb(segs, foot, _add(foot, (-b * 0.6 * s, 0.15 * s, 0)), 0.15 * s)
    _limb(segs, hips[0], hips[1], 0.36 * s)
    _limb(segs, pelvis, neck, 0.52 * s)
    _limb(segs, front_sh, back_sh, 0.3 * s)
    _limb(segs, neck, head, 0.17 * s)
    for sh, pole in zip((front_sh, back_sh), poles):
        elbow = _elbow(sh, hands, 1.05 * s, 1.1 * s, pole)
        _limb(segs, sh, elbow, 0.2 * s)
        _limb(segs, elbow, hands, 0.16 * s)
    bat_end = _add(hands, bat_dir, 2.8)
    _limb(segs, _add(hands, bat_dir, -0.25), _add(hands, bat_dir, 1.2), 0.05)  # handle
    _limb(segs, _add(hands, bat_dir, 1.2), bat_end, 0.1)                       # barrel
    _limb(segs, _add(head, (0, 0, 0.05 * s)), _add(head, (b * 0.0, 0.28 * s, 0.05 * s)), 0.15 * s)  # helmet bill
    return segs, [(head, 0.41 * s)]


def catcher_figure(glove: tuple) -> tuple[list, list]:
    """The catcher in his crouch behind the plate, his mitt where the pitch arrives."""
    y = -3.2
    pelvis, neck = (0.0, y, 1.6), (0.0, y + 0.55, 3.15)
    head = (0.0, y + 0.75, 3.7)
    segs = []
    for side in (-1, 1):
        hip, foot = (side * 0.35, y, 1.6), (side * 1.0, y + 0.3, 0.0)
        knee = (side * 1.1, y + 1.2, 1.25)
        _limb(segs, hip, knee, 0.27)
        _limb(segs, knee, foot, 0.2)
    _limb(segs, pelvis, neck, 0.55)
    _limb(segs, (-0.62, y + 0.55, 3.0), (0.62, y + 0.55, 3.0), 0.3)
    _limb(segs, neck, head, 0.17)
    mitt_sh = (-0.62, y + 0.55, 3.0)
    elbow = _elbow(mitt_sh, glove, 1.05, 1.15, (-1, 0, -1))
    _limb(segs, mitt_sh, elbow, 0.2)
    _limb(segs, elbow, glove, 0.16)
    _limb(segs, (0.62, y + 0.55, 3.0), (0.8, y + 0.4, 1.9), 0.18)
    return segs, [(head, 0.42), (glove, 0.36)]


def umpire_figure() -> tuple[list, list]:
    y = -6.0
    pelvis, neck = (0.0, y, 2.9), (0.0, y + 0.6, 4.6)
    head = (0.0, y + 0.8, 5.15)
    segs = []
    for side in (-1, 1):
        hip, foot = (side * 0.35, y, 2.9), (side * 1.1, y - 0.2, 0.0)
        knee = _elbow(hip, foot, 1.55, 1.55, (side, 1, 0))
        _limb(segs, hip, knee, 0.28)
        _limb(segs, knee, foot, 0.21)
    _limb(segs, pelvis, neck, 0.6)
    _limb(segs, (-0.65, y + 0.6, 4.45), (0.65, y + 0.6, 4.45), 0.3)
    _limb(segs, neck, head, 0.17)
    for side in (-1, 1):
        _limb(segs, (side * 0.65, y + 0.6, 4.45), (side * 0.5, y + 1.0, 3.3), 0.18)
    return segs, [(head, 0.4)]


# --------------------------------------------------------------------------- #
#  Colours                                                                     #
# --------------------------------------------------------------------------- #
# Baseball Savant's pitch colours, so a pitch looks the way it does there
PITCH_COLORS = {
    "FF": "#d22d49", "FA": "#d22d49", "SI": "#fe9d00", "FT": "#de6a04", "FC": "#933f2c",
    "SL": "#eee716", "ST": "#ddb33a", "SV": "#93afd4", "CU": "#00d1ed", "KC": "#6236cd",
    "CS": "#0068ff", "CH": "#1dbe3a", "FS": "#3bacac", "FO": "#55ccab", "SC": "#60db33",
    "KN": "#3c44cd", "EP": "#888888", "PO": "#cccccc",
}
OTHER_PITCH = "#bbbbbb"
BALL_COLOR, STRIKE_COLOR, IN_PLAY_COLOR = "#2e9d4f", "#d23c3c", "#3b7dd8"
HIT_COLOR = "#ffffff"


def faded(color: QColor, amount: float = 0.5) -> QColor:
    """``color`` washed toward grey -- opaque, since a translucent wide line draws far slower."""
    g = 150
    return QColor(int(color.red() + (g - color.red()) * amount), int(color.green() + (g - color.green()) * amount),
                  int(color.blue() + (g - color.blue()) * amount))


def pitch_color(code: str) -> QColor:
    return QColor(PITCH_COLORS.get(code or "", OTHER_PITCH))


def call_color(pitch: dict) -> QColor:
    return QColor(IN_PLAY_COLOR if pitch.get("in_play") else BALL_COLOR if pitch.get("ball")
                  else STRIKE_COLOR if pitch.get("strike") else "#888888")


SCENES = {  # light (day), dark (night)
    "sky": (("#8fb9e0", "#dfeaf3"), ("#0b1424", "#22324a")),
    "foul": ("#5a8f4a", "#2c4a27"), "grass": ("#6aa857", "#36592f"), "infield": ("#73b25f", "#3a6033"),
    "dirt": ("#c49a6c", "#7a5d40"), "chalk": ("#ffffff", "#e8e8e8"), "fence": ("#2f5b3a", "#1d3424"),
    "player": ("#1a1d22", "#05070a"), "extra": ("#4a525c", "#262d36"), "zone": ("#ffffff", "#e6e6e6"),
    "shadow": ("#3f5a35", "#141f12"),
}


# --------------------------------------------------------------------------- #
#  The view                                                                    #
# --------------------------------------------------------------------------- #
PRESETS = {  # target, yaw (0 = from behind home plate looking out), elevation, distance, field of view
    # television's center-field camera: some 400 ft out on a long lens, just off the pitcher's line
    "Broadcast": ((0.0, 12.0, 3.6), 184.5, 3.4, 420.0, 4.2),
    "Catcher": ((0.0, 22.0, 3.0), 0.0, 3.5, 42.0, 38.0),
    "Side": ((0.0, 30.0, 3.4), 90.0, 6.0, 64.0, 38.0),
    "Overhead": ((0.0, 30.0, 0.0), 0.0, 89.0, 92.0, 38.0),
    "Field": ((0.0, 165.0, 0.0), 0.0, 26.0, 360.0, 38.0),
}
NEAR = 0.3


class StatcastView(QWidget):
    """The diorama.  ``set_at_bat`` gives it a plate appearance and the pitch to show;
    ``stepPitch``/``stepAtBat`` ask the tab to move on (the arrow keys)."""
    stepPitch = pyqtSignal(int)
    stepAtBat = pyqtSignal(int)
    animationFinished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(480, 320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(False)
        self._preset = "Broadcast"
        self._target, self._yaw, self._elev, self._dist, self._fov = PRESETS[self._preset]
        self._drag = None
        self._play = None
        self._index = 0
        self._earlier = True
        self._message = ""
        self._explore = False    # the message sits at the top: the park is there to look round
        self._fence = None       # the park's fence ((angle, ft), ...), or None for the generic one
        self._walls = ()         # sections of it whose height is known ({"from", "to", "height", ...})
        self._field = None       # the field's flat layers (built once a theme is known)
        self._field_dark = None
        self._dynamic = None     # the figures and arcs for the at-bat on show
        self._anim_timer = QTimer(self, interval=16)
        self._anim_timer.timeout.connect(self.update)
        self._anim_clock = QElapsedTimer()
        self._anim_speed = 1.0
        self._anim_total = 0.0

    # ---- what to show ------------------------------------------------------------------
    def set_message(self, text: str, explore: bool = False):
        """Nothing to fly, and why; ``explore``: the message goes to the top, out of the way of
        the park, which is still there to look round."""
        self._message = text
        self._explore = explore
        self._play = None
        self._dynamic = None
        self.stop()
        self.update()

    def set_at_bat(self, play: dict | None, index: int, earlier: bool):
        """``play`` from ``mlbstats.parse_gameday``; ``index`` the pitch on show in
        ``play["pitches"]``; ``earlier``: the at-bat's earlier pitches stay in view, faded."""
        self.stop()
        self._play, self._index, self._earlier = play, index, earlier
        self._message = ""
        self._dynamic = None
        self.update()

    def set_park(self, fence, walls=()):
        """The park's fence as ``((angle, ft), ...)`` from line to line (``stadiums``), or None for
        the generic 330-400 ft one; ``walls`` the sections whose height is known (8 ft elsewhere)."""
        walls = tuple(walls or ())
        if fence != self._fence or walls != self._walls:
            self._fence = fence
            self._walls = walls
            self._field = None
            self._dynamic = None
            self.update()

    def current_wall(self) -> dict | None:
        """The ``wall_contact`` of the batted ball on show, if it met (or cleared) the fence."""
        dyn = self._scene()
        hit = ((dyn or {}).get("current") or {}).get("hit") or {}
        return hit.get("wall")

    def fence_at(self, angle: float) -> float:
        return fence_distance(angle) if self._fence is None else naturalzone.fence_at(self._fence, angle)

    def wall_at(self, angle: float) -> float:
        """The fence's height (ft) at ``angle``: a known section's, else 8 ft."""
        for w in self._walls:
            if w["from"] <= angle <= w["to"]:
                return w["height"]
        return WALL_H

    def set_preset(self, name: str):
        self._preset = name
        self._target, self._yaw, self._elev, self._dist, self._fov = PRESETS[name]
        self.update()

    def preset(self) -> str:
        return self._preset

    # ---- animation ---------------------------------------------------------------------
    def play(self, speed: float = 1.0):
        """Fly the pitch on show (and its batted ball) at ``speed`` x real time."""
        dyn = self._scene()
        current = dyn.get("current") if dyn else None
        if not current:
            return
        self._anim_speed = speed
        self._anim_total = current["pitch_time"] + (current["hit"]["points"][-1][0] + 0.4 if current["hit"] else 0.25)
        self._anim_clock.start()
        self._anim_timer.start()
        self.update()

    def stop(self):
        if self._anim_timer.isActive():
            self._anim_timer.stop()
            self.update()

    def playing(self) -> bool:
        return self._anim_timer.isActive()

    def _anim_time(self):
        """Seconds of flight shown so far, or None when not animating."""
        if not self._anim_timer.isActive():
            return None
        t = self._anim_clock.elapsed() / 1000.0 * self._anim_speed
        if t >= self._anim_total:
            self._anim_timer.stop()
            self.animationFinished.emit()
            return None
        return t

    # ---- scene building ----------------------------------------------------------------
    def _dark(self) -> bool:
        return self.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def _c(self, key, alpha=255) -> QColor:
        c = QColor(SCENES[key][1 if self._dark() else 0])
        c.setAlpha(alpha)
        return c

    def _build_field(self):
        """The field's flat layers, drawn in order: [(points, fill QColor, pen QColor|None, width)],
        and the standing parts (the fence, the mound's slopes) sorted with everything else."""
        flat, standing = [], []

        def arc(cx, cy, r, a0, a1, n=48, z=0.0):
            return [(cx + r * math.sin(math.radians(a0 + (a1 - a0) * i / n)),
                     cy + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)), z) for i in range(n + 1)]
        flat.append(([(-900, -400, 0), (900, -400, 0), (900, 1000, 0), (-900, 1000, 0)], self._c("foul"), None, 0))
        fence_distance = self.fence_at  # the park's, or the generic one
        outfield = [(0.0, 0.0, 0.0)] + [(fence_distance(a) * math.sin(math.radians(a)),
                                          fence_distance(a) * math.cos(math.radians(a)), 0.0)
                                         for a in [-45 + 90 * i / 90 for i in range(91)]]
        flat.append((outfield, self._c("grass"), None, 0))
        # the infield dirt: 95 ft around the mound between the lines
        dirt = [(0.0, 0.0, 0.0)] + [p for p in arc(0, RUBBER_Y, 95, -62, 62, 40)]
        dirt = [(0.0, 0.0, 0.0), (-67.2, 67.2, 0.0)] + [p for p in dirt[1:] if abs(p[0]) <= p[1]] + [(67.2, 67.2, 0.0)]
        flat.append((dirt, self._c("dirt"), None, 0))
        corner = BASE / math.sqrt(2)
        inset = 0.8
        cy = corner
        grass = [(0.0, cy - corner * inset, 0.0), (corner * inset, cy, 0.0), (0.0, cy + corner * inset, 0.0),
                 (-corner * inset, cy, 0.0)]
        flat.append((grass, self._c("infield"), None, 0))
        flat.append((arc(0, 0, 13, 0, 360, 40), self._c("dirt"), None, 0))
        flat.append((arc(0, MOUND_Y, MOUND_R, 0, 360, 40), self._c("dirt"), None, 0))
        chalk = self._c("chalk")

        def line(a, b, width=0.25):  # a chalk line: a thin strip on the ground
            d = _norm((b[0] - a[0], b[1] - a[1], 0.0))
            n = (-d[1] * width / 2, d[0] * width / 2, 0.0)
            flat.append(([_add(a, n), _add(b, n), _sub(b, n), _sub(a, n)], chalk, None, 0))
        for sx in (-1, 1):  # the foul lines and poles
            end = fence_distance(45 * sx)
            far = (sx * end / math.sqrt(2), end / math.sqrt(2), 0.0)
            line((sx * 0.1, 0.1, 0.0), far, 0.33)
            standing.append(("seg", far, _add(far, (0, 0, 45)), 0.6, QColor("#f2d23a")))
        # the batter's boxes
        for sx in (-1, 1):
            x0, x1 = sx * (PLATE_HALF + 0.5), sx * (PLATE_HALF + 0.5 + 4)
            box = [(x0, -3.0, 0.0), (x1, -3.0, 0.0), (x1, 3.0, 0.0), (x0, 3.0, 0.0)]
            for a, b in zip(box, box[1:] + box[:1]):
                line(a, b, 0.25)
        flat.append(([(-PLATE_HALF, PLATE_FRONT, 0), (PLATE_HALF, PLATE_FRONT, 0), (PLATE_HALF, 8.5 / 12, 0),
                      (0, 0, 0), (-PLATE_HALF, 8.5 / 12, 0)], QColor("#f4f4f0"), None, 0))
        for bx, by in ((corner, corner), (0, 2 * corner), (-corner, corner)):  # bases, 15 in
            h = 0.625
            flat.append(([(bx - h, by, 0), (bx, by + h, 0), (bx + h, by, 0), (bx, by - h, 0)], QColor("#f4f4f0"), None, 0))
        # the mound: a low cone, its top flat
        ring = 36
        for i in range(ring):
            a0, a1 = 360 * i / ring, 360 * (i + 1) / ring
            o0, o1 = arc(0, MOUND_Y, MOUND_R, a0, a1, 1)
            i0, i1 = arc(0, MOUND_Y, MOUND_TOP_R, a0, a1, 1, MOUND_H)
            facing = math.cos(math.radians((a0 + a1) / 2 - 200))  # lit from behind the plate's left
            shade = self._c("dirt").lighter(int(100 + 12 * facing))
            standing.append(("mound", [o0, o1, i1, i0], shade))
        standing.append(("mound", arc(0, MOUND_Y, MOUND_TOP_R, 0, 360, 24, MOUND_H), self._c("dirt").lighter(104)))
        standing.append(("mound", [(-1.0, RUBBER_Y, MOUND_H + 0.02), (1.0, RUBBER_Y, MOUND_H + 0.02),
                                   (1.0, RUBBER_Y + 0.5, MOUND_H + 0.02), (-1.0, RUBBER_Y + 0.5, MOUND_H + 0.02)],
                         QColor("#f4f4f0")))
        # the outfield fence: 8 ft, or a section's known height (its ends, and a step where the height changes)
        steps = 60
        cuts = sorted({-45 + 90 * i / steps for i in range(steps + 1)}
                      | {e for w in self._walls for e in (w["from"], w["to"]) if -45 <= e <= 45})
        for a0, a1 in zip(cuts, cuts[1:]):
            h = self.wall_at((a0 + a1) / 2)
            p0 = (fence_distance(a0) * math.sin(math.radians(a0)), fence_distance(a0) * math.cos(math.radians(a0)), 0.0)
            p1 = (fence_distance(a1) * math.sin(math.radians(a1)), fence_distance(a1) * math.cos(math.radians(a1)), 0.0)
            standing.append(("fence", (a0, a1), [p0, p1, _add(p1, (0, 0, h)), _add(p0, (0, 0, h))],
                             self._c("fence")))
        self._field = (flat, standing)
        self._field_dark = self._dark()

    def _scene(self):
        """The figures and arcs for the at-bat on show (built when first drawn)."""
        if self._dynamic is not None or self._play is None:
            return self._dynamic
        play = self._play
        pitches = play["pitches"]
        shown = [i for i in range(len(pitches)) if i <= self._index and (self._earlier or i == self._index)]
        arcs = []
        current = None
        for i in shown:
            pitch = pitches[i]
            track = pitch.get("track")
            if not track:
                continue
            contact = None
            hit = None
            if pitch.get("in_play") and play.get("hit") and i == len(pitches) - 1:
                contact = (track["px"], PLATE_FRONT, track["pz"])
                hit = wall_contact(batted_path(play["hit"], contact), self.fence_at, play.get("event") or "",
                                   self.wall_at)
            end = PLATE_FRONT if hit else -1.2
            path = pitch_path(track, end)
            plate_t = path[-1][0] if hit else pitch_path(track, PLATE_FRONT)[-1][0]
            entry = {"i": i, "pitch": pitch, "track": track, "path": path, "hit": hit,
                     "pitch_time": path[-1][0], "plate_time": plate_t}
            arcs.append(entry)
            if i == self._index:
                current = entry
        pitch = pitches[self._index] if 0 <= self._index < len(pitches) else None
        figures, extras = [], []
        if current:
            release = release_point(current["track"])
            figures.append(pitcher_figure(release, play.get("pitch_hand"), play.get("pitcher_height")))
            glove = current["path"][-1][1] if not current["hit"] else (current["track"]["px"] * 0.6, -2.4, 2.3)
            extras.append(catcher_figure(glove))
        else:
            figures.append(pitcher_figure((-1.8 if play.get("pitch_hand") != "L" else 1.8, 54.5, 5.8),
                                          play.get("pitch_hand"), play.get("pitcher_height")))
            extras.append(catcher_figure((0.0, -2.4, 2.4)))
        contact = (current["track"]["px"], PLATE_FRONT, current["track"]["pz"]) if current else None
        figures.append(batter_figure(play.get("bat_side"), play.get("batter_height"),
                                     bool(pitch and swung(pitch)), contact))
        extras.append(umpire_figure())
        track = (current or (arcs[-1] if arcs else {})).get("track") if (current or arcs) else None
        zone = (track.get("sz_bot") or 1.6, track.get("sz_top") or 3.4) if track else (1.6, 3.4)
        self._dynamic = {"arcs": arcs, "current": current, "figures": figures, "extras": extras, "zone": zone}
        return self._dynamic

    # ---- camera ------------------------------------------------------------------------
    def _camera(self):
        yaw, el = math.radians(self._yaw), math.radians(self._elev)
        tx, ty, tz = self._target
        pos = (tx + self._dist * math.cos(el) * math.sin(yaw),
               ty - self._dist * math.cos(el) * math.cos(yaw),
               tz + self._dist * math.sin(el))
        fwd = _norm(_sub(self._target, pos))
        right = _cross(fwd, (0.0, 0.0, 1.0))
        right = _norm(right) if _len(right) > 1e-6 else (math.cos(yaw), math.sin(yaw), 0.0)
        up = _cross(right, fwd)
        focal = 0.5 * self.height() / math.tan(math.radians(self._fov / 2))
        return pos, right, up, fwd, focal

    def _to_cam(self, p, cam):
        pos, right, up, fwd, _f = cam
        d = (p[0] - pos[0], p[1] - pos[1], p[2] - pos[2])
        return (d[0] * right[0] + d[1] * right[1] + d[2] * right[2],
                d[0] * up[0] + d[1] * up[1] + d[2] * up[2],
                d[0] * fwd[0] + d[1] * fwd[1] + d[2] * fwd[2])

    def _screen(self, c, cam):
        f = cam[4]
        return QPointF(self.width() / 2 + f * c[0] / c[2], self.height() / 2 - f * c[1] / c[2])

    def _clip_poly(self, cs):
        """Clip a polygon (camera coordinates) to in front of the near plane."""
        out = []
        n = len(cs)
        for i in range(n):
            a, b = cs[i], cs[(i + 1) % n]
            ina, inb = a[2] >= NEAR, b[2] >= NEAR
            if ina:
                out.append(a)
            if ina != inb:
                t = (NEAR - a[2]) / (b[2] - a[2])
                out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, NEAR))
        return out

    def _clip_seg(self, a, b):
        if a[2] < NEAR and b[2] < NEAR:
            return None
        if a[2] < NEAR:
            t = (NEAR - a[2]) / (b[2] - a[2])
            a = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, NEAR)
        elif b[2] < NEAR:
            t = (NEAR - b[2]) / (a[2] - b[2])
            b = (b[0] + (a[0] - b[0]) * t, b[1] + (a[1] - b[1]) * t, NEAR)
        return a, b

    # ---- painting ----------------------------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self.height()
        sky = SCENES["sky"][1 if self._dark() else 0]
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0, QColor(sky[0]))
        grad.setColorAt(1, QColor(sky[1]))
        p.fillRect(self.rect(), QBrush(grad))
        if self._field is None or self._field_dark != self._dark():
            self._build_field()
            self._dynamic = None
        cam = self._camera()
        f = cam[4]
        flat, standing = self._field
        dyn = self._scene()
        anim_t = self._anim_time()

        # the flat field, in order
        p.setPen(Qt.PenStyle.NoPen)
        for pts, fill, _pen, _width in flat + self._pushed_back_grass(dyn):
            cs = self._clip_poly([self._to_cam(q, cam) for q in pts])
            if len(cs) >= 3:
                p.setBrush(fill)
                p.drawPolygon(QPolygonF([self._screen(c, cam) for c in cs]))

        items = []  # the field's standing parts: ("seg", a, b, radius, color) or ("poly", points, color, is_mound)
        gap = self._fence_gap(dyn)  # this play's lower section of fence, which replaces the fence there
        for kind, *rest in standing:
            if kind == "seg":
                a, b, r, color = rest
                items.append(("seg", a, b, r, color))
            elif kind == "fence":
                (a0, a1), pts, color = rest
                if not (gap and a1 > gap[0] and a0 < gap[1]):
                    items.append(("poly", pts, color, False))
            else:
                items.append(("poly", rest[0], rest[1], kind == "mound"))
        mound_items = [it for it in items if it[0] == "poly" and it[3]]
        other_static = [it for it in items if not (it[0] == "poly" and it[3])]

        # the mound first (it is low, under everything standing on it), back to front
        mounds = []
        for it in mound_items:
            cs = self._clip_poly([self._to_cam(q, cam) for q in it[1]])
            if len(cs) >= 3:
                mounds.append((sum(c[2] for c in cs) / len(cs), cs, it[2]))
        p.setPen(Qt.PenStyle.NoPen)
        for _d, cs, color in sorted(mounds, key=lambda m: -m[0]):
            p.setBrush(color)
            p.drawPolygon(QPolygonF([self._screen(c, cam) for c in cs]))
        if dyn:  # the flights' shadows on the ground, under everything standing
            self._paint_shadows(p, dyn, cam, anim_t)

        draw: list = []  # (depth, callable)
        labels: list = []

        def add_poly(pts, color, pen=None):
            cs = self._clip_poly([self._to_cam(q, cam) for q in pts])
            if len(cs) >= 3:
                poly = QPolygonF([self._screen(c, cam) for c in cs])
                depth = sum(c[2] for c in cs) / len(cs)
                draw.append((depth, lambda poly=poly, color=color, pen=pen: (
                    p.setPen(pen if pen else Qt.PenStyle.NoPen), p.setBrush(color), p.drawPolygon(poly))))

        def add_seg(a, b, color, radius=None, px=2.0, depth_bias=0.0):
            seg = self._clip_seg(self._to_cam(a, cam), self._to_cam(b, cam))
            if not seg:
                return
            mid = (seg[0][2] + seg[1][2]) / 2
            width = max(1.0, 2 * radius * f / mid) if radius else px
            pa, pb = self._screen(seg[0], cam), self._screen(seg[1], cam)
            pen = QPen(color, width, cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin)
            draw.append((mid + depth_bias, lambda pa=pa, pb=pb, pen=pen: (p.setPen(pen), p.drawLine(pa, pb))))

        def draw_lines(pen, lines):
            # (one drawLine each: Qt's drawLines/drawPolyline stroke the run as a single path,
            # many times slower with a wide antialiased pen)
            p.setPen(pen)
            for line in lines:
                p.drawLine(line)

        def add_path(pts, color, px, chunk=6):
            """A flight in short runs of segments, each run sorted on its own depth (so the flight
            passes properly in front of and behind the figures) and drawn in one call."""
            pen = QPen(color, px, cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin)
            cs = [self._to_cam(q, cam) for q in pts]
            for start in range(0, len(cs) - 1, chunk):
                lines, depths = [], []
                for a, b in zip(cs[start:start + chunk], cs[start + 1:start + chunk + 1]):
                    seg = self._clip_seg(a, b)
                    if seg:
                        lines.append(QLineF(self._screen(seg[0], cam), self._screen(seg[1], cam)))
                        depths.append((seg[0][2] + seg[1][2]) / 2)
                if lines:
                    draw.append((sum(depths) / len(depths),
                                 lambda lines=lines, pen=pen: draw_lines(pen, lines)))

        def add_ball(center, radius, fill, outline=None, min_px=0.0, label=None, label_color=None):
            c = self._to_cam(center, cam)
            if c[2] < NEAR:
                return
            r = max(radius * f / c[2], min_px)
            pt = self._screen(c, cam)

            def go(pt=pt, r=r, fill=fill, outline=outline):
                p.setPen(QPen(outline, 1.5) if outline is not None else Qt.PenStyle.NoPen)
                p.setBrush(fill)
                p.drawEllipse(pt, r, r)
            draw.append((c[2], go))
            if label:
                labels.append((QPointF(pt.x() + r + 2, pt.y() - r - 1), label, label_color or QColor("#ffffff")))

        for it in other_static:
            if it[0] == "seg":
                add_seg(it[1], it[2], it[4], radius=it[3])
            else:
                add_poly(it[1], it[2])

        if dyn:
            self._paint_dynamic(p, dyn, cam, anim_t, add_seg, add_path, add_ball, add_poly, labels)

        for _d, go in sorted(draw, key=lambda d: -d[0]):
            go()

        font = QFont(self.font())
        font.setBold(True)
        p.setFont(font)
        fm = QFontMetrics(font)
        for pt, text, color in labels:
            rect = QRectF(pt.x(), pt.y() - fm.ascent(), fm.horizontalAdvance(text) + 6, fm.height())
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 140))
            p.drawRoundedRect(rect.adjusted(-2, -1, 0, 1), 3, 3)
            p.setPen(color)
            p.drawText(rect.adjusted(1, 0, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

        p.setFont(self.font())
        self._paint_overlay(p, dyn)
        if anim_t is None and self._anim_timer.isActive():
            self._anim_timer.stop()
        p.end()

    def _paint_dynamic(self, p, dyn, cam, anim_t, add_seg, add_path, add_ball, add_poly, labels):
        zone_c = self._c("zone")
        # the strike zone at the front of the plate: a 3x3 grid, and the shadow zones outside it
        bot, top = dyn["zone"]
        xs = [-PLATE_HALF + 2 * PLATE_HALF * i / 3 for i in range(4)]
        zs = [bot + (top - bot) * i / 3 for i in range(4)]
        y = PLATE_FRONT
        faint = QColor(zone_c)
        faint.setAlpha(40)
        add_poly([(xs[0], y, zs[0]), (xs[3], y, zs[0]), (xs[3], y, zs[3]), (xs[0], y, zs[3])], faint)
        line = QColor(zone_c)
        for x in xs:
            add_seg((x, y, zs[0]), (x, y, zs[3]), line, px=1.4 if x in (xs[0], xs[3]) else 1.0, depth_bias=-0.01)
        for z in zs:
            add_seg((xs[0], y, z), (xs[3], y, z), line, px=1.4 if z in (zs[0], zs[3]) else 1.0, depth_bias=-0.01)
        outer = faded(QColor(zone_c), 0.45)
        ox, oz = PLATE_HALF * 1.6, (top - bot) * 0.3
        rect = [(-ox, y, bot - oz), (ox, y, bot - oz), (ox, y, top + oz), (-ox, y, top + oz)]
        for a, b in zip(rect, rect[1:] + rect[:1]):
            add_seg(a, b, outer, px=1.0, depth_bias=-0.01)
        add_seg((0, y, bot - oz), (0, y, bot), outer, px=1.0)
        add_seg((0, y, top), (0, y, top + oz), outer, px=1.0)
        add_seg((-ox, y, (bot + top) / 2), (-PLATE_HALF, y, (bot + top) / 2), outer, px=1.0)
        add_seg((PLATE_HALF, y, (bot + top) / 2), (ox, y, (bot + top) / 2), outer, px=1.0)

        player = self._c("player")
        extra = self._c("extra")  # opaque: a translucent wide stroke draws many times slower
        eye, fwd = cam[0], cam[3]
        for figs, color in ((dyn["figures"], player), (dyn["extras"], extra)):
            for segs, balls in figs:
                to_head = _sub(balls[0][0], eye)
                if figs is dyn["extras"] and _len(to_head) < 30 and _dot(_norm(to_head), fwd) > 0.93:
                    continue  # the catcher or umpire the camera is looking through
                for a, b, r in segs:
                    add_seg(a, b, color, radius=r)
                for center, r in balls:
                    add_ball(center, r, color)

        current = dyn["current"]
        for arc in dyn["arcs"]:
            is_current = arc is current
            color = pitch_color(arc["pitch"].get("code"))
            if not is_current:
                color = faded(color)
            limit = anim_t if (is_current and anim_t is not None) else None
            pts = [q for t, q in arc["path"] if limit is None or t <= limit]
            add_path(pts, color, 3.2 if is_current else 2.0)
            tr = arc["track"]
            reached = limit is None or limit >= arc["plate_time"]
            if reached:
                n = arc["pitch"].get("n")
                add_ball((tr["px"], PLATE_FRONT, tr["pz"]), BALL_R, color, call_color(arc["pitch"]),
                         min_px=6 if is_current else 4.5,
                         label=str(n) if n is not None and is_current else None)
            if is_current and limit is not None and limit <= arc["pitch_time"]:
                ball = self._point_at(arc["path"], limit)
                add_ball(ball, BALL_R, QColor("#ffffff"), QColor("#202020"), min_px=4.5)
            hit = arc["hit"]
            wall = (hit or {}).get("wall") or {}
            if is_current and (wall.get("taller") or wall.get("lower") or wall.get("nearer") or wall.get("farther")):
                self._paint_wall(wall, add_poly, add_seg, add_ball)
            if hit:
                hlimit = None if limit is None else limit - arc["pitch_time"]
                if hlimit is not None and hlimit < 0:
                    continue
                hpts = [q for t, q in hit["points"] if hlimit is None or t <= hlimit]
                hc = QColor(HIT_COLOR if is_current else "#cccccc")
                add_path(hpts, hc, 3.2 if is_current else 2.0, chunk=10)
                if hlimit is not None and hlimit <= hit["points"][-1][0]:
                    add_ball(self._point_at(hit["points"], hlimit), BALL_R, QColor("#ffffff"), QColor("#202020"),
                             min_px=4.5)
                if hlimit is None:
                    land = hit["landing"]
                    ring = [(land[0] + 4 * math.cos(math.radians(a)), land[1] + 4 * math.sin(math.radians(a)), 0.05)
                            for a in range(0, 361, 20)]
                    add_path(ring, hc, 2.0, chunk=len(ring))
                    if is_current:
                        hd = self._play.get("hit") or {}
                        bits = [f"{hd['ev']:.1f} mph" if hd.get("ev") is not None else "",
                                f"{hd['la']:.0f}°" if hd.get("la") is not None else "",
                                f"{hit['distance']:.0f} ft"]
                        add_ball(land, 0.01, QColor(0, 0, 0, 0), label=" · ".join(b for b in bits if b))

    def _pushed_back_grass(self, dyn) -> list:
        """The outfield grass out to this play's fence where it's pushed back (a flat layer), or []."""
        wall = (((dyn or {}).get("current") or {}).get("hit") or {}).get("wall") or {}
        if not wall.get("farther"):
            return []
        lo, hi = self._fence_gap(dyn)
        angles = [lo + (hi - lo) * i / 12 for i in range(13)]
        at = lambda r, a: (r * math.sin(math.radians(a)), r * math.cos(math.radians(a)), 0.0)
        pts = [at(wall["r"], a) for a in angles] + [at(self.fence_at(a), a) for a in reversed(angles)]
        return [(pts, self._c("grass"), None, 0)]

    def _fence_gap(self, dyn):
        """The angles (from, to) of fence this play's lower section stands in for: whole pieces of
        the drawn fence (60 across the field) around where the home run crossed, or None."""
        hit = ((dyn or {}).get("current") or {}).get("hit") or {}
        wall = hit.get("wall") or {}
        if not (wall.get("lower") or wall.get("nearer") or wall.get("farther")):
            return None
        step = 90 / 60
        lo = -45 + step * math.floor((wall["angle"] - WALL_SPAN + 45) / step)
        hi = -45 + step * math.ceil((wall["angle"] + WALL_SPAN + 45) / step)
        return max(-45.0, lo), min(45.0, hi)

    def _paint_wall(self, wall, add_poly, add_seg, add_ball):
        """A section of wall as tall (or as low) as this play shows it must be, where the ball met
        (or cleared) it: the fence's colour, lighter, outlined, labelled as for this play."""
        h, ang = wall["height"], wall["angle"]
        n = 6
        if wall.get("lower") or wall.get("nearer") or wall.get("farther"):  # exactly the fence pieces it replaces
            lo, hi = self._fence_gap(self._dynamic)
            angles = [lo + (hi - lo) * i / n for i in range(n + 1)]
        else:
            angles = [ang - WALL_SPAN + 2 * WALL_SPAN * i / n for i in range(n + 1)]
        moved = wall.get("nearer") or wall.get("farther")
        radius = (lambda a: wall["r"]) if moved else self.fence_at
        foot = [(radius(a) * math.sin(math.radians(a)), radius(a) * math.cos(math.radians(a)), 0.0) for a in angles]
        fill = self._c("fence").lighter(150)
        fill.setAlpha(225)
        edge = QColor("#f2d23a")
        for a, b in zip(foot, foot[1:]):
            add_poly([a, b, _add(b, (0, 0, h)), _add(a, (0, 0, h))], fill)
            add_seg(_add(a, (0, 0, h)), _add(b, (0, 0, h)), edge, px=2.0, depth_bias=-0.5)
        for a in (foot[0], foot[-1]):
            add_seg(a, _add(a, (0, 0, h)), edge, px=2.0, depth_bias=-0.5)
        if moved:  # side walls back to the drawn fence, either end
            for a, f in ((angles[0], foot[0]), (angles[-1], foot[-1])):
                out = (self.fence_at(a) * math.sin(math.radians(a)), self.fence_at(a) * math.cos(math.radians(a)), 0.0)
                add_poly([f, out, _add(out, (0, 0, self.wall_at(a))), _add(f, (0, 0, h))], fill)
        top = foot[n // 2]
        text = (f"fence < {wall['distance']:.0f} ft (MLB distance)" if wall.get("nearer")  # in full: wall_text
                else f"fence > {wall['distance']:.0f} ft (stayed in the park)" if wall.get("farther")
                else f"\u2248 {max(1, round(wall['z']))} ft (model estimate)")
        add_ball(_add(top, (0, 0, max(h, self.wall_at(ang)) + 14)), 0.01, QColor(0, 0, 0, 0), label=text,
                 label_color=edge)

    def _paint_shadows(self, p, dyn, cam, anim_t):
        """The pitch on show and its batted ball, traced on the ground beneath them."""
        current = dyn["current"]
        if not current:
            return
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(self._c("shadow"), 2.0, cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin))
        paths = [(current["path"], anim_t)]
        if current["hit"]:
            paths.append((current["hit"]["points"], None if anim_t is None else anim_t - current["pitch_time"]))
        for path, limit in paths:
            if limit is not None and limit < 0:
                continue
            ground = [(q[0], q[1], mound_height(q[0], q[1]) + 0.03) for t, q in path if limit is None or t <= limit]
            for a, b in zip(ground, ground[1:]):
                seg = self._clip_seg(self._to_cam(a, cam), self._to_cam(b, cam))
                if seg:
                    p.drawLine(self._screen(seg[0], cam), self._screen(seg[1], cam))

    @staticmethod
    def _point_at(path, t):
        for (t0, a), (t1, b) in zip(path, path[1:]):
            if t1 >= t:
                f = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
                return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)
        return path[-1][1]

    def _paint_overlay(self, p, dyn):
        """The message (no data), the catcher's-view inset of the zone, and the mouse hint."""
        w, h = self.width(), self.height()
        text_color = QColor("#ffffff") if self._dark() else QColor("#1a1d22")
        if self._message:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            box = QRectF(w * 0.12, 10, w * 0.76, 64) if self._explore else QRectF(w * 0.12, h * 0.38, w * 0.76, h * 0.24)
            p.drawRoundedRect(box, 8, 8)
            p.setPen(QColor("#ffffff"))
            p.drawText(box.adjusted(14, 8, -14, -8), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       self._message)
        hint = "Drag to rotate · right-drag to pan · wheel to zoom · ←/→ pitch · ↑/↓ at-bat · double-click resets"
        small = QFont(self.font())
        small.setPointSizeF(small.pointSizeF() * 0.85)
        p.setFont(small)
        p.setPen(QColor(255, 255, 255, 200) if self._dark() else QColor(20, 30, 40, 190))
        p.drawText(QRectF(8, h - 22, w - 16, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, hint)
        if not dyn or not dyn["arcs"]:
            return
        # the inset: the zone as the catcher sees it, every pitch shown so far
        bot, top = dyn["zone"]
        size = max(120, min(190, int(h * 0.36)))
        box = QRectF(w - size - 10, 10, size, size * 1.15)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 120) if self._dark() else QColor(255, 255, 255, 170))
        p.drawRoundedRect(box, 6, 6)
        p.setPen(text_color)
        p.drawText(box.adjusted(0, 3, 0, 0), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, "Catcher's view")
        span_x, z_lo, z_hi = 2.2, -0.6, 5.0  # feet shown: +-2.2 ft across, from just under the ground up
        area = box.adjusted(10, 20, -10, -8)
        sc = min(area.width() / (2 * span_x), area.height() / (z_hi - z_lo))
        cx, base = area.center().x(), area.bottom()

        def at(x, z, clamp=False):
            if clamp:  # a pitch way out of the zone sits at the inset's edge
                x, z = max(-span_x, min(span_x, x)), max(z_lo, min(z_hi, z))
            return QPointF(cx + x * sc, base - (z - z_lo) * sc)
        side = (self._play or {}).get("bat_side")
        if side in ("L", "R"):
            p.drawText(QRectF(area.left(), area.top(), area.width(), 16),
                       (Qt.AlignmentFlag.AlignLeft if side == "R" else Qt.AlignmentFlag.AlignRight)
                       | Qt.AlignmentFlag.AlignVCenter, " RHB " if side == "R" else " LHB ")
        p.setPen(QPen(text_color, 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(at(-PLATE_HALF, top), at(PLATE_HALF, bot)))
        thin = QColor(text_color)
        thin.setAlpha(110)
        p.setPen(QPen(thin, 1))
        for i in (1, 2):
            x = -PLATE_HALF + 2 * PLATE_HALF * i / 3
            p.drawLine(at(x, bot), at(x, top))
            z = bot + (top - bot) * i / 3
            p.drawLine(at(-PLATE_HALF, z), at(PLATE_HALF, z))
        p.drawLine(at(-span_x, 0), at(span_x, 0))  # the ground
        plate = QPolygonF([at(-PLATE_HALF, 0.0), at(PLATE_HALF, 0.0), at(PLATE_HALF, -0.12), at(0, -0.3),
                           at(-PLATE_HALF, -0.12)])
        p.setBrush(QColor(text_color.red(), text_color.green(), text_color.blue(), 60))
        p.drawPolygon(plate)
        r = max(4.0, BALL_R * sc)
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 0.75)
        font.setBold(True)
        p.setFont(font)
        for arc in dyn["arcs"]:
            tr = arc["track"]
            pt = at(tr["px"], tr["pz"], clamp=True)
            current = arc is dyn["current"]
            fill = pitch_color(arc["pitch"].get("code"))
            if not current:
                fill.setAlpha(150)
            p.setPen(QPen(call_color(arc["pitch"]), 2.0 if current else 1.2))
            p.setBrush(fill)
            rr = r * (1.25 if current else 1.0)
            p.drawEllipse(pt, rr, rr)
            n = arc["pitch"].get("n")
            if n is not None:
                p.setPen(QColor("#000000"))
                p.drawText(QRectF(pt.x() - rr, pt.y() - rr, 2 * rr, 2 * rr), Qt.AlignmentFlag.AlignCenter, str(n))
        p.setFont(self.font())

    # ---- mouse and keys ----------------------------------------------------------------
    def mousePressEvent(self, event):
        self.setFocus()
        pan = event.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton) or \
            bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self._drag = (event.position(), pan, self._yaw, self._elev, self._target)

    def mouseMoveEvent(self, event):
        if not self._drag:
            return
        start, pan, yaw, elev, target = self._drag
        dx, dy = event.position().x() - start.x(), event.position().y() - start.y()
        if pan:
            _pos, right, _up, fwd, focal = self._camera()
            ground = _norm((fwd[0], fwd[1], 0.0)) if abs(fwd[2]) < 0.99 else _norm(_cross((0, 0, 1), right))
            k = self._dist / focal
            if abs(fwd[2]) > 0.9:  # looking down: drag the ground
                ground = _norm(_cross((0.0, 0.0, 1.0), right))
            self._target = (target[0] - right[0] * dx * k + ground[0] * dy * k,
                            target[1] - right[1] * dx * k + ground[1] * dy * k, target[2])
        else:
            self._yaw = (yaw - dx * 0.4) % 360
            self._elev = max(0.5, min(89.5, elev + dy * 0.3))
        self.update()

    def mouseReleaseEvent(self, _event):
        self._drag = None

    def mouseDoubleClickEvent(self, _event):
        self.set_preset(self._preset)

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120
        self._dist = max(4.0, min(1500.0, self._dist * 0.88 ** steps))
        self.update()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self.stepPitch.emit(-1 if key == Qt.Key.Key_Left else 1)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.stepAtBat.emit(-1 if key == Qt.Key.Key_Up else 1)
        elif key == Qt.Key.Key_Space:
            self.stop() if self.playing() else self.play(self._anim_speed or 1.0)
        else:
            super().keyPressEvent(event)
