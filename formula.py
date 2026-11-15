#!/usr/bin/env python3
"""A small, safe arithmetic-expression evaluator for user-defined "custom
stat" formulas, e.g. ``{Batting: Runs} ^ 2 / ({Batting: Runs} ^ 2 + {Pitching:
Runs allowed} ^ 2)`` (the Pythagorean win expectation).

No ``eval()``: a hand-written recursive-descent parser over a fixed grammar
(numbers, + - * / ^, parentheses, unary +/-, ``{...}`` variable references and
two functions) -- a formula can only ever do arithmetic on the named values
it's handed.

* ``qualify(amount, minimum, value)`` is ``value`` when ``amount`` reaches
  ``minimum`` rounded to the nearest whole number (as MLB rounds its qualifying
  marks: 3.1 x 162 games is 502 plate appearances), else blank -- a stat that
  only counts with enough playing time, e.g. ``qualify({Batting: PA}, 3.1 *
  {Team: G}, ...)``.
* ``product(a, b, ...)`` multiplies whichever of its arguments aren't blank,
  and ``average(a, b, ...)`` averages them (each blank only if all are).
* ``{Custom: Name}`` is another custom stat, as if its formula were written in
  its place (:func:`set_custom_formulas` says which there are).  ``^`` is right-associative and binds tighter than * and /, so
``2 * 3 ^ 2`` is ``2 * 9`` and ``2 ^ 3 ^ 2`` is ``2 ^ (3 ^ 2)``, same as most
calculators and every mainstream language's ``**``/``^``.
"""
from __future__ import annotations

import functools
import re


class FormulaError(ValueError):
    """A formula failed to parse, or referenced an unknown variable."""


_TOKEN_RE = re.compile(r"""
    (?P<num>\d+\.?\d*|\.\d+)
  | \{(?P<var>[^{}]+)\}
  | (?P<fn>[A-Za-z_]+)
  | (?P<op>[()+\-*/^,])
""", re.VERBOSE)
FUNCTIONS = {"qualify": 3, "product": None, "average": None}   # name -> how many arguments (None: one or more)
CUSTOM_PREFIX = "Custom:"
_CUSTOM: dict[str, str] = {}                  # custom stat name -> its formula


def set_custom_formulas(formulas: dict[str, str]):
    """The custom stats ``{Custom: Name}`` may refer to (``{name: formula}``)."""
    global _CUSTOM
    if formulas != _CUSTOM:
        _CUSTOM = dict(formulas)
        parse_formula.cache_clear()


def custom_formula(name: str) -> str | None:
    """A custom stat's formula, if there is one by that name."""
    return _CUSTOM.get(name)


def _tokenize(formula: str) -> list[tuple[str, str]]:
    tokens = []
    pos, n = 0, len(formula)
    while pos < n:
        if formula[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(formula, pos)
        if not m:
            raise FormulaError(f"Can't parse formula at: {formula[pos:pos + 20]!r}")
        if m.group("num") is not None:
            tokens.append(("num", m.group("num")))
        elif m.group("var") is not None:
            tokens.append(("var", m.group("var").strip()))
        elif m.group("fn") is not None:
            tokens.append(("fn", m.group("fn")))
        else:
            tokens.append(("op", m.group("op")))
        pos = m.end()
    return tokens


class _Parser:
    """expr := term (('+'|'-') term)* ; term := unary (('*'|'/') unary)*
    unary := ('+'|'-') unary | power ; power := primary ('^' unary)?
    primary := NUM | VAR | '(' expr ')'
    (power's right side is ``unary``, not ``power``, so it recurses through
    unary -> power again for the next '^' -- that's what makes it
    right-associative, and lets something like ``2 ^ -1`` parse.)"""

    def __init__(self, tokens: list[tuple[str, str]], expanding: tuple = (), expand: bool = True):
        self.tokens = tokens
        self.i = 0
        self.expanding = expanding   # the custom stats being expanded, to catch one that refers to itself
        self.expand = expand         # False: leave {Custom: ...} a plain variable

    def _peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _advance(self):
        tok = self._peek()
        self.i += 1
        return tok

    def parse(self):
        node = self._expr()
        if self._peek() is not None:
            raise FormulaError(f"Unexpected token: {self._peek()[1]!r}")
        return node

    def _expr(self):
        node = self._term()
        while True:
            tok = self._peek()
            if tok and tok[0] == "op" and tok[1] in "+-":
                self._advance()
                node = (tok[1], node, self._term())
            else:
                return node

    def _term(self):
        node = self._unary()
        while True:
            tok = self._peek()
            if tok and tok[0] == "op" and tok[1] in "*/":
                self._advance()
                node = (tok[1], node, self._unary())
            else:
                return node

    def _unary(self):
        tok = self._peek()
        if tok and tok[0] == "op" and tok[1] == "-":
            self._advance()
            return ("neg", self._unary())
        if tok and tok[0] == "op" and tok[1] == "+":
            self._advance()
            return self._unary()
        return self._power()

    def _power(self):
        node = self._primary()
        tok = self._peek()
        if tok and tok[0] == "op" and tok[1] == "^":
            self._advance()
            node = ("^", node, self._unary())
        return node

    def _primary(self):
        tok = self._peek()
        if tok is None:
            raise FormulaError("Unexpected end of formula.")
        kind, val = tok
        if kind == "num":
            self._advance()
            return ("num", float(val))
        if kind == "var":
            self._advance()
            if self.expand and val.startswith(CUSTOM_PREFIX):
                return self._custom(val[len(CUSTOM_PREFIX):].strip())
            return ("var", val)
        if kind == "fn":
            return self._call()
        if kind == "op" and val == "(":
            self._advance()
            node = self._expr()
            if self._peek() != ("op", ")"):
                raise FormulaError("Missing closing parenthesis.")
            self._advance()
            return node
        raise FormulaError(f"Unexpected token: {val!r}")

    def _call(self):
        name = self._advance()[1]
        if name not in FUNCTIONS:
            raise FormulaError(f'Unknown function "{name}" (there are: {", ".join(FUNCTIONS)}).')
        if self._peek() != ("op", "("):
            raise FormulaError(f'"{name}" needs its arguments in parentheses.')
        self._advance()
        args = [self._expr()]
        while self._peek() == ("op", ","):
            self._advance()
            args.append(self._expr())
        if self._peek() != ("op", ")"):
            raise FormulaError("Missing closing parenthesis.")
        self._advance()
        wanted = FUNCTIONS[name]
        if wanted is not None and len(args) != wanted:
            raise FormulaError(f'"{name}" takes {wanted} arguments, not {len(args)}.')
        return ("fn", name, tuple(args))

    def _custom(self, name: str):
        """``{Custom: Name}``: that custom stat's formula, parsed in its place."""
        text = _CUSTOM.get(name)
        if text is None:
            raise FormulaError(f'Unknown custom stat "{name}".')
        if name in self.expanding:
            raise FormulaError(f'Custom stat "{name}" refers back to itself.')
        tokens = _tokenize(text)
        if not tokens:
            raise FormulaError(f'Custom stat "{name}" has no formula.')
        return _Parser(tokens, self.expanding + (name,)).parse()


@functools.lru_cache(maxsize=4096)  # a tree is nested tuples: safe to share
def parse_formula(formula: str, expand: bool = True):
    """``formula`` as a tree; ``expand=False`` leaves each ``{Custom: ...}`` a plain variable, for a
    caller that works custom stats out itself (the Info tab)."""
    tokens = _tokenize(formula)
    if not tokens:
        raise FormulaError("Empty formula.")
    tree = _Parser(tokens, expand=expand).parse()
    return tree


def _tree_variables(node, out: set):
    if node[0] == "var":
        out.add(node[1])
    elif node[0] == "fn":
        for arg in node[2]:
            _tree_variables(arg, out)
    elif node[0] != "num":
        for child in node[1:]:
            _tree_variables(child, out)
    return out


def formula_variables(formula: str, expand: bool = True) -> set[str]:
    """Every ``{...}`` variable name ``formula`` uses -- a ``{Custom: ...}`` stat's own
    variables in its place unless ``expand`` is False (raises FormulaError on a syntax
    error, same as :func:`parse_formula`)."""
    return _tree_variables(parse_formula(formula, expand), set())


def _eval_node(node, values: dict[str, float | None], qualifying: bool = True):
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "var":
        name = node[1]
        if name not in values:
            raise FormulaError(f'Unknown variable "{name}".')
        return values[name]
    if kind == "neg":
        v = _eval_node(node[1], values, qualifying)
        return None if v is None else -v
    if kind == "fn":
        args = [_eval_node(arg, values, qualifying) for arg in node[2]]
        if node[1] == "qualify":
            amount, minimum, value = args
            if not qualifying:
                return value
            return None if amount is None or minimum is None or amount < round(minimum) else value
        present = [a for a in args if a is not None]   # product, average
        if not present:
            return None
        if node[1] == "average":
            return sum(present) / len(present)
        result = 1.0
        for a in present:
            result *= a
        return result
    a = _eval_node(node[1], values, qualifying)
    b = _eval_node(node[2], values, qualifying)
    if a is None or b is None:
        return None
    if kind == "+":
        return a + b
    if kind == "-":
        return a - b
    if kind == "*":
        return a * b
    if kind == "/":
        return None if b == 0 else a / b
    if kind == "^":
        if a == 0 and b < 0:
            return None  # 0 ** negative: division by zero, same spirit as "/"
        try:
            result = a ** b
        except OverflowError:
            return None
        # a negative base with a fractional exponent (e.g. (-4) ^ 0.5) has no
        # real result -- Python hands back a complex number instead of
        # raising, so catch that here rather than let it leak into a "number".
        return None if isinstance(result, complex) else result
    raise FormulaError(f"Internal error: unknown node {kind!r}")  # pragma: no cover


def evaluate_formula(formula: str, values: dict[str, float | None], expand: bool = True,
                     qualifying: bool = True) -> float | None:
    """Parse and evaluate ``formula`` against ``values`` (name -> number or
    ``None``).  Returns ``None`` -- rather than raising -- if a referenced
    variable's value is missing or a division by zero occurs, so the caller
    can just skip that observation the same as any other missing datum.
    Raises :class:`FormulaError` for anything that's actually wrong with the
    formula itself (bad syntax, an unknown variable name).  With ``qualifying`` False every
    ``qualify(...)`` gives its value whatever the playing time -- what a stat would be if it counted."""
    return _eval_node(parse_formula(formula, expand), values, qualifying)
