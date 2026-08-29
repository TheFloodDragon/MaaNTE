"""把云异环从启动器主页一路推进到游戏内。

## 这一层补的是一个真实阻断

61a07a0 的实现从「窗口出现」直接跳到「等 InWorld 命中」，中间**没有任何点击**。
但实机的启动器停在主页，需要点「开始游戏」，随后还有一个带 **30 秒倒计时**的
确认弹窗（超时自动「退出启动」）。因此原实现在实机上永远进不了游戏，只会干等到
`ready_timeout` 再报「未能进入游戏」，而真因是没人点按钮。

## 状态机

每轮截一帧，OCR 判断当前处于哪个阶段，再决定动作。**先识别再动作，不预设顺序**，
因为这几个阶段的出现次序会因「不再提醒」是否勾选、是否需要排队而变化：

```
  已在游戏内 ────────────────────────────────► 成功
  确认弹窗   ──点「进入游戏」──┐
  启动器主页 ──点「开始游戏」──┤
  都不是（排队/加载/登录中）──┴──► 继续轮询，直到进游戏或超时
```

## 三条硬性约束

1. **确认弹窗优先于主页**：弹窗有倒计时，错过就白跑。所以每轮先查弹窗。
2. **时长为 0 必须立即停**：点进去也进不了，还会让用户以为任务在跑。
   但「读不到时长」不等于「时长为 0」——OCR 抖一下就中止任务是不可接受的，
   所以只有明确读到 0 才停。
3. **排队超上限放弃并报错**（用户选定的行为）：定时调度里静默占着时长最糟。

## 未实机验证的部分

排队界面本身**没有采集到**（本机进程无输入桌面权限，点不动按钮；见
`spec://cloudgame_calibration.md`）。所以这里不猜排队界面的文案与 ROI，
而是把「既不在主页、也不在弹窗、也不在游戏内」统一当作**推进中**（排队或加载），
靠 `max_queue_time` 兜底，并把每次看到的 OCR 文本打进日志——用户实机跑一次，
日志里就会出现真实的排队文案，据此再做精确识别。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .launcher_ui import (
    LauncherScreen,
    TEXT_CONFIRM_ENTER,
    TEXT_START_GAME,
    TextHit,
    read_playtime,
)

# 已验证的游戏内场景判定节点（与 ready.py 一致）
IN_GAME_NODES = ("InWorld", "InMiniWorld")

DEFAULT_POLL_INTERVAL = 2.0
# 排队等待上限。与三月七的 cloud_game_max_queue_time 同义，默认 30 分钟。
DEFAULT_MAX_QUEUE_TIME = 1800.0
# 总超时：从主页到进游戏的全程上限
DEFAULT_ENTER_TIMEOUT = 600.0
# 连续命中多少次才认为稳定进入游戏
DEFAULT_CONFIRM_HITS = 2
# 同一个按钮的最短重点击间隔。点了之后界面需要时间反应，
# 每轮都点会在弹窗上乱点。
CLICK_COOLDOWN = 3.0


@dataclass
class EnterResult:
    """进入流程的结果。"""

    ok: bool
    stage: str
    elapsed: float
    message: str
    free_minutes: int | None = None
    paid_minutes: int | None = None


def _controller_of(context):
    """现取 controller，**绝不缓存**。

    实机踩到的坑：在 AgentServer 上下文里，每次访问
    ``context.tasker.controller`` 都会返回**新对象、新句柄**（实测两次访问
    ``是同一对象=False 同一句柄=False``）。把它取出来跨调用复用，句柄会失效，
    输入 API 随即抛 ``OSError: access violation reading 0xFFFFFFFFFFFFFFFF``。

    症状很有迷惑性：截图全程正常（因为截图那条路每次都重新取 controller），
    只有点击失败，且**偶发成功**（刚取到的句柄有时还没失效）。
    """
    return getattr(getattr(context, "tasker", None), "controller", None)


class _Clicker:
    """带冷却的点击器，避免同一按钮被连点。

    持有的是 ``context`` 而不是 controller：controller 句柄不能缓存，
    每次点击都要现取。
    """

    def __init__(self, context, clock, logger=None, sleeper=time.sleep):
        self._context = context
        self._clock = clock
        self._logger = logger
        self._sleep = sleeper
        self._last: dict[str, float] = {}
        self._step = "init"

    def click(self, key: str, hit: TextHit) -> bool:
        now = self._clock()
        if now - self._last.get(key, -1e9) < CLICK_COOLDOWN:
            return False
        x, y = hit.center
        try:
            self._post_click(x, y)
        except Exception as exc:
            # 必须报出**是哪一步**失败。只打异常文本时实机日志里满屏
            # "access violation" 却看不出是 move / down / up 哪个调用炸的，
            # 定位全靠猜。
            if self._logger:
                self._logger(
                    f"点击「{hit.text}」失败于 {self._step}: "
                    f"{type(exc).__name__}: {exc}"
                )
            return False
        self._last[key] = now
        if self._logger:
            self._logger(f"已点击「{hit.text}」于 ({x},{y})")
        return True

    def _post_click(self, x: int, y: int) -> None:
        """用 touch 三段式点击。

        ``post_touch_move`` -> ``post_touch_down`` -> ``post_touch_up``，
        与仓库通用工具 ``Common.utils.click_rect`` 一致。先 move 再按下更贴近
        真实鼠标：部分控件依赖 hover 状态才响应。

        controller 现取（见 :func:`_controller_of`），不能缓存。

        坐标用的是**截图的坐标空间**，不必手动换算窗口尺寸。已从
        MaaFramework 源码确认：``ControllerAgent::preproc_touch_point`` 会按
        ``image_raw / image_target`` 把点缩放到设备原始坐标，``handle_click``
        与 ``handle_touch_down/move`` 都先过它；只有控制单元带
        ``NoScalingTouchPoints`` 时才不缩放，而 ``Win32ControlUnitMgr`` 从不
        设置该标志。所以直接用 OCR box 的中心即可，窗口 1280x720 还是
        1600x900 都一样。
        """
        controller = _controller_of(self._context)
        if controller is None:
            raise RuntimeError("拿不到 controller")
        self._step = "touch_move"
        controller.post_touch_move(x, y).wait()
        self._sleep(0.05)
        self._step = "touch_down"
        controller.post_touch_down(x, y).wait()
        self._sleep(0.05)
        self._step = "touch_up"
        controller.post_touch_up().wait()
        self._step = "done"


def enter_cloud_game(
    context,
    ocr_screen,
    max_queue_time: float = DEFAULT_MAX_QUEUE_TIME,
    enter_timeout: float = DEFAULT_ENTER_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    confirm_hits: int = DEFAULT_CONFIRM_HITS,
    stop_when_no_playtime: bool = True,
    should_stop=None,
    logger=None,
    clock=time.monotonic,
    sleeper=time.sleep,
    in_game_nodes: tuple[str, ...] | None = None,
) -> EnterResult:
    """驱动启动器进入游戏。

    ``ocr_screen(image) -> LauncherScreen`` 由调用方注入，便于离线测试时
    直接喂造好的 OCR 结果，不需要真的跑模型。
    """
    nodes = in_game_nodes or IN_GAME_NODES
    started = clock()
    deadline = started + max(float(enter_timeout), 0.0)
    needed = max(int(confirm_hits), 1)

    # 注意：这里**不**把 controller 取出来存着。它的句柄每次访问都不同，
    # 缓存复用会让输入 API 抛 access violation。见 _controller_of 的说明。
    clicker = _Clicker(context, clock, logger, sleeper=sleeper)

    def log(message: str) -> None:
        if logger is not None:
            logger(message)

    streak = 0
    hit_node = ""
    # 排队计时从「离开主页 / 弹窗」那一刻开始，而不是从函数入口，
    # 否则等登录的时间会被算进排队时长。
    queue_started: float | None = None
    free_minutes: int | None = None
    paid_minutes: int | None = None
    logged_stages: set[str] = set()

    def result(ok: bool, stage: str, message: str) -> EnterResult:
        return EnterResult(
            ok=ok,
            stage=stage,
            elapsed=clock() - started,
            message=message,
            free_minutes=free_minutes,
            paid_minutes=paid_minutes,
        )

    while clock() < deadline:
        if should_stop is not None and should_stop():
            return result(False, "stopped", "进入云游戏时任务被停止")

        image = _screencap(context)
        if image is None:
            sleeper(max(float(poll_interval), 0.05))
            continue

        # 1. 已经在游戏里？优先判定，避免在游戏内继续找按钮。
        matched = ""
        for node in nodes:
            try:
                if _is_hit(context.run_recognition(node, image)):
                    matched = node
                    break
            except Exception as exc:
                # 识别异常不能当作「已进入游戏」，否则会在加载页就放行。
                log(f"识别 {node} 异常: {exc}")

        if matched:
            if matched != hit_node:
                hit_node = matched
                streak = 0
            streak += 1
            if streak >= needed:
                return result(True, "in_game", f"已进入游戏（{matched}）")
            sleeper(max(float(poll_interval), 0.05))
            continue

        streak = 0
        hit_node = ""

        # 2. 不在游戏里，看启动器界面处于哪个阶段。
        try:
            screen = ocr_screen(image)
        except Exception as exc:
            log(f"启动器界面 OCR 异常: {exc}")
            sleeper(max(float(poll_interval), 0.05))
            continue

        # 2a. 确认弹窗最优先——它有 30 秒倒计时，错过就白跑一趟。
        if screen.is_confirm_dialog:
            queue_started = None
            countdown = screen.confirm_countdown
            if "confirm" not in logged_stages:
                logged_stages.add("confirm")
                tail = f"，剩余 {countdown}s" if countdown is not None else ""
                log(f"出现启动确认弹窗{tail}")
            enter = screen.find(TEXT_CONFIRM_ENTER)
            if enter is not None:
                clicker.click("confirm_enter", enter)
            else:
                log("确认弹窗上没找到「进入游戏」按钮，等下一帧")
            sleeper(max(float(poll_interval), 0.05))
            continue

        # 2b. 启动器主页：先看时长，再点开始游戏。
        if screen.is_home:
            queue_started = None
            free_minutes, paid_minutes = read_playtime(screen)
            if "home" not in logged_stages:
                logged_stages.add("home")
                log(
                    "启动器主页，剩余时长 免费="
                    f"{_fmt_minutes(free_minutes)} 付费={_fmt_minutes(paid_minutes)}"
                )

            # 只有**明确读到**两项都为 0 才算时长耗尽。读不到（None）不能停：
            # OCR 抖一帧就中止任务是不可接受的。
            if stop_when_no_playtime and free_minutes == 0 and paid_minutes == 0:
                return result(
                    False,
                    "no_playtime",
                    "云游戏剩余时长为 0（免费与付费均为 0），已停止。请先充值或等免费时长刷新",
                )

            start = screen.find(TEXT_START_GAME)
            if start is not None:
                clicker.click("start_game", start)
            else:
                log("主页上没找到「开始游戏」按钮，等下一帧")
            sleeper(max(float(poll_interval), 0.05))
            continue

        # 2c. 既不在游戏、也不在主页/弹窗 —— 排队、串流加载或登录中。
        #
        # 排队界面尚未实机采集，这里不猜它的文案与 ROI。把 OCR 文本打进日志，
        # 用户实机跑一次日志里就会出现真实排队文案，据此再做精确识别。
        if queue_started is None:
            queue_started = clock()
            log("已离开启动器主页，正在排队 / 加载中")
        waited = clock() - queue_started
        snapshot = screen.all_text
        if snapshot and snapshot not in logged_stages:
            logged_stages.add(snapshot)
            log(f"当前画面文本（{waited:.0f}s）: {snapshot}")

        if waited >= max(float(max_queue_time), 0.0):
            return result(
                False,
                "queue_timeout",
                f"排队 / 加载超过上限 {max_queue_time:.0f}s，已放弃。"
                "可在任务选项里调高「排队等待上限」，或改到低峰时段运行",
            )

        sleeper(max(float(poll_interval), 0.05))

    return result(
        False,
        "timeout",
        f"进入云游戏总超时（{enter_timeout:.0f}s）。"
        "请确认云客户端已保存登录状态；若停在登录页需先手动登录一次",
    )


def _fmt_minutes(value: int | None) -> str:
    if value is None:
        return "未读到"
    return f"{value // 60}小时{value % 60}分钟"


def _is_hit(result) -> bool:
    """兼容 MAA 不同返回结构，统一判断识别是否命中。"""
    if result is None:
        return False
    status = getattr(result, "status", None)
    succeeded = getattr(status, "succeeded", None)
    if succeeded is not None:
        return bool(succeeded)
    if status is not None:
        return status == 0
    return bool(getattr(result, "hit", True))


def _screencap(context):
    """通过控制器截取当前画面；失败返回 ``None``。"""
    controller = _controller_of(context)
    if controller is None:
        return None
    try:
        return controller.post_screencap().wait().get()
    except Exception:
        return None


__all__ = [
    "CLICK_COOLDOWN",
    "DEFAULT_CONFIRM_HITS",
    "DEFAULT_ENTER_TIMEOUT",
    "DEFAULT_MAX_QUEUE_TIME",
    "DEFAULT_POLL_INTERVAL",
    "EnterResult",
    "IN_GAME_NODES",
    "enter_cloud_game",
]
