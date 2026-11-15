#!/usr/bin/env python3
"""Ordinary least-squares multiple regression, standard library only.

No numpy/scipy: the normal-equation solve is plain nested lists, and the
t/F p-values come from the regularized incomplete beta function (the same
continued-fraction the Numerical Recipes ``betai`` uses), built on
``math.lgamma``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


class RegressionError(ValueError):
    """Not enough data, or the predictors are collinear."""


class _SingularColumn(Exception):
    """Internal: Gauss-Jordan hit a zero pivot at design-matrix column ``index``
    (0 = intercept, i = the (i-1)th predictor in ``labels`` order)."""

    def __init__(self, index: int):
        super().__init__(index)
        self.index = index


# --------------------------------------------------------------------------- #
#  Plain-list matrix algebra                                                  #
# --------------------------------------------------------------------------- #
def _transpose(m: list[list[float]]) -> list[list[float]]:
    return [list(row) for row in zip(*m)]


def _matmul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    bt = _transpose(b)
    return [[sum(x * y for x, y in zip(row, col)) for col in bt] for row in a]


def _matvec(a: list[list[float]], v: list[float]) -> list[float]:
    return [sum(x * y for x, y in zip(row, v)) for row in a]


def _invert(m: list[list[float]]) -> list[list[float]]:
    """Gauss-Jordan with partial pivoting.  Raises ``_SingularColumn`` (not
    ``RegressionError`` directly) so the caller can name the culprit."""
    n = len(m)
    aug = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot_row][col]) < 1e-10:
            raise _SingularColumn(col)
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        pivot = aug[col][col]
        aug[col] = [x / pivot for x in aug[col]]
        for r in range(n):
            if r != col and aug[r][col] != 0:
                factor = aug[r][col]
                aug[r] = [x - factor * y for x, y in zip(aug[r], aug[col])]
    return [row[n:] for row in aug]


# --------------------------------------------------------------------------- #
#  Collinearity diagnostics -- used only once a fit has actually failed.      #
# --------------------------------------------------------------------------- #
def _pearson(a: list[float], b: list[float], ma: float, mb: float) -> float | None:
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da == 0 or db == 0:
        return None
    return num / (da * db)


def diagnose_collinearity(x_rows: list[list[float]], labels: list[str],
                          singular_index: int | None, dup_threshold: float = 0.995) -> list[str]:
    """Best-effort, human-readable reasons the predictor set won't invert.

    Checked in order of how directly actionable they are: a predictor that
    never varies, then near-duplicate pairs (by correlation), then -- if
    neither turns anything up, e.g. a three-or-more-way exact dependency --
    the specific column where the elimination itself first broke down.
    Never raises; always returns at least one line.
    """
    n, k = len(x_rows), len(labels)
    suggestions = []
    if k and n:
        cols = [[row[j] for row in x_rows] for j in range(k)]
        means = [sum(c) / n for c in cols]

        for i, (col, mean) in enumerate(zip(cols, means)):
            spread = math.sqrt(sum((v - mean) ** 2 for v in col) / n)
            if spread < 1e-9 * (abs(mean) + 1.0):
                suggestions.append(
                    f'"{labels[i]}" is constant across this sample (no variation '
                    "for the regression to use) -- drop it.")

        pairs = []
        for i in range(k):
            for j in range(i + 1, k):
                r = _pearson(cols[i], cols[j], means[i], means[j])
                if r is not None and abs(r) >= dup_threshold:
                    pairs.append((labels[i], labels[j], r))
        pairs.sort(key=lambda t: -abs(t[2]))
        for a, b, r in pairs[:5]:
            suggestions.append(
                f'"{a}" and "{b}" move almost in lockstep across this sample '
                f"(r={r:.4f}) -- they're carrying the same information, drop one.")

    if not suggestions and singular_index:  # 0 = intercept, never the culprit here
        suggestions.append(
            f'"{labels[singular_index - 1]}" looks fully explained by a combination '
            "of the intercept and the other predictors picked before it in the "
            "list -- try dropping it.")

    if not suggestions:
        suggestions.append(
            "Couldn't pin down a specific variable -- try removing predictors one "
            "at a time, favoring a rate stat alongside the counting stats it's "
            "derived from (e.g. WAR alongside its RAR/wRAA components).")
    return suggestions


def _collinearity_message(x_rows: list[list[float]], labels: list[str],
                          singular_index: int) -> str:
    lines = ["The selected predictors are collinear -- no unique regression solution exists."]
    lines.append("")
    lines.extend(f"- {s}" for s in diagnose_collinearity(x_rows, labels, singular_index))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Regularized incomplete beta -> Student's t and F survival functions        #
# --------------------------------------------------------------------------- #
def _betacf(a: float, b: float, x: float, max_iter: int = 200, eps: float = 1e-12) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-300:
        d = 1e-300
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1e-300 if abs(d) < 1e-300 else d
        c = 1.0 + aa / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1e-300 if abs(d) < 1e-300 else d
        c = 1.0 + aa / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_bt = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    bt = math.exp(ln_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: float) -> float:
    """P(|T| > |t|) for a Student's t with ``df`` degrees of freedom."""
    if df <= 0 or t != t:  # nan
        return float("nan")
    x = df / (df + t * t)
    return _betai(df / 2.0, 0.5, x)


def f_p_value(f: float, df1: float, df2: float) -> float:
    """P(F > f) for an F distribution with (df1, df2) degrees of freedom."""
    if df1 <= 0 or df2 <= 0 or f != f:
        return float("nan")
    if f <= 0:
        return 1.0
    x = df2 / (df2 + df1 * f)
    return _betai(df2 / 2.0, df1 / 2.0, x)


# --------------------------------------------------------------------------- #
#  The regression itself                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class RegressionResult:
    labels: list[str]            # "Intercept" first, then one per predictor
    coefficients: list[float]
    std_errors: list[float]
    t_stats: list[float]
    p_values: list[float]
    n: int
    k: int                       # predictor count, excluding the intercept
    r_squared: float
    adj_r_squared: float
    f_stat: float
    f_p_value: float
    residual_std_error: float


def multiple_regression(y: list[float], x_rows: list[list[float]],
                        labels: list[str]) -> RegressionResult:
    """OLS of ``y`` on ``x_rows`` (one row per observation, no intercept column).

    Raises :class:`RegressionError` if there are too few observations or the
    predictors are collinear.
    """
    n = len(y)
    k = len(labels)
    if n <= k + 1:
        raise RegressionError(
            f"Need more observations than predictors: {k} predictor(s) need at "
            f"least {k + 2}, only {n} usable season(s) were found."
        )
    design = [[1.0] + list(row) for row in x_rows]
    xt = _transpose(design)
    xtx = _matmul(xt, design)
    try:
        xtx_inv = _invert(xtx)
    except _SingularColumn as exc:
        raise RegressionError(_collinearity_message(x_rows, labels, exc.index)) from None
    xty = _matvec(xt, y)
    beta = _matvec(xtx_inv, xty)

    y_hat = _matvec(design, beta)
    residuals = [yi - yhi for yi, yhi in zip(y, y_hat)]
    ss_res = sum(r * r for r in residuals)
    y_mean = sum(y) / n
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    df_resid = n - k - 1
    adj_r_squared = (1.0 - (1.0 - r_squared) * (n - 1) / df_resid
                     if df_resid > 0 else float("nan"))
    mse = ss_res / df_resid if df_resid > 0 else float("nan")
    residual_std_error = math.sqrt(mse) if mse == mse and mse >= 0 else float("nan")

    se = [math.sqrt(mse * xtx_inv[i][i]) if mse == mse and mse * xtx_inv[i][i] > 0 else 0.0
          for i in range(k + 1)]
    t_stats = [beta[i] / se[i] if se[i] > 0 else float("nan") for i in range(k + 1)]
    p_values = [t_two_sided_p(t_stats[i], df_resid) if df_resid > 0 else float("nan")
                for i in range(k + 1)]

    if k > 0 and df_resid > 0 and ss_res > 0:
        f_stat = ((ss_tot - ss_res) / k) / (ss_res / df_resid)
        f_pval = f_p_value(f_stat, k, df_resid)
    else:
        f_stat, f_pval = float("nan"), float("nan")

    return RegressionResult(
        labels=["Intercept"] + list(labels),
        coefficients=beta, std_errors=se, t_stats=t_stats, p_values=p_values,
        n=n, k=k, r_squared=r_squared, adj_r_squared=adj_r_squared,
        f_stat=f_stat, f_p_value=f_pval, residual_std_error=residual_std_error,
    )
