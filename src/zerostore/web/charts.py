"""Small server-side SVG charts for the status page. No JavaScript.

Marks follow one spec: columns at most 20px wide with a 4px rounded top and a
square base, a 2px surface gap between stacked segments, 2px lines with 8px
end dots ringed in the surface color, hairline solid gridlines. Two series use
the blue and orange categorical slots (checked for colour-blind separation on
the #1A1D21 surface); a single series is blue. Text is never in a series
color, every mark has a hover title, and each chart has a table twin.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from html import escape

from markupsafe import Markup

BLUE = "#3987e5"
ORANGE = "#d95926"
SURFACE = "#1A1D21"
GRID = "#2A2E34"

W = 520
PLOT_H = 140
TOP = 12
LEFT = 34
RIGHT = 8
AXIS_H = 22
BAR_MAX = 20
RADIUS = 4


def nice_max(value: float) -> float:
    """A round axis top at or above `value`: 1, 2, 5, 10, 20, 50..."""
    if value <= 0:
        return 1
    power = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 5, 10):
        if value <= step * power:
            return step * power
    return 10 * power


def compact(n: float) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return f"{n:g}"


def _svg(body: str, label: str, height: int) -> str:
    return (
        f'<svg class="viz" viewBox="0 0 {W} {height}" role="img" aria-label="{escape(label)}" '
        f'preserveAspectRatio="xMinYMin meet">{body}</svg>'
    )


def _column(x: float, y: float, w: float, h: float, color: str, rounded: bool) -> str:
    """A column from baseline y+h up to y; only the data end is rounded."""
    if h <= 0:
        return ""
    if not rounded:
        return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" fill="{color}"/>'
    r = min(RADIUS, h, w / 2)
    return (
        f'<path fill="{color}" d="M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
        f'H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z"/>'
    )


def _y_axis(top_value: float) -> str:
    """Hairline gridlines at 0, the middle (when it is a whole number) and the top."""
    out = []
    for frac in (0, 0.5, 1):
        value = top_value * frac
        if value != int(value):
            continue
        y = TOP + PLOT_H - frac * PLOT_H
        out.append(f'<line x1="{LEFT}" x2="{W - RIGHT}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}"/>')
        out.append(
            f'<text class="tick" x="{LEFT - 6}" y="{y + 4:.1f}" text-anchor="end">{compact(value)}</text>'
        )
    return "".join(out)


def _x_labels(labels: Sequence[str], centers: Sequence[float]) -> str:
    n = len(labels)
    picks = {0, n // 2, n - 1} if n > 2 else set(range(n))
    y = TOP + PLOT_H + 16
    return "".join(
        f'<text class="tick" x="{centers[i]:.1f}" y="{y}" text-anchor="middle">{escape(labels[i])}</text>'
        for i in sorted(picks)
    )


def _slots(n: int) -> tuple[float, float, list[float]]:
    slot = (W - LEFT - RIGHT) / max(n, 1)
    bar = min(BAR_MAX, slot * 0.6)
    centers = [LEFT + slot * (i + 0.5) for i in range(n)]
    return slot, bar, centers


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in row) + "</tr>" for row in rows)
    return f'<details class="twin"><summary>table</summary><table><tr>{head}</tr>{body}</table></details>'


def _legend(items: Sequence[tuple[str, str]]) -> str:
    return (
        '<div class="legend">'
        + "".join(f'<span><i style="background:{c}"></i>{escape(name)}</span>' for name, c in items)
        + "</div>"
    )


def _empty(message: str) -> Markup:
    return Markup(f'<p class="cmd empty">{escape(message)}</p>')


def short_day(day: str) -> str:
    return day[5:]  # 2026-10-14 -> 10-14


def stacked_columns(
    days: Sequence[str], series: Sequence[tuple[str, str, Sequence[int]]], label: str, empty: str
) -> Markup:
    """Columns per day, one stacked segment per series (bottom first)."""
    totals = [sum(s[2][i] for s in series) for i in range(len(days))]
    if not any(totals):
        return _empty(empty)
    top = nice_max(max(totals))
    _, bar, centers = _slots(len(days))
    scale = PLOT_H / top
    marks = []
    for i, day in enumerate(days):
        base = TOP + PLOT_H
        visible = [(name, color, values[i]) for name, color, values in series if values[i]]
        for k, (_name, color, value) in enumerate(visible):
            h = value * scale
            gap = 2 if k else 0  # surface gap between stacked segments
            marks.append(_column(centers[i] - bar / 2, base - h, bar, h - gap, color, k == len(visible) - 1))
            base -= h
        detail = ", ".join(f"{values[i]} {name}" for name, _c, values in series)
        x = centers[i] - (W - LEFT - RIGHT) / len(days) / 2
        slot_w = (W - LEFT - RIGHT) / len(days)
        marks.append(
            f'<rect class="hit" x="{x:.1f}" y="{TOP}" width="{slot_w:.1f}" height="{PLOT_H}">'
            f"<title>{escape(day)}: {escape(detail)}</title></rect>"
        )
    body = _y_axis(top) + "".join(marks) + _x_labels([short_day(d) for d in days], centers)
    legend = _legend([(name, color) for name, color, _v in series]) if len(series) > 1 else ""
    rows = [[d, *(s[2][i] for s in series)] for i, d in enumerate(days)]
    return Markup(
        legend + _svg(body, label, TOP + PLOT_H + AXIS_H) + _table(["day", *(s[0] for s in series)], rows)
    )


def columns(days: Sequence[str], values: Sequence[int], label: str, unit: str, empty: str) -> Markup:
    return stacked_columns(days, [(unit, BLUE, values)], label, empty)


def bars(items: Sequence[tuple[str, int]], label: str, empty: str) -> Markup:
    """Horizontal bars, one per category, value at the tip."""
    if not any(v for _n, v in items):
        return _empty(empty)
    row_h, bar_h, name_w = 28, 14, 150
    top = max(v for _n, v in items)
    span = W - name_w - RIGHT - 40
    marks = []
    for i, (name, value) in enumerate(items):
        y = 6 + i * row_h
        w = span * value / top if top else 0
        marks.append(f'<text class="name" x="0" y="{y + 11}">{escape(name)}</text>')
        if w > 0:
            r = min(RADIUS, w)
            x0 = name_w
            marks.append(
                f'<path fill="{BLUE}" d="M{x0},{y} H{x0 + w - r:.1f} Q{x0 + w:.1f},{y} {x0 + w:.1f},{y + r} '
                f'V{y + bar_h - r} Q{x0 + w:.1f},{y + bar_h} {x0 + w - r:.1f},{y + bar_h} H{x0} Z"/>'
            )
        marks.append(f'<text class="value" x="{name_w + w + 6:.1f}" y="{y + 11}">{value}</text>')
        marks.append(
            f'<rect class="hit" x="0" y="{y - 6}" width="{W}" height="{row_h}">'
            f"<title>{escape(name)}: {value}</title></rect>"
        )
    height = 6 + len(items) * row_h
    return Markup(_svg("".join(marks), label, height) + _table(["outcome", "count"], items))


def histogram(buckets: Sequence[int], threshold: float, label: str, empty: str) -> Markup:
    """Confidence in ten 0.1-wide bins, with a hairline at the auto-apply threshold."""
    if not any(buckets):
        return _empty(empty)
    top = nice_max(max(buckets))
    n = len(buckets)
    slot = (W - LEFT - RIGHT) / n
    bar = min(BAR_MAX * 1.6, slot - 2)  # adjacent bins: 2px surface gap
    scale = PLOT_H / top
    marks = [_y_axis(top)]
    for i, value in enumerate(buckets):
        x = LEFT + slot * i + (slot - bar) / 2
        h = value * scale
        marks.append(_column(x, TOP + PLOT_H - h, bar, h, BLUE, True))
        lo, hi = i / 10, (i + 1) / 10
        marks.append(
            f'<rect class="hit" x="{LEFT + slot * i:.1f}" y="{TOP}" width="{slot:.1f}" height="{PLOT_H}">'
            f"<title>confidence {lo:.1f}-{hi:.1f}: {value}</title></rect>"
        )
    tx = LEFT + (W - LEFT - RIGHT) * threshold
    marks.append(f'<line class="mark" x1="{tx:.1f}" x2="{tx:.1f}" y1="{TOP - 4}" y2="{TOP + PLOT_H}"/>')
    anchor = "end" if threshold > 0.7 else "start"
    nudge = -4 if anchor == "end" else 4
    marks.append(
        f'<text class="name" x="{tx + nudge:.1f}" y="{TOP + 6}" text-anchor="{anchor}">'
        f"auto-apply {threshold:.2f}</text>"
    )
    y = TOP + PLOT_H + 16
    for v in (0, 0.5, 1):
        x = LEFT + (W - LEFT - RIGHT) * v
        anchor = {0: "start", 0.5: "middle", 1: "end"}[v]
        marks.append(f'<text class="tick" x="{x:.1f}" y="{y}" text-anchor="{anchor}">{v:.1f}</text>')
    rows = [(f"{i / 10:.1f}-{(i + 1) / 10:.1f}", v) for i, v in enumerate(buckets)]
    return Markup(_svg("".join(marks), label, TOP + PLOT_H + AXIS_H) + _table(["confidence", "count"], rows))


def line(days: Sequence[str], values: Sequence[int | None], label: str, unit: str, empty: str) -> Markup:
    """A 2px line with gaps where a day has no value, end dot on the last point."""
    known = [v for v in values if v is not None]
    if not known:
        return _empty(empty)
    top = nice_max(max(known))
    _, _, centers = _slots(len(days))
    scale = PLOT_H / top

    def y(v: float) -> float:
        return TOP + PLOT_H - v * scale

    marks = [_y_axis(top)]
    run: list[str] = []
    runs: list[list[str]] = []
    for i, v in enumerate(values):
        if v is None:
            if run:
                runs.append(run)
            run = []
            continue
        run.append(f"{centers[i]:.1f},{y(v):.1f}")
    if run:
        runs.append(run)
    for points in runs:
        if len(points) > 1:
            marks.append(f'<polyline class="series" stroke="{BLUE}" points="{" ".join(points)}"/>')
    slot_w = (W - LEFT - RIGHT) / len(days)
    last = max(i for i, v in enumerate(values) if v is not None)
    for i, v in enumerate(values):
        if v is None:
            continue
        lone = (i == 0 or values[i - 1] is None) and (i == len(values) - 1 or values[i + 1] is None)
        if i == last or lone:
            marks.append(
                f'<circle cx="{centers[i]:.1f}" cy="{y(v):.1f}" r="4" fill="{BLUE}" stroke="{SURFACE}" '
                f'stroke-width="2"/>'
            )
        marks.append(
            f'<rect class="hit" x="{centers[i] - slot_w / 2:.1f}" y="{TOP}" width="{slot_w:.1f}" '
            f'height="{PLOT_H}"><title>{escape(days[i])}: {v} {escape(unit)}</title></rect>'
        )
    lv = values[last]
    marks.append(
        f'<text class="value" x="{min(centers[last], W - RIGHT - 30):.1f}" y="{y(lv) - 10:.1f}" '
        f'text-anchor="middle">{lv} {escape(unit)}</text>'
    )
    marks.append(_x_labels([short_day(d) for d in days], centers))
    rows = [(d, "" if v is None else v) for d, v in zip(days, values, strict=True)]
    return Markup(_svg("".join(marks), label, TOP + PLOT_H + AXIS_H) + _table(["day", unit], rows))
