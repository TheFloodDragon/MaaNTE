"""FishPro 控条的离线算法验证。

不依赖游戏、不依赖 MaaFramework 控制器，用合成图与假控制器覆盖六个层面：

1. 识别层：合成绿条 + 黄线图，验证命中位置、干扰块过滤与暗色场景；
2. 状态层：平滑、速度估计、跳变抑制、宽度确认与预测量；
3. 规则层：安全区静默、边缘恢复、框外强拉、接近降档、方向与上限；
4. 执行层：死区、脉冲时长、长按判定、按键序列与释放保证；
5. 学习层：特征维度、奖励符号、在线更新、持久化与回放；
6. 闭环：模拟控条物理，统计留框率，并覆盖丢帧宽限与超限重置。

用法：``python tools/verify_fishpro.py``
"""

from __future__ import annotations

import random
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent"))

import cv2  # noqa: E402

from agent.custom.action.AutoFish.fishpro.config import (  # noqa: E402
    FishProConfig,
    load_fish_pro_config,
)
from agent.custom.action.AutoFish.fishpro.executor import (  # noqa: E402
    KEY_A,
    KEY_D,
    ActionExecutor,
)
from agent.custom.action.AutoFish.fishpro.learning import (  # noqa: E402
    FEATURE_COUNT,
    ResidualPolicy,
)
from agent.custom.action.AutoFish.fishpro.policy import (  # noqa: E402
    action_cap_for_observation,
    compute_rule_action,
    merge_policy_action,
)
from agent.custom.action.AutoFish.fishpro.runtime import (  # noqa: E402
    REASON_CONTROL_FINISHED,
    REASON_LOST_ABORT,
    FishProSession,
)
from agent.custom.action.AutoFish.fishpro.state import (  # noqa: E402
    ControlState,
    build_observation,
    can_recover_from_missing,
    mark_missing_state,
    missing_fallback_action,
    update_cursor_state,
)
from agent.custom.action.AutoFish.fishpro.vision import (  # noqa: E402
    Detection,
    detect_control,
    find_green_target,
    find_yellow_cursor,
)

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(f"{label}{f' | {detail}' if detail else ''}")


def section(title: str) -> None:
    print(f"\n--- {title} ---")


# ============ 合成图工具 ============

ROI_W, ROI_H = 512, 58
BAR_TOP, BAR_HEIGHT = 24, 12


def make_frame(
    bar_left: int,
    bar_width: int,
    cursor_x: int | None,
    *,
    dusk: bool = False,
    noise_blocks: tuple[tuple[int, int, int, int], ...] = (),
    frame_w: int = 1280,
    frame_h: int = 720,
    roi_origin: tuple[int, int] = (384, 21),
) -> np.ndarray:
    """构造一张 1280x720 画面，在 ROI 内绘制绿条与黄色指针。"""
    frame = np.zeros((frame_h, frame_w, 3), dtype=np.uint8)
    frame[:, :] = (30, 25, 20)
    ox, oy = roi_origin

    green = (60, 150, 60) if not dusk else (50, 115, 48)
    cv2.rectangle(
        frame,
        (ox + bar_left, oy + BAR_TOP),
        (ox + bar_left + bar_width, oy + BAR_TOP + BAR_HEIGHT),
        green,
        -1,
    )

    for bx, by, bw, bh in noise_blocks:
        cv2.rectangle(
            frame, (ox + bx, oy + by), (ox + bx + bw, oy + by + bh), green, -1
        )

    if cursor_x is not None:
        # 暗色取值刻意选在 primary 色域之外（V<150），只能由 dusk 掩码命中，
        # 从而真正覆盖黄昏分支。
        yellow = (40, 205, 235) if not dusk else (55, 110, 140)
        cv2.rectangle(
            frame,
            (ox + cursor_x - 2, oy + BAR_TOP - 3),
            (ox + cursor_x + 2, oy + BAR_TOP + BAR_HEIGHT + 3),
            yellow,
            -1,
        )
    return frame


def roi_of(frame: np.ndarray, roi=(384, 21, ROI_W, ROI_H)) -> np.ndarray:
    x, y, w, h = roi
    return np.ascontiguousarray(frame[y : y + h, x : x + w, :3])


# ============ 1. 识别层 ============


def verify_vision() -> None:
    section("1. 识别层")
    config = FishProConfig()

    frame = make_frame(120, 160, 200)
    roi = roi_of(frame)
    target = find_green_target(roi, config)
    check(target is not None, "绿条应被识别")
    if target is not None:
        check(
            abs(target.center_x - 200) <= 3,
            "绿条中心应接近 200",
            f"实际 {target.center_x}",
        )
        check(
            abs(target.width - 160) <= 4,
            "绿条宽度应接近 160",
            f"实际 {target.width}",
        )

    cursor = find_yellow_cursor(roi, config, target=target)
    check(cursor is not None, "光标应被识别")
    if cursor is not None:
        check(
            abs(cursor.center_x - 200) <= 3,
            "光标中心应接近 200",
            f"实际 {cursor.center_x}",
        )

    # 光标位于绿条外仍应命中（在 x margin 内）。
    frame_out = make_frame(120, 160, 100)
    target_out, cursor_out = detect_control(roi_of(frame_out), config)
    check(target_out is not None and cursor_out is not None, "框外光标应被识别")
    if cursor_out is not None:
        check(
            abs(cursor_out.center_x - 100) <= 3,
            "框外光标中心应接近 100",
            f"实际 {cursor_out.center_x}",
        )

    # 暗色/黄昏场景。
    frame_dusk = make_frame(150, 140, 210, dusk=True)
    target_dusk, cursor_dusk = detect_control(roi_of(frame_dusk), config)
    check(target_dusk is not None, "暗色场景绿条应被识别")
    check(cursor_dusk is not None, "暗色场景光标应被识别")

    # 干扰：一个又高又方的绿块（长宽比不足）应被过滤，真绿条仍胜出。
    # 干扰块与绿条保持足够间距，避免被水平桥接闭运算连成一体。
    frame_noise = make_frame(150, 150, 220, noise_blocks=((20, 4, 26, 44),))
    target_noise = find_green_target(roi_of(frame_noise), config)
    check(target_noise is not None, "干扰下绿条应仍被识别")
    if target_noise is not None:
        check(
            abs(target_noise.center_x - 225) <= 6,
            "干扰块不应被选为绿条",
            f"实际中心 {target_noise.center_x}",
        )
        check(
            abs(target_noise.width - 150) <= 6,
            "干扰块不应污染绿条宽度",
            f"实际宽度 {target_noise.width}",
        )

    # 只有干扰块时应无命中，确认过滤条件本身有效。
    frame_only_noise = make_frame(
        -500, 1, None, noise_blocks=((60, 4, 26, 44),)
    )
    check(
        find_green_target(roi_of(frame_only_noise), config) is None,
        "仅有方形干扰块时不应命中绿条",
    )

    # 无光标帧。
    frame_nocursor = make_frame(120, 160, None)
    target_nc, cursor_nc = detect_control(roi_of(frame_nocursor), config)
    check(target_nc is not None, "无光标帧绿条仍应识别")
    check(cursor_nc is None, "无光标帧不应误报光标")

    # 帧间连续性：两个候选时应偏向靠近上一帧的那个。
    frame_two = make_frame(120, 160, 160)
    roi_two = roi_of(frame_two)
    cv2.rectangle(roi_two, (238, BAR_TOP - 3), (242, BAR_TOP + BAR_HEIGHT + 3), (40, 205, 235), -1)
    t2 = find_green_target(roi_two, config)
    near = find_yellow_cursor(roi_two, config, last_center_x=158, target=t2)
    far = find_yellow_cursor(roi_two, config, last_center_x=242, target=t2)
    check(near is not None and far is not None, "双候选均应有命中")
    if near is not None and far is not None:
        check(
            near.center_x < far.center_x,
            "连续性惩罚应使命中偏向上一帧位置",
            f"near={near.center_x} far={far.center_x}",
        )

    # 越界 ROI 与空图。
    small = np.zeros((10, 10, 3), dtype=np.uint8)
    check(find_green_target(None, config) is None, "空输入应返回 None")
    check(
        detect_control(roi_of(small, (0, 0, 10, 10)), config) == (None, None),
        "过小画面不应命中",
    )
    print(f"识别层检查完成，累计 {CHECKS} 项")


# ============ 2. 状态层 ============


def make_detection(center: float, width: float) -> Detection:
    left = int(round(center - width / 2))
    right = int(round(center + width / 2))
    return Detection(
        center_x=float(center),
        left=left,
        right=right,
        top=BAR_TOP,
        bottom=BAR_TOP + BAR_HEIGHT,
        box=(left, BAR_TOP, right - left, BAR_HEIGHT),
        area=float((right - left) * BAR_HEIGHT),
    )


def cursor_detection(center: float) -> Detection:
    return Detection(
        center_x=float(center),
        left=int(center) - 2,
        right=int(center) + 2,
        top=BAR_TOP - 3,
        bottom=BAR_TOP + BAR_HEIGHT + 3,
        box=(int(center) - 2, BAR_TOP - 3, 5, BAR_HEIGHT + 6),
        area=float(5 * (BAR_HEIGHT + 6)),
    )


def verify_state() -> None:
    section("2. 状态层")
    config = FishProConfig()
    state = ControlState()

    # 首帧初始化：速度为 0，位置直接采用观测。
    x0, v0 = update_cursor_state(cursor_detection(200.0), state, config, 0.000)
    check(abs(x0 - 200.0) < 1e-6, "首帧光标位置应直接采用观测")
    check(abs(v0) < 1e-6, "首帧速度应为 0")

    # 匀速右移：速度应为正且逐步收敛到真实值附近。
    pos = 200.0
    t = 0.0
    for _ in range(12):
        pos += 6.0
        t += 0.02
        x, v = update_cursor_state(cursor_detection(pos), state, config, t)
    check(v > 150, "匀速右移速度应显著为正", f"实际 {v:.1f}")
    check(v < 420, "速度不应过冲", f"实际 {v:.1f}")

    # 跳变抑制：单帧大跳点不应污染速度。
    v_before = state.smoothed_cursor_velocity
    t += 0.02
    _, v_jump = update_cursor_state(
        cursor_detection(pos + config.cursor_jump_max_px + 40), state, config, t
    )
    check(
        abs(v_jump - v_before) < 1e-6,
        "跳点帧速度应保持上一次平滑值",
        f"before={v_before:.1f} after={v_jump:.1f}",
    )

    # 速度上限钳制。
    fast_state = ControlState()
    update_cursor_state(cursor_detection(0.0), fast_state, config, 0.0)
    cfg_fast = load_fish_pro_config({"cursor_jump_max_px": 100000})
    _, v_fast = update_cursor_state(
        cursor_detection(9000.0), fast_state, cfg_fast, 0.001
    )
    check(
        abs(v_fast) <= cfg_fast.cursor_max_velocity_px + 1e-6,
        "速度应被 cursor_max_velocity_px 钳制",
        f"实际 {v_fast:.1f}",
    )

    # 观测量：静止居中时应处于中心静默。
    obs_state = ControlState()
    target = make_detection(200.0, 120.0)
    update_cursor_state(cursor_detection(200.0), obs_state, config, 0.0)
    obs = build_observation(target, 200.0, 0.0, obs_state, config, 0.0)
    check(obs.inside_target, "居中应判定框内")
    check(obs.in_center_no_move, "居中静止应进入中心静默")
    check(abs(obs.error_px) < 1e-6, "居中静止误差应为 0")
    check(
        abs(obs.edge_margin_px - 60.0) < 1e-6,
        "边缘余量应为半宽",
        f"实际 {obs.edge_margin_px}",
    )

    # 框外判定与负边缘余量。
    obs_out = build_observation(
        target, 100.0, 0.0, ControlState(), config, 0.0
    )
    check(not obs_out.inside_target, "框外应判定 inside_target=False")
    check(obs_out.edge_margin_px < 0, "框外边缘余量应为负")

    # 预测：光标向右高速运动时预测位置应更靠右。
    obs_fast = build_observation(
        target, 200.0, 400.0, ControlState(), config, 0.0
    )
    check(
        obs_fast.predicted_cursor_x > obs_fast.cursor_x,
        "右移时预测位置应更靠右",
    )
    check(
        obs_fast.control_target_x < obs_fast.predicted_target_center_x,
        "右移时制动偏置应把控制目标左移",
    )

    # 绿条移动时目标速度估计与前瞻。
    move_state = ControlState()
    ts = 0.0
    center = 200.0
    for _ in range(10):
        build_observation(
            make_detection(center, 120.0), center, 0.0, move_state, config, ts
        )
        center += 5.0
        ts += 0.02
    check(
        move_state.smoothed_target_velocity > 80,
        "绿条右移应估计出正的目标速度",
        f"实际 {move_state.smoothed_target_velocity:.1f}",
    )

    # 宽度多帧确认：单帧异常宽度不立即采纳，连续出现才更新。
    width_state = ControlState()
    build_observation(make_detection(200, 120), 200, 0, width_state, config, 0.0)
    build_observation(make_detection(200, 120), 200, 0, width_state, config, 0.02)
    stable = width_state.confirmed_target_width
    obs_w1 = build_observation(
        make_detection(200, 220), 200, 0, width_state, config, 0.04
    )
    check(
        abs(obs_w1.target_width - stable) < 5.0,
        "单帧宽度跳变不应立即采纳",
        f"stable={stable:.1f} got={obs_w1.target_width:.1f}",
    )
    obs_w2 = build_observation(
        make_detection(200, 220), 200, 0, width_state, config, 0.06
    )
    check(
        obs_w2.target_width > stable + 40,
        "连续确认后宽度应更新",
        f"got={obs_w2.target_width:.1f}",
    )

    # 丢帧宽限与衰减。
    grace_state = ControlState()
    grace_state.last_observation = obs
    grace_state.last_valid_observation_time = 1.0
    grace_state.last_final_action = 0.8
    mark_missing_state(grace_state, 1.01, cursor_missing=True)
    check(
        can_recover_from_missing(
            grace_state, config, 1.01, cursor_missing=True
        ),
        "宽限内应允许按上次观测恢复",
    )
    check(
        not can_recover_from_missing(
            grace_state, config, 1.0 + config.missing_cursor_grace_sec + 0.01,
            cursor_missing=True,
        ),
        "超过宽限应停止恢复",
    )
    decayed = missing_fallback_action(grace_state, config, target_missing=False)
    check(
        0 < decayed < 0.8,
        "丢帧动作应衰减但不归零",
        f"实际 {decayed:.3f}",
    )
    grace_state.last_final_action = 0.05
    check(
        missing_fallback_action(grace_state, config, target_missing=False) == 0.0,
        "小于死区的衰减动作应归零",
    )
    print(f"状态层检查完成，累计 {CHECKS} 项")


# ============ 3. 规则层 ============


def build_obs(
    cursor_x: float,
    target_center: float = 200.0,
    target_width: float = 120.0,
    cursor_vx: float = 0.0,
    target_vx: float = 0.0,
    config: FishProConfig | None = None,
    state: ControlState | None = None,
):
    config = config or FishProConfig()
    state = state or ControlState()
    target = make_detection(target_center, target_width)
    if target_vx:
        # 先喂一帧建立目标速度基线。
        prev = make_detection(target_center - target_vx * 0.02, target_width)
        build_observation(prev, cursor_x, cursor_vx, state, config, 0.0)
        return build_observation(
            target, cursor_x, cursor_vx, state, config, 0.02
        )
    return build_observation(target, cursor_x, cursor_vx, state, config, 0.0)


def verify_policy() -> None:
    section("3. 规则层")
    config = FishProConfig()

    # 安全区静默：居中静止不应输出动作。
    state = ControlState()
    obs = build_obs(200.0, config=config, state=state)
    action = compute_rule_action(obs, state, config, 0.0)
    check(action == 0.0, "安全区居中应输出 0 动作", f"实际 {action}")
    check(state.active_mode == "safe", "模式应为 safe", state.active_mode)
    check(state.center_silence_active, "应进入静默态")

    # 静默滞回：小幅偏移但边缘仍安全时保持静默。
    obs_small = build_obs(206.0, config=config, state=state)
    action_small = compute_rule_action(obs_small, state, config, 0.01)
    check(action_small == 0.0, "静默滞回内应保持 0 动作", f"实际 {action_small}")

    # 框内靠右边缘：应向左（负）拉回。
    edge_state = ControlState()
    obs_edge = build_obs(255.0, config=config, state=edge_state)
    action_edge = compute_rule_action(obs_edge, edge_state, config, 0.0)
    check(action_edge < 0, "偏右应输出向左动作", f"实际 {action_edge}")
    check(
        edge_state.active_mode in ("edge", "active"),
        "模式应为 edge/active",
        edge_state.active_mode,
    )
    check(
        abs(action_edge) <= action_cap_for_observation(obs_edge, config) + 1e-6,
        "动作应受框内上限约束",
    )

    # 框内靠左边缘：应向右（正）。
    left_state = ControlState()
    obs_left = build_obs(145.0, config=config, state=left_state)
    action_left = compute_rule_action(obs_left, left_state, config, 0.0)
    check(action_left > 0, "偏左应输出向右动作", f"实际 {action_left}")

    # 框外远距离：强度应明显大于框内边缘。
    far_state = ControlState()
    obs_far = build_obs(60.0, config=config, state=far_state)
    action_far = compute_rule_action(obs_far, far_state, config, 0.0)
    check(action_far > 0, "框外左侧应向右强拉", f"实际 {action_far}")
    check(
        action_far > abs(action_edge),
        "框外恢复力度应大于框内修正",
        f"far={action_far:.3f} edge={abs(action_edge):.3f}",
    )
    check(
        far_state.active_mode in ("recover", "approach"),
        "模式应为 recover/approach",
        far_state.active_mode,
    )

    # 分级恢复下限。
    hard_state = ControlState()
    obs_hard = build_obs(200.0 - 60 - 60, config=config, state=hard_state)
    action_hard = compute_rule_action(obs_hard, hard_state, config, 0.0)
    check(
        abs(action_hard) >= min(config.recovery_fast_min_action, 0.5),
        "超远距离应触发恢复下限",
        f"实际 {action_hard:.3f}",
    )

    # 接近降档：框外但高速朝框内运动时应限制强度。
    appr_state = ControlState()
    obs_appr = build_obs(
        135.0, cursor_vx=260.0, config=config, state=appr_state
    )
    action_appr = compute_rule_action(obs_appr, appr_state, config, 0.0)
    check(
        appr_state.active_mode == "approach",
        "高速接近应进入 approach 模式",
        appr_state.active_mode,
    )
    # 上限即 action_cap_for_observation 的返回值（宽目标会附加缩放），
    # 这里直接对齐同一函数，避免把缩放漏算成偏差。
    appr_cap = action_cap_for_observation(obs_appr, config)
    check(
        abs(action_appr) <= appr_cap + 1e-6,
        "approach 模式动作应被降档到上限内",
        f"实际 {abs(action_appr):.3f} cap={appr_cap:.3f}",
    )
    # 同时确认降档确实低于框外全力恢复。
    recov_state = ControlState()
    obs_recov = build_obs(70.0, config=config, state=recov_state)
    action_recov = compute_rule_action(obs_recov, recov_state, config, 0.0)
    check(
        abs(action_appr) < abs(action_recov),
        "approach 强度应明显低于全力恢复",
        f"approach={abs(action_appr):.3f} recover={abs(action_recov):.3f}",
    )

    # 方向冷却/制动：刚按下反向键后小误差不应立刻反向。
    brake_state = ControlState()
    brake_state.held_direction = "right"
    brake_state.last_move_time = 0.0
    obs_brake = build_obs(212.0, config=config, state=brake_state)
    brake_state.center_silence_active = False
    action_brake = compute_rule_action(obs_brake, brake_state, config, 0.01)
    check(
        action_brake == 0.0,
        "反向冷却内应输出 0（制动）",
        f"mode={brake_state.active_mode} action={action_brake}",
    )

    # 主动制动：朝目标高速接近时强度应低于同位置静止。
    # 选取靠近右边缘但未进入中心静默的位置，此处静止会触发 edge 加成，
    # 制动效果体现为显著低于静止值。
    stat_state = ControlState()
    obs_static = build_obs(252.0, config=config, state=stat_state)
    act_static = abs(compute_rule_action(obs_static, stat_state, config, 0.0))
    tw_state = ControlState()
    obs_toward = build_obs(
        252.0, cursor_vx=-260.0, config=config, state=tw_state
    )
    act_toward = abs(compute_rule_action(obs_toward, tw_state, config, 0.0))
    check(act_static > 0, "该位置静止应有修正动作", f"实际 {act_static:.3f}")
    check(
        act_toward < act_static,
        "朝目标接近时应主动制动降低强度",
        f"static={act_static:.3f} toward={act_toward:.3f}",
    )

    # 远离目标时应加力：在 approach 场景下强度未触顶，可体现增益。
    # 相同位置下，远离（预测更远）应比接近获得更高强度。
    away_state = ControlState()
    obs_away = build_obs(135.0, cursor_vx=-120.0, config=config, state=away_state)
    act_away = abs(compute_rule_action(obs_away, away_state, config, 0.0))
    check(
        act_away > abs(action_appr),
        "远离目标时应比接近目标输出更大强度",
        f"away={act_away:.3f} approach={abs(action_appr):.3f}",
    )

    # 动作上限：宽目标框内上限应被放大。
    obs_wide = build_obs(200.0 + 70, target_width=200.0, config=config)
    obs_narrow = build_obs(200.0 + 40, target_width=100.0, config=config)
    check(
        action_cap_for_observation(obs_wide, config)
        > config.inside_action_cap * 0.99,
        "宽目标框内上限应不小于基准",
    )
    check(
        action_cap_for_observation(obs_narrow, config) <= config.inside_edge_action_cap
        + 1e-6,
        "窄目标框内上限应受限",
    )

    # merge：静默态残差应被清零。
    merge_state = ControlState()
    merge_state.center_silence_active = True
    obs_merge = build_obs(200.0, config=config)
    final = merge_policy_action(obs_merge, 0.5, 0.2, merge_state, config)
    check(final == 0.0, "静默态最终动作应为 0", f"实际 {final}")
    check(merge_state.last_residual_action == 0.0, "静默态残差应清零")

    # merge：非静默时平滑 + 上限钳制。
    merge_state2 = ControlState()
    obs_merge2 = build_obs(255.0, config=config)
    final2 = merge_policy_action(obs_merge2, -1.0, 0.0, merge_state2, config)
    cap2 = action_cap_for_observation(obs_merge2, config)
    check(
        abs(final2) <= cap2 + 1e-6,
        "最终动作应受上限钳制",
        f"final={final2:.3f} cap={cap2:.3f}",
    )
    check(final2 < 0, "最终动作应保留规则层方向")
    print(f"规则层检查完成，累计 {CHECKS} 项")


# ============ 4. 执行层 ============


class FakeAdapter:
    def __init__(self, fail: bool = False):
        self.events: list[tuple[str, int]] = []
        self.fail = fail

    def key_down(self, key: int) -> None:
        if self.fail:
            raise RuntimeError("模拟按键失败")
        self.events.append(("down", key))

    def key_up(self, key: int) -> None:
        self.events.append(("up", key))

    def pressed(self) -> list[int]:
        held: list[int] = []
        for kind, key in self.events:
            if kind == "down":
                held.append(key)
            elif key in held:
                held.remove(key)
        return held


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def verify_executor() -> None:
    section("4. 执行层")
    config = FishProConfig()
    rng = random.Random(20240607)

    adapter = FakeAdapter()
    clock = FakeClock()
    executor = ActionExecutor(
        adapter, config, rng=rng, clock=clock, sleeper=lambda _s: None
    )

    # 死区：中心区小动作不产生按键。
    state = ControlState()
    obs_center = build_obs(200.0, config=config)
    result = executor.apply(obs_center, 0.10, state)
    check(result == "idle", "中心区小动作应 idle", result)
    check(adapter.events == [], "中心区不应有按键事件")

    # 中心区死区放大：需要更大的动作才触发。
    dz_state = ControlState()
    dir_small = executor.choose_direction(0.30, obs_center, dz_state)
    check(dir_small == "", "中心区 0.30 动作应仍在死区内", dir_small)

    # 框外大动作：应按下正确方向键。
    out_state = ControlState()
    obs_out_left = build_obs(80.0, config=config)
    result_out = executor.apply(obs_out_left, 0.9, out_state)
    check(result_out.startswith("hold_right"), "向右动作应按 D", result_out)
    check(("down", KEY_D) in adapter.events, "应发出 D keyDown")
    check(out_state.held_direction == "right", "状态应记录持有方向")

    # 脉冲到期：非紧急时应释放。
    clock.advance(1.0)
    obs_inside = build_obs(200.0, config=config)
    inside_state = ControlState()
    inside_state.held_direction = "right"
    inside_state.hold_started_at = clock.now - 0.5
    inside_state.pulse_release_time = clock.now - 0.01
    adapter.events.clear()
    res_pulse = executor.apply(obs_inside, 0.0, inside_state)
    check(
        res_pulse.startswith("pulse_done") or res_pulse.startswith("deadzone"),
        "脉冲到期应结束按压",
        res_pulse,
    )
    check(inside_state.held_direction == "", "脉冲结束后不应仍持有按键")
    check(("up", KEY_D) in adapter.events, "应发出 D keyUp")

    # 方向切换：应先松开旧键再按新键。
    sw_state = ControlState()
    sw_state.held_direction = "left"
    sw_state.hold_started_at = clock.now - 1.0
    adapter.events.clear()
    obs_need_right = build_obs(80.0, config=config)
    executor.apply(obs_need_right, 0.95, sw_state)
    kinds = [kind for kind, _ in adapter.events]
    check(
        adapter.events and adapter.events[0] == ("up", KEY_A),
        "切换应先释放旧键",
        str(adapter.events[:2]),
    )
    check(("down", KEY_D) in adapter.events, "切换后应按下新键")
    check(kinds.count("down") == 1, "切换只应按下一个新键")

    # 切换保护：刚按下后小误差不立刻反向。
    # 需选取不在中心静默区、且误差小于 switch_limit 的位置，否则会被
    # 静默判定或大误差放行而无法体现保护逻辑。
    prot_state = ControlState()
    prot_state.held_direction = "right"
    prot_state.hold_started_at = clock.now
    obs_small_err = build_obs(255.0, config=config)
    check(
        not obs_small_err.in_center_no_move,
        "切换保护用例应位于静默区之外",
    )
    kept = executor.choose_direction(-0.9, obs_small_err, prot_state)
    check(kept == "right", "切换保护期内应保持原方向", kept)
    # 超过保护窗口后应允许反向。
    prot_state2 = ControlState()
    prot_state2.held_direction = "right"
    prot_state2.hold_started_at = clock.now - config.switch_hold_time - 0.01
    switched = executor.choose_direction(-0.9, obs_small_err, prot_state2)
    check(switched == "left", "超过保护窗口应允许反向", switched)

    # 脉冲时长：框内应短于框外，且强度越大越长。
    d_inside = executor.pulse_press_duration(0.5, obs_inside)
    d_outside = executor.pulse_press_duration(0.5, obs_out_left)
    check(
        d_inside < d_outside,
        "框内脉冲应短于框外",
        f"inside={d_inside:.4f} outside={d_outside:.4f}",
    )
    cfg_nojitter = load_fish_pro_config({"pulse_jitter_sec": 0})
    ex_nj = ActionExecutor(
        FakeAdapter(), cfg_nojitter, rng=rng, clock=clock, sleeper=lambda _s: None
    )
    d_low = ex_nj.pulse_press_duration(0.25, obs_out_left)
    d_high = ex_nj.pulse_press_duration(0.95, obs_out_left)
    check(d_high > d_low, "强动作脉冲应更长", f"{d_low:.4f} -> {d_high:.4f}")
    check(
        cfg_nojitter.pulse_min_press_sec - 1e-9
        <= d_low
        <= cfg_nojitter.pulse_max_press_sec + 1e-9,
        "脉冲时长应在配置区间内",
    )

    # 长按判定：框内普通危险不长按，框外大误差长按。
    check(
        not executor.should_use_long_hold(0.95, obs_inside),
        "框内安全位置不应长按",
    )
    check(
        executor.should_use_long_hold(0.95, obs_out_left),
        "框外强动作应允许长按",
    )

    # 紧急判定。
    check(executor.is_urgent(obs_out_left), "框外应判定紧急")
    check(not executor.is_urgent(obs_center), "居中不应判定紧急")

    # 异常安全：keyDown 失败时状态不应残留持有。
    bad_adapter = FakeAdapter(fail=True)
    bad_exec = ActionExecutor(
        bad_adapter, config, rng=rng, clock=clock, sleeper=lambda _s: None
    )
    bad_state = ControlState()
    res_bad = bad_exec.apply(build_obs(80.0, config=config), 0.95, bad_state)
    check(res_bad.startswith("error_"), "按键失败应返回 error", res_bad)
    check(bad_state.held_direction == "", "按键失败后不应残留持有状态")

    # release_all 必须同时释放 A/D。
    rel_adapter = FakeAdapter()
    rel_exec = ActionExecutor(
        rel_adapter, config, rng=rng, clock=clock, sleeper=lambda _s: None
    )
    rel_exec.release_all()
    check(
        ("up", KEY_A) in rel_adapter.events and ("up", KEY_D) in rel_adapter.events,
        "release_all 应释放 A 与 D",
    )
    print(f"执行层检查完成，累计 {CHECKS} 项")


# ============ 5. 学习层 ============


def verify_learning() -> None:
    section("5. 学习层")
    config = load_fish_pro_config({"learning_enabled": True})
    tmp = Path(tempfile.mkdtemp(prefix="fishpro_learn_"))
    try:
        policy = ResidualPolicy(config, tmp)
        obs_inside = build_obs(200.0, config=config)
        obs_edge = build_obs(255.0, config=config)
        obs_out = build_obs(80.0, config=config)

        features = policy.feature_vector(obs_inside)
        check(
            features.shape == (FEATURE_COUNT,),
            "特征维度应为 13",
            str(features.shape),
        )
        check(np.all(np.isfinite(features)), "特征不应含 NaN/Inf")
        check(abs(features[0] - 1.0) < 1e-6, "首位应为偏置 1.0")

        # 初始权重为 0，残差应为 0。
        check(abs(policy.predict(obs_inside)) < 1e-9, "初始残差应为 0")

        # 残差限幅。
        policy.weights[:] = 5.0
        residual = policy.predict(obs_out)
        check(
            abs(residual) <= config.learning_residual_limit + 1e-9,
            "残差应被限幅",
            f"实际 {residual:.4f}",
        )
        policy.weights[:] = 0.0

        # 探索噪声：框外幅度应更大。
        rng = random.Random(7)
        inside_span = max(
            abs(policy.explore(0.0, obs_inside, rng)) for _ in range(200)
        )
        outside_span = max(
            abs(policy.explore(0.0, obs_out, rng)) for _ in range(200)
        )
        check(
            outside_span > inside_span,
            "框外探索噪声应更大",
            f"inside={inside_span:.4f} outside={outside_span:.4f}",
        )

        # 奖励符号：进框 > 保持框内 > 掉出。
        pending_out = policy.build_pending_record(obs_out, 0.9, 0.0, 0.9)
        r_recover = policy.reward_from_transition(pending_out, obs_inside)
        pending_in = policy.build_pending_record(obs_inside, 0.0, 0.0, 0.0)
        r_stay = policy.reward_from_transition(pending_in, obs_inside)
        r_drop = policy.reward_from_transition(pending_in, obs_out)
        check(r_recover > 0, "恢复进框奖励应为正", f"{r_recover:.3f}")
        check(r_stay > 0, "保持框内奖励应为正", f"{r_stay:.3f}")
        check(r_drop < 0, "掉出框外奖励应为负", f"{r_drop:.3f}")
        check(
            r_recover > r_drop and r_stay > r_drop,
            "掉框应是最差情形",
        )
        for reward in (r_recover, r_stay, r_drop):
            check(-1.5 <= reward <= 1.5, "奖励应被限幅在 ±1.5")

        # 分级惩罚：掉得越远惩罚越重。
        # 起点也取框外，避免叠加「掉出框」的额外惩罚后双双触到 -1.5 下限
        # 而掩盖分级差异。
        pending_outside = policy.build_pending_record(
            build_obs(130.0, config=config), 0.5, 0.0, 0.5
        )
        r_near = policy.reward_from_transition(
            pending_outside, build_obs(128.0, config=config)
        )
        r_mid = policy.reward_from_transition(
            pending_outside, build_obs(100.0, config=config)
        )
        r_far = policy.reward_from_transition(
            pending_outside, build_obs(60.0, config=config)
        )
        check(
            r_near > r_mid > r_far,
            "掉出越远惩罚应越重",
            f"near={r_near:.3f} mid={r_mid:.3f} far={r_far:.3f}",
        )

        # 掉出框的分级惩罚：刚掉出应轻于掉得很远。
        r_drop_near = policy.reward_from_transition(
            pending_in, build_obs(138.0, config=config)
        )
        r_drop_far = policy.reward_from_transition(
            pending_in, build_obs(60.0, config=config)
        )
        check(
            r_drop_near <= 0 and r_drop_far <= 0,
            "掉出框奖励都应为负",
        )
        check(
            r_drop_far <= r_drop_near,
            "掉得越远的掉框惩罚不应更轻",
            f"near={r_drop_near:.3f} far={r_drop_far:.3f}",
        )

        # 在线更新：样本数、权重与 EMA 应变化。
        before_weights = policy.weights.copy()
        pending = policy.build_pending_record(obs_out, 0.9, 0.2, 0.9)
        transition = policy.update_from_transition(pending, obs_inside)
        check(policy.sample_count == 1, "更新后样本数应为 1")
        check(policy.update_count == 1, "更新后计数应为 1")
        check(
            not np.allclose(before_weights, policy.weights),
            "在线更新应改变权重",
        )
        check("reward" in transition and "loss" in transition, "转移记录应含奖励与损失")
        check(np.all(np.isfinite(policy.weights)), "权重不应出现 NaN/Inf")

        # 多轮更新稳定性：权重应保持在 ±5 内。
        for index in range(400):
            src = obs_out if index % 3 == 0 else obs_inside
            dst = obs_inside if index % 2 == 0 else obs_edge
            rec = policy.build_pending_record(src, 0.5, 0.1, 0.5)
            policy.update_from_transition(rec, dst)
        check(
            float(np.max(np.abs(policy.weights))) <= 5.0 + 1e-6,
            "权重应被裁剪在 ±5",
            f"max={float(np.max(np.abs(policy.weights))):.3f}",
        )
        check(np.all(np.isfinite(policy.weights)), "长时间更新后权重仍应有限")

        # 持久化：写盘 + 重载 + 回放。
        check(policy.save_all(force_history=True), "保存应成功")
        check(policy.model_path.exists(), "模型文件应存在")
        check(policy.data_path.exists(), "样本文件应存在")

        reloaded = ResidualPolicy(config, tmp)
        info = reloaded.load_artifacts(replay_history=False)
        check(info["loaded"], "重载应命中模型")
        check(
            np.allclose(reloaded.weights, policy.weights),
            "重载权重应一致",
        )
        check(
            reloaded.sample_count == policy.sample_count,
            "重载样本数应一致",
        )
        check(info["history_count"] > 0, "应统计到历史样本数")

        replayed = ResidualPolicy(config, tmp)
        replayed.load_artifacts(replay_history=True)
        check(replayed.sample_count > 0, "历史回放应产生样本更新")

        # 坏样本不应导致崩溃。
        check(not replayed.replay_record({"features": [1.0, 2.0]}), "维度不符应返回 False")
        with policy.data_path.open("a", encoding="utf-8") as handle:
            handle.write("not-json\n")
        tolerant = ResidualPolicy(config, tmp)
        tolerant.load_artifacts(replay_history=True)
        check(True, "含坏行的样本文件应可容错读取")

        # 模型维度不符应重置而非崩溃。
        np.savez_compressed(policy.model_path, weights=np.zeros(5, dtype=np.float32))
        mismatched = ResidualPolicy(config, tmp)
        mismatched.load_artifacts(replay_history=False)
        check(
            mismatched.weights.shape == (FEATURE_COUNT,),
            "维度不符应重置为 13 维",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"学习层检查完成，累计 {CHECKS} 项")


# ============ 6. 闭环仿真 ============


class BarSimulator:
    """简化控条物理：按键给光标加速度，光标带阻尼，绿条正弦漂移。"""

    def __init__(
        self,
        adapter: FakeAdapter,
        *,
        bar_width: float = 120.0,
        bar_drift: float = 0.0,
        drift_hz: float = 0.35,
        drag_px: float = 260.0,
        accel: float = 2600.0,
        damping: float = 4.2,
        dt: float = 0.016,
        cursor_start: float = 200.0,
        missing_frames: frozenset[int] = frozenset(),
    ):
        self.adapter = adapter
        self.bar_width = bar_width
        self.bar_drift = bar_drift
        self.drift_hz = drift_hz
        self.drag_px = drag_px
        self.accel = accel
        self.damping = damping
        self.dt = dt
        self.cursor_x = cursor_start
        self.cursor_v = 0.0
        self.bar_center = 200.0
        self.frame = 0
        self.time = 0.0
        self.missing_frames = missing_frames
        self.inside = 0
        self.samples = 0

    def _bar_center_at(self, t: float) -> float:
        return 200.0 + self.bar_drift * np.sin(2 * np.pi * self.drift_hz * t)

    def step(self) -> np.ndarray | None:
        held = self.adapter.pressed()
        accel = -self.drag_px  # 鱼持续把光标往左拉
        if KEY_D in held:
            accel += self.accel
        if KEY_A in held:
            accel -= self.accel

        self.cursor_v += (accel - self.damping * self.cursor_v) * self.dt
        self.cursor_v = float(np.clip(self.cursor_v, -900.0, 900.0))
        self.cursor_x += self.cursor_v * self.dt
        self.cursor_x = float(np.clip(self.cursor_x, 10.0, ROI_W - 10.0))

        self.time += self.dt
        self.frame += 1
        self.bar_center = self._bar_center_at(self.time)

        left = self.bar_center - self.bar_width / 2
        right = self.bar_center + self.bar_width / 2
        self.samples += 1
        if left <= self.cursor_x <= right:
            self.inside += 1

        if self.frame in self.missing_frames:
            return make_frame(
                int(round(left)), int(round(self.bar_width)), None
            )
        return make_frame(
            int(round(left)),
            int(round(self.bar_width)),
            int(round(self.cursor_x)),
        )

    @property
    def inside_ratio(self) -> float:
        return self.inside / max(1, self.samples)


def run_closed_loop(
    *,
    frames: int,
    bar_drift: float = 0.0,
    cursor_start: float = 200.0,
    missing_frames: frozenset[int] = frozenset(),
    config_override: dict | None = None,
    learning: bool = False,
):
    override = {"session_timeout_ms": 0, "loop_delay_min": 0, "loop_delay_max": 0}
    override.update(config_override or {})
    config = load_fish_pro_config(override)

    adapter = FakeAdapter()
    clock = FakeClock()
    sim = BarSimulator(
        adapter,
        bar_drift=bar_drift,
        cursor_start=cursor_start,
        missing_frames=missing_frames,
    )
    rng = random.Random(1234)

    def sleeper(seconds: float) -> None:
        clock.advance(max(0.0, seconds))

    executor = ActionExecutor(
        adapter, config, rng=rng, clock=clock, sleeper=sleeper
    )

    remaining = {"n": frames}

    def screencap():
        if remaining["n"] <= 0:
            return None
        remaining["n"] -= 1
        clock.advance(sim.dt)
        return sim.step()

    def should_stop() -> bool:
        return remaining["n"] <= 0

    policy = None
    tmp: Path | None = None
    if learning:
        tmp = Path(tempfile.mkdtemp(prefix="fishpro_loop_"))
        policy = ResidualPolicy(config, tmp)

    session = FishProSession(
        config=config,
        executor=executor,
        screencap=screencap,
        should_stop=should_stop,
        residual_policy=policy,
        debug_dir=None,
        rng=rng,
        clock=clock,
        sleeper=sleeper,
    )
    result = session.run()
    if tmp is not None:
        shutil.rmtree(tmp, ignore_errors=True)
    return result, sim, adapter, session


def verify_closed_loop() -> None:
    section("6. 闭环仿真")

    # 静止绿条：应保持高留框率。
    result, sim, adapter, _ = run_closed_loop(frames=600)
    print(f"静止绿条留框率: {sim.inside_ratio * 100:.1f}%")
    check(
        sim.inside_ratio >= 0.90,
        "静止绿条留框率应不低于 90%",
        f"实际 {sim.inside_ratio * 100:.1f}%",
    )
    check(adapter.pressed() == [], "会话结束后不应残留按下的键")
    check(result.valid_frames > 500, "有效帧数应足够", str(result.valid_frames))

    # 移动绿条：应仍能跟随。
    _, sim_move, adapter_move, _ = run_closed_loop(frames=800, bar_drift=55.0)
    print(f"移动绿条留框率: {sim_move.inside_ratio * 100:.1f}%")
    check(
        sim_move.inside_ratio >= 0.80,
        "移动绿条留框率应不低于 80%",
        f"实际 {sim_move.inside_ratio * 100:.1f}%",
    )
    check(adapter_move.pressed() == [], "移动场景结束后应释放按键")

    # 从框外起步：应快速拉回。
    _, sim_out, _, _ = run_closed_loop(frames=600, cursor_start=90.0)
    print(f"框外起步留框率: {sim_out.inside_ratio * 100:.1f}%")
    check(
        sim_out.inside_ratio >= 0.75,
        "框外起步应能快速恢复",
        f"实际 {sim_out.inside_ratio * 100:.1f}%",
    )

    # 丢帧：零散丢帧不应显著恶化。
    missing = frozenset(range(40, 800, 9))
    _, sim_miss, adapter_miss, _ = run_closed_loop(
        frames=800, bar_drift=40.0, missing_frames=missing
    )
    print(f"零散丢帧留框率: {sim_miss.inside_ratio * 100:.1f}%")
    check(
        sim_miss.inside_ratio >= 0.75,
        "零散丢帧下留框率应仍不低于 75%",
        f"实际 {sim_miss.inside_ratio * 100:.1f}%",
    )
    check(adapter_miss.pressed() == [], "丢帧场景结束后应释放按键")

    # 开启学习不应破坏基线。
    _, sim_learn, adapter_learn, session_learn = run_closed_loop(
        frames=700, bar_drift=40.0, learning=True,
        config_override={"learning_enabled": True},
    )
    print(f"开启学习留框率: {sim_learn.inside_ratio * 100:.1f}%")
    check(
        sim_learn.inside_ratio >= 0.75,
        "开启学习后留框率不应崩塌",
        f"实际 {sim_learn.inside_ratio * 100:.1f}%",
    )
    check(
        session_learn.state.learning_sample_count > 0,
        "学习开启时应累计样本",
    )
    check(adapter_learn.pressed() == [], "学习场景结束后应释放按键")

    # 控条结束：光标持续消失应成功返回。
    end_frames = frozenset(range(80, 400))
    result_end, _, adapter_end, _ = run_closed_loop(
        frames=400, missing_frames=end_frames
    )
    check(
        result_end.success and result_end.reason == REASON_CONTROL_FINISHED,
        "光标持续消失应判定控条结束且成功",
        f"success={result_end.success} reason={result_end.reason}",
    )
    check(adapter_end.pressed() == [], "结束时应释放按键")

    # 全程无识别：应失败退出交给 Pipeline。
    result_lost, _, adapter_lost, _ = run_closed_loop(
        frames=400, missing_frames=frozenset(range(0, 400))
    )
    check(
        not result_lost.success and result_lost.reason == REASON_LOST_ABORT,
        "全程无识别应失败退出",
        f"success={result_lost.success} reason={result_lost.reason}",
    )
    check(adapter_lost.pressed() == [], "失败退出也应释放按键")

    # 截图异常：不应抛出，且必须释放按键。
    config = load_fish_pro_config(
        {"session_timeout_ms": 0, "loop_delay_min": 0, "loop_delay_max": 0}
    )
    adapter_err = FakeAdapter()
    clock_err = FakeClock()
    executor_err = ActionExecutor(
        adapter_err,
        config,
        rng=random.Random(1),
        clock=clock_err,
        sleeper=lambda s: clock_err.advance(s),
    )
    calls = {"n": 0}

    def bad_screencap():
        calls["n"] += 1
        if calls["n"] > 3:
            raise RuntimeError("模拟截图崩溃")
        return make_frame(140, 120, 200)

    session_err = FishProSession(
        config=config,
        executor=executor_err,
        screencap=bad_screencap,
        should_stop=lambda: False,
        rng=random.Random(1),
        clock=clock_err,
        sleeper=lambda s: clock_err.advance(s),
    )
    result_err = session_err.run()
    check(not result_err.success, "截图异常应返回失败")
    check(adapter_err.pressed() == [], "异常路径也必须释放按键")

    # 任务停止：应立即失败退出并释放。
    adapter_stop = FakeAdapter()
    clock_stop = FakeClock()
    executor_stop = ActionExecutor(
        adapter_stop,
        config,
        rng=random.Random(2),
        clock=clock_stop,
        sleeper=lambda s: clock_stop.advance(s),
    )
    stop_state = {"n": 0}

    def stop_screencap():
        stop_state["n"] += 1
        clock_stop.advance(0.016)
        return make_frame(140, 120, 260)

    session_stop = FishProSession(
        config=config,
        executor=executor_stop,
        screencap=stop_screencap,
        should_stop=lambda: stop_state["n"] >= 5,
        rng=random.Random(2),
        clock=clock_stop,
        sleeper=lambda s: clock_stop.advance(s),
    )
    result_stop = session_stop.run()
    check(not result_stop.success, "任务停止应返回失败")
    check(adapter_stop.pressed() == [], "任务停止应释放按键")

    # 会话超时保护。
    adapter_to = FakeAdapter()
    clock_to = FakeClock()
    config_to = load_fish_pro_config(
        {
            "session_timeout_ms": 200,
            "loop_delay_min": 0,
            "loop_delay_max": 0,
        }
    )
    executor_to = ActionExecutor(
        adapter_to,
        config_to,
        rng=random.Random(3),
        clock=clock_to,
        sleeper=lambda s: clock_to.advance(s),
    )

    def slow_screencap():
        clock_to.advance(0.05)
        return make_frame(140, 120, 200)

    session_to = FishProSession(
        config=config_to,
        executor=executor_to,
        screencap=slow_screencap,
        should_stop=lambda: False,
        rng=random.Random(3),
        clock=clock_to,
        sleeper=lambda s: clock_to.advance(s),
    )
    result_to = session_to.run()
    check(not result_to.success, "超时应返回失败")
    check(adapter_to.pressed() == [], "超时应释放按键")
    print(f"闭环仿真检查完成，累计 {CHECKS} 项")


# ============ 7. 参数解析 ============


def verify_config() -> None:
    section("7. 参数解析")
    default = FishProConfig()

    check(load_fish_pro_config(None).roi_px == default.roi_px, "空参数应取默认")
    check(load_fish_pro_config("").safe_margin_px == default.safe_margin_px, "空串应取默认")
    check(
        load_fish_pro_config("{bad json").action_deadzone == default.action_deadzone,
        "非法 JSON 应取默认",
    )

    parsed = load_fish_pro_config('{"safe_margin_px": 15}')
    check(parsed.safe_margin_px == 15, "字符串 JSON 应可解析")

    parsed_dict = load_fish_pro_config({"action_deadzone": 0.4})
    check(abs(parsed_dict.action_deadzone - 0.4) < 1e-9, "字典参数应可解析")

    check(
        load_fish_pro_config({"safe_margin_px": -5}).safe_margin_px == 0,
        "负值应被钳到 0",
    )
    check(
        abs(load_fish_pro_config({"action_smooth_alpha": 3.0}).action_smooth_alpha - 1.0)
        < 1e-9,
        "比例字段应钳到 1.0",
    )
    check(
        load_fish_pro_config({"safe_margin_px": "abc"}).safe_margin_px
        == default.safe_margin_px,
        "非数值应回退默认",
    )
    check(
        load_fish_pro_config({"safe_margin_px": float("nan")}).safe_margin_px
        == default.safe_margin_px,
        "NaN 应回退默认",
    )
    check(
        load_fish_pro_config({"learning_enabled": "yes"}).learning_enabled,
        "布尔字符串应可解析",
    )
    check(
        not load_fish_pro_config({"learning_enabled": "no"}).learning_enabled,
        "布尔 no 应解析为 False",
    )

    hsv = load_fish_pro_config({"green_min_hsv": [10, 999, -5]}).green_min_hsv
    check(hsv == (10, 255, 0), "HSV 应被钳到 0-255", str(hsv))
    check(
        load_fish_pro_config({"green_min_hsv": [1, 2]}).green_min_hsv
        == default.green_min_hsv,
        "长度不符的 HSV 应回退默认",
    )

    roi_bad = load_fish_pro_config({"roi_px": [10, 10, 0, 0]}).roi_px
    check(roi_bad == default.roi_px, "非法 ROI 应回退默认", str(roi_bad))
    roi_ok = load_fish_pro_config({"roi_px": [1, 2, 3, 4]}).roi_px
    check(roi_ok == (1, 2, 3, 4), "合法 ROI 应生效", str(roi_ok))

    # 相对关系修正。
    swapped = load_fish_pro_config(
        {"pulse_min_press_sec": 0.09, "pulse_max_press_sec": 0.01}
    )
    check(
        swapped.pulse_max_press_sec >= swapped.pulse_min_press_sec,
        "脉冲区间应被修正为非空",
    )
    ratio_bad = load_fish_pro_config(
        {"roi_left_ratio": 0.8, "roi_right_ratio": 0.2, "use_ratio_roi": True}
    )
    check(
        ratio_bad.roi_right_ratio > ratio_bad.roi_left_ratio,
        "反向比例 ROI 应被修正",
    )
    check(
        load_fish_pro_config({"learning_residual_limit": 0}).learning_residual_limit
        > 0,
        "残差限幅不应为 0",
    )
    check(
        load_fish_pro_config({"target_width_confirm_frames": 0}).target_width_confirm_frames
        >= 1,
        "宽度确认帧数至少为 1",
    )
    check(
        load_fish_pro_config({"learning_data_name": "  "}).learning_data_name
        == default.learning_data_name,
        "空白文件名应回退默认",
    )
    print(f"参数解析检查完成，累计 {CHECKS} 项")


def main() -> int:
    print("=== FishPro 离线算法验证 ===")
    verify_vision()
    verify_state()
    verify_policy()
    verify_executor()
    verify_learning()
    verify_closed_loop()
    verify_config()

    print(f"\n总计 {CHECKS} 项检查")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项:")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
