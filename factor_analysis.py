#!/usr/bin/env python3
"""Principal-axis factor analysis, standard library only.

Same spirit as ``regression.py``: no numpy/scipy.  The correlation matrix's
eigendecomposition uses the classic cyclic Jacobi algorithm (simple and
numerically stable for the small, dense, symmetric matrices a predictor list
produces); communalities are estimated by the standard iterative principal-
axis procedure (seeded with the squared multiple correlations); and factors
are rotated to an orthogonal simple structure with Kaiser's varimax.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


class FactorAnalysisError(ValueError):
    """Not enough data/variables, or no usable (positive-eigenvalue) factor."""


# --------------------------------------------------------------------------- #
#  Plain-list matrix algebra                                                  #
# --------------------------------------------------------------------------- #
def _invert(m: list[list[float]]) -> list[list[float]] | None:
    """Gauss-Jordan with partial pivoting.  ``None`` (not an exception) on a
    singular matrix -- callers here always have a fallback for that case."""
    n = len(m)
    aug = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot_row][col]) < 1e-10:
            return None
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        pivot = aug[col][col]
        aug[col] = [x / pivot for x in aug[col]]
        for r in range(n):
            if r != col and aug[r][col] != 0:
                factor = aug[r][col]
                aug[r] = [x - factor * y for x, y in zip(aug[r], aug[col])]
    return [row[n:] for row in aug]


def _jacobi_eigen(a: list[list[float]], max_sweeps: int = 100,
                  tol: float = 1e-12) -> tuple[list[float], list[list[float]]]:
    """Cyclic Jacobi eigendecomposition of a real symmetric matrix.

    Returns ``(eigenvalues, eigenvectors)``, sorted by eigenvalue descending,
    where ``eigenvectors[i][f]`` is the i-th variable's loading onto the f-th
    eigenvector (i.e. eigenvectors are columns).  Fine for the modest matrix
    sizes a predictor checklist produces -- no need for a faster QR-based
    solver here.
    """
    n = len(a)
    A = [row[:] for row in a]
    V = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _sweep in range(max_sweeps):
        off = sum(A[i][j] * A[i][j] for i in range(n) for j in range(n) if i != j)
        if off < tol:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                apq = A[p][q]
                if abs(apq) < 1e-300:
                    continue
                theta = (A[q][q] - A[p][p]) / (2.0 * apq)
                sign = 1.0 if theta >= 0 else -1.0
                t = sign / (abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                app, aqq = A[p][p], A[q][q]
                A[p][p] = c * c * app - 2.0 * s * c * apq + s * s * aqq
                A[q][q] = s * s * app + 2.0 * s * c * apq + c * c * aqq
                A[p][q] = A[q][p] = 0.0
                for i in range(n):
                    if i != p and i != q:
                        aip, aiq = A[i][p], A[i][q]
                        A[i][p] = A[p][i] = c * aip - s * aiq
                        A[i][q] = A[q][i] = s * aip + c * aiq
                for i in range(n):
                    vip, viq = V[i][p], V[i][q]
                    V[i][p] = c * vip - s * viq
                    V[i][q] = s * vip + c * viq
    eigenvalues = [A[i][i] for i in range(n)]
    order = sorted(range(n), key=lambda i: -eigenvalues[i])
    eigvals_sorted = [eigenvalues[i] for i in order]
    eigvecs_sorted = [[V[r][i] for i in order] for r in range(n)]
    return eigvals_sorted, eigvecs_sorted


def correlation_matrix(x_rows: list[list[float]]) -> list[list[float]]:
    """Pearson correlation matrix of the columns of ``x_rows`` (one row per
    observation).  A column with zero variance correlates as 0 with every
    other column (and 1 with itself) rather than dividing by zero."""
    n = len(x_rows)
    k = len(x_rows[0]) if n else 0
    cols = [[row[j] for row in x_rows] for j in range(k)]
    means = [sum(c) / n for c in cols]
    stds = [math.sqrt(sum((v - m) ** 2 for v in c) / n) for c, m in zip(cols, means)]
    r = [[0.0] * k for _ in range(k)]
    for i in range(k):
        for j in range(k):
            if i == j:
                r[i][j] = 1.0
            elif stds[i] == 0 or stds[j] == 0:
                r[i][j] = 0.0
            else:
                cov = sum((cols[i][x] - means[i]) * (cols[j][x] - means[j]) for x in range(n)) / n
                r[i][j] = cov / (stds[i] * stds[j])
    return r


def _smc(r: list[list[float]]) -> list[float]:
    """Squared multiple correlation of each variable against all the others
    -- the standard initial communality estimate for principal-axis
    factoring.  Falls back to each variable's largest single correlation
    (squared) if ``r`` is singular (e.g. two near-duplicate predictors)."""
    n = len(r)
    inv = _invert(r)
    if inv is not None:
        out = []
        for i in range(n):
            diag = inv[i][i]
            out.append(1.0 - 1.0 / diag if diag > 1e-10 else 0.0)
        return [min(max(v, 0.0), 0.9999) for v in out]
    return [min(max((max(abs(r[i][j]) for j in range(n) if j != i) if n > 1 else 0.0) ** 2,
                    0.0), 0.9999)
            for i in range(n)]


def _varimax(loadings: list[list[float]], max_iter: int = 100,
            tol: float = 1e-6) -> list[list[float]]:
    """Kaiser normalized varimax rotation: rotate factor pairs to maximize
    the variance of the squared loadings, i.e. push each variable toward
    loading strongly on as few factors as possible (orthogonal simple
    structure).  A no-op for fewer than two factors -- there is nothing to
    rotate against."""
    n = len(loadings)
    k = len(loadings[0]) if n else 0
    if k < 2 or n == 0:
        return [row[:] for row in loadings]
    h = [math.sqrt(sum(v * v for v in row)) or 1.0 for row in loadings]
    L = [[v / hi for v in row] for row, hi in zip(loadings, h)]
    for _ in range(max_iter):
        max_phi = 0.0
        for p in range(k - 1):
            for q in range(p + 1, k):
                u = [L[i][p] ** 2 - L[i][q] ** 2 for i in range(n)]
                v = [2.0 * L[i][p] * L[i][q] for i in range(n)]
                su, sv = sum(u), sum(v)
                num = sum(2.0 * ui * vi for ui, vi in zip(u, v)) - 2.0 * su * sv / n
                den = sum(ui * ui - vi * vi for ui, vi in zip(u, v)) - (su * su - sv * sv) / n
                phi = 0.25 * math.atan2(num, den)
                if abs(phi) < 1e-12:
                    continue
                c, s = math.cos(phi), math.sin(phi)
                for i in range(n):
                    lp, lq = L[i][p], L[i][q]
                    L[i][p] = c * lp + s * lq
                    L[i][q] = -s * lp + c * lq
                max_phi = max(max_phi, abs(phi))
        if max_phi < tol:
            break
    return [[L[i][f] * h[i] for f in range(k)] for i in range(n)]


# --------------------------------------------------------------------------- #
#  The factor analysis itself                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class FactorResult:
    labels: list[str]                  # one per variable
    n: int
    k: int                              # number of variables
    n_factors: int
    raw_eigenvalues: list[float]        # of the plain correlation matrix, all k of them
    suggested_factors: int              # Kaiser criterion: count(raw_eigenvalue > 1)
    loadings: list[list[float]]         # [variable][factor], after varimax rotation
    communalities: list[float]          # one per variable, sum of squared loadings
    variance_explained: list[float]     # one per factor, as a fraction of total variables
    iterations: int
    converged: bool


def factor_analysis(x_rows: list[list[float]], labels: list[str],
                    n_factors: int | None = None,
                    max_iter: int = 50, tol: float = 1e-3) -> FactorResult:
    """Principal-axis factor analysis of ``x_rows`` (one row per observation,
    one column per variable in ``labels`` order), with a varimax rotation to
    orthogonal simple structure.

    ``n_factors`` picked explicitly, or (``None``) the Kaiser criterion:
    every raw correlation-matrix eigenvalue greater than 1, at least 1.
    Raises :class:`FactorAnalysisError` if there are too few observations,
    fewer than two variables, or the requested factor count can't be backed
    by a positive eigenvalue for this sample.
    """
    n = len(x_rows)
    k = len(labels)
    if k < 2:
        raise FactorAnalysisError("Factor analysis needs at least two variables.")
    if n <= k:
        raise FactorAnalysisError(
            f"Need more observations than variables: {k} variable(s) need at least "
            f"{k + 1}, only {n} usable row(s) were found."
        )
    r = correlation_matrix(x_rows)
    raw_eigenvalues, _raw_vecs = _jacobi_eigen(r)
    suggested = max(1, sum(1 for ev in raw_eigenvalues if ev > 1.0))
    requested = n_factors if n_factors is not None else suggested
    requested = max(1, min(requested, k - 1 if k > 1 else 1))

    communalities = _smc(r)
    loadings, iterations, converged = None, 0, False
    for iterations in range(1, max_iter + 1):
        reduced = [row[:] for row in r]
        for i in range(k):
            reduced[i][i] = communalities[i]
        eigenvalues, eigenvectors = _jacobi_eigen(reduced)
        usable = sum(1 for f in range(requested) if eigenvalues[f] > 1e-8)
        if usable == 0:
            raise FactorAnalysisError(
                "No positive-eigenvalue factor could be extracted from this sample -- "
                "try fewer/different predictors, or a smaller factor count.")
        loadings = [[eigenvectors[i][f] * math.sqrt(eigenvalues[f]) for f in range(usable)]
                   for i in range(k)]
        new_communalities = [min(sum(loadings[i][f] ** 2 for f in range(usable)), 0.9999)
                             for i in range(k)]
        converged = max(abs(a - b) for a, b in zip(new_communalities, communalities)) < tol
        communalities = new_communalities
        if converged:
            break

    n_factors_used = len(loadings[0]) if loadings else 0
    rotated = _varimax(loadings)
    final_communalities = [sum(row[f] ** 2 for f in range(n_factors_used)) for row in rotated]
    variance_explained = [sum(rotated[i][f] ** 2 for i in range(k)) / k
                          for f in range(n_factors_used)]

    return FactorResult(
        labels=list(labels), n=n, k=k, n_factors=n_factors_used,
        raw_eigenvalues=raw_eigenvalues, suggested_factors=suggested,
        loadings=rotated, communalities=final_communalities,
        variance_explained=variance_explained, iterations=iterations, converged=converged,
    )


def factor_scores(x_rows: list[list[float]], result: FactorResult) -> list[list[float]]:
    """Per-observation factor scores (the "regression method"/Thomson
    estimate: ``Z @ R^-1 @ Loadings``, in standard-deviation units of each
    factor), one row per ``x_rows`` observation, one column per factor.

    Falls back to an unweighted sum of each standardized variable times its
    loading if the correlation matrix can't be inverted (exactly collinear
    predictors) -- a cruder score, but still orthogonal-factor-shaped and
    better than refusing to show anything.
    """
    n = len(x_rows)
    k = result.k
    cols = [[row[j] for row in x_rows] for j in range(k)]
    means = [sum(c) / n for c in cols]
    stds = [math.sqrt(sum((v - m) ** 2 for v in c) / n) for c, m in zip(cols, means)]
    z = [[(x_rows[i][j] - means[j]) / stds[j] if stds[j] else 0.0 for j in range(k)]
        for i in range(n)]
    if result.n_factors == 0:
        return [[] for _ in range(n)]

    r = correlation_matrix(x_rows)
    rinv = _invert(r)
    if rinv is None:
        return [[sum(z[i][j] * result.loadings[j][f] for j in range(k))
                for f in range(result.n_factors)] for i in range(n)]
    b = [[sum(rinv[j][m] * result.loadings[m][f] for m in range(k))
         for f in range(result.n_factors)] for j in range(k)]
    return [[sum(z[i][j] * b[j][f] for j in range(k)) for f in range(result.n_factors)]
           for i in range(n)]


def top_loadings(result: FactorResult, factor_index: int, count: int = 5) -> list[tuple[str, float]]:
    """The ``count`` variables with the largest |loading| on one factor,
    highest first -- the "what defines this factor" list a user picks a
    label from."""
    pairs = [(result.labels[i], result.loadings[i][factor_index]) for i in range(result.k)]
    pairs.sort(key=lambda t: -abs(t[1]))
    return pairs[:count]
