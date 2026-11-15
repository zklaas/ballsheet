#!/usr/bin/env python3
"""Draw BallSheet's app icon: a baseball rising out of a spreadsheet.

    python3 make_icons.py

Writes icons/ballsheet.png (512) plus -256, -128, -64 variants, matching the
naming that install-desktop.sh expects.  Each size is drawn from scratch (not
downscaled) so the small tiles stay crisp.
"""
import math
import os
import sys

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QGuiApplication, QLinearGradient,
                         QPainter, QPainterPath, QPen, QPixmap)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "icons")

CARD_TOP = QColor("#fbf9f3")
CARD_BOT = QColor("#e9e1cf")
GRID_LINE = QColor("#93accb")
GRID_FILL = QColor("#dfe9f5")
HEADER_FILL = QColor("#3f6fb0")
EMPTY_CELL = QColor("#b9c8dc")
BAR = QColor("#e08a3c")
BALL_FILL = QColor("#ffffff")
BALL_EDGE = QColor("#c4c4c4")
SEAM = QColor("#d21f3c")
SHADOW = QColor(30, 42, 66, 60)
MOTION = QColor(150, 132, 96, 150)


def _lerp(a, b, t):
    return a + (b - a) * t


def _seam_path(cx, cy, r, side):
    """One curved seam whose belly bows toward the centre: ')(' facing pair."""
    sx = -1 if side == "left" else 1
    p = QPainterPath()
    p.moveTo(cx + sx * 0.78 * r, cy - 0.58 * r)
    p.cubicTo(cx + sx * 0.16 * r, cy - 0.30 * r,
              cx + sx * 0.16 * r, cy + 0.30 * r,
              cx + sx * 0.78 * r, cy + 0.58 * r)
    return p


def _draw_seam(painter, path, r):
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(SEAM, max(1.4, r / 26), Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap))
    painter.drawPath(path)

    tick = r / 12
    painter.setPen(QPen(SEAM, max(1.0, r / 42), Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap))
    n = 8
    for i in range(1, n):
        t = i / n
        pt = path.pointAtPercent(t)
        ang = math.radians(path.angleAtPercent(t))
        tx, ty = math.cos(ang), -math.sin(ang)      # tangent
        nx, ny = -ty, tx                            # normal
        for s in (0.5, -0.5):                       # a little V of stitching
            painter.drawLine(
                QPointF(pt.x() + nx * tick + tx * tick * s,
                        pt.y() + ny * tick + ty * tick * s),
                QPointF(pt.x() - nx * tick + tx * tick * s,
                        pt.y() - ny * tick + ty * tick * s),
            )


def render(size: int) -> QPixmap:
    S = size
    pm = QPixmap(S, S)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    # --- rounded-square card -------------------------------------------
    grad = QLinearGradient(0, S * 0.03, 0, S * 0.97)
    grad.setColorAt(0.0, CARD_TOP)
    grad.setColorAt(1.0, CARD_BOT)
    p.setBrush(QBrush(grad))
    p.setPen(Qt.PenStyle.NoPen)
    rad = S * 0.16
    p.drawRoundedRect(QRectF(S * 0.03, S * 0.03, S * 0.94, S * 0.94), rad, rad)

    # --- spreadsheet: a plane tilting away ---------------------------
    back_y, front_y = S * 0.56, S * 0.90
    back_l, back_r = S * 0.30, S * 0.70
    front_l, front_r = S * 0.11, S * 0.89
    cols, rows = 6, 5

    def edges(row):                      # left/right x at a given row fraction
        y = _lerp(back_y, front_y, row)
        lo = _lerp(back_l, front_l, row)
        hi = _lerp(back_r, front_r, row)
        return lo, hi, y

    def cell_quad(rr, cc):
        l0, r0, y0 = edges(rr / rows)
        l1, r1, y1 = edges((rr + 1) / rows)
        q = QPainterPath()
        q.moveTo(_lerp(l0, r0, cc / cols), y0)
        q.lineTo(_lerp(l0, r0, (cc + 1) / cols), y0)
        q.lineTo(_lerp(l1, r1, (cc + 1) / cols), y1)
        q.lineTo(_lerp(l1, r1, cc / cols), y1)
        q.closeSubpath()
        return q

    plane = QPainterPath()
    plane.moveTo(back_l, back_y)
    plane.lineTo(back_r, back_y)
    plane.lineTo(front_r, front_y)
    plane.lineTo(front_l, front_y)
    plane.closeSubpath()
    p.setBrush(QBrush(GRID_FILL))
    p.drawPath(plane)

    p.save()
    p.setClipPath(plane)

    # header row
    p.setBrush(QBrush(HEADER_FILL))
    for cc in range(cols):
        p.drawPath(cell_quad(0, cc))

    # one "empty" cell at the ball's base, then a scatter of in-cell bars
    p.setBrush(QBrush(EMPTY_CELL))
    p.drawPath(cell_quad(1, 3))

    bars = {1: (0, 1, 2, 4, 5), 2: (0, 1, 2, 4, 5), 3: (0, 2, 3, 4, 5), 4: range(6)}
    heights = [0.5, 0.7, 0.42, 0.62, 0.8, 0.55, 0.48, 0.66, 0.58, 0.74,
               0.45, 0.6, 0.5, 0.7, 0.4, 0.64, 0.52, 0.72]
    p.setBrush(QBrush(BAR))
    hi = 0
    for rr, ccs in bars.items():
        l0, r0, y0 = edges(rr / rows)
        _, _, y1 = edges((rr + 1) / rows)
        for cc in ccs:
            h = heights[hi % len(heights)] * (y1 - y0) * 0.6
            hi += 1
            cl = _lerp(l0, r0, (cc + 0.22) / cols)
            cw = (r0 - l0) / cols * 0.56
            p.drawRect(QRectF(cl, y1 - (y1 - y0) * 0.18 - h, cw, h))

    # grid lines
    p.setPen(QPen(GRID_LINE, max(1.0, S / 210)))
    for c in range(cols + 1):
        p.drawLine(QPointF(_lerp(back_l, back_r, c / cols), back_y),
                   QPointF(_lerp(front_l, front_r, c / cols), front_y))
    for rw in range(rows + 1):
        l0, r0, y0 = edges(rw / rows)
        p.drawLine(QPointF(l0, y0), QPointF(r0, y0))
    p.restore()

    # --- contact shadow on the sheet -------------------------------
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(SHADOW))
    p.drawEllipse(QRectF(S * 0.35, S * 0.60, S * 0.30, S * 0.065))

    # --- rising motion ticks ------------------------------------
    p.setPen(QPen(MOTION, max(1.3, S / 130), Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap))
    for x, y0, y1 in [(0.30, 0.55, 0.44), (0.345, 0.60, 0.50),
                      (0.655, 0.60, 0.50), (0.70, 0.55, 0.44)]:
        p.drawLine(QPointF(S * x, S * y0), QPointF(S * x, S * y1))

    # --- the baseball --------------------------------------
    d = S * 0.44
    cx, cy = S * 0.5, S * 0.40
    r = d / 2
    ball = QRectF(cx - r, cy - r, d, d)
    p.setPen(QPen(BALL_EDGE, max(1.0, S / 170)))
    p.setBrush(QBrush(BALL_FILL))
    p.drawEllipse(ball)

    p.setPen(Qt.PenStyle.NoPen)                     # soft sheen
    p.setBrush(QColor(255, 255, 255, 120))
    p.drawEllipse(QRectF(cx - r * 0.42, cy - r * 0.5, r * 0.7, r * 0.7))

    _draw_seam(p, _seam_path(cx, cy, r, "left"), r)
    _draw_seam(p, _seam_path(cx, cy, r, "right"), r)

    p.end()
    return pm


def main():
    app = QGuiApplication(sys.argv)  # noqa: F841 - keep the instance alive
    os.makedirs(OUT, exist_ok=True)
    for size in (512, 256, 128, 64, 48):
        name = "ballsheet.png" if size == 512 else f"ballsheet-{size}.png"
        path = os.path.join(OUT, name)
        render(size).save(path, "PNG")
        print("wrote", path)


if __name__ == "__main__":
    main()
