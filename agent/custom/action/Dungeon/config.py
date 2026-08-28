"""刷本配置的解析与校验。

## 为什么是配置驱动

刷本流程里真正难的部分不是循环，而是「点哪里、认什么字」。这些值必须来自
**实机截图标定**，凭空写一个 ROI 只会做出一个看起来能跑、实际乱点的功能
（这类 bug 极难排查，因为它在某些分辨率/语言下偶然能过）。

所以本模块只定义 *结构*：副本名、传送点、每一步做什么。所有屏幕坐标、
OCR 文本、模板路径都由用户在 JSON 里显式给出。缺一个就报错，绝不猜。

## 校验的强制要求

- ``ocr`` 步骤必须同时给 ``text`` 和 ``roi``
- ``template`` 步骤必须同时给 ``path`` 和 ``roi``
- ``key`` / ``hold`` 步骤的键必须在内核 VK 表里（否则运行时静默不发键）
- ``hold`` 必须给正的 ``duration``（按住多久）
- ``search`` 必须给 ``until``（识别类、不点击）与至少一步 ``probe``
- ``roi`` 必须是 4 个数且宽高为正

解析失败返回 ``issues``，由调用方决定是警告还是拒绝运行。致命问题进
``errors``，调用方**必须**拒绝运行——带着错配置跑刷本会在游戏里乱点。

## 为什么需要 hold 和 search

副本流程里有两段无法用「点某个坐标」表达的动作：

- **进副本要走过去才开打**：敌人在远处，不前进就一直站着。
- **打完要找出口才能领奖**：出口位置随地形和镜头朝向变化，没有固定坐标。

``hold``（按住方向键 N 秒）+ ``search``（反复 probe 直到 until 命中）
把这两段变成可配置、可离线验证的结构：配置只说「怎么小步探一下」和
「看到什么算到了」，具体位置来自识别结果，不来自猜测的坐标。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

try:
    from agent.custom.action.Combat.kernel.constants import (
        DEFAULT_HEIGHT,
        DEFAULT_WIDTH,
        MOUSE_VK,
        VK,
    )
except ImportError:  # pragma: no cover - 直接以包内相对路径导入时
    from ..Combat.kernel.constants import (
        DEFAULT_HEIGHT,
        DEFAULT_WIDTH,
        MOUSE_VK,
        VK,
    )

# 步骤类型
STEP_OCR = "ocr"
STEP_TEMPLATE = "template"
STEP_NODE = "node"
STEP_KEY = "key"
STEP_WAIT = "wait"
STEP_CLICK = "click_rect"
STEP_HOLD = "hold"
STEP_SEARCH = "search"

STEP_TYPES = (
    STEP_OCR,
    STEP_TEMPLATE,
    STEP_NODE,
    STEP_KEY,
    STEP_WAIT,
    STEP_CLICK,
    STEP_HOLD,
    STEP_SEARCH,
)

# 识别类步骤：可以放在 ``settle`` 或 ``search.until`` 位置。
RECOGNITION_STEPS = (STEP_OCR, STEP_TEMPLATE, STEP_NODE)

# 流程阶段。顺序即执行顺序，``settle`` 是识别而非动作序列。
PHASE_ENTRY = "entry"
PHASE_CONFIRM = "confirm"
PHASE_ADVANCE = "advance"
PHASE_LOCATE = "locate"
PHASE_REWARD = "reward"
PHASE_RECOVER = "recover"
PHASE_EXIT = "exit"

ACTION_PHASES = (
    PHASE_ENTRY,
    PHASE_CONFIRM,
    PHASE_ADVANCE,
    PHASE_LOCATE,
    PHASE_REWARD,
    PHASE_EXIT,
)
ALL_PHASES = ACTION_PHASES + (PHASE_RECOVER,)

# 默认值。只用于「重试几次、等多久」这类与界面无关的参数；
# 任何与屏幕位置有关的值都没有默认，必须用户给。
DEFAULT_STEP_TIMEOUT = 10.0
DEFAULT_STEP_INTERVAL = 0.4
DEFAULT_OCR_THRESHOLD = 0.7
DEFAULT_TEMPLATE_THRESHOLD = 0.8
DEFAULT_SETTLE_TIMEOUT = 120.0
DEFAULT_MAX_FAILURES = 3
DEFAULT_ROUNDS = 1
MAX_ROUNDS = 9999

# search 步骤默认值。
# 探测循环的超时必须比单次识别长得多：它要覆盖「走一段路再看一眼」的全过程。
DEFAULT_SEARCH_TIMEOUT = 60.0
# 按住方向键的单次上限。超过这个值一次走太远，撞墙也发现不了；
# 探测循环本来就是「小步走 + 频繁看」，一步不该太大。
MAX_HOLD_DURATION = 10.0

# 轮次之间怎么回到「可以再打一次」的状态。
# ``reenter``：exit 退回大世界，下一轮重新走 entry+confirm 进副本。慢但状态明确。
# ``again``  ：exit 点「再次挑战」，副本原地重开，下一轮直接从 advance 开始。
#             只在上一轮**成功**时才敢跳过 entry——失败后当前界面不确定，
#             必须走完整流程重新定位，否则会在错误的界面上继续乱点。
LOOP_MODE_REENTER = "reenter"
LOOP_MODE_AGAIN = "again"
LOOP_MODES = (LOOP_MODE_REENTER, LOOP_MODE_AGAIN)
DEFAULT_LOOP_MODE = LOOP_MODE_REENTER

# 界面选项的挂钩角色。
# ``stage``      —— 在副本列表里选哪一个（OCR 文本可被界面覆盖）
# ``difficulty`` —— 难度按钮（点击矩形由 difficulty_rects 决定）
ROLE_STAGE = "stage"
ROLE_DIFFICULTY = "difficulty"
KNOWN_ROLES = (ROLE_STAGE, ROLE_DIFFICULTY)

# 「缺少战斗脚本」错误的固定文案。入口层在界面选了预设后需要撤掉这条错误，
# 用共享常量而不是字符串前缀匹配——否则改文案会让撤销静默失效。
ERROR_COMBAT_NO_SCRIPT = (
    "combat 必须指定 script / preset / script_path 之一，"
    "否则 AutoCombat 会拒绝运行（不提供隐式默认脚本）"
)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_roi(raw, where: str, errors: list[str]):
    """ROI 必须是 4 个数，宽高为正。缺失或畸形一律报错，不给默认值。"""
    if raw is None:
        errors.append(f"{where}: 缺少 roi（必须从实机截图标定，不能省略）")
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        errors.append(f"{where}: roi 必须是 [x, y, w, h] 四个数，得到 {raw!r}")
        return None
    if not all(_is_number(v) for v in raw):
        errors.append(f"{where}: roi 的四个值必须都是数字，得到 {raw!r}")
        return None
    x, y, w, h = (int(v) for v in raw)
    if w <= 0 or h <= 0:
        errors.append(f"{where}: roi 宽高必须为正，得到 w={w} h={h}")
        return None
    return [x, y, w, h]


def _check_roi_bounds(roi, where: str, issues: list[str]):
    """ROI 超出 720p 基准范围时提醒。不报错——只警告，避免挡掉边缘情况。"""
    if roi is None:
        return
    x, y, w, h = roi
    if x < 0 or y < 0 or x + w > DEFAULT_WIDTH or y + h > DEFAULT_HEIGHT:
        issues.append(
            f"{where}: roi {roi} 超出 {DEFAULT_WIDTH}x{DEFAULT_HEIGHT} 基准范围，"
            "请确认是按 720p 标定的"
        )


@dataclass(frozen=True)
class Step:
    """一个流程步骤。

    字段按类型使用，未用到的保持默认。刻意用一个扁平结构而不是继承树：
    步骤种类少且固定，扁平结构让 JSON 与代码一一对应，读配置时不用跳类。
    """

    type: str
    desc: str = ""
    # 界面选项的挂钩点。空字符串表示这一步不受界面覆盖影响。
    # 用角色标签而不是「第几步」来定位，是因为步骤顺序会随配置调整，
    # 而按下标覆盖会在用户插一步之后静默改错对象。
    role: str = ""
    # ocr / template
    text: tuple = ()
    path: str = ""
    roi: tuple = ()
    threshold: float = 0.0
    click: bool = True
    # 识别类步骤的等待
    timeout: float = DEFAULT_STEP_TIMEOUT
    interval: float = DEFAULT_STEP_INTERVAL
    # 找不到时是否算失败（False 表示可选步骤，找不到就跳过）
    required: bool = True
    # node
    node: str = ""
    # key / hold
    key: str = ""
    # wait / hold
    duration: float = 0.0
    # search：探测循环
    until: Any = None
    probe: tuple = ()
    on_found: tuple = ()
    sweep: tuple = ()
    sweep_every: int = 0
    # 执行后额外等待
    post_wait: float = 0.0

    def describe(self) -> str:
        if self.desc:
            return self.desc
        if self.type == STEP_OCR:
            return f"ocr{list(self.text)}@{list(self.roi)}"
        if self.type == STEP_TEMPLATE:
            return f"template({self.path})@{list(self.roi)}"
        if self.type == STEP_NODE:
            return f"node({self.node})"
        if self.type == STEP_KEY:
            return f"key({self.key})"
        if self.type == STEP_WAIT:
            return f"wait({self.duration}s)"
        if self.type == STEP_CLICK:
            return f"click{list(self.roi)}"
        if self.type == STEP_HOLD:
            return f"hold({self.key},{self.duration}s)"
        if self.type == STEP_SEARCH:
            target = self.until.describe() if self.until is not None else "?"
            return f"search(until={target},probe={len(self.probe)}步)"
        return self.type


def _parse_text_list(raw, where: str, errors: list[str]) -> tuple:
    """OCR 期望文本，支持单串或列表（多语言/多写法）。"""
    if raw is None:
        errors.append(f"{where}: 缺少 text（OCR 要匹配的文本）")
        return ()
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    result = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{where}: text 里出现空或非字符串项: {item!r}")
            continue
        result.append(item)
    if not result:
        errors.append(f"{where}: text 为空")
    return tuple(result)


def parse_step(
    raw,
    where: str,
    errors: list[str],
    issues: list[str],
    *,
    default_click: bool = True,
    allow_search: bool = True,
):
    """解析单个步骤。返回 ``Step`` 或 ``None``（已记入 errors）。

    ``default_click`` 用于识别类步骤：动作序列里的 OCR/模板默认点击命中处，
    但 ``settle`` / ``search.until`` 这类**纯观测**位置默认不点——它们只是
    在问「到了没」，点下去会在不确定的界面上乱按。
    """
    if isinstance(raw, str):
        # 语法糖：裸字符串当作已有 pipeline 节点名
        return Step(
            type=STEP_NODE, node=raw, click=default_click, desc=f"node({raw})"
        )

    if not isinstance(raw, dict):
        errors.append(f"{where}: 步骤必须是对象或节点名字符串，得到 {type(raw).__name__}")
        return None

    # 类型可以显式给，也可以由出现的键推断（让 JSON 写起来更短）
    stype = raw.get("type")
    if not stype:
        for candidate in STEP_TYPES:
            if candidate in raw:
                stype = candidate
                break
    if not stype and "until" in raw:
        # search 的标志性字段。允许省略 type，写起来更接近「走到看见 X 为止」
        stype = STEP_SEARCH
    if not stype:
        errors.append(
            f"{where}: 无法判断步骤类型，请给 type 或使用 "
            f"{', '.join(STEP_TYPES)} 之一作为键"
        )
        return None
    if stype not in STEP_TYPES:
        errors.append(f"{where}: 未知步骤类型 {stype!r}，可用: {', '.join(STEP_TYPES)}")
        return None
    if stype == STEP_SEARCH and not allow_search:
        errors.append(
            f"{where}: search 不能嵌套在另一个 search 的子步骤里"
            "（探测循环里再套循环，超时与失败语义会变得无法预期）"
        )
        return None

    desc = str(raw.get("desc", "") or "")
    role = str(raw.get("role", "") or "").strip()
    if role and role not in KNOWN_ROLES:
        issues.append(
            f"{where}: 未知的 role {role!r}（可用: {', '.join(KNOWN_ROLES)}），"
            "界面选项不会覆盖这一步"
        )
    required = bool(raw.get("required", True))
    post_wait = raw.get("post_wait", 0.0)
    if not _is_number(post_wait) or post_wait < 0:
        issues.append(f"{where}: post_wait 非法（{post_wait!r}），按 0 处理")
        post_wait = 0.0

    common = {
        "desc": desc,
        "role": role,
        "required": required,
        "post_wait": float(post_wait),
    }

    if stype in (STEP_OCR, STEP_TEMPLATE):
        timeout = raw.get("timeout", DEFAULT_STEP_TIMEOUT)
        interval = raw.get("interval", DEFAULT_STEP_INTERVAL)
        if not _is_number(timeout) or timeout < 0:
            issues.append(f"{where}: timeout 非法（{timeout!r}），按默认处理")
            timeout = DEFAULT_STEP_TIMEOUT
        if not _is_number(interval) or interval <= 0:
            issues.append(f"{where}: interval 非法（{interval!r}），按默认处理")
            interval = DEFAULT_STEP_INTERVAL
        roi = _parse_roi(raw.get("roi"), where, errors)
        _check_roi_bounds(roi, where, issues)
        click = bool(raw.get("click", default_click))

        if stype == STEP_OCR:
            text = _parse_text_list(raw.get("text", raw.get(STEP_OCR)), where, errors)
            threshold = raw.get("threshold", DEFAULT_OCR_THRESHOLD)
            if not _is_number(threshold) or not 0 < threshold <= 1:
                issues.append(
                    f"{where}: OCR threshold 非法（{threshold!r}），按 "
                    f"{DEFAULT_OCR_THRESHOLD} 处理"
                )
                threshold = DEFAULT_OCR_THRESHOLD
            if roi is None or not text:
                return None
            return Step(
                type=STEP_OCR, text=text, roi=tuple(roi),
                threshold=float(threshold), click=click,
                timeout=float(timeout), interval=float(interval), **common,
            )

        path = raw.get("path", raw.get(STEP_TEMPLATE))
        if not isinstance(path, str) or not path.strip():
            errors.append(f"{where}: 缺少 path（模板图相对 resource/base 的路径）")
            return None
        threshold = raw.get("threshold", DEFAULT_TEMPLATE_THRESHOLD)
        if not _is_number(threshold) or not 0 < threshold <= 1:
            issues.append(
                f"{where}: 模板 threshold 非法（{threshold!r}），按 "
                f"{DEFAULT_TEMPLATE_THRESHOLD} 处理"
            )
            threshold = DEFAULT_TEMPLATE_THRESHOLD
        if roi is None:
            return None
        return Step(
            type=STEP_TEMPLATE, path=path.strip(), roi=tuple(roi),
            threshold=float(threshold), click=click,
            timeout=float(timeout), interval=float(interval), **common,
        )

    if stype == STEP_NODE:
        node = raw.get("node", raw.get(STEP_NODE))
        if not isinstance(node, str) or not node.strip():
            errors.append(f"{where}: 缺少 node（已有 pipeline 节点名）")
            return None
        # node 步骤在动作位置走 run_task、在观测位置走 run_recognition，
        # click 本身不改变行为；跟着 default_click 走只是为了让
        # 「显式写 click:true」这个错误配置在 search.until 里能被查出来。
        return Step(
            type=STEP_NODE,
            node=node.strip(),
            click=bool(raw.get("click", default_click)),
            **common,
        )

    if stype == STEP_KEY:
        key = raw.get("key", raw.get(STEP_KEY))
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{where}: 缺少 key（要按的键名）")
            return None
        normalized = key.strip().lower()
        if normalized not in VK and normalized not in MOUSE_VK:
            errors.append(
                f"{where}: 未知按键 {key!r}，内核 VK 表里没有它，运行时不会发键"
            )
            return None
        return Step(type=STEP_KEY, key=normalized, **common)

    if stype == STEP_WAIT:
        duration = raw.get("wait", raw.get("duration"))
        if not _is_number(duration) or duration < 0:
            errors.append(f"{where}: wait 必须是非负数字，得到 {duration!r}")
            return None
        return Step(type=STEP_WAIT, duration=float(duration), **common)

    if stype == STEP_HOLD:
        # hold 的键值可以写在 hold 里（{"hold": "w", "duration": 2}），
        # 也可以分开写（{"type": "hold", "key": "w", "duration": 2}）。
        key = raw.get("key", raw.get(STEP_HOLD))
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{where}: hold 缺少要按住的键名")
            return None
        normalized = key.strip().lower()
        if normalized not in VK:
            # 鼠标键不走这里：按住鼠标是攻击行为，属于战斗脚本的职责。
            errors.append(
                f"{where}: hold 的按键 {key!r} 不在内核 VK 表里，运行时不会发键"
            )
            return None
        duration = raw.get("duration", raw.get("wait"))
        if not _is_number(duration) or duration <= 0:
            errors.append(
                f"{where}: hold 必须给正的 duration（按住多久），得到 {duration!r}"
            )
            return None
        if duration > MAX_HOLD_DURATION:
            issues.append(
                f"{where}: hold duration={duration} 过长，截断为 "
                f"{MAX_HOLD_DURATION}（一次走太远撞墙也发现不了，"
                "请用 search 的探测循环分多次走）"
            )
            duration = MAX_HOLD_DURATION
        return Step(
            type=STEP_HOLD, key=normalized, duration=float(duration), **common
        )

    if stype == STEP_SEARCH:
        return _parse_search(raw, where, errors, issues, common)

    # STEP_CLICK：只接受用户显式给出的矩形
    roi = _parse_roi(raw.get("click_rect", raw.get("roi")), where, errors)
    _check_roi_bounds(roi, where, issues)
    if roi is None:
        return None
    return Step(type=STEP_CLICK, roi=tuple(roi), **common)


def _parse_search(raw, where: str, errors: list[str], issues: list[str], common):
    """解析 search 步骤：反复执行 ``probe`` 直到 ``until`` 命中。

    这是「走过去找出口」这类操作的唯一诚实写法：出口在哪不固定（副本地形
    随机、镜头朝向不同），没法预先给一个坐标。所以配置只描述
    「怎么小步探一下」和「看到什么算到了」，位置由识别结果给。

    ``until`` 必须是识别类步骤且**不点击**：它只回答「到了没」。真正要做的
    交互（按 F、点领取）放在 ``on_found`` 里，这样命中与动作分开，
    日志能看出到底是没找到还是找到了却没点成。
    """
    until_raw = raw.get("until", raw.get(STEP_SEARCH))
    if until_raw is None:
        errors.append(
            f"{where}: search 缺少 until（探测的终止条件，"
            "即「看到什么算找到了」）"
        )
        return None

    until = parse_step(
        until_raw,
        f"{where}.until",
        errors,
        issues,
        default_click=False,
        allow_search=False,
    )
    if until is None:
        return None
    if until.type not in RECOGNITION_STEPS:
        errors.append(
            f"{where}.until: 必须是识别类步骤（{'/'.join(RECOGNITION_STEPS)}），"
            f"得到 {until.type}——探测循环需要一个可反复观测的条件"
        )
        return None
    if until.click:
        # 显式写了 click: true。这会让每次探测都点一下识别位置，
        # 循环里反复乱点是很难排查的问题，直接拒绝而不是悄悄改掉。
        errors.append(
            f"{where}.until: 不能设 click:true。终止条件只负责判断「到了没」，"
            f"要执行的交互请放到 {where}.on_found 里"
        )
        return None

    probe = _parse_steps(
        raw.get("probe"), f"{where}.probe", errors, issues, allow_search=False
    )
    if not probe:
        errors.append(
            f"{where}.probe: 至少要有一步（每轮探测做什么，"
            "例如 hold 前进 1 秒）。没有 probe 的 search 只是原地空转"
        )
        return None

    on_found = _parse_steps(
        raw.get("on_found"), f"{where}.on_found", errors, issues, allow_search=False
    )
    sweep = _parse_steps(
        raw.get("sweep"), f"{where}.sweep", errors, issues, allow_search=False
    )

    sweep_every = raw.get("sweep_every", 0)
    if not isinstance(sweep_every, int) or isinstance(sweep_every, bool) or sweep_every < 0:
        issues.append(f"{where}: sweep_every 非法（{sweep_every!r}），按 0 处理")
        sweep_every = 0
    if sweep and sweep_every <= 0:
        # 给了转向动作却没说隔几轮转一次：默认每轮都转会导致原地打转，
        # 这里取一个保守值并提醒，而不是静默忽略 sweep。
        issues.append(
            f"{where}: 给了 sweep 但没给 sweep_every，按每 3 轮转一次处理"
        )
        sweep_every = 3
    if sweep_every > 0 and not sweep:
        issues.append(f"{where}: 给了 sweep_every 但没有 sweep 步骤，该值无效")
        sweep_every = 0

    timeout = raw.get("timeout", DEFAULT_SEARCH_TIMEOUT)
    if not _is_number(timeout) or timeout <= 0:
        issues.append(
            f"{where}: search timeout 非法（{timeout!r}），按 "
            f"{DEFAULT_SEARCH_TIMEOUT} 处理"
        )
        timeout = DEFAULT_SEARCH_TIMEOUT

    return Step(
        type=STEP_SEARCH,
        until=until,
        probe=probe,
        on_found=on_found,
        sweep=sweep,
        sweep_every=int(sweep_every),
        timeout=float(timeout),
        **common,
    )


def _parse_steps(
    raw,
    where: str,
    errors: list[str],
    issues: list[str],
    *,
    allow_search: bool = True,
) -> tuple:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        errors.append(f"{where}: 必须是步骤数组，得到 {type(raw).__name__}")
        return ()
    steps = []
    for index, item in enumerate(raw):
        step = parse_step(
            item,
            f"{where}[{index}]",
            errors,
            issues,
            allow_search=allow_search,
        )
        if step is not None:
            steps.append(step)
    return tuple(steps)


@dataclass
class DungeonConfig:
    """一个副本的完整刷取配置。"""

    name: str = ""
    desc: str = ""
    teleport_point_id: str = ""
    rounds: int = DEFAULT_ROUNDS
    max_failures: int = DEFAULT_MAX_FAILURES
    loop_mode: str = DEFAULT_LOOP_MODE
    # 各阶段动作序列
    phases: dict = field(default_factory=dict)
    # 难度按钮的点击矩形，按 I..V 顺序。界面选了第 N 档就用第 N 个。
    # 单独放在顶层而不是写死在步骤里，是为了让「换难度」只改一个数字，
    # 而不是让用户去猜某个 click_rect 该填什么坐标。
    difficulty_rects: tuple = ()
    # 战斗结束识别（缺省则依赖 AutoCombat 自身的时长/脱战判定）
    settle: Any = None
    settle_timeout: float = DEFAULT_SETTLE_TIMEOUT
    # 传给 AutoCombat 的参数
    combat: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    issues: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def steps(self, phase: str) -> tuple:
        return self.phases.get(phase, ())

    def describe(self) -> str:
        parts = [f"name={self.name or '<未命名>'}", f"rounds={self.rounds}"]
        if self.teleport_point_id:
            parts.append(f"teleport={self.teleport_point_id}")
        parts.append(f"loop={self.loop_mode}")
        for phase in ALL_PHASES:
            count = len(self.steps(phase))
            if count:
                parts.append(f"{phase}={count}步")
        if self.settle is not None:
            parts.append("settle=有")
        return " ".join(parts)


def _parse_difficulty_rects(raw, errors: list[str], issues: list[str]) -> tuple:
    """解析难度按钮矩形表。

    缺省或空数组都表示「这个副本没有难度档位」——两者语义相同，
    模板里写 `[]` 是在说明这个字段存在，不该因此被拒绝运行。
    """
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        errors.append(
            f"difficulty_rects 必须是数组（每档一个 [x,y,w,h]），"
            f"得到 {type(raw).__name__}"
        )
        return ()
    if not raw:
        return ()
    rects = []
    for index, item in enumerate(raw):
        rect = _parse_roi(item, f"difficulty_rects[{index}]", errors)
        _check_roi_bounds(rect, f"difficulty_rects[{index}]", issues)
        if rect is not None:
            rects.append(tuple(rect))
    return tuple(rects)


def parse_config(raw) -> DungeonConfig:
    """把 JSON 数据解析成 ``DungeonConfig``。

    永远返回对象（不抛异常），把问题收在 ``errors`` / ``issues`` 里，
    让调用方能一次性把所有配置问题告诉用户，而不是改一个报一个。
    """
    errors: list[str] = []
    issues: list[str] = []

    if not isinstance(raw, dict):
        return DungeonConfig(
            errors=[f"配置必须是 JSON 对象，得到 {type(raw).__name__}"]
        )

    # 未标定守卫：模板配置里的 ROI 是占位值，直接跑会在游戏里乱点。
    # 只有显式写成 false 才拒绝——用户自己写的配置不带这个字段，无额外负担。
    if raw.get("_calibrated") is False:
        return DungeonConfig(
            name=str(raw.get("name", "") or "").strip(),
            errors=[
                "该配置标记为未标定（_calibrated: false）。"
                "里面的 roi/文本只是占位示例，直接运行会在游戏里点错位置。"
                "请按 1280x720 实机截图校准每个 roi 与 OCR 文本，"
                "确认无误后把 _calibrated 改为 true 或删掉该字段"
            ],
        )

    name = str(raw.get("name", "") or "").strip()
    if not name:
        issues.append("缺少 name，日志里会显示为 <未命名>")

    rounds = raw.get("rounds", DEFAULT_ROUNDS)
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 0:
        errors.append(f"rounds 必须是非负整数（0 表示无限），得到 {rounds!r}")
        rounds = DEFAULT_ROUNDS
    elif rounds > MAX_ROUNDS:
        issues.append(f"rounds={rounds} 过大，截断为 {MAX_ROUNDS}")
        rounds = MAX_ROUNDS

    max_failures = raw.get("max_failures", DEFAULT_MAX_FAILURES)
    if (
        not isinstance(max_failures, int)
        or isinstance(max_failures, bool)
        or max_failures < 1
    ):
        issues.append(
            f"max_failures 非法（{max_failures!r}），按 {DEFAULT_MAX_FAILURES} 处理"
        )
        max_failures = DEFAULT_MAX_FAILURES

    teleport = raw.get("teleport_point_id", raw.get("teleport", "")) or ""
    if not isinstance(teleport, str):
        errors.append(f"teleport_point_id 必须是字符串，得到 {teleport!r}")
        teleport = ""

    loop_mode = raw.get("loop_mode", DEFAULT_LOOP_MODE)
    if not isinstance(loop_mode, str) or loop_mode.strip() not in LOOP_MODES:
        issues.append(
            f"loop_mode 非法（{loop_mode!r}，可用: {', '.join(LOOP_MODES)}），"
            f"按 {DEFAULT_LOOP_MODE} 处理"
        )
        loop_mode = DEFAULT_LOOP_MODE
    else:
        loop_mode = loop_mode.strip()

    # 阶段步骤。兼容两种写法：顶层直接给，或放在 steps 下。
    holder = raw.get("steps") if isinstance(raw.get("steps"), dict) else raw
    phases = {}
    for phase in ALL_PHASES:
        steps = _parse_steps(holder.get(phase), f"{phase}", errors, issues)
        if steps:
            phases[phase] = steps

    settle_raw = raw.get("settle")
    difficulty_rects = _parse_difficulty_rects(
        raw.get("difficulty_rects"), errors, issues
    )
    settle = None
    if settle_raw is not None:
        settle = parse_step(
            settle_raw, "settle", errors, issues,
            default_click=False, allow_search=False,
        )
        if settle is not None and settle.type not in RECOGNITION_STEPS:
            errors.append(
                f"settle 必须是识别类步骤（{'/'.join(RECOGNITION_STEPS)}），"
                f"得到 {settle.type}"
            )
            settle = None

    settle_timeout = raw.get("settle_timeout", DEFAULT_SETTLE_TIMEOUT)
    if not _is_number(settle_timeout) or settle_timeout <= 0:
        issues.append(
            f"settle_timeout 非法（{settle_timeout!r}），按 "
            f"{DEFAULT_SETTLE_TIMEOUT} 处理"
        )
        settle_timeout = DEFAULT_SETTLE_TIMEOUT

    combat = raw.get("combat")
    if combat is None:
        combat = {}
    elif not isinstance(combat, dict):
        errors.append(f"combat 必须是对象，得到 {type(combat).__name__}")
        combat = {}
    else:
        has_script = any(
            key in combat for key in ("script", "preset", "script_path")
        )
        if not has_script:
            errors.append(ERROR_COMBAT_NO_SCRIPT)

    # 没有任何可执行内容的配置直接拒绝：跑起来什么都不会发生，
    # 用户只会以为程序坏了。
    if not phases and settle is None:
        errors.append(
            "配置没有任何阶段步骤，刷本流程无事可做。"
            f"请至少配置 {PHASE_ENTRY} 或 {PHASE_CONFIRM} 阶段"
        )

    return DungeonConfig(
        name=name,
        desc=str(raw.get("desc", "") or ""),
        teleport_point_id=teleport.strip(),
        rounds=rounds,
        max_failures=max_failures,
        loop_mode=loop_mode,
        phases=phases,
        difficulty_rects=difficulty_rects,
        settle=settle,
        settle_timeout=float(settle_timeout),
        combat=combat,
        errors=errors,
        issues=issues,
    )


def apply_overrides(config: DungeonConfig, params: dict) -> list[str]:
    """把任务选项覆盖到已解析的配置上，返回给用户看的提示。

    放在这里而不是入口层，是为了能离线测试：界面开关不生效是一类很难在
    实机上察觉的 bug（用户以为选了，其实没生效），必须有断言兜着。

    覆盖语义：

    - ``rounds`` / ``teleport_point_id`` / ``skip_teleport`` 直接改字段；
    - ``stage`` 改写 ``role: "stage"`` 步骤的 OCR 文本（在副本列表里选哪个）；
    - ``difficulty`` 改写 ``role: "difficulty"`` 步骤的点击矩形，0 表示
      「不动难度」，此时该步骤会被整个移除——沿用游戏记住的上次选择比
      点一个不确定的坐标安全；
    - ``combat_preset`` 为空表示「用配置文件里的」，非空才覆盖。覆盖时清掉
      ``script`` / ``script_path``，因为 AutoCombat 按 ``script > preset >
      script_path`` 取值，留着它们会让界面选择失效；
    - 覆盖出了脚本之后，撤掉解析期的「缺少战斗脚本」错误——否则用户写了
      ``combat: {"duration": 200}`` 再在界面选预设会被误拒。
    """
    notes: list[str] = []
    if params is None:
        return notes

    if params.get("rounds") is not None:
        try:
            config.rounds = max(0, int(params["rounds"]))
        except (TypeError, ValueError):
            notes.append(f"rounds 非法（{params['rounds']!r}），沿用配置值")

    teleport = params.get("teleport_point_id")
    if teleport:
        config.teleport_point_id = str(teleport).strip()
    if _parse_truthy(params.get("skip_teleport")):
        config.teleport_point_id = ""

    notes.extend(_apply_stage(config, params))
    notes.extend(_apply_difficulty(config, params))

    preset = str(params.get("combat_preset", "") or "").strip()
    if preset:
        for key in ("script", "script_path"):
            config.combat.pop(key, None)
        config.combat["preset"] = preset
        config.errors = [
            item for item in config.errors if item != ERROR_COMBAT_NO_SCRIPT
        ]

    if params.get("log_decisions") is not None:
        config.combat["log_decisions"] = _parse_truthy(
            params.get("log_decisions")
        )

    return notes


def _map_role_steps(config, role: str, transform):
    """对所有带指定 role 的步骤应用 ``transform``。

    ``transform`` 返回新步骤，或返回 ``None`` 表示删掉这一步。
    返回命中的步骤数，让调用方能区分「改了」和「配置里根本没这个角色」——
    后者必须告诉用户，否则界面上选了却什么都没发生。
    """
    hits = 0
    for phase, steps in list(config.phases.items()):
        replaced = []
        for step in steps:
            if step.role != role:
                replaced.append(step)
                continue
            hits += 1
            new_step = transform(step)
            if new_step is not None:
                replaced.append(new_step)
        config.phases[phase] = tuple(replaced)
    return hits


def _apply_stage(config, params) -> list[str]:
    """把界面选的副本名写进 ``role: "stage"`` 的 OCR 步骤。"""
    stage = str(params.get("stage", "") or "").strip()
    if not stage:
        return []

    notes: list[str] = []

    def rewrite(step):
        if step.type != STEP_OCR:
            notes.append(
                f"role=stage 的步骤类型是 {step.type}，只有 ocr 步骤能按副本名覆盖，已跳过"
            )
            return step
        return replace(step, text=(stage,), desc=step.desc or f"选择副本「{stage}」")

    hits = _map_role_steps(config, ROLE_STAGE, rewrite)
    if hits == 0:
        notes.append(
            f"界面选了副本「{stage}」，但配置里没有 role=\"stage\" 的步骤，"
            "这个选择不会生效"
        )
    return notes


def _apply_difficulty(config, params) -> list[str]:
    """按界面选的档位改写 ``role: "difficulty"`` 步骤的点击矩形。"""
    raw = params.get("difficulty")
    if raw is None or raw == "":
        return []
    try:
        level = int(raw)
    except (TypeError, ValueError):
        return [f"difficulty 非法（{raw!r}），沿用配置值"]
    if level < 0:
        return [f"difficulty 不能是负数（{level}），沿用配置值"]

    notes: list[str] = []

    if level == 0:
        removed = _map_role_steps(config, ROLE_DIFFICULTY, lambda step: None)
        if removed:
            notes.append("difficulty=0：跳过难度选择，沿用游戏里上次选的档位")
        return notes

    rects = config.difficulty_rects
    if not rects:
        return [
            f"界面选了难度 {level}，但配置里没有 difficulty_rects，"
            "无法确定要点哪个按钮（这个选择不会生效）"
        ]
    if level > len(rects):
        return [
            f"难度 {level} 超出配置的 {len(rects)} 档（difficulty_rects），"
            "沿用配置值"
        ]

    rect = rects[level - 1]

    def rewrite(step):
        if step.type != STEP_CLICK:
            notes.append(
                f"role=difficulty 的步骤类型是 {step.type}，"
                "只有 click_rect 步骤能按档位覆盖，已跳过"
            )
            return step
        return replace(
            step, roi=tuple(rect), desc=step.desc or f"选择难度第 {level} 档"
        )

    hits = _map_role_steps(config, ROLE_DIFFICULTY, rewrite)
    if hits == 0:
        notes.append(
            f"界面选了难度 {level}，但配置里没有 role=\"difficulty\" 的步骤，"
            "这个选择不会生效"
        )
    return notes


def _parse_truthy(value) -> bool:
    """本模块自用的宽松布尔解析。

    不复用 pinkpaw 的 ``_parse_bool``：那个模块会拉进 win32 依赖，
    而本模块要保持可离线导入（校验脚本在没有 MAA 的环境里也得能跑）。
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {
            "1", "true", "yes", "on", "enable", "enabled",
        }
    return bool(value)
