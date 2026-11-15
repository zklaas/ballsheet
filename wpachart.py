#!/usr/bin/env python3
"""The WPA tab's chart: MLB's win probability through one game, drawn with QPainter.

The y axis is one team's chance of winning (0-100 %), 50 % across the middle: the line sits
above it while the home team is favoured and below it while the visitors are, and the area
between the line and 50 % takes that team's colour.  The x axis is the game by half-inning,
each half-inning the same width with its plate appearances spread evenly through it, so a
game in progress shows the innings still to come as empty space and the line stops where
the game has got to.  Hovering snaps a crosshair to the nearest plate appearance and shows
it; clicking one emits ``picked`` with its index (the Gameday ``atBatIndex``).
"""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPalette, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from infochart import SERIES_COLORS, THEME
from mlbstats import ordinal

# away = the palette's first hue, home = its second (a validated adjacent pair)
AWAY_COLOR, HOME_COLOR = SERIES_COLORS[0], SERIES_COLORS[1]


def layout_points(plays: list[dict], open_half: bool) -> list[float]:
    """Each play's x, in half-innings from the first pitch: half-inning ``k`` (top of the 1st
    = 0) spans [k, k+1] and its plays share it evenly, the last at its end.  In a half-inning
    still being played (``open_half``, the last one) the plays are spaced as if at least four
    were coming, so the line doesn't run to the half-inning's end before it is over."""
    groups: dict[int, list[int]] = {}
    for i, p in enumerate(plays):
        k = (p["inning"] - 1) * 2 + (0 if p["half"] == "top" else 1)
        groups.setdefault(k, []).append(i)
    xs = [0.0] * len(plays)
    last = max(groups) if groups else None
    for k, members in groups.items():
        n = len(members)
        denom = max(n + 1, 4) if (open_half and k == last) else n
        for j, i in enumerate(members):
            xs[i] = k + (j + 1) / denom
    return xs


class WPAChart(QWidget):
    picked = pyqtSignal(int)  # a plate appearance's atBatIndex

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(420, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._wp = None
        self._message = ""
        self._names = ("Away", "Home")
        self._halves = 18
        self._open_half = False
        self._live_note = ""
        self._xs: list[float] = []
        self._hover = None
        self._marked: set[int] = set()

    def set_data(self, wp: dict | None, away: str = "Away", home: str = "Home", halves: int = 18,
                 open_half: bool = False, live_note: str = "", message: str = "", marked=()):
        """``wp`` from ``mlbstats.get_win_probability``; ``halves`` = half-innings the x axis
        spans; ``live_note`` labels the unplayed part of a game in progress; ``marked`` = play
        indexes to ring (the biggest swings)."""
        self._wp, self._names, self._halves = wp, (away, home), max(halves, 2)
        self._open_half, self._live_note, self._message = open_half, live_note, message
        self._marked = set(marked)
        self._xs = layout_points(wp["plays"], open_half) if wp else []
        self._hover = None
        self.update()

    # ---- geometry -------------------------------------------------------
    def _dark(self) -> bool:
        return self.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def _colors(self):
        mode = 1 if self._dark() else 0
        return QColor(AWAY_COLOR[mode]), QColor(HOME_COLOR[mode])

    def _plot_rect(self) -> QRectF:
        fm = QFontMetrics(self.font())
        left = fm.horizontalAdvance("100%") + 14
        return QRectF(left, fm.height() * 2 + 14, self.width() - left - 16,
                      self.height() - fm.height() * 4 - 18)

    def _px(self, x: float, r: QRectF) -> float:
        return r.left() + x / self._halves * r.width()

    @staticmethod
    def _py(wp: float, r: QRectF) -> float:
        return r.bottom() - wp / 100.0 * r.height()

    def _points(self, r: QRectF) -> list[QPointF]:
        pts = [QPointF(self._px(0, r), self._py(self._wp["start"], r))]
        for x, p in zip(self._xs, self._wp["plays"]):
            pts.append(QPointF(self._px(x, r), self._py(p["home_wp"], r)))
        return pts

    # ---- painting -------------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        surface, ink, ink2, muted, grid, ref = (QColor(c) for c in THEME["dark" if self._dark() else "light"])
        p.fillRect(self.rect(), surface)
        font = QFont(self.font())
        fm = QFontMetrics(font)
        if self._wp is None or not self._wp["plays"]:
            p.setPen(ink2)
            p.drawText(self.rect().adjusted(20, 20, -20, -20),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._message)
            return
        away_c, home_c = self._colors()
        r = self._plot_rect()

        # legend, top left: which side of 50 % is whose
        x = r.left()
        y = 6
        for name, color in ((f"{self._names[1]} favoured (above 50%)", home_c),
                            (f"{self._names[0]} favoured (below 50%)", away_c)):
            p.fillRect(QRectF(x, y + fm.height() / 2 - 5, 10, 10), color)
            p.setPen(ink2)
            p.drawText(QPointF(x + 15, y + fm.ascent()), name)
            x += 15 + fm.horizontalAdvance(name) + 24

        # grid: 0/25/50/75/100 and the half-inning boundaries; inning numbers under the axis
        p.setFont(font)
        for v in (0, 25, 50, 75, 100):
            yy = self._py(v, r)
            p.setPen(QPen(ref if v == 50 else grid, 1))
            p.drawLine(QPointF(r.left(), yy), QPointF(r.right(), yy))
            p.setPen(muted)
            label = f"{v}%"
            p.drawText(QPointF(r.left() - 8 - fm.horizontalAdvance(label), yy + fm.ascent() / 2 - 1), label)
        innings = (self._halves + 1) // 2
        for k in range(self._halves + 1):
            xx = self._px(k, r)
            p.setPen(QPen(grid, 1, Qt.PenStyle.SolidLine if k % 2 == 0 else Qt.PenStyle.DotLine))  # dotted mid-inning
            p.drawLine(QPointF(xx, r.top()), QPointF(xx, r.bottom()))
        p.setPen(muted)
        for inn in range(1, innings + 1):
            label = str(inn)
            cx = self._px((inn - 1) * 2 + 1, r)
            p.drawText(QPointF(cx - fm.horizontalAdvance(label) / 2, r.bottom() + fm.ascent() + 4), label)
        caption = "Inning (each split into top and bottom halves)"
        p.drawText(QPointF(r.center().x() - fm.horizontalAdvance(caption) / 2,
                           r.bottom() + fm.height() + fm.ascent() + 6), caption)

        # the unplayed rest of a game in progress
        pts = self._points(r)
        if self._live_note:
            last_x = pts[-1].x()
            if r.right() - last_x > 4:
                shade = QColor(muted)
                shade.setAlpha(28)
                p.fillRect(QRectF(last_x, r.top(), r.right() - last_x, r.height()), shade)
                p.setPen(ink2)
                note_rect = QRectF(last_x + 6, r.top() + 4, r.right() - last_x - 10, fm.height() * 3)
                p.drawText(note_rect, Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap, self._live_note)

        # fill between the line and 50 %: home colour above, away colour below
        mid = self._py(50, r)
        for color, top in ((home_c, True), (away_c, False)):
            path = QPainterPath(QPointF(pts[0].x(), mid))
            for pt in pts:
                path.lineTo(pt)
            path.lineTo(QPointF(pts[-1].x(), mid))
            path.closeSubpath()
            fill = QColor(color)
            fill.setAlpha(70)
            p.save()
            clip = QRectF(r.left(), r.top(), r.width(), mid - r.top()) if top else \
                QRectF(r.left(), mid, r.width(), r.bottom() - mid)
            p.setClipRect(clip)
            p.fillPath(path, fill)
            p.restore()
        line = QPainterPath(pts[0])
        for pt in pts[1:]:
            line.lineTo(pt)
        p.setPen(QPen(ink, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPath(line)

        # the biggest swings get ringed dots; a game in progress gets a dot where it stands
        plays = self._wp["plays"]
        for i, play in enumerate(plays):
            if play["index"] in self._marked:
                self._dot(p, pts[i + 1], home_c if play["added"] > 0 else away_c, surface, 5)
        if self._live_note:
            self._dot(p, pts[-1], ink, surface, 4)

        if self._hover is not None:
            self._draw_hover(p, r, pts, surface, ink, ink2, muted, fm)

    @staticmethod
    def _dot(p, pt, color, ring, radius):
        p.setPen(QPen(ring, 2))
        p.setBrush(color)
        p.drawEllipse(pt, radius, radius)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_hover(self, p, r, pts, surface, ink, ink2, muted, fm):
        i = self._hover
        play = self._wp["plays"][i]
        pt = pts[i + 1]
        p.setPen(QPen(muted, 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(pt.x(), r.top()), QPointF(pt.x(), r.bottom()))
        away_c, home_c = self._colors()
        batting_home = play["half"] == "bottom"
        self._dot(p, pt, home_c if play["home_wp"] >= 50 else away_c, surface, 5)
        team = self._names[1] if batting_home else self._names[0]
        gain = play["added"] if batting_home else -play["added"]
        favoured = max(play["home_wp"], 100 - play["home_wp"])
        leader = self._names[1] if play["home_wp"] >= 50 else self._names[0]
        lines = [
            (f"{'Top' if play['half'] == 'top' else 'Bottom'} {ordinal(play['inning'])} — "
             f"{play['batter']} vs {play['pitcher']}", True),
            (play["description"] or play["event"], False),
            (f"{team} {gain:+.1f}% win probability (WPA)", False),
            (f"After it: {leader} {favoured:.1f}% to win   ·   score "
             f"{self._names[0]} {play['away_score']}, {self._names[1]} {play['home_score']}", False),
        ]
        if play["li"] is not None:
            lines.append((f"Leverage index {play['li']:.2f} (1 = an average moment)", False))
        bold = QFont(self.font())
        bold.setBold(True)
        bfm = QFontMetrics(bold)
        width = min(max(max(bfm.horizontalAdvance(t) if b else fm.horizontalAdvance(t) for t, b in lines) + 20, 200),
                    max(int(r.width() * 0.7), 200))
        rects = []
        height = 10
        for text, b in lines:
            br = (bfm if b else fm).boundingRect(0, 0, width - 20, 2000, Qt.TextFlag.TextWordWrap, text)
            rects.append(br.height())
            height += br.height() + 2
        x = pt.x() + 12 if pt.x() + 12 + width < self.width() - 4 else pt.x() - 12 - width
        y = min(max(r.top(), pt.y() - height / 2), r.bottom() - height)
        box = QRectF(max(x, 4), y, width, height)
        p.setPen(QPen(muted, 1))
        p.setBrush(surface)
        p.drawRoundedRect(box, 6, 6)
        p.setBrush(Qt.BrushStyle.NoBrush)
        yy = box.top() + 5
        for (text, b), h in zip(lines, rects):
            p.setFont(bold if b else self.font())
            p.setPen(ink if b else ink2)
            p.drawText(QRectF(box.left() + 10, yy, width - 20, h), Qt.TextFlag.TextWordWrap, text)
            yy += h + 2
        p.setFont(self.font())

    # ---- mouse ----------------------------------------------------------
    def _nearest(self, pos) -> int | None:
        if not self._wp or not self._wp["plays"]:
            return None
        r = self._plot_rect()
        if not r.adjusted(-8, -8, 8, 8).contains(pos):
            return None
        xs = [self._px(x, r) for x in self._xs]
        best = min(range(len(xs)), key=lambda i: abs(xs[i] - pos.x()))
        return best if abs(xs[best] - pos.x()) < 30 else None

    def mouseMoveEvent(self, event):
        hover = self._nearest(event.position())
        if hover != self._hover:
            self._hover = hover
            self.setCursor(Qt.CursorShape.PointingHandCursor if hover is not None else Qt.CursorShape.ArrowCursor)
            self.update()

    def mousePressEvent(self, event):
        i = self._nearest(event.position())
        if i is not None and event.button() == Qt.MouseButton.LeftButton:
            self.picked.emit(self._wp["plays"][i]["index"])

    def leaveEvent(self, _event):
        if self._hover is not None:
            self._hover = None
            self.update()
