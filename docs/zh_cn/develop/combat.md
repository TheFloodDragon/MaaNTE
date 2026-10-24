# 战斗系统

战斗内核位于 `agent/custom/action/Combat/`，负责「进战 → 出招/切人 → 脱战判定」。走到怪物面前、战后领奖、处理弹窗等流程由调用方 Pipeline 负责。

用户侧说明见 [自动战斗](../introduction/AutoCombat.md)，配队与后续能力规划见 [自动配队计划](./combat-auto-team-plan.md)。

## 设计来源

| 来源 | 采用的部分 |
|---|---|
| ok-NTE（BnanZ0/ok-nte main） | 出招表 DSL 语法（兼容子集）、「每个角色跑一段出招再切人」的轮转模型、默认键位、Q 动画期间 HUD 消失的处理、进场先按 R、中键锁敌与丢失后重锁、冲刺（按住 W 点 Shift）与带方向闪避 |
| `pinkpaw/pinkpaw_core3.py`（已实机校准） | HUD 斜杠检测、当前角色高亮、黑屏判定的 ROI 与阈值，切人确认 + 窗口内补按一次 |
| `PinkPawHeist/tools.json` | 敌人血条 ColorMatch 参数（复制为 `CombatEnemyVisible`，不再引用粉爪节点） |

## 模块

| 文件 | 职责 |
|---|---|
| `api.py` | 对外入口 `run_combat`，组装运行时，`PrintReporter` 输出 i18n 用户消息 |
| `actions.py` | MaaFramework 注册：`combat_run`（动作）、`combat_in_hud`（识别） |
| `settings.py` | 选项定义与合并（`CombatOptions`、载体节点读取） |
| `locate.py` / `scheme.py` | 方案文件查找、解析、校验 |
| `dsl.py` | 出招表编译器 |
| `engine.py` | 等待进战、轮转、切人确认、降级、队伍人数过滤 |
| `executor.py` | 指令 → 输入原语，HUD 门控，禁用动作过滤 |
| `targeting.py` | 锁敌、丢失重锁、暂缓终结技、可选的转视角索敌（纯逻辑） |
| `runtime.py` | 时钟、停止检查、节流快照、可中断等待、脱战判定 |
| `perception.py` | 纯 numpy 的 HUD / 当前槽位 / 黑屏判定 |
| `inputs.py` | 只通过控制器发键，记账并保证释放 |
| `model.py` / `keys.py` | 共用类型、结束原因、键位表 |

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

`Combat.json` 中的 `CombatRun` 是同样配置的现成节点，可直接 `context.run_task("CombatRun")`。

### Pipeline 门控

- `CombatEnemyVisible`：画面里有敌人血条（ColorMatch）。
- `CombatInHud`：处于可操作界面（底部血条可见且非黑屏），`detail.slot` 为当前槽位。
- `CombatFightWhenEnemy`：两者都满足时开打，打完回到父节点。适合挂在跑图流程的 `next` 中：

```jsonc
"next": ["MyTaskArrived", "[JumpBack]CombatFightWhenEnemy", "MyTaskKeepWalking"]
```

### Python

```python
from custom.action.Combat import run_combat

result = run_combat(context, overrides={"max_duration": 120, "scheme": "solo_skill", "use_ultimate": False})
if not result.success:
    ...  # result.reason 见下文「结束原因」
```

签名：`run_combat(context, overrides=None, option_nodes=(), reporter=None) -> CombatResult`。`reporter` 为 `None` 时用 `PrintReporter` 推送 MXU 消息；传入自定义 `Reporter` 子类可接管消息。

## 选项

| 参数 | 默认 | 说明 |
|---|---|---|
| `scheme` | `team_rotation` | 内置方案名或 `custom` |
| `custom_scheme` | 空 | `scheme=custom` 时的方案文件名或路径 |
| `wait_timeout` | 30 | 等待进战秒数，0 = 一直等 |
| `max_duration` | 300 | 单场最长秒数，0 = 不限 |
| `clear_confirm` | 3 | 敌人血条持续消失多久判定脱战（HUD 不可见期间不计时） |
| `hud_lost_timeout` | 10 | 战斗中 HUD 连续消失多久结束本场，0 = 不判断 |
| `team_size` | 4 | 队伍人数 1~4，超出人数的槽位不参与切人，出招表里切到这些槽位的 `switch(n)` 直接跳过 |
| `lock_target` | true | 开战中键锁敌，敌人血条消失后重新锁定 |
| `search_turn` | false | 敌人丢失时转视角寻找（仅前台 Seize 鼠标），开启后脱战确认额外 +2 秒 |
| `use_ultimate` / `use_skill` / `use_arc` / `use_dodge` / `use_jump` / `use_switch` | true | false 时跳过出招表中对应的指令；`use_switch=false` 时轮转方案改为站场 |
| `option_nodes` | 空 | 调用方自己的载体节点列表，读取它们的 `attach` 作为覆盖 |

合并优先级（后者覆盖前者）：内置默认 < 全局战斗配置载体节点 < `custom_action_param` < `option_nodes`。`None` 与空字符串视为未设置；非法值保留上一层的值并写 warning 日志，不中断战斗。布尔值接受 `true/false/yes/no/on/off/1/0`，`team_size` 必须是 1~4 的整数。可禁用的动作名见 `TOGGLE_ACTIONS`。

任务选项请**每个选项写自己的载体节点 `attach`**，不要多个选项同时覆盖同一个节点的 `custom_action_param`（同名字段会整体替换，只有最后一个生效）。参考 `AutoCombat.json` 的 `AutoCombat_*Option`。

## 结束原因

| `reason` | `success` | 含义 |
|---|---|---|
| `cleared` | ✔ | 敌人血条持续消失 `clear_confirm` 秒 |
| `timeout` | ✔ | 超过 `max_duration` |
| `hud_lost` | ✔ | 战斗中长时间看不到 HUD（阵亡、剧情、弹窗），画面交还调用方处理 |
| `no_combat` | ✘ | `wait_timeout` 内没有进战 |
| `stopped` | ✘ | 用户停止任务 |
| `scheme_error` | ✘ | 方案加载或校验失败 |
| `error` | ✘ | 内部异常（日志有堆栈） |

所有路径都会在 finally 中释放按住的键和鼠标。

## 全局战斗配置

MXU「设置 → 任务设置」下有两个分区，选项都定义在 `assets/resource/tasks/CombatSetting.json`：

| 分区 | 选项 | 类型 | 载体节点 → `attach` |
|---|---|---|---|
| 战斗配置 | `CombatScheme` | select（9 个内置方案 + 自定义） | `CombatSetting_Scheme` → `scheme` |
| 战斗配置 | └ `CombatCustomScheme`（仅「自定义」时显示） | input | `CombatSetting_CustomScheme` → `custom_scheme` |
| 战斗配置 | `CombatTeamSize` | select（1~4） | `CombatSetting_TeamSize` → `team_size` |
| 战斗策略 | `CombatDisabledActions` | checkbox（不使用的动作） | 每个 case 一个：`CombatSetting_Disable<Action>` → `use_<action>: false` |
| 战斗策略 | `CombatLockTarget` | switch | `CombatSetting_LockTarget` → `lock_target` |
| 战斗策略 | `CombatSearchTurn` | switch | `CombatSetting_SearchTurn` → `search_turn` |
| 战斗策略 | `CombatClearConfirm` | input（int，1~60） | `CombatSetting_ClearConfirm` → `clear_confirm` |

已对照 MXU v2.3.1 源码（`OptionEditor.tsx`、`pipelineOverride.ts`）确认的行为：

- 运行时只合并 `global_option` 里的选项（优先级最低，对所有任务生效），`setting` 只负责显示。所以 6 个顶层选项两处都要写；子选项随父选项的 case 合并，不单独登记。
- case 的 `description` 不显示；option 的 `description` 渲染 Markdown。方案说明因此写在 `CombatScheme` 的描述列表里。
- checkbox 只为勾选的 case 生成 override。所以用「不使用的动作」这种否定写法：不勾选时载体节点的 `attach` 保持为空，也就是全部启用。客户端读不到这个选项时同样是全部启用。
- `pipeline_type: int` 渲染为数字框；`verify` 不通过只提示、不阻止运行，非法值由 `settings.merge_options` 忽略并保留默认值。
- 每个选项、每个 checkbox case 各写自己的载体节点（`enabled: false`，只用来存值），互不覆盖。
- 需要 MXU >= v2.3.0（`build.py` / `install.yml` 已升级到 v2.3.1）。旧版 MXU 没有该界面，按载体节点默认值运行。
- 本地 `deps/tools/interface.schema.json` 尚未同步 `setting` 字段（PI v2.8.0 的 MXU 扩展），VS Code 会提示该字段未知，属预期。

新增全局战斗选项：

1. 在 `CombatSetting.json` 定义 option，并在 `Combat.json` 新增载体节点。
2. 在 `settings.CombatOptions` 加字段，并登记到 `GLOBAL_SETTING_NODES`。
3. 在 `interface.json` 的 `global_option` 与 `setting[].option` 中加入。
4. 补齐 5 个语言的文案。

`test_assets.py` 会模拟 MXU 用默认值合并一遍，检查结果是否等于 `CombatOptions()`。

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
- 编译错误 `ComboError(code, line, col, *params)` 对应 agent i18n key `combat.dsl.<code>`；方案错误 `SchemeError(code, *params)` 对应 `combat.scheme.<code>`。新增错误码时要补 5 个语言的文案，`tests/combat/test_assets.py` 会检查占位符数量。

执行语义（`executor.py`）：

- 每条指令前都会等待 HUD 可见；锁敌维护也在这个等待里执行，因此不会打断按住的键。
- 连点类指令（`l_click(秒)`、`r_click(秒)`、补足在场时间）每次点击前都等 HUD。被遮挡的时间顺延，不计入时长。
- 战斗策略禁用的指令直接跳过。一段出招全被跳过时改为普攻 1 秒，避免空转。
- 敌人消失超过 1 秒时跳过 `ultimate`。
- `ultimate` 之后最多观察 0.4 秒：看到 HUD 消失（终结技动画）就交给下一条指令前的等待处理。
- 一段出招结束或被打断时，释放未配对的 `keydown` / `mousedown`。

## 锁敌与索敌

`targeting.py` 是纯逻辑模块：`Runtime` 每拿到一帧快照就调用一次 `observe`，每次确认 HUD 可见后调用 `maintain`（带防重入）。

| 行为 | 条件 | 默认参数 |
|---|---|---|
| 锁定 | 开战时，`lock_target` 开启 | 中键一次 |
| 重锁 | 敌人血条连续消失 ≥ 0.5 秒 | 每 0.6 秒中键一次 |
| 暂缓终结技 | 敌人消失 ≥ 1 秒（不受锁敌开关影响） | 跳过 `ultimate` |
| 转视角索敌 | `search_turn` 开启，敌人消失 ≥ 1 秒 | 每 0.6 秒向右 90°，每次丢失最多 4 次；脱战确认 +2 秒 |

丢失计时只看 HUD 可见的帧，识别失败（`enemy=None`）不改变状态。转视角用 `post_relative_move`，每度 10 像素（与 Navi 一致），按每步 150 像素拆分发送。控制器不支持时记一次 warning，本场之后不再尝试。

新增指令：`dsl._SPECS` 声明参数 → `ComboExecutor._dispatch` 实现 → 补 `test_dsl.py` 用例与 [自动战斗](../introduction/AutoCombat.md#出招表指令) 指令表。

## 感知与校准

坐标以 1280×720 为基准，按截图尺寸等比缩放。

| 信号 | ROI | 判定 |
|---|---|---|
| HUD 可见 | `[620, 654, 95, 42]` | 最大通道 ≥ 175 且饱和度 ≤ 95 的像素 ≥ 10 |
| 当前槽位 | `[1168, 164, 68, 36]`，每槽 y + 88 | 逐槽白色/彩色阈值 + 最低分 + 领先差；二号位用核心区 `[1176, 172, 38, 16]` 兜底 |
| 黑屏 | 全屏采样 | 均值 ≤ 18 且亮像素 ≤ 300 |
| 敌人血条 | `[33, 27, 1222, 639]` | ColorMatch RGB `[242,31,32]`~`[244,33,34]`，≥ 3 个连通像素 |

切人：按槽位键 → 1.2 秒内等队伍栏高亮到目标槽位，0.5 秒未确认补按一次，黑屏顺延。失败的槽位本场移出轮转；整个确认窗口都识别不到任何槽位时判定识别失效，降级为站场并提示一次。两次切人至少间隔 0.5 秒，每位角色至少在场 `min_field_time`。

超出 `team_size` 的槽位开战前就移出轮转，因此而相邻的同一槽位合并为一轮（`effective_order`，例如主C站场在 2 人队时变为 `[1, 2]`）。跳过不可用槽位后如果回到当前角色，会继续往后找，不让同一角色连续两轮。

## 测试

```bash
python -m unittest discover -s tests/combat -t .
```

不依赖 maa 与游戏：`tests/combat/_loader.py` 用桩替换 `utils.logger` / `utils.i18n` / `utils.maafocus`。引擎测试用假时钟和假游戏世界，模拟切人延迟、Q 动画隐藏 HUD、敌人出现/消失、相对鼠标移动。

覆盖范围：

- DSL、方案、输入驱动（组合键、冲刺、闪避、转视角）、锁敌逻辑、感知（合成帧）。
- 引擎场景：轮转、战斗策略、HUD 门控、锁敌集成。
- 选项合并、`run_combat` 入口。
- 资源一致性：节点引用、注册、方案与下拉一致、设置页默认值的合并结果、5 语言文案与占位符、无残留文案 key。

## 已知限制

- E/Q 不做可用性识别：只看图标亮度会误判（CD 数字同样是白色），需要 CD OCR。冷却中按下无效但无害；DSL 暂无条件分支。
- 敌人检测是纯血条颜色，敌人在视野外会被当作脱战（靠 `clear_confirm` 去抖、重锁、可选的转视角缓解）；云游戏压缩画面可能识别不到。
- 当前角色识别依赖右侧队伍栏，被遮挡或非 16:9 时降级为站场。
- 中键锁敌的实际语义（锁定 / 切换目标 / 开关）未在本项目实机验证。如果实际是开关，重锁可能把锁定关掉，可在战斗策略中关闭。
- 转视角的实际角度取决于游戏鼠标灵敏度，只保证大致方向。
- 只在「桌面端-前台」控制器下实测过粉爪同源参数，后台控制器的按键是否生效未验证。
