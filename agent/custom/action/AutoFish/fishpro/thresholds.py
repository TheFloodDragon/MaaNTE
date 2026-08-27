"""FishPro 控条的共享数值工具与自适应阈值。

对应附件 ``autofish.py`` 中的 ``clamp_*`` 与 ``dynamic_*`` 系列函数，
区别是全部改为显式接收 ``FishProConfig``，不依赖模块级全局配置。
"""

from __future__ import annotations

from .config import FishProConfig


def clamp_float(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def dynamic_center_no_move_px(target_width: float, config: FishProConfig) -> int:
    """中心静默半径：目标越宽，允许的静默范围越大。"""
    return int(
        round(
            max(
                float(config.center_no_move_px),
                target_width * config.center_no_move_width_ratio,
            )
        )
    )


def dynamic_center_reentry_px(target_width: float, config: FishProConfig) -> int:
    """静默重入半径，用于滞回，避免框内反复抖动。"""
    return int(
        round(
            max(
                float(config.center_reentry_px),
                target_width * config.center_reentry_width_ratio,
            )
        )
    )


def dynamic_center_release_px(target_width: float, config: FishProConfig) -> int:
    """静默释放半径。"""
    return int(
        round(
            max(
                float(config.center_release_px),
                target_width * config.center_release_width_ratio,
            )
        )
    )


def dynamic_safe_margin_px(target_width: float, config: FishProConfig) -> int:
    """安全边距：与目标宽度成比例，并保留像素下限。"""
    return int(
        round(
            max(
                float(config.safe_margin_px),
                target_width * config.safe_margin_width_ratio,
            )
        )
    )


def effective_safe_margin_px(
    target_width: float, target_vx: float, config: FishProConfig
) -> int:
    """移动目标补偿：绿条高速移动时增大安全边距。"""
    margin = dynamic_safe_margin_px(target_width, config)
    if abs(target_vx) >= config.moving_target_speed_px:
        margin += config.moving_safe_margin_boost_px
    return margin


def center_edge_margin_px(safe_margin: float, config: FishProConfig) -> float:
    """静默滞回使用的边缘余量门槛。"""
    return max(float(config.edge_recovery_margin_px), safe_margin * 0.70)
