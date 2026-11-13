"""Draw the site's waveform diagrams from examples/sample.vcd using WaveQ itself.

Run from the repository root:  python site/build_diagrams.py
It rewrites the regions between <!-- diagram:NAME --> and <!-- /diagram:NAME --> in
site/index.html. Colours come from CSS classes, so the SVG carries no literal colours.
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from waveq.engine import WaveQEngine  # noqa: E402
VCD = ROOT / "examples" / "sample.vcd"
PAGE = ROOT / "site" / "index.html"


def changes(e: WaveQEngine, sig: str) -> list[tuple[int, str]]:
    r = e.changes(sig, 0, 50, limit=10000)
    assert r["ok"], r
    return [(c["tick"], c["value"]) for c in r["data"]["changes"]]


class Axis:
    def __init__(self, x0: float, x1: float, t0: int, t1: int) -> None:
        self.x0, self.x1, self.t0, self.t1 = x0, x1, t0, t1

    def __call__(self, t: float) -> float:
        return round(self.x0 + (t - self.t0) * (self.x1 - self.x0) / (self.t1 - self.t0), 2)


def bit_path(ch: list[tuple[int, str]], x: Axis, top: float, h: float, t_end: int) -> str:
    """A 1-bit waveform as one polyline path: high at top, low at top+h."""
    y = lambda v: top if v == "1" else top + h
    pts: list[str] = []
    for i, (t, v) in enumerate(ch):
        if i == 0:
            pts.append(f"M{x(t)} {y(v)}")
        else:
            pts.append(f"H{x(t)} V{y(v)}")
    pts.append(f"H{x(t_end)}")
    return " ".join(pts)


def bus_shapes(ch: list[tuple[int, str]], x: Axis, top: float, h: float, t_end: int, cls: str = "bus") -> str:
    """A vector lane as hexagonal segments labelled with their hex value."""
    out = []
    mid, s = top + h / 2, 4
    bounds = [t for t, _ in ch] + [t_end]
    for (t, v), t_next in zip(ch, bounds[1:]):
        a, b = x(t), x(t_next)
        sa = s if t > x.t0 else 0
        sb = s if t_next < t_end else 0
        d = f"M{a} {mid} L{a + sa} {top} H{b - sb} L{b} {mid} L{b - sb} {top + h} H{a + sa} Z"
        out.append(f'<path class="{cls}" d="{d}"/>')
        label = f"{int(v[1:], 2):02X}" if v.startswith("b") and set(v[1:]) <= {"0", "1"} else v
        out.append(f'<text class="bus-label" x="{round((a + b) / 2, 2)}" y="{mid}">{label}</text>')
    return "\n".join(out)


def time_axis(x: Axis, y: float, step: int, top: float) -> str:
    out = [f'<line class="axis" x1="{x.x0}" y1="{y}" x2="{x.x1}" y2="{y}"/>']
    for t in range(x.t0, x.t1 + 1, step):
        out.append(f'<line class="grid" x1="{x(t)}" y1="{top}" x2="{x(t)}" y2="{y}"/>')
        unit = " ns" if t + step > x.t1 else ""
        out.append(f'<text class="tick" x="{x(t)}" y="{y + 18}">{t}{unit}</text>')
    return "\n".join(out)


def note(x: Axis, t: int, y: float, text: str, gap: int = 10) -> str:
    """A label beside a mark: right of it at the start of the axis, left of it elsewhere (clear of the cursor)."""
    if t == x.t0:
        return f'<text class="note" x="{x(t) + gap}" y="{y}">{text}</text>'
    return f'<text class="note" text-anchor="end" x="{x(t) - gap}" y="{y}">{text}</text>'


def lane_label(text: str, y: float, x0: float = 0) -> str:
    return f'<text class="lane" x="{x0}" y="{y}">{text}</text>'


def vcd_figure(e: WaveQEngine) -> str:
    W, LABEL, PAD, LANE_H, GAP = 640, 104, 16, 26, 22
    x = Axis(LABEL, W - PAD, 0, 50)
    lanes = [("clk", "tb.clk"), ("rst_n", "tb.rst_n"), ("valid", "tb.valid"), ("ready", "tb.ready"),
             ("data[7:0]", "tb.data"), ("psel", "tb.u_apb.psel"), ("penable", "tb.u_apb.penable")]
    top0 = 8
    body = []
    for i, (name, sig) in enumerate(lanes):
        top = top0 + i * (LANE_H + GAP)
        body.append(lane_label(name, top + LANE_H / 2 + 5))
        ch = changes(e, sig)
        if name.startswith("data"):
            body.append(bus_shapes(ch, x, top, LANE_H, 50))
        else:
            body.append(f'<path class="wave" d="{bit_path(ch, x, top, LANE_H, 50)}"/>')
    axis_y = top0 + len(lanes) * (LANE_H + GAP) - GAP + 16
    H = axis_y + 28
    return (
        f'<svg class="diagram" viewBox="0 0 {W} {H}" role="img" aria-label="Waveform diagram of examples/sample.vcd from 0 to 50 ns: '
        f'clk toggles every 5 ns, rst_n rises at 10 ns, valid is high 15 to 40 ns, ready 20 to 40 ns, data becomes A5 at 15 ns, psel is high 25 to 40 ns and penable 30 to 40 ns.">\n'
        + time_axis(x, axis_y, 10, top0 - 4) + "\n" + "\n".join(body) + "\n</svg>"
    )


def index_figure(e: WaveQEngine) -> str:
    W, LABEL, PAD = 1200, 120, 24
    x = Axis(LABEL, W - PAD, 0, 51)
    valid = changes(e, "tb.valid")
    runs = [(0, 15, "0"), (15, 40, "1"), (40, 51, "0")]
    edges = e.edges("tb.valid")["data"]["edges"]
    cursor = 16
    v = e.value_at("tb.valid", cursor)["data"]
    assert (v["held_from_tick"], v["held_until_tick_exclusive"], v["value"]) == (15, 40, "1")
    rows = {"signal": 36, "changes": 116, "runs": 180, "edges": 256}
    out = []
    for name, y in rows.items():
        out.append(lane_label(name if name != "signal" else "tb.valid", y + 19))
    out.append(f'<path class="wave" d="{bit_path(valid, x, rows["signal"], 30, 51)}"/>')
    # changes: one row per transition
    out.append(f'<line class="rail" x1="{x(0)}" y1="{rows["changes"] + 14}" x2="{x(51)}" y2="{rows["changes"] + 14}"/>')
    for t, val in valid:
        out.append(f'<circle class="dot" cx="{x(t)}" cy="{rows["changes"] + 14}" r="5"/>')
        out.append(note(x, t, rows["changes"] + 19, f"{t} ns → {val}"))
    # runs: one row per held value, the one value_at hits is lit
    for a, b, val in runs:
        lit = a <= cursor < b
        out.append(f'<rect class="run{" run--hit" if lit else ""}" x="{x(a) + 2}" y="{rows["runs"]}" width="{round(x(b) - x(a) - 4, 2)}" height="40" rx="4"/>')
        out.append(f'<text class="run-label{" run-label--hit" if lit else ""}" x="{round((x(a) + x(b)) / 2, 2)}" y="{rows["runs"] + 25}">{val} from {a} to {b} ns</text>')
    # edges
    out.append(f'<line class="rail" x1="{x(0)}" y1="{rows["edges"] + 14}" x2="{x(51)}" y2="{rows["edges"] + 14}"/>')
    for ed in edges:
        t, kind = ed["tick"], ed["kind"]
        cy = rows["edges"] + 14
        if kind in ("rise", "fall"):
            tip, base = (cy - 10, cy + 10) if kind == "rise" else (cy + 10, cy - 10)
            out.append(f'<path class="edge-mark" d="M{x(t) - 6} {base} L{x(t)} {tip} L{x(t) + 6} {base}"/>')
        else:  # x_enter, x_exit, z_enter, z_exit: a diamond
            out.append(f'<path class="edge-mark" d="M{x(t)} {cy - 8} L{x(t) + 7} {cy} L{x(t)} {cy + 8} L{x(t) - 7} {cy} Z"/>')
        out.append(note(x, t, rows["edges"] + 19, kind, gap=14))
    # the query cursor
    out.append(f'<line class="cursor" x1="{x(cursor)}" y1="10" x2="{x(cursor)}" y2="{rows["runs"] + 44}"/>')
    out.append(f'<text class="cursor-label" x="{x(cursor) + 8}" y="18">value_at 16 ns</text>')
    H = rows["edges"] + 48
    return (
        f'<svg class="diagram diagram--wide" viewBox="0 0 {W} {H}" role="img" aria-label="Index diagram for tb.valid in sample.vcd: '
        f'the changes table holds 0 at 0 ns, 1 at 15 ns and 0 at 40 ns; the runs table holds 0 from 0 to 15 ns, 1 from 15 to 40 ns and 0 from 40 to 51 ns; '
        f'the edges table holds a rise at 15 ns and a fall at 40 ns. A query at 16 ns lands in the run from 15 to 40 ns, so the answer is 1.">\n'
        + "\n".join(out) + "\n</svg>"
    )


def find_figure(e: WaveQEngine) -> str:
    W, LABEL, PAD, LANE_H, GAP = 640, 104, 16, 26, 30
    x = Axis(LABEL, W - PAD, 0, 50)
    e.define_clock("tb.clk")
    r = e.find_times([{"signal": "tb.u_apb.psel", "value": "1"}, {"signal": "tb.u_apb.penable", "value": "1"}], clock="tb.clk")
    assert r["ok"] and [m["cycle_index"] for m in r["data"]["matches"]] == [3], r
    hit_edges = {m["edge_tick"] for m in r["data"]["matches"]}
    lanes = [("clk", "tb.clk"), ("psel", "tb.u_apb.psel"), ("penable", "tb.u_apb.penable")]
    top0 = 34
    out = []
    rises = [ed["tick"] for ed in e.edges("tb.clk", kind="rise")["data"]["edges"]]
    lane_tops = {}
    for i, (name, sig) in enumerate(lanes):
        top = top0 + i * (LANE_H + GAP)
        lane_tops[name] = top
        out.append(lane_label(name, top + LANE_H / 2 + 5))
        out.append(f'<path class="wave" d="{bit_path(changes(e, sig), x, top, LANE_H, 50)}"/>')
    bottom = top0 + len(lanes) * (LANE_H + GAP) - GAP
    for t in rises:
        hit = t in hit_edges
        if hit:
            out.insert(0, f'<rect class="match-band" x="{x(t - 1) - 7}" y="{top0 - 12}" width="{round(x(t) - x(t - 1) + 14, 2)}" height="{bottom - top0 + 24}" rx="4"/>')
            out.append(f'<text class="cursor-label" x="{x(t) + 10}" y="{top0 - 18}">cycle 3, sampled at 34 ns</text>')
        out.append(f'<line class="edge-line{" edge-line--hit" if hit else ""}" x1="{x(t)}" y1="{top0 - 8}" x2="{x(t)}" y2="{bottom + 8}"/>')
        for name in ("psel", "penable"):
            sig = dict(lanes)[name]
            val = e.value_at(sig, t - 1)["data"]["value"]
            cy = lane_tops[name] + (0 if val == "1" else LANE_H)
            out.append(f'<circle class="sample{" sample--hit" if hit else ""}" cx="{x(t - 1)}" cy="{cy}" r="4"/>')
    axis_y = bottom + 24
    H = axis_y + 28
    return (
        f'<svg class="diagram" viewBox="0 0 {W} {H}" role="img" aria-label="Diagram of find_times on sample.vcd: rising clk edges at 5, 15, 25, 35 and 45 ns, '
        f'with psel and penable sampled one tick before each edge. psel rises exactly at the 25 ns edge, so the sample at 24 ns still reads 0. '
        f'Only the 35 ns edge, cycle 3, sees both high.">\n'
        + time_axis(x, axis_y, 10, top0 - 8) + "\n" + "\n".join(out) + "\n</svg>"
    )


def scope_figure(e: WaveQEngine) -> str:
    """The hero's live waveform: four lanes, a cursor that travels to 16 ns, and the run of valid it lands in."""
    W, LABEL, PAD, LANE_H, GAP, T = 440, 56, 12, 16, 12, 16
    x = Axis(LABEL, W - PAD, 0, 50)
    lanes = [("clk", "tb.clk"), ("valid", "tb.valid"), ("ready", "tb.ready"), ("data", "tb.data")]
    top0 = 10
    held = e.value_at("tb.valid", T)
    assert held["ok"], held
    run_a, run_b = held["data"]["held_from_tick"], held["data"]["held_until_tick_exclusive"]
    body, run = [], ""
    for i, (name, sig) in enumerate(lanes):
        top = top0 + i * (LANE_H + GAP)
        body.append(lane_label(name, top + LANE_H / 2 + 4))
        ch = changes(e, sig)
        if name == "data":
            body.append(bus_shapes(ch, x, top, LANE_H, 50))
        else:
            hit = " wave--hit" if sig == "tb.valid" else ""
            body.append(f'<path class="wave{hit}" d="{bit_path(ch, x, top, LANE_H, 50)}"/>')
        if sig == "tb.valid":
            run = (f'<rect class="scope__run" x="{x(run_a)}" y="{top - 4}" width="{round(x(run_b) - x(run_a), 2)}" '
                   f'height="{LANE_H + 8}" rx="3"/>')
    axis_y = top0 + len(lanes) * (LANE_H + GAP) - GAP + 12
    H = axis_y + 26
    cursor = (
        f'<g class="scope__cursor" style="--from: {round(x(0) - x(T), 2)}px">'
        f'<line class="cursor" x1="{x(T)}" y1="2" x2="{x(T)}" y2="{axis_y}"/></g>'
        f'<text class="cursor-label scope__stamp" text-anchor="middle" x="{x(T)}" y="{axis_y + 16}">{T} ns</text>'
    )
    return (
        f'<svg class="diagram scope" viewBox="0 0 {W} {H}" role="img" aria-label="Waveform of sample.vcd from 0 to 50 ns with a cursor at {T} ns: '
        f'valid is 1 there, in the run from {run_a} to {run_b} ns.">\n'
        + "\n".join([time_axis(x, axis_y, 10, top0 - 6), run, *body, cursor]) + "\n</svg>"
    )


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="waveq-site-"))
    e = WaveQEngine()
    try:
        assert e.configure(str(VCD), cache_dir=str(tmp), rebuild=True)["ok"]
        figures = {"scope": scope_figure(e), "vcd": vcd_figure(e), "index": index_figure(e), "find": find_figure(e)}
    finally:
        e.close()
        shutil.rmtree(tmp, ignore_errors=True)
    page = PAGE.read_text(encoding="utf-8")
    for name, svg in figures.items():
        pattern = re.compile(rf"(<!-- diagram:{name} -->).*?(<!-- /diagram:{name} -->)", re.S)
        page, n = pattern.subn(lambda m: f"{m.group(1)}\n{svg}\n{m.group(2)}", page)
        if n != 1:
            raise SystemExit(f"marker for diagram {name!r} not found exactly once in {PAGE}")
    PAGE.write_text(page, encoding="utf-8")
    print("wrote", ", ".join(figures))


if __name__ == "__main__":
    main()
