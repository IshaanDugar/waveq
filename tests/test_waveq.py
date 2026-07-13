from __future__ import annotations

import json
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
