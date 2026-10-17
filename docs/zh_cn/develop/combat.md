# 战斗系统

战斗内核位于 `agent/custom/action/Combat/`，负责「进战 → 出招/切人 → 脱战判定」。走到怪物面前、战后领奖、处理弹窗等流程由调用方 Pipeline 负责。

用户侧说明见 [自动战斗](../introduction/AutoCombat.md)，配队与后续能力规划见 [自动配队计划](./combat-auto-team-plan.md)。

## 设计来源

| 来源 | 采用的部分 |
|---|---|
| ok-NTE（BnanZ0/ok-nte main） | 出招表 DSL 语法（兼容子集）、「每个角色跑一段出招再切人」的轮转模型、默认键位、Q 动画期间 HUD 消失的处理 |
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
| `engine.py` | 等待进战、轮转、切人确认、降级 |
| `executor.py` | 指令 → 输入原语 |
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

result = run_combat(context, overrides={"max_duration": 120, "scheme": "solo_skill"})
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
| `option_nodes` | 空 | 调用方自己的载体节点列表，读取它们的 `attach` 作为覆盖 |

合并优先级（后者覆盖前者）：内置默认 < 全局战斗配置载体节点 < `custom_action_param` < `option_nodes`。`None` 与空字符串视为未设置；非法值保留上一层的值并写 warning 日志，不中断战斗。

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

- `assets/resource/tasks/CombatSetting.json` 只定义 option：`CombatScheme`（select）及其子选项 `CombatCustomScheme`（input）。
- `assets/interface.json` 的 `global_option` 让 `CombatScheme` 参与所有任务的 pipeline override（优先级最低）；`setting` 把它渲染到 MXU「设置 → 任务设置 → 战斗配置」。需要 MXU >= v2.3.0（`build.py` / `install.yml` 已升级到 v2.3.1）。旧版 MXU 没有该界面，按默认值 `team_rotation` 运行。
- 选项值写入载体节点 `CombatSetting_Scheme`、`CombatSetting_CustomScheme` 的 `attach`，节点 `enabled: false`，只用来存值。
- 本地 `deps/tools/interface.schema.json` 尚未同步 `setting` 字段（PI v2.8.0 的 MXU 扩展），VS Code 会提示该字段未知，属预期。

新增全局战斗选项：在 `CombatSetting.json` 定义 option → `Combat.json` 新增载体节点 → `settings.GLOBAL_SETTING_NODES` 登记 → `interface.json` 的 `setting[].option` 与 `global_option` 加入（子选项只需挂在父 case 的 `option` 下）。

## 方案文件

内置方案在 `assets/resource/base/combat/schemes/`，示例在 `assets/resource/base/combat/examples/`。格式见 [自动战斗](../introduction/AutoCombat.md#自定义方案导入脚本)，校验规则：

- `version` 必须为 1；`combos` 的键只能是 `1`~`4` 或 `default`，所有出招表编译通过。
- `cycle` 模式下 `order` 中每个槽位都要有出招表（或 `default`）；`stay` 模式需要 `default` 或 1~4 全部。
- 文件 ≤ 64 KB，UTF-8（可带 BOM），支持 `//`、`/* */` 注释；未知字段只告警。
- `name`、`team[].character`、`team[].note` 以 `$` 开头时按 agent i18n key 翻译（内置方案用 `$combat.scheme_name.*`）。

自定义方案查找顺序：绝对路径 → `<cwd>/config/combat/`（发布包即安装目录）→ `<项目根>/config/combat/`（开发模式）→ 示例目录。找不到时提示中会列出全部搜索过的目录。`config/` 已被 `.gitignore` 忽略，用户方案不会进入仓库。

## 出招表 DSL

- 用 `ast.parse` 解析，只接受：裸名称、`名称(参数)` 调用、一层逗号序列。其他节点一律报 `unsupported_*`，**没有** `hasattr` 回退，`perform` 之类的内部方法无法被调用。
- 参数只走 `ast.literal_eval`，裸标识符当作字符串（`walk(w, 0.3)`）；全角逗号、括号、冒号自动转半角。
- 编译错误 `ComboError(code, line, col, *params)` 对应 agent i18n key `combat.dsl.<code>`；方案错误 `SchemeError(code, *params)` 对应 `combat.scheme.<code>`。新增错误码时要补 5 个语言的文案，`tests/combat/test_assets.py` 会检查占位符数量。

执行语义（`executor.py`）：每条指令前等待 HUD 可见；`ultimate` 后最多观察 0.4 秒，看到 HUD 消失（终结技动画）就交给下一条指令前的等待；一段出招结束或被打断时释放未配对的 `keydown`/`mousedown`。

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

## 测试

```bash
python -m unittest discover -s tests/combat -t .
```

不依赖 maa 与游戏：`tests/combat/_loader.py` 用桩替换 `utils.logger` / `utils.i18n` / `utils.maafocus`，引擎测试用假时钟和假游戏世界（切人延迟、Q 动画隐藏 HUD、敌人出现/消失）。覆盖 DSL、方案、感知（合成帧）、引擎场景、选项合并、`run_combat` 入口，以及资源一致性（节点引用、注册、5 语言文案与占位符）。

## 已知限制

- E/Q 不做可用性识别，冷却中按下无效但无害；DSL 暂无条件分支。
- 敌人检测是纯血条颜色，敌人在视野外会被当作脱战（靠 `clear_confirm` 去抖缓解）；云游戏压缩画面可能识别不到。
- 当前角色识别依赖右侧队伍栏，被遮挡或非 16:9 时降级为站场。
- 只在「桌面端-前台」控制器下实测过粉爪同源参数，后台控制器的按键是否生效未验证。
