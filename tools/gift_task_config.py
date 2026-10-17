"""从两份离线资料库生成赠礼 UI、独立 attach 配置及五语文案。

本工具不访问网络，不导入 MaaFramework；--check 只读，--write 仅更新赠礼
生成文件、赠礼翻译字段和任务 import。已有的其他任务、翻译及 JSONC 注释保留。
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LANGUAGES = ("zh_cn", "zh_tw", "en_us", "ja_jp", "ko_kr")
TASK_PATH = Path("assets/resource/tasks/Gift.json")
CONFIG_PATH = Path("assets/resource/base/pipeline/Gift/GiftConfig.json")
TASK_IMPORT = "resource/tasks/Gift.json"
CHARACTERS_PATH = Path("assets/resource/base/data/characters.json")
GIFTS_PATH = Path("assets/resource/base/data/gifts.json")

_INTERFACE_MESSAGES = {
    "zh_cn": {
        "task_gift_label": "羁遇赠礼",
        "task_gift_desc": (
            "逐条添加赠礼方案，为角色选择礼物和当日目标。按首选 → 备用 → 更多备选赠送，库存耗尽后才切换。"
            "不购买礼物；重复运行会按游戏已有次数补足当日目标。"
        ),
        "task_gift_recipients": "赠礼角色（可多选）",
        "task_gift_recipients_desc": "默认不选择任何角色。勾选角色后分别设置礼物顺序与当日累计上限；主角不可选。",
        "task_gift_priority": "礼物优先级与完整清单",
        "task_gift_priority_desc": (
            "用 `>` 分隔任意数量的礼物完整名称或 ID，例如 `溶月茶具 > 时令寿司船`；分隔符两侧可留空格。"
            "默认留空，使用完整优先序模式时必须填写。严格按此顺序使用已有库存，耗尽才换下一候选，不购买或自动替换。"
            "可复制下方完整原名、资料库中已核实的名称或 ID；不要使用猜测的外语译名。"
            "未知喜好不代表不喜欢。\n\n"
            "来源：[本地完整礼物资料库](resource/base/data/gifts.json)。每条记录的 `source` 保存 Wiki 来源与页面 ID；"
            "下列清单由该库生成，显示 ID 与中文原名，有已核实的本地化名称时同时显示。"
        ),
        "task_gift_limit": "角色当日累计上限",
        "task_gift_limit_desc": (
            "选择 1–3，默认 3。这是当日累计目标，不是本次追加件数。重复运行会读取游戏已有次数，"
            "只补足目标；已达标则不赠礼。全体角色每日合计最多 10 件，以游戏提示为准。"
        ),
        "task_gift_priority_input": "礼物完整名称或 ID（用 > 分隔）",
        "task_gift_limit_input": "当日累计件数",
        "task_gift_limit_invalid": "请输入 1、2 或 3。",
        "task_gift_dry_run": "演练模式（不提交赠礼）",
        "task_gift_dry_run_desc": "仅检查角色、候选礼物、库存和游戏次数并展示计划，不点击最终赠礼确认，不消耗礼物或次数。",
        "task_gift_plan_count": "赠礼方案数量",
        "task_gift_plan_count_desc": "选择要配置的方案数量，默认 1。按方案顺序赠礼；每个方案分别选择角色、礼物和当日累计目标。",
        "task_gift_recipient_row_desc": "可搜索角色。不同方案请选择不同角色；重复选择同一角色时共用礼物与上限，只执行一次。",
        "task_gift_recipient_none": "请选择角色",
        "task_gift_priority_mode": "礼物设置方式",
        "task_gift_priority_mode_desc": "列表模式选择首选与备用礼物；完整优先序模式保留已保存的文字配置。",
        "task_gift_priority_mode_list": "从礼物列表选择",
        "task_gift_priority_mode_manual": "输入完整优先序",
        "task_gift_primary": "首选礼物",
        "task_gift_primary_desc": "可搜索礼物名称。先赠送首选礼物，库存耗尽后才使用备用；默认不选择礼物。",
        "task_gift_primary_none": "请选择首选礼物",
        "task_gift_backup": "备用礼物",
        "task_gift_backup_desc": "首选耗尽后的第二候选，可留空。只使用已选择的礼物，不会自动替换。",
        "task_gift_backup_none": "不使用备用礼物",
        "task_gift_extra": "更多备选礼物（可留空）",
        "task_gift_extra_desc": "第 3 项起的备选，按 > 分隔完整名称或 ID，数量不限。首选、备用耗尽后严格按此顺序使用。",
        "task_gift_extra_input": "完整名称或 ID（用 > 分隔）",
    },
    "zh_tw": {
        "task_gift_label": "羈遇贈禮",
        "task_gift_desc": (
            "逐條新增贈禮方案，為角色選擇禮物和當日目標。依首選 → 備用 → 更多備選贈送，庫存耗盡後才切換。"
            "不購買禮物；重複執行會依遊戲已有次數補足當日目標。"
        ),
        "task_gift_recipients": "贈禮角色（可複選）",
        "task_gift_recipients_desc": "預設不選擇任何角色。勾選角色後分別設定禮物順序與當日累計上限；主角不可選。",
        "task_gift_priority": "禮物優先順序與完整清單",
        "task_gift_priority_desc": (
            "用 `>` 分隔任意數量的禮物完整名稱或 ID，例如 `溶月茶具 > 时令寿司船`；分隔符兩側可留空格。"
            "預設留空，使用完整優先順序模式時必須填寫。嚴格依此順序使用已有庫存，耗盡才換下一候選，不購買或自動替換。"
            "可複製下方完整原名、資料庫中已核實的名稱或 ID；不要使用猜測的外語譯名。"
            "未知喜好不代表不喜歡。\n\n"
            "來源：[本機完整禮物資料庫](resource/base/data/gifts.json)。每筆記錄的 `source` 保存 Wiki 來源與頁面 ID；"
            "下列清單由該庫產生，顯示 ID 與中文原名，有已核實的本地化名稱時同時顯示。"
        ),
        "task_gift_limit": "角色當日累計上限",
        "task_gift_limit_desc": (
            "選擇 1–3，預設 3。這是當日累計目標，不是本次追加件數。重複執行會讀取遊戲已有次數，"
            "只補足目標；已達標則不贈禮。全體角色每日合計最多 10 件，以遊戲提示為準。"
        ),
        "task_gift_priority_input": "禮物完整名稱或 ID（用 > 分隔）",
        "task_gift_limit_input": "當日累計件數",
        "task_gift_limit_invalid": "請輸入 1、2 或 3。",
        "task_gift_dry_run": "演練模式（不提交贈禮）",
        "task_gift_dry_run_desc": "僅檢查角色、候選禮物、庫存和遊戲次數並展示計畫，不點擊最終贈禮確認，不消耗禮物或次數。",
        "task_gift_plan_count": "贈禮方案數量",
        "task_gift_plan_count_desc": "選擇要設定的方案數量，預設 1。依方案順序贈禮；每個方案分別選擇角色、禮物和當日累計目標。",
        "task_gift_recipient_row_desc": "可搜尋角色。不同方案請選擇不同角色；重複選擇同一角色時共用禮物與上限，只執行一次。",
        "task_gift_recipient_none": "請選擇角色",
        "task_gift_priority_mode": "禮物設定方式",
        "task_gift_priority_mode_desc": "清單模式選擇首選與備用禮物；完整優先順序模式保留已儲存的文字設定。",
        "task_gift_priority_mode_list": "從禮物清單選擇",
        "task_gift_priority_mode_manual": "輸入完整優先順序",
        "task_gift_primary": "首選禮物",
        "task_gift_primary_desc": "可搜尋禮物名稱。先贈送首選禮物，庫存耗盡後才使用備用；預設不選擇禮物。",
        "task_gift_primary_none": "請選擇首選禮物",
        "task_gift_backup": "備用禮物",
        "task_gift_backup_desc": "首選耗盡後的第二候選，可留空。只使用已選擇的禮物，不會自動替換。",
        "task_gift_backup_none": "不使用備用禮物",
        "task_gift_extra": "更多備選禮物（可留空）",
        "task_gift_extra_desc": "第 3 項起的備選，以 > 分隔完整名稱或 ID，數量不限。首選、備用耗盡後嚴格依此順序使用。",
        "task_gift_extra_input": "完整名稱或 ID（以 > 分隔）",
    },
    "en_us": {
        "task_gift_label": "Bond gifts",
        "task_gift_desc": (
            "Add plans and choose each character's gifts and daily target. Use primary → backup → extra gifts, "
            "switching only when stock is exhausted. Never buys gifts; repeated runs use the game's existing count "
            "to fill the remaining daily target."
        ),
        "task_gift_recipients": "Gift recipients (multiple selections)",
        "task_gift_recipients_desc": (
            "No recipients are selected by default. Select characters to configure their gift order and cumulative daily "
            "limit separately. The protagonist cannot be selected."
        ),
        "task_gift_priority": "Gift priority and complete list",
        "task_gift_priority_desc": (
            "Separate any number of full gift names or IDs with `>`, for example `溶月茶具 > 时令寿司船`; surrounding "
            "spaces are optional. Blank by default; required for each selected character. Existing stock is used in exactly "
            "this order, moving on only when exhausted, with no purchases or automatic substitutions. Copy a full original "
            "name, a verified name from the catalog, or an ID below; do not guess localized item names. "
            "Unknown preferences do not mean dislike.\n\n"
            "Source: [complete local gift catalog](resource/base/data/gifts.json). Each record's `source` contains its Wiki "
            "source and page ID. The list below is generated from that catalog and shows every ID and original Chinese "
            "name, together with a localized name only when verified."
        ),
        "task_gift_limit": "Character's cumulative daily limit",
        "task_gift_limit_desc": (
            "Choose 1–3 (default 3), the cumulative target for today rather than extra gifts this run. Repeated runs "
            "read the game's existing count and only fill the shortfall. At most 10 total per day, subject to the game."
        ),
        "task_gift_priority_input": "Full gift names or IDs (separate with >)",
        "task_gift_limit_input": "Cumulative gifts today",
        "task_gift_limit_invalid": "Enter 1, 2, or 3.",
        "task_gift_dry_run": "Dry run (do not submit gifts)",
        "task_gift_dry_run_desc": (
            "Only checks characters, candidate gifts, inventory and in-game counts, then shows the plan. Never presses "
            "the final gift confirmation or consumes gifts or attempts."
        ),
        "task_gift_plan_count": "Number of gift plans",
        "task_gift_plan_count_desc": (
            "Choose how many plans to configure (default 1). Plans run in row order; select each character, gifts "
            "and cumulative daily target separately."
        ),
        "task_gift_recipient_row_desc": (
            "Search for a character. Choose different characters for different plans. Repeated characters share "
            "their gifts and limit and run only once."
        ),
        "task_gift_recipient_none": "Choose a character",
        "task_gift_priority_mode": "Gift configuration",
        "task_gift_priority_mode_desc": (
            "Choose primary and backup gifts from the list, or enter a complete priority list. "
            "Saved text values remain available in manual mode."
        ),
        "task_gift_priority_mode_list": "Choose from the gift list",
        "task_gift_priority_mode_manual": "Enter a complete priority list",
        "task_gift_primary": "Primary gift",
        "task_gift_primary_desc": (
            "Search by gift name. Use the primary gift until stock is exhausted before trying backups. "
            "No gift is selected by default."
        ),
        "task_gift_primary_none": "Choose a primary gift",
        "task_gift_backup": "Backup gift",
        "task_gift_backup_desc": (
            "Optional second choice after the primary is exhausted. Only selected gifts are used; "
            "there are no automatic substitutions."
        ),
        "task_gift_backup_none": "No backup gift",
        "task_gift_extra": "More backup gifts (optional)",
        "task_gift_extra_desc": (
            "Third and later choices: separate full names or IDs with >, with no length limit. "
            "Used in this order after the primary and backup are exhausted."
        ),
        "task_gift_extra_input": "Full names or IDs (separate with >)",
    },
    "ja_jp": {
        "task_gift_label": "絆ギフト",
        "task_gift_desc": (
            "プランを追加し、各キャラクターのギフトと当日の目標を選びます。第1候補 → 第2候補 → その他の候補の順に、"
            "在庫を使い切ってから切り替えます。購入はせず、再実行時はゲーム内の贈呈済み回数に応じて当日の不足分だけ贈ります。"
        ),
        "task_gift_recipients": "贈るキャラクター（複数選択可）",
        "task_gift_recipients_desc": "初期状態では誰も選択されません。選択したキャラクターごとにギフトの順序と当日の累計上限を設定します。主人公は選択できません。",
        "task_gift_priority": "ギフトの優先順と全一覧",
        "task_gift_priority_desc": (
            "ギフトの正式な全名称またはIDを `>` で区切って任意の数だけ入力します。例：`溶月茶具 > 时令寿司船`。"
            "区切りの前後に空白を入れても構いません。初期値は空欄で、選択したキャラクターには入力が必要です。"
            "所持品をこの順序で使用し、使い切った場合のみ次へ進みます。購入や自動代替は行いません。"
            "下記の原名、資料庫で確認済みの名称、またはIDをコピーし、翻訳名を推測しないでください。"
            "好みが不明でも嫌いとは限りません。\n\n"
            "出典：[ローカルの全ギフト資料庫](resource/base/data/gifts.json)。各記録の `source` にWikiの出典とページIDがあります。"
            "以下は資料庫から生成した全IDと中国語原名の一覧です。確認済みの翻訳名がある場合のみ併記します。"
        ),
        "task_gift_limit": "キャラクターの当日累計上限",
        "task_gift_limit_desc": (
            "1～3を選びます（初期値3）。当日の累計目標であり、今回追加する個数ではありません。再実行時はゲーム内の贈呈済み回数を読み、"
            "不足分だけ贈ります。全キャラクターで1日合計最大10個ですが、ゲーム内表示を優先します。"
        ),
        "task_gift_priority_input": "ギフトの全名称またはID（> で区切る）",
        "task_gift_limit_input": "当日の累計個数",
        "task_gift_limit_invalid": "1、2、3のいずれかを入力してください。",
        "task_gift_dry_run": "ドライラン（ギフトを確定しない）",
        "task_gift_dry_run_desc": "キャラクター、候補ギフト、所持数、ゲーム内回数を確認して計画を表示するだけです。最後の贈呈確定は押さず、ギフトや回数を消費しません。",
        "task_gift_plan_count": "ギフトプラン数",
        "task_gift_plan_count_desc": "設定するプラン数を選びます（初期値1）。行の順番に贈ります。各プランでキャラクター、ギフト、当日の累計目標を設定してください。",
        "task_gift_recipient_row_desc": "キャラクターを検索できます。各プランでは別のキャラクターを選んでください。同じキャラクターを選ぶとギフトと上限を共有し、1回だけ実行します。",
        "task_gift_recipient_none": "キャラクターを選択してください",
        "task_gift_priority_mode": "ギフトの設定方法",
        "task_gift_priority_mode_desc": "一覧から第1・第2候補を選ぶか、優先順をすべて入力します。保存済みの入力内容は手動入力モードで利用できます。",
        "task_gift_priority_mode_list": "ギフト一覧から選ぶ",
        "task_gift_priority_mode_manual": "優先順をすべて入力する",
        "task_gift_primary": "第1候補のギフト",
        "task_gift_primary_desc": "ギフト名で検索できます。第1候補の在庫を使い切ってから予備を使います。初期状態では未選択です。",
        "task_gift_primary_none": "第1候補を選択してください",
        "task_gift_backup": "第2候補のギフト",
        "task_gift_backup_desc": "第1候補を使い切った後の候補です。空欄でも構いません。選択したギフトのみを使い、自動で代替しません。",
        "task_gift_backup_none": "第2候補を使わない",
        "task_gift_extra": "その他の予備ギフト（任意）",
        "task_gift_extra_desc": "第3候補以降の全名称またはIDを > で区切って入力します。個数制限はありません。第1・第2候補を使い切ってから入力順に使います。",
        "task_gift_extra_input": "全名称またはID（> で区切る）",
    },
    "ko_kr": {
        "task_gift_label": "인연 선물",
        "task_gift_desc": (
            "계획을 추가하고 각 캐릭터의 선물과 당일 목표를 선택하세요. 첫 번째 → 두 번째 → 추가 후보 순서로 "
            "재고를 모두 소진한 뒤에만 전환합니다. 선물을 구매하지 않으며, 반복 실행 시 게임의 기존 횟수에 따라 당일 부족분만 채웁니다."
        ),
        "task_gift_recipients": "선물할 캐릭터 (여러 명 선택 가능)",
        "task_gift_recipients_desc": "기본으로 선택된 캐릭터는 없습니다. 선택한 캐릭터마다 선물 순서와 당일 누적 한도를 따로 설정하세요. 주인공은 선택할 수 없습니다.",
        "task_gift_priority": "선물 우선순위 및 전체 목록",
        "task_gift_priority_desc": (
            "선물의 전체 이름이나 ID를 `>`로 구분하여 개수 제한 없이 입력하세요. 예: `溶月茶具 > 时令寿司船`. "
            "구분자 앞뒤의 공백은 선택 사항입니다. 기본값은 빈칸이며 선택한 캐릭터는 반드시 입력해야 합니다. "
            "입력한 순서대로 보유 재고를 사용하고 소진한 뒤에만 다음 후보로 넘어가며, 구매하거나 자동 대체하지 않습니다. "
            "아래의 원명, 자료에서 확인된 이름 또는 ID를 복사하세요. 번역된 이름을 추측해서 입력하지 마세요. "
            "취향 미상은 싫어한다는 뜻이 아닙니다.\n\n"
            "출처: [로컬 전체 선물 자료](resource/base/data/gifts.json). 각 항목의 `source`에 Wiki 출처와 페이지 ID가 있습니다. "
            "아래 목록은 해당 자료에서 생성한 모든 ID와 중국어 원명이며, 확인된 번역명이 있을 때만 함께 표시합니다."
        ),
        "task_gift_limit": "캐릭터 당일 누적 한도",
        "task_gift_limit_desc": (
            "1~3을 선택하세요(기본값 3). 당일 누적 목표이며 이번에 추가할 수량이 아닙니다. 반복 실행 시 게임의 기존 횟수를 읽고 "
            "부족한 수량만 채웁니다. 모든 캐릭터를 합쳐 하루 최대 10개이며 게임 안내가 우선합니다."
        ),
        "task_gift_priority_input": "선물 전체 이름 또는 ID (>로 구분)",
        "task_gift_limit_input": "당일 누적 개수",
        "task_gift_limit_invalid": "1, 2, 3 중 하나를 입력하세요.",
        "task_gift_dry_run": "모의 실행 (선물 확정 안 함)",
        "task_gift_dry_run_desc": "캐릭터, 후보 선물, 재고, 게임 내 횟수만 확인하고 계획을 표시합니다. 최종 선물 확인을 누르지 않으며 선물이나 횟수를 소모하지 않습니다.",
        "task_gift_plan_count": "선물 계획 수",
        "task_gift_plan_count_desc": "설정할 계획 수를 선택하세요(기본값 1). 행 순서대로 선물하며, 각 계획의 캐릭터, 선물, 당일 누적 목표를 따로 설정합니다.",
        "task_gift_recipient_row_desc": "캐릭터를 검색할 수 있습니다. 각 계획에서 서로 다른 캐릭터를 선택하세요. 같은 캐릭터를 반복 선택하면 선물과 한도를 공유하며 한 번만 실행합니다.",
        "task_gift_recipient_none": "캐릭터를 선택하세요",
        "task_gift_priority_mode": "선물 설정 방식",
        "task_gift_priority_mode_desc": "목록에서 첫 번째와 두 번째 후보를 선택하거나 전체 우선순위를 입력하세요. 저장된 텍스트는 직접 입력 모드에서 유지됩니다.",
        "task_gift_priority_mode_list": "선물 목록에서 선택",
        "task_gift_priority_mode_manual": "전체 우선순위 입력",
        "task_gift_primary": "첫 번째 선물",
        "task_gift_primary_desc": "선물 이름으로 검색하세요. 첫 번째 선물의 재고를 모두 사용한 뒤에만 다음 후보를 사용합니다. 기본으로 선택된 선물은 없습니다.",
        "task_gift_primary_none": "첫 번째 선물을 선택하세요",
        "task_gift_backup": "두 번째 선물",
        "task_gift_backup_desc": "첫 번째 선물 소진 후 사용할 후보이며 비워 둘 수 있습니다. 선택한 선물만 사용하며 자동 대체하지 않습니다.",
        "task_gift_backup_none": "두 번째 선물 사용 안 함",
        "task_gift_extra": "추가 후보 선물 (선택 사항)",
        "task_gift_extra_desc": "세 번째 이후 후보의 전체 이름이나 ID를 >로 구분하세요. 개수 제한 없이 입력할 수 있으며 앞선 후보가 소진된 뒤 입력 순서대로 사용합니다.",
        "task_gift_extra_input": "전체 이름 또는 ID (>로 구분)",
    },
}

_PLAN_RECIPIENT_LABELS = {
    "zh_cn": "方案 %d · 角色",
    "zh_tw": "方案 %d · 角色",
    "en_us": "Plan %d · Character",
    "ja_jp": "プラン %d · キャラクター",
    "ko_kr": "계획 %d · 캐릭터",
}

_CATALOG_TITLES = {
    "zh_cn": "### 完整离线礼物清单（%d 项）",
    "zh_tw": "### 完整離線禮物清單（%d 項）",
    "en_us": "### Complete offline gift list (%d items)",
    "ja_jp": "### オフライン全ギフト一覧（%d件）",
    "ko_kr": "### 오프라인 전체 선물 목록 (%d개)",
}

_AGENT_MESSAGES = {
    "zh_cn": {
        "gift.started": "羁遇赠礼开始：%d 位角色，模式：%s",
        "gift.mode.dry_run": "演练（不提交）",
        "gift.mode.execute": "正式赠礼",
        "gift.character": "角色：%s，当日累计目标：%d 件",
        "gift.candidate": "当前候选礼物：%s",
        "gift.exhausted": "礼物已耗尽：%s",
        "gift.sent": "已确认赠礼：%s ← %s；本角色本次确认成功 %d 件，游戏剩余 %d 次",
        "gift.already_done": "%s 已达到当日目标，无需赠礼",
        "gift.dry_run": "演练计划：向 %s 赠送 %s（不会提交）",
        "gift.warning": "角色 %s：%s",
        "gift.summary": "赠礼汇总：已确认 %d 件，无需赠礼 %d 位，演练 %d 位，无法完成 %d 位，结果不明 %d 位",
        "gift.reason.invalid_config": "赠礼配置无效，请检查角色、礼物顺序和每日累计上限。",
        "gift.reason.no_recipients": "未选择可赠礼角色，未执行任何赠礼。",
        "gift.reason.unknown_character": "本地资料库不包含该角色，或该角色不可赠礼。",
        "gift.reason.character_not_found": "未在羁遇界面找到该角色，未向其他角色赠礼。",
        "gift.reason.identity": "无法唯一确认当前角色，已停止赠礼。",
        "gift.reason.gift_not_found": "未找到配置的礼物，请核对礼物完整名称、ID 和背包。",
        "gift.reason.gift_identity": "无法唯一确认礼物身份，未提交赠礼。",
        "gift.reason.inventory": "无法确认礼物库存，未提交赠礼。",
        "gift.reason.quota": "无法确认角色当日剩余次数，未提交赠礼。",
        "gift.reason.quantity": "无法确认赠送数量为 1，未提交赠礼。",
        "gift.reason.infinite": "检测到不限量或无法计数的物品，未提交赠礼。",
        "gift.reason.global_limit": "游戏提示全体角色当日赠礼上限已到，停止本次任务。",
        "gift.reason.budget": "已达到安全操作预算，停止本次任务。",
        "gift.reason.page": "角色或礼物页面状态异常，未继续操作。",
        "gift.reason.navigation": "无法进入已确认的羁遇赠礼页面，未提交赠礼。",
        "gift.reason.recovery": "无法安全返回已知页面，停止本次任务。",
        "gift.reason.ambiguous": "识别结果不唯一，未执行不确定的点击。",
        "gift.reason.scan": "无法安全完成页面扫描，未继续赠礼。",
        "gift.reason.receipt": "提交后的结果证据不一致，结果不明，不会重复提交。",
        "gift.reason.receipt_timeout": "未及时确认赠礼结果，结果不明，不会重复提交。",
        "gift.reason.controller": "控制器截图或输入失败，已停止操作。",
        "gift.reason.stopping": "已收到停止请求，不再提交赠礼。",
        "gift.reason.dry_run": "演练模式不会提交赠礼。",
        "gift.reason.exhausted": "所有配置候选礼物均已耗尽，无法完成本角色目标。",
        "gift.reason.catalog": "本地角色或礼物资料库缺失或无效，未提交赠礼。",
        "gift.reason.internal": "发生内部异常，已停止以避免重复赠礼。",
    },
    "zh_tw": {
        "gift.started": "羈遇贈禮開始：%d 位角色，模式：%s",
        "gift.mode.dry_run": "演練（不提交）",
        "gift.mode.execute": "正式贈禮",
        "gift.character": "角色：%s，當日累計目標：%d 件",
        "gift.candidate": "目前候選禮物：%s",
        "gift.exhausted": "禮物已耗盡：%s",
        "gift.sent": "已確認贈禮：%s ← %s；本角色本次確認成功 %d 件，遊戲剩餘 %d 次",
        "gift.already_done": "%s 已達到當日目標，無需贈禮",
        "gift.dry_run": "演練計畫：向 %s 贈送 %s（不會提交）",
        "gift.warning": "角色 %s：%s",
        "gift.summary": "贈禮彙總：已確認 %d 件，無需贈禮 %d 位，演練 %d 位，無法完成 %d 位，結果不明 %d 位",
        "gift.reason.invalid_config": "贈禮設定無效，請檢查角色、禮物順序和每日累計上限。",
        "gift.reason.no_recipients": "未選擇可贈禮角色，未執行任何贈禮。",
        "gift.reason.unknown_character": "本機資料庫不包含該角色，或該角色不可贈禮。",
        "gift.reason.character_not_found": "未在羈遇介面找到該角色，未向其他角色贈禮。",
        "gift.reason.identity": "無法唯一確認目前角色，已停止贈禮。",
        "gift.reason.gift_not_found": "未找到設定的禮物，請核對禮物完整名稱、ID 和背包。",
        "gift.reason.gift_identity": "無法唯一確認禮物身分，未提交贈禮。",
        "gift.reason.inventory": "無法確認禮物庫存，未提交贈禮。",
        "gift.reason.quota": "無法確認角色當日剩餘次數，未提交贈禮。",
        "gift.reason.quantity": "無法確認贈送數量為 1，未提交贈禮。",
        "gift.reason.infinite": "偵測到不限量或無法計數的物品，未提交贈禮。",
        "gift.reason.global_limit": "遊戲提示全體角色當日贈禮上限已到，停止本次任務。",
        "gift.reason.budget": "已達到安全操作預算，停止本次任務。",
        "gift.reason.page": "角色或禮物頁面狀態異常，未繼續操作。",
        "gift.reason.navigation": "無法進入已確認的羈遇贈禮頁面，未提交贈禮。",
        "gift.reason.recovery": "無法安全返回已知頁面，停止本次任務。",
        "gift.reason.ambiguous": "識別結果不唯一，未執行不確定的點擊。",
        "gift.reason.scan": "無法安全完成頁面掃描，未繼續贈禮。",
        "gift.reason.receipt": "提交後的結果證據不一致，結果不明，不會重複提交。",
        "gift.reason.receipt_timeout": "未及時確認贈禮結果，結果不明，不會重複提交。",
        "gift.reason.controller": "控制器擷取畫面或輸入失敗，已停止操作。",
        "gift.reason.stopping": "已收到停止請求，不再提交贈禮。",
        "gift.reason.dry_run": "演練模式不會提交贈禮。",
        "gift.reason.exhausted": "所有設定的候選禮物均已耗盡，無法完成本角色目標。",
        "gift.reason.catalog": "本機角色或禮物資料庫缺失或無效，未提交贈禮。",
        "gift.reason.internal": "發生內部異常，已停止以避免重複贈禮。",
    },
    "en_us": {
        "gift.started": "Starting bond gifts: %d characters, mode: %s",
        "gift.mode.dry_run": "dry run (no submission)",
        "gift.mode.execute": "send gifts",
        "gift.character": "Character: %s; cumulative target today: %d gifts",
        "gift.candidate": "Current candidate gift: %s",
        "gift.exhausted": "Gift stock exhausted: %s",
        "gift.sent": "Gift confirmed: %s received %s; %d confirmed for this character this run, %d attempts remaining in-game",
        "gift.already_done": "%s has reached today's target; no gifts needed",
        "gift.dry_run": "Dry-run plan: give %s the gift %s (will not submit)",
        "gift.warning": "Character %s: %s",
        "gift.summary": "Gift summary: %d confirmed gifts, %d characters already done, %d dry-run plans, %d incomplete, %d uncertain",
        "gift.reason.invalid_config": "Invalid gift configuration. Check recipients, gift order and cumulative daily limits.",
        "gift.reason.no_recipients": "No eligible recipients selected. No gifts were sent.",
        "gift.reason.unknown_character": "The local catalog does not contain this character, or the character cannot receive gifts.",
        "gift.reason.character_not_found": "The character was not found on the bond screen. No other character was gifted instead.",
        "gift.reason.identity": "Cannot uniquely confirm the current character. Gift sending stopped.",
        "gift.reason.gift_not_found": "The configured gift was not found. Check its full name or ID and your inventory.",
        "gift.reason.gift_identity": "Cannot uniquely confirm the gift's identity. No gift was submitted.",
        "gift.reason.inventory": "Cannot confirm gift inventory. No gift was submitted.",
        "gift.reason.quota": "Cannot confirm this character's remaining attempts today. No gift was submitted.",
        "gift.reason.quantity": "Cannot confirm the gift quantity is 1. No gift was submitted.",
        "gift.reason.infinite": "An unlimited or uncountable item was detected. No gift was submitted.",
        "gift.reason.global_limit": "The game reports the daily gift limit for all characters has been reached. Task stopped.",
        "gift.reason.budget": "The safe operation budget has been reached. Task stopped.",
        "gift.reason.page": "The character or gift page is in an unexpected state. No further actions were taken.",
        "gift.reason.navigation": "Cannot reach a verified bond gifting screen. No gift was submitted.",
        "gift.reason.recovery": "Cannot safely return to a known screen. Task stopped.",
        "gift.reason.ambiguous": "Recognition was ambiguous. No uncertain click was performed.",
        "gift.reason.scan": "The page scan could not be completed safely. Gift sending stopped.",
        "gift.reason.receipt": "Post-submission evidence is inconsistent. The result is uncertain; no resubmission will occur.",
        "gift.reason.receipt_timeout": "The gift result was not confirmed in time. The result is uncertain; no resubmission will occur.",
        "gift.reason.controller": "Controller capture or input failed. Actions stopped.",
        "gift.reason.stopping": "A stop was requested. No more gifts will be submitted.",
        "gift.reason.dry_run": "Dry-run mode does not submit gifts.",
        "gift.reason.exhausted": "All configured candidate gifts are exhausted. This character's target cannot be completed.",
        "gift.reason.catalog": "The local character or gift catalog is missing or invalid. No gift was submitted.",
        "gift.reason.internal": "An internal error occurred. Stopped to avoid duplicate gifts.",
    },
    "ja_jp": {
        "gift.started": "絆ギフト開始：%d人、モード：%s",
        "gift.mode.dry_run": "ドライラン（確定なし）",
        "gift.mode.execute": "ギフト贈呈",
        "gift.character": "キャラクター：%s、本日の累計目標：%d個",
        "gift.candidate": "現在の候補ギフト：%s",
        "gift.exhausted": "ギフトの在庫を使い切りました：%s",
        "gift.sent": "贈呈確認：%s に %s；このキャラクターへの今回の確認済み贈呈 %d個、ゲーム内残り %d回",
        "gift.already_done": "%s は本日の目標に達しています。贈呈は不要です",
        "gift.dry_run": "ドライラン計画：%s に %s を贈呈（確定しません）",
        "gift.warning": "キャラクター %s：%s",
        "gift.summary": "ギフト集計：確認済み %d個、贈呈不要 %d人、ドライラン %d人、未達成 %d人、結果不明 %d人",
        "gift.reason.invalid_config": "ギフト設定が無効です。キャラクター、ギフト順、当日累計上限を確認してください。",
        "gift.reason.no_recipients": "贈呈可能なキャラクターが選択されていません。ギフトは贈っていません。",
        "gift.reason.unknown_character": "ローカル資料庫にないキャラクター、またはギフトを受け取れないキャラクターです。",
        "gift.reason.character_not_found": "絆画面でキャラクターを見つけられませんでした。別のキャラクターには贈っていません。",
        "gift.reason.identity": "現在のキャラクターを一意に確認できないため、贈呈を停止しました。",
        "gift.reason.gift_not_found": "設定されたギフトが見つかりません。全名称、ID、所持品を確認してください。",
        "gift.reason.gift_identity": "ギフトを一意に確認できないため、贈呈を確定していません。",
        "gift.reason.inventory": "ギフトの所持数を確認できないため、贈呈を確定していません。",
        "gift.reason.quota": "キャラクターの当日残り回数を確認できないため、贈呈を確定していません。",
        "gift.reason.quantity": "贈呈数が1個であることを確認できないため、確定していません。",
        "gift.reason.infinite": "無制限または数えられないアイテムを検出したため、贈呈を確定していません。",
        "gift.reason.global_limit": "全キャラクターの当日贈呈上限に達したとゲームに表示されました。タスクを停止します。",
        "gift.reason.budget": "安全な操作回数の上限に達したため、タスクを停止します。",
        "gift.reason.page": "キャラクターまたはギフト画面が想定外の状態です。操作を続けていません。",
        "gift.reason.navigation": "確認済みの絆ギフト画面へ移動できないため、贈呈を確定していません。",
        "gift.reason.recovery": "既知の画面へ安全に戻れないため、タスクを停止します。",
        "gift.reason.ambiguous": "認識結果が一意でないため、不確かなクリックは行っていません。",
        "gift.reason.scan": "画面の確認を安全に完了できないため、贈呈を停止しました。",
        "gift.reason.receipt": "確定後の結果の証拠が一致しません。結果不明として再送しません。",
        "gift.reason.receipt_timeout": "時間内に贈呈結果を確認できませんでした。結果不明として再送しません。",
        "gift.reason.controller": "コントローラーの画面取得または入力に失敗したため、操作を停止しました。",
        "gift.reason.stopping": "停止要求を受け付けました。これ以上贈呈を確定しません。",
        "gift.reason.dry_run": "ドライランでは贈呈を確定しません。",
        "gift.reason.exhausted": "設定された候補ギフトをすべて使い切ったため、このキャラクターの目標を達成できません。",
        "gift.reason.catalog": "ローカルのキャラクターまたはギフト資料庫がないか無効です。贈呈を確定していません。",
        "gift.reason.internal": "内部エラーが発生したため、重複贈呈を避けるために停止しました。",
    },
    "ko_kr": {
        "gift.started": "인연 선물 시작: 캐릭터 %d명, 모드: %s",
        "gift.mode.dry_run": "모의 실행 (확정 안 함)",
        "gift.mode.execute": "선물 보내기",
        "gift.character": "캐릭터: %s, 오늘 누적 목표: %d개",
        "gift.candidate": "현재 후보 선물: %s",
        "gift.exhausted": "선물 재고 소진: %s",
        "gift.sent": "선물 확인: %s에게 %s; 이번 실행에서 이 캐릭터의 성공 확인 %d개, 게임 내 남은 횟수 %d회",
        "gift.already_done": "%s의 오늘 목표에 도달하여 선물이 필요 없습니다",
        "gift.dry_run": "모의 실행 계획: %s에게 %s 선물 (확정하지 않음)",
        "gift.warning": "캐릭터 %s: %s",
        "gift.summary": "선물 요약: 확인된 선물 %d개, 선물 불필요 %d명, 모의 실행 %d명, 미완료 %d명, 결과 불명 %d명",
        "gift.reason.invalid_config": "선물 설정이 잘못되었습니다. 캐릭터, 선물 순서, 당일 누적 한도를 확인하세요.",
        "gift.reason.no_recipients": "선물 가능한 캐릭터를 선택하지 않았습니다. 선물을 보내지 않았습니다.",
        "gift.reason.unknown_character": "로컬 자료에 없는 캐릭터이거나 선물을 받을 수 없는 캐릭터입니다.",
        "gift.reason.character_not_found": "인연 화면에서 캐릭터를 찾지 못했습니다. 다른 캐릭터에게 대신 선물하지 않았습니다.",
        "gift.reason.identity": "현재 캐릭터를 유일하게 확인할 수 없어 선물을 중지했습니다.",
        "gift.reason.gift_not_found": "설정한 선물을 찾지 못했습니다. 전체 이름, ID, 가방을 확인하세요.",
        "gift.reason.gift_identity": "선물을 유일하게 확인할 수 없어 선물을 확정하지 않았습니다.",
        "gift.reason.inventory": "선물 재고를 확인할 수 없어 선물을 확정하지 않았습니다.",
        "gift.reason.quota": "캐릭터의 당일 남은 횟수를 확인할 수 없어 선물을 확정하지 않았습니다.",
        "gift.reason.quantity": "선물 수량이 1개인지 확인할 수 없어 확정하지 않았습니다.",
        "gift.reason.infinite": "무제한 또는 셀 수 없는 아이템이 감지되어 선물을 확정하지 않았습니다.",
        "gift.reason.global_limit": "게임에서 모든 캐릭터의 일일 선물 한도에 도달했다고 표시했습니다. 작업을 중지합니다.",
        "gift.reason.budget": "안전한 조작 횟수 한도에 도달하여 작업을 중지합니다.",
        "gift.reason.page": "캐릭터 또는 선물 화면이 예상과 다른 상태입니다. 조작을 계속하지 않았습니다.",
        "gift.reason.navigation": "확인된 인연 선물 화면에 진입할 수 없어 선물을 확정하지 않았습니다.",
        "gift.reason.recovery": "알려진 화면으로 안전하게 돌아갈 수 없어 작업을 중지합니다.",
        "gift.reason.ambiguous": "인식 결과가 유일하지 않아 불확실한 클릭을 하지 않았습니다.",
        "gift.reason.scan": "화면 탐색을 안전하게 완료할 수 없어 선물을 중지했습니다.",
        "gift.reason.receipt": "확정 후 결과 증거가 일치하지 않습니다. 결과 불명으로 처리하며 다시 보내지 않습니다.",
        "gift.reason.receipt_timeout": "제시간에 선물 결과를 확인하지 못했습니다. 결과 불명으로 처리하며 다시 보내지 않습니다.",
        "gift.reason.controller": "컨트롤러 화면 캡처 또는 입력에 실패하여 조작을 중지했습니다.",
        "gift.reason.stopping": "중지 요청을 받았습니다. 더 이상 선물을 확정하지 않습니다.",
        "gift.reason.dry_run": "모의 실행에서는 선물을 확정하지 않습니다.",
        "gift.reason.exhausted": "설정한 모든 후보 선물이 소진되어 이 캐릭터의 목표를 달성할 수 없습니다.",
        "gift.reason.catalog": "로컬 캐릭터 또는 선물 자료가 없거나 잘못되었습니다. 선물을 확정하지 않았습니다.",
        "gift.reason.internal": "내부 오류가 발생하여 중복 선물을 방지하기 위해 중지했습니다.",
    },
}


def _records(document: dict, field: str) -> list[dict]:
    """Validate renderer inputs without replacing the catalog's full validation."""
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
    ):
        raise ValueError("%s: schema_version must be integer 1" % field)
    if document["schema_version"] != 1:
        raise ValueError("%s: unsupported schema_version" % field)
    records = document.get(field)
    if not isinstance(records, list) or not records:
        raise ValueError("%s: expected a nonempty array" % field)
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("%s: each record must be an object" % field)
        identifier = record.get("id")
        pattern = r"char_[0-9]+" if field == "characters" else r"[A-Za-z0-9_]+"
        if not isinstance(identifier, str) or re.fullmatch(pattern, identifier) is None:
            raise ValueError("%s: invalid record ID" % field)
        if identifier in seen:
            raise ValueError("%s: duplicate ID %s" % (field, identifier))
        seen.add(identifier)
        names = record.get("names")
        if not isinstance(names, dict) or not names.get("zh_cn"):
            raise ValueError("%s: missing original Chinese name" % identifier)
        if any(
            not isinstance(name, str) or not name.strip() for name in names.values()
        ):
            raise ValueError("%s: names must be nonempty strings" % identifier)
        image = record.get("image")
        image_path = image.get("path") if isinstance(image, dict) else None
        if not isinstance(image_path, str) or not image_path:
            raise ValueError("%s: missing local image path" % identifier)
        path = PurePosixPath(image_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or re.search(r"[:\\]", image_path)
            or path.as_posix() != image_path
        ):
            raise ValueError(
                "%s: image path must be relative to base/image" % identifier
            )
        if field == "characters" and type(record.get("giftable")) is not bool:
            raise ValueError("%s: giftable must be a boolean" % identifier)
    return records


def render_task(characters: dict, gifts: dict) -> dict:
    """Render searchable plan rows using MXU's recursive select case options."""
    recipients = [
        item for item in _records(characters, "characters") if item["giftable"]
    ]
    gift_records = _records(gifts, "gifts")
    if not recipients:
        raise ValueError("characters: expected at least one giftable character")
    options: dict[str, Any] = {
        "GiftPlanCount": {
            "type": "select",
            "label": "$task_gift_plan_count",
            "description": "$task_gift_plan_count_desc",
            "default_case": "1",
            "cases": [
                {
                    "name": str(count),
                    "option": ["GiftRecipient_%d" % row for row in range(1, count + 1)],
                    "pipeline_override": {
                        "GiftSettings": {
                            "attach": {"recipient_mode": "rows", "plan_count": count}
                        }
                    },
                }
                for count in range(1, len(recipients) + 1)
            ],
        },
        "GiftDryRun": {
            "type": "switch",
            "label": "$task_gift_dry_run",
            "description": "$task_gift_dry_run_desc",
            "default_case": "No",
            "cases": [
                {
                    "name": name,
                    "label": "$option_switch_case_%s" % name.lower(),
                    "pipeline_override": {
                        "GiftSettings": {"attach": {"dry_run": value}}
                    },
                }
                for name, value in (("Yes", True), ("No", False))
            ],
        },
    }
    for row in range(1, len(recipients) + 1):
        options["GiftRecipient_%d" % row] = {
            "type": "select",
            "label": "$task_gift_plan_recipient_%d" % row,
            "description": "$task_gift_recipient_row_desc",
            "default_case": "None",
            "cases": [
                {
                    "name": "None",
                    "label": "$task_gift_recipient_none",
                    "pipeline_override": {
                        "GiftSettings": {"attach": {"recipient_%d" % row: ""}}
                    },
                },
                *[
                    {
                        "name": character["id"],
                        "label": "$task_gift_recipient_%s" % character["id"],
                        "icon": "resource/base/image/%s" % character["image"]["path"],
                        "option": [
                            "GiftPriorityMode_%s" % character["id"],
                            "GiftLimit_%s" % character["id"],
                        ],
                        "pipeline_override": {
                            "GiftConfig_%s"
                            % character["id"]: {"attach": {"selected": True}},
                            "GiftSettings": {
                                "attach": {"recipient_%d" % row: character["id"]}
                            },
                        },
                    }
                    for character in recipients
                ],
            ],
        }
    for character in recipients:
        identifier = character["id"]
        node = "GiftConfig_%s" % identifier
        priority = "GiftPriority_%s" % identifier
        options["GiftPriorityMode_%s" % identifier] = {
            "type": "select",
            "label": "$task_gift_priority_mode",
            "description": "$task_gift_priority_mode_desc",
            "default_case": "List",
            "cases": [
                {
                    "name": "List",
                    "label": "$task_gift_priority_mode_list",
                    "option": [
                        "GiftPrimary_%s" % identifier,
                        "GiftBackup_%s" % identifier,
                        "GiftExtra_%s" % identifier,
                    ],
                    "pipeline_override": {node: {"attach": {"gift_mode": "choices"}}},
                },
                {
                    "name": "Custom",
                    "label": "$task_gift_priority_mode_manual",
                    "option": [priority],
                    "pipeline_override": {node: {"attach": {"gift_mode": "manual"}}},
                },
            ],
        }
        for option, field, label in (
            ("GiftPrimary", "primary_gift", "primary"),
            ("GiftBackup", "backup_gift", "backup"),
        ):
            options["%s_%s" % (option, identifier)] = {
                "type": "select",
                "label": "$task_gift_%s" % label,
                "description": "$task_gift_%s_desc" % label,
                "default_case": "None",
                "cases": [
                    {
                        "name": "None",
                        "label": "$task_gift_%s_none" % label,
                        "pipeline_override": {node: {"attach": {field: ""}}},
                    },
                    *[
                        {
                            "name": gift["id"],
                            "label": "$task_gift_item_%s" % gift["id"],
                            "icon": "resource/base/image/%s" % gift["image"]["path"],
                            "pipeline_override": {
                                node: {"attach": {field: gift["id"]}}
                            },
                        }
                        for gift in gift_records
                    ],
                ],
            }
        options["GiftExtra_%s" % identifier] = {
            "type": "input",
            "label": "$task_gift_extra",
            "description": "$task_gift_extra_desc",
            "inputs": [
                {
                    "name": "extra_gifts",
                    "label": "$task_gift_extra_input",
                    "default": "",
                    "pipeline_type": "string",
                }
            ],
            "pipeline_override": {node: {"attach": {"extra_gifts": "{extra_gifts}"}}},
        }
        # Retain both the option ID and the input name for previously saved values.
        options[priority] = {
            "type": "input",
            "label": "$task_gift_priority",
            "description": "$task_gift_priority_desc",
            "inputs": [
                {
                    "name": "gifts",
                    "label": "$task_gift_priority_input",
                    "default": "",
                    "pipeline_type": "string",
                }
            ],
            "pipeline_override": {node: {"attach": {"gifts": "{gifts}"}}},
        }
        options["GiftLimit_%s" % identifier] = {
            "type": "select",
            "label": "$task_gift_limit",
            "description": "$task_gift_limit_desc",
            "default_case": "3",
            "cases": [
                {
                    "name": str(limit),
                    "pipeline_override": {node: {"attach": {"limit": limit}}},
                }
                for limit in range(1, 4)
            ],
        }
    return {
        "task": [
            {
                "name": "Gift",
                "label": "$task_gift_label",
                "entry": "GiftEntry",
                "description": "$task_gift_desc",
                "group": ["Daily"],
                "option": ["GiftPlanCount", "GiftDryRun"],
            }
        ],
        "option": options,
    }


def render_config(characters: dict) -> dict:
    """Render inert data nodes read by Context.get_node_data, not a business flow."""
    recipients = [
        item for item in _records(characters, "characters") if item["giftable"]
    ]
    # MaaFramework merges attach fields shallowly; keep each row in its own field.
    attachments = {
        "GiftSettings": {
            "dry_run": False,
            "recipient_mode": "legacy",
            "plan_count": 1,
            **{"recipient_%d" % row: "" for row in range(1, len(recipients) + 1)},
        }
    }
    for character in recipients:
        identifier = character["id"]
        attachments["GiftConfig_%s" % identifier] = {
            "character_id": identifier,
            "selected": False,
            "gifts": "",
            "limit": 3,
            "gift_mode": "manual",
            "primary_gift": "",
            "backup_gift": "",
            "extra_gifts": "",
        }
    return {
        name: {
            "recognition": {"type": "DirectHit"},
            "action": {"type": "DoNothing"},
            "rate_limit": 0,
            "pre_delay": 0,
            "post_delay": 0,
            "attach": attach,
        }
        for name, attach in attachments.items()
    }


def _markdown_name(name: str) -> str:
    return re.sub(r"([\\`*{}\[\]()!|_])", r"\\\1", html.escape(name, quote=False))


def render_interface_locales(
    characters: dict, gifts: dict
) -> dict[str, dict[str, str]]:
    """Keep original names when a translation has not been verified by the catalog."""
    recipients = [
        item for item in _records(characters, "characters") if item["giftable"]
    ]
    gift_records = _records(gifts, "gifts")
    result = {}
    for language in LANGUAGES:
        messages = dict(_INTERFACE_MESSAGES[language])
        for row in range(1, len(recipients) + 1):
            messages["task_gift_plan_recipient_%d" % row] = (
                _PLAN_RECIPIENT_LABELS[language] % row
            )
        for character in recipients:
            messages["task_gift_recipient_%s" % character["id"]] = character[
                "names"
            ].get(language, character["names"]["zh_cn"])
        listing = []
        for gift in gift_records:
            original = gift["names"]["zh_cn"]
            localized = gift["names"].get(language, original)
            messages["task_gift_item_%s" % gift["id"]] = localized
            name = _markdown_name(original)
            if localized != original:
                name += " / " + _markdown_name(localized)
            listing.append("- `%s` — %s" % (gift["id"], name))
        messages["task_gift_priority_desc"] += "\n\n%s\n\n%s" % (
            _CATALOG_TITLES[language] % len(gift_records),
            "\n".join(listing),
        )
        result[language] = messages
    return result


def render_agent_locales() -> dict[str, dict[str, str]]:
    return {language: dict(_AGENT_MESSAGES[language]) for language in LANGUAGES}


_JSONC_TOKEN = re.compile(r'("(?:\\.|[^"\\])*")|(//[^\r\n]*|/\*[\s\S]*?\*/)')


def _mask_comments(text: str) -> str:
    """Mask JSONC comments without moving offsets or altering quoted URLs."""
    return _JSONC_TOKEN.sub(
        lambda match: (
            match.group(1)
            if match.group(1) is not None
            else re.sub(r"[^\r\n]", " ", match.group(0))
        ),
        text,
    )


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _loads(text: str) -> Any:
    return json.loads(_mask_comments(text), object_pairs_hook=_unique_pairs)


def _member_spans(text: str) -> dict[str, tuple[int, int]]:
    """Get exact top-level value spans for surgical changes to shared JSON files."""
    clean = _mask_comments(text)
    if not isinstance(_loads(text), dict):
        raise ValueError("expected a JSON object")
    decoder = json.JSONDecoder()
    position = clean.index("{") + 1
    result = {}
    while True:
        position = re.compile(r"\s*").match(clean, position).end()
        if clean[position] == "}":
            return result
        key, position = decoder.raw_decode(clean, position)
        position = clean.index(":", position) + 1
        position = re.compile(r"\s*").match(clean, position).end()
        start = position
        _, position = decoder.raw_decode(clean, position)
        result[key] = (start, position)
        position = re.compile(r"\s*").match(clean, position).end()
        if clean[position] == ",":
            position += 1


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _merge_locale(text: str, messages: dict[str, str]) -> str:
    """Append/update only this feature's fields; leave all unrelated bytes alone."""
    document = _loads(text)
    spans = _member_spans(text)
    replacements = []
    missing = {}
    for key, value in messages.items():
        if key not in document:
            missing[key] = value
        elif document[key] != value:
            start, end = spans[key]
            replacements.append((start, end, _json(value)))
    for start, end, value in sorted(replacements, reverse=True):
        text = text[:start] + value + text[end:]
    if missing:
        spans = _member_spans(text)
        position = (
            max(end for _, end in spans.values()) if spans else text.index("{") + 1
        )
        newline = _newline(text)
        block = ("," if spans else "") + newline
        block += ("," + newline).join(
            "    %s: %s" % (_json(key), _json(value)) for key, value in missing.items()
        )
        if not text[position:].startswith(("\n", "\r\n")):
            block += newline
        text = text[:position] + block + text[position:]
    _loads(text)
    return text


def _ensure_import(text: str) -> str:
    document = _loads(text)
    if not isinstance(document, dict) or not isinstance(document.get("import"), list):
        raise ValueError("assets/interface.json: expected an import array")
    imports = document["import"]
    if any(not isinstance(item, str) for item in imports):
        raise ValueError("assets/interface.json: import items must be strings")
    if imports.count(TASK_IMPORT) > 1:
        raise ValueError("assets/interface.json: duplicate Gift import")
    if TASK_IMPORT in imports:
        return text
    start, end = _member_spans(text)["import"]
    clean = _mask_comments(text)
    position = start + 1
    last_end = None
    decoder = json.JSONDecoder()
    for _ in imports:
        position = re.compile(r"\s*").match(clean, position).end()
        _, position = decoder.raw_decode(clean, position)
        last_end = position
        position = re.compile(r"\s*").match(clean, position).end()
        if clean[position] == ",":
            position += 1
    newline = _newline(text)
    if last_end is None:
        insertion = newline + "        " + _json(TASK_IMPORT) + newline + "    "
        text = text[: end - 1] + insertion + text[end - 1 :]
    else:
        insertion = "," + newline + "        " + _json(TASK_IMPORT)
        text = text[:last_end] + insertion + text[last_end:]
    _loads(text)
    return text


def _read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def _task_json(document: dict) -> str:
    """保持原生 PI 数据结构；生成的每个 case 一行，避免数 MB 重复缩进。"""

    def compact(value: Any) -> str:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )

    lines = ["{", '    "task":' + compact(document["task"]) + ",", '    "option":{']
    options = list(document["option"].items())
    for option_index, (name, option) in enumerate(options):
        lines.append("        " + compact(name) + ":{")
        fields = list(option.items())
        for field_index, (key, value) in enumerate(fields):
            suffix = "," if field_index + 1 < len(fields) else ""
            prefix = "            " + compact(key) + ":"
            if key == "cases":
                lines.append(prefix + "[")
                lines.extend(
                    "                "
                    + compact(case)
                    + ("," if index + 1 < len(value) else "")
                    for index, case in enumerate(value)
                )
                lines.append("            ]" + suffix)
            else:
                lines.append(prefix + compact(value) + suffix)
        lines.append("        }" + ("," if option_index + 1 < len(options) else ""))
    lines.extend(["    }", "}", ""])
    return "\n".join(lines)


def _plan(root: Path, characters: dict | None, gifts: dict | None) -> dict[Path, str]:
    if characters is None:
        characters = _loads(_read_text(root / CHARACTERS_PATH))
    if gifts is None:
        gifts = _loads(_read_text(root / GIFTS_PATH))
    # Validate and read every input before the first write, including shared files.
    plan = {
        root / TASK_PATH: _task_json(render_task(characters, gifts)),
        root
        / CONFIG_PATH: json.dumps(
            render_config(characters), ensure_ascii=False, indent=4
        )
        + "\n",
    }
    for family, translations in (
        ("interface", render_interface_locales(characters, gifts)),
        ("agent", render_agent_locales()),
    ):
        for language, messages in translations.items():
            path = root / "assets/resource/locales" / family / (language + ".json")
            plan[path] = _merge_locale(_read_text(path), messages)
    interface_path = root / "assets/interface.json"
    plan[interface_path] = _ensure_import(_read_text(interface_path))
    return plan


def check(
    root: Path = ROOT, characters: dict | None = None, gifts: dict | None = None
) -> list[str]:
    """Return diagnostics (empty when current); never write or access the network."""
    root = Path(root).resolve()
    try:
        plan = _plan(root, characters, gifts)
        return [
            "%s: needs regeneration" % path.relative_to(root).as_posix()
            for path, content in plan.items()
            if not path.is_file() or _read_text(path) != content
        ]
    except (OSError, ValueError, UnicodeError) as exc:
        return ["Gift configuration check failed: %s" % exc]


def update(
    root: Path = ROOT, characters: dict | None = None, gifts: dict | None = None
) -> list[Path]:
    """Update generated Gift fields only and return changed paths; preserve others."""
    root = Path(root).resolve()
    plan = _plan(root, characters, gifts)
    changed = []
    for path, content in plan.items():
        if path.is_file() and _read_text(path) == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        changed.append(path)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="只读检查生成结果，不联网")
    mode.add_argument("--write", action="store_true", help="更新赠礼配置和五语文案")
    parser.add_argument("--root", type=Path, default=ROOT, help="仓库根目录")
    args = parser.parse_args(argv)
    if args.check:
        diagnostics = check(args.root)
        if diagnostics:
            sys.stderr.write("\n".join(diagnostics) + "\n")
            return 1
        sys.stdout.write("赠礼配置与离线资料库一致。\n")
        return 0
    try:
        changed = update(args.root)
    except (OSError, ValueError, UnicodeError) as exc:
        sys.stderr.write("赠礼配置更新失败：%s\n" % exc)
        return 1
    sys.stdout.write("已更新 %d 个赠礼资源文件。\n" % len(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
