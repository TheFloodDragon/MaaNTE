# -*- coding: utf-8 -*-
"""角色都市技能同步 — Custom Action

由 Pipeline 节点 ``SyncCharacterAbilityCityAbilityMain`` 驱动，
自定义动作名 ``SyncCharacterAbilityCityAbilityMainAction``。

遍历角色列表，OCR 识别每个角色的名字和两个都市技能等级，
通过 ``CharacterAbility_CityAbility`` 管理器持久化数据。

``custom_action_param``::

    {"fresh_record": true}   -- 全新记录模式：全部扫描完毕后清空旧数据再存入
    {} 或不传                -- 默认模式：逐角色保存
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.pipeline import JRecognitionType, JTemplateMatch

from custom.action.Common.CharacterAbility_CityAbility import (
    clear_all,
    set_character_abilities,
)

from utils.logger import logger
from utils.task_control import (
    TaskCancelledError,
    TaskFailedError,
    interruptible_wait,
    require_task,
)

# ---------------------------------------------------------------------------
# 子节点名称（对应 SyncCharacterAbilityCityAbility.json）
# ---------------------------------------------------------------------------
_NODE_OPEN_INFO = "SyncCharacterAbilityCityAbilityOpenInfoPage"
_NODE_OPEN_ABILITY = "SyncCharacterAbilityCityAbilityOpenAbilityPage"
_NODE_NEXT = "SyncCharacterAbilityCityAbilityNextCharacter"
_NODE_OCR_NAME = "SyncCharacterAbilityCityAbilityOCRName"
_NODE_OCR_SKILL0 = "SyncCharacterAbilityCityAbilityOCRSkill0"
_NODE_OCR_SKILL1 = "SyncCharacterAbilityCityAbilityOCRSkill1"
_NODE_MATCH_CHAR = "SyncCharacterAbilityCityAbilityMatchCharacter"
_CLICK_NODES = [
    "SyncCharacterAbilityCityAbilityCharacter2Move",
    "SyncCharacterAbilityCityAbilityCharacter3Move",
    "SyncCharacterAbilityCityAbilityCharacter4Move",
    "SyncCharacterAbilityCityAbilityCharacter5Move",
]

# OCR pipeline override — only_rec=True 让 OCR 返回 ROI 内所有文本，无需 expected
_OCR_OVERRIDE = {"recognition": {"param": {"only_rec": True}}}

_MAX_NO_CHANGE = 3  # NextCharacter 连续无变化，认为已到列表末尾
_MAX_ITERATIONS = 300  # 安全上限
_TM_THRESHOLD = 0.9  # TemplateMatch 置信度阈值，≥此值直接采用

# 初始页面导航的 pipeline override — 1s 超时，失败时终止同步
_RECOVERY_OVERRIDE = {"timeout": 1000}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _check_cancelled(context: Context) -> None:
    if context.tasker.stopping:
        raise TaskCancelledError("SyncCityAbility")


def _screencap(context: Context):
    """截取当前屏幕，返回 numpy 图像数组。"""
    _check_cancelled(context)
    job = context.tasker.controller.post_screencap().wait()
    _check_cancelled(context)
    if not job.succeeded:
        raise TaskFailedError("SyncCityAbility screenshot failed")
    image = job.get()
    _check_cancelled(context)
    if image is None:
        raise TaskFailedError("SyncCityAbility screenshot is empty")
    return image


def _ocr_on_image(context: Context, node_name: str, image) -> str | None:
    """对给定截图运行 OCR，未命中返回 None；取消时抛出异常。"""
    _check_cancelled(context)
    result = context.run_recognition(
        node_name,
        image,
        pipeline_override={node_name: _OCR_OVERRIDE},
    )
    _check_cancelled(context)
    if result is None or not result.hit:
        return None
    best = result.best_result
    if best is None:
        return None
    text: str = getattr(best, "text", "")
    return text.strip() or None


def _parse_skill_level(text: str | None, max_level: int) -> int:
    """解析技能 OCR 文本（如 ``"0/5"``）→ 当前等级整数。

    无法解析 / 技能不存在时返回 -1（即 N/A）。
    """
    if text is None:
        return -1
    # "当前/最大" 格式，如 "3/5"
    m = re.search(r"(\d+)\s*/\s*\d+", text)
    if m:
        level = int(m.group(1))
        if 0 <= level <= max_level:
            return level
    # 纯数字兜底
    m = re.search(r"(\d+)", text)
    if m:
        level = int(m.group(1))
        if 0 <= level <= max_level:
            return level
    return -1


def _ocr_skills(context: Context) -> list[int]:
    """OCR 技能等级，返回 ``[skill0, skill1]``。调用方负责确保已在技能页面。"""
    img = _screencap(context)
    skill0 = _parse_skill_level(
        _ocr_on_image(context, _NODE_OCR_SKILL0, img),
        max_level=5,
    )
    skill1 = _parse_skill_level(
        _ocr_on_image(context, _NODE_OCR_SKILL1, img),
        max_level=2,
    )
    return [skill0, skill1]


# ---------------------------------------------------------------------------
# 角色名识别 — TemplateMatch 优先，OCR 兜底
# ---------------------------------------------------------------------------

# 模板文件名 → 角色名 映射。key 为文件名（含扩展名）；未命中时取 stem。
_TEMPLATE_TO_NAME: dict[str, str] = {
    "Adler.png": "阿德勒",
    "Aurelia.png": "海月",
    "Baicang.png": "白藏",  # 没抽到，没放图
    "Chaos.png": "卡厄斯",  # 没开池子，放不了图
    "Chiz.png": "小吱",
    "Daffodill.png": "达芙蒂尔",
    "Edgar.png": "埃德嘉",
    "Fadia.png": "法帝娅",  # 没抽到，没放图
    "Haniel.png": "哈尼娅",
    "Hathor.png": "哈索尔",
    "Hotori.png": "浔",
    "Jiuyuan.png": "九原",
    "Lacrimosa.png": "安魂曲",
    "Mint.png": "薄荷",
    "Nanally.png": "娜娜莉",
    "Sakiri.png": "早雾",
    "Skia.png": "翳",
    "Zero.png": "零",
}


# TemplateMatch 硬编码回退配置 — 仅在 get_node_data 读取 JSON 节点失败时使用。
# 正常运行时 JSON 节点存在，此配置不生效。应与 SyncCharacterAbilityCityAbilityMatchCharacter 保持同步。
_TM_CONFIG = {
    "templates": [
        "Character_UI/Character_Pic/Adler.png",
        "Character_UI/Character_Pic/Aurelia.png",
        "Character_UI/Character_Pic/Chiz.png",
        "Character_UI/Character_Pic/Daffodill.png",
        "Character_UI/Character_Pic/Edgar.png",
        "Character_UI/Character_Pic/Haniel.png",
        "Character_UI/Character_Pic/Hathor.png",
        "Character_UI/Character_Pic/Hotori.png",
        "Character_UI/Character_Pic/Jiuyuan.png",
        "Character_UI/Character_Pic/Lacrimosa.png",
        "Character_UI/Character_Pic/Mint.png",
        "Character_UI/Character_Pic/Nanally.png",
        "Character_UI/Character_Pic/Sakiri.png",
        "Character_UI/Character_Pic/Skia.png",
        "Character_UI/Character_Pic/Zero.png",
    ],
    "roi": (390, 80, 200, 210),
    "threshold": 0.8,
    "green_mask": True,
}


def _tm_get_name(context: Context) -> str | None:
    """逐模板运行 TemplateMatch，置信度 ≥ _TM_THRESHOLD 时返回角色名。

    优先从 pipeline JSON 动态读取配置，失败时回退到 ``_TM_CONFIG``。
    """
    img = _screencap(context)

    node_data = context.get_node_data(_NODE_MATCH_CHAR)
    if node_data:
        param = node_data.get("recognition", {}).get("param", {})
        templates = param.get("template", [])
        roi = param.get("roi", (0, 0, 0, 0))
        threshold = param.get("threshold", 0.8)
        green_mask = param.get("green_mask", False)
    else:
        logger.debug("SyncCityAbility: get_node_data 失败，使用硬编码 TM 配置")
        templates = _TM_CONFIG["templates"]
        roi = _TM_CONFIG["roi"]
        threshold = _TM_CONFIG["threshold"]
        green_mask = _TM_CONFIG["green_mask"]

    if not templates:
        return None

    for template_path in templates:
        _check_cancelled(context)
        result = context.run_recognition_direct(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(
                template=[template_path],
                roi=tuple(roi) if roi else (0, 0, 0, 0),
                threshold=[threshold] if not isinstance(threshold, list) else threshold,
                green_mask=green_mask,
            ),
            img,
        )
        _check_cancelled(context)
        if result is None or not result.hit:
            continue
        best = result.best_result
        if best is None:
            continue
        score: float = getattr(best, "score", 0)
        if score < _TM_THRESHOLD:
            continue

        filename = Path(template_path).name
        stem = Path(template_path).stem
        name = _TEMPLATE_TO_NAME.get(filename, stem)
        logger.info("SyncCityAbility: TemplateMatch → %s (%.2f)", name, score)
        return name

    logger.debug("SyncCityAbility: TemplateMatch 无高置信度匹配")
    return None


def _ocr_name_with_retry(context: Context) -> str | None:
    """OCR 识别角色名字（重试），不含页面导航。"""
    for attempt in range(1, 4):
        img = _screencap(context)
        name = _ocr_on_image(context, _NODE_OCR_NAME, img)
        if name is not None:
            return name
        logger.debug("SyncCityAbility: OCR 名字第 %d 次失败", attempt)
        if not interruptible_wait(context, 0.2):
            raise TaskCancelledError(_NODE_OCR_NAME)

    logger.info("SyncCityAbility: OCR 名字 3 次失败")
    return None


def _get_character_name(context: Context) -> str | None:
    """获取当前角色名：OCR 先识别 → 技能页 TemplateMatch 优先采用。

    调用前需在信息页面；调用后停留在技能页面。
    TM 置信度 ≥ 0.9 时直接返回 TM 结果（覆盖 OCR）。
    """

    # 1. OCR 名字（信息页）— 先尝试
    name = _ocr_name_with_retry(context)
    if name is not None:
        logger.info("SyncCityAbility: OCR → %s", name)

    # 2. 进入技能页面
    require_task(context, _NODE_OPEN_ABILITY)

    # 3. TemplateMatch — 置信度 ≥ 0.9 则优先采用，否则回退 OCR
    tm_name = _tm_get_name(context)
    if tm_name is not None:
        return tm_name

    return name


# ---------------------------------------------------------------------------
# Custom Action
# ---------------------------------------------------------------------------


@AgentServer.custom_action("SyncCharacterAbilityCityAbilityMainAction")
class SyncCharacterAbilityCityAbilityMainAction(CustomAction):
    """角色都市技能同步主流程。

    遍历角色列表，OCR 识别每个角色的名字和两个都市技能等级，
    通过 ``CharacterAbility_CityAbility`` 管理器持久化数据。

    custom_action_param:
        {"fresh_record": true}  — 全新记录，扫描完一次性清空+存入
        不传或 {}               — 逐角色保存
    """

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> CustomAction.RunResult:
        # ---- 0. 解析参数 ----
        fresh_record = False
        if argv.custom_action_param:
            try:
                params = json.loads(argv.custom_action_param)
                if isinstance(params, dict):
                    fresh_record = bool(params.get("fresh_record", False))
            except (json.JSONDecodeError, TypeError):
                pass

        logger.info(
            "SyncCityAbility: 开始遍历  fresh_record=%s",
            fresh_record,
        )

        try:
            _check_cancelled(context)
            # 防止内部 NextCharacter→Character1Click 重新触发 Main。
            context.set_anchor("CityAbilityAfterClick", "")
            require_task(
                context,
                _NODE_OPEN_INFO,
                pipeline_override={_NODE_OPEN_INFO: _RECOVERY_OVERRIDE},
            )

            last_name: str | None = None
            no_change = 0
            all_recognized = True
            results: dict[str, list[int]] = {}

            for iteration in range(_MAX_ITERATIONS):
                _check_cancelled(context)

                # 获取名字后停留在技能页；导航失败抛出异常，识别 miss 返回 None。
                name = _get_character_name(context)
                _check_cancelled(context)
                if name is None:
                    logger.debug("SyncCityAbility[%d]: 识别失败，跳过", iteration)
                    all_recognized = False
                    no_change += 1
                else:
                    levels = _ocr_skills(context)
                    _check_cancelled(context)
                    logger.info(
                        "SyncCityAbility[%d]: %s 技能0=%d 技能1=%d",
                        iteration,
                        name,
                        levels[0],
                        levels[1],
                    )
                    if levels[0] < 0:
                        logger.debug("SyncCityAbility: %s 技能0未识别，跳过记录", name)
                        all_recognized = False
                    elif fresh_record:
                        results[name] = levels
                    else:
                        set_character_abilities(name, levels)

                    if name == last_name:
                        no_change += 1
                    else:
                        no_change = 0
                        last_name = name

                # 连续未前进时扫描剩余可见位置；只有完整扫描才能覆盖旧记录。
                if no_change >= _MAX_NO_CHANGE:
                    logger.info(
                        "SyncCityAbility: %d 次未识别到新角色，扫描列表剩余角色",
                        no_change,
                    )
                    require_task(context, _NODE_OPEN_INFO)
                    tail_complete = self._scan_remaining_characters(
                        context, results if fresh_record else None
                    )
                    _check_cancelled(context)
                    if not all_recognized or not tail_complete:
                        logger.error("SyncCityAbility: 存在未识别角色，同步未完成")
                        return CustomAction.RunResult(success=False)
                    break

                require_task(context, _NODE_OPEN_INFO)
                require_task(context, _NODE_NEXT)
            else:
                logger.error(
                    "SyncCityAbility: 达到 %d 轮上限，同步未完成", _MAX_ITERATIONS
                )
                return CustomAction.RunResult(success=False)

            _check_cancelled(context)
            if fresh_record and results:
                clear_all()
                for char_name, levels in results.items():
                    set_character_abilities(char_name, levels)
                logger.info("SyncCityAbility: 批量存入 %d 个角色", len(results))

            _check_cancelled(context)
            logger.info("SyncCityAbility: 遍历完成，共 %d 轮", iteration + 1)
            return CustomAction.RunResult(success=True)
        except TaskCancelledError:
            logger.debug("SyncCityAbility: 收到停止信号，同步未完成")
        except TaskFailedError as exc:
            logger.error("SyncCityAbility: 必需子任务失败: %s", exc)
        return CustomAction.RunResult(success=False)

    # ------------------------------------------------------------------
    # 列表剩余可见角色
    # ------------------------------------------------------------------

    @staticmethod
    def _scan_remaining_characters(
        context: Context,
        results: dict[str, list[int]] | None = None,
    ) -> bool:
        """扫描末尾角色；识别 miss 标记未完成，导航失败或取消抛出异常。"""
        all_recognized = True
        for click_node in _CLICK_NODES:
            _check_cancelled(context)
            logger.info("SyncCityAbility tail: %s", click_node)
            require_task(context, click_node)
            if not interruptible_wait(context, 0.3):
                raise TaskCancelledError(click_node)

            # 点击后已在信息页，走标准角色流程。
            name = _get_character_name(context)
            _check_cancelled(context)
            if name is None:
                logger.debug("SyncCityAbility tail: %s 识别失败，跳过", click_node)
                all_recognized = False
            else:
                levels = _ocr_skills(context)
                _check_cancelled(context)
                logger.info(
                    "SyncCityAbility tail: %s 技能0=%d 技能1=%d",
                    name,
                    levels[0],
                    levels[1],
                )
                if levels[0] < 0:
                    logger.debug("SyncCityAbility tail: %s 技能0未识别，跳过记录", name)
                    all_recognized = False
                elif results is not None:
                    results[name] = levels
                else:
                    set_character_abilities(name, levels)

            require_task(context, _NODE_OPEN_INFO)

        _check_cancelled(context)
        return all_recognized
