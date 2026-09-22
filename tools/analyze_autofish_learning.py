#!/usr/bin/env python3
"""离线分析新版钓鱼学习 JSONL 与模型健康度。

这是描述性统计工具：只汇总录制到的观测转移，不训练、不修改参数，
也不推进游戏动力学。所有比率都标注分母与覆盖率，任何"下一帧在框"
比例都不是钓鱼成功率或换参收益，请勿据此断言实机表现。

用法::

    python tools/analyze_autofish_learning.py            # 自动发现默认目录
    python tools/analyze_autofish_learning.py --data a.jsonl b.jsonl
    python tools/analyze_autofish_learning.py --model config/autofish/xxx.npz
    python tools/analyze_autofish_learning.py --json report.json
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
from typing import Any, Iterable

# 与 agent/custom/action/AutoFish/fish_learning.py 保持一致，仅用于校验读入模型。
EXPECTED_FEATURE_COUNT = 13
MODEL_FIELDS = {"metadata", "features", "weights", "sample_count", "update_count", "reward_ema"}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _numbers(records: Iterable[dict], key: str) -> list[float]:
    values = []
    for record in records:
        value = record.get(key)
        if _is_number(value):
            values.append(float(value))
    return values


def _quantiles(values: list[float]) -> dict[str, float] | None:
    data = sorted(values)
    if not data:
        return None

    def pick(q: float) -> float:
        if len(data) == 1:
            return data[0]
        position = q * (len(data) - 1)
        lower = math.floor(position)
        upper = min(lower + 1, len(data) - 1)
        return data[lower] + (data[upper] - data[lower]) * (position - lower)

    return {
        "count": len(data),
        "min": data[0],
        "p50": pick(0.5),
        "p90": pick(0.9),
        "p99": pick(0.99),
        "max": data[-1],
        "mean": sum(data) / len(data),
    }


def load_jsonl(path: Path) -> dict[str, Any]:
    """逐行读取，坏行计数但不中断；区分 transition 与 summary 记录。"""
    transitions: list[dict] = []
    summaries: list[dict] = []
    bad_lines = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                bad_lines += 1
                continue
            if not isinstance(record, dict):
                bad_lines += 1
                continue
            kind = record.get("type")
            if kind == "transition":
                transitions.append(record)
            elif kind == "summary":
                summaries.append(record)
            else:
                bad_lines += 1
    return {"path": str(path), "transitions": transitions, "summaries": summaries, "bad_lines": bad_lines}


def _group_key(record: dict) -> tuple:
    return (
        record.get("mode", "?"),
        record.get("signature", "?"),
        record.get("feature_version", "?"),
        record.get("reward_version", "?"),
    )


def _transition_matrix(transitions: list[dict]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    known = 0
    for record in transitions:
        prev, following = record.get("inside_target"), record.get("next_inside")
        if isinstance(prev, bool) and isinstance(following, bool):
            counts[("in" if prev else "out") + "->" + ("in" if following else "out")] += 1
            known += 1
    matrix = {name: {"count": counts.get(name, 0)} for name in ("in->in", "in->out", "out->in", "out->out")}
    if known:
        for name in matrix:
            matrix[name]["rate_of_labeled"] = matrix[name]["count"] / known
    return {"labeled": known, "cells": matrix}


def _time_weighted_inside(transitions: list[dict]) -> dict[str, Any] | None:
    total_dt = 0.0
    inside_dt = 0.0
    for record in transitions:
        dt = record.get("dt")
        if not _is_number(dt) or dt <= 0:
            continue
        total_dt += dt
        if record.get("next_inside") is True:
            inside_dt += dt
    if total_dt <= 0:
        return None
    return {"total_dt_sec": total_dt, "next_inside_time_ratio": inside_dt / total_dt}


def summarize_group(transitions: list[dict]) -> dict[str, Any]:
    total = len(transitions)
    inside_prev = sum(1 for record in transitions if record.get("inside_target") is True)
    updated = sum(1 for record in transitions if record.get("updated") is True)
    pressed = [record for record in transitions if record.get("key") in (65, 68)]
    inside_actions = [
        abs(record["final_action"])
        for record in transitions
        if record.get("inside_target") is True and _is_number(record.get("final_action"))
    ]
    outside_actions = [
        abs(record["final_action"])
        for record in transitions
        if record.get("inside_target") is False and _is_number(record.get("final_action"))
    ]
    return {
        "transitions": total,
        "inside_coverage": (inside_prev / total) if total else None,
        "outside_coverage": ((total - inside_prev) / total) if total else None,
        "updated_ratio": (updated / total) if total else None,
        "pressed_ratio": (len(pressed) / total) if total else None,
        "next_inside_frame_ratio": (
            sum(1 for record in transitions if record.get("next_inside") is True) / total
            if total
            else None
        ),
        "time_weighted_inside": _time_weighted_inside(transitions),
        "transition_matrix": _transition_matrix(transitions),
        "latency": {
            "dt_sec": _quantiles(_numbers(transitions, "dt")),
            "capture_ms": _quantiles(_numbers(transitions, "capture_ms")),
            "action_delay_ms": _quantiles(_numbers(transitions, "action_delay_ms")),
            "held_ms": _quantiles(_numbers(transitions, "held_ms")),
        },
        "action_strength": {
            "inside_abs_final": _quantiles(inside_actions),
            "outside_abs_final": _quantiles(outside_actions),
            "residual_abs": _quantiles([abs(v) for v in _numbers(transitions, "residual")]),
        },
        "reward_heuristic": _quantiles(_numbers(transitions, "reward")),
    }


def summarize_summaries(summaries: list[dict]) -> dict[str, Any]:
    drops: Counter[str] = Counter()
    for record in summaries:
        reasons = record.get("drop_reasons")
        if isinstance(reasons, dict):
            for reason, count in reasons.items():
                if _is_number(count):
                    drops[str(reason)] += int(count)
    return {
        "rounds": len(summaries),
        "recorded_samples": sum(int(r["recorded"]) for r in summaries if _is_number(r.get("recorded"))),
        "dropped_samples": sum(int(r["dropped"]) for r in summaries if _is_number(r.get("dropped"))),
        "drop_reasons": dict(drops),
    }


def inspect_model(path: Path) -> dict[str, Any]:
    try:
        import numpy as np
    except ImportError:
        return {"path": str(path), "error": "numpy unavailable"}
    try:
        with np.load(path, allow_pickle=False) as data:
            fields = set(data.files)
            report: dict[str, Any] = {"path": str(path), "fields_ok": fields == MODEL_FIELDS}
            if "weights" in fields:
                weights = data["weights"]
                report["weight_shape"] = list(weights.shape)
                report["weight_count_ok"] = weights.shape == (EXPECTED_FEATURE_COUNT,)
                report["weights_finite"] = bool(np.all(np.isfinite(weights)))
                report["weight_abs_max"] = float(np.max(np.abs(weights))) if weights.size else 0.0
            for name in ("sample_count", "update_count"):
                if name in fields and data[name].shape == ():
                    report[name] = int(data[name].item())
            if "reward_ema" in fields and data["reward_ema"].shape == ():
                report["reward_ema"] = float(data["reward_ema"].item())
            if "metadata" in fields and data["metadata"].shape == ():
                try:
                    report["metadata"] = json.loads(str(data["metadata"].item()))
                except (ValueError, TypeError):
                    report["metadata"] = None
            return report
    except Exception as exc:  # 分析工具遇到损坏模型只报告，不抛出。
        return {"path": str(path), "error": type(exc).__name__ + ": " + str(exc)}


def build_report(sources: list[dict], models: list[Path]) -> dict[str, Any]:
    transitions: list[dict] = []
    summaries: list[dict] = []
    bad_lines = 0
    for source in sources:
        transitions.extend(source["transitions"])
        summaries.extend(source["summaries"])
        bad_lines += source["bad_lines"]

    groups: dict[tuple, list[dict]] = {}
    for record in transitions:
        groups.setdefault(_group_key(record), []).append(record)

    return {
        "files": [source["path"] for source in sources],
        "totals": {
            "transitions": len(transitions),
            "summaries": len(summaries),
            "bad_lines": bad_lines,
            "groups": len(groups),
        },
        "groups": [
            {
                "mode": key[0],
                "signature": key[1],
                "feature_version": key[2],
                "reward_version": key[3],
                **summarize_group(records),
            }
            for key, records in sorted(groups.items(), key=lambda item: -len(item[1]))
        ],
        "round_summaries": summarize_summaries(summaries),
        "models": [inspect_model(path) for path in models],
    }


def _format_quantiles(label: str, stats: dict[str, float] | None, indent: str) -> list[str]:
    if not stats:
        return [indent + label + ": (无数据)"]
    return [
        indent
        + "%s: n=%d min=%.3f p50=%.3f p90=%.3f p99=%.3f max=%.3f mean=%.3f"
        % (label, stats["count"], stats["min"], stats["p50"], stats["p90"], stats["p99"], stats["max"], stats["mean"])
    ]


def _percent(value: float | None) -> str:
    return "n/a" if value is None else "%.2f%%" % (value * 100)


def print_report(report: dict[str, Any]) -> None:
    lines: list[str] = []
    totals = report["totals"]
    lines.append("# 钓鱼学习数据分析（描述性统计，非成功率/非换参收益）")
    lines.append("文件: " + (", ".join(report["files"]) or "(无)"))
    lines.append(
        "转移=%d 轮次摘要=%d 坏行=%d 分组=%d"
        % (totals["transitions"], report["totals"]["summaries"], totals["bad_lines"], totals["groups"])
    )

    for group in report["groups"]:
        lines.append("")
        lines.append(
            "## mode=%s signature=%s feature=%s reward=%s"
            % (group["mode"], group["signature"], group["feature_version"], group["reward_version"])
        )
        lines.append(
            "  转移数(分母)=%d 框内覆盖=%s 框外覆盖=%s 实际更新占比=%s 按键占比=%s"
            % (
                group["transitions"],
                _percent(group["inside_coverage"]),
                _percent(group["outside_coverage"]),
                _percent(group["updated_ratio"]),
                _percent(group["pressed_ratio"]),
            )
        )
        time_inside = group["time_weighted_inside"]
        lines.append(
            "  下一帧在框: 帧计数=%s 时间加权=%s（分母见括号，勿当作钓鱼成功率）"
            % (
                _percent(group["next_inside_frame_ratio"]),
                _percent(time_inside["next_inside_time_ratio"]) if time_inside else "n/a",
            )
        )
        matrix = group["transition_matrix"]
        cells = matrix["cells"]
        lines.append(
            "  状态转移(已标注 %d): in->in=%d in->out=%d out->in=%d out->out=%d"
            % (
                matrix["labeled"],
                cells["in->in"]["count"],
                cells["in->out"]["count"],
                cells["out->in"]["count"],
                cells["out->out"]["count"],
            )
        )
        lines.append("  延迟分位数:")
        for label, key in (("帧间隔 dt(s)", "dt_sec"), ("截图 capture_ms", "capture_ms"),
                           ("观测→按下 ms", "action_delay_ms"), ("按键时长 ms", "held_ms")):
            lines.extend(_format_quantiles(label, group["latency"][key], "    "))
        lines.append("  动作强度分位数:")
        lines.extend(_format_quantiles("框内 |final|", group["action_strength"]["inside_abs_final"], "    "))
        lines.extend(_format_quantiles("框外 |final|", group["action_strength"]["outside_abs_final"], "    "))
        lines.extend(_format_quantiles("生效残差 |residual|", group["action_strength"]["residual_abs"], "    "))
        lines.extend(_format_quantiles("启发式奖励 reward", group["reward_heuristic"], "  "))

    summaries = report["round_summaries"]
    lines.append("")
    lines.append("## 轮次摘要")
    lines.append(
        "  轮数=%d 记录样本=%d 丢弃样本=%d"
        % (summaries["rounds"], summaries["recorded_samples"], summaries["dropped_samples"])
    )
    if summaries["drop_reasons"]:
        reasons = ", ".join("%s=%d" % (reason, count) for reason, count in sorted(summaries["drop_reasons"].items()))
        lines.append("  丢弃原因: " + reasons)

    for model in report["models"]:
        lines.append("")
        lines.append("## 模型 " + model["path"])
        if "error" in model:
            lines.append("  无法读取: " + model["error"])
            continue
        lines.append(
            "  字段完整=%s 权重形状=%s 权重有限=%s |w|max=%.4f"
            % (
                model.get("fields_ok"),
                model.get("weight_shape"),
                model.get("weights_finite"),
                model.get("weight_abs_max", float("nan")),
            )
        )
        lines.append(
            "  样本数=%s 更新数=%s reward_ema=%s"
            % (model.get("sample_count"), model.get("update_count"), model.get("reward_ema"))
        )

    lines.append("")
    lines.append("解读须知: 以上为录制样本的条件描述性统计。下一帧在框比例、状态转移、")
    lines.append("奖励均基于历史观测子集，受采样节奏与选择偏差影响，不能作为实机钓鱼")
    lines.append("成功率或调参收益的证据；实机效果需在游戏内验证。")
    print("\n".join(lines))


def _default_sources() -> tuple[list[Path], list[Path]]:
    root = Path.cwd()
    data = sorted((root / "debug" / "custom" / "autofish").glob("*.jsonl"))
    models = sorted((root / "config" / "autofish").glob("*.npz"))
    return data, models


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="分析新版钓鱼学习 JSONL（描述性统计，不训练、不改参）")
    parser.add_argument("--data", nargs="*", type=Path, help="JSONL 文件路径；缺省自动扫描 debug/custom/autofish/")
    parser.add_argument("--model", nargs="*", type=Path, help="模型 NPZ 路径；缺省自动扫描 config/autofish/")
    parser.add_argument("--json", type=Path, help="将结构化报告写入该 JSON 文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    default_data, default_models = _default_sources()
    data_paths = args.data if args.data else default_data
    model_paths = args.model if args.model else default_models

    sources = []
    for path in data_paths:
        if path.is_file():
            sources.append(load_jsonl(path))
        else:
            print("跳过不存在的数据文件: %s" % path)
    if not sources:
        print("未找到可分析的 JSONL；请用 --data 指定，或先在 learn 模式下运行以生成数据。")
        return 1

    report = build_report(sources, [path for path in model_paths if path.is_file()])
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print("已写入 JSON 报告: %s" % args.json)
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
