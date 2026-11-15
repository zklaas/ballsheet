#!/usr/bin/env python3
"""BallSheet -- a PyQt6 desk for MLB batter, pitcher and team numbers.

Pick a team, then walk its history one season at a time with the arrow
buttons (or Ctrl+Left / Ctrl+Right).  Views:

  * Batting / Pitching   -- one row per player, sortable (base-running lives
                             in Batting; a separate Advanced, Sabermetrics
                             and Expected view of each adds BABIP/ISO-style
                             detail, WAR/wOBA/FIP, and Statcast xStats
  * Fielding             -- one row per player per position, sortable
  * Team totals          -- the franchise's aggregate batting, pitching and
                             fielding lines

Advanced/Sabermetrics/Expected are separate statTypes the MLB API keeps next
to the plain box score -- not every group has all three (fielding has no
advanced tier through this endpoint, so there's no Fielding variant).

Data comes live from the official MLB Stats API and is cached on disk, so
finished seasons load instantly on the second visit and the current season
refreshes every few minutes.  Export the visible view to CSV, or export every
season of a team to a multi-sheet .xlsx workbook (one sheet per year).

Run it with:  ballsheet
"""
from __future__ import annotations

import csv
import datetime as _dt
import html
import json
import math
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

try:
    from PyQt6.QtCore import (QEvent, QObject, QPointF, QRectF, QRunnable, Qt, QThreadPool, QTimer,
                              QSortFilterProxyModel, pyqtSignal)
    from PyQt6.QtGui import (QAction, QBrush, QColor, QFont, QFontDatabase, QFontMetrics, QImage, QKeySequence,
                             QPainter, QPalette, QPen, QPolygonF, QShortcut, QStandardItem, QStandardItemModel)
    from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
                                 QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                                 QLineEdit, QListWidget,
                                 QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                                 QProgressBar, QProgressDialog, QProxyStyle, QPushButton, QRadioButton, QScrollArea, QScrollBar, QSpinBox,
                                 QSizePolicy, QSplitter, QStackedWidget, QStyle, QStyleOptionTab, QStylePainter, QTabBar, QTableView, QTableWidget,
                                 QTableWidgetItem, QTabWidget, QTextBrowser,
                                 QToolButton, QToolTip, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
                                 QWidgetAction)
except ImportError:  # pragma: no cover
    sys.exit("BallSheet needs PyQt6.  Install it with:  pip install --user PyQt6")

import factor_analysis
import formula
import infochart
import infoquery
import mlbstats
import statcastview
import wpachart
import naturalzone
import stadiums
import regression
import retrosheet
import simcards
import simpath
import simulator
import uptodate
from xlsxlite import Workbook

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "icons", "ballsheet.png")
HOME = os.path.expanduser("~")
LIVE_TTL = 600  # seconds before a current-season response is refetched

# The app's text size, in points relative to the desktop's (View menu; takes effect at the next
# start, since the fonts are worked out from it as the window is built).  Smaller than the
# desktop by default: the window has to fit short screens (1280x624 here).
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".config", "ballsheet", "settings.json")
DEFAULT_TEXT_OFFSET = -2
MIN_TEXT_POINTS = 7.0


class _CompactRows(QProxyStyle):
    """The desktop's own style, with table rows only as tall as their text needs (the default
    leaves a lot of air, which a short screen can't spare)."""

    def pixelMetric(self, metric, option=None, widget=None):
        if metric == QStyle.PixelMetric.PM_HeaderDefaultSectionSizeVertical:
            return QFontMetrics(widget.font() if widget is not None else QApplication.font()).height() + 6
        return super().pixelMetric(metric, option, widget)


def _load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_settings(data: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
TEXT_KEYS = {"inningsPitched", "innings"}  # thirds-encoded: keep "172.1" as text

# Head-To-Head tab: (key of the batter's "for" number in the Batting view, label,
# name of the player-level custom stat that gives the pitcher's matching
# "against" number, JSON file in this folder that defines that custom stat).
H2H_STATS = [
    ("avg", "AVG", "Batting Average Against", "custom-batting-average-against.json"),
    ("obp", "OBP", "On-Base Average Against", "custom-on-base-average-against.json"),
    ("slg", "SLG", "Slugging Average Against", "custom-slugging-average-against.json"),
]

# Production tab: the player-level custom stat it reports, and the JSON file in this
# folder that defines it (auto-loaded at startup, like the H2H_STATS files).
PRODUCTION_STAT = "Runs Produced"
PRODUCTION_FILE = "runs-produced-player.json"
# Prevention tab: (column, player-level custom stat behind it, JSON file defining it, table heading).
PREVENTION_STATS = [
    ("R/9", "Runs per Nine Innings", "runs-per-nine-innings-player.json", "Runs per\nNine Innings"),
    ("ERA", "Earned Run Average", "earned-run-average-player.json", "Earned Run\nAverage"),
    ("UR/9", "Unearned Runs per Nine Innings", "unearned-runs-per-nine-innings-player.json",
     "Unearned Runs\nper Nine Innings"),
]
PREVENTION_RATIO_STAT = "R/9"  # the one compared with MLB's figure

# Speed tab: (column, player-level custom stat behind it, JSON file defining it).  Bases Gained per
# Opportunity is the figure compared with MLB's.  Opportunities has no column of its own here: it is the
# denominator (and what runners are listed by); without its definition the built-in count is used.
SPEED_STATS = [
    ("XB", "Extra Bases", "extra-bases-player.json"),
    ("TU", "Tag Ups", "tag-ups-player.json"),
    ("BG", "Bases Gained", "bases-gained-player.json"),
    ("OPP", "Opportunities", "opportunities-player.json"),
]

# Denied tab's 19th-century toggle: 1901 is the usual dividing line for "modern" baseball, so
# 1900 goes with the 19th century.
DENIED_MODERN_FROM = 1901

# Denied tab's "Got as far as" choices: (label, key) -- a key of mlbstats.DENIED_STAGES, or
# "any", "champion" (won the final), "none" (never reached the postseason, tiebreaker losers too).
DENIED_STAGE_CHOICES = [("Any round", "any"), ("Won it all", "champion"), ("Final series", "final"),
                        ("Championship series", "L"), ("Division series", "D"), ("Wild card round", "F"),
                        ("Didn't reach the postseason", "none")]

# Standings tab's Pythagorean columns: (column, team-level custom stat behind it, JSON file defining it).
PYTHAGOREAN_STATS = [
    ("PW", "Pythagorean Wins", "pythagorean-wins-team.json"),
    ("PL", "Pythagorean Losses", "pythagorean-losses-team.json"),
    ("PPct", "Pythagorean Win-Loss Percentage", "pythagorean-win-loss-percentage-team.json"),
]
# ...and the two-line heading each gets in the table (the word in full, so it can't be mistaken for W, L, Pct).
PYTHAGOREAN_HEADERS = {"PW": "Pythagorean\nWins", "PL": "Pythagorean\nLosses", "PPct": "Pythagorean\nWin-Loss Pct"}
# Figures other tabs work out in code, also offered as custom stats (JSON files in this folder, each
# a list of definitions, auto-loaded at startup) so they can be used on the Custom, Correlation and
# Regression tabs.  The Retrosheet ones read the Relief, Batted In and Safe/Out (Retrosheet) variables.
TAB_DERIVED_FILES = [
    "unearned-runs-player.json",                        # Prevention's UR
    "runs-produced-per-plate-appearance-player.json",   # Production's RP / PA
    "bases-gained-per-opportunity-player.json",         # Speed's Gained per Opportunity
    "bullpen-support-player.json",                      # Bullpen Support: IP per start, W-L %, differential ...
    "bullpen-support-team.json",                        # ... and the same for a whole staff
    "safe-out-team.json",                               # Safe/Out's per-inning rates and ratios, and allowed
    "players-batted-in-player.json",                    # Players Batted In: shares by base
]
# The Score stats: each tab's green/red comparison with MLB as a player-level custom stat, counted
# only with enough playing time, and their combination -- loaded after the stats they're built on.
SCORE_FILES = [
    "production-score-player.json",    # the Production tab's runs produced per PA, against MLB's
    "prevention-score-player.json",    # MLB's runs per nine innings, against the Prevention tab's
    "range-score-player.json",         # the Range tab's chances made, against an average fielder's
    "speed-score-player.json",         # the Speed tab's bases gained per opportunity
    "overall-score-player.json",       # the qualifying scores averaged
]
ORDINALS = ("1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th")


# --------------------------------------------------------------------------- #
#  Background workers                                                         #
# --------------------------------------------------------------------------- #
class _Signals(QObject):
    ok = pyqtSignal(int, object)
    err = pyqtSignal(int, str)


class _SimProgress(QObject):
    """The Simulator's games played so far, sent from the worker thread: (run, done, total); total 0 = still
    loading the teams."""
    progress = pyqtSignal(int, int, int)


class _DownloadNote(QObject):
    """A download starting, going on or ending, from whatever thread (``retrosheet.DOWNLOAD_HOOKS``): (what, bytes
    so far, size or -1, done)."""
    note = pyqtSignal(str, int, int, bool)


class _JobRelease(QObject):
    """Hands a finished job back to the window (queued after the job's own signals)."""
    done = pyqtSignal(object)


class _Held(QRunnable):
    """Runs ``job`` on the pool while the window keeps a reference to it (and so to its signal
    object) until it has finished and what it sent has arrived.  Left to the pool alone, the Python
    side of a job could be collected mid-run (more so under Python 3.14's collector), deleting its
    signals under it -- "wrapped C/C++ object ... has been deleted", or a segmentation fault."""

    def __init__(self, job: QRunnable, release: _JobRelease):
        super().__init__()
        self.job, self.release = job, release
        self.setAutoDelete(False)

    def run(self):  # noqa: D401
        try:
            self.job.run()
        finally:
            self.release.done.emit(self)


class _Job(QRunnable):
    """Run ``fn()`` off the UI thread; tag the result with ``token``."""

    def __init__(self, token: int, fn):
        super().__init__()
        self.token, self.fn, self.sig = token, fn, _Signals()

    def run(self):  # noqa: D401
        try:
            self.sig.ok.emit(self.token, self.fn())
        except Exception as exc:  # noqa: BLE001 - report anything to the UI
            self.sig.err.emit(self.token, str(exc))


class _ExportSignals(QObject):
    progress = pyqtSignal(int, int, str)
    done = pyqtSignal(str)
    failed = pyqtSignal(str)


class _FactorSignals(QObject):
    progress = pyqtSignal(int, int, str)
    done = pyqtSignal(object)  # (FactorResult, sample_desc)
    failed = pyqtSignal(str)


class _StepSignal(QObject):
    """Progress from a worker thread to the window (a signal is safe to emit from any thread)."""
    step = pyqtSignal(object)


class _UptodateJob(QRunnable):
    """Bring this year's Uptodate file up to date off the UI thread (:func:`uptodate.update`)."""

    def __init__(self, season: int, path: str):
        super().__init__()
        self.season, self.path = season, path
        self.sig = _FactorSignals()  # progress(done, total, text), done(summary dict), failed(str)
        self.stop = threading.Event()

    def run(self):  # noqa: D401
        try:
            result = uptodate.update(self.season, self.path, cancelled=self.stop.is_set,
                                     progress=lambda done, total, text: self.sig.progress.emit(done, total, text))
            self.sig.done.emit(result)
        except Exception as exc:  # noqa: BLE001 - report anything to the UI
            self.sig.failed.emit(str(exc))


class _ExportJob(QRunnable):
    def __init__(self, kind, team_id, seasons, view, path, live_from):
        super().__init__()
        self.kind, self.team_id, self.seasons = kind, team_id, seasons
        self.view, self.path, self.live_from = view, path, live_from
        self.sig = _ExportSignals()
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _ttl(self, year):
        return LIVE_TTL if year >= self.live_from else None

    def run(self):
        try:
            wb = Workbook()
            total = len(self.seasons)
            if self.kind == "players":
                group, stat_type, cols = mlbstats.VIEW_SPECS[self.view]
                for i, year in enumerate(self.seasons):
                    if self._cancel:
                        return self.sig.failed.emit("Cancelled.")
                    self.sig.progress.emit(i, total, f"{year} — {self.view}")
                    rows = mlbstats.get_player_stats(self.team_id, year, group,
                                                     self._ttl(year), stat_type=stat_type)
                    ws = wb.add_sheet(str(year))
                    ws.append([h for h, _ in cols], header=True)
                    if not rows:
                        ws.append(["(no data for this season)"])
                    for row in rows:
                        ws.append([_cell(k, row.get(k, "")) for _, k in cols])
            else:  # team totals, one row per season
                hf = mlbstats.TEAM_HITTING_FIELDS
                pf = mlbstats.TEAM_PITCHING_FIELDS
                ff = mlbstats.TEAM_FIELDING_FIELDS
                ws = wb.add_sheet("Team totals")
                ws.append(["Season", "Team"]
                          + [f"B: {l}" for l, _ in hf]
                          + [f"P: {l}" for l, _ in pf]
                          + [f"F: {l}" for l, _ in ff],
                          header=True)
                for i, year in enumerate(self.seasons):
                    if self._cancel:
                        return self.sig.failed.emit("Cancelled.")
                    self.sig.progress.emit(i, total, f"{year} — team totals")
                    h = mlbstats.get_team_totals(self.team_id, year, "hitting", self._ttl(year))
                    p = mlbstats.get_team_totals(self.team_id, year, "pitching", self._ttl(year))
                    f = mlbstats.get_team_totals(self.team_id, year, "fielding", self._ttl(year))
                    if not h and not p and not f:
                        continue
                    era = mlbstats.get_team_map(year, self._ttl(year)).get(self.team_id, {})
                    ws.append([year, era.get("name", "")]
                              + [_cell(k, h.get(k, "")) for _, k in hf]
                              + [_cell(k, p.get(k, "")) for _, k in pf]
                              + [_cell(k, f.get(k, "")) for _, k in ff])
            self.sig.progress.emit(total, total, "writing workbook…")
            wb.save(self.path)
            self.sig.done.emit(self.path)
        except Exception as exc:  # noqa: BLE001
            self.sig.failed.emit(str(exc))


def _cell(key: str, value):
    """Coerce an API value for a spreadsheet cell."""
    if key in TEXT_KEYS:
        return "" if value is None else str(value)
    num = mlbstats.as_number(value)
    if num is not None:
        return num
    return "" if value is None else str(value)


def _monospace_font():
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


def _fmt_p(p: float) -> str:
    if p != p:  # nan
        return "n/a"
    return "<.0001" if p < 0.0001 else f"{p:.4f}"


def _fmt_rate(value: float | None, signed: bool = False) -> str:
    """Baseball style: ``.288`` (no leading zero), ``+.012`` / ``-.023`` when signed."""
    if value is None:
        return "n/a"
    text = f"{abs(value):.3f}"
    if text.startswith("0."):
        text = text[1:]
    if value < 0 and text != ".000":
        return "-" + text
    return ("+" + text) if signed else text


def _format_regression(result, dep_label: str, sample_desc: str) -> str:
    lines = [f"Dependent variable: {dep_label}", sample_desc, ""]
    name_w = max(len("Predictor"), max(len(l) for l in result.labels))
    header = f"{'Predictor':<{name_w}}  {'Coef':>13}  {'Std Err':>12}  {'t':>8}  {'p':>8}"
    lines.append(header)
    lines.append("-" * len(header))
    for label, coef, se, t, p in zip(result.labels, result.coefficients, result.std_errors,
                                     result.t_stats, result.p_values):
        t_str = f"{t:.3f}" if t == t else "n/a"
        lines.append(f"{label:<{name_w}}  {coef:>13.4f}  {se:>12.4f}  {t_str:>8}  {_fmt_p(p):>8}")
    lines.append("")
    df_resid = result.n - result.k - 1
    f_str = f"{result.f_stat:.3f}" if result.f_stat == result.f_stat else "n/a"
    lines.append(f"n = {result.n}    R² = {result.r_squared:.4f}    "
                 f"Adj R² = {result.adj_r_squared:.4f}")
    lines.append(f"F({result.k}, {df_resid}) = {f_str}    p = {_fmt_p(result.f_p_value)}    "
                 f"Residual std error = {result.residual_std_error:.4f}")
    return "\n".join(lines)


def _format_factor_analysis(result, sample_desc: str, labels: list[str] | None = None) -> str:
    """``labels`` is one string per factor (may be blank) -- the user's own
    name for each factor, applied after looking at its top loaders; omitted
    (or all-blank) the first time a result is shown, before any are typed."""
    labels = labels or [""] * result.n_factors

    def fhead(f: int) -> str:
        return f"F{f + 1} ({labels[f]})" if labels[f] else f"F{f + 1}"

    lines = [sample_desc, ""]
    conv = "converged" if result.converged else "stopped (did not fully converge)"
    lines.append(f"n = {result.n}    variables = {result.k}    factors extracted = {result.n_factors}"
                 f"    ({conv} after {result.iterations} iteration(s))")
    lines.append(f"Kaiser criterion (eigenvalue > 1) suggests {result.suggested_factors} factor(s) -- "
                 "correlation-matrix eigenvalues: "
                 + ", ".join(f"{v:.3f}" for v in result.raw_eigenvalues))
    lines.append("")
    lines.append("Rotated factor loadings (varimax) and communalities:")
    name_w = max(len("Variable"), max(len(l) for l in result.labels))
    col_w = max(8, max((len(fhead(f)) for f in range(result.n_factors)), default=8))
    header = f"{'Variable':<{name_w}}  " + "  ".join(f"{fhead(f):>{col_w}}" for f in range(result.n_factors))
    header += f"  {'h2':>8}"
    lines.append(header)
    lines.append("-" * len(header))
    for i, label in enumerate(result.labels):
        row = f"{label:<{name_w}}  " + "  ".join(
            f"{result.loadings[i][f]:>{col_w}.3f}" for f in range(result.n_factors))
        row += f"  {result.communalities[i]:>8.3f}"
        lines.append(row)
    lines.append("")
    lines.append("Variance explained: " + "  ".join(
        f"{fhead(f)}={result.variance_explained[f] * 100:.1f}%" for f in range(result.n_factors))
        + f"   total={sum(result.variance_explained) * 100:.1f}%")
    lines.append("")
    lines.append("Top-loading variables per factor (the strongest hint at what each factor "
                 "represents -- pick a label for it below):")
    for f in range(result.n_factors):
        top = factor_analysis.top_loadings(result, f, 5)
        lines.append(f"  {fhead(f)}: " + ", ".join(f"{lab} ({val:+.3f})" for lab, val in top))
    return "\n".join(lines)


def _format_correlation(labels: list[str], matrix: list[list[float]], n: int,
                        sample_desc: str) -> str:
    """A Pearson correlation matrix (r), a variance-explained matrix (r²),
    and a "strongest relationships" list -- with more than a handful of
    variables the raw matrices get hard to scan, so the ranked list is what
    actually answers "what's related to what"."""
    name_w = max(len("Variable"), max(len(l) for l in labels))
    col_w = max(7, name_w)

    def matrix_block(title: str, values: list[list[float]]) -> list[str]:
        header = f"{'Variable':<{name_w}}  " + "  ".join(f"{l:>{col_w}}" for l in labels)
        block = [title, header, "-" * len(header)]
        for i, label in enumerate(labels):
            row = f"{label:<{name_w}}  " + "  ".join(
                f"{values[i][j]:>{col_w}.3f}" for j in range(len(labels)))
            block.append(row)
        return block

    r_squared = [[v * v for v in row] for row in matrix]

    lines = [sample_desc, "", f"n = {n}    variables = {len(labels)}", ""]
    lines += matrix_block("Correlation (r):", matrix)
    lines.append("")
    lines += matrix_block("Variance explained (r²):", r_squared)

    pairs = [(labels[i], labels[j], matrix[i][j])
            for i in range(len(labels)) for j in range(i + 1, len(labels))]
    pairs.sort(key=lambda t: -abs(t[2]))
    lines.append("")
    lines.append("Strongest relationships (by |r|):")
    if not pairs:
        lines.append("  (only one variable checked -- nothing to correlate it against)")
    for a, b, r in pairs[:20]:
        lines.append(f"  {a}  <->  {b}   r = {r:+.3f}   r² = {r * r:.3f}")
    return "\n".join(lines)


class _RegressionJob(QRunnable):
    """Build the observation matrix for one regression, then run OLS.

    ``kind`` picks which (team, season) pairs make up the sample:
      * "history" -- one team, across every season it has existed.
      * "league"  -- one season, across every team that played it.
      * "roster"  -- exactly one (team, season) pair.
    ``level`` picks the row granularity within that sample:
      * "team"   -- one row per team-season: the dependent variable is a Team
                    totals number, predictors are rolled up across that
                    team-season's roster.  (Not valid for "roster" -- a single
                    team-season would be exactly one point.)
      * "player" -- one row per player (within each team-season the ``kind``
                    visits): dependent variable and predictors are both
                    per-player, joined by player id (a Fielding row-per-
                    position is rolled up to one number per player first).
    Either way, a season/team/player missing any needed value (listwise
    deletion) is dropped and counted in the report rather than silently
    biasing the fit.
    """

    def __init__(self, kind, level, team_id, team_name, season, first_year,
                dep_source, dep_key, dep_label, predictors, live_from, dep_agg=None):
        super().__init__()
        self.kind, self.level = kind, level
        self.team_id, self.team_name = team_id, team_name
        self.season, self.first_year = season, first_year
        # level="team": dep_source is a Team-totals group ("hitting" etc), dep_agg unused.
        # level="player": dep_source is a player-list view name ("Batting
        # (Sabermetrics)" etc, same namespace as a predictor's view) and
        # dep_agg is its "sum"/"avg" roll-up rule.
        self.dep_source, self.dep_key, self.dep_label, self.dep_agg = (
            dep_source, dep_key, dep_label, dep_agg)
        self.predictors = predictors  # [(view, key, agg, label), ...]
        self.live_from = live_from
        self.sig = _ExportSignals()

    def _ttl(self, year):
        return LIVE_TTL if year >= self.live_from else None

    def _observations(self):
        """-> list of (team_id, season, label): the team-seasons to visit."""
        return _sample_observations(self.kind, self.team_id, self.team_name,
                                    self.season, self.first_year, self.live_from)

    def run(self):
        try:
            if self.level == "player":
                self._run_player_level()
            else:
                self._run_team_level()
        except regression.RegressionError as exc:
            self.sig.failed.emit(str(exc))
        except mlbstats.MLBStatsError as exc:
            self.sig.failed.emit(f"Network/API error: {exc}")
        except Exception as exc:  # noqa: BLE001
            self.sig.failed.emit(str(exc))

    def _run_team_level(self):
        """One row per team-season: dependent variable is a Team totals
        number, predictors are rolled up across that team-season's roster.
        A custom stat (dependent or predictor) is instead evaluated straight
        from every Team totals field -- it's already one number per
        team-season, no roster roll-up needed."""
        obs = self._observations()
        total = len(obs)
        is_dep_custom = self.dep_source == "__custom__"
        has_custom_predictor = any(view == "__custom__" for view, _k, _a, _l in self.predictors)
        needs_team_lookup = is_dep_custom or has_custom_predictor
        team_names = set().union(*(formula.formula_variables(key) for view, key, _a, _l in self.predictors
                                   if view == "__custom__"),
                                 formula.formula_variables(self.dep_key) if is_dep_custom else set())
        views_needed = sorted({view for view, _k, _a, _l in self.predictors if view != "__custom__"})
        y_vals, x_rows = [], []
        skipped = 0
        for i, (team_id, season, label) in enumerate(obs):
            self.sig.progress.emit(i, total, label)
            ttl = self._ttl(season)
            team_lookup = mlbstats.team_totals_lookup(team_id, season, ttl, team_names) if needs_team_lookup else None
            try:
                if is_dep_custom:
                    # self.dep_key holds the formula text for a custom stat.
                    y = formula.evaluate_formula(self.dep_key, team_lookup)
                else:
                    team_totals = mlbstats.get_team_totals(team_id, season, self.dep_source, ttl)
                    y = mlbstats.as_number(team_totals.get(self.dep_key))
            except mlbstats.MLBStatsError:
                # e.g. a season before this franchise actually took the field --
                # some expansion teams' recorded "first year" is off by a season
                # or two, so a 404 here means "no data", not "give up".
                skipped += 1
                continue
            if y is None:
                skipped += 1
                continue
            try:
                rows_by_view = {}
                for view in views_needed:
                    group, stat_type, _cols = mlbstats.VIEW_SPECS[view]
                    rows_by_view[view] = mlbstats.get_player_stats(
                        team_id, season, group, ttl, stat_type=stat_type)
            except mlbstats.MLBStatsError:
                skipped += 1
                continue
            x_row, complete = [], True
            for view, key, agg, _label in self.predictors:
                val = (formula.evaluate_formula(key, team_lookup) if view == "__custom__"
                      else mlbstats.aggregate_rows(rows_by_view[view], key, agg))
                if val is None:
                    complete = False
                    break
                x_row.append(val)
            if not complete:
                skipped += 1
                continue
            y_vals.append(y)
            x_rows.append(x_row)

        self.sig.progress.emit(total, total, "running regression…")
        labels = [label if view == "__custom__" else f"{view}: {label}"
                 for view, _key, _agg, label in self.predictors]
        result = regression.multiple_regression(y_vals, x_rows, labels)

        if self.kind in SEASON_SCOPES:
            sample_desc = (f"Sample: {self.season} season, {_all_teams(_scope_label(self.kind, self.season, self.team_id, self.live_from))} "
                           f"({result.n} of {total} teams used, {skipped} skipped "
                           "for missing/incomplete data)")
        else:
            sample_desc = (f"Sample: {self.team_name}, {self.first_year}-{self.live_from} "
                           f"({result.n} of {total} seasons used, {skipped} skipped "
                           "for missing/incomplete data)")
        self.sig.done.emit(_format_regression(result, self.dep_label, sample_desc))

    def _run_player_level(self):
        """One row per player, pooled across every team-season ``_observations``
        visits: one team's whole history ("history"), the whole league in one
        season ("league"), or a single roster ("roster").  Within each
        team-season, dependent variable and predictors are joined by player id."""
        obs = self._observations()
        total_groups = len(obs)
        is_dep_custom = self.dep_source == "__custom__"
        # self.dep_key holds the formula text for a custom stat; it needs
        # every view its "{View: Label}" variables touch, not just one view.
        dep_formula_vars = formula.formula_variables(self.dep_key) if is_dep_custom else None
        dep_views = ({mlbstats.REGRESSION_VARIABLE_INDEX[v][0] for v in dep_formula_vars}
                    if is_dep_custom else {self.dep_source})

        # Same idea per predictor: a custom one's formula vars fold into the
        # views fetched, and it's evaluated (not aggregate_rows-rolled-up)
        # per player at the bottom of the loop.
        predictor_formula_vars: dict[int, set[str]] = {}
        predictor_views = set()
        for idx, (view, key, _agg, _label) in enumerate(self.predictors):
            if view == "__custom__":
                fvars = formula.formula_variables(key)
                predictor_formula_vars[idx] = fvars
                predictor_views |= {mlbstats.REGRESSION_VARIABLE_INDEX[v][0] for v in fvars}
            else:
                predictor_views.add(view)

        views_needed = sorted(dep_views | predictor_views)
        y_vals, x_rows = [], []
        candidates = 0
        skipped = 0
        for gi, (team_id, season, group_label) in enumerate(obs):
            self.sig.progress.emit(gi, total_groups, group_label)
            ttl = self._ttl(season)
            try:
                groups_by_view = {}
                for view in views_needed:
                    group, stat_type, _cols = mlbstats.VIEW_SPECS[view]
                    rows = mlbstats.get_player_stats(team_id, season, group, ttl,
                                                     stat_type=stat_type)
                    groups_by_view[view] = mlbstats.group_by_player(rows)
            except mlbstats.MLBStatsError:
                continue  # this team-season isn't available at all; not a candidate

            # Every player who shows up in any view the dependent variable
            # needs is a candidate; a custom formula spanning e.g. Batting
            # and Pitching will just fail (and skip) for one-way players.
            candidate_pids = {pid for view in dep_views for pid in groups_by_view[view]}
            for pid in candidate_pids:
                candidates += 1
                if is_dep_custom:
                    values = mlbstats.player_variable_lookup(dep_formula_vars, groups_by_view, pid)
                    y = formula.evaluate_formula(self.dep_key, values)
                else:
                    player_rows = groups_by_view[self.dep_source].get(pid)
                    y = (mlbstats.aggregate_rows(player_rows, self.dep_key, self.dep_agg)
                        if player_rows else None)
                if y is None:
                    skipped += 1
                    continue
                x_row, complete = [], True
                for idx, (view, key, agg, _label) in enumerate(self.predictors):
                    if view == "__custom__":
                        pvals = mlbstats.player_variable_lookup(
                            predictor_formula_vars[idx], groups_by_view, pid)
                        val = formula.evaluate_formula(key, pvals)
                    else:
                        other_rows = groups_by_view[view].get(pid)
                        val = mlbstats.aggregate_rows(other_rows, key, agg) if other_rows else None
                    if val is None:
                        complete = False
                        break
                    x_row.append(val)
                if not complete:
                    skipped += 1
                    continue
                y_vals.append(y)
                x_rows.append(x_row)

        self.sig.progress.emit(total_groups, total_groups, "running regression…")
        labels = [label if view == "__custom__" else f"{view}: {label}"
                 for view, _key, _agg, label in self.predictors]
        result = regression.multiple_regression(y_vals, x_rows, labels)

        if self.kind == "roster":
            sample_desc = f"Sample: {self.team_name}, {self.season} roster"
        elif self.kind in SEASON_SCOPES:
            sample_desc = (f"Sample: {self.season} season, "
                           f"{_every_team_players(_scope_label(self.kind, self.season, self.team_id, self.live_from))}")
        else:
            sample_desc = (f"Sample: {self.team_name}, every player-season "
                           f"{self.first_year}-{self.live_from}")
        sample_desc += (f" ({result.n} of {candidates} player-row(s) used, {skipped} skipped "
                        "-- a player-row only counts if it has a value for every picked "
                        "variable, so mixing hitting and pitching predictors will drop "
                        "most rows)")
        self.sig.done.emit(_format_regression(result, self.dep_label, sample_desc))


class _FactorJob(QRunnable):
    """Build one row per player across a chosen scope, then run factor
    analysis on the checked predictor variables -- always at player level
    (a Team-totals row-per-team-season sample is too small/coarse for a
    correlation-structure method like this to say much).

    ``kind`` picks which (team, season) pairs make up the sample -- the same
    three options as the Regression tab: "history" (one team, every season
    it has existed), "league" (one season, every team), "roster" (one
    team-season).  Which views the predictors are allowed to come from
    (Batting-family, Pitching-family, or everything) is decided by the
    caller when it builds ``predictors`` -- the player pool this naturally
    produces (whoever has a complete row of the checked variables) is
    exactly the "Batters"/"Pitchers"/"All players" grouping.
    """

    def __init__(self, kind, team_id, team_name, season, first_year,
                predictors, n_factors, live_from):
        super().__init__()
        self.kind = kind
        self.team_id, self.team_name = team_id, team_name
        self.season, self.first_year = season, first_year
        self.predictors = predictors  # [(view, key, agg, label), ...]
        self.n_factors = n_factors    # None -> Kaiser criterion (eigenvalue > 1)
        self.live_from = live_from
        self.sig = _FactorSignals()

    def _ttl(self, year):
        return LIVE_TTL if year >= self.live_from else None

    def _observations(self):
        """-> list of (team_id, season, label): the team-seasons to visit."""
        return _sample_observations(self.kind, self.team_id, self.team_name,
                                    self.season, self.first_year, self.live_from)

    def run(self):
        try:
            self._run_player_level()
        except factor_analysis.FactorAnalysisError as exc:
            self.sig.failed.emit(str(exc))
        except mlbstats.MLBStatsError as exc:
            self.sig.failed.emit(f"Network/API error: {exc}")
        except Exception as exc:  # noqa: BLE001
            self.sig.failed.emit(str(exc))

    def _run_player_level(self):
        obs = self._observations()
        total_groups = len(obs)

        # A custom predictor's formula can reach into any view its "{View:
        # Label}" variables name -- fold those into the views fetched, same
        # as the Regression tab's player-level custom predictor handling.
        predictor_formula_vars: dict[int, set[str]] = {}
        views_needed = set()
        for idx, (view, key, _agg, _label) in enumerate(self.predictors):
            if view == "__custom__":
                fvars = formula.formula_variables(key)
                predictor_formula_vars[idx] = fvars
                views_needed |= {mlbstats.REGRESSION_VARIABLE_INDEX[v][0] for v in fvars}
            else:
                views_needed.add(view)
        views_needed = sorted(views_needed)

        labels = [label if view == "__custom__" else f"{view}: {label}"
                 for view, _key, _agg, label in self.predictors]
        x_rows = []
        row_labels = []  # one per x_rows entry, for the factor-scores table
        candidates = 0
        skipped = 0
        for gi, (team_id, season, group_label) in enumerate(obs):
            self.sig.progress.emit(gi, total_groups, group_label)
            ttl = self._ttl(season)
            try:
                groups_by_view = {}
                for view in views_needed:
                    group, stat_type, _cols = mlbstats.VIEW_SPECS[view]
                    rows = mlbstats.get_player_stats(team_id, season, group, ttl,
                                                     stat_type=stat_type)
                    groups_by_view[view] = mlbstats.group_by_player(rows)
            except mlbstats.MLBStatsError:
                continue  # this team-season isn't available at all; not a candidate

            candidate_pids = {pid for view in views_needed for pid in groups_by_view[view]}
            for pid in candidate_pids:
                candidates += 1
                name = next((groups_by_view[view][pid][0].get("_name")
                            for view in views_needed if groups_by_view[view].get(pid)), None)
                x_row, complete = [], True
                for idx, (view, key, agg, _label) in enumerate(self.predictors):
                    if view == "__custom__":
                        pvals = mlbstats.player_variable_lookup(
                            predictor_formula_vars[idx], groups_by_view, pid)
                        val = formula.evaluate_formula(key, pvals)
                    else:
                        other_rows = groups_by_view[view].get(pid)
                        val = mlbstats.aggregate_rows(other_rows, key, agg) if other_rows else None
                    if val is None:
                        complete = False
                        break
                    x_row.append(val)
                if not complete:
                    skipped += 1
                    continue
                x_rows.append(x_row)
                who = name or "?"
                row_labels.append(who if self.kind == "roster" else f"{who} ({group_label})")

        self.sig.progress.emit(total_groups, total_groups, "running factor analysis…")
        result = factor_analysis.factor_analysis(x_rows, labels, n_factors=self.n_factors)
        scored_rows = list(zip(row_labels, factor_analysis.factor_scores(x_rows, result)))

        if self.kind == "roster":
            sample_desc = f"Sample: {self.team_name}, {self.season} roster"
        elif self.kind in SEASON_SCOPES:
            sample_desc = (f"Sample: {self.season} season, "
                           f"{_every_team_players(_scope_label(self.kind, self.season, self.team_id, self.live_from))}")
        else:
            sample_desc = (f"Sample: {self.team_name}, every player-season "
                           f"{self.first_year}-{self.live_from}")
        sample_desc += (f" ({result.n} of {candidates} player-row(s) used, {skipped} skipped "
                        "-- a player-row only counts if it has a value for every checked "
                        "variable, so mixing hitting and pitching predictors will drop "
                        "most rows)")
        self.sig.done.emit((result, sample_desc, scored_rows))


def _sample_observations(kind: str, team_id, team_name, season, first_year, live_from):
    """-> list of (team_id, season, label): the team-seasons one of the
    three sample-scope choices visits -- "history" (one team, every season
    it has existed), "league" (one season, every team), "same_league" /
    "same_division" (one season, the chosen team's league / division),
    "roster" (exactly one team-season).  Used by every tab's job."""
    if kind in SEASON_SCOPES:
        teams = mlbstats.get_team_map(season, LIVE_TTL if season >= live_from else None)
        if kind != "league":
            scoped = mlbstats.teams_in_scope(teams, kind, team_id)
            if not scoped:
                raise ValueError(
                    f"{team_name} didn't play in {season}." if team_id not in teams else
                    f"{team_name} wasn't in a division in {season} (divisions began in "
                    f"{mlbstats.INTERDIVISION_FIRST_SEASON})." if kind == "same_division" else
                    f"{team_name} wasn't in a league in {season}.")
            teams = scoped
        return sorted(
            ((tid, season, rec.get("name", str(tid))) for tid, rec in teams.items()),
            key=lambda o: o[2])
    if kind == "roster":
        return [(team_id, season, f"{team_name} {season}")]
    start = max(1876, first_year or 1876)
    return [(team_id, yr, str(yr)) for yr in range(start, live_from + 1)]


class _CorrelationJob(QRunnable):
    """Build an observation matrix for the checked variables, then compute
    their Pearson correlation matrix -- no dependent variable, since
    correlation is symmetric.  Otherwise identical data handling to the
    Regression tab's own predictor gathering: same three sample scopes,
    and the same Team totals (each variable rolled up across the roster
    per team-season) / Player (one row per player) levels.
    """

    def __init__(self, kind, level, team_id, team_name, season, first_year,
                variables, live_from):
        super().__init__()
        self.kind, self.level = kind, level
        self.team_id, self.team_name = team_id, team_name
        self.season, self.first_year = season, first_year
        self.variables = variables  # [(view, key, agg, label), ...]
        self.live_from = live_from
        self.sig = _FactorSignals()  # (progress, done(object), failed(str))

    def _ttl(self, year):
        return LIVE_TTL if year >= self.live_from else None

    def _observations(self):
        return _sample_observations(self.kind, self.team_id, self.team_name,
                                    self.season, self.first_year, self.live_from)

    def run(self):
        try:
            if self.level == "player":
                self._run_player_level()
            else:
                self._run_team_level()
        except factor_analysis.FactorAnalysisError as exc:
            self.sig.failed.emit(str(exc))
        except mlbstats.MLBStatsError as exc:
            self.sig.failed.emit(f"Network/API error: {exc}")
        except Exception as exc:  # noqa: BLE001
            self.sig.failed.emit(str(exc))

    @staticmethod
    def _require_enough_rows(x_rows):
        if len(x_rows) < 3:
            raise factor_analysis.FactorAnalysisError(
                "Need at least 3 usable rows to compute correlations -- only "
                f"{len(x_rows)} found for this sample.")

    def _run_team_level(self):
        """One row per team-season, each variable rolled up across that
        team-season's roster -- same as the Regression tab's Team-totals
        predictors.  A custom variable is instead evaluated straight from
        the Team totals fields, same as a custom predictor there too."""
        obs = self._observations()
        total = len(obs)
        has_custom = any(view == "__custom__" for view, _k, _a, _l in self.variables)
        team_names = set().union(*(formula.formula_variables(key) for view, key, _a, _l in self.variables
                                   if view == "__custom__"))
        views_needed = sorted({view for view, _k, _a, _l in self.variables if view != "__custom__"})
        x_rows = []
        skipped = 0
        for i, (team_id, season, label) in enumerate(obs):
            self.sig.progress.emit(i, total, label)
            ttl = self._ttl(season)
            team_lookup = mlbstats.team_totals_lookup(team_id, season, ttl, team_names) if has_custom else None
            try:
                rows_by_view = {}
                for view in views_needed:
                    group, stat_type, _cols = mlbstats.VIEW_SPECS[view]
                    rows_by_view[view] = mlbstats.get_player_stats(
                        team_id, season, group, ttl, stat_type=stat_type)
            except mlbstats.MLBStatsError:
                skipped += 1
                continue
            row, complete = [], True
            for view, key, agg, _label in self.variables:
                val = (formula.evaluate_formula(key, team_lookup) if view == "__custom__"
                      else mlbstats.aggregate_rows(rows_by_view[view], key, agg))
                if val is None:
                    complete = False
                    break
                row.append(val)
            if not complete:
                skipped += 1
                continue
            x_rows.append(row)

        self.sig.progress.emit(total, total, "computing correlations…")
        self._require_enough_rows(x_rows)
        labels = [label if view == "__custom__" else f"{view}: {label}"
                 for view, _key, _agg, label in self.variables]
        matrix = factor_analysis.correlation_matrix(x_rows)
        n = len(x_rows)

        if self.kind in SEASON_SCOPES:
            sample_desc = (f"Sample: {self.season} season, {_all_teams(_scope_label(self.kind, self.season, self.team_id, self.live_from))} "
                           f"({n} of {total} teams used, {skipped} skipped)")
        else:
            sample_desc = (f"Sample: {self.team_name}, {self.first_year}-{self.live_from} "
                           f"({n} of {total} seasons used, {skipped} skipped)")
        self.sig.done.emit((labels, matrix, n, sample_desc))

    def _run_player_level(self):
        """One row per player, pooled across every team-season ``_observations``
        visits -- same candidate/completeness rules as the Regression and
        Factor Analysis tabs' own player-level predictor handling, custom
        variables included."""
        obs = self._observations()
        total_groups = len(obs)
        predictor_formula_vars: dict[int, set[str]] = {}
        views_needed = set()
        for idx, (view, key, _agg, _label) in enumerate(self.variables):
            if view == "__custom__":
                fvars = formula.formula_variables(key)
                predictor_formula_vars[idx] = fvars
                views_needed |= {mlbstats.REGRESSION_VARIABLE_INDEX[v][0] for v in fvars}
            else:
                views_needed.add(view)
        views_needed = sorted(views_needed)

        x_rows = []
        candidates = 0
        skipped = 0
        for gi, (team_id, season, group_label) in enumerate(obs):
            self.sig.progress.emit(gi, total_groups, group_label)
            ttl = self._ttl(season)
            try:
                groups_by_view = {}
                for view in views_needed:
                    group, stat_type, _cols = mlbstats.VIEW_SPECS[view]
                    rows = mlbstats.get_player_stats(team_id, season, group, ttl,
                                                     stat_type=stat_type)
                    groups_by_view[view] = mlbstats.group_by_player(rows)
            except mlbstats.MLBStatsError:
                continue

            candidate_pids = {pid for view in views_needed for pid in groups_by_view[view]}
            for pid in candidate_pids:
                candidates += 1
                row, complete = [], True
                for idx, (view, key, agg, _label) in enumerate(self.variables):
                    if view == "__custom__":
                        pvals = mlbstats.player_variable_lookup(
                            predictor_formula_vars[idx], groups_by_view, pid)
                        val = formula.evaluate_formula(key, pvals)
                    else:
                        other_rows = groups_by_view[view].get(pid)
                        val = mlbstats.aggregate_rows(other_rows, key, agg) if other_rows else None
                    if val is None:
                        complete = False
                        break
                    row.append(val)
                if not complete:
                    skipped += 1
                    continue
                x_rows.append(row)

        self.sig.progress.emit(total_groups, total_groups, "computing correlations…")
        self._require_enough_rows(x_rows)
        labels = [label if view == "__custom__" else f"{view}: {label}"
                 for view, _key, _agg, label in self.variables]
        matrix = factor_analysis.correlation_matrix(x_rows)
        n = len(x_rows)

        if self.kind == "roster":
            sample_desc = f"Sample: {self.team_name}, {self.season} roster"
        elif self.kind in SEASON_SCOPES:
            sample_desc = (f"Sample: {self.season} season, "
                           f"{_every_team_players(_scope_label(self.kind, self.season, self.team_id, self.live_from))}")
        else:
            sample_desc = (f"Sample: {self.team_name}, every player-season "
                           f"{self.first_year}-{self.live_from}")
        sample_desc += f" ({n} of {candidates} player-row(s) used, {skipped} skipped)"
        self.sig.done.emit((labels, matrix, n, sample_desc))


def _sampled_note(sampled: list[str]) -> str:
    """The Custom tab's footnote for figures marked * -- ``sampled`` lists "team-season share" for each."""
    if not sampled:
        return ""
    return ("* From Retrosheet\u2019s play-by-play of only part of the season, so a sample of it: "
            + ", ".join(sampled) + " of the team\u2019s innings in MLB\u2019s records.")


class _CustomTableJob(QRunnable):
    """Compute every currently-defined custom stat at one level, across
    whichever team-seasons the chosen sample scope visits, for the Custom
    tab's table.

    Team level: "roster" (a single team-season) shows a Statistic | Value
    table; "history"/"league" show one row per season/team, one column per
    stat -- there'd be nothing to tabulate from a single number otherwise.
    Player level: always one row per player (pooled across every team-season
    the scope visits for "history"/"league"), one column per stat -- same
    shape and player-pooling rules as the Factor Analysis tab's own sample.
    """

    def __init__(self, kind, level, team_id, team_name, season, first_year,
                stats, live_from, force=False):
        super().__init__()
        self.kind, self.level = kind, level
        self.team_id, self.team_name = team_id, team_name
        self.season, self.first_year = season, first_year
        self.stats = stats  # [(name, formula_text), ...]
        self.live_from = live_from
        self.force = force
        self.sig = _FactorSignals()  # (progress, done(object), failed(str)) -- generic enough to reuse

    def _ttl(self, year):
        if self.force:
            return 0
        return LIVE_TTL if year >= self.live_from else None

    def _observations(self):
        return _sample_observations(self.kind, self.team_id, self.team_name,
                                    self.season, self.first_year, self.live_from)

    def run(self):
        try:
            if self.level == "player":
                self._run_player_level()
            else:
                self._run_team_level()
        except mlbstats.MLBStatsError as exc:
            self.sig.failed.emit(f"Network/API error: {exc}")
        except Exception as exc:  # noqa: BLE001
            self.sig.failed.emit(str(exc))

    def _run_team_level(self):
        obs = self._observations()
        total = len(obs)
        out_rows = []
        names = set().union(*(formula.formula_variables(text) for _name, text in self.stats))
        unavailable: dict[str, str] = {}  # Retrosheet view -> why its data couldn't be had (first reason)
        sampled: list[str] = []  # team-seasons whose figures rest on part of their play-by-play
        for i, (team_id, season, label) in enumerate(obs):
            self.sig.progress.emit(i, total, label)
            try:
                lookup = mlbstats.team_totals_lookup(team_id, season, self._ttl(season), names, unavailable)
            except mlbstats.MLBStatsError:
                continue
            row = [label] + [formula.evaluate_formula(text, lookup) for _name, text in self.stats]
            if names & mlbstats.RETRO_TEAM_VARIABLES:
                coverage = mlbstats.retro_sample(team_id, season, self._ttl(season))
                if coverage is not None:
                    bare = {k: (None if k in mlbstats.RETRO_TEAM_VARIABLES else v) for k, v in lookup.items()}
                    row[1:] = [mlbstats.mark_sampled(v, formula.evaluate_formula(text, bare), coverage)
                               for v, (_name, text) in zip(row[1:], self.stats)]
                    if any(isinstance(v, mlbstats.SampledValue) for v in row[1:]):
                        sampled.append(f"{label} {coverage:.0%}")
            out_rows.append(row)
        self.sig.progress.emit(total, total, "done")
        note = _sampled_note(sampled)
        if unavailable:
            blank = [name for name, text in self.stats
                     if any(v.split(":", 1)[0] in unavailable for v in formula.formula_variables(text))]
            note = (f"{', '.join(blank)}: blank where the data isn't available \u2014 "
                    + "; ".join(dict.fromkeys(unavailable.values())) + ".  " + note).strip()

        if self.kind == "roster":
            desc = f"{self.team_name}, {self.season}"
            pairs = (list(zip([name for name, _t in self.stats], out_rows[0][1:]))
                    if out_rows else [])
            self.sig.done.emit(("team_single", None, pairs, desc, note))
            return
        if self.kind == "history":
            row_label, desc = "Season", f"{self.team_name}, {self.first_year}-{self.live_from}"
        else:
            label = _scope_label(self.kind, self.season, self.team_id, self.live_from)
            row_label, desc = "Team", f"{self.season} season, every {label + ' ' if label else ''}team"
        headers = [row_label] + [name for name, _t in self.stats]
        self.sig.done.emit(("team_multi", headers, out_rows, desc, note))

    def _run_player_level(self):
        obs = self._observations()
        total = len(obs)
        parsed = [(name, text, formula.formula_variables(text)) for name, text in self.stats]
        views_needed = sorted({mlbstats.REGRESSION_VARIABLE_INDEX[v][0]
                              for _n, _t, fvars in parsed for v in fvars})
        out_rows = []
        unavailable: dict[str, str] = {}  # view -> why its data couldn't be had (first reason)
        sampled: list[str] = []  # team-seasons whose figures rest on part of their play-by-play
        for gi, (team_id, season, group_label) in enumerate(obs):
            self.sig.progress.emit(gi, total, group_label)
            ttl = self._ttl(season)
            groups_by_view = {}
            for view in views_needed:
                # A source with nothing for this team-season (Retrosheet before a season is over, say)
                # leaves only the stats that need it blank -- not the whole table.
                try:
                    group, stat_type, _cols = mlbstats.VIEW_SPECS[view]
                    rows = mlbstats.get_player_stats(team_id, season, group, ttl, stat_type=stat_type)
                    groups_by_view[view] = mlbstats.group_by_player(rows)
                except mlbstats.MLBStatsError as exc:
                    groups_by_view[view] = {}
                    unavailable.setdefault(view, str(exc))
            if not any(groups_by_view.values()):
                continue  # nothing at all for this team-season

            candidate_pids = {pid for view in views_needed for pid in groups_by_view[view]}
            coverage = (mlbstats.retro_sample(team_id, season, ttl)
                        if any(groups_by_view.get(view) for view in mlbstats.RETRO_VIEWS) else None)
            bare_groups = {view: ({} if view in mlbstats.RETRO_VIEWS else got) for view, got in groups_by_view.items()}
            marked = False
            for pid in candidate_pids:
                name = next((groups_by_view[view][pid][0].get("_name")
                            for view in views_needed if groups_by_view[view].get(pid)), "?")
                who = name if self.kind == "roster" else f"{name} ({group_label})"
                row = [who]
                for _name, text, fvars in parsed:
                    pvals = mlbstats.player_variable_lookup(fvars, groups_by_view, pid)
                    value = formula.evaluate_formula(text, pvals)
                    if coverage is not None:
                        without = formula.evaluate_formula(text, mlbstats.player_variable_lookup(fvars, bare_groups, pid))
                        value = mlbstats.mark_sampled(value, without, coverage)
                        marked = marked or isinstance(value, mlbstats.SampledValue)
                    row.append(value)
                out_rows.append(row)
            if marked:
                sampled.append(f"{group_label} {coverage:.0%}")
        out_rows.sort(key=lambda r: r[0])
        self.sig.progress.emit(total, total, "done")

        if self.kind == "roster":
            desc = f"{self.team_name}, {self.season} roster"
        elif self.kind in SEASON_SCOPES:
            desc = f"{self.season} season, {_every_team_players(_scope_label(self.kind, self.season, self.team_id, self.live_from))}"
        else:
            desc = f"{self.team_name}, every player-season {self.first_year}-{self.live_from}"
        headers = ["Player"] + [name for name, _t, _f in parsed]
        note = _sampled_note(sampled)
        if unavailable:
            blank = [name for name, _t, fvars in parsed
                     if any(mlbstats.REGRESSION_VARIABLE_INDEX[v][0] in unavailable for v in fvars)]
            note = (f"{', '.join(blank)}: blank where the data isn't available \u2014 "
                    + "; ".join(dict.fromkeys(unavailable.values())) + ".  " + note).strip()
        self.sig.done.emit(("player", headers, out_rows, desc, note))


# --------------------------------------------------------------------------- #
#  A year-entry spin box that selects its text on focus                       #
# --------------------------------------------------------------------------- #
class _YearSpin(QSpinBox):
    """Without this, clicking into the box only places a cursor -- it doesn't
    select the existing year -- so typing a replacement inserts digits next
    to the old ones.  QSpinBox's own validator rejects that (the in-progress
    text is out of range/too long) character by character, so nothing appears
    to happen, and Enter just re-commits the unchanged old value."""

    def focusInEvent(self, event):
        super().focusInEvent(event)
        QTimer.singleShot(0, self.selectAll)


class _SortItem(QTableWidgetItem):
    """A table cell that sorts by the number stored in ``UserRole + 1`` (its text is for show)."""

    def __lt__(self, other):
        mine, theirs = self.data(Qt.ItemDataRole.UserRole + 1), other.data(Qt.ItemDataRole.UserRole + 1)
        if isinstance(mine, (int, float)) and isinstance(theirs, (int, float)):
            return mine < theirs
        return super().__lt__(other)


# --------------------------------------------------------------------------- #
#  A tab bar that can tint individual tabs                                    #
# --------------------------------------------------------------------------- #
class _TintedTabBar(QTabBar):
    """Paints a background colour (a QColor stored as the tab's data) behind
    the tabs that have one; stylesheets can't target a single tab."""

    def paintEvent(self, event):
        painter = QStylePainter(self)
        for i in range(self.count()):
            opt = QStyleOptionTab()
            self.initStyleOption(opt, i)
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, opt)
            tint = self.tabData(i)
            if isinstance(tint, QColor):
                selected = bool(opt.state & QStyle.StateFlag.State_Selected)
                painter.fillRect(opt.rect.adjusted(2, 2, -2, 0 if selected else -1),
                                 tint.darker(115) if selected else tint)
                opt.palette.setColor(opt.palette.ColorRole.WindowText, QColor("black"))
                opt.palette.setColor(opt.palette.ColorRole.ButtonText, QColor("black"))
            painter.drawControl(QStyle.ControlElement.CE_TabBarTabLabel, opt)


# --------------------------------------------------------------------------- #
#  Postseason bracket                                                         #
# --------------------------------------------------------------------------- #
WON_COLOR, LOST_COLOR, LIVE_COLOR = "#2e9e4f", "#d9534f", "#e08e0b"


def _top_scrollbar(view) -> QScrollBar:
    """A horizontal scrollbar to put above ``view`` in place of its own at the bottom (which is
    hidden): the two are kept in step, and the top one shows only when there's anything to scroll."""
    own = view.horizontalScrollBar()
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    top = QScrollBar(Qt.Orientation.Horizontal)
    top.setVisible(False)

    def sync_range(low: int, high: int):
        top.setRange(low, high)
        top.setPageStep(own.pageStep())
        top.setSingleStep(own.singleStep())
        top.setVisible(high > low)
    own.rangeChanged.connect(sync_range)
    own.valueChanged.connect(top.setValue)
    top.valueChanged.connect(own.setValue)
    return top


class IntergroupBalance(QWidget):
    """The Intergroup tab's records drawn as balances: for each set of groups that played
    each other, a beam hung from a fulcrum with a box per group on a cord.  A group that won
    more than it lost is pulled down by its margin over .500, one that lost more is pushed
    up.  The beam tilts by the best straight fit to those pulls through the fulcrum -- with
    two groups, which always pull equally and oppositely, that's the whole of it; with three
    the middle division's own pull shows in its cord being longer or shorter.

    With interleague play the whole thing is a mobile: each league's division balance hangs
    from beneath that league's box on the interleague beam.  Before it (or if a league's
    divisions are missing) the balances are drawn separately.  The drawing is scaled to the
    space it's given."""

    BOX_W, BOX_H, GAP, CORD, TITLE_H, MOUNT_H, MARGIN = 104, 50, 56, 34, 22, 24, 18
    PART_H = 16       # a line of a box's breakdown (its record against each league)
    PULL = 450.0      # pixels per 1.000 of winning percentage away from .500
    MAX_PULL = 70.0   # no box moves further than this from its fulcrum's level
    BEAM_PAST = 14    # how far a beam runs past its outermost cords
    MAX_SCALE = 1.5
    TRI_R = 130.0     # a triangular plate's centre-to-corner distance
    TILT = 0.38       # how much depth shows when a plate is seen from a little above
    HANG_ROOM = 40.0  # from the box a balance hangs from down to that balance's mount

    def __init__(self, parent=None):
        super().__init__(parent)
        self._top: tuple | None = None          # ("Interleague", groups, [divisions per league])
        self._separate: list[tuple] = []        # [(title, groups), ...] drawn one after another
        self._short_labels = False              # "Other div." / "Other lg." when space is short
        self.setMinimumSize(260, 160)

    def set_records(self, interleague: list | None, divisions: list[tuple[str, list]]):
        """``interleague``: ``[(short label, full name, W, L), ...]`` per league, left to right,
        or None; ``divisions``: ``[(title, groups), ...]`` in the same league order, each group
        list like ``interleague``'s.  A group may add ``[(against, W, L), ...]``, a breakdown its
        box lists, with their sum (``against`` being ``"division"`` or ``"league"`` -- its games
        against the other divisions of its league, or the other league -- or any other label,
        such as a league's divisions), and then which of those lines is the W-L it pulls with,
        shown in bold: 0 (the first, the default) or -1 (the sum).  A balance with fewer than two groups that played is left
        out; the divisions hang from the leagues when there's a set for each league."""
        def played(groups):
            return groups and sum(1 for g in groups if g[2] + g[3]) >= 2
        self._top, self._separate = None, []
        if played(interleague):
            hangers = [g if played(g) else None for _t, g in divisions]
            if len(hangers) == len(interleague) and any(hangers):
                self._top = ("Interleague, with each league's divisions hanging from it",
                             interleague, hangers)
            else:
                self._separate.append(("Interleague", interleague))
        if self._top is None:
            self._separate += [(t, g) for t, g in divisions if played(g)]
        self.update()

    def clear(self):
        self._top, self._separate = None, []
        self.update()

    # ---- layout ---------------------------------------------------------
    def _small_font(self) -> QFont:
        small = QFont(self.font())
        small.setPointSizeF(max(6.0, small.pointSizeF() * 0.9))
        return small

    LABELS = {"division": ("Other division", "Other div."), "league": ("Other league", "Other lg.")}

    def _lines(self, group) -> list[tuple]:
        """A broken-down box's lines, ``[(label, W-L text, Pct text), ...]``, its parts then their
        total -- the parts' labels spelled out, or both abbreviated if space is short."""
        def line(against, w, l):
            label = self.LABELS.get(against, (against, against))[self._short_labels]
            return label, f"{w}-{l}", _fmt_rate(w / (w + l)) if w + l else "n/a"
        parts = group[4] if len(group) > 4 else []
        if not parts:
            return []
        return [line(*part) for part in parts] + [
            line("Total", sum(w for _a, w, _l in parts), sum(l for _a, _w, l in parts))]

    @staticmethod
    def _pulling_line(group) -> int:
        """Which of a broken-down box's lines is the record it pulls with."""
        index = group[5] if len(group) > 5 else 0
        return index % (len(group[4]) + 1)

    def _part_font(self, group, index: int) -> QFont:
        """A breakdown line's font: the record the box pulls with in bold."""
        font = self._small_font()
        font.setBold(index == self._pulling_line(group))
        return font

    def _columns(self, group) -> tuple[float, float, float]:
        """The widths of a broken-down box's label, W-L and Pct columns."""
        lines = self._lines(group)
        metrics = [QFontMetrics(self._part_font(group, k)) for k in range(len(lines))]
        return tuple(max(fm.horizontalAdvance(line[i]) for fm, line in zip(metrics, lines))
                     for i in range(3))

    def _box_w(self, group) -> float:
        if len(group) < 5:
            return self.BOX_W
        label, wl, pct = self._columns(group)
        return max(self.BOX_W, 9 + label + 12 + wl + 10 + pct + 9)

    def _tri_r(self, groups) -> float:
        """A plate big enough for its boxes to overlap the front one by no more than a quarter."""
        return max(self.TRI_R, 0.75 * max(self._box_w(g) for g in groups) / 0.866)

    def _width_of(self, groups, hangers=None) -> float:
        if len(groups) == 3 and not hangers:
            return 2 * self._tri_r(groups) * 0.866 + max(self._box_w(g) for g in groups)
        n = max(2, len(groups))
        widest = max([self._box_w(g) for g in groups]
                     + [self._width_of(h) for h in (hangers or []) if h])
        return n * widest + (n - 1) * self.GAP

    def _pulls(self, groups) -> list[float]:
        pulls = []
        for _s, _n, w, l, *_parts in groups:
            pct = w / (w + l) if w + l else 0.5
            pulls.append(max(-self.MAX_PULL, min(self.MAX_PULL, (pct - 0.5) * self.PULL)))
        return pulls

    def _hang(self, scene, anchor: QPointF, d: float, plane_y: float, group, below: float = 0.0):
        """A group's box on a cord from ``anchor`` (``plane_y`` below its fulcrum): the cord takes
        up whatever of the pull ``d`` the beam's tilt doesn't -- and the box's top is no higher
        than ``below``, if that's given.  Returns the box."""
        extra = max(-self.CORD / 2, min(self.MAX_PULL / 2, d - plane_y))
        if below:
            extra = max(extra, below - anchor.y() - self.CORD)
        height = self.BOX_H + self.PART_H * len(group[4] if len(group) > 4 else ())
        width = self._box_w(group)
        box = QRectF(anchor.x() - width / 2, anchor.y() + self.CORD + extra, width, height)
        scene.append(("cord", anchor, QPointF(anchor.x(), box.top())))
        scene.append(("box", box, group))
        return box

    def _balance(self, scene, groups, cx, pivot_y, hangers=None):
        """Adds one balance -- its fulcrum at (cx, pivot_y) -- and anything hanging from it to ``scene``."""
        if len(groups) == 3 and not hangers:   # hung by a cord from a bar to the plate's pin
            scene.append(("bar", cx, pivot_y - self.MOUNT_H - self.HANG_ROOM))
            scene.append(("cord", QPointF(cx, pivot_y - self.MOUNT_H - self.HANG_ROOM), QPointF(cx, pivot_y)))
            self._triangle(scene, groups, cx, pivot_y)
            return
        n = len(groups)
        step = (self._width_of(groups, hangers) + self.GAP) / max(2, n)
        xs = [(i - (n - 1) / 2) * step for i in range(n)]
        pulls = self._pulls(groups)
        slope = sum(x * d for x, d in zip(xs, pulls)) / (sum(x * x for x in xs) or 1)
        reach = xs[-1] + self.BEAM_PAST
        if abs(slope * reach) > self.MAX_PULL * 1.2:   # keep the beam's ends within reach
            slope = self.MAX_PULL * 1.2 / reach * (1 if slope > 0 else -1)
        scene.append(("beam", QPointF(cx - reach, pivot_y - slope * reach),
                      QPointF(cx + reach, pivot_y + slope * reach)))
        scene.append(("mount", cx, pivot_y))
        for i, (x, d, group) in enumerate(zip(xs, pulls, groups)):
            box = self._hang(scene, QPointF(cx + x, pivot_y + slope * x), d, slope * x, group)
            if hangers and hangers[i]:
                pin_y = box.bottom() + self.HANG_ROOM + self.MOUNT_H
                if len(hangers[i]) == 3:   # a plate hangs by its pin
                    scene.append(("cord", QPointF(cx + x, box.bottom()), QPointF(cx + x, pin_y)))
                    self._triangle(scene, hangers[i], cx + x, pin_y)
                else:
                    scene.append(("cord", QPointF(cx + x, box.bottom()), QPointF(cx + x, pin_y - self.MOUNT_H)))
                    self._balance(scene, hangers[i], cx + x, pin_y)

    def _triangle(self, scene, groups, cx, pivot_y):
        """Three groups on a triangular plate pinned at its centre, seen from a little above:
        the first at the back left, the second in front, the third at the back right.  The
        plate tips both ways, by the flat plane through the pin that best fits the pulls."""
        r = self._tri_r(groups)
        corners = [(r * math.cos(math.radians(a)), r * math.sin(math.radians(a)))
                   for a in (210, 90, 330)]   # plan view, y toward the viewer
        pulls = self._pulls(groups)
        a = sum(x * d for (x, _y), d in zip(corners, pulls)) / sum(x * x for x, _y in corners)
        b = sum(y * d for (_x, y), d in zip(corners, pulls)) / sum(y * y for _x, y in corners)
        heights = [a * x + b * y for x, y in corners]
        steepest = max(abs(h) for h in heights)
        if steepest > self.MAX_PULL:   # keep the corners within reach
            heights = [h * self.MAX_PULL / steepest for h in heights]
        points = [QPointF(cx + x, pivot_y + y * self.TILT + h) for (x, y), h in zip(corners, heights)]
        scene.append(("plate", points))
        scene.append(("pin", QPointF(cx, pivot_y)))
        backs = [self._hang(scene, points[i], pulls[i], heights[i], groups[i]) for i in (0, 2)]
        # the front box goes over them, and below any it would hide part of
        width = self._box_w(groups[1])
        front = QRectF(points[1].x() - width / 2, 0, width, 1)
        below = max([b.bottom() + 6 for b in backs if b.left() < front.right() and front.left() < b.right()
                     and len(groups[1]) > 4], default=0.0)
        self._hang(scene, points[1], pulls[1], heights[1], groups[1], below)

    def _scene(self, columns: int = 1):
        """``(width, height, items)`` in drawing units, the items as ``_balance`` makes them plus
        ``("title", rect, text)``; separate balances go ``columns`` to a row.  The mobile hangs
        as close under its title as its beam's raised end allows."""
        def bottom(items) -> float:
            return max(it[1].bottom() for it in items if it[0] == "box")

        scene = []
        rise = self.TITLE_H + self.MAX_PULL * 1.2 + self.TRI_R * 0.5 * self.TILT + 8   # title to fulcrum
        if self._top is not None:
            title, groups, hangers = self._top
            width = self._width_of(groups, hangers) + 2 * self.MARGIN
            self._balance(scene, groups, width / 2, 0.0, hangers)   # a dry run, to find its top
            pivot_y = self.TITLE_H + 4 + max([self.MOUNT_H] + [-y for it in scene if it[0] == "beam"
                                                               for y in (it[1].y(), it[2].y())])
            scene = [("title", QRectF(0, 0, width, self.TITLE_H), title)]
            self._balance(scene, groups, width / 2, pivot_y, hangers)
            return width, bottom(scene) + self.MARGIN, scene
        cell = max((self._width_of(g) for _t, g in self._separate), default=0) + 2 * self.MARGIN
        width = cell * min(columns, max(1, len(self._separate)))
        top = 0.0
        for start in range(0, len(self._separate), columns):
            row = []
            for i, (title, groups) in enumerate(self._separate[start:start + columns]):
                row.append(("title", QRectF(i * cell, top, cell, self.TITLE_H), title))
                self._balance(row, groups, i * cell + cell / 2, top + rise)
            scene += row
            top = bottom(row) + self.MARGIN
        return width, top, scene

    def _fitted(self):
        """``(scale, x offset, items)``: the scene in whichever arrangement can be drawn biggest.
        The boxes' breakdowns are spelled out unless that keeps the drawing from its full size and
        abbreviating would let it be bigger."""
        self._short_labels = False
        best = self._arranged()
        if best[0] < 1.0 and any(len(g) > 4 for g in self._all_groups()):
            self._short_labels = True
            short = self._arranged()
            if short[0] > best[0] + 1e-6:
                return short
            self._short_labels = False
        return best

    def _all_groups(self) -> list:
        if self._top is not None:
            return [g for groups in [self._top[1]] + list(self._top[2]) if groups for g in groups]
        return [g for _t, groups in self._separate for g in groups]

    def _arranged(self):
        """:meth:`_fitted` with the labels as they are."""
        best = None
        for columns in ((1, 2) if self._top is None and len(self._separate) > 1 else (1,)):
            width, height, scene = self._scene(columns)
            scale = min(self.MAX_SCALE, self.width() / width, self.height() / height)
            if best is None or scale > best[0] + 1e-6:
                best = (scale, max(0.0, (self.width() - width * scale) / 2), scene)
        return best

    # ---- painting -------------------------------------------------------
    def paintEvent(self, _event):
        if self._top is None and not self._separate:
            return
        scale, dx, scene = self._fitted()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(dx, 0)
        p.scale(scale, scale)
        pal = self.palette()
        text = pal.text().color()
        line = QColor(text)
        line.setAlphaF(0.6)
        bold = QFont(self.font())
        bold.setBold(True)
        small = self._small_font()
        won, lost = QColor(WON_COLOR), QColor(LOST_COLOR)
        # cords and beams under the boxes and fulcrums
        order = {"title": 0, "bar": 0, "cord": 1, "beam": 2, "plate": 2, "mount": 3, "pin": 3, "box": 4}
        for item in sorted(scene, key=lambda it: order[it[0]]):
            kind = item[0]
            if kind == "title":
                p.setFont(bold)
                p.setPen(text)
                # (top-aligned, so the title is level with the tab's title beside it)
                p.drawText(item[1], Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, item[2])
            elif kind == "cord":
                p.setPen(QPen(line, 1.4))
                p.drawLine(item[1], item[2])
            elif kind == "beam":
                p.setPen(QPen(text, 4, cap=Qt.PenCapStyle.RoundCap))
                p.drawLine(item[1], item[2])
            elif kind == "plate":
                plate = QColor(text)
                plate.setAlphaF(0.12)
                p.setPen(QPen(text, 3, join=Qt.PenJoinStyle.RoundJoin))
                p.setBrush(plate)
                p.drawPolygon(item[1])
            elif kind == "bar":
                p.setPen(QPen(line, 2))
                p.drawLine(QPointF(item[1] - 30, item[2]), QPointF(item[1] + 30, item[2]))
            elif kind == "pin":
                p.setPen(QPen(text, 1.5))
                p.setBrush(pal.base())
                p.drawEllipse(item[1], 4, 4)
            elif kind == "mount":   # a fulcrum hanging from a bar, the beam pinned at its point
                _k, cx, pivot_y = item
                bar_y = pivot_y - self.MOUNT_H
                p.setPen(QPen(line, 2))
                p.drawLine(QPointF(cx - 30, bar_y), QPointF(cx + 30, bar_y))
                p.setPen(QPen(text, 1.5))
                p.setBrush(pal.button())
                p.drawPolygon([QPointF(cx - 13, bar_y), QPointF(cx + 13, bar_y), QPointF(cx, pivot_y)])
                p.setBrush(pal.base())
                p.drawEllipse(QPointF(cx, pivot_y), 4, 4)
            else:
                _k, box, (short, _full, w, l, *parts) = item
                pct = w / (w + l) if w + l else None
                fill = QColor(pal.base().color())
                if pct is not None and pct != 0.5:
                    tint = won if pct > 0.5 else lost
                    t = min(1.0, abs(pct - 0.5) / 0.1) * 0.55 + 0.15
                    fill = QColor(int(fill.red() + (tint.red() - fill.red()) * t),
                                  int(fill.green() + (tint.green() - fill.green()) * t),
                                  int(fill.blue() + (tint.blue() - fill.blue()) * t))
                p.setPen(QPen(line, 1.2))
                p.setBrush(fill)
                p.drawRoundedRect(box, 5, 5)
                p.setPen(text)
                p.setFont(bold)
                p.drawText(QRectF(box.left(), box.top() + 3, box.width(), self.BOX_H / 2 - 2),
                           Qt.AlignmentFlag.AlignCenter, short)
                p.setFont(small)
                if not parts:
                    p.drawText(QRectF(box.left(), box.top() + self.BOX_H / 2, box.width(), self.BOX_H / 2 - 3),
                               Qt.AlignmentFlag.AlignCenter,
                               f"{w}-{l}  {_fmt_rate(pct)}" if pct is not None else "no games")
                    continue
                # each part and then the total, under a rule: label, W-L and Pct in columns
                _lw, wl_w, pct_w = self._columns(item[2])
                left, right = box.left() + 9, box.right() - 9
                y = box.top() + self.BOX_H / 2 - 4
                lines = self._lines(item[2])
                for i, (label, wl, rate) in enumerate(lines):
                    if i == len(lines) - 1:
                        p.setPen(QPen(line, 1))
                        p.drawLine(QPointF(left, y + 2), QPointF(right, y + 2))
                        p.setPen(text)
                        y += 4
                    p.setFont(self._part_font(item[2], i))
                    row = QRectF(left, y, right - left, self.PART_H)
                    middle = Qt.AlignmentFlag.AlignVCenter
                    p.drawText(row, Qt.AlignmentFlag.AlignLeft | middle, label)
                    p.drawText(QRectF(right - pct_w - 10 - wl_w, y, wl_w, self.PART_H),
                               Qt.AlignmentFlag.AlignRight | middle, wl)
                    p.drawText(row, Qt.AlignmentFlag.AlignRight | middle, rate)
                    y += self.PART_H
        p.end()

    def event(self, ev):
        if ev.type() == QEvent.Type.ToolTip:
            self._tooltip(ev)
            return True
        return super().event(ev)

    def _tooltip(self, ev):
        """The full name and record of the box under the pointer."""
        if self._top is not None or self._separate:
            scale, dx, scene = self._fitted()
            pos = QPointF((ev.pos().x() - dx) / scale, ev.pos().y() / scale)
            for item in scene:
                if item[0] == "box" and item[1].contains(pos):
                    group = item[2]
                    _s, full, w, l, *parts = group
                    def rate(w, l):
                        return _fmt_rate(w / (w + l)) if w + l else "n/a"
                    breakdown = parts[0] if parts else []
                    if breakdown:
                        w, l = sum(pw for _a, pw, _l in breakdown), sum(pl for _a, _w, pl in breakdown)
                    rows = [(self.LABELS.get(a, (a,))[0] + ": ", pw, pl) for a, pw, pl in breakdown]
                    rows.append(("Total: " if breakdown else "", w, l))
                    pulling = self._pulling_line(group) if breakdown else -1
                    lines = [f"{label}{pw:,}-{pl:,} ({rate(pw, pl)}) in {pw + pl:,} games"
                             + (" (what it pulls with)" if breakdown and i == pulling else "")
                             for i, (label, pw, pl) in enumerate(rows)]
                    QToolTip.showText(ev.globalPos(), "\n".join([full] + lines), self)
                    return
        QToolTip.hideText()


class NaturalZoneField(QWidget):
    """The Naturalzone tab's field: the Retrosheet / Total Zone location zones (faint, with their
    codes), each fielder's average zone (dashed borders), and the defense's natural zones -- each
    fielder's outline, the ground he covers that a neighbour usually does shaded as made up, ground
    nobody reaches as a gap, ground two reach as an overlap.  Arrows show who reaches into whose
    zone, and by how much of it.  Hovering a spot names its zone and who covers it."""

    MADE_UP, GAP, OVERLAP = WON_COLOR, LOST_COLOR, "#4a7bd0"
    ARROW_MIN = 0.03  # a fielder reaching less of a neighbour's zone than this gets no arrow
    MAX_ZOOM = 12.0
    MARGIN = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result = None
        self._park = None   # a real park's fence (Stadiums tab), or None for the uniform park
        self._outlines: dict = {}
        self._players: dict = {}
        self._image: QImage | None = None
        self._zoom, self._pan = 1.0, QPointF(0, 0)
        self._drag: QPointF | None = None   # where a pan drag last was
        self.setMinimumSize(360, 330)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._zoom_in_btn, self._zoom_out_btn = QToolButton(self), QToolButton(self)
        for btn, text, tip, steps in ((self._zoom_in_btn, "+", "Zoom in", 2),
                                      (self._zoom_out_btn, "\u2212", "Zoom out", -2)):
            btn.setText(text)
            btn.setToolTip(tip)
            btn.setFixedSize(28, 28)
            font = btn.font()
            font.setPointSizeF(font.pointSizeF() * 1.3)
            font.setBold(True)
            btn.setFont(font)
            btn.setCursor(Qt.CursorShape.ArrowCursor)
            btn.clicked.connect(lambda _c=False, n=steps: self._zoom_by(n))
        self._zoom_out_btn.setEnabled(False)

    def set_result(self, result: dict | None, outlines: dict, players: dict):
        """``players``: {position: (name, ratio or None)}."""
        self._result, self._outlines, self._players = result, outlines, players
        park = (result or {}).get("park")
        if park != self._park:
            self._park = park
            self._zoom_by(-1e6)  # a different-sized field: back to the whole of it
        self._image = None
        self.update()

    def _grid(self) -> dict:
        return naturalzone.grid(self._park)

    def _shaded(self) -> bool:
        """Whether there's a cell image to draw."""
        return self._result is not None

    # ---- geometry: feet -> widget --------------------------------------
    def _transform(self):
        x0, x1, y0, y1 = naturalzone.bounds(self._park)
        w, h = self._map_rect().width(), self._map_rect().height()
        scale = min(w / (x1 - x0), h / (y1 - y0))
        ox = self.MARGIN + (w - (x1 - x0) * scale) / 2
        oy = self.MARGIN + (h - (y1 - y0) * scale) / 2
        # zoomed by self._zoom and moved by self._pan (pixels) from that whole-field fit
        return scale * self._zoom, ox * self._zoom + self._pan.x(), oy * self._zoom + self._pan.y()

    def _map_rect(self) -> QRectF:
        """Where the field is drawn: the widget less its margins and the legend below."""
        return QRectF(self.MARGIN, self.MARGIN, self.width() - 2 * self.MARGIN,
                      self.height() - 2 * self.MARGIN - self._legend_layout()[3] - 6)

    def _pt(self, x: float, y: float, t) -> QPointF:
        scale, ox, oy = t
        x0, _x1, _y0, y1 = naturalzone.bounds(self._park)
        return QPointF(ox + (x - x0) * scale, oy + (y1 - y) * scale)

    def _feet(self, px: float, py: float, t):
        scale, ox, oy = t
        x0, _x1, _y0, y1 = naturalzone.bounds(self._park)
        return x0 + (px - ox) / scale, y1 - (py - oy) / scale

    def _render(self):
        """The shaded cells as an image, one pixel per cell (rows run north to south)."""
        g = self._grid()
        nx, ny = g["nx"], g["ny"]
        img = QImage(nx, ny, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        pal = self.palette()
        grass = QColor(pal.text().color())
        grass.setAlpha(14)
        colours = {"own": grass.rgba()}
        for name, hexcol, alpha in (("made", self.MADE_UP, 120), ("gap", self.GAP, 170), ("over", self.OVERLAP, 95)):
            c = QColor(hexcol)
            c.setAlpha(alpha)
            colours[name] = c.rgba()
        cover, base = self._result["cover"], g["base"]
        for k in g["field"]:
            who = cover[k]
            kind = "gap" if not who else "over" if len(who) > 1 else "own" if base[k] in who else "made"
            img.setPixel(k % nx, ny - 1 - k // nx, colours[kind])
        self._image = img

    # ---- painting -------------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        text = pal.text().color()
        faint = QColor(text)
        faint.setAlpha(45)
        t = self._transform()
        scale = t[0]
        nz = naturalzone
        drawing = nz.zone_drawing(self._park)
        x0, x1, y0, y1 = nz.bounds(self._park)
        p.save()
        p.setClipRect(self._map_rect())
        if self._shaded():
            if self._image is None:
                self._render()
            target = QRectF(self._pt(x0, y1, t), self._pt(x1, y0, t))
            p.drawImage(target, self._image)

        def segments(segs, pen):
            p.setPen(pen)
            for (x0, y0), (x1, y1) in segs:
                p.drawLine(self._pt(x0, y0, t), self._pt(x1, y1, t))
        segments(drawing["zone_borders"], QPen(faint, 0.8))
        edge = QColor(text)
        edge.setAlpha(110)
        segments(drawing["edge"], QPen(edge, 1.2))
        if self._park is not None:  # where the uniform park's fence would be
            segments(nz.compare_park(self._park)["uniform_edge"], QPen(edge, 1.4, Qt.PenStyle.DotLine))
        # the diamond
        p.setPen(QPen(edge, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(QPolygonF([self._pt(x, y, t) for x, y in nz.BASES]))
        # zone codes, where they fit
        small = QFont(self.font())
        small.setPointSizeF(max(5.5, small.pointSizeF() * 0.7))
        p.setFont(small)
        fm = QFontMetrics(small)
        code_pen = QColor(text)
        code_pen.setAlpha(90)
        p.setPen(code_pen)
        for code, (x, y, area) in drawing["labels"].items():
            if area * scale * scale < fm.horizontalAdvance(code) * fm.height() * 2.2:
                continue
            c = self._pt(x, y, t)
            p.drawText(QRectF(c.x() - 30, c.y() - fm.height() / 2, 60, fm.height()),
                       Qt.AlignmentFlag.AlignCenter, code)
        # average zones dashed, natural zones solid
        avg = QColor(text)
        avg.setAlpha(150)
        segments(drawing["average_borders"], QPen(avg, 1.2, Qt.PenStyle.DashLine))
        if self._result is not None:
            for pos, segs in self._outlines.items():
                if abs(self._result["reach"].get(pos, 0.0)) > 1e-6:
                    segments(segs, QPen(text, 2.0))
            self._paint_arrows(p, t, text)
        self._paint_extra(p, t, text)
        self._paint_fielders(p, t, text)
        p.restore()
        self._paint_legend(p, text)
        p.end()

    def _paint_extra(self, p, t, text):
        """Anything a subclass draws over the field, under the fielders."""

    def _paint_arrows(self, p, t, text):
        """From each fielder toward the part of a neighbour's zone he reaches, labelled with the share."""
        g = self._grid()
        nx, base, cover = g["nx"], g["base"], self._result["cover"]
        n = len(naturalzone.POSITIONS)
        sums = {}
        for k in g["field"]:
            who = cover[k]
            if not who:
                continue
            for f in who:
                if f != base[k]:
                    s = sums.setdefault((f, base[k]), [0.0, 0.0, 0])
                    s[0] += g["x0"] + (k % nx + 0.5) * naturalzone.CELL
                    s[1] += g["y0"] + (k // nx + 0.5) * naturalzone.CELL
                    s[2] += 1
        small = QFont(self.font())
        small.setPointSizeF(max(6.0, small.pointSizeF() * 0.8))
        small.setBold(True)
        p.setFont(small)
        fm = QFontMetrics(small)
        colour = QColor(self.MADE_UP).darker(130)
        placed: list[QRectF] = []
        for (f, z), (sx, sy, count) in sorted(sums.items(), key=lambda kv: -kv[1][2]):
            share = self._result["coverage"][naturalzone.POSITIONS[z]][naturalzone.POSITIONS[f]]
            if share < self.ARROW_MIN:
                continue
            start = self._pt(*naturalzone.SITES[naturalzone.POSITIONS[f]], t)
            end = self._pt(sx / count, sy / count, t)
            dx, dy = end.x() - start.x(), end.y() - start.y()
            length = math.hypot(dx, dy)
            if length < 12:
                continue
            ux, uy = dx / length, dy / length
            start = QPointF(start.x() + ux * 9, start.y() + uy * 9)
            p.setPen(QPen(colour, 1.6))
            p.drawLine(start, end)
            head = [end, QPointF(end.x() - ux * 7 - uy * 4, end.y() - uy * 7 + ux * 4),
                    QPointF(end.x() - ux * 7 + uy * 4, end.y() - uy * 7 - ux * 4)]
            p.setBrush(colour)
            p.drawPolygon(QPolygonF(head))
            label = f"{share * 100:.1f}% of {naturalzone.POSITIONS[z]}"
            w = fm.horizontalAdvance(label) + 6
            box = QRectF(end.x() + (4 if ux >= 0 else -w - 4), end.y() - fm.height() / 2, w, fm.height())
            for dy in (0, fm.height(), -fm.height(), 2 * fm.height(), -2 * fm.height(), None):
                if dy is None:
                    box = None  # nowhere free near the arrow's head: the arrow goes unlabelled
                    break
                trial = box.translated(0, dy)
                if not any(trial.intersects(r) for r in placed):
                    box = trial
                    break
            if box is None:
                continue
            placed.append(box)
            back = QColor(self.palette().base().color())
            back.setAlpha(200)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(back)
            p.drawRoundedRect(box, 3, 3)
            p.setPen(colour)
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

    def _paint_fielders(self, p, t, text):
        bold = QFont(self.font())
        bold.setBold(True)
        bold.setPointSizeF(max(6.5, bold.pointSizeF() * 0.85))
        p.setFont(bold)
        fm = QFontMetrics(bold)
        for pos in naturalzone.POSITIONS:
            c = self._pt(*naturalzone.SITES[pos], t)
            _name, ratio = self._players.get(pos, (None, None))
            p.setPen(QPen(text, 1))
            p.setBrush(self.palette().base())
            p.drawEllipse(c, 4, 4)
            label = pos if ratio is None else f"{pos} {ratio * 100:.1f}%"
            w = fm.horizontalAdvance(label) + 8
            box = QRectF(c.x() - w / 2, c.y() + 6, w, fm.height() + 2)
            back = QColor(self.palette().base().color())
            back.setAlpha(215)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(back)
            p.drawRoundedRect(box, 3, 3)
            colour = text if ratio is None or round(ratio * 100, 1) == 100.0 else \
                QColor(WON_COLOR if ratio > 1 else LOST_COLOR)
            p.setPen(colour)
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

    def _legend_items(self):
        """(kind, colour, alpha, label) for each legend entry: a swatch, or a dashed / solid line."""
        return [("swatch", self.MADE_UP, 120, "made up by a neighbour"),
                ("swatch", self.GAP, 170, "gap: no one reaches it"),
                ("swatch", self.OVERLAP, 95, "overlap: two reach it"),
                ("dash", None, 0, "average zone"), ("line", None, 0, "natural zone")] \
            + ([("dot", None, 0, "uniform park's fence")] if self._park is not None else []) \
            + [("hint", None, 0, "scroll or +/\u2212 to zoom \u00b7 drag to pan \u00b7 double-click for the whole field")]

    def _legend_layout(self):
        """Each entry's top-left corner, wrapping onto more rows when the widget is narrow, and the
        rows' total height."""
        small = QFont(self.font())
        small.setPointSizeF(max(6.0, small.pointSizeF() * 0.85))
        fm = QFontMetrics(small)
        x, row, spots = float(self.MARGIN), 0, []
        for _kind, _c, _a, label in self._legend_items():
            w = (0 if _kind == "hint" else 22) + fm.horizontalAdvance(label)
            if x > self.MARGIN and x + w > self.width() - self.MARGIN:
                x, row = float(self.MARGIN), row + 1
            spots.append((x, row))
            x += w + 16
        rows = row + 1
        return small, fm, spots, rows * (fm.height() + 4)

    def _paint_legend(self, p, text):
        small, fm, spots, height = self._legend_layout()
        p.setFont(small)
        top = self.height() - self.MARGIN - height
        for (kind, hexcol, alpha, label), (x, row) in zip(self._legend_items(), spots):
            y = top + row * (fm.height() + 4)
            mid = y + fm.height() / 2
            if kind == "hint":
                hint = QColor(text)
                hint.setAlpha(140)
                p.setPen(hint)
                p.drawText(QPointF(x, y + fm.ascent()), label)
                continue
            if kind == "swatch":
                c = QColor(hexcol)
                c.setAlpha(alpha)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawRect(QRectF(x, y + 2, 14, fm.height() - 4))
            else:
                style = {"line": Qt.PenStyle.SolidLine, "dot": Qt.PenStyle.DotLine}.get(kind, Qt.PenStyle.DashLine)
                p.setPen(QPen(text, {"line": 2, "dot": 1.4}.get(kind, 1.2), style))
                p.drawLine(QPointF(x, mid), QPointF(x + 16, mid))
            p.setPen(text)
            p.drawText(QPointF(x + 22, y + fm.ascent()), label)

    # ---- hover ----------------------------------------------------------
    def _zoom_by(self, steps: float, at: QPointF | None = None):
        """Zoom in (``steps`` > 0) or out about ``at`` -- the middle of the drawing if not given --
        keeping the spot there where it is."""
        at = at if at is not None else self._map_rect().center()
        zoom = max(1.0, min(self.MAX_ZOOM, self._zoom * 1.2 ** steps))
        if zoom < 1.001:
            zoom = 1.0  # back to the whole field, whatever the rounding
        self._pan = QPointF(at.x() - (at.x() - self._pan.x()) * zoom / self._zoom,
                            at.y() - (at.y() - self._pan.y()) * zoom / self._zoom)
        self._zoom = zoom
        if zoom == 1.0:
            self._pan = QPointF(0, 0)
        self._zoom_out_btn.setEnabled(zoom > 1.0)
        self._zoom_in_btn.setEnabled(zoom < self.MAX_ZOOM)
        self.update()

    def wheelEvent(self, ev):
        """Zoom in or out about the point under the pointer."""
        steps = ev.angleDelta().y() / 120
        if steps:
            self._zoom_by(steps, ev.position())
        ev.accept()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # the zoom buttons sit in the drawing's top right corner
        x = self.width() - self.MARGIN - self._zoom_in_btn.width()
        self._zoom_in_btn.move(x, self.MARGIN)
        self._zoom_out_btn.move(x, self.MARGIN + self._zoom_in_btn.height() + 4)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag = ev.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            QToolTip.hideText()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mouseDoubleClickEvent(self, ev):
        self._zoom_by(-1e6)  # all the way out: the whole field

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            delta = ev.position() - self._drag
            self._drag = ev.position()
            if self._zoom > 1.0:
                self._pan += delta
                self.update()
            return
        g = self._grid()
        x, y = self._feet(ev.position().x(), ev.position().y(), self._transform())
        i, j = int((x - g["x0"]) / naturalzone.CELL), int((y - g["y0"]) / naturalzone.CELL)
        if not (0 <= i < g["nx"] and 0 <= j < g["ny"]) or g["base"][j * g["nx"] + i] < 0:
            QToolTip.hideText()
            return
        k = j * g["nx"] + i
        usual = naturalzone.POSITIONS[g["base"][k]]
        text = f"Zone {g['codes'][k]}  ·  usually {usual}'s ground"
        if self._result is not None:
            who = sorted(self._result["cover"][k], key=naturalzone.POSITIONS.__getitem__)
            names = [naturalzone.POSITIONS[f] for f in who]
            text += "\n" + ("No one reaches it (a gap)" if not names else
                            "Covered by " + " and ".join(names))
        QToolTip.showText(ev.globalPosition().toPoint(), text, self)


class StadiumField(NaturalZoneField):
    """The Stadiums tab's field: a real park's ground over the Naturalzone tab's uniform park --
    green where the park reaches past the uniform fence (or its foul ground runs further), red
    where the uniform park has ground this one hasn't -- with the Total Zone zones and usual
    zones laid out in the park, and its posted distances at their marks."""

    GAINED, LOST = WON_COLOR, LOST_COLOR

    def __init__(self, parent=None):
        super().__init__(parent)
        self._marks: list = []

    def set_park(self, park, marks: list):
        """``park``: the fence (``stadiums.fence_points``) or None; ``marks``: ``[(label, angle, feet)]``."""
        self._marks = marks
        self.set_result({"park": park} if park is not None else None, {}, {})

    def _shaded(self) -> bool:
        return self._park is not None

    def _render(self):
        g = self._grid()
        nx, ny = g["nx"], g["ny"]
        img = QImage(nx, ny, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        grass = QColor(self.palette().text().color())
        grass.setAlpha(14)
        colours = {0: grass.rgba()}
        for kind, hexcol in ((1, self.GAINED), (-1, self.LOST)):
            c = QColor(hexcol)
            c.setAlpha(150)
            colours[kind] = c.rgba()
        for k, kind in enumerate(naturalzone.compare_park(self._park)["status"]):
            if kind is not None:
                img.setPixel(k % nx, ny - 1 - k // nx, colours[kind])
        self._image = img

    def paintEvent(self, event):
        result, self._result = self._result, None  # no natural zones here: just the park
        try:
            super().paintEvent(event)
        finally:
            self._result = result

    def _paint_extra(self, p, t, text):
        """The posted distances, just beyond the fence at their marks."""
        if self._park is None:
            return
        bold = QFont(self.font())
        bold.setBold(True)
        bold.setPointSizeF(max(6.5, bold.pointSizeF() * 0.85))
        p.setFont(bold)
        fm = QFontMetrics(bold)
        for label, angle, feet in self._marks:
            uniform = naturalzone.fence(angle)
            out = feet + 14
            c = self._pt(out * math.sin(math.radians(angle)), out * math.cos(math.radians(angle)), t)
            tick = f"{feet:.0f}"
            w = fm.horizontalAdvance(tick) + 8
            box = QRectF(c.x() - w / 2, c.y() - fm.height() / 2, w, fm.height() + 2)
            area = self._map_rect()  # kept inside the drawing (centre's would be cut off at the top)
            box.moveTop(max(box.top(), area.top()))
            box.moveLeft(max(area.left(), min(box.left(), area.right() - w)))
            back = QColor(self.palette().base().color())
            back.setAlpha(215)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(back)
            p.drawRoundedRect(box, 3, 3)
            diff = round(feet - uniform)
            p.setPen(text if diff == 0 else QColor(self.GAINED if diff > 0 else self.LOST))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, tick)

    def _legend_items(self):
        return [("swatch", self.GAINED, 150, "ground the uniform park hasn't"),
                ("swatch", self.LOST, 150, "uniform park's ground this park hasn't"),
                ("dot", None, 0, "uniform park's fence"), ("dash", None, 0, "usual zone"),
                ("hint", None, 0, "scroll or +/\u2212 to zoom \u00b7 drag to pan \u00b7 double-click for the whole field")]

    def mouseMoveEvent(self, ev):
        if self._drag is not None or self._park is None:
            return super().mouseMoveEvent(ev)
        g = self._grid()
        x, y = self._feet(ev.position().x(), ev.position().y(), self._transform())
        i, j = math.floor((x - g["x0"]) / naturalzone.CELL), math.floor((y - g["y0"]) / naturalzone.CELL)
        cmp = naturalzone.compare_park(self._park)
        k = j * g["nx"] + i
        if not (0 <= i < g["nx"] and 0 <= j < g["ny"]) or cmp["status"][k] is None:
            QToolTip.hideText()
            return
        kind, who = cmp["status"][k], naturalzone.POSITIONS[cmp["owner"][k]]
        text = {1: f"Gained: ground this park has that the uniform park hasn't  ·  {who}'s",
                -1: f"Lost: the uniform park's ground (usually {who}'s) this park hasn't",
                0: f"Zone {g['codes'][k]}  ·  usually {who}'s ground"}[kind]
        angle = math.degrees(math.atan2(x, y))
        if abs(angle) <= 45:
            here, there = naturalzone.fence_at(self._park, angle), naturalzone.fence(angle)
            text += f"\nFence this way: {here:.0f} ft here, {there:.0f} ft in the uniform park ({here - there:+.0f})"
        QToolTip.showText(ev.globalPosition().toPoint(), text, self)


LEAGUE_NAMES = {"AL": "American League", "NL": "National League", "NNL": "Negro National League",
                "NAL": "Negro American League", "ECL": "Eastern Colored League",
                "NSL": "Negro Southern League"}
SIM_CARDS_GUIDE = (
    "<b>Every card reads the same way</b>: how often something happens, against an average opponent, with the "
    "league's figure in brackets and \u25b2/\u25bc where they differ by 5% or more \u2014 green better, red worse "
    "<i>for the card's owner</i>, grey where it's neither (how often a runner tries to steal).  Batting and "
    "Running are what he does; Pitching and Fielding are what an average batter or runner does against him.  "
    "Each team's first card is its home park: how plate appearances end there for an average batter and pitcher "
    "(grey arrows: a park is neither side's); every game there is played in it, and players' cards are in a "
    "neutral park.<br>"
    "<b>Batters' and pitchers' cards show the same thing</b>: how plate appearances end, as shares of 100%. "
    "A batter's card is him against an average pitcher; a pitcher's card is an average hitter against him (what "
    "he <i>allows</i>, not what he prevents): more hits is green for a batter, fewer is green for a pitcher.<br>"
    "<b>A plate appearance</b> puts the two cards together: each result's chance is the batter's against the "
    "league \u00d7 the pitcher's against the league \u00d7 the league's (log5), then scaled to add up to 100%.  "
    "A batter who walks twice as often as average, facing a pitcher who walks half as often, walks at the "
    "league's rate.<br>"
    "<b>Platoon</b> (batters and pitchers): his plate appearances against each hand on the other side, beside an "
    "average player of his hand there.  In a game, the batter's figures against the pitcher's hand meet the "
    "pitcher's against the batter's side, in the league's rates for that matchup of hands (a switch hitter bats "
    "away from the pitcher).  His real splits (MLB, from 1950) are under Reference: they count for little next to "
    "his whole season, since most of a split is luck.<br>"
    "<b>Running and Fielding are paired the same way as Batting and Pitching</b>: a runner's table is him against "
    "average fielders, a fielder's is an average runner (or batter) against him, and in play the two meet as batter "
    "and pitcher do \u2014 each against his own league, multiplied in odds.  So a runner who takes an extra base "
    "52% of the time (league 38%) against an outfielder who allows 45% (league 50%) takes it about 47% of the time.  "
    "Range and Errors count only on balls to the fielder's own ground (or a hole or gap he shares).  Before each pitch, a runner with the next base "
    "open may steal: his tries and success times the catcher's and the pitcher's, and his success times the "
    "infielders covering the base (second base and short on a steal of second, third base on a steal of third).  "
    "After a hit or out, where "
    "the runners end up is drawn from what really happened in that base-out situation that season "
    "(Retrosheet), weighted toward outcomes where a runner takes extra bases by his own extra bases (or tag-ups "
    "on fly outs) times the arm of the fielder who picked up the ball; and toward double plays by the batter's "
    "own times how often the fielder who fielded the grounder turned them.  Slow regulars are run for late in close "
    "games.<br>"
    "<b>Fielding is individual, ball by ball</b>: each ball in play goes where that season's balls of its kind "
    "went (Retrosheet's hit locations) \u2014 to one fielder's ground, or a hole or gap two share (the 5\u20136 "
    "hole, the left-center gap) \u2014 and only the fielders on the field there count on it: their range sets the "
    "odds of a hit, their errors the odds of an error, a shared ball taking each one's part equally.  Nobody "
    "else on the team, on the field or on the bench, counts on that ball.  A pitcher's real line was earned in "
    "front of his club's fielders all season, so that is taken out position by position (otherwise they would "
    "count twice); each play's fielders are then judged against it.  His real fielding, and how much of the field "
    "is his ground, are under Reference.<br>"
    "<b>Bold names</b> on the table rows and lines (Batting, Pitching; Steals, Extra bases, Tag-ups, Double plays, "
    "Pinch running; Range, Errors, Arms, Turning DPs, Stopping steals) are the categories the path analysis "
    "measures (Platoon too); Order and Usage decide who plays when.  Managing gives how often he is walked "
    "intentionally and sacrifices, each as odds against the league's: the games decide both before the pitch, by "
    "the situation (base-out state, inning, margin), and the path analysis counts them in Batting.")
SIM_PATH_GUIDE = (
    "<p><b>What it shows.</b>  What each part of each team -- and each of its rows -- did, measured by playing the "
    "same games again (same seed) with only that part made league-average or switched off.  Each part is followed "
    "along the chain by which runs, and so wins, are made.</p>"
    "<p><b>The chain.</b>  Runs come from batters reaching base and being driven around before the outs run out "
    "(Bill James); a game's 27 outs are the clock.  So the columns, left to right: <b>Outs at the plate</b> (the "
    "share of plate appearances that end in an out) and <b>Outs on the bases</b> (caught stealing, picked off, "
    "thrown out on the bases, the second out of a double play) decide how many <b>Plate appearances</b> the 27 outs "
    "buy; of those batters so many are <b>Batters reaching</b> (on base or home); of those, <b>Reachers who "
    "score</b>; that makes the <b>Runs</b>, and the runs the <b>Wins</b>.  Each lane's column heads give its own "
    "figures; each value under them is what that row changed (pt: percentage points).  A fine pitching staff, say, "
    "reads: more outs at the plate, so fewer plate appearances, fewer reaching, fewer runs, more wins for it.  A "
    "part can work through one link and cost at another -- aggressive runners take more outs on the bases but more "
    "of their reachers score -- and the track shows both.</p>"
    "<p><b>Colours.</b>  Each value is coloured for the team that owns the part: green helped it, red hurt it.  "
    "Grey with a ? is within two standard errors of nothing, game by game (luck?).  Faint: never came up -- the "
    "replay was identical (no defensive replacement ever qualified, no rest days with Fatigue off...).  The line "
    "from a box to its track takes the colour of its runs.</p>"
    "<p><b>The lanes.</b>  Each team at bat has a lane.  Its batting order and baserunning are its own; the "
    "pitching staff and fielding are the other team's.  A box's bold track is the whole part made league-average; "
    "the tracks under it are each row alone (the cards' categories), so every branch of the model shows.  The parts "
    "and rows aren't exactly additive.</p>"
    "<p><b>Platoon.</b>  Inside Batting order and Pitching staff, Batting or Pitching is the players made "
    "league-average, and Platoon is only their own left/right splits taken away: each player then has his "
    "whole season's figures against both hands, with the league's platoon effect still in play.</p>"
    "<p><b>Fielding and baserunning</b> include the fielders against the runners: Arms (outfielders on extra bases "
    "and tag-ups), Turning DPs, and Stopping steals (catcher and pitcher).  League-average fielding is MLB-average "
    "fielders at every position (range factors count chances, so a club's are first divided by its staff's balls in "
    "play per out against the league's, to leave the fielders' skill).</p>"
    "<p><b>Managing and home field</b> (the band at the bottom).  These act on both sides of a team's games, so each "
    "row has two tracks: <b>at bat</b>, the team's own chain, and <b>against</b>, the other side's chain against it "
    "(coloured for the team: more outs there is green).  Its wins come from both.  The rows, each switched off for "
    "that team: <b>Sacrifices</b> (its batters swing away instead), <b>Intentional walks</b> (its pitchers pitch to "
    "the batters it would have walked), <b>Pinch hitting</b> (for the pitcher and for regulars), <b>Defensive "
    "subs</b> (its late-inning replacements with a lead), <b>Rest days</b> (its regulars play every game) and "
    "<b>Tired relievers</b> (its relievers are always fresh); and its <b>Home field</b>: its <b>Park</b> made "
    "neutral and its <b>Home-field edge</b> (its batters' edge at home) taken away.  The boxes' bold tracks are all "
    "of theirs at once.  Platoon is inside Batting order and Pitching staff; pinch running inside Baserunning.</p>"
    "<p><b>Room.</b>  The window opens as large as the screen allows; scroll across and down to see the whole "
    "model.  It plays the same games as your run (the first 2,000 of a longer one) again once per part and once "
    "per row (56 times), so it takes several minutes; the count of replays done shows at the top while it works.  "
    "Each of those is a game that never happened -- the same game with one thing switched off -- so it can't be "
    "read off the run itself; the run's own games are the baseline it's compared with.</p>"
    "<p><b>Not measured.</b>  Order and Usage (who bats where, who pitches when): there's no league-average "
    "version of them.</p>")
SIM_CARDS_GUIDE = "<p>" + SIM_CARDS_GUIDE.replace("<br>", "</p><p>") + "</p>"  # a paragraph per topic


ROUND_HEADINGS = {"F": "Wild Card", "D": "Division Series", "L": "Championship Series", "W": "World Series"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _short_date(iso: str, with_year: bool = False) -> str:
    """"2025-10-07" -> "Oct 7" (or "Oct 7, 2025")."""
    try:
        y, m, d = (int(p) for p in iso[:10].split("-"))
    except ValueError:
        return iso
    return f"{_MONTHS[m - 1]} {d}" + (f", {y}" if with_year else "")


class PostseasonBracket(QWidget):
    """One season's postseason drawn as a bracket: the American League's rounds on the left
    working inward, the National League's on the right, the World Series in the middle --
    only the rounds that were played that year.  Each series is a box with a row per club
    and a cell per game: a check and the club's runs for a win, a cross for a loss, a dash for a
    tie; a game still to come shows its date (italic if it's only played if necessary).
    Hovering a game gives the date, the full score and the ballpark."""

    ROW_H, HEAD_H, V_GAP, H_GAP, MARGIN, TOP, LEAGUE_GAP = 22, 18, 18, 40, 16, 58, 56

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series: list[dict] = []
        self._children: dict[str, list[str]] = {}
        self._season = 0
        self._scale = 1.0
        self._boxes: dict[str, QRectF] = {}
        self._headings: list[tuple[QRectF, str]] = []
        self._tips: list[tuple[QRectF, str]] = []
        self._natural = (0.0, 0.0)
        self._note_rect: QRectF | None = None
        self._banner_rect: QRectF | None = None
        self.stacked = False  # True: AL above NL rather than AL left, NL right
        self.sides: tuple = ("AL", "NL")  # the leagues drawn left and right (Negro leagues: NNL...)
        self.setMouseTracking(True)

    def set_stacked(self, stacked: bool):
        if stacked == self.stacked:
            return
        self.stacked = stacked
        self._layout()
        self._apply_scale()
        self.update()

    # ---- data and layout ------------------------------------------------
    def set_data(self, season: int, series: list[dict], sides: tuple = ("AL", "NL")):
        self._season, self._series, self.sides = season, series, sides
        self._children = mlbstats.link_postseason(series)
        self._layout()
        self._apply_scale()
        self.update()

    def natural_width(self) -> float:
        return self._natural[0]

    def set_scale(self, scale: float):
        if abs(scale - self._scale) > 1e-3:
            self._scale = scale
            self._apply_scale()
            self.update()

    def _apply_scale(self):
        w, h = self._natural
        self.setFixedSize(max(1, int(w * self._scale + 0.5)), max(1, int(h * self._scale + 0.5)))

    def _fonts(self):
        base = QFont(self.font())
        bold = QFont(base)
        bold.setBold(True)
        small = QFont(base)
        small.setPointSizeF(max(6.0, base.pointSizeF() * 0.85))
        italic = QFont(small)
        italic.setItalic(True)
        return base, bold, small, italic

    def _box_height(self) -> float:
        return self.HEAD_H + 2 * self.ROW_H

    def _layout(self):
        base, bold, _small, _italic = self._fonts()
        fm, fm_bold = QFontMetrics(base), QFontMetrics(bold)
        names = [t["name"] for s in self._series for t in s["teams"]] or [""]
        self._name_w = max(fm_bold.horizontalAdvance(n) for n in names) + 14
        self._wins_w = fm_bold.horizontalAdvance("88") + 10
        self._cell_w = max(fm.horizontalAdvance("✗ 88"), fm.horizontalAdvance("Oct 30")) + 8
        box_h, slot = self._box_height(), self._box_height() + self.V_GAP
        rounds = [r for r in mlbstats.POSTSEASON_ROUNDS[:-1] if any(s["round"] == r for s in self._series)]
        left_side, right_side = self.sides
        if self.stacked:  # AL above NL, each round one column, the World Series at the right
            columns = [("*", r) for r in rounds] + [(None, "W")]
        else:  # as on MLB.com: AL rounds left, NL rounds right, the World Series between
            columns = [(left_side, r) for r in rounds] + [(None, "W")] + [(right_side, r) for r in reversed(rounds)]

        def members_of(league, r):
            return [s for s in self._series
                    if s["round"] == r and (r == "W" or league == "*" or s["league"] == league)]

        def box_width(s):
            return self._name_w + self._wins_w + s["slots"] * self._cell_w

        col_x, x = {}, self.MARGIN
        for league, r in columns:
            w = max((box_width(s) for s in members_of(league, r)), default=0)
            col_x[(league, r)] = (x, w)
            if w:
                x += w + self.H_GAP
        width = x - self.H_GAP + self.MARGIN if x > self.MARGIN else self.MARGIN * 2

        # vertical placement: walk each league's tree from its latest round; a series with no
        # feeders gets the next free slot, one with feeders sits level with their middle
        centres: dict[str, float] = {}

        def place(sid: str, counter: list) -> float:
            kids = sorted(self._children.get(sid, []))
            if kids:
                ys = [place(k, counter) for k in kids]
                centres[sid] = sum(ys) / len(ys)
            else:
                centres[sid] = counter[0] * slot + box_h / 2
                counter[0] += 1
            return centres[sid]

        side_heights, roots_of = {}, {}
        for league in self.sides:
            last = next((r for r in reversed(rounds) if any(s["round"] == r and s["league"] == league
                                                             for s in self._series)), None)
            roots_of[league] = sorted(s["id"] for s in self._series if s["round"] == last and s["league"] == league)
            counter = [0]
            for sid in roots_of[league]:
                place(sid, counter)
            side_heights[league] = counter[0] * slot - self.V_GAP if counter[0] else 0

        self._boxes, self._headings = {}, []
        if self.stacked and rounds:
            nl_top = side_heights[left_side] + self.LEAGUE_GAP if side_heights[left_side] else 0
            for s in self._series:
                if s["league"] == right_side and s["id"] in centres:
                    centres[s["id"]] += nl_top
            tallest = nl_top + side_heights[right_side] if side_heights[right_side] else side_heights[left_side]
            tallest = max(tallest, box_h)
            ends = [centres[r] for league in self.sides for r in roots_of[league][:1]]
            middle = sum(ends) / len(ends) if ends else tallest / 2
            left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            if side_heights[left_side]:
                self._headings.append((QRectF(self.MARGIN, self.TOP - 22, width, 18),
                                       LEAGUE_NAMES.get(left_side, left_side), left))
            if side_heights[right_side]:
                self._headings.append((QRectF(self.MARGIN, self.TOP + nl_top - 26, width, 18),
                                       LEAGUE_NAMES.get(right_side, right_side), left))
        else:
            tallest = max([box_h, *side_heights.values()])
            for league in self.sides:  # centre the shorter side on the taller one
                shift = (tallest - side_heights[league]) / 2
                for s in self._series:
                    if s["league"] == league and s["id"] in centres:
                        centres[s["id"]] += shift
            middle = tallest / 2
        for s in self._series:
            if s["round"] == "W":
                centres[s["id"]] = middle
            centres.setdefault(s["id"], middle)  # unlinked (shouldn't happen)

        for (league, r), (cx, cw) in col_x.items():
            if not cw:
                continue
            members = members_of(league, r)
            title = members[0]["title"] if members else ROUND_HEADINGS[r]
            relabel = bool(league) and league in self.sides  # the league goes back on in front
            if relabel and title[:3] in ("AL ", "NL "):
                title = title[3:]
            head = f"{league} {title}" if relabel else title
            self._headings.append((QRectF(cx, self.MARGIN, cw, 20), head, Qt.AlignmentFlag.AlignCenter))
            for s in members:
                bw = box_width(s)
                bx = cx if league == right_side and not self.stacked else cx + (cw - bw)  # face the next round
                if r == "W":
                    bx = cx + (cw - bw) / 2
                self._boxes[s["id"]] = QRectF(bx, self.TOP + centres[s["id"]] - box_h / 2, bw, box_h)
        # headings as wide as their text, centred on their column
        self._headings = [(QRectF(r.center().x() - max(r.width(), fm_bold.horizontalAdvance(h) + 12) / 2,
                                  r.top(), max(r.width(), fm_bold.horizontalAdvance(h) + 12), r.height()), h, a)
                          if a == Qt.AlignmentFlag.AlignCenter else (r, h, a) for r, h, a in self._headings]
        # the champions' banner over the World Series box, and a series note (pre-1903: how it
        # was decided, a dispute...) under it -- each as wide as it needs, centred on the box
        self._note_rect = self._banner_rect = None
        height = self.TOP + tallest + self.MARGIN + 8
        world = next((s for s in self._series if s["round"] == "W"), None)
        if world and world["id"] in self._boxes:
            box = self._boxes[world["id"]]
            banner = self._banner_text(world)
            if banner:
                banner_w = fm_bold.horizontalAdvance(banner[0]) + 16
                self._banner_rect = QRectF(box.center().x() - banner_w / 2, box.top() - 26, banner_w, 20)
            if world.get("note"):
                note_w = max(box.width(), 460)
                flags = int(Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap)
                note_h = QFontMetrics(_small).boundingRect(0, 0, int(note_w), 1000, flags, world["note"]).height()
                self._note_rect = QRectF(box.center().x() - note_w / 2, box.bottom() + 8, note_w, note_h + 4)
                height = max(height, self._note_rect.bottom() + self.MARGIN)
        # anything hanging off the left edge moves everything right; the width takes in the rest
        rects = ([r for r, _h, _a in self._headings] + list(self._boxes.values())
                 + [r for r in (self._banner_rect, self._note_rect) if r is not None])
        shift = max(0.0, self.MARGIN - min((r.left() for r in rects), default=self.MARGIN))
        if shift:
            for r in rects:
                r.translate(shift, 0)
        width = max([width] + [r.right() + self.MARGIN for r in rects])
        self._natural = (width, height)

    def _banner_text(self, world: dict) -> tuple[str, bool] | None:
        """The line over the World Series box -- ("★ 1948 champions: … ★", True) -- or, for a
        finished series with no winner (1885, 1890), ("1885: no winner", False)."""
        if world["winner"]:
            champ = next(t["name"] for t in world["teams"] if t["id"] == world["winner"])
            label = world.get("winner_label", "champions")
            return f"\u2605 {self._season} {label}: {champ} \u2605", True
        if world.get("note") and world["games"] and all(g["state"] == "final" for g in world["games"]):
            return f"{self._season}: no winner", False
        return None

    # ---- painting -------------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(self._scale, self._scale)
        pal = self.palette()
        text, dim = pal.text().color(), pal.placeholderText().color()
        base, bold, small, italic = self._fonts()
        self._tips = []

        p.setFont(bold)
        p.setPen(text)
        for rect, head, align in self._headings:
            p.drawText(rect, align, head)

        # connectors first, so the boxes sit on top of them
        p.setPen(QPen(pal.mid().color(), 1.4))
        for parent_id, kids in self._children.items():
            if parent_id not in self._boxes:
                continue
            pr = self._boxes[parent_id]
            for kid in kids:
                if kid not in self._boxes:
                    continue
                kr = self._boxes[kid]
                if kr.center().x() < pr.center().x():
                    x0, x1 = kr.right(), pr.left()
                else:
                    x0, x1 = kr.left(), pr.right()
                y0, y1 = kr.center().y(), pr.center().y()
                xm = (x0 + x1) / 2
                p.drawPolyline([QPointF(x0, y0), QPointF(xm, y0), QPointF(xm, y1), QPointF(x1, y1)])

        by_id = {s["id"]: s for s in self._series}
        for sid, rect in self._boxes.items():
            self._paint_series(p, by_id[sid], rect, pal, text, dim, base, bold, small, italic)

        world = next((s for s in self._series if s["round"] == "W"), None)
        if world and self._banner_rect is not None:
            banner, won = self._banner_text(world)
            p.setFont(bold)
            p.setPen(QColor(WON_COLOR) if won else dim)
            p.drawText(self._banner_rect, Qt.AlignmentFlag.AlignCenter, banner)
        if self._note_rect is not None and world is not None:
            p.setFont(small)
            p.setPen(dim)
            p.drawText(self._note_rect, int(Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap), world["note"])
        p.end()

    def _paint_series(self, p, s, rect, pal, text, dim, base, bold, small, italic):
        p.setPen(QPen(pal.mid().color(), 1))
        p.setBrush(pal.base())
        p.drawRoundedRect(rect, 5, 5)
        head = QRectF(rect.left(), rect.top(), rect.width(), self.HEAD_H)
        p.setBrush(pal.alternateBase())
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(head.adjusted(1, 1, -1, 0), 4, 4)

        cells_x = rect.left() + self._name_w + self._wins_w
        p.setFont(small)
        p.setPen(dim)
        label = s.get("format") or ("one game" if s["best_of"] == 1 else f"best of {s['best_of']}")
        label_rect = QRectF(rect.left() + 6, rect.top(), cells_x - rect.left() - 6, self.HEAD_H)
        if s.get("official") is False:  # a Negro league series that doesn't count as official
            p.setPen(QColor(LIVE_COLOR))
            label = "unofficial \u00b7 " + label
        p.drawText(label_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   QFontMetrics(small).elidedText(label, Qt.TextElideMode.ElideRight,
                                                  int(cells_x - rect.left() - 8)))
        p.setPen(dim)
        about = "\n\n".join(t for t in (s.get("status", ""), s.get("note", "")) if t)
        if about:
            self._tips.append((label_rect, f"{s['title']}\n{about}"))
        for i in range(s["slots"]):
            p.drawText(QRectF(cells_x + i * self._cell_w, rect.top(), self._cell_w, self.HEAD_H),
                       Qt.AlignmentFlag.AlignCenter, f"G{i + 1}")

        top = rect.top() + self.HEAD_H
        p.setPen(QPen(pal.midlight().color(), 1))
        p.drawLine(QPointF(rect.left(), top), QPointF(rect.right(), top))
        p.drawLine(QPointF(rect.left() + 4, top + self.ROW_H), QPointF(rect.right() - 4, top + self.ROW_H))
        for i in range(s["slots"]):
            x = cells_x + i * self._cell_w
            p.drawLine(QPointF(x, top + 3), QPointF(x, rect.bottom() - 3))

        decided = s["winner"] is not None
        for row, team in enumerate(s["teams"]):
            y = top + row * self.ROW_H
            won = team["id"] == s["winner"]
            if team["placeholder"]:
                p.setFont(italic)
                p.setPen(dim)
            else:
                p.setFont(bold if won else base)
                p.setPen(dim if decided and not won else text)
            name_rect = QRectF(rect.left() + 7, y, self._name_w - 7, self.ROW_H)
            p.drawText(name_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, team["name"])
            if not team["placeholder"] and any(g["state"] != "scheduled" for g in s["games"]):
                p.setFont(bold if won else base)
                p.drawText(QRectF(rect.left() + self._name_w, y, self._wins_w, self.ROW_H),
                           Qt.AlignmentFlag.AlignCenter, str(s["wins"].get(team["id"], 0)))

        names = {t["id"]: t["name"] for t in s["teams"]}
        for i, g in enumerate(s["games"]):
            cell = QRectF(cells_x + i * self._cell_w, top, self._cell_w, 2 * self.ROW_H)
            self._tips.append((cell, self._game_tip(s, g, names)))
            if g["state"] == "scheduled":
                p.setFont(italic if g["if_necessary"] else small)
                p.setPen(dim)
                month_day = _short_date(g["date"]).split(" ")
                for row, part in enumerate(month_day[:2]):
                    p.drawText(QRectF(cell.left(), top + row * self.ROW_H, self._cell_w, self.ROW_H),
                               Qt.AlignmentFlag.AlignCenter, part)
                continue
            for row, team in enumerate(s["teams"]):
                runs = g["score"].get(team["id"])
                if runs is None:
                    continue
                other = next((v for k, v in g["score"].items() if k != team["id"]), None)
                if g.get("counted") is False:  # played, but thrown out or not counted
                    p.setFont(base)
                    p.setPen(dim)
                    p.drawText(QRectF(cell.left(), top + row * self.ROW_H, self._cell_w, self.ROW_H),
                               Qt.AlignmentFlag.AlignCenter, f"({runs})*")
                    continue
                if g["state"] == "live":
                    mark, color, font = "●", QColor(LIVE_COLOR), base
                elif g["tie"]:
                    mark, color, font = "\u2013", dim, base
                elif other is not None and runs > other:
                    mark, color, font = "✓", QColor(WON_COLOR), bold
                else:
                    mark, color, font = "✗", QColor(LOST_COLOR), base
                p.setFont(font)
                p.setPen(color)
                p.drawText(QRectF(cell.left(), top + row * self.ROW_H, self._cell_w, self.ROW_H),
                           Qt.AlignmentFlag.AlignCenter, f"{mark} {runs}" + ("*" if g.get("note") else ""))
        for row, team in enumerate(s["teams"]):
            self._tips.append((QRectF(rect.left(), top + row * self.ROW_H, self._name_w, self.ROW_H),
                               team["name"]))

    def _game_tip(self, s: dict, g: dict, names: dict) -> str:
        away, home = names.get(g["away"], "?"), names.get(g["home"], "?")
        lines = [f"{s['title']}, game {g['number']} — {_short_date(g['date'], with_year=True)}"]
        if g["state"] == "scheduled":
            lines.append(f"{away} at {home}" + (" (if necessary)" if g["if_necessary"] else ""))
        else:
            a, h = g["score"].get(g["away"]), g["score"].get(g["home"])
            status = "in progress" if g["state"] == "live" else ("tie" if g["tie"] else "final")
            lines.append(f"{away} {a}, {home} {h} ({status})")
        if g["venue"]:
            lines.append(g["venue"])
        if g.get("note"):
            lines.append(g["note"][0].upper() + g["note"][1:])
        return "\n".join(lines)

    def event(self, ev):
        if ev.type() == QEvent.Type.ToolTip:
            pos = ev.position() if hasattr(ev, "position") else QPointF(ev.pos())
            point = QPointF(pos.x() / self._scale, pos.y() / self._scale)
            tip = next((t for r, t in self._tips if r.contains(point)), None)
            if tip:
                QToolTip.showText(ev.globalPos(), tip, self)
            else:
                QToolTip.hideText()
                ev.ignore()
            return True
        return super().event(ev)


class BaseDiamond(QWidget):
    """The Gameday tab's little infield: who is on which base, the outs, and when that was (after
    the play picked, or right now in a game in progress).  Occupied bases are filled, with the
    runner's surname beside them and the full names in the tooltip.  Clicking it emits
    ``clicked`` (the tab uses that to go back to "now")."""

    clicked = pyqtSignal()
    ON_COLOR = LIVE_COLOR

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bases = ("", "", "")
        self._outs = None
        self._caption = ""
        self._runs = None
        self._homer = ""
        self._score = ""
        self._matchup = ("", "")
        self._surnames: dict[str, str] = {}  # full name -> MLB's surname, for the game shown
        # at least this wide; any spare width in the header is the runners' names' to use
        self.setMinimumWidth(270)
        self.setFixedHeight(4 * self.fontMetrics().height() + 2 * 36 + 40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_state(self, bases=("", "", ""), outs=None, caption: str = "", hint: str = "", runs=None,
                  homer: str = "", score: str = "", matchup=("", "")):
        """``runs``: the runs scored on the play shown (None: no runs line, as for "now");
        ``homer``: who hit the home run, when the play is one (named by home plate); ``score``:
        the score after it ("NYY 3–2 LAD"), shown after the runs; ``matchup``: (batter, pitcher),
        shown as "Batter facing Pitcher" under the caption."""
        self._bases, self._outs, self._caption, self._runs = tuple(bases), outs, caption, runs
        self._homer = homer
        self._score = score if runs is not None else ""
        self._matchup = tuple(matchup) if matchup and all(matchup) else ("", "")
        names = [f"{label}: {name}" for label, name in zip(("1st", "2nd", "3rd"), bases) if name]
        tip = [caption] if caption else []
        if self._matchup[0]:
            tip.append(f"{self._matchup[0]} facing {self._matchup[1]}")
        if runs is not None:
            tip.append(self._runs_text() + (f" · {self._score}" if self._score else ""))
        if homer:
            tip.append(f"Home run: {homer}")
        tip.append("\n".join(names) if names else "Bases empty")
        if hint:
            tip.append(hint)
        self.setToolTip("\n".join(tip))
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def _runs_text(self) -> str:
        return "No runs scored" if not self._runs else f"{self._runs} run{'s' if self._runs != 1 else ''} scored"

    def set_surnames(self, surnames: dict[str, str]):
        """MLB's surnames for the players of the game shown (``parse_gameday``'s ``surnames``)."""
        self._surnames = dict(surnames or {})
        self.update()

    def _surname(self, name: str) -> str:
        """MLB's own surname for the player ("Josue De Paula" -> "De Paula", "Vladimir Guerrero Jr." ->
        "Guerrero Jr."); for a name the game's roster doesn't have, the last word (with a Jr./Sr./II...)."""
        if name in self._surnames:
            return self._surnames[name]
        parts = name.split()
        if len(parts) > 2 and parts[-1].rstrip(".").lower() in ("jr", "sr", "ii", "iii", "iv"):
            return " ".join(parts[-2:])
        return parts[-1] if parts else ""

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        text = pal.color(QPalette.ColorRole.WindowText)
        faint = QColor(text)
        faint.setAlpha(110)
        w = self.width()
        metrics = p.fontMetrics()
        line = metrics.height()

        # caption on top
        p.setPen(text)
        p.drawText(QRectF(0, 0, w, line), Qt.AlignmentFlag.AlignCenter,
                   metrics.elidedText(self._caption, Qt.TextElideMode.ElideRight, w))
        # who was batting and who pitching, by surname (full names in the tooltip)
        if self._matchup[0]:
            batter, pitcher = self._matchup
            words = f"{self._surname(batter)} facing {self._surname(pitcher)}"
            p.drawText(QRectF(0, line, w, line), Qt.AlignmentFlag.AlignCenter,
                       metrics.elidedText(words, Qt.TextElideMode.ElideRight, w))
        # the runs scored on the play, in bold just under that (the lines are kept even when
        # empty, so the diamond doesn't jump about)
        if self._runs is not None:
            runs_font = p.font()
            runs_font.setBold(True)
            p.setFont(runs_font)
            bold_metrics = QFontMetrics(runs_font)
            runs_text = self._runs_text()
            score_text = f" · {self._score}" if self._score else ""
            x = max(0.0, (w - bold_metrics.horizontalAdvance(runs_text + score_text)) / 2)
            p.setPen(QColor(WON_COLOR) if self._runs else text)
            p.drawText(QRectF(x, 2 * line, w - x, line), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       runs_text)
            if score_text:  # the score after the play, in the ordinary colour
                x += bold_metrics.horizontalAdvance(runs_text)
                p.setPen(text)
                p.drawText(QRectF(x, 2 * line, max(0.0, w - x), line),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           bold_metrics.elidedText(score_text, Qt.TextElideMode.ElideRight, int(max(0.0, w - x))))
            p.setFont(self.font())
            p.setPen(text)

        # the infield: home at the bottom, 2nd at the top
        cx, r, half = w / 2, 36, 7
        cy = 4 * line + half + 8 + r  # room above 2nd for the caption, matchup, runs and a runner's name
        spots = {"1B": QPointF(cx + r, cy), "2B": QPointF(cx, cy - r), "3B": QPointF(cx - r, cy),
                 "home": QPointF(cx, cy + r)}
        p.setPen(QPen(faint, 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(QPolygonF([spots["home"], spots["1B"], spots["2B"], spots["3B"]]))
        plate = spots["home"]
        p.setBrush(QBrush(faint))
        p.drawPolygon(QPolygonF([QPointF(plate.x() - 5, plate.y() - 4), QPointF(plate.x() + 5, plate.y() - 4),
                                 QPointF(plate.x() + 5, plate.y()), QPointF(plate.x(), plate.y() + 5),
                                 QPointF(plate.x() - 5, plate.y())]))
        if self._homer:  # the home run's hitter, beside the plate
            hr_font = p.font()
            hr_font.setBold(True)
            p.setFont(hr_font)
            p.setPen(text)
            room = w - (plate.x() + 10)
            p.drawText(QRectF(plate.x() + 10, plate.y() - line / 2, room, line),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       QFontMetrics(hr_font).elidedText(f"{self._surname(self._homer)} (HR)",
                                                        Qt.TextElideMode.ElideRight, int(room)))
            p.setFont(self.font())
        on_color = QColor(self.ON_COLOR)
        bold = p.font()
        bold.setBold(True)
        for key, name in zip(("1B", "2B", "3B"), self._bases):
            c = spots[key]
            bag = QPolygonF([QPointF(c.x(), c.y() - half), QPointF(c.x() + half, c.y()),
                             QPointF(c.x(), c.y() + half), QPointF(c.x() - half, c.y())])
            p.setPen(QPen(on_color.darker(130) if name else faint, 1.4))
            p.setBrush(QBrush(on_color) if name else Qt.BrushStyle.NoBrush)
            p.drawPolygon(bag)
            if not name:
                continue
            p.setFont(bold)
            label = self._surname(name)
            room = cx - r - half - 6
            if key == "2B":
                box = QRectF(cx - 70, c.y() - half - 3 - line, 140, line)
                align = Qt.AlignmentFlag.AlignCenter
                room = 140
            elif key == "1B":
                box = QRectF(c.x() + half + 4, c.y() - line / 2, room, line)
                align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            else:
                box = QRectF(c.x() - half - 4 - room, c.y() - line / 2, room, line)
                align = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            p.setPen(text)
            p.drawText(box, align, QFontMetrics(bold).elidedText(label, Qt.TextElideMode.ElideRight, int(room)))
            p.setFont(self.font())

        # the outs, as three lights along the bottom
        if self._outs is not None:
            y = self.height() - line / 2 - 2
            label = "Outs"
            lw = metrics.horizontalAdvance(label) + 6
            x0 = cx - (lw + 3 * 14) / 2
            p.setPen(text)
            p.drawText(QRectF(x0, y - line / 2, lw, line), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       label)
            for i in range(3):
                lit = i < self._outs
                p.setPen(QPen(QColor(LOST_COLOR) if lit else faint, 1.2))
                p.setBrush(QBrush(QColor(LOST_COLOR)) if lit else Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(x0 + lw + 5 + i * 14, y), 4.5, 4.5)
        p.end()


class _FitTable(QTableWidget):
    """A table that calls ``on_resize`` whenever its width changes (to re-fit its columns)."""

    def __init__(self, on_resize, parent=None):
        super().__init__(parent)
        self._on_resize = on_resize

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self._on_resize()


class _BracketScroll(QScrollArea):
    """A scroll area over a column of brackets (the MLB postseason, then the Negro leagues'),
    each laid out and shrunk to the viewport's width on its own."""

    AUTO_MIN_SCALE = 0.8  # "auto" goes side by side only if that fits at this size or larger

    def __init__(self, content: QWidget):
        super().__init__()
        self.brackets: list[PostseasonBracket] = []
        self.fit = True
        self.layout_mode = "auto"  # or "side", "stacked"
        self.setWidget(content)
        self.setWidgetResizable(True)

    def refit(self):
        room = self.viewport().width() - 24
        for bracket in self.brackets:
            if bracket.isHidden():
                continue
            if self.layout_mode == "auto":
                bracket.set_stacked(False)
                if room > 0 and bracket.natural_width() * self.AUTO_MIN_SCALE > room:
                    bracket.set_stacked(True)
            else:
                bracket.set_stacked(self.layout_mode == "stacked")
            natural = bracket.natural_width()
            scale = min(1.0, room / natural) if self.fit and natural > 0 and room > 0 else 1.0
            bracket.set_scale(max(0.45, scale))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refit()


SEASON_SCOPES = ("league", "same_league", "same_division")  # the samples drawn from one season's teams


def _scope_label(kind: str, season: int, team_id, live_from: int) -> str:
    """The league or division a season scope covers ("American League East"), for a sample
    description; empty for every team.  From the chosen team's record that season."""
    if kind == "league":
        return ""
    record = mlbstats.get_team_map(season, LIVE_TTL if season >= live_from else None).get(team_id) or {}
    return (record.get("league") if kind == "same_league" else record.get("division")) or ""


def _all_teams(label: str) -> str:
    return f"all {label} teams" if label else "all teams"


def _every_team_players(label: str) -> str:
    return f"every {label} team's players" if label else "every team's players"


class _SampleScope:
    """The "Sample:" choice of the Custom, Correlation, Regression and Factor Analysis
    tabs -- which (team, season) pairs the numbers come from.  Two rows of radio buttons:
    the chosen team, over all its seasons or in the chosen season; and the chosen season
    across all teams, across the chosen team's league, or (from 1969, when divisions
    began) across its division.  ``checked()`` names the choice: "history", "roster",
    "league", "same_league" or "same_division"."""

    LABELS = {
        "history": "This team, across all its seasons",
        "roster": "This team, this season",
        "league": "This season, across all teams",
        "same_league": "This season, across all league teams",
        "same_division": "This season, across all divisional teams",
    }

    def __init__(self, window, default: str, on_changed):
        self.on_changed = on_changed
        self.radios = {kind: QRadioButton(text) for kind, text in self.LABELS.items()}
        self.radios[default].setChecked(True)
        self.group = QButtonGroup(window)
        for button in self.radios.values():
            self.group.addButton(button)
        self.group.buttonClicked.connect(lambda _b: on_changed())
        self.radios["same_league"].setToolTip("The league of the team chosen above, in the chosen season.")
        self._division_tip = "The division of the team chosen above, in the chosen season."
        self.radios["same_division"].setToolTip(self._division_tip)

        label = QLabel("Sample:")
        indent = label.sizeHint().width() + 6
        first, second = QHBoxLayout(), QHBoxLayout()
        first.addWidget(label)
        for kind in ("history", "roster"):
            first.addWidget(self.radios[kind])
        second.addSpacing(indent)
        for kind in ("league", "same_league", "same_division"):
            second.addWidget(self.radios[kind])
        for row in (first, second):
            row.addStretch(1)
        self.box = QVBoxLayout()
        self.box.setSpacing(2)
        self.box.addLayout(first)
        self.box.addLayout(second)

    def checked(self) -> str:
        return next(kind for kind, button in self.radios.items() if button.isChecked())

    def sync_division(self, season: int):
        """Divisions began in 1969: before then there is no such sample."""
        available = season >= mlbstats.INTERDIVISION_FIRST_SEASON
        button = self.radios["same_division"]
        button.setEnabled(available)
        button.setToolTip(self._division_tip if available else
                          f"Divisions began in {mlbstats.INTERDIVISION_FIRST_SEASON}.")
        if not available and button.isChecked():
            self.radios["league"].setChecked(True)
            self.on_changed()


# --------------------------------------------------------------------------- #
#  Team / Season / Player drop-downs for one side of the Head-To-Head tab     #
# --------------------------------------------------------------------------- #
def _ordinal_suffix(n: int) -> str:
    """1 -> "st", 2 -> "nd", 11 -> "th" ..."""
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


class _PlayerPicker(QGroupBox):
    """Pick a player by team, season and name.  ``group`` is the MLB stat group
    whose roster fills the Player list ("hitting" for the batter, "pitching"
    for the pitcher), or "all" for everyone who appeared for the team that
    season in any role (batting, pitching or fielding) -- that mode starts on a
    "choose a player" placeholder, so nothing is selected until the user picks.
    ``group="fielding"`` with ``with_player=False`` is just a team-and-season
    chooser: no Player drop-down, and the whole fielding roster (one row per player
    per position) goes out through ``roster_loaded``.

    Changing the season re-lists the teams as the league stood that season
    (same idea as the other tabs' team drop-downs), and any change of team or
    season reloads the roster.  ``changed`` fires once a new roster has landed
    and whenever the player choice changes; ``selection()`` then describes the
    player.  ``window`` is the MainWindow, borrowed for its background-job
    runner and its team/season caches.
    """

    changed = pyqtSignal()
    # (team_id, team_name, season, rows) once a roster has loaded, or None if it
    # couldn't be -- for a user of the picker that wants the whole roster rather
    # than one chosen player (``with_player=False``; the Range tab).
    roster_loaded = pyqtSignal(object)

    def __init__(self, window, tag: str, group: str, title: str, with_player: bool = True):
        super().__init__(title)
        self.win, self.tag, self.group, self.with_player = window, tag, group, with_player
        self._roster_key = None  # (team_id, team_name, season) the Player list was loaded for

        self.team_combo = QComboBox()
        self.team_combo.setMinimumWidth(200)
        self.season_spin = _YearSpin()
        self.season_spin.setRange(1876, 2100)
        self.season_spin.setKeyboardTracking(False)
        self.season_spin.setToolTip("Typing a year needs Enter (or click elsewhere) to take effect.")
        self.player_combo = QComboBox()
        self.player_combo.setMinimumWidth(240)
        self.player_combo.setEnabled(False)

        form = QFormLayout(self)
        form.addRow("Team", self.team_combo)
        form.addRow("Season", self.season_spin)
        if with_player:
            form.addRow("Player", self.player_combo)

        self._debounce = QTimer(self, singleShot=True, interval=250)
        self._debounce.timeout.connect(self._season_settled)
        self.season_spin.valueChanged.connect(lambda _: self._debounce.start())
        self.team_combo.currentIndexChanged.connect(lambda _: self._load_roster())
        self.player_combo.currentIndexChanged.connect(lambda _: self.changed.emit())

    def set_teams(self, teams: list[dict], season: int, start: int | None = None):
        """Fill the team list, allow seasons up to ``season`` and start at ``start`` (default: the same)
        -- called once teams are known."""
        self.team_combo.blockSignals(True)
        self.team_combo.clear()
        for t in teams:
            self.team_combo.addItem(t["name"], t["id"])
        self.team_combo.blockSignals(False)
        self.season_spin.blockSignals(True)
        self.season_spin.setRange(1876, season)
        self.season_spin.setValue(start or season)
        self.season_spin.blockSignals(False)
        self._season_settled()

    def selection(self) -> dict | None:
        """The chosen player and the team-season the Player list was loaded
        for (not whatever the spin box says right now -- it may be mid-change)."""
        pid = self.player_combo.currentData()
        if pid is None or self._roster_key is None:
            return None
        team_id, team_name, season = self._roster_key
        return {"pid": pid, "name": self.player_combo.currentData(Qt.ItemDataRole.UserRole + 1),
                "team_id": team_id, "team_name": team_name, "season": season}

    def team_season(self) -> tuple | None:
        """(team_id, team_name, season) as the boxes show them now -- not the roster last loaded, which
        lags a season just typed in (it loads a quarter-second later, in the background), so pressing a
        button straight after typing a year would otherwise get the old one."""
        team_id = self.team_combo.currentData()
        return None if team_id is None else (team_id, self.team_combo.currentText(), self.season_spin.value())

    # ---- loading -------------------------------------------------------
    def _season_settled(self):
        season = self.season_spin.value()
        cached = self.win._season_names.get(season)
        if cached is not None:
            self._relabel_teams(cached)
            self._load_roster()
            return
        ttl = LIVE_TTL if season >= self.win._season_now else None

        def fn():
            return season, mlbstats.get_team_map(season, ttl)

        self.win._run_latest(f"{self.tag}_teams", fn, self._on_teams_loaded, self._on_failed)

    def _on_teams_loaded(self, data):
        season, names = data
        self.win._season_names[season] = names
        if season == self.season_spin.value():  # the spin box may have moved on already
            self._relabel_teams(names)
            self._load_roster()

    def _relabel_teams(self, names: dict):
        """List the teams as they stood in the selected season, keeping the
        current franchise selected if it existed then (otherwise the first
        team -- a franchise that didn't exist that season has no roster to
        pick from, so leaving it selected would just be a dead end)."""
        if not names:
            return
        keep_id = self.team_combo.currentData()
        teams = sorted(names.values(), key=lambda t: t["name"])
        self.team_combo.blockSignals(True)
        self.team_combo.clear()
        target = 0
        for i, t in enumerate(teams):
            self.team_combo.addItem(t["name"], t["id"])
            if t["id"] == keep_id:
                target = i
        self.team_combo.setCurrentIndex(target)
        self.team_combo.blockSignals(False)

    def _load_roster(self):
        team_id = self.team_combo.currentData()
        if team_id is None:
            return
        season = self.season_spin.value()
        team_name = self.team_combo.currentText()
        ttl = LIVE_TTL if season >= self.win._season_now else None
        group = self.group

        self._roster_key = None
        self.player_combo.setEnabled(False)

        def fn():
            if group == "fielding":
                return team_id, team_name, season, mlbstats.get_player_stats(
                    team_id, season, "fielding", ttl)
            if group == "all":
                by_id: dict = {}
                position: dict = {}  # a player's busiest fielding position
                for g in ("hitting", "pitching", "fielding"):
                    for r in mlbstats.get_player_stats(team_id, season, g, ttl):
                        if r.get("_id") is None:
                            continue
                        by_id.setdefault(r["_id"], r)
                        if g == "fielding" and r.get("_pos"):
                            position.setdefault(r["_id"], r["_pos"])
                rows = [dict(r, _pos=position.get(pid, "")) for pid, r in by_id.items()]
                return team_id, team_name, season, rows
            rows = mlbstats.get_player_stats(team_id, season, group, ttl)
            # A batter needs plate appearances and a pitcher batters faced --
            # otherwise the other side's players (a pitcher's empty batting
            # line, say) would crowd the list.
            weight = "plateAppearances" if group == "hitting" else "battersFaced"
            rows = [r for r in rows if (mlbstats.as_number(r.get(weight)) or 0) > 0]
            return team_id, team_name, season, rows

        self.win._run_latest(f"{self.tag}_roster", fn, self._on_roster_loaded, self._on_failed)

    def _on_roster_loaded(self, data):
        team_id, team_name, season, rows = data
        self._roster_key = (team_id, team_name, season)
        if not self.with_player:
            self.roster_loaded.emit(data)
            return
        self.player_combo.blockSignals(True)
        self.player_combo.clear()
        if self.group == "all" and rows:
            self.player_combo.addItem("(choose a player\u2026)", None)
        for row in sorted(rows, key=lambda r: r.get("_name", "")):
            if self.group == "hitting":
                extra = f"{int(mlbstats.as_number(row.get('plateAppearances')) or 0)} PA"
            elif self.group == "pitching":
                extra = f"{row.get('inningsPitched', '?')} IP"
            else:
                extra = row.get("_pos") or "no position"
            self.player_combo.addItem(f"{row.get('_name', '?')}  ({extra})", row.get("_id"))
            self.player_combo.setItemData(self.player_combo.count() - 1, row.get("_name", "?"),
                                          Qt.ItemDataRole.UserRole + 1)
        if not rows:
            self.player_combo.addItem("(none found for this team and season)", None)
        self.player_combo.setEnabled(bool(rows))
        self.player_combo.blockSignals(False)
        self.changed.emit()

    def _on_failed(self, message: str):
        self._roster_key = None
        if not self.with_player:
            self.win._say(self.tag.split("_")[0], f"{self.title()}: {message}")
            self.roster_loaded.emit(None)
            return
        self.player_combo.blockSignals(True)
        self.player_combo.clear()
        self.player_combo.addItem("(couldn't load this team and season)", None)
        self.player_combo.setEnabled(False)
        self.player_combo.blockSignals(False)
        self.win._say(self.tag.split("_")[0], f"{self.title()}: {message}")
        self.changed.emit()


# --------------------------------------------------------------------------- #
#  Numeric-aware sorting                                                      #
# --------------------------------------------------------------------------- #
class _NumProxy(QSortFilterProxyModel):
    def lessThan(self, left, right):
        a = self.sourceModel().data(left, Qt.ItemDataRole.UserRole)
        b = self.sourceModel().data(right, Qt.ItemDataRole.UserRole)
        a_num, b_num = isinstance(a, (int, float)), isinstance(b, (int, float))
        if a_num and b_num:
            return a < b
        if a_num != b_num:
            return b_num  # push blanks / text below numbers when ascending
        return str(a) < str(b)


# --------------------------------------------------------------------------- #
#  Main window                                                               #
# --------------------------------------------------------------------------- #
def _ref_value(text: str):
    """A reference line's label read as a number (".400" -> 0.4), or None."""
    return mlbstats.as_number(text.strip())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BallSheet")
        self.resize(1040, 640)

        self.pool = QThreadPool.globalInstance()
        self._live_jobs: set = set()   # jobs on the pool, kept until they've finished (see _Held)
        self._job_release = _JobRelease(self)
        self._job_release.done.connect(self._live_jobs.discard)
        # every download (Retrosheet's seasons, MLB's Stats API) is said on the status bar while it lasts
        self._downloads: dict = {}  # what -> [running, bytes so far, size or -1]
        self._download_note = _DownloadNote(self)
        self._download_note.note.connect(self._on_download)
        self._download_hook = lambda what, got, total, done: self._download_note.note.emit(
            what, got, -1 if total is None else total, done)
        retrosheet.DOWNLOAD_HOOKS.append(self._download_hook)
        self._token = 0
        self._teams: list[dict] = []            # canonical modern franchise list
        self._team_by_id: dict[int, dict] = {}  # franchise id -> modern record
        self._season_names: dict[int, dict] = {}  # season -> {id: era record}
        self._era_record: dict | None = None    # selected team's name for the shown season
        self._season_now = 0
        self._suspend = False
        self._export_job = None
        self._csv_headers: list[str] = []
        self._csv_rows: list[list[str]] = []
        self._reg_job = None
        # [(level, name, formula), ...] -- defined on the Custom tab, consumed there and by
        # the Regression/Factor Analysis tabs' dependent-variable/predictor lists.
        self._custom_stats: list[tuple[str, str, str]] = []
        self._custom_predictor_items: list[QListWidgetItem] = []  # the reg_list's "-- Custom --" section
        self._custom_pending_jump: str | None = None  # a stat name to select once its table reload lands
        self._custom_job = None
        self._custom_reload_pending = False  # a reload was asked for while another was running
        self._corr_job = None
        self._corr_predictor_items: list[QListWidgetItem] = []  # the corr_list's "-- Custom --" section
        self._tag_tokens: dict[str, int] = {}  # see _run_latest
        self._tab_keys = ["gd", "sc", "box", "wpa", "stats", "ld", "ts", "personal", "pscore", "daily", "h2h", "custom", "corr", "reg", "fa", "prod", "pbi", "prev", "hs", "range", "nz", "speed", "so", "st", "dn", "sd", "il", "up", "sim", "ps", "pst", "info", "ig"]
        self._tab_status: dict[str, str] = {}  # each tab's own status-bar line, see _say
        self.tabs = None
        self._fa_job = None
        self._fa_result = None          # last FactorResult, so a label edit can re-render it
        self._fa_sample_desc = ""
        self._fa_labels: list[str] = []  # one per current factor, typed in the "Name factors…" dialog
        self._fa_scored_rows: list[tuple[str, list[float]]] = []  # (player label, [score, ...])

        # --- controls -----------------------------------------------------
        self.team_combo = QComboBox()
        self.team_combo.setMinimumWidth(240)

        self.season_spin = _YearSpin()
        self.season_spin.setRange(1876, 2100)
        self.season_spin.setKeyboardTracking(False)
        self.season_spin.setToolTip("Previous/next season: Ctrl+Left / Ctrl+Right")

        self.view_combo = QComboBox()
        self.view_combo.addItems(mlbstats.PLAYER_VIEWS + ["Team totals"])

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Ignore the cache and pull this view again")
        self.csv_btn = QPushButton("Export CSV…")
        self.book_btn = QPushButton("Export workbook…")
        self.book_btn.setToolTip("Every season of this team, one sheet per year")

        bar = QHBoxLayout()
        for w in (QLabel("Team"), self.team_combo, QLabel("  Season"), self.season_spin,
                  QLabel("  View"), self.view_combo):
            bar.addWidget(w)
        bar.addStretch(1)
        for w in (self.refresh_btn, self.csv_btn, self.book_btn):
            bar.addWidget(w)

        # --- table ------------------------------------------------------
        self.model = QStandardItemModel(self)
        self.proxy = _NumProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setHighlightSections(False)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addLayout(bar)
        lay.addWidget(self.table)

        tabs = QTabWidget()
        tabs.setTabBar(_TintedTabBar())
        tabs.addTab(central, "Stats")
        tabs.addTab(self._build_leaders_tab(), "Leaders")
        tabs.addTab(self._build_teamstats_tab(), "Teamstats")
        tabs.addTab(self._build_personal_tab(), "Personal")
        tabs.addTab(self._build_player_scores_tab(), "Player Scores")
        tabs.addTab(self._build_dailies_tab(), "Dailies")
        tabs.addTab(self._build_h2h_tab(), "Head-To-Head")
        tabs.addTab(self._build_custom_tab(), "Custom")
        tabs.addTab(self._build_correlation_tab(), "Correlation")
        tabs.addTab(self._build_regression_tab(), "Regression")
        tabs.addTab(self._build_factor_tab(), "Factor Analysis")
        tabs.addTab(self._build_production_tab(), "Production")
        tabs.addTab(self._build_pbi_tab(), "Players Batted In")
        tabs.addTab(self._build_prevention_tab(), "Prevention")
        tabs.addTab(self._build_holds_saves_tab(), "Bullpen Support")
        tabs.addTab(self._build_range_tab(), "Range")
        tabs.addTab(self._build_naturalzone_tab(), "Naturalzone")
        tabs.addTab(self._build_speed_tab(), "Speed")
        tabs.addTab(self._build_safe_out_tab(), "Safe/Out")
        uptodate_page = self._build_uptodate_tab()  # built where it always was (after Safe/Out), shown later
        simulator_page = self._build_simulator_tab()  # built where it always was (after Safe/Out), shown later
        tabs.addTab(self._build_standings_tab(), "Standings")
        tabs.addTab(self._build_denied_tab(), "Denied")
        tabs.addTab(self._build_stadiums_tab(), "Stadiums")
        tabs.addTab(self._build_interleague_tab(), "Intergroup")
        tabs.addTab(uptodate_page, "Uptodate")  # the last of the green tabs
        tabs.addTab(simulator_page, "Simulator")
        tabs.addTab(self._build_postseason_tab(), "Postseason")
        tabs.addTab(self._build_poststats_tab(), "Poststats")
        tabs.insertTab(0, self._build_gameday_tab(), "Gameday")  # built in its old place, shown first
        tabs.insertTab(1, self._build_statcast_tab(), "Statcast")  # all three follow the Gameday game
        tabs.insertTab(2, self._build_boxscore_tab(), "Boxscore")
        tabs.insertTab(3, self._build_wpa_tab(), "WPA")
        tabs.addTab(self._build_info_tab(), "Info")
        tabs.addTab(self._build_infograph_tab(), "Infograph")
        # each page scrolls when the window is shorter (or narrower) than it can go: otherwise the tallest
        # page sets the window's least height, and on a short screen (1280x615) a maximised window ran off
        # the bottom of it
        for i in range(tabs.count()):
            page, text = tabs.widget(i), tabs.tabText(i)
            tabs.removeTab(i)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(page)
            tabs.insertTab(i, scroll, text)
        for i in range(self._tab_keys.index("stats"), self._tab_keys.index("custom") + 1):  # Stats through Custom
            tabs.tabBar().setTabData(i, QColor("#f6c1c1"))
        for key in ("corr", "reg", "fa"):  # Correlation through Factor Analysis
            tabs.tabBar().setTabData(self._tab_keys.index(key), QColor("#f9d7b0"))
        for i in range(self._tab_keys.index("prod"), self._tab_keys.index("so") + 1):  # Production through Safe/Out
            tabs.tabBar().setTabData(i, QColor("#fbf0a8"))
        for keys, tint in ((("st", "dn", "sd", "il", "up"), "#c8ebc4"),  # Standings .. Intergroup, Uptodate: light green
                           (("ps", "pst"), "#cdcdf2"),    # Postseason, Poststats: light indigo
                           (("info", "ig"), "#e6cdf2"),   # Info, Infograph: light violet
                           (("sim",), "#d4e4ee")):        # Simulator: light slate
            for key in keys:
                tabs.tabBar().setTabData(self._tab_keys.index(key), QColor(tint))
        tabs.setCurrentIndex(self._tab_keys.index("gd"))  # open on Gameday (inserting it kept Stats current)
        self.tabs = tabs
        tabs.currentChanged.connect(self._show_tab_status)
        tabs.currentChanged.connect(lambda i: self._tab_keys[i] in self.GD_KEYS and self._gd_tab_shown())
        tabs.currentChanged.connect(lambda i: self._tab_keys[i] == "ig" and self._ig_tab_shown())
        tabs.currentChanged.connect(lambda i: self._tab_keys[i] == "ld" and self._ld_tab_shown())
        tabs.currentChanged.connect(lambda i: self._tab_keys[i] == "up" and self._up_tab_shown())
        tabs.currentChanged.connect(lambda i: self._tab_keys[i] == "pbi" and self._pbi_update())
        tabs.currentChanged.connect(lambda i: self._tab_keys[i] == "hs" and self._hs_refresh())
        tabs.currentChanged.connect(lambda i: self._tab_keys[i] == "ts" and self._ts_tab_shown())
        tabs.currentChanged.connect(lambda i: self._tab_keys[i] == "dn" and self._dn_tab_shown())
        tabs.currentChanged.connect(lambda i: self._tab_keys[i] == "sd" and self._sd_tab_shown())
        self.setCentralWidget(tabs)
        self._say("stats", "Loading teams…")

        # --- menu -----------------------------------------------------
        m = self.menuBar().addMenu("&File")
        act_csv = QAction("Export &CSV…", self, triggered=self._export_csv)
        act_book = QAction("Export &workbook…", self, triggered=self._export_workbook)
        act_clear = QAction("Clear &cache", self, triggered=self._clear_cache)
        act_quit = QAction("&Quit", self, triggered=self.close)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        for a in (act_csv, act_book, act_clear):
            m.addAction(a)
        m.addSeparator()
        m.addAction(act_quit)
        view = self.menuBar().addMenu("&View")
        view.addAction(QAction("&Smaller text", self, triggered=lambda: self._change_text_size(-1)))
        view.addAction(QAction("&Larger text", self, triggered=lambda: self._change_text_size(1)))
        view.addAction(QAction("&Desktop text size", self, triggered=lambda: self._change_text_size(None)))

        # --- signals --------------------------------------------------
        self._debounce = QTimer(self, singleShot=True, interval=250)
        self._debounce.timeout.connect(self._reload)
        self.team_combo.currentIndexChanged.connect(self._team_changed)
        self.view_combo.currentIndexChanged.connect(lambda _: self._queue_reload())
        self.season_spin.valueChanged.connect(lambda _: self._queue_reload())
        self.refresh_btn.clicked.connect(lambda: self._reload(force=True))
        self.csv_btn.clicked.connect(self._export_csv)
        self.book_btn.clicked.connect(self._export_workbook)
        QShortcut(QKeySequence("Ctrl+Left"), self, activated=lambda: self.season_spin.stepBy(-1))
        QShortcut(QKeySequence("Ctrl+Right"), self, activated=lambda: self.season_spin.stepBy(1))

        self._set_enabled(False)
        self._load_default_custom_stats()
        self._load_default_info_queries()
        self._run(self._bootstrap, self._on_bootstrap)

    def _change_text_size(self, step):
        """View menu: one point smaller or larger, or back to the desktop's size (``None``) --
        saved, and used from the next start."""
        settings = _load_settings()
        offset = settings.get("text_offset", DEFAULT_TEXT_OFFSET)
        offset = 0 if step is None else offset + step
        desktop = QApplication.instance().property("desktop_points") or 10.0
        offset = max(offset, MIN_TEXT_POINTS - desktop)
        settings["text_offset"] = offset
        try:
            _save_settings(settings)
        except OSError as exc:
            QMessageBox.warning(self, "Text size", f"Couldn't save the setting: {exc}")
            return
        QMessageBox.information(
            self, "Text size",
            f"Text will be {desktop + offset:g} pt (the desktop's is {desktop:g} pt) "
            "the next time BallSheet starts.")

    def fit_to_screen(self):
        """Open no bigger than the screen's free area (a window taller than a short screen
        has its bottom -- status bar, notes, the ends of tables -- out of sight); if it
        can't fit at its usual size, fill the screen instead."""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.show()
            return
        free = screen.availableGeometry()
        frame = 40  # title bar and borders, which the window's own size leaves out
        if self.width() > free.width() or self.height() + frame > free.height():
            self.resize(min(self.width(), free.width()), min(self.height(), free.height() - frame))
            self.showMaximized()
        else:
            self.show()

    # ---- teamstats tab -----------------------------------------------------------
    TS_SCOPES = {  # sample -> radio label
        "league": "This season, across all teams",
        "same_league": "This season, across the team's league",
        "same_division": "This season, across the team's division",
        "history": "This team, across all its seasons",
    }

    def _build_teamstats_tab(self) -> QWidget:
        """The Stats tab's views with one row per team-season instead of per player: one season
        across all of MLB (or the chosen team's league or division), or the chosen team across
        all its seasons.  Standard and Advanced are MLB's team figures; Sabermetrics and Expected
        are rolled up from the team's players (see :func:`mlbstats._rolled_up_team_row`)."""
        self._ts_started = False  # loads on first view, not at startup
        self._ts_rows: list[dict] = []
        self._ts_headers: list[str] = []
        self._ts_table_rows: list[list] = []

        self.ts_team_combo = QComboBox()
        self.ts_team_combo.setMinimumWidth(240)
        self.ts_team_combo.currentIndexChanged.connect(lambda _i: self._ts_queue())
        self.ts_season_spin = _YearSpin()
        self.ts_season_spin.setRange(1876, 2100)
        self.ts_season_spin.setKeyboardTracking(False)
        self.ts_season_spin.setToolTip("Typing a year needs Enter (or click elsewhere) to take effect.")
        self.ts_season_spin.valueChanged.connect(lambda _v: self._ts_season_debounce.start())
        self._ts_season_debounce = QTimer(self, singleShot=True, interval=250)
        self._ts_season_debounce.timeout.connect(self._ts_season_settled)
        season_prev, season_next = QPushButton("\u25c0"), QPushButton("\u25b6")
        for btn, step in ((season_prev, -1), (season_next, 1)):
            btn.setFixedWidth(32)
            btn.clicked.connect(lambda _c, d=step: self.ts_season_spin.stepBy(d))
        self.ts_view_combo = QComboBox()
        self.ts_view_combo.addItems(mlbstats.TEAM_STAT_VIEWS)
        self.ts_view_combo.currentIndexChanged.connect(lambda _i: self._ts_queue())
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Pull the figures again, ignoring the cache.")
        refresh.clicked.connect(lambda: self._ts_load(force=True))
        self.ts_export_btn = QPushButton("Export\u2026")
        self.ts_export_btn.setToolTip("Save the table as an .xlsx workbook or CSV.")
        self.ts_export_btn.setEnabled(False)
        self.ts_export_btn.clicked.connect(self._ts_export)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Team"))
        bar.addWidget(self.ts_team_combo)
        bar.addSpacing(6)
        bar.addWidget(QLabel("Season"))
        bar.addWidget(season_prev)
        bar.addWidget(self.ts_season_spin)
        bar.addWidget(season_next)
        bar.addSpacing(12)
        bar.addWidget(QLabel("View"))
        bar.addWidget(self.ts_view_combo)
        bar.addStretch(1)
        bar.addWidget(refresh)
        bar.addWidget(self.ts_export_btn)

        self.ts_scope_radios = {kind: QRadioButton(text) for kind, text in self.TS_SCOPES.items()}
        self.ts_scope_radios["league"].setChecked(True)
        scope_group = QButtonGroup(self)
        scope = QHBoxLayout()
        scope.addWidget(QLabel("Teams:"))
        for button in self.ts_scope_radios.values():
            scope_group.addButton(button)
            scope.addWidget(button)
        scope.addStretch(1)
        scope_group.buttonClicked.connect(lambda _b: self._ts_queue())

        self.ts_title = QLabel()
        bold = self.ts_title.font()
        bold.setBold(True)
        self.ts_title.setFont(bold)

        self.ts_model = QStandardItemModel(self)
        self.ts_proxy = _NumProxy(self)
        self.ts_proxy.setSourceModel(self.ts_model)
        self.ts_table = QTableView()
        self.ts_table.setModel(self.ts_proxy)
        self.ts_table.setSortingEnabled(True)
        self.ts_table.setAlternatingRowColors(True)
        self.ts_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.ts_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.ts_table.verticalHeader().setVisible(False)
        self.ts_table.horizontalHeader().setHighlightSections(False)

        note = QLabel(
            "Batting, Pitching and Fielding, and their Advanced views, are MLB's own team figures. "
            "MLB has no team figure for Sabermetrics or Expected, so those are rolled up from the "
            "team's players: WAR, RAR and the other run counts added up, rates (wOBA, wRC+, FIP, "
            "xAVG ...) weighted by playing time -- plate appearances for batters, innings for "
            "pitchers' FIP family, batters faced for pitchers' expected stats. Expected stats begin "
            "in 2015. The chosen team's row is in bold; click a heading to sort.")
        note.setWordWrap(True)
        small = note.font()
        small.setPointSizeF(small.pointSizeF() * 0.9)
        note.setFont(small)

        self._ts_debounce = QTimer(self, singleShot=True, interval=200)
        self._ts_debounce.timeout.connect(self._ts_load)

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(bar)
        outer.addLayout(scope)
        outer.addWidget(self.ts_title)
        outer.addWidget(self.ts_table, 1)
        outer.addWidget(note)
        return page

    def _ts_setup(self):
        """At bootstrap: this season's teams, starting on the Stats tab's team."""
        self.ts_season_spin.blockSignals(True)
        self.ts_season_spin.setRange(1876, self._season_now)
        self.ts_season_spin.setValue(self._season_now)
        self.ts_season_spin.blockSignals(False)
        self.ts_team_combo.blockSignals(True)
        self.ts_team_combo.clear()
        for t in self._teams:
            self.ts_team_combo.addItem(t["name"], t["id"])
        index = self.ts_team_combo.findData(self.team_combo.currentData())
        self.ts_team_combo.setCurrentIndex(max(index, 0))
        self.ts_team_combo.blockSignals(False)
        self._ts_sync_division()
        if self._current_tab_key() == "ts":
            self._ts_tab_shown()

    def _ts_tab_shown(self):
        if self.ts_team_combo.count() and not self._ts_started:
            self._ts_started = True
            self._ts_load()

    def _ts_queue(self):
        if self._ts_started:
            self._ts_debounce.start()

    def _ts_scope(self) -> str:
        return next(kind for kind, button in self.ts_scope_radios.items() if button.isChecked())

    def _ts_sync_division(self):
        """Divisions began in 1969: before then there is no such sample."""
        season = self.ts_season_spin.value()
        button = self.ts_scope_radios["same_division"]
        available = season >= mlbstats.INTERDIVISION_FIRST_SEASON
        button.setEnabled(available)
        button.setToolTip("" if available else f"Divisions began in {mlbstats.INTERDIVISION_FIRST_SEASON}.")
        if not available and button.isChecked():
            self.ts_scope_radios["league"].setChecked(True)

    def _ts_season_settled(self):
        """The season changed: list the teams as that season had them (the same franchise stays
        picked when it played then), then reload."""
        self._ts_sync_division()
        season = self.ts_season_spin.value()
        names = self._season_names.get(season)
        if names is not None:
            self._ts_apply_names(names)
            return
        ttl = LIVE_TTL if season >= self._season_now else None
        self._run_latest("ts_teams", lambda: (season, mlbstats.get_team_map(season, ttl)),
                         self._on_ts_teams, lambda m: self._on_team_refresh_failed("ts", m))

    def _on_ts_teams(self, data):
        season, names = data
        self._season_names[season] = names
        if season == self.ts_season_spin.value():
            self._ts_apply_names(names)

    def _ts_apply_names(self, names: dict):
        if names:
            keep = self.ts_team_combo.currentData()
            self.ts_team_combo.blockSignals(True)
            self.ts_team_combo.clear()
            target = 0
            for i, team in enumerate(sorted(names.values(), key=lambda t: t["name"])):
                self.ts_team_combo.addItem(team["name"], team["id"])
                if team["id"] == keep:
                    target = i
            self.ts_team_combo.setCurrentIndex(target)
            self.ts_team_combo.blockSignals(False)
        self._ts_queue()

    def _ts_load(self, force: bool = False):
        team_id = self.ts_team_combo.currentData()
        if team_id is None or not self._ts_started:
            return
        season, view, scope = self.ts_season_spin.value(), self.ts_view_combo.currentText(), self._ts_scope()
        now = self._season_now

        def ttl_for(year):
            return 0 if force else (LIVE_TTL if year >= now else None)

        if scope == "history":
            first = max(1876, (self._team_by_id.get(team_id) or {}).get("first_year") or 1876)
            self._say("ts", f"Loading {self.ts_team_combo.currentText()}, {view}, {first}\u2013{now}\u2026 "
                            "(the first time, a long history takes a little while)")

            def fn():
                rows = mlbstats.get_team_history_rows(team_id, view, range(first, now + 1), ttl_for)
                return {"scope": scope, "team_id": team_id, "season": season, "view": view, "rows": rows,
                        "names": {}}
        else:
            self._say("ts", f"Loading {season}, {view}\u2026")

            def fn():
                names = mlbstats.get_team_map(season, ttl_for(season))
                teams = mlbstats.teams_in_scope(names, scope, team_id)
                rows = mlbstats.get_league_team_rows(season, view, sorted(teams), ttl_for(season))
                return {"scope": scope, "team_id": team_id, "season": season, "view": view, "rows": rows,
                        "names": names}
        self._run_latest("ts", fn, self._on_ts_loaded, lambda m: self._say("ts", f"Teamstats: {m}"))

    def _on_ts_loaded(self, data):
        scope, view, rows, names = data["scope"], data["view"], data["rows"], data["names"]
        if names:
            self._season_names[data["season"]] = names
        team_id = data["team_id"]
        cols = mlbstats.team_view_columns(view)
        if scope == "history":
            lead = [("Season", "_season"), ("Team", "_team")]
            rows = sorted(rows, key=lambda r: r["_season"])
        else:
            for r in rows:
                rec = names.get(r["_team_id"]) or {}
                r["_league"], r["_division"] = rec.get("league") or "", rec.get("division") or ""
            lead = [("Team", "_team"), ("League", "_league")]
            if any(r["_division"] for r in rows):
                lead.append(("Division", "_division"))
            rows = sorted(rows, key=lambda r: r["_team"])
        columns = lead + cols
        text_keys = ("_team", "_league", "_division")
        self._ts_rows = rows
        self._ts_headers = [h for h, _k in columns]
        self._ts_table_rows = [[r.get(k) for _h, k in columns] for r in rows]

        self.ts_table.setSortingEnabled(False)
        self.ts_model.clear()
        self.ts_model.setHorizontalHeaderLabels(self._ts_headers)
        bold = self.ts_table.font()
        bold.setBold(True)
        for r in rows:
            items = [self._new_item(r.get(k, ""), numeric=(k not in text_keys)) for _h, k in columns]
            if scope != "history" and r["_team_id"] == team_id:
                for item in items:
                    item.setFont(bold)
            self.ts_model.appendRow(items)
        self.ts_table.setSortingEnabled(True)
        self.ts_table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)  # keep the row order above
        self.ts_table.resizeColumnsToContents()
        self.ts_export_btn.setEnabled(bool(rows))

        team = self.ts_team_combo.currentText()
        if scope == "history":
            span = f"{rows[0]['_season']}\u2013{rows[-1]['_season']}" if rows else ""
            title = f"{team}, every season {span} \u2014 {view}"
        else:
            rec = names.get(team_id) or {}
            where = {"league": "all teams", "same_league": rec.get("league") or "the team's league",
                     "same_division": rec.get("division") or "the team's division"}[scope]
            title = f"{data['season']}, {where} \u2014 {view}"
        self.ts_title.setText(title)
        if not rows:
            why = (" (expected stats begin in 2015)" if "Expected" in view else "")
            self._say("ts", f"Teamstats: no {view} figures here{why}")
        else:
            self._say("ts", f"Teamstats: {len(rows)} team-season{'s' * (len(rows) != 1)} \u00b7 {title}")

    def _ts_export(self):
        if not self._ts_table_rows:
            return
        name = self.ts_title.text().replace(" \u2014 ", "_").replace(", ", "_").replace(" ", "-")
        name = re.sub(r"[^\w.\-]+", "", name) or "teamstats"
        path, chosen = QFileDialog.getSaveFileName(
            self, "Export Teamstats", os.path.join(HOME, f"teamstats_{name}.xlsx"),
            "Excel workbook (*.xlsx);;CSV (*.csv)")
        if not path:
            return
        as_csv = path.lower().endswith(".csv") or (chosen.startswith("CSV") and not path.lower().endswith(".xlsx"))
        if as_csv and not path.lower().endswith(".csv"):
            path += ".csv"
        elif not as_csv and not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        # the table's order as sorted on screen
        order = [self.ts_proxy.mapToSource(self.ts_proxy.index(i, 0)).row() for i in range(self.ts_proxy.rowCount())]
        rows = [[mlbstats.as_number(v) if mlbstats.as_number(v) is not None else ("" if v is None else v)
                 for v in self._ts_table_rows[i]] for i in order]
        try:
            if as_csv:
                with open(path, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(self._ts_headers)
                    writer.writerows(rows)
            else:
                book = Workbook()
                sheet = book.add_sheet(self.ts_view_combo.currentText()[:31])
                sheet.append(self._ts_headers, header=True)
                for row in rows:
                    sheet.append(row)
                book.save(path)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self._say("ts", f"Saved {path}")

    # ---- personal tab -----------------------------------------------------------
    def _build_personal_tab(self) -> QWidget:
        """One player's whole career, in every role he played -- Batting, Running,
        Pitching and Fielding: year-by-year lines plus a career line, in the
        Standard set and (where the Stats tab has them) the Advanced,
        Sabermetrics and Expected sets.  Find the player by name (any era), or
        browse a team's roster for a season; either way it's the player's full
        career that's shown."""
        self.personal_search = QLineEdit()
        self.personal_search.setPlaceholderText("e.g. Ohtani, or Vladimir Guerrero")
        self.personal_search.returnPressed.connect(self._personal_find)
        self.personal_find_btn = QPushButton("Find")
        self.personal_find_btn.clicked.connect(self._personal_find)
        self.personal_matches = QComboBox()
        self.personal_matches.setMinimumWidth(300)
        self.personal_matches.setEnabled(False)
        self.personal_matches.activated.connect(self._on_personal_match_chosen)

        name_row = QHBoxLayout()
        name_row.addWidget(self.personal_search, 1)
        name_row.addWidget(self.personal_find_btn)
        find_box = QGroupBox("Find a player by name")
        find_form = QFormLayout(find_box)
        find_form.addRow("Name", name_row)
        find_form.addRow("Matches", self.personal_matches)

        self.personal_picker = _PlayerPicker(self, "personal_pick", "all", "Or browse a team\u2019s roster")
        self.personal_picker.changed.connect(self._on_personal_picked)

        pickers = QHBoxLayout()
        pickers.addWidget(find_box)
        pickers.addWidget(self.personal_picker)

        self.personal_bio = QLabel("Find a player by name, or pick one from a roster.")
        self.personal_bio.setWordWrap(True)
        self.personal_bio.setTextFormat(Qt.TextFormat.RichText)

        # One sub-tab per role.  Each has a "Stat set" chooser (Standard / Advanced /
        # Sabermetrics / Expected where the role has them); the Standard set comes with
        # the player, the others are fetched when first chosen and then kept.
        self.personal_tabs = QTabWidget()
        self.personal_models: dict[str, QStandardItemModel] = {}
        self.personal_tables: dict[str, QTableView] = {}
        self.personal_notes: dict[str, QLabel] = {}
        self.personal_set_combos: dict[str, QComboBox] = {}
        for title, role in mlbstats.PERSON_ROLES:
            model = QStandardItemModel(self)
            table = QTableView()
            table.setModel(model)
            table.setAlternatingRowColors(True)
            table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
            table.verticalHeader().setVisible(False)
            table.horizontalHeader().setHighlightSections(False)
            sets = mlbstats.PERSON_SETS[role]
            combo = QComboBox()
            combo.addItems(sets)
            combo.currentIndexChanged.connect(lambda _i, r=role: self._refresh_person_role(r))
            set_row = QHBoxLayout()
            set_label = QLabel("Stat set")
            set_row.addWidget(set_label)
            set_row.addWidget(combo)
            set_row.addStretch(1)
            if len(sets) == 1:  # nothing to choose between
                set_label.hide()
                combo.hide()
            note = QLabel()
            note.setWordWrap(True)
            note.hide()
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.addLayout(set_row)
            lay.addWidget(note)
            lay.addWidget(table, 1)
            self.personal_tabs.addTab(page, title)
            self.personal_models[role], self.personal_tables[role] = model, table
            self.personal_notes[role] = note
            self.personal_set_combos[role] = combo
        self.personal_tabs.setTabToolTip(
            1, "Base-running: stolen bases and caught stealing, plus the Sabermetrics "
               "running metrics (BsR, UBR, wSB, wGDP, Spd)")
        # Until the user clicks a sub-tab themselves, each newly loaded player opens on
        # their main role; after that their own choice sticks (handy for comparing,
        # say, everyone's fielding).  Programmatic tab changes don't emit this signal.
        self._personal_tab_pinned = False
        self.personal_tabs.tabBarClicked.connect(lambda _i: setattr(self, "_personal_tab_pinned", True))
        self.personal_tabs.currentChanged.connect(lambda i: self._refresh_person_role(self._person_role(i)))

        # The player currently shown, and what's been fetched for them.
        self._personal_pid = None
        self._personal_name = ""
        self._personal_stats: dict[str, dict] = {}      # API group -> Standard year-by-year + career
        self._personal_cache: dict[tuple, dict] = {}    # (player id, API group, source) -> data

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(pickers)
        outer.addWidget(self.personal_bio)
        outer.addWidget(self.personal_tabs, 1)
        return page

    @staticmethod
    def _person_role(tab_index: int) -> str:
        return mlbstats.PERSON_ROLES[tab_index][1]

    def _personal_find(self):
        text = self.personal_search.text().strip()
        if len(text) < 2:
            self._say("personal", "Type at least two letters of a player's name.")
            return
        self.personal_find_btn.setEnabled(False)
        self._say("personal", f"Searching for \u201c{text}\u201d\u2026")

        def fn():
            return text, mlbstats.search_people(text)

        self._run_latest("personal_search", fn, self._on_personal_matches, self._on_personal_error)

    def _on_personal_matches(self, data):
        text, people = data
        self.personal_find_btn.setEnabled(True)
        if self._fill_player_matches(self.personal_matches, people, "personal", text):
            self._on_personal_match_chosen(1)

    def _fill_player_matches(self, combo: QComboBox, people: list[dict], key: str, text: str) -> bool:
        """List a name search's ``people`` in ``combo`` (a nickname that found one, in quotes), with
        the tab ``key``'s status line; -> True when there was just one, now chosen."""
        combo.blockSignals(True)
        combo.clear()
        if not people:
            combo.addItem("(no players found)", None)
            combo.setEnabled(False)
            combo.blockSignals(False)
            self._say(key, f"No players found for \u201c{text}\u201d.")
            return False
        combo.addItem(f"({len(people)} match{'es' if len(people) != 1 else ''} \u2014 choose one)", None)
        for p in people:
            if p["debut"]:
                detail = f"{p['position']}, debut {p['debut'][:4]}"
            else:
                detail = f"{p['position']}, no MLB games"
            if p["birth"]:
                detail += f", b. {p['birth'][:4]}"
            aka = f" \u201c{p['aka']}\u201d" if p.get("aka") and p["aka"] != p["name"] else ""
            combo.addItem(f"{p['name']}{aka}  ({detail})", p["id"])
            combo.setItemData(combo.count() - 1, p["name"], Qt.ItemDataRole.UserRole + 1)
        combo.setEnabled(True)
        combo.blockSignals(False)
        self._say(key, f"{len(people)} player(s) match \u201c{text}\u201d.")
        if len(people) == 1:
            combo.setCurrentIndex(1)
            return True
        return False

    def _on_personal_match_chosen(self, index: int):
        pid = self.personal_matches.itemData(index)
        if pid is not None:
            self._load_personal(pid, self.personal_matches.itemData(index, Qt.ItemDataRole.UserRole + 1))

    def _on_personal_picked(self):
        sel = self.personal_picker.selection()
        if sel is not None:
            self._load_personal(sel["pid"], sel["name"])

    def _load_personal(self, pid: int, name: str):
        self.personal_bio.setText(f"Loading {html.escape(str(name))}\u2026")

        def fn():
            # The current season is in these responses, so always use the live TTL.
            person = mlbstats.get_person(pid, LIVE_TTL)
            stats = {group: mlbstats.get_person_stats(pid, group, LIVE_TTL)
                     for group in ("hitting", "pitching", "fielding")}
            return pid, person, stats

        self._run_latest("personal_load", fn, self._on_personal_loaded, self._on_personal_error)

    def _on_personal_error(self, message: str):
        self.personal_find_btn.setEnabled(True)
        self._say("personal", f"Personal: {message}")
        self.personal_bio.setText(f"Couldn't load that: {html.escape(message)}")

    def _on_personal_loaded(self, data):
        pid, person, stats = data
        name = person.get("name", "?")
        self._personal_pid, self._personal_name, self._personal_stats = pid, name, stats

        facts = [person.get("position")]
        if person.get("bats") or person.get("throws"):
            facts.append(f"Bats {person.get('bats') or '?'} / Throws {person.get('throws') or '?'}")
        size = ", ".join(x for x in (person.get("height"),
                                     f"{person['weight']} lb" if person.get("weight") else "") if x)
        facts.append(size)
        if person.get("birth_date"):
            born = f"Born {person['birth_date']}"
            facts.append(f"{born} ({person['birth_place']})" if person.get("birth_place") else born)
        if person.get("debut"):
            facts.append(f"MLB debut {person['debut']}")
        facts.append("active" if person.get("active") else "retired")
        self.personal_bio.setText(
            f"<b>{html.escape(name)}</b>   \u00b7   "
            + "   \u00b7   ".join(html.escape(str(f)) for f in facts if f))

        # Which roles this player has any Standard rows for (Running comes from the
        # hitting data).  Jump to one that has something -- the pitcher's own first
        # -- unless the user has picked a sub-tab themselves and it has rows.
        has = {role: bool(stats[mlbstats.PERSON_API_GROUP[role]]["season"]
                          or stats[mlbstats.PERSON_API_GROUP[role]]["career"])
               for _t, role in mlbstats.PERSON_ROLES}
        roles = [role for _t, role in mlbstats.PERSON_ROLES]
        current = roles[self.personal_tabs.currentIndex()]
        if not self._personal_tab_pinned or not has[current]:
            order = (["pitching", "hitting", "running", "fielding"] if person.get("position") == "P"
                     else ["hitting", "pitching", "running", "fielding"])
            first = next((r for r in order if has[r]), None)
            if first is not None:
                self.personal_tabs.setCurrentIndex(roles.index(first))  # (this refreshes it)
        current = roles[self.personal_tabs.currentIndex()]
        # Every role showing its Standard set fills at once; one on a set that needs a
        # fetch waits until its tab is visible.
        for role in roles:
            if role == current or self.personal_set_combos[role].currentText() == "Standard":
                self._refresh_person_role(role)
            else:
                self.personal_models[role].clear()
        seasons = sorted({r["_season"] for g in stats.values() for r in g["season"]})
        self._say("personal", 
            f"{name} \u00b7 " + (f"{len(seasons)} MLB season(s), {seasons[0]}\u2013{seasons[-1]}"
                                  if seasons else "no MLB stats"))

    # Which fetch backs each Stat set (None = it came with the player).
    _PERSON_SOURCE = {"Standard": None, "Advanced": "advanced",
                      "Sabermetrics": "sabermetrics", "Expected": "expectedStatistics"}

    def _refresh_person_role(self, role: str):
        """Show one role's chosen Stat set for the current player, fetching it first
        if it isn't in hand (only ever for the visible sub-tab)."""
        pid = self._personal_pid
        if pid is None:
            return
        set_name = self.personal_set_combos[role].currentText()
        group = mlbstats.PERSON_API_GROUP[role]
        source = self._PERSON_SOURCE[set_name]
        data = self._personal_stats[group] if source is None else self._personal_cache.get((pid, group, source))
        if data is not None:
            self._fill_person_table(role, set_name, data)
            return
        if role != self._person_role(self.personal_tabs.currentIndex()):
            return
        self.personal_models[role].clear()
        note = self.personal_notes[role]
        note.setText(f"Loading {set_name}\u2026" if source == "advanced"
                     else f"Loading {set_name} \u2014 one request per season, so this takes a moment "
                          "the first time\u2026")
        note.show()
        standard, now = self._personal_stats[group], self._season_now

        def fn():
            if source == "advanced":
                return mlbstats.get_person_stats(pid, group, LIVE_TTL, advanced=True)
            seasons = sorted({r["_season"] for r in standard["season"]})
            if source == "expectedStatistics":
                seasons = [s for s in seasons if int(s) >= mlbstats.EXPECTED_FROM_SEASON]
            # A finished season never changes: cache it for good.
            return mlbstats.get_person_season_stats(
                pid, group, source, seasons, lambda season: LIVE_TTL if season >= now else None)

        self._run_latest(f"personal_set_{role}", fn,
                         lambda result: self._on_person_set_loaded(pid, group, source, result),
                         lambda message: self._on_person_set_failed(role, set_name, message))

    def _on_person_set_loaded(self, pid: int, group: str, source: str, data: dict):
        self._personal_cache[(pid, group, source)] = data
        if pid != self._personal_pid:  # the user moved on to another player meanwhile
            return
        current = self._person_role(self.personal_tabs.currentIndex())
        for _title, role in mlbstats.PERSON_ROLES:
            if role == current and mlbstats.PERSON_API_GROUP[role] == group:
                self._refresh_person_role(role)

    def _on_person_set_failed(self, role: str, set_name: str, message: str):
        note = self.personal_notes[role]
        note.setText(f"Couldn't load {set_name}: {message}")
        note.show()
        self._say("personal", f"Personal: {message}")

    def _fill_person_table(self, role: str, set_name: str, data: dict):
        """Show one role's chosen set: year-by-year rows, then the career line where
        there is one -- or say there's nothing.  Team-total and career rows are
        bold."""
        title = dict((r, t) for t, r in mlbstats.PERSON_ROLES)[role]
        name = self._personal_name
        rows = data["season"] + data["career"]
        columns = mlbstats.PERSON_SET_COLUMNS[(role, set_name)]
        model, table, note = self.personal_models[role], self.personal_tables[role], self.personal_notes[role]
        model.clear()
        model.setHorizontalHeaderLabels([h for h, _ in columns])
        if not rows:
            if set_name == "Expected":
                note.setText(f"{name} has no Expected stats \u2014 they're Statcast-based and only "
                             f"exist from {mlbstats.EXPECTED_FROM_SEASON} on.")
            else:
                what = {"Standard": f"{title.lower()} stats", "Advanced": f"advanced {title.lower()} stats",
                        "Sabermetrics": f"Sabermetrics {title.lower()} numbers"}[set_name]
                note.setText(f"{name} has no {what} in the majors.")
            note.show()
            return
        if set_name in ("Sabermetrics", "Expected"):
            note.setText("Season by season only \u2014 the source has no career line for this set.")
            note.show()
        else:
            note.hide()
        text_keys = ("_season", "_team", "_pos")
        for row in rows:
            items = [self._new_item(row.get(key, ""), numeric=key not in text_keys)
                     for _h, key in columns]
            if row["_kind"] != "season":
                for item in items:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
            model.appendRow(items)
        table.resizeColumnsToContents()

    # ---- dailies tab --------------------------------------------------------
    # ---- player scores tab -------------------------------------------------------
    SCORE_STATS = ("Production Score", "Prevention Score", "Range Score", "Speed Score", "Overall Score")
    CAREER_MIN_SEASONS = 3  # seasons with a score needed for its Career average

    def _build_player_scores_tab(self) -> QWidget:
        """Personal's companion: one player's five Score stats (the Custom tab's player-level stats of
        those names) for every season he played -- one line per team, and a whole-season line for a
        season split between teams.  Find him by name or from a roster, as on Personal."""
        self.pscore_search = QLineEdit()
        self.pscore_search.setPlaceholderText("e.g. Ohtani, or Vladimir Guerrero")
        self.pscore_search.returnPressed.connect(self._pscore_find)
        self.pscore_find_btn = QPushButton("Find")
        self.pscore_find_btn.clicked.connect(self._pscore_find)
        self.pscore_matches = QComboBox()
        self.pscore_matches.setMinimumWidth(300)
        self.pscore_matches.setEnabled(False)
        self.pscore_matches.activated.connect(self._on_pscore_match_chosen)
        name_row = QHBoxLayout()
        name_row.addWidget(self.pscore_search, 1)
        name_row.addWidget(self.pscore_find_btn)
        find_box = QGroupBox("Find a player by name")
        find_form = QFormLayout(find_box)
        find_form.addRow("Name", name_row)
        find_form.addRow("Matches", self.pscore_matches)
        self.pscore_picker = _PlayerPicker(self, "pscore_pick", "all", "Or browse a team\u2019s roster")
        self.pscore_picker.changed.connect(self._on_pscore_picked)
        pickers = QHBoxLayout()
        pickers.addWidget(find_box)
        pickers.addWidget(self.pscore_picker)

        self.pscore_bio = QLabel("Find a player by name, or pick one from a roster.")
        self.pscore_bio.setWordWrap(True)
        self.pscore_bio.setTextFormat(Qt.TextFormat.RichText)
        self.pscore_model = QStandardItemModel(self)
        self.pscore_table = QTableView()
        self.pscore_table.setModel(self.pscore_model)
        self.pscore_table.setAlternatingRowColors(True)
        self.pscore_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.pscore_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.pscore_table.verticalHeader().setVisible(False)
        self.pscore_table.horizontalHeader().setHighlightSections(False)
        self.pscore_note = QLabel()
        self.pscore_note.setWordWrap(True)
        # while a player is assembled: what's left to fetch, and how far it's got
        self.pscore_wait = QLabel()
        self.pscore_wait.setWordWrap(True)
        self.pscore_wait.setStyleSheet(f"color: {LIVE_COLOR};")
        self.pscore_wait.hide()
        self.pscore_progress = QProgressBar()
        self.pscore_progress.setTextVisible(True)
        self.pscore_progress.hide()
        self._pscore_steps = _StepSignal()
        self._pscore_steps.step.connect(self._on_pscore_step)
        about = QLabel(
            "Each score is a Custom tab stat: 1.00 is MLB\u2019s figure that season, green better and red worse "
            "(Prevention is MLB\u2019s runs per nine innings \u00f7 his, so above 1.00 is fewer runs allowed).  A score is in grey parentheses where he hasn\u2019t the playing "
            "time it needs with that team \u2014 3.1 plate appearances, 0.3 innings pitched, 3 fielding innings "
            "or 1 baserunning opportunity per team game \u2014 and counts in no average: the Overall Score "
            "averages the ones he qualifies for.  "
            "A season split between teams has a line for each and one for the "
            "whole season.  The Career line at the bottom averages each score over the seasons that have one "
            "(a split season once, by its whole-season line), given at least three.  The first time, a long "
            "career can take several minutes: each season needs its team\u2019s and MLB\u2019s figures and "
            "Retrosheet\u2019s play-by-play.  After this, the downloaded seasons are kept, will make this query "
            "fast in the future, and may make other queries faster.")
        about.setWordWrap(True)
        self._pscore_pid = None
        self._pscore_name = ""

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(pickers)
        outer.addWidget(self.pscore_bio)
        outer.addWidget(self.pscore_wait)
        outer.addWidget(self.pscore_progress)
        outer.addWidget(self.pscore_note)
        outer.addWidget(self.pscore_table, 1, Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(about)
        return page

    def _pscore_find(self):
        text = self.pscore_search.text().strip()
        if len(text) < 2:
            self._say("pscore", "Type at least two letters of a player's name.")
            return
        self.pscore_find_btn.setEnabled(False)
        self._say("pscore", f"Searching for \u201c{text}\u201d\u2026")
        self._run_latest("pscore_search", lambda: (text, mlbstats.search_people(text)),
                         self._on_pscore_matches, self._on_pscore_error)

    def _on_pscore_matches(self, data):
        text, people = data
        self.pscore_find_btn.setEnabled(True)
        if self._fill_player_matches(self.pscore_matches, people, "pscore", text):
            self._on_pscore_match_chosen(1)

    def _on_pscore_match_chosen(self, index: int):
        pid = self.pscore_matches.itemData(index)
        if pid is not None:
            self._load_pscore(pid, self.pscore_matches.itemData(index, Qt.ItemDataRole.UserRole + 1))

    def _on_pscore_picked(self):
        sel = self.pscore_picker.selection()
        if sel is not None:
            self._load_pscore(sel["pid"], sel["name"])

    def _pscore_definitions(self) -> tuple[list, list]:
        """The Score stats as the Custom tab has them -> ``([(name, formula), ...], [missing names])``."""
        defs = {name: ftext for lvl, name, ftext in self._custom_stats if lvl == "player"}
        return ([(n, defs[n]) for n in self.SCORE_STATS if n in defs],
                [n for n in self.SCORE_STATS if n not in defs])

    def _load_pscore(self, pid: int, name: str):
        self._pscore_pid, self._pscore_name = pid, name
        stats, _missing = self._pscore_definitions()
        if not stats:
            self.pscore_bio.setText("The Score stats aren\u2019t defined (Custom tab): load "
                                    "production-score-player.json and the others.")
            return
        self.pscore_bio.setText(f"Assembling <b>{html.escape(str(name))}</b>\u2019s scores, season by season\u2026")
        self.pscore_wait.setText("Looking up his seasons\u2026")
        self.pscore_wait.show()
        self.pscore_progress.setRange(0, 0)   # (busy until the seasons are counted)
        self.pscore_progress.show()
        self._say("pscore", f"Assembling {name}\u2019s scores\u2026")
        now = self._season_now or _dt.date.today().year
        steps = self._pscore_steps.step

        def fn():
            person = mlbstats.get_person(pid, LIVE_TTL)
            groups = {g: mlbstats.get_person_stats(pid, g, LIVE_TTL) for g in ("hitting", "pitching", "fielding")}
            stints = mlbstats.person_stints(groups)
            batted = mlbstats.person_batted(groups)
            downloads = sorted({season for season, _team in batted if not mlbstats.retro_is_cached(season)})
            # a team-season with nothing to download counts as on hand from the start, so the bar
            # opens on what's already loaded and then fills as the downloads come in
            on_hand = {(season, tid) for season, tid, _n in stints
                       if season not in downloads or (season, tid) not in batted}
            finished = set()

            def advance(_done, total, stint):
                finished.add(stint[:2])
                steps.emit((pid, len(on_hand | finished), total, stint, downloads, len(on_hand)))
            steps.emit((pid, len(on_hand), len(stints), None, downloads, len(on_hand)))
            result = mlbstats.person_custom_stats(
                pid, stints, stats, lambda season: LIVE_TTL if season >= now else None, batted=batted,
                progress=advance)
            return pid, person, result

        self._run_latest("pscore_load", fn, self._on_pscore_loaded, self._on_pscore_error)

    def _on_pscore_step(self, data):
        """Progress from the worker: the warning once the seasons are counted, then the bar."""
        pid, done, total, stint, downloads, on_hand = data
        if pid != self._pscore_pid:
            return
        seasons = "season" if total == 1 else "seasons"
        if downloads:
            minutes = max(1, round(len(downloads) * 25 / 60))
            warning = (f"This will take a while: {total} team-{seasons} to put together, and Retrosheet\u2019s "
                       f"play-by-play to download for {len(downloads)} of his seasons (4\u201310 MB each) \u2014 "
                       f"roughly {minutes} minute{'s' * (minutes != 1)}.  After this, the downloaded seasons are kept, "
                       f"will make this query fast in the future, and may make other queries faster.")
        else:
            warning = f"{total} team-{seasons} to put together \u2014 everything needed is already downloaded."
        self.pscore_wait.setText(warning)
        self.pscore_progress.setRange(0, max(1, total))
        self.pscore_progress.setValue(done)
        held = f" ({on_hand} already on hand)" if on_hand and on_hand < total else ""
        last = f" \u2014 last done: {stint[0]} {stint[2]}" if stint else ""
        self.pscore_progress.setFormat(f"%v of %m team-seasons{held}{last}")

    def _pscore_done_waiting(self):
        self.pscore_wait.hide()
        self.pscore_progress.hide()

    def _on_pscore_error(self, message: str):
        self._pscore_done_waiting()
        self.pscore_find_btn.setEnabled(True)
        self._say("pscore", f"Player Scores: {message}")
        self.pscore_bio.setText(f"Couldn't load that: {html.escape(message)}")

    def _on_pscore_loaded(self, data):
        pid, person, result = data
        if pid != self._pscore_pid:
            return
        self._pscore_done_waiting()
        name = person.get("name", self._pscore_name)
        rows = result["rows"]
        _stats, missing = self._pscore_definitions()
        facts = [person.get("position"), f"MLB debut {person['debut']}" if person.get("debut") else None,
                 "active" if person.get("active") else "retired"]
        self.pscore_bio.setText(f"<b>{html.escape(name)}</b>   \u00b7   "
                                + "   \u00b7   ".join(html.escape(str(f)) for f in facts if f))
        model = self.pscore_model
        model.clear()
        model.setHorizontalHeaderLabels(["Season", "Team"] + [n.replace(" Score", "") for n in self.SCORE_STATS])
        for col in range(2, 2 + len(self.SCORE_STATS)):
            model.setHeaderData(col, Qt.Orientation.Horizontal,
                                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                                Qt.ItemDataRole.TextAlignmentRole)
        for row in rows:
            total = row["teams"] > 1
            items = [QStandardItem(str(row["season"])), QStandardItem(row["team"])]
            for score in self.SCORE_STATS:
                value = row["values"].get(score)
                loose = row.get("unqualified", {}).get(score)  # short of the playing time: shown, not counted
                sample = row.get("sampled", {}).get(score)
                star = "*" if sample is not None else ""
                text = (f"{value:.3f}{star}" if value is not None else f"({loose:.3f}){star}" if loose is not None else "")
                item = QStandardItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                tips = []
                if loose is not None:
                    tips.append("Not enough playing time to qualify: shown for what it is, but left out of the "
                                "Overall and Career averages")
                if sample is not None:
                    tips.append(f"From Retrosheet\u2019s play-by-play of only {sample:.0%} of the team\u2019s "
                                f"innings that season: a sample, not the whole season")
                if tips:
                    item.setToolTip("\n".join(tips))
                if value is not None and value != 1:
                    item.setForeground(QBrush(QColor(WON_COLOR if value > 1 else LOST_COLOR)))
                elif loose is not None:
                    item.setForeground(QBrush(QColor("#888888")))
                if score == "Overall Score":
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                items.append(item)
            for item in items:
                item.setEditable(False)
                if total:
                    font = item.font()
                    font.setItalic(True)
                    item.setFont(font)
            model.appendRow(items)
        if rows:  # the yearly scores averaged, where there are enough
            career = mlbstats.career_scores(rows, self.SCORE_STATS, self.CAREER_MIN_SEASONS)
            first, last = rows[0]["season"], rows[-1]["season"]
            items = [QStandardItem("Career"), QStandardItem(f"{first}\u2013{last}" if first != last else str(first))]
            for score in self.SCORE_STATS:
                value, count, sampled = career[score]
                item = QStandardItem("" if value is None else f"{value:.3f}" + ("*" if sampled else ""))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                noun = "season" if count == 1 else "seasons"
                item.setToolTip((f"Average of {count} {noun} with a score"
                                 + (", some of them from a sample of the season\u2019s play-by-play" if sampled else ""))
                                if value is not None else
                                f"Only {count} {noun} with a score; a career figure needs {self.CAREER_MIN_SEASONS}")
                if value is not None and value != 1:
                    item.setForeground(QBrush(QColor(WON_COLOR if value > 1 else LOST_COLOR)))
                items.append(item)
            for item in items:
                item.setEditable(False)
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setBackground(self.palette().midlight())
            model.appendRow(items)
        self.pscore_table.resizeColumnsToContents()
        self.pscore_table.setMinimumWidth(self.pscore_table.horizontalHeader().length()
                                          + 2 * self.pscore_table.frameWidth()
                                          + self.pscore_table.verticalScrollBar().sizeHint().width() + 4)
        notes = []
        samples = sorted({(r["season"], r["team"], min(r["sampled"].values())) for r in rows
                          if r.get("sampled") and r["teams"] == 1})
        if samples:
            notes.append("* From Retrosheet\u2019s play-by-play of only part of the season \u2014 "
                         + ", ".join(f"{season} {team} {share:.0%}" for season, team, share in samples)
                         + " of the team\u2019s innings in MLB\u2019s records \u2014 so a sample of it, not the whole.")
        if missing:
            notes.append(", ".join(missing) + (" isn\u2019t" if len(missing) == 1 else " aren\u2019t")
                         + " defined on the Custom tab, so blank.")
        if result["unavailable"]:
            notes.append("Some figures couldn\u2019t be had, so their scores are blank where they\u2019re needed: "
                         + "; ".join(dict.fromkeys(result["unavailable"].values())) + ".")
        self.pscore_note.setText("  ".join(notes))
        self.pscore_note.setVisible(bool(notes))
        seasons = sorted({r["season"] for r in rows})
        self._say("pscore", f"{name} \u00b7 " + (f"{len(seasons)} season(s), {seasons[0]}\u2013{seasons[-1]}"
                                                    if seasons else "no MLB seasons"))

    def _refresh_player_scores(self):
        """The Score stats changed on the Custom tab: work the shown player's out again."""
        if getattr(self, "_pscore_pid", None) is not None:
            self._load_pscore(self._pscore_pid, self._pscore_name)

    def _build_dailies_tab(self) -> QWidget:
        """One player's chosen season game by game, in every role he played that year --
        Batting, Running, Pitching and Fielding -- with season-to-date rates and a total
        line (regular season, then postseason if asked for).  Laid out like the Personal
        tab: find the player by name (any era) or browse a team's roster, then pick one
        of his seasons.  At the Team level it's the club's own game-by-game lines instead,
        for the team and season in the roster box.  The source's game logs start in 1901."""
        self.daily_search = QLineEdit()
        self.daily_search.setPlaceholderText("e.g. Ohtani, or Vladimir Guerrero")
        self.daily_search.returnPressed.connect(self._daily_find)
        self.daily_find_btn = QPushButton("Find")
        self.daily_find_btn.clicked.connect(self._daily_find)
        self.daily_matches = QComboBox()
        self.daily_matches.setMinimumWidth(300)
        self.daily_matches.setEnabled(False)
        self.daily_matches.activated.connect(self._on_daily_match_chosen)

        name_row = QHBoxLayout()
        name_row.addWidget(self.daily_search, 1)
        name_row.addWidget(self.daily_find_btn)
        find_box = QGroupBox("Find a player by name")
        find_form = QFormLayout(find_box)
        find_form.addRow("Name", name_row)
        find_form.addRow("Matches", self.daily_matches)

        self.daily_picker = _PlayerPicker(self, "daily_pick", "all", "Or browse a team\u2019s roster")
        self.daily_picker.changed.connect(self._on_daily_picked)

        pickers = QHBoxLayout()
        pickers.addWidget(find_box)
        pickers.addWidget(self.daily_picker)

        self.daily_bio = QLabel("Find a player by name, or pick one from a roster.")
        self.daily_bio.setWordWrap(True)
        self.daily_bio.setTextFormat(Qt.TextFormat.RichText)
        # Which of the player's seasons to show -- on the bio line, so the page is no
        # taller than the Personal tab.
        self.daily_season = QComboBox()
        self.daily_season.setEnabled(False)
        self.daily_season.setToolTip(f"The player's MLB seasons (game logs exist from {mlbstats.DAILY_FROM_SEASON} on)")
        self.daily_season.currentIndexChanged.connect(lambda _i: self._load_daily_games())
        self.daily_post = QCheckBox("Include postseason")
        self.daily_post.setToolTip("Add the player's postseason games, after the regular season, with a total of their own")
        self.daily_post.toggled.connect(lambda _on: self._reload_dailies())
        # Level: the chosen player, or the whole team (the team and season in the roster box).
        self.daily_level_player = QRadioButton("Player")
        self.daily_level_player.setChecked(True)
        self.daily_level_team = QRadioButton("Team")
        self.daily_level_team.setToolTip(
            "The club's own game-by-game lines, for the team and season in \u201cOr browse a team\u2019s roster\u201d")
        level_group = QButtonGroup(self)
        for b in (self.daily_level_player, self.daily_level_team):
            level_group.addButton(b)
        self.daily_level_team.toggled.connect(lambda _on: self._on_daily_level_changed())
        self.daily_season_label = QLabel("Season")
        bio_row = QHBoxLayout()
        bio_row.addWidget(self.daily_bio, 1)
        bio_row.addWidget(QLabel("Level"))
        bio_row.addWidget(self.daily_level_player)
        bio_row.addWidget(self.daily_level_team)
        bio_row.addSpacing(8)
        bio_row.addWidget(self.daily_season_label)
        bio_row.addWidget(self.daily_season)
        bio_row.addWidget(self.daily_post)

        self.daily_tabs = QTabWidget()
        self.daily_models: dict[str, QStandardItemModel] = {}
        self.daily_tables: dict[str, QTableView] = {}
        self.daily_notes: dict[str, QLabel] = {}
        for title, role in mlbstats.PERSON_ROLES:
            model = QStandardItemModel(self)
            table = QTableView()
            table.setModel(model)
            table.setAlternatingRowColors(True)
            table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
            table.verticalHeader().setVisible(False)
            table.horizontalHeader().setHighlightSections(False)
            note = QLabel()
            note.setWordWrap(True)
            page = QWidget()
            lay = QVBoxLayout(page)
            lay.addWidget(note)
            lay.addWidget(table, 1)
            self.daily_tabs.addTab(page, title)
            self.daily_models[role], self.daily_tables[role], self.daily_notes[role] = model, table, note
        self.daily_tabs.setTabToolTip(
            1, "Base-running game by game: times on base, stolen bases, caught stealing, runs scored")
        # Same rule as Personal: each new player opens on his main role until the user
        # picks a sub-tab themselves.
        self._daily_tab_pinned = False
        self.daily_tabs.tabBarClicked.connect(lambda _i: setattr(self, "_daily_tab_pinned", True))

        self._daily_pid = None
        self._daily_name = ""
        self._daily_position = ""
        self._daily_bio_html = ""  # the player's bio line, put back when Level returns to Player

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(pickers)
        outer.addLayout(bio_row)
        outer.addWidget(self.daily_tabs, 1)
        return page

    def _daily_find(self):
        text = self.daily_search.text().strip()
        if len(text) < 2:
            self._say("daily", "Type at least two letters of a player's name.")
            return
        self.daily_find_btn.setEnabled(False)
        self._say("daily", f"Searching for \u201c{text}\u201d\u2026")

        def fn():
            return text, mlbstats.search_people(text)

        self._run_latest("daily_search", fn, self._on_daily_matches, self._on_daily_error)

    def _on_daily_matches(self, data):
        text, people = data
        self.daily_find_btn.setEnabled(True)
        combo = self.daily_matches
        combo.blockSignals(True)
        combo.clear()
        if not people:
            combo.addItem("(no players found)", None)
            combo.setEnabled(False)
            combo.blockSignals(False)
            self._say("daily", f"No players found for \u201c{text}\u201d.")
            return
        combo.addItem(f"({len(people)} match{'es' if len(people) != 1 else ''} \u2014 choose one)", None)
        for p in people:
            if p["debut"]:
                detail = f"{p['position']}, debut {p['debut'][:4]}"
            else:
                detail = f"{p['position']}, no MLB games"
            if p["birth"]:
                detail += f", b. {p['birth'][:4]}"
            aka = f" \u201c{p['aka']}\u201d" if p.get("aka") and p["aka"] != p["name"] else ""
            combo.addItem(f"{p['name']}{aka}  ({detail})", p["id"])
            combo.setItemData(combo.count() - 1, p["name"], Qt.ItemDataRole.UserRole + 1)
        combo.setEnabled(True)
        combo.blockSignals(False)
        self._say("daily", f"{len(people)} player(s) match \u201c{text}\u201d.")
        if len(people) == 1:
            combo.setCurrentIndex(1)
            self._on_daily_match_chosen(1)

    def _on_daily_match_chosen(self, index: int):
        pid = self.daily_matches.itemData(index)
        if pid is not None:
            self._set_daily_level_player()
            self._load_daily_player(pid, self.daily_matches.itemData(index, Qt.ItemDataRole.UserRole + 1))

    def _on_daily_picked(self):
        """A new roster landed or a player was chosen in the roster box.  Choosing a player
        means the Player level; a new team or season, at the Team level, is that team."""
        sel = self.daily_picker.selection()
        if sel is not None:
            self._set_daily_level_player()
            self._load_daily_player(sel["pid"], sel["name"], sel["season"])
        elif self.daily_level_team.isChecked():
            self._load_daily_team()

    def _set_daily_level_player(self):
        if self.daily_level_team.isChecked():
            self.daily_level_player.blockSignals(True)
            self.daily_level_team.blockSignals(True)
            self.daily_level_player.setChecked(True)
            self.daily_level_player.blockSignals(False)
            self.daily_level_team.blockSignals(False)
            self._show_daily_level()

    def _show_daily_level(self):
        """The player's Season box only applies at the Player level (a team's season is the
        roster box's)."""
        team = self.daily_level_team.isChecked()
        self.daily_season_label.setVisible(not team)
        self.daily_season.setVisible(not team)
        if not team:
            self.daily_bio.setText(self._daily_bio_html or "Find a player by name, or pick one from a roster.")

    def _on_daily_level_changed(self):
        self._show_daily_level()
        self._reload_dailies()

    def _reload_dailies(self):
        if self.daily_level_team.isChecked():
            self._load_daily_team()
        elif self._daily_pid is not None:
            self._load_daily_games()
        else:
            for role in self.daily_models:
                self.daily_models[role].clear()
                self.daily_notes[role].setText("")

    def _load_daily_player(self, pid: int, name: str, season: int | None = None):
        """Fetch the player's bio and which seasons he played, then show ``season`` (from a
        roster browse) or else his latest one."""
        self._daily_bio_html = f"Loading {html.escape(str(name))}\u2026"
        self.daily_bio.setText(self._daily_bio_html)

        def fn():
            person = mlbstats.get_person(pid, LIVE_TTL)
            seasons = set()
            for group in ("hitting", "pitching", "fielding"):
                seasons |= {int(r["_season"]) for r in mlbstats.get_person_stats(pid, group, LIVE_TTL)["season"]
                            if str(r["_season"]).isdigit()}
            return pid, person, sorted(seasons), season

        self._run_latest("daily_load", fn, self._on_daily_player_loaded, self._on_daily_error)

    def _on_daily_error(self, message: str):
        self.daily_find_btn.setEnabled(True)
        self._say("daily", f"Dailies: {message}")
        self.daily_bio.setText(f"Couldn't load that: {html.escape(message)}")

    def _on_daily_player_loaded(self, data):
        pid, person, seasons, wanted = data
        name = person.get("name", "?")
        self._daily_pid, self._daily_name, self._daily_position = pid, name, person.get("position", "")
        facts = [person.get("position")]
        if person.get("bats") or person.get("throws"):
            facts.append(f"Bats {person.get('bats') or '?'} / Throws {person.get('throws') or '?'}")
        if person.get("debut"):
            facts.append(f"MLB debut {person['debut']}")
        facts.append("active" if person.get("active") else "retired")
        self._daily_bio_html = (f"<b>{html.escape(name)}</b>   \u00b7   "
                                + "   \u00b7   ".join(html.escape(str(f)) for f in facts if f))
        self.daily_bio.setText(self._daily_bio_html)

        combo = self.daily_season
        combo.blockSignals(True)
        combo.clear()
        for s in reversed(seasons):  # newest first
            combo.addItem(str(s), s)
        if seasons:
            target = wanted if wanted in seasons else seasons[-1]
            combo.setCurrentIndex(combo.findData(target))
        combo.setEnabled(bool(seasons))
        combo.blockSignals(False)
        if not seasons:
            for role in self.daily_models:
                self.daily_models[role].clear()
                self.daily_notes[role].setText(f"{name} has no MLB games.")
            self._say("daily", f"{name} \u00b7 no MLB stats")
            return
        self._load_daily_games()

    def _load_daily_games(self):
        """Fetch the chosen player-season's game logs (all three API groups) plus the
        scores of his teams' games, and fill the four sub-tabs."""
        pid, season = self._daily_pid, self.daily_season.currentData()
        if pid is None or season is None or self.daily_level_team.isChecked():
            return
        name, post = self._daily_name, self.daily_post.isChecked()
        if season < mlbstats.DAILY_FROM_SEASON:
            for role in self.daily_models:
                self.daily_models[role].clear()
                self.daily_notes[role].setText(
                    f"No game-by-game data for {season} \u2014 the source's game logs start in "
                    f"{mlbstats.DAILY_FROM_SEASON}.  {name}'s season totals are on the Personal tab.")
            self._say("daily", f"{name} \u00b7 {season} \u00b7 no game logs before {mlbstats.DAILY_FROM_SEASON}")
            return
        for role in self.daily_models:
            self.daily_notes[role].setText(f"Loading {name}'s {season} games\u2026")
        ttl = LIVE_TTL if season >= self._season_now else None

        def fn():
            logs = {group: mlbstats.get_game_log(pid, group, season, post, ttl)
                    for group in ("hitting", "pitching", "fielding")}
            abbrevs = {tid: t.get("abbrev") for tid, t in mlbstats.get_team_map(season, ttl).items()}
            games = {}
            for tid in {r["_team_id"] for rows in logs.values() for r in rows if r["_team_id"]}:
                try:  # only for the score -- without it the Result column is just W or L
                    games.update((g["pk"], g) for g in mlbstats.get_team_games(tid, season, ttl))
                except mlbstats.MLBStatsError:
                    pass
            return pid, season, post, logs, abbrevs, games

        self._run_latest("daily_games", fn, self._on_daily_games, self._on_daily_error)

    def _load_daily_team(self):
        """The Team level: the club's own game logs for the roster box's team and season."""
        key = self.daily_picker.team_season()
        if key is None:
            self.daily_bio.setText("Choose a team and season in \u201cOr browse a team\u2019s roster\u201d.")
            return
        team_id, team_name, season = key
        post = self.daily_post.isChecked()
        self.daily_bio.setText(f"<b>{html.escape(team_name)}</b>   \u00b7   {season}")
        if season < mlbstats.DAILY_FROM_SEASON:
            for role in self.daily_models:
                self.daily_models[role].clear()
                self.daily_notes[role].setText(
                    f"No game-by-game data for {season} \u2014 the source's game logs start in "
                    f"{mlbstats.DAILY_FROM_SEASON}.  The season's team totals are on the Stats tab.")
            self._say("daily", f"{team_name} \u00b7 {season} \u00b7 no game logs before {mlbstats.DAILY_FROM_SEASON}")
            return
        for role in self.daily_models:
            self.daily_notes[role].setText(f"Loading the {season} {team_name} games\u2026")
        ttl = LIVE_TTL if season >= self._season_now else None

        def fn():
            logs = {group: mlbstats.get_team_game_log(team_id, group, season, post, ttl)
                    for group in ("hitting", "pitching", "fielding")}
            abbrevs = {tid: t.get("abbrev") for tid, t in mlbstats.get_team_map(season, ttl).items()}
            try:
                games = {g["pk"]: g for g in mlbstats.get_team_games(team_id, season, ttl)}
            except mlbstats.MLBStatsError:
                games = {}
            return key, post, logs, abbrevs, games

        # Same tag as a player's games, so whichever was asked for last is what's shown.
        self._run_latest("daily_games", fn, self._on_daily_team_games, self._on_daily_error)

    def _on_daily_team_games(self, data):
        (team_id, team_name, season), post, logs, abbrevs, games = data
        if not self.daily_level_team.isChecked():
            return
        for title, role in mlbstats.PERSON_ROLES:
            rows = logs[mlbstats.PERSON_API_GROUP[role]]
            self._fill_daily_table(role, title, season, post,
                                   mlbstats.daily_lines(role, rows, abbrevs, games, team=True),
                                   mlbstats.DAILY_TEAM_COLUMNS[role], team_name, team=True)
        # The record, from the regular-season scores (ties and all), else the team log's W/L.
        regular = [r for r in logs["pitching"] if r["_type"] == "R"]
        results = [(games.get(r["_pk"]) or {}).get("result") or ("W" if r["_win"] else "L") for r in regular]
        record = "\u2013".join(str(results.count(x)) for x in ("W", "L"))
        if results.count("T"):
            record += f"\u2013{results.count('T')}"
        self.daily_bio.setText(f"<b>{html.escape(team_name)}</b>   \u00b7   {season}"
                               + (f"   \u00b7   {record} in the regular season" if regular else ""))
        n_games = len({r["_pk"] for rows in logs.values() for r in rows})
        self._say("daily", f"{team_name} \u00b7 {season} \u00b7 {n_games} game(s)"
                           + (" incl. postseason" if post else ""))

    def _on_daily_games(self, data):
        pid, season, post, logs, abbrevs, games = data
        if pid != self._daily_pid or self.daily_level_team.isChecked():
            return
        name = self._daily_name
        roles = [role for _t, role in mlbstats.PERSON_ROLES]
        has = {}
        for title, role in mlbstats.PERSON_ROLES:
            rows = logs[mlbstats.PERSON_API_GROUP[role]]
            has[role] = bool(rows)
            self._fill_daily_table(role, title, season, post,
                                   mlbstats.daily_lines(role, rows, abbrevs, games),
                                   mlbstats.DAILY_COLUMNS[role], name)
        current = roles[self.daily_tabs.currentIndex()]
        if not self._daily_tab_pinned or not has[current]:
            order = (["pitching", "hitting", "running", "fielding"] if self._daily_position == "P"
                     else ["hitting", "pitching", "running", "fielding"])
            first = next((r for r in order if has[r]), None)
            if first is not None:
                self.daily_tabs.setCurrentIndex(roles.index(first))
        n_games = len({r["_pk"] for rows in logs.values() for r in rows})
        self._say("daily", f"{name} \u00b7 {season} \u00b7 {n_games} game(s)"
                           + (" incl. postseason" if post else ""))

    def _fill_daily_table(self, role: str, title: str, season: int, post: bool, lines: list[dict],
                          columns: list, name: str, team: bool = False):
        """One role's game rows, total lines bold -- or a note saying there's nothing.
        ``name`` is the player's or (``team``) the club's."""
        model, table, note = self.daily_models[role], self.daily_tables[role], self.daily_notes[role]
        model.clear()
        model.setHorizontalHeaderLabels([h for h, _ in columns])
        if not lines and team:
            note.setText(f"No {title.lower()} game logs for the {name} in {season}.")
            return
        if not lines:
            what = {"hitting": "bat", "running": "bat (so no base-running)", "pitching": "pitch",
                    "fielding": "take the field"}[role]
            note.setText(f"{name} didn't {what} in {season}" + (", regular season or postseason." if post else "."))
            return
        star = ("* season to date, through that game" + (" (the postseason counts on its own)" if post else "")
                + ".  The total line is the season's.")
        if role == "fielding":
            star = ("FLD% is that game's; FLD%* is the season to date" + ("." if team else " at that position.  "
                    "Each position gets its own total line."))
        note.setText(star)
        for row in lines:
            items = [self._new_item(row.get(key, ""), numeric=key not in mlbstats.DAILY_TEXT_KEYS)
                     for _h, key in columns]
            if row["_kind"] != "game":
                for item in items:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
            model.appendRow(items)
        table.resizeColumnsToContents()

    # ---- custom stat tab --------------------------------------------------
    def _build_custom_tab(self) -> QWidget:
        """Define reusable formula-based "custom stats" over either Team
        totals or per-player fields, browse their computed values for a
        team-season in a spreadsheet (a pop-up window, opened from this tab),
        and save/load a set of definitions as JSON.  Once defined here, a custom stat also shows up
        as a dependent variable/predictor on the Regression tab (matching
        its own level) and as a predictor on the Factor Analysis tab
        (player-level ones only -- Factor Analysis is always one row per
        player).
        """
        self.custom_team_combo = QComboBox()
        self.custom_team_combo.setMinimumWidth(220)
        self.custom_season_spin = _YearSpin()
        self.custom_season_spin.setRange(1876, 2100)
        self.custom_season_spin.setKeyboardTracking(False)
        self._custom_team_debounce = QTimer(self, singleShot=True, interval=250)
        self._custom_team_debounce.timeout.connect(self._reload_custom_teams)
        self.custom_season_spin.valueChanged.connect(lambda _: self._custom_team_debounce.start())
        self.custom_team_combo.currentIndexChanged.connect(lambda _: self._reload_custom_table())
        self.custom_refresh_btn = QPushButton("Refresh")
        self.custom_refresh_btn.setToolTip("Ignore the cache and recompute the spreadsheet")
        self.custom_refresh_btn.clicked.connect(lambda: self._reload_custom_table(force=True))

        team_row = QHBoxLayout()
        for w in (QLabel("Team"), self.custom_team_combo, QLabel("  Season"), self.custom_season_spin):
            team_row.addWidget(w)
        team_row.addStretch(1)
        team_row.addWidget(self.custom_refresh_btn)
        self.custom_show_btn = QPushButton("Show spreadsheet\u2026")
        self.custom_show_btn.setToolTip("Open the computed values for the team/season and sample above in a window")
        self.custom_show_btn.clicked.connect(self._show_custom_popup)
        team_row.addWidget(self.custom_show_btn)

        # Sample: which (team, season) pairs the spreadsheet covers -- same
        # three choices as the Regression and Factor Analysis tabs.  "This
        # team, this season" keeps today's one-team-season table shape;
        # the other two add a row per season (history) or per team (league)
        # at Team level, and pool player-seasons across the scope at Player
        # level, same as those two tabs' own samples.
        self.custom_scope = _SampleScope(self, "roster", lambda: self._reload_custom_table())

        scope_row = self.custom_scope.box
        self.custom_season_spin.valueChanged.connect(self.custom_scope.sync_division)
        self.custom_scope.sync_division(self.custom_season_spin.value())

        # Level: which variable namespace (and so which table shape) a
        # custom stat uses -- one number per team-season (Team totals
        # fields) or one row per player (per-player fields), same
        # distinction as the Regression tab's "One row per".
        self.custom_level_team = QRadioButton("Team totals")
        self.custom_level_team.setChecked(True)
        self.custom_level_player = QRadioButton("Player")
        level_group = QButtonGroup(self)
        for b in (self.custom_level_team, self.custom_level_player):
            level_group.addButton(b)
        level_group.buttonClicked.connect(lambda _b: self._on_custom_level_changed())

        level_row = QHBoxLayout()
        level_row.addWidget(QLabel("Level:"))
        level_row.addWidget(self.custom_level_team)
        level_row.addWidget(self.custom_level_player)
        level_row.addStretch(1)

        self.custom_name = QLineEdit()
        self.custom_name.setPlaceholderText("Name, e.g. Runs Produced")
        self.custom_formula = QLineEdit()
        self.custom_formula.setPlaceholderText(
            "Formula, e.g. {Batting: Runs} + {Batting: RBI} - {Batting: Home runs}")
        self.custom_insert = QComboBox()
        self.custom_insert.setMinimumWidth(200)
        insert_btn = QPushButton("Insert")
        insert_btn.setToolTip("Insert the selected field into the formula at the cursor")
        insert_btn.clicked.connect(self._insert_custom_token)
        add_btn = QPushButton("Add custom stat")
        add_btn.clicked.connect(self._add_custom_stat)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name"))
        name_row.addWidget(self.custom_name, 1)
        # The formula box gets its own full-width line -- squeezed onto a row
        # with a label, the insert combo, and two buttons, it was too narrow
        # to read or edit comfortably.
        formula_tools_row = QHBoxLayout()
        formula_tools_row.addWidget(self.custom_insert, 1)
        formula_tools_row.addWidget(insert_btn)
        formula_tools_row.addWidget(add_btn)

        self._custom_hidden: set[tuple[str, str]] = set()  # (level, name) of stats left out of the spreadsheet
        self.custom_defs_list = QListWidget()
        self.custom_defs_list.setToolTip(
            "Every custom stat defined this session.  The ticked ones are shown in the spreadsheet\n"
            "(at the current Level) -- untick a stat to leave it out.  Select one and Remove to delete it.\n"
            "Double-click one to tick it, switch the Level above to match and open the spreadsheet at its\n"
            "column (there's no separate 'run' step -- the ticked stats are already computed there for\n"
            "the team/season above).")
        self.custom_defs_list.currentItemChanged.connect(
            lambda cur, _prev: self.custom_remove_btn.setEnabled(cur is not None))
        self.custom_defs_list.itemDoubleClicked.connect(self._jump_to_custom_stat)
        self.custom_defs_list.itemChanged.connect(self._on_custom_stat_ticked)
        self.custom_remove_btn = QPushButton("Remove selected")
        self.custom_remove_btn.setEnabled(False)
        self.custom_remove_btn.clicked.connect(self._remove_selected_custom_stat)
        tick_all_btn = QPushButton("Tick all")
        tick_all_btn.clicked.connect(lambda: self._tick_custom_stats(True))
        untick_all_btn = QPushButton("Untick all")
        untick_all_btn.clicked.connect(lambda: self._tick_custom_stats(False))
        remove_row = QHBoxLayout()
        remove_row.addWidget(self.custom_remove_btn, 1)
        remove_row.addWidget(tick_all_btn)
        remove_row.addWidget(untick_all_btn)

        self.custom_save_btn = QPushButton("Save definitions…")
        self.custom_save_btn.setToolTip("Save every custom stat defined this session to a JSON file")
        self.custom_save_btn.clicked.connect(self._save_custom_stats)
        self.custom_load_btn = QPushButton("Load definitions…")
        self.custom_load_btn.setToolTip(
            "Add custom stats from one or more JSON files -- each a list of definitions\n"
            "(what Save writes) or a single definition (one file per stat)")
        self.custom_load_btn.clicked.connect(self._load_custom_stats)
        file_row = QHBoxLayout()
        file_row.addWidget(self.custom_save_btn)
        file_row.addWidget(self.custom_load_btn)
        file_row.addStretch(1)

        left_col = QVBoxLayout()
        left_col.addLayout(team_row)
        left_col.addLayout(scope_row)
        left_col.addLayout(level_row)
        left_col.addWidget(QLabel("Define a custom stat:"))
        left_col.addLayout(name_row)
        left_col.addWidget(QLabel("Formula"))
        left_col.addWidget(self.custom_formula)
        left_col.addLayout(formula_tools_row)
        left_col.addWidget(QLabel(
            "Defined custom stats \u2014 tick the ones to show in the spreadsheet (double-click one to open it "
            "at that column):"))
        left_col.addWidget(self.custom_defs_list, 1)  # the list takes the room the spreadsheet used to
        left_col.addLayout(remove_row)
        left_col.addLayout(file_row)
        left_widget = QWidget()
        left_widget.setLayout(left_col)

        self.custom_model = QStandardItemModel(self)
        self.custom_proxy = _NumProxy(self)
        self.custom_proxy.setSourceModel(self.custom_model)
        self.custom_table = QTableView()
        self.custom_table.setModel(self.custom_proxy)
        self.custom_table.setSortingEnabled(True)
        self.custom_table.setAlternatingRowColors(True)
        self.custom_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.custom_table.verticalHeader().setVisible(False)
        self.custom_table.horizontalHeader().setHighlightSections(False)

        # The computed values live in a window of their own rather than beside the definitions.  It is
        # created once and only hidden when closed, so the table keeps updating and comes back as it was.
        self.custom_popup = QDialog(self)
        self.custom_popup.setWindowTitle("Custom stats")
        self.custom_popup.setModal(False)
        self.custom_popup.resize(900, 600)
        popup_col = QVBoxLayout(self.custom_popup)
        custom_table_note = QLabel(
            "Computed automatically for every stat at the current Level, for the team/season "
            "in the Custom tab -- no separate step to run one.")
        custom_table_note.setWordWrap(True)
        popup_col.addWidget(custom_table_note)
        self.custom_popup_warning = QLabel()   # what is blank or missing, and why
        self.custom_popup_warning.setWordWrap(True)
        self.custom_popup_warning.setStyleSheet("color: #d9534f;")
        self.custom_popup_warning.setVisible(False)
        popup_col.addWidget(self.custom_popup_warning)
        popup_col.addWidget(self.custom_table, 1)

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addWidget(left_widget, 1)
        self._update_custom_insert_combo()
        return page

    def _reload_custom_teams(self):
        season = self.custom_season_spin.value()
        cached = self._season_names.get(season)
        if cached is not None:
            self._relabel_custom_teams(cached)
            self._reload_custom_table()
            return
        ttl = LIVE_TTL if season >= self._season_now else None

        def fn():
            return season, mlbstats.get_team_map(season, ttl)

        self._run_latest("custom_teams", fn, self._on_custom_teams_loaded,
                         lambda m: self._on_team_refresh_failed("custom", m))

    def _on_custom_teams_loaded(self, data):
        season, names = data
        self._season_names[season] = names
        if season == self.custom_season_spin.value():  # spinner may have moved on already
            self._relabel_custom_teams(names)
            self._reload_custom_table()

    def _relabel_custom_teams(self, names: dict):
        keep_id = self.custom_team_combo.currentData()
        if not names or keep_id not in names:
            return
        teams = sorted(names.values(), key=lambda t: t["name"])
        self.custom_team_combo.blockSignals(True)
        self.custom_team_combo.clear()
        target = 0
        for i, t in enumerate(teams):
            self.custom_team_combo.addItem(t["name"], t["id"])
            if t["id"] == keep_id:
                target = i
        self.custom_team_combo.setCurrentIndex(target)
        self.custom_team_combo.blockSignals(False)

    def _on_custom_level_changed(self):
        self._update_custom_insert_combo()
        self._reload_custom_table()

    def _update_custom_insert_combo(self):
        is_player = self.custom_level_player.isChecked()
        level = "player" if is_player else "team"
        self.custom_insert.clear()
        for token in self._formula_tokens(level):
            self.custom_insert.addItem(token)
        self.custom_formula.setToolTip(
            "Formula over " + ("per-player" if is_player else "Team totals") + ' fields, e.g. '
            '"{Batting: Runs} + {Batting: RBI} - {Batting: Home runs}". Use "Insert" so a '
            "field's name matches exactly."
            + ("\n{Custom: Name} uses another player-level custom stat.  qualify(amount, minimum, value) is the\n"
               "value only if the amount reaches the minimum (e.g. qualify({Batting: PA}, 3.1 * {Team: G}, ...));\n"
               "product(a, b, ...) multiplies whichever aren't blank, average(a, b, ...) averages them.  {MLB: ...} is MLB's figure that season,\n"
               "{Range: ...} a fielder's range as on the Range tab, {Team: G} his team's games." if is_player else ""))

    def _insert_custom_token(self):
        self.custom_formula.insert(f"{{{self.custom_insert.currentText()}}}")
        self.custom_formula.setFocus()

    def _add_custom_stat(self):
        name = self.custom_name.text().strip()
        formula_text = self.custom_formula.text().strip()
        if not name:
            QMessageBox.warning(self, "Custom stat", "Give the stat a name first.")
            return
        if not formula_text:
            QMessageBox.warning(self, "Custom stat", "Write a formula first.")
            return
        try:
            used = formula.formula_variables(formula_text)
        except formula.FormulaError as exc:
            QMessageBox.warning(self, "Custom stat", f"Couldn't parse that formula:\n\n{exc}")
            return
        is_player = self.custom_level_player.isChecked()
        level = "player" if is_player else "team"
        valid = set(self._formula_tokens(level))
        unknown = used - valid
        if unknown:
            QMessageBox.warning(
                self, "Custom stat",
                "Unrecognized variable(s): " + ", ".join(sorted(unknown)) +
                "\n\nUse the Insert dropdown so the names match a field exactly for the "
                f"current '{'Player' if is_player else 'Team totals'}' level.")
            return
        self._custom_stats.append((level, name, formula_text))
        self._on_custom_stats_changed()
        self.custom_name.clear()
        self.custom_formula.clear()
        self._say("custom", f'Added custom stat "{name}" ({level} level).')

    def _remove_selected_custom_stat(self):
        item = self.custom_defs_list.currentItem()
        if item is None:
            return
        idx = item.data(Qt.ItemDataRole.UserRole)
        level, name, _ftext = self._custom_stats[idx]
        del self._custom_stats[idx]
        self._on_custom_stats_changed()
        self._say("custom", f'Removed custom stat "{name}" ({level} level).')

    def _show_custom_popup(self):
        """Open (or bring forward) the spreadsheet window."""
        first = not self.custom_popup.isVisible() and not getattr(self, "_custom_popup_placed", False)
        self.custom_popup.show()
        if first:  # the first time, centre it over the main window; after that it stays where it was left
            self._custom_popup_placed = True
            geo = self.geometry()
            self.custom_popup.move(geo.center().x() - self.custom_popup.width() // 2,
                                   geo.center().y() - self.custom_popup.height() // 2)
        self.custom_popup.raise_()
        self.custom_popup.activateWindow()

    def _jump_to_custom_stat(self, item):
        """Double-click a definition: there's no "run" step -- the ticked stats at the current level
        are already in the spreadsheet -- so this opens it, ticks the stat if it was left out, makes
        sure the Level matches this stat (switching it, and so reloading the table, if not) and
        selects its column/row once it's there."""
        idx = item.data(Qt.ItemDataRole.UserRole)
        level, name, _ftext = self._custom_stats[idx]
        is_player = level == "player"
        self._show_custom_popup()
        was_hidden = (level, name) in self._custom_hidden
        if was_hidden:
            self._custom_hidden.discard((level, name))
            self.custom_defs_list.blockSignals(True)  # one reload below, not one per change
            item.setCheckState(Qt.CheckState.Checked)
            self.custom_defs_list.blockSignals(False)
        if self.custom_level_player.isChecked() != is_player:
            self._custom_pending_jump = name  # table reload is async; _on_custom_table finishes this
            (self.custom_level_player if is_player else self.custom_level_team).setChecked(True)
            self._on_custom_level_changed()
        elif was_hidden:
            self._custom_pending_jump = name
            self._reload_custom_table()
        else:
            self._select_custom_column(name)
        self._say("custom",
            f'Showing "{name}" ({"Player" if is_player else "Team totals"} level) in the spreadsheet.')

    def _select_custom_column(self, name: str):
        """Jump to one custom stat in the table -- a no-op if it isn't there
        yet (a level switch's reload is still in flight; this simply runs
        again once that lands, from ``_on_custom_table``).  Player level:
        each stat is a column, and the table's row-selection behavior can't
        highlight a column, so instead sort by it (descending) -- which
        doubles as "run" in the sense the user's likely after: the top
        scorers on that stat rise straight to the top.  Team level: each
        stat is a row, so select it directly."""
        headers = [self.custom_model.horizontalHeaderItem(c).text()
                  for c in range(self.custom_model.columnCount())]
        if name in headers:
            col = headers.index(name)
            self.custom_table.sortByColumn(col, Qt.SortOrder.DescendingOrder)
            self.custom_table.scrollToTop()
            return
        for r in range(self.custom_model.rowCount()):
            first_cell = self.custom_model.item(r, 0)
            if first_cell is not None and first_cell.text() == name:
                self.custom_table.selectRow(r)
                self.custom_table.scrollTo(self.custom_proxy.index(r, 0))
                return

    def _custom_stats_shown(self, level: str) -> list[tuple[str, str]]:
        """The stats at ``level`` that are ticked to show in the spreadsheet."""
        return [(name, ftext) for lvl, name, ftext in self._custom_stats
                if lvl == level and (lvl, name) not in self._custom_hidden]

    def _on_custom_stat_ticked(self, item):
        level, name, _ftext = self._custom_stats[item.data(Qt.ItemDataRole.UserRole)]
        if item.checkState() == Qt.CheckState.Unchecked:
            self._custom_hidden.add((level, name))
        else:
            self._custom_hidden.discard((level, name))
        self._reload_custom_table()

    def _tick_custom_stats(self, shown: bool):
        """Tick (or untick) every stat at once; the spreadsheet is recomputed once."""
        self._custom_hidden = set() if shown else {(lvl, name) for lvl, name, _f in self._custom_stats}
        self._refresh_custom_defs_list()
        self._reload_custom_table()

    def _refresh_custom_defs_list(self):
        self.custom_defs_list.blockSignals(True)  # ticking the items below isn't the user ticking them
        self.custom_defs_list.clear()
        for i, (level, name, ftext) in enumerate(self._custom_stats):
            level_label = "Team totals" if level == "team" else "Player"
            item = QListWidgetItem(f"[{level_label}] {name}  =  {ftext}")
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setToolTip(ftext)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if (level, name) in self._custom_hidden
                               else Qt.CheckState.Checked)
            self.custom_defs_list.addItem(item)
        self.custom_defs_list.blockSignals(False)
        self.custom_remove_btn.setEnabled(False)

    def _on_custom_stats_changed(self):
        """Central refresh point after ``self._custom_stats`` is mutated --
        added, removed, or loaded from a file -- keeps the Custom tab's own
        list/table and the Regression/Correlation/Factor Analysis tabs'
        dependent-variable and predictor lists all in sync."""
        self._sync_custom_formulas()
        self._refresh_custom_defs_list()
        self._update_reg_dep_combo()
        self._update_corr_list()
        self._populate_fa_list()
        self._reload_custom_table()
        self._refresh_production_offer()
        self._refresh_standings_pyth()
        self._refresh_prevention()
        self._refresh_info_insert()
        self._refresh_speed()
        self._refresh_player_scores()

    def _sync_custom_formulas(self):
        """Tell the formula module which custom stats ``{Custom: Name}`` can refer to (a player-level
        one if both levels have that name)."""
        mapping = {name: ftext for lvl, name, ftext in self._custom_stats if lvl == "player"}
        for lvl, name, ftext in self._custom_stats:
            mapping.setdefault(name, ftext)
        formula.set_custom_formulas(mapping)

    def _formula_tokens(self, level: str) -> list[str]:
        """Every ``{...}`` a custom-stat formula at ``level`` may use: the fields of
        :meth:`_variable_universe`, and at player level the Score views' (MLB's figures, a
        fielder's range, his team's games) and the player-level custom stats themselves."""
        tokens = [token for token, _data in self._variable_universe(level)]
        if level == "player":
            tokens += [f"{view}: {label}" for view, columns in mlbstats.SCORE_VARIABLES.items()
                       for label, _key, _agg in columns]
            tokens += [f"{formula.CUSTOM_PREFIX} {name}" for lvl, name, _f in self._custom_stats if lvl == "player"]
        return tokens

    def _reload_custom_table(self, force: bool = False):
        if self._custom_job is not None:
            self._custom_reload_pending = True  # one is running; run again, with the controls as they are then
            return
        is_player = self.custom_level_player.isChecked()
        level = "player" if is_player else "team"
        stats = self._custom_stats_shown(level)

        choice = self._sample_choice(self.custom_scope, self.custom_season_spin, self.custom_team_combo)
        if choice is None:
            return
        kind, team_id, team_name, season, first_year = choice

        self.custom_model.clear()
        if not stats:
            self.custom_model.setHorizontalHeaderLabels(
                ["Player"] if is_player else ["Custom stat", "Value"])
            kind_name = "Player" if is_player else "Team totals"
            self._set_custom_warning(
                f"No {kind_name}-level custom stats are defined." if not self._custom_stats_for(level) else
                f"None of the {kind_name}-level custom stats is ticked in the list on the Custom tab.")
            return

        job = _CustomTableJob(kind, level, team_id, team_name, season, first_year,
                              stats, self._season_now, force=force)
        job.sig.progress.connect(
            lambda i, total, label: self._say("custom", f"Fetching {label}…  ({i}/{total})"))
        job.sig.done.connect(self._on_custom_table)
        job.sig.failed.connect(self._on_custom_table_failed)
        self._custom_job = job
        self._start_job(job)

    def _set_custom_warning(self, text: str):
        """The red line at the top of the spreadsheet window: what is blank or missing, and why."""
        self.custom_popup_warning.setText(text)
        self.custom_popup_warning.setVisible(bool(text))

    def _on_custom_table(self, payload):
        kind, headers, rows, desc, *rest = payload
        note = rest[0] if rest else ""
        self._custom_job = None
        if self._run_pending_custom_reload():
            return  # what just arrived is for controls that have since changed
        self._set_custom_warning(note)
        self.custom_model.clear()
        if kind == "team_single":
            self.custom_model.setHorizontalHeaderLabels(["Custom stat", "Value"])
            for name, val in rows:
                self.custom_model.appendRow([self._new_item(name, numeric=False), self._new_item(val)])
        else:
            self.custom_model.setHorizontalHeaderLabels(headers)
            for row in rows:
                items = [self._new_item(row[0], numeric=False)]
                items += [self._new_item(v) for v in row[1:]]
                self.custom_model.appendRow(items)
        self.custom_table.resizeColumnsToContents()
        self.custom_popup.setWindowTitle(f"Custom stats \u2014 {desc}")
        self._say("custom", f"{desc} · {len(rows)} row(s)" + (f"  \u2014  {note}" if note else ""))
        if self._custom_pending_jump is not None:
            self._select_custom_column(self._custom_pending_jump)
            self._custom_pending_jump = None

    def _on_custom_table_failed(self, message: str):
        self._custom_job = None
        if self._run_pending_custom_reload():
            return
        self._say("custom", f"Custom stats: {message}")

    def _run_pending_custom_reload(self) -> bool:
        """If a reload was asked for while one was running, start it now (the result that has just
        arrived was for the controls as they were, so it is dropped).  -> whether one was started."""
        if not self._custom_reload_pending:
            return False
        self._custom_reload_pending = False
        self._reload_custom_table()
        return True

    def _save_custom_stats(self):
        if not self._custom_stats:
            QMessageBox.information(self, "Custom stats", "No custom stat definitions to save yet.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save custom stat definitions", os.path.join(HERE, "custom-stats.json"),
            "JSON files (*.json)")
        if not path:
            return
        data = [{"level": level, "name": name, "formula": ftext}
               for level, name, ftext in self._custom_stats]
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return
        self._say("custom", f"Saved {len(data)} custom stat definition(s) to {path}")

    @staticmethod
    def _read_custom_definitions(path: str) -> list:
        """The definitions in one JSON file: a list of ``{level, name, formula}``
        objects (what "Save definitions\u2026" writes), or a single such object
        (one file per custom stat).  Raises OSError / ValueError if unreadable."""
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
        raise ValueError("expected a custom stat definition, or a list of them")

    def _ingest_custom_definitions(self, entries: list) -> tuple[int, list[str]]:
        """Validate each definition and add it to ``self._custom_stats`` (a
        definition with the same level and name as an existing one replaces it,
        so re-loading a file refreshes rather than duplicates).  -> (how many
        were added or replaced, names that were skipped as invalid).  The
        caller refreshes the tabs via ``_on_custom_stats_changed``."""
        added, skipped = 0, []
        for entry in entries:
            if not isinstance(entry, dict):
                skipped.append(str(entry)[:40])
                continue
            level = entry.get("level")
            name = str(entry.get("name", "")).strip()
            ftext = str(entry.get("formula", "")).strip()
            if level not in ("team", "player") or not name or not ftext:
                skipped.append(name or "(unnamed)")
                continue
            try:
                used = formula.formula_variables(ftext)
            except formula.FormulaError:
                skipped.append(name)
                continue
            valid = set(self._formula_tokens(level))
            if used - valid:
                skipped.append(name)
                continue
            for i, (lvl, existing, _f) in enumerate(self._custom_stats):
                if lvl == level and existing == name:
                    self._custom_stats[i] = (level, name, ftext)
                    break
            else:
                self._custom_stats.append((level, name, ftext))
            self._sync_custom_formulas()   # so a later definition can refer to this one
            added += 1
        return added, skipped

    def _load_custom_stats(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Load custom stat definitions", HERE, "JSON files (*.json)")
        if not paths:
            return
        added, skipped = 0, []
        for path in paths:
            try:
                entries = self._read_custom_definitions(path)
            except (OSError, ValueError) as exc:
                skipped.append(f"{os.path.basename(path)} ({exc})")
                continue
            file_added, file_skipped = self._ingest_custom_definitions(entries)
            added += file_added
            skipped += file_skipped

        if added:
            self._on_custom_stats_changed()
        msg = f"Loaded {added} custom stat definition(s) from {len(paths)} file(s)."
        if skipped:
            shown = ", ".join(skipped[:5]) + ("…" if len(skipped) > 5 else "")
            msg += f"  Skipped {len(skipped)}: {shown}"
        self._say("custom", msg)
        if skipped:
            QMessageBox.warning(self, "Load custom stats", msg)

    def _load_default_custom_stats(self):
        """At startup, load the JSON files the Head-To-Head tab depends on (the
        three "...Against" custom stats) and the Production tab's Runs Produced
        from the app folder, if they're there."""
        added = 0
        for fname in [f for _key, _label, _cname, f in H2H_STATS] + [PRODUCTION_FILE] + [f for _c, _n, f in PYTHAGOREAN_STATS] + [f for _c, _n, f, _h in PREVENTION_STATS] + [f for _c, _n, f in SPEED_STATS] + TAB_DERIVED_FILES + SCORE_FILES:
            try:
                entries = self._read_custom_definitions(os.path.join(HERE, fname))
            except (OSError, ValueError):
                continue
            file_added, _skipped = self._ingest_custom_definitions(entries)
            added += file_added
        if added:
            self._on_custom_stats_changed()

    # ---- correlation tab ---------------------------------------------------
    def _build_correlation_tab(self) -> QWidget:
        """Pearson correlation matrix over any combination of Batting/
        Pitching/Fielding (incl. Advanced/Sabermetrics/Expected) numbers,
        plus any custom stats defined for the current level -- same sample
        scope, level, and predictor-selection tools as the Regression tab,
        minus the dependent variable (correlation has no "dependent" side).
        """
        self.corr_team_combo = QComboBox()
        self.corr_team_combo.setMinimumWidth(220)

        self.corr_scope = _SampleScope(self, "history", lambda: self._on_corr_scope_changed())

        # Same "roster scope forces Player level" rule as the Regression tab
        # -- a single team-season is exactly one point at Team level, so
        # there'd be nothing to correlate.
        self.corr_level_team = QRadioButton("Team totals")
        self.corr_level_team.setChecked(True)
        self.corr_level_player = QRadioButton("Player")
        level_group = QButtonGroup(self)
        for b in (self.corr_level_team, self.corr_level_player):
            level_group.addButton(b)
        level_group.buttonClicked.connect(lambda _b: self._update_corr_list())

        self.corr_season_spin = _YearSpin()
        self.corr_season_spin.setRange(1876, 2100)
        self.corr_season_spin.setKeyboardTracking(False)
        self.corr_season_spin.setToolTip(
            "Used by 'this season, across all teams' and 'this team, this season'.\n"
            "Typing a year needs Enter (or click elsewhere) to take effect.")
        self._corr_team_debounce = QTimer(self, singleShot=True, interval=250)
        self._corr_team_debounce.timeout.connect(self._reload_corr_teams)
        self.corr_season_spin.valueChanged.connect(lambda _: self._corr_team_debounce.start())

        team_row = QHBoxLayout()
        for w in (QLabel("Team"), self.corr_team_combo, QLabel("  Season"), self.corr_season_spin):
            team_row.addWidget(w)
        team_row.addStretch(1)

        scope_row = self.corr_scope.box
        self.corr_season_spin.valueChanged.connect(self.corr_scope.sync_division)
        self.corr_scope.sync_division(self.corr_season_spin.value())

        level_row = QHBoxLayout()
        level_row.addWidget(QLabel("One row per:"))
        for w in (self.corr_level_team, self.corr_level_player):
            level_row.addWidget(w)
        level_row.addStretch(1)

        # Text set for real by _update_corr_list() -- see _custom_note_text().
        self.corr_custom_note = QLabel()
        self.corr_custom_note.setWordWrap(True)

        self.corr_filter = QLineEdit()
        self.corr_filter.setPlaceholderText("Filter variables…")
        self.corr_filter.textChanged.connect(self._filter_corr_predictors)

        corr_select_all_btn = QPushButton("Select all")
        corr_select_all_btn.setToolTip("Check every variable currently shown (respects the filter above)")
        corr_select_all_btn.clicked.connect(lambda: self._set_corr_predictors_checked(True))
        corr_select_none_btn = QPushButton("Select none")
        corr_select_none_btn.setToolTip("Uncheck every variable currently shown (respects the filter above)")
        corr_select_none_btn.clicked.connect(lambda: self._set_corr_predictors_checked(False))
        corr_select_row = QHBoxLayout()
        corr_select_row.addWidget(corr_select_all_btn)
        corr_select_row.addWidget(corr_select_none_btn)
        corr_select_row.addStretch(1)

        self.corr_list = QListWidget()
        self.corr_list.setToolTip(
            "Check any combination of variables to correlate against each other.\n"
            "Counting stats are summed across the roster; rate/scale stats\n"
            "(averages, WHIP, wOBA, FIP…) are weighted by playing time."
        )
        for view, columns in mlbstats.REGRESSION_VARIABLES.items():
            header = QListWidgetItem(f"— {view} —")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            self.corr_list.addItem(header)
            for label, key, agg in columns:
                item = QListWidgetItem(label)
                item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsSelectable)
                item.setCheckState(Qt.CheckState.Unchecked)
                item.setData(Qt.ItemDataRole.UserRole, (view, key, agg, label))
                self.corr_list.addItem(item)

        self.corr_run_btn = QPushButton("Run correlation")
        self.corr_run_btn.setEnabled(False)
        self.corr_run_btn.clicked.connect(self._run_correlation)

        predictors_col = QVBoxLayout()
        predictors_col.addWidget(QLabel("Variables (any combination, from any view but Team totals)"))
        predictors_col.addWidget(self.corr_filter)
        predictors_col.addLayout(corr_select_row)
        predictors_col.addWidget(self.corr_list, 1)
        predictors_col.addWidget(self.corr_run_btn)
        predictors_widget = QWidget()
        predictors_widget.setLayout(predictors_col)

        self.corr_output = QPlainTextEdit()
        self.corr_output.setReadOnly(True)
        self.corr_output.setFont(_monospace_font())
        self.corr_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.corr_output.setPlaceholderText(
            "Check at least two variables, then Run correlation."
        )

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(predictors_widget)
        splitter.addWidget(self.corr_output)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 680])

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(team_row)
        outer.addLayout(scope_row)
        outer.addLayout(level_row)
        outer.addWidget(self.corr_custom_note)
        outer.addWidget(splitter, 1)
        self._update_corr_list()
        return page

    def _filter_corr_predictors(self, text: str):
        text = text.strip().lower()
        current_header = None
        header_has_match = {}
        for i in range(self.corr_list.count()):
            item = self.corr_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if data is None:
                current_header = item
                header_has_match[id(item)] = False
                continue
            match = not text or text in item.text().lower() or text in data[0].lower()
            item.setHidden(not match)
            if match and current_header is not None:
                header_has_match[id(current_header)] = True
        for i in range(self.corr_list.count()):
            item = self.corr_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) is None:
                item.setHidden(not header_has_match.get(id(item), True))

    def _set_corr_predictors_checked(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.corr_list.count()):
            item = self.corr_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) is not None and not item.isHidden():
                item.setCheckState(state)

    def _checked_corr_predictors(self) -> list[tuple[str, str, str, str]]:
        out = []
        for i in range(self.corr_list.count()):
            item = self.corr_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if data is not None and item.checkState() == Qt.CheckState.Checked:
                out.append(data)
        return out

    def _reload_corr_teams(self):
        season = self.corr_season_spin.value()
        cached = self._season_names.get(season)
        if cached is not None:
            self._relabel_corr_teams(cached)
            return
        ttl = LIVE_TTL if season >= self._season_now else None

        def fn():
            return season, mlbstats.get_team_map(season, ttl)

        self._run_latest("corr_teams", fn, self._on_corr_teams_loaded,
                         lambda m: self._on_team_refresh_failed("corr", m))

    def _on_corr_teams_loaded(self, data):
        season, names = data
        self._season_names[season] = names
        if season == self.corr_season_spin.value():
            self._relabel_corr_teams(names)

    def _relabel_corr_teams(self, names: dict):
        keep_id = self.corr_team_combo.currentData()
        if not names or keep_id not in names:
            return
        teams = sorted(names.values(), key=lambda t: t["name"])
        self.corr_team_combo.blockSignals(True)
        self.corr_team_combo.clear()
        target = 0
        for i, t in enumerate(teams):
            self.corr_team_combo.addItem(t["name"], t["id"])
            if t["id"] == keep_id:
                target = i
        self.corr_team_combo.setCurrentIndex(target)
        self.corr_team_combo.blockSignals(False)

    def _on_corr_scope_changed(self):
        is_roster = self.corr_scope.checked() in ("roster", "same_division")  # too few teams for Team level
        self.corr_level_team.setEnabled(not is_roster)
        self.corr_level_player.setEnabled(not is_roster)
        if is_roster and not self.corr_level_player.isChecked():
            self.corr_level_player.setChecked(True)
            self._update_corr_list()

    def _update_corr_list(self):
        """Refresh the predictor checklist's "-- Custom --" section and the
        note above it for the current level.  Called on level toggle, and
        whenever the Custom tab's stat list changes."""
        is_player = self.corr_level_player.isChecked()
        level = "player" if is_player else "team"
        self.corr_custom_note.setText(self._custom_note_text(level))
        self._corr_predictor_items = self._refresh_custom_predictor_section(
            level, self.corr_list, self._corr_predictor_items)

    def _run_correlation(self):
        if self._corr_job is not None:
            return
        variables = self._checked_corr_predictors()
        if len(variables) < 2:
            QMessageBox.information(self, "Correlation", "Check at least two variables first.")
            return
        level = "player" if self.corr_level_player.isChecked() else "team"

        choice = self._sample_choice(self.corr_scope, self.corr_season_spin, self.corr_team_combo, "Correlation")
        if choice is None:
            return
        kind, team_id, team_name, season, first_year = choice

        job = _CorrelationJob(kind, level, team_id, team_name, season, first_year,
                              variables, self._season_now)

        self.corr_run_btn.setEnabled(False)
        self.corr_output.setPlainText("Fetching data…")
        job.sig.progress.connect(
            lambda i, total, label: self.corr_output.setPlainText(f"Fetching {label}…  ({i}/{total})"))
        job.sig.done.connect(self._on_correlation_done)
        job.sig.failed.connect(self._on_correlation_failed)
        self._corr_job = job
        self._start_job(job)

    def _on_correlation_done(self, payload):
        labels, matrix, n, sample_desc = payload
        self._corr_job = None
        self.corr_run_btn.setEnabled(True)
        self.corr_output.setPlainText(_format_correlation(labels, matrix, n, sample_desc))

    def _on_correlation_failed(self, message: str):
        self._corr_job = None
        self.corr_run_btn.setEnabled(True)
        self.corr_output.setPlainText(f"Correlation failed:\n\n{message}")

    # ---- regression tab --------------------------------------------------
    def _build_regression_tab(self) -> QWidget:
        """Multiple regression: any Team-totals number as the dependent
        variable, against any combination of Batting/Pitching/Fielding
        (incl. Advanced/Sabermetrics/Expected) numbers as predictors.
        """
        self.reg_team_combo = QComboBox()
        self.reg_team_combo.setMinimumWidth(220)

        # Scope: which (team, season) pairs make up the sample.
        self.reg_scope = _SampleScope(self, "history", lambda: self._on_reg_scope_changed())

        # Unit of analysis: one row per team-season (Team totals dependent
        # variable, predictors rolled up across the roster) or one row per
        # player (dependent variable and predictors both per-player, joined
        # by player id).  "This team, this season" only has one team-season,
        # so a Team-totals row there would be a single point -- Player is
        # forced whenever that scope is picked.
        self.reg_level_team = QRadioButton("Team totals")
        self.reg_level_team.setChecked(True)
        self.reg_level_player = QRadioButton("Player")
        level_group = QButtonGroup(self)
        for b in (self.reg_level_team, self.reg_level_player):
            level_group.addButton(b)
        level_group.buttonClicked.connect(lambda _b: self._update_reg_dep_combo())

        self.reg_season_spin = _YearSpin()
        self.reg_season_spin.setRange(1876, 2100)
        self.reg_season_spin.setKeyboardTracking(False)
        self.reg_season_spin.setToolTip(
            "Used by 'this season, across all teams' and 'this team, this season'.\n"
            "Typing a year needs Enter (or click elsewhere) to take effect.")
        # Same behavior as the Stats tab: the dropdown shows teams under the
        # name they carried *in the selected season* (e.g. "St. Louis Browns"
        # for 1953, "Baltimore Orioles" for 1954+) -- debounced since it's a
        # network fetch, and it keeps whichever franchise is already selected.
        self._reg_team_debounce = QTimer(self, singleShot=True, interval=250)
        self._reg_team_debounce.timeout.connect(self._reload_reg_teams)
        self.reg_season_spin.valueChanged.connect(lambda _: self._reg_team_debounce.start())

        team_row = QHBoxLayout()
        for w in (QLabel("Team"), self.reg_team_combo, QLabel("  Season"), self.reg_season_spin):
            team_row.addWidget(w)
        team_row.addStretch(1)

        scope_row = self.reg_scope.box
        self.reg_season_spin.valueChanged.connect(self.reg_scope.sync_division)
        self.reg_scope.sync_division(self.reg_season_spin.value())

        level_row = QHBoxLayout()
        level_row.addWidget(QLabel("One row per:"))
        for w in (self.reg_level_team, self.reg_level_player):
            level_row.addWidget(w)
        level_row.addStretch(1)

        self.reg_dep_combo = QComboBox()
        self.reg_dep_label = QLabel("Dependent variable")

        dep_row = QHBoxLayout()
        dep_row.addWidget(self.reg_dep_label)
        dep_row.addWidget(self.reg_dep_combo, 1)

        # Text set for real by _update_reg_dep_combo() -- it names exactly which
        # custom stats are showing at the current level, and flags any that
        # exist at the other level and so are hidden (namespaces don't mix:
        # a Team-totals custom stat has no meaning as a per-player number).
        self.reg_custom_note = QLabel()
        self.reg_custom_note.setWordWrap(True)

        self.reg_filter = QLineEdit()
        self.reg_filter.setPlaceholderText("Filter predictors…")
        self.reg_filter.textChanged.connect(self._filter_reg_predictors)

        reg_select_all_btn = QPushButton("Select all")
        reg_select_all_btn.setToolTip("Check every predictor currently shown (respects the filter above)")
        reg_select_all_btn.clicked.connect(lambda: self._set_reg_predictors_checked(True))
        reg_select_none_btn = QPushButton("Select none")
        reg_select_none_btn.setToolTip("Uncheck every predictor currently shown (respects the filter above)")
        reg_select_none_btn.clicked.connect(lambda: self._set_reg_predictors_checked(False))
        reg_select_row = QHBoxLayout()
        reg_select_row.addWidget(reg_select_all_btn)
        reg_select_row.addWidget(reg_select_none_btn)
        reg_select_row.addStretch(1)

        self.reg_list = QListWidget()
        self.reg_list.setToolTip(
            "Check any combination of variables to use as predictors.\n"
            "Counting stats are summed across the roster; rate/scale stats\n"
            "(averages, WHIP, wOBA, FIP…) are weighted by playing time."
        )
        for view, columns in mlbstats.REGRESSION_VARIABLES.items():
            header = QListWidgetItem(f"— {view} —")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            self.reg_list.addItem(header)
            for label, key, agg in columns:
                item = QListWidgetItem(label)
                item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsSelectable)
                item.setCheckState(Qt.CheckState.Unchecked)
                item.setData(Qt.ItemDataRole.UserRole, (view, key, agg, label))
                self.reg_list.addItem(item)

        self.reg_run_btn = QPushButton("Run regression")
        self.reg_run_btn.setEnabled(False)
        self.reg_run_btn.clicked.connect(self._run_regression)

        predictors_col = QVBoxLayout()
        predictors_col.addWidget(QLabel("Predictors (any combination, from any view but Team totals)"))
        predictors_col.addWidget(self.reg_filter)
        predictors_col.addLayout(reg_select_row)
        predictors_col.addWidget(self.reg_list, 1)
        predictors_col.addWidget(self.reg_run_btn)
        predictors_widget = QWidget()
        predictors_widget.setLayout(predictors_col)

        self.reg_output = QPlainTextEdit()
        self.reg_output.setReadOnly(True)
        self.reg_output.setFont(_monospace_font())
        # No wrap: the table columns line up in fixed-width monospace, so
        # wrapping would break that alignment -- a horizontal scrollbar
        # (Qt's default whenever content is wider than the viewport, with
        # wrapping off) keeps it intact instead.
        self.reg_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.reg_output.setPlaceholderText(
            "Pick a dependent variable and one or more predictors, then Run regression."
        )

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(predictors_widget)
        splitter.addWidget(self.reg_output)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 680])

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(team_row)
        outer.addLayout(scope_row)
        outer.addLayout(level_row)
        outer.addLayout(dep_row)
        outer.addWidget(self.reg_custom_note)
        outer.addWidget(splitter, 1)
        self._update_reg_dep_combo()
        return page

    def _filter_reg_predictors(self, text: str):
        text = text.strip().lower()
        current_header = None
        header_has_match = {}
        for i in range(self.reg_list.count()):
            item = self.reg_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if data is None:
                current_header = item
                header_has_match[id(item)] = False
                continue
            match = not text or text in item.text().lower() or text in data[0].lower()
            item.setHidden(not match)
            if match and current_header is not None:
                header_has_match[id(current_header)] = True
        for i in range(self.reg_list.count()):
            item = self.reg_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) is None:
                item.setHidden(not header_has_match.get(id(item), True))

    def _set_reg_predictors_checked(self, checked: bool):
        """Check/uncheck every predictor currently shown -- a hidden item
        (filtered out) is left exactly as it was, so filtering down to e.g.
        "Pitching" and hitting Select none only clears Pitching checks."""
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.reg_list.count()):
            item = self.reg_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) is not None and not item.isHidden():
                item.setCheckState(state)

    def _checked_predictors(self) -> list[tuple[str, str, str, str]]:
        out = []
        for i in range(self.reg_list.count()):
            item = self.reg_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if data is not None and item.checkState() == Qt.CheckState.Checked:
                out.append(data)  # (view, key, agg, label)
        return out

    def _reload_reg_teams(self):
        """Refresh reg_team_combo's labels for reg_season_spin's value --
        cheap when the Stats tab (or an earlier call here) already fetched
        this season, a background request otherwise."""
        season = self.reg_season_spin.value()
        cached = self._season_names.get(season)
        if cached is not None:
            self._relabel_reg_teams(cached)
            return
        ttl = LIVE_TTL if season >= self._season_now else None

        def fn():
            return season, mlbstats.get_team_map(season, ttl)

        self._run_latest("reg_teams", fn, self._on_reg_teams_loaded,
                         lambda m: self._on_team_refresh_failed("reg", m))

    def _on_reg_teams_loaded(self, data):
        season, names = data
        self._season_names[season] = names
        if season == self.reg_season_spin.value():  # spinner may have moved on already
            self._relabel_reg_teams(names)

    def _relabel_reg_teams(self, names: dict):
        """Rename reg_team_combo's entries to how the league stood in a given
        season, keeping the current selection by franchise id -- same idea as
        the Stats tab's team dropdown.  A franchise not yet in the league that
        season leaves the list untouched rather than yanking the selection."""
        keep_id = self.reg_team_combo.currentData()
        if not names or keep_id not in names:
            return
        teams = sorted(names.values(), key=lambda t: t["name"])
        self.reg_team_combo.blockSignals(True)
        self.reg_team_combo.clear()
        target = 0
        for i, t in enumerate(teams):
            self.reg_team_combo.addItem(t["name"], t["id"])
            if t["id"] == keep_id:
                target = i
        self.reg_team_combo.setCurrentIndex(target)
        self.reg_team_combo.blockSignals(False)

    def _on_reg_scope_changed(self):
        is_roster = self.reg_scope.checked() in ("roster", "same_division")  # too few teams for Team level
        self.reg_level_team.setEnabled(not is_roster)
        self.reg_level_player.setEnabled(not is_roster)
        # Only entering Roster while on Team level actually changes the level
        # (and so needs the dependent-variable/predictor lists rebuilt) --
        # switching between History and League, or leaving Roster, leaves the
        # level alone and shouldn't reset whatever the user had picked.
        if is_roster and not self.reg_level_player.isChecked():
            self.reg_level_player.setChecked(True)
            self._update_reg_dep_combo()

    def _variable_universe(self, level: str) -> list[tuple[str, tuple]]:
        """-> [(token, comboData), ...] of every non-custom field at
        ``level`` ("player" or "team") -- the Regression tab's dependent-
        variable combo, and the Custom tab's own dependent/"Insert variable"
        combo and formula validation, all draw from this."""
        if level == "player":
            return [(f"{view}: {label}", (view, key, agg, f"{view}: {label}"))
                    for view, columns in mlbstats.REGRESSION_VARIABLES.items()
                    for label, key, agg in columns]
        return [(f"{view}: {label}", (group, key, None, f"{view}: {label}"))
                for view, group, label, key in mlbstats.DEPENDENT_VARIABLES]

    def _custom_stats_for(self, level: str) -> list[tuple[str, str]]:
        return [(name, ftext) for lvl, name, ftext in self._custom_stats if lvl == level]

    def _custom_note_text(self, level: str) -> str:
        """Explains exactly which custom stats are usable at ``level`` right
        now, and points at the toggle for any that exist at the other level
        instead -- namespaces don't mix (a Team-totals custom stat has no
        meaning as a per-player number), so those are otherwise invisible
        without an obvious reason why.  Shared by the Regression and
        Correlation tabs."""
        other_level = "team" if level == "player" else "player"
        other_label = "Team totals" if level == "player" else "Player"
        here = self._custom_stats_for(level)
        elsewhere = self._custom_stats_for(other_level)
        note = 'Custom stats are defined on the "Custom" tab.'
        if here:
            note += " At this level: " + ", ".join(n for n, _f in here) + "."
        if elsewhere:
            note += (f" {len(elsewhere)} more defined at the {other_label} level -- switch "
                     f"\"One row per: {other_label}\" above to use " +
                     ("them" if len(elsewhere) > 1 else "it") + ".")
        if not here and not elsewhere:
            note += " None defined yet."
        return note

    def _update_reg_dep_combo(self):
        """Repopulate the dependent-variable combo and the predictor list's
        "-- Custom --" section, for the current level: Team totals (one
        number per team-season) or Player (one row per player) -- each with
        its own custom stats, since the two levels' formulas reference
        different variable namespaces.  Called on level toggle, and also
        whenever the Custom tab's stat list changes."""
        is_player = self.reg_level_player.isChecked()
        level = "player" if is_player else "team"
        self.reg_dep_label.setText(
            "Dependent variable (per player)" if is_player else "Dependent variable (Team totals)")

        universe = self._variable_universe(level)
        self.reg_dep_combo.clear()
        for token, data in universe:
            self.reg_dep_combo.addItem(token, data)
        for name, formula_text in self._custom_stats_for(level):
            self.reg_dep_combo.addItem(f"Custom: {name}",
                                       ("__custom__", formula_text, None, f"Custom: {name}"))

        self.reg_custom_note.setText(self._custom_note_text(level))
        self._custom_predictor_items = self._refresh_custom_predictor_section(
            level, self.reg_list, self._custom_predictor_items)

    def _refresh_custom_predictor_section(self, level: str, list_widget: QListWidget,
                                          tracking: list[QListWidgetItem]) -> list[QListWidgetItem]:
        """(Re)build one predictor checklist's "-- Custom --" section to
        match ``level``'s custom stats, preserving which ones were already
        checked.  Team-level custom stats are already one number per
        team-season (no roster roll-up); player-level ones are evaluated per
        player, same as any other predictor.  Shared by the Regression and
        Correlation tabs' predictor lists (Factor Analysis has its own --
        it always rebuilds its whole list from scratch on a grouping
        change, so folding the custom section in there directly is simpler
        than sharing this one). Returns the new tracking list -- the caller
        stores it back (e.g. ``self._custom_predictor_items = ...``)."""
        checked_labels = {
            item.data(Qt.ItemDataRole.UserRole)[3] for item in tracking
            if item.checkState() == Qt.CheckState.Checked
        }
        for item in tracking:
            list_widget.takeItem(list_widget.row(item))
        tracking = []

        matching = self._custom_stats_for(level)
        if not matching:
            return tracking
        header = QListWidgetItem("— Custom —")
        header.setFlags(Qt.ItemFlag.NoItemFlags)
        font = header.font()
        font.setBold(True)
        header.setFont(font)
        list_widget.addItem(header)
        tracking.append(header)
        for name, formula_text in matching:
            item_label = f"Custom: {name}"
            item = QListWidgetItem(name)
            item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                          | Qt.ItemFlag.ItemIsSelectable)
            item.setCheckState(Qt.CheckState.Checked if item_label in checked_labels
                               else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, ("__custom__", formula_text, None, item_label))
            list_widget.addItem(item)
            tracking.append(item)
        return tracking

    def _run_regression(self):
        if self._reg_job is not None:
            return
        predictors = self._checked_predictors()
        if not predictors:
            QMessageBox.information(self, "Regression", "Check at least one predictor first.")
            return
        dep_source, dep_key, dep_agg, dep_label = self.reg_dep_combo.currentData()
        level = "player" if self.reg_level_player.isChecked() else "team"

        choice = self._sample_choice(self.reg_scope, self.reg_season_spin, self.reg_team_combo, "Regression")
        if choice is None:
            return
        kind, team_id, team_name, season, first_year = choice

        job = _RegressionJob(kind, level, team_id, team_name, season, first_year,
                             dep_source, dep_key, dep_label, predictors, self._season_now,
                             dep_agg=dep_agg)

        self.reg_run_btn.setEnabled(False)
        self.reg_output.setPlainText("Fetching data…")
        job.sig.progress.connect(
            lambda i, total, label: self.reg_output.setPlainText(f"Fetching {label}…  ({i}/{total})"))
        job.sig.done.connect(self._on_regression_done)
        job.sig.failed.connect(self._on_regression_failed)
        self._reg_job = job
        self._start_job(job)

    def _on_regression_done(self, text: str):
        self._reg_job = None
        self.reg_run_btn.setEnabled(True)
        self.reg_output.setPlainText(text)

    def _on_regression_failed(self, message: str):
        self._reg_job = None
        self.reg_run_btn.setEnabled(True)
        self.reg_output.setPlainText(f"Regression failed:\n\n{message}")

    # ---- factor analysis tab ---------------------------------------------
    def _build_factor_tab(self) -> QWidget:
        """Factor analysis: pick a player grouping and any combination of
        Batting/Pitching/Fielding numbers, and it finds the orthogonal
        (varimax-rotated) factors behind however those numbers move
        together -- then lets you name each one from its top loaders.
        """
        self.fa_team_combo = QComboBox()
        self.fa_team_combo.setMinimumWidth(220)

        # Scope: which (team, season) pairs make up the sample -- identical
        # three choices as the Regression tab's "Sample".
        self.fa_scope = _SampleScope(self, "history", lambda: None)

        # Grouping: which players are eligible, expressed as which views the
        # predictor list offers -- a player only has a complete row (and so
        # only counts) if every checked variable resolves for them, which is
        # what actually confines the sample to batters/pitchers/everyone.
        self.fa_group_batters = QRadioButton("Batters")
        self.fa_group_batters.setChecked(True)
        self.fa_group_pitchers = QRadioButton("Pitchers")
        self.fa_group_all = QRadioButton("All players")
        fa_grouping_group = QButtonGroup(self)
        for b in (self.fa_group_batters, self.fa_group_pitchers, self.fa_group_all):
            fa_grouping_group.addButton(b)
        fa_grouping_group.buttonClicked.connect(lambda _b: self._populate_fa_list())

        self.fa_season_spin = _YearSpin()
        self.fa_season_spin.setRange(1876, 2100)
        self.fa_season_spin.setKeyboardTracking(False)
        self.fa_season_spin.setToolTip(
            "Used by 'this season, across all teams' and 'this team, this season'.\n"
            "Typing a year needs Enter (or click elsewhere) to take effect.")
        self._fa_team_debounce = QTimer(self, singleShot=True, interval=250)
        self._fa_team_debounce.timeout.connect(self._reload_fa_teams)
        self.fa_season_spin.valueChanged.connect(lambda _: self._fa_team_debounce.start())

        team_row = QHBoxLayout()
        for w in (QLabel("Team"), self.fa_team_combo, QLabel("  Season"), self.fa_season_spin):
            team_row.addWidget(w)
        team_row.addStretch(1)

        scope_row = self.fa_scope.box
        self.fa_season_spin.valueChanged.connect(self.fa_scope.sync_division)
        self.fa_scope.sync_division(self.fa_season_spin.value())

        group_row = QHBoxLayout()
        group_row.addWidget(QLabel("Group:"))
        for w in (self.fa_group_batters, self.fa_group_pitchers, self.fa_group_all):
            group_row.addWidget(w)
        group_row.addStretch(1)

        # Number of factors: Kaiser's "eigenvalue > 1" rule by default, or a
        # manual count.
        self.fa_factors_auto = QCheckBox("Auto (Kaiser: eigenvalue > 1)")
        self.fa_factors_auto.setChecked(True)
        self.fa_factors_spin = QSpinBox()
        self.fa_factors_spin.setRange(1, 20)
        self.fa_factors_spin.setEnabled(False)
        self.fa_factors_auto.toggled.connect(lambda on: self.fa_factors_spin.setEnabled(not on))

        factors_row = QHBoxLayout()
        factors_row.addWidget(QLabel("Factors:"))
        factors_row.addWidget(self.fa_factors_auto)
        factors_row.addWidget(self.fa_factors_spin)
        factors_row.addStretch(1)

        self.fa_filter = QLineEdit()
        self.fa_filter.setPlaceholderText("Filter variables…")
        self.fa_filter.textChanged.connect(self._filter_fa_predictors)

        fa_select_all_btn = QPushButton("Select all")
        fa_select_all_btn.setToolTip("Check every variable currently shown (respects the filter above)")
        fa_select_all_btn.clicked.connect(lambda: self._set_fa_predictors_checked(True))
        fa_select_none_btn = QPushButton("Select none")
        fa_select_none_btn.setToolTip("Uncheck every variable currently shown (respects the filter above)")
        fa_select_none_btn.clicked.connect(lambda: self._set_fa_predictors_checked(False))
        fa_select_row = QHBoxLayout()
        fa_select_row.addWidget(fa_select_all_btn)
        fa_select_row.addWidget(fa_select_none_btn)
        fa_select_row.addStretch(1)

        self.fa_list = QListWidget()
        self.fa_list.setToolTip(
            "Check any combination of variables to factor-analyze.\n"
            "A player only counts if every checked variable has a value for them,\n"
            "so mixing Batting and Pitching variables mostly keeps two-way players."
        )
        self.fa_run_btn = QPushButton("Run factor analysis")
        self.fa_run_btn.setEnabled(False)
        self.fa_run_btn.clicked.connect(self._run_factor_analysis)

        predictors_col = QVBoxLayout()
        predictors_col.addWidget(QLabel("Variables (any combination, matching the group above)"))
        predictors_col.addWidget(self.fa_filter)
        predictors_col.addLayout(fa_select_row)
        predictors_col.addWidget(self.fa_list, 1)
        predictors_col.addWidget(self.fa_run_btn)
        predictors_widget = QWidget()
        predictors_widget.setLayout(predictors_col)

        self.fa_output = QPlainTextEdit()
        self.fa_output.setReadOnly(True)
        self.fa_output.setFont(_monospace_font())
        self.fa_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.fa_output.setPlaceholderText(
            "Pick a group and at least two variables, then Run factor analysis."
        )

        # Enabled after a run: "Name factors…" opens a dialog with one label
        # field per factor (scrollable, for when there are a lot of them),
        # seeded with that factor's top loaders as a hint; "View factor
        # scores…" opens the per-case score table.
        self.fa_label_btn = QPushButton("Name factors…")
        self.fa_label_btn.setToolTip("Give each extracted factor a name, from a hint of its top loaders")
        self.fa_label_btn.setEnabled(False)
        self.fa_label_btn.clicked.connect(self._show_fa_label_dialog)
        self.fa_scores_btn = QPushButton("View factor scores…")
        self.fa_scores_btn.setToolTip("How each individual player in the sample scored on every factor")
        self.fa_scores_btn.setEnabled(False)
        self.fa_scores_btn.clicked.connect(self._show_fa_scores)
        results_btn_row = QHBoxLayout()
        results_btn_row.addStretch(1)
        results_btn_row.addWidget(self.fa_label_btn)
        results_btn_row.addWidget(self.fa_scores_btn)

        output_col = QVBoxLayout()
        output_col.addWidget(self.fa_output, 1)
        output_col.addLayout(results_btn_row)
        output_widget = QWidget()
        output_widget.setLayout(output_col)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(predictors_widget)
        splitter.addWidget(output_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 680])

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(team_row)
        outer.addLayout(scope_row)
        outer.addLayout(group_row)
        outer.addLayout(factors_row)
        outer.addWidget(splitter, 1)
        self._populate_fa_list()
        return page

    def _current_fa_grouping(self) -> str:
        if self.fa_group_pitchers.isChecked():
            return "Pitchers"
        if self.fa_group_all.isChecked():
            return "All players"
        return "Batters"

    def _populate_fa_list(self):
        """(Re)build the variable checklist for the current grouping,
        preserving which (view, key) pairs were already checked -- Fielding
        is offered under both Batters and Pitchers, so switching between
        those two keeps any Fielding checks intact.  Also called whenever
        the Custom tab's stat list changes, to refresh the "-- Custom --"
        section at the bottom (player-level custom stats only -- Factor
        Analysis is always one row per player)."""
        checked_keys = set()
        for i in range(self.fa_list.count()):
            item = self.fa_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if data is not None and item.checkState() == Qt.CheckState.Checked:
                checked_keys.add((data[0], data[1]))

        self.fa_list.clear()
        for view in mlbstats.FACTOR_GROUPING_VIEWS[self._current_fa_grouping()]:
            header = QListWidgetItem(f"— {view} —")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            self.fa_list.addItem(header)
            for label, key, agg in mlbstats.REGRESSION_VARIABLES[view]:
                item = QListWidgetItem(label)
                item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsSelectable)
                item.setCheckState(Qt.CheckState.Checked if (view, key) in checked_keys
                                   else Qt.CheckState.Unchecked)
                item.setData(Qt.ItemDataRole.UserRole, (view, key, agg, label))
                self.fa_list.addItem(item)

        custom = self._custom_stats_for("player")
        if custom:
            header = QListWidgetItem("— Custom —")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            self.fa_list.addItem(header)
            for name, formula_text in custom:
                item_label = f"Custom: {name}"
                item = QListWidgetItem(name)
                item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsSelectable)
                item.setCheckState(Qt.CheckState.Checked
                                   if ("__custom__", formula_text) in checked_keys
                                   else Qt.CheckState.Unchecked)
                item.setData(Qt.ItemDataRole.UserRole, ("__custom__", formula_text, None, item_label))
                self.fa_list.addItem(item)

        self._filter_fa_predictors(self.fa_filter.text())

    def _filter_fa_predictors(self, text: str):
        text = text.strip().lower()
        current_header = None
        header_has_match = {}
        for i in range(self.fa_list.count()):
            item = self.fa_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if data is None:
                current_header = item
                header_has_match[id(item)] = False
                continue
            match = not text or text in item.text().lower() or text in data[0].lower()
            item.setHidden(not match)
            if match and current_header is not None:
                header_has_match[id(current_header)] = True
        for i in range(self.fa_list.count()):
            item = self.fa_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) is None:
                item.setHidden(not header_has_match.get(id(item), True))

    def _set_fa_predictors_checked(self, checked: bool):
        """Check/uncheck every variable currently shown -- a hidden item
        (filtered out) is left exactly as it was, so filtering down to e.g.
        "AVG" and hitting Select none only clears AVG-shaped checks."""
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.fa_list.count()):
            item = self.fa_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) is not None and not item.isHidden():
                item.setCheckState(state)

    def _checked_fa_predictors(self) -> list[tuple[str, str, str, str]]:
        out = []
        for i in range(self.fa_list.count()):
            item = self.fa_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if data is not None and item.checkState() == Qt.CheckState.Checked:
                out.append(data)  # (view, key, agg, label)
        return out

    def _reload_fa_teams(self):
        season = self.fa_season_spin.value()
        cached = self._season_names.get(season)
        if cached is not None:
            self._relabel_fa_teams(cached)
            return
        ttl = LIVE_TTL if season >= self._season_now else None

        def fn():
            return season, mlbstats.get_team_map(season, ttl)

        self._run_latest("fa_teams", fn, self._on_fa_teams_loaded,
                         lambda m: self._on_team_refresh_failed("fa", m))

    def _on_fa_teams_loaded(self, data):
        season, names = data
        self._season_names[season] = names
        if season == self.fa_season_spin.value():
            self._relabel_fa_teams(names)

    def _relabel_fa_teams(self, names: dict):
        keep_id = self.fa_team_combo.currentData()
        if not names or keep_id not in names:
            return
        teams = sorted(names.values(), key=lambda t: t["name"])
        self.fa_team_combo.blockSignals(True)
        self.fa_team_combo.clear()
        target = 0
        for i, t in enumerate(teams):
            self.fa_team_combo.addItem(t["name"], t["id"])
            if t["id"] == keep_id:
                target = i
        self.fa_team_combo.setCurrentIndex(target)
        self.fa_team_combo.blockSignals(False)

    def _show_fa_label_dialog(self):
        """A dialog with one label field per extracted factor -- its top
        loaders shown as a hint of what it represents -- in a scroll area on
        the right so a large factor count doesn't blow up the dialog height.
        Applying re-renders the output above with the typed names folded in,
        and updates the factor-scores table's column headers too."""
        result = self._fa_result
        if result is None:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Name each factor")
        dialog.resize(520, 480)
        outer = QVBoxLayout(dialog)
        note = QLabel(
            "Each factor's top loaders are shown as a hint -- name it for whatever most "
            "defines the players that score high on it.")
        note.setWordWrap(True)
        outer.addWidget(note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        form = QVBoxLayout(inner)
        edits = []
        for f in range(result.n_factors):
            top = factor_analysis.top_loadings(result, f, 3)
            hint = ", ".join(f"{lab} ({val:+.2f})" for lab, val in top)
            row = QHBoxLayout()
            row.addWidget(QLabel(f"Factor {f + 1}:"))
            edit = QLineEdit()
            edit.setPlaceholderText(f"e.g. based on {hint}")
            edit.setToolTip(f"Top loaders: {hint}")
            if f < len(self._fa_labels):
                edit.setText(self._fa_labels[f])
            row.addWidget(edit, 1)
            form.addLayout(row)
            edits.append(edit)
        form.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        apply_btn = QPushButton("Apply")
        apply_btn.setDefault(True)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(apply_btn)
        outer.addLayout(btn_row)

        def do_apply():
            self._fa_labels = [edit.text().strip() for edit in edits]
            self.fa_output.setPlainText(
                _format_factor_analysis(result, self._fa_sample_desc, self._fa_labels))
            dialog.accept()

        apply_btn.clicked.connect(do_apply)
        dialog.exec()

    def _run_factor_analysis(self):
        if self._fa_job is not None:
            return
        predictors = self._checked_fa_predictors()
        if len(predictors) < 2:
            QMessageBox.information(self, "Factor analysis", "Check at least two variables first.")
            return
        n_factors = None if self.fa_factors_auto.isChecked() else self.fa_factors_spin.value()

        choice = self._sample_choice(self.fa_scope, self.fa_season_spin, self.fa_team_combo, "Factor analysis")
        if choice is None:
            return
        kind, team_id, team_name, season, first_year = choice

        job = _FactorJob(kind, team_id, team_name, season, first_year,
                         predictors, n_factors, self._season_now)

        self.fa_run_btn.setEnabled(False)
        self.fa_scores_btn.setEnabled(False)
        self.fa_label_btn.setEnabled(False)
        self.fa_output.setPlainText("Fetching data…")
        self._fa_labels = []
        self._fa_scored_rows = []
        job.sig.progress.connect(
            lambda i, total, label: self.fa_output.setPlainText(f"Fetching {label}…  ({i}/{total})"))
        job.sig.done.connect(self._on_factor_done)
        job.sig.failed.connect(self._on_factor_failed)
        self._fa_job = job
        self._start_job(job)

    def _on_factor_done(self, payload):
        result, sample_desc, scored_rows = payload
        self._fa_job = None
        self.fa_run_btn.setEnabled(True)
        self._fa_result, self._fa_sample_desc = result, sample_desc
        self._fa_scored_rows = scored_rows
        self._fa_labels = [""] * result.n_factors
        self.fa_output.setPlainText(_format_factor_analysis(result, sample_desc))
        self.fa_label_btn.setEnabled(bool(result.n_factors))
        self.fa_scores_btn.setEnabled(bool(scored_rows))

    def _on_factor_failed(self, message: str):
        self._fa_job = None
        self.fa_run_btn.setEnabled(True)
        self._fa_result = None
        self._fa_labels = []
        self._fa_scored_rows = []
        self.fa_label_btn.setEnabled(False)
        self.fa_scores_btn.setEnabled(False)
        self.fa_output.setPlainText(f"Factor analysis failed:\n\n{message}")

    def _show_fa_scores(self):
        """A sortable table of every case's score on each factor -- e.g. how
        each Blue Jays batter individually fit the three factors extracted
        from a "Toronto Blue Jays, batters, 2026" run.  Column headers pick
        up whatever names were last applied in the "Name factors…" dialog."""
        result = self._fa_result
        if result is None or not self._fa_scored_rows:
            return
        labels = (self._fa_labels if len(self._fa_labels) == result.n_factors
                 else [""] * result.n_factors)
        headers = ["Player"] + [f"F{f + 1}" + (f" — {labels[f]}" if labels[f] else "")
                                for f in range(result.n_factors)]

        model = QStandardItemModel(self)
        model.setHorizontalHeaderLabels(headers)
        for row_label, scores in self._fa_scored_rows:
            name_item = QStandardItem(row_label)
            name_item.setEditable(False)
            name_item.setData(row_label.lower(), Qt.ItemDataRole.UserRole)
            row = [name_item]
            for val in scores:
                item = QStandardItem(f"{val:+.3f}")
                item.setEditable(False)
                item.setData(val, Qt.ItemDataRole.UserRole)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                row.append(item)
            model.appendRow(row)

        proxy = _NumProxy(self)
        proxy.setSourceModel(model)
        table = QTableView()
        table.setModel(proxy)
        table.setSortingEnabled(True)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setHighlightSections(False)
        if result.n_factors:
            table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
        table.resizeColumnsToContents()

        dialog = QDialog(self)
        dialog.setWindowTitle("Factor scores")
        dialog.resize(760, 520)
        lay = QVBoxLayout(dialog)
        note = QLabel(
            self._fa_sample_desc.split(" (")[0] + " — standardized factor scores "
            "(roughly, standard deviations from the sample's own average on that factor; "
            "0 = average, +1 ≈ one std dev above). Click a column header to sort.")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addWidget(table, 1)
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        close_row.addWidget(close_btn)
        lay.addLayout(close_row)
        dialog.exec()

    # ---- head-to-head tab -------------------------------------------------
    H2H_HEADERS = ["Stat", "Batter (for)", "Pitcher (against)", "Head-to-head", "Change vs. batter",
                   "Actual, season", "Actual, career"]

    def _build_h2h_tab(self) -> QWidget:
        """A batter against a pitcher.  For AVG, OBP and SLG the batter's own
        ("for") number and the pitcher's "against" number -- calculated by
        the player-level custom stats named in ``H2H_STATS`` -- are added and
        divided by two: the batter's number deflated (lower than his own) or
        inflated (higher) by that pitcher.  Beside it, what the batter actually did against
        that pitcher -- in the batter's season and over their careers (regular season,
        from the play-by-play era on)."""
        self.h2h_batter = _PlayerPicker(self, "h2h_bat", "hitting", "Batter")
        self.h2h_pitcher = _PlayerPicker(self, "h2h_pit", "pitching", "Pitcher")
        self.h2h_batter.changed.connect(self._reload_h2h)
        self.h2h_pitcher.changed.connect(self._reload_h2h)

        pickers = QHBoxLayout()
        pickers.addWidget(self.h2h_batter)
        pickers.addWidget(self.h2h_pitcher)

        self.h2h_title = QLabel("Choose a batter and a pitcher above.")
        self.h2h_title.setWordWrap(True)
        font = self.h2h_title.font()
        font.setBold(True)
        self.h2h_title.setFont(font)
        self.h2h_actual = QLabel()   # how often they really met, and what happened
        self.h2h_actual.setWordWrap(True)
        self.h2h_actual.hide()

        self.h2h_model = QStandardItemModel(self)
        self.h2h_model.setHorizontalHeaderLabels(self.H2H_HEADERS)
        self.h2h_table = QTableView()
        self.h2h_table.setModel(self.h2h_model)
        self.h2h_table.setAlternatingRowColors(True)
        self.h2h_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.h2h_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.h2h_table.verticalHeader().setVisible(False)
        self.h2h_table.horizontalHeader().setHighlightSections(False)
        self.h2h_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.h2h_table.setMaximumHeight(150)

        note = QLabel(
            "Head-to-head = (the batter's number + the pitcher's \"against\" number) ÷ 2.  "
            "Below the batter's own number, this pitcher deflates it; above, inflates it.  "
            "The \"against\" numbers come from the custom stats "
            + ", ".join(f"“{cname}”" for _k, _l, cname, _f in H2H_STATS)
            + " (see the Custom tab).  n/a means the source data doesn't exist for that season: "
            "sacrifice flies allowed are only recorded from 1954 (needed for OBP against) and "
            "total bases allowed from 1974 (needed for SLG against).  "
            "The Actual columns are what the batter really did against this pitcher, regular season only "
            f"— in the batter's season, and over all the seasons they met; the source has these from "
            f"about {mlbstats.VS_PLAYER_FROM_SEASON} on.")
        note.setWordWrap(True)

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(pickers)
        outer.addWidget(self.h2h_title)
        outer.addWidget(self.h2h_actual)
        outer.addWidget(self.h2h_table)
        outer.addWidget(note)
        outer.addStretch(1)
        return page

    def _show_h2h_message(self, text: str):
        self.h2h_title.setText(text)
        self.h2h_actual.hide()
        self.h2h_model.clear()
        self.h2h_model.setHorizontalHeaderLabels(self.H2H_HEADERS)

    def _reload_h2h(self):
        bat, pit = self.h2h_batter.selection(), self.h2h_pitcher.selection()
        if bat is None or pit is None:
            self._show_h2h_message("Choose a batter and a pitcher above.")
            return
        defs = {name.lower(): ftext for lvl, name, ftext in self._custom_stats if lvl == "player"}
        missing = [cname for _k, _l, cname, _f in H2H_STATS if cname.lower() not in defs]
        if missing:
            files = ", ".join(fname for _k, _l, cname, fname in H2H_STATS if cname in missing)
            self._show_h2h_message(
                "The pitcher's \"against\" numbers come from player-level custom stats that aren't "
                f"defined right now: {', '.join(missing)}.  Load {files} on the Custom tab "
                "(Load definitions…).")
            return

        now = self._season_now

        def ttl(season):
            return LIVE_TTL if season >= now else None

        def fn():
            batter_rows = mlbstats.group_by_player(mlbstats.get_player_stats(
                bat["team_id"], bat["season"], "hitting", ttl(bat["season"]))).get(bat["pid"], [])
            pitcher_groups: dict[str, dict] = {}  # view -> {player id: rows}, fetched as a formula needs it
            out = []
            for key, label, cname, _fname in H2H_STATS:
                batter_for = mlbstats.aggregate_rows(batter_rows, key, "avg") if batter_rows else None
                text = defs[cname.lower()]
                variables = formula.formula_variables(text)
                for var in variables:
                    view = mlbstats.REGRESSION_VARIABLE_INDEX[var][0]
                    if view not in pitcher_groups:
                        group, stat_type, _cols = mlbstats.VIEW_SPECS[view]
                        pitcher_groups[view] = mlbstats.group_by_player(mlbstats.get_player_stats(
                            pit["team_id"], pit["season"], group, ttl(pit["season"]),
                            stat_type=stat_type))
                values = mlbstats.player_variable_lookup(variables, pitcher_groups, pit["pid"])
                out.append((label, batter_for, formula.evaluate_formula(text, values)))
            try:  # the actual meetings; without them the prediction still stands
                vs = mlbstats.get_vs_player(bat["pid"], pit["pid"], LIVE_TTL)
            except mlbstats.MLBStatsError as exc:
                vs = str(exc)
            try:  # MLB's surname ("De Paula", not "Paula")
                surname = mlbstats.get_person(pit["pid"]).get("surname") or ""
            except mlbstats.MLBStatsError:
                surname = ""
            return out, vs, surname

        self._run_latest("h2h_compare", fn, lambda res: self._on_h2h_done(bat, pit, *res),
                         self._on_h2h_failed)

    @staticmethod
    def _h2h_line(stat: dict) -> str:
        """“8 AB, 3 H, 1 HR, 2 BB, 3 SO” -- what happened in some plate appearances."""
        parts = [f"{int(stat.get('atBats') or 0)} AB", f"{int(stat.get('hits') or 0)} H"]
        for key, tag in (("doubles", "2B"), ("triples", "3B"), ("homeRuns", "HR"), ("baseOnBalls", "BB"),
                         ("hitByPitch", "HBP"), ("strikeOuts", "SO")):
            if stat.get(key):
                parts.append(f"{int(stat[key])} {tag}")
        return ", ".join(parts)

    def _h2h_actual_text(self, bat: dict, pit: dict, vs) -> str:
        """The sentence above the table: how often the two really met, and how it went."""
        if isinstance(vs, str):
            return f"Couldn't fetch their actual meetings: {html.escape(vs)}"
        season, last = bat["season"], pit.get("surname") or pit["name"].split()[-1]
        year = vs["seasons"].get(season)
        career = vs["career"]
        times = lambda n: "once" if n == 1 else f"{n} times"
        if year and year.get("plateAppearances"):
            text = (f"In fact, {html.escape(bat['name'])} faced {html.escape(last)} "
                    f"{times(int(year['plateAppearances']))} in {season}: {self._h2h_line(year)}.")
        elif season < mlbstats.VS_PLAYER_FROM_SEASON and not career:
            return (f"No record of their actual meetings — the source's batter-vs-pitcher figures start "
                    f"about {mlbstats.VS_PLAYER_FROM_SEASON}.")
        else:
            text = f"{html.escape(bat['name'])} didn't face {html.escape(last)} in {season} (regular season)."
        if career and career.get("plateAppearances"):
            seasons = sorted(vs["seasons"])
            span = f"{seasons[0]}–{seasons[-1]}" if len(seasons) > 1 else str(seasons[0]) if seasons else ""
            text += (f"  Career{' (' + span + ')' if span else ''}: {times(int(career['plateAppearances']))}, "
                     f"{self._h2h_line(career)}.")
        else:
            text += "  They never met in the regular season."
        if pit["season"] != season:
            text += (f"  (The batter's season is used: you picked the pitcher's {pit['season']} line for "
                     "the prediction.)")
        return text

    def _on_h2h_done(self, bat: dict, pit: dict, rows: list, vs=None, surname: str = ""):
        if surname:
            pit = dict(pit, surname=surname)
        self.h2h_title.setText(
            f"{bat['name']} ({bat['team_name']}, {bat['season']})   vs.   "
            f"{pit['name']} ({pit['team_name']}, {pit['season']})")
        self.h2h_model.clear()
        self.h2h_model.setHorizontalHeaderLabels(self.H2H_HEADERS)

        def cell(text: str, right: bool = True) -> QStandardItem:
            item = QStandardItem(text)
            item.setEditable(False)
            if right:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return item

        actual = {} if isinstance(vs, str) or vs is None else vs
        year = (actual.get("seasons") or {}).get(bat["season"]) or {}
        career = actual.get("career") or {}
        self.h2h_model.setHorizontalHeaderLabels(
            self.H2H_HEADERS[:5] + [f"Actual, {bat['season']}", "Actual, career"])
        if vs is not None:
            self.h2h_actual.setText(self._h2h_actual_text(bat, pit, vs))
            self.h2h_actual.show()
        key_of = {label: key for key, label, _c, _f in H2H_STATS}
        for label, batter_for, against in rows:
            combined = mlbstats.head_to_head(batter_for, against)
            if combined is None:
                change = "n/a"
            else:
                delta = round(combined - batter_for, 3)
                effect = "deflated" if delta < 0 else "inflated" if delta > 0 else "unchanged"
                change = f"{_fmt_rate(delta, signed=True)}   {effect}"
            self.h2h_model.appendRow([
                cell(label, right=False), cell(_fmt_rate(batter_for)), cell(_fmt_rate(against)),
                cell(_fmt_rate(combined)), cell(change),
                cell(_fmt_rate(year.get(key_of[label])) if year.get("plateAppearances") else "—"),
                cell(_fmt_rate(career.get(key_of[label])) if career.get("plateAppearances") else "—")])

    def _on_h2h_failed(self, message: str):
        self._show_h2h_message(f"Couldn't calculate the matchup: {message}")

    # ---- range tab ----------------------------------------------------------------
    def _build_range_tab(self) -> QWidget:
        """Nine players, one per fielding position, judged on range factor.

        Each player's range factor at his position -- RF/9, (putouts + assists) per
        nine innings, or RF/G per game when innings aren't recorded -- is divided by what all of MLB did
        at that position that season; the nine proportions are averaged, and over
        100% means the defense covers more ground than MLB's average.

        The Team and Season chooser only decides who's *offered* in the nine
        drop-downs.  Once a player is picked he's locked into his slot along with his
        team, year and both range factors (his, and all of MLB's at that position that
        season), so changing the team or season never disturbs him -- pick another
        player, or "(none)", to replace him.
        """
        self.range_picker = _PlayerPicker(self, "range_pick", "fielding",
                                          "Team and season to choose players from", with_player=False)
        self.range_picker.roster_loaded.connect(self._on_range_roster)
        self._range_roster = None        # (team_id, team_name, season, rows) currently on offer
        self._range_slots = {pos: None for pos, _n in mlbstats.RANGE_POSITIONS}  # locked players
        self._range_league = {}          # season -> {position: rf_figures} for all of MLB
        self._range_loading: set = set()  # seasons whose MLB figures are being fetched
        self._range_league_errors: dict = {}

        grid = QGridLayout()
        for col, text in enumerate(("Position", "Player", "Putouts", "Assists", "Measure", "Player RF", "MLB RF",
                                    "Player \u00f7 MLB")):
            head = QLabel(f"<b>{text}</b>")
            head.setAlignment(Qt.AlignmentFlag.AlignRight if col >= 2 else Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(head, 0, col)
        self.range_combos: dict[str, QComboBox] = {}
        self.range_cells: dict[str, tuple] = {}
        for row, (pos, name) in enumerate(mlbstats.RANGE_POSITIONS, start=1):
            grid.addWidget(QLabel(f"<b>{pos}</b>   {name}"), row, 0)
            combo = QComboBox()
            combo.setMinimumWidth(500)
            combo.view().setMinimumWidth(640)  # the open list, so a long team + player name isn't clipped
            combo.addItem("(none)", None)
            combo.activated.connect(lambda index, p=pos: self._on_range_pick(p, index))
            grid.addWidget(combo, row, 1)
            cells = []
            for col in range(2, 8):
                label = QLabel()
                label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                label.setMinimumWidth(70 if col < 4 else 90)
                grid.addWidget(label, row, col)
                cells.append(label)
            self.range_combos[pos] = combo
            self.range_cells[pos] = tuple(cells)
        grid.setColumnStretch(1, 1)

        self.range_summary = QLabel("Choose players for the positions above.")
        self.range_summary.setTextFormat(Qt.TextFormat.RichText)
        self.range_summary.setWordWrap(True)
        font = self.range_summary.font()
        font.setPointSize(font.pointSize() + 2)
        self.range_summary.setFont(font)

        clear_btn = QPushButton("Clear all")
        clear_btn.clicked.connect(self._range_clear_all)
        self.range_auto = QCheckBox("Fill the positions when the team changes")
        self.range_auto.setChecked(True)
        self.range_auto.setToolTip("Put the team's usual players at the nine positions when a team or season is chosen "
                                   "(whoever played each for the most innings).\nTurn off to keep your own picks "
                                   "while browsing other teams.")
        note = QLabel(
            "RF/9 = (putouts + assists) \u00d7 9 \u00f7 innings played at the position; where "
            "a player's innings aren't recorded it's RF/G, (putouts + assists) \u00f7 games, "
            "instead, and MLB's figure is put on the same footing.  \u201cMLB\u201d is both leagues "
            "combined, for that player's season.  A chosen player stays in his slot, with his "
            "team, year and both figures, when the team or season above changes.")
        note.setWordWrap(True)

        page = QWidget()
        outer = QVBoxLayout(page)
        top = QHBoxLayout()
        top.addWidget(self.range_picker)
        top.addWidget(self.range_auto, 0, Qt.AlignmentFlag.AlignBottom)
        top.addStretch(1)
        top.addWidget(clear_btn, 0, Qt.AlignmentFlag.AlignBottom)
        outer.addLayout(top)
        outer.addLayout(grid)
        outer.addWidget(self.range_summary)
        outer.addWidget(note)
        outer.addStretch(1)
        return page

    def _on_range_roster(self, data):
        self._range_roster = data  # None if it couldn't be loaded: only locked players remain
        for pos in self.range_combos:
            self._rebuild_range_combo(pos)
        if data is not None:
            self._ensure_range_league(data[2])  # the whole-roster figure needs MLB's positional figures too
        self._range_autofill()
        self._update_range_summary()

    def _range_candidates(self, pos: str) -> list[dict]:
        """The offered roster's players who played ``pos``, busiest first."""
        if self._range_roster is None:
            return []
        rows = [r for r in self._range_roster[3] if r.get("_pos") == pos and r.get("_id") is not None]
        rows.sort(key=lambda r: (-mlbstats.innings_to_outs(r.get("innings", 0)),
                                 -(mlbstats.as_number(r.get("gamesPlayed")) or 0), r.get("_name", "")))
        return rows

    @staticmethod
    def _range_label(name: str, team_name: str, season: int, figures: dict, innings: str) -> str:
        """The same text for every entry in a slot's drop-down, locked player or
        offered alike: name, team and year, games, innings, and range factor."""
        if figures.get("rf_9") is not None:
            rf = f"RF/9 {figures['rf_9']:.2f}"
        elif figures.get("rf_game") is not None:
            rf = f"RF/G {figures['rf_game']:.2f}"
        else:
            rf = "RF n/a"
        return (f"{name}  ({team_name} {season}  \u00b7  {int(figures.get('games') or 0)} G  \u00b7  "
                f"{innings} Inn  \u00b7  {rf})")

    def _rebuild_range_combo(self, pos: str):
        """(Re)list a slot's drop-down: "(none)", the locked player (if any, always kept,
        labelled with his own team and year), then whoever the chosen team and season
        offer at this position."""
        combo, lock = self.range_combos[pos], self._range_slots[pos]
        offered_team, offered_season = (self._range_roster[0], self._range_roster[2]) \
            if self._range_roster else (None, None)
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("(none)", None)
        if lock:
            outs = int(lock["player"]["outs"])  # innings are kept as outs; back to the API's 1375.1 style
            combo.addItem(self._range_label(lock["name"], lock["team_name"], lock["season"],
                                            lock["player"], f"{outs // 3}.{outs % 3}"), ("locked", None))
        for row in self._range_candidates(pos):
            if (lock and lock["pid"] == row["_id"] and lock["team_id"] == offered_team
                    and lock["season"] == offered_season):
                continue  # already there, as the locked entry
            combo.addItem(self._range_label(row.get("_name", "?"), self._range_roster[1], offered_season,
                                            mlbstats.fielding_row_figures(row), row.get("innings") or "0.0"),
                          ("roster", row))
        combo.setCurrentIndex(1 if lock else 0)
        combo.blockSignals(False)

    def _on_range_pick(self, pos: str, index: int):
        data = self.range_combos[pos].itemData(index)
        if data is not None and data[0] == "locked":
            return  # the player already in the slot
        if data is None:
            self._range_slots[pos] = None
        else:
            row = data[1]
            self._range_slots[pos] = self._range_lock(row, pos)
            self._ensure_range_league(self._range_roster[2])
        self._rebuild_range_combo(pos)
        self._update_range_row(pos)
        self._update_range_summary()

    def _range_lock(self, row: dict, pos: str) -> dict:
        """What a slot keeps of the player placed in it: his figures, and the team and season they came from."""
        team_id, team_name, season, _rows = self._range_roster
        return {
            "pid": row["_id"], "name": row.get("_name", "?"), "pos": pos,
            "team_id": team_id, "team_name": team_name, "season": season,
            "player": mlbstats.fielding_row_figures(row),
            "po": mlbstats.as_number(row.get("putOuts")), "a": mlbstats.as_number(row.get("assists")),
            "league": self._range_league.get(season, {}).get(pos),
        }

    def _range_autofill(self):
        """Fill the nine positions with the team's usual defense: whoever played each for the most
        innings (see :func:`mlbstats.assign_positions`), replacing what was there."""
        if not self.range_auto.isChecked() or self._range_roster is None:
            return
        chosen = mlbstats.assign_positions(self._range_roster[3])
        if not chosen:
            return
        for pos in self._range_slots:
            self._range_slots[pos] = None
        for pos, row in chosen.items():
            self._range_slots[pos] = self._range_lock(row, pos)
        self._ensure_range_league(self._range_roster[2])
        for pos in self._range_slots:
            self._rebuild_range_combo(pos)
            self._update_range_row(pos)
        self._update_range_summary()

    def _range_clear_all(self):
        for pos in self._range_slots:
            self._range_slots[pos] = None
            self._rebuild_range_combo(pos)
            self._update_range_row(pos)
        self._update_range_summary()

    def _ensure_range_league(self, season: int):
        """Fetch what all of MLB did at each position in ``season`` (once per season)."""
        if season in self._range_league or season in self._range_loading:
            return
        self._range_league_errors.pop(season, None)
        self._range_loading.add(season)
        ttl = LIVE_TTL if season >= self._season_now else None  # a finished season never changes

        def fn():
            return season, mlbstats.get_league_position_totals(season, ttl)

        self._run_latest(f"range_league_{season}", fn, self._on_range_league,
                         lambda message: self._on_range_league_failed(season, message))

    def _on_range_league(self, data):
        season, totals = data
        self._range_loading.discard(season)
        self._range_league[season] = totals
        for pos, lock in self._range_slots.items():
            if lock and lock["season"] == season and lock["league"] is None:
                lock["league"] = totals[pos]  # kept in the slot from here on
                self._update_range_row(pos)
        self._update_range_summary()

    def _on_range_league_failed(self, season: int, message: str):
        self._range_loading.discard(season)
        self._range_league_errors[season] = message
        self._say("range", f"Range: couldn't get MLB's {season} figures: {message}")
        for pos, lock in self._range_slots.items():
            if lock and lock["season"] == season:
                self._update_range_row(pos)
        self._update_range_summary()

    def _range_result(self, lock: dict):
        """-> (measure text, player RF, MLB RF, ratio) for a slot; each is None where it
        can't be known (yet).  Uses :func:`mlbstats.range_ratio` once MLB's figures are
        in; until then just the player's own."""
        player, league = lock["player"], lock["league"]
        if league is not None:
            result = mlbstats.range_ratio(player, league)
            if result:
                measure, p, l, ratio = result
                return ("RF/9" if measure == "rf9" else "RF/G"), p, l, ratio
            return None, None, None, None
        key, label = ("rf_9", "RF/9") if player.get("rf_9") is not None else ("rf_game", "RF/G")
        return (label, player.get(key), None, None) if player.get(key) is not None else (None, None, None, None)

    def _update_range_row(self, pos: str):
        po_l, a_l, measure_l, player_l, league_l, ratio_l = self.range_cells[pos]
        lock = self._range_slots[pos]
        for label in (po_l, a_l, measure_l, player_l, league_l, ratio_l):
            label.setText("")
            label.setToolTip("")
            label.setStyleSheet("")
        if lock is None:
            return
        po_l.setText("" if lock["po"] is None else f"{lock['po']:.0f}")
        a_l.setText("" if lock["a"] is None else f"{lock['a']:.0f}")
        measure, p, l, ratio = self._range_result(lock)
        if measure is None:
            for label in (measure_l, player_l, league_l, ratio_l):
                label.setText("n/a")
            return
        measure_l.setText(measure)
        player_l.setText(f"{p:.3f}")
        if l is not None:
            league_l.setText(f"{l:.3f}")
            ratio_l.setText(f"{ratio * 100:.1f}%")
            # judged on the figure as shown, so "100.0%" is never painted as a shortfall
            ratio_l.setStyleSheet("font-weight: bold; color: %s;"
                                  % ("#2e9e4f" if round(ratio * 100, 1) >= 100.0 else "#d9534f"))
            ratio_l.setToolTip(f"{lock['name']}'s {p:.3f} \u00f7 all of MLB's {l:.3f} at {pos} in {lock['season']}")
        elif lock["season"] in self._range_league_errors:
            league_l.setText("n/a")
            ratio_l.setText("n/a")
            league_l.setToolTip(self._range_league_errors[lock["season"]])
        else:
            league_l.setText("\u2026")
            ratio_l.setText("\u2026")

    def _range_roster_result(self) -> dict | None:
        """Every fielder of the roster on offer (the picker's team and season) against MLB, as
        :func:`mlbstats.roster_range` -> ``None`` with no roster; else ``{"who", "pct", "players",
        "chances", "expected", "state"}`` where ``pct`` is ``None`` and ``state`` says why ("waiting", "failed"
        or "none": no one could be measured) until there is a figure."""
        if self._range_roster is None:
            return None
        _team_id, team_name, season, rows = self._range_roster
        who = f"{team_name} {season}"
        if season in self._range_league_errors:
            return {"who": who, "pct": None, "state": "failed"}
        league = self._range_league.get(season)
        if league is None:
            return {"who": who, "pct": None, "state": "waiting"}
        got = mlbstats.roster_range(rows, league)
        if got is None:
            return {"who": who, "pct": None, "state": "none"}
        return dict(got, who=who, pct=round(got["ratio"] * 100, 1), state="ok")

    def _update_range_summary(self):
        self._schedule_naturalzone()
        locks = {pos: lock for pos, lock in self._range_slots.items() if lock}
        roster = self._range_roster_result()
        if not locks and roster is None:
            self.range_summary.setText("Choose players for the positions above.")
            return
        headline, details = [], []
        if locks:
            ratios = [r[3] for lock in locks.values() for r in [self._range_result(lock)] if r[3] is not None]
            waiting = len(locks) - len(ratios)
            if ratios:
                pct = round(sum(ratios) / len(ratios) * 100, 1)  # judged on the figure as shown
                colour = "#2e9e4f" if pct >= 100.0 else "#d9534f"
                verdict = ("more range than" if pct > 100.0 else "the same range as" if pct == 100.0
                           else "less range than")
                headline.append(f"Lineup range: <b style='color:{colour}'>{pct:.1f}%</b> of the MLB average "
                                f"\u2014 {verdict} an average MLB defense at these positions")
                detail = (f"Lineup: mean of {len(ratios)} position proportion(s), "
                          f"{sum(ratios):.3f} \u00f7 {len(ratios)};  {len(locks)} of 9 positions filled")
                details.append(detail + (f";  {waiting} still waiting on MLB's figures or without data"
                                         if waiting else "") + ".")
            else:
                headline.append(f"{len(locks)} of 9 positions filled \u2014 waiting on MLB's figures\u2026")
        if roster is not None:
            if roster["pct"] is not None:
                colour = "#2e9e4f" if roster["pct"] >= 100.0 else "#d9534f"
                headline.append(f"Entire {roster['who']} roster: <b style='color:{colour}'>{roster['pct']:.1f}%</b> "
                                "of that average")
                noun = "fielder" if roster["players"] == 1 else "fielders"
                details.append(f"Roster: all {roster['players']} {noun} who played a position, "
                               f"{roster['chances']:,.0f} chances against the {roster['expected']:,.0f} an average MLB "
                               "fielder would have had in the same innings at the same positions.")
            elif roster["state"] == "waiting":
                headline.append(f"Entire {roster['who']} roster: waiting on MLB's figures\u2026")
        text = "  \u00b7  ".join(headline)
        if details:
            text += "<br><span style='font-size:small'>" + "<br>".join(details) + "</span>"
        self.range_summary.setText(text or "Choose players for the positions above.")

    # ---- stadiums tab ------------------------------------------------------------
    SD_ZONE_HEADERS = ["Pos", "Uniform park\n(sq ft)", "This park\n(sq ft)", "Gained\n(sq ft)", "Lost\n(sq ft)",
                       "Net\n(sq ft)", "Change"]

    def _build_stadiums_tab(self) -> QWidget:
        """A club's home park (MLB's posted distances, see :mod:`stadiums`) over the Naturalzone tab's
        uniform park: green where it has ground the uniform park hasn't, red where it lacks ground the
        uniform park has, and whose usual zones grow or shrink by how much.  The park chosen here can
        be used on the Naturalzone tab."""
        self._sd_started = False
        self._sd_parks: list[dict] = []
        self._sd_park: dict | None = None
        self.sd_season_spin = _YearSpin()
        self.sd_season_spin.setRange(1876, 2100)
        self.sd_season_spin.setKeyboardTracking(False)
        self.sd_season_spin.setToolTip("Typing a year needs Enter (or click elsewhere) to take effect.")
        self._sd_season_debounce = QTimer(self, singleShot=True, interval=250)
        self._sd_season_debounce.timeout.connect(lambda: self._sd_load())
        self.sd_season_spin.valueChanged.connect(lambda _v: self._sd_season_debounce.start())
        season_prev, season_next = QPushButton("\u25c0"), QPushButton("\u25b6")
        for btn, step in ((season_prev, -1), (season_next, 1)):
            btn.setFixedWidth(32)
            btn.clicked.connect(lambda _c, d=step: self.sd_season_spin.stepBy(d))
        self.sd_team_combo = QComboBox()
        self.sd_team_combo.setMinimumWidth(240)
        self.sd_team_combo.currentIndexChanged.connect(lambda _i: self._sd_show())
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Pull the parks again, ignoring the cache.")
        refresh.clicked.connect(lambda: self._sd_load(force=True))

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Season"))
        bar.addWidget(season_prev)
        bar.addWidget(self.sd_season_spin)
        bar.addWidget(season_next)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Team"))
        bar.addWidget(self.sd_team_combo)
        bar.addStretch(1)
        bar.addWidget(refresh)

        self.sd_title = QLabel("Loading the parks\u2026")
        self.sd_title.setWordWrap(True)
        self.sd_title.setTextFormat(Qt.TextFormat.RichText)
        self.sd_source = QLabel()
        self.sd_source.setWordWrap(True)
        self.sd_source.setTextFormat(Qt.TextFormat.RichText)
        self.sd_source.setOpenExternalLinks(True)
        self.sd_field = StadiumField()
        self.sd_summary = QLabel()
        self.sd_summary.setWordWrap(True)
        self.sd_summary.setTextFormat(Qt.TextFormat.RichText)

        def table(headers):
            t = QTableWidget(0, len(headers))
            t.setHorizontalHeaderLabels(headers)
            t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            t.verticalHeader().setVisible(False)
            t.setAlternatingRowColors(True)
            t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            return t
        self.sd_marks = table(["Mark", "Angle", "This park\n(ft)", "Uniform park\n(ft)", "Difference\n(ft)"])
        self.sd_zones = table(self.SD_ZONE_HEADERS)
        self.sd_zones.setToolTip("Each fielder's usual ground (the Total Zone zones he owns) in the uniform park and\n"
                                 "in this one: what the park adds beyond the uniform fence and what it takes away.")
        tables = QTabWidget()
        tables.addTab(self.sd_marks, "Distances")
        tables.addTab(self.sd_zones, "Usual zones")

        note = QLabel(
            "Dimensions come from Wikipedia where its park article gives them for the season (transcribed by hand "
            "for 82 parks, era by era where the article dates its changes; text CC BY-SA 4.0), otherwise from MLB's "
            "posted distances (the Stats API's venue field info), which keep one set per park.  The line above the "
            "field says which, and links the article: consult it, and its references, for further sourcing.  The "
            "lines are put at 45\u00b0, the alleys at 22.5\u00b0, other marks where their names place them, and "
            "the fence between them is joined straight in angle and distance, as the uniform park's is \u2014 an "
            "outline of the posted distances, not the wall's every corner.  The "
            "uniform park is the Naturalzone tab's: the Retrosheet location diagram's, 330 ft down the lines and "
            "405 ft to centre.  Its zones keep their shapes here, the deepest ones running to this park's fence and "
            "the foul strips to the end of its lines.")
        note.setWordWrap(True)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.sd_title)
        ll.addWidget(self.sd_source)
        ll.addWidget(self.sd_field, 1)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.sd_summary)
        rl.addWidget(tables, 1)
        rl.addWidget(note)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([560, 440])

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(bar)
        outer.addWidget(splitter, 1)
        return page

    def _sd_tab_shown(self):
        if not self._sd_started:
            self._sd_started = True
            now = max(getattr(self, "_season_now", 0) or 0, mlbstats.current_season())
            self.sd_season_spin.blockSignals(True)
            self.sd_season_spin.setMaximum(now)
            self.sd_season_spin.setValue(now)
            self.sd_season_spin.blockSignals(False)
            self._sd_load()

    def _sd_load(self, force: bool = False):
        """The season's clubs and their home parks."""
        season = self.sd_season_spin.value()
        ttl = 0 if force else (24 * 3600 if season >= mlbstats.current_season() else None)
        self.sd_title.setText(f"Loading {season}'s parks\u2026")
        self._run_latest("sd", lambda: (season, stadiums.get_home_parks(season, ttl=ttl)), self._on_sd_parks,
                         lambda m: (self.sd_title.setText(f"Couldn't load {season}'s parks: {m}"),
                                    self._say("sd", f"Stadiums: {m}")))

    def _on_sd_parks(self, data):
        season, parks = data
        if season != self.sd_season_spin.value():
            return
        was = self.sd_team_combo.currentData()
        if was is None and self.team_combo.count():
            was = self.team_combo.currentData()  # to start with, the Stats tab's team
        self._sd_parks = parks
        self.sd_team_combo.blockSignals(True)
        self.sd_team_combo.clear()
        for i, park in enumerate(parks):
            self.sd_team_combo.addItem(park["team"], park["team_id"])
        at = self.sd_team_combo.findData(was)
        self.sd_team_combo.setCurrentIndex(at if at >= 0 else 0)
        self.sd_team_combo.blockSignals(False)
        if not parks:
            self.sd_title.setText(f"No clubs found for {season}.")
        self._sd_show()

    def _sd_show(self):
        i = self.sd_team_combo.currentIndex()
        if not (0 <= i < len(self._sd_parks)):
            return
        park = dict(self._sd_parks[i], season=self.sd_season_spin.value())
        marks = park["marks"]
        self.sd_source.setText(self._sd_source_text(park))
        if park["fence"] is None:
            self._sd_park = None
            self.sd_title.setText(f"<b>{park['venue']}</b> ({park['team']}, {park['season']}) \u2014 no "
                                  "dimensions for this park this season" + (" beyond " + ", ".join(
                                      f"{label} {feet:.0f}" for label, _a, feet in marks) if marks else "") + ".")
            self.sd_field.set_park(None, [])
            self.sd_summary.setText("")
            self.sd_marks.setRowCount(0)
            self.sd_zones.setRowCount(0)
            self._sd_park_changed()
            return
        self.sd_title.setText(f"<b>{park['venue']}</b> ({park['team']}, {park['season']})  \u00b7  "
                              + "  ".join(f"{label} {feet:.0f}" for label, _a, feet in marks)
                              + "  \u2014 working it out\u2026")
        fence = park["fence"]

        def fn():
            uniform = naturalzone.average_zones()["areas"]
            here = naturalzone.average_zones(fence)["areas"]
            naturalzone.zone_drawing(fence)
            return park, marks, uniform, here, naturalzone.compare_park(fence)
        self._run_latest("sd-park", fn, self._on_sd_park, lambda m: self._say("sd", f"Stadiums: {m}"))

    def _on_sd_park(self, data):
        park, marks, uniform, here, cmp = data
        i = self.sd_team_combo.currentIndex()
        if not (0 <= i < len(self._sd_parks)) or self._sd_parks[i]["team_id"] != park["team_id"] \
                or park["season"] != self.sd_season_spin.value():
            return
        self._sd_park = park
        self.sd_title.setText(f"<b>{park['venue']}</b> ({park['team']}, {park['season']})  \u00b7  "
                              + "  ".join(f"{label} {feet:.0f}" for label, _a, feet in marks))
        self.sd_field.set_park(park["fence"], marks)

        def item(text, right=True, bold=False, colour=None):
            it = QTableWidgetItem(text)
            if right:
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if bold:
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            if colour:
                it.setForeground(QBrush(QColor(colour)))
            return it
        sign = lambda v: None if round(v) == 0 else (WON_COLOR if v > 0 else LOST_COLOR)

        t = self.sd_marks
        t.setRowCount(len(marks))
        for row, (label, angle, feet) in enumerate(marks):
            uni = naturalzone.fence(angle)
            for col, cell in enumerate((item(label, False, True), item(f"{angle:+g}\u00b0"), item(f"{feet:.0f}"),
                                        item(f"{uni:.0f}"),
                                        item(f"{round(feet - uni):+d}" if round(feet - uni) else "0", colour=sign(feet - uni)))):
                t.setItem(row, col, cell)

        P = naturalzone.POSITIONS
        z = self.sd_zones
        z.setRowCount(len(P) + 1)
        for row, pos in enumerate(P + ["Field"]):
            if pos == "Field":
                u, h = cmp["areas"]["uniform"], cmp["areas"]["park"]
                g, l = sum(cmp["gained"].values()), sum(cmp["lost"].values())
            else:
                u, h, g, l = uniform[pos], here[pos], cmp["gained"][pos], cmp["lost"][pos]
            cells = [item(pos, False, True), item(f"{u:,.0f}"), item(f"{h:,.0f}"),
                     item(f"{g:,.0f}" if g else "", colour=WON_COLOR if g else None),
                     item(f"{l:,.0f}" if l else "", colour=LOST_COLOR if l else None),
                     item(f"{h - u:+,.0f}" if h != u else "", colour=sign(h - u)),
                     item(f"{(h / u - 1) * 100:+.1f}%" if u and h != u else "", colour=sign(h - u))]
            for col, cell in enumerate(cells):
                z.setItem(row, col, cell)

        u, h = cmp["areas"]["uniform"], cmp["areas"]["park"]
        g, l = sum(cmp["gained"].values()), sum(cmp["lost"].values())
        of = {pos: here[pos] / uniform[pos] - 1 for pos in ("LF", "CF", "RF")}
        self.sd_summary.setText(
            f"<b>{park['venue']}</b> covers <b>{h:,.0f} sq ft</b> of fair ground and foul strips against the uniform "
            f"park's {u:,.0f} ({(h / u - 1) * 100:+.1f}%): "
            f"<b style='color:{WON_COLOR}'>{g:,.0f} sq ft gained</b>, "
            f"<b style='color:{LOST_COLOR}'>{l:,.0f} sq ft lost</b>.  Outfielders' usual ground: "
            + ", ".join(f"{pos} {v * 100:+.1f}%" for pos, v in of.items())
            + ".  Tick <i>Play them in the Stadiums tab's park</i> on the Naturalzone tab to fit the natural "
              "zones to it.")
        self._sd_park_changed()

    @staticmethod
    def _sd_source_text(park: dict) -> str:
        """Where a park's distances come from, with Wikipedia's links."""
        small = "<span style='font-size:small'>{}</span>"
        if park["source"] == "wikipedia":
            w = park["wiki"]
            also = "".join(f" and <a href='{a['url']}'>{html.escape(a['article'])}</a> "
                           f"(<a href='{a['revision_url']}'>revision</a>)" for a in w.get("also", []))
            walls = "".join(f"  Wall: {html.escape(x['label'])}, {html.escape(x['note'])}."
                            for x in w.get("walls", []))
            if walls:
                walls += "  Elsewhere the wall's height isn't given (the Statcast tab draws 8 ft)."
            return small.format(
                f"<b>Source:</b> Wikipedia, <a href='{w['url']}'>{html.escape(w['article'])}</a> "
                f"(<a href='{w['revision_url']}'>the revision transcribed</a>, CC BY-SA 4.0){also} \u2014 "
                f"{html.escape(w['note'])}.{walls}  For further sourcing, consult the Wikipedia articles and their "
                "references.")
        if park["source"] == "mlb":
            return small.format("<b>Source:</b> MLB Stats API (the venue's posted distances: one set per park, "
                                "not by season).  Wikipedia has nothing for this park this season.")
        return small.format("<b>Source:</b> none \u2014 neither Wikipedia nor MLB has enough distances "
                            "for this park this season.")

    def _sd_park_changed(self):
        """Tell the Naturalzone tab which park the Stadiums tab shows."""
        park = self._sd_park
        self.nz_use_park.setEnabled(park is not None)
        self.nz_use_park.setText(f"Play them in {park['venue']} ({park['season']}, from the Stadiums tab)"
                                 if park else "Play them in the Stadiums tab's park")
        if self.nz_use_park.isChecked():
            self._schedule_naturalzone()

    # ---- naturalzone tab ----------------------------------------------------------
    NZ_FIELDER_HEADERS = ["Pos", "Player", "Range", "Usual zone\n(sq ft)", "Natural zone\n(sq ft)", "Reach\n(ft)",
                          "Gap in his zone", "Made up by others", "Reaches into"]

    def _build_naturalzone_tab(self) -> QWidget:
        """The Range tab's nine fielders on the Total Zone map of the field (see
        :mod:`naturalzone`): each one's zone grown or shrunk by his range against MLB's at his
        position, showing whose extra range could make up for whose shortfall, and the gaps
        no one is likely to reach."""
        self.nz_source = QLabel()
        self.nz_source.setWordWrap(True)
        self.nz_fix_pc = QCheckBox("Keep the pitcher's and catcher's zones as they are")
        self.nz_fix_pc.setChecked(False)
        self.nz_fix_pc.setToolTip("A pitcher's and a catcher's range factors are mostly strikeouts, comebackers and\n"
                                  "pop-ups rather than ground covered: check this to leave their zones as they are.")
        self.nz_fix_pc.toggled.connect(lambda _on: self._schedule_naturalzone())
        self.nz_use_park = QCheckBox("Play them in the Stadiums tab's park")
        self.nz_use_park.setEnabled(False)
        self.nz_use_park.setToolTip("Lay the field out in the park chosen on the Stadiums tab instead of the uniform\n"
                                    "park: its fence (from MLB's posted distances) bounds the deepest zones and the\n"
                                    "foul strips, so each fielder's usual ground -- and his natural zone -- is his\n"
                                    "in that park.")
        self.nz_use_park.toggled.connect(lambda _on: self._schedule_naturalzone())
        self._nz_timer = QTimer(self, singleShot=True, interval=150)
        self._nz_timer.timeout.connect(self._reload_naturalzone)
        self._nz_players: dict = {}

        self.nz_field = NaturalZoneField()
        self.nz_summary = QLabel("Fill the nine positions on the Range tab.")
        self.nz_summary.setTextFormat(Qt.TextFormat.RichText)
        self.nz_summary.setWordWrap(True)

        def table(headers):
            t = QTableWidget(0, len(headers))
            t.setHorizontalHeaderLabels(headers)
            t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            t.verticalHeader().setVisible(False)
            t.setAlternatingRowColors(True)
            t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            return t
        self.nz_fielders = table(self.NZ_FIELDER_HEADERS)
        self.nz_cover = table(["Usual zone"] + naturalzone.POSITIONS + ["Nobody"])
        self.nz_cover.setToolTip("How much of each fielder's usual zone (rows) each fielder's natural zone\n"
                                 "takes in (columns) -- a row can add up to more than 100% where two reach\n"
                                 "the same ground.  Nobody: the gap no fielder is likely to reach.")
        self.nz_contig = table([""] + naturalzone.POSITIONS)
        self.nz_contig.setToolTip("Feet of border each two fielders' usual zones share on the Total Zone map\n"
                                  "(blank: not contiguous).  Extra range spills across these borders.")
        tables = QTabWidget()
        tables.addTab(self.nz_fielders, "Fielders")
        tables.addTab(self.nz_cover, "Who covers each zone")
        tables.addTab(self.nz_contig, "Contiguity")

        note = QLabel(
            "Usual zones follow Total Zone's geography: the Project Scoresheet / Retrosheet batted-ball location "
            "zones, drawn to scale for a park 330 ft down the lines and 405 ft to centre.  A zone named for one "
            "fielder (6, 6S, 6D) is his; one named for two (56, 78XD) is split down its middle.  Each fielder's "
            "natural zone is his usual zone grown or shrunk along its borders with his teammates until its area is "
            "his range factor ÷ MLB's (Range tab) times the usual area.  The zones are fitted one by one, so "
            "they can overlap, or leave gaps no one is likely to reach.")
        note.setWordWrap(True)

        caveat = QLabel(
            "<i>The model assumes one uniform field size for every park, and that a fielder's range factor "
            "against MLB's corresponds uniformly to the share of his usual ground he covers \u2014 so its "
            "figures are indices under those assumptions, not literal claims about ground covered or balls "
            "reached.</i>")
        caveat.setWordWrap(True)
        # left: where the nine come from, the caveat and the field; right: the figures
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.nz_source)
        ll.addWidget(caveat)
        ll.addWidget(self.nz_field, 1)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.nz_fix_pc)
        rl.addWidget(self.nz_use_park)
        rl.addWidget(self.nz_summary)
        rl.addWidget(tables, 1)
        rl.addWidget(note)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([560, 440])  # the field gets the larger share to start with

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addWidget(splitter, 1)
        self._schedule_naturalzone()
        return page

    def _schedule_naturalzone(self):
        if hasattr(self, "_nz_timer"):
            self._nz_timer.start()

    def _reload_naturalzone(self):
        """Gather the Range tab's nine and work out their natural zones (in the background: the
        first time builds the field's grid)."""
        players = {}
        for pos, lock in self._range_slots.items():
            if lock:
                players[pos] = (lock["name"], self._range_result(lock)[3], lock["team_name"], lock["season"])
        self._nz_players = players
        teams = sorted({f"{team} {season}" for _n, _r, team, season in players.values()})
        measured = sum(1 for v in players.values() if v[1] is not None)
        if not players:
            self.nz_source.setText("<b>No fielders yet</b> — choose them on the Range tab.  "
                                   "Until then this shows the usual zones.")
        else:
            self.nz_source.setText(f"<b>From the Range tab:</b> {', '.join(teams)}  ·  "
                                   f"{measured} of 9 positions measured against MLB"
                                   + ("" if measured == len(players) else "  (still waiting on MLB's figures)"))
        ratios = {pos: v[1] for pos, v in players.items()}
        fixed = ("P", "C") if self.nz_fix_pc.isChecked() else ()
        sd = getattr(self, "_sd_park", None)
        park = sd["fence"] if sd and self.nz_use_park.isChecked() else None
        if park is not None:
            self.nz_source.setText(self.nz_source.text() + f"<br><b>Park:</b> {sd['venue']} ({sd['season']}, "
                                   "from the Stadiums tab; distances from "
                                   + ("Wikipedia" if sd["source"] == "wikipedia" else "MLB")
                                   + ") \u2014 the uniform park's fence dotted")

        def fn():
            result = naturalzone.natural_zones(ratios, fixed, park)
            naturalzone.zone_drawing(park)  # built once, here off the GUI thread
            if park is not None:
                naturalzone.compare_park(park)
            return result, naturalzone.natural_drawing(result)

        self._run_latest("nz", fn, self._on_naturalzone, lambda m: self._say("nz", f"Naturalzone: {m}"))

    def _on_naturalzone(self, data):
        result, outlines = data
        players = self._nz_players
        self.nz_field.set_result(result, outlines, {pos: (v[0], v[1]) for pos, v in players.items()})
        P = naturalzone.POSITIONS
        fixed = self.nz_fix_pc.isChecked()

        def item(text, right=True, bold=False, colour=None, tip=""):
            it = QTableWidgetItem(text)
            if right:
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if bold:
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            if colour:
                it.setForeground(QBrush(QColor(colour)))
            if tip:
                it.setToolTip(tip)
            return it
        pct = lambda v: f"{v * 100:.1f}%" if v >= 0.0005 else ""
        share = lambda v: f"{v * 100:.1f}%" if v >= 0.0005 else "<0.1%"
        by_share = lambda d: sorted(d.items(), key=lambda fv: (-fv[1], fv[0]))

        def made_up_text(zone: str) -> str:
            """How a zone's given-up ground is made up: by one fielder alone (no overlap), and where
            two or more fielders' zones overlap -- "1.7% alone (LF 1.2%, SS 0.5%); 0.2% overlapping (LF+SS)"."""
            alone, shared = result["made_up_alone"][zone], result["made_up_shared"][zone]
            parts = []
            if alone:
                parts.append(f"{share(sum(alone.values()))} alone ("
                             + ", ".join(f"{f} {share(v)}" for f, v in by_share(alone)) + ")")
            if shared:
                groups = by_share({"+".join(who): v for who, v in shared.items()})
                parts.append(f"{share(sum(shared.values()))} overlapping ("
                             + ", ".join(f"{who} {share(v)}" if len(groups) > 1 else who for who, v in groups) + ")")
            return "; ".join(parts)

        t = self.nz_fielders
        t.setRowCount(len(P))
        for row, pos in enumerate(P):
            name, ratio = (players.get(pos) or (None, None))[:2]
            held = fixed and pos in ("P", "C")
            made = made_up_text(pos)
            into = ", ".join(f"{z} {share(result['coverage'][z][pos])}" for z in P
                             if z != pos and result["coverage"][z][pos] >= 0.005)
            reach = result["reach"][pos]
            colour = None if ratio is None else (WON_COLOR if round(ratio * 100, 1) >= 100.0 else LOST_COLOR)
            cells = [item(pos, False, True), item(name or "", False),
                     item("" if ratio is None else f"{ratio * 100:.1f}%", colour=colour),
                     item(f"{result['avg_areas'][pos]:,.0f}"),
                     item(f"{result['areas'][pos]:,.0f}"),
                     item("held" if held else f"{reach:+.1f}",
                          tip="How far his zone's borders with his teammates move out (+) or in (−)."),
                     item(pct(result["gap"][pos]), colour=LOST_COLOR if result["gap"][pos] >= 0.0005 else None),
                     item(made, False), item(into, False)]
            for col, cell in enumerate(cells):
                t.setItem(row, col, cell)

        c = self.nz_cover
        c.setRowCount(len(P))
        for row, zone in enumerate(P):
            c.setItem(row, 0, item(zone, False, True))
            for col, f in enumerate(P, start=1):
                v = result["coverage"][zone][f]
                c.setItem(row, col, item(pct(v), bold=f == zone,
                                         colour=None if f == zone or v < 0.0005 else WON_COLOR))
            gap = result["gap"][zone]
            c.setItem(row, len(P) + 1, item(pct(gap), colour=LOST_COLOR if gap >= 0.0005 else None))

        g = self.nz_contig
        g.setRowCount(len(P))
        for row, a in enumerate(P):
            g.setItem(row, 0, item(a, False, True))
            for col, b in enumerate(P, start=1):
                feet = result["contiguity"][a][b]
                g.setItem(row, col, item(f"{feet:.0f} ft" if feet else ""))

        totals = result["totals"]
        if not any(v[1] is not None for v in players.values()):
            self.nz_summary.setText("Fill the nine positions on the Range tab to see how their range reshapes "
                                    "the usual zones.")
            return
        lines = []
        if totals["gap"]:
            worst = sorted((z for z in P if result["gap"][z] >= 0.0005), key=lambda z: -result["gap"][z])
            lines.append(f"<b style='color:{LOST_COLOR}'>Gaps: {totals['gap']:,.0f} sq ft</b> "
                         f"({totals['gap'] / totals['field'] * 100:.1f}% of the field) no one is likely to reach"
                         " — " + ", ".join(f"{result['gap'][z] * 100:.1f}% of {z}'s zone" for z in worst[:4]))
        else:
            lines.append("<b>No gaps</b> — every part of the field is still someone's.")
        alone = {z: sum(result["made_up_alone"][z].values()) for z in P}
        made = sorted((z for z in P if alone[z] >= 0.0005), key=lambda z: -alone[z])
        if totals["made_up_alone"]:
            lines.append(f"<b style='color:{WON_COLOR}'>Made up alone: {totals['made_up_alone']:,.0f} sq ft</b> "
                         f"({totals['made_up_alone'] / totals['field'] * 100:.1f}% of the field) of ground given up, "
                         "covered by one neighbour with no overlap"
                         + (f" ({totals['made_up_shared']:,.0f} sq ft more where two overlap)"
                            if totals["made_up_shared"] else "")
                         + " \u2014 " + ", ".join(f"{alone[z] * 100:.1f}% of {z}'s zone" for z in made[:4]))
        else:
            lines.append(f"<b style='color:{WON_COLOR}'>Nothing made up</b> \u2014 "
                         "no fielder gave up ground a neighbour reaches.")
        if totals["overlap"]:
            most = sorted((z for z in P if result["overlap"][z] >= 0.0005), key=lambda z: -result["overlap"][z])
            lines.append(f"<b style='color:{NaturalZoneField.OVERLAP}'>Overlaps: {totals['overlap']:,.0f} sq ft</b> "
                         f"({totals['overlap'] / totals['field'] * 100:.1f}% of the field) two fielders now both reach"
                         " \u2014 " + ", ".join(f"{result['overlap'][z] * 100:.1f}% of {z}'s zone" for z in most[:4]))
        else:
            lines.append(f"<b style='color:{NaturalZoneField.OVERLAP}'>No overlaps</b> \u2014 "
                         "no two fielders reach the same ground.")
        self.nz_summary.setText("<br>".join(lines))

    # ---- safe/out tab -------------------------------------------------------------
    # (header, key in a row, format): the per-inning outcomes, then the comparison ratios
    SAFE_OUT_COLUMNS = [
        ("Team", "abbrev", "text"), ("Lg", "lg", "text"), ("Innings", "innings", "inn"),
        ("Outs\nper\ninning", "outs_inn", "2"), ("On a base\n(reached 1st)\nper inning", "on_base_inn", "3"),
        ("Reached\n2nd per\ninning", "second_inn", "3"), ("Reached\n3rd per\ninning", "third_inn", "3"),
        ("Scored\nper\ninning", "scored_inn", "3"),
        ("On a\nbase to\nouts", "on_base_outs", "3"), ("% of\nMLB", "on_base_outs_pct", "pct"),
        ("Scored\nto\nouts", "scored_outs", "3"), ("% of\nMLB", "scored_outs_pct", "pct"),
        ("Scored\nto on a\nbase", "scored_on_base", "3"), ("% of\nMLB", "scored_on_base_pct", "pct"),
    ]

    def _build_safe_out_tab(self) -> QWidget:
        """The Speed tab's companion at the team level: for every club in a season, how often per
        inning its players got on a base, reached second and third, and scored -- however they got
        there -- against the three outs every inning costs, from the same Retrosheet play-by-play
        (see :func:`retrosheet.parse_team_running`)."""
        self.so_season_spin = _YearSpin()
        self.so_season_spin.setRange(1901, 2100)  # set to the last completed season once the app knows it
        self.so_season_spin.setKeyboardTracking(False)
        self.so_season_spin.setToolTip("Typing a year needs Enter (or click elsewhere) to take effect.")
        self._so_debounce = QTimer(self, singleShot=True, interval=250)
        self._so_debounce.timeout.connect(self._reload_safe_out)
        self.so_season_spin.valueChanged.connect(lambda _v: self._so_debounce.start())
        about = QPushButton("About these figures\u2026")
        about.setToolTip("What each column counts, and how")
        about.clicked.connect(self._show_safe_out_about)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Season"))
        bar.addWidget(self.so_season_spin)
        bar.addStretch(1)
        bar.addWidget(about)

        self.so_summary = QLabel()
        self.so_summary.setTextFormat(Qt.TextFormat.RichText)
        self.so_summary.setWordWrap(True)
        font = self.so_summary.font()
        font.setPointSize(font.pointSize() + 1)
        self.so_summary.setFont(font)
        self.so_status = QLabel()
        self.so_status.setWordWrap(True)
        self.so_status.setVisible(False)

        def table(side: str) -> QTableWidget:
            t = QTableWidget(0, len(self.SAFE_OUT_COLUMNS))
            t.setHorizontalHeaderLabels([h for h, _k, _f in self.SAFE_OUT_COLUMNS])
            t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            t.verticalHeader().setVisible(False)
            t.setAlternatingRowColors(True)
            t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            t.horizontalHeader().setSortIndicatorShown(True)
            t.horizontalHeader().setHighlightSections(False)  # a selected row doesn't embolden the headings
            t.horizontalHeader().sectionClicked.connect(lambda col, s=side: self._sort_safe_out(s, col))
            t.setColumnHidden(0, True)  # the team is shown in the frozen column beside it
            t.setHorizontalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
            return t

        def frozen(side: str) -> QTableWidget:
            """The Team column, kept in place at the left while the figures scroll sideways."""
            t = QTableWidget(0, 1)
            t.setHorizontalHeaderLabels([self.SAFE_OUT_COLUMNS[0][0]])
            t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            t.verticalHeader().setVisible(False)
            t.setAlternatingRowColors(True)
            t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            t.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            t.horizontalHeader().setSortIndicatorShown(True)
            t.horizontalHeader().setHighlightSections(False)
            t.horizontalHeader().sectionClicked.connect(lambda _col, s=side: self._sort_safe_out(s, 0))
            return t
        # batting: what each club's players did; defense: what each club allowed its opponents
        self.so_tables = {"batting": table("batting"), "defense": table("defense")}
        self.so_frozen = {"batting": frozen("batting"), "defense": frozen("defense")}
        for side in self.so_tables:
            self._link_frozen(self.so_frozen[side], self.so_tables[side])
        scored = self._safe_out_column("scored_inn")
        self._so_sort = {"batting": (scored, Qt.SortOrder.DescendingOrder),   # the most runs first
                         "defense": (scored, Qt.SortOrder.AscendingOrder)}    # the fewest allowed first
        self._so_data = None

        def heading(text: str) -> QLabel:
            label = QLabel(text)
            label.setWordWrap(True)
            return label
        batting_box = QWidget()
        bl = QVBoxLayout(batting_box)
        bl.addWidget(heading("<b>Offensive Safe/Out</b> \u2014 what each club's batters and runners did"))
        bl.addLayout(self._frozen_grid("batting"), 1)
        defense_box = QWidget()
        dl = QVBoxLayout(defense_box)
        dl.addWidget(heading("<b>Defensive Safe/Out</b> \u2014 what each club allowed the teams it played"))
        dl.addLayout(self._frozen_grid("defense"), 1)
        tables = QTabWidget()  # a page each: what the club did, and what it allowed
        tables.addTab(batting_box, "Offensive")
        tables.addTab(defense_box, "Defensive")

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(bar)
        outer.addWidget(self.so_summary)
        outer.addWidget(self.so_status)
        outer.addWidget(tables, 1)
        return page

    def _frozen_grid(self, side: str) -> QGridLayout:
        """The frozen Team column beside its table, the sideways scrollbar above the table only."""
        grid = QGridLayout()
        grid.setSpacing(0)
        grid.addWidget(_top_scrollbar(self.so_tables[side]), 0, 1)
        grid.addWidget(self.so_frozen[side], 1, 0)
        grid.addWidget(self.so_tables[side], 1, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)
        return grid

    @staticmethod
    def _link_frozen(frozen: QTableWidget, main: QTableWidget):
        """Keep a frozen column and its table in step: scrolling up and down, and the row selected."""
        main.verticalScrollBar().valueChanged.connect(frozen.verticalScrollBar().setValue)
        frozen.verticalScrollBar().valueChanged.connect(main.verticalScrollBar().setValue)

        def follow(source: QTableWidget, target: QTableWidget):
            def on_change(row, _col, _prev_row, _prev_col):
                if row >= 0 and target.currentRow() != row:
                    target.blockSignals(True)
                    target.selectRow(row)
                    target.blockSignals(False)
            source.currentCellChanged.connect(on_change)
        follow(main, frozen)
        follow(frozen, main)

    SAFE_OUT_ABOUT = (
        "<p><b>Offensive Safe/Out</b> counts what a club's batters and runners did; <b>Defensive Safe/Out</b> "
        "counts the same for the teams it played, credited to it in the field \u2014 its innings are innings in "
        "the field, and its scored are runs allowed.  MLB's line is the same in both tables.</p>"
        "<p><b>On a base (reached 1st)</b>: every time a player reached first safely, however he got there \u2014 "
        "hit, walk, hit by pitch, error, fielder's choice or force play (the batter safe at first on a force has "
        "reached it; the runner forced out hasn't reached second).</p>"
        "<p><b>Reached 2nd / 3rd</b>: each trip round the bases counts once at every base up to the furthest it "
        "got, so a double counts at first and second and a home run at every base.  <b>Scored</b>: runs.  The "
        "extra-innings runner put on second (2020 on) isn't counted as reaching first or second, only from third "
        "on.</p>"
        "<p><b>Innings</b> are counted as pitchers' are, outs \u00f7 3, so outs per inning is always 3 (a walk-off "
        "or rain-shortened inning counts as part of one).</p>"
        "<p>The last columns compare the per-inning figures: <b>on a base to outs</b>, <b>scored to "
        "outs</b>, and <b>scored to on a base</b> (the share of baserunners who scored), each followed by "
        "its <b>% of MLB</b> \u2014 the club's ratio \u00f7 all of MLB's that season.  Offensively, at or above "
        "100% is green; defensively, where these are what a club allowed, at or below 100% is.</p>"
        "<p>Click a column heading to sort by it.  Regular season only.  Play-by-play from Retrosheet "
        "(retrosheet.org), obtained free of charge and copyrighted by Retrosheet.</p>")

    def _show_safe_out_about(self):
        """The Safe/Out tab's explanation, in a window of its own (one at a time; it stays open
        alongside the tab)."""
        dialog = getattr(self, "_so_about", None)
        if dialog is None:
            dialog = self._so_about = QDialog(self)
            dialog.setWindowTitle("Safe/Out \u2014 about these figures")
            text = QLabel(self.SAFE_OUT_ABOUT)
            text.setWordWrap(True)
            text.setTextFormat(Qt.TextFormat.RichText)
            text.setFixedWidth(560)
            text.setMinimumHeight(text.heightForWidth(560) + 8)  # all of it, wrapped
            close = QPushButton("Close")
            close.clicked.connect(dialog.close)
            lay = QVBoxLayout(dialog)
            lay.addWidget(text)
            row = QHBoxLayout()
            row.addStretch(1)
            row.addWidget(close)
            lay.addLayout(row)
            dialog.adjustSize()  # tall enough for all of the text
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _safe_out_column(self, key: str) -> int:
        return next(i for i, (_h, k, _f) in enumerate(self.SAFE_OUT_COLUMNS) if k == key)

    def _reload_safe_out(self):
        season = self.so_season_spin.value()
        ttl = LIVE_TTL if season >= self._season_now else None
        if not mlbstats.retro_team_is_cached(season):
            self.so_status.setText(f"Getting Retrosheet's {season} play-by-play (once, a few seconds)…")
            self.so_status.setVisible(True)
        self._run_latest("so_load", lambda: (season, mlbstats.get_safe_out(season, ttl)),
                         self._on_safe_out, self._on_safe_out_failed)

    def _on_safe_out_failed(self, message: str):
        season = self.so_season_spin.value()
        if season >= self._season_now:
            message += f"  Retrosheet publishes a season after it ends; the last completed season is {self._season_now - 1}."
        self._so_data = None
        for t in list(self.so_tables.values()) + list(self.so_frozen.values()):
            t.setRowCount(0)
        self.so_summary.setText("")
        self.so_status.setText(f"Couldn't get {season}: {message}")
        self.so_status.setVisible(True)

    SAFE_OUT_RATIOS = (("on_base_outs", "on a base to outs"), ("scored_outs", "scored to outs"),
                       ("scored_on_base", "scored to on a base"))

    def _on_safe_out(self, data):
        season, result = data
        # each comparison ratio as a percentage of MLB's (MLB's own line is 100%)
        for row in result["teams"] + result["allowed"] + [result["mlb"]]:
            for key, _label in self.SAFE_OUT_RATIOS:
                mine, mlb = row.get(key), result["mlb"].get(key)
                row[key + "_pct"] = mine / mlb * 100 if mine is not None and mlb else None
        self._so_data = (season, result)
        self.so_status.setVisible(False)
        m = result["mlb"]
        fmt = lambda v: "" if v is None else f"{v:.3f}"
        lines = [f"<b>{season}, all of MLB</b>: per inning, 3 outs against <b>{fmt(m['on_base_inn'])}</b> on a base "
                 f"({fmt(m['second_inn'])} reaching second, {fmt(m['third_inn'])} third) and <b>{fmt(m['scored_inn'])}</b> "
                 f"scoring — on a base to outs {fmt(m['on_base_outs'])}, scored to outs {fmt(m['scored_outs'])}, "
                 f"scored to on a base {fmt(m['scored_on_base'])} ({m['scored_on_base'] * 100:.1f}% of baserunners scored)."]
        # (clubs with only a sample of their games are marked * below instead)
        whole = [t for t in result["teams"] if t["official_runs"] is not None and t.get("sample") is None]
        short = [t for t in whole + [t for t in result["allowed"] if t["official_runs"] is not None
                                     and t.get("sample") is None] if t["runs"] != t["official_runs"]]
        if short:
            runs, official = sum(t["runs"] for t in whole), sum(t["official_runs"] for t in whole)
            lines.append(f"<span style='color:{LIVE_COLOR}'>Retrosheet's {season} play-by-play doesn't account for "
                         f"every run: {runs:,} of the {official:,} on record (some games' accounts are "
                         "missing or reconstructed), so these figures are a little low.</span>")
        sampled = sorted({(t["name"], t["sample"]) for t in result["teams"] if t.get("sample") is not None})
        if sampled:
            lines.append("* From Retrosheet's play-by-play of only part of these clubs' seasons, so a sample of each: "
                         + ", ".join(f"{html.escape(name)} {share:.0%}" for name, share in sampled)
                         + " of their innings in MLB's records.")
        self.so_summary.setText("<br>".join(lines))
        for side in self.so_tables:
            self._fill_safe_out(side)

    def _sort_safe_out(self, side: str, column: int):
        current, order = self._so_sort[side]
        if column == current:
            order = (Qt.SortOrder.AscendingOrder if order == Qt.SortOrder.DescendingOrder
                     else Qt.SortOrder.DescendingOrder)
        else:
            # names A-Z; figures best first: the most for a club's batters, the fewest allowed
            order = (Qt.SortOrder.AscendingOrder if self.SAFE_OUT_COLUMNS[column][2] == "text" or side == "defense"
                     else Qt.SortOrder.DescendingOrder)
        self._so_sort[side] = (column, order)
        self._fill_safe_out(side)

    def _fill_safe_out(self, side: str):
        """One table's clubs, sorted by its chosen column, then MLB's line (always last)."""
        if self._so_data is None:
            return
        _season, result = self._so_data
        column, order = self._so_sort[side]
        _h, key, kind = self.SAFE_OUT_COLUMNS[column]
        clubs = result["teams"] if side == "batting" else result["allowed"]
        teams = sorted(clubs, key=lambda t: (t.get(key) is None,
                                             (t.get(key) or "") if kind == "text" else (t.get(key) or 0)),
                       reverse=order == Qt.SortOrder.DescendingOrder)
        rows = teams + [result["mlb"]]
        frozen = self.so_frozen[side]
        frozen.setRowCount(len(rows))
        t = self.so_tables[side]
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            is_mlb = row is result["mlb"]
            for c, (_hh, k, f) in enumerate(self.SAFE_OUT_COLUMNS):
                v = row.get(k)
                if f == "text":
                    text = str(v or "")
                elif v is None:
                    text = ""
                elif f == "inn":
                    outs = int(row["outs"])
                    text = f"{outs // 3:,}.{outs % 3}"  # thirds, as innings pitched are written
                elif f == "pct":
                    text = f"{v:.1f}%"
                else:
                    text = f"{v:.{f}f}"
                if f != "text" and text and row.get("sample") is not None:
                    text += "*"
                item = QTableWidgetItem(text)
                if f != "text":
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                elif k == "abbrev" and not is_mlb:
                    item.setToolTip(f"{row['name']}, {row['league']}")
                if is_mlb or f == "pct":
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                if f == "pct" and v is not None and not is_mlb:
                    shown = round(v, 1)  # judged on the figure as shown, so 100.0% is never coloured a shortfall
                    good = shown >= 100.0 if side == "batting" else shown <= 100.0
                    item.setForeground(QBrush(QColor(WON_COLOR if good else LOST_COLOR)))
                    base = k[:-len("_pct")]
                    label = dict(self.SAFE_OUT_RATIOS)[base]
                    item.setToolTip(f"{row['name']}: {row[base]:.3f} \u00f7 all of MLB's {result['mlb'][base]:.3f} "
                                    f"{label}" + (" allowed" if side == "defense" else "")
                                    + ("  (below 100% is better)" if side == "defense" else ""))
                if k in ("on_base_inn", "second_inn", "third_inn", "scored_inn") and not is_mlb:
                    counts = {"on_base_inn": "reach1", "second_inn": "reach2", "third_inn": "reach3",
                              "scored_inn": "runs"}[k]
                    item.setToolTip(f"{row[counts]:,} in {row['outs']:,} outs"
                                    + (" in the field" if side == "defense" else ""))
                if f != "text" and item.text().endswith("*"):
                    item.setToolTip((item.toolTip() + "\n" if item.toolTip() else "")
                                    + f"From Retrosheet\u2019s play-by-play of only {row['sample']:.0%} of the "
                                    f"{row['name']}\u2019s innings: a sample of the season")
                t.setItem(r, c, item)
                if c == 0:
                    frozen.setItem(r, 0, item.clone())
        t.horizontalHeader().setSortIndicator(column, order)
        # the frozen column: sort mark when sorted by team, as wide as its names, header as tall as the table's
        if column == 0:
            frozen.horizontalHeader().setSortIndicator(0, order)
        else:
            frozen.horizontalHeader().setSortIndicator(-1, order)
        frozen.resizeColumnsToContents()
        # wide enough for the heading with its sort arrow, as well as for the names
        width = max(frozen.columnWidth(0), frozen.horizontalHeader().sectionSizeHint(0) + 12)
        frozen.setColumnWidth(0, width)
        frozen.setFixedWidth(width + 2 * frozen.frameWidth())
        height = t.horizontalHeader().sizeHint().height()
        t.horizontalHeader().setFixedHeight(height)
        frozen.horizontalHeader().setFixedHeight(height)
        for r in range(len(rows)):
            frozen.setRowHeight(r, t.rowHeight(r))

    # ---- production tab -----------------------------------------------------------
    def _build_production_tab(self) -> QWidget:
        """Nine batters, one per place in the batting order, judged on Runs Produced.

        Each batter's Runs Produced -- the player-level custom stat named in
        ``PRODUCTION_STAT``, defined by ``PRODUCTION_FILE`` -- is shown beside his runs
        and RBI, and divided by his plate appearances; that rate is divided by what
        all of MLB did per plate appearance that season, and the nine proportions
        are averaged.  Over 100% means the lineup produces more runs per trip to the
        plate than the average MLB batter.

        Laid out like the Range tab: the Team and Season chooser only decides who is
        *offered* in the nine drop-downs, and a chosen player is locked into his place
        with his team, year and figures.  Each place's list starts with the players
        who batted there most often that season (MLB's batting-order splits, from
        1950), then those who never did, by plate appearances.
        """
        self.prod_picker = _PlayerPicker(self, "prod_pick", "hitting",
                                         "Team and season to choose players from", with_player=False)
        self.prod_picker.roster_loaded.connect(self._on_prod_roster)
        self._prod_roster = None   # (team_id, team_name, season, rows) the picker last loaded
        self._prod_offer = None    # that roster with its batting order and Runs Produced, once loaded
        self._prod_slots = {n: None for n in mlbstats.LINEUP_SLOTS}  # locked players
        self._prod_league = {}     # (season, formula) -> mlbstats.league_production() for all of MLB
        self._prod_loading: set = set()
        self._prod_league_errors: dict = {}

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        headers = ("Place", "Player", "PA", "R", "RBI", "HR", PRODUCTION_STAT, "RP / PA", "MLB RP / PA",
                   "Player \u00f7 MLB")
        for col, text in enumerate(headers):
            head = QLabel(f"<b>{text}</b>")
            head.setAlignment(Qt.AlignmentFlag.AlignRight if col >= 2 else Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(head, 0, col)
        self.prod_combos: dict[int, QComboBox] = {}
        self.prod_cells: dict[int, tuple] = {}
        for slot in mlbstats.LINEUP_SLOTS:
            grid.addWidget(QLabel(f"<b>{ORDINALS[slot - 1]}</b>"), slot, 0)
            combo = QComboBox()
            combo.setMinimumWidth(440)
            combo.view().setMinimumWidth(680)  # the open list, so a long team + player name isn't clipped
            combo.addItem("(none)", None)
            combo.activated.connect(lambda index, n=slot: self._on_prod_pick(n, index))
            grid.addWidget(combo, slot, 1)
            cells = []
            for col in range(2, len(headers)):
                label = QLabel()
                label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                label.setMinimumWidth(36)  # otherwise each column is as wide as its header
                grid.addWidget(label, slot, col)
                cells.append(label)
            self.prod_combos[slot] = combo
            self.prod_cells[slot] = tuple(cells)
        grid.setColumnStretch(1, 1)

        self.prod_status = QLabel()
        self.prod_status.setWordWrap(True)
        self.prod_status.setVisible(False)
        self.prod_summary = QLabel("Choose players for the batting places above.")
        self.prod_summary.setTextFormat(Qt.TextFormat.RichText)
        self.prod_summary.setWordWrap(True)
        font = self.prod_summary.font()
        font.setPointSize(font.pointSize() + 2)
        self.prod_summary.setFont(font)

        clear_btn = QPushButton("Clear all")
        clear_btn.clicked.connect(self._prod_clear_all)
        self.prod_auto = QCheckBox("Fill the lineup when the team changes")
        self.prod_auto.setChecked(True)
        self.prod_auto.setToolTip("Put the team's usual batters in the nine places when a team or season is chosen "
                                  "(whoever batted from each place most often).\nTurn off to keep your own picks "
                                  "while browsing other teams.")
        note = QLabel(
            f"RP = {PRODUCTION_STAT} ({PRODUCTION_FILE}), per plate appearance; MLB = all batters "
            "that season.  Lists start with who batted there most (records from 1950).")
        note.setWordWrap(True)

        page = QWidget()
        outer = QVBoxLayout(page)
        top = QHBoxLayout()
        top.addWidget(self.prod_picker)
        top.addWidget(self.prod_auto, 0, Qt.AlignmentFlag.AlignBottom)
        top.addStretch(1)
        top.addWidget(clear_btn, 0, Qt.AlignmentFlag.AlignBottom)
        outer.addLayout(top)
        outer.addWidget(self.prod_status)
        outer.addLayout(grid)
        outer.addWidget(self.prod_summary)
        outer.addWidget(note)
        outer.addStretch(1)
        return page

    def _set_prod_status(self, text: str):
        """The line above the table: loading, problems, or a season without batting-order
        records.  Hidden when empty, so it takes no room."""
        self.prod_status.setText(text)
        self.prod_status.setVisible(bool(text))

    def _prod_definition(self) -> str | None:
        """The formula of the player-level custom stat named ``PRODUCTION_STAT``, if defined."""
        for level, name, ftext in self._custom_stats:
            if level == "player" and name.lower() == PRODUCTION_STAT.lower():
                return ftext
        return None

    def _prod_missing_message(self) -> str:
        return (f"The Production tab needs a player-level custom stat named \u201c{PRODUCTION_STAT}\u201d, "
                f"which isn't defined right now.  Load {PRODUCTION_FILE} on the Custom tab "
                "(Load definitions\u2026).")

    def _on_prod_roster(self, data, fill: bool = True):
        """The picker's team and season have (re)loaded: fetch what that roster's drop-downs
        also need -- each player's Runs Produced and where he batted.  The lists already on
        offer stay until the new ones are ready (as on the Range tab), so there is nothing to
        announce meanwhile."""
        self._prod_roster = data
        self._tag_tokens["prod_offer"] = self._tag_tokens.get("prod_offer", 0) + 1  # drop a load still in flight
        ftext = self._prod_definition()
        if data is None or ftext is None:
            self._prod_offer = None
            self._rebuild_all_prod_combos()
            self._set_prod_status("Couldn't load that team and season." if data is None
                                  else self._prod_missing_message())
        else:
            team_id, team_name, season, rows = data
            ttl = LIVE_TTL if season >= self._season_now else None

            def fn():
                rp = mlbstats.get_team_production(team_id, season, ftext, ttl)
                try:
                    order, order_error = mlbstats.get_batting_order(team_id, season, ttl), None
                except mlbstats.MLBStatsError as exc:  # the lists just fall back to plate appearances
                    order, order_error = {}, str(exc)
                return {"team_id": team_id, "team_name": team_name, "season": season, "rows": rows,
                        "order": order, "order_error": order_error, "rp": rp, "ftext": ftext, "fill": fill}

            self._run_latest("prod_offer", fn, self._on_prod_offer, self._on_prod_offer_failed)
        self._update_prod_summary()

    def _refresh_production_offer(self):
        """The custom stats changed: reload the roster on offer if the definition of
        the Production stat did (a locked player keeps the figures he was locked with)."""
        have = self._prod_offer["ftext"] if self._prod_offer else None
        if self._prod_roster is not None and self._prod_definition() != have:
            self._on_prod_roster(self._prod_roster, fill=False)  # a redefined stat is no reason to reset the lineup
        else:
            self._update_prod_summary()

    def _on_prod_offer(self, offer: dict):
        self._prod_offer = offer
        who = f"{offer['team_name']} {offer['season']}"
        if offer["order_error"]:
            self._set_prod_status(f"{who}: batting order unavailable ({offer['order_error']}) "
                                     "\u2014 lists by plate appearances.")
        elif not offer["order"]:
            self._set_prod_status(f"{who}: no batting-order records before 1950 "
                                     "\u2014 lists by plate appearances.")
        else:
            self._set_prod_status("")  # nothing to report
        self._rebuild_all_prod_combos()
        self._ensure_prod_league(offer["season"], offer["ftext"])  # the whole-roster figure needs MLB's rate too
        if offer.get("fill"):
            self._prod_autofill()
        self._update_prod_summary()

    def _on_prod_offer_failed(self, message: str):
        self._prod_offer = None  # not the previous team's players under an error about this one
        self._rebuild_all_prod_combos()
        self._set_prod_status(f"Couldn't load the players: {message}")
        self._say("prod", f"Production: {message}")

    @staticmethod
    def _prod_num(value) -> str:
        if value is None:
            return "n/a"
        return f"{value:,.0f}" if abs(value - round(value)) < 1e-9 else f"{value:,.2f}"

    def _prod_label(self, name: str, team_name: str, season: int, slot: int, games, pa, rp) -> str:
        """The same text for every entry in a place's drop-down, locked player or offered
        alike: name, team and year, games batting from this place (when MLB has the
        record), plate appearances and Runs Produced."""
        parts = [f"{team_name} {season}"]
        if games is not None:
            parts.append(f"{games} G batting {ORDINALS[slot - 1]}")
        parts += [f"{int(pa or 0)} PA", f"RP {self._prod_num(rp)}"]
        separator = "  \u00b7  "
        return f"{name}  ({separator.join(parts)})"

    def _rebuild_all_prod_combos(self):
        for slot in self.prod_combos:
            self._rebuild_prod_combo(slot)

    def _rebuild_prod_combo(self, slot: int):
        """(Re)list a place's drop-down: "(none)", the locked player (if any, always kept,
        labelled with his own team and year), then the roster on offer -- those who batted
        from this place first, most games first -- minus anyone locked in a place."""
        combo, lock, offer = self.prod_combos[slot], self._prod_slots[slot], self._prod_offer
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("(none)", None)
        if lock:
            combo.addItem(self._prod_label(lock["name"], lock["team_name"], lock["season"], slot,
                                           lock["games"], lock["pa"], lock["rp"]), ("locked", None, None))
        if offer:
            taken = {(l["pid"], l["team_id"], l["season"]) for l in self._prod_slots.values() if l}
            rows = [r for r in offer["rows"] if r.get("_id") is not None
                    and (r["_id"], offer["team_id"], offer["season"]) not in taken]
            here, others = mlbstats.rank_for_slot(rows, offer["order"], slot)

            def add(row, games):
                combo.addItem(self._prod_label(
                    row.get("_name", "?"), offer["team_name"], offer["season"], slot, games,
                    mlbstats.as_number(row.get("plateAppearances")), offer["rp"].get(row["_id"])),
                    ("roster", row, games))

            for row in here:
                add(row, offer["order"][row["_id"]][slot]["games"])
            if here and others:
                combo.insertSeparator(combo.count())
            for row in others:
                add(row, 0 if offer["order"] else None)  # None: MLB has no record of this season's order
        combo.setCurrentIndex(1 if lock else 0)
        combo.blockSignals(False)

    def _on_prod_pick(self, slot: int, index: int):
        data = self.prod_combos[slot].itemData(index)
        if data is not None and data[0] == "locked":
            return  # the player already in the place
        if data is None:
            self._prod_slots[slot] = None
        else:
            _tag, row, games = data
            offer = self._prod_offer
            self._prod_slots[slot] = self._prod_lock(offer, row, games)
            self._ensure_prod_league(offer["season"], offer["ftext"])
        self._rebuild_all_prod_combos()  # a player placed here is no longer on offer elsewhere
        self._update_prod_row(slot)
        self._update_prod_summary()

    @staticmethod
    def _prod_lock(offer: dict, row: dict, games) -> dict:
        """What a slot keeps of the batter placed in it: his figures, and the team and season they came from."""
        return {
            "pid": row["_id"], "name": row.get("_name", "?"), "games": games,
            "team_id": offer["team_id"], "team_name": offer["team_name"], "season": offer["season"],
            "pa": mlbstats.as_number(row.get("plateAppearances")),
            "runs": mlbstats.as_number(row.get("runs")), "rbi": mlbstats.as_number(row.get("rbi")),
            "hr": mlbstats.as_number(row.get("homeRuns")),
            "rp": offer["rp"].get(row["_id"]), "formula": offer["ftext"],
        }

    def _prod_autofill(self):
        """Fill the nine places with the team's usual batting order: whoever batted from each most
        often (see :func:`mlbstats.assign_lineup`), replacing what was there.  If nothing is known of
        the order (before 1950) the nine with the most plate appearances fill it, most first."""
        offer = self._prod_offer
        if not self.prod_auto.isChecked() or not offer:
            return
        if offer["order"]:
            chosen = mlbstats.assign_lineup(offer["rows"], offer["order"])
        else:
            chosen = mlbstats.lineup_by_plate_appearances(offer["rows"])
        if not chosen:
            return
        for slot in self._prod_slots:
            self._prod_slots[slot] = None
        for slot, row in chosen.items():
            games = offer["order"][row["_id"]][slot]["games"] if offer["order"] else None  # None: no record
            self._prod_slots[slot] = self._prod_lock(offer, row, games)
        self._ensure_prod_league(offer["season"], offer["ftext"])
        self._rebuild_all_prod_combos()
        for slot in self._prod_slots:
            self._update_prod_row(slot)
        self._update_prod_summary()

    def _prod_clear_all(self):
        for slot in self._prod_slots:
            self._prod_slots[slot] = None
            self._update_prod_row(slot)
        self._rebuild_all_prod_combos()
        self._update_prod_summary()

    def _ensure_prod_league(self, season: int, ftext: str):
        """Fetch what all of MLB did in ``season`` (once per season and definition)."""
        key = (season, ftext)
        if key in self._prod_league or key in self._prod_loading:
            return
        self._prod_league_errors.pop(key, None)
        self._prod_loading.add(key)
        ttl = LIVE_TTL if season >= self._season_now else None  # a finished season never changes

        def fn():
            return key, mlbstats.get_league_production(season, ftext, ttl)

        self._run_latest(f"prod_league_{season}_{ftext}", fn, self._on_prod_league,
                         lambda message: self._on_prod_league_failed(key, message))

    def _on_prod_league(self, data):
        key, league = data
        self._prod_loading.discard(key)
        self._prod_league[key] = league
        self._update_prod_rows_for(key)

    def _on_prod_league_failed(self, key, message: str):
        self._prod_loading.discard(key)
        self._prod_league_errors[key] = message
        self._say("prod", f"Production: couldn't get MLB's {key[0]} figures: {message}")
        self._update_prod_rows_for(key)

    def _update_prod_rows_for(self, key):
        for slot, lock in self._prod_slots.items():
            if lock and (lock["season"], lock["formula"]) == key:
                self._update_prod_row(slot)
        self._update_prod_summary()

    def _prod_result(self, lock: dict):
        """-> (player's RP per PA, MLB's, player \u00f7 MLB); each is None where it can't be
        known (yet)."""
        rate = mlbstats.production_rate(lock["rp"], lock["pa"])
        league = self._prod_league.get((lock["season"], lock["formula"]))
        league_rate = league["rate"] if league else None
        ratio = rate / league_rate if rate is not None and league_rate else None
        return rate, league_rate, ratio

    def _update_prod_row(self, slot: int):
        cells = self.prod_cells[slot]
        pa_l, runs_l, rbi_l, hr_l, rp_l, rate_l, league_l, ratio_l = cells
        lock = self._prod_slots[slot]
        for label in cells:
            label.setText("")
            label.setToolTip("")
            label.setStyleSheet("")
        if lock is None:
            return
        rate, league_rate, ratio = self._prod_result(lock)
        pa_l.setText(self._prod_num(lock["pa"]))
        runs_l.setText(self._prod_num(lock["runs"]))
        rbi_l.setText(self._prod_num(lock["rbi"]))
        hr_l.setText(self._prod_num(lock["hr"]))
        rp_l.setText(self._prod_num(lock["rp"]))
        rate_l.setText("n/a" if rate is None else f"{rate:.3f}")
        key = (lock["season"], lock["formula"])
        if ratio is not None:
            league_l.setText(f"{league_rate:.3f}")
            ratio_l.setText(f"{ratio * 100:.1f}%")
            # judged on the figure as shown, so "100.0%" is never painted as a shortfall
            ratio_l.setStyleSheet("font-weight: bold; color: %s;"
                                  % ("#2e9e4f" if round(ratio * 100, 1) >= 100.0 else "#d9534f"))
            ratio_l.setToolTip(f"{lock['name']}'s {rate:.3f} \u00f7 all of MLB's {league_rate:.3f} "
                               f"{PRODUCTION_STAT} per plate appearance in {lock['season']}")
        elif rate is None or key in self._prod_league_errors:
            league_l.setText("n/a")
            ratio_l.setText("n/a")
            if key in self._prod_league_errors:
                league_l.setToolTip(self._prod_league_errors[key])
        else:
            league_l.setText("\u2026")
            ratio_l.setText("\u2026")

    def _prod_roster_result(self) -> dict | None:
        """The whole roster on offer (every batter of the picker's team and season with a plate
        appearance) against MLB, as :func:`mlbstats.roster_production` -> ``None`` with nothing on offer,
        else ``{"who", "players", "total", "pa", "rate", "league_rate", "pct", "failed"}`` where ``pct``
        (and ``league_rate``) stay ``None`` until MLB's figures are in."""
        offer = self._prod_offer
        if not offer:
            return None
        got = mlbstats.roster_production(offer["rows"], offer["rp"])
        key = (offer["season"], offer["ftext"])
        league = self._prod_league.get(key)
        league_rate = league["rate"] if league else None
        pct = round(got["rate"] / league_rate * 100, 1) if got["rate"] is not None and league_rate else None
        return {"who": f"{offer['team_name']} {offer['season']}", "players": got["players"], "total": got["total"],
                "pa": got["pa"], "rate": got["rate"], "league_rate": league_rate, "pct": pct,
                "failed": key in self._prod_league_errors}

    def _update_prod_summary(self):
        self._pbi_refresh_choices()  # the Players Batted In tab offers the same nine
        locks = {n: lock for n, lock in self._prod_slots.items() if lock}
        roster = self._prod_roster_result()
        if not locks and roster is None:
            self.prod_summary.setText("Choose players for the batting places above."
                                      if self._prod_definition() is not None else self._prod_missing_message())
            return
        headline, details = [], []
        if locks:
            ratios = [r[2] for lock in locks.values() for r in [self._prod_result(lock)] if r[2] is not None]
            waiting = len(locks) - len(ratios)
            if ratios:
                pct = round(sum(ratios) / len(ratios) * 100, 1)  # judged on the figure as shown
                colour = "#2e9e4f" if pct >= 100.0 else "#d9534f"
                total = sum(lock["rp"] for lock in locks.values() if lock["rp"] is not None)
                headline.append(f"Lineup production: <b style='color:{colour}'>{pct:.1f}%</b> of the MLB average")
                detail = (f"Lineup: mean of {len(ratios)} proportion(s), {sum(ratios):.3f} \u00f7 {len(ratios)};  "
                          f"{len(locks)} of 9 places filled;  {self._prod_num(total)} {PRODUCTION_STAT}")
                details.append(detail + (f";  {waiting} waiting on MLB or without data" if waiting else "") + ".")
            else:
                headline.append(f"{len(locks)} of 9 places filled \u2014 waiting on MLB's figures\u2026")
        if roster is not None:
            if roster["pct"] is not None:
                colour = "#2e9e4f" if roster["pct"] >= 100.0 else "#d9534f"
                headline.append(f"Entire {roster['who']} roster: <b style='color:{colour}'>{roster['pct']:.1f}%</b> "
                                "of that average")
                noun = "batter" if roster["players"] == 1 else "batters"
                details.append(f"Roster: all {roster['players']} {noun} with a plate appearance, "
                               f"{self._prod_num(roster['total'])} {PRODUCTION_STAT} in {roster['pa']:,.0f} PA = "
                               f"{roster['rate']:.3f} per PA against MLB's {roster['league_rate']:.3f}.")
            elif not roster["failed"] and roster["rate"] is not None:
                headline.append(f"Entire {roster['who']} roster: waiting on MLB's figures\u2026")
        text = "  \u00b7  ".join(headline)
        if details:
            text += "<br><span style='font-size:small'>" + "<br>".join(details) + "</span>"
        self.prod_summary.setText(text or "Choose players for the batting places above.")

    # ---- postseason tab -----------------------------------------------------------
    POSTSEASON_GAPS = {
        1904: "There was no World Series in 1904: the National League champion New York Giants "
              "refused to play the American League's Boston Americans.",
        1994: "There was no postseason in 1994: the players' strike ended the season on August 12.",
    }

    def _build_postseason_tab(self) -> QWidget:
        """One season's postseason as a bracket (see :class:`PostseasonBracket`) -- every
        round played that year, from a single series (1884-1968: the 19th-century World's
        Series, Temple Cup and the like, then the World Series) to today's wild card,
        division, championship and World Series rounds.  A season still under way shows the
        matchups known so far and placeholders for the rest."""
        self.ps_season_spin = _YearSpin()
        self.ps_season_spin.setRange(mlbstats.EARLY_POSTSEASON_FIRST_SEASON, 2100)
        self.ps_season_spin.setKeyboardTracking(False)
        self.ps_season_spin.setToolTip("Typing a year needs Enter (or click elsewhere) to take effect.")
        self._ps_debounce = QTimer(self, singleShot=True, interval=250)
        self._ps_debounce.timeout.connect(self._reload_postseason)
        self.ps_season_spin.valueChanged.connect(lambda _: self._ps_debounce.start())
        prev_btn, next_btn = QPushButton("\u25c0"), QPushButton("\u25b6")
        for btn, step in ((prev_btn, -1), (next_btn, 1)):
            btn.setFixedWidth(32)
            btn.clicked.connect(lambda _c, d=step: self.ps_season_spin.stepBy(d))
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Re-pull this postseason, ignoring the cache (for one under way).")
        refresh.clicked.connect(lambda: self._reload_postseason(force=True))
        self.ps_layout = QComboBox()
        self.ps_layout.addItems(["Auto", "Side by side (as on MLB.com)", "Stacked (AL above NL)"])
        self.ps_layout.setToolTip("Side by side is wide; stacked fits a narrower window.  Auto picks "
                                  "side by side when it fits the window at a readable size.")
        self.ps_fit = QCheckBox("Fit to window")
        self.ps_fit.setChecked(True)
        self.ps_fit.setToolTip("Shrink the bracket to the window's width instead of scrolling.")

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Season"))
        bar.addWidget(prev_btn)
        bar.addWidget(self.ps_season_spin)
        bar.addWidget(next_btn)
        bar.addSpacing(12)
        bar.addWidget(refresh)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Layout"))
        bar.addWidget(self.ps_layout)
        bar.addWidget(self.ps_fit)
        bar.addStretch(1)

        self.ps_message = QLabel()
        self.ps_message.setWordWrap(True)
        self.ps_message.setVisible(False)
        self.ps_bracket = PostseasonBracket()
        bold = self.font()
        bold.setBold(True)
        bold.setPointSizeF(bold.pointSizeF() * 1.15)
        self.ps_mlb_head = QLabel("American and National Leagues")
        self.ps_mlb_head.setFont(bold)
        self.ps_negro_head = QLabel("Negro leagues")
        self.ps_negro_head.setFont(bold)
        self.ps_tb_head = QLabel("Tiebreakers (regular season)")
        self.ps_tb_head.setFont(bold)
        self.ps_tb_about = QLabel(
            "Played to break a tie for a pennant, division or wild card spot before the postseason. "
            "Tiebreaker games count in the regular-season standings and statistics, not the "
            "postseason\u2019s (Poststats leaves them out unless its Tiebreakers box is ticked).")
        self.ps_tb_about.setWordWrap(True)
        self.ps_tb_boxes = QVBoxLayout()  # one small bracket per tiebreaker
        self.ps_negro_about = QLabel(
            "Since 2020 MLB has recognized the Negro leagues of 1920\u20131948 as major leagues. "
            f"A series marked <span style='color:{LIVE_COLOR}'>unofficial</span> falls outside "
            "that: before 1920 or after 1948, between independent clubs rather than a league's, or "
            "disputed or abandoned. Its note says which; hover over a series' header for the details. "
            "<br><small>Games: Retrosheet. The information used here was obtained free of charge "
            "from and is copyrighted by Retrosheet. Interested parties may contact Retrosheet at "
            "\u201cwww.retrosheet.org\u201d.</small>")
        self.ps_negro_about.setWordWrap(True)
        self.ps_negro_bracket = PostseasonBracket()
        self.ps_negro_others = QVBoxLayout()  # one small bracket per one-off series
        self.ps_negro_notes = QLabel()
        self.ps_negro_notes.setWordWrap(True)
        content = QWidget()
        column = QVBoxLayout(content)
        column.addWidget(self.ps_mlb_head)
        column.addWidget(self.ps_message)
        column.addWidget(self.ps_bracket, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(18)
        column.addWidget(self.ps_tb_head)
        column.addWidget(self.ps_tb_about)
        column.addLayout(self.ps_tb_boxes)
        column.addSpacing(18)
        column.addWidget(self.ps_negro_head)
        column.addWidget(self.ps_negro_about)
        column.addWidget(self.ps_negro_bracket, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addLayout(self.ps_negro_others)
        column.addWidget(self.ps_negro_notes)
        column.addStretch(1)
        self.ps_scroll = _BracketScroll(content)
        self.ps_scroll.brackets = [self.ps_bracket, self.ps_negro_bracket]
        self._ps_tiebreaks: list[dict] = []
        self._ps_show_negro([])
        self._ps_show_tiebreaks([])
        self.ps_fit.toggled.connect(self._ps_fit_toggled)
        self.ps_layout.currentIndexChanged.connect(self._ps_layout_changed)
        legend = QLabel(
            f"<span style='color:{WON_COLOR}'>\u2713</span> won &nbsp; "
            f"<span style='color:{LOST_COLOR}'>\u2717</span> lost &nbsp; \u2013 tied &nbsp; "
            f"<span style='color:{LIVE_COLOR}'>\u25cf</span> in progress &nbsp;\u2014 each with the club's runs. "
            "A date is a game still to come (<i>italic</i>: only if necessary); * marks a game with a note "
            "(shortened, forfeited\u2026); runs in brackets, (4), are a game played but not counted. "
            "The number after a club is its games won in the series.  Hover over a game for its date, "
            "ballpark and any note.")
        legend.setWordWrap(True)

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(bar)
        outer.addWidget(self.ps_scroll, 1)
        outer.addWidget(legend)
        return page

    def _ps_layout_changed(self, index: int):
        self.ps_scroll.layout_mode = ("auto", "side", "stacked")[index]
        self.ps_scroll.refit()

    def _ps_fit_toggled(self, on: bool):
        self.ps_scroll.fit = on
        self.ps_scroll.refit()

    def _reload_postseason(self, force: bool = False):
        season = self.ps_season_spin.value()
        ttl = 0 if force else (LIVE_TTL if season >= self._season_now else None)

        def fetch():
            mlb = [] if season in self.POSTSEASON_GAPS else mlbstats.get_postseason(season, ttl)
            return season, mlb, mlbstats.negro_postseason(season), mlbstats.get_tiebreak_series(season, ttl)

        self._ps_show_message(f"Loading the {season} postseason\u2026")
        self._ps_show_negro([])
        self._ps_show_tiebreaks([])
        self._run_latest("ps_load", fetch, self._on_ps_loaded, self._on_ps_failed)

    def _ps_show_message(self, text: str):
        """A message in place of the MLB bracket (or none: show the bracket)."""
        self.ps_message.setText(text)
        self.ps_message.setVisible(bool(text))
        self.ps_bracket.setVisible(not text)

    def _ps_show_negro(self, series: list[dict]):
        """Draw the Negro league section for ``series`` (hide it when there are none): the
        league playoffs and World Series as one bracket, each one-off series on its own, and
        the notes of the bracket's series underneath."""
        while self.ps_negro_others.count():
            widget = self.ps_negro_others.takeAt(0).widget()
            if widget is not None:
                self.ps_scroll.brackets = [b for b in self.ps_scroll.brackets if b is not widget]
                widget.deleteLater()
        bracketed = [s for s in series if s["round"] != "X"]
        others = [s for s in series if s["round"] == "X"]
        for widget in (self.ps_negro_head, self.ps_negro_about):
            widget.setVisible(bool(series))
        self.ps_mlb_head.setVisible(bool(series) or bool(self._ps_tiebreaks))
        self.ps_negro_bracket.setVisible(bool(bracketed))
        season = self.ps_season_spin.value()
        if bracketed:
            present = [s["league"] for s in bracketed if s["league"]]
            present += [lg for s in bracketed for lg in s.get("leagues", [])]
            order = [lg for lg in ("NNL", "NSL", "ECL", "NAL") if lg in present] + ["", ""]
            self.ps_negro_bracket.set_data(season, bracketed, sides=(order[0], order[1]))
        for s in others:
            about = " ".join(t for t in (s.get("status", ""), s.get("note", "")) if t)
            one = PostseasonBracket()
            one.set_data(season, [dict(s, round="W", id="W_1", note=about)], sides=("", ""))
            self.ps_negro_others.addWidget(one, 0, Qt.AlignmentFlag.AlignHCenter)
            self.ps_scroll.brackets.append(one)
        notes = []
        for s in bracketed:
            about = " ".join(t for t in (s.get("status", ""), s.get("note", "")) if t)
            if about and not (s["round"] == "W" and s.get("official", True)):
                league = f"{s['league']} " if s.get("league") else ""
                flag = f" <span style='color:{LIVE_COLOR}'>(unofficial)</span>" if s.get("official") is False else ""
                notes.append(f"<b>{league}{s['title']}</b>{flag}: {about}")
        self.ps_negro_notes.setText("<br>".join(notes))
        self.ps_negro_notes.setVisible(bool(notes))
        self.ps_scroll.refit()

    def _ps_show_tiebreaks(self, series: list[dict]):
        """The season's tiebreakers under the MLB bracket, each as a box of its own (hidden when
        there were none)."""
        self._ps_tiebreaks = series
        while self.ps_tb_boxes.count():
            widget = self.ps_tb_boxes.takeAt(0).widget()
            if widget is not None:
                self.ps_scroll.brackets = [b for b in self.ps_scroll.brackets if b is not widget]
                widget.deleteLater()
        for widget in (self.ps_tb_head, self.ps_tb_about):
            widget.setVisible(bool(series))
        season = self.ps_season_spin.value()
        for s in series:
            one = PostseasonBracket()
            one.set_data(season, [dict(s, round="W", id="W_1", note="")], sides=("", ""))
            self.ps_tb_boxes.addWidget(one, 0, Qt.AlignmentFlag.AlignHCenter)
            self.ps_scroll.brackets.append(one)
        self.ps_scroll.refit()

    def _on_ps_loaded(self, result):
        season, series, negro, tiebreaks = result
        if season != self.ps_season_spin.value():
            return
        if series:
            self._ps_show_message("")
            self.ps_bracket.set_data(season, series)
        elif season in self.POSTSEASON_GAPS:
            self._ps_show_message(self.POSTSEASON_GAPS[season])
        else:
            self._ps_show_message(f"No postseason series was played in {season}." if season < self._season_now
                                  else f"No postseason games are listed for {season} yet.")
        self._ps_show_tiebreaks(tiebreaks)
        self._ps_show_negro(negro)
        played = sum(1 for s in series for g in s["games"] if g["state"] == "final")
        status = (f"Postseason {season}: {len(series)} series, {played} games played" if series
                  else f"Postseason {season}: none" if season < self._season_now
                  else f"Postseason {season}: nothing scheduled yet")
        if tiebreaks:
            status += f"; {len(tiebreaks)} tiebreaker{'s' * (len(tiebreaks) != 1)} (regular season)"
        if negro:
            status += f"; Negro leagues: {len(negro)} series"
        self._say("ps", status)

    def _on_ps_failed(self, message: str):
        self._ps_show_message(f"Couldn't load the postseason: {message}")
        self._say("ps", f"Postseason: {message}")

    # ---- poststats tab --------------------------------------------------------------
    PST_VIEWS = ["Batting", "Pitching", "Fielding", "Team totals"]
    PST_TEAM_COLUMNS = [("Team", "_team"), ("G", "batting.gamesPlayed"), ("R", "batting.runs"),
                        ("H", "batting.hits"), ("HR", "batting.homeRuns"), ("AVG", "batting.avg"),
                        ("OBP", "batting.obp"), ("SLG", "batting.slg"), ("OPS", "batting.ops"),
                        ("W", "pitching.wins"), ("L", "pitching.losses"), ("ERA", "pitching.era"),
                        ("IP", "pitching.inningsPitched"), ("SO", "pitching.strikeOuts"),
                        ("BB", "pitching.baseOnBalls"), ("E", "fielding.errors"),
                        ("FLD%", "fielding.fielding")]

    def _build_poststats_tab(self) -> QWidget:
        """Player statistics for the postseason only, in the manner of the Stats tab, for the
        kinds of series ticked: the World Series, championship series, division series and
        wild card round (each taking in the tiebreakers of its era), and the other postseason
        series -- the 19th century's and the Negro leagues' -- grouped by the leagues involved.
        Summed from box scores (see mlbstats.poststats_lines)."""
        self.pst_team_combo = QComboBox()
        self.pst_team_combo.setMinimumWidth(240)
        self.pst_season_spin = _YearSpin()
        self.pst_season_spin.setRange(mlbstats.EARLY_POSTSEASON_FIRST_SEASON, 2100)
        self.pst_season_spin.setKeyboardTracking(False)
        self.pst_season_spin.setToolTip("Typing a year needs Enter (or click elsewhere) to take effect.")
        self._pst_debounce = QTimer(self, singleShot=True, interval=250)
        self._pst_debounce.timeout.connect(self._pst_load_catalog)
        self.pst_season_spin.valueChanged.connect(lambda _: self._pst_debounce.start())
        prev_btn, next_btn = QPushButton("◀"), QPushButton("▶")
        for btn, step in ((prev_btn, -1), (next_btn, 1)):
            btn.setFixedWidth(32)
            btn.clicked.connect(lambda _c, d=step: self.pst_season_spin.stepBy(d))
        self.pst_view_combo = QComboBox()
        self.pst_view_combo.addItems(self.PST_VIEWS)
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Re-pull this season's games, ignoring the cache (for a postseason under way).")
        refresh.clicked.connect(lambda: self._pst_load_catalog(force=True))
        csv_btn = QPushButton("Export CSV…")
        csv_btn.clicked.connect(self._pst_export_csv)

        bar = QHBoxLayout()
        for w in (QLabel("Team"), self.pst_team_combo, QLabel("  Season"), prev_btn, self.pst_season_spin,
                  next_btn, QLabel("  View"), self.pst_view_combo):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(refresh)
        bar.addWidget(csv_btn)

        self.pst_rounds = QHBoxLayout()  # the series kinds' check boxes, rebuilt for each season
        self._pst_checks: dict[str, QCheckBox] = {}
        self._pst_ticked: dict[str, bool] = {}  # remembered across seasons, by kind
        self._pst_catalog: list[dict] = []
        self._pst_season = None
        self.pst_info = QLabel()
        self.pst_info.setWordWrap(True)

        self.pst_model = QStandardItemModel(self)
        pst_proxy = _NumProxy(self)
        pst_proxy.setSourceModel(self.pst_model)
        self.pst_table = QTableView()
        self.pst_table.setModel(pst_proxy)
        self.pst_table.setSortingEnabled(True)
        self.pst_table.setAlternatingRowColors(True)
        self.pst_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.pst_table.verticalHeader().setVisible(False)
        self.pst_table.horizontalHeader().setHighlightSections(False)
        self._pst_csv: tuple[list, list] = ([], [])

        self.pst_team_combo.currentIndexChanged.connect(lambda _i: self._pst_reload())
        self.pst_view_combo.currentIndexChanged.connect(lambda _i: self._pst_reload())

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(bar)
        outer.addLayout(self.pst_rounds)
        outer.addWidget(self.pst_info)
        outer.addWidget(self.pst_table, 1)
        return page

    def _pst_load_catalog(self, force: bool = False):
        season = self.pst_season_spin.value()
        ttl = 0 if force else (LIVE_TTL if season >= self._season_now else None)
        self._pst_force = force
        self._say("pst", f"Loading the {season} postseason…")
        self._run_latest("pst_catalog", lambda: (season, mlbstats.poststats_catalog(season, ttl)),
                         self._on_pst_catalog, self._on_pst_failed)

    def _on_pst_catalog(self, result):
        season, cats = result
        if season != self.pst_season_spin.value():
            return
        self._pst_season, self._pst_catalog = season, cats
        # check boxes, a section label before each group
        while self.pst_rounds.count():
            item = self.pst_rounds.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._pst_checks = {}
        section = None
        for c in cats:
            if c["section"] != section:
                section = c["section"]
                head = QLabel(("    " if self._pst_checks else "") + f"<b>{section}:</b>")
                self.pst_rounds.addWidget(head)
            box = QCheckBox(c["label"])
            # tiebreakers (traditionally regular-season games) and unofficial Negro league series
            # start unticked
            default = c["key"] != "T" and c.get("official", True)
            box.setChecked(self._pst_ticked.get(c["key"], default) and c["available"])
            box.setEnabled(c["available"])
            box.setToolTip(c["tip"] + (f"\n\n{c['reason']}" if c["reason"] else ""))
            box.toggled.connect(lambda on, k=c["key"]: self._pst_toggled(k, on))
            self.pst_rounds.addWidget(box)
            self._pst_checks[c["key"]] = box
        self.pst_rounds.addStretch(1)
        # clubs: every one that played in this postseason (All teams first); keep the pick
        keep = self.pst_team_combo.currentData()
        teams = {}
        for c in cats:
            if c["available"]:
                teams.update(c["teams"])
        self.pst_team_combo.blockSignals(True)
        self.pst_team_combo.clear()
        self.pst_team_combo.addItem("All teams", None)
        for key, name in sorted(teams.items(), key=lambda kv: kv[1]):
            self.pst_team_combo.addItem(name, key)
        index = self.pst_team_combo.findData(keep) if keep is not None else 0
        self.pst_team_combo.setCurrentIndex(max(index, 0))
        self.pst_team_combo.blockSignals(False)
        self._pst_reload()

    def _pst_toggled(self, key: str, on: bool):
        self._pst_ticked[key] = on
        self._pst_reload()

    def _pst_reload(self):
        season = self._pst_season
        if season is None or season != self.pst_season_spin.value():
            return
        chosen = [c for c in self._pst_catalog if c["available"] and self._pst_checks.get(c["key"])
                  and self._pst_checks[c["key"]].isChecked()]
        reasons = [c["reason"] for c in self._pst_catalog if not c["available"] and c["reason"]]
        tiebreak = next((c for c in self._pst_catalog if c["key"] == "T" and c["available"]), None)
        if tiebreak is not None:  # said on screen, not just in the box's tooltip
            if tiebreak in chosen:
                reasons.append("Tiebreaker games are included here, though they count in the "
                               "regular-season statistics, not the postseason\u2019s.")
            else:
                reasons.append(f"{season} had a tiebreaker; its games count in the regular-season "
                               "statistics, so they\u2019re left out unless Tiebreakers is ticked.")
        if not self._pst_catalog:
            reasons.append(mlb_gap if (mlb_gap := self.POSTSEASON_GAPS.get(season))
                           else f"No postseason series was played in {season}." if season < self._season_now
                           else f"No postseason games are listed for {season} yet.")
        if not chosen:
            self.pst_model.clear()
            self._pst_csv = ([], [])
            if self._pst_catalog and any(c["available"] for c in self._pst_catalog):
                reasons.append("Tick at least one kind of series above.")
            self.pst_info.setText("  ".join(reasons))
            self._say("pst", f"Poststats {season}: nothing to show")
            return
        team = self.pst_team_combo.currentData()
        view = self.pst_view_combo.currentText()
        force = getattr(self, "_pst_force", False)
        ttl = 0 if force else (LIVE_TTL if season >= self._season_now else None)
        n_games = sum(1 for c in chosen for g in c["games"] if g["final"] and
                      (team is None or team in (g["away"][0], g["home"][0])))
        self._say("pst", f"Loading {n_games} box score{'s' * (n_games != 1)}…" if n_games
                  else "Loading…")

        def fetch():
            lines, games = mlbstats.poststats_lines(season, chosen, team, ttl)
            return season, team, view, [c["label"] for c in chosen], lines, games, reasons

        self._run_latest("pst_load", fetch, self._on_pst_loaded, self._on_pst_failed)

    def _on_pst_loaded(self, result):
        season, team, view, labels, lines, games, reasons = result
        self._pst_force = False
        if (season, team, view) != (self._pst_season, self.pst_team_combo.currentData(),
                                     self.pst_view_combo.currentText()):
            return
        self.pst_model.clear()
        # rows start in their own order (most active first); a single club's totals, a list of
        # statistics, can't be sorted at all
        self.pst_table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        self.pst_table.setSortingEnabled(not (view == "Team totals" and team is not None))
        if view == "Team totals":
            rows = mlbstats.post_team_totals(lines, games)
            if team is not None and rows:
                self._pst_fill_team(rows[0])
            else:
                self._pst_fill(self.PST_TEAM_COLUMNS, rows, dotted=True)
            count = f"{len(rows)} team{'s' * (len(rows) != 1)}"
        else:
            cols = list(mlbstats.POST_COLUMNS[view])
            if team is None:
                cols.insert(1, ("Team", "_team"))
            rows = mlbstats.aggregate_post_lines(lines, view)
            self._pst_fill(cols, rows)
            count = f"{len(rows)} row{'s' * (len(rows) != 1)}"
        if not lines and team is not None:  # the club played only in series left unticked
            where = [c["label"] for c in self._pst_catalog if c["available"] and team in c["teams"]
                     and not self._pst_checks[c["key"]].isChecked()]
            if where:
                reasons.append(f"{self.pst_team_combo.currentText()} played in {season} only in "
                               f"series not ticked above: {', '.join(where)}. Tick one to see them.")
        self.pst_info.setText("  ".join(reasons + [mlbstats.post_blanks(lines)]))
        who = self.pst_team_combo.currentText()
        self._say("pst", f"{who} · {season} · {', '.join(labels)} · {count}")

    def _pst_fill(self, cols, rows, dotted=False):
        def get(row, key):
            if dotted and "." in key:
                group, stat = key.split(".", 1)
                return row.get(group, {}).get(stat)
            return row.get(key)
        self.pst_model.setHorizontalHeaderLabels([h for h, _ in cols])
        for row in rows:
            self.pst_model.appendRow([self._new_item(get(row, k), numeric=k not in ("_name", "_pos", "_team"))
                                      for _, k in cols])
        self.pst_table.resizeColumnsToContents()
        self._pst_csv = ([h for h, _ in cols],
                         [["" if get(r, k) is None else str(get(r, k)) for _, k in cols] for r in rows])

    def _pst_fill_team(self, row: dict):
        self.pst_model.setHorizontalHeaderLabels(["Statistic", "Value"])
        csv_rows = []
        for title, key in (("Batting", "hitting"), ("Pitching", "pitching"), ("Fielding", "fielding")):
            group = {"hitting": "batting"}.get(key, key)
            self.pst_model.appendRow([self._new_item(f"— {title} —", numeric=False),
                                      self._new_item("", numeric=False)])
            csv_rows.append([f"-- {title} --", ""])
            for label, stat in mlbstats.TEAM_FIELDS[key]:
                source = row.get(group, {})
                if key == "fielding" and stat == "wildPitches":
                    source = row.get("pitching", {})
                value = source.get(stat)
                self.pst_model.appendRow([self._new_item(label, numeric=False), self._new_item(value)])
                csv_rows.append([label, "" if value is None else str(value)])
        self.pst_table.resizeColumnsToContents()
        self._pst_csv = (["Statistic", "Value"], csv_rows)

    def _on_pst_failed(self, message: str):
        self.pst_info.setText(f"Couldn't load the postseason: {message}")
        self._say("pst", f"Poststats: {message}")

    def _pst_export_csv(self):
        headers, rows = self._pst_csv
        if not rows:
            return
        team = self.pst_team_combo.currentText().replace(" ", "-")
        name = f"poststats_{self._pst_season}_{team}_{self.pst_view_combo.currentText().lower().replace(' ', '-')}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", os.path.join(HOME, name), "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(headers)
                writer.writerows(rows)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self._say("pst", f"Saved {path}")

    # ---- leaders tab -------------------------------------------------------------
    LD_GROUPS = (("Batting", "hitting"), ("Pitching", "pitching"), ("Fielding", "fielding"))
    LD_LEAGUE_SETS = (("Major leagues", (103, 104)), ("19th century", (100, 101, 105)), ("Federal", (106,)),
                      ("Negro leagues", (430, 427, 426, 429, 432, 431, 428)))
    LD_OPENING = {"hitting": "homeRuns", "pitching": "earnedRunAverage", "fielding": "assists"}  # each group's first board

    def _build_leaders_tab(self) -> QWidget:
        """MLB's own leaderboards for the statistics the Stats tab shows (see
        :data:`mlbstats.LEADER_STATS`): a Yearly subtab -- one season's leaders, or the best
        single seasons ever -- and a Career subtab.  Nothing is fetched until the tab is shown."""
        self._ld = {}          # "yearly" / "career" -> that page's widgets and state
        self._ld_started = False
        self.ld_tabs = QTabWidget()
        self.ld_tabs.addTab(self._leaders_page("yearly"), "Yearly")
        self.ld_tabs.addTab(self._leaders_page("career"), "Career")
        self.ld_tabs.currentChanged.connect(lambda _i: self._ld_tab_shown())
        page = QWidget()
        QVBoxLayout(page).addWidget(self.ld_tabs)
        return page

    def _leaders_page(self, which: str) -> QWidget:
        ui = {"which": which}
        self._ld[which] = ui
        reload = QTimer(self, singleShot=True, interval=250)
        reload.timeout.connect(lambda: self._ld_reload(which))
        ui["debounce"] = reload
        later = lambda *_: reload.start()

        bar = QHBoxLayout()
        if which == "yearly":
            ui["season"] = _YearSpin()
            ui["season"].setRange(1871, _dt.date.today().year)
            ui["season"].setValue(_dt.date.today().year)
            ui["season"].setKeyboardTracking(False)
            ui["season"].setToolTip("Typing a year needs Enter (or click elsewhere) to take effect.")
            ui["season"].valueChanged.connect(later)
            ui["alltime"] = QCheckBox("Best single seasons ever")
            ui["alltime"].setToolTip("The best seasons anyone has had, from every year, instead of one year\u2019s leaders.")
            ui["alltime"].toggled.connect(lambda on: (ui["season"].setEnabled(not on), self._ld_fill_stats(which)))
            bar.addWidget(QLabel("Season"))
            bar.addWidget(ui["season"])
            bar.addWidget(ui["alltime"])
            bar.addSpacing(12)
        ui["group"] = QComboBox()
        for label, group in self.LD_GROUPS:
            ui["group"].addItem(label, group)
        ui["group"].currentIndexChanged.connect(lambda _i: self._ld_fill_stats(which))
        ui["stat"] = QComboBox()
        ui["stat"].setMinimumWidth(260)
        ui["stat"].currentIndexChanged.connect(later)
        ui["position"] = QComboBox()
        ui["position"].addItem("All positions", None)
        for pos in mlbstats.LEADER_POSITIONS:
            ui["position"].addItem(pos, pos)
        ui["position"].currentIndexChanged.connect(lambda _i: self._ld_fill_stats(which))
        ui["position_label"] = QLabel("Position")
        ui["league"] = self._ld_league_picker(which)
        ui["era"] = QComboBox()
        for label, era in (("All years", None), ("1901 Onwards", "modern"), ("Before 1901", "early")):
            ui["era"].addItem(label, era)
        ui["era"].setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)   # (no names cut short)
        ui["era"].setToolTip("Which seasons count.  1901 Onwards starts with the American League\u2019s first "
                             "season: the 19th-century leagues drop out, and so do the National League\u2019s "
                             "seasons before then.  Before 1901 keeps only those: the National League\u2019s "
                             "1876\u20131900, the American Association, Union Association and Players\u2019 League.")
        ui["era"].currentIndexChanged.connect(lambda _i: self._ld_scope_changed(which))
        ui["limit"] = QSpinBox()
        ui["limit"].setRange(10, mlbstats.LEADER_LIMIT)
        ui["limit"].setSingleStep(5)
        ui["limit"].setValue(25)
        ui["limit"].setKeyboardTracking(False)
        ui["limit"].valueChanged.connect(later)
        ui["attempts"] = QSpinBox()
        ui["attempts"].setRange(1, 5000)
        ui["attempts"].setKeyboardTracking(False)
        ui["attempts"].setToolTip("The fewest steals tried against a catcher for him to make the board.")
        ui["attempts"].valueChanged.connect(later)
        ui["attempts_label"] = QLabel("At least")
        ui["attempts_unit"] = QLabel("attempts")
        ui["stat"].currentIndexChanged.connect(lambda _i: self._ld_attempts_shown(which))
        for label, widget in (("Group", ui["group"]), ("Statistic", ui["stat"]), (None, ui["position"]),
                              ("Leagues", ui["league"])):
            bar.addWidget(ui["position_label"] if widget is ui["position"] else QLabel(label))
            bar.addWidget(widget)
        bar.addStretch(1)
        # the era and the board's length on a line of their own, the era's menu wide enough for its
        # longest name with room to spare (the style's arrow and padding vary)
        ui["era"].setMinimumWidth(max(ui["era"].fontMetrics().horizontalAdvance(ui["era"].itemText(i))
                                      for i in range(ui["era"].count())) + 70)
        second = QHBoxLayout()
        for label, widget in (("Era", ui["era"]), ("Top", ui["limit"])):
            second.addWidget(QLabel(label))
            second.addWidget(widget)
        second.addSpacing(12)
        for widget in (ui["attempts_label"], ui["attempts"], ui["attempts_unit"]):
            second.addWidget(widget)
        second.addStretch(1)

        ui["title"] = QLabel()
        font = ui["title"].font()
        font.setBold(True)
        ui["title"].setFont(font)
        ui["title"].setWordWrap(True)
        ui["model"] = QStandardItemModel(self)
        ui["table"] = QTableView()
        ui["table"].setModel(ui["model"])
        ui["table"].setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        ui["table"].setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        ui["table"].setAlternatingRowColors(True)
        ui["table"].verticalHeader().setVisible(False)
        ui["table"].horizontalHeader().setHighlightSections(False)
        note = QLabel("MLB\u2019s own leaderboards: regular season only, with MLB\u2019s playing-time minimums "
                      "for the averages and rates.  Tied players share a rank."
                      + ("  On a career board the team is the player\u2019s last (within the leagues and "
                         "era counted).  With leagues left "
                         "out or one era chosen, MLB has no career boards, so they\u2019re added up here "
                         f"from each player\u2019s league careers or seasons, with the averages and rates worked "
                         f"out again for careers of {mlbstats.CAREER_MIN_PA:,} plate appearances or "
                         f"{mlbstats.CAREER_MIN_OUTS // 3:,} innings." if which == "career" else
                         "  With one era chosen, the best single seasons are picked from MLB\u2019s own "
                         "season lists, with its own minimums.")
                      + "  Catchers\u2019 caught stealing (CS%) is the runners a catcher threw out for each "
                        "steal tried against him, CS / (SB + CS), put together here from MLB\u2019s fielding "
                        "records, which have them from the 1950s on.")
        note.setWordWrap(True)

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addLayout(bar)
        lay.addLayout(second)
        lay.addWidget(ui["title"])
        lay.addWidget(ui["table"], 1, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(note)
        self._ld_fill_stats(which)
        return page

    def _ld_league_picker(self, which: str) -> QToolButton:
        """A drop-down of the major leagues by abbreviation, each one ticked to count or not, in
        groups (the American and National, the 19th-century majors, the Federal League, the Negro
        leagues) -- with All and None for each group.  It stays open while leagues are ticked."""
        ui = self._ld[which]
        button = QToolButton()
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setText("All")
        button.setMinimumWidth(QFontMetrics(button.font()).horizontalAdvance("All but NNL II, NNL I, ECL") + 28)
        names = {lid: (abbr, name) for lid, abbr, name in mlbstats.LEADER_LEAGUES}
        panel = QWidget()
        grid = QGridLayout(panel)
        grid.setContentsMargins(8, 6, 8, 6)
        ui["league_boxes"] = {}
        for row, (title, ids) in enumerate(self.LD_LEAGUE_SETS):
            heading = QLabel(title)
            font = heading.font()
            font.setBold(True)
            heading.setFont(font)
            grid.addWidget(heading, row, 0)
            for col, lid in enumerate(ids, start=1):
                box = QCheckBox(names[lid][0])
                box.setChecked(True)
                box.setToolTip(names[lid][1])
                box.toggled.connect(lambda _on: self._ld_scope_changed(which))
                ui["league_boxes"][lid] = box
                grid.addWidget(box, row, col)
        everything = QHBoxLayout()
        for label, state in (("All", True), ("AL and NL only", None), ("None", False)):
            pick = QPushButton(label)
            pick.clicked.connect(lambda _c=False, state=state: self._ld_pick_leagues(which, state))
            everything.addWidget(pick)
        everything.addStretch(1)
        grid.addLayout(everything, len(self.LD_LEAGUE_SETS), 0, 1, 8)
        menu = QMenu(button)
        action = QWidgetAction(menu)
        action.setDefaultWidget(panel)
        menu.addAction(action)
        button.setMenu(menu)
        button.setToolTip("Which leagues count.  All of them is every league MLB counts as major \u2014 the "
                          "Negro leagues (1920\u201348) and the 19th-century majors included.")
        return button

    def _ld_pick_leagues(self, which: str, state):
        """All (True), none (False) or just the American and National Leagues (None)."""
        boxes = self._ld[which]["league_boxes"]
        for lid, box in boxes.items():
            box.blockSignals(True)
            box.setChecked(state if state is not None else lid in (103, 104))
            box.blockSignals(False)
        self._ld_scope_changed(which)

    def _ld_leagues(self, ui: dict):
        """The ids of the leagues ticked, or None when they all are."""
        ticked = [lid for lid, box in ui["league_boxes"].items() if box.isChecked()]
        return None if len(ticked) == len(ui["league_boxes"]) else ticked

    def _ld_league_text(self, ui: dict) -> str:
        """The leagues ticked, as the drop-down shows them: All, the few ticked, or all but the few
        left out."""
        boxes = ui["league_boxes"]
        ticked = [box.text() for box in boxes.values() if box.isChecked()]
        left_out = [box.text() for box in boxes.values() if not box.isChecked()]
        if not left_out:
            return "All"
        if not ticked:
            return "None"
        if len(ticked) <= 3:
            return ", ".join(ticked)
        return "All but " + ", ".join(left_out) if len(left_out) <= 3 else f"{len(ticked)} leagues"

    def _ld_scope_changed(self, which: str):
        """The leagues or the era changed: relabel, keep the year within the era and re-list the
        statistics (an added-up career board can't count fielders' games at every position)."""
        ui = self._ld[which]
        ui["league"].setText(self._ld_league_text(ui))
        if which == "yearly":
            self._ld_season_range()
        self._ld_fill_stats(which)

    def _ld_season_range(self):
        """The Yearly season box's years: the era's, up to this season."""
        ui = self._ld["yearly"]
        era = ui["era"].currentData()
        now = self._season_now or _dt.date.today().year
        ui["season"].setRange(mlbstats.MODERN_FROM if era == "modern" else 1871,
                              mlbstats.MODERN_FROM - 1 if era == "early" else now)

    def _ld_kind(self, ui: dict) -> str:
        if ui["which"] == "career":
            return "career"
        return "single" if ui["alltime"].isChecked() else "season"

    def _ld_fill_stats(self, which: str):
        """(Re)list the statistics MLB keeps boards of for this group and kind of board, keeping
        the one chosen if it's still there; the position choice shows for fielding only."""
        ui = self._ld[which]
        group = ui["group"].currentData()
        fielding = group == "fielding"
        ui["position"].setVisible(fielding)
        ui["position_label"].setVisible(fielding)
        position = ui["position"].currentData() if fielding else None
        kind = self._ld_kind(ui)
        assembled = group == "fielding" and not position and mlbstats.leaders_assembled(
            kind, self._ld_leagues(ui), ui["era"].currentData())
        keep = ui["stat"].currentData()
        if keep is None or ui.get("group_shown") != group:   # a new group opens on its headline stat
            keep = next(((c, a, n) for a, n, c, _k in mlbstats.LEADER_STATS[group]
                         if c == self.LD_OPENING[group]), None)
        ui["group_shown"] = group
        ui["stat"].blockSignals(True)
        ui["stat"].clear()
        for abbr, name, category, kinds in mlbstats.LEADER_STATS[group]:
            if kind in kinds and (position or category not in mlbstats.LEADERS_NEED_POSITION) \
                    and not (assembled and category in mlbstats.CAREER_FIELDING_NEED_POSITION) \
                    and (category != mlbstats.CS_PERCENT or position in (None, "C")):
                ui["stat"].addItem(f"{name} ({abbr})", (category, abbr, name))
        index = next((i for i in range(ui["stat"].count()) if ui["stat"].itemData(i) == keep), 0)
        ui["stat"].setCurrentIndex(index)
        ui["stat"].blockSignals(False)
        self._ld_attempts_shown(which)
        ui["debounce"].start()

    def _ld_attempts_shown(self, which: str):
        """The fewest attempts, shown for catchers' caught stealing percentage only, and set to
        its default whenever the kind of board changes."""
        ui = self._ld[which]
        shown = (ui["stat"].currentData() or (None,))[0] == mlbstats.CS_PERCENT
        for widget in (ui["attempts_label"], ui["attempts"], ui["attempts_unit"]):
            widget.setVisible(shown)
        kind = self._ld_kind(ui)
        if ui.get("attempts_kind") != kind:
            ui["attempts_kind"] = kind
            ui["attempts"].blockSignals(True)
            ui["attempts"].setValue(mlbstats.CS_MIN_ATTEMPTS[kind])
            ui["attempts"].blockSignals(False)

    def _ld_tab_shown(self):
        """The first time the tab is on screen, load the page showing."""
        if self._current_tab_key() != "ld":
            return
        which = ("yearly", "career")[self.ld_tabs.currentIndex()]
        if not self._ld[which].get("loaded"):
            self._ld_reload(which)

    def _ld_reload(self, which: str):
        ui = self._ld[which]
        if self._current_tab_key() != "ld" or ("yearly", "career")[self.ld_tabs.currentIndex()] != which:
            ui["loaded"] = False   # fetched when next shown
            return
        ui["loaded"] = True
        stat = ui["stat"].currentData()
        if stat is None:
            return
        category, abbr, name = stat
        group = ui["group"].currentData()
        kind = self._ld_kind(ui)
        season = ui["season"].value() if kind == "season" else None
        leagues = self._ld_leagues(ui)
        era = ui["era"].currentData() if kind != "season" else None
        position = ui["position"].currentData() if group == "fielding" else None
        limit = ui["limit"].value()
        catchers = category == mlbstats.CS_PERCENT
        attempts = ui["attempts"].value() if catchers else None
        if leagues == []:
            ui["model"].clear()
            ui["title"].setText("Tick at least one league.")
            return
        if era and not mlbstats.leagues_in_era(leagues, era):
            ui["model"].clear()
            ui["title"].setText("None of the leagues ticked played " + ("from 1901 on." if era == "modern"
                                                                        else "before 1901."))
            return
        if kind == "season" and leagues is not None and not mlbstats.leagues_playing(leagues, season):
            ui["model"].clear()
            ui["title"].setText(f"None of the leagues ticked played in {season}.")
            return
        live = kind != "season" or season >= (self._season_now or _dt.date.today().year)
        ttl = LIVE_TTL if live else None
        where = "the major leagues" if leagues is None else ", ".join(
            mlbstats.LEAGUE_ABBREVIATIONS[lid] for lid in leagues)
        if era:
            where += ", from 1901 on" if era == "modern" else ", before 1901"
        at = f" at {position}" if position else ""
        title = {"season": f"{season} leaders in {where}{at}: {name}",
                 "single": f"Best single seasons in {where}{at}: {name}",
                 "career": f"Career leaders in {where}{at}: {name}"}[kind]
        if catchers:
            title += f", at least {attempts:,} attempts"
        assembled = mlbstats.leaders_assembled(kind, leagues, era) or catchers
        ui["title"].setText(f"Assembling\u2026 {title} \u2014 added up here from every player\u2019s record, "
                            "which can take a minute the first time" if assembled else f"Loading\u2026 {title}")
        self._say("ld", f"Loading {title}\u2026")
        self._run_latest(
            f"ld_{which}",
            lambda: mlbstats.get_leaders(group, category, kind, season, leagues, position, limit, ttl, era,
                                         attempts),
            lambda board: self._ld_show(which, title, abbr, kind, board, assembled),
            lambda message: (ui["title"].setText(f"Couldn\u2019t load {title}: {message}"),
                             self._say("ld", f"Leaders: {message}")))

    def _ld_show(self, which: str, title: str, abbr: str, kind: str, board: list[dict], assembled: bool = False):
        """The board in the table; an added-up career board lists every league counted for the
        player, and his last team within them."""
        ui = self._ld[which]
        model = ui["model"]
        model.clear()
        added_up = assembled and kind == "career"
        tries = abbr == "CS%"   # a catcher's steals tried against him, ahead of the percentage
        headers = ["Rank", "Player"] + (["Season"] if kind == "single" else []) \
            + ["Last team" if kind == "career" else "Team", "Leagues" if added_up else "League"] \
            + (["SB", "CS", "Att"] if tries else []) + [abbr]
        model.setHorizontalHeaderLabels(headers)
        previous = None
        for entry in board:
            league = mlbstats.LEAGUE_ABBREVIATIONS.get(entry["league_id"], entry["league"])
            cells = [str(entry["rank"]) if entry["rank"] != previous else "", entry["name"]] \
                + ([entry["season"] or ""] if kind == "single" else []) \
                + [entry["team"], league] \
                + ([f"{entry[k]:,}" for k in ("sb", "cs", "att")] if tries else []) + [entry["value"]]
            previous = entry["rank"]
            row = []
            for i, text in enumerate(cells):
                item = QStandardItem(text)
                item.setEditable(False)
                if i == 0 or i >= len(cells) - (4 if tries else 1) or (kind == "single" and i == 2):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if i == len(cells) - 1:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                row.append(item)
            model.appendRow(row)
        ui["table"].resizeColumnsToContents()
        ui["table"].setMinimumWidth(ui["table"].horizontalHeader().length() + 2 * ui["table"].frameWidth()
                                    + ui["table"].verticalScrollBar().sizeHint().width() + 4)
        if board:
            ui["title"].setText(title)
            self._say("ld", f"{title} \u2014 {len(board)} players")
        else:
            ui["title"].setText(f"{title}: MLB has no leaders for this.")
            self._say("ld", "")

    # ---- interleague tab ----------------------------------------------------------
    def _build_interleague_tab(self) -> QWidget:
        """How each division did against the other divisions of its own league (from 1969,
        when the leagues split into divisions), and how the leagues did against each other
        (from 1997, when interleague play began) -- one season at a time.  The matrix is every
        division against every other, from the season's games; the balance is drawn from the
        standings' win-loss records against each league and division."""
        self.il_season_spin = _YearSpin()
        self.il_season_spin.setRange(mlbstats.INTERDIVISION_FIRST_SEASON, 2100)
        self.il_season_spin.setKeyboardTracking(False)
        self.il_season_spin.setToolTip("Typing a year needs Enter (or click elsewhere) to take effect.")
        self._il_debounce = QTimer(self, singleShot=True, interval=250)
        self._il_debounce.timeout.connect(self._reload_interleague)
        self.il_season_spin.valueChanged.connect(lambda _: self._il_debounce.start())

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Season"))
        bar.addWidget(self.il_season_spin)
        bar.addStretch(1)

        self.il_title = QLabel()
        self.il_title.setWordWrap(True)
        font = self.il_title.font()
        font.setBold(True)
        self.il_title.setFont(font)

        self.il_warning = QLabel()
        self.il_warning.setWordWrap(True)
        self.il_warning.setStyleSheet("color: #d9534f;")
        self.il_warning.setVisible(False)
        self.il_note = QLabel()
        self.il_note.setWordWrap(True)
        self.il_note.setVisible(False)

        self.il_model = QStandardItemModel(self)
        self.il_table = QTableView()
        self.il_table.setModel(self.il_model)
        self.il_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.il_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.il_table.verticalHeader().setVisible(False)
        self.il_table.horizontalHeader().setHighlightSections(False)
        self.il_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.il_balance = IntergroupBalance()
        self._il_loading = None   # the season being assembled, while it is
        self._show_interleague_message("")

        note = QLabel(f"Divisional play began in {mlbstats.INTERDIVISION_FIRST_SEASON}; "
                      f"interleague play began in {mlbstats.INTERLEAGUE_FIRST_SEASON}.")
        note.setWordWrap(True)

        matrix = QWidget()
        matrix_layout = QVBoxLayout(matrix)
        matrix_layout.addWidget(self.il_table, 0, Qt.AlignmentFlag.AlignLeft)
        matrix_note = QLabel("Each row is a division\u2019s record against each column\u2019s, from the "
                             "season\u2019s games; the Total row is the other divisions\u2019 record against "
                             "that column\u2019s, its own record turned around.  A division\u2019s games "
                             "against its own teams are left out.  Hover over a cell for its games.")
        matrix_note.setWordWrap(True)
        matrix_layout.addWidget(matrix_note)
        matrix_layout.addStretch(1)
        self.il_tabs = QTabWidget()
        self.il_tabs.addTab(self.il_balance, "Balance")   # the opening view
        self.il_tabs.addTab(matrix, "Matrix")

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(bar)
        outer.addWidget(self.il_title)
        outer.addWidget(self.il_warning)
        outer.addWidget(self.il_note)
        outer.addWidget(self.il_tabs, 1)
        outer.addWidget(note)
        return page

    def _show_interleague_message(self, text: str):
        self.il_title.setText(text)
        self.il_warning.setVisible(False)
        self.il_note.setVisible(False)
        self.il_model.clear()
        self.il_table.setVisible(False)   # (an empty table is a stray dot)
        self.il_balance.clear()

    def _size_interleague_table(self):
        """Just big enough for the rows, and no wider than the columns."""
        table = self.il_table
        table.setFixedHeight(table.horizontalHeader().sizeHint().height()
                             + table.verticalHeader().length() + 2 * table.frameWidth() + 2)
        table.setFixedWidth(table.horizontalHeader().length() + 2 * table.frameWidth() + 2)

    def _reload_interleague(self):
        season = self.il_season_spin.value()
        ttl = LIVE_TTL if season >= self._season_now else None
        def load():
            records = mlbstats.get_league_records(season, ttl)
            return season, records, mlbstats.get_division_matrix(season, records, ttl)

        def still_loading():   # only a season not on hand yet takes long enough for this to show
            if self._il_loading == season:
                self._show_interleague_message(
                    f"Assembling {season} \u2014 fetching the standings and every game of the season\u2026")
        self._il_loading = season
        QTimer.singleShot(250, still_loading)
        self._run_latest("il_load", load, self._on_interleague_done, self._on_interleague_failed)

    def _on_interleague_failed(self, message: str):
        self._il_loading = None
        self._show_interleague_message(f"Couldn't load {self.il_season_spin.value()}: {message}")

    def _on_interleague_done(self, data):
        season, records, matrix = data
        self._il_loading = None
        self._show_interleague_message(f"{season}")
        if not records["interleague"] and not records["interdivision"]:
            self.il_title.setText(f"{season}: no standings records for this season yet.")
            return
        if any(d["w"] + d["l"] for d in records["interdivision"]):
            no_interdivision = None
        elif season >= self._season_now:
            no_interdivision = f"No interdivision games played yet in {season}."
        else:
            no_interdivision = f"No interdivision games were played in {season}."
            if season == 2020:  # the 60-game season
                no_interdivision += (" Teams played only their own division and the matching "
                                     "division of the other league.")
        divisions = records["interdivision"]
        if not divisions:
            self.il_title.setText(f"{season}: no divisions yet \u2014 they began in "
                                  f"{mlbstats.INTERDIVISION_FIRST_SEASON}.")
            return
        self._fill_division_matrix(records, matrix)
        self.il_balance.set_records(*self._interleague_balances(season, records))
        kinds = [k for k, shown in (("interleague", season >= mlbstats.INTERLEAGUE_FIRST_SEASON),
                                    ("interdivision", not no_interdivision)) if shown]
        self.il_title.setText(f"{season} \u2014 {' and '.join(kinds)} records"
                              + (" (both halves of the season)" if records.get("halves") else ""))
        if no_interdivision:
            self.il_note.setText(no_interdivision)
            self.il_note.setVisible(True)
        warnings = []
        if records["incomplete"]:
            warnings.append(f"MLB's standings for {season} are missing games for {records['incomplete']} "
                            "teams, so the balance undercounts.")
        if matrix["missing"]:
            games = (matrix["missing"] + 1) // 2
            warnings.append(f"MLB's schedule for {season} is missing {games} game{'s' * (games != 1)} "
                            "the standings count, so the matrix undercounts.")
        if warnings:
            self.il_warning.setText(" ".join(warnings))
            self.il_warning.setVisible(True)

    def _fill_division_matrix(self, records: dict, matrix: dict):
        """Every division against every other, across the leagues: each row a division's W-L
        against each column's, adding up to its record against the other divisions in the Total
        column (and its winning percentage after it); the Total row adds up each column -- every
        other division's record against that one, its own record turned around -- with the Pct
        row under it.  A division's games against itself, which always come out even, are left
        out: its own cell is blank."""
        leagues = records["leagues"]
        divisions = records["interdivision"]
        cells = matrix["cells"]

        def label(d) -> str:
            league = leagues.get(d["league"], "")
            short = "".join(word[0] for word in league.split() if word[:1].isupper()) or league
            return f"{short} {d['name'].split()[-1]}"

        def rate(w, l) -> str:
            return _fmt_rate(w / (w + l)) if w + l else "n/a"

        def blank() -> QStandardItem:
            item = QStandardItem("")
            item.setEditable(False)
            return item

        def cell(w, l, tip: str, bold: bool = False, pct: bool = False) -> QStandardItem:
            shown = (rate(w, l) if pct else f"{w:,}-{l:,}") if w + l else "\u2014"
            item = QStandardItem(shown)
            item.setEditable(False)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if w + l:
                item.setToolTip(f"{tip}: {w:,}-{l:,} ({rate(w, l)}) in {w + l:,} games")
            if bold:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            if w + l and w != l:
                tint = QColor(WON_COLOR if w > l else LOST_COLOR)
                tint.setAlphaF(min(1.0, abs(w / (w + l) - 0.5) / 0.1) * 0.45 + 0.1)
                item.setBackground(QBrush(tint))
            return item

        def heading(text: str) -> QStandardItem:
            item = QStandardItem(text)
            item.setEditable(False)
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            return item

        names = [label(d) for d in divisions]
        self.il_model.setHorizontalHeaderLabels([""] + names + ["Total", "Pct"])
        column_totals = [[0, 0] for _ in divisions]
        for d, name in zip(divisions, names):
            row, total = [heading(name)], [0, 0]
            for k, (o, other) in enumerate(zip(divisions, names)):
                w, l = cells.get((d["id"], o["id"]), (0, 0))
                own = o["id"] == d["id"]
                row.append(blank() if own else cell(w, l, f"{d['name']} against the {o['name']}"))
                if not own:
                    total[0] += w
                    total[1] += l
                    column_totals[k][0] += w
                    column_totals[k][1] += l
            row.append(cell(*total, f"{d['name']} against the other divisions", bold=True))
            row.append(cell(*total, f"{d['name']} against the other divisions", bold=True, pct=True))
            self.il_model.appendRow(row)
        bottom = [heading("Total")]
        for (w, l), o in zip(column_totals, divisions):
            bottom.append(cell(w, l, f"The other divisions against the {o['name']}", bold=True))
        grand = [sum(t[0] for t in column_totals), sum(t[1] for t in column_totals)]
        bottom.append(cell(*grand, "All games between divisions, from both sides", bold=True))
        bottom.append(cell(*grand, "All games between divisions, from both sides", bold=True, pct=True))
        self.il_model.appendRow(bottom)
        rates = [heading("Pct")]
        for (w, l), o in zip(column_totals, divisions):
            rates.append(cell(w, l, f"The other divisions against the {o['name']}", bold=True, pct=True))
        rates.append(cell(*grand, "All games between divisions, from both sides", bold=True, pct=True))
        rates.append(blank())
        self.il_model.appendRow(rates)
        self.il_table.resizeColumnsToContents()
        self._size_interleague_table()
        self.il_table.setVisible(True)

    @staticmethod
    def _interleague_balances(season: int, records: dict) -> tuple:
        """The Intergroup tab's records as :meth:`IntergroupBalance.set_records` wants them: the
        leagues against each other (None before interleague play), and each league's
        divisions against each other -- each division weighed, from interleague play on, by
        its games against the other league's divisions as well as its own league's."""
        def abbrev(name: str) -> str:
            return "".join(word[0] for word in name.split() if word[:1].isupper()) or name

        leagues = records["leagues"]
        interleague = None
        if season >= mlbstats.INTERLEAGUE_FIRST_SEASON and records["interleague"]:
            # each league's box lists its divisions' records against the other league, and
            # pulls with their total (in bold)
            interleague = [(abbrev(leagues.get(lid, str(lid))), leagues.get(lid, str(lid)), w, l)
                           + ((by_division, -1) if (by_division := [
                               (d["name"].split()[-1], d["il_w"], d["il_l"])
                               for d in records["interdivision"] if d["league"] == lid]) else ())
                           for lid, (w, l) in records["interleague"].items()]
        def group(d) -> tuple:
            """A division's box, pulling with its record against the rest of its league -- its
            interleague games already weigh in through its league, above it.  From interleague
            play on the box also lists its record against the other league, and the two summed."""
            short = d["name"].split()[-1]
            if season < mlbstats.INTERLEAGUE_FIRST_SEASON:
                return short, d["name"], d["w"], d["l"]
            return (short, d["name"], d["w"], d["l"],
                    [("division", d["w"], d["l"]), ("league", d["il_w"], d["il_l"])])

        divisions = [(f"{name} interdivision",
                      [group(d) for d in records["interdivision"] if d["league"] == lid])
                     for lid, name in leagues.items()]
        return interleague, divisions

    # ---- players batted in tab -----------------------------------------------------
    def _build_pbi_tab(self) -> QWidget:
        """The Production tab's companion: whom one batter drove in over a season, from Retrosheet's
        play-by-play (see :func:`retrosheet.parse_batted_in`), most runs first.  The batter is chosen
        from the Production tab's nine, in batting order, then the rest of the team on offer there.
        Nothing is fetched until the tab is on screen."""
        self._pbi_shown = None   # (pid, team_id, season) of the batter whose table is on screen or loading
        self.pbi_combo = QComboBox()
        self.pbi_combo.setMinimumWidth(440)
        self.pbi_combo.view().setMinimumWidth(560)
        self.pbi_combo.activated.connect(lambda _i: self._pbi_update())
        self.pbi_source = QLabel()
        self.pbi_source.setWordWrap(True)

        self.pbi_summary = QLabel()
        self.pbi_summary.setTextFormat(Qt.TextFormat.RichText)
        self.pbi_summary.setWordWrap(True)
        font = self.pbi_summary.font()
        font.setPointSize(font.pointSize() + 2)
        self.pbi_summary.setFont(font)
        self.pbi_status = QLabel()
        self.pbi_status.setWordWrap(True)
        self.pbi_status.setVisible(False)

        self.pbi_model = QStandardItemModel(self)
        self.pbi_table = QTableView()
        self.pbi_table.setModel(self.pbi_model)
        self.pbi_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.pbi_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.pbi_table.verticalHeader().setVisible(False)
        self.pbi_table.horizontalHeader().setHighlightSections(False)
        self.pbi_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

        note = QLabel(
            "Runs the batter was credited with driving in (RBI), by the player who scored: from third, second or "
            "first base, and the batter himself on his home runs.  Runs scoring on an error, a double play or a wild "
            "pitch are no one\u2019s RBI and aren\u2019t counted.  A pinch runner who scored is the one listed.  "
            "Regular season only.  Play-by-play from Retrosheet (retrosheet.org), obtained free of charge and "
            "copyrighted by Retrosheet.")
        note.setWordWrap(True)

        page = QWidget()
        outer = QVBoxLayout(page)
        top = QHBoxLayout()
        top.addWidget(QLabel("Batter"))
        top.addWidget(self.pbi_combo)
        self.pbi_combo.setToolTip("These batters come from the Production tab: the nine in its lineup, in batting "
                                  "order,\nthen the rest of the team chosen there.  Change the team, season or "
                                  "lineup on that tab.")
        top.addStretch(1)
        outer.addWidget(self.pbi_source)
        outer.addLayout(top)
        outer.addWidget(self.pbi_summary)
        outer.addWidget(self.pbi_status)
        outer.addWidget(self.pbi_table, 1, Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(note)
        self._pbi_refresh_choices()
        return page

    def _set_pbi_status(self, text: str):
        self.pbi_status.setText(text)
        self.pbi_status.setVisible(bool(text))

    def _pbi_refresh_choices(self):
        """(Re)list the batters: the Production tab's nine in batting order, then the rest of the
        team on offer there, by plate appearances.  The batter chosen stays chosen if still listed."""
        combo = getattr(self, "pbi_combo", None)
        if combo is None:
            return  # (the Production tab is built first)
        keep = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        locked = set()
        for slot, lock in self._prod_slots.items():
            if lock:
                key = (lock["pid"], lock["team_id"], lock["season"])
                locked.add(key)
                combo.addItem(f"{ORDINALS[slot - 1]}  \u00b7  {lock['name']}  ({lock['team_name']} {lock['season']})",
                              (*key, lock["name"], lock["team_name"]))
        offer = self._prod_offer
        if offer:
            rows = [r for r in offer["rows"] if r.get("_id") is not None
                    and (r["_id"], offer["team_id"], offer["season"]) not in locked]
            rows.sort(key=lambda r: -(mlbstats.as_number(r.get("plateAppearances")) or 0))
            if rows and combo.count():
                combo.insertSeparator(combo.count())
            for r in rows:
                name = r.get("_name", "?")
                combo.addItem(f"{name}  ({offer['team_name']} {offer['season']})",
                              (r["_id"], offer["team_id"], offer["season"], name, offer["team_name"]))
        lineup = sorted({f"{lock['team_name']} {lock['season']}" for lock in self._prod_slots.values() if lock})
        parts = []
        if lineup:
            parts.append(f"the {len(locked)} in its lineup ({', '.join(lineup)}), in batting order")
        if offer:
            parts.append(("then " if parts else "") + f"the rest of the {offer['team_name']} {offer['season']}")
        if parts:
            self.pbi_source.setText("<b>From the Production tab:</b> " + html.escape(", ".join(parts))
                                    + ".  Change the team, season or lineup there.")
        else:
            self.pbi_source.setText("<b>No batters yet</b> \u2014 choose a team and season, and the lineup, "
                                    "on the Production tab.")
        index = next((i for i in range(combo.count()) if keep and combo.itemData(i)
                      and combo.itemData(i)[:3] == keep[:3]), 0 if combo.count() else -1)
        combo.setCurrentIndex(index)
        combo.blockSignals(False)
        self._pbi_update()

    def _pbi_update(self):
        """Show whom the chosen batter drove in -- only while the tab is on screen (the first use of a
        season downloads its play-by-play), and only when the choice has changed."""
        if self._current_tab_key() != "pbi":
            return
        choice = self.pbi_combo.currentData()
        if choice is None:
            self._pbi_shown = None
            self._tag_tokens["pbi_data"] = self._tag_tokens.get("pbi_data", 0) + 1  # drop a load in flight
            self.pbi_model.clear()
            self.pbi_summary.setText("Choose players on the Production tab: its nine, and the rest of its team, "
                                     "are the batters listed here.")
            self._set_pbi_status("")
            return
        if choice[:3] == self._pbi_shown:
            return
        self._pbi_shown = choice[:3]
        pid, team_id, season, name, team_name = choice
        ttl = LIVE_TTL if season >= self._season_now else None
        self._set_pbi_status("" if mlbstats.retro_rbi_is_cached(season) else
                             f"Getting Retrosheet's {season} play-by-play (once, a few seconds)\u2026")
        self._run_latest("pbi_data", lambda: dict(mlbstats.get_batted_in(team_id, season, pid, name, ttl),
                                                  sample=mlbstats.retro_sample(team_id, season, ttl)),
                         lambda result: self._on_pbi_data(name, team_name, season, result),
                         self._on_pbi_failed)

    def _on_pbi_data(self, name: str, team_name: str, season: int, result: dict):
        self._set_pbi_status("")
        rows = result["rows"]
        model = self.pbi_model
        model.clear()
        model.setHorizontalHeaderLabels(["#", "Player batted in", "Times", "Share", "From 3rd", "From 2nd",
                                         "From 1st", "Himself (HR)"])
        rbi = result["rbi"]
        for rank, r in enumerate(rows, start=1):
            share = f"{100 * r['total'] / rbi:.1f}%" if rbi else ""
            cells = [str(rank), r["name"] + ("  (himself)" if r["self"] else ""), str(r["total"]), share,
                     str(r["from3"] or ""), str(r["from2"] or ""), str(r["from1"] or ""), str(r["own"] or "")]
            items = []
            for col, text in enumerate(cells):
                item = QStandardItem(text)
                if col != 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if r["self"]:
                    item.setForeground(QBrush(QColor("#777777")))
                items.append(item)
            model.appendRow(items)
        sample = result.get("sample")
        if sample is not None:
            self._star_sample(model, sample, skip_columns={0, 1})
        self._fit_table_width(self.pbi_table)
        own = sum(r["own"] for r in rows)
        others = sum(1 for r in rows if not r["self"])
        if not rows:
            self.pbi_summary.setText(f"<b>{html.escape(name)}</b> ({html.escape(team_name)} {season}) "
                                     + ("drove in no runs, by Retrosheet's play-by-play." if result.get("found") else
                                        "has no runs batted in under that name in Retrosheet's play-by-play "
                                        "\u2014 either he drove in none, or Retrosheet spells his name differently."))
        else:
            self.pbi_summary.setText(
                f"<b>{html.escape(name)}</b> ({html.escape(team_name)} {season}) drove in <b>{rbi}{'*' if sample is not None else ''}</b> run"
                f"{'s' if rbi != 1 else ''}: {others} different teammate{'s' if others != 1 else ''}"
                + (f", and himself {own} time{'s' if own != 1 else ''} on home runs." if own else "."))
        if sample is not None:
            self.pbi_summary.setText(self.pbi_summary.text() + "<br><span style='font-size:small'>"
                                     + html.escape(mlbstats.sample_note(sample, team_name, season)) + "</span>")
        self._say("pbi", f"Players Batted In: {name}, {team_name} {season} \u2014 {rbi} RBI")

    def _on_pbi_failed(self, message: str):
        self._pbi_shown = None  # so choosing it again tries again
        self.pbi_model.clear()
        self.pbi_summary.setText("")
        self._set_pbi_status(f"Couldn't get whom this batter drove in: {message}")
        self._say("pbi", f"Players Batted In: {message}")

    # ---- prevention tab -----------------------------------------------------------
    PREV_LINEUP_LABEL = " + ".join(mlbstats.PITCHER_LINEUP_ROLES) + " total"

    def _build_prevention_tab(self) -> QWidget:
        """A team's pitchers in one season, sorted into roles by how each compares with the team
        average per pitcher (see :func:`mlbstats.classify_pitchers`): Rotation, Long Relief,
        Closers and Other (Long Relief's innings are measured against the pitchers outside the Rotation).  Each pitcher, each role and the whole staff get runs per nine
        innings, earned run average and unearned runs per nine innings -- the player-level custom
        stats in ``PREVENTION_STATS`` -- with the counting figures they come from summed; runs per
        nine are compared with what all of MLB allowed per nine that season (MLB's \u00f7 theirs, so above
        100% is better).
        Team and Season are chosen as on the Range and Production tabs."""
        self.prev_picker = _PlayerPicker(self, "prev_pick", "pitching",
                                         "Team and season", with_player=False)
        self.prev_picker.roster_loaded.connect(self._on_prev_roster)
        self._prev_roster = None     # (team_id, team_name, season, pitching rows) the picker last loaded
        self._prev_used = None       # the custom-stat definitions the table on screen was drawn with

        self.prev_summary = QLabel("Choose a team and season above.")
        self.prev_summary.setTextFormat(Qt.TextFormat.RichText)
        self.prev_summary.setWordWrap(True)
        font = self.prev_summary.font()
        font.setPointSize(font.pointSize() + 2)
        self.prev_summary.setFont(font)
        self.prev_status = QLabel()
        self.prev_status.setWordWrap(True)
        self.prev_status.setVisible(False)
        self.prev_averages = QLabel()

        self.prev_model = QStandardItemModel(self)
        self.prev_table = QTableView()
        self.prev_table.setModel(self.prev_model)
        self.prev_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.prev_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.prev_table.verticalHeader().setVisible(False)
        self.prev_table.horizontalHeader().setHighlightSections(False)
        self.prev_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

        note = QLabel(
            "Rotation: GS at or above the average of the team's starters (pitchers with a start), or at least 40% of "
            "the busiest starter's GS, or twice as many starts as relief appearances and at least 30% of his GS.  "
            "Closers: GF at or above the average of its finishers (not Rotation).  "
            "Long Relief: GS and GF below those, IP above the non-Rotation average, and at least one game in relief.  Other: the rest.  "
            "Each role has its own total, and the first three are totalled together as well.  "
            "MLB's runs per nine \u00f7 the pitchers': above 100% is better.")
        note.setWordWrap(True)

        page = QWidget()
        outer = QVBoxLayout(page)
        top = QHBoxLayout()
        top.addWidget(self.prev_picker)
        top.addStretch(1)
        outer.addLayout(top)
        outer.addWidget(self.prev_summary)
        outer.addWidget(self.prev_status)
        outer.addWidget(self.prev_averages)
        outer.addWidget(self.prev_table, 1, Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(note)
        return page

    def _prev_definitions(self) -> list[tuple[str, str, str]]:
        """``[(column, custom stat name, formula), ...]`` for each ``PREVENTION_STATS`` stat that is
        defined at Player level, in column order."""
        found = []
        for column, stat_name, _file, _heading in PREVENTION_STATS:
            for level, name, ftext in self._custom_stats:
                if level == "player" and name.lower() == stat_name.lower():
                    found.append((column, stat_name, ftext))
                    break
        return found

    def _prev_missing_message(self) -> str:
        have = {column for column, _n, _f in self._prev_definitions()}
        missing = [(name, file) for column, name, file, _h in PREVENTION_STATS if column not in have]
        if not missing:
            return ""
        return ("The Prevention tab needs the player-level custom stat(s) "
                + ", ".join(f"\u201c{name}\u201d" for name, _f in missing) + " \u2014 load "
                + ", ".join(file for _n, file in missing) + " on the Custom tab (Load definitions\u2026).")

    def _set_prev_status(self, text: str):
        self.prev_status.setText(text)
        self.prev_status.setVisible(bool(text))

    def _clear_prevention(self, message: str = ""):
        """Empty the table and summary (there is nothing to show) with ``message`` above it."""
        self.prev_table.clearSpans()
        self.prev_model.clear()
        self.prev_summary.setText("Choose a team and season above." if not message else "")
        self.prev_averages.setText("")
        self._set_prev_status(message)
        self._prev_used = self._prev_definitions()

    def _refresh_prevention(self):
        """The custom stats changed: redraw if a definition the table uses did."""
        if self._prev_roster is not None and self._prev_definitions() != self._prev_used:
            self._on_prev_roster(self._prev_roster)

    def _on_prev_roster(self, data):
        """The picker's team and season have (re)loaded: fetch what the figures need -- the pitchers'
        other views, if a formula uses them, and MLB's league-wide figure.  The table already on
        screen stays until the new one is ready."""
        self._prev_roster = data
        self._hs_refresh()  # the Bullpen Support tab works on the same team and season
        self._tag_tokens["prev_data"] = self._tag_tokens.get("prev_data", 0) + 1  # drop a load in flight
        defs = self._prev_definitions()
        if data is None:
            self._clear_prevention("Couldn't load that team and season.")
            return
        if not defs:
            self._clear_prevention(self._prev_missing_message())
            return
        team_id, team_name, season, rows = data
        ttl = LIVE_TTL if season >= self._season_now else None
        stats = {column: ftext for column, _n, ftext in defs}

        def fn():
            groups = mlbstats.team_view_groups(team_id, season, mlbstats.formula_views(stats.values(), always=("Pitching",)), ttl)
            report = mlbstats.prevention_report(rows, groups, stats)
            league = mlbstats.get_league_pooled(season, stats.values(), ttl)
            return {"team_name": team_name, "season": season, "report": report, "defs": defs,
                    "league": {column: league[ftext] for column, ftext in stats.items()}}

        self._run_latest("prev_data", fn, self._on_prev_loaded, self._on_prev_failed)

    def _on_prev_failed(self, message: str):
        self._clear_prevention(f"Couldn't load the pitchers: {message}")
        self._say("prev", f"Prevention: {message}")

    def _prev_ratio_cell(self, value, league, name: str, season: int) -> QStandardItem:
        """MLB's runs per nine as a percentage of his (the Prevention Score), green when at or above 100%
        (no more runs allowed than MLB's); with no runs allowed, there's no ratio to show."""
        if value is None or not league:
            return self._st_cell("")
        tip = f"{name}: all of MLB's {league:.2f} runs per nine innings in {season} \u00f7 his {value:.2f}"
        if not value:
            item = self._st_cell("\u2014", bold=True, tip=tip + " (no runs allowed)")
            item.setForeground(QBrush(QColor("#2e9e4f")))
            return item
        ratio = league / value
        good = round(ratio * 100, 1) >= 100.0  # judged on the figure as shown
        item = self._st_cell(f"{ratio * 100:.1f}%", bold=True, tip=tip)
        item.setForeground(QBrush(QColor("#2e9e4f" if good else "#d9534f")))
        return item

    def _on_prev_loaded(self, data):
        report, defs, league, season = data["report"], data["defs"], data["league"], data["season"]
        columns = [column for column, _n, _f in defs]
        headings = {column: heading for column, _n, _f, heading in PREVENTION_STATS}
        compare = PREVENTION_RATIO_STAT in columns
        base = ["Pitcher", "GS", "GF", "IP", "R", "ER", "UR"]
        headers = base + [headings[c] for c in columns] + (["MLB Runs per\nNine Innings", "MLB \u00f7 Runs\nper Nine"] if compare else [])
        self.prev_table.clearSpans()
        self.prev_model.clear()
        self.prev_model.setHorizontalHeaderLabels(headers)
        for column, tip in ((1, "Games started"), (2, "Games finished"), (3, "Innings pitched"), (4, "Runs allowed"),
                            (5, "Earned runs allowed"), (6, "Unearned runs allowed (runs \u2212 earned runs)")):
            self.prev_model.horizontalHeaderItem(column).setToolTip(tip)
        band_brush, total_brush = self.palette().midlight(), self.palette().alternateBase()

        def figure_cells(label, fig, bold=False, background=None):
            cells = [self._st_cell(label, right=False, bold=bold),
                     self._st_cell(f"{fig['gs']:.0f}"), self._st_cell(f"{fig['gf']:.0f}"),
                     self._st_cell(mlbstats.outs_to_innings(fig["outs"])), self._st_cell(f"{fig['r']:.0f}"),
                     self._st_cell(f"{fig['er']:.0f}"), self._st_cell(f"{fig['ur']:.0f}")]
            values = fig["stats"]
            cells += [self._st_cell("n/a" if values.get(c) is None else f"{values[c]:.2f}", bold=bold) for c in columns]
            if compare:
                league_value = league.get(PREVENTION_RATIO_STAT)
                cells += [self._st_cell("" if league_value is None else f"{league_value:.2f}", bold=bold),
                          self._prev_ratio_cell(values.get(PREVENTION_RATIO_STAT), league_value, label, season)]
            if background is not None:
                for item in cells:
                    item.setBackground(background)
            return cells

        last_lineup = max(i for i, e in enumerate(report["roles"]) if e["role"] in mlbstats.PITCHER_LINEUP_ROLES)
        for index, entry in enumerate(report["roles"]):
            total = entry["total"]
            row = self.prev_model.rowCount()
            noun = "pitcher" if total["count"] == 1 else "pitchers"
            band = [self._st_cell((f"{entry['role']} \u2014 {total['count']} {noun}" if total["count"]
                                   else f"{entry['role']} \u2014 none") if col == 0 else "", right=False, bold=True)
                    for col in range(len(headers))]
            for item in band:
                item.setBackground(band_brush)
            self.prev_model.appendRow(band)
            self.prev_table.setSpan(row, 0, 1, len(headers))
            for pitcher in entry["pitchers"]:
                self.prev_model.appendRow(figure_cells(pitcher["name"], pitcher))
            if total["count"]:
                self.prev_model.appendRow(figure_cells(f"{entry['role']} total", total, bold=True, background=total_brush))
            if index == last_lineup and report["lineup"]["count"]:  # the three "lineup" roles together, before the rest
                self.prev_model.appendRow(figure_cells(self.PREV_LINEUP_LABEL, report["lineup"], bold=True,
                                                       background=band_brush))
        everyone = report["all"]
        self.prev_model.appendRow(figure_cells("All pitchers", everyone, bold=True, background=band_brush))

        averages = report["averages"]
        self.prev_averages.setText(
            f"Team averages ({averages['pitchers']} pitchers):  GS {averages['gs']:.1f} among the "
            f"{averages['starters']} who started  \u00b7  GF {averages['gf']:.1f} among the "
            f"{averages['finishers']} who finished  \u00b7  IP outside the Rotation "
            f"{mlbstats.outs_to_innings(averages['relief_outs'])}")
        league_value = league.get(PREVENTION_RATIO_STAT)
        team_value, lineup_value = everyone["stats"].get(PREVENTION_RATIO_STAT), report["lineup"]["stats"].get(PREVENTION_RATIO_STAT)
        if compare and team_value is not None and league_value:
            def pct_html(value):
                if not value:
                    return "<b style='color:#2e9e4f'>\u2014 (no runs allowed)</b>"
                pct = round(league_value / value * 100, 1)  # judged on the figure as shown
                return f"<b style='color:{'#2e9e4f' if pct >= 100.0 else '#d9534f'}'>{pct:.1f}%</b>"

            text = f"{data['team_name']} {season}: "
            small = ""
            if lineup_value is not None:
                text += f"Rotation, Long Relief and Closers {pct_html(lineup_value)}  \u00b7  entire pitching roster "
                small = (f"Rotation + Long Relief + Closers ({report['lineup']['count']} pitchers): {lineup_value:.2f} runs per nine.  "
                         f"All {everyone['count']} pitchers: ")
            else:
                text += "all pitchers "
            text += f"{pct_html(team_value)} (MLB's average runs per nine innings \u00f7 theirs; above 100% is fewer runs allowed)."
            small += f"{team_value:.2f} runs per nine, against MLB's {league_value:.2f}."
            self.prev_summary.setText(text + f"<br><span style='font-size:small'>{small}</span>")
        else:
            self.prev_summary.setText(f"{data['team_name']} {season}")
        self._set_prev_status(self._prev_missing_message())
        self._prev_used = defs
        self._fit_table_width(self.prev_table)

    def _fit_table_width(self, table):
        """No wider than its columns (plus the scroll bar) -- the height takes what's left."""
        table.resizeColumnsToContents()
        table.setFixedWidth(table.horizontalHeader().length() + 2 * table.frameWidth()
                            + table.verticalScrollBar().sizeHint().width() + 4)

    # ---- holds and saves tab --------------------------------------------------------
    BLOWN_TIPS = {
        "holds": "Blown holds (this tab's own stat, not MLB's): MLB blown saves by a reliever who handed the game on "
                 "to another, when his team later took the lead back",
        "saves": "Blown saves: MLB's blown saves, less the blown holds -- the lead lost and never won back, or lost "
                 "by the pitcher who finished the game",
        "official": "MLB's official blown saves: blown holds and blown saves excluding blown holds together (before 1999, "
                    "when MLB's records have none, counted by MLB's definition)"}

    def _build_holds_saves_tab(self) -> QWidget:
        """The Prevention tab's companion: how its roles work together, from Retrosheet's game records
        (see :func:`retrosheet.parse_staff`) and MLB's official holds, saves and blown saves (see
        :func:`mlbstats.holds_saves_report`).  Rotation: innings per start, and which relievers (every role)
        followed each starter -- right after him or later -- for how long, and how
        the team did.  Long Relief and Closers: innings per appearance, appearances begun in a save
        situation and innings in them, and the holds or saves those turned into.  Team, season and
        roles are the Prevention tab's; nothing is fetched until this tab is on screen."""
        self._hs_shown = None   # (team_id, season) of the figures on screen or loading
        self._hs_report = None
        self.hs_source = QLabel()
        self.hs_source.setWordWrap(True)
        self.hs_status = QLabel()
        self.hs_status.setWordWrap(True)
        self.hs_status.setVisible(False)

        def table(model):
            view = QTableView()
            view.setModel(model)
            view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
            view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
            view.verticalHeader().setVisible(False)
            view.horizontalHeader().setHighlightSections(False)
            return view

        self.hs_rot_model, self.hs_follow_model = QStandardItemModel(self), QStandardItemModel(self)
        self.hs_lr_model, self.hs_cl_model = QStandardItemModel(self), QStandardItemModel(self)
        self.hs_ot_model = QStandardItemModel(self)
        self.hs_ot_table = table(self.hs_ot_model)
        self.hs_rot_table, self.hs_follow_table = table(self.hs_rot_model), table(self.hs_follow_model)
        self.hs_lr_table, self.hs_cl_table = table(self.hs_lr_model), table(self.hs_cl_model)
        self.hs_rot_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.hs_rot_table.selectionModel().currentRowChanged.connect(lambda cur, _prev: self._hs_show_followers(cur.row()))
        self.hs_follow_title = QLabel()

        def instructions(text):
            label = QLabel(text)
            label.setWordWrap(True)
            return label

        rot_page, bullpen_page = QWidget(), QWidget()
        rl = QVBoxLayout(rot_page)
        rl.addWidget(instructions(
            "Click a starter to choose him (or a total row, for a group of starters): the bullpen that supported "
            "him -- the relievers who worked in his starts -- is shown on the Bullpen/Starter support sub-tab."))
        rl.addWidget(self.hs_rot_table, 1, Qt.AlignmentFlag.AlignLeft)
        bl = QVBoxLayout(bullpen_page)
        bl.addWidget(instructions(
            "This shows the bullpen for the starter chosen on the Rotation sub-tab. To see another starter's "
            "supporting bullpen, choose that starter on the Rotation sub-tab."))
        bl.addWidget(self.hs_follow_title)
        bl.addWidget(self._hs_scroll_box(self.hs_follow_table), 1)
        lr_page, cl_page, ot_page = QWidget(), QWidget(), QWidget()
        QVBoxLayout(lr_page).addWidget(self._hs_scroll_box(self.hs_lr_table), 1)
        QVBoxLayout(cl_page).addWidget(self._hs_scroll_box(self.hs_cl_table), 1)
        QVBoxLayout(ot_page).addWidget(self._hs_scroll_box(self.hs_ot_table), 1)
        self.hs_tabs = QTabWidget()
        self.hs_tabs.addTab(rot_page, "Rotation")
        self.hs_tabs.addTab(bullpen_page, "Bullpen/Starter support")
        self.hs_tabs.addTab(lr_page, "Long Relief")
        self.hs_tabs.addTab(cl_page, "Closers")
        self.hs_tabs.addTab(ot_page, "Other")

        note = QLabel(
            "Roles: the Prevention tab's.  IP: true innings (5.50 = 5\u00bd).  Save situations, holds, saves and blown saves "
            "follow MLB's current rules: MLB's own figures, counted by those rules where its records have none (holds and "
            "blown saves before 1999) or used older rules (saves before 1975).  "
            "Blown holds are a custom stat: blown saves by a reliever who handed the game on, when his team later took the "
            "lead back (hover the column headings).  Game records from Retrosheet (retrosheet.org), obtained free of "
            "charge and copyrighted by Retrosheet.")
        note.setWordWrap(True)

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addWidget(self.hs_source)
        outer.addWidget(self.hs_status)
        outer.addWidget(self.hs_tabs, 1)
        outer.addWidget(note)
        self._hs_refresh()
        return page

    @staticmethod
    def _hs_scroll_box(view) -> QWidget:
        """``view`` with its horizontal scrollbar on top (:func:`_top_scrollbar`), in a box that is never
        wider than the table's columns (:meth:`_hs_fit`) but narrows to the window, scrolling sideways."""
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(_top_scrollbar(view))
        column.addWidget(view, 1)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(box, 1)
        row.addStretch(0)
        view._hs_box = box
        return holder

    def _hs_fit(self, view):
        """Size a Bullpen Support table to its columns -- at most; the window may make it narrower."""
        view.resizeColumnsToContents()
        width = (view.horizontalHeader().length() + 2 * view.frameWidth()
                 + view.verticalScrollBar().sizeHint().width() + 4)
        view.setMinimumWidth(0)
        view.setMaximumWidth(width)
        view._hs_box.setMaximumWidth(width)

    def _set_hs_status(self, text: str):
        self.hs_status.setText(text)
        self.hs_status.setVisible(bool(text))

    def _hs_refresh(self):
        """The Prevention tab's team or season changed (or this tab came on screen): say where the
        figures come from, and load them if this tab is showing and they're for another team-season."""
        if not hasattr(self, "hs_source"):
            return  # (the Prevention tab is built first)
        data = self._prev_roster
        if data is None:
            self.hs_source.setText("<b>No team yet</b> \u2014 choose a team and season on the Prevention tab.")
        else:
            _tid, team_name, season, rows = data
            roles = mlbstats.classify_pitchers(rows)["roles"]
            counts = ", ".join(f"{role} {len(roles[role])}" for role in mlbstats.PITCHER_ROLES)
            self.hs_source.setText(f"<b>From the Prevention tab:</b> {html.escape(team_name)} {season}, its roles "
                                   f"({counts}).  Change the team or season there.")
        if self._current_tab_key() != "hs":
            return
        key = None if data is None else (data[0], data[2])
        if key == self._hs_shown:
            return
        self._hs_shown = key
        self._tag_tokens["hs_data"] = self._tag_tokens.get("hs_data", 0) + 1  # drop a load in flight
        if data is None:
            self._hs_clear("")
            return
        team_id, team_name, season, rows = data
        ttl = LIVE_TTL if season >= self._season_now else None
        self._set_hs_status("" if mlbstats.retro_staff_is_cached(season) else
                            f"Getting Retrosheet's {season} game records (once, a few seconds)\u2026")
        self._run_latest("hs_data", lambda: dict(mlbstats.holds_saves_report(team_id, season, rows, ttl),
                                                 sample=mlbstats.retro_sample(team_id, season, ttl)),
                         lambda report: self._on_hs_data(team_name, season, report), self._on_hs_failed)

    def _hs_clear(self, message: str):
        for model in (self.hs_rot_model, self.hs_follow_model, self.hs_lr_model, self.hs_cl_model, self.hs_ot_model):
            model.clear()
        self.hs_follow_title.setText("")
        self._hs_report = None
        self._set_hs_status(message)

    def _on_hs_failed(self, message: str):
        self._hs_shown = None  # so coming back tries again
        self._hs_clear(f"Couldn't get the pitchers' games: {message}")
        self._say("hs", f"Bullpen Support: {message}")

    @staticmethod
    def _hs_ip(outs) -> str:
        return "" if outs is None else f"{outs / 3:.2f}"

    @staticmethod
    def _hs_pct(part, whole) -> str:
        return f"{100 * part / whole:.0f}%" if whole else ""

    HS_DIFF_TIP = ("Team W\u2013L % minus his W\u2013L %: above zero, the team did better in these games than his own "
                   "decisions; below, worse.  Blank when he had no decisions.")

    # Long Relief, Closers and Other: the season-wide record of the games each pitched in relief -- the
    # same figures the Total row's bullpen shows on Bullpen/Starter support, for every game, not one starter's
    HS_RELIEF_RECORD = [
        ("W\u2013L", "His own wins and losses in relief (the official scorer's decisions)"),
        ("W\u2013L %", "Wins \u00f7 (wins + losses)"),
        ("No decision,\nno hold/save,\nteam won", "Relief appearances with no decision and no hold, blown hold, "
                                                  "save or blown save (those are in the hold and save columns), that "
                                                  "the team won -- every game of the season, not one starter's"),
        ("No decision,\nno hold/save,\nteam lost", "... and that the team lost"),
        ("Team W\u2013L\nwhen he\nrelieved", "The team's record in every game he pitched in relief, his hold and "
                                            "save games included"),
        ("Team\nW\u2013L %", ""),
        ("Differential", "Team W\u2013L % minus his W\u2013L %: above zero, the team did better in these games than "
                         "his own decisions; below, worse.  Blank when he had no decisions."),
    ]

    def _hs_diff(self, r: dict) -> QStandardItem:
        """Team W-L % less his own (``dw`` / ``dl``), signed like +.026 -- blank without both."""
        if not r["dw"] + r["dl"] or not r["w"] + r["l"]:
            return self._st_cell("")
        diff = r["w"] / (r["w"] + r["l"]) - r["dw"] / (r["dw"] + r["dl"])
        text = f"{abs(diff):.3f}".lstrip("0") if abs(diff) < 1 else f"{abs(diff):.3f}"
        return self._st_cell(("+" if diff > 0 else "\u2212" if diff < 0 else "") + text)

    def _hs_wl(self, w: int, l: int) -> list:
        return [self._st_cell(f"{w}\u2013{l}"), self._st_cell(f"{w / (w + l):.3f}".lstrip("0") if w + l else "")]

    def _on_hs_data(self, team_name: str, season: int, report: dict):
        self._hs_season = season
        status = [("Not found in Retrosheet's records: " + ", ".join(report["unmatched"]) + ".")
                  if report["unmatched"] else ""]
        if report.get("sample") is not None:
            status.append(mlbstats.sample_note(report["sample"], team_name, season))
        self._set_hs_status("  ".join(t for t in status if t))
        self._hs_report = report
        total_brush = self.palette().alternateBase()

        def bold_row(cells):
            for item in cells:
                item.setBackground(total_brush)
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            return cells

        # Rotation: one line per starter, then the role
        m = self.hs_rot_model
        m.clear()
        m.setHorizontalHeaderLabels(["Starter", "Starts", "IP per start", "W\u2013L", "W\u2013L %", "No decision,\nteam won",
                                     "No decision,\nteam lost", "Team W\u2013L in\nhis starts", "Team\nW\u2013L %", "Differential"])
        m.horizontalHeaderItem(9).setToolTip(self.HS_DIFF_TIP)
        for col, tip in ((3, "His own wins and losses in his starts: the official scorer's decisions"),
                         (4, "His winning percentage in his starts: wins \u00f7 (wins + losses)"),
                         (5, "Starts he left without a decision that the team went on to win"),
                         (6, "Starts he left without a decision that the team went on to lose"),
                         (7, "The team's record in his starts: his wins + no-decisions won, his losses + no-decisions lost")):
            m.horizontalHeaderItem(col).setToolTip(tip)

        def starter_cells(label, r):
            return [self._st_cell(label, right=False), self._st_cell(r["starts"]), self._st_cell(self._hs_ip(r["avg_outs"])),
                    *self._hs_wl(r["dw"], r["dl"]), self._st_cell(r["nd_w"]), self._st_cell(r["nd_l"]),
                    *self._hs_wl(r["w"], r["l"]), self._hs_diff(r)]

        # each row, and what clicking it shows the bullpen for: a starter, then the groups
        self._hs_rows = []
        for r in report["rotation"]:
            m.appendRow(starter_cells(r["name"], r))
            self._hs_rows.append((f"<b>{html.escape(r['name'])}</b> in his {r['starts']} starts", r))
        rot = report["totals"]["Rotation"]
        m.appendRow(bold_row(starter_cells("Rotation", rot)))
        self._hs_rows.append((f"<b>the Rotation</b> in its {rot['starts']} starts", rot))
        others = report["other_starters"]
        if others["starts"]:
            m.appendRow(starter_cells("Other starters", others))
            m.item(m.rowCount() - 1, 0).setToolTip("Starts by pitchers outside the Rotation: " + ", ".join(others["names"]))
            self._hs_rows.append((f"<b>the other starters</b> ({html.escape(', '.join(others['names']))}) in their "
                                  f"{others['starts']} starts", others))
        every = report["all_starts"]
        m.appendRow(bold_row(starter_cells("Total", every)))
        self._hs_rows.append((f"<b>every starter</b> \u2014 all {every['starts']} games", every))
        self._fit_table_width(self.hs_rot_table)
        self.hs_rot_table.resizeRowsToContents()

        # from 1975 the official saves are by today's rule, so one saves column says both
        modern = season >= mlbstats.CURRENT_SAVE_RULE_FROM

        def relief(model, view, pitchers, total, label, kinds):
            """A role's relievers and the role's total, with their holds and saves (``kinds``)."""
            nouns = [k.capitalize() for k in kinds]
            all_heads = [("MLB official saves\n(current rule)" if modern and kind == "save" else f"{n}s (all,\ncurrent rule)")
                         for kind, n in zip(kinds, nouns)]
            model.clear()
            model.setHorizontalHeaderLabels(["Pitcher", "GS", "Relief G", "IP per G", "Save\nsituations", "IP per\nsituation",
                                             *(f"{n}s from\nthem" for n in nouns), "Converted",
                                             "Blown\nholds", "Blown saves\nexcluding\nblown holds", "MLB official\nblown saves",
                                             *all_heads, *([] if modern else ["MLB official saves\n(rule of the time)"]),
                                             *(h for h, _tip in self.HS_RELIEF_RECORD)])
            k = len(kinds)
            tips = [(1, "Games he started for the team (the figures to the right are his relief appearances only)"),
                    (2, "Relief appearances"), (4, "Appearances begun in a save situation (MLB's definition)")]
            tips += [(6 + i, f"MLB's official {kind}s earned in those appearances") for i, kind in enumerate(kinds)]
            tips += [(6 + k, " + ".join(f"{n}s" for n in nouns) + " from save situations \u00f7 save situations"),
                     (7 + k, self.BLOWN_TIPS["holds"]), (8 + k, self.BLOWN_TIPS["saves"]), (9 + k, self.BLOWN_TIPS["official"])]
            tips += [(10 + k + i, f"All his {kind}s by the current rule (MLB's own" + (", and saves before 1975 counted by "
                     "today's rule 9.19 -- they were credited under looser rules then" if kind == "save" else
                     ", and before 1999 counted by its definition") + ")") for i, kind in enumerate(kinds)]
            if not modern:
                tips.append((10 + 2 * k, "MLB's official saves as its records have them, by the rule of the time: today's "
                                            "rule from 1975, a stricter version in 1973-74, and from 1969 to 1972 any reliever "
                                            "who finished a win with the lead.  Before 1969, when there was no save, MLB's "
                                            "saves were worked out later by the 1969 rule."))
            first = model.columnCount() - len(self.HS_RELIEF_RECORD)
            tips += [(first + i, tip) for i, (_h, tip) in enumerate(self.HS_RELIEF_RECORD)]
            for col, tip in tips:
                model.horizontalHeaderItem(col).setToolTip(tip)

            def cells(name, f, bold=False):
                got = [f[f"sit_{kind}s"] for kind in kinds]
                row = [self._st_cell(name, right=False), self._st_cell(f["gs"]), self._st_cell(f["apps"]),
                       self._st_cell(self._hs_ip(f["avg_outs"])),
                       self._st_cell(f["sit"]), self._st_cell(self._hs_ip(f["avg_sit_outs"])),
                       *(self._st_cell(n) for n in got), self._st_cell(self._hs_pct(sum(got), f["sit"])),
                       self._st_cell(f["blown_holds"]), self._st_cell(f["blown_saves"]), self._st_cell(f["blown"]),
                       *(self._st_cell(f[f"{kind}s"]) for kind in kinds),
                       *([] if modern else [self._st_cell(f["sv_mlb"])]),
                       *self._hs_wl(f["dw"], f["dl"]), self._st_cell(f["nd_w"]), self._st_cell(f["nd_l"]),
                       *self._hs_wl(f["w"], f["l"]), self._hs_diff(f)]
                return bold_row(row) if bold else row

            for f in pitchers:
                if f["apps"] or f["gs"]:
                    model.appendRow(cells(f["name"], f))
            model.appendRow(cells(label, total, bold=True))
            self._hs_fit(view)

        relief(self.hs_lr_model, self.hs_lr_table, report["long_relief"], report["totals"]["Long Relief"], "Long Relief",
               ["hold", "save"])
        relief(self.hs_cl_model, self.hs_cl_table, report["closers"], report["totals"]["Closers"], "Closers", ["hold", "save"])
        relief(self.hs_ot_model, self.hs_ot_table, report["other"], report["totals"]["Other"], "Other", ["hold", "save"])
        if report.get("sample") is not None:
            for model in (self.hs_rot_model, self.hs_lr_model, self.hs_cl_model, self.hs_ot_model):
                self._star_sample(model, report["sample"])
        if report["rotation"]:
            self.hs_rot_table.selectRow(0)
            self._hs_show_followers(0)
        self._say("hs", f"Bullpen Support: {team_name} {season}")

    def _hs_show_followers(self, row: int):
        """Every pitcher who followed the starter on ``row`` in his starts, with his Prevention role."""
        report = self._hs_report
        m = self.hs_follow_model
        m.clear()
        rows = getattr(self, "_hs_rows", [])
        if not report or not 0 <= row < len(rows):
            self.hs_follow_title.setText("No starter chosen: choose one on the Rotation sub-tab to see who followed him.")
            return
        what, starter = rows[row]
        self.hs_follow_title.setText(f"The bullpen with {what} \u2014 every reliever, with his role on the Prevention tab, "
                                     "most games first:")
        modern = getattr(self, "_hs_season", 0) >= mlbstats.CURRENT_SAVE_RULE_FROM
        headers = [("Reliever", ""), ("Role", "His role on the Prevention tab"),
                   ("Right after\nhim", "Times he came in right after the starter"),
                   ("Later in\nthe game", "Times he came in later in the starter's games"), ("Games", ""),
                   ("IP per game\nwith this\npitcher", "His innings per game in this starter's starts"),
                   ("Holds", "Holds by the current rule (MLB's own from 1999; before that, counted by its definition)"),
                   ("Blown\nholds", self.BLOWN_TIPS["holds"]),
                   *([("MLB official\nsaves (current\nrule)", "MLB's official saves, by today's rule 9.19")] if modern else
                     [("Saves\n(current\nrule)", "Saves by today's rule 9.19 (MLB's own from 1975; before that, counted by it)"),
                      ("MLB official\nsaves (rule\nof the time)", "MLB's official saves, by the rule of the time (before "
                                                                  "1969, MLB's retroactive saves by the 1969 rule)")]),
                   ("Blown saves\nexcluding\nblown holds", self.BLOWN_TIPS["saves"]),
                   ("MLB official\nblown saves", self.BLOWN_TIPS["official"]),
                   ("W\u2013L", "The reliever's own wins and losses in this starter's starts (the official scorer's decisions)"),
                   ("W\u2013L %", "His winning percentage in those games: wins \u00f7 (wins + losses)"),
                   ("No decision,\nno hold/save,\nteam won",
                    "Games in this starter's starts the reliever pitched in without a decision and without a hold, "
                    "blown hold, save or blown save (those games are in the hold and save columns), that the team won"),
                   ("No decision,\nno hold/save,\nteam lost", "... and that the team lost"),
                   ("Team W\u2013L\nwith both", "The team's record in every one of this starter's starts the reliever "
                                                "pitched in, his hold and save games included"),
                   ("Team\nW\u2013L %", ""), ("Differential", self.HS_DIFF_TIP)]
        m.setHorizontalHeaderLabels([h for h, _t in headers])
        for col, (_h, tip) in enumerate(headers):
            if tip:
                m.horizontalHeaderItem(col).setToolTip(tip)
        for f in starter["followers"]:
            m.appendRow([self._st_cell(f["name"], right=False), self._st_cell(f["role"], right=False),
                         self._st_cell(f["direct"] or ""), self._st_cell(f["later"] or ""), self._st_cell(f["games"]),
                         self._st_cell(self._hs_ip(f["avg_outs"])), self._st_cell(f["holds"] or ""),
                         self._st_cell(f["blown_holds"] or ""), self._st_cell(f["saves"] or ""),
                         *([] if modern else [self._st_cell(f["sv_mlb"] or "")]),
                         self._st_cell(f["blown_saves"] or ""), self._st_cell(f["blown"] or ""),
                         *self._hs_wl(f["dw"], f["dl"]), self._st_cell(f["nd_w"]), self._st_cell(f["nd_l"]),
                         *self._hs_wl(f["w"], f["l"]), self._hs_diff(f)])
        if report.get("sample") is not None:
            self._star_sample(m, report["sample"])
        self._hs_fit(self.hs_follow_table)

    # ---- speed tab ------------------------------------------------------------------
    def _build_speed_tab(self) -> QWidget:
        """A team's runners in one season, from Retrosheet's play-by-play: stolen bases, extra
        bases taken on hits, bases gained tagging up on fly outs, and their sum, Bases Gained --
        the last three player-level custom stats in ``SPEED_STATS`` built from Retrosheet counts
        (see retrosheet.py).  Bases Gained per opportunity (a chance to steal, take an extra base
        or tag up) is compared with all of MLB's that season as on the Production tab; runners are
        listed most Opportunities first, leaving out any with none.  Team and Season are chosen as
        on the Range, Production and Prevention tabs."""
        self.speed_picker = _PlayerPicker(self, "speed_pick", "hitting", "Team and season", with_player=False)
        self.speed_picker.roster_loaded.connect(self._on_speed_roster)
        self._speed_roster = None    # (team_id, team_name, season, rows) the picker last loaded
        self._speed_used = None      # the custom-stat definitions the table on screen was drawn with

        self.speed_summary = QLabel("Choose a team and season above.")
        self.speed_summary.setTextFormat(Qt.TextFormat.RichText)
        self.speed_summary.setWordWrap(True)
        font = self.speed_summary.font()
        font.setPointSize(font.pointSize() + 2)
        self.speed_summary.setFont(font)
        self.speed_status = QLabel()
        self.speed_status.setWordWrap(True)
        self.speed_status.setVisible(False)

        self.speed_model = QStandardItemModel(self)
        self.speed_table = QTableView()
        self.speed_table.setModel(self.speed_model)
        self.speed_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.speed_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.speed_table.verticalHeader().setVisible(False)
        self.speed_table.horizontalHeader().setHighlightSections(False)
        self.speed_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

        note = QLabel(
            "Extra Bases: bases taken beyond the hit.  Tag Ups: bases gained on fly outs.  Regulars have at least the "
            "team's average opportunities per runner; among them Speedy is 125% or more of MLB's bases gained per "
            "opportunity, Slow 75% or less, Smart in between.  Speedy Bench: below the regulars' cut-off but at least a fifth "
            "of MLB's average opportunities per runner that season, and Speedy; Other is everyone else.  Play-by-play from Retrosheet "
            "(retrosheet.org), obtained free of charge and copyrighted by Retrosheet.")
        note.setWordWrap(True)

        page = QWidget()
        outer = QVBoxLayout(page)
        top = QHBoxLayout()
        top.addWidget(self.speed_picker)
        top.addStretch(1)
        outer.addLayout(top)
        outer.addWidget(self.speed_summary)
        outer.addWidget(self.speed_status)
        outer.addWidget(self.speed_table, 1, Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(note)
        return page

    def _speed_definitions(self) -> list[tuple[str, str, str]]:
        """``[(column, custom stat name, formula), ...]`` for each ``SPEED_STATS`` stat defined at
        Player level, in column order."""
        found = []
        for column, stat_name, _file in SPEED_STATS:
            for level, name, ftext in self._custom_stats:
                if level == "player" and name.lower() == stat_name.lower():
                    found.append((column, stat_name, ftext))
                    break
        return found

    def _speed_missing_message(self) -> str:
        have = {column for column, _n, _f in self._speed_definitions()}
        missing = [(name, file) for column, name, file in SPEED_STATS if column not in have and column != "OPP"]
        if not missing:
            return ""
        return ("The Speed tab needs the player-level custom stat(s) "
                + ", ".join(f"\u201c{name}\u201d" for name, _f in missing) + " \u2014 load "
                + ", ".join(file for _n, file in missing) + " on the Custom tab (Load definitions\u2026).")

    def _set_speed_status(self, text: str):
        self.speed_status.setText(text)
        self.speed_status.setVisible(bool(text))

    def _clear_speed(self, message: str = ""):
        self.speed_model.clear()
        self.speed_summary.setText("Choose a team and season above." if not message else "")
        self._set_speed_status(message)
        self._speed_used = self._speed_definitions()

    def _refresh_speed(self):
        """The custom stats changed: redraw if a definition the table uses did."""
        if self._speed_roster is not None and self._speed_definitions() != self._speed_used:
            self._on_speed_roster(self._speed_roster)

    def _on_speed_roster(self, data):
        """The picker's team and season have (re)loaded: get the runners from Retrosheet.  The first
        time a season is used its play-by-play is downloaded (4-10 MB) and read, which takes a few
        seconds, so that -- and only that -- is announced; the table on screen stays until the new
        one is ready."""
        self._speed_roster = data
        self._tag_tokens["speed_data"] = self._tag_tokens.get("speed_data", 0) + 1  # drop a load in flight
        defs = self._speed_definitions()
        if data is None:
            self._clear_speed("Couldn't load that team and season.")
            return
        if not defs:
            self._clear_speed(self._speed_missing_message())
            return
        team_id, team_name, season, _rows = data
        ttl = LIVE_TTL if season >= self._season_now else None
        stats = {column: ftext for column, _n, ftext in defs}
        if not mlbstats.retro_is_cached(season):
            self._set_speed_status(f"Getting Retrosheet's {season} play-by-play (once, a few seconds)\u2026")

        def fn():
            rows = mlbstats.get_player_stats(team_id, season, "retrosheet", ttl)
            groups = {mlbstats.RETRO_VIEW: mlbstats.group_by_player(rows)}
            pids = list(groups[mlbstats.RETRO_VIEW])
            opp_formula = stats.pop("OPP", None)  # the Opportunities custom stat, if defined

            def opportunities(groups_by_view, pid_list, rows_of_them) -> float:
                if opp_formula:
                    return mlbstats.pooled_stat(opp_formula, groups_by_view, pid_list) or 0
                return sum(retrosheet.opportunities(r) for r in rows_of_them)

            def figures(name: str, pid_list, rows_of_them) -> dict:
                """A runner's, or a group's (pooled), counts and custom stats."""
                values = {column: mlbstats.pooled_stat(ftext, groups, pid_list) for column, ftext in stats.items()}
                return {"name": name, "sb": sum(r["sb"] for r in rows_of_them),
                        "opps": opportunities(groups, pid_list, rows_of_them), "stats": values,
                        "bg": values.get("BG")}

            runners = []
            for pid in pids:
                mine = groups[mlbstats.RETRO_VIEW][pid]
                runners.append(dict(figures(mine[0]["_name"], [pid], mine), id=pid))
            total = figures("All runners", pids, rows)
            league_rows = mlbstats.league_view_rows(season, mlbstats.RETRO_VIEW, ttl)
            league_groups = {mlbstats.RETRO_VIEW: {"league": league_rows}}
            league = {column: mlbstats.pooled_stat(ftext, league_groups, ["league"]) for column, ftext in stats.items()}
            league["opps"] = opportunities(league_groups, ["league"], league_rows)
            # MLB's average opportunities per runner (each runner-team line that had a chance), the norm the bench is judged on
            each = {mlbstats.RETRO_VIEW: {i: [row] for i, row in enumerate(league_rows)}}
            per_runner = [n for n in (opportunities(each, [i], [row]) for i, row in enumerate(league_rows)) if n >= 1]
            league_average_opps = sum(per_runner) / len(per_runner) if per_runner else None
            # Speedy / Smart / Slow regulars and the rest, each with its pooled subtotal (needs Bases Gained)
            grouped, average_opps, bench_floor = None, None, None
            league_rate = league["BG"] / league["opps"] if league.get("BG") is not None and league["opps"] else None
            if league_rate:
                listed = [r for r in runners if r["opps"] >= 1]
                classified = mlbstats.classify_runners(listed, league_rate, league_average_opps)
                average_opps, bench_floor = classified["average_opps"], classified["bench_floor"]
                grouped = []
                for label in mlbstats.RUNNER_GROUPS:
                    members = classified["groups"][label]
                    member_ids = [m["id"] for m in members]
                    member_rows = [row for pid in member_ids for row in groups[mlbstats.RETRO_VIEW][pid]]
                    grouped.append({"group": label, "runners": members,
                                    "total": figures(f"{label} total", member_ids, member_rows) if members else None})
            return {"team_name": team_name, "season": season, "defs": defs, "runners": runners,
                    "total": total, "league": league, "groups": grouped, "average_opps": average_opps,
                    "bench_floor": bench_floor, "sample": mlbstats.retro_sample(team_id, season, ttl)}

        self._run_latest("speed_data", fn, self._on_speed_loaded, self._on_speed_failed)

    def _on_speed_failed(self, message: str):
        if self._speed_roster and self._speed_roster[2] >= self._season_now:
            message += f"  The last completed season is {self._season_now - 1}."
        self._clear_speed(message)
        self._say("speed", f"Speed: {message}")

    def _speed_ratio_cell(self, rate, league_rate, name: str, season: int) -> QStandardItem:
        """Bases gained per opportunity as a percentage of MLB's, green at or above it."""
        if rate is None or not league_rate:
            return self._st_cell("")
        ratio = rate / league_rate
        item = self._st_cell(f"{ratio * 100:.1f}%", bold=True,
                             tip=f"{name}: {rate:.3f} \u00f7 all of MLB's {league_rate:.3f} bases gained per "
                                 f"opportunity in {season}")
        item.setForeground(QBrush(QColor("#2e9e4f" if round(ratio * 100, 1) >= 100.0 else "#d9534f")))
        return item

    def _on_speed_loaded(self, data):
        defs, season, league = data["defs"], data["season"], data["league"]
        columns = [column for column, _n, _f in defs if column != "OPP"]
        names = {column: name for column, name, _f in SPEED_STATS}
        compare = "BG" in columns
        headers = (["Runner", "SB"] + [names[c] for c in columns]
                   + ["Opportunities"] + (["Gained per\nOpportunity", "MLB Gained per\nOpportunity",
                                          "Gained per Opp.\n\u00f7 MLB"] if compare else []))
        self.speed_table.clearSpans()
        self.speed_model.clear()
        self.speed_model.setHorizontalHeaderLabels(headers)
        league_rate = (league["BG"] / league["opps"]) if compare and league.get("BG") is not None and league["opps"] else None

        def cells(runner: dict, bold: bool = False, background=None) -> list[QStandardItem]:
            stats = runner["stats"]
            out = [self._st_cell(runner["name"], right=False, bold=bold), self._st_cell(f"{runner['sb']:.0f}", bold=bold)]
            out += [self._st_cell("n/a" if stats.get(c) is None else f"{stats[c]:.0f}", bold=bold) for c in columns]
            out.append(self._st_cell(f"{runner['opps']:.0f}", bold=bold))
            if compare:
                gained = stats.get("BG")
                rate = gained / runner["opps"] if gained is not None and runner["opps"] else None
                out += [self._st_cell("" if rate is None else f"{rate:.3f}", bold=bold),
                        self._st_cell("" if league_rate is None else f"{league_rate:.3f}", bold=bold),
                        self._speed_ratio_cell(rate, league_rate, runner["name"], season)]
            if background is not None:
                for item in out:
                    item.setBackground(background)
            return out

        def gained(runner: dict) -> float:
            return (runner["stats"].get("BG") if compare else runner["sb"]) or 0

        band_brush, total_brush = self.palette().midlight(), self.palette().alternateBase()
        if data["groups"] is not None:
            for entry in data["groups"]:
                members, subtotal = entry["runners"], entry["total"]
                row = self.speed_model.rowCount()
                noun = "runner" if len(members) == 1 else "runners"
                band = [self._st_cell((f"{entry['group']} \u2014 {len(members)} {noun}" if members
                                       else f"{entry['group']} \u2014 none") if col == 0 else "", right=False, bold=True)
                        for col in range(len(headers))]
                for item in band:
                    item.setBackground(band_brush)
                self.speed_model.appendRow(band)
                self.speed_table.setSpan(row, 0, 1, len(headers))
                for runner in members:
                    self.speed_model.appendRow(cells(runner))
                if subtotal:
                    self.speed_model.appendRow(cells(subtotal, bold=True, background=total_brush))
        else:
            # anyone with at least one opportunity, most opportunities first
            listed = sorted((r for r in data["runners"] if r["opps"] >= 1),
                            key=lambda r: (-r["opps"], -gained(r), r["name"]))
            for runner in listed:
                self.speed_model.appendRow(cells(runner))
        self.speed_model.appendRow(cells(data["total"], bold=True, background=band_brush))

        total = data["total"]
        rate = (total["stats"].get("BG") / total["opps"]) if compare and total["stats"].get("BG") is not None and total["opps"] else None
        if rate is not None and league_rate:
            pct = round(rate / league_rate * 100, 1)
            colour = "#2e9e4f" if pct >= 100.0 else "#d9534f"
            star = "*" if data.get("sample") is not None else ""
            text = (f"{data['team_name']} {season}: <b style='color:{colour}'>{pct:.1f}%{star}</b> of the MLB average bases gained "
                    f"per opportunity \u2014 {total['stats']['BG']:.0f} bases on {total['opps']:.0f} opportunities "
                    f"({rate:.3f} against MLB's {league_rate:.3f}).")
            if data["average_opps"] is not None:
                text += (f"<br><span style='font-size:small'>Regulars: at least {data['average_opps']:.0f} opportunities "
                         "(the team's average per runner)")
                if data["bench_floor"] is not None:
                    text += (f".  Speedy bench: fewer than that but at least {data['bench_floor']:.0f} "
                             "(a fifth of MLB's average per runner), and Speedy")
                text += ".</span>"
        else:
            text = f"{data['team_name']} {season}"
        if data.get("sample") is not None:
            self._star_sample(self.speed_model, data["sample"], skip_columns={headers.index("MLB Gained per\nOpportunity")}
                              if compare else ())
            text += ("<br><span style='font-size:small'>"
                     + html.escape(mlbstats.sample_note(data["sample"], data["team_name"], season)) + "</span>")
        self.speed_summary.setText(text)
        notes = []
        if season == self._season_now - 1:
            notes.append(f"{season} is the last completed season; Retrosheet adds a season once it is over.")
        notes.append(self._speed_missing_message())
        self._set_speed_status("  ".join(n for n in notes if n))
        self._say("speed", "")  # no leftover message from an earlier attempt
        self._speed_used = defs
        self._fit_table_width(self.speed_table)

    # ---- standings tab ------------------------------------------------------------
    # ---- Denied tab ---------------------------------------------------
    def _build_denied_tab(self) -> QWidget:
        """The teams that "ought to have won" in the postseason but didn't (``mlbstats.denied_report``):
        each season's best record in MLB (the American and National Leagues, and the 19th-century majors), from 1969 each league's best, from 1995 each
        division's and each league's best wild card, and the Negro leagues' best (1920-1948) -- what
        became of them, and a "Denied" label where they went out in a round they ought to have won.
        Every season is fetched once (standings + postseason, cached) the first time the tab is shown."""
        self.dn_from_spin = _YearSpin()
        self.dn_to_spin = _YearSpin()
        for spin, value in ((self.dn_from_spin, 1876), (self.dn_to_spin, 2100)):
            spin.setRange(1876, 2100)
            spin.setValue(value)
            spin.setKeyboardTracking(False)
            spin.setToolTip("Typing a year needs Enter (or click elsewhere) to take effect.")
            spin.valueChanged.connect(lambda _v: self._dn_render())
        self.dn_denied_only = QCheckBox("Denied/Better Record teams only")
        self.dn_denied_only.setToolTip("The denied teams (red), and the clubs put out by a tiebreaker that had a "
                                       "better record than a later series winner (orange).")
        self.dn_denied_only.toggled.connect(lambda _on: self._dn_render())
        self.dn_stage_combo = QComboBox()
        for label, key in DENIED_STAGE_CHOICES:
            self.dn_stage_combo.addItem(label, key)
        self.dn_stage_combo.currentIndexChanged.connect(lambda _i: self._dn_render())
        self.dn_further = QCheckBox("or further")
        self.dn_further.setToolTip("Also the teams that went past the chosen round.")
        self.dn_further.toggled.connect(lambda _on: self._dn_render())
        self.dn_tiebreak_only = QCheckBox("Tiebreaker teams only")
        self.dn_tiebreak_only.setToolTip("Only the clubs that played a tiebreaker (a pennant playoff or “Game 163”).")
        self.dn_tiebreak_only.toggled.connect(lambda _on: self._dn_render())
        self.dn_negro = QCheckBox("Negro leagues (1920–1948)")
        self.dn_negro.setChecked(True)
        self.dn_negro.toggled.connect(lambda _on: self._dn_render())
        self.dn_19th = QCheckBox(f"19th century (1876–{DENIED_MODERN_FROM - 1})")
        self.dn_19th.setChecked(True)
        self.dn_19th.setToolTip(f"Untick to start at {DENIED_MODERN_FROM}, the usual start of "
                                "\u201cmodern\u201d baseball (the American League's first season as a major league).")
        self.dn_19th.toggled.connect(lambda _on: self._dn_render())

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Seasons"))
        bar.addWidget(self.dn_from_spin)
        bar.addWidget(QLabel("to"))
        bar.addWidget(self.dn_to_spin)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Got as far as"))
        bar.addWidget(self.dn_stage_combo)
        bar.addWidget(self.dn_further)
        bar.addStretch(1)
        filters = QHBoxLayout()  # a second row: one row of everything widened the whole window
        filters.addWidget(self.dn_denied_only)
        filters.addSpacing(6)
        filters.addWidget(self.dn_tiebreak_only)
        filters.addSpacing(6)
        filters.addWidget(self.dn_negro)
        filters.addSpacing(6)
        filters.addWidget(self.dn_19th)
        filters.addStretch(1)

        self.dn_title = QLabel()
        self.dn_title.setWordWrap(True)
        font = self.dn_title.font()
        font.setBold(True)
        self.dn_title.setFont(font)
        self.dn_note = QLabel(
            "Best record in MLB (every season; ties all count): ought to win it all.  From 1969, best "
            "in each league: ought to win the championship series.  From 1995, best in each division and "
            "among each league’s wild cards: ought to win the wild card and division rounds.  Denied = "
            "beaten by an underdog (a club with a worse record) in a round it ought to have won, or never "
            "reached it (no postseason, 1981’s split season); losing to an equal or better record is "
            "no upset, so no denial.  The Negro leagues are judged apart, by their official postseason (the Colored/Negro "
            "World Series, or a league’s championship series in a year without one).  Tiebreakers "
            "(pennant playoffs, “Game 163”) counted as regular-season games — in the W–L "
            "here and in the players’ statistics.  Every club that played one is listed; the clubs were "
            "level, so nobody ought to have won it, but a club put out by one is compared with the later "
            "series winners it had a better record than (shaded orange; shown in the Denied column).  * MLB’s standings leave that season’s "
            "tiebreaker games out; they are added back here.")
        self.dn_note.setWordWrap(True)
        self._dn_rows: list[dict] = []
        self._dn_started = False

        self.dn_model = QStandardItemModel(self)
        self.dn_table = QTableView()
        self.dn_proxy = _NumProxy(self)  # click a header to sort (numbers as numbers)
        self.dn_proxy.setSourceModel(self.dn_model)
        self.dn_table.setModel(self.dn_proxy)
        self.dn_table.setSortingEnabled(True)
        self.dn_table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)  # season order to start with
        self.dn_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.dn_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.dn_table.verticalHeader().setVisible(False)
        self.dn_table.horizontalHeader().setHighlightSections(False)
        self.dn_empty = QLabel()  # stands in for the table when the filters leave no rows
        self.dn_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dn_empty.setWordWrap(True)
        self.dn_empty.setVisible(False)

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(bar)
        outer.addLayout(filters)
        outer.addWidget(self.dn_title)
        outer.addWidget(self.dn_table, 1)
        outer.addWidget(self.dn_empty, 1)
        outer.addWidget(self.dn_note)
        return page

    def _dn_tab_shown(self):
        if not self._dn_started:
            self._dn_started = True
            self._dn_load()

    def _dn_load(self):
        now = max(self._season_now, mlbstats.current_season())
        self.dn_to_spin.blockSignals(True)
        self.dn_to_spin.setMaximum(now)
        self.dn_from_spin.setMaximum(now)
        self.dn_to_spin.setValue(min(self.dn_to_spin.value(), now))
        self.dn_to_spin.blockSignals(False)
        self.dn_title.setText(f"Loading every season, 1876–{now}…  (the first time takes a little while)")
        self._say("dn", "Denied: loading every season’s standings and postseason…")

        def fn():
            def one(season):
                try:
                    return season, mlbstats.get_denied(season, LIVE_TTL if season >= now else None), None
                except mlbstats.MLBStatsError as exc:
                    return season, [], str(exc)
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(one, range(1876, now + 1)))
            rows = [row for _s, season_rows, _e in results for row in season_rows]
            return rows, [season for season, _r, err in results if err]

        self._run_latest("dn_load", fn, self._on_dn_loaded, self._on_dn_failed)

    def _on_dn_loaded(self, data):
        rows, failed = data
        self._dn_rows = rows
        self._dn_render()
        self._say("dn", f"Denied: couldn't load {', '.join(map(str, failed))}." if failed else "")

    def _on_dn_failed(self, message: str):
        self.dn_title.setText(f"Couldn't load the seasons: {message}")
        self._say("dn", f"Denied: {message}")
        self._dn_started = False  # try again next time the tab is shown

    def _dn_render(self):
        stage_key = self.dn_stage_combo.currentData()
        self.dn_further.setEnabled(stage_key not in ("any", "champion", "none"))
        if not self._dn_rows:
            return
        first, last = sorted((self.dn_from_spin.value(), self.dn_to_spin.value()))
        only_19th = not self.dn_19th.isChecked() and last < DENIED_MODERN_FROM
        if not self.dn_19th.isChecked() and not only_19th:
            first = max(first, DENIED_MODERN_FROM)  # the title names the seasons shown
        rows = [r for r in self._dn_rows if first <= r["season"] <= last
                and (self.dn_negro.isChecked() or r["track"] != "Negro")
                and (self.dn_19th.isChecked() or r["season"] >= DENIED_MODERN_FROM)]
        best = [r for r in rows if r["best_in"]]
        denied = [r for r in rows if r["denied"]]

        def reached(r):
            if stage_key == "any":
                return True
            if stage_key == "champion":
                return r["champion"]
            if stage_key == "none":
                return r["stage"] <= mlbstats.DENIED_STAGES["T"]
            level = mlbstats.DENIED_STAGES[stage_key]
            return r["stage"] >= level if self.dn_further.isChecked() else r["stage"] == level

        shown = [r for r in rows if reached(r)
                 and (r["denied"] or r["better_than"] or not self.dn_denied_only.isChecked())
                 and (r["tiebreaker"] or not self.dn_tiebreak_only.isChecked())]
        headers = ["Season", "Team", "League", "W", "L", "Pct", "Best record in", "Postseason", "Denied"]
        extras = []
        if any(r["tiebreaker"] for r in shown):
            extras.append(("Tiebreaker (regular-season games)", lambda r: r["tiebreaker"]))
        header = self.dn_table.horizontalHeader()
        sort_column, sort_order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        self.dn_table.setSortingEnabled(False)
        self.dn_model.clear()
        self.dn_model.setHorizontalHeaderLabels(headers + [h for h, _f in extras])
        shade, tiebreak_shade = QColor("#f8d4d4"), QColor("#fbdcb4")  # denied: light red; tiebreaker: orange
        for r in shown:
            tip = r["record_note"]
            decisions = r["wins"] + r["losses"]
            cells = [self._st_cell(r["season"]),
                     self._st_cell(r["team"], right=False, bold=bool(r["denied"])),
                     self._st_cell(r["league"], right=False),
                     self._st_cell(r["wins"], tip=tip), self._st_cell(r["losses"], tip=tip),
                     self._st_cell(r["pct"] + (" *" if tip else ""), tip=tip),
                     self._st_cell(", ".join(r["best_in"]) or "—", right=False),
                     self._st_cell(r["result"], right=False),
                     self._st_cell(r["denied"], right=False, bold=True) if r["denied"] else
                     # a club put out by a tiebreaker can't be denied: its column holds the comparison
                     self._st_cell("Better record than " + "; ".join(r["better_than"]) if r["better_than"] else "",
                                   right=False)]
            cells += [self._st_cell(fn(r), right=False) for _h, fn in extras]
            keys = [r["season"], r["team"], r["league"], r["wins"], r["losses"],
                    r["wins"] / decisions if decisions else None]
            for cell, key in zip(cells, keys):  # what the sort compares; the rest sort by their text
                cell.setData(key, Qt.ItemDataRole.UserRole)
            for cell in cells[len(keys):]:
                cell.setData(cell.text(), Qt.ItemDataRole.UserRole)
                if cell.text() and not cell.toolTip():
                    cell.setToolTip(cell.text())  # the whole text, if the column runs off the window
            put_out = r["stage"] == mlbstats.DENIED_STAGES["T"]  # put out by a tiebreaker
            if r["denied"] or put_out:
                for cell in cells:
                    cell.setBackground(shade if r["denied"] else tiebreak_shade)
                    cell.setForeground(QColor("#000000"))
            self.dn_model.appendRow(cells)
        self.dn_table.setSortingEnabled(True)
        if 0 <= sort_column < self.dn_model.columnCount():  # keep the user's sort across filter changes
            self.dn_table.sortByColumn(sort_column, sort_order)
        else:
            self.dn_table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        self.dn_table.resizeColumnsToContents()
        if not shown:
            if stage_key == "champion" and self.dn_denied_only.isChecked():
                why = ("A team that won it all can\u2019t have been denied, or put out by a tiebreaker.  Untick "
                       "\u201cDenied/Better Record teams only\u201d to see the best teams that won.")
            elif only_19th:
                why = (f"These seasons are all before {DENIED_MODERN_FROM}.  Tick \u201c19th century\u201d "
                       "to see them.")
            else:
                why = "No team in these seasons matches these filters."
            self.dn_empty.setText(why)
        self.dn_table.setVisible(bool(shown))
        self.dn_empty.setVisible(not shown)
        better = [r for r in rows if r["better_than"] and not r["denied"]]  # put out by a tiebreaker
        seasons = len({r["season"] for r in denied + better})
        self.dn_title.setText(
            f"{first}–{last}: {len(denied) + len(better)} Denied/Better Record teams "
            f"({len(denied)} of {len(best)} best teams denied; {len(better)} put out by a tiebreaker "
            f"with a better record than a later series winner), in {seasons} seasons"
            + (f" — showing {len(shown)} row{'s' if len(shown) != 1 else ''}" if len(shown) != len(rows) else ""))

    # ---- Simulator tab ---------------------------------------------------
    def closeEvent(self, event):
        """Quitting: the Simulator's long jobs (a run, a path analysis's minutes of replays, a round robin) are told to
        stop -- they check between games -- and the window waits a moment for the background work to finish, so
        Python doesn't shut down under a thread still running (that could crash on the way out)."""
        self._sim_run_id = getattr(self, "_sim_run_id", 0) + 1
        self._sim_path_run = getattr(self, "_sim_path_run", 0) + 1
        self._sim_rr_token = getattr(self, "_sim_rr_token", 0) + 1
        self.pool.waitForDone(5000)
        super().closeEvent(event)

    def _build_simulator_tab(self) -> QWidget:
        """Games between two team-seasons, each set up as the Production, Prevention, Range and Speed tabs
        would by default, played plate appearance by plate appearance (see :mod:`simulator`)."""
        self.sim_vis = _PlayerPicker(self, "sim_vis", "fielding", "Team 1", with_player=False)
        self.sim_home = _PlayerPicker(self, "sim_home", "fielding", "Team 2", with_player=False)
        self.sim_games = QSpinBox()
        self.sim_games.setRange(1, 100000)
        self.sim_games.setSingleStep(100)
        self.sim_games.setValue(100)
        self.sim_games.setToolTip("How many games to play (a second or two per thousand; 100 to start)")
        self.sim_seed = QSpinBox()
        self.sim_seed.setRange(1, 999999)
        self.sim_seed.setValue(1)
        self.sim_seed.setToolTip("The same seed and teams play the same games again")
        self.sim_run_btn = QPushButton("Play")
        self.sim_run_btn.clicked.connect(self._run_simulation)
        self.sim_path_btn = QPushButton("Path analysis\u2026")
        self.sim_path_btn.setToolTip("A diagram of what each part of each team -- batting, running, pitching, fielding, "
                                     "and the cards' categories within them -- did to the result: the same games "
                                     "replayed with that part league-average")
        self.sim_path_btn.setEnabled(False)
        self.sim_path_btn.clicked.connect(self._show_sim_path)
        self._sim_path_dialog = None
        self._sim_result = None   # the last run: teams, environment, totals

        self.sim_dh = QComboBox()
        self.sim_dh.addItem("Each team's league's rule that season", "own")
        self.sim_dh.addItem("Designated hitter for both teams", "dh")
        self.sim_dh.addItem("Pitchers bat for both teams", "pitchers")
        self.sim_dh.setToolTip("By default each team plays as its league did that season, so a team whose pitchers "
                               "batted can meet one with a DH; choose one rule for both instead")
        self.sim_fatigue = QComboBox()
        self.sim_fatigue.addItem("Rest days and tired relievers", True)
        self.sim_fatigue.addItem("Everyone plays every game, fresh", False)
        self.sim_fatigue.setToolTip("Regulars sit as often as they really didn't start (the bench man who played his "
                                    "position most plays), and a reliever who pitched the last two games, or long in "
                                    "the last, sits out; or the same nine and a fresh bullpen every game")
        self.sim_format = QComboBox()
        self.sim_format.addItem("Single games, taking turns at home", None)
        for best_of, pattern in simulator.SERIES_FORMATS.items():
            self.sim_format.addItem(f"Best-of-{best_of} series ({pattern})", best_of)
        self.sim_format.setToolTip("Single games, or series: Games is then how many series; the better real record "
                                   "has home field, each club's four busiest starters (or your rotation) go in turn, "
                                   "and nobody takes a day off")
        self.sim_format.currentIndexChanged.connect(
            lambda _i: self.sim_games_label.setText("Series" if self.sim_format.currentData() else "Games"))
        self.sim_games_label = QLabel("Games")
        controls = QFormLayout()
        controls.addRow(self.sim_games_label, self.sim_games)
        controls.addRow("Format", self.sim_format)
        controls.addRow("DH", self.sim_dh)
        controls.addRow("Fatigue", self.sim_fatigue)
        controls.addRow("Seed", self.sim_seed)
        # how far the run has got, beside Play
        self.sim_progress = QProgressBar()
        self.sim_progress.setMinimumWidth(170)
        self.sim_progress.setTextVisible(True)
        self.sim_progress.setRange(0, 1)
        self.sim_progress.setValue(0)
        self.sim_progress.setFormat("")
        self._sim_run_id = 0
        self._sim_progress_sig = _SimProgress()
        self._sim_progress_sig.progress.connect(self._on_sim_progress)
        play_row = QHBoxLayout()
        play_row.addWidget(self.sim_run_btn)
        play_row.addWidget(self.sim_progress, 1)
        controls.addRow("", play_row)
        controls.addRow("", self.sim_path_btn)
        rr_btn = QPushButton("Round robin\u2026")
        rr_btn.setToolTip("Several team-seasons, every pair playing, with standings and head-to-head records")
        rr_btn.clicked.connect(self._show_sim_round_robin)
        controls.addRow("", rr_btn)
        self.sim_export_btn = QPushButton("Export\u2026")
        self.sim_export_btn.setToolTip("Save the run: the result, every game and each team's batting and pitching, "
                                       "as an Excel workbook (a sheet each) or CSV files (one each)")
        self.sim_export_btn.setEnabled(False)
        self.sim_export_btn.clicked.connect(self._export_sim)
        controls.addRow("", self.sim_export_btn)
        top = QHBoxLayout()
        top.addWidget(self.sim_vis)
        top.addWidget(self.sim_home)
        top.addLayout(controls)
        top.addStretch(1)

        self.sim_pages = QTabWidget()
        self.sim_summary = QTextBrowser()
        self.sim_summary.setOpenLinks(False)  # (its one link opens the Denied tab)
        self.sim_summary.anchorClicked.connect(self._on_sim_link)
        self.sim_visitor_view = QTextBrowser()
        self.sim_home_view = QTextBrowser()
        self.sim_how = QTextBrowser()
        self.sim_game_view = QTextBrowser()
        self.sim_game_spin = QSpinBox()
        self.sim_game_spin.setRange(1, 1)
        self.sim_game_spin.setToolTip("Which of the games played to show, every event with the numbers behind it")
        self.sim_game_spin.valueChanged.connect(lambda _v: self._show_sim_game())
        game_page = QWidget()
        game_lay = QVBoxLayout(game_page)
        game_bar = QHBoxLayout()
        game_bar.addWidget(QLabel("Game"))
        game_bar.addWidget(self.sim_game_spin)
        self.sim_game_show = QComboBox()
        self.sim_game_show.addItems(["Play by play", "Box score"])
        self.sim_game_show.setToolTip("Every plate appearance with the numbers behind it, or the game's box score as "
                                      "the Boxscore tab draws one")
        self.sim_game_show.currentIndexChanged.connect(lambda _i: self._show_sim_game())
        game_bar.addWidget(QLabel("Show"))
        game_bar.addWidget(self.sim_game_show)
        game_bar.addStretch(1)
        game_lay.addLayout(game_bar)
        self.sim_wp_chart = wpachart.WPAChart()  # the home team's chance of winning, plate appearance by plate appearance
        self.sim_wp_chart.setToolTip("The home team's chance of winning after each plate appearance, from this run's "
                                     "own scoring (runs from each base and out to the end of the inning)")
        game_split = QSplitter(Qt.Orientation.Vertical)
        game_split.addWidget(self.sim_wp_chart)
        game_split.addWidget(self.sim_game_view)
        game_split.setSizes([220, 480])
        game_lay.addWidget(game_split)
        self.sim_pages.addTab(self.sim_summary, "Result")
        self._sim_overrides: dict[int, tuple] = {}  # side -> ((team id, season), overrides) you chose
        self.sim_edit_btns = []
        for side, view, title in ((0, self.sim_visitor_view, "Team 1"), (1, self.sim_home_view, "Team 2")):
            page = QWidget()
            lay = QVBoxLayout(page)
            bar = QHBoxLayout()
            edit = QPushButton("Change lineup, fielders or rotation\u2026")
            edit.setToolTip("Choose this team's batting order, who plays each position and the rotation order; "
                            "the games are then played again")
            edit.setEnabled(False)
            edit.clicked.connect(lambda _c, sd=side: self._edit_sim_team(sd))
            reset = QPushButton("Back to the tabs' defaults")
            reset.setEnabled(False)
            reset.clicked.connect(lambda _c, sd=side: self._reset_sim_team(sd))
            self.sim_edit_btns.append((edit, reset))
            bar.addWidget(edit)
            bar.addWidget(reset)
            bar.addStretch(1)
            lay.addLayout(bar)
            lay.addWidget(view, 1)
            self.sim_pages.addTab(page, title)
        self.sim_pages.addTab(self._build_sim_cards_page(), "Cards")
        self.sim_pages.addTab(self._build_card_battle_page(), "Card Battle")
        charts = QWidget()  # how the runs and the margins fell, as bars
        charts_lay = QVBoxLayout(charts)
        self.sim_runs_chart, self.sim_margin_chart = infochart.InfoChart(), infochart.InfoChart()
        for chart in (self.sim_runs_chart, self.sim_margin_chart):
            chart.set_result(None, "Press Play.")
            charts_lay.addWidget(chart, 1)
        self.sim_pages.addTab(charts, "Charts")
        self.sim_pages.addTab(game_page, "One game, play by play")
        self.sim_pages.addTab(self.sim_how, "How it works")
        self.sim_summary.setHtml("<p>Choose the two team-seasons above and press <b>Play</b>.  They take turns at home, Team 2 first "
            "(so with an odd number of games Team 2 has the extra home game).</p>")
        self.sim_how.setHtml(self._sim_how_html(None))

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(top)
        outer.addWidget(self.sim_pages, 1)
        return page

    def _build_sim_cards_page(self) -> QWidget:
        """The Simulator's player cards: one per batter, pitcher and non-batting fielder of both sides, with
        what the Simulator plays him with (see :func:`simulator.player_cards`)."""
        self.sim_cards_team = QComboBox()
        self.sim_cards_team.addItems(["Both teams", "Team 1", "Team 2"])
        self.sim_cards_kind = QComboBox()
        self.sim_cards_kind.addItems(["Everyone", "Batters", "Pitchers", "Bench", "Fielders who don't bat", "Parks"])
        self.sim_cards_role = QComboBox()
        self.sim_cards_role.addItems(["Any role"] + list(mlbstats.PITCHER_ROLES))
        self.sim_cards_role.setToolTip("Pitchers' Prevention-tab role")
        for combo in (self.sim_cards_team, self.sim_cards_kind, self.sim_cards_role):
            combo.currentIndexChanged.connect(lambda _i: self._show_sim_cards())
        self.sim_cards_view = simcards.CardsView()
        self._sim_cards: list[dict] = []
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.sim_cards_view)
        page = QWidget()
        lay = QVBoxLayout(page)
        bar = QHBoxLayout()
        for widget in (QLabel("Show"), self.sim_cards_team, self.sim_cards_kind, self.sim_cards_role):
            bar.addWidget(widget)
        bar.addStretch(1)
        how = QPushButton("How a card is played\u2026")
        how.setToolTip("Open the explanation of the cards in its own window")
        how.clicked.connect(self._show_sim_cards_guide)
        self._sim_cards_guide = None
        bar.addWidget(how)
        lay.addLayout(bar)
        lay.addWidget(scroll, 1)
        return page

    def _show_text_window(self, attr: str, title: str, html: str, size=(640, 480)):
        """An explanation in a window of its own (non-modal, kept in ``attr`` between uses), its text set anew."""
        dlg = getattr(self, attr, None)
        if dlg is None:
            dlg = QDialog(self)
            dlg.setWindowTitle(title)
            dlg.text = QTextBrowser()
            close = QPushButton("Close")
            close.clicked.connect(dlg.close)
            lay = QVBoxLayout(dlg)
            lay.addWidget(dlg.text, 1)
            lay.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
            dlg.resize(*size)
            setattr(self, attr, dlg)
        dlg.text.setHtml(html)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    # ---- Card Battle: one matchup worked out from the cards
    _BATTLE_PLAYS = (("steal", "Steal (any base)"), ("extra_base", "Extra base on a single or double"),
                     ("tag_up", "Tag-up on a fly out"), ("double_play", "Double play on a ground ball"))

    def _build_card_battle_page(self) -> QWidget:
        """Choose two (or more) cards from the last run and see the outcome worked out from them, as the games
        do it: a batter against a pitcher in one of the two parks, or a runner against the fielders on one kind of
        play."""
        self.cb_mode = QComboBox()
        self.cb_mode.addItems(["Batter vs. Pitcher", "Fielder vs. Runner"])
        self.cb_side = QComboBox()  # who bats (or runs)
        self.cb_park = QComboBox()
        self.cb_batter, self.cb_pitcher = QComboBox(), QComboBox()
        self.cb_play, self.cb_runner, self.cb_fielder, self.cb_battery = QComboBox(), QComboBox(), QComboBox(), QComboBox()
        for key, label in self._BATTLE_PLAYS:
            self.cb_play.addItem(label, key)
        for combo in (self.cb_batter, self.cb_pitcher, self.cb_runner, self.cb_fielder, self.cb_battery):
            combo.setMinimumWidth(220)
        top = QHBoxLayout()
        for label, widget in (("Battle", self.cb_mode), ("Batting / running", self.cb_side), ("Park", self.cb_park)):
            top.addWidget(QLabel(label))
            top.addWidget(widget)
        top.addStretch(1)
        # the two modes' own choices
        bvp, fvr = QWidget(), QWidget()
        row = QHBoxLayout(bvp)
        row.setContentsMargins(0, 0, 0, 0)
        for label, widget in (("Batter", self.cb_batter), ("Pitcher", self.cb_pitcher)):
            row.addWidget(QLabel(label))
            row.addWidget(widget)
        row.addStretch(1)
        row = QHBoxLayout(fvr)
        row.setContentsMargins(0, 0, 0, 0)
        self.cb_runner_label, self.cb_fielder_label = QLabel("Runner"), QLabel("Fielder")
        for label, widget in ((QLabel("Play"), self.cb_play), (self.cb_runner_label, self.cb_runner),
                              (self.cb_fielder_label, self.cb_fielder), (QLabel("Pitcher"), self.cb_battery)):
            row.addWidget(label)
            row.addWidget(widget)
        self.cb_battery_label = row.itemAt(row.count() - 2).widget()
        row.addStretch(1)
        self.cb_modes = QStackedWidget()
        self.cb_modes.addWidget(bvp)
        self.cb_modes.addWidget(fvr)
        self.cb_modes.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.cb_view = QTextBrowser()
        self.cb_cards = simcards.CardsView()
        cards_scroll = QScrollArea()
        cards_scroll.setWidgetResizable(True)
        cards_scroll.setWidget(self.cb_cards)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.cb_view)
        split.addWidget(cards_scroll)
        split.setSizes([620, 700])
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addLayout(top)
        lay.addWidget(self.cb_modes)
        lay.addWidget(split, 1)
        self.cb_mode.currentIndexChanged.connect(lambda _i: (self.cb_modes.setCurrentIndex(self.cb_mode.currentIndex()),
                                                             self._fill_card_battle(keep=True)))
        self.cb_side.currentIndexChanged.connect(lambda _i: self._fill_card_battle(keep=False))
        self.cb_play.currentIndexChanged.connect(lambda _i: self._fill_card_battle(keep=True))
        for combo in (self.cb_park, self.cb_batter, self.cb_pitcher, self.cb_runner, self.cb_fielder, self.cb_battery):
            combo.currentIndexChanged.connect(lambda _i: self._show_card_battle())
        self.cb_view.setHtml("<p>Press <b>Play</b>, then choose the cards to battle.</p>")
        return page

    @staticmethod
    def _refill(combo: QComboBox, items: list, keep: bool = True):
        """Replace a drop-down's ``(label, data)`` items, keeping the choice where it's still there."""
        was = combo.currentData() if keep else None
        combo.blockSignals(True)
        combo.clear()
        for label, data in items:
            combo.addItem(label, data)
        if was is not None and combo.findData(was) >= 0:
            combo.setCurrentIndex(combo.findData(was))
        combo.blockSignals(False)

    def _fill_card_battle(self, keep: bool = True):
        """The Card Battle drop-downs from the last run: the batting side's batters and runners, the other side's
        pitchers and fielders, and the two parks."""
        data = self._sim_result
        if data is None:
            return
        teams = data["teams"]
        self._refill(self.cb_side, [(f"Team {i + 1}: {t.name}", i) for i, t in enumerate(teams)])
        self._refill(self.cb_park, [(f"{(t.venue or {}).get('venue') or t.short + ' park'} ({t.short})", i)
                                    for i, t in enumerate(teams)])
        side = self.cb_side.currentData() or 0
        bat, field = teams[side], teams[1 - side]
        batters = [b for b in bat.lineup if b is not None] + list(bat.bench)
        names = [(f"{b.name}" + ("" if b in bat.lineup else "  (bench)"), b.pid) for b in batters]
        self._refill(self.cb_batter, names, keep)
        self._refill(self.cb_runner, names, keep)
        pitchers = [(f"{p.name}  ({p.role})", p.pid) for p in field.pitchers]
        self._refill(self.cb_pitcher, pitchers, keep)
        self._refill(self.cb_battery, pitchers, keep)
        play = self.cb_play.currentData()
        if play in ("extra_base", "tag_up"):
            wanted = ("LF", "CF", "RF")
        elif play == "double_play":
            wanted = ("P", "C", "1B", "2B", "3B", "SS")
        else:
            wanted = ()
        usual = {(row["_id"], pos) for pos, row in field.defense.items()}
        fielders = []
        for r in sorted(field.fielding_rows, key=lambda r: (r.get("_pos") not in wanted,
                                                           (r["_id"], r.get("_pos")) not in usual,
                                                           -mlbstats.innings_to_outs(r.get("innings", 0)))):
            if r.get("_pos") in wanted and r.get("_id") is not None:
                fielders.append((f"{r.get('_name', '?')}  ({r['_pos']}, {r.get('innings') or '?'} inn)",
                                 f"{r['_id']}|{r['_pos']}"))
        self._refill(self.cb_fielder, fielders, keep)
        steal = play == "steal"
        self.cb_fielder.setVisible(not steal)
        self.cb_fielder_label.setVisible(not steal)
        self.cb_battery.setVisible(steal)
        self.cb_battery_label.setVisible(steal)
        self.cb_runner_label.setText("Batter" if play == "double_play" else "Runner")
        self._show_card_battle()

    def _show_card_battle(self):
        data = self._sim_result
        if data is None:
            return
        esc = html.escape
        teams, envs = data["teams"], data["envs"]
        side = self.cb_side.currentData() or 0
        bat, field = teams[side], teams[1 - side]
        park = self.cb_park.currentData() if self.cb_park.currentData() is not None else 1
        env = envs[park]
        by_pid = {b.pid: b for b in [b for b in bat.lineup if b is not None] + list(bat.bench)}
        pitchers = {p.pid: p for p in field.pitchers}
        place = esc((teams[park].venue or {}).get("venue") or f"{teams[park].short}'s park")
        out = [self._SIM_CSS]
        cards = []

        def card(team_side: int, pid, kinds):
            return next((c for c in self._sim_cards if c["side"] == team_side and c.get("pid") == pid
                         and c["kind"] in kinds), None)
        if self.cb_mode.currentIndex() == 0:
            batter, pitcher = by_pid.get(self.cb_batter.currentData()), pitchers.get(self.cb_pitcher.currentData())
            if batter is None or pitcher is None:
                self.cb_view.setHtml("<p>Choose a batter and a pitcher.</p>")
                return
            pa = simulator.battle_plate_appearance(batter, bat, pitcher, field, env)
            pct = lambda x: "" if x is None else f"{x:.1%}"
            out.append(f"<h3>{esc(batter.name)} ({esc(bat.short)}) vs {esc(pitcher.name)} ({esc(field.short)}), "
                       f"at {place}</h3>")
            hands = pa["hands"]
            if hands:
                matchup = self._sim_hands_text(batter.bats, hands)
                out.append(f"<p class=g>{esc(matchup)}.  Each result's chance is (batter against "
                           f"{'left' if hands[1] == 'L' else 'right'}-handed pitchers \u00f7 his league) \u00d7 "
                           f"(pitcher against batters from the {'left' if hands[0] == 'L' else 'right'} \u00f7 his "
                           "league) \u00d7 the matchup here (the home league's rate, times the park's factor, times "
                           "how this matchup of hands did against all plate appearances that season), the nine scaled "
                           f"to 100% (log5); then {esc(field.short)}'s usual fielders behind him, each on balls to his "
                           "own ground.</p>")
            else:
                out.append("<p class=g>Each result's chance is (batter \u00f7 his league) \u00d7 (pitcher \u00f7 his "
                           "league) \u00d7 here (the home league's rate times the park's factor), the nine scaled to "
                           f"100% (log5); then {esc(field.short)}'s usual fielders behind him, each on balls to his own "
                           "ground.</p>")
            keys = ("batter", "b_league", "pitcher", "p_league", "league", "park") + (("env",) if hands else ()) + ("log5",)
            out.append("<table><tr><th>Result</th><th>Batter</th><th>His league</th><th>Pitcher</th><th>His league</th>"
                       "<th>League</th><th>Here (park)</th>"
                       + (f"<th>{hands[0]}HB vs {hands[1]}HP here</th>" if hands else "")
                       + "<th>log5</th><th>After fielders</th></tr>")
            for r in pa["rows"]:
                out.append(f"<tr><td>{esc(r['name'])}</td>" + "".join(
                    f"<td class=n>{pct(r[k])}</td>" for k in keys) + f"<td class=n><b>{pct(r['final'])}</b></td></tr>")
            out.append("</table>")
            line = pa["line"]
            out.append(f"<p><b>Outcome:</b> this plate appearance comes out {self._f3(line['AVG'])} / "
                       f"{self._f3(line['OBP'])} / {self._f3(line['SLG'])} (AVG / OBP / SLG); strikeout "
                       f"{line['K']:.1%}, walk {line['BB']:.1%}, home run {line['HR']:.1%}, on an error "
                       f"{line['ROE']:.1%}.</p>")
            cards = [card(side, batter.pid, ("batter", "bench")), card(1 - side, pitcher.pid, ("pitcher",)),
                     next((c for c in self._sim_cards if c["kind"] == "park" and c["side"] == park), None)]
        else:
            play = self.cb_play.currentData()
            runner = by_pid.get(self.cb_runner.currentData())
            if runner is None:
                self.cb_view.setHtml("<p>Choose a runner.</p>")
                return
            fielder_pid = position = None
            pitcher = None
            if play == "steal":
                pitcher = pitchers.get(self.cb_battery.currentData())
            else:
                choice = self.cb_fielder.currentData()
                if not choice:
                    self.cb_view.setHtml("<p>Choose a fielder.</p>")
                    return
                pid_text, position = choice.split("|")
                fielder_pid = int(pid_text) if pid_text.lstrip("-").isdigit() else pid_text
            res = simulator.battle_running(play, runner, bat, field, env, fielder_pid, position, pitcher)
            names = {r["_id"]: r.get("_name", "?") for r in field.fielding_rows}
            against = (f"{esc(res.get('catcher') or 'no catcher')} catching and {esc(pitcher.name if pitcher else '?')} "
                       "pitching" if play == "steal" else f"{esc(names.get(fielder_pid, '?'))} at {position}")
            label = dict(self._BATTLE_PLAYS)[play]
            out.append(f"<h3>{label}: {esc(runner.name)} ({esc(bat.short)}) against {against} ({esc(field.short)}), "
                       f"at {place}</h3>")
            out.append("<p class=g>As in the games: the league's rate (here, the home league's) in odds, times each "
                       "side's own figure against his league \u2014 the runner's, and the fielders' \u2014 and back "
                       "to a rate.  In play the same factors tilt the season's play tables.</p>")
            for what, steps in res["parts"]:
                out.append(f"<p><b>{esc(runner.name)} {esc(what)}</b></p><table><tr><th></th><th>Factor</th>"
                           "<th>Odds</th><th>Rate</th></tr>")
                out.append(f"<tr><td>League here</td><td></td><td class=n>{steps['base_odds']:.3f}</td>"
                           f"<td class=n>{steps['base']:.1%}</td></tr>")
                for who, f, odds in steps["steps"]:
                    out.append(f"<tr><td>\u00d7 {esc(who)}</td><td class=n>\u00d7{f:.2f}</td>"
                               f"<td class=n>{odds:.3f}</td><td class=n>{odds / (1 + odds):.1%}</td></tr>")
                out.append(f"<tr><td><b>Outcome</b></td><td></td><td class=n>{steps['odds']:.3f}</td>"
                           f"<td class=n><b>{steps['rate']:.1%}</b></td></tr></table>")
            if play == "steal" and res.get("per_base"):
                out.append("<p><b>By base</b> (the factors applied to each base's league rates)</p><table><tr>"
                           "<th>Stealing</th><th>Tries</th><th>League</th><th>Safe</th><th>League</th>"
                           "<th>Covering</th></tr>")
                for b in res["per_base"]:
                    out.append(f"<tr><td>{b['base']}</td><td class=n>{b['tries']:.1%}</td>"
                               f"<td class=n>{b['league_tries']:.1%}</td><td class=n>{b['safe']:.1%}</td>"
                               f"<td class=n>{b['league_safe']:.1%}</td><td>{esc(b['covering'])}</td></tr>")
                out.append("</table>")
                if res.get("any_base"):
                    tries, safe = res["any_base"]
                    out.append(f"<p><b>Outcome:</b> at any base, as the games weigh the bases, he tries on {tries:.1%} "
                               f"of his chances and is safe {safe:.1%} of the time.</p>")
            fielders = []
            if play == "steal":
                catcher = field.defense.get("C")
                if catcher is not None:
                    fielders.append(card(1 - side, catcher["_id"], ("batter", "fielder", "bench")))
                for pos in simulator.STEAL_COVER[1]:  # second base and short, who take the throw at second
                    if field.defense.get(pos) is not None:
                        fielders.append(card(1 - side, field.defense[pos]["_id"], ("batter", "fielder", "bench")))
                if pitcher is not None:
                    fielders.append(card(1 - side, pitcher.pid, ("pitcher",)))
            else:
                fielders.append(card(1 - side, fielder_pid, ("batter", "fielder", "bench")))
            cards = [card(side, runner.pid, ("batter", "bench"))] + fielders
            missing = [c for c in cards if c is None]
            if missing:
                out.append("<p class=g>(A chosen fielder who isn't one of the usual nine or the bench has no card.)</p>")
        self.cb_view.setHtml("".join(out))
        self.cb_cards.set_cards([c for c in cards if c is not None], "No cards for these players.")

    def _show_sim_cards_guide(self):
        self._show_text_window("_sim_cards_guide", "Simulator \u2014 how a card is played", SIM_CARDS_GUIDE)

    def _show_sim_cards(self):
        team = self.sim_cards_team.currentIndex() - 1          # -1 both, 0 visitor, 1 home
        kind = {1: "batter", 2: "pitcher", 3: "bench", 4: "fielder", 5: "park"}.get(self.sim_cards_kind.currentIndex())
        role = self.sim_cards_role.currentText() if self.sim_cards_role.currentIndex() > 0 else None
        self.sim_cards_role.setEnabled(kind in (None, "pitcher"))
        def wanted(card) -> bool:
            if team >= 0 and card["side"] != team:
                return False
            if kind is not None and card["kind"] != kind:
                return False
            # a chosen role shows just the pitchers in it
            return role is None or (card["kind"] == "pitcher" and card["role"] == role)
        shown = [c for c in self._sim_cards if wanted(c)]
        self.sim_cards_view.set_cards(shown, "No cards match." if self._sim_cards else "Press Play to deal the cards.")

    def _sim_speed_formulas(self) -> tuple[str | None, str | None]:
        defs = {column: ftext for column, _n, ftext in self._speed_definitions()}
        return defs.get("BG"), defs.get("OPP")

    def _run_simulation(self):
        vis, home = self.sim_vis.team_season(), self.sim_home.team_season()
        if vis is None or home is None:
            self._say("sim", "Choose both teams first.")
            return
        games, seed = self.sim_games.value(), self.sim_seed.value()
        dh_rule = self.sim_dh.currentData()
        fatigue = bool(self.sim_fatigue.currentData())
        best_of = self.sim_format.currentData()
        self._sim_unit = "series" if best_of else "games"
        bg, opp = self._sim_speed_formulas()
        last_complete = self._season_now - 1
        season_now = self._season_now
        self.sim_run_btn.setEnabled(False)
        fetching = [s for s in {vis[2], home[2]} if s <= last_complete and not mlbstats.retro_sim_is_cached(s)]
        self._say("sim", f"Playing {games:,} games…" + (" (getting Retrosheet's play-by-play first, once)"
                                                             if fetching else ""))

        overrides = {}
        for sd, info in ((0, vis), (1, home)):
            saved = self._sim_overrides.get(sd)
            if saved and saved[0] == (info[0], info[2], dh_rule):  # (a lineup made for this team, season and rule)
                overrides[sd] = saved[1]

        def side(info, label, chosen=None):
            return self._sim_make_team(info, dh_rule, fatigue, bg, opp, season_now, chosen)

        self._sim_run_id += 1
        run_id, signal = self._sim_run_id, self._sim_progress_sig
        self._on_sim_progress(run_id, 0, 0)
        cancelled = lambda: run_id != self._sim_run_id  # a newer Play has replaced this run: stop it
        if getattr(self, "_sim_path_dialog", None) is not None and self.sim_path_view.data is None:
            self._sim_path_run += 1  # a path analysis still working on the old run: stop it too
            self.sim_path_view.set_data(None, "Stopped: a new run started.  Press Path analysis again for it.")

        def fn():
            v, h = side(vis, "Team 1", overrides.get(0)), side(home, "Team 2", overrides.get(1))
            unresolved = [sd for sd, t in enumerate((v, h)) if t.lineup_problems()["bats_only"]]
            if unresolved:  # a real lineup card: everyone who bats fields (or is the DH) -- ask first
                return {"resolve": unresolved, "teams": (v, h)}
            # each game in its home team's environment: its league that season and Retrosheet's play tables
            envs, notes, built = [], [], {}
            for t in (v, h):
                if t.season not in built:
                    summary, used, note = simulator.load_tables(t.season, last_complete)
                    built[t.season] = (simulator.Tables(summary), used)
                    if note:
                        notes.append(note)
                tables, used = built[t.season]
                envs.append(simulator.Environment(t, tables, used))
            envs = tuple(envs)
            signal.progress.emit(run_id, 0, games)
            res = simulator.simulate(v, h, envs, games, seed, progress=lambda done: signal.progress.emit(run_id, done, games),
                                     series=best_of, cancelled=cancelled)
            notes += v.notes + h.notes
            real = None
            if best_of and v.season == h.season:  # did they really meet in that postseason?
                try:
                    ttl = LIVE_TTL if v.season >= season_now else None
                    real = next((sr for sr in mlbstats.get_postseason(v.season, ttl)
                                 if {t["id"] for t in sr["teams"]} == {v.team_id, h.team_id}), None)
                except mlbstats.MLBStatsError:
                    real = None
            if not bg:
                notes.append("The Speed tab's running groups need the “Bases Gained” custom stat "
                             "(bases-gained-player.json); runners are still measured, just not labelled.")
            return {"teams": (v, h), "envs": envs, "res": res, "notes": notes, "real_series": real}

        self._run_latest("sim", fn, self._on_simulation, self._on_simulation_failed)

    @staticmethod
    def _sim_make_team(info, dh_rule: str, fatigue: bool, bg, opp, season_now: int, chosen=None) -> "simulator.Team":
        """One side for the Simulator (in a worker thread): its team-season fetched and set up by the tabs' rules."""
        team_id, team_name, season = info
        ttl = LIVE_TTL if season >= season_now else None
        raw = simulator.fetch_team(team_id, season, ttl)
        try:  # the club's name that season (the box may not have caught up with a year just typed)
            team_name = (mlbstats.get_team_map(season, ttl).get(team_id) or {}).get("name") or team_name
        except mlbstats.MLBStatsError:
            pass
        try:
            groups = simulator.speed_groups(raw.get("runners"), raw.get("league_runners"), bg, opp)
        except mlbstats.MLBStatsError:
            groups = {}
        return simulator.Team(raw, f"{team_name} {season}", groups, chosen, dh_rule=dh_rule, fatigue=fatigue)

    # ---- Round robin: several team-seasons, every pair playing
    def _show_sim_round_robin(self):
        """A window to list team-seasons and play a round robin among them (:func:`simulator.round_robin`), with
        the DH and fatigue choices of the tab; the standings and head-to-head records come back in it."""
        dlg = getattr(self, "_sim_rr_dialog", None)
        if dlg is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("Simulator — round robin")
            self.sim_rr_pick = _PlayerPicker(self, "sim_rr", "fielding", "Team-season", with_player=False)
            self.sim_rr_list = QListWidget()
            self.sim_rr_list.setToolTip("The team-seasons in the round robin (two to twelve)")
            add, remove = QPushButton("Add →"), QPushButton("Remove")
            add.clicked.connect(self._sim_rr_add)
            remove.clicked.connect(lambda: [self.sim_rr_list.takeItem(self.sim_rr_list.row(i))
                                            for i in self.sim_rr_list.selectedItems()])
            self.sim_rr_games = QSpinBox()
            self.sim_rr_games.setRange(2, 2000)
            self.sim_rr_games.setSingleStep(2)
            self.sim_rr_games.setValue(20)
            self.sim_rr_games.setToolTip("Games each pair plays, taking turns at home")
            self.sim_rr_play = QPushButton("Play")
            self.sim_rr_play.clicked.connect(self._sim_rr_run)
            self.sim_rr_status = QLabel()
            self.sim_rr_view = QTextBrowser()
            left = QVBoxLayout()
            left.addWidget(self.sim_rr_pick)
            buttons = QHBoxLayout()
            buttons.addWidget(add)
            buttons.addWidget(remove)
            left.addLayout(buttons)
            form = QFormLayout()
            form.addRow("Games per pair", self.sim_rr_games)
            left.addLayout(form)
            left.addWidget(self.sim_rr_play)
            left.addWidget(self.sim_rr_status)
            left.addStretch(1)
            top = QHBoxLayout()
            top.addLayout(left)
            top.addWidget(self.sim_rr_list, 1)
            lay = QVBoxLayout(dlg)
            lay.addLayout(top)
            lay.addWidget(self.sim_rr_view, 1)
            dlg.resize(980, 720)
            self._sim_rr_dialog = dlg
            self.sim_rr_view.setHtml("<p>Add two or more team-seasons and press <b>Play</b>.  Every pair plays the "
                                     "games, half at each park, with the Simulator tab's DH and Fatigue choices "
                                     "and its Seed.</p>")
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _sim_rr_add(self):
        info = self.sim_rr_pick.team_season()
        if info is None:
            return
        team_id, name, season = info
        for i in range(self.sim_rr_list.count()):
            if self.sim_rr_list.item(i).data(Qt.ItemDataRole.UserRole) == info:
                return
        if self.sim_rr_list.count() >= 12:
            self.sim_rr_status.setText("Twelve at most.")
            return
        item = QListWidgetItem(f"{name} {season}")
        item.setData(Qt.ItemDataRole.UserRole, info)
        self.sim_rr_list.addItem(item)

    def _sim_rr_run(self):
        self._sim_rr_token = getattr(self, "_sim_rr_token", 0) + 1  # a newer round robin stops an older one
        token = self._sim_rr_token
        infos = [self.sim_rr_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.sim_rr_list.count())]
        if len(infos) < 2:
            self.sim_rr_status.setText("Add at least two team-seasons.")
            return
        games, seed = self.sim_rr_games.value(), self.sim_seed.value()
        dh_rule, fatigue = self.sim_dh.currentData(), bool(self.sim_fatigue.currentData())
        bg, opp = self._sim_speed_formulas()
        season_now, last_complete = self._season_now, self._season_now - 1
        pairs = len(infos) * (len(infos) - 1) // 2
        self.sim_rr_play.setEnabled(False)
        self.sim_rr_status.setText(f"Setting up {len(infos)} teams, then {pairs} pairs × {games} games…")

        def fn():
            teams = [self._sim_make_team(info, dh_rule, fatigue, bg, opp, season_now) for info in infos]
            built, envs, notes = {}, [], []
            for t in teams:
                if t.season not in built:
                    summary, used, note = simulator.load_tables(t.season, last_complete)
                    built[t.season] = (simulator.Tables(summary), used)
                    if note:
                        notes.append(note)
                tables, used = built[t.season]
                envs.append(simulator.Environment(t, tables, used))
            for t in teams:  # (a lineup with batters who don't field: the tabs' suggested positions)
                if t.lineup_problems()["bats_only"]:
                    notes.append(f"{t.name}: some batters don't field in the tabs' lineup -- played as it is; set "
                                 "it in the Simulator tab to fix it")
            return {"rr": simulator.round_robin(teams, envs, games, seed,
                                                cancelled=lambda: token != self._sim_rr_token), "notes": notes}

        def done(data):
            self.sim_rr_play.setEnabled(True)
            self.sim_rr_status.setText(f"{pairs} pairs × {games} games played (seed {seed}).")
            self.sim_rr_view.setHtml(self._sim_rr_html(data))

        def failed(message):
            self.sim_rr_play.setEnabled(True)
            self.sim_rr_status.setText(f"Couldn't play: {message}")
        self._run_latest("sim_rr", fn, done, failed)

    def _sim_rr_html(self, data: dict) -> str:
        rr = data["rr"]
        esc = html.escape
        table, order = rr["table"], rr["order"]
        out = [self._SIM_CSS, "<h2>Standings</h2><table><tr><th></th><th>Team</th><th>W</th><th>L</th>"
               "<th>Pct</th><th>GB</th><th>RS/G</th><th>RA/G</th><th>Pythagorean</th></tr>"]
        top = table[order[0]]
        for rank, i in enumerate(order, start=1):
            r = table[i]
            pct = (r["W"] + r["T"] / 2) / r["G"] if r["G"] else 0.0
            gb = ((top["W"] - r["W"]) + (r["L"] - top["L"])) / 2
            rs, ra = r["RS"] / r["G"], r["RA"] / r["G"]
            pyth = rs ** 2 / (rs ** 2 + ra ** 2) if rs + ra else 0.0
            out.append(f"<tr><td>{rank}</td><td><b>{esc(r['team'].name)}</b></td><td class=n>{r['W']:,}</td>"
                       f"<td class=n>{r['L']:,}</td><td class=n>{self._f3(pct)}</td>"
                       f"<td class=n>{'–' if gb == 0 else f'{gb:g}'}</td><td class=n>{rs:.2f}</td>"
                       f"<td class=n>{ra:.2f}</td><td class=n>{self._f3(pyth)}</td></tr>")
        out.append("</table><h3>Head to head</h3><p class=g>Each row's wins against each column's, in "
                   f"{rr['games']} games a pair.</p><table><tr><th></th>")
        out.extend(f"<th>{esc(table[j]['team'].short)}</th>" for j in order)
        out.append("</tr>")
        for i in order:
            out.append(f"<tr><td>{esc(table[i]['team'].short)}</td>")
            for j in order:
                w = rr["h2h"][i][j]
                cls = "" if i == j else " class=good" if w * 2 > rr["games"] else " class=bad" if w * 2 < rr["games"] \
                    else ""
                out.append(f"<td{cls} style='text-align:right'>{'' if i == j else w}</td>")
            out.append("</tr>")
        out.append("</table>")
        if data["notes"]:
            out.append("<h3>Notes</h3><ul>" + "".join(f"<li>{esc(t)}</li>" for t in data["notes"]) + "</ul>")
        return "".join(out)

    SIM_PATH_MAX_GAMES = 2000  # each path analysis plays the games simulator.path_replays() times

    def _show_sim_path(self):
        """Open the path-analysis diagram (a non-modal window kept between uses) and work it out for the last
        run: the run again plus once per part per team with that part league-average, on the same seed."""
        data = self._sim_result
        if data is None:
            return
        if self._sim_path_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("Simulator \u2014 path analysis")
            self.sim_path_view = simpath.PathView(dlg)
            self.sim_path_note = QLabel()  # which games, when it's fewer than the run
            how = QPushButton("How to read this\u2026")
            how.setToolTip("Open the explanation of the diagram in its own window")
            how.clicked.connect(lambda: self._show_text_window(
                "_sim_path_guide", "Simulator \u2014 reading the path analysis", SIM_PATH_GUIDE))
            bar = QHBoxLayout()  # at the top, clear of the diagram's scrollbars
            bar.addWidget(self.sim_path_note, 1)
            bar.addWidget(how)
            scroll = QScrollArea()  # (the diagram keeps the size it needs: a smaller window scrolls it)
            scroll.setWidgetResizable(True)
            scroll.setWidget(self.sim_path_view)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)  # (the model is wide)
            scroll.setMinimumSize(320, 200)  # (no more: a short screen must still show the bottom scrollbar)
            dlg.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
            lay = QVBoxLayout(dlg)
            lay.addLayout(bar)
            lay.addWidget(scroll, 1)
            # as big as the whole model, as far as the screen allows; scrollbars both ways for the rest
            screen = dlg.screen().availableGeometry() if dlg.screen() else None
            want_w, want_h = self.sim_path_view.MIN_WIDTH + 40, self.sim_path_view.content_height() + 80
            # (room left for the title bar and the desktop's frame, so the bottom scrollbar is on the screen)
            dlg.resize(min(want_w, screen.width() - 40) if screen else want_w,
                       min(want_h, screen.height() - 90) if screen else want_h)
            if screen:
                dlg.move(screen.left() + 20, screen.top() + 10)
            self._sim_path_sig = _SimProgress()  # replays done, from the worker thread
            self._sim_path_sig.progress.connect(
                lambda run, done, total: run == self._sim_path_run and self.sim_path_view.set_data(
                    None, f"Replaying {self._sim_path_games:,} games: {done} of {total} replays done "
                          f"(each part, each row, without it)\u2026"))
            self._sim_path_run = 0
            self._sim_path_dialog = dlg
        res = data["res"]
        games = min(res["games"], self.SIM_PATH_MAX_GAMES)
        reused = games == res["games"] and not res.get("series")  # the run is the baseline: not played again
        self.sim_path_view.set_data(None, f"Replaying {games:,} games {simulator.path_replays() - 1} more times, "
                                          "once for each part and each row\u2026"
                                          + ("" if reused else "  (and once as they were)"))
        self.sim_path_note.setText((f"First {games:,} of the {res['games']:,} games." if games < res["games"] else "")
                                   + ("  Played as single games, taking turns at home (the parts' effects need the "
                                      "same games every time; a series' length changes with them)."
                                      if res.get("series") else ""))
        self._sim_path_dialog.show()
        self._sim_path_dialog.raise_()
        (v, h), env, seed = data["teams"], data["envs"], res["seed"]
        self._sim_path_run += 1
        self._sim_path_games = games
        run, signal = self._sim_path_run, self._sim_path_sig

        def fn():
            return simulator.path_analysis(v, h, env, games, seed,
                                           progress=lambda done, total: signal.progress.emit(run, done, total),
                                           base=res,  # (the run's own games when they're the same ones)
                                           cancelled=lambda: run != self._sim_path_run)

        def failed(message):
            if run == self._sim_path_run:  # (a stopped one has already said so)
                self.sim_path_view.set_data(None, f"Couldn't work it out: {message}")
        self._run_latest("sim_path", fn, lambda pa: run == self._sim_path_run and self.sim_path_view.set_data(pa),
                         failed)

    def _on_simulation_failed(self, message: str):
        self.sim_run_btn.setEnabled(True)
        self._sim_progress_idle("Not played")
        self._say("sim", f"Couldn't play: {message}")

    def _edit_sim_team(self, side: int, team=None, resolve: bool = False):
        """Choose one side's batting order -- a player and a position for each place, so the fielders are the
        batters at their positions (and the DH, where there is one) -- and its rotation, then play again with
        them.  ``resolve``: opened by Play because the lineup has batters who don't field; the dialog says so,
        starts on a suggested fix (:meth:`simulator.Team.suggest_positions`) and won't play until every place has
        a position."""
        if team is None:
            data = self._sim_result
            if data is None:
                return
            team = data["teams"][side]
        raw = team.raw
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Team {side + 1}: {team.name}")
        names = {r["_id"]: r.get("_name", "?") for r in raw["hitting"] + raw["pitching"] + raw["fielding"]}
        field_positions = [pos for pos, _label in mlbstats.RANGE_POSITIONS if pos != "P"]
        positions_of = {}  # player id -> [(field position, innings)], busiest first (MLB's "DH" rows aren't fielding)
        for r in sorted(raw["fielding"], key=lambda r: -mlbstats.innings_to_outs(r.get("innings", 0))):
            if r.get("_pos") in field_positions:
                positions_of.setdefault(r["_id"], []).append((r["_pos"], r.get("innings") or "?"))

        def lineup_positions(order_ids, defense_ids) -> list:
            """Each place's position from a defense ``{position: id}``: "P" for the pitcher's spot, "DH" for
            the first batter (DH league) who doesn't field, None for any other who doesn't."""
            where = {pid: pos for pos, pid in defense_ids.items() if pos != "P"}
            out, dh = [], team.uses_dh
            for pid in order_ids:
                if pid == "P":
                    out.append("P")
                elif pid in where:
                    out.append(where[pid])
                elif dh:
                    out.append("DH")
                    dh = False
                else:
                    out.append(None)
            return out

        # batting order: a player and a position per place
        order_box = QGroupBox("Batting order and positions")
        order_grid = QGridLayout(order_box)
        order_grid.addWidget(QLabel("<b>Player</b>"), 0, 1)
        order_grid.addWidget(QLabel("<b>Position</b>"), 0, 2)
        player_combos, pos_combos = [], []

        def fill_positions(row: int, keep=None):
            pid = player_combos[row].currentData()
            combo = pos_combos[row]
            combo.blockSignals(True)
            combo.clear()
            if pid == "P":
                combo.addItem("P (whoever pitches)", "P")
                combo.setEnabled(False)
            else:
                combo.setEnabled(True)
                combo.addItem("\u2014 choose \u2014", None)
                for pos, innings in positions_of.get(pid, []):
                    combo.addItem(f"{pos}  ({innings} inn)", pos)
                if team.uses_dh:
                    combo.addItem("DH", "DH")
            combo.setCurrentIndex(max(combo.findData(keep), 0))
            combo.blockSignals(False)

        for i, slot in enumerate(mlbstats.LINEUP_SLOTS):
            player = QComboBox()
            if not team.uses_dh:
                player.addItem("Pitcher's spot (whoever pitches)", "P")
            for r in team.eligible_batters:
                pa = int(mlbstats.as_number(r.get("plateAppearances")) or 0)
                usual = ", ".join(p for p, _inn in positions_of.get(r["_id"], [])[:2]) or "no position"
                player.addItem(f"{r.get('_name', '?')}  ({usual}; {pa} PA)", r["_id"])
            pos = QComboBox()
            player_combos.append(player)
            pos_combos.append(pos)
            order_grid.addWidget(QLabel(f"{slot}{_ordinal_suffix(slot)}"), i + 1, 0)
            order_grid.addWidget(player, i + 1, 1)
            order_grid.addWidget(pos, i + 1, 2)
            player.currentIndexChanged.connect(lambda _i, row=i: (fill_positions(row, pos_combos[row].currentData()),
                                                                  check()))
            pos.currentIndexChanged.connect(lambda _i: check())

        def set_lineup(order_ids, positions):
            for row, (pid, pos) in enumerate(zip(order_ids, positions)):
                player_combos[row].blockSignals(True)
                player_combos[row].setCurrentIndex(max(player_combos[row].findData(pid), 0))
                player_combos[row].blockSignals(False)
                fill_positions(row, pos)

        current_order = [("P" if b is None else b.pid) for b in team.lineup]
        current_positions = lineup_positions(current_order, {pos: row["_id"] for pos, row in team.defense.items()})
        suggestion = team.suggest_positions() if resolve else {}
        set_lineup(current_order, [suggestion.get(pid, pos) for pid, pos in zip(current_order, current_positions)])
        # rotation
        start_box = QGroupBox("Rotation")
        start_form = QFormLayout(start_box)
        rotation_mode = QComboBox()
        rotation_mode.addItem("Prevention tab's, shared like the real starts", False)
        rotation_mode.addItem("Yours, in strict turn", True)
        rotation_mode.setCurrentIndex(1 if team.custom_rotation else 0)
        start_form.addRow(rotation_mode)
        rotation_combos = []
        current_rotation = [p.pid for p in team.rotation]
        for turn in range(6):
            combo = QComboBox()
            combo.addItem("\u2014", None)
            for role in mlbstats.PITCHER_ROLES:
                for p in team.roles[role]:
                    gs = int(mlbstats.as_number(p.row.get("gamesStarted")) or 0)
                    combo.addItem(f"{p.name}  ({role}, {gs} GS)", p.pid)
            if turn < len(current_rotation):
                combo.setCurrentIndex(max(combo.findData(current_rotation[turn]), 0))
            rotation_combos.append(combo)
            start_form.addRow(f"{turn + 1}{_ordinal_suffix(turn + 1)}", combo)
        start_note = QLabel("Your rotation: game 1 goes to your 1st, game 2 to your 2nd\u2026 (one pitcher = every "
                            "game).  A reliever starting goes as long as he does in relief; a starter left out may "
                            "relieve when the bullpen is used up.")
        start_note.setWordWrap(True)

        def sync_rotation():
            for combo in rotation_combos:
                combo.setEnabled(rotation_mode.currentData())
        rotation_mode.currentIndexChanged.connect(lambda _i: (sync_rotation(), check()))
        sync_rotation()
        intro = QLabel()
        intro.setWordWrap(True)
        if resolve:
            problems = team.lineup_problems()
            fixed = [names.get(pid, "?") + f" to {pos}" for pid, pos in suggestion.items()]
            intro.setText(f"<b>Before playing:</b> {html.escape(', '.join(problems['bats_only']))} "
                          f"{'bats' if len(problems['bats_only']) == 1 else 'bat'} but "
                          f"{'doesn&#39;t' if len(problems['bats_only']) == 1 else 'don&#39;t'} field in the tabs' "
                          "lineup.  Give every batter a position (or DH), or put another player in his place and "
                          "give him one."
                          + (f"  Suggested, from the positions they played: {html.escape(', '.join(fixed))}."
                             if fixed else ""))
        intro.setVisible(resolve)
        warning = QLabel()
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #b42318")

        def chosen():
            order = [c.currentData() for c in player_combos]
            positions = [c.currentData() for c in pos_combos]
            defense = {pos: pid for pid, pos in zip(order, positions) if pos not in (None, "P", "DH")}
            rotation = [c.currentData() for c in rotation_combos if c.currentData() is not None] \
                if rotation_mode.currentData() else []
            return order, positions, defense, rotation

        def check() -> bool:
            order, positions, defense, rotation = chosen()
            problems = []
            batters = [pid for pid in order if pid != "P"]
            if len(set(batters)) < len(batters):
                problems.append("A batter is in the order twice.")
            if not team.uses_dh and order.count("P") != 1:
                problems.append(f"No DH in the {team.league_name} in {team.season}: the pitcher's spot must be in "
                                "the order once.")
            unplaced = [names.get(pid, "?") for pid, pos in zip(order, positions) if pid != "P" and pos is None]
            if unplaced:
                problems.append("Give a position (or DH) to " + ", ".join(unplaced) + " \u2014 or put another player "
                                "in his place.")
            taken = [pos for pos in positions if pos not in (None, "P")]
            twice = sorted({pos for pos in taken if taken.count(pos) > 1})
            if twice:
                problems.append("More than one player at " + ", ".join(twice) + ".")
            missing = [pos for pos in field_positions if pos not in taken]
            if missing and not unplaced and not twice:
                problems.append("Nobody plays " + ", ".join(missing) + ".")
            if rotation_mode.currentData() and not rotation:
                problems.append("Your rotation needs at least one pitcher.")
            if len(set(rotation)) < len(rotation):
                problems.append("A pitcher is in the rotation twice.")
            warning.setText(" ".join(problems))
            ok_btn.setEnabled(not problems)
            return not problems

        buttons = QHBoxLayout()
        defaults = QPushButton("Defaults")
        defaults.setToolTip("The Production, Range and Prevention tabs' choices, with the suggested positions for "
                            "anyone who bats but doesn't field")
        ok_btn = QPushButton("Play with these")
        ok_btn.setDefault(True)
        cancel = QPushButton("Cancel")

        def set_defaults():
            order_ids = list(team.default_order)
            positions = lineup_positions(order_ids, team.default_defense)
            fix = team.suggest_positions() if not team.overrides else {}
            set_lineup(order_ids, [fix.get(pid, pos) for pid, pos in zip(order_ids, positions)])
            rotation_mode.setCurrentIndex(0)
            for turn, combo in enumerate(rotation_combos):
                pid = team.default_rotation[turn] if turn < len(team.default_rotation) else None
                combo.setCurrentIndex(max(combo.findData(pid), 0))
            check()
        defaults.clicked.connect(set_defaults)
        ok_btn.clicked.connect(dlg.accept)
        cancel.clicked.connect(dlg.reject)
        buttons.addWidget(defaults)
        buttons.addStretch(1)
        buttons.addWidget(ok_btn)
        buttons.addWidget(cancel)
        for combo in rotation_combos:
            combo.currentIndexChanged.connect(lambda _i: check())
        boxes = QHBoxLayout()
        boxes.addWidget(order_box, 3)
        boxes.addWidget(start_box, 2)
        lay = QVBoxLayout(dlg)
        lay.addWidget(intro)
        lay.addLayout(boxes)
        lay.addWidget(start_note)
        lay.addWidget(warning)
        lay.addLayout(buttons)
        check()
        if dlg.exec() != QDialog.DialogCode.Accepted or not check():
            if resolve:
                self._say("sim", f"Not played: {team.name}'s lineup still has batters who don't field.")
                self._sim_progress_idle("Not played")
            return
        order, _positions, defense, rotation = chosen()
        self._sim_overrides[side] = ((team.team_id, team.season, team.dh_rule),
                                     {"order": order, "defense": defense, "rotation": rotation})
        self._run_simulation()

    def _reset_sim_team(self, side: int):
        if self._sim_overrides.pop(side, None) is not None:
            self._run_simulation()

    def _on_sim_progress(self, run_id: int, done: int, total: int):
        """The counter beside Play: loading, then games played of the run (a run since replaced is ignored)."""
        if run_id != self._sim_run_id:
            return
        bar = self.sim_progress
        if total == 0:
            bar.setRange(0, 0)  # busy: the teams and Retrosheet's tables are loading
            bar.setFormat("Loading teams\u2026")
        else:
            bar.setRange(0, total)
            bar.setValue(done)
            bar.setFormat(f"{done:,} / {total:,} {getattr(self, '_sim_unit', 'games')}")

    def _sim_progress_idle(self, text: str = ""):
        self.sim_progress.setRange(0, 1)
        self.sim_progress.setValue(0)
        self.sim_progress.setFormat(text)

    def _on_simulation(self, data: dict):
        self.sim_run_btn.setEnabled(True)
        if not data.get("resolve"):
            sr = data["res"].get("series")
            games = sr["count"] if sr else data["res"]["games"]
            self.sim_progress.setRange(0, games)
            self.sim_progress.setValue(games)
            self.sim_progress.setFormat(f"{games:,} / {games:,} {'series' if sr else 'games'}")
        else:
            self._sim_progress_idle("Waiting for a lineup")
        if data.get("resolve"):
            side = data["resolve"][0]
            team = data["teams"][side]
            self._say("sim", f"{team.name}: some batters don't field \u2014 choose their positions to play.")
            self._edit_sim_team(side, team, resolve=True)
            return
        for side, (edit, reset) in enumerate(self.sim_edit_btns):
            edit.setEnabled(True)
            team = data["teams"][side]
            saved = self._sim_overrides.get(side)
            reset.setEnabled(bool(saved) and saved[0] == (team.team_id, team.season, team.dh_rule))
        self.sim_path_btn.setEnabled(True)
        self.sim_export_btn.setEnabled(True)
        self._sim_result = data
        res = data["res"]
        self.sim_summary.setHtml(self._sim_summary_html(data))
        self.sim_visitor_view.setHtml(self._sim_team_html(data, 0))
        self.sim_home_view.setHtml(self._sim_team_html(data, 1))
        self.sim_how.setHtml(self._sim_how_html(data))
        # each player against average opponents in his own team's environment (its league that season)
        self._sim_cards = (simulator.player_cards(data["teams"][0], data["envs"][0], res, 0)
                           + simulator.player_cards(data["teams"][1], data["envs"][1], res, 1))
        self._show_sim_cards()
        self._fill_card_battle()
        self._show_sim_charts(data)
        self.sim_game_spin.blockSignals(True)
        self.sim_game_spin.setRange(1, res["games"])
        self.sim_game_spin.setValue(1)
        self.sim_game_spin.blockSignals(False)
        self._show_sim_game()
        v, h = data["teams"]
        sr = res.get("series")
        if sr:
            self._say("sim", f"{v.name} won {sr['wins'][0]:,} series, {h.name} {sr['wins'][1]:,}, best of "
                             f"{sr['best_of']} (seed {res['seed']}).")
            return
        self._say("sim", f"{v.name} {res['wins'][0]:,}, {h.name} {res['wins'][1]:,} in {res['games']:,} games "
                         f"(seed {res['seed']}).")

    # -- pages
    _SIM_CSS = ("<style>table{border-collapse:collapse} td,th{padding:1px 6px} th{background:#eee;text-align:left}"
                " td.n{text-align:right} .g{color:#777} .good{color:#1a7f37} .bad{color:#b42318} h3{margin-bottom:2px}"
                "</style>")

    @staticmethod
    def _f3(x) -> str:
        return "" if x is None else f"{x:.3f}".lstrip("0") if 0 <= x < 1 else f"{x:.3f}"

    def _on_sim_link(self, url):
        """The Result page's link to the Denied tab: its seasons set to the two teams'."""
        text = url.toString()
        if not text.startswith("denied:") or self.tabs is None:
            return
        first, last = (int(x) for x in text[len("denied:"):].split("-"))
        self.dn_from_spin.setValue(first)
        self.dn_to_spin.setValue(last)
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == "Denied":
                self.tabs.setCurrentIndex(i)
                break

    def _sim_series_html(self, data: dict) -> list[str]:
        """A series run's headline, series lengths, the higher seed's chance of being "denied" by the underdog (the
        Denied tab's terms), and the real series if the two met that postseason."""
        res, teams = data["res"], data["teams"]
        sr = res["series"]
        esc = html.escape
        count, best_of, seed = sr["count"], sr["best_of"], sr["seed_team"]
        names = tuple(t.name for t in teams)
        out = []
        lead = 0 if sr["wins"][0] >= sr["wins"][1] else 1
        out.append(f"<h2>{esc(names[lead])} won {sr['wins'][lead] / count:.1%} of {count:,} best-of-{best_of} "
                   "series</h2>")
        need = best_of // 2 + 1
        out.append("<table><tr><th></th>" + "".join(f"<th>In {k}</th>" for k in range(need, best_of + 1))
                   + "<th>Series won</th></tr>")
        for t in (0, 1):
            out.append(f"<tr><td>Team {t + 1}: <b>{esc(names[t])}</b>" + (" (home field)" if t == seed else "")
                       + "</td>" + "".join(f"<td class=n>{sr['lengths'].get((t, k), 0) / count:.1%}</td>"
                                         for k in range(need, best_of + 1))
                       + f"<td class=n><b>{sr['wins'][t] / count:.1%}</b></td></tr>")
        out.append("</table>")
        single = res["wins"][seed] / max(res["wins"][0] + res["wins"][1], 1)
        out.append(f"<p class=g>Home field to {esc(names[seed])}, the better real record (as the higher seed), "
                   f"{simulator.SERIES_FORMATS[best_of]}: games " + ", ".join(
                       str(k + 1) for k in range(best_of) if simulator.series_host(best_of, k))
                   + " at its park.  Each club's four busiest starters (or your rotation) go in turn from game 1; "
                   "nobody takes a day off, and tired relievers rest only on travel days.  "
                   f"{res['games']:,} games in all; the tables below are over those games.</p>")
        denied = sr["wins"][1 - seed] / count
        seasons = sorted({t.season for t in teams})
        out.append(f"<p><b>Denied?</b>  In the Denied tab's terms {esc(names[seed])}, with the better record, "
                   f"ought to win; the underdog put it out in {denied:.1%} of the series (it won "
                   f"{single:.1%} of the games).  <a href='denied:{seasons[0]}-{seasons[-1]}'>Open the Denied tab"
                   f"{' for ' + str(seasons[0]) if len(seasons) == 1 else ''}</a>.</p>")
        real = data.get("real_series")
        if real:
            wins = real.get("wins") or {}
            score = "–".join(str(wins.get(t.team_id, 0)) for t in teams)
            winner = next((t.name for t in teams if t.team_id == real.get("winner")), None)
            out.append(f"<p><b>Really:</b> they met in the {esc(real.get('title') or 'postseason')} "
                       f"(best of {real.get('best_of')}): " + (f"{esc(winner)} won, " if winner else "")
                       + f"{esc(score)} (Team 1–Team 2).</p>")
        return out

    def _show_sim_charts(self, data: dict):
        """The Charts page: each team's runs in a game, and the final margins, as shares of the games."""
        res, teams = data["res"], data["teams"]
        n = res["games"] or 1

        def bars(title, x_ticks, series):
            return {"title": title, "x": "value", "x_label": "", "x_ticks": x_ticks, "x_kind": "number",
                    "chart": "bars", "series": series, "notes": [],
                    "panels": [{"n": 1, "title": "% of games", "min": 0, "max": None, "format": "1", "refs": []}]}
        runs = []
        for side, t in enumerate(teams):
            counts = [0] * 11
            for r in res["runs"][side]:
                counts[min(r, 10)] += 1
            runs.append({"key": t.name, "name": t.name, "label": t.short, "panel": 1, "format": "1",
                         "points": [{"x": float(k), "xlabel": f"{k}{'+' if k == 10 else ''} runs",
                                     "y": 100 * c / n, "detail": f"{c:,} games"} for k, c in enumerate(counts)]})
        self.sim_runs_chart.set_result(bars("Runs in a game (% of games)",
                                            [(float(k), f"{k}{'+' if k == 10 else ''}") for k in range(11)], runs))
        margins = []  # Team 2's runs less Team 1's, 8+ either way pooled
        for side, t in enumerate(teams):
            points = []
            sign = 1 if side else -1  # (the margin is Team 2's runs less Team 1's)
            for m in range(1, 9):
                c = sum(v for d, v in res["margin"].items() if (sign * d >= m if m == 8 else sign * d == m))
                x = float(sign * m)
                points.append({"x": x, "xlabel": f"{t.short} by {m}{'+' if m == 8 else ''}", "y": 100 * c / n,
                               "detail": f"{c:,} games"})
            margins.append({"key": t.name, "name": t.name, "label": t.short, "panel": 1, "format": "1",
                            "points": points})
        ticks = [(float(-m), f"{teams[0].short.split()[0]} +{m}{'+' if m == 8 else ''}") for m in range(8, 0, -1)] + \
                [(float(m), f"{teams[1].short.split()[0]} +{m}{'+' if m == 8 else ''}") for m in range(1, 9)]
        self.sim_margin_chart.set_result(bars("Final margin (% of games): who won, by how many", ticks, margins))

    def _export_sim(self):
        """The last run's tables (:func:`simulator.export_tables`) to an .xlsx (a sheet each) or CSV files (one
        each, the table's name after the file's)."""
        data = self._sim_result
        if data is None:
            return
        v, h = data["teams"]
        slug = re.sub(r"[^A-Za-z0-9]+", "-", f"sim {v.short} vs {h.short}").strip("-").lower()
        path, chosen = QFileDialog.getSaveFileName(
            self, "Export the run", os.path.join(HOME, f"{slug}.xlsx"),
            "Excel workbook (*.xlsx);;CSV files, one per table (*.csv)")
        if not path:
            return
        tables = simulator.export_tables(data["teams"], data["res"])
        try:
            if chosen.startswith("CSV") or path.lower().endswith(".csv"):
                base = path[:-4] if path.lower().endswith(".csv") else path
                saved = []
                for name, header, rows in tables:
                    out = f"{base}-{re.sub(r'[^A-Za-z0-9]+', '-', name).strip('-').lower()}.csv"
                    with open(out, "w", newline="", encoding="utf-8") as fh:
                        writer = csv.writer(fh)
                        writer.writerow(header)
                        writer.writerows(rows)
                    saved.append(out)
                where = f"{len(saved)} CSV files beside {saved[0]}"
            else:
                if not path.lower().endswith(".xlsx"):
                    path += ".xlsx"
                wb = Workbook()
                for name, header, rows in tables:
                    ws = wb.add_sheet(name)
                    ws.append(header, header=True)
                    for row in rows:
                        ws.append(row)
                wb.save(path)
                where = path
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self._say("sim", f"Saved {where}")

    def _sim_box_html(self, index: int, teams: tuple, host: int, score: tuple, box: dict) -> str:
        """A replayed game's box score (:func:`simulator.box_score`): the line score, then each side as the Boxscore
        tab draws it, visitors first."""
        esc = html.escape
        line = box["line"]
        order = (1 - host, host)
        out = [f"<h3 style='margin:0'>Game {index + 1}: {esc(teams[order[0]].name)} {score[order[0]]} at "
               f"{esc(teams[order[1]].name)} {score[order[1]]}</h3>",
               "<table cellspacing='0' cellpadding='2' style='margin:6px 0'><tr style='background:#e6e6e6'><th></th>"
               + "".join(f"<th align='right'>&nbsp;{i}&nbsp;</th>" for i in range(1, line["innings"] + 1))
               + "<th align='right'>&nbsp;R&nbsp;</th><th align='right'>&nbsp;H&nbsp;</th>"
                 "<th align='right'>&nbsp;E&nbsp;</th></tr>"]
        for t in order:
            cells = "".join(f"<td align='right'>{'x' if r is None else r}</td>" for r in line["runs"][t])
            out.append(f"<tr><td><b>{esc(teams[t].short)}</b></td>{cells}<td align='right'><b>{score[t]}</b></td>"
                       f"<td align='right'>{line['hits'][t]}</td><td align='right'>{line['errors'][t]}</td></tr>")
        out.append("</table>")
        bat_cols = [(lbl, key) for lbl, key in mlbstats.BOX_BATTING if key != "leftOnBase"]
        pit_cols = [(lbl, key) for lbl, key in mlbstats.BOX_PITCHING if key not in ("earnedRuns", "numberOfPitches")]
        out.append("<table width='100%' cellspacing='0' cellpadding='0'><tr>"
                   + "".join(f"<td width='50%' valign='top' style='padding:0 10px'>"
                             f"{self._box_team_html(box['sides'][t], teams[t].name, bat_cols, pit_cols, False, False)}"
                             "</td>" for t in order)
                   + "</tr></table>")
        out.append("<p style='margin:4px 10px; color:#777'><small>Errors are the times the other side reached on "
                   "one (the Simulator doesn't charge errors on throws or advances); runs aren't split into earned "
                   "and unearned.</small></p>")
        return "".join(out)

    def _sim_uncertainty_html(self, data: dict) -> str:
        """How sure the run is: a 95% interval on Team 1's share of the games (and of the series), its run margin's,
        and how many games it takes to tell the two apart at the share it found."""
        res, (v, h) = data["res"], data["teams"]
        n = res["games"]
        share = (res["wins"][0] + res["ties"] / 2) / n if n else 0.5
        low, high = simulator.win_interval(res["wins"][0] + res["ties"] / 2, n)
        m, m_lo, m_hi = simulator.run_margin_interval(res)
        need = simulator.games_to_separate(share)
        parts = [f"{html.escape(v.name)}'s share of the games {self._f3(share)}, 95% interval {self._f3(low)}"
                 f"\u2013{self._f3(high)}; run margin {m:+.2f} a game ({m_lo:+.2f} to {m_hi:+.2f})."]
        sr = res.get("series")
        if sr:
            lo_s, hi_s = simulator.win_interval(sr["wins"][0], sr["count"])
            parts.append(f"Its share of the series {sr['wins'][0] / sr['count']:.1%} ({lo_s:.1%}\u2013{hi_s:.1%}).")
        if need is None:
            parts.append("Dead even: no number of games tells them apart.")
        elif low > 0.5 or high < 0.5:
            parts.append(f"That's clear of an even match; at this share it takes about {need:,} games to be sure "
                         "(95%).")
        else:
            parts.append(f"Not yet clear of an even match: at this share it takes about {need:,} games to tell "
                         "them apart (95%)" + (" \u2014 play more." if need > n else "."))
        return "<p class=g>" + "  ".join(parts) + "</p>"

    def _sim_summary_html(self, data: dict) -> str:
        res, (v, h), envs = data["res"], data["teams"], data["envs"]
        n = res["games"]
        esc = html.escape
        out = [self._SIM_CSS]
        if res.get("series"):
            out += self._sim_series_html(data)
        lead = 0 if res["wins"][0] > res["wins"][1] else 1 if res["wins"][1] > res["wins"][0] else None
        names = (v.name, h.name)
        tag = "h3" if res.get("series") else "h2"  # (a series run's headline is the series)
        if lead is None:
            out.append(f"<{tag}>{esc(v.name)} and {esc(h.name)} split {n:,} games</{tag}>")
        else:
            out.append(f"<{tag}>{esc(names[lead])} won {res['wins'][lead] / n:.1%} of {n:,} games</{tag}>")
        out.append(self._sim_uncertainty_html(data))
        out.append("<table><tr><th></th><th>W</th><th>L</th><th>Win %</th><th>Home</th><th>Road</th><th>Runs/G</th>"
                   "<th>Allowed/G</th><th>Shutouts</th><th>Pythagorean %</th></tr>")
        for side, team in enumerate((v, h)):
            rs, ra = sum(res["runs"][side]) / n, sum(res["runs"][1 - side]) / n
            pyth = rs ** 2 / (rs ** 2 + ra ** 2) if rs + ra else None
            losses = res["home"][side][1] + res["road"][side][1]
            out.append(f"<tr><td>Team {side + 1}: <b>{esc(team.name)}</b></td>"
                       f"<td class=n>{res['wins'][side]:,}</td><td class=n>{losses:,}</td>"
                       f"<td class=n>{self._f3(res['wins'][side] / n)}</td>"
                       f"<td class=n>{res['home'][side][0]:,}\u2013{res['home'][side][1]:,}</td>"
                       f"<td class=n>{res['road'][side][0]:,}\u2013{res['road'][side][1]:,}</td>"
                       f"<td class=n>{rs:.2f}</td>"
                       f"<td class=n>{ra:.2f}</td><td class=n>{res['shutouts'][side]:,}</td>"
                       f"<td class=n>{self._f3(pyth)}</td></tr>")
        out.append("</table>")
        hosted = sum(res["hosts"])
        rule = {"own": "each team as its league played that season", "dh": "the DH for both teams (your choice)",
                "pitchers": "pitchers bat for both teams (your choice)"}.get(v.dh_rule, "")
        how = "; ".join(f"{esc(t.short)} {'with the DH' if t.uses_dh else 'pitchers batting'}" for t in (v, h))
        turns = "" if res.get("series") else "The teams take turns at home, Team 2 first: "
        out.append(f"<p class=g>{turns}{esc(h.name)} hosted {hosted:,} games, "
                   f"{esc(v.name)} {n - hosted:,}.  Designated hitter: {rule} ({how}).</p>")
        out.append(f"<p>{res['extra']:,} games went to extra innings ({res['extra'] / n:.1%}); games averaged "
                   f"{res['innings']:.2f} innings" + (f"; {res['ties']:,} were still tied after {simulator.MAX_INNINGS}"
                                                       if res["ties"] else "") + ".  "
                   + ("Extra innings start with a runner on second (the rule from 2020) in "
                      + " and ".join(f"{t.name}'s home games" for t, e in zip((v, h), envs) if e.placed_runner) + "."
                      if any(e.placed_runner for e in envs) else "")
                   + "</p>")
        # how the margins fell
        buckets = [("Team 1 by 5+", lambda m: m <= -5)] + \
                  [(f"Team 1 by {-k}", (lambda k: lambda m: m == k)(k)) for k in (-4, -3, -2, -1)] + \
                  [(f"Team 2 by {k}", (lambda k: lambda m: m == k)(k)) for k in (1, 2, 3, 4)] + \
                  [("Team 2 by 5+", lambda m: m >= 5)]
        out.append("<h3>Final margins</h3><table><tr>")
        out.extend(f"<th>{esc(lab)}</th>" for lab, _f in buckets)
        out.append("</tr><tr>")
        for _lab, f in buckets:
            count = sum(c for m, c in res["margin"].items() if f(m))
            out.append(f"<td class=n>{count / n:.1%}</td>")
        out.append("</tr></table>")
        # runs per game distribution
        out.append("<h3>Runs in a game</h3><table><tr><th></th>")
        out.extend(f"<th>{k}</th>" for k in list(range(10)) + ["10+"])
        out.append("</tr>")
        for side, team in enumerate((v, h)):
            out.append(f"<tr><td>{esc(team.name)}</td>")
            runs = res["runs"][side]
            for k in range(11):
                count = sum(1 for r in runs if (r >= 10 if k == 10 else r == k))
                out.append(f"<td class=n>{count / n:.1%}</td>")
            out.append("</tr>")
        out.append("</table>")
        # starters
        out.append("<h3>Team record in each starter's games</h3><table><tr><th>Team</th><th>Starter</th>"
                   "<th>Starts</th><th>W</th><th>L</th><th>Win %</th></tr>")
        for side, team in enumerate((v, h)):
            for p in team.rotation:
                w, l = res["starter_record"][side].get(p.pid, [0, 0])
                starts = res["stats"]["pitching"][side].get(p.pid, {}).get("GS", 0)
                if not starts:
                    continue
                out.append(f"<tr><td>{esc(team.name)}</td><td>{esc(p.name)}</td><td class=n>{starts:,}</td>"
                           f"<td class=n>{w:,}</td><td class=n>{l:,}</td>"
                           f"<td class=n>{self._f3(w / (w + l)) if w + l else ''}</td></tr>")
        out.append("</table>")
        where = "; ".join(f"at {esc(t.name)}'s, the {esc(t.league_name)} of {e.season} (runners moving as in "
                          f"{e.tables_season or 'textbook rules'})" for t, e in zip((v, h), envs))
        out.append(f"<p class=g>Each game is played in its home team's environment, its league's rates that season "
                   f"and Retrosheet's play tables: {where}.  Seed {res['seed']}.</p>")
        if data["notes"]:
            out.append("<h3>Notes</h3><ul>" + "".join(f"<li>{esc(t)}</li>" for t in data["notes"]) + "</ul>")
        out.append("<p class=g>The information used here was obtained free of charge from and is copyrighted by "
                   "Retrosheet.  Interested parties may contact Retrosheet at 20 Sunset Rd., Newark, DE 19711.</p>")
        return "".join(out)

    def _sim_team_html(self, data: dict, side: int) -> str:
        res = data["res"]
        team = data["teams"][side]
        n = res["games"]
        esc = html.escape
        bat = res["stats"]["batting"][side]
        pit = res["stats"]["pitching"][side]
        out = [self._SIM_CSS, f"<h2>{esc(team.name)}</h2>"]
        out.append(f"<h3>Batting order</h3><p class=g>From the Production tab's rule: {esc(team.order_source)}.  "
                   "Fielding position from the Range tab (\u201cbats only\u201d: the Range tab's usual fielder at his "
                   "position is someone else, or he was the DH), running group from the Speed tab.  Simulated figures "
                   "first, the real season's after the slash; (L), (R), (S) is the side he bats from (S: both).  Running factors are odds against the league "
                   "(1.00 = average): steal tries, steals safe, extra bases on hits, tag-ups, and grounding into "
                   "double plays.  Started: the share of games he started (he rests as often as he really didn't "
                   "start, with fatigue on).  IBB and SH: intentional walks and sacrifice bunts per 162 games, then the real "
                   "season's totals.</p>")
        out.append("<table><tr><th>#</th><th>Batter</th><th>Pos</th><th>Running</th><th>Started</th><th>PA/G</th>"
                   "<th>AVG</th><th>OBP</th><th>SLG</th><th>HR/G</th><th>R/G</th><th>RBI/G</th><th>SB/G</th>"
                   "<th>IBB</th><th>SH</th>"
                   "<th>RP/PA</th><th>Tries</th><th>Safe</th><th>XB</th><th>Tag</th><th>GDP</th></tr>")
        for (slot, row, pos), batter in zip(team.lineup_rows, team.lineup):
            if batter is None:
                pitchers_line = simulator._blank_batting()
                for p in team.pitchers:
                    st = bat.get(p.pid)
                    if st:
                        for k in pitchers_line:
                            pitchers_line[k] += st[k]
                st, real, name = pitchers_line, None, "Pitcher's spot (whoever is pitching)"
                running = None
            else:
                st = bat.get(batter.pid, simulator._blank_batting())
                real, name, running = simulator.real_batting(row), batter.name, batter.running
            sim = simulator.summarize_batting(st)
            pair = lambda k: f"{self._f3(sim[k])}" + (f" <span class=g>/ {self._f3(real[k])}</span>" if real else "")
            fac = (lambda k: f"{running[k]:.2f}") if running and running.get("counts") else (lambda k: "")
            hand = f" <span class=g>({batter.bats})</span>" if batter is not None and batter.bats else ""
            out.append(f"<tr><td>{slot}</td><td>{esc(name)}{hand}</td><td>{esc(pos)}</td>"
                       f"<td>{esc(batter.group if batter else '')}</td>"
                       + (f"<td class=n>{st['GS'] / n:.0%} <span class=g>/ {1 - team.rest.get(batter.pid, 0):.0%}</span>"
                          "</td>" if batter is not None else "<td></td>")
                       + f"<td class=n>{st['PA'] / n:.2f}</td>"
                       f"<td class=n>{pair('AVG')}</td><td class=n>{pair('OBP')}</td><td class=n>{pair('SLG')}</td>"
                       f"<td class=n>{st['HR'] / n:.3f}</td><td class=n>{st['R'] / n:.3f}</td>"
                       f"<td class=n>{st['RBI'] / n:.3f}</td><td class=n>{st['SB'] / n:.3f}</td>"
                       + "".join(f"<td class=n>{st[k] / n * 162:.0f}" + (f" <span class=g>/ "
                                 f"{int(mlbstats.as_number(row.get(f)) or 0)}</span>" if real else "") + "</td>"
                                 for k, f in (("IBB", "intentionalWalks"), ("SH", "sacBunts")))
                       + f"<td class=n>{pair('RP/PA')}</td><td class=n>{fac('att')}</td><td class=n>{fac('succ')}</td>"
                       f"<td class=n>{fac('xb')}</td><td class=n>{fac('tu')}</td>"
                       f"<td class=n>{'' if batter is None else f'{batter.gdp:.2f}'}</td></tr>")
        out.append("</table>")
        # the bench that got into the games
        bench = [(b, bat.get(b.pid)) for b in team.bench if bat.get(b.pid)]
        if bench:
            out.append(f"<h3>Bench</h3><p class=g>Started: games he started for a resting regular"
                       + ("" if team.fatigue else " (none: fatigue is off)") + ".  Pinch runners go in for a Slow "
                       f"regular on base from the "
                       f"{simulator.PINCH_RUN_FROM_INNING}th inning, game within {simulator.PINCH_RUN_MARGIN} runs, "
                       "in the Speed tab's order (Speedy bench, Speedy regulars, Smart regulars; fastest first).  "
                       "When his side takes the field, whoever has the best fielding record at the position "
                       "(the pinch runner or a bench player) plays it.</p>")
            out.append("<table><tr><th>Player</th><th>Speed tab</th><th>Started</th><th>Pinch ran</th>"
                       "<th>On defense</th><th>PA</th><th>R</th><th>SB</th><th>CS</th></tr>")
            for b, st in sorted(bench, key=lambda t: -(t[1]["GS"] + t[1]["PR"] + t[1]["DEF"])):
                out.append(f"<tr><td>{esc(b.name)}</td><td>{esc(b.group)}</td><td class=n>{st['GS']:,}</td>"
                           f"<td class=n>{st['PR']:,}</td>"
                           f"<td class=n>{st['DEF']:,}</td><td class=n>{st['PA']:,}</td><td class=n>{st['R']:,}</td>"
                           f"<td class=n>{st['SB']:,}</td><td class=n>{st['CS']:,}</td></tr>")
            out.append("</table>")
        # defense
        own_fielders = any(team.default_defense.get(pos) != row["_id"] for pos, row in team.defense.items())
        out.append("<h3>Defense</h3><p class=g>" + ("<b>Your fielders.</b>  " if own_fielders else
                   "The Range tab's usual nine (most innings at each position).  ") +
                   "Range is putouts + assists per nine innings against all of MLB at the position.</p>")
        out.append("<table><tr><th>Pos</th><th>Fielder</th><th>Range vs MLB</th><th>Club's season there</th>"
                   "<th>Hits on his ground</th><th>Errors</th><th>Chances</th><th>Errors on his ground</th></tr>")
        # (the pitcher's row is the Range tab's busiest one; in the games whoever is pitching fields there)
        for pos, _label in mlbstats.RANGE_POSITIONS:
            row = team.defense.get(pos)
            if row is None:
                continue
            bats = any(b is not None and b.pid == row["_id"] for b in team.lineup) or pos == "P"
            out.append(f"<tr><td>{pos}</td><td>{esc(row.get('_name', '?'))}"
                       + ("" if bats else " <span class=g>(doesn't bat)</span>") + "</td>")
            ratio = None
            league = self._sim_league_position(data, side, pos)
            if league:
                r = mlbstats.range_ratio(mlbstats.fielding_row_figures(row), league)
                ratio = r[3] if r else None
            club = team.club_range.get(pos)
            hits = (f"\u00d7{team.range_against_club(row['_id'], pos) ** -simulator.DEFENSE_ELASTICITY:.3f}"
                    if club else "")
            errors = "" if pos == "P" else f"\u00d7{team.error_factor(row['_id'], pos):.3f}"
            out.append(f"<td class=n>{'' if ratio is None else f'{ratio:.1%}'}</td>"
                       f"<td class=n>{'' if not club else f'{club:.1%}'}</td><td class=n>{hits}</td>"
                       f"<td class=n>{int(mlbstats.as_number(row.get('errors')) or 0)}</td>"
                       f"<td class=n>{int(mlbstats.as_number(row.get('chances')) or 0)}</td>"
                       f"<td class=n>{errors}</td></tr>")
        out.append("</table>")
        out.append("<p class=g>Each ball in play goes where that season's balls of its kind went (Retrosheet's hit "
                   "locations: a fielder's ground, or a hole or gap two share), and only the fielders whose ground "
                   "that is count on it: the odds of a hit there are multiplied by the figure in <i>Hits on his "
                   "ground</i> (his range against the club's season at the position, which the pitchers' lines "
                   "already have in them), and of an error by <i>Errors on his ground</i> (his errors per chance "
                   "against MLB's).  A hole or gap takes each fielder's part equally.  Whoever is pitching fields "
                   "the pitcher's ground with his own errors.</p>")
        # pitching
        out.append("<h3>Pitching staff</h3><p class=g>"
                   + ("<b>Your rotation, in strict turn: " + esc(", ".join(p.name for p in team.rotation)) + ".</b>  "
                      if team.custom_rotation else "Starts are shared out like the real ones.  ")
                   + "The Prevention tab's roles; a starter goes until he has faced his usual batters per start, a reliever his usual "
                   "batters per relief appearance.  IP per 162 games for comparison with a season.  R/9 simulated, "
                   "real after the slash.</p>")
        out.append("<table><tr><th>Role</th><th>Pitcher</th><th>BF/start</th><th>BF/relief</th><th>Starts</th>"
                   "<th>G</th><th>IP per 162</th><th>R/9</th><th>K/9</th><th>BB/9</th></tr>")
        for role in mlbstats.PITCHER_ROLES:
            for p in team.roles[role]:
                st = pit.get(p.pid)
                outs = st["outs"] if st else 0
                real_outs = mlbstats.pitcher_workload(p.row)[2]
                real_r9 = 27 * (mlbstats.as_number(p.row.get("runs")) or 0) / real_outs if real_outs else None
                sim_r9 = 27 * st["R"] / outs if st and outs else None
                mark = " (closer)" if p is team.closer else ""
                out.append(f"<tr><td>{role}{mark}</td><td>{esc(p.name)}"
                           + (f" <span class=g>({p.throws})</span>" if p.throws else "") + "</td>"
                           f"<td class=n>{'' if not p.bf_start else f'{p.bf_start:.1f}'}</td>"
                           f"<td class=n>{p.bf_relief:.1f}</td><td class=n>{st['GS'] if st else 0:,}</td>"
                           f"<td class=n>{st['G'] if st else 0:,}</td>"
                           f"<td class=n>{outs / 3 / n * 162:.1f}</td>"
                           f"<td class=n>{'' if sim_r9 is None else f'{sim_r9:.2f}'}"
                           f"{'' if real_r9 is None else f' <span class=g>/ {real_r9:.2f}</span>'}</td>"
                           f"<td class=n>{'' if not outs else f'{27 * st[chr(75)] / outs:.1f}'}</td>"
                           f"<td class=n>{'' if not outs else f'{27 * st[chr(66) + chr(66)] / outs:.1f}'}</td></tr>")
        out.append("</table>")
        return "".join(out)

    def _sim_league_position(self, data, side, pos):
        team = data["teams"][side]
        return team.league_positions.get(pos)

    def _show_sim_game(self):
        data = self._sim_result
        if data is None:
            self.sim_game_view.setHtml("<p>Play some games first.</p>")
            return
        res, (v, h), envs = data["res"], data["teams"], data["envs"]
        index = self.sim_game_spin.value() - 1
        played = {}
        log, score, host = simulator.replay(v, h, envs, res["games"], res["seed"], index,
                                            (res.get("tired") or [None] * (index + 1))[index], res, played)
        if res.get("rest_runs"):
            wp = simulator.game_win_probability(log, score, host, res, envs[host].placed_runner)
            swings = sorted(wp["plays"], key=lambda p: -abs(p["added"]))[:3]
            innings = max((line["inning"] for line in log if line.get("kind") == "pa"), default=9)
            self.sim_wp_chart.set_data(wp, (v.name, h.name)[1 - host], (v.name, h.name)[host],
                                       halves=max(18, 2 * innings), marked=[p["index"] for p in swings])
        else:
            self.sim_wp_chart.set_data(None, message="No win probability for this run.")
        esc = html.escape
        names = (v.name, h.name)
        if self.sim_game_show.currentIndex() == 1:
            self.sim_game_view.setHtml(self._sim_box_html(index, (v, h), host, score,
                                                          simulator.box_score(log, played, (v, h), host, score)))
            return
        out = [self._SIM_CSS, f"<h2>Game {index + 1}: {esc(v.name)} {score[0]}, {esc(h.name)} {score[1]}</h2>"
               f"<p>At {esc(names[host])}'s park (its environment); {esc(names[1 - host])} bat first.</p>",
               "<p class=g>Each plate appearance: the chance of every result for this batter against this pitcher "
               "(log5 of their lines -- against each other's hand, with the side the batter batted from and the "
               "pitcher's hand in brackets -- then the fielders), the random number drawn (results are stacked in the order "
               "K, BB, HBP, 1B, 2B, 3B, HR, ROE, GO, AO and the number falls in one), then how the runners moved: an "
               "outcome drawn from what happened in the majors on that kind of play in that base-out state, "
               "weighted by the runners' own records.</p>"]
        half = None
        for line in log:
            kind = line["kind"]
            if kind == "pa":
                key = (line["inning"], line["side"])
                if key != half:
                    half = key
                    out.append(f"<h3>{'Top' if line['top'] else 'Bottom'} of the {line['inning']}"
                               f"{_ordinal_suffix(line['inning'])} — {esc(names[line['side']])} batting "
                               f"<span class=g>({line['score'][0]}–{line['score'][1]})</span></h3>")
                bases = ", ".join(("1st", "2nd", "3rd")[i] for i, b in enumerate(line["bases"]) if b) or "empty"
                if line["cls"] in ("IBB", "SH"):  # decided before the pitch, by the situation
                    probs = (f"{'intentional walk' if line['cls'] == 'IBB' else 'sacrifice bunt'}: "
                             f"{line['probs'][line['cls']]:.1%} in this situation (base-out state, inning, margin: the "
                             "league's, tilted by the batter's own)")
                else:
                    probs = " · ".join(f"{c} {line['probs'].get(c, 0):.1%}" for c in simulator.CLASSES)
                tilt = [f"{line['bases'][i].name} ×{f:.2f}" for i, f in enumerate(line["factors"])
                        if line["bases"][i] is not None and f != 1.0]
                if line["cls"] == "GO" and line["bases"][0] is not None and line["outs"] < 2 and \
                        (line["gdp"] != 1.0 or line.get("turn", 1.0) != 1.0):
                    tilt.append(f"double plays: the batter ×{line['gdp']:.2f}, {line.get('picked') or 'the fielder'} "
                                f"×{line.get('turn', 1.0):.2f}")
                if line.get("arm", 1.0) != 1.0 and any(line["bases"]):
                    tilt.append(f"each runner's own times {line.get('picked')}'s arm ×{line['arm']:.2f}")
                source = "a walk: forced runners move up" if line["cls"] == "IBB" else \
                    (f"this outcome happened {line['count']:,} times in {line['total']:,} such plays"
                          + (f"; with the tilts its chance was {line['share']:.1%}" if tilt else
                             f" ({line['share']:.1%})")) if line["total"] else "textbook advance"
                hands = f" ({line['hands'][0]} vs {line['hands'][1]})" if line.get("hands") else ""
                out.append(f"<p><b>{esc(line['batter'])}</b> vs {esc(line['pitcher'])}{hands}, {line['outs']} out, bases "
                           f"{bases}: <b>{simulator.CLASS_NAMES[line['cls']]}</b>"
                           + (f", to {esc(' and '.join(line['fielders']))}" if line.get("fielders") else "")
                           + (f", picked up by {esc(line['picked'])}" if line.get("picked")
                              and line["picked"] not in line.get("fielders", []) else "")
                           + (f" (drew {line['roll']:.3f}).  " if line["roll"] is not None else ".  ")
                           + f"{esc(line['text'])}" + (f" — <b>{line['runs']} run{'s' if line['runs'] != 1 else ''}"
                                                     f"</b>" if line["runs"] else "")
                           + f"<br><span class=g>{probs}<br>Runners: {esc(source)}"
                           + (f" (tilts: {esc(', '.join(tilt))})" if tilt else "") + "</span></p>")
            elif kind == "change":
                out.append(f"<p><i>Pitching change ({esc(names[line['side']])}): {esc(line['pitcher'])}, "
                           f"{esc(line['role'])} — {esc(line['why'])}; up to {line['stint']:.1f} batters.</i></p>")
            elif kind in ("steal", "adv"):
                out.append(f"<p><i>{esc(line['text'])}</i> <span class=g>({esc(line['detail'])})</span></p>")
            else:
                out.append(f"<p><i>{esc(line['text'])}</i>"
                           + (f" <span class=g>({esc(line['detail'])})</span>" if line.get("detail") else "") + "</p>")
        self.sim_game_view.setHtml("".join(out))

    def _sim_how_html(self, data: dict | None) -> str:
        esc = html.escape
        out = [self._SIM_CSS, "<h2>How the Simulator decides</h2>",
               "<p>Every game is played one plate appearance at a time, the way a Strat-O-Matic or APBA game is, "
               "but with probabilities worked out from the players' real seasons instead of printed on cards.</p>",
               "<h3>1. The teams, as the other tabs set them up</h3><ul>"
               "<li><b>Batting order</b> — the Production tab's: whoever batted from each place in the most "
               "games (records from 1950; before that the nine with the most plate appearances, most first).  "
               "In a league without the DH that season (AL before 1973, NL before 2022 except 2020) the pitcher "
               "bats where the club's pitchers batted most, and whoever pitches bats there.</li>"
               "<li><b>Pitching staff</b> — the Prevention tab's roles: Rotation, Long Relief, Closers, Other.</li>"
               "<li><b>Defense</b> — the Range tab's: whoever played each position the most innings.</li>"
               "<li><b>Running</b> — the Speed tab's Retrosheet counts for each batter: steal chances, attempts "
               "and steals, extra bases taken on hits, tag-ups on fly outs (and its Speedy/Smart/Slow label).</li></ul>",
               "<h3>2. One plate appearance</h3>"
               "<p>A batter's season becomes the share of his plate appearances that ended in each of nine results "
               "(strikeout, walk, hit by pitch, single, double, triple, home run, ground-ball out, air out); a "
               f"pitcher's the same from the batters he faced.  Each is pulled toward his league as if he had "
               f"{simulator.SHRINK_PA} more plate appearances at its average, so a cameo can't bat .600.  The two "
               "are combined by the <b>odds-ratio (log5) method</b>:</p>"
               "<p style='margin-left:2em'>chance of a result ∝ (batter's rate ÷ his league's) × "
               "(pitcher's rate ÷ his league's) × the environment's rate</p>"
               "<p>and the nine are scaled to add up to 100%.  The environment is the home team's league that "
               "season, so players from different eras are each measured against their own league and then "
               "placed in the home team's.  The two teams take turns at home, Team 2 first (with an odd number of "
               "games Team 2 has the extra home game), so each is at home in half the games, played in its own "
               "league's environment.</p>"
               "<p><b>Home parks.</b>  The environment is also the home team's park: each result's rate there is "
               "multiplied by the park's factor -- how often it happened in the club's home games (both teams "
               "batting) against its road games that season (Retrosheet), with "
               f"{simulator.SHRINK_PARK_PA:,} neutral plate appearances added so a season's luck counts for less.  "
               "A player's own line was made half in his home park, so half of that is taken out first.</p>"
               "<p><b>Platoon (left/right).</b>  Batters do better against pitchers throwing from the other side.  "
               "Retrosheet's play-by-play of the season gives each result's rate in each matchup of hands (a "
               "left-handed batter against a left-hander, and so on) against all plate appearances, and the "
               "environment is multiplied by that matchup's ratio.  A player's own lines already have his matchups "
               "in them, so they're first divided by the ones he had.  MLB's splits (from 1950) then move each player "
               "toward his own record against each hand, pulled toward his whole season as if each split had "
               f"{simulator.SHRINK_PLATOON_PA['R']:,} more plate appearances (right-handed and switch hitters), "
               f"{simulator.SHRINK_PLATOON_PA['L']:,} (left-handed) or {simulator.SHRINK_PLATOON_BF:,} batters "
               "faced (pitchers): most of a split is luck (after <i>The Book</i>, Tango, Lichtman and Dolphin).  A "
               "switch hitter bats away from the pitcher.  Before 1950 only the league's effect plays; without "
               "Retrosheet's hands, none.</p>"
               "<p><b>Fielding.</b>  Each ball in play goes where that kind of ball (single, double, ground out, "
               "air out) went in the environment season, by Retrosheet's hit locations: one fielder's ground "
               "(\u201c6\u201d, the shortstop's) or a hole or gap two share (\u201c56\u201d, \u201c78\u201d); "
               "where locations weren't recorded, the fielder who picked the ball up (and for a ground-ball "
               "hit to the outfield, the infielders it got past).  Only the fielders whose ground it is count on "
               "it.  A pitcher's line was earned in front of his club's fielders all season, so for each "
               "position the fielder's range is taken against the club's season there: the odds of a hit on his "
               f"ground are multiplied by (his range \u00f7 the club's) to the power \u2212{simulator.DEFENSE_ELASTICITY}.  "
               "That exponent was measured over 168 team-seasons, 1975\u20132023: a club whose fielders reached 1% "
               "more chances allowed about 1.3% fewer hits on balls in play (correlation \u22120.52) \u2014 measured "
               "for whole teams and used here for each fielder's ground.  An error happens on the league's share "
               "of the outs on that ground, scaled by the fielder's errors per chance against MLB's at his "
               f"position ({simulator.SHRINK_ERROR_CHANCES} average chances added).  A hole or gap takes each fielder's part equally (the "
               "geometric mean).  The catcher's and pitcher's range isn't measured; whoever is pitching fields "
               "the pitcher's ground with his own errors.</p>",
               "<h3>3. The runners</h3>"
               "<p>Where the runners go is not a rule of thumb: Retrosheet's play-by-play of the environment season "
               "says, for every kind of result and every base-out state (runners on and outs), how often each "
               "outcome happened — where the batter and every runner ended up, outs on the bases and errors "
               "included.  The Simulator draws one in proportion.  Each runner then tilts the draw by his own record "
               "(an outcome in which he takes x extra bases is weighted by his factor to the power x): extra bases "
               "taken on singles and doubles, tag-ups on air outs \u2014 each times the arm of the fielder who picked "
               "up the ball (who that was, in that zone, comes from Retrosheet too): on hits he picked up, how often "
               "runners took an extra base, and on fly balls he caught, how often they tagged up and advanced, "
               "against MLB's at his position, in odds.  A batter's double plays (per ground out, against "
               "the league's) times how often the fielder who fielded the grounder turned double-play chances "
               "(against MLB's at his position) tilt ground outs that make two outs.  Runners' figures are pulled "
               "toward the league's "
               f"too ({simulator.SHRINK_STEAL_CHANCES} steal chances, {simulator.SHRINK_STEAL_ATTEMPTS} attempts, "
               f"{simulator.SHRINK_EXTRA_BASE} extra-base and {simulator.SHRINK_TAG_UP} tag-up chances, "
               f"{simulator.SHRINK_GROUND_OUTS} ground outs), and so are the fielders' "
               f"({simulator.SHRINK_ARM_EXTRA_BASE} extra-base and {simulator.SHRINK_ARM_TAG_UP} tag-up chances, "
               f"{simulator.SHRINK_TURNING_DPS} double-play grounders).</p>"
               "<p>Before each plate appearance a runner with the next base open may try to steal: the league's "
               "attempt and success rates from that base, tilted by his own times the battery's.  The catcher's "
               "attempts per inning and runners safe are against MLB's catchers; the pitcher's attempts per batter "
               "faced and runners safe against his own staff's (who threw to the same catchers, so they aren't "
               "counted twice); and whether he's safe, times the infielders covering the base -- second base and "
               "short sharing a steal of second, third base a steal of third -- each against his own club's runners "
               f"safe on those steals (Retrosheet; {simulator.SHRINK_TAG_ATTEMPTS} attempts at the club's rate "
               "added).  Wild pitches, passed balls, balks and pickoffs happen as often as they did in that "
               "base-out state.</p>",
               "<h3>4. Managing</h3><ul>"
               "<li>Starts are shared out in proportion to the Rotation's real starts.</li>"
               "<li>Without the DH, the pitcher's spot is pinch-hit for when a reliever is due up, and for the "
               f"starter from the {simulator.PINCH_HIT_FROM_INNING}{_ordinal_suffix(simulator.PINCH_HIT_FROM_INNING)} "
               "inning (he then leaves the game): by the bench's busiest "
               "hitter not yet used.  The DH rule is each team's league's that season, unless you choose one for both "
               "(the DH box above Play).</li>"
               "<li>A two-way player (one who played 20 or more games without pitching: Ruth, Ohtani) bats in his "
               "own place and starts his share of games; he doesn't relieve while he's in the order.  On his starts, "
               "with the DH from 2022 he stays the DH; with the DH before that the club gives up the DH, so his "
               "reliever bats in his place; with no DH the bench's best fielder at his position plays there and "
               "bats in the pitcher's spot until he leaves the mound and goes back to it.</li>"
               "<li>A starter pitches until he has faced his usual batters per start (Retrosheet's outs per start "
               "× his batters per out; estimated from MLB's line where Retrosheet hasn't the season), so a bad "
               "day ends earlier in the game.  A reliever goes for his usual batters per relief appearance.</li>"
               "<li>The reliever is chosen by the situation: the closer (the Closers' most games finished) with a "
               "lead of 1–3 from the 9th, brought in at the start of the inning if whoever is pitching would "
               "run out during it; the other Closers in the 7th and 8th with the score close (down 1 to up 3); Long "
               "Relief if the starter goes before the 6th; otherwise Other and Long Relief.  Within a role, relievers "
               "are picked in proportion to their real relief work.</li>"
               "<li><b>Pinch runners.</b>  From the 8th inning, with the game within two runs, a runner the Speed tab calls a "
               "Slow regular gives way to the first bench runner available in the Speed tab's order: Speedy bench, then "
               "Speedy regulars, then Smart regulars, each fastest first (bases gained per opportunity).  He takes the "
               "Slow regular's place in the batting order.  When his side takes the field, the position goes to "
               "whoever has the best fielding record there \u2014 the pinch runner or a bench player who played it "
               "(range as a share of MLB's chances, with 50 league-average innings added so a few innings don't win "
               "the job).  If nobody could play the position afterwards, there is no pinch runner.</li>"
               "<li>On the Team 1 and Team 2 pages you can choose a side's batting order, who plays each position (anyone "
               "who played it that season) and a rotation of your own, which goes in strict turn.</li>"
               "<li>The home team doesn't bat in the 9th when ahead; a home team's winning run ends the game (all "
               "runs count on a home run).  From 2020 extra innings start with a runner on second.</li></ul>",
               "<h3>5. Checks</h3>"
               "<p>With every batter and pitcher set to exactly the league's rates, the Simulator scores within 2% of "
               "the league's real runs per game in 1927 AL (0.99), 1975 NL (1.02), 1985 NL (1.01), 2019 NL (1.00) and 2023 "
               "AL (1.02), and with pinch hitting for the pitcher, 2015 NL (1.01) and 1975 NL (1.02) -- each kind of batter "
               "at his kind's average (position players at the position players', pitchers at the pitchers'); the early-1970s AL runs a little high (1971 1.04, 1972 1.05, 1973 1.03), something of that "
               "low-scoring game, such as its sacrifice bunting, that the model doesn't play.  Where MLB's lines don't "
               "split outs into ground and air outs (before the mid-1970s) the season's Retrosheet play-by-play gives the "
               "league's split, and every player's outs are split that way.  A club's regulars playing every game score a few percent more than its real season, which "
               "had bench players and rest days in it.</p>",
               "<h3>6. Left out</h3>"
               "<p>Pinch hitters other than for the pitcher, other defensive changes, platoon-minded managing "
               "(pinch hitters and relievers chosen for the hand), home-field advantage "
               "beyond batting last and the home park, bunting strategy, intentional walks by situation, and fatigue from game "
               "to game.  Each game is independent: game 437 can be replayed exactly on the play-by-play page.</p>"]
        if data is not None:
            out.append(self._sim_worked_example(data))
        return "".join(out)

    @staticmethod
    def _sim_hands_text(bats: str, hands: tuple) -> str:
        """``("L", "R")`` -> "Bats left against a right-hander" (a switch hitter: "a switch hitter, batting ...")."""
        side, hand = ("left" if h == "L" else "right" for h in hands)
        return (f"A switch hitter, batting {side}-handed" if bats == "S" else f"Bats {side}") + \
            f" against a {hand}-hander"

    def _sim_worked_example(self, data: dict) -> str:
        esc = html.escape
        (v, h), env = data["teams"], data["envs"][1]  # Team 2 at home, as in the first game
        pitcher = h.rotation[0] if h.rotation else h.pitchers[0]
        slot = 0
        batter = v.lineup[slot]
        rows = simulator.matchup_table(v, slot, h, pitcher, env)
        pct = lambda x: "" if x is None else f"{x:.1%}"
        name = batter.name if batter else "the pitcher's spot"
        hands = rows[0]["hands"] if rows else None
        out = [f"<h3>Worked example: {esc(name)} ({esc(v.name)}) leading off against {esc(pitcher.name)} "
               f"({esc(h.name)}), at {esc(h.name)}'s as in game 1</h3>"]
        if hands:
            bats = batter.bats if batter else v.rotation[0].bats
            out.append(f"<p class=g>{esc(self._sim_hands_text(bats, hands))}: the batter's and pitcher's columns are "
                       "their rates against that hand and side, and the environment's is the home league's in its park "
                       "for this matchup of hands.</p>")
        out.append("<table><tr><th>Result</th><th>Batter</th><th>His league</th><th>Pitcher</th>"
                   "<th>His league</th><th>Environment</th><th>log5 mix</th><th>After fielders</th></tr>")
        for r in rows:
            out.append(f"<tr><td>{esc(r['name'])}</td>" + "".join(
                f"<td class=n>{pct(r[k])}</td>" for k in ("batter", "b_league", "pitcher", "p_league", "env", "log5",
                                                         "final")) + "</tr>")
        out.append("</table>")
        k = next(r for r in rows if r["cls"] == "HR")
        if k["batter"] and k["pitcher"] and k["b_league"] and k["p_league"]:
            out.append(f"<p>For a home run: ({pct(k['batter'])} ÷ {pct(k['b_league'])}) × "
                       f"({pct(k['pitcher'])} ÷ {pct(k['p_league'])}) × {pct(k['env'])} = "
                       f"{(k['batter'] / k['b_league']) * (k['pitcher'] / k['p_league']) * k['env']:.2%} before the "
                       f"nine are scaled to 100%, {pct(k['log5'])} after.</p>")
        # a runner table: single with a runner on first, nobody out
        cell = env.tables.trans.get(("1B", 1, 0))
        if cell:
            runner = next((b for b in v.lineup if b is not None and b.running.get("counts")), None)
            total = sum(e[1] for e in cell)
            out.append("<h3>Worked example: a single with a runner on first, nobody out</h3>"
                       f"<p class=g>In {env.tables_season} this happened {total:,} times in the majors.  Outcome = where "
                       "the batter / the runner from first ended up.</p><table><tr><th>Batter</th><th>Runner</th>"
                       "<th>Times</th><th>League share</th>"
                       + (f"<th>With {esc(runner.name)} on first (extra bases ×{runner.running['xb']:.2f})</th>"
                          if runner else "") + "</tr>")
            weights = [e[1] * (runner.running["xb"] ** e[2][0] if runner else 1) for e in cell]
            wsum = sum(weights)
            words = {"1": "first", "2": "second", "3": "third", "H": "scores", "X": "out"}
            for e, w in zip(cell[:8], weights[:8]):
                out.append(f"<tr><td>{words.get(e[0][0], e[0][0])}</td><td>{words.get(e[0][1], e[0][1])}</td>"
                           f"<td class=n>{e[1]:,}</td><td class=n>{e[1] / total:.1%}</td>"
                           + (f"<td class=n>{w / wsum:.1%}</td>" if runner else "") + "</tr>")
            out.append("</table>")
        return "".join(out)

    def _build_standings_tab(self) -> QWidget:
        """Team standings for one season -- every league that played it, by division from 1969
        -- or, from 1994, each league in wild card order (division leaders, wild card holders, then
        the rest by games behind the last wild card spot) -- or one franchise's line in every season it played, under whatever name it went by
        then (franchise ids survive moves and renames; a new name is picked out, and the
        names are listed above the table).  Team and Season pick the franchise: the Team list
        is the league as it stood in the chosen season, as on the other tabs."""
        self.st_mode_season = QRadioButton("One season, all teams")
        self.st_mode_wildcard = QRadioButton("One season, wild card order")
        self.st_mode_team = QRadioButton("One team, all its seasons")
        self.st_mode_season.setChecked(True)
        mode_group = QButtonGroup(self)
        for button in (self.st_mode_season, self.st_mode_wildcard, self.st_mode_team):
            mode_group.addButton(button)
        mode_group.buttonClicked.connect(lambda _b: self._on_st_mode_changed())

        self.st_team_combo = QComboBox()
        self.st_team_combo.setMinimumWidth(240)
        self.st_team_combo.setEnabled(False)
        self.st_team_combo.currentIndexChanged.connect(lambda _i: self._on_st_team_changed())
        self.st_season_label = QLabel("Season")
        self.st_season_spin = _YearSpin()
        self.st_season_spin.setRange(1876, 2100)
        self.st_season_spin.setKeyboardTracking(False)
        self.st_season_spin.setToolTip("Typing a year needs Enter (or click elsewhere) to take effect.")
        self._st_debounce = QTimer(self, singleShot=True, interval=250)
        self._st_debounce.timeout.connect(self._st_season_settled)
        self.st_season_spin.valueChanged.connect(lambda _: self._st_debounce.start())
        self.st_season_spin.valueChanged.connect(self._st_sync_wildcard)

        bar = QHBoxLayout()
        bar.addWidget(self.st_mode_season)
        bar.addWidget(self.st_mode_wildcard)
        bar.addWidget(self.st_mode_team)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Team"))
        bar.addWidget(self.st_team_combo)
        bar.addSpacing(6)
        bar.addWidget(self.st_season_label)
        bar.addWidget(self.st_season_spin)
        bar.addStretch(1)

        self.st_title = QLabel()
        self.st_title.setWordWrap(True)
        font = self.st_title.font()
        font.setBold(True)
        self.st_title.setFont(font)
        self.st_names = QLabel()
        self.st_names.setWordWrap(True)
        self.st_names.setVisible(False)
        self.st_note = QLabel()
        self.st_note.setWordWrap(True)
        self.st_note.setVisible(False)
        self._st_rendered = False       # has any standings table been drawn yet?
        self._st_pyth_used = None       # the Pythagorean definitions the table on screen was drawn with

        self.st_model = QStandardItemModel(self)
        self.st_table = QTableView()
        self.st_table.setModel(self.st_model)
        self.st_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.st_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.st_table.verticalHeader().setVisible(False)
        self.st_table.horizontalHeader().setHighlightSections(False)
        self.st_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(bar)
        outer.addWidget(self.st_title)
        outer.addWidget(self.st_names)
        outer.addWidget(self.st_table, 1, Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(self.st_note)
        self._st_sync_wildcard(self.st_season_spin.value())
        return page

    def _pyth_definitions(self) -> list[tuple[str, str, str]]:
        """``[(column, custom stat name, formula), ...]`` for each Pythagorean stat in
        ``PYTHAGOREAN_STATS`` that is defined at Team level and needs nothing beyond a standings
        line's runs, wins and losses -- in column order."""
        found = []
        for column, stat_name, _file in PYTHAGOREAN_STATS:
            for level, name, ftext in self._custom_stats:
                if level == "team" and name.lower() == stat_name.lower():
                    try:
                        usable = formula.formula_variables(ftext) <= mlbstats.STANDINGS_VARIABLES
                    except formula.FormulaError:
                        usable = False
                    if usable:
                        found.append((column, stat_name, ftext))
                    break
        return found

    def _refresh_standings_pyth(self):
        """The custom stats changed: redraw the standings if a Pythagorean definition did
        (it comes from the cache, so this is quick)."""
        if self._st_rendered and self._pyth_definitions() != self._st_pyth_used:
            self._reload_standings()

    def _st_columns(self, headers: list[str]) -> list[str]:
        """The table's headers plus a column for each available Pythagorean stat; sets them on
        the model (the new ones with tooltips)."""
        extra = self._pyth_definitions()
        headers = headers + [column for column, _n, _f in extra]  # "PW" ... name the columns for the callers
        self.st_model.setHorizontalHeaderLabels([PYTHAGOREAN_HEADERS.get(h, h) for h in headers])
        for offset, (_column, stat_name, _ftext) in enumerate(extra, start=len(headers) - len(extra)):
            self.st_model.horizontalHeaderItem(offset).setToolTip(f"{stat_name} (a custom stat, see the Custom tab)")
        return headers

    def _st_pyth_cells(self, team: dict) -> list[QStandardItem]:
        """A standings line's cells for the Pythagorean columns: wins and losses to the nearest
        whole game (the exact figure in the tooltip), the percentage as .610."""
        cells = []
        for column, _name, ftext in self._pyth_definitions():
            value = mlbstats.standings_stat(ftext, team)
            if value is None:
                cells.append(self._st_cell(""))
            elif column == "PPct":
                cells.append(self._st_cell(_fmt_rate(value)))
            else:
                cells.append(self._st_cell(int(value + 0.5), tip=f"{value:.1f}"))
        return cells

    def _st_finish(self, note: str = ""):
        """After drawing a table: remember which Pythagorean definitions it used, and show
        the note under it -- ``note`` plus a word on any missing stat."""
        defined = self._pyth_definitions()
        self._st_rendered, self._st_pyth_used = True, defined
        have = {column for column, _n, _f in defined}
        missing = [(name, file) for column, name, file in PYTHAGOREAN_STATS if column not in have]
        if missing:
            note = (note + "  " if note else "") + (
                "Pythagorean columns need the team-level custom stat(s) "
                + ", ".join(f"\u201c{name}\u201d" for name, _f in missing)
                + " \u2014 load " + ", ".join(file for _n, file in missing)
                + " on the Custom tab (Load definitions\u2026); they may use only Runs, Runs allowed, Wins and Losses.")
        self.st_note.setText(note)
        self.st_note.setVisible(bool(note))

    def _st_mode(self) -> str:
        """"season", "wildcard" or "team"."""
        if self.st_mode_team.isChecked():
            return "team"
        return "wildcard" if self.st_mode_wildcard.isChecked() else "season"

    def _st_sync_wildcard(self, season: int):
        """The wild card format began in 1994: before that there is no such list."""
        available = season >= mlbstats.WILD_CARD_FIRST_SEASON
        self.st_mode_wildcard.setEnabled(available)
        self.st_mode_wildcard.setToolTip(
            "Division leaders, then wild card holders, then the rest by games behind the last wild "
            "card spot." if available else f"The wild card began in {mlbstats.WILD_CARD_FIRST_SEASON}.")
        if not available and self.st_mode_wildcard.isChecked():
            self.st_mode_season.setChecked(True)  # the settled season reloads it

    def _on_st_mode_changed(self):
        team_mode = self._st_mode() == "team"
        self.st_team_combo.setEnabled(team_mode)
        self.st_season_label.setText("Team list from" if team_mode else "Season")
        self.st_season_spin.setToolTip(
            "The Team list is the league as it stood in this season.  Typing a year needs Enter "
            "(or click elsewhere) to take effect." if team_mode else
            "Typing a year needs Enter (or click elsewhere) to take effect.")
        self._reload_standings()

    def _on_st_team_changed(self):
        if self._st_mode() == "team":
            self._reload_standings()

    def _st_season_settled(self):
        """The season changed: show it (one-season mode) and re-list the teams as that
        season had them."""
        season = self.st_season_spin.value()
        if self._st_mode() in ("season", "wildcard"):
            self._reload_standings()
        cached = self._season_names.get(season)
        if cached is not None:
            self._st_apply_names(cached)
            return
        ttl = LIVE_TTL if season >= self._season_now else None
        self._run_latest("st_teams", lambda: (season, mlbstats.get_team_map(season, ttl)),
                         self._on_st_teams_loaded, lambda m: self._on_team_refresh_failed("st", m))

    def _on_st_teams_loaded(self, data):
        season, names = data
        self._season_names[season] = names
        if season == self.st_season_spin.value():
            self._st_apply_names(names)

    def _st_apply_names(self, names: dict):
        """List the teams as they stood in the chosen season, keeping the same franchise selected
        if it played then (otherwise the first team, and -- in one-team mode -- the table
        follows)."""
        if not names:
            return
        keep_id = self.st_team_combo.currentData()
        teams = sorted(names.values(), key=lambda t: t["name"])
        self.st_team_combo.blockSignals(True)
        self.st_team_combo.clear()
        target = 0
        for i, team in enumerate(teams):
            self.st_team_combo.addItem(team["name"], team["id"])
            if team["id"] == keep_id:
                target = i
        self.st_team_combo.setCurrentIndex(target)
        self.st_team_combo.blockSignals(False)
        if self._st_mode() == "team" and self.st_team_combo.currentData() != keep_id:
            self._reload_standings()

    def _show_standings_message(self, text: str):
        self.st_title.setText(text)
        self.st_names.setVisible(False)
        self.st_note.setVisible(False)
        self.st_table.clearSpans()
        self.st_model.clear()

    def _reload_standings(self):
        season = self.st_season_spin.value()

        def ttl(year):
            return LIVE_TTL if year >= self._season_now else None

        mode = self._st_mode()
        if mode == "season":
            self._show_standings_message(f"Loading {season}\u2026")
            self._run_latest("st_load", lambda: (season, mlbstats.get_standings(season, ttl(season))),
                             self._on_st_season_loaded, self._on_st_failed)
            return
        if mode == "wildcard":
            self._show_standings_message(f"Loading {season}\u2026")
            self._run_latest("st_load", lambda: (season, mlbstats.get_wildcard_standings(season, ttl(season))),
                             self._on_st_wildcard_loaded, self._on_st_failed)
            return
        team_id = self.st_team_combo.currentData()
        if team_id is None:
            self._show_standings_message("Choose a team above.")
            return
        name, first_year = self._team_identity(team_id, season, self.st_team_combo.currentText())
        start = max(1876, first_year - 2)  # a club's recorded first year can be a season or two off
        self._show_standings_message(f"Loading {name}, every season\u2026  (the first time takes a few seconds)")
        self._run_latest(
            "st_load",
            lambda: (team_id, name, mlbstats.franchise_history(team_id, range(start, self._season_now + 1), ttl)),
            self._on_st_team_loaded, self._on_st_failed)

    def _on_st_failed(self, message: str):
        self._show_standings_message(f"Couldn't load the standings: {message}")
        self._say("st", f"Standings: {message}")

    def _st_cell(self, text, right: bool = True, bold: bool = False, italic: bool = False,
                 tip: str = "") -> QStandardItem:
        item = QStandardItem("" if text is None else str(text))
        item.setEditable(False)
        if right:
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if bold or italic:
            font = item.font()
            font.setBold(bold)
            font.setItalic(italic)
            item.setFont(font)
        if tip:
            item.setToolTip(tip)
        return item

    @staticmethod
    def _star_sample(model, sample: float, skip_columns=(), rows=None):
        """Mark the figures in ``model`` (its numbers and percentages; names and bands are left alone)
        with * as resting on Retrosheet's play-by-play of only ``sample`` of the team-season."""
        tip = f"From Retrosheet\u2019s play-by-play of only {sample:.0%} of the team\u2019s innings: a sample of the season"
        for r in (range(model.rowCount()) if rows is None else rows):
            for c in range(model.columnCount()):
                item = model.item(r, c)
                text = item.text() if item is not None and c not in skip_columns else ""
                if text and (text[0].isdigit() or text[0] in "+-\u2212.") and not text.endswith("*"):
                    item.setText(text + "*")
                    item.setToolTip((item.toolTip() + "\n" if item.toolTip() else "") + tip)

    @staticmethod
    def _st_gb(text: str) -> str:
        return "\u2013" if text in ("-", "") else text

    @staticmethod
    def _st_diff(value) -> str:
        return "" if value is None else f"{value:+d}"

    def _fit_standings_table(self):
        """No wider than its columns (plus the scroll bar) -- the height takes what's left."""
        table = self.st_table
        table.resizeColumnsToContents()
        table.setFixedWidth(table.horizontalHeader().length() + 2 * table.frameWidth()
                            + table.verticalScrollBar().sizeHint().width() + 4)

    def _on_st_season_loaded(self, data):
        season, groups = data
        self._show_standings_message("")
        if not groups:
            self.st_title.setText(f"{season}: no standings recorded.")
            return
        ties = any(t["ties"] for g in groups for t in g["teams"])
        headers = self._st_columns(["Team", "W", "L"] + (["T"] if ties else []) + ["Pct", "GB", "RS", "RA", "Diff"])
        for group in groups:
            row = self.st_model.rowCount()
            band = [self._st_cell((group["name"] + (" *" if group.get("computed") else "")) if col == 0 else "",
                                  right=False, bold=True) for col in range(len(headers))]
            for item in band:
                item.setBackground(self.palette().midlight())
            self.st_model.appendRow(band)
            self.st_table.setSpan(row, 0, 1, len(headers))
            for team in group["teams"]:
                cells = [self._st_cell(team["name"], right=False, bold=team["rank"] == 1),
                         self._st_cell(team["wins"]), self._st_cell(team["losses"])]
                if ties:
                    cells.append(self._st_cell(team["ties"] or ""))
                cells += [self._st_cell(team["pct"]), self._st_cell(self._st_gb(team["gb"])),
                          self._st_cell(team["rs"]), self._st_cell(team["ra"]),
                          self._st_cell(self._st_diff(team["diff"]))] + self._st_pyth_cells(team)
                self.st_model.appendRow(cells)
        self.st_title.setText(f"{season} standings")
        computed = [g["name"] for g in groups if g.get("computed")]
        note = ""
        if computed:
            note = (f"* MLB recognizes the {' or '.join(computed)} in {season}, but MLB's API has no standings for "
                    "it; rank, games back and percentage are worked out from each club's season win-loss record.")
            if season == 1892:
                note += "  The National League played a split season that year; this is the full-season record."
        self._st_finish(note)
        self._fit_standings_table()

    def _on_st_wildcard_loaded(self, data):
        season, leagues = data
        self._show_standings_message("")
        if not any(league["teams"] for league in leagues):
            self.st_title.setText(f"{season}: no standings recorded.")
            return
        ties = any(t["ties"] for league in leagues for t in league["teams"])
        headers = self._st_columns(["Team", "Status", "W", "L"] + (["T"] if ties else [])
                                   + ["Pct", "GB", "RS", "RA", "Diff"])
        for league in leagues:
            row = self.st_model.rowCount()
            band = [self._st_cell(league["league"] if col == 0 else "", right=False, bold=True)
                    for col in range(len(headers))]
            for item in band:
                item.setBackground(self.palette().midlight())
            self.st_model.appendRow(band)
            self.st_table.setSpan(row, 0, 1, len(headers))
            for team in league["teams"]:
                in_position = team["section"] != "other"
                gb = self._st_cell(self._st_gb(team["gb"]),
                                   tip=f"{team['ahead']} games ahead of the last wild card spot" if team["ahead"] else "")
                cells = [self._st_cell(team["name"], right=False, bold=in_position),
                         self._st_cell(team["status"], right=False),
                         self._st_cell(team["wins"]), self._st_cell(team["losses"])]
                if ties:
                    cells.append(self._st_cell(team["ties"] or ""))
                cells += [self._st_cell(team["pct"]), gb, self._st_cell(team["rs"]), self._st_cell(team["ra"]),
                          self._st_cell(self._st_diff(team["diff"]))] + self._st_pyth_cells(team)
                self.st_model.appendRow(cells)
        self.st_title.setText(f"{season} \u2014 wild card order")
        note = "GB is games behind the last wild card spot; \u2013 means leading a division or holding a wild card spot."
        if season == 1994:
            note += "  The 1994 season ended with the strike, before any wild card was awarded."
        elif season == 2020:
            note += "  In 2020 the top two teams in each division qualified, plus two wild cards."
        self._st_finish(note)
        self._fit_standings_table()

    def _on_st_team_loaded(self, data):
        team_id, name, (rows, failed) = data
        self._show_standings_message("")
        if not rows:
            self.st_title.setText(f"{name}: no standings recorded.")
            return
        eras = mlbstats.name_eras(rows)
        ties = any(r["ties"] for r in rows)
        headers = self._st_columns(["Season", "Team", "Division / league", "Finish", "W", "L"]
                                   + (["T"] if ties else []) + ["Pct", "GB", "RS", "RA", "Diff"])
        previous = None
        for row in rows:
            renamed = previous is not None and row["name"] != previous
            tip = f"Renamed from {previous}" if renamed else ""
            cells = [self._st_cell(f"{row['season']} *" if row.get("computed") else row["season"], right=False,
                                   tip="MLB's API has no standings for this season; rank and games back are "
                                       "worked out from win-loss records" if row.get("computed") else ""),
                     self._st_cell(row["name"], right=False, bold=renamed, tip=tip),
                     self._st_cell(row["group"], right=False),
                     self._st_cell(mlbstats.ordinal(row["rank"]) if row["rank"] else "",
                                   bold=row["rank"] == 1),
                     self._st_cell(row["wins"]), self._st_cell(row["losses"])]
            if ties:
                cells.append(self._st_cell(row["ties"] or ""))
            cells += [self._st_cell(row["pct"]), self._st_cell(self._st_gb(row["gb"])),
                      self._st_cell(row["rs"]), self._st_cell(row["ra"]),
                      self._st_cell(self._st_diff(row["diff"]))] + self._st_pyth_cells(row)
            self.st_model.appendRow(cells)
            previous = row["name"]
        latest = eras[-1][0]
        title = f"{latest} \u2014 {rows[0]['season']}\u2013{rows[-1]['season']}, {len(rows)} seasons"
        if failed:
            title += f"  ({len(failed)} season(s) couldn't be loaded)"
        self.st_title.setText(title)
        if len(eras) > 1:
            self.st_names.setText("Names:  " + "  \u00b7  ".join(
                f"{n} {a}" if a == b else f"{n} {a}\u2013{b}" for n, a, b in eras))
            self.st_names.setVisible(True)
        starred = [str(r["season"]) for r in rows if r.get("computed")]
        self._st_finish(("* " + ", ".join(starred) + ": MLB recognizes the season, but MLB's API has no standings "
                         "for it; rank and games back are worked out from win-loss records.") if starred else "")
        self._fit_standings_table()

    # ---- sample scope -------------------------------------------------------
    def _team_identity(self, team_id, season: int, shown_name: str) -> tuple[str, int]:
        """-> (name, first year) of the team chosen in a tab's Team drop-down.  The 30 current
        clubs come from the bootstrap list; any other club (a Negro league team, a
        franchise that has since folded) from that season's team list."""
        team = self._team_by_id.get(team_id)
        if team:
            return team["name"], max(1876, team.get("first_year") or 1876)
        record = self._season_names.get(season, {}).get(team_id) or {}
        return record.get("name") or shown_name, max(1876, record.get("first_year") or 1876)

    def _sample_choice(self, scope: "_SampleScope", season_spin, team_combo, warn_title: str | None = None):
        """The checked Sample choice as ``(kind, team_id, team_name, season, first_year)`` for a
        job, or ``None`` if it needs a team and none is picked (after saying so, if ``warn_title``)."""
        kind, season = scope.checked(), season_spin.value()
        if kind == "league":
            return kind, None, None, season, None
        team_id = team_combo.currentData()
        if team_id is None:
            if warn_title:
                QMessageBox.warning(self, warn_title, "Pick a team first.")
            return None
        name, first_year = self._team_identity(team_id, season, team_combo.currentText())
        if kind == "history":
            return kind, team_id, name, None, first_year
        return kind, team_id, name, season, None  # "roster", "same_league", "same_division"

    # ---- gameday tab ----------------------------------------------------------------
    GD_PLAY_HEADERS = ["Play", "Batter", "Pitcher", "Count", "Outs", "Score", "Description"]
    GD_PITCH_COLUMNS = [("#", "n"), ("Count", "count"), ("Result", "call"), ("Pitch", "pitch"),
                        ("MPH", "mph"), ("Spin\n(rpm)", "spin"), ("Vertical Break\n(induced, in)", "ivb"),
                        ("Horizontal\nBreak (in)", "hb"), ("Location", "zone")]
    GD_PITCH_TIPS = {
        "Count": "Balls-strikes after the pitch",
        "Spin\n(rpm)": "Spin rate, revolutions per minute",
        "Vertical Break\n(induced, in)":
            "Induced vertical break: how many inches the pitch's spin moves it up (or down) compared "
            "with a spinless ball, with gravity left out. A four-seam fastball's backspin holds it up "
            "(about 15-18 in \"up\"); a curveball's topspin pulls it down.",
        "Horizontal\nBreak (in)":
            "Horizontal break: how many inches the spin moves the pitch sideways compared with a "
            "spinless ball. Arm side = toward the pitcher's throwing-arm side (a right-hander's "
            "fastball, sinker or changeup); glove side = the other way (sliders, sweepers).",
        "Location": "Where the pitch crossed the plate, in the batter's terms: up/down, and in "
                    "(toward him) or away. \"Out:\" means outside the strike zone. MLB's Gameday "
                    "zone number is in the cell's tooltip.",
    }
    GD_FIELDERS = {"1": "the pitcher", "2": "the catcher", "3": "first base", "4": "second base",
                   "5": "third base", "6": "shortstop", "7": "left field", "8": "center field",
                   "9": "right field"}
    GD_KEYS = ("gd", "box", "wpa", "sc")  # the tabs showing the Gameday game
    GD_REFRESH_MS = 20000  # a game in progress is re-pulled this often while the tab is on screen

    def _build_gameday_tab(self) -> QWidget:
        """One game's play-by-play from MLB's game feed (what MLB's Gameday draws from): the line
        score, every plate appearance grouped by half-inning, and -- for the play picked -- its
        pitches, batted ball and runners.  Team and Season list the team's games (regular season
        and postseason); how much each play carries depends on the era (see the note at the
        bottom)."""
        self._gd_games: list[dict] = []
        self._gd_game: dict | None = None
        self._gd_started = False  # the tab loads on first view, not at startup

        self.gd_team_combo = QComboBox()
        self.gd_team_combo.setMinimumWidth(240)
        self.gd_team_combo.currentIndexChanged.connect(lambda _i: self._gd_load_games())
        self.gd_season_spin = _YearSpin()
        self.gd_season_spin.setRange(1876, 2100)
        self.gd_season_spin.setKeyboardTracking(False)
        self.gd_season_spin.setToolTip("Typing a year needs Enter (or click elsewhere) to take effect.")
        self._gd_debounce = QTimer(self, singleShot=True, interval=250)
        self._gd_debounce.timeout.connect(self._gd_season_settled)
        self.gd_season_spin.valueChanged.connect(lambda _: self._gd_debounce.start())
        season_prev, season_next = QPushButton("◀"), QPushButton("▶")
        for btn, step in ((season_prev, -1), (season_next, 1)):
            btn.setFixedWidth(32)
            btn.clicked.connect(lambda _c, d=step: self.gd_season_spin.stepBy(d))

        self.gd_game_combo = QComboBox()
        self.gd_game_combo.setMinimumWidth(320)
        self.gd_game_combo.setMaxVisibleItems(25)
        self.gd_game_combo.currentIndexChanged.connect(lambda _i: self._gd_load_game())
        game_prev, game_next = QPushButton("◀"), QPushButton("▶")
        game_prev.setToolTip("Previous game")
        game_next.setToolTip("Next game")
        for btn, step in ((game_prev, -1), (game_next, 1)):
            btn.setFixedWidth(32)
            btn.clicked.connect(lambda _c, d=step: self._gd_step_game(d))
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Re-pull this team's schedule and the game on screen, ignoring the cache.")
        refresh.clicked.connect(self._gd_refresh)
        self.gd_scoring_only = QCheckBox("Scoring plays only")
        self.gd_scoring_only.toggled.connect(lambda _on: self._gd_apply_filter())
        self.gd_export_btn = QPushButton("Export…")
        self.gd_export_btn.setToolTip("Save the plays, pitches and line score as an .xlsx workbook, "
                                      "or the plays alone as CSV.")
        self.gd_export_btn.setEnabled(False)
        self.gd_export_btn.clicked.connect(self._gd_export)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Team"))
        bar.addWidget(self.gd_team_combo)
        bar.addSpacing(6)
        bar.addWidget(QLabel("Season"))
        bar.addWidget(season_prev)
        bar.addWidget(self.gd_season_spin)
        bar.addWidget(season_next)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Game"))
        bar.addWidget(game_prev)
        bar.addWidget(self.gd_game_combo)
        bar.addWidget(game_next)
        bar.addStretch(1)

        self.gd_title = QLabel()
        bold = self.font()
        bold.setBold(True)
        bold.setPointSizeF(bold.pointSizeF() * 1.15)
        self.gd_title.setFont(bold)
        self.gd_title.setWordWrap(True)
        self.gd_info = QLabel()
        self.gd_info.setWordWrap(True)

        self.gd_line = QTableWidget()
        self.gd_line.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.gd_line.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.gd_line.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.gd_line.verticalHeader().setVisible(False)
        self.gd_line.horizontalHeader().setHighlightSections(False)
        self.gd_line.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.gd_line.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.gd_line.setVisible(False)

        self.gd_message = QLabel()
        self.gd_message.setWordWrap(True)
        self.gd_message.setVisible(False)

        self.gd_plays = QTreeWidget()
        self.gd_plays.setColumnCount(len(self.GD_PLAY_HEADERS))
        self.gd_plays.setHeaderLabels(self.GD_PLAY_HEADERS)
        self.gd_plays.setRootIsDecorated(True)
        self.gd_plays.setUniformRowHeights(True)
        self.gd_plays.setAlternatingRowColors(True)
        header = self.gd_plays.header()
        header.setStretchLastSection(True)
        for col in range(len(self.GD_PLAY_HEADERS) - 1):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.gd_plays.currentItemChanged.connect(lambda cur, _prev: self._gd_show_play(cur))

        self.gd_detail = QLabel()
        self.gd_detail.setWordWrap(True)
        self.gd_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.gd_detail.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.gd_pitches = _FitTable(self._gd_fit_pitch_columns)
        self.gd_pitches.setColumnCount(len(self.GD_PITCH_COLUMNS))
        self.gd_pitches.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.gd_pitches.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.gd_pitches.verticalHeader().setVisible(False)
        self.gd_pitches.horizontalHeader().setHighlightSections(False)
        self.gd_pitches.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.gd_pitches.horizontalHeader().setStretchLastSection(False)
        self.gd_pitches.setTextElideMode(Qt.TextElideMode.ElideRight)
        for col, (title, _key) in enumerate(self.GD_PITCH_COLUMNS):
            item = QTableWidgetItem(title)
            if title in self.GD_PITCH_TIPS:
                item.setToolTip(self.GD_PITCH_TIPS[title])
            self.gd_pitches.setHorizontalHeaderItem(col, item)
        detail = QWidget()
        detail_layout = QHBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(self.gd_detail)
        detail_layout.addWidget(detail_scroll, 1)
        detail_layout.addWidget(self.gd_pitches, 2)

        self.gd_split = QSplitter(Qt.Orientation.Vertical)
        self.gd_split.addWidget(self.gd_plays)
        self.gd_split.addWidget(detail)
        # the play list scrolls, so the at-bat below gets the larger share of the height
        self.gd_split.setStretchFactor(0, 1)
        self.gd_split.setStretchFactor(1, 2)
        self.gd_split.setSizes([150, 300])
        for table, extra in ((self.gd_line, 8), (self.gd_pitches, 3)):  # compact pitch rows: more of them in view
            table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + extra)
            table.verticalHeader().setMinimumSectionSize(self.fontMetrics().height() + extra)

        note = QLabel(
            "Plays come from MLB's game feed, and how much it holds depends on the era: nothing before "
            "about 1950 (just the line score and decisions); one line per plate appearance from then, "
            "with the ball-strike sequence patchy until the late 1980s; each pitch's speed from 2008; "
            "spin, break and batted-ball exit velocity, launch angle and distance from 2015. "
            "A game in progress updates itself every 20 seconds while this tab is on screen.")
        note.setWordWrap(True)
        small = note.font()
        small.setPointSizeF(small.pointSizeF() * 0.9)
        note.setFont(small)
        note.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        # the buttons and the note sit to the right of the line score, no wider than the row of
        # buttons (the note wraps tall rather than wide, leaving the width to the diamond)
        buttons = QHBoxLayout()
        buttons.addWidget(refresh)
        buttons.addSpacing(8)
        buttons.addWidget(self.gd_scoring_only)
        buttons.addSpacing(8)
        buttons.addWidget(self.gd_export_btn)
        buttons.addStretch(1)
        beside_box = QWidget()
        beside = QVBoxLayout(beside_box)
        beside.setContentsMargins(0, 0, 0, 0)
        beside.addLayout(buttons)
        beside.addWidget(note, 1)
        beside_box.setFixedWidth(buttons.sizeHint().width())
        # the title, date line and line score in one column; the buttons and the note beside
        # them from the title down, so the note has the height of all three to wrap in
        game_col = QVBoxLayout()
        game_col.addWidget(self.gd_title)
        game_col.addWidget(self.gd_info)
        game_col.addWidget(self.gd_line, 0, Qt.AlignmentFlag.AlignTop)
        game_col.addStretch(1)
        # the diamond in the top right corner, beside the note (below the game picker, so the
        # picker and the diamond needn't fit side by side and the window stays as narrow as before)
        self.gd_diamond = BaseDiamond()
        self.gd_diamond.clicked.connect(self._gd_diamond_clicked)
        line_row = QHBoxLayout()
        line_row.addLayout(game_col, 0)
        line_row.addSpacing(12)
        line_row.addWidget(beside_box, 0)
        line_row.addWidget(self.gd_diamond, 1, Qt.AlignmentFlag.AlignTop)  # it takes the spare width

        self._gd_timer = QTimer(self, interval=self.GD_REFRESH_MS)
        self._gd_timer.timeout.connect(self._gd_tick)

        header = QVBoxLayout()
        header.addLayout(bar)
        header.addLayout(line_row)

        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addLayout(header)
        outer.addWidget(self.gd_message)
        outer.addWidget(self.gd_split, 1)
        self._gd_clear_game()
        # The game's header stays put; the play list and the pitch table scroll on their own.  The
        # buttons sit beside the line score so the page is narrower than the app's other tabs.
        self.gd_plays.setMinimumHeight(110)
        detail.setMinimumHeight(90)
        return page

    def _gd_setup(self):
        """At bootstrap: the season and team lists, starting on the Stats tab's team this season.
        Nothing is fetched until the tab is first opened."""
        self.gd_season_spin.setRange(1876, self._season_now)
        self.gd_season_spin.blockSignals(True)
        self.gd_season_spin.setValue(self._season_now)
        self.gd_season_spin.blockSignals(False)
        self.gd_team_combo.blockSignals(True)
        self.gd_team_combo.clear()
        for t in self._teams:
            self.gd_team_combo.addItem(t["name"], t["id"])
        index = self.gd_team_combo.findData(self.team_combo.currentData())
        self.gd_team_combo.setCurrentIndex(max(index, 0))
        self.gd_team_combo.blockSignals(False)
        if self._current_tab_key() == "gd":
            self._gd_tab_shown()

    def _gd_tab_shown(self):
        """Gameday, Boxscore, WPA or Statcast came on screen: they share one game, loaded on first view
        (and re-pulled straight away when it's live)."""
        if self.gd_team_combo.count() and not self._gd_started:
            self._gd_started = True
            self._gd_load_games()
        elif self._gd_game and self._gd_game["state"] == "live":
            self._gd_load_game()
        elif self._current_tab_key() == "wpa" and self._wpa_stale and self._gd_game:
            self._wpa_load()

    def _gd_season_settled(self):
        """The season changed: re-list the teams as that season had them, then its games."""
        season = self.gd_season_spin.value()
        cached = self._season_names.get(season)
        if cached is not None:
            self._gd_apply_names(cached)
            return
        ttl = LIVE_TTL if season >= self._season_now else None
        self._run_latest("gd_teams", lambda: (season, mlbstats.get_team_map(season, ttl)),
                         self._on_gd_teams_loaded, lambda m: self._on_team_refresh_failed("gd", m))

    def _on_gd_teams_loaded(self, data):
        season, names = data
        self._season_names[season] = names
        if season == self.gd_season_spin.value():
            self._gd_apply_names(names)

    def _gd_apply_names(self, names: dict):
        """List the teams of the chosen season (keeping the same franchise when it played then),
        and load the chosen team's games."""
        if names:
            keep_id = self.gd_team_combo.currentData()
            self.gd_team_combo.blockSignals(True)
            self.gd_team_combo.clear()
            target = 0
            for i, team in enumerate(sorted(names.values(), key=lambda t: t["name"])):
                self.gd_team_combo.addItem(team["name"], team["id"])
                if team["id"] == keep_id:
                    target = i
            self.gd_team_combo.setCurrentIndex(target)
            self.gd_team_combo.blockSignals(False)
        self._gd_load_games()

    def _gd_refresh(self):
        self._gd_load_games(force=True)

    def _gd_load_games(self, force: bool = False):
        team_id = self.gd_team_combo.currentData()
        if team_id is None or not self._gd_started:
            return
        season = self.gd_season_spin.value()
        ttl = 0 if force else (LIVE_TTL if season >= self._season_now else None)
        self._run_latest("gd_games",
                         lambda: (team_id, season, force, mlbstats.get_team_games(team_id, season, ttl)),
                         self._on_gd_games, self._on_gd_failed)

    @staticmethod
    def _gd_game_label(g: dict) -> str:
        """ "Oct 25  WS 1  @ Los Angeles Dodgers  L 3-6" """
        parts = [_short_date(g["date"])]
        if g["round"]:
            abbrev = {"Wild Card": "WC", "Division Series": "DS", "LCS": "LCS", "World Series": "WS"}[g["round"]]
            parts.append(f"{abbrev} {g['series_game']}" if g.get("series_game") else abbrev)
        parts.append(("vs " if g["home"] else "@ ") + g["opponent"]
                     + (f" (game {g['game_number']})" if g["double_header"] else ""))
        if g["state"] == "final" and g["runs"] is not None:
            parts.append(f"{g['result']} {g['runs']}–{g['opp_runs']}".strip())
        elif g["state"] == "live":
            score = f" {g['runs']}–{g['opp_runs']}" if g["runs"] is not None else ""
            parts.append(f"● live{score}")
        elif g["detailed"] and g["detailed"] not in ("Scheduled", "Pre-Game", "Warmup"):
            parts.append(g["detailed"].lower())
        return "   ".join(parts)

    def _on_gd_games(self, data):
        team_id, season, force, games = data
        if (team_id, season) != (self.gd_team_combo.currentData(), self.gd_season_spin.value()):
            return
        keep = self.gd_game_combo.currentData()
        self._gd_games = games
        self.gd_game_combo.blockSignals(True)
        self.gd_game_combo.clear()
        for g in games:
            self.gd_game_combo.addItem(self._gd_game_label(g), g["pk"])
        index = self.gd_game_combo.findData(keep) if keep is not None else -1
        if index < 0:  # the latest game played (or under way), else the first on the schedule
            played = [i for i, g in enumerate(games) if g["state"] != "scheduled"]
            index = played[-1] if played else 0
        self.gd_game_combo.setCurrentIndex(index if games else -1)
        self.gd_game_combo.blockSignals(False)
        if not games:
            self._gd_clear_game(f"{self.gd_team_combo.currentText()} have no games listed for {season}.")
            self._say("gd", f"Gameday: no {season} games for {self.gd_team_combo.currentText()}")
            return
        self._gd_load_game(force=force)

    def _gd_step_game(self, step: int):
        index = self.gd_game_combo.currentIndex() + step
        if 0 <= index < self.gd_game_combo.count():
            self.gd_game_combo.setCurrentIndex(index)

    def _gd_load_game(self, force: bool = False):
        pk = self.gd_game_combo.currentData()
        if pk is None:
            return
        self._run_latest("gd_game", lambda: mlbstats.get_gameday(pk, force), self._on_gd_game, self._on_gd_failed)

    def _gd_tick(self):
        """The live-game timer: re-pull only while the tab is on screen and the game is live."""
        if self._current_tab_key() in self.GD_KEYS and self._gd_game and self._gd_game["state"] == "live":
            self._gd_load_game()
        elif not (self._gd_game and self._gd_game["state"] == "live"):
            self._gd_timer.stop()

    def _on_gd_failed(self, message: str):
        self._say("gd", f"Gameday: {message}")
        if self._gd_game is None:
            self._gd_clear_game(f"Couldn't load the game: {message}")

    def _gd_clear_game(self, message: str = ""):
        self._gd_game = None
        self._gd_timer.stop()
        self.gd_title.setText("")
        self.gd_info.setText("")
        self.gd_line.setVisible(False)
        self.gd_plays.clear()
        self.gd_export_btn.setEnabled(False)
        self._gd_show_play(None)
        self.gd_message.setText(message)
        self.gd_message.setVisible(bool(message))
        if getattr(self, "box_view", None) is not None:  # (Gameday is built before its two companions)
            self._box_clear(message)
            self._wpa_clear(message)
            self._sc_clear(message)

    def _on_gd_game(self, game: dict):
        if game["pk"] != self.gd_game_combo.currentData():
            return
        same_game = self._gd_game is not None and self._gd_game["pk"] == game["pk"]
        selected = self._gd_selected_play() if same_game else None
        scroll = self.gd_plays.verticalScrollBar().value() if same_game else 0
        self._gd_game = game
        self.gd_diamond.set_surnames(game.get("surnames"))
        away, home = game["away"], game["home"]

        # the game list's label follows the feed (a live score, a game just finished)
        index = self.gd_game_combo.currentIndex()
        if 0 <= index < len(self._gd_games):
            g = self._gd_games[index]
            us, them = (home, away) if g["home"] else (away, home)
            g.update(state=game["state"], detailed=game["detailed"], runs=us["runs"], opp_runs=them["runs"])
            if game["state"] == "final" and us["runs"] is not None and them["runs"] is not None:
                g["result"] = "W" if us["runs"] > them["runs"] else "L" if us["runs"] < them["runs"] else "T"
            self.gd_game_combo.setItemText(index, self._gd_game_label(g))

        # title and info lines
        state = ("Final" if game["detailed"] in ("Final", "Game Over") else game["detailed"]) \
            if game["state"] == "final" else (game["current"] or "In progress") if game["state"] == "live" \
            else game["detailed"] or "Scheduled"
        extra = len(game["innings"]) - game["scheduled_innings"]
        if game["state"] == "final" and extra:
            state += f" ({len(game['innings'])} innings)"
        if away["runs"] is not None and game["state"] != "scheduled":
            self.gd_title.setText(f"{away['name']} {away['runs']} @ {home['name']} {home['runs']}   — {state}")
        else:
            self.gd_title.setText(f"{away['name']} @ {home['name']}   — {state}")
        info = []
        try:
            info.append(_dt.date.fromisoformat(game["date"]).strftime("%A, %B %-d, %Y"))
        except ValueError:
            info.append(game["date"])
        g = self._gd_games[index] if 0 <= index < len(self._gd_games) else {}
        if game["round"]:
            info.append(f"{game['round']}, game {g['series_game']}" if g.get("series_game") else game["round"])
        if game["venue"]:
            info.append(game["venue"])
        if game["decisions"]:
            info.append("   ".join(f"<b>{k}</b>: {v}" for k, v in game["decisions"].items()))
        self.gd_info.setText(" · ".join(info))

        self._gd_fill_linescore(game)
        self._gd_fill_plays(game, selected)
        self.gd_plays.verticalScrollBar().setValue(scroll)
        self.gd_export_btn.setEnabled(bool(game["plays"]))

        if game["state"] == "live":
            if not self._gd_timer.isActive():
                self._gd_timer.start()
        else:
            self._gd_timer.stop()

        pitches = sum(1 for p in game["plays"] for q in p["pitches"] if q["n"] is not None)
        what = []
        if game["has_speed"]:
            what.append("pitch speeds")
        if game["has_statcast"]:
            what.append("Statcast batted balls")
        status = (f"Gameday: {away['abbr']} @ {home['abbr']}, {_short_date(game['date'], True)} — "
                  f"{len(game['plays'])} plays")
        if game["sequences"] != "none":
            status += f", {pitches} pitches" + (" (sequences for only some plate appearances)"
                                                if game["sequences"] == "partial" else "")
        if what:
            status += " (" + ", ".join(what) + ")"
        if not game["plays"] and game["state"] == "final":
            status += " (no play-by-play in MLB's API for this game)"
        self._say("gd", status)
        self._box_fill(game, same_game)
        self._wpa_game_changed(game, same_game)
        self._sc_game_changed(game, same_game)

    def _gd_fill_linescore(self, game: dict):
        innings = game["innings"]
        count = max(len(innings), game["scheduled_innings"] if innings or game["state"] != "scheduled" else 0)
        if not count and game["away"]["runs"] is None:
            self.gd_line.setVisible(False)
            return
        headers = [""] + [str(i) for i in range(1, count + 1)] + ["R", "H", "E"]
        self.gd_line.clear()
        self.gd_line.setColumnCount(len(headers))
        self.gd_line.setRowCount(2)
        self.gd_line.setHorizontalHeaderLabels(headers)
        bold = self.gd_line.font()
        bold.setBold(True)
        for row, side in enumerate(("away", "home")):
            team = game[side]
            name = QTableWidgetItem(team["name"])
            self.gd_line.setItem(row, 0, name)
            for col in range(count):
                runs = innings[col][side]["runs"] if col < len(innings) else None
                text = "" if runs is None else str(runs)
                if (runs is None and side == "home" and game["state"] == "final" and col == len(innings) - 1):
                    text = "X"  # the home team didn't need its last turn at bat
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.gd_line.setItem(row, col + 1, item)
            for offset, key in enumerate(("runs", "hits", "errors")):
                value = team[key]
                item = QTableWidgetItem("" if value is None else str(value))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setFont(bold)
                self.gd_line.setItem(row, count + 1 + offset, item)
        self.gd_line.resizeColumnsToContents()
        height = self.gd_line.horizontalHeader().height() + sum(self.gd_line.rowHeight(r) for r in range(2))
        self.gd_line.setFixedHeight(height + 2 * self.gd_line.frameWidth())
        width = sum(self.gd_line.columnWidth(c) for c in range(self.gd_line.columnCount()))
        self.gd_line.setFixedWidth(width + 2 * self.gd_line.frameWidth())
        self.gd_line.setVisible(True)

    def _gd_selected_play(self):
        item = self.gd_plays.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        return data if isinstance(data, (int, tuple)) else None

    def _gd_fill_plays(self, game: dict, selected):
        self.gd_plays.blockSignals(True)
        self.gd_plays.clear()
        plays = game["plays"]
        if not plays:
            self.gd_plays.blockSignals(False)
            if game["state"] == "scheduled":
                msg = "This game hasn't started yet."
            elif game["state"] == "live":
                msg = "No plays yet."
            else:
                msg = ("MLB's API has no play-by-play for this game — only the line score and decisions "
                       "above.  Its play-by-play begins in about 1950.")
            self.gd_message.setText(msg)
            self.gd_message.setVisible(True)
            self.gd_split.setVisible(False)
            self._gd_show_play(None)
            return
        self.gd_message.setVisible(False)
        self.gd_split.setVisible(True)
        bold = self.gd_plays.font()
        bold.setBold(True)
        italic = self.gd_plays.font()
        italic.setItalic(True)
        scoring_brush = QBrush(QColor(WON_COLOR))
        grey = QBrush(QColor("#888888"))
        innings = {inn["num"]: inn for inn in game["innings"]}
        half_item, half_key, target = None, None, None
        for play in plays:
            key = (play["inning"], play["half"])
            if key != half_key:
                half_key = key
                side = "away" if play["half"] == "top" else "home"
                ordinal = mlbstats.ordinal(play["inning"]) if play["inning"] else "?"
                half_item = QTreeWidgetItem([f"{play['half'].title()} {ordinal}", game[side]["name"] + " batting"])
                half_item.setFont(0, bold)
                half_item.setFont(1, bold)
                line = (innings.get(play["inning"]) or {}).get(side) or {}
                if line.get("runs") is not None:
                    bits = [f"{line['runs']} R", f"{line.get('hits', 0) or 0} H", f"{line.get('errors', 0) or 0} E"]
                    if line.get("leftOnBase") is not None:
                        bits.append(f"{line['leftOnBase']} LOB")
                    half_item.setText(6, ", ".join(bits))
                half_item.setFirstColumnSpanned(False)
                half_item.setData(0, Qt.ItemDataRole.UserRole, ("half", play["index"]))
                self.gd_plays.addTopLevelItem(half_item)
                if half_item.data(0, Qt.ItemDataRole.UserRole) == selected:
                    target = half_item
            for n, (_before, text) in enumerate(play["notes"]):
                note = QTreeWidgetItem(["", "", "", "", "", "", text])
                for col in range(len(self.GD_PLAY_HEADERS)):
                    note.setForeground(col, grey)
                note.setData(0, Qt.ItemDataRole.UserRole, ("note", play["index"], n))
                half_item.addChild(note)
                if note.data(0, Qt.ItemDataRole.UserRole) == selected:
                    target = note
            score = ""
            if play["away_score"] is not None:
                score = f"{play['away_score']}–{play['home_score']}"
            event = play["event"] or ("(in progress)" if not play["complete"] else "")
            item = QTreeWidgetItem([event, play["batter"], play["pitcher"],
                                    play["count"] if mlbstats.play_has_sequence(game, play) else "",
                                    "" if play["outs"] is None else str(play["outs"]), score, play["description"]])
            item.setData(0, Qt.ItemDataRole.UserRole, play["index"])
            item.setToolTip(6, play["description"])
            if play["scoring"]:
                for col in range(len(self.GD_PLAY_HEADERS)):
                    item.setFont(col, bold)
                item.setForeground(5, scoring_brush)
            if not play["complete"]:
                for col in range(len(self.GD_PLAY_HEADERS)):
                    item.setFont(col, italic)
            half_item.addChild(item)
            if play["index"] == selected:
                target = item
        self.gd_plays.expandAll()
        self.gd_plays.blockSignals(False)
        self._gd_apply_filter()
        if target is not None and not target.isHidden():
            self.gd_plays.setCurrentItem(target)
        else:
            self._gd_show_play(None)

    def _gd_apply_filter(self):
        """Scoring plays only: hide every other play (and the notes), and half-innings with none."""
        only = self.gd_scoring_only.isChecked()
        by_index = {p["index"]: p for p in (self._gd_game or {}).get("plays", [])}
        for i in range(self.gd_plays.topLevelItemCount()):
            half = self.gd_plays.topLevelItem(i)
            shown = 0
            for j in range(half.childCount()):
                child = half.child(j)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                visible = not only or (isinstance(data, int) and by_index.get(data, {}).get("scoring"))
                child.setHidden(not visible)
                shown += bool(visible)
            half.setHidden(only and not shown)

    @staticmethod
    def _gd_fmt(key: str, value) -> str:
        if value is None or value == "":
            return ""
        if key == "mph":
            return f"{value:.1f}"
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    def _gd_show_play(self, item):
        """The detail pane for the play picked: what happened, the batted ball, the runners, and
        its pitches (with anything that happened between them)."""
        data = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        self._gd_show_bases(data)
        if isinstance(data, tuple):  # a note row: show the play it belongs to
            data = None if data[0] == "half" else data[1]
        game = self._gd_game
        play = next((p for p in game["plays"] if p["index"] == data), None) if game and isinstance(data, int) else None
        if play is not None and getattr(self, "sc_view", None) is not None:
            self._sc_follow_gameday(play["index"])
        self.gd_pitches.setRowCount(0)
        if play is None:
            self.gd_detail.setText("<i>Pick a play to see its pitches, batted ball and runners.</i>"
                                   if game and game["plays"] else "")
            self.gd_pitches.setVisible(bool(game and game["sequences"] != "none"))
            return

        def esc(text):
            return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        hands = {"L": "left", "R": "right", "S": "both sides"}
        lines = [f"<b>{esc(play['description'] or play['event'] or 'In progress')}</b>"]
        batter = esc(play["batter"]) + (f" (bats {hands[play['bat_side']]})" if play["bat_side"] in hands else "")
        pitcher = esc(play["pitcher"]) + (f" (throws {hands[play['pitch_hand']]})" if play["pitch_hand"] in hands else "")
        lines.append(f"{batter} vs {pitcher}")
        hit = play["hit"]
        if hit:
            bits = []
            if hit["ev"] is not None:
                bits.append(f"{hit['ev']:.1f} mph off the bat")
            if hit["la"] is not None:
                bits.append(f"{hit['la']:.0f}° launch angle")
            if hit["dist"] is not None:
                bits.append(f"{hit['dist']:.0f} ft")
            if hit["trajectory"]:
                bits.append(hit["trajectory"])
            if hit["hardness"] and hit["ev"] is None:
                bits.append(f"hit {hit['hardness']}")
            if hit["location"] in self.GD_FIELDERS:
                bits.append(f"to {self.GD_FIELDERS[hit['location']]}")
            if bits:
                lines.append("<b>Batted ball:</b> " + ", ".join(bits))
        if play["runners"]:
            moves = []
            for r in play["runners"]:
                who = esc(r["name"]) + (" (batter)" if r["batter"] else "")
                if r["out"]:
                    what = f"out at {r['out_base']}" if r["out_base"] else "out"
                elif r["scored"]:
                    tags = [t for t, on in (("earned", r["earned"]), ("RBI", r["rbi"])) if on]
                    what = "scores" + (f" ({', '.join(tags)})" if tags else "")
                elif r["batter"]:
                    what = f"to {r['end']}"
                else:
                    what = f"{r['start']} → {r['end']}"
                if r["event"] and r["event"] != play["event"]:
                    what += f" — {esc(r['event'])}"
                moves.append(f"{who}: {what}")
            lines.append("<b>Runners:</b> " + "; ".join(moves))
        if play["rbi"]:
            lines.append(f"<b>RBI:</b> {play['rbi']}")
        known = mlbstats.play_has_sequence(game, play)
        if game["sequences"] == "none":
            lines.append("<i>MLB's API has no pitch-by-pitch record for this game.</i>")
        elif not known:
            lines.append("<i>MLB's API has no pitch sequence for this plate appearance (it has one for only "
                         "some of this game's).</i>")
        self.gd_detail.setText("<br>".join(lines))

        self.gd_pitches.setVisible(game["sequences"] != "none")
        if game["sequences"] == "none":
            return
        # hide the columns this game has nothing for (no speeds before 2008, no spin before 2015)
        for col, (_title, key) in enumerate(self.GD_PITCH_COLUMNS):
            if key in ("mph", "spin", "ivb", "hb", "zone"):
                has = any(q[key] is not None for p in game["plays"] for q in p["pitches"])
                self.gd_pitches.setColumnHidden(col, not has)
        rows: list = []
        notes = sorted(play["notes"])
        for i, pitch in enumerate(play["pitches"] if known else []):
            rows += [("note", text) for before, text in notes if before == i]
            rows.append(("pitch", pitch))
        rows += [("note", text) for before, text in notes if before >= (len(play["pitches"]) if known else 0)]
        self.gd_pitches.clearSpans()
        self.gd_pitches.setRowCount(len(rows))
        grey = QBrush(QColor("#888888"))
        ball_brush, strike_brush, play_brush = (QBrush(QColor(c)) for c in (WON_COLOR, LOST_COLOR, "#3b7dd8"))
        for row, (kind, value) in enumerate(rows):
            if kind == "note":
                item = QTableWidgetItem(value)
                item.setForeground(grey)
                item.setToolTip(value)
                self.gd_pitches.setItem(row, 0, item)
                self.gd_pitches.setSpan(row, 0, 1, len(self.GD_PITCH_COLUMNS))
                continue
            for col, (_title, key) in enumerate(self.GD_PITCH_COLUMNS):
                if key == "zone":
                    item = QTableWidgetItem(mlbstats.zone_location(value[key], play["bat_side"]))
                    if value[key] is not None:
                        item.setToolTip(f"Gameday zone {value[key]}")
                elif key == "ivb":
                    item = QTableWidgetItem(mlbstats.vertical_break(value[key]))
                elif key == "hb":
                    item = QTableWidgetItem(mlbstats.break_side(value[key], play["pitch_hand"]))
                else:
                    item = QTableWidgetItem(self._gd_fmt(key, value[key]))
                if key in ("n", "count", "mph", "spin"):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if key in self.GD_SHRINKABLE and not item.toolTip():
                    item.setToolTip(item.text())  # the full text, should the column be narrowed
                if key == "call":
                    item.setForeground(play_brush if value["in_play"] else ball_brush if value["ball"]
                                       else strike_brush if value["strike"] else grey)
                self.gd_pitches.setItem(row, col, item)
        self._gd_fit_pitch_columns()

    def _gd_show_bases(self, data):
        """The diamond: who was on after the play (or the note) picked, who was on as a
        half-inning began, or -- nothing picked, or the play under way in a live game -- now."""
        game = self._gd_game
        if not game or not game["plays"]:
            self.gd_diamond.setVisible(False)
            return
        self.gd_diamond.setVisible(True)
        by_index = {p["index"]: p for p in game["plays"]}
        kind, index = (data[0], data[1]) if isinstance(data, tuple) else ("play", data)
        play = by_index.get(index)
        live = game.get("live")
        latest = game["plays"][-1]
        if live and (play is None or (kind == "play" and play is latest and not play["complete"])):
            count = f"{live['balls']}-{live['strikes']}" if live["balls"] is not None else ""
            caption = " · ".join(t for t in (game["current"] or "Now", "now", count) if t)
            self.gd_diamond.set_state(live["bases"], live["outs"], caption,
                                      matchup=(live["batter"], live.get("pitcher", "")))
            return
        if play is None:  # a finished game, nothing picked
            self.gd_diamond.set_state(caption="Pick a play to see the bases")
            return
        when = f"{play['half'].title()} {mlbstats.ordinal(play['inning']) if play['inning'] else '?'}"
        hint = "Click for the game as it stands now." if live else ""

        def score(pair):
            if not pair or pair[0] is None or pair[1] is None:
                return ""
            return f"{game['away']['abbr']} {pair[0]}\u2013{pair[1]} {game['home']['abbr']}"
        if kind == "half":
            self.gd_diamond.set_state(play["bases_before"], 0, f"{when} · leading off", hint,
                                      matchup=(play["batter"], play["pitcher"]))
        elif kind == "note":
            n = data[2]
            text = play["notes"][n][1] if n < len(play["notes"]) else ""
            # a change that isn't a play goes by its short name (in full in the tooltip):
            # "Defensive Substitution: Joe Blow replaces ..." -> "Defensive Substitution", "Joe Blow
            # remains in the game as the designated hitter." -> "Defensive Switch"; a play
            # between pitches ("Ann steals 2nd.") is kept whole
            kind_of, colon, _rest = text.partition(":")
            short = kind_of.strip() if colon else play["note_labels"][n]
            if short:
                hint = "\n".join(t for t in (text, hint) if t)
                text = short
            self.gd_diamond.set_state(play["note_bases"][n], play["note_outs"][n],
                                      f"{when} · after: {text}", hint, play["note_runs"][n],
                                      score=score(play["note_scores"][n]), matchup=(play["batter"], play["pitcher"]))
        else:
            what = play["event"] or "the play so far"
            self.gd_diamond.set_state(play["bases"], play["outs"], f"{when} · after: {what}", hint,
                                      play["play_runs"], play["batter"] if play["event"] == "Home Run" else "",
                                      score((play["away_score"], play["home_score"])),
                                      matchup=(play["batter"], play["pitcher"]))

    def _gd_diamond_clicked(self):
        """In a live game, clicking the diamond lets go of the play picked, back to now."""
        if self._gd_game and self._gd_game.get("live"):
            self.gd_plays.setCurrentItem(None)
            self.gd_plays.clearSelection()

    # the pitch table's text columns that give up width (in this order) when the table is too
    # narrow for every column at full size; their cells are elided, with the full text as a tooltip
    GD_SHRINKABLE = ("pitch", "call", "zone", "hb", "ivb")

    def _gd_fit_pitch_columns(self):
        """Size the pitch table's columns to the pitches (not to the notes, which span the whole
        row), then, if they are wider than the table, narrow the text columns so every column
        stays in view."""
        table = getattr(self, "gd_pitches", None)
        if table is None or table.columnCount() == 0:
            return
        metrics = table.fontMetrics()
        pad = 16
        widths = {}
        for col, (title, key) in enumerate(self.GD_PITCH_COLUMNS):
            if table.isColumnHidden(col):
                continue
            width = max(metrics.horizontalAdvance(line) for line in title.split("\n"))
            for row in range(table.rowCount()):
                if table.columnSpan(row, 0) > 1:
                    continue  # a note row
                item = table.item(row, col)
                if item is not None:
                    width = max(width, metrics.horizontalAdvance(item.text()))
            widths[col] = width + pad
        available = table.viewport().width()
        excess = sum(widths.values()) - available
        floors = {"pitch": metrics.horizontalAdvance("Four-Seam") + pad,
                  "call": metrics.horizontalAdvance("Called Str") + pad,
                  "zone": metrics.horizontalAdvance("Up & away") + pad,
                  "hb": metrics.horizontalAdvance("10.8 glove") + pad,
                  "ivb": metrics.horizontalAdvance("16.0 up") + pad}
        keys = {key: col for col, (_t, key) in enumerate(self.GD_PITCH_COLUMNS)}
        for key in self.GD_SHRINKABLE:
            if excess <= 0:
                break
            col = keys[key]
            if col not in widths:
                continue
            give = min(excess, max(0, widths[col] - floors[key]))
            widths[col] -= give
            excess -= give
        for col, width in widths.items():
            table.setColumnWidth(col, width)

    # ---- boxscore tab (follows the Gameday game) ----------------------------------------
    BOX_TIPS = {"AB": "At bats", "R": "Runs", "H": "Hits", "RBI": "Runs batted in", "BB": "Walks",
                "SO": "Strikeouts", "LOB": "Runners left on base", "AVG": "Batting average, season to date",
                "OPS": "On-base plus slugging, season to date", "IP": "Innings pitched", "ER": "Earned runs",
                "HR": "Home runs allowed", "NP": "Pitches thrown", "ERA": "Earned run average, season to date"}

    def _build_boxscore_tab(self) -> QWidget:
        """MLB's box score of the game picked on the Gameday tab: each side's batting and
        pitching lines with the notes under them, then the game notes.  It comes in the same
        feed as the play-by-play (no extra download), from 1901 on; a game in progress shows
        its box score so far and updates with Gameday."""
        self.box_view = QTextBrowser()
        self.box_view.setOpenLinks(False)
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(self.box_view, 1)
        self._box_clear("Pick a game on the Gameday tab.")
        return page

    def _box_clear(self, message: str = ""):
        self.box_view.setHtml(f"<p><i>{html.escape(message)}</i></p>" if message else "")

    def _box_team_html(self, t: dict, team_name: str, bat_cols, pit_cols, show_avg: bool, show_era: bool,
                       live: bool = False) -> str:
        """One side of a box score as the Boxscore tab draws it: its batters with the notes and info under them, then
        its pitchers (``t`` as ``mlbstats``'s box side; the Simulator's replayed game makes one the same way)."""
        esc = html.escape

        def num(v):
            return "" if v is None else esc(str(v))

        def head(first, cols):
            cells = "".join(f"<th align='right' title='{esc(self.BOX_TIPS.get(c, c))}'>&nbsp;{c}&nbsp;</th>"
                            for c in cols)
            return f"<tr style='background:#e6e6e6'><th align='left'>{esc(first)}</th>{cells}</tr>"

        def team_html(t, team_name):
            out = [f"<h4 style='margin-bottom:2px'>{esc(team_name)}</h4>",
                   "<table cellspacing='0' cellpadding='2' width='100%'>",
                   head("Batters", [c for c, _k in bat_cols] + (["AVG", "OPS"] if show_avg else []))]
            for i, b in enumerate(t["batters"]):
                mark = "▶ " if b["current"] and live else ""
                name = ("&nbsp;&nbsp;&nbsp;" if b["sub"] else "") + mark + esc(b["name"])
                name = f"<b>{name}</b>" if mark else name
                shade = " style='background:#f4f4f4'" if i % 2 else ""
                cells = "".join(f"<td align='right'>{num(b['stats'][k])}</td>" for _c, k in bat_cols)
                if show_avg:
                    cells += f"<td align='right'>{esc(b['avg'])}</td><td align='right'>{esc(b['ops'])}</td>"
                out.append(f"<tr{shade}><td>{name} <span style='color:#777'>{esc(b['pos'])}</span></td>{cells}</tr>")
            tot = "".join(f"<td align='right'><b>{num(t['batting_totals'][k])}</b></td>" for _c, k in bat_cols)
            out.append(f"<tr><td><b>Totals</b></td>{tot}{'<td></td><td></td>' if show_avg else ''}</tr></table>")
            if t["notes"]:
                out.append("<p style='margin:2px 0'>" + "<br>".join(esc(n) for n in t["notes"]) + "</p>")
            for title, fields in t["info"]:
                if fields:
                    out.append(f"<p style='margin:4px 0'><b>{esc(title.title())}</b><br>"
                               + "<br>".join(f"<b>{esc(l)}:</b> {esc(v)}" for l, v in fields) + "</p>")
            out.append("<table cellspacing='0' cellpadding='2' width='100%' style='margin-top:6px'>")
            out.append(head("Pitchers", [c for c, _k in pit_cols] + (["ERA"] if show_era else [])))
            for i, p in enumerate(t["pitchers"]):
                mark = "▶ " if p["current"] and live else ""
                name = mark + esc(p["name"]) + (f" <span style='color:#777'>{esc(p['note'])}</span>" if p["note"] else "")
                name = f"<b>{name}</b>" if mark else name
                shade = " style='background:#f4f4f4'" if i % 2 else ""
                cells = "".join(f"<td align='right'>{num(p['stats'][k]) if k != 'numberOfPitches' or p['stats'][k] else ''}</td>"
                                for _c, k in pit_cols)
                if show_era:
                    cells += f"<td align='right'>{esc(p['era'])}</td>"
                out.append(f"<tr{shade}><td>{name}</td>{cells}</tr>")
            tot = "".join(f"<td align='right'><b>{num(t['pitching_totals'][k])}</b></td>" for _c, k in pit_cols)
            out.append(f"<tr><td><b>Totals</b></td>{tot}{'<td></td>' if show_era else ''}</tr></table>")
            return "".join(out)

        return team_html(t, team_name)

    def _box_fill(self, game: dict, same_game: bool = False):
        box = game.get("box") or {}
        bar = self.box_view.verticalScrollBar()
        scroll = bar.value() if same_game else 0
        title, info = self.gd_title.text(), self.gd_info.text()  # the Gameday header, set just before
        esc = html.escape
        parts = [f"<h3 style='margin:0'>{esc(title)}</h3><p style='margin-top:2px'>{info}</p>"]
        if game["state"] == "live":
            parts.append(f"<p><b style='color:{LIVE_COLOR}'>● In progress — {esc(game['current'] or '')}.</b> "
                         "This is the box score so far; it updates with the Gameday tab every 20 seconds "
                         "while it's on screen. ▶ marks the batter up and the pitcher in the game.</p>")
        elif game["state"] == "scheduled":
            parts.append("<p><i>This game hasn't started"
                         + (" — the lineups below are as posted." if any(box.get(s, {}).get("batters")
                                                                           for s in ("away", "home"))
                            else "; the box score fills in once it does.") + "</i></p>")
        sides = [s for s in ("away", "home") if box.get(s)]
        if not any(box[s]["batters"] or box[s]["pitchers"] for s in sides):
            if game["state"] != "scheduled":
                parts.append("<p><i>MLB's API has no box score for this game.</i></p>")
            self.box_view.setHtml("".join(parts))
            return
        # columns MLB has nothing for in this game (left on base and pitch counts before play-by-play)
        bat_cols = [(lbl, key) for lbl, key in mlbstats.BOX_BATTING
                    if key != "leftOnBase" or any(box[s]["batting_totals"].get(key) for s in sides)]
        pit_cols = [(lbl, key) for lbl, key in mlbstats.BOX_PITCHING
                    if key != "numberOfPitches" or any(box[s]["pitching_totals"].get(key) for s in sides)]
        show_avg = any(b["avg"] for s in sides for b in box[s]["batters"])
        show_era = any(p["era"] for s in sides for p in box[s]["pitchers"])


        parts.append("<table width='100%' cellspacing='0' cellpadding='0'><tr>"
                     + "".join(f"<td width='50%' valign='top' style='padding:0 10px'>"
                               f"{self._box_team_html(box[s], game[s]['name'], bat_cols, pit_cols, show_avg, show_era, game['state'] == 'live')}"
                               "</td>" for s in sides)
                     + "</tr></table>")
        notes = [esc(n) for n in box.get("pitching_notes") or []]
        notes += [f"<b>{esc(l)}:</b> {esc(v)}" for l, v in box.get("info") or []]
        if notes:
            parts.append("<hr><p style='margin:4px 10px'>" + "<br>".join(notes) + "</p>")
        if game["state"] == "final" and game["date"][:4] < "1950":
            parts.append("<p style='margin:4px 10px; color:#777'><small>Box scores before play-by-play (about "
                         "1950) carry MLB's batting and pitching lines without left-on-base or pitch counts."
                         "</small></p>")
        self.box_view.setHtml("".join(parts))
        bar.setValue(scroll)
        b = box["away"]["batting_totals"], box["home"]["batting_totals"]
        self._say("box", f"Boxscore: {game['away']['abbr']} {b[0]['runs']} R {b[0]['hits']} H, "
                         f"{game['home']['abbr']} {b[1]['runs']} R {b[1]['hits']} H"
                         + (" — in progress" if game["state"] == "live" else ""))

    # ---- WPA tab (follows the Gameday game) ---------------------------------------------
    WPA_PLAY_HEADERS = ["Inning", "Team", "Batter", "Pitcher", "Play", "WPA", "Win % after"]
    WPA_PLAYER_HEADERS = ["Player", "Team", "Batting", "Pitching", "Total"]

    def _build_wpa_tab(self) -> QWidget:
        """Win Probability Added for the game picked on the Gameday tab: MLB's own win
        probability after every plate appearance, drawn as one team's chances through the game,
        with the biggest swings and each player's total below.  A game in progress is drawn as
        far as it has got, the innings still to come left empty."""
        self._wpa = None
        self._wpa_pk = None
        self._wpa_stale = True
        self.wpa_title = QLabel()
        bold = self.font()
        bold.setBold(True)
        bold.setPointSizeF(bold.pointSizeF() * 1.15)
        self.wpa_title.setFont(bold)
        self.wpa_title.setWordWrap(True)
        self.wpa_summary = QLabel()
        self.wpa_summary.setWordWrap(True)
        self.wpa_chart = wpachart.WPAChart()
        self.wpa_chart.picked.connect(self._wpa_go_to_play)

        def table(headers, tips):
            t = QTableWidget()
            t.setColumnCount(len(headers))
            t.setHorizontalHeaderLabels(headers)
            for col, tip in tips.items():
                t.horizontalHeaderItem(col).setToolTip(tip)
            t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            t.verticalHeader().setVisible(False)
            t.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 4)
            t.setAlternatingRowColors(True)
            t.setSortingEnabled(True)
            t.horizontalHeader().setHighlightSections(False)
            return t
        self.wpa_plays = table(self.WPA_PLAY_HEADERS, {
            5: "Win Probability Added: how much the play raised (+) or lowered the batting team's chance "
               "of winning, in percentage points",
            6: "The chance of winning, after the play, of the team that was ahead in it"})
        self.wpa_plays.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.wpa_plays.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.wpa_plays.cellDoubleClicked.connect(
            lambda row, _col: self._wpa_go_to_play(self.wpa_plays.item(row, 0).data(Qt.ItemDataRole.UserRole)))
        self.wpa_plays.setToolTip("Double-click a play to see it on the Gameday tab.")
        self.wpa_players = table(self.WPA_PLAYER_HEADERS, {
            2: "Sum of the WPA of his plate appearances, for his team",
            3: "Sum of the WPA of the plate appearances he pitched, for his team (a strikeout is + for "
               "the pitcher, - for the batter)",
            4: "Batting + pitching"})
        self.wpa_players.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.wpa_players.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        plays_box = QGroupBox("Every play, biggest swings first")
        QVBoxLayout(plays_box).addWidget(self.wpa_plays)
        players_box = QGroupBox("Players")
        QVBoxLayout(players_box).addWidget(self.wpa_players)
        lower = QSplitter(Qt.Orientation.Horizontal)
        lower.addWidget(plays_box)
        lower.addWidget(players_box)
        lower.setStretchFactor(0, 3)
        lower.setStretchFactor(1, 2)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.wpa_chart)
        split.addWidget(lower)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([300, 180])
        lower.setMinimumHeight(110)

        note = QLabel(
            "Win probabilities are MLB's own, the same figures its Gameday shows: after each plate "
            "appearance, the chance of each team winning from that score, inning, outs and runners on "
            "base. Win Probability Added (WPA) is how much one play moved it. MLB has them for games with "
            "play-by-play — from about 1950. Hover over the chart to see a play; click one to open it on "
            "the Gameday tab.")
        note.setWordWrap(True)
        small = note.font()
        small.setPointSizeF(small.pointSizeF() * 0.9)
        note.setFont(small)

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(self.wpa_title)
        lay.addWidget(self.wpa_summary)
        lay.addWidget(split, 1)
        lay.addWidget(note)
        self._wpa_clear("Pick a game on the Gameday tab.")
        return page

    def _wpa_clear(self, message: str = ""):
        self._wpa = None
        self._wpa_pk = None
        self.wpa_title.setText("")
        self.wpa_summary.setText("")
        self.wpa_chart.set_data(None, message=message)
        self.wpa_plays.setRowCount(0)
        self.wpa_players.setRowCount(0)

    def _wpa_game_changed(self, game: dict, same_game: bool):
        """The Gameday game was (re)loaded: pull its win probability now if the WPA tab is on
        screen, otherwise when it next is."""
        self._wpa_stale = True
        self.wpa_title.setText(self.gd_title.text())
        if not same_game:
            self._wpa_clear("Loading win probability…")
            self.wpa_title.setText(self.gd_title.text())
        if self._current_tab_key() == "wpa":
            self._wpa_load()

    def _wpa_load(self, force: bool = False):
        game = self._gd_game
        if game is None:
            return
        self._wpa_stale = False
        if game["state"] == "scheduled" or not game["plays"]:
            self._wpa_show(game, {"start": 50.0, "plays": []})
            return
        pk, final = game["pk"], game["state"] == "final"
        complete = sum(1 for p in game["plays"] if p["complete"])
        self._run_latest("wpa", lambda: (pk, mlbstats.get_win_probability(pk, final, complete, force)),
                         lambda data: self._on_wpa(*data),
                         lambda m: self._say("wpa", f"WPA: {m}"))

    def _on_wpa(self, pk, wp):
        if self._gd_game is None or self._gd_game["pk"] != pk:
            return
        self._wpa_show(self._gd_game, wp)

    @staticmethod
    def _half_key(inning, half) -> int:
        return (inning - 1) * 2 + (0 if half == "top" else 1)

    def _wpa_show(self, game: dict, wp: dict):
        self._wpa, self._wpa_pk = wp, game["pk"]
        away, home = game["away"], game["home"]
        self.wpa_title.setText(self.gd_title.text())
        plays = wp["plays"]
        if not plays:
            if game["state"] == "scheduled":
                msg = "This game hasn't started yet."
            elif game["state"] == "live":
                msg = "No plate appearance has finished yet."
            else:
                msg = ("MLB has no win probability for this game. Its figures are worked out play by play, "
                       "and MLB's API has play-by-play only from about 1950.")
            self.wpa_chart.set_data(None, message=msg)
            self.wpa_summary.setText("")
            self.wpa_plays.setRowCount(0)
            self.wpa_players.setRowCount(0)
            self._say("wpa", "WPA: " + msg)
            return
        live = game["state"] == "live"
        reached = max(self._half_key(p["inning"], p["half"]) for p in plays) + 1
        open_half = False
        if live and game["plays"]:
            now = game["plays"][-1]
            now_key = self._half_key(now["inning"] or 1, now["half"])
            reached = max(reached, now_key + 1)
            open_half = not now["complete"] and now_key == self._half_key(plays[-1]["inning"], plays[-1]["half"])
        halves = max(reached, game["scheduled_innings"] * 2) if live else reached
        biggest = sorted(plays, key=lambda p: -abs(p["added"]))[:3]
        self.wpa_chart.set_data(
            wp, away["name"], home["name"], halves, open_half,
            live_note=f"In progress — {game['current']}: still to be played" if live else "",
            marked=[p["index"] for p in biggest])

        last = plays[-1]
        leader, chance = (home["name"], last["home_wp"]) if last["home_wp"] >= 50 else (away["name"], 100 - last["home_wp"])
        if live:
            text = f"<b>Now</b> ({game['current']}): {leader} {chance:.1f}% to win. "
        else:
            text = ""
        top = biggest[0]
        team = home["abbr"] if top["half"] == "bottom" else away["abbr"]
        gain = top["added"] if top["half"] == "bottom" else -top["added"]
        text += (f"<b>Biggest play:</b> {html.escape(top['description'] or top['event'])} "
                 f"({team} {gain:+.1f}%, {top['half'].title()} {mlbstats.ordinal(top['inning'])}). "
                 f"Before the first pitch MLB had {home['name']} at {wp['start']:.1f}%.")
        self.wpa_summary.setText(text)

        # every play, biggest swings first
        self.wpa_plays.setSortingEnabled(False)
        self.wpa_plays.setRowCount(len(plays))
        for row, p in enumerate(sorted(plays, key=lambda p: -abs(p["added"]))):
            batting_home = p["half"] == "bottom"
            gain = p["added"] if batting_home else -p["added"]
            lead = max(p["home_wp"], 100 - p["home_wp"])
            who = home["abbr"] if (p["home_wp"] >= 50) else away["abbr"]
            cells = [f"{'Top' if p['half'] == 'top' else 'Bot'} {p['inning']}",
                     home["abbr"] if batting_home else away["abbr"], p["batter"], p["pitcher"],
                     p["description"] or p["event"], gain, f"{who} {lead:.1f}%"]
            for col, value in enumerate(cells):
                item = _SortItem(f"{value:+.1f}") if col == 5 else QTableWidgetItem(str(value))
                if col == 5:
                    item.setData(Qt.ItemDataRole.UserRole + 1, abs(value))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, p["index"])
                    item.setData(Qt.ItemDataRole.UserRole + 1, p["index"])
                if col == 4:
                    item.setToolTip(p["description"])
                self.wpa_plays.setItem(row, col, item)
        self.wpa_plays.setSortingEnabled(True)
        self.wpa_plays.horizontalHeader().setSortIndicatorShown(False)

        players = mlbstats.wpa_by_player(wp)
        self.wpa_players.setSortingEnabled(False)
        self.wpa_players.setRowCount(len(players))
        for row, r in enumerate(players):
            cells = [r["name"], game[r["side"]]["abbr"],
                     r["bat"] if r["pa"] else None, r["pitch"] if r["bf"] else None, r["total"]]
            for col, value in enumerate(cells):
                if col >= 2:
                    item = _SortItem("" if value is None else f"{value:+.1f}")
                    item.setData(Qt.ItemDataRole.UserRole + 1, -1e9 if value is None else value)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                else:
                    item = QTableWidgetItem(value)
                self.wpa_players.setItem(row, col, item)
        self.wpa_players.setSortingEnabled(True)
        self.wpa_players.horizontalHeader().setSortIndicatorShown(False)
        hero = players[0]
        self._say("wpa", f"WPA: {len(plays)} plate appearances"
                         + (f" so far ({game['current']})" if live else "")
                         + f" — top player {hero['name']} {hero['total']:+.1f}%")

    def _wpa_go_to_play(self, index):
        """Open a plate appearance on the Gameday tab."""
        if not isinstance(index, int):
            return
        target = None
        for i in range(self.gd_plays.topLevelItemCount()):
            half = self.gd_plays.topLevelItem(i)
            for j in range(half.childCount()):
                if half.child(j).data(0, Qt.ItemDataRole.UserRole) == index:
                    target = half.child(j)
        if target is None:
            return
        if target.isHidden():
            self.gd_scoring_only.setChecked(False)
        self.tabs.setCurrentIndex(self._tab_keys.index("gd"))
        self.gd_plays.setCurrentItem(target)
        self.gd_plays.scrollToItem(target, QTreeWidget.ScrollHint.PositionAtCenter)

    # ---- Statcast tab (follows the Gameday game) ----------------------------------------
    SC_SPEEDS = (("Real speed", 1.0), ("Half speed", 0.5), ("Quarter speed", 0.25), ("Tenth speed", 0.1))

    def _build_statcast_tab(self) -> QWidget:
        """The Gameday game in 3D, one plate appearance at a time: a pitcher and a batter on a
        regulation field, each pitch's flight from the pitcher's hand to the plate against the
        strike zone, and the batted ball's flight off the bat (see ``statcastview``).  Its at-bat
        follows the play picked on the Gameday tab, and stepping here moves Gameday along too."""
        self._sc_game = None
        self._sc_index = None      # the plate appearance on show (its atBatIndex)
        self._sc_pitch = 0         # the pitch on show, in its play["pitches"]
        self._sc_syncing = False   # moving Gameday's selection from here (don't follow it back)
        self.sc_title = QLabel()
        bold = self.font()
        bold.setBold(True)
        bold.setPointSizeF(bold.pointSizeF() * 1.15)
        self.sc_title.setFont(bold)
        self.sc_title.setWordWrap(True)
        self.sc_park = QLabel()  # the game's park, and where its distances come from
        self.sc_park.setWordWrap(True)
        self.sc_park.setTextFormat(Qt.TextFormat.RichText)
        self.sc_park.setOpenExternalLinks(True)

        self.sc_ab_combo = QComboBox()
        self.sc_ab_combo.setMinimumWidth(380)
        self.sc_ab_combo.setMaxVisibleItems(30)
        self.sc_ab_combo.currentIndexChanged.connect(lambda _i: self._sc_at_bat_picked())
        ab_prev, ab_next = QPushButton("◀"), QPushButton("▶")
        ab_prev.setToolTip("Previous plate appearance (↑)")
        ab_next.setToolTip("Next plate appearance (↓)")
        for btn, step in ((ab_prev, -1), (ab_next, 1)):
            btn.setFixedWidth(32)
            btn.clicked.connect(lambda _c, d=step: self._sc_step_at_bat(d))
        self.sc_in_play = QCheckBox("Balls in play only")
        self.sc_in_play.setToolTip("List only the plate appearances that ended with the ball in play.")
        self.sc_in_play.toggled.connect(lambda _on: self._sc_fill_at_bats(self._sc_index))
        to_gameday = QPushButton("Show on Gameday")
        to_gameday.setToolTip("Open this plate appearance on the Gameday tab.")
        to_gameday.clicked.connect(lambda: self._wpa_go_to_play(self._sc_index))

        pitch_prev, pitch_next = QPushButton("◀"), QPushButton("▶")
        pitch_prev.setToolTip("Previous pitch (←); from the first, the last pitch of the plate appearance before")
        pitch_next.setToolTip("Next pitch (→); from the last, the first pitch of the next plate appearance")
        for btn, step in ((pitch_prev, -1), (pitch_next, 1)):
            btn.setFixedWidth(32)
            btn.clicked.connect(lambda _c, d=step: self._sc_step_pitch(d))
        self.sc_chips = QHBoxLayout()
        self.sc_chips.setSpacing(3)
        self.sc_chip_group = QButtonGroup(self)
        self.sc_chip_group.setExclusive(True)
        self.sc_chip_group.idClicked.connect(self._sc_pick_pitch)
        self.sc_earlier = QCheckBox("Earlier pitches")
        self.sc_earlier.setChecked(True)
        self.sc_earlier.setToolTip("Keep the plate appearance's earlier pitches in view, faded.")
        self.sc_earlier.toggled.connect(lambda _on: self._sc_show())
        self.sc_play_btn = QPushButton("▶ Play")
        self.sc_play_btn.setToolTip("Fly the pitch (and the batted ball) in time (Space)")
        self.sc_play_btn.clicked.connect(self._sc_toggle_play)
        self.sc_speed = QComboBox()
        for label, speed in self.SC_SPEEDS:
            self.sc_speed.addItem(label, speed)
        self.sc_speed.setCurrentIndex(2)

        self.sc_view = statcastview.StatcastView()
        self.sc_view.stepPitch.connect(self._sc_step_pitch)
        self.sc_view.stepAtBat.connect(self._sc_step_at_bat)
        self.sc_view.animationFinished.connect(lambda: self.sc_play_btn.setText("▶ Play"))
        self.sc_preset_group = QButtonGroup(self)
        presets = QHBoxLayout()
        presets.addWidget(QLabel("View"))
        for i, name in enumerate(statcastview.PRESETS):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setChecked(name == self.sc_view.preset())
            btn.setToolTip({"Broadcast": "From center field, over the pitcher's shoulder",
                            "Catcher": "From behind home plate, the umpire's view",
                            "Side": "From the first-base side: the whole flight of the pitch",
                            "Overhead": "Straight down on the pitcher and batter",
                            "Field": "The whole field, for the batted ball"}[name])
            self.sc_preset_group.addButton(btn, i)
            presets.addWidget(btn)
        self.sc_preset_group.idClicked.connect(
            lambda i: (self.sc_view.set_preset(list(statcastview.PRESETS)[i]), self.sc_view.setFocus()))
        presets.addStretch(1)
        self.sc_view.setMinimumHeight(150)  # (its own 320 made the tab too tall for a 615-px-high screen)

        self.sc_info = QLabel()
        self.sc_info.setWordWrap(True)
        self.sc_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.sc_info.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.sc_info.setContentsMargins(6, 4, 6, 4)
        info_scroll = QScrollArea()
        info_scroll.setWidgetResizable(True)
        info_scroll.setWidget(self.sc_info)
        info_scroll.setMinimumWidth(220)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.sc_view)
        split.addWidget(info_scroll)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        split.setSizes([800, 270])

        ab_row = QHBoxLayout()
        ab_row.addWidget(QLabel("At-bat"))
        ab_row.addWidget(ab_prev)
        ab_row.addWidget(self.sc_ab_combo, 1)
        ab_row.addWidget(ab_next)
        ab_row.addSpacing(8)
        ab_row.addWidget(self.sc_in_play)
        ab_row.addSpacing(8)
        ab_row.addWidget(to_gameday)
        pitch_row = QHBoxLayout()
        pitch_row.addWidget(QLabel("Pitch"))
        pitch_row.addWidget(pitch_prev)
        pitch_row.addLayout(self.sc_chips)  # (a layout, not a widget: it grows as buttons are added)
        pitch_row.addWidget(pitch_next)
        pitch_row.addStretch(1)
        pitch_row.addWidget(self.sc_earlier)
        pitch_row.addSpacing(8)
        pitch_row.addWidget(self.sc_play_btn)
        pitch_row.addWidget(self.sc_speed)

        note = QLabel(
            "Each pitch's flight is MLB's own tracking of it (PITCHf/x from 2008, Statcast from 2015), "
            "run from the release point to the plate; the zone is the batter's, the figures are drawn to "
            "the players' listed heights and the infield to regulation distances. The outfield fence is the "
            "game's park's (as on the Stadiums tab: Wikipedia's distances for the season, else MLB's; a "
            "generic 330–400 ft one where neither has any), 8 ft high except where Wikipedia gives a section's "
            "height (Fenway's Green Monster; Field or Catcher view shows it). A batted ball (2015 on) has "
            "only its launch and where it ended up, so its flight is modelled: drag tuned to bring it down at "
            "MLB's distance, a ground ball hopping out to where it was fielded. A hit that stayed in the park "
            "stops at the fence; where it met the fence above its height, a section of wall that tall is drawn "
            "(outlined in yellow) for that play only, where a home run crossed it lower than that a lower one, "
            "and where a home run's distance falls short of the fence, the fence pulled in.")
        note.setWordWrap(True)
        small = note.font()
        small.setPointSizeF(small.pointSizeF() * 0.9)
        note.setFont(small)

        # just for fun: play the game in another park (kept from game to game until undone)
        self._sc_alt = None  # (the pick from the search, its park) or None: the game's own park
        self.sc_alt_btn = QPushButton("Select alternate stadium")
        self.sc_alt_btn.setToolTip("Put the game in another park of your choice: search the parks "
                                   "the Stadiums tab knows, by name or year.")
        self.sc_alt_btn.clicked.connect(self._sc_pick_alternate)
        self.sc_alt_back = QPushButton("Back to the game's park")
        self.sc_alt_back.setToolTip("Lay the field out in the park the game was really played in.")
        self.sc_alt_back.clicked.connect(lambda: self._sc_use_alternate(None))
        # always there, greyed while the game's own park is on show: showing and hiding it changed the
        # bottom row's height, which a maximised or snapped window didn't lay out again (the other
        # button went missing until the window was resized)
        self.sc_alt_back.setEnabled(False)
        presets.addWidget(self.sc_alt_back)  # up on the View row, where they're always on screen
        presets.addWidget(self.sc_alt_btn)
        # the note shrinks (and scrolls) before anything else, so a short screen still shows the field
        note.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        note_scroll = QScrollArea()
        note_scroll.setWidgetResizable(True)
        note_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        note_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        note_scroll.setWidget(note)
        note_scroll.setMinimumHeight(QFontMetrics(small).lineSpacing() * 2 + 4)
        note_scroll.setMaximumHeight(QFontMetrics(small).lineSpacing() * 6 + 8)
        note_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)  # share by stretch

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(self.sc_title)
        lay.addWidget(self.sc_park)
        lay.addLayout(ab_row)
        lay.addLayout(pitch_row)
        lay.addLayout(presets)
        lay.addWidget(split, 6)
        lay.addWidget(note_scroll, 1)
        self._sc_clear("Pick a game on the Gameday tab.")
        return page

    def _sc_pick_alternate(self):
        """The "Select alternate stadium" search: every park era Wikipedia gives, and MLB's parks."""
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            parks = stadiums.get_alternate_parks()
        except mlbstats.MLBStatsError:  # offline: Wikipedia's parks alone, by their articles' names
            parks = stadiums.parse_alternate_parks(stadiums.wikipedia_parks(), [])
        finally:
            QApplication.restoreOverrideCursor()
        dlg = QDialog(self)
        dlg.setWindowTitle("Select alternate stadium")
        dlg.resize(620, 520)
        search = QLineEdit()
        search.setPlaceholderText("Search: a park's name, a year (1927), a word (Monster)…")
        search.setClearButtonEnabled(True)
        found = QListWidget()
        count = QLabel()
        ok, cancel = QPushButton("Use this park"), QPushButton("Cancel")
        ok.setDefault(True)

        def refill():
            words = search.text().casefold().split()
            found.clear()
            for park in parks:
                if all(w in park["search"] for w in words):
                    item = QListWidgetItem(park["label"])
                    item.setToolTip(park["label"])
                    item.setData(Qt.ItemDataRole.UserRole, park)
                    found.addItem(item)
            found.setCurrentRow(0)
            count.setText(f"{found.count()} of {len(parks)} parks")
            ok.setEnabled(found.count() > 0)

        search.textChanged.connect(lambda _t: refill())
        found.itemDoubleClicked.connect(lambda _i: dlg.accept())
        ok.clicked.connect(dlg.accept)
        cancel.clicked.connect(dlg.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(count)
        buttons.addStretch(1)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        box = QVBoxLayout(dlg)
        box.addWidget(search)
        box.addWidget(found, 1)
        box.addLayout(buttons)
        refill()
        search.setFocus()
        if dlg.exec() != QDialog.DialogCode.Accepted or found.currentItem() is None:
            return
        self._sc_use_alternate(found.currentItem().data(Qt.ItemDataRole.UserRole))

    def _sc_use_alternate(self, pick: dict | None):
        """Play the Statcast tab's games in ``pick``'s park (None: back in each game's own)."""
        if pick is None:
            self._sc_alt = None
        else:
            park = stadiums.venue_park(pick["venue_id"], pick["venue"], pick["field"], pick["season"])
            self._sc_alt = (pick, park)
        self.sc_alt_back.setEnabled(self._sc_alt is not None)
        if self._sc_game is not None:
            self._sc_set_park(self._sc_game)
            if self._sc_index is not None:
                self._sc_show()  # the play again: where a hit met this park's fence

    def _sc_set_park(self, game: dict):
        """Lay the 3D field out in the game's park -- or the user's alternate one -- and say where
        its distances come from."""
        if self._sc_alt is not None:
            pick, park = self._sc_alt
            self.sc_view.set_park(park["fence"], park.get("walls"))
            years = pick["label"].split(" \u2014 ")[0]
            source = self._sd_source_text(park).replace("<b>Source:</b>", "distances from", 1)
            real = html.escape(game.get("venue") or "the game's park")
            self.sc_park.setText(source.replace(
                "<span style='font-size:small'>",
                f"<span style='font-size:small'><b>Park:</b> {html.escape(years)} \u2014 "
                f"<b>a user-selected stadium</b>, in place of {real}, where the game was played; ", 1))
            return
        try:
            season = int((game.get("date") or "")[:4])
        except ValueError:
            season = None
        park = stadiums.venue_park(game.get("venue_id"), game.get("venue") or "the park",
                                   game.get("venue_field") or {}, season)
        self.sc_view.set_park(park["fence"], park.get("walls"))
        if park["fence"] is None:
            self.sc_park.setText(f"<span style='font-size:small'><b>Park:</b> {html.escape(park['venue'])} "
                                 "\u2014 no distances for it (neither Wikipedia nor MLB has enough), so the "
                                 "fence is a generic 330\u2013400 ft one.</span>")
            return
        source = self._sd_source_text(park).replace("<b>Source:</b>", "distances from", 1)
        self.sc_park.setText(source.replace("<span style='font-size:small'>",
                                            f"<span style='font-size:small'><b>Park:</b> "
                                            f"{html.escape(park['venue'])} \u2014 ", 1))

    def _sc_clear(self, message: str = ""):
        self._sc_game = None
        self._sc_index = None
        self.sc_title.setText("")
        self.sc_park.setText("")
        self.sc_view.set_park(None)
        self.sc_ab_combo.blockSignals(True)
        self.sc_ab_combo.clear()
        self.sc_ab_combo.blockSignals(False)
        self._sc_set_chips(None)
        self.sc_info.setText("")
        self.sc_view.set_message(message)

    @staticmethod
    def _sc_tracked(play: dict) -> bool:
        return any(q.get("track") for q in play["pitches"])

    def _sc_game_changed(self, game: dict, same_game: bool):
        """The Gameday game was (re)loaded.  The same game (a live one updating) keeps its
        at-bat -- or, watching the latest pitch, moves on with the game; a new game opens on
        the play picked on Gameday, else its first plate appearance."""
        old = self._sc_game if same_game else None
        follow = False
        if old and self._sc_index is not None and old["plays"]:
            last = old["plays"][-1]
            follow = last["index"] == self._sc_index and self._sc_pitch >= len(last["pitches"]) - 1
        self._sc_game = game
        self.sc_title.setText(self.gd_title.text())
        self._sc_set_park(game)
        if not game["plays"]:
            self._sc_clear({"scheduled": "This game hasn't started yet.", "live": "No plays yet."}.get(
                game["state"], "MLB's API has no play-by-play for this game."))
            self._sc_game = game
            self.sc_title.setText(self.gd_title.text())
            self._sc_set_park(game)
            return
        if follow:
            keep = game["plays"][-1]["index"]
            self._sc_pitch = 10 ** 6  # its latest pitch
        elif same_game and self._sc_index is not None:
            keep = self._sc_index
        else:
            keep = self._gd_selected_play() if isinstance(self._gd_selected_play(), int) else None
            self._sc_pitch = 10 ** 6
        self._sc_fill_at_bats(keep)

    def _sc_at_bat_label(self, play: dict) -> str:
        """ "T3 · Aaron Judge vs Kevin Gausman · Double" """
        half = "T" if play["half"] == "top" else "B"
        event = play["event"] or ("in progress" if not play["complete"] else "")
        return f"{half}{play['inning']} · {play['batter']} vs {play['pitcher']}" + (f" · {event}" if event else "")

    def _sc_listed(self) -> list[dict]:
        game = self._sc_game
        if not game:
            return []
        plays = [p for p in game["plays"] if p["pitches"]]
        if self.sc_in_play.isChecked():
            plays = [p for p in plays if any(q["in_play"] for q in p["pitches"])]
        return plays

    def _sc_fill_at_bats(self, keep):
        plays = self._sc_listed()
        self.sc_ab_combo.blockSignals(True)
        self.sc_ab_combo.clear()
        for play in plays:
            self.sc_ab_combo.addItem(self._sc_at_bat_label(play), play["index"])
        index = self.sc_ab_combo.findData(keep) if keep is not None else -1
        if index < 0 and keep is not None:  # (filtered out) the nearest one listed after it
            index = next((i for i, p in enumerate(plays) if p["index"] > keep), len(plays) - 1)
        if index < 0:
            index = next((i for i, p in enumerate(plays) if self._sc_tracked(p)), 0)
        self.sc_ab_combo.setCurrentIndex(index if plays else -1)
        self.sc_ab_combo.blockSignals(False)
        if not plays:
            self._sc_index = None
            self._sc_set_chips(None)
            self.sc_info.setText("")
            self.sc_view.set_message("No plate appearance with pitches in this game."
                                     if not self.sc_in_play.isChecked() else "No balls in play yet.")
            return
        self._sc_at_bat_picked(keep_pitch=True)

    def _sc_play(self):
        game = self._sc_game
        if not game or self._sc_index is None:
            return None
        return next((p for p in game["plays"] if p["index"] == self._sc_index), None)

    def _sc_at_bat_picked(self, keep_pitch: bool = False, first: bool = False):
        """A plate appearance was chosen: show its last pitch (the one that settled it), its
        first when stepping into it pitch by pitch, or the same pitch on a live update."""
        changed = self.sc_ab_combo.currentData() != self._sc_index
        self._sc_index = self.sc_ab_combo.currentData()
        play = self._sc_play()
        if play is None:
            return
        n = len(play["pitches"])
        if first:
            self._sc_pitch = 0
        elif changed or not keep_pitch:
            self._sc_pitch = n - 1
        self._sc_pitch = max(0, min(self._sc_pitch, n - 1))
        self._sc_set_chips(play)
        self._sc_show()
        self._sc_sync_gameday()

    def _sc_set_chips(self, play):
        """One button per pitch of the plate appearance, coloured by its result."""
        for btn in self.sc_chip_group.buttons():
            self.sc_chip_group.removeButton(btn)
            btn.deleteLater()
        while self.sc_chips.count():
            self.sc_chips.takeAt(0)
        if not play:
            return
        for i, q in enumerate(play["pitches"]):
            btn = QPushButton(f"{q['n'] if q['n'] is not None else '–'} {q.get('code') or ''}".strip())
            btn.setCheckable(True)
            btn.setMinimumWidth(10)
            btn.setFixedWidth(max(34, btn.fontMetrics().horizontalAdvance(btn.text()) + 16))
            color = (statcastview.IN_PLAY_COLOR if q["in_play"] else statcastview.BALL_COLOR if q["ball"]
                     else statcastview.STRIKE_COLOR if q["strike"] else "#888888")
            btn.setStyleSheet(f"QPushButton {{ color: {color}; font-weight: bold; }}")
            mph = f" {q['mph']:.1f} mph" if q["mph"] is not None else ""
            btn.setToolTip(f"{q['count']} after · {q['pitch'] or 'pitch'}{mph} · {q['call']}")
            self.sc_chip_group.addButton(btn, i)
            self.sc_chips.addWidget(btn)

    def _sc_pick_pitch(self, i: int):
        self._sc_pitch = i
        self._sc_show()
        self.sc_view.setFocus()

    def _sc_step_pitch(self, step: int):
        play = self._sc_play()
        if play is None:
            return
        target = self._sc_pitch + step
        if 0 <= target < len(play["pitches"]):
            self._sc_pitch = target
            self._sc_show()
            return
        row = self.sc_ab_combo.currentIndex() + step
        if 0 <= row < self.sc_ab_combo.count():  # on into the next (back into the previous) plate appearance
            self.sc_ab_combo.blockSignals(True)
            self.sc_ab_combo.setCurrentIndex(row)
            self.sc_ab_combo.blockSignals(False)
            self._sc_at_bat_picked(first=step > 0)

    def _sc_step_at_bat(self, step: int):
        row = self.sc_ab_combo.currentIndex() + step
        if 0 <= row < self.sc_ab_combo.count():
            self.sc_ab_combo.setCurrentIndex(row)
        self.sc_view.setFocus()

    def _sc_toggle_play(self):
        if self.sc_view.playing():
            self.sc_view.stop()
            self.sc_play_btn.setText("▶ Play")
        else:
            self.sc_view.play(self.sc_speed.currentData())
            if self.sc_view.playing():
                self.sc_play_btn.setText("■ Stop")
        self.sc_view.setFocus()

    def _sc_show(self):
        """Put the pitch on show into the view and the panel beside it."""
        play = self._sc_play()
        self.sc_play_btn.setText("▶ Play")
        if play is None:
            return
        btn = self.sc_chip_group.button(self._sc_pitch)
        if btn is not None:
            btn.setChecked(True)
        if not self._sc_tracked(play):
            year = (self._sc_game or {}).get("date", "")[:4]
            if year and year < "2008" and not any(self._sc_tracked(p) for p in self._sc_game["plays"]):
                self.sc_view.set_message(
                    "Can't show action: MLB's pitch tracking begins in 2008 (batted balls in 2015). "
                    "But you can explore the park if you wish \u2014 drag to rotate, wheel to zoom, "
                    "or the Field view for all of it.", explore=True)
            else:
                self.sc_view.set_message("MLB has no tracking for this plate appearance's pitches.")
        else:
            self.sc_view.set_at_bat(play, self._sc_pitch, self.sc_earlier.isChecked())
        self.sc_info.setText(self._sc_info_html(play))
        q = play["pitches"][self._sc_pitch]
        self._say("sc", f"Statcast: {play['batter']} vs {play['pitcher']} — pitch {self._sc_pitch + 1} of "
                        f"{len(play['pitches'])}: {q['pitch'] or 'pitch'}, {q['call']}")

    def _sc_info_html(self, play: dict) -> str:
        esc = html.escape
        hands = {"L": "left", "R": "right", "S": "both sides"}
        q = play["pitches"][self._sc_pitch]
        tr = q.get("track")
        lines = [f"<b>{esc(play['batter'])}</b>" + (f" (bats {hands[play['bat_side']]})" if play["bat_side"] in hands else ""),
                 f"vs <b>{esc(play['pitcher'])}</b>" + (f" (throws {hands[play['pitch_hand']]})"
                                                        if play["pitch_hand"] in hands else ""),
                 f"<i>{esc(play['description'] or play['event'] or 'In progress')}</i>", ""]
        color = statcastview.PITCH_COLORS.get(q.get("code") or "", statcastview.OTHER_PITCH)
        title = q["pitch"] or "Pitch"
        lines.append(f"<span style='color:{color}'>■</span> <b>Pitch {q['n'] if q['n'] is not None else '–'}: "
                     f"{esc(title)}</b>")
        rows = [("Result", esc(q["call"])), ("Count after", q["count"])]
        if q["mph"] is not None:
            speed = f"{q['mph']:.1f} mph"
            if tr and tr.get("end_mph"):
                speed += f" → {tr['end_mph']:.1f} at the plate"
            rows.append(("Speed", speed))
        if q["spin"] is not None:
            rows.append(("Spin", f"{q['spin']:.0f} rpm"))
        if q["ivb"] is not None:
            rows.append(("Vertical break", mlbstats.vertical_break(q["ivb"])))
        if q["hb"] is not None:
            rows.append(("Horizontal break", mlbstats.break_side(q["hb"], play["pitch_hand"])))
        if q["zone"] is not None:
            rows.append(("Location", mlbstats.zone_location(q["zone"], play["bat_side"])))
        if tr:
            rel = statcastview.release_point(tr)
            arm = "3B" if rel[0] < 0 else "1B"
            rows.append(("Release", f"{rel[2]:.1f} ft high, {abs(rel[0]):.1f} ft to the {arm} side"))
            if tr.get("extension"):
                rows.append(("Extension", f"{tr['extension']:.1f} ft (released {statcastview.release_y(tr):.1f} ft "
                                          "from the plate)"))
            flight = statcastview.pitch_path(tr)[-1][0]
            rows.append(("Flight", f"{flight:.3f} s to the plate"))
            rows.append(("Crossed", f"{tr['px'] * 12:+.1f} in across, {tr['pz'] * 12:.1f} in up"))
            if tr.get("sz_top") and tr.get("sz_bot"):
                rows.append(("Zone", f"{tr['sz_bot'] * 12:.0f}–{tr['sz_top'] * 12:.0f} in (this batter)"))
        else:
            rows.append(("Tracking", "none for this pitch"))
        lines += [f"{k}: {v}" for k, v in rows]
        hit = play.get("hit")
        if hit and q["in_play"] and self._sc_pitch == len(play["pitches"]) - 1:
            lines += ["", "<b>Batted ball</b>"]
            if hit["ev"] is not None:
                lines.append(f"Exit velocity: {hit['ev']:.1f} mph")
            if hit["la"] is not None:
                lines.append(f"Launch angle: {hit['la']:.0f}°")
            if hit["dist"] is not None:
                lines.append(f"Distance: {hit['dist']:.0f} ft" + (" (to its first bounce)"
                                                                 if statcastview.grounder(hit) else ""))
            if hit["trajectory"]:
                lines.append(f"Type: {esc(hit['trajectory'])}" + (f", hit {esc(hit['hardness'])}" if hit["hardness"] else ""))
            angle = statcastview.spray_angle(hit)
            if angle is not None:
                field = "center field" if abs(angle) < 8 else ("left" if angle < 0 else "right") + " field"
                pull = ""
                if play["bat_side"] in ("L", "R") and abs(angle) >= 8:
                    pull = " (pulled)" if (angle < 0) == (play["bat_side"] == "R") else " (opposite field)"
                lines.append(f"Direction: {abs(angle):.0f}° toward {field}{pull}" if abs(angle) >= 8
                             else "Direction: straightaway center")
            if tr and hit["ev"] is not None and hit["la"] is not None:
                model = statcastview.batted_path(hit, (tr["px"], statcastview.PLATE_FRONT, tr["pz"]))
                if model:
                    what = "in the air" if model["air"] else "before its first bounce"
                    lines.append(f"Hang time (modelled): {model['hang']:.1f} s {what}")
                    if not model["air"]:
                        lines.append(f"Fielded about {model['distance']:.0f} ft out")
            wall = self.sc_view.current_wall()
            if wall:
                lines.append(f"<i>{esc(statcastview.wall_text(wall))}</i>")
        return "<br>".join(lines)

    def _sc_sync_gameday(self):
        """Move the Gameday tab's selection to the plate appearance on show here."""
        if self._sc_index is None or self._sc_game is not self._gd_game:
            return
        for i in range(self.gd_plays.topLevelItemCount()):
            half = self.gd_plays.topLevelItem(i)
            for j in range(half.childCount()):
                item = half.child(j)
                if item.data(0, Qt.ItemDataRole.UserRole) == self._sc_index:
                    if not item.isHidden() and self.gd_plays.currentItem() is not item:
                        self._sc_syncing = True
                        try:
                            self.gd_plays.setCurrentItem(item)
                            self.gd_plays.scrollToItem(item)
                        finally:
                            self._sc_syncing = False
                    return

    def _sc_follow_gameday(self, index):
        """A play was picked on the Gameday tab: show it here (its last pitch)."""
        if self._sc_syncing or index is None or self._sc_game is None or self._sc_game is not self._gd_game:
            return
        if index == self._sc_index:
            return
        row = self.sc_ab_combo.findData(index)
        if row < 0 and self.sc_in_play.isChecked():
            self.sc_in_play.blockSignals(True)
            self.sc_in_play.setChecked(False)
            self.sc_in_play.blockSignals(False)
            self._sc_fill_at_bats(index)
            return
        if row >= 0:
            self.sc_ab_combo.setCurrentIndex(row)

    def _gd_export(self):
        game = self._gd_game
        if not game or not game["plays"]:
            return
        away, home = game["away"], game["home"]
        name = f"gameday_{game['date']}_{away['abbr']}-at-{home['abbr']}".replace(" ", "-")
        path, chosen = QFileDialog.getSaveFileName(
            self, "Export game", os.path.join(HOME, name + ".xlsx"),
            "Excel workbook (*.xlsx);;CSV, plays only (*.csv)")
        if not path:
            return
        as_csv = path.lower().endswith(".csv") or (chosen.startswith("CSV") and not path.lower().endswith(".xlsx"))
        if as_csv and not path.lower().endswith(".csv"):
            path += ".csv"
        elif not as_csv and not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        play_headers, play_rows, pitch_headers, pitch_rows = mlbstats.gameday_rows(game)
        try:
            if as_csv:
                with open(path, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(play_headers)
                    writer.writerows(play_rows)
            else:
                book = Workbook()
                sheet = book.add_sheet("Plays")
                sheet.append(play_headers, header=True)
                for row in play_rows:
                    sheet.append(row)
                if game["sequences"] != "none":
                    sheet = book.add_sheet("Pitches")
                    sheet.append(pitch_headers, header=True)
                    for row in pitch_rows:
                        sheet.append(row)
                sheet = book.add_sheet("Line score")
                count = len(game["innings"])
                sheet.append(["Team"] + list(range(1, count + 1)) + ["R", "H", "E", "LOB"], header=True)
                for side in ("away", "home"):
                    t = game[side]
                    sheet.append([t["name"]] + [inn[side]["runs"] for inn in game["innings"]]
                                 + [t["runs"], t["hits"], t["errors"], t["lob"]])
                book.save(path)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self._say("gd", f"Saved {path}")

    # ---- info + infograph tabs ---------------------------------------------
    _INFO_SERIES_COLS = ["Name", "Formula", "Panel", "Format"]

    def _build_info_tab(self) -> QWidget:
        """Info queries: each defines a range of values -- per game or per season, for one or
        more players or teams -- by formula series over the Dailies/Personal data (see
        ``infoquery``).  Queries live in .json files (``info-*.json`` in the app folder load
        at startup); here they're listed, edited, run and tabled.  The Infograph tab graphs
        them."""
        self._info_queries: list[dict] = []          # normalised (infoquery.parse_query)
        self._info_results: dict[str, dict] = {}     # query name -> its last run
        self._info_dirty = False
        self._info_loading_form = False

        self.info_list = QListWidget()
        self.info_list.setToolTip("Every Info query this session.  Double-click one to graph it.")
        self.info_list.currentRowChanged.connect(self._on_info_selected)
        self.info_list.itemDoubleClicked.connect(lambda _i: self._graph_info())
        new_btn = QPushButton("New")
        new_btn.clicked.connect(self._new_info_query)
        self.info_remove_btn = QPushButton("Remove")
        self.info_remove_btn.clicked.connect(self._remove_info_query)
        load_btn = QPushButton("Load…")
        load_btn.setToolTip("Add queries from one or more JSON files (a query, or a list of them)")
        load_btn.clicked.connect(self._load_info_queries)
        self.info_save_btn = QPushButton("Save…")
        self.info_save_btn.setToolTip("Save the selected query to a JSON file")
        self.info_save_btn.clicked.connect(self._save_info_query)
        list_box = QGroupBox("Queries")
        lb = QVBoxLayout(list_box)
        lb.addWidget(self.info_list, 1)
        row1, row2 = QHBoxLayout(), QHBoxLayout()
        row1.addWidget(new_btn)
        row1.addWidget(self.info_remove_btn)
        row2.addWidget(load_btn)
        row2.addWidget(self.info_save_btn)
        lb.addLayout(row1)
        lb.addLayout(row2)

        # the editor
        self.info_name = QLineEdit()
        self.info_source = QComboBox()
        for key, label in infoquery.SOURCES.items():
            self.info_source.addItem(label, key)
        self.info_subjects = QLineEdit()
        self.info_subjects.setPlaceholderText("e.g. George Brett  ·  Babe Ruth; Hank Aaron  ·  National League  ·  AL East")
        self.info_subjects.setToolTip(
            "Players or teams, separated by semicolons.  For teams, a league's or division's name (“National League”,\n"
            "“American League East” or “AL East”) means every club in it that season.\n"
            "“Name [id 123]” picks one of several players with a name.")
        self.info_season = _YearSpin()
        self.info_season.setRange(mlbstats.DAILY_FROM_SEASON, 2100)
        self.info_post = QCheckBox("Include postseason")
        self.info_season_to = _YearSpin()
        self.info_season_to.setRange(mlbstats.DAILY_FROM_SEASON, 2100)
        self.info_season_to.setToolTip("The last season of the span (the ranked lists of all MLB seasons or games)")
        self.info_season_to_label = QLabel("to")
        self.info_rank_by = QLineEdit()
        self.info_rank_by.setPlaceholderText("a formula — the highest values rank first")
        self.info_rank_by.setToolTip(
            "Every player's season in the span is worked out by this formula and the highest kept.\n"
            "Besides {Batting: …} and {Fielding: …} (his main position) it can use {MLB: AVG}, {MLB: OBP},\n"
            "{MLB: SLG}, {MLB: RF/9} and {MLB: RF/G} (the league average that season, RF at his position)\n"
            "and {Team: G} (his team's games, for the qualifying rules).")
        self.info_top = QSpinBox()
        self.info_top.setRange(1, infoquery.MAX_TOP)
        self.info_top.setValue(10)
        self.info_rank_row = QWidget()
        rank_lay = QHBoxLayout(self.info_rank_row)
        rank_lay.setContentsMargins(0, 0, 0, 0)
        rank_lay.addWidget(self.info_rank_by, 1)
        rank_lay.addWidget(QLabel("Top"))
        rank_lay.addWidget(self.info_top)
        self.info_career = QCheckBox("Career")
        self.info_career.setToolTip("Every season the player played (game logs begin in 1901), not one season")
        self.info_career.toggled.connect(lambda _on: self._on_info_source_changed())
        self.info_where = QLineEdit()
        self.info_where.setPlaceholderText("e.g. {Pitching: H} = 0; {Pitching: Outs} >= 27  (blank = every game)")
        self.info_where.setToolTip(
            "Keep only the games (or seasons) where every condition holds, separated by semicolons.\n"
            "Each is two formulas, or a formula and a number, joined by = != < <= > or >=.\n"
            "Running totals, game numbers and the graph then count the kept games alone.")
        self.info_x = QComboBox()
        self.info_running = QCheckBox("Running totals")
        self.info_running.setToolTip("Work each formula out over the totals so far, not one game or season alone\n"
                                     "(so {Batting: H} / {Batting: AB} is the season-to-date average)")
        self.info_series = QTableWidget(0, len(self._INFO_SERIES_COLS))
        self.info_series.setHorizontalHeaderLabels(self._INFO_SERIES_COLS)
        self.info_series.verticalHeader().setVisible(False)
        hh = self.info_series.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in (0, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.info_series.setToolTip(
            "One line (per subject) for each series.  Formula: as on the Custom tab, e.g. {Batting: H} / {Batting: AB}.\n"
            f"Panel 1–{infoquery.MAX_PANELS}: series of different sizes go in different panels (stacked, one y scale each).\n"
            "Format: auto, int, rate (.400), or 1/2/3 decimals.")
        self.info_series.setMinimumHeight(90)
        add_series = QPushButton("Add series")
        add_series.clicked.connect(lambda: self._add_info_series_row({"name": "", "formula": "", "panel": 1}))
        del_series = QPushButton("Remove series")
        del_series.clicked.connect(self._remove_info_series_row)
        self.info_insert = QComboBox()
        self.info_insert.setMinimumWidth(160)
        insert_btn = QPushButton("Insert")
        insert_btn.setToolTip("Add the chosen variable to the selected series' formula")
        insert_btn.clicked.connect(self._insert_info_variable)
        self.info_refs = QLineEdit()
        self.info_refs.setPlaceholderText("e.g. 2: .400  (panel: value, comma-separated)")
        self.info_refs.setToolTip("Horizontal reference lines, labelled with the value as typed")

        form = QFormLayout()
        form.addRow("Name", self.info_name)
        form.addRow("Data", self.info_source)
        form.addRow("Subjects", self.info_subjects)
        season_row = QHBoxLayout()
        season_row.addWidget(self.info_season)
        season_row.addWidget(self.info_season_to_label)
        season_row.addWidget(self.info_season_to)
        season_row.addWidget(self.info_career)
        season_row.addWidget(self.info_post)
        season_row.addSpacing(10)
        season_row.addWidget(QLabel("X axis"))
        season_row.addWidget(self.info_x)
        season_row.addWidget(self.info_running)
        season_row.addStretch(1)
        form.addRow("Season", season_row)
        form.addRow("Only where", self.info_where)
        form.addRow("Rank by", self.info_rank_row)
        self._info_form = form
        series_tools = QHBoxLayout()
        series_tools.addWidget(add_series)
        series_tools.addWidget(del_series)
        series_tools.addStretch(1)
        series_tools.addWidget(self.info_insert, 1)
        series_tools.addWidget(insert_btn)
        self.info_run_btn = QPushButton("Run")
        self.info_run_btn.setToolTip("Apply the changes, fetch the data and fill the table")
        self.info_run_btn.clicked.connect(lambda: self._run_info(graph=False))
        graph_btn = QPushButton("Graph ▸")
        graph_btn.setToolTip("Run, then show it on the Infograph tab")
        graph_btn.clicked.connect(self._graph_info)
        buttons = QHBoxLayout()
        buttons.addWidget(QLabel("Reference lines"))
        buttons.addWidget(self.info_refs, 1)
        buttons.addWidget(self.info_run_btn)
        buttons.addWidget(graph_btn)
        editor = QGroupBox("Query")
        ed = QVBoxLayout(editor)
        ed.addLayout(form)
        ed.addWidget(self.info_series, 1)
        ed.addLayout(series_tools)
        ed.addLayout(buttons)
        self.info_source.currentIndexChanged.connect(lambda _i: self._on_info_source_changed())
        for w in (self.info_name, self.info_subjects, self.info_refs, self.info_where):
            w.textEdited.connect(lambda _t: self._mark_info_dirty())
        for w in (self.info_season, self.info_season_to, self.info_top):
            w.valueChanged.connect(lambda _v: self._mark_info_dirty())
        self.info_rank_by.textEdited.connect(lambda _t: self._mark_info_dirty())
        for w in (self.info_post, self.info_running):
            w.toggled.connect(lambda _on: self._mark_info_dirty())
        self.info_x.currentIndexChanged.connect(lambda _i: self._mark_info_dirty())
        self.info_series.itemChanged.connect(lambda _it: self._mark_info_dirty())

        # the result
        self.info_result_note = QLabel("Run a query to see its values.")
        self.info_result_note.setWordWrap(True)
        self.info_model = QStandardItemModel(self)
        self.info_table = QTableView()
        self.info_table.setModel(self.info_model)
        self.info_table.setAlternatingRowColors(True)
        self.info_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.info_table.verticalHeader().setVisible(False)
        self.info_table.horizontalHeader().setHighlightSections(False)
        result_box = QGroupBox("Values")
        rb = QVBoxLayout(result_box)
        rb.addWidget(self.info_result_note)
        rb.addWidget(self.info_table, 1)

        split = QSplitter(Qt.Orientation.Horizontal)
        for w in (list_box, editor, result_box):
            split.addWidget(w)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 5)
        split.setStretchFactor(2, 4)
        split.setSizes([210, 520, 420])
        page = QWidget()
        QVBoxLayout(page).addWidget(split)
        self._on_info_source_changed()
        self._set_info_editor_enabled(False)
        return page

    def _build_infograph_tab(self) -> QWidget:
        """Graph an Info query: pick it, and its lines are drawn (``infochart.InfoChart``)."""
        self.ig_combo = QComboBox()
        self.ig_combo.setMinimumWidth(320)
        self.ig_combo.activated.connect(self._on_ig_chosen)
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Fetch the data again and redraw")
        refresh.clicked.connect(lambda: self._run_info_named(self.ig_combo.currentText(), graph=True, force=True))
        edit = QPushButton("Edit on Info…")
        edit.clicked.connect(self._ig_edit)
        save = QPushButton("Save image…")
        save.clicked.connect(self._save_infograph)
        top = QHBoxLayout()
        top.addWidget(QLabel("Query"))
        top.addWidget(self.ig_combo, 1)
        top.addWidget(refresh)
        top.addWidget(edit)
        top.addWidget(save)
        self.ig_desc = QLabel()
        self.ig_desc.setWordWrap(True)
        self.ig_chart = infochart.InfoChart()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addLayout(top)
        lay.addWidget(self.ig_desc)
        lay.addWidget(self.ig_chart, 1)
        return page

    # -- the list ------------------------------------------------------------
    def _load_default_info_queries(self):
        for fname in sorted(os.listdir(HERE)):
            if fname.startswith("info-") and fname.endswith(".json"):
                try:
                    self._add_info_queries(infoquery.read_queries(os.path.join(HERE, fname)))
                except (OSError, ValueError) as exc:
                    self._say("info", f"Skipped {fname}: {exc}")
        if self._info_queries:
            self.info_list.setCurrentRow(0)

    def _add_info_queries(self, queries: list[dict]):
        """Add (or, same name, replace) queries and refresh both tabs' lists."""
        for q in queries:
            names = [x["name"] for x in self._info_queries]
            if q["name"] in names:
                self._info_queries[names.index(q["name"])] = q
                self._info_results.pop(q["name"], None)
            else:
                self._info_queries.append(q)
        self._refresh_info_lists(queries[-1]["name"] if queries else None)

    def _refresh_info_lists(self, select: str | None = None):
        names = [q["name"] for q in self._info_queries]
        current = select or (self.info_list.currentItem().text() if self.info_list.currentItem() else None)
        self._info_loading_form = True
        self.info_list.blockSignals(True)
        self.info_list.clear()
        self.info_list.addItems(names)
        self.info_list.blockSignals(False)
        self._info_loading_form = False
        ig_now = self.ig_combo.currentText()
        self.ig_combo.blockSignals(True)
        self.ig_combo.clear()
        self.ig_combo.addItems(names)
        self.ig_combo.setCurrentIndex(names.index(ig_now) if ig_now in names else (0 if names else -1))
        self.ig_combo.blockSignals(False)
        if current in names:
            self.info_list.setCurrentRow(names.index(current))
        elif names:
            self.info_list.setCurrentRow(0)
        else:
            self._on_info_selected(-1)

    def _on_info_selected(self, row: int):
        if self._info_loading_form:
            return
        self._info_dirty = False
        if row < 0 or row >= len(self._info_queries):
            self._set_info_editor_enabled(False)
            self.info_model.clear()
            self.info_result_note.setText("No query selected.")
            return
        self._set_info_editor_enabled(True)
        self._fill_info_form(self._info_queries[row])
        result = self._info_results.get(self._info_queries[row]["name"])
        if result:
            self._fill_info_table(result)
        else:
            self.info_model.clear()
            self.info_result_note.setText("Not run yet — Run fills this table; Graph ▸ draws it.")

    def _set_info_editor_enabled(self, on: bool):
        for w in (self.info_name, self.info_source, self.info_subjects, self.info_season, self.info_post,
                  self.info_career, self.info_where, self.info_season_to, self.info_rank_by, self.info_top,
                  self.info_x, self.info_running, self.info_series, self.info_refs, self.info_run_btn,
                  self.info_remove_btn, self.info_save_btn):
            w.setEnabled(on)
        if on:
            self._on_info_source_changed()

    def _new_info_query(self):
        if not self._apply_info_form(quiet=True):
            return
        names = {q["name"] for q in self._info_queries}
        n = 1
        while f"New query {n}" in names:
            n += 1
        q = infoquery.parse_query({"name": f"New query {n}", "source": "player-games", "subjects": ["George Brett"],
                                   "season": 1980, "x": "date", "running": True,
                                   "series": [{"name": "Hits", "formula": "{Batting: H}"}]})
        self._add_info_queries([q])
        self.info_name.setFocus()
        self.info_name.selectAll()

    def _remove_info_query(self):
        row = self.info_list.currentRow()
        if 0 <= row < len(self._info_queries):
            q = self._info_queries.pop(row)
            self._info_results.pop(q["name"], None)
            self._info_dirty = False
            self._refresh_info_lists()

    def _load_info_queries(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Load Info queries", HERE, "JSON files (*.json)")
        problems, added = [], 0
        for path in paths:
            try:
                qs = infoquery.read_queries(path)
            except (OSError, ValueError) as exc:
                problems.append(f"{os.path.basename(path)}: {exc}")
                continue
            self._add_info_queries(qs)
            added += len(qs)
        if problems:
            QMessageBox.warning(self, "Load Info queries", "Skipped:\n" + "\n".join(problems))
        if paths:
            self._say("info", f"Loaded {added} quer{'y' if added == 1 else 'ies'}.")

    def _save_info_query(self):
        if not self._apply_info_form():
            return
        row = self.info_list.currentRow()
        if not 0 <= row < len(self._info_queries):
            return
        q = self._info_queries[row]
        slug = "".join(c if c.isalnum() else "-" for c in q["name"].lower()).strip("-")
        while "--" in slug:
            slug = slug.replace("--", "-")
        path, _ = QFileDialog.getSaveFileName(self, "Save Info query", os.path.join(HERE, f"info-{slug}.json"),
                                              "JSON files (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(infoquery.query_to_json(q), fh, indent=2)
                fh.write("\n")
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return
        self._say("info", f"Saved {path}")

    # -- the editor ----------------------------------------------------------
    def _mark_info_dirty(self):
        if not self._info_loading_form:
            self._info_dirty = True

    def _on_info_source_changed(self):
        source = self.info_source.currentData()
        keep = self.info_x.currentData()
        self.info_x.blockSignals(True)
        self.info_x.clear()
        for x in infoquery.X_CHOICES[source]:
            self.info_x.addItem(infoquery.X_LABELS[x], x)
        i = self.info_x.findData(keep)
        self.info_x.setCurrentIndex(max(i, 0))
        self.info_x.blockSignals(False)
        games, on = source != "player-seasons", self.info_name.isEnabled()
        career_ok, league = source == "player-games", source in infoquery.RANKED
        post_ok = games and source != "league-seasons"  # a ranking of games may take in the postseason
        self.info_career.setEnabled(career_ok and on)
        self.info_season.setEnabled(games and on and not (career_ok and self.info_career.isChecked()))
        self.info_post.setEnabled(post_ok and on)
        self.info_subjects.setEnabled(on and not league)
        for w in (self.info_season_to_label, self.info_season_to):
            w.setVisible(league)
        self.info_career.setVisible(not league)
        self.info_post.setVisible(source != "league-seasons")
        self._info_form.setRowVisible(self.info_rank_row, league)
        self._refresh_info_insert()
        self._mark_info_dirty()

    def _refresh_info_insert(self):
        """The Insert list: the data's variables, then every Player-level custom stat."""
        keep = self.info_insert.currentText()
        self.info_insert.clear()
        row = self.info_list.currentRow()
        pitchers = 0 <= row < len(self._info_queries) and self._info_queries[row].get("pitchers", False)
        self.info_insert.addItems(infoquery.variables_for(self.info_source.currentData(), pitchers)
                                  + [f"Custom: {n}" for n, _f in self._custom_stats_for("player")])
        i = self.info_insert.findText(keep)
        if i >= 0:
            self.info_insert.setCurrentIndex(i)

    def _fill_info_form(self, q: dict):
        self._info_loading_form = True
        try:
            self.info_name.setText(q["name"])
            self.info_source.setCurrentIndex(self.info_source.findData(q["source"]))
            self._on_info_source_changed()
            self.info_subjects.setText("; ".join(
                s if isinstance(s, str) else (f"{s.get('name', '')} [id {s['id']}]" if s.get("id") else s["name"])
                for s in q["subjects"]))
            career = q.get("season") == infoquery.CAREER
            self.info_career.setChecked(career)
            if q.get("season") and not career:
                self.info_season.setValue(q["season"])
            self.info_where.setText("; ".join(q.get("where") or []))
            if q["source"] in infoquery.RANKED:
                self.info_season_to.setValue(q["season_to"])
            self.info_rank_by.setText(q.get("rank_by", ""))
            self.info_rank_by.setPlaceholderText(
                f"a formula — the {q.get('order', 'highest')} values rank first")
            self.info_top.setValue(q.get("top", 10))
            self._on_info_source_changed()
            self.info_post.setChecked(q["postseason"])
            self.info_x.setCurrentIndex(max(self.info_x.findData(q["x"]), 0))
            self.info_running.setChecked(q["running"])
            self.info_series.setRowCount(0)
            for s in q["series"]:
                self._add_info_series_row(s)
            self.info_refs.setText(", ".join(
                f"{r['panel']}: {r['label'] or infoquery.format_value(r['value'])}"
                + ("" if not r["label"] or _ref_value(r["label"]) == r["value"] else f" = {r['value']:g}")
                for r in q["reference_lines"]))
        finally:
            self._info_loading_form = False
        self._info_dirty = False

    def _add_info_series_row(self, s: dict):
        t = self.info_series
        row = t.rowCount()
        t.insertRow(row)
        name = QTableWidgetItem(s.get("name", ""))
        # what the form doesn't show (a series' own source/subjects/season...) rides along unchanged
        name.setData(Qt.ItemDataRole.UserRole,
                     {k: s[k] for k in ("running", "source", "subjects", "season", "postseason") if k in s})
        t.setItem(row, 0, name)
        t.setItem(row, 1, QTableWidgetItem(s.get("formula", "")))
        t.setItem(row, 2, QTableWidgetItem(str(s.get("panel", 1))))
        t.setItem(row, 3, QTableWidgetItem(s.get("format", "auto")))
        t.setCurrentCell(row, 1)
        self._mark_info_dirty()

    def _remove_info_series_row(self):
        row = self.info_series.currentRow()
        if row >= 0:
            self.info_series.removeRow(row)
            self._mark_info_dirty()

    def _insert_info_variable(self):
        t = self.info_series
        row = t.currentRow()
        if row < 0:
            if t.rowCount() == 0:
                self._add_info_series_row({"name": "", "formula": "", "panel": 1})
            row = t.rowCount() - 1
        item = t.item(row, 1) or QTableWidgetItem("")
        text = item.text().rstrip()
        token = "{" + self.info_insert.currentText() + "}"
        item.setText(f"{text} {token}".strip())
        t.setItem(row, 1, item)
        t.setCurrentCell(row, 1)
        self._mark_info_dirty()

    def _info_form_object(self) -> dict:
        """The editor's contents as a query object (keeping what the form doesn't show)."""
        row = self.info_list.currentRow()
        old = self._info_queries[row] if 0 <= row < len(self._info_queries) else {}
        subjects = []
        for part in self.info_subjects.text().split(";"):
            part = part.strip()
            if not part:
                continue
            m = re.fullmatch(r"(.*?)\s*\[id\s*(\d+)\]", part)
            subjects.append({"id": int(m.group(2)), "name": m.group(1)} if m else part)
        series = []
        t = self.info_series
        for r in range(t.rowCount()):
            cell = lambda c: (t.item(r, c).text().strip() if t.item(r, c) else "")
            if not cell(0) and not cell(1):
                continue
            item = dict(t.item(r, 0).data(Qt.ItemDataRole.UserRole) or {}) if t.item(r, 0) else {}
            item.update(name=cell(0), formula=cell(1), panel=cell(2) or 1, format=cell(3) or "auto")
            series.append(item)
        refs = []
        for part in self.info_refs.text().split(","):
            part = part.strip()
            if not part:
                continue
            m = re.fullmatch(r"(\d+)\s*:\s*(.+?)(?:\s*=\s*(-?[\d.]+))?", part)
            if not m:
                raise infoquery.QueryError(f"reference line “{part}”: write it as panel: value, e.g. 2: .400")
            label, value = m.group(2).strip(), m.group(3)
            number = float(value) if value else _ref_value(label)
            if number is None:
                raise infoquery.QueryError(f"reference line “{part}”: {label!r} isn't a number")
            refs.append({"panel": int(m.group(1)), "value": number, "label": label})
        source = self.info_source.currentData()
        season = self.info_season.value() if source != "player-seasons" else None
        if source == "player-games" and self.info_career.isChecked():
            season = infoquery.CAREER
        extra = {}
        if source in infoquery.RANKED:  # positions / leagues / distinct / pitchers / order / each are
            extra = {"season_to": self.info_season_to.value(),  # file-only: kept as they were
                     "rank_by": self.info_rank_by.text().strip(),
                     "top": self.info_top.value(), "positions": old.get("positions"),
                     "leagues": old.get("leagues"), "distinct": old.get("distinct", False),
                     "pitchers": old.get("pitchers", False), "order": old.get("order"), "each": old.get("each")}
        return {**extra, "name": self.info_name.text().strip(), "title": old.get("title", ""),
                "description": old.get("description", ""), "source": source, "subjects": subjects,
                "season": season, "where": [w.strip() for w in self.info_where.text().split(";") if w.strip()],
                "postseason": self.info_post.isChecked(), "x": self.info_x.currentData(),
                "running": self.info_running.isChecked(), "series": series, "chart": old.get("chart", "lines"),
                "panels": {str(k): v for k, v in (old.get("panels") or {}).items()}, "reference_lines": refs}

    def _apply_info_form(self, quiet: bool = False) -> bool:
        """Store the editor's changes into the selected query.  False (and says why) if they
        don't make a valid query."""
        row = self.info_list.currentRow()
        if not self._info_dirty or not 0 <= row < len(self._info_queries):
            return True
        try:
            q = infoquery.parse_query(self._info_form_object())
        except infoquery.QueryError as exc:
            if not quiet:
                QMessageBox.warning(self, "Info query", str(exc))
            self._say("info", f"Not applied: {exc}")
            return quiet
        other = [x["name"] for i, x in enumerate(self._info_queries) if i != row]
        if q["name"] in other:
            QMessageBox.warning(self, "Info query", f"There's already a query called “{q['name']}”.")
            return False
        old_name = self._info_queries[row]["name"]
        self._info_queries[row] = q
        self._info_results.pop(old_name, None)
        self._info_dirty = False
        self._refresh_info_lists(q["name"])
        return True

    # -- running ----------------------------------------------------------------
    def _graph_info(self):
        self._run_info(graph=True)

    def _run_info(self, graph: bool):
        if not self._apply_info_form():
            return
        row = self.info_list.currentRow()
        if 0 <= row < len(self._info_queries):
            self._run_info_named(self._info_queries[row]["name"], graph=graph, force=True)

    def _run_info_named(self, name: str, graph: bool, force: bool = False):
        q = next((x for x in self._info_queries if x["name"] == name), None)
        if q is None:
            return
        if graph:
            self.ig_combo.setCurrentIndex(self.ig_combo.findText(name))
            self.tabs.setCurrentIndex(self._tab_keys.index("ig"))
        if not force and name in self._info_results:
            self._show_info_result(name)
            return
        self._say("info", f"Running “{name}”…")
        self._say("ig", f"Running “{name}”…")
        self.ig_chart.set_result(None, f"Fetching the data for “{name}”…")
        custom, now = self._custom_stats_for("player"), self._season_now

        def fn():
            return name, infoquery.run_query(q, custom, now, LIVE_TTL)

        self._run_latest("info_run", fn, self._on_info_ran, self._on_info_failed)

    def _on_info_ran(self, data):
        name, result = data
        self._info_results[name] = result
        self._show_info_result(name)
        n = sum(len(s["points"]) for s in result["series"])
        msg = f"“{name}”: {len(result['series'])} line(s), {n} value(s)"
        self._say("info", msg)
        self._say("ig", msg)

    def _on_info_failed(self, message: str):
        self._say("info", f"Info: {message}")
        self._say("ig", f"Infograph: {message}")
        self.info_result_note.setText(f"Couldn't run that: {message}")
        self.ig_chart.set_result(None, f"Couldn't run that: {message}")

    def _show_info_result(self, name: str):
        result = self._info_results.get(name)
        if result is None:
            return
        cur = self.info_list.currentItem()
        if cur is not None and cur.text() == name:
            self._fill_info_table(result)
        if self.ig_combo.currentText() == name:
            q = next((x for x in self._info_queries if x["name"] == name), {})
            notes = " ".join(result["notes"])
            self.ig_desc.setText(" ".join(x for x in (q.get("description", ""), notes) if x))
            self.ig_desc.setVisible(bool(self.ig_desc.text()))
            self.ig_chart.set_result(result, "Nothing to graph — the query found no values.")

    def _fill_info_table(self, result: dict):
        headers, rows = infoquery.result_table(result)
        model = self.info_model
        model.clear()
        model.setHorizontalHeaderLabels(headers)
        for row in rows:
            items = []
            for i, (text, detail) in enumerate(row):
                item = self._new_item(text, numeric=i > 0)
                if detail:
                    item.setToolTip(detail)
                items.append(item)
            model.appendRow(items)
        self.info_table.resizeColumnsToContents()
        notes = " ".join(result["notes"])
        self.info_result_note.setText(
            f"<b>{html.escape(result['title'])}</b> — {len(rows)} row(s); hover a value for its game or season."
            + (f"<br>{html.escape(notes)}" if notes else ""))

    # -- infograph --------------------------------------------------------------
    def _ig_tab_shown(self):
        name = self.ig_combo.currentText()
        if name and name not in self._info_results:
            self._run_info_named(name, graph=False)
        elif name:
            self._show_info_result(name)

    def _on_ig_chosen(self, _index: int):
        self._run_info_named(self.ig_combo.currentText(), graph=False)

    def _ig_edit(self):
        name = self.ig_combo.currentText()
        names = [q["name"] for q in self._info_queries]
        if name in names:
            self.info_list.setCurrentRow(names.index(name))
        self.tabs.setCurrentIndex(self._tab_keys.index("info"))

    def _save_infograph(self):
        name = self.ig_combo.currentText() or "infograph"
        slug = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
        path, _ = QFileDialog.getSaveFileName(self, "Save image", os.path.join(HOME, f"{slug}.png"),
                                              "PNG images (*.png)")
        if path:
            if self.ig_chart.grab().save(path):
                self._say("ig", f"Saved {path}")
            else:
                QMessageBox.warning(self, "Save failed", f"Couldn't write {path}")

    # ---- status bar ---------------------------------------------------------
    def _current_tab_key(self) -> str:
        return self._tab_keys[self.tabs.currentIndex()] if self.tabs is not None else "stats"

    # ---- Uptodate tab ----------------------------------------------------
    def _build_uptodate_tab(self) -> QWidget:
        """This year's play-by-play from MLB's games, laid out as Retrosheet's season file, for the tabs
        that read Retrosheet until Retrosheet publishes the season (:mod:`uptodate`)."""
        self._up_job = None
        self._up_published: dict[int, bool] = {}  # season -> Retrosheet has its own file (asked once)
        year = _dt.date.today().year
        intro = QLabel(
            f"<b>This year's play-by-play, before Retrosheet has it.</b>  Retrosheet publishes a season's "
            f"play-by-play after the season ends.  <b>Update</b> builds {year}'s from MLB's finished games "
            f"(regular season, postseason and the All-Star Game) in the layout of Retrosheet's own file -- "
            f"the seven CSV files of <i>{year}csvs.zip</i> -- and later adds only the games it doesn't have "
            f"yet.  Until Retrosheet publishes {year}, the tabs that read Retrosheet use it: Players Batted In, "
            f"Bullpen Support, Speed, Safe/Out, the Simulator and the Retrosheet variables of Custom and "
            f"Regression.  Once Retrosheet's file is out they use Retrosheet's again, and this one can be "
            f"deleted.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        self.up_update = QPushButton(f"Update {year}")
        self.up_update.setToolTip(f"Download {year}'s finished games that the file doesn't have yet "
                                  "(the first time, every game so far: a few minutes)")
        self.up_update.clicked.connect(self._up_start)
        self.up_stop = QPushButton("Stop")
        self.up_stop.setToolTip("Stop after the games already under way; what's been downloaded is kept")
        self.up_stop.setEnabled(False)
        self.up_stop.clicked.connect(self._up_cancel)
        self.up_progress = QProgressBar()
        self.up_progress.setTextVisible(False)
        self.up_progress.setVisible(False)
        self.up_count = QLabel("")
        top = QHBoxLayout()
        top.addWidget(self.up_update)
        top.addWidget(self.up_stop)
        top.addWidget(self.up_progress, 1)
        top.addWidget(self.up_count)

        self.up_files = QTreeWidget()
        self.up_files.setRootIsDecorated(False)
        self.up_files.setHeaderLabels(["Season", "Games", "Through", "Updated", "Size", "Retrosheet's own file"])
        self.up_files.header().setStretchLastSection(True)
        self.up_files.itemSelectionChanged.connect(self._up_selection_changed)
        self.up_open = QPushButton("Open folder")
        self.up_open.clicked.connect(self._up_open_folder)
        self.up_copy = QPushButton("Save a copy…")
        self.up_copy.setToolTip("Copy the file somewhere else (it's a zip of Retrosheet-style CSV files)")
        self.up_copy.clicked.connect(self._up_save_copy)
        self.up_delete = QPushButton("Delete…")
        self.up_delete.setToolTip("Delete the file -- once Retrosheet has published the season, the tabs use "
                                  "Retrosheet's own")
        self.up_delete.clicked.connect(self._up_delete_file)
        buttons = QHBoxLayout()
        buttons.addWidget(self.up_open)
        buttons.addWidget(self.up_copy)
        buttons.addWidget(self.up_delete)
        buttons.addStretch(1)
        self.up_where = QLabel()
        self.up_where.setWordWrap(True)
        self.up_where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box = QGroupBox("Files on disk")
        blay = QVBoxLayout(box)
        blay.addWidget(self.up_files, 1)
        blay.addLayout(buttons)
        blay.addWidget(self.up_where)

        notes = QLabel(
            "<small>How it's made: the counts (hits, outs, runners before and after, who scored and who drove "
            "them in, earned runs, putouts and assists, lineups and fielders) come straight from MLB's "
            "play-by-play; the event text is written in Retrosheet's notation from it, and where a ball went "
            "(<i>loc</i>) from MLB's hit coordinates on Retrosheet's hit-location diagram, so it is an "
            "estimate.  Checked against Retrosheet's 2025 file over 200 games: the counting columns agree "
            "99.5-100%, the event text 94% and the hit locations 93%.  Ids are Retrosheet's (from MLB's "
            "cross-reference and Retrosheet's biographical file); a rookie Retrosheet hasn't listed yet "
            "gets one made up the same way, numbered from 999 down.  Data: MLB Stats API "
            "(statsapi.mlb.com, for personal use).  Player, park and umpire ids: the information used here "
            "was obtained free of charge from and is copyrighted by Retrosheet.  Interested parties may "
            "contact Retrosheet at \u201cwww.retrosheet.org\u201d.</small>")
        notes.setWordWrap(True)
        notes.setTextFormat(Qt.TextFormat.RichText)
        self.up_status = QLabel("")
        self.up_status.setWordWrap(True)

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(intro)
        lay.addLayout(top)
        lay.addWidget(self.up_status)
        lay.addWidget(box, 1)
        lay.addWidget(notes)
        self._up_refresh_files()
        return page

    def _up_files_on_disk(self) -> list[tuple[int, str]]:
        """(season, path) of every Uptodate file, newest season first."""
        folder = uptodate.data_dir()
        out = []
        try:
            for name in os.listdir(folder):
                m = re.fullmatch(r"(\d{4})csvs\.zip", name)
                if m:
                    out.append((int(m.group(1)), os.path.join(folder, name)))
        except OSError:
            pass
        return sorted(out, reverse=True)

    def _up_refresh_files(self):
        keep = self._up_selected()
        self.up_files.clear()
        year = _dt.date.today().year
        for season, path in self._up_files_on_disk():
            info = uptodate.file_summary(path) or {}
            published = self._up_published.get(season)
            own = ("published -- the tabs use it; this file can be deleted" if published
                   else "not yet" if published is False else "…")
            size = info.get("size", 0)
            item = QTreeWidgetItem([str(season), f"{info.get('games', 0):,}",
                                    info.get("last_date", "") or "—", info.get("updated", "") or "—",
                                    f"{size / 1e6:.1f} MB" if size else "—",
                                    "can't read the file" if info.get("broken") else own])
            item.setData(0, Qt.ItemDataRole.UserRole, (season, path))
            for col in (1, 4):
                item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.up_files.addTopLevelItem(item)
            if (keep and keep[0] == season) or (not keep and season == year):
                self.up_files.setCurrentItem(item)
        for col in range(5):
            self.up_files.resizeColumnToContents(col)
        if not self.up_files.topLevelItemCount():
            self.up_where.setText(f"No file yet.  {year}'s will be saved as {uptodate.default_path(year)}")
        self._up_selection_changed()

    def _up_selected(self):
        item = self.up_files.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _up_selection_changed(self):
        chosen = self._up_selected()
        busy = self._up_job is not None
        for b in (self.up_open, self.up_copy):
            b.setEnabled(chosen is not None)
        self.up_delete.setEnabled(chosen is not None and not busy)
        if chosen:
            self.up_where.setText(f"Saved as {chosen[1]}")

    def _up_tab_shown(self):
        self._up_refresh_files()
        for season, _path in self._up_files_on_disk():
            if season not in self._up_published:
                self._up_published[season] = None
                self._run_latest(f"up_published_{season}", lambda s=season: (s, uptodate.retrosheet_has(s)),
                                 self._up_on_published, lambda _msg: None)

    def _up_on_published(self, result):
        season, published = result
        self._up_published[season] = published
        if published:
            retrosheet._PUBLISHED[season] = True  # the other tabs switch to Retrosheet's own file
        self._up_refresh_files()

    def _up_start(self):
        if self._up_job is not None:
            return
        year = _dt.date.today().year
        job = _UptodateJob(year, uptodate.default_path(year))
        job.sig.progress.connect(self._up_on_progress)
        job.sig.done.connect(self._up_on_done)
        job.sig.failed.connect(self._up_on_failed)
        self._up_job = job
        self.up_update.setEnabled(False)
        self.up_stop.setEnabled(True)
        self.up_delete.setEnabled(False)
        self.up_progress.setRange(0, 0)  # busy until the schedule is in
        self.up_progress.setVisible(True)
        self.up_count.setText("")
        self.up_status.setText(f"Getting MLB's {year} schedule…")
        self._say("up", f"Uptodate: updating {year}…")
        self._start_job(job)

    def _up_cancel(self):
        if self._up_job is not None:
            self._up_job.stop.set()
            self.up_stop.setEnabled(False)
            self.up_status.setText("Stopping after the games already under way…")

    def _up_on_progress(self, done: int, total: int, text: str):
        if total:
            self.up_progress.setRange(0, total)
            self.up_progress.setValue(done)
            self.up_count.setText(f"{done:,} of {total:,} downloaded, {total - done:,} left")
        self.up_status.setText(text)
        self._say("up", f"Uptodate: {text}")

    def _up_finish(self):
        self._up_job = None
        self.up_update.setEnabled(True)
        self.up_stop.setEnabled(False)
        self.up_progress.setVisible(False)
        self._up_refresh_files()

    def _up_on_done(self, result: dict):
        self._up_finish()
        parts = []
        if result.get("cancelled"):
            parts.append("Stopped.")
        added = result.get("added", 0)
        parts.append(f"Added {added:,} game{'s' if added != 1 else ''}; the file now holds "
                     f"{result.get('games', 0):,}, through {result.get('last_date') or '—'}."
                     if added else f"Already up to date: {result.get('games', 0):,} games, through "
                     f"{result.get('last_date') or '—'}.")
        failed = result.get("failed") or []
        if failed:
            parts.append(f"{len(failed)} game{'s' if len(failed) != 1 else ''} couldn't be read and will be "
                         f"tried again next time (game {failed[0][0]}: {failed[0][1]}).")
        if result.get("made"):
            parts.append(f"{result['made']} player id{'s' if result['made'] != 1 else ''} made up for people "
                         "Retrosheet hasn't listed yet.")
        self.up_count.setText("")
        self.up_status.setText("  ".join(parts))
        self._say("up", "Uptodate: " + parts[-1] if failed else "Uptodate: " + "  ".join(parts))

    def _up_on_failed(self, message: str):
        self._up_finish()
        self.up_count.setText("")
        self.up_status.setText(f"Couldn't update: {message}  (Whatever was downloaded before the problem "
                               f"has been kept.)")
        self._say("up", f"Uptodate: couldn't update: {message}")

    def _up_open_folder(self):
        chosen = self._up_selected()
        if chosen:
            from PyQt6.QtCore import QUrl
            from PyQt6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(chosen[1])))

    def _up_save_copy(self):
        chosen = self._up_selected()
        if not chosen:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save a copy", os.path.join(HOME, os.path.basename(chosen[1])),
                                              "Zip files (*.zip)")
        if not path:
            return
        import shutil
        try:
            shutil.copyfile(chosen[1], path)
            self.up_status.setText(f"Copied to {path}")
        except OSError as exc:
            QMessageBox.warning(self, "Save a copy", f"Couldn't copy the file: {exc}")

    def _up_delete_file(self):
        chosen = self._up_selected()
        if not chosen or self._up_job is not None:
            return
        season, path = chosen
        published = self._up_published.get(season)
        why = (f"Retrosheet has published {season}, so the tabs use Retrosheet's own file now."
               if published else
               f"Retrosheet hasn't published {season} yet: until it does, the tabs that read Retrosheet will "
               f"have nothing for {season} (Update makes the file again, downloading every game).")
        answer = QMessageBox.question(self, "Delete file", f"Delete {path}?\n\n{why}",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            uptodate.delete_file(path)
        except uptodate.UptodateError as exc:
            QMessageBox.warning(self, "Delete file", str(exc))
            return
        self.up_status.setText(f"Deleted {path}.")
        self._say("up", f"Uptodate: deleted the {season} file")
        self._up_refresh_files()

    def _say(self, tab: str, text: str):
        """A status-bar message belonging to one tab (a key of ``_tab_keys``).  The bar
        shows only what the tab on screen has to say: another tab's message is kept
        for when it is next shown, not put up over this one."""
        self._tab_status[tab] = text
        if tab == self._current_tab_key() and not self._downloads:  # (a download's notice stays up till it ends)
            self.statusBar().showMessage(text)

    def _on_download(self, what: str, got: int, total: int, done: bool):
        """Say what is being downloaded (and how far it has got) on the status bar; once nothing is, put back the
        tab's own message."""
        d = self._downloads.setdefault(what, [0, 0, -1])
        if done:
            d[0] -= 1
            if d[0] <= 0:
                del self._downloads[what]
        elif got == 0:
            d[0] += 1
        else:
            d[1], d[2] = got, total
        if not self._downloads:
            self.statusBar().showMessage(self._tab_status.get(self._current_tab_key(), ""))
            return
        parts = []
        for name, (_n, so_far, size) in self._downloads.items():
            if so_far and size > 0:
                parts.append(f"{name}: {so_far / 1e6:.1f} of {size / 1e6:.1f} MB")
            elif so_far:
                parts.append(f"{name}: {so_far / 1e6:.1f} MB")
            else:
                parts.append(name)
        once = any(name.startswith("Retrosheet") for name in self._downloads)
        self.statusBar().showMessage("Downloading " + "; ".join(parts) + "\u2026"
                                     + (" (once: it's kept on disk)" if once else ""))

    def _show_tab_status(self, index: int):
        if not self._downloads:
            self.statusBar().showMessage(self._tab_status.get(self._tab_keys[index], ""))

    # ---- worker plumbing ------------------------------------------------
    def _start_job(self, job: QRunnable):
        """Run ``job`` on the thread pool, held until it has finished (see :class:`_Held`)."""
        held = _Held(job, self._job_release)
        self._live_jobs.add(held)
        self.pool.start(held)

    def _run(self, fn, on_ok, on_err=None):
        self._token += 1
        job = _Job(self._token, fn)
        job.sig.ok.connect(lambda tok, res, cb=on_ok: tok == self._token and cb(res))
        job.sig.err.connect(
            lambda tok, msg, cb=on_err or self._on_error: tok == self._token and cb(msg)
        )
        self._start_job(job)

    def _run_latest(self, tag: str, fn, on_ok, on_err):
        """Like ``_run``, but a result is only dropped when a newer call with the
        *same tag* has started -- not when anything else in the app has.
        ``_run`` shares one counter across every tab, which is fine for one-at-a-
        time loads but would have the Head-To-Head tab's batter and pitcher
        rosters (loading side by side) cancel each other."""
        self._tag_tokens[tag] = self._tag_tokens.get(tag, 0) + 1
        job = _Job(self._tag_tokens[tag], fn)
        job.sig.ok.connect(lambda tok, res: tok == self._tag_tokens[tag] and on_ok(res))
        job.sig.err.connect(lambda tok, msg: tok == self._tag_tokens[tag] and on_err(msg))
        self._start_job(job)

    def _on_team_refresh_failed(self, owner: str, message: str):
        """A tab's team drop-down couldn't be re-listed for its season -- not
        worth interrupting anything; the existing list stays as it was."""
        self._say(owner, f"Couldn't refresh the team list: {message}")

    def _set_enabled(self, on: bool):
        for w in (self.team_combo, self.season_spin, self.view_combo, self.refresh_btn):
            w.setEnabled(on)
        has_rows = on and bool(self._csv_rows)
        self.csv_btn.setEnabled(has_rows)
        self.book_btn.setEnabled(on and bool(self._teams))

    # ---- bootstrap ----------------------------------------------------
    @staticmethod
    def _bootstrap():
        season = mlbstats.current_season()
        teams = mlbstats.get_teams(season, ttl=24 * 3600)
        return season, teams

    def _on_bootstrap(self, data):
        self._season_now, teams = data
        self._team_by_id = {t["id"]: t for t in teams}
        self._teams = sorted(teams, key=lambda t: t["name"])
        self._suspend = True
        self.team_combo.clear()
        for t in self._teams:
            self.team_combo.addItem(t["name"], t["id"])
        self.season_spin.setRange(1876, self._season_now)
        self.season_spin.setValue(self._season_now)
        self._suspend = False
        self._set_enabled(True)
        self._apply_team_range()
        self._reload()
        self._ts_setup()

        # Fill in the team and season first, without each change starting a load of its own (adding the
        # first team used to load the spin box's starting year, 1876, before the season was set), then
        # load the table once, for the season shown.
        self.custom_team_combo.blockSignals(True)
        self.custom_team_combo.clear()
        for t in self._teams:
            self.custom_team_combo.addItem(t["name"], t["id"])
        self.custom_team_combo.blockSignals(False)
        self.custom_season_spin.setRange(1876, self._season_now)
        self.custom_season_spin.blockSignals(True)
        self.custom_season_spin.setValue(self._season_now)
        self.custom_season_spin.blockSignals(False)
        self._update_custom_insert_combo()
        self._refresh_custom_defs_list()
        self.custom_scope.sync_division(self.custom_season_spin.value())
        self._reload_custom_table()

        self.corr_team_combo.clear()
        for t in self._teams:
            self.corr_team_combo.addItem(t["name"], t["id"])
        self.corr_season_spin.setRange(1876, self._season_now)
        self.corr_season_spin.setValue(self._season_now)
        self.corr_run_btn.setEnabled(True)

        self.reg_team_combo.clear()
        for t in self._teams:
            self.reg_team_combo.addItem(t["name"], t["id"])
        self.reg_season_spin.setRange(1876, self._season_now)
        self.reg_season_spin.setValue(self._season_now)
        self.reg_run_btn.setEnabled(True)

        self.fa_team_combo.clear()
        for t in self._teams:
            self.fa_team_combo.addItem(t["name"], t["id"])
        self.fa_season_spin.setRange(1876, self._season_now)
        self.fa_season_spin.setValue(self._season_now)
        self.fa_run_btn.setEnabled(True)

        self.personal_picker.set_teams(self._teams, self._season_now)
        self.pscore_picker.set_teams(self._teams, self._season_now)
        self.daily_picker.set_teams(self._teams, self._season_now)
        self.h2h_batter.set_teams(self._teams, self._season_now)
        self.h2h_pitcher.set_teams(self._teams, self._season_now)
        self.range_picker.set_teams(self._teams, self._season_now)
        self.prod_picker.set_teams(self._teams, self._season_now)
        self.prev_picker.set_teams(self._teams, self._season_now)
        # Retrosheet publishes a season after it is over, so start on the last completed one
        self.speed_picker.set_teams(self._teams, self._season_now, start=self._season_now - 1)
        # the Simulator's runners move as Retrosheet recorded: start on the last completed season too
        self.sim_vis.set_teams(self._teams, self._season_now, start=self._season_now - 1)
        self.sim_home.set_teams(self._teams, self._season_now, start=self._season_now - 1)
        if self.sim_home.team_combo.count() > 1:
            self.sim_home.team_combo.setCurrentIndex(1)
        # Safe/Out reads the same Retrosheet play-by-play: the last completed season
        self.so_season_spin.setRange(1901, self._season_now)
        self.so_season_spin.blockSignals(True)
        self.so_season_spin.setValue(self._season_now - 1)
        self.so_season_spin.blockSignals(False)
        self._reload_safe_out()
        ld_season = self._ld["yearly"]["season"]
        ld_season.blockSignals(True)
        self._ld_season_range()
        ld_season.setValue(self._season_now)
        ld_season.blockSignals(False)
        self.il_season_spin.setRange(mlbstats.INTERDIVISION_FIRST_SEASON, self._season_now)
        self.il_season_spin.blockSignals(True)
        self.il_season_spin.setValue(self._season_now)
        self.il_season_spin.blockSignals(False)
        self._reload_interleague()
        self.st_season_spin.setRange(1876, self._season_now)
        self.st_season_spin.blockSignals(True)
        self.st_season_spin.setValue(self._season_now)
        self.st_season_spin.blockSignals(False)
        self._st_sync_wildcard(self.st_season_spin.value())
        self._st_apply_names({t["id"]: t for t in self._teams})
        self._reload_standings()
        self.ps_season_spin.setRange(mlbstats.EARLY_POSTSEASON_FIRST_SEASON, self._season_now)
        self.ps_season_spin.blockSignals(True)
        self.ps_season_spin.setValue(self._season_now)
        self.ps_season_spin.blockSignals(False)
        self._reload_postseason()
        self.pst_season_spin.setRange(mlbstats.EARLY_POSTSEASON_FIRST_SEASON, self._season_now)
        self.pst_season_spin.blockSignals(True)
        self.pst_season_spin.setValue(self._season_now)
        self.pst_season_spin.blockSignals(False)
        self._pst_load_catalog()
        self._gd_setup()

    # ---- reload -----------------------------------------------------
    def _current_team(self) -> dict | None:
        tid = self.team_combo.currentData()
        for t in self._teams:
            if t["id"] == tid:
                return t
        return self._team_by_id.get(tid)

    def _apply_team_range(self):
        team = self._current_team()
        if not team:
            return
        low = max(1876, team.get("first_year") or 1876)
        self._suspend = True
        self.season_spin.setMinimum(low)
        self._suspend = False
        era = self._era_name(team["id"], self.season_spin.value())
        name = era["name"] if era else team["name"]
        self._say("stats", f"{name} · seasons {low}–{self._season_now}")

    def _queue_reload(self):
        if not self._suspend:
            self._debounce.start()

    def _team_changed(self, _idx):
        if self._suspend:
            return
        self._apply_team_range()
        self._debounce.start()

    def _reload(self, force: bool = False):
        team = self._current_team()
        if self._suspend or not team:
            return
        season = self.season_spin.value()
        view = self.view_combo.currentText()
        ttl = 0 if force else (LIVE_TTL if season >= self._season_now else None)
        team_id = team["id"]
        self._say("stats", f"Loading {team['name']} {season} — {view}…")

        if view == "Team totals":
            def payload():
                return ("team", {
                    g: mlbstats.get_team_totals(team_id, season, g, ttl)
                    for g in ("hitting", "pitching", "fielding")
                })
        else:
            group, stat_type, _cols = mlbstats.VIEW_SPECS[view]

            def payload():
                return ("players", view,
                        mlbstats.get_player_stats(team_id, season, group, ttl, stat_type=stat_type))

        def fn():
            return {
                "season": season,
                "names": mlbstats.get_team_map(season, ttl),
                "payload": payload(),
            }

        self._run(fn, self._on_data)

    # ---- render ----------------------------------------------------
    def _era_name(self, team_id: int, season: int) -> dict | None:
        """The record for ``team_id`` as named in ``season`` (cached maps only)."""
        return self._season_names.get(season, {}).get(team_id)

    def _rebuild_teams(self, names: dict, keep_id) -> None:
        """Replace the dropdown with the league *as it stood in this season*.

        The currently-selected franchise stays selected (it is guaranteed to
        exist in the season, since the year spinner is clamped to its debut).
        If somehow it isn't in this season's league — e.g. a folded club walked
        forward past its last year — the list is left as-is so the selection is
        never yanked out from under the user.
        """
        if not names or keep_id not in names:
            return
        teams = sorted(names.values(), key=lambda t: t["name"])
        self._suspend = True
        self.team_combo.clear()
        target = 0
        for i, t in enumerate(teams):
            self.team_combo.addItem(t["name"], t["id"])
            if t["id"] == keep_id:
                target = i
        self.team_combo.setCurrentIndex(target)
        self._suspend = False
        self._teams = teams

    def _on_data(self, result):
        season = result["season"]
        names = result["names"]
        self._season_names[season] = names
        keep_id = self.team_combo.currentData()
        self._rebuild_teams(names, keep_id)

        self._era_record = (names.get(keep_id) or self._team_by_id.get(keep_id)
                            or self._current_team())
        label = self._era_record["name"] if self._era_record else "?"

        payload = result["payload"]
        if payload[0] == "players":
            _, view, rows = payload
            self._fill_players(view, rows)
            note = f"{len(rows)} row{'s' * (len(rows) != 1)}"
        else:
            self._fill_team(payload[1])
            note = "team totals"
        self._say("stats", f"{label} · {season} · {note}")
        self._set_enabled(True)

    def _new_item(self, value, numeric=True):
        # Sabermetric fields arrive as raw floats (e.g. war=1.8067432); round
        # for display while the full-precision value still drives sorting.
        text = f"{value:.3f}" if isinstance(value, float) else ("" if value is None else str(value))
        item = QStandardItem(text)
        item.setEditable(False)
        if isinstance(value, mlbstats.SampledValue):
            item.setText(text + "*")
            item.setToolTip(f"From Retrosheet\u2019s play-by-play of only {value.coverage:.0%} of the "
                            "team\u2019s innings: a sample of the season")
        num = mlbstats.as_number(value) if numeric else None
        item.setData(num if num is not None else str(value).lower(),
                     Qt.ItemDataRole.UserRole)
        if numeric:
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return item

    def _fill_players(self, view: str, rows: list[dict]):
        cols = mlbstats.VIEW_SPECS[view][2]
        self.model.clear()
        self.model.setHorizontalHeaderLabels([h for h, _ in cols])
        for row in rows:
            items = [self._new_item(row.get(key, ""), numeric=(key not in ("_name", "_pos")))
                     for _, key in cols]
            self.model.appendRow(items)
        self.table.resizeColumnsToContents()
        self._csv_headers = [h for h, _ in cols]
        self._csv_rows = [[str(r.get(k, "")) for _, k in cols] for r in rows]

    def _fill_team(self, groups: dict):
        self.model.clear()
        self.model.setHorizontalHeaderLabels(["Statistic", "Value"])
        self._csv_headers = ["Statistic", "Value"]
        self._csv_rows = []
        for title, key in (("Batting", "hitting"), ("Pitching", "pitching"),
                           ("Fielding", "fielding")):
            head = self._new_item(f"— {title} —", numeric=False)
            self.model.appendRow([head, self._new_item("", numeric=False)])
            self._csv_rows.append([f"-- {title} --", ""])
            for label, stat in mlbstats.TEAM_FIELDS[key]:
                value = groups.get(key, {}).get(stat, "")
                self.model.appendRow([self._new_item(label, numeric=False),
                                      self._new_item(value)])
                self._csv_rows.append([label, str(value)])
        self.table.resizeColumnsToContents()

    def _on_error(self, message: str):
        self._say("stats", f"Error: {message}")
        self._set_enabled(True)

    # ---- exports --------------------------------------------------
    def _default_name(self, suffix: str) -> str:
        rec = self._era_record or self._current_team() or {}
        abbr = (rec.get("abbrev") or rec.get("name") or "mlb").replace(" ", "-")
        view = self.view_combo.currentText().lower().replace(" ", "-")
        return os.path.join(HOME, f"{abbr}_{self.season_spin.value()}_{view}.{suffix}")

    def _export_csv(self):
        if not self._csv_rows:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", self._default_name("csv"), "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(self._csv_headers)
                writer.writerows(self._csv_rows)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self._say("stats", f"Saved {path}")

    def _export_workbook(self):
        team = self._current_team()
        if not team:
            return
        view = self.view_combo.currentText()
        kind = "team" if view == "Team totals" else "players"
        start = max(1876, team.get("first_year") or 1876)
        seasons = list(range(start, self._season_now + 1))

        msg = (f"Build a workbook for the {team['name']} covering "
               f"{start}–{self._season_now} ({len(seasons)} seasons)?\n\n"
               f"View: {view}\n"
               "Seasons already cached are instant; the rest are one request each.")
        if QMessageBox.question(self, "Export workbook", msg) != QMessageBox.StandardButton.Yes:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export workbook", self._default_name("xlsx"), "Excel workbook (*.xlsx)")
        if not path:
            return

        dlg = QProgressDialog("Starting…", "Cancel", 0, len(seasons), self)
        dlg.setWindowTitle("Exporting")
        dlg.setMinimumDuration(0)
        dlg.setValue(0)

        job = _ExportJob(kind, team["id"], seasons, view, path, self._season_now)
        self._export_job = job  # keep a reference alive

        def on_progress(i, total, label):
            dlg.setMaximum(total)
            dlg.setValue(i)
            dlg.setLabelText(f"Fetching {label}…  ({i}/{total})")

        def finish(saved):
            dlg.reset()
            self._export_job = None
            self._say("stats", f"Saved {saved}")
            QMessageBox.information(self, "Export complete", f"Workbook saved to:\n{saved}")

        def fail(err):
            dlg.reset()
            self._export_job = None
            self._say("stats", f"Export: {err}")
            if err != "Cancelled.":
                QMessageBox.warning(self, "Export failed", err)

        job.sig.progress.connect(on_progress)
        job.sig.done.connect(finish)
        job.sig.failed.connect(fail)
        dlg.canceled.connect(job.cancel)
        self._start_job(job)

    def _clear_cache(self):
        removed = mlbstats.clear_cache()
        self._say(self._current_tab_key(), f"Cleared {removed} cached file(s)")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("BallSheet")
    # On GNOME/Wayland the shell finds a window's icon by matching its app_id
    # to an installed <app_id>.desktop file -- setWindowIcon() alone is ignored
    # there.  Qt derives the Wayland app_id (and the X11 WM_CLASS) from this.
    app.setDesktopFileName("ballsheet")
    if os.path.exists(ICON):
        from PyQt6.QtGui import QIcon
        app.setWindowIcon(QIcon(ICON))
    desktop = app.font().pointSizeF()
    app.setProperty("desktop_points", desktop)
    offset = _load_settings().get("text_offset", DEFAULT_TEXT_OFFSET)
    if isinstance(offset, (int, float)) and offset:
        font = app.font()
        font.setPointSizeF(max(MIN_TEXT_POINTS, desktop + offset))
        app.setFont(font)
    app.setStyle(_CompactRows())  # wraps the current style; keep after the font is set
    win = MainWindow()
    win.fit_to_screen()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
