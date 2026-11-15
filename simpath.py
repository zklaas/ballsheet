#!/usr/bin/env python3
"""The Simulator tab's path analysis drawn as a diagram (QPainter only).

Runs come from batters reaching base and being driven around before the outs run out (Bill James): a game's 27 outs
are the clock.  So every part is followed along that chain, link by link -- **outs at the plate** (per plate
appearance) and **outs on the bases** (caught stealing, picked off, thrown out, the second out of a double play)
decide how many **plate appearances** the outs buy; of those batters so many **reach**, of those so many **score**;
that's the **runs**, and the runs decide the **wins**.

Two lanes, one per team at bat.  In each, the parts that act on it -- the batting team's order and baserunning, the
fielding team's pitchers and fielders -- are boxes on the left, each row (the part as a whole, then each of its card
categories) a track running right through the chain's columns: what that part or row changed at each link, measured
by replaying the same games without it (``simulator.path_analysis``).  Each value is coloured for the team that owns
the part: green helped it, red hurt it, grey is within two standard errors of nothing (luck?).  The columns' heads
give the lane's own figures.

Under the lanes, a band for what acts on both sides of a team's games -- its **Managing** (sacrifices, intentional
walks, pinch hitting, defensive replacements, rest days, tired relievers) and its **Home field** (park, home batters'
edge) -- each row with two tracks: the team at bat, and the other side at bat against it; its wins come from both.
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QSizePolicy, QWidget

import simulator

INK = QColor("#1f2328")
MUTED = QColor("#59636e")
FAINT = QColor("#b4bcc5")
BOX = QColor("#f6f8fa")
BORDER = QColor("#d0d7de")
GOOD = QColor("#1a7f37")
BAD = QColor("#cf222e")
NEUTRAL = QColor("#8c959f")
LANE = (QColor("#eef4fb"), QColor("#fbf3ea"))
BAND = QColor("#f3f1f8")
GOOD_FILL = QColor(26, 127, 55, 34)
BAD_FILL = QColor(207, 34, 46, 30)

STAGES = simulator.CHAIN_STAGES + (("win", "Wins", True),)
HEAD_NOTES = {"out_rate": "of plate appearances", "outs_bases": "a game", "pa": "a game", "reached": "a game",
              "scored": "of those reaching", "runs": "a game", "win": "the team's share"}


def _fmt(stage: str, d: float) -> str:
    if stage in ("out_rate", "scored"):
        return f"{d * 100:+.1f} pt"
    if stage == "win":
        return f"{d:+.1%}"
    return f"{d:+.2f}"


class PathView(QWidget):
    MARGIN, GAP, BOTTOM = 8, 4, 28  # around the lanes, between boxes, and clear space under the diagram
    W_IN = 300                       # the parts' boxes
    LEAD = 62                        # from a box to its track's first value (the band's "at bat"/"against")
    COL_W, COL_GAP = 128, 26         # each link of the chain, and the arrow between two
    W_RES = 230                      # the result
    MIN_WIDTH = 14 + W_IN + LEAD + len(STAGES) * COL_W + (len(STAGES) - 1) * COL_GAP + 40 + W_RES + 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = None
        self.message = "Press “Path analysis” after playing some games."
        self.setMinimumSize(self.MIN_WIDTH, self.content_height())
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # ---- geometry
    def _small(self) -> QFont:
        """The rows and values, a little smaller than the titles."""
        f = QFont(self.font())
        f.setPointSizeF(max(self.font().pointSizeF() * 0.88, 6))
        return f

    def _row_h(self) -> int:
        return QFontMetrics(self._small()).height()

    def _head_h(self) -> int:
        """A lane's column heads: the link's name, the lane's figure, what it's per."""
        return 3 * self._row_h() + 8

    def _box_heights(self) -> list:
        line_h = self.fontMetrics().height()
        return [line_h + self._row_h() * len(simulator.PATH_CATEGORIES[part]) + 8
                for part in ("batting", "pitching", "fielding", "running")]

    def _band_heights(self) -> list:
        """The band's boxes, top to bottom: Managing and Home field for Team 1, then for Team 2 -- every row two
        tracks (the team at bat, the other side against it)."""
        line_h, row_h = self.fontMetrics().height(), self._row_h()
        managing = 2 * line_h + 2 * row_h * len(simulator.MANAGING_CATEGORIES) + 8
        home = 2 * line_h + 2 * row_h * len(simulator.HOME_CATEGORIES) + 8
        return [managing, home, managing, home]

    def lane_height(self) -> int:
        return int(self.fontMetrics().height() + 4 + self._head_h() + sum(self._box_heights()) + 5 * self.GAP)

    def band_height(self) -> int:
        return int(self.fontMetrics().height() + 4 + self._head_h() + sum(self._band_heights()) + 5 * self.GAP + 4)

    def content_height(self) -> int:
        return 2 * self.lane_height() + self.band_height() + 2 * self.MARGIN + 12 + self.BOTTOM

    def sizeHint(self):
        return QSize(self.MIN_WIDTH, self.content_height())

    def set_data(self, data: dict | None, message: str = ""):
        self.data, self.message = data, message
        self.update()

    def _col_x(self, i: int) -> float:
        """The left edge of the chain's ``i``-th column (a wider window spreads them out)."""
        extra = max(self.width() - self.MIN_WIDTH, 0) / len(STAGES)
        start = self.MARGIN + 6 + self.W_IN + self.LEAD
        return start + i * (self.COL_W + self.COL_GAP + extra)

    # ---- drawing helpers
    def _box(self, p: QPainter, rect: QRectF, title: str, fill=BOX):
        p.setPen(QPen(BORDER, 1))
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(rect, 6, 6)
        p.setPen(INK)
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        p.drawText(QRectF(rect.left() + 6, rect.top() + 4, rect.width() - 12, fm.height()), Qt.AlignmentFlag.AlignLeft,
                   fm.elidedText(title, Qt.TextElideMode.ElideRight, int(rect.width() - 12)))
        p.setFont(self.font())

    def _arrowhead(self, p: QPainter, tip: QPointF, color: QColor, size: float = 5):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawPolygon(QPolygonF([tip, QPointF(tip.x() - size, tip.y() - size / 1.7),
                                 QPointF(tip.x() - size, tip.y() + size / 1.7)]))

    def _heads(self, p: QPainter, top: float, figures: dict | None):
        """The chain's column heads across a lane (with its own figures) or the band, arrows from link to link."""
        small = self._small()
        bold = QFont(small)
        bold.setBold(True)
        row_h = QFontMetrics(small).height()
        for i, (stage, label, _more) in enumerate(STAGES):
            x = self._col_x(i)
            p.setFont(bold)
            p.setPen(INK)
            p.drawText(QRectF(x, top, self.COL_W, row_h), Qt.AlignmentFlag.AlignCenter, label)
            p.setFont(small)
            p.setPen(MUTED)
            if figures is not None:
                v = figures["_win"] if stage == "win" else figures[stage]
                text = f"{v:.1%}" if stage in ("out_rate", "scored", "win") else f"{v:.2f}"
                p.drawText(QRectF(x, top + row_h, self.COL_W, row_h), Qt.AlignmentFlag.AlignCenter, text)
                p.drawText(QRectF(x, top + 2 * row_h, self.COL_W, row_h), Qt.AlignmentFlag.AlignCenter,
                           HEAD_NOTES[stage])
            if i + 1 < len(STAGES):  # the chain: this link leads to the next
                a = QPointF(x + self.COL_W + 3, top + row_h / 2 + 1)
                b = QPointF(self._col_x(i + 1) - 3, top + row_h / 2 + 1)
                p.setPen(QPen(MUTED, 1.2))
                p.drawLine(a, QPointF(b.x() - 4, b.y()))
                self._arrowhead(p, b, MUTED)
        p.setFont(self.font())

    def _track(self, p: QPainter, start: QPointF, chain: dict | None, win: dict | None, owner_bats: bool,
               bold: bool = False, faint: bool = False):
        """One row's track: from its box through every link of the chain, each value coloured for the part's owner
        (``owner_bats``: its team is the one at bat here, so more of a good link helps it).  ``win`` is
        ``{"win", "win_se"}`` or None (no wins column on this line)."""
        small = QFont(self._small())
        small.setBold(bold)
        p.setFont(small)
        h = p.fontMetrics().height()
        y = start.y()
        cells = []
        for stage, _label, more_good in STAGES:
            if stage == "win":
                if win is None:
                    cells.append(None)
                    continue
                d, se, good_dir = win["win"], win.get("win_se", 0), 1
            else:
                if chain is None:
                    cells.append(None)
                    continue
                d, se = chain[stage]["d"], chain[stage]["se"]
                good_dir = (1 if more_good else -1) * (1 if owner_bats else -1)
            real = abs(d) > 1e-9 and abs(d) >= 2 * se
            colour = FAINT if faint else (GOOD if d * good_dir > 0 else BAD) if real else NEUTRAL
            cells.append((stage, d, real, colour))
        # the line from the box to the first value, coloured by the row's runs
        runs_cell = next((c for c in cells if c and c[0] == "runs"), None)
        lead_colour = runs_cell[3] if runs_cell and runs_cell[2] else (FAINT if faint else NEUTRAL)
        first_x = self._col_x(0)
        p.setPen(QPen(lead_colour, 2.2 if bold else 1.2))
        p.drawLine(start, QPointF(first_x - 6, y))
        self._arrowhead(p, QPointF(first_x - 2, y), lead_colour, 5 if bold else 4)
        for i, cell in enumerate(cells):
            if cell is None:
                continue
            x = self._col_x(i)
            rect = QRectF(x, y - h / 2, self.COL_W, h)
            stage, d, real, colour = cell
            if real and not faint:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(GOOD_FILL if colour is GOOD else BAD_FILL))
                p.drawRoundedRect(rect.adjusted(6, 0, -6, 0), 4, 4)
            p.setPen(colour.darker(125) if real and not faint else colour)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, _fmt(stage, d) + ("" if real or faint else "?"))
            nxt = next((j for j in range(i + 1, len(cells)) if cells[j] is not None), None)
            if nxt is not None:  # a quiet link on to the next value
                p.setPen(QPen(FAINT, 1))
                p.drawLine(QPointF(x + self.COL_W - 4, y), QPointF(self._col_x(nxt) + 4, y))
        p.setFont(self.font())

    def _rows(self, p: QPainter, rect: QRectF, labels: list[str], double: bool = False) -> list[float]:
        """A box's rows under its title (``double``: two lines each, for the band) -> each row's middle y."""
        p.setFont(self._small())
        fm = p.fontMetrics()
        line_h = self.fontMetrics().height()
        y = rect.top() + 4 + line_h * (2 if double else 1) + 1
        mids = []
        for label in labels:
            p.setPen(INK)
            p.drawText(QRectF(rect.left() + 12, y, rect.width() - 18, fm.height()), Qt.AlignmentFlag.AlignLeft, label)
            mids.append(y + fm.height() / 2)
            y += fm.height() * (2 if double else 1)
        p.setFont(self.font())
        return mids

    def _sub(self, p: QPainter, y: float, text: str):
        """The band's track label, between its box and the first value."""
        p.setFont(self._small())
        p.setPen(MUTED)
        fm = p.fontMetrics()
        r = QRectF(self.MARGIN + 6 + self.W_IN + 4, y - fm.height() / 2, self.LEAD - 6, fm.height())
        p.fillRect(r, BAND)
        p.drawText(r, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        p.setFont(self.font())

    def _to_result(self, p: QPainter, y: float, col_res: float):
        """The wins column's arrow on to the result."""
        x = self._col_x(len(STAGES) - 1) + self.COL_W
        p.setPen(QPen(MUTED, 1.2))
        p.drawLine(QPointF(x + 2, y), QPointF(col_res - 6, y))
        self._arrowhead(p, QPointF(col_res - 2, y), MUTED)

    # ---- the whole diagram
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("white"))
        if not self.data:  # the message at the top, where it's seen without scrolling
            f = QFont(self.font())
            f.setPointSizeF(self.font().pointSizeF() * 1.15)
            p.setFont(f)
            p.setPen(INK)
            p.drawText(QRectF(self.MARGIN + 8, self.MARGIN + 8, max(self.width() - 2 * self.MARGIN - 16, 100), 200),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, self.message)
            return
        d = self.data
        names = d["names"]
        short = d.get("short") or names
        eff = d["effects"]
        base = d["base"]
        n = base["games"] or 1
        W = self.width()
        line_h = p.fontMetrics().height()
        margin, gap = self.MARGIN, self.GAP
        lane_h = self.lane_height()
        col_in, w_in = margin + 6, self.W_IN
        col_res = W - margin - self.W_RES
        band_top = margin + 2 * (lane_h + 6)
        band_h = self.band_height()
        res_rect = QRectF(col_res, margin + 4, self.W_RES, band_top + band_h - 8 - margin - 4)

        for lane in (0, 1):  # lane 0: Team 1 bats against Team 2
            top = margin + lane * (lane_h + 6)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(LANE[lane]))
            p.drawRoundedRect(QRectF(margin, top, col_res - margin - 12, lane_h), 8, 8)
            bat, field = lane, 1 - lane
            p.setPen(INK)
            p.drawText(QRectF(margin + 8, top + 2, W, line_h), Qt.AlignmentFlag.AlignLeft,
                       f"{names[bat]} batting against {names[field]}"
                       + ("  (each team hosts half the games)" if lane == 0 else ""))
            figures = dict(d["chain_base"][bat], _win=base["wins"][bat] / n) if d.get("chain_base") else None
            head_top = top + line_h + 4
            self._heads(p, head_top, figures)
            self._to_result(p, head_top + self._row_h() / 2 + 1, col_res)
            y = head_top + self._head_h()
            heights = self._box_heights()
            for i, (owner, part) in enumerate(((bat, "batting"), (field, "pitching"), (field, "fielding"),
                                               (bat, "running"))):
                y += gap
                rect = QRectF(col_in, y, w_in, heights[i])
                y += heights[i]
                self._box(p, rect, f"{simulator.PATH_LABELS[part]} · {short[owner]}")
                cats = simulator.PATH_CATEGORIES[part]
                mids = self._rows(p, rect, [simulator.CATEGORY_LABELS[c] for c in cats])
                whole = eff[owner][part]
                self._track(p, QPointF(rect.right(), rect.top() + 4 + line_h / 2), whole.get("chain"), whole,
                            owner == bat, bold=True)
                for cat, my in zip(cats, mids):
                    e = eff[owner].get(cat)
                    if e is not None:
                        self._track(p, QPointF(rect.right(), my), e.get("chain"), e, owner == bat)

        # the band: what acts on both sides of each team's games
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(BAND))
        p.drawRoundedRect(QRectF(margin, band_top, col_res - margin - 12, band_h), 8, 8)
        p.setPen(INK)
        p.drawText(QRectF(margin + 8, band_top + 2, W, line_h), Qt.AlignmentFlag.AlignLeft,
                   "Managing and home field, each switched off for that team: its own batting (“at bat”) and "
                   "the other side's against it (“against”) -- its wins come from both")
        head_top = band_top + line_h + 4
        self._heads(p, head_top, None)
        self._to_result(p, head_top + self._row_h() / 2 + 1, col_res)
        y = head_top + self._head_h()
        heights = self._band_heights()
        boxes = [(0, "managing", simulator.MANAGING_CATEGORIES, "Managing"),
                 (0, "home", simulator.HOME_CATEGORIES, "Home field"),
                 (1, "managing", simulator.MANAGING_CATEGORIES, "Managing"),
                 (1, "home", simulator.HOME_CATEGORIES, "Home field")]
        for i, (side, key, cats, title) in enumerate(boxes):
            y += gap
            rect = QRectF(col_in, y, w_in, heights[i])
            y += heights[i]
            self._box(p, rect, f"{title} · {short[side]}")
            mids = self._rows(p, rect, [simulator.CATEGORY_LABELS[c] for c in cats], double=True)
            rows = [(eff[side].get(key), True, rect.top() + 4 + line_h / 2)] + \
                   [(eff[side].get(c), False, my) for c, my in zip(cats, mids)]
            for e, whole, my in rows:
                if e is None:
                    continue
                faint = bool(e.get("inactive")) or e.get("known") is False
                chain = e.get("chain") or {}
                step = line_h if whole else self._row_h()
                self._track(p, QPointF(rect.right(), my), chain.get("bat"), e, True, bold=whole, faint=faint)
                self._sub(p, my, "at bat")
                self._track(p, QPointF(rect.right(), my + step), chain.get("field"), None, False, bold=whole,
                            faint=faint)
                self._sub(p, my + step, "against")
                if faint:
                    p.setFont(self._small())
                    p.setPen(MUTED)
                    note = "no data: neutral" if e.get("known") is False else "never came up"
                    p.drawText(QRectF(rect.left() + 24, my + step / 2, rect.width() - 30, self._row_h()),
                               Qt.AlignmentFlag.AlignLeft, note)
                    p.setFont(self.font())

        # the result
        stages = d["stages"]
        lines = [f"{short[0]}  wins {base['wins'][0] / n:.1%}", f"{short[1]}  wins {base['wins'][1] / n:.1%}",
                 f"{base['games']:,} games, seed {d['seed']}", ""]
        for side in (0, 1):
            lines.append(f"{short[side]} scores {stages[side]['R/G']:.2f} a game,")
            lines.append(f"  allows {stages[1 - side]['R/G']:.2f}")
        lines += ["", "Each track: what that part or row", "changed at each link -- the same",
                  "games replayed without it.", "", "27 outs a game are the clock:", "fewer outs at the plate or on",
                  "the bases buy more plate", "appearances; more batters reach;", "more of them score: runs, then",
                  "wins.", "", "Green helped its team, red hurt", "it; grey with ? is within two",
                  "standard errors of nothing.", "Faint: never came up.", "", "pt: percentage points."]
        self._box(p, res_rect, "Result", QColor("white"))
        p.setPen(MUTED)
        fm = p.fontMetrics()
        yy = res_rect.top() + 6 + fm.height() + 2
        for text in lines:
            p.drawText(QRectF(res_rect.left() + 8, yy, res_rect.width() - 14, fm.height()), Qt.AlignmentFlag.AlignLeft,
                       text)
            yy += fm.height()
