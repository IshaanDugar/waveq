from __future__ import annotations

import pathlib
import shutil
import sqlite3
import unittest
import uuid

from waveq.engine import WaveQEngine
from waveq.mcp_server import handle_message

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
TMPROOT = ROOT / ".tmp" / "tests"
TMPROOT.mkdir(parents=True, exist_ok=True)


def make_tmp_dir() -> pathlib.Path:
    path = TMPROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path


def write_text(path: pathlib.Path, text: str) -> pathlib.Path:
    path.write_text(text, encoding="utf-8")
    return path


def write_large_metadata_vcd(path: pathlib.Path, size_mb: int = 58) -> pathlib.Path:
    header = "$timescale 1 ns $end\n$scope module tb $end\n$var wire 1 ! clk $end\n$upscope $end\n$enddefinitions $end\n"
    with path.open("w", encoding="utf-8") as f:
        f.write(header)
        f.seek(size_mb * 1024 * 1024)
        f.write("\n")
    return path


class WaveQEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_tmp_dir()
        self.engine = WaveQEngine()
        result = self.engine.configure(str(FIXTURES / "sample.vcd"), cache_dir=str(self.tmp), rebuild=True)
        self.assertTrue(result["ok"], result)

    def tearDown(self) -> None:
        self.engine.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_info_counts_are_exact(self) -> None:
        info = self.engine.info()
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["data"]["timescale"], "1ns")
        self.assertEqual(info["data"]["signal_count"], 10)
        self.assertGreater(info["data"]["change_count"], 20)
        self.assertGreater(info["data"]["run_count"], 10)
        self.assertGreater(info["data"]["edge_count"], 10)

    def test_value_at_uses_held_run(self) -> None:
        r = self.engine.value_at("tb.valid", "16ns")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["value"], "1")
        self.assertEqual(r["data"]["held_from_tick"], 15)
        self.assertEqual(r["data"]["held_until_tick_exclusive"], 40)

    def test_values_at_batch_is_strict(self) -> None:
        r = self.engine.values_at(["tb.u_apb.psel", "tb.u_apb.penable", "paddr"], "30ns")
        self.assertTrue(r["ok"], r)
        values = {item["signal"]: item["value"] for item in r["data"]["values"]}
        self.assertEqual(values["tb.u_apb.psel"], "1")
        self.assertEqual(values["tb.u_apb.penable"], "1")
        self.assertEqual(values["tb.u_apb.paddr [7:0]"], "b00100000")

    def test_values_at_does_not_false_pass_missing_signal(self) -> None:
        r = self.engine.values_at(["tb.valid", "definitely_missing"], "20ns")
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["status"], "partial_failure")
        self.assertEqual(len(r["data"]["errors"]), 1)

    def test_edges_are_precomputed(self) -> None:
        r = self.engine.edges("tb.clk", kind="rise", start="0ns", end="50ns")
        self.assertTrue(r["ok"], r)
        self.assertEqual([e["tick"] for e in r["data"]["edges"]], [5, 15, 25, 35, 45])

    def test_window_digest_summarizes_without_raw_dump(self) -> None:
        r = self.engine.window_digest("30ns", "10ns", scope="u_apb", max_signals=10)
        self.assertTrue(r["ok"], r)
        signals = {item["signal"] for item in r["data"]["signals"]}
        self.assertIn("tb.u_apb.psel", signals)
        self.assertIn("tb.u_apb.penable", signals)

    def test_clock_cycle_index(self) -> None:
        r = self.engine.define_clock("tb.clk", "rise")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["cycles"], 5)
        cyc = self.engine.time_to_cycle("tb.clk", "31ns")
        self.assertTrue(cyc["ok"], cyc)
        self.assertEqual(cyc["data"]["cycle_index"], 2)
        vals = self.engine.values_at_cycle(["tb.valid", "tb.ready"], "tb.clk", 2)
        self.assertTrue(vals["ok"], vals)
        self.assertEqual({v["signal"]: v["value"] for v in vals["data"]["values"]}["tb.ready"], "1")

    def test_non_integral_time_is_rejected(self) -> None:
        r = self.engine.value_at("tb.valid", "0.5ns")
        self.assertFalse(r["ok"], r)
        self.assertIn("not representable", r["summary"])


class TimescaleAndAliasTests(unittest.TestCase):
    def test_10ps_timescale_exactness(self) -> None:
        tmp = make_tmp_dir()
        try:
            e = WaveQEngine()
            self.assertTrue(e.configure(str(FIXTURES / "timescale_10ps.vcd"), cache_dir=str(tmp), rebuild=True)["ok"])
            self.assertEqual(e.value_at("tb.sig", "10ps")["data"]["value"], "1")
            bad = e.value_at("tb.sig", "5ps")
            self.assertFalse(bad["ok"], bad)
            self.assertIn("not representable", bad["summary"])
            e.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class StagedIndexTests(unittest.TestCase):
    def test_large_configure_is_metadata_only_and_query_requires_index(self) -> None:
        tmp = make_tmp_dir()
        try:
            vcd = write_large_metadata_vcd(tmp / "large.vcd")
            e = WaveQEngine()
            r = e.configure(str(vcd), cache_dir=str(tmp / "cache"), rebuild=True)
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["data"]["size_class"], "large")
            self.assertFalse(r["data"]["query_ready"])
            self.assertEqual(r["data"]["index_levels_ready"], ["metadata"])
            self.assertEqual(r["data"]["recommended_next_tool"], "build_index")
            value = e.value_at("tb.clk", "0ns")
            self.assertFalse(value["ok"], value)
            self.assertEqual(value["status"], "index_required")
            e.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_build_index_uses_configured_cache_dir(self) -> None:
        tmp = make_tmp_dir()
        try:
            vcd = tmp / "sample.vcd"
            shutil.copyfile(FIXTURES / "sample.vcd", vcd)
            cache = tmp / "cache"
            e = WaveQEngine()
            self.assertTrue(e.configure(str(vcd), cache_dir=str(cache), rebuild=True)["ok"])
            r = e.build_index(level="runs")
            self.assertTrue(r["ok"], r)
            self.assertTrue(str(r["data"]["db_path"]).startswith(str(cache)))
            self.assertIn("runs", r["data"]["index_levels_ready"])
            e.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cache_reuse_and_stale_detection(self) -> None:
        tmp = make_tmp_dir()
        try:
            vcd = tmp / "copy.vcd"
            shutil.copyfile(FIXTURES / "sample.vcd", vcd)
            cache = tmp / "cache"
            e = WaveQEngine()
            first = e.configure(str(vcd), cache_dir=str(cache), rebuild=True)
            self.assertTrue(first["ok"], first)
            second = e.configure(str(vcd), cache_dir=str(cache), rebuild=False)
            self.assertTrue(second["ok"], second)
            self.assertTrue(second["data"]["cache_reused"], second)
            with vcd.open("a", encoding="utf-8") as f:
                f.write("\n$comment changed $end\n")
            st = e.index_status(str(vcd), str(cache))
            self.assertTrue(st["ok"], st)
            self.assertEqual(st["data"]["status"], "stale")
            self.assertFalse(st["data"]["cache_valid"])
            e.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_build_index_changes_does_not_claim_runs_ready(self) -> None:
        tmp = make_tmp_dir()
        try:
            e = WaveQEngine()
            r = e.build_index(str(FIXTURES / "sample.vcd"), str(tmp), level="changes", rebuild=True)
            self.assertTrue(r["ok"], r)
            self.assertIn("changes", r["data"]["index_levels_ready"])
            self.assertNotIn("runs", r["data"]["index_levels_ready"])
            changes = e.changes("tb.valid", "0ns", "20ns")
            self.assertTrue(changes["ok"], changes)
            value = e.value_at("tb.valid", "16ns")
            self.assertFalse(value["ok"], value)
            self.assertEqual(value["status"], "index_required")
            e.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_partial_build_state_is_not_ready(self) -> None:
        tmp = make_tmp_dir()
        try:
            vcd = write_large_metadata_vcd(tmp / "partial.vcd")
            e = WaveQEngine()
            r = e.configure(str(vcd), cache_dir=str(tmp / "cache"), rebuild=True)
            self.assertTrue(r["ok"], r)
            db_path = pathlib.Path(r["data"]["db_path"])
            e.close()
            with sqlite3.connect(db_path) as con:
                con.execute("UPDATE meta SET value='runs_building' WHERE key='build_state'")
                con.commit()
            st = WaveQEngine().index_status(str(vcd), str(tmp / "cache"))
            self.assertTrue(st["ok"], st)
            self.assertEqual(st["data"]["status"], "partial")
            self.assertFalse(st["data"]["query_ready"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ambiguous_signal_does_not_false_pass(self) -> None:
        tmp = make_tmp_dir()
        try:
            vcd = write_text(tmp / "ambiguous.vcd", """$timescale 1 ns $end
$scope module tb $end
$scope module u0 $end
$var wire 1 ! sig $end
$upscope $end
$scope module u1 $end
$var wire 1 " sig $end
$upscope $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
1"
$end
#1
1!
0"
""")
            e = WaveQEngine()
            self.assertTrue(e.configure(str(vcd), cache_dir=str(tmp / "cache"), rebuild=True)["ok"])
            r = e.value_at("sig", "1ns")
            self.assertFalse(r["ok"], r)
            self.assertEqual(r["status"], "ambiguous_signal")
            e.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_aliases_receive_same_changes_and_xz_edges(self) -> None:
        tmp = make_tmp_dir()
        try:
            e = WaveQEngine()
            self.assertTrue(e.configure(str(FIXTURES / "aliases_and_xz.vcd"), cache_dir=str(tmp), rebuild=True)["ok"])
            self.assertEqual(e.value_at("tb.a", "1ns")["data"]["value"], "1")
            self.assertEqual(e.value_at("tb.alias_a", "1ns")["data"]["value"], "1")
            x_exit = e.edges("tb.b", kind="x_exit", start="1ns", end="4ns")
            self.assertTrue(x_exit["ok"], x_exit)
            self.assertEqual([edge["tick"] for edge in x_exit["data"]["edges"]], [4])
            e.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class StrictnessRegressionTests(unittest.TestCase):
    """Queries that used to return ok: true on bad input or wrong data."""

    def setUp(self) -> None:
        self.tmp = make_tmp_dir()
        self.engine = WaveQEngine()
        result = self.engine.configure(str(FIXTURES / "sample.vcd"), cache_dir=str(self.tmp), rebuild=True)
        self.assertTrue(result["ok"], result)

    def tearDown(self) -> None:
        self.engine.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assertFails(self, result: dict, status: str) -> None:
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], status, result)

    def test_unknown_edge_kinds_fail(self) -> None:
        self.assertFails(self.engine.edges("tb.clk", kind="posedge"), "bad_edge_kind")
        self.assertFails(self.engine.first_edge_after("tb.clk", "posedge", "0ns"), "bad_edge_kind")
        self.assertFails(self.engine.define_clock("tb.clk", "posedge"), "bad_edge_kind")
        self.assertFails(self.engine.define_clock("tb.clk", "x_enter"), "bad_edge_kind")
        self.assertFails(self.engine.time_to_cycle("tb.clk", "10ns", edge="posedge"), "bad_edge_kind")

    def test_define_clock_with_no_edges_fails(self) -> None:
        # rst_n only ever rises, so it has no fall edges.
        self.assertFails(self.engine.define_clock("tb.rst_n", "fall"), "no_clock_edges")

    def test_cycle_queries_distinguish_undefined_clock_and_bad_cycle(self) -> None:
        self.assertFails(self.engine.cycle_to_time("tb.clk", 0), "clock_not_defined")
        self.assertTrue(self.engine.define_clock("tb.clk")["ok"])
        self.assertFails(self.engine.cycle_to_time("tb.clk", 99), "cycle_out_of_range")
        self.assertFails(self.engine.time_to_cycle("tb.clk", "1ns"), "before_first_cycle")

    def test_out_of_range_times_fail(self) -> None:
        self.assertFails(self.engine.window_digest("500ns", "10ns"), "time_out_of_range")
        self.assertFails(self.engine.changes("tb.valid", "0ns", "500ns"), "time_out_of_range")
        self.assertFails(self.engine.stable_between("tb.valid", "0ns", "500ns"), "time_out_of_range")
        self.assertFails(self.engine.edges("tb.clk", start="0ns", end="500ns"), "time_out_of_range")
        self.assertFails(self.engine.first_edge_after("tb.clk", "rise", "500ns"), "time_out_of_range")
        self.assertTrue(self.engine.define_clock("tb.clk")["ok"])
        self.assertFails(self.engine.time_to_cycle("tb.clk", "500ns"), "time_out_of_range")
        # Radius may still extend past the end; the window is clamped.
        self.assertTrue(self.engine.window_digest("45ns", "100ns")["ok"])

    def test_vector_resolves_by_path_without_range(self) -> None:
        for name in ("tb.u_apb.paddr", "tb.u_apb.paddr[7:0]", "TB.U_APB.PADDR [7:0]"):
            r = self.engine.value_at(name, "30ns")
            self.assertTrue(r["ok"], (name, r))
            self.assertEqual(r["data"]["signal"], "tb.u_apb.paddr [7:0]")
            self.assertEqual(r["data"]["value"], "b00100000")

    def test_cache_records_package_version(self) -> None:
        from waveq import __version__
        st = self.engine.index_status()
        self.assertEqual(st["data"]["metadata"]["waveq_version"], __version__)

    def test_clock_definition_survives_rebuild(self) -> None:
        self.assertTrue(self.engine.define_clock("tb.clk", "rise")["ok"])
        self.assertTrue(self.engine.build_index(level="full", rebuild=True)["ok"])
        cyc = self.engine.time_to_cycle("tb.clk", "31ns")
        self.assertTrue(cyc["ok"], cyc)
        self.assertEqual(cyc["data"]["cycle_index"], 2)


class BatchedQueryTests(unittest.TestCase):
    """Batched values_at / window_digest must agree with per-signal queries."""

    def setUp(self) -> None:
        self.tmp = make_tmp_dir()
        self.engine = WaveQEngine()
        self.assertTrue(self.engine.configure(str(FIXTURES / "sample.vcd"), cache_dir=str(self.tmp), rebuild=True)["ok"])

    def tearDown(self) -> None:
        self.engine.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_window_digest_matches_single_signal_queries(self) -> None:
        e = self.engine
        for center, radius in (("30ns", "10ns"), ("0ns", "7ns"), ("50ns", "3ns"), ("17ns", "100ns")):
            d = e.window_digest(center, radius, max_signals=200)
            self.assertTrue(d["ok"], d)
            a, b = d["data"]["start_tick"], d["data"]["end_tick"]
            for s in d["data"]["signals"]:
                name = s["signal"]
                changes = e.changes(name, a, b, limit=10000)["data"]["changes"]
                self.assertEqual(s["change_count"], len(changes), (center, name))
                self.assertEqual(s["edge_count"], len(e.edges(name, start=a, end=b, limit=10000)["data"]["edges"]), (center, name))
                self.assertEqual(s["first_change_tick"], changes[0]["tick"] if changes else None)
                self.assertEqual(s["start_value"], e.value_at(name, a)["data"]["value"])
                self.assertEqual(s["end_value"], e.value_at(name, b)["data"]["value"])

    def test_window_digest_explicit_signals_dedupes_and_reports_errors(self) -> None:
        d = self.engine.window_digest("30ns", "5ns", signals=["tb.valid", "valid", "nope"])
        self.assertFalse(d["ok"], d)
        self.assertEqual(d["status"], "partial_failure")
        self.assertEqual([s["signal"] for s in d["data"]["signals"]], ["tb.valid"])
        self.assertEqual(len(d["data"]["errors"]), 1)

    def test_values_at_matches_value_at_and_keeps_order(self) -> None:
        names = ["tb.u_apb.pwdata", "tb.clk", "tb.data", "tb.rst_n", "tb.u_apb.paddr"]
        for tick in (0, 5, 24, 25, 50):
            batch = self.engine.values_at(names, tick)
            self.assertTrue(batch["ok"], batch)
            self.assertEqual([v["signal"] for v in batch["data"]["values"]], [self.engine.value_at(n, tick)["data"]["signal"] for n in names])
            for name, got in zip(names, batch["data"]["values"]):
                self.assertEqual(got, self.engine.value_at(name, tick)["data"])


class FindTimesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_tmp_dir()
        self.engine = WaveQEngine()
        self.assertTrue(self.engine.configure(str(FIXTURES / "sample.vcd"), cache_dir=str(self.tmp), rebuild=True)["ok"])

    def tearDown(self) -> None:
        self.engine.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def brute_force_ticks(self, conditions: list[dict], a: int, b: int) -> list[int]:
        from waveq.engine import compile_condition
        hits = []
        for tick in range(a, b + 1):
            good = True
            for c in conditions:
                v = self.engine.value_at(c["signal"], tick)["data"]
                row = {"has_x": v["has_x"], "has_z": v["has_z"], "int_value": v["int_value"]}
                width = self.engine.get_signal(c["signal"]).width
                good = good and compile_condition(c.get("op", "=="), c["value"], width)(row)
            if good:
                hits.append(tick)
        return hits

    def test_intervals_match_brute_force(self) -> None:
        cases = [
            [{"signal": "tb.valid", "value": "1"}, {"signal": "tb.ready", "value": "1"}],
            [{"signal": "tb.clk", "value": "1"}],
            [{"signal": "tb.data", "value": "0xa5"}],
            [{"signal": "tb.data", "op": "!=", "value": "0"}, {"signal": "tb.u_apb.psel", "value": "0"}],
            [{"signal": "tb.u_apb.paddr", "value": "b00100000"}, {"signal": "tb.u_apb.pwdata", "value": "85"}],
            [{"signal": "tb.rst_n", "value": "0"}, {"signal": "tb.valid", "value": "1"}],
        ]
        for conditions in cases:
            for a, b in ((0, 50), (12, 33)):
                r = self.engine.find_times(conditions, a, b, limit=10000)
                self.assertTrue(r["ok"], r)
                got = [t for m in r["data"]["matches"] for t in range(m["start_tick"], m["end_tick_exclusive"])]
                self.assertEqual(got, self.brute_force_ticks(conditions, a, b), (conditions, a, b))
                for m in r["data"]["matches"]:
                    self.assertEqual(m["may_start_earlier"], m["start_tick"] == a and a > 0)

    def test_clock_mode_samples_before_edge_by_default(self) -> None:
        self.assertTrue(self.engine.define_clock("tb.clk")["ok"])
        cond = [{"signal": "tb.u_apb.psel", "value": "1"}]
        before = self.engine.find_times(cond, clock="tb.clk")
        at = self.engine.find_times(cond, clock="tb.clk", sample="at")
        # psel rises on the same tick as the 25ns clock edge, so a flop only sees it at 35ns.
        self.assertEqual([m["cycle_index"] for m in before["data"]["matches"]], [3])
        self.assertEqual([m["cycle_index"] for m in at["data"]["matches"]], [2, 3])
        self.assertEqual(before["data"]["matches"][0]["sample_tick"], 34)

    def test_limit_truncates_and_first_match(self) -> None:
        r = self.engine.find_times([{"signal": "tb.clk", "value": "1"}], limit=2)
        self.assertTrue(r["ok"], r)
        self.assertEqual([m["start_tick"] for m in r["data"]["matches"]], [5, 15])
        self.assertTrue(r["data"]["truncated"])
        self.assertIn("result truncated at limit", r["limitations"])

    def test_no_match_is_ok_with_status(self) -> None:
        r = self.engine.find_times([{"signal": "tb.rst_n", "value": "0"}, {"signal": "tb.ready", "value": "1"}])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], "no_match")
        self.assertEqual(r["data"]["matches"], [])

    def test_bad_inputs_fail(self) -> None:
        e = self.engine
        self.assertEqual(e.find_times([])["status"], "bad_query")
        self.assertEqual(e.find_times([{"signal": "nope", "value": "1"}])["status"], "bad_condition")
        self.assertEqual(e.find_times([{"signal": "tb.valid", "value": "2"}])["status"], "bad_condition")
        self.assertEqual(e.find_times([{"signal": "tb.data", "value": "0x100"}])["status"], "bad_condition")
        self.assertEqual(e.find_times([{"signal": "tb.valid", "op": ">", "value": "0"}])["status"], "bad_condition")
        self.assertEqual(e.find_times([{"signal": "tb.valid", "value": "high"}])["status"], "bad_condition")
        self.assertEqual(e.find_times([{"signal": "tb.valid", "value": "1"}], end="500ns")["status"], "time_out_of_range")
        self.assertEqual(e.find_times([{"signal": "tb.valid", "value": "1"}], clock="tb.clk")["status"], "clock_not_defined")
        self.assertTrue(e.define_clock("tb.clk")["ok"])
        self.assertEqual(e.find_times([{"signal": "tb.valid", "value": "1"}], clock="tb.clk", sample="after")["status"], "bad_sample")

    def test_x_and_z_conditions(self) -> None:
        tmp = make_tmp_dir()
        try:
            e = WaveQEngine()
            self.assertTrue(e.configure(str(FIXTURES / "aliases_and_xz.vcd"), cache_dir=str(tmp), rebuild=True)["ok"])
            x = e.find_times([{"signal": "tb.b", "value": "x"}])
            self.assertEqual([(m["start_tick"], m["end_tick_exclusive"]) for m in x["data"]["matches"]], [(3, 4)])
            z = e.find_times([{"signal": "tb.b", "value": "z"}])
            self.assertEqual([(m["start_tick"], m["end_tick_exclusive"]) for m in z["data"]["matches"]], [(0, 3)])
            # A numeric != still matches while the value is x/z, because it is not that number.
            ne = e.find_times([{"signal": "tb.b", "op": "!=", "value": "1"}])
            self.assertEqual([(m["start_tick"], m["end_tick_exclusive"]) for m in ne["data"]["matches"]], [(0, 4)])
            e.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ParserRegressionTests(unittest.TestCase):
    def configure_text(self, text: str) -> tuple[WaveQEngine, dict, pathlib.Path]:
        tmp = make_tmp_dir()
        self.addCleanup(shutil.rmtree, tmp, True)
        vcd = write_text(tmp / "case.vcd", text)
        e = WaveQEngine()
        self.addCleanup(e.close)
        return e, e.configure(str(vcd), cache_dir=str(tmp / "cache"), rebuild=True), tmp

    def test_redumped_value_is_not_a_change(self) -> None:
        e, r, _ = self.configure_text("""$timescale 1 ns $end
$scope module tb $end
$var wire 1 ! a $end
$var wire 2 " v $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
b01 "
$end
#5
1!
#10
$dumpall
1!
b01 "
$end
#20
1!
""")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["change_count"], 3)
        stable = e.stable_between("tb.a", "5ns", "20ns")
        self.assertTrue(stable["data"]["stable"], stable)
        self.assertTrue(e.stable_between("tb.v", "0ns", "20ns")["data"]["stable"])
        self.assertEqual([c["tick"] for c in e.changes("tb.a", "0ns", "20ns")["data"]["changes"]], [0, 5])

    def test_body_comment_text_is_not_parsed_as_values(self) -> None:
        e, r, _ = self.configure_text("""$timescale 1 ns $end
$scope module tb $end
$var wire 1 ! a $end
$var wire 4 " bus $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
b0000 "
$end
#5
$comment
1!
b1111 "
see above
$end
#10
1!
""")
        self.assertTrue(r["ok"], r)
        self.assertEqual(e.value_at("tb.a", "5ns")["data"]["value"], "0")
        self.assertEqual(e.value_at("tb.bus", "5ns")["data"]["value"], "b0000")
        self.assertEqual(e.value_at("tb.a", "10ns")["data"]["value"], "1")

    def test_multiline_and_packed_header_directives(self) -> None:
        e, r, _ = self.configure_text("""$comment $var wire 1 ? fake $end
$timescale
  10 ps
$end
$scope module tb $end $var wire 1 ! clk $end
$scope module
  core $end
$var
  wire 8 " data [7:0]
$end
$upscope $end $upscope $end
$enddefinitions $end
#0
0!
b00000001 "
#1
1!
""")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["timescale"], "10ps")
        self.assertEqual(r["data"]["signal_count"], 2)
        self.assertEqual(e.value_at("tb.core.data", "0ps")["data"]["value"], "b00000001")
        self.assertEqual(e.value_at("tb.clk", "10ps")["data"]["value"], "1")

    def test_missing_enddefinitions_fails(self) -> None:
        _, r, _ = self.configure_text("$timescale 1 ns $end\n$scope module tb $end\n$var wire 1 ! a $end\n")
        self.assertFalse(r["ok"], r)
        self.assertIn("$enddefinitions", r["summary"])


class MCPTests(unittest.TestCase):
    def call_tool(self, name: str, arguments: dict[str, object], msg_id: int = 100) -> dict[str, object]:
        resp = handle_message({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        self.assertIsNotNone(resp)
        return resp["result"]

    def test_notifications_get_no_response(self) -> None:
        self.assertIsNone(handle_message({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}))
        self.assertIsNone(handle_message({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}))
        self.assertIsNone(handle_message({"jsonrpc": "2.0", "method": "notifications/unknown", "params": {}}))

    def test_tools_list_has_batch_and_digest_tools(self) -> None:
        resp = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        self.assertIsNotNone(resp)
        names = {tool["name"] for tool in resp["result"]["tools"]}
        self.assertIn("index_status", names)
        self.assertIn("build_index", names)
        self.assertIn("values_at", names)
        self.assertIn("changes_many", names)
        self.assertIn("window_digest", names)
        self.assertIn("define_clock", names)
        self.assertIn("find_times", names)

    def test_mcp_find_times_smoke(self) -> None:
        tmp = make_tmp_dir()
        try:
            self.assertFalse(self.call_tool("configure_waveform", {"vcd_path": str(FIXTURES / "sample.vcd"), "cache_dir": str(tmp), "rebuild": True})["isError"])
            self.assertFalse(self.call_tool("define_clock", {"signal": "tb.clk"})["isError"])
            r = self.call_tool("find_times", {"conditions": [{"signal": "tb.valid", "value": "1"}, {"signal": "tb.ready", "value": "1"}], "clock": "tb.clk", "limit": 1})
            self.assertFalse(r["isError"], r)
            self.assertEqual([m["cycle_index"] for m in r["structuredContent"]["data"]["matches"]], [2])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unknown_tool_is_error_result_not_success(self) -> None:
        resp = handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "missing", "arguments": {}}})
        self.assertIsNotNone(resp)
        result = resp["result"]
        self.assertTrue(result["isError"], result)
        self.assertFalse(result["structuredContent"]["ok"])

    def test_mcp_large_configure_and_index_required_smoke(self) -> None:
        tmp = make_tmp_dir()
        try:
            vcd = write_large_metadata_vcd(tmp / "large.vcd")
            result = self.call_tool("configure_waveform", {"vcd_path": str(vcd), "cache_dir": str(tmp / "cache"), "rebuild": True})
            self.assertFalse(result["isError"], result)
            data = result["structuredContent"]["data"]
            self.assertEqual(data["size_class"], "large")
            self.assertFalse(data["query_ready"])
            self.assertLess(data["configure_seconds"], 2.0)
            value = self.call_tool("value_at", {"signal": "tb.clk", "time": "0ns"})
            self.assertTrue(value["isError"], value)
            self.assertEqual(value["structuredContent"]["status"], "index_required")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mcp_false_pass_smoke(self) -> None:
        tmp = make_tmp_dir()
        try:
            vcd = tmp / "sample.vcd"
            shutil.copyfile(FIXTURES / "sample.vcd", vcd)
            cache = tmp / "cache"
            self.assertFalse(self.call_tool("configure_waveform", {"vcd_path": str(vcd), "cache_dir": str(cache), "rebuild": True})["isError"])
            missing = self.call_tool("value_at", {"signal": "missing_signal", "time": "1ns"})
            self.assertTrue(missing["isError"], missing)
            bad_time = self.call_tool("value_at", {"signal": "tb.valid", "time": "0.5ns"})
            self.assertTrue(bad_time["isError"], bad_time)
            with vcd.open("a", encoding="utf-8") as f:
                f.write("\n$comment changed $end\n")
            stale = self.call_tool("index_status", {"vcd_path": str(vcd), "cache_dir": str(cache)})
            self.assertFalse(stale["isError"], stale)
            self.assertEqual(stale["structuredContent"]["data"]["status"], "stale")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
