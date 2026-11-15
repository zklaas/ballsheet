#!/usr/bin/env python3
"""The Simulator tab's player cards, drawn with QPainter in a grid that reflows with the width.

A card is what the Simulator plays a player with, laid out the way a table-game card is: the chances of each
result against an average opponent in the game's environment (a bar and a list beside the league's), then his
running against average fielders and his fielding against an average batter or runner, laid out the same way
(how often, beside the league's), and his real and simulated lines.  Batters, pitchers, and fielders in
the Range tab's usual nine who don't bat each get one (``simulator.player_cards``).  Below the tables come the
other things the games use (ALSO USED IN PLAY: the batting order, a pitcher's usage, pinch running), his real
season and notes (REFERENCE) and what he did in the simulated games (SIMULATED).  Each line and table row leads
with its category in bold (Batting, Steals, Range ...), the names the path analysis measures them by.
"""
from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

import simulator

INK = QColor("#1f2328")
MUTED = QColor("#59636e")
BORDER = QColor("#d0d7de")
UP = QColor("#1a7f37")
DOWN = QColor("#cf222e")
HEADER = (QColor("#dbe9f7"), QColor("#f8e3cc"))   # visitor, home (the path diagram's lane colours, deeper)
# one colour per result, light to dark within a family: outs greys/browns, walks blues, hits greens, HR red
RESULT_COLOURS = {"K": "#8c959f", "BB": "#5b8fd1", "HBP": "#a8c4e8", "1B": "#8fd19e", "2B": "#43a35e",
                  "3B": "#1f6f3f", "HR": "#cf4b4b", "ROE": "#e3b341", "GO": "#c9a27e", "AO": "#9a7653"}
SHORT = {"K": "K", "BB": "BB", "HBP": "HBP", "1B": "1B", "2B": "2B", "3B": "3B", "HR": "HR", "ROE": "ROE",
         "GO": "Ground out", "AO": "Air out"}
# for a batter more of these is good; for a pitcher fewer
GOOD_FOR_BATTER = {"BB", "HBP", "1B", "2B", "3B", "HR", "ROE"}

CATEGORIES = set(simulator.CATEGORY_LABELS.values())
NEUTRAL = QColor("#8c959f")   # a difference that's neither better nor worse for him (how often he tries to steal)
RATE_COLS = 112               # a running or fielding table's figures: his, the arrow, (the league's)

CARD_W = 330
GAP = 10


class CardsView(QWidget):
    """A grid of cards.  ``set_cards`` replaces them; the widget asks for the height its rows need, so it sits in
    a QScrollArea with ``widgetResizable``."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cards: list[dict] = []
        self.message = "Press Play to deal the cards."
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

    def set_cards(self, cards: list[dict], message: str = ""):
        self.cards, self.message = cards, message
        self._relayout()
        self.update()

    # ---- layout
    def _line_h(self) -> int:
        return self.fontMetrics().height()

    def card_height(self, card: dict) -> int:
        """Header (2 lines); for a batter or pitcher the bar and the five result lines; then each section's
        heading and lines."""
        lh = self._line_h()
        lines = 2
        if card.get("chances"):
            lines += 1 + 5 + len(self._wrap_line(card.get("caption", "")))
        for table in card.get("tables") or []:
            lines += len(self._wrap_line(table["caption"])) + sum(len(self._row_label(row)) for row in table["rows"])
            lines += 0.4
        for key in ("used", "reference", "result"):
            if card.get(key):
                lines += 1 + sum(len(self._wrap_line(line)) for line in card[key])
        return int(lh * lines + 30 + 6 * 3)

    def _wrap(self, text: str, width: float | None = None) -> list[str]:
        """``text`` broken at spaces into lines that fit a card (a long word is elided when drawn)."""
        fm = self.fontMetrics()
        width = width or CARD_W - 20
        lines, line = [], ""
        for word in text.split(" "):
            trial = f"{line} {word}" if line else word
            if line and fm.horizontalAdvance(trial) > width:
                lines.append(line)
                line = "   " + word  # a continued line is indented
            else:
                line = trial
        lines.append(line)
        return lines

    def _wrap_line(self, text: str, width: float | None = None) -> list[str]:
        """``_wrap`` for a card line whose category (before the colon) is drawn bold, so wider."""
        head, sep, _rest = text.partition(": ")
        extra = 0
        if sep and head in CATEGORIES:
            f = QFont(self.font())
            f.setBold(True)
            extra = QFontMetrics(f).horizontalAdvance(head + ": ") - self.fontMetrics().horizontalAdvance(head + ": ")
        return self._wrap(text, (width or CARD_W - 20) - extra - 2)

    def _row_label(self, row: dict, width: float | None = None) -> list[str]:
        """A table row's words -- its category, then what it counts -- wrapped beside the figures."""
        text = f"{simulator.CATEGORY_LABELS[row['cat']]}: {row['label']}"
        return self._wrap_line(text, (width or CARD_W - 20) - RATE_COLS)

    def _columns(self) -> int:
        return max(1, (self.width() - GAP) // (CARD_W + GAP))

    def _positions(self):
        cols = self._columns()
        x0 = GAP
        y = GAP + self._line_h() + 10  # room for the colour key
        out = []
        row_h = 0
        for i, card in enumerate(self.cards):
            c = i % cols
            if c == 0 and i:
                y += row_h + GAP
                row_h = 0
            h = self.card_height(card)
            row_h = max(row_h, h)
            out.append(QRectF(x0 + c * (CARD_W + GAP), y, CARD_W, h))
        return out, y + row_h + GAP

    def _relayout(self):
        _rects, bottom = self._positions() if self.cards else ([], 120)
        self.setMinimumHeight(int(bottom))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    # ---- painting
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("white"))
        if not self.cards:
            p.setPen(MUTED)
            p.drawText(QRectF(0, 0, self.width(), 120), Qt.AlignmentFlag.AlignCenter, self.message)
            return
        self._key(p)
        rects, _bottom = self._positions()
        clip = QRectF(self.visibleRegion().boundingRect())
        for card, rect in zip(self.cards, rects):
            if rect.intersects(clip):
                self._card(p, card, rect)

    def _key(self, p: QPainter):
        lh = self._line_h()
        x, y = GAP, GAP
        fm = p.fontMetrics()
        for cls in simulator.CLASSES:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(RESULT_COLOURS[cls])))
            p.drawRoundedRect(QRectF(x, y + 3, 12, lh - 6), 2, 2)
            p.setPen(INK)
            label = SHORT[cls]
            p.drawText(QRectF(x + 16, y, fm.horizontalAdvance(label) + 4, lh), Qt.AlignmentFlag.AlignLeft, label)
            x += 16 + fm.horizontalAdvance(label) + 14

    def _text(self, p, x, y, w, text, colour=INK, bold=False, align=Qt.AlignmentFlag.AlignLeft):
        if bold:
            f = QFont(self.font())
            f.setBold(True)
            p.setFont(f)
        p.setPen(colour)
        fm = p.fontMetrics()
        p.drawText(QRectF(x, y, w, fm.height()), align | Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(text, Qt.TextElideMode.ElideRight, int(w)))
        if bold:
            p.setFont(self.font())

    def _line(self, p, x, y, w, text, colour=INK):
        """A card line, its category (``simulator.CATEGORY_LABELS``) in bold before the colon."""
        head, sep, rest = text.partition(": ")
        if not sep or head not in CATEGORIES:
            self._text(p, x, y, w, text, colour)
            return
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        hw = p.fontMetrics().horizontalAdvance(head + ": ")
        p.setFont(self.font())
        self._text(p, x, y, hw, head + ":", colour, bold=True)
        self._text(p, x + hw, y, w - hw, rest, colour)

    def _card(self, p: QPainter, card: dict, rect: QRectF):
        lh = self._line_h()
        p.setPen(QPen(BORDER, 1))
        p.setBrush(QBrush(QColor("white")))
        p.drawRoundedRect(rect, 8, 8)
        # header band
        head = QRectF(rect.left(), rect.top(), rect.width(), lh * 2 + 10)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(HEADER[card["side"]]))
        p.drawRoundedRect(head, 8, 8)
        p.drawRect(QRectF(head.left(), head.bottom() - 8, head.width(), 8))
        x, w = rect.left() + 10, rect.width() - 20
        y = rect.top() + 5
        tag = card["tag"]
        fm = p.fontMetrics()
        tag_w = fm.horizontalAdvance(tag) + 6
        self._text(p, x, y, w - tag_w, card["name"], bold=True)
        self._text(p, x + w - tag_w, y, tag_w, tag, MUTED, align=Qt.AlignmentFlag.AlignRight)
        y += lh
        self._text(p, x, y, w, card["short"] + (f" \u00b7 {card['sub']}" if card.get("sub") else ""), MUTED)
        y = head.bottom() + 6
        if card.get("chances"):
            y = self._chances(p, card, x, y, w)
        for table in card.get("tables") or []:
            y = self._table(p, table, x, y + lh * 0.4, w)
        for key, heading, colour in (("used", "ALSO USED IN PLAY", INK),
                                     ("reference", "REFERENCE \u2014 the counts behind it, and notes" if card["kind"] == "park"
                                      else "REFERENCE \u2014 his real season, and notes", MUTED),
                                     ("result", "SIMULATED", INK)):
            lines = card.get(key)
            if not lines:
                continue
            y += 6
            small = QFont(self.font())
            small.setPointSizeF(max(self.font().pointSizeF() * 0.8, 6))
            small.setBold(True)
            p.setFont(small)
            p.setPen(MUTED)
            p.drawText(QRectF(x, y, w, lh), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, heading)
            p.setFont(self.font())
            y += lh
            for line in lines:
                for part in self._wrap_line(line, w):
                    self._line(p, x, y, w, part, colour)
                    y += lh

    def _table(self, p: QPainter, table: dict, x, y, w):
        """A running or fielding table, laid out as the chances are: what it is (the caption), then a row each --
        his figure, ▲/▼ where it differs from the league's by 5% or more (green better, red worse for him; grey
        where neither), and the league's in brackets -> the y below it."""
        lh = self._line_h()
        for part in self._wrap_line(table["caption"], w):
            self._line(p, x, y, w, part, MUTED)
            y += lh
        fm = p.fontMetrics()
        num_w = fm.horizontalAdvance("00.0%") + 4
        for row in table["rows"]:
            parts = self._row_label(row, w)
            for i, part in enumerate(parts):
                self._line(p, x, y + i * lh, w - RATE_COLS, part)
            nx = x + w - RATE_COLS + 4
            mine, lg = row["mine"], row["league"]
            self._text(p, nx, y, num_w, f"{mine:.1%}", align=Qt.AlignmentFlag.AlignRight)
            ax = nx + num_w + 2
            if lg and abs(mine / lg - 1) >= 0.05:
                up = mine > lg
                if row["good"] is None:
                    colour = NEUTRAL
                else:
                    colour = UP if up == (row["good"] == "higher") else DOWN
                self._text(p, ax, y, 12, "\u25b2" if up else "\u25bc", colour)
            self._text(p, ax + 13, y, x + w - ax - 13, f"({lg * 100:.1f})", MUTED)
            y += lh * len(parts)
        return y

    def _chances(self, p: QPainter, card: dict, x, y, w):
        """The bar and the two columns of results -> the y below them."""
        lh = self._line_h()
        chances = card["chances"]
        for part in self._wrap_line(card.get("caption", ""), w):
            self._line(p, x, y, w, part, MUTED)
            y += lh
        bar = QRectF(x, y, w, lh * 0.9)
        cx = bar.left()
        for cls in simulator.CLASSES:
            seg = bar.width() * chances.get(cls, 0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(RESULT_COLOURS[cls])))
            p.drawRect(QRectF(cx, bar.top(), seg, bar.height()))
            cx += seg
        y += lh + 4
        col_w = w / 2
        if card["kind"] in ("batter", "bench"):
            better = lambda cls, mine, lg: (mine > lg) == (cls in GOOD_FOR_BATTER)
        elif card["kind"] == "park":
            better = None  # a park is neither side's
        else:
            better = lambda cls, mine, lg: (mine < lg) == (cls in GOOD_FOR_BATTER)
        fm = p.fontMetrics()
        name_w, num_w = fm.horizontalAdvance("ROE") + 6, fm.horizontalAdvance("00.0%") + 4
        for i, cls in enumerate(simulator.CLASSES):
            cxx = x + (i // 5) * col_w
            cy = y + (i % 5) * lh
            mine, lg = chances.get(cls, 0), card["league"].get(cls, 0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(RESULT_COLOURS[cls])))
            p.drawRoundedRect(QRectF(cxx, cy + 3, 9, lh - 6), 2, 2)
            self._text(p, cxx + 13, cy, name_w, cls)
            nx = cxx + 13 + name_w
            self._text(p, nx, cy, num_w, f"{mine:.1%}", align=Qt.AlignmentFlag.AlignRight)
            ax = nx + num_w + 2
            if lg and abs(mine / lg - 1) >= 0.05:  # the arrow says which way, the colour whether that's good for him
                colour = NEUTRAL if better is None else UP if better(cls, mine, lg) else DOWN
                self._text(p, ax, cy, 12, "\u25b2" if mine > lg else "\u25bc", colour)
            self._text(p, ax + 13, cy, cxx + col_w - ax - 13, f"({lg * 100:.1f})", MUTED)
        return y + 5 * lh
