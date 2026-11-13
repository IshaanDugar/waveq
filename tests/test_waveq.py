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


# Bus/handshake waveform: clk rises at 10c+5 (cycle c); stimulus changes at 10c, so "before the edge" is clean.
# Requests at cycles 1 (id 1), 2 (id 2), 5 (id 3); responses at cycles 4 (rid 2) and 6 (rid 1); id 3 never completes.
_BUS_VCD_HEADER = """$timescale 1 ns $end
$scope module tb $end
$var wire 1 ! clk $end
$scope module bus $end
$var wire 1 " req $end
$var wire 1 # rsp $end
$var wire 2 $ id [1:0] $end
$var wire 2 % rid [1:0] $end
$var wire 1 & stuck $end
$var wire 2 ' st [1:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
"""
_BUS_STIMULUS = {
    0: ['0"', '0#', 'b00 $', 'b00 %', '1&', "b00 '"],
    10: ['1"', 'b01 $', "b01 '"],
    20: ['b10 $', "b10 '"],
    30: ['0"', "b00 '"],
    40: ['1#', 'b10 %', "b01 '"],
    50: ['0#', '1"', 'b11 $', "b10 '"],
    60: ['0"', '1#', 'b01 %', "b00 '"],
    70: ['0#'],
}


def write_bus_vcd(path: pathlib.Path) -> pathlib.Path:
    lines = [_BUS_VCD_HEADER]
    for t in range(0, 101, 5):
        lines.append(f"#{t}")
        if t == 0:
            lines.append("$dumpvars")
        lines.append("1!" if t % 10 == 5 else "0!")
        lines.extend(_BUS_STIMULUS.get(t, []))
        if t == 0:
            lines.append("$end")
    return write_text(path, "\n".join(lines) + "\n")


# X/Z origins: d is x from the start, c goes z at 10, f goes partly z at 12 then fully z at 14,
# a goes x at 20, recovers at 25, and goes x again at 30; b goes x at 40 (and changes while x at 45);
# e (in core2, a different scope) goes x at 50.
_XZ_VCD = """$timescale 1 ns $end
$scope module tb $end
$scope module core $end
$var wire 1 ! a $end
$var wire 4 " b [3:0] $end
$var wire 1 # c $end
$var wire 1 $ d $end
$var wire 2 & f [1:0] $end
$upscope $end
$scope module core2 $end
$var wire 2 % e [1:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
$dumpvars
0!
b0000 "
0#
x$
b00 %
b00 &
$end
#10
z#
#12
b0z &
#14
bzz &
#20
x!
#25
0!
#30
x!
#40
b00x0 "
#45
b01x0 "
#50
b0x %
#60
"""


class DebugToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_tmp_dir()
        self.engine = WaveQEngine()

    def tearDown(self) -> None:
        self.engine.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load(self, vcd: pathlib.Path) -> None:
        r = self.engine.configure(str(vcd), cache_dir=str(self.tmp / "cache"), rebuild=True)
        self.assertTrue(r["ok"], r)

    def load_bus(self) -> None:
        self.load(write_bus_vcd(self.tmp / "bus.vcd"))
        self.assertTrue(self.engine.define_clock("tb.clk")["ok"])

    def test_list_scopes_hierarchy_and_segment_match(self) -> None:
        self.load(write_text(self.tmp / "xz.vcd", _XZ_VCD))
        r = self.engine.list_scopes()
        self.assertTrue(r["ok"], r)
        rows = {row["scope"]: row for row in r["data"]["scopes"]}
        self.assertEqual(rows["tb"]["signals"], 0)
        self.assertEqual(rows["tb"]["signals_in_subtree"], 6)
        self.assertEqual(rows["tb.core"]["signals"], 5)
        r = self.engine.list_scopes("core", max_depth=0)
        self.assertEqual([row["scope"] for row in r["data"]["scopes"]], ["tb.core"])
        self.assertEqual([s["leaf"] for s in r["data"]["root_signals"]], ["a", "b", "c", "d", "f"])
        self.assertEqual(self.engine.list_scopes("nope")["status"], "scope_not_found")

    def test_list_scopes_needs_metadata_only(self) -> None:
        vcd = write_large_metadata_vcd(self.tmp / "large.vcd")
        r = self.engine.configure(str(vcd), cache_dir=str(self.tmp / "cache"), rebuild=True)
        self.assertFalse(r["data"]["query_ready"])
        r = self.engine.list_scopes()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["scopes"][0]["signals_in_subtree"], 1)

    def test_x_report_orders_by_when_unknown_began(self) -> None:
        self.load(write_text(self.tmp / "xz.vcd", _XZ_VCD))
        r = self.engine.x_report(scope="core")
        self.assertTrue(r["ok"], r)
        got = [(e["signal"].split(" ")[0], e["unknown_since_tick"]) for e in r["data"]["signals"]]
        self.assertEqual(got, [("tb.core.d", 0), ("tb.core.c", 10), ("tb.core.f", 12), ("tb.core.a", 30), ("tb.core.b", 40)])
        self.assertTrue(r["data"]["signals"][0]["unknown_since_start"])
        everything = self.engine.x_report()
        self.assertIn("tb.core2.e [1:0]", [e["signal"] for e in everything["data"]["signals"]])
        early = self.engine.x_report("22ns", scope="core")
        self.assertEqual([(e["signal"], e["unknown_since_tick"]) for e in early["data"]["signals"]][-1], ("tb.core.a", 20))
        self.assertEqual(self.engine.x_report("5ns", scope="core2")["status"], "no_match")
        self.assertEqual(self.engine.x_report(scope="missing")["status"], "scope_not_found")

    def test_x_report_matches_value_at(self) -> None:
        self.load(write_text(self.tmp / "xz.vcd", _XZ_VCD))
        for t in range(0, 61, 3):
            reported = {e["signal"] for e in self.engine.x_report(t)["data"]["signals"]}
            expected = set()
            for scope in ("tb.core", "tb.core2"):
                for leaf in ("a", "b", "c", "d", "e", "f"):
                    v = self.engine.value_at(f"{scope}.{leaf}", t)
                    if v["ok"] and (v["data"]["has_x"] or v["data"]["has_z"]):
                        expected.add(v["data"]["signal"])
            self.assertEqual(reported, expected, t)

    def test_activity_finds_where_design_went_quiet(self) -> None:
        self.load_bus()
        r = self.engine.activity(ignore=["tb.clk"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["last_activity_tick"], 70)
        self.assertEqual(r["data"]["most_recent"][0]["signal"], "tb.bus.rsp")
        self.assertEqual([s["signal"] for s in r["data"]["idle"]], ["tb.bus.stuck"])
        self.assertEqual(r["data"]["idle"][0]["end_value"], "1")
        self.assertNotIn("tb.clk", [s["signal"] for s in r["data"]["most_recent"]])
        # Without ignoring it, the free-running clock masks the hang.
        self.assertEqual(self.engine.activity()["data"]["most_recent"][0]["signal"], "tb.clk")
        late = self.engine.activity(start="70ns", ignore=["tb.clk"])
        self.assertIsNone(late["data"]["last_activity_tick"])
        self.assertEqual(self.engine.activity(ignore=["nope"])["status"], "bad_ignore")

    def test_state_transitions_counts_and_dwell(self) -> None:
        self.load_bus()
        r = self.engine.state_transitions("tb.bus.st")
        self.assertTrue(r["ok"], r)
        trans = {(t["from"], t["to"]): t["count"] for t in r["data"]["transitions"]}
        self.assertEqual(trans, {("x", "0x0"): 1, ("0x0", "0x1"): 2, ("0x1", "0x2"): 2, ("0x2", "0x0"): 2})
        dwell = {v["value"]: (v["ticks"], v["visits"]) for v in r["data"]["values"]}
        self.assertEqual(dwell, {"0x0": (61, 3), "0x1": (20, 2), "0x2": (20, 2)})
        window = self.engine.state_transitions("tb.bus.st", "15ns", "45ns")
        dwell = {v["value"]: v["ticks"] for v in window["data"]["values"]}
        self.assertEqual(sum(dwell.values()), 31)
        self.assertEqual(dwell, {"0x1": 5 + 6, "0x2": 10, "0x0": 10})

    def test_find_times_payload_matches_values_at(self) -> None:
        self.load_bus()
        r = self.engine.find_times([{"signal": "tb.bus.req", "value": "1"}], clock="tb.clk", payload=["tb.bus.id", "tb.bus.st"])
        self.assertTrue(r["ok"], r)
        self.assertEqual([m["cycle_index"] for m in r["data"]["matches"]], [1, 2, 5])
        for m in r["data"]["matches"]:
            ref = self.engine.values_at(["tb.bus.id", "tb.bus.st"], m["sample_tick"])["data"]["values"]
            self.assertEqual(list(m["values"].values()), [v["hex"] for v in ref])
        intervals = self.engine.find_times([{"signal": "tb.bus.req", "value": "1"}], payload=["tb.bus.id"])
        self.assertEqual([(m["start_tick"], m["values"]["tb.bus.id [1:0]"]) for m in intervals["data"]["matches"]], [(10, "0x1"), (50, "0x3")])
        self.assertEqual(self.engine.find_times([{"signal": "tb.bus.req", "value": "1"}], payload=["nope"])["status"], "bad_payload")

    def test_values_at_cycle_sample_modes(self) -> None:
        self.load(FIXTURES / "sample.vcd")
        self.assertTrue(self.engine.define_clock("tb.clk")["ok"])
        # rst_n rises at 10ns; cycle 1 edge is 15ns. psel rises exactly on the cycle-2 edge (25ns).
        at = self.engine.values_at_cycle(["tb.u_apb.psel"], "tb.clk", 2)
        before = self.engine.values_at_cycle(["tb.u_apb.psel"], "tb.clk", 2, sample="before")
        self.assertEqual(at["data"]["values"][0]["value"], "1")
        self.assertEqual(before["data"]["values"][0]["value"], "0")
        self.assertEqual(before["data"]["tick"], 24)
        self.assertEqual(self.engine.values_at_cycle(["tb.u_apb.psel"], "tb.clk", 0, sample="before")["status"], "ok")
        self.assertEqual(self.engine.values_at_cycle(["tb.u_apb.psel"], "tb.clk", 0, sample="late")["status"], "bad_sample")

    def test_pair_events_in_order_and_by_id(self) -> None:
        self.load_bus()
        req = [{"signal": "tb.bus.req", "value": "1"}]
        rsp = [{"signal": "tb.bus.rsp", "value": "1"}]
        r = self.engine.pair_events("tb.clk", req, rsp)
        self.assertTrue(r["ok"], r)
        self.assertEqual([(p["request_cycle"], p["response_cycle"]) for p in r["data"]["pairs"]], [(1, 4), (2, 6)])
        self.assertEqual([u["cycle_index"] for u in r["data"]["unmatched_requests"]], [5])
        self.assertEqual(r["data"]["latency_cycles"]["max"], 4)
        by_id = self.engine.pair_events("tb.clk", req, rsp, request_id="tb.bus.id", response_id="tb.bus.rid")
        self.assertTrue(by_id["ok"], by_id)
        self.assertEqual(sorted((p["request_cycle"], p["response_cycle"], p["id"]) for p in by_id["data"]["pairs"]), [(1, 6, "0x1"), (2, 4, "0x2")])
        self.assertEqual(by_id["data"]["unmatched_requests"], [{"cycle_index": 5, "time": "55ns", "id": "0x3"}])
        self.assertEqual(by_id["data"]["latency_cycles"]["min"], 2)
        self.assertEqual(self.engine.pair_events("tb.clk", req, rsp, request_id="tb.bus.id")["status"], "bad_query")

    def test_pair_events_same_cycle_rule(self) -> None:
        self.load(FIXTURES / "sample.vcd")
        self.assertTrue(self.engine.define_clock("tb.clk")["ok"])
        setup = [{"signal": "tb.u_apb.psel", "value": "1"}, {"signal": "tb.u_apb.penable", "value": "0"}]
        access = [{"signal": "tb.u_apb.psel", "value": "1"}, {"signal": "tb.u_apb.penable", "value": "1"}]
        # The testbench drives on the clock edge, so the setup phase is only visible with sample="at".
        r = self.engine.pair_events("tb.clk", setup, access, sample="at")
        self.assertEqual([(p["request_cycle"], p["latency_cycles"]) for p in r["data"]["pairs"]], [(2, 1)])
        same = [{"signal": "tb.valid", "value": "1"}]
        strict = self.engine.pair_events("tb.clk", same, same)
        loose = self.engine.pair_events("tb.clk", same, same, allow_same_cycle=True)
        self.assertTrue(all(p["latency_cycles"] >= 1 for p in strict["data"]["pairs"]))
        self.assertTrue(all(p["latency_cycles"] == 0 for p in loose["data"]["pairs"]))
        self.assertEqual(loose["data"]["unmatched_request_count"], 0)

    def test_configure_index_modes(self) -> None:
        vcd = write_large_metadata_vcd(self.tmp / "large.vcd")
        full = self.engine.configure(str(vcd), cache_dir=str(self.tmp / "cache"), index="full")
        self.assertTrue(full["ok"], full)
        self.assertTrue(full["data"]["query_ready"])
        self.assertTrue(self.engine.value_at("tb.clk", 0)["ok"])
        self.assertEqual(self.engine.configure(str(vcd), cache_dir=str(self.tmp / "cache"), index="lazy")["status"], "bad_index_mode")
        small = self.engine.configure(str(FIXTURES / "sample.vcd"), cache_dir=str(self.tmp / "c2"), rebuild=True, index="metadata")
        self.assertFalse(small["data"]["query_ready"])

    def test_signal_memo_is_reset_on_reopen(self) -> None:
        self.load(FIXTURES / "sample.vcd")
        self.assertTrue(self.engine.value_at("tb.valid", 0)["ok"])
        self.assertIn("tb.valid", self.engine._sig_cache)
        self.load(write_text(self.tmp / "xz.vcd", _XZ_VCD))
        self.assertEqual(self.engine._sig_cache, {})
        self.assertFalse(self.engine.value_at("tb.valid", 0)["ok"])


class CacheLocationTests(unittest.TestCase):
    def setUp(self) -> None:
        import os
        self.saved = {k: os.environ.get(k) for k in ("WAVEQ_CACHE_DIR", "OneDrive", "OneDriveConsumer", "OneDriveCommercial")}
        for k in self.saved:
            os.environ.pop(k, None)
        self.tmp = make_tmp_dir()

    def tearDown(self) -> None:
        import os
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_override_and_synced_folder_fallback(self) -> None:
        import os
        from waveq.db import default_cache_dir_for, user_cache_dir
        plain = self.tmp / "plain"
        plain.mkdir()
        synced = self.tmp / "sync_root" / "proj"
        synced.mkdir(parents=True)
        vcd = plain / "a.vcd"
        expected_plain = user_cache_dir() if "onedrive" in str(plain).lower() else plain / ".waveq_cache"
        self.assertEqual(default_cache_dir_for(vcd), expected_plain)
        os.environ["OneDrive"] = str(self.tmp / "sync_root")
        self.assertEqual(default_cache_dir_for(synced / "a.vcd"), user_cache_dir())
        os.environ["WAVEQ_CACHE_DIR"] = str(self.tmp / "explicit")
        self.assertEqual(default_cache_dir_for(synced / "a.vcd"), (self.tmp / "explicit").resolve())


def divergence_vcd(path: pathlib.Path, *, unit: str = "ns", a_fall: int = 30, d_change: int | None = None, extra_var: str = "only_a") -> pathlib.Path:
    """Run with a 1-bit `a` (rises at 10, falls at a_fall), an 8-bit `d` (5 at 20, optionally changes at d_change), and one run-specific signal."""
    lines = [
        f"$timescale 1 {unit} $end",
        "$scope module tb $end",
        "$var wire 1 ! a $end",
        "$var wire 8 \" d [7:0] $end",
        f"$var wire 1 # {extra_var} $end",
        "$upscope $end",
        "$enddefinitions $end",
        "$dumpvars", "0!", "b00000000 \"", "0#", "$end",
        "#10", "1!",
        "#20", "b00000101 \"",
    ]
    events = {a_fall: "0!"}
    if d_change is not None:
        events[d_change] = "b00000110 \""
    for tick in sorted(events):
        lines += [f"#{tick}", events[tick]]
    lines += ["#40", ""]
    return write_text(path, "\n".join(lines))


class FirstDivergenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = make_tmp_dir()
        self.cache = str(self.tmp / "cache")
        # B differs from A: d is 6 from 25 (A keeps 5), and a falls at 35 instead of 30.
        self.a = divergence_vcd(self.tmp / "a.vcd", a_fall=30, extra_var="only_a")
        self.b = divergence_vcd(self.tmp / "b.vcd", a_fall=35, d_change=25, extra_var="only_b")
        self.engine = WaveQEngine()
        result = self.engine.configure(str(self.a), cache_dir=self.cache, rebuild=True)
        self.assertTrue(result["ok"], result)

    def tearDown(self) -> None:
        self.engine.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def diverge(self, **kwargs: object) -> dict[str, object]:
        return self.engine.first_divergence(str(self.b), other_cache_dir=self.cache, **kwargs)

    def test_first_divergence_orders_by_tick_and_reports_values(self) -> None:
        r = self.diverge()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], "ok")
        data = r["data"]
        self.assertEqual(data["first_divergence_tick"], 25)
        self.assertEqual(data["first_divergence_time"], "25ns")
        self.assertEqual(data["compared_count"], 2)
        self.assertEqual(data["diverged_count"], 2)
        self.assertEqual([(s["signal"], s["tick"]) for s in data["signals"]], [("tb.d [7:0]", 25), ("tb.a", 30)])
        self.assertEqual((data["signals"][0]["value_a"], data["signals"][0]["value_b"]), ("0x05", "0x06"))
        self.assertEqual((data["signals"][1]["value_a"], data["signals"][1]["value_b"]), ("0", "1"))
        self.assertEqual(data["context"]["tick"], 24)
        self.assertEqual(data["context"]["values"][0], {"signal": "tb.d [7:0]", "value_a": "0x05", "value_b": "0x05"})
        self.assertFalse(data["truncated"])

    def test_signals_only_in_one_run_are_counted_not_compared(self) -> None:
        data = self.diverge()["data"]
        self.assertEqual(data["only_in_a"], ["tb.only_a"])
        self.assertEqual(data["only_in_b"], ["tb.only_b"])
        self.assertEqual((data["only_in_a_count"], data["only_in_b_count"]), (1, 1))

    def test_identical_runs_report_no_match(self) -> None:
        same = divergence_vcd(self.tmp / "same.vcd", a_fall=30, extra_var="only_a")
        r = self.engine.first_divergence(str(same), other_cache_dir=self.cache)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], "no_match")
        self.assertIsNone(r["data"]["first_divergence_tick"])
        self.assertEqual(r["data"]["diverged_count"], 0)
        self.assertEqual(r["data"]["compared_count"], 3)

    def test_timescale_mismatch_fails_with_both_timescales(self) -> None:
        other = divergence_vcd(self.tmp / "ps.vcd", unit="ps")
        r = self.engine.first_divergence(str(other), other_cache_dir=self.cache)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["status"], "timescale_mismatch")
        self.assertEqual((r["data"]["timescale_a"], r["data"]["timescale_b"]), ("1ns", "1ps"))

    def test_window_start_inside_held_difference_diverges_at_start(self) -> None:
        data = self.diverge(start="27ns")["data"]
        self.assertEqual([(s["signal"], s["tick"]) for s in data["signals"]], [("tb.d [7:0]", 27), ("tb.a", 30)])
        data = self.diverge(start="32ns")["data"]
        self.assertEqual([(s["signal"], s["tick"]) for s in data["signals"]], [("tb.a", 32), ("tb.d [7:0]", 32)])
        data = self.diverge(start="36ns")["data"]
        self.assertEqual([(s["signal"], s["tick"]) for s in data["signals"]], [("tb.d [7:0]", 36)])

    def test_window_end_before_difference_is_no_match(self) -> None:
        r = self.diverge(end="24ns")
        self.assertEqual(r["status"], "no_match", r)

    def test_signal_and_scope_selection(self) -> None:
        r = self.diverge(signals=["tb.a"])
        self.assertEqual([(s["signal"], s["tick"]) for s in r["data"]["signals"]], [("tb.a", 30)])
        self.assertEqual(r["data"]["compared_count"], 1)
        r = self.diverge(scope="tb")
        self.assertEqual(r["data"]["diverged_count"], 2)

    def test_bad_inputs_fail(self) -> None:
        missing = self.diverge(signals=["tb.only_a"])
        self.assertFalse(missing["ok"], missing)
        self.assertEqual(missing["data"]["errors"][0]["status"], "not_in_other_run")
        self.assertFalse(self.diverge(signals=["tb.nope"])["ok"])
        self.assertFalse(self.diverge(start="50ns")["ok"])
        self.assertEqual(self.diverge(start="30ns", end="20ns")["status"], "bad_range")
        self.assertFalse(self.engine.first_divergence(str(self.tmp / "missing.vcd"), other_cache_dir=self.cache)["ok"])

    def test_limit_truncates(self) -> None:
        data = self.diverge(limit=1)["data"]
        self.assertEqual(len(data["signals"]), 1)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["diverged_count"], 2)

    def test_mcp_first_divergence_smoke(self) -> None:
        def call(name: str, arguments: dict[str, object]) -> dict[str, object]:
            resp = handle_message({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            self.assertIsNotNone(resp)
            return resp["result"]

        self.assertFalse(call("configure_waveform", {"vcd_path": str(self.a), "cache_dir": self.cache})["isError"])
        r = call("first_divergence", {"other_vcd_path": str(self.b), "other_cache_dir": self.cache})
        self.assertFalse(r["isError"], r)
        self.assertEqual(r["structuredContent"]["data"]["first_divergence_tick"], 25)
        bad = call("first_divergence", {"other_vcd_path": str(self.tmp / "missing.vcd")})
        self.assertTrue(bad["isError"], bad)


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
        for name in ("list_scopes", "x_report", "activity", "state_transitions", "pair_events"):
            self.assertIn(name, names)

    def test_mcp_find_times_smoke(self) -> None:
        tmp = make_tmp_dir()
        try:
            self.assertFalse(self.call_tool("configure_waveform", {"vcd_path": str(FIXTURES / "sample.vcd"), "cache_dir": str(tmp), "rebuild": True})["isError"])
            self.assertFalse(self.call_tool("define_clock", {"signal": "tb.clk"})["isError"])
            r = self.call_tool("find_times", {"conditions": [{"signal": "tb.valid", "value": "1"}, {"signal": "tb.ready", "value": "1"}], "clock": "tb.clk", "limit": 1})
            self.assertFalse(r["isError"], r)
            self.assertEqual([m["cycle_index"] for m in r["structuredContent"]["data"]["matches"]], [2])
            r = self.call_tool("pair_events", {
                "clock": "tb.clk",
                "request": [{"signal": "tb.u_apb.psel", "value": "1"}, {"signal": "tb.u_apb.penable", "value": "0"}],
                "response": [{"signal": "tb.u_apb.psel", "value": "1"}, {"signal": "tb.u_apb.penable", "value": "1"}],
                "sample": "at",
            })
            self.assertFalse(r["isError"], r)
            self.assertEqual(r["structuredContent"]["data"]["pair_count"], 1)
            r = self.call_tool("find_times", {"conditions": [{"signal": "tb.valid", "value": "1"}], "clock": "tb.clk", "payload": ["tb.data"], "limit": 1})
            self.assertEqual(r["structuredContent"]["data"]["matches"][0]["values"], {"tb.data [7:0]": "0xa5"})
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
