# 自动战斗引擎

基于 JSON 规则表的自动战斗实现。用户写「什么条件下按什么键」，引擎负责识别状态、选规则、发按键。

- 代码位置：`agent/custom/action/Combat/`
- 自定义动作：`AutoCombat`（`agent/custom/action/auto_combat.py`）
- Pipeline 节点：`AutoCombatMain`（`assets/resource/base/pipeline/Combat/AutoCombat.json`）
- 任务定义：`assets/resource/tasks/AutoCombat.json`
- 内置预设：`assets/resource/base/combat/*.json`
- 离线验证：`tools/verify_combat.py`
- 资产校验：`tools/check_combat_assets.py`

> [!IMPORTANT]
> **使用前提：需要你自己先进入战斗。**
> 本任务只负责「在战斗中按规则出招」，不做寻怪、不做场景跳转、不识别结算画面。
> 进入战斗后启动任务，离开战斗界面超过 3 秒任务自动结束。
>
> 自动寻怪与结算识别尚未实现，原因见文末「未完成的部分」。

## 分层结构

依赖方向严格单向，每层可独立测试。

```
kernel/        底层机制：等待、键鼠、队伍 UI 识别、切人确认状态机
               ↑ 与 pinkpaw 路线共用同一份实现
identity/      角色身份：名录、队伍编成、槽位识别
script/        编排层：规则解析、条件求值、决策、动作翻译
perception.py  画面 → CombatState
runtime.py     会话生命周期：进战 / tick 循环 / 退出 / 安全释放
auto_combat.py CustomAction 入口：参数解析、脚本加载、装配
```

数据流：

```
截图 → perception（CombatState）→ script.engine（Decision）
     → script.primitives（ActionRunner）→ kernel（按键）
```

### 为什么 engine 与 primitives 分开

`engine` 只决定「做什么」（纯函数，返回 `Decision`），`primitives` 只负责「怎么做」（调内核）。两者互不依赖。

好处是决策逻辑的测试完全不需要 mock 内核——构造一个 `CombatState`，断言 `decide()` 的返回值即可。想验证「血量低于 30% 时优先吃药」，不需要游戏、不需要截图。

## 与 pinkpaw 的关系

`kernel/` 是从 `pinkpaw_core3.py` 提取出来的，两边**共用同一份实现**，不是复制。

提取时 pinkpaw 侧只做了「删除 + 转发」：103 个方法的名称与签名零变化，因此 core3 内 200 多处调用点无需改动。

`kernel/constants.py` 里的所有阈值都与提取前逐一对齐。改动这些取值等于改动已上线的粉爪路线，请务必配合 `tools/verify_combat.py` 的第 11 组等价性测试一起验证。

### 机制与策略的分界

`pinkpaw_core3.sleep` 原本在一次等待里轮询了 6 件事。提取时按归属拆开：

| 轮询项 | 归属 | 位置 |
|---|---|---|
| 停止检查 | 机制 | `kernel/waiting.py` |
| 切人异步确认 | 机制 | `kernel/switching.py` |
| 尾部忙等（保时间精度） | 机制 | `kernel/waiting.py` |
| 按键时序敏感判定 | 机制 | `kernel/__init__.py` |
| 顺手捡保险箱 | 策略 | pinkpaw 的 `on_poll_early` 钩子 |
| 还在劫案里吗 | 策略 | pinkpaw 的 `on_poll` 钩子 |
| 移动中交互监听 | 策略 | pinkpaw 的 `on_poll` 钩子 |

调用顺序与提取前逐行一致。这点很重要：切人确认是由 `sleep` 驱动的异步过程，顺序变了会影响确认时序。

## 脚本格式

### 最小示例

```json
{
  "name": "my_combat",
  "fallback": [{"type": "click", "key": "left", "duration": 0.05}],
  "fallback_interval": 0.35
}
```

所有规则都不满足时执行 `fallback`（通常是普攻）。`fallback_interval` 限制它的频率。

### 完整示例

```json
{
  "name": "example",
  "roster": {"1": "mint", "2": "skia", "3": "zero"},
  "rules": [
    {
      "name": "burst",
      "when": {"all": [{"character": "mint"}, {"enemy_visible": true}]},
      "actions": ["e", "wait:0.2", "q"],
      "cooldown": 12.0,
      "priority": 10
    },
    {
      "name": "opener",
      "actions": ["r"],
      "once": true,
      "priority": 5
    }
  ],
  "fallback": ["click:left"],
  "fallback_interval": 0.35
}
```

### 规则字段

| 字段 | 说明 | 默认 |
|---|---|---|
| `name` | 规则名，日志与冷却记账用 | `rule{序号}` |
| `when` / `condition` | 触发条件，省略视为恒真 | `always` |
| `actions` | 动作序列 | 必填，缺则规则被丢弃 |
| `cooldown` | 冷却秒数 | `0` |
| `priority` | 越小越优先 | `100` |
| `once` | 整场战斗只触发一次 | `false` |

同优先级按声明顺序（稳定排序）。

### 动作写法

两种形式等价：

```json
"e"                                          // 点按 E
{"type": "key", "key": "e", "duration": 0.1} // 同上，指定按住时长

"hold:w"        {"type": "hold", "key": "w"}
"release:w"     {"type": "release", "key": "w"}
"click:left"    {"type": "click", "key": "left"}
"wait:0.5"      {"type": "wait", "duration": 0.5}
```

还支持 `mouse_down` / `mouse_up`。

按键必须在内核 `VK` 表里，鼠标键限 `left` / `right` / `middle`。**打错键名会在解析期被拒绝并记录 issue**，而不是运行时静默不发键。

`hold` 没配对 `release` 也没关系——会话结束时（含异常路径）一定会被释放。

### 条件写法

| 条件 | 含义 | 可用性 |
|---|---|---|
| `"always"` / `"never"` | 恒真 / 恒假 | 可用 |
| `{"enemy_visible": true}` | 视野内有敌人 | 可用 |
| `{"enemy_count_at_least": 3}` | 敌人数量下限 | 可用（当前只能区分 0/1）|
| `{"elapsed_above": 3.0}` | 本场战斗已超过 N 秒 | 可用 |
| `{"every": 5.0}` | 每 N 秒满足一次 | 可用 |
| `{"character": "mint"}` | 当前角色是谁 | 需声明 `roster` |
| `{"slot": 0}` | 当前槽位（0-based） | 可用 |
| `{"in_team": true}` | 在队伍可操作界面 | 可用 |
| `{"hp_below": 0.3}` | 自身血量低于 30% | **未实现**，恒为假 |
| `{"boss_hp_below": 0.2}` | Boss 血量 | **未实现**，恒为假 |

组合：`{"all": [...]}`、`{"any": [...]}`、`{"not": {...}}`。数组和多键对象都视为 `all`。

`hp_below` 支持百分数写法（`50` 等价于 `0.5`）。

### 一条重要约定：未知即不满足

识别不到的量用 `None` 表示，条件求值时当作**不满足**。

比如血量识别失败时，`{"hp_below": 0.3}` 返回 `false`，脚本退回兜底普攻，而不是误放大招。

宁可少做一件事，不要做错一件事——这个取向贯穿整个引擎。

## 角色身份

战斗脚本想表达「薄荷在场时放这个技能」，需要知道当前角色是谁。本项目用**槽位识别 + 用户声明编成**：

```
侧栏高亮打分 → 槽位索引 → roster 查表 → 角色键名
（复用 pinkpaw    （已验证）   （用户声明）
  已验证的实现）
```

`roster` 两种写法等价：

```json
"roster": {"1": "mint", "3": "zero"}      // 键是键盘按键号（1-based）
"roster": ["mint", null, "zero", null]    // 按槽位顺序（0-based）
```

角色名接受英文键名（`mint`）、中文名（`薄荷`）或立绘文件名（`Mint.png`），大小写无关。名录见 `identity/catalog.py`，与已上线的 `SyncCharacterAbilityCityAbility` 共享同一份数据。

### 为什么不做头像自动认人

仓库里的角色图只有 `Character_UI/Character_Pic/*.png`，那是 200×210 的**角色详情页立绘**（用在 ROI `[390,80,200,210]`）。战斗侧栏槽位是 68×36，两者尺寸差约 5.8 倍，且可见内容不同：立绘是半身构图，侧栏是头部特写带边框。缩放后匹配不可靠。

`PortraitIdentifier` 保留为未启用的扩展点：结构与 ROI 计算都在，但没有侧栏模板时 `available` 为 `False`，识别一律返回未知。它**不会**退化成「用立绘凑合匹配」——错误的角色身份比没有身份更危险，因为脚本会按错误前提放技能，而用户很难从行为反推出「识别错了」。

若将来补齐侧栏模板（放到 `Character_UI/Team_Slot/`，文件名沿用立绘名），该类会自动发现并启用，无需改调用方。

## 感知层的可靠性

`perception.py` 只使用**已在真机验证过的识别方式**，不自己发明阈值。

| 信号 | 来源 | 可靠性 |
|---|---|---|
| 敌人可见 | pinkpaw `PinkPawHeist_CheckMonsterOnce` 红色血条签名 | 已上线验证 |
| 在队伍界面 | 内核 `team.is_in_team` | 已上线验证 |
| 黑屏 / 加载 | 内核 `team.is_black_screen` | 已上线验证 |
| 当前槽位 | 内核 `team.current_slot_index` | 已上线验证 |
| ESC 菜单打开 | 场景管理器公共节点 `InEscMenu` | 已上线验证 |
| 是否在大世界 | 场景管理器公共节点 `InWorld` | 已上线验证 |
| 角色身份 | 槽位 + `roster` | 取决于用户声明是否正确 |
| 自身 / Boss 血量 | **未实现** | 恒为 `None` |

### 为什么血量没实现

读血量需要知道血条的 ROI 与颜色区间。仓库里没有玩家血条 / Boss 血条的识别节点，pinkpaw 也不读血量（它只用「有没有红色血条」判断有没有怪）。

在没有游戏环境标定 ROI 的情况下凭空写一个阈值，等于制造一个看起来能用、实际会误判的功能。所以这里保持 `None`。

### 扩展感知

补一个新信号的步骤：

1. 先在游戏里标定 ROI 与颜色/模板阈值，加一个 pipeline 识别节点；
2. 在 `perception.py` 的 `observe()` 里填充对应 `CombatState` 字段；
3. 若该信号原本在 `check_combat_assets.py` 的 `UNIMPLEMENTED_SIGNALS` 里，把它移除；
4. 在 `tools/verify_combat.py` 第 8 组补测试，覆盖「识别成功 / 失败 / 异常」三条路径。

第 3 步别忘：那份清单是用来提醒用户「你配的条件其实不生效」的。

## 会话时序

一个 tick 的顺序固定，改动会影响行为：

1. 检查停止 / 超时
2. 截图 → `Perception.observe` → `CombatState`
3. `CombatEngine.decide` → `Decision`
4. `ActionRunner.run` 执行动作
5. `engine.commit` 记账
6. 间隔等待（走内核可中断 `sleep`，期间仍响应停止）

### 第 5 步为什么在第 4 步之后

记账放在执行**之后**，是为了避免「决定了但没执行成功」却白占冷却。

这个顺序有测试守护（第 9 组的 `ExplodingRunner` 用例）：动作执行抛异常时，断言冷却账本没有被写入。

### 脱战判定

连续离开队伍界面超过 `leave_grace`（默认 3 秒）才算结束。黑屏视为过渡态，不计入——否则场景切换的加载黑屏会被误判成战斗结束。

### 会话结束条件

按优先级：超时 / tick 上限 → 脱战 → 清怪 → ESC 菜单打开够久。

`stop_when_no_enemy`（默认**关**）是为刷本加的：副本里打完怪队伍 UI 还在，脱战判据永远不成立，不看血条就只能干等满 `duration`。开启后，见过敌人之后血条连续消失 `no_enemy_grace`（默认 4 秒）就结束会话，`reason` 为 `no_enemy`。

两个约束值得注意：

- **必须先见过敌人**才生效。否则开局那几帧还没识别到血条就会被判成打完了，战斗直接空转结束。
- **宽限期不能太短**。血条会因为镜头转动、技能特效遮挡短暂消失，判太快会在敌人还活着时收兵。

默认关闭的理由：普通「自动战斗」任务里用户可能只是想让脚本挂着出招，不该替他决定何时收手。刷本入口（`DungeonFarm`）默认打开。回归断言见 `verify_combat.py` 第 9 组「清怪收兵」四条用例（会收、开局不误收、闪断不误判、默认关闭时行为不变）。

### 启动前场景守卫

启动时先探几帧：只有确认处于**大世界且完全看不到敌人**时才拒绝运行，并在日志里说明原因。

本任务只负责战斗中出招，在空地上跑只会对着空气乱按技能键。与其让用户看着角色抽风然后自己猜哪里错了，不如直接拒绝并告诉他「请先进入战斗」。

判据由两部分组成，**必须同时成立**才拒绝：

| 信号 | 节点 | 含义 |
|---|---|---|
| 在大世界 | `InWorld` | ESC 手机按钮 + 任务菜单按钮同时存在 |
| 看不到敌人 | `PinkPawHeist_CheckMonsterOnce` | 画面中没有红色敌人血条 |

探测窗口为 `guard_probes` 帧（默认 6，间隔 `guard_probe_interval` 默认 0.12 秒）。窗口内**任何一帧**出现敌人、或任何一帧不在大世界，都立即放行。截图失败同样放行。

> **不能只看 `InWorld`。** 这是这道守卫最初的致命 bug：NTE 的野外战斗就发生在大世界里，战斗中 ESC 手机按钮与任务菜单按钮同样存在，`InWorld` 必然命中。只凭它拒绝运行会把**每一次**合法战斗都挡掉，用户侧的表现就是「自动战斗完全没反应」——任务秒退、日志只有一句「当前处于大世界」、一个键都没发出去。
>
> 敌人血条是仓库里唯一已验证的直接战斗信号，因此用「缺少战斗证据」代替「在大世界」作为拒绝依据。回归断言见 `verify_combat.py` 第 9 组「大世界中有敌人 -> 守卫放行」。

`guard_open_world` 可关（界面开关「启动前检查是否在战斗中」），留给想在非战斗场景跑自定义脚本的用户。

### 预设目录：两种布局都要支持

内置预设按以下顺序查找，命中即用：

```
<cwd>/assets/resource/base/combat      # 开发仓库
<cwd>/resource/base/combat             # 发布包
<cwd>/../…                             # dev 模式（main.py 会把 cwd 切到 assets/）
<项目根>/…                              # 兜底
```

> **发布包没有 `assets/` 这一层。** 打包时资源被放到 `resource/base/combat`，而早期实现只认 `assets/resource/base/combat`，导致发布版每次都报「预设不存在（可用预设: 无）」并立即失败——三个内置预设一个都加载不了。
>
> 更麻烦的是所有测试都在开发仓库布局下运行，1051 项断言全绿也测不出这个问题。因此 `verify_combat.py` 增加了第 12 组，用临时目录分别搭出发布包布局、开发布局和 `cwd=assets` 三种情况来验证路径解析。新增内置资源时请一并确认这两种布局都能找到。

### ESC 菜单保护

检测到 ESC 菜单打开时，**立刻停止发键并松开已按住的键**，不等宽限期。

理由：菜单开着按键会点到菜单项上，有误触发「退出副本」之类操作的风险；而如果角色正按住 W，菜单里也可能仍在移动。

宽限期（`menu_grace`，默认 1 秒）只决定「要不要结束会话」——手滑开一下菜单不该中断整场战斗。把 `stop_when_menu_open` 关掉可以让它只暂停不结束。

菜单判定用的是场景管理器公共节点 `InEscMenu`（`Interface/Scene/Status.json`），是已验证的 OCR 识别，不是新写的。

菜单检测异常时**视为未打开**——与敌人检测的降级方向相反。误判菜单已开会让战斗白白停下，而漏报一拍最多多按几下键，下次检测就会发现。

### 安全保证

无论正常结束、异常、还是 tasker 停止，`run()` 都会在 `finally` 里释放所有按住的键。

战斗中最糟的 bug 是角色卡住一直走或一直按着攻击键。释放逻辑逐个 `try`，一个键失败不连累其它键。

## 内置预设

| 预设 | 说明 |
|---|---|
| `basic_attack` | 只做普攻。不依赖任何未验证的识别，任何角色任何场景都能用 |
| `skill_rotation` | 通用技能循环：见敌按 E / Q，冷却期普攻，周期性闪避 |
| `team_rotation` | 按队伍编成分角色出招的**示例**，`roster` 必须改成自己的实际站位 |

预设里的冷却时间是保守估计，不针对特定角色，请按自己的实际技能 CD 调整。

## 参数

`custom_action_param` 支持：

| 参数 | 说明 | 默认 |
|---|---|---|
| `script` | 内联脚本对象 | — |
| `preset` | 内置预设名 | — |
| `script_path` | 自定义脚本路径 | — |
| `duration` | 最长运行秒数 | `60` |
| `tick_interval` | tick 间隔秒数 | `0.05` |
| `stop_when_not_in_team` | 脱离队伍界面即结束 | `true` |
| `stop_when_menu_open` | ESC 菜单打开时结束（关掉则只暂停） | `true` |
| `guard_open_world` | 启动时若在大世界**且看不到敌人**则拒绝运行 | `true` |
| `guard_probes` | 守卫探测帧数 | `6` |
| `guard_probe_interval` | 守卫探测间隔秒数 | `0.12` |
| `max_ticks` | tick 数上限，0 为不限 | `0` |
| `log_decisions` | 打印每次规则触发 | `false` |
| `timing_scale` | 等待时长微调倍率 | `1.0` |
| `direct_input` | 用 SendInput 直发按键 | `true` |

脚本来源优先级：`script` > `preset` > `script_path`。

**三者都没给会直接返回失败**，不提供隐式默认脚本——让用户明确知道自己在跑什么，而不是被一个来源不明的动作序列驱动。

## 验证

```bash
python tools/verify_combat.py        # 离线逻辑验证
python tools/check_combat_assets.py  # 预设与资产校验
```

### 测试分组

| 组 | 内容 |
|---|---|
| 1-2 | 脚本解析：合法输入 + 21 类非法输入 |
| 3 | 条件求值：边界、未知量、嵌套组合 |
| 4 | 决策引擎：优先级、冷却、`once`、兜底节流 |
| 5 | 动作执行：调用映射、释放保证（含释放失败场景）|
| 6 | 角色名录：与 `SyncCharacterAbilityCityAbility` 交叉校验 |
| 7 | 身份识别的能力边界 |
| 8 | 感知：状态构建与降级 |
| 9 | 会话闭环仿真：tick 时序、脱战、异常安全、启动守卫（含大世界战斗必须放行）|
| 10 | 入口：脚本来源解析与拒绝策略 |
| 11 / 11b / 11c | 内核等价性：与提取前的 core3 逐项比对 |
| 12 | 预设目录解析：发布包布局 / 开发布局 / `cwd=assets` |

`check_combat_assets.py` 的 6 组：预设解析、locale 五语言齐全、pipeline 节点引用、CustomAction 注册、任务接线自洽（option 声明/group 名/预设引用）、locale 行尾。

### 关于第 11 组

`tools/fixtures/pinkpaw_core3_baseline.py` 是内核提取**之前**的 core3 完整快照。第 11 组用它做「原实现 vs 新内核」的逐项比对。

这个快照**不要删、不要跟着 core3 一起改**。它的价值就在于冻结提取前的行为。如果哪天真要调整内核阈值，应当先让等价测试失败，由人确认该差异是预期的，再连同基线一起更新并在 PR 里说明原因。

第 11c 组更进一步：把原实现的切人状态机也接到同一个假环境上，逐条比对事件流。

### 变异测试

改完内核或引擎后，建议手动植入几个缺陷，确认测试真的会失败。已验证能被捕获的：

- 内核：核心打分权重偏移
- 引擎：选最低优先级、忽略冷却、血量未知误判为真、按键白名单失效
- 身份：头像缺失时瞎猜、未配置 roster 时返回默认角色
- 运行时：异常时不释放按键、`commit` 提前、黑屏误判脱战、节流失效

「文档承诺了但测试没守护」的地方最容易在重构中被悄悄破坏。`commit` 顺序那条就是这样被发现的——文档写了承诺，但最初没有对应测试。

## Pipeline 接线

入口节点是**纯 `action: Custom`，不含任何识别**：

```json
"AutoCombatMain": {
    "action": "Custom",
    "custom_action": "AutoCombat",
    "custom_action_param": {"preset": "basic_attack"}
}
```

这与 `SoundDodgeMain`、`RealTimeTaskMain`、`TetrisEntrance` 等既有任务同构——它们都是「用户已就位，直接把控制权交给 Python」的模式，节点本身不携带界面信息。

界面选项（`assets/resource/tasks/AutoCombat.json`）：

| 选项 | 类型 | 默认 |
|---|---|---|
| 战斗预设 | select | 仅普攻 |
| 最长运行时间（秒） | input | 60 |
| 脱离战斗界面时结束 | switch | 开 |
| 输出规则触发日志 | switch | 关 |

任务归到 `RealTimeAssist` 分组，与实时辅助、音频闪避并列——都是「玩家在玩，脚本辅助」而非全自动托管的任务。

## 未完成的部分

### 自动寻怪与结算识别

`AutoCombat` 本身不做场景导航，需要用户自己进入战斗。

需要「一键托管」的场景请看 [刷本](./dungeon-farm.md)：它在 `AutoCombat` 外面套了一层
配置驱动的流程（传送 → 进副本 → 前进开战 → 战斗 → 找出口领奖 → 循环），
战斗部分直接复用这里的脚本加载与 `CombatSession`。

刷本也带来了本引擎的一处扩展：`SessionConfig.stop_when_no_enemy`（见「会话结束条件」）。
副本里打完怪队伍 UI 还在，脱战判据永远不成立，不看血条就只能每轮干等满 `duration`。
该开关默认关闭，普通自动战斗行为不变。

`AutoCombat` 自己仍然缺三项界面信息才能独立托管：

1. **战斗入口**：从哪个界面、点什么进入战斗？
2. **战斗结束判定**：结算画面的 ROI 与模板/文案是什么？
3. **异常处理**：战斗失败、角色阵亡、意外弹窗分别是什么画面？

这三项都需要游戏界面截图与跳转逻辑。[编码规范](./coding-standards.md) 明确禁止在缺少这些信息的情况下编写识别节点：

> 禁止在未向 AI 提供游戏界面截图、界面跳转逻辑等上下文的情况下，让 AI 直接编写 Pipeline。缺乏界面信息的 AI 只能依赖幻觉和项目已有代码拼凑，产出代码质量极低。

所以这部分留给掌握界面信息的开发者。补的时候：把识别节点加进 `pipeline/Combat/AutoCombat.json`，用 `next` 串起「进战 → AutoCombatMain → 结算」，任务 `entry` 改成新的入口节点。`check_combat_assets.py` 会自动校验新节点的引用有效性。

### 其它待补

| 项 | 说明 |
|---|---|
| 血量感知 | 见「为什么血量没实现」 |
| 敌人计数 | 当前只能区分「有 / 没有」，`enemy_count_at_least` 大于 1 时恒假 |
| 队伍切换 | 内核的 `CharacterSwitcher` 可用，但战斗引擎 v1 没接（脚本暂时只操作当前角色）|
| 侧栏头像认人 | 见「为什么不做头像自动认人」 |
| 真机回归 | 全部验证都是离线的。内核提取改动了 pinkpaw 的等待轮询结构，合并前需实机跑一次粉爪 |
