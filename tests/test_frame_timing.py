"""MCP frame-timing tools that work without the viewer: capture listing,
names and comparisons."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))
import frame_timing as ft


def summary(p50, gpu):
    return {"delivered_fps": 90.0, "frame_time_ms": {"p50": p50, "p99": p50, "p999": p50, "max": p50},
            "one_percent_low_fps": 80.0, "over_budget": {"percent": 0.0}, "frames": 900, "hitches": 0,
            "seconds": 10.0, "headroom_p50_ms": 2.0, "simulator_ms": {"p50": 0.3}, "gpu": gpu, "target_hz": 90,
            "capture": {"path": "capture.xrft"}, "worst_frames": []}


class FrameTimingTests(unittest.TestCase):
    def test_tools_are_described(self):
        names = [spec[0] for spec in ft.TOOL_SPECS]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(name.startswith("perf_") for name in names))

    def test_comparison_without_gpu_timing(self):
        # A capture recorded with GPU timing off says "gpu": null.
        baseline, candidate = summary(8.0, None), summary(9.0, {"p50_ms": 7.0, "p99_ms": 8.0})
        with mock.patch.object(ft, "analyze_capture", side_effect=[baseline, candidate]):
            result = ft.compare_captures({"baseline": "a.xrft", "candidate": "b.xrft"})
        self.assertNotIn("gpu_p50_ms", result["delta"])
        self.assertIn("frame_time_p50_ms", result["regressed"])

    def test_listing_skips_damaged_files(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(ft, "CAPTURES_DIR", Path(folder)):
            Path(folder, "short.xrft").write_bytes(b"XRFT")
            Path(folder, "foreign.xrft").write_bytes(b"\0" * ft._CAPTURE_HEADER.size)
            Path(folder, "huge.xrft").write_bytes(self._header(metadata_bytes=1 << 40))
            Path(folder, "bad-utf8.xrft").write_bytes(self._header(metadata_bytes=2) + b"\xff\xfe")
            Path(folder, "good.xrft").write_bytes(self._header(metadata_bytes=2) + b"{}")
            listed = ft.list_captures({})
        self.assertEqual([entry["file"] for entry in listed["captures"]], ["good.xrft"])

    def test_capture_names_are_never_reused(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(ft, "CAPTURES_DIR", Path(folder)):
            first = ft._capture_path("run", "game.exe")
            first.write_bytes(b"")
            second = ft._capture_path("run", "game.exe")
        self.assertNotEqual(first, second)
        self.assertTrue(second.name.startswith("game_") and second.suffix == ".xrft")

    @staticmethod
    def _header(metadata_bytes):
        values = list(struct.unpack(ft._CAPTURE_HEADER.format, bytes(ft._CAPTURE_HEADER.size)))
        values[0] = b"XRFTCAP\0"
        values[7] = metadata_bytes
        return ft._CAPTURE_HEADER.pack(*values)


if __name__ == "__main__":
    unittest.main()
