#!/usr/bin/env python3
"""The Infograph tab's chart: line series from an Info query result (``infoquery.run_query``)
drawn with QPainter -- no plotting library.

One y scale per panel: two measures of different size (hits and batting average, say)
go in panels stacked over a shared x axis, never on twin axes.  Colours follow the
entity (each subject, or each series when there's one subject) in a fixed order of eight
colour-blind-checked hues; a legend whenever there are two or more lines, direct end
labels when there are four or fewer, a crosshair + tooltip on hover, and hovering a
legend entry brings its line forward.  A query drawn as bars (``"chart": "bars"``) gets a group
of bars per subject, one per series (series sharing a ``stack`` name piled into one bar), from a
zero baseline, each bar labelled with its value -- a stack with its total -- when the label fits.  Light and dark steps of the palette are picked
from the window's own colours.
"""
from __future__ import annotations

import datetime as _dt
import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPalette, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

import infoquery

# The reference categorical palette (light, dark), in its validated order.
SERIES_COLORS = [("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"), ("#1baf7a", "#199e70"),
                 ("#eda100", "#c98500"), ("#e87ba4", "#d55181"), ("#008300", "#008300"),
                 ("#4a3aa7", "#9085e9"), ("#e34948", "#e66767")]
OVERFLOW_COLOR = ("#8a8985", "#8a8985")  # a ninth line and on: never a made-up hue
THEME = {  # surface, primary text, secondary text, muted text, grid, reference line
    "light": ("#fcfcfb", "#0b0b0b", "#52514e", "#7a7974", "#e7e6e2", "#8f8e89"),
    "dark": ("#1a1a19", "#ffffff", "#c3c2b7", "#9a9990", "#333331", "#8a8982"),
}
MARKER_LIMIT = 40  # draw dots on a line with this few points or fewer
DIRECT_LABEL_LIMIT = 4
BAR_GROUP = 0.78   # of each subject's slot on the x axis: the rest is the gap between groups
BAR_GAP = 2        # pixels between the bars of one group


def nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    """Round tick values spanning [lo, hi] (1, 2, 2.5 or 5 times a power of ten apart)."""
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        hi = lo + 1
    raw = (hi - lo) / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.floor(lo / step + 1e-9) * step
    ticks = []
    t = first
    while t <= hi + step * 1e-6:
        ticks.append(round(t, 10))
        t += step
    if ticks[-1] < hi - 1e-9:
        ticks.append(round(t, 10))
    return ticks


MAX_TICK_TIERS = 3  # staggered rows of x-axis names, at most


class InfoChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(420, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.result = None
        self.message = "Choose a query, or run one on the Info tab."
        self._hover_x = None      # mouse x in pixels, over a plot
        self._hover_key = None    # legend entry under the mouse
        self._legend_hits = []    # (QRectF, key)
        self._geom = None

    # ---- data ------------------------------------------------------------
    def set_result(self, result: dict | None, message: str = ""):
        self.result, self.message = result, message
        self._hover_x = self._hover_key = None
        keys = list(dict.fromkeys(s["key"] for s in result["series"])) if result else []
        self._color_index = {k: i for i, k in enumerate(keys)}
        self.update()

    def _bars(self) -> bool:
        return bool(self.result) and self.result.get("chart") == "bars"

    def _dark(self) -> bool:
        return self.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def _theme(self):
        return THEME["dark" if self._dark() else "light"]

    def color_for(self, key) -> QColor:
        i = self._color_index.get(key, 0)
        pair = SERIES_COLORS[i] if i < len(SERIES_COLORS) else OVERFLOW_COLOR
        return QColor(pair[1] if self._dark() else pair[0])

    # ---- layout ----------------------------------------------------------
    def _fonts(self):
        base = QFont(self.font())
        small = QFont(base)
        small.setPointSizeF(max(base.pointSizeF() - 1, 7))
        title = QFont(base)
        title.setPointSizeF(base.pointSizeF() + 1.5)
        title.setWeight(QFont.Weight.DemiBold)
        return base, small, title

    def _x_range(self):
        xs = [p["x"] for s in self.result["series"] for p in s["points"]]
        lo, hi = min(xs), max(xs)
        if hi == lo:
            lo, hi = lo - 1, hi + 1
        if self.result.get("x_ticks"):
            lo, hi = lo - 0.5, hi + 0.5  # room for the first and last names
        return lo, hi

    def _y_range(self, panel):
        ys = [p["y"] for s in self.result["series"] if s["panel"] == panel["n"]
              for p in s["points"] if p["y"] is not None]
        ys += [v for v, _l in panel["refs"]]
        if self._bars():  # a stack reaches the sum of its pieces
            totals: dict = {}
            for s in self.result["series"]:
                if s["panel"] == panel["n"] and s.get("stack"):
                    for pt in s["points"]:
                        if pt["y"] is not None:
                            key = (pt["x"], s["stack"])
                            totals[key] = totals.get(key, 0) + pt["y"]
            ys += list(totals.values())
        lo = panel["min"] if panel["min"] is not None else (min(ys) if ys else 0)
        hi = panel["max"] if panel["max"] is not None else (max(ys) if ys else 1)
        if panel["min"] is None and lo > 0 and ((hi - lo) > 0.4 * hi or self._bars()):
            lo = 0  # counts that start near zero, and every bar: show the baseline
        ticks = nice_ticks(lo, hi)
        if panel["min"] is not None:
            ticks = [t for t in ticks if t >= lo - 1e-9] or ticks
        if panel["max"] is not None:
            ticks = [t for t in ticks if t <= hi + 1e-9] or ticks
        lo = min(lo, ticks[0]) if panel["min"] is None else lo
        hi = max(hi, ticks[-1]) if panel["max"] is None else hi
        return lo, hi, ticks

    def _layout(self):
        base, small, title_font = self._fonts()
        fm, sfm, tfm = QFontMetrics(base), QFontMetrics(small), QFontMetrics(title_font)
        r = self.result
        w, h = self.width(), self.height()
        top = 10 + tfm.height() + 6
        # legend: wraps across rows under the title
        keys = list(dict.fromkeys(s["key"] for s in r["series"]))
        legend = []
        if len(keys) > 1:
            x, y = 12, top
            for key in keys:
                iw = 22 + sfm.horizontalAdvance(str(key)) + 16
                if x + iw > w - 12 and x > 12:
                    x, y = 12, y + sfm.height() + 6
                legend.append((QRectF(x, y, iw - 10, sfm.height() + 2), key))
                x += iw
            top = y + sfm.height() + 12
        panels = r["panels"]
        y_info = {p["n"]: self._y_range(p) for p in panels}
        left = 14 + max(sfm.horizontalAdvance(infoquery.format_value(t, p["format"]))
                        for p in panels for t in y_info[p["n"]][2])
        # direct end labels when there are few lines in a panel
        right = 16
        self._direct = {}
        for p in panels:
            members = [s for s in r["series"] if s["panel"] == p["n"]]
            if len(members) <= DIRECT_LABEL_LIMIT and len(keys) > 1 and not self._bars():
                self._direct[p["n"]] = True
                right = max(right, 18 + max(sfm.horizontalAdvance(self._end_label(s)) for s in members))
        right = min(right, w // 3)
        ticks = r.get("x_ticks") or []
        tick_lines = max((text.count("\n") + 1 for _t, text in ticks), default=1)
        # names too wide to sit side by side under their points go on staggered rows -- as many
        # as it takes (up to three) for every name to fit
        widest = max((sfm.horizontalAdvance(row) for _t, text in ticks for row in text.split("\n")), default=0)
        spacing = (w - left - right) / (len(ticks) + 1) if ticks else 0
        self._tiers = next((n for n in (1, 2) if widest + 8 <= n * spacing), MAX_TICK_TIERS) if ticks else 1
        x_title = 1 if r.get("x_label") else 0
        bottom = h - (sfm.height() * (x_title + tick_lines * self._tiers) + 14)
        gap = sfm.height() + 18
        n = len(panels)
        ph = max((bottom - top - gap * (n - 1) - sfm.height() * n) / n, 30)
        rects = {}
        y = top
        for p in panels:
            y += sfm.height() + 4  # the panel's title above its plot
            rects[p["n"]] = QRectF(left, y, max(w - left - right, 50), ph)
            y += ph + gap
        self._geom = {"rects": rects, "y": y_info, "x": self._x_range(), "legend": legend,
                      "fonts": (base, small, title_font), "left": left, "right": right}
        self._legend_hits = legend
        return self._geom

    def _end_label(self, s) -> str:
        last = next((p for p in reversed(s["points"]) if p["y"] is not None), None)
        name = s["key"]
        return f"{name}  {infoquery.format_value(last['y'], s['format'])}" if last else name

    def _px(self, x, rect, xr):
        return rect.left() + (x - xr[0]) / (xr[1] - xr[0]) * rect.width()

    @staticmethod
    def _py(y, rect, yr):
        lo, hi = yr[0], yr[1]
        return rect.bottom() - (y - lo) / ((hi - lo) or 1) * rect.height()

    # ---- painting --------------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        surface, ink, ink2, muted, grid, refc = (QColor(c) for c in self._theme())
        p.fillRect(self.rect(), surface)
        r = self.result
        if not r or not any(s["points"] for s in r["series"]):
            p.setPen(ink2)
            p.drawText(self.rect().adjusted(20, 20, -20, -20),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       self.message or "Nothing to graph.")
            return
        g = self._layout()
        base, small, title_font = g["fonts"]
        sfm = QFontMetrics(small)
        p.setFont(title_font)
        p.setPen(ink)
        p.drawText(QPointF(12, 10 + QFontMetrics(title_font).ascent()), r["title"])

        # legend
        p.setFont(small)
        for rect, key in g["legend"]:
            faded = self._hover_key is not None and key != self._hover_key
            col = self.color_for(key)
            if faded:
                col.setAlphaF(0.3)
            cy = rect.center().y()
            if self._bars():
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(col)
                p.drawRoundedRect(QRectF(rect.left() + 3, cy - 5, 10, 10), 2, 2)
            else:
                p.setPen(QPen(col, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPointF(rect.left(), cy), QPointF(rect.left() + 16, cy))
            p.setPen(ink2 if not faded else muted)
            p.drawText(QPointF(rect.left() + 22, rect.top() + sfm.ascent() + 1), str(key))

        xr = g["x"]
        for panel in r["panels"]:
            rect = g["rects"][panel["n"]]
            lo, hi, ticks = g["y"][panel["n"]]
            yr = (lo, hi)
            # panel title, y grid + tick labels
            p.setPen(ink2)
            p.drawText(QPointF(rect.left(), rect.top() - 6), panel["title"])
            for t in ticks:
                if t < lo - 1e-9 or t > hi + 1e-9:
                    continue
                y = self._py(t, rect, yr)
                p.setPen(QPen(grid, 1))
                p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
                p.setPen(muted)
                label = infoquery.format_value(t, panel["format"])
                p.drawText(QRectF(0, y - sfm.height() / 2, rect.left() - 6, sfm.height()),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
            if self._bars():
                self._draw_bars(p, panel, rect, xr, yr, ink2)
            else:
                self._draw_lines(p, panel, rect, xr, yr, surface)
            # reference lines (over the data, so the label tab stays readable)
            for value, label in panel["refs"]:
                if not lo <= value <= hi:
                    continue
                y = self._py(value, rect, yr)
                p.setPen(QPen(refc, 1))
                p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
                if label:  # on a surface-coloured tab, so lines crossing it don't hide it
                    tw = sfm.horizontalAdvance(label)
                    tab = QRectF(rect.left() + 4, y - sfm.height() - 1, tw + 8, sfm.height())
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(surface)
                    p.drawRoundedRect(tab, 3, 3)
                    p.setPen(ink2)
                    p.drawText(tab, Qt.AlignmentFlag.AlignCenter, label)
            # baseline / x ticks under the last panel only
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(rect.left(), rect.bottom()), QPointF(rect.right(), rect.bottom()))
        last = g["rects"][r["panels"][-1]["n"]]
        self._draw_x_axis(p, last, xr, muted, ink2, grid, small)
        if self._hover_x is not None:
            self._draw_hover(p, g, surface, ink, ink2, muted, small)

    def _draw_lines(self, p, panel, rect, xr, yr, surface):
        members = [s for s in self.result["series"] if s["panel"] == panel["n"]]
        # the hovered entity last, so it sits on top
        members.sort(key=lambda s: s["key"] == self._hover_key)
        style_of = {}
        for s in members:
            style_of.setdefault(s["key"], []).append(s["name"])
        p.save()
        p.setClipRect(rect.adjusted(-6, 0, 6, 0))  # dots at the ends may poke out sideways, never past a range
        labels = []
        for s in members:
            col = self.color_for(s["key"])
            if self._hover_key is not None and s["key"] != self._hover_key:
                col.setAlphaF(0.22)
            dashed = style_of[s["key"]].index(s["name"]) > 0  # a 2nd series of one entity in a panel
            pen = QPen(col, 2.5 if s["key"] == self._hover_key else 2,
                       Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            path, started, pts = QPainterPath(), False, []
            for pt in s["points"]:
                if pt["y"] is None:
                    started = False
                    continue
                q = QPointF(self._px(pt["x"], rect, xr), self._py(pt["y"], rect, yr))
                pts.append(q)
                if started:
                    path.lineTo(q)
                else:
                    path.moveTo(q)
                    started = True
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            if len(pts) <= MARKER_LIMIT:
                p.setPen(QPen(surface, 2))
                p.setBrush(col)
                for q in pts:
                    p.drawEllipse(q, 4, 4)
            if pts and self._direct.get(panel["n"]):
                labels.append((pts[-1].y(), self._end_label(s), col))
        p.restore()
        # end labels, top to bottom; one that would overlap the last placed is dropped
        if labels:
            _base, small, _t = self._geom["fonts"]
            sfm = QFontMetrics(small)
            p.setFont(small)
            placed = []
            for y, text, col in sorted(labels):
                if placed and y - placed[-1] < sfm.height():
                    continue
                placed.append(y)
                ink = QColor(self._theme()[1])
                if col.alphaF() < 1:
                    ink.setAlphaF(0.35)
                p.setPen(ink)
                p.drawText(QPointF(rect.right() + 8, y + sfm.ascent() / 2 - 1), text)

    def _draw_bars(self, p, panel, rect, xr, yr, ink2):
        """Each subject's group of bars, one per series in this panel side by side in series order --
        series sharing a stack name piled into one bar, in series order from the bottom; a value
        (a stack's total) over its bar when it fits."""
        in_panel = [s for s in self.result["series"] if s["panel"] == panel["n"]]
        slots = list(dict.fromkeys(s.get("stack") or s["name"] for s in in_panel))
        if not slots:
            return
        slot = rect.width() / (xr[1] - xr[0])
        width = max((slot * BAR_GROUP - BAR_GAP * (len(slots) - 1)) / len(slots), 2)
        _base, small, _t = self._geom["fonts"]
        sfm = QFontMetrics(small)
        p.setFont(small)
        zero = max(yr[0], 0)
        base_y = self._py(zero, rect, yr)
        if self._hover_x is not None:  # a band behind the hovered group
            snap, _hits = self._hover_points(self._geom)
            if snap is not None:
                band = QColor(self._theme()[3])
                band.setAlphaF(0.14)
                p.fillRect(QRectF(self._px(snap, rect, xr) - slot / 2, rect.top(), slot, rect.height()), band)
        bars: dict = {}  # (x, slot) -> [(series, value), ...] bottom first
        for s in in_panel:
            for pt in s["points"]:
                if pt["y"] is not None:
                    bars.setdefault((pt["x"], s.get("stack") or s["name"]), []).append((s, pt["y"]))
        for (x_value, name), pieces in bars.items():
            i = slots.index(name)
            x = self._px(x_value, rect, xr)
            left = x - slot * BAR_GROUP / 2 + i * (width + BAR_GAP)
            level, show = zero, False
            for n, (s, value) in enumerate(pieces):
                col = self.color_for(s["key"])
                if self._hover_key is not None and s["key"] != self._hover_key:
                    col.setAlphaF(0.25)
                else:
                    show = True
                lo_y, hi_y = self._py(level, rect, yr), self._py(level + value, rect, yr)
                piece = QRectF(left, min(lo_y, hi_y), width, abs(lo_y - hi_y))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(col)
                if n == len(pieces) - 1:  # the top piece is rounded; its foot runs under the one below
                    p.drawRoundedRect(piece.adjusted(0, 0, 0, 2), 2, 2)
                else:
                    p.drawRect(piece)
                level += value
            s0 = pieces[0][0]
            text = infoquery.format_value(level, s0["format"])
            room = width + 2 * BAR_GAP if i else width + slot * (1 - BAR_GROUP)  # the first may use the gap before it
            if sfm.horizontalAdvance(text) <= room and show:
                top = self._py(level, rect, yr)
                p.setPen(ink2)
                p.drawText(QRectF(left + width / 2 - room / 2, top - sfm.height() - 1, room, sfm.height()),
                           Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, text)

    def _draw_x_axis(self, p, rect, xr, muted, ink2, grid, font):
        sfm = QFontMetrics(font)
        p.setFont(font)
        y = rect.bottom() + 4 + sfm.ascent()
        if self.result["x_kind"] == "date":
            lo, hi = (_dt.date.fromordinal(int(v)) for v in (xr[0], xr[1]))
            years = hi.year != lo.year
            d = _dt.date(lo.year, lo.month, 1)
            while d <= hi:
                if d >= lo:
                    x = self._px(d.toordinal(), rect, xr)
                    p.setPen(QPen(grid, 1))
                    p.drawLine(QPointF(x, rect.bottom()), QPointF(x, rect.bottom() + 3))
                    p.setPen(muted)
                    text = d.strftime("%b") + (f" {d.year}" if years and d.month == 1 else "")
                    p.drawText(QPointF(x + 3, y), text)
                d = _dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        elif self.result.get("x_ticks"):  # named x values (a ranking): each name under its point
            lines = max(text.count("\n") + 1 for _t, text in self.result["x_ticks"])
            tiers = getattr(self, "_tiers", 1)
            last = [None] * tiers
            for i, (t, text) in enumerate(self.result["x_ticks"]):  # a name may run to more than one line
                tier = i % tiers
                x = self._px(t, rect, xr)
                rows = text.split("\n")
                w = max(sfm.horizontalAdvance(row) for row in rows)
                if last[tier] is not None and x - w / 2 < last[tier] + 8:
                    continue  # it would run into the one before
                top = y + tier * lines * sfm.height()
                if not self._bars():
                    p.setPen(QPen(grid, 1))
                    p.drawLine(QPointF(x, rect.bottom()), QPointF(x, top - sfm.ascent() - 1))
                p.setPen(ink2 if self._bars() else muted)
                for j, row in enumerate(rows):
                    p.drawText(QPointF(x - sfm.horizontalAdvance(row) / 2, top + j * sfm.height()), row)
                last[tier] = x + w / 2
            y += (lines * tiers - 1) * sfm.height()
        else:
            for t in nice_ticks(xr[0], xr[1], 8):
                if t < xr[0] - 1e-9 or t > xr[1] + 1e-9:
                    continue
                x = self._px(t, rect, xr)
                p.setPen(QPen(grid, 1))
                p.drawLine(QPointF(x, rect.bottom()), QPointF(x, rect.bottom() + 3))
                p.setPen(muted)
                text = infoquery.format_value(t)
                p.drawText(QPointF(x - sfm.horizontalAdvance(text) / 2, y), text)
        if self.result["x_label"]:
            p.setPen(ink2)
            p.drawText(QRectF(rect.left(), y + 2, rect.width(), sfm.height() + 2),
                       Qt.AlignmentFlag.AlignHCenter, self.result["x_label"])

    def _hover_points(self, g):
        """The x under the mouse (snapped to the nearest data point) and, per line, its point there."""
        rect0 = next(iter(g["rects"].values()))
        xr = g["x"]
        x_val = xr[0] + (self._hover_x - rect0.left()) / rect0.width() * (xr[1] - xr[0])
        all_x = [pt["x"] for s in self.result["series"] for pt in s["points"] if pt["y"] is not None]
        if not all_x:
            return None, []
        snap = min(all_x, key=lambda v: abs(v - x_val))
        tol = max((xr[1] - xr[0]) / rect0.width() * 14, 0.5)
        hits = []
        for s in self.result["series"]:
            near = [pt for pt in s["points"] if pt["y"] is not None and abs(pt["x"] - snap) <= tol]
            if near:
                hits.append((s, min(near, key=lambda pt: abs(pt["x"] - snap))))
        return snap, hits

    def _draw_hover(self, p, g, surface, ink, ink2, muted, font):
        snap, hits = self._hover_points(g)
        if snap is None or not hits:
            return
        xr = g["x"]
        rect0 = next(iter(g["rects"].values()))
        x = self._px(snap, rect0, xr)
        for panel in self.result["panels"]:
            rect = g["rects"][panel["n"]]
            if self._bars():
                continue  # the band behind the hovered group is drawn with the bars
            p.setPen(QPen(muted, 1))
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        for s, pt in ([] if self._bars() else hits):
            rect = g["rects"][s["panel"]]
            lo, hi, _t = g["y"][s["panel"]]
            q = QPointF(self._px(pt["x"], rect, xr), self._py(pt["y"], rect, (lo, hi)))
            if rect.adjusted(-4, -4, 4, 4).contains(q):
                p.setPen(QPen(surface, 2))
                p.setBrush(self.color_for(s["key"]))
                p.drawEllipse(q, 5, 5)
        # tooltip: the x, then each line's value (highest first when there are many)
        fm = QFontMetrics(font)
        p.setFont(font)
        if len(hits) > DIRECT_LABEL_LIMIT:
            hits = sorted(hits, key=lambda h: -h[1]["y"])
        header = hits[0][1]["xlabel"] if self.result["x"] != "game" else f"Game {int(snap)}"
        rows = [(s, f"{s['label']}", infoquery.format_value(pt["y"], s["format"]), pt["detail"]) for s, pt in hits]
        name_w = max(fm.horizontalAdvance(r[1]) for r in rows)
        val_w = max(fm.horizontalAdvance(r[2]) for r in rows)
        det_w = max(fm.horizontalAdvance(r[3]) for r in rows) if any(r[3] for r in rows) else 0
        line_h = fm.height() + 3
        box_w = max(16 + name_w + 12 + val_w + (14 + det_w if det_w else 0) + 12, fm.horizontalAdvance(header) + 20)
        box_h = 8 + line_h * (len(rows) + 1) + 4
        bx = x + 14 if x + 14 + box_w < self.width() - 4 else x - 14 - box_w
        by = max(4, min(rect0.top(), self.height() - box_h - 4))
        box = QRectF(bx, by, box_w, box_h)
        bg = QColor(surface)
        p.setPen(QPen(QColor(self._theme()[4]), 1))
        p.setBrush(bg)
        p.drawRoundedRect(box, 6, 6)
        p.setPen(ink)
        p.drawText(QPointF(bx + 10, by + 6 + fm.ascent()), header)
        y = by + 6 + line_h
        for s, name, val, det in rows:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.color_for(s["key"]))
            p.drawEllipse(QPointF(bx + 14, y + fm.height() / 2), 4, 4)
            p.setPen(ink2)
            p.drawText(QPointF(bx + 24, y + fm.ascent()), name)
            p.setPen(ink)
            p.drawText(QPointF(bx + 24 + name_w + 12 + val_w - fm.horizontalAdvance(val), y + fm.ascent()), val)
            if det:
                p.setPen(muted)
                p.drawText(QPointF(bx + 24 + name_w + 12 + val_w + 14, y + fm.ascent()), det)
            y += line_h

    # ---- mouse -----------------------------------------------------------
    def mouseMoveEvent(self, event):
        if not self.result or self._geom is None:
            return
        pos = event.position()
        key = next((k for rect, k in self._legend_hits if rect.adjusted(-4, -3, 4, 3).contains(pos)), None)
        over_plot = any(r.adjusted(0, -4, 0, 4).contains(pos) for r in self._geom["rects"].values())
        new_x = pos.x() if over_plot else None
        if key != self._hover_key or new_x != self._hover_x:
            self._hover_key, self._hover_x = key, new_x
            self.update()

    def leaveEvent(self, _event):
        if self._hover_x is not None or self._hover_key is not None:
            self._hover_x = self._hover_key = None
            self.update()
