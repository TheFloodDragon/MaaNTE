"""离线分析工具的行为契约：只做描述性统计，坏数据不致命，模型只读。"""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "_maante_autofish_analysis", ROOT / "tools" / "analyze_autofish_learning.py"
)
analysis = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = analysis
_SPEC.loader.exec_module(analysis)


def transition(**changes):
    record = {
        "type": "transition",
        "mode": "learn",
        "signature": "sig-a",
        "feature_version": "relative-motion-1",
        "reward_version": "geometry-time-1",
        "frame_id": 1,
        "next_frame_id": 2,
        "dt": 0.05,
        "capture_ms": 8.0,
        "action_delay_ms": 3.0,
        "held_ms": 12.0,
        "key": 65,
        "rule_action": -0.4,
        "requested_residual": 0.05,
        "residual": 0.05,
        "final_action": -0.35,
        "inside_target": False,
        "next_inside": True,
        "error_px": 30.0,
        "next_error_px": 4.0,
        "edge_margin_px": -5.0,
        "next_edge_margin_px": 6.0,
        "target_width": 80.0,
        "reward": 0.5,
        "reward_ema": 0.1,
        "updated": True,
    }
    record.update(changes)
    return record


def summary(**changes):
    record = {
        "type": "summary",
        "mode": "learn",
        "signature": "sig-a",
        "samples": 3,
        "recorded": 3,
        "dropped": 2,
        "drop_reasons": {"unreliable": 1, "feedback_stale": 1},
        "reward_ema": 0.1,
        "update_count": 3,
    }
    record.update(changes)
    return record


class LoadJsonlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _write(self, name, lines):
        path = self.root / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_bad_and_unknown_lines_are_counted_not_fatal(self):
        path = self._write(
            "mixed.jsonl",
            [
                json.dumps(transition()),
                "not json at all",
                json.dumps([1, 2, 3]),
                json.dumps({"type": "unexpected"}),
                "",
                json.dumps(summary()),
            ],
        )
        loaded = analysis.load_jsonl(path)
        self.assertEqual(len(loaded["transitions"]), 1)
        self.assertEqual(len(loaded["summaries"]), 1)
        # 非法行、非字典、未知类型都计入坏行，空行忽略。
        self.assertEqual(loaded["bad_lines"], 3)


class SummarizeTests(unittest.TestCase):
    def test_group_ratios_expose_denominator_and_coverage(self):
        records = [
            transition(inside_target=True, next_inside=True, key=None, final_action=0.0, residual=0.0, updated=False),
            transition(inside_target=False, next_inside=True),
            transition(inside_target=False, next_inside=False, next_edge_margin_px=-20.0, reward=-0.3),
        ]
        group = analysis.summarize_group(records)
        self.assertEqual(group["transitions"], 3)
        self.assertAlmostEqual(group["inside_coverage"], 1 / 3)
        self.assertAlmostEqual(group["outside_coverage"], 2 / 3)
        self.assertAlmostEqual(group["pressed_ratio"], 2 / 3)
        self.assertAlmostEqual(group["updated_ratio"], 2 / 3)
        self.assertAlmostEqual(group["next_inside_frame_ratio"], 2 / 3)
        matrix = group["transition_matrix"]
        self.assertEqual(matrix["labeled"], 3)
        self.assertEqual(matrix["cells"]["out->in"]["count"], 1)
        self.assertEqual(matrix["cells"]["in->in"]["count"], 1)
        self.assertEqual(matrix["cells"]["out->out"]["count"], 1)

    def test_time_weighted_inside_uses_dt_not_frame_counts(self):
        records = [
            transition(dt=0.30, next_inside=True),
            transition(dt=0.01, next_inside=False),
            transition(dt=float("nan"), next_inside=False),
        ]
        group = analysis.summarize_group(records)
        # 帧计数近似 50%，但时间加权由长 dt 帧主导。
        self.assertAlmostEqual(group["next_inside_frame_ratio"], 1 / 3)
        time_inside = group["time_weighted_inside"]
        self.assertAlmostEqual(time_inside["total_dt_sec"], 0.31)
        self.assertAlmostEqual(time_inside["next_inside_time_ratio"], 0.30 / 0.31)

    def test_quantiles_handle_single_and_empty(self):
        self.assertIsNone(analysis._quantiles([]))
        one = analysis._quantiles([5.0])
        self.assertEqual((one["min"], one["p50"], one["max"], one["mean"]), (5.0, 5.0, 5.0, 5.0))

    def test_summary_drop_reasons_are_merged(self):
        merged = analysis.summarize_summaries([summary(), summary(drop_reasons={"unreliable": 2})])
        self.assertEqual(merged["rounds"], 2)
        self.assertEqual(merged["dropped_samples"], 4)
        self.assertEqual(merged["drop_reasons"]["unreliable"], 3)
        self.assertEqual(merged["drop_reasons"]["feedback_stale"], 1)


class BuildReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_report_splits_incompatible_groups(self):
        source = {
            "path": "memory",
            "transitions": [
                transition(signature="sig-a"),
                transition(signature="sig-a"),
                transition(signature="sig-b", feature_version="other"),
            ],
            "summaries": [summary()],
            "bad_lines": 2,
        }
        report = analysis.build_report([source], [])
        self.assertEqual(report["totals"]["transitions"], 3)
        self.assertEqual(report["totals"]["bad_lines"], 2)
        self.assertEqual(report["totals"]["groups"], 2)
        # 分组按样本数降序，最大的分组在前，避免把不兼容特征混算。
        self.assertEqual(report["groups"][0]["transitions"], 2)
        self.assertEqual(report["groups"][0]["signature"], "sig-a")

    def test_inspect_model_reports_health_without_raising(self):
        good = self.root / "good.npz"
        np.savez(
            good,
            metadata=np.array(json.dumps({"schema": 1})),
            features=np.asarray([str(i) for i in range(13)]),
            weights=np.zeros(13),
            sample_count=np.int64(4),
            update_count=np.int64(2),
            reward_ema=np.float64(0.2),
        )
        report = analysis.inspect_model(good)
        self.assertTrue(report["fields_ok"])
        self.assertTrue(report["weight_count_ok"])
        self.assertTrue(report["weights_finite"])
        self.assertEqual(report["sample_count"], 4)

        corrupt = self.root / "corrupt.npz"
        corrupt.write_bytes(b"not a real npz archive")
        broken = analysis.inspect_model(corrupt)
        self.assertIn("error", broken)

    def test_main_writes_json_and_reports_missing_data(self):
        empty_root = self.root / "empty"
        empty_root.mkdir()
        # 无数据时返回非零，且不因缺目录崩溃。
        self.assertEqual(analysis.main(["--data", str(empty_root / "missing.jsonl")]), 1)

        data = self.root / "round.jsonl"
        data.write_text(json.dumps(transition()) + "\n" + json.dumps(summary()) + "\n", encoding="utf-8")
        out = self.root / "report.json"
        self.assertEqual(analysis.main(["--data", str(data), "--json", str(out)]), 0)
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(written["totals"]["transitions"], 1)
        self.assertEqual(written["round_summaries"]["rounds"], 1)


if __name__ == "__main__":
    unittest.main()
