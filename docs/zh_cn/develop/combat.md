# 战斗系统

战斗内核位于 `agent/custom/action/Combat/`，负责「进战 → 出招/切人 → 脱战判定」。走到怪物面前、战后领奖、处理弹窗等流程由调用方 Pipeline 负责。

用户侧说明见 [自动战斗](../introduction/AutoCombat.md)，配队与后续能力规划见 [自动配队计划](./combat-auto-team-plan.md)。

## 设计来源

| 来源 | 采用的部分 |
|---|---|
| ok-NTE（BnanZ0/ok-nte main） | 出招表 DSL 语法（兼容子集）、「每个角色跑一段出招再切人」的轮转模型、默认键位、Q 动画期间 HUD 消失的处理、进场先按 R、中键锁敌、冲刺（按住 W 点 Shift）与带方向闪避 |
| ok-NTE `CombatCheck.py`（提交 `893525d`） | 血条 + 敌人等级 + Boss 多证据判定、「未知不等于阴性」、目标丢失后先确认再结束；血条颜色与区域换算到 720p 后用于 `CombatEnemyBarShape`，等级/Boss 区域用于 OCR 节点（均未实机校准） |
| `pinkpaw/pinkpaw_core3.py`（已实机校准） | HUD 斜杠检测、当前角色高亮、黑屏判定的 ROI 与阈值，切人确认 + 窗口内补按一次 |
| `PinkPawHeist/tools.json` | 敌人血条 ColorMatch 参数（复制为 `CombatEnemyHealthBar`）、撤离确认的文字与 ROI（复制为 `CombatRetreatPrompt`）；不引用粉爪节点，也不复用其动作链 |

## 模块

| 文件 | 职责 |
|---|---|
| `api.py` | 对外入口 `run_combat` / `prepare_combat`，信号节点校验，组装运行时；`PrintReporter` 输出 i18n 用户消息与开战摘要 |
| `actions.py` | MaaFramework 注册：`combat_run`（动作）、`combat_in_hud` / `combat_enemy_bar`（识别） |
| `settings.py` | 选项定义与合并（`CombatOptions`、`SlotOverride`、载体节点读取、`accepted_reasons` 解析） |
| `plan.py` | 有效方案：方案副本 + 轮转/槽位/禁用覆盖 → 校验 → `CombatPlan` 摘要与指纹 |
| `locate.py` / `scheme.py` | 方案文件查找、解析、校验 |
| `dsl.py` | 出招表编译器（含 `speed` 控制指令） |
| `engine.py` | 等待进战、首发与轮转、切人确认、降级、队伍人数/禁用槽位过滤 |
| `session.py` | 脚本会话 `CombatSession` |
| `executor.py` | 指令 → 输入原语，HUD 门控，禁用动作/槽位过滤，输入节奏 |
| `targeting.py` | 有限锁敌与转视角索敌（纯逻辑） |
| `runtime.py` | `Monitor` 生命周期判定、时钟、停止检查、节流快照、输入门控、可中断等待 |
| `perception.py` | HUD / 当前槽位 / 黑屏判定、血条形状、多证据快照 `observe_frame` |
| `inputs.py` | 只通过控制器发键，记账并保证释放 |
| `model.py` / `keys.py` | 共用类型（`Snapshot`、`CombatResult`、`ActionResult`）、结束原因、键位表 |

只有 `actions.py` 依赖 maa，其余模块可以直接单测。

## 调用方式

三种方式都会读取全局战斗配置，再叠加调用方的覆盖参数。

### Pipeline 步骤

在自己的节点里调用 `combat_run`，后面接自己的 `next`：

```jsonc
"MyTaskFight": {
    "recognition": {"type": "DirectHit"},
    "action": {
        "type": "Custom",
        "param": {
            "custom_action": "combat_run",
            "custom_action_param": {"wait_timeout": 10, "max_duration": 120}
        }
    },
    "rate_limit": 0,
    "pre_delay": 0,
    "post_delay": 0,
    "next": ["MyTaskClaimReward", "[JumpBack]SceneAnyEnterWorld"]
}
```

`custom_action_param` 中两个专用键（不参与战斗选项合并）：

- `accepted_reasons`：哪些结束原因让本动作成功（走 `next`），其余走 `on_error`。默认只有 `["cleared"]`。只影响 Pipeline 流向，不改变 `CombatResult.success`。
- `signal_nodes`：调用方自己的只读识别节点，键固定为 `active` / `cleared` / `disengaged` / `retreated` / `defeated` / `suspended`，值为节点名或节点名列表（任一命中即真）。开战前校验：节点必须存在、未禁用、识别类型不是 `DirectHit`；只调用 recognition，不执行节点 action。

```jsonc
"custom_action_param": {
    "max_duration": 120,
    "signal_nodes": {"cleared": ["MyTask_VictoryText"], "suspended": "MyTask_Dialog"},
    "accepted_reasons": ["cleared", "disengaged", "target_lost"]
}
```

`Combat.json` 中的 `CombatRun` 是默认配置的现成节点（只接受 `cleared`），可直接 `context.run_task("CombatRun")`。

### Pipeline 门控

- `CombatEnemyVisible`：画面中有敌人。`Or` 组合四个证据节点：`CombatEnemyHealthBar`（精确颜色）、`CombatEnemyBarShape`（容差颜色 + 形状）、`CombatEnemyLevel`（等级文字）、`CombatBossLevel`（Boss 等级文字）。Python 内核逐个执行同一组子节点并分别记录证据，两边判定一致。
- `CombatInHud`：处于可操作界面（底部血条可见且非黑屏），`detail.slot` 为当前槽位。
- `CombatFightWhenEnemy`：两者都满足时开打，确认结束或目标丢失后回到父节点（`accepted_reasons` 已设为 `cleared/disengaged/target_lost`）；撤退、超时、异常走 `on_error`。门控单帧命中后，内核仍要两张新截图确认才算进战。

```jsonc
"next": ["MyTaskArrived", "[JumpBack]CombatFightWhenEnemy", "MyTaskKeepWalking"]
```

### Python

```python
from custom.action.Combat import prepare_combat, run_combat

plan = prepare_combat(context, overrides={"rotation_order": "2,1,3"})  # 只合并与校验：不截图、不发输入
result = run_combat(
    context,
    overrides={"max_duration": 120, "combat_speed": 0.8},
    signal_nodes={"cleared": ["MyTask_VictoryText"]},
)
if result.reason == "cleared":
    ...  # 其他原因见下文「结束原因」
```

签名：

- `run_combat(context, overrides=None, option_nodes=(), reporter=None, *, scheme_data=None, signal_nodes=None, script=None) -> CombatResult`
- `prepare_combat(context, overrides=None, option_nodes=(), *, scheme_data=None) -> CombatPlan`，失败抛 `SchemeError`。

`scheme_data` 是内存中的版本 1 方案对象，走与文件相同的校验，不需要落盘。`script` 见下文「脚本会话」。`reporter` 为 `None` 时用 `PrintReporter` 推送 MXU 消息；传入 `Reporter` 子类可接管消息，未实现的可选回调（`plan_summary`、`state_changed`、`switched`）会被跳过。

## 选项

| 参数 | 默认 | 说明 |
|---|---|---|
| `scheme` | `team_rotation` | 内置方案名或 `custom` |
| `custom_scheme` | 空 | `scheme=custom` 时的方案文件名或路径 |
| `wait_timeout` | 30 | 等待进战秒数，0 = 一直等 |
| `max_duration` | 300 | 单场最长秒数，0 = 不限 |
| `clear_confirm` | 3 | 目标连续丢失多久停止本场（结果为 `target_lost`）；只累计可判断的阴性帧 |
| `hud_lost_timeout` | 10 | 战斗中 HUD 连续消失多久结束本场，0 = 不判断 |
| `team_size` | 4 | 队伍人数 1~4，超出人数的槽位不参与切人，出招表里切到这些槽位的 `switch(n)` 直接跳过 |
| `lock_target` | true | 开战按一次中键锁敌，每个目标丢失阶段最多重锁一次 |
| `search_turn` | false | 目标丢失时转视角寻找（仅前台 Seize 鼠标），开启后目标丢失确认额外 +2 秒 |
| `use_ultimate` / `use_skill` / `use_arc` / `use_dodge` / `use_jump` / `use_switch` | true | false 时跳过对应指令及其原始按键写法；`use_switch=false` 时轮转方案改为站场 |
| `assist_detection` | true | 血条阴性时再用血条形状、敌人等级、Boss 等级确认敌人；false 时只用精确颜色血条 |
| `combat_speed` | 1.0 | 输入节奏倍率 0.5~2.0，见「输入节奏」 |
| `rotation_mode` | 空 | 空 = 沿用方案，否则 `cycle` / `stay` |
| `rotation_order` | 空 | 如 `"1,2,1,3"` 或 `[1, 2, 1, 3]`，1~16 项；首位即首发 |
| `min_field_time` | 空 | 覆盖方案的最短站场秒数 0~10 |
| `slots` | 空 | 逐槽覆盖：`{"2": {"enabled": false, "character": "零", "note": "主C", "combo": "skill, ultimate", "min_field_time": 2}}` |
| `option_nodes` | 空 | 调用方自己的载体节点列表，读取它们的 `attach` 作为覆盖 |

合并优先级（后者覆盖前者）：内置默认 < 全局战斗配置载体节点 < `custom_action_param` < `option_nodes`。`None` 与空字符串视为未设置；`slots` 按槽位、按字段合并，明确的 `false` 与 `0` 保留。布尔值接受 `true/false/yes/no/on/off/1/0`，`team_size` 必须是 1~4 的整数。

非法值的处理分两类：

- 既有的简单选项（时长、开关、队伍人数等）：保留上一层的值并写 warning 日志，不中断战斗。
- 新增选项（`combat_speed`、`assist_detection`、`rotation_*`、`min_field_time`、`slots`）：开战前报 `scheme_error`（`combat.scheme.bad_option`），不发送任何游戏输入，也不会悄悄换成别的出招。

任务选项请**每个选项写自己的载体节点 `attach`**，不要多个选项同时覆盖同一个节点的 `custom_action_param`（同名字段会整体替换，只有最后一个生效）。参考 `AutoCombat.json` 的 `AutoCombat_*Option`。

### 有效方案

`prepare_combat` 与 `run_combat` 共用同一步骤：加载方案 → 合并选项 → `plan.apply_overrides` → 校验 → `CombatPlan`。覆盖作用于方案的深拷贝，不污染内置方案缓存或下一场。

- `rotation_order` 去掉禁用槽位和超出 `team_size` 的槽位后作为生效顺序（`effective_order`），首位即首发；站场模式不强制首发，跟随当前角色。
- 非空的 `slots.N.combo` 用同一 DSL 编译器编译，错误报 `combat.scheme.slot_combo` 并带行列位置；未覆盖的槽位沿用方案。
- 轮转中没有可用槽位报 `no_enabled_slot`；轮转用到的槽位缺出招报 `missing_combo`。
- `CombatPlan.fingerprint` 按实际生效内容计算，开战摘要以此去重：改了出招、节奏或顺序会重新提示，同一配置在持续模式下只提示一次。

### 输入节奏

`combat_speed` 是输入节奏倍率，不是游戏速度，也不改技能冷却。

- 缩放：默认连点间隔（0.1 秒 / 倍率）、每轮最短耗时；倍率低于 1 时每条指令后额外等待 `(1/倍率 - 1) × 0.1` 秒。
- 不缩放：50 ms 点按、显式的 `hold` / `walk` / `wait` / `sprint` 秒数、站场时长、识别节流、切人确认、目标丢失确认窗口、`max_duration`。
- 出招表可用 `speed(1.5)` 改变本场后续倍率，脚本可用 `session.set_speed()`。改动只在本场有效，下一场恢复配置值。

## 结束原因

| `reason` | `success` | `confirmed_end` | 含义 |
|---|---|---|---|
| `cleared` | ✔ | ✔ | 调用方 `cleared` 信号连续两张新图命中，且同帧没有敌人证据 |
| `disengaged` | ✘ | ✔ | 调用方 `disengaged` 信号确认已脱离战斗 |
| `retreated` | ✘ | ✔ | 调用方 `retreated` 信号确认已撤离 |
| `defeated` | ✘ | ✔ | 调用方 `defeated` 信号确认战斗失败 |
| `target_lost` | ✘ | ✘ | 连续 `clear_confirm` 秒可判断地看不到敌人，已停止输出；**不等于清场** |
| `timeout` | ✘ | ✘ | 超过 `max_duration` |
| `hud_lost` | ✘ | ✘ | 战斗中长时间看不到 HUD（阵亡、剧情、弹窗、撤离转场） |
| `observation_lost` | ✘ | ✘ | 持续截图失败、识别无法启动或证据矛盾超过 10 秒 |
| `no_combat` | ✘ | ✘ | `wait_timeout` 内没有进战 |
| `stopped` | ✘ | ✘ | 用户停止任务 |
| `script_finished` | ✘ | ✘ | 脚本正常返回且没有终局证据 |
| `scheme_error` | ✘ | ✘ | 方案、配置或信号节点校验失败（开战前，未发送输入） |
| `error` | ✘ | ✘ | 内部异常或脚本异常（日志有堆栈） |

`CombatResult` 字段：`success`、`reason`、`duration`、`entered`（是否确认进战）、`confirmed_end`、`evidence`（最后一帧各证据的 `True/False/None`）。

> **行为变更**：以前 `timeout` 与 `hud_lost` 也算 `success=True`，敌人血条消失 3 秒报 `cleared`。现在只有正向清场证据才是 `cleared`；普通开放世界没有胜利画面可识别时，最好的结果是 `target_lost`。需要旧的“打完就继续”语义时，在 `custom_action_param` 里写 `accepted_reasons`，不要依赖 `success`。

所有路径都会在 finally 中释放按住的键和鼠标。

### 生命周期

`Monitor`（`runtime.py`）对每张新截图只更新一次，状态为 `waiting → active ⇄ uncertain/verifying`，另有可恢复的 `suspended` 和终止的 `ended`。

- 进战：连续两张新图可操作且有敌人证据；单帧噪点不启动。
- 敌人证据：精确颜色血条、血条形状、等级、Boss、调用方 `active`，**任一**为真即有敌人；只有全部已配置的检测都成功返回阴性才算“没看到”。识别异常、节点无法启动、截图失败都是未知（`None`），不是阴性。
- 清场计时只累计两张相邻可判断阴性帧之间的时间（单步最多 1 秒）；未知、黑屏、HUD 隐藏的时间不计入，恢复后的首帧也不补算。敌人重新出现立即清零。
- 候选结束前先取新截图复核，不用过期缓存结束战斗。同帧出现敌人证据或多个互斥终局信号时进入不确定状态，不宣布胜利。
- 撤离确认界面（`CombatRetreatPrompt`）出现时立即暂停并释放按键，不把提示当作撤退完成，也不会自动点确认。取消后要新截图确认仍有敌人才恢复输入；之后的黑屏转场不是离场证据，只有调用方 `retreated` 信号才产生 `retreated`，否则按 `hud_lost` 结束。

### 输入门控

- `wait_ready` 只在当前截图可操作时返回（HUD 可见、非黑屏、无暂停/撤离提示、敌人证据不是未知）；截图失败或长时间失明由 `Monitor` 以 `observation_lost` / `hud_lost` 结束，不再“超时后照样输入”。
- 每笔输入前检查停止、终局与门控；截图复用不超过 0.2 秒，画面变为不可操作时立即释放按住的键，当前动作剩余部分放弃。
- 同时按下的组合键（`walk(wa)` 等）只做一次门控，按下顺序固定，松开顺序相反。

### Pipeline 结果适配

`combat_run` 的返回值：`result.reason in accepted_reasons`。内置入口的设置：

| 节点 | `accepted_reasons` | 效果 |
|---|---|---|
| `CombatRun` | 默认（仅 `cleared`） | 公开接口，调用方按需放宽 |
| `CombatFightWhenEnemy` | `cleared` / `disengaged` / `target_lost` | 打完或目标丢失回到父节点 |
| `AutoCombatMain` | `cleared` / `disengaged` / `target_lost` | 持续模式回到监听，单次模式正常结束；撤退、超时、HUD/截图异常、出错均结束任务，撤退后不会立即重开 |

### 脚本会话

`run_combat(context, script=fn)` 在确认进战后调用 `fn(session)` 一次，与默认轮转共用同一个引擎、监视器和输入驱动。脚本由调用方代码传入，方案 JSON 不能执行任意代码。

```python
def my_script(session):
    session.switch(2)                         # 复用切人确认，禁用/超出队伍的槽位返回 skipped
    session.perform("skill, ultimate, l_click(1)")
    session.perform("arc, skill", slot=3)     # 先确认切到 3 号位再出招
    session.set_speed(0.8)
    while session.state != "ended":
        session.run_default(turns=1)          # 按有效方案跑一轮，与自动战斗相同

result = run_combat(context, script=my_script)
```

| 成员 | 说明 |
|---|---|
| `state` / `snapshot` / `evidence` / `elapsed` | 只读：生命周期阶段、最近快照、最近证据、已战斗秒数 |
| `current_slot` / `unavailable_slots` / `speed` | 只读：当前确认的槽位（0 未知）、本场不可用或被排除的槽位、当前倍率 |
| `switch(slot)` / `perform(combo, slot=None)` | 切人；执行一段 DSL（文本、多行列表或 `compile_combo` 结果） |
| `attack` / `skill` / `ultimate` / `arc` / `dodge` / `jump` / `walk` / `wait` | 单条指令的便捷写法 |
| `set_speed(factor)` / `run_default(turns=1)` / `stop()` | 改本场倍率；跑默认轮转；主动结束（`script_finished`） |

控制方法返回 `ActionResult(status, executed, skipped, controls)`：`sent`（控制器已接受输入，**不代表技能放出或命中**）、`unchanged`（已在目标槽位，未按键）、`configured`（只改了倍率）、`completed`（`run_default` 跑完）、`skipped`（被策略禁用）、`switch_failed`、`interrupted`（画面不可操作，动作已放弃）。参数非法抛 `ValueError` / `ComboError`。

所有方法都经过同一停止检查、HUD/场景门控、动作与槽位禁用以及松键逻辑，原始按键写法（`keydown(q)`、`keypress(4)`）同样受禁用约束。战斗结束时动作方法抛出内部结束信号；脚本吞掉该信号后，原因仍会被保留，之后的调用继续被拒绝。会话结束后再调用抛 `RuntimeError`。脚本抛出异常时结果为 `error`，并释放所有按键。

## 全局战斗配置

MXU「设置 → 任务设置」下有四个分区，选项都定义在 `assets/resource/tasks/CombatSetting.json`：

| 分区 | 选项 | 类型 | 载体节点 → `attach` |
|---|---|---|---|
| 战斗配置 | `CombatScheme` | select（9 个内置方案 + 自定义） | `CombatSetting_Scheme` → `scheme` |
| 战斗配置 | └ `CombatCustomScheme`（仅「自定义」时显示） | input | `CombatSetting_CustomScheme` → `custom_scheme` |
| 战斗配置 | `CombatTeamSize` | select（1~4） | `CombatSetting_TeamSize` → `team_size` |
| 角色与轮转 | `CombatAdvanced` | switch（默认关） | 无；「是」时显示下面的子选项 |
| 角色与轮转 | └ `CombatRotationMode` | select（沿用/轮转/站场） | `CombatSetting_RotationMode` → `rotation_mode` |
| 角色与轮转 | └ `CombatRotationOrder` | input（string，可空） | `CombatSetting_RotationOrder` → `rotation_order` |
| 角色与轮转 | └ `CombatFieldTime` | input（string，可空） | `CombatSetting_FieldTime` → `min_field_time` |
| 角色与轮转 | └ `CombatDisabledSlots` | checkbox（1~4） | 每个 case 一个：`CombatSetting_DisableSlotN` → `slots.N.enabled: false` |
| 角色与轮转 | └ `CombatSlot1`~`CombatSlot4` | input（名称/备注/出招/站场，均可空） | `CombatSetting_SlotN` → `slots.N.*` |
| 输入节奏 | `CombatSpeed` | input（string，0.5~2.0） | `CombatSetting_Speed` → `combat_speed` |
| 识别与安全策略 | `CombatDisabledActions` | checkbox（不使用的动作） | 每个 case 一个：`CombatSetting_Disable<Action>` → `use_<action>: false` |
| 识别与安全策略 | `CombatLockTarget` | switch | `CombatSetting_LockTarget` → `lock_target` |
| 识别与安全策略 | `CombatSearchTurn` | switch | `CombatSetting_SearchTurn` → `search_turn` |
| 识别与安全策略 | `CombatAssistDetection` | switch | `CombatSetting_AssistDetection` → `assist_detection` |
| 识别与安全策略 | `CombatClearConfirm` | input（int，1~60） | `CombatSetting_ClearConfirm` → `clear_confirm` |

已对照 MXU 源码（v2.3.1 的 `OptionEditor.tsx`、`pipelineOverride.ts`，以及当前打包产物中的同名逻辑）确认的行为：

- 运行时只合并 `global_option` 里的选项（优先级最低，对所有任务生效），`setting` 只负责显示。所以顶层选项两处都要写；子选项随父选项**选中 case** 的 `option` 递归合并，不单独登记。「高级调整」关闭时，即使之前填过槽位内容也不会生效。
- case 的 `description` 不显示；option 的 `description` 渲染 Markdown。方案说明因此写在 `CombatScheme` 的描述列表里。
- checkbox 只为勾选的 case 生成 override。所以用「不使用的动作」「不参与轮转的槽位」这种否定写法：不勾选时载体节点的 `attach` 保持为空，也就是沿用默认。
- input 的值按 `pipeline_type` 代入：`string` 经 `JSON.stringify` 转义（出招里的引号、反斜杠不会破坏 JSON）；`int` 的空值会被写成 `0`。所以“留空 = 沿用方案”的数值项一律用 `string`，由 `settings` 解析。
- `verify` 不通过只提示、不阻止运行；简单选项的非法值由 `settings.merge_options` 忽略，新增选项的非法值开战前报错。
- 每个选项、每个 checkbox case 各写自己的载体节点（`enabled: false`，只用来存值），互不覆盖；同一槽位的禁用与内容分属不同载体，按字段合并。
- 需要 MXU >= v2.3.0（`build.py` / `install.yml` 已升级到 v2.3.1）。旧版 MXU 没有该界面，按载体节点默认值运行。

新增全局战斗选项：

1. 在 `CombatSetting.json` 定义 option，并在 `Combat.json` 新增载体节点。
2. 在 `settings.CombatOptions` 加字段，并登记到 `GLOBAL_SETTING_NODES`。
3. 顶层选项写进 `interface.json` 的 `global_option` 与 `setting[].option`；子选项挂在父选项 case 的 `option` 里。
4. 补齐 5 个语言的文案。

`test_assets.py` 会模拟 MXU 用默认值合并一遍，检查结果是否等于 `CombatOptions()`；另外检查高级调整关闭时隐藏输入不生效、开启时按槽位按字段合并，以及 `verify` 正则与解析器一致。

## 方案文件

内置方案在 `assets/resource/base/combat/schemes/`，示例在 `assets/resource/base/combat/examples/`。格式见 [自动战斗](../introduction/AutoCombat.md#自定义方案导入脚本)，校验规则：

- `version` 必须为 1；`combos` 的键只能是 `1`~`4` 或 `default`，所有出招表编译通过。
- `cycle` 模式下 `order` 中每个槽位都要有出招表（或 `default`）；`stay` 模式需要 `default` 或 1~4 全部。
- 文件 ≤ 64 KB，UTF-8（可带 BOM），支持 `//`、`/* */` 注释；未知字段只告警。
- `name`、`team[].character`、`team[].note` 以 `$` 开头时按 agent i18n key 翻译（内置方案用 `$combat.scheme_name.*`）。

内置方案共 9 个，`scheme.BUILTIN_SCHEMES` 的顺序就是设置页下拉顺序。新增内置方案需要同时改 4 处：加文件、在 `BUILTIN_SCHEMES` 登记、在 `CombatScheme` 加 case、补 5 个语言的名称。测试会检查文件与登记一致、case 与登记一致，以及每个方案在 1~4 人队伍下都能得到有效轮转。

自定义方案查找顺序：绝对路径 → `<cwd>/config/combat/`（发布包即安装目录）→ `<项目根>/config/combat/`（开发模式）→ 示例目录。找不到时提示中会列出全部搜索过的目录。`config/` 已被 `.gitignore` 忽略，用户方案不会进入仓库。

## 出招表 DSL

- 用 `ast.parse` 解析，只接受：裸名称、`名称(参数)` 调用、一层逗号序列。其他节点一律报 `unsupported_*`，**没有** `hasattr` 回退，`perform` 之类的内部方法无法被调用。
- 参数只走 `ast.literal_eval`，裸标识符当作字符串（`walk(w, 0.3)`）；全角逗号、括号、冒号自动转半角。
- 方向参数：`walk`、`dodge` 接受 1~2 个方向键（`w`、`wa`、`sd`……），重复键或相反方向（`ws`、`ad`）报 `bad_value`；`turn` 的角度限制在 ±360，超出报 `degrees_out_of_range`。
- `speed(factor)` 是控制指令：改变本场后续输入节奏（0.5~2.0，越界报 `speed_out_of_range`），不发送游戏输入，不计为一次出招。
- 编译错误 `ComboError(code, line, col, *params)` 对应 agent i18n key `combat.dsl.<code>`；方案错误 `SchemeError(code, *params)` 对应 `combat.scheme.<code>`。新增错误码时要补 5 个语言的文案，`tests/combat/test_assets.py` 会检查占位符数量。

执行语义（`executor.py`）：

- 每条指令前都会等待画面可操作；锁敌维护也在这个等待里执行，因此不会打断按住的键。
- 连点类指令（`l_click(秒)`、`r_click(秒)`、补足在场时间）每次点击前都等画面可操作。被遮挡的时间顺延，不计入时长。
- 战斗策略禁用的指令直接跳过；同一动作的原始按键写法也跳过（`keydown(q)` 视为终结技、`keypress(1)`~`(4)` 视为切人，禁用槽位的切人键同理），`keyup` 始终放行。一段出招全被跳过时改为普攻 1 秒，避免空转。
- 敌人消失超过 1 秒时跳过 `ultimate`。
- `ultimate` 之后最多观察 0.4 秒：看到 HUD 消失（终结技动画）就交给下一条指令前的等待处理。
- 一段出招结束或被打断时，释放未配对的 `keydown` / `mousedown`。

## 锁敌与索敌

`targeting.py` 是纯逻辑模块：`Runtime` 每拿到一帧快照就调用一次 `observe`，每次确认可以输入后调用 `maintain`（带防重入）。每张新截图最多做一个动作，动作后必须重新截图再决定下一步。

| 行为 | 条件 | 默认参数 |
|---|---|---|
| 锁定 | 开战时，`lock_target` 开启 | 中键一次 |
| 重锁 | 本次目标丢失阶段连续可判断地看不到敌人 ≥ 0.5 秒 | 每个丢失阶段最多一次；开启转视角时先转再锁 |
| 暂缓终结技 | 敌人消失 ≥ 1 秒或敌人证据未知（不受锁敌开关影响） | 跳过 `ultimate` |
| 转视角索敌 | `search_turn` 开启，敌人消失 ≥ 1 秒 | 每 0.6 秒向右 90°，每次丢失最多 4 次；目标丢失确认 +2 秒 |

丢失计时与 `Monitor` 共用同一段“可判断阴性”时长，遮挡、黑屏、未知帧不计入，也不再用墙钟；看到敌人后重锁与转视角次数清零，打开菜单或出现撤离提示立即停止索敌。不再周期性盲按中键（中键可能是开关，反复按会解除锁定）。转视角用 `post_relative_move`，每度 10 像素（与 Navi 一致），按每步 150 像素拆分发送。控制器不支持时记一次 warning，本场之后不再尝试。

新增指令：`dsl._SPECS` 声明参数 → `ComboExecutor._dispatch` 实现 → 补 `test_dsl.py` 用例与 [自动战斗](../introduction/AutoCombat.md#出招表指令) 指令表。

## 感知与校准

坐标以 1280×720 为基准，按截图尺寸等比缩放。同一帧的所有识别共用同一张截图，同名节点每帧只执行一次。

| 信号 | 节点 / ROI | 判定 | 校准状态 |
|---|---|---|---|
| HUD 可见 | `[620, 654, 95, 42]` | 最大通道 ≥ 175 且饱和度 ≤ 95 的像素 ≥ 10 | 粉爪实机 |
| 当前槽位 | `[1168, 164, 68, 36]`，每槽 y + 88 | 逐槽白色/彩色阈值 + 最低分 + 领先差；二号位用核心区 `[1176, 172, 38, 16]` 兜底 | 粉爪实机 |
| 黑屏 | 全屏采样 | 均值 ≤ 18 且亮像素 ≤ 300 | 粉爪实机 |
| 敌人血条（精确） | `CombatEnemyHealthBar`，`[33, 27, 1222, 639]` | ColorMatch RGB `[242,31,32]`~`[244,33,34]`，≥ 3 个连通像素 | 粉爪实机 |
| 敌人血条（形状） | `CombatEnemyBarShape`，`[126, 0, 1021, 644]` | HSV S ≥ 190、V ≥ 175 且 R 210~255 / G 20~80 / B 20~100；连通块宽 6~100、高 2~6、长宽比 ≥ 2.5、填充率 ≥ 0.7 | ok-NTE 换算，未实机 |
| 敌人等级 | `CombatEnemyLevel`，`[198, 0, 963, 504]` | OCR `Lv.数字` | ok-NTE 区域换算，未实机 |
| Boss 等级 | `CombatBossLevel`，`[458, 15, 182, 26]` | OCR `Lv.数字` | ok-NTE 区域换算，未实机 |
| 撤离确认 | `CombatRetreatPrompt`，`[670, 481, 199, 46]` | OCR「确认撤离」（同粉爪） | 粉爪实机 |

血条精确命中时不再执行等级与 Boss 的 OCR；HUD 不可见时不判断槽位和敌人，但仍检查撤离提示与调用方信号；黑屏时什么都不识别（全部视为未知）。关闭 `assist_detection` 只保留精确血条。`CombatEnemyBarShape` 在 Python 内核里直接用同一实现 `perception.find_health_bars` 计算，不在 custom action 中回调 agent；在 Pipeline 中通过 `combat_enemy_bar` 自定义识别执行。

切人：按槽位键 → 1.2 秒“可操作时间”内等队伍栏高亮到目标槽位；0.5 秒未确认时按新截图补按一次。遮挡与动画时间同时顺延确认窗口和补按计时，避免动画刚结束就补按。失败的槽位本场移出轮转；整个确认窗口都识别不到任何槽位时判定识别失效，降级为站场并提示一次。两次切人至少间隔 0.5 秒，每位角色至少在场 `min_field_time`。

超出 `team_size` 或被禁用的槽位开战前就移出轮转，因此而相邻的同一槽位合并为一轮（`effective_order`，例如主C站场在 2 人队时变为 `[1, 2]`）。轮转模式开战先切到首发；跳过不可用槽位后如果回到当前角色，会继续往后找，不让同一角色连续两轮。

## 测试

```bash
python -m unittest discover -s tests/combat -t .
```

不依赖 maa 与游戏：`tests/combat/_loader.py` 用桩替换 `utils.logger` / `utils.i18n` / `utils.maafocus`。引擎测试用假时钟和假游戏世界，模拟切人延迟、Q 动画隐藏 HUD、敌人出现/消失、撤离提示、黑屏、截图失败、终局信号和相对鼠标移动。

覆盖范围：

- DSL、方案、输入驱动（组合键、冲刺、闪避、转视角）、锁敌逻辑、感知（合成帧：血条形状、多证据、黑屏/HUD 隐藏）。
- 生命周期（`test_lifecycle.py`）：单帧不进战/不结束、等级/Boss/active 维持战斗、未知与黑屏不计时、重复缓存帧只计一次、正向终局与矛盾信号、撤离暂停/取消/确认、所有退出路径松键。
- 有效方案与会话（`test_session.py`）：逐槽覆盖、禁用槽位、首发、每槽出招、节奏缩放与真实秒、9 个方案 × 1~4 人、脚本切人/出招/停止/异常/吞信号/越权。
- 选项合并、`run_combat` / `prepare_combat` 入口、信号节点校验、开战摘要去重。
- 资源一致性：节点引用、注册、方案与下拉一致、设置页默认值与高级调整的合并结果、`verify` 正则、`accepted_reasons`、5 语言文案与占位符、无残留文案 key。

## 已知限制

- 合成图与假时钟只能验证逻辑，**不能证明真实画面的识别率**。仓库里没有战斗截图回归集；血条形状、等级、Boss 节点的参数换算自 ok-NTE，尚未在 MaaNTE 实机校准，误报会让战斗持续到超时，漏报会提前得到 `target_lost`。遇到问题可关闭「辅助识别敌人」退回原有的精确颜色判定。
- 开放世界没有可识别的胜利画面时，内核无法证明击杀，只能报 `target_lost`；需要 `cleared` 的任务应通过 `signal_nodes` 提供自己的胜利/结算识别。
- E/Q 不做可用性识别：只看图标亮度会误判（CD 数字同样是白色），需要 CD OCR。`sent` 只表示控制器接受了输入；冷却中按下无效但无害；DSL 暂无条件分支。
- 当前角色识别依赖右侧队伍栏，被遮挡或非 16:9 时降级为站场。角色名称只是用户标注，不识别头像，也不会自动编队。
- 中键锁敌的实际语义（锁定 / 切换目标 / 开关）未在本项目实机验证；现在每个丢失阶段最多重锁一次，仍可在战斗策略中关闭。
- 转视角的实际角度取决于游戏鼠标灵敏度，只保证大致方向。
- 撤离提示只识别粉爪同款「确认撤离」文字；其他玩法的撤离/离场界面需要调用方用 `signal_nodes` 提供。
- 只在「桌面端-前台」控制器下实测过粉爪同源参数，后台控制器的按键是否生效未验证。
