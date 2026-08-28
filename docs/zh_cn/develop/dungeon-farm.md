# 刷本

按配置自动循环刷副本：传送到入口 → 前进进入 → 选副本 → 前进开战 → 自动战斗 → 打完找出口领奖 → 再来一轮。

- 代码位置：`agent/custom/action/Dungeon/`
- 自定义动作：`DungeonFarm`（`Dungeon/action.py`）
- Pipeline 节点：`DungeonFarmMain`（`assets/resource/base/pipeline/Dungeon/DungeonFarm.json`）
- 任务定义：`assets/resource/tasks/DungeonFarm.json`
- 副本配置：`assets/resource/base/dungeon/*.json`
- 离线验证：`tools/verify_dungeon.py`
- ROI 核对：`tools/calibrate_dungeon.py`

> [!IMPORTANT]
> 随包的 `rabbit_hole.json`（兔子洞异象界域）的 ROI 是**按玩家提供的界面截图标定的，尚未在实机上跑过**。
> 首次使用请按下文「核对标定」跑一遍 `calibrate_dungeon.py`，并把轮数设成 1 观察一轮。

## 分层结构

```
config.py   配置解析与校验（纯数据，无 MAA 依赖）
steps.py    把 Step 翻译成 MAA 调用（识别、按键、点击、探测循环）
runner.py   刷本循环：阶段顺序、失败处理、轮次衔接
action.py   CustomAction 入口：参数解析、依赖装配
```

`runner` 只有纯逻辑，外部能力（步骤执行、传送、战斗、时钟）全部构造注入，
所以「第 2 轮失败后是否走恢复流程」这类问题能离线跑闭环仿真，不需要游戏。

战斗直接复用 `AutoCombat` 的脚本加载与 `CombatSession`——战斗逻辑只应该有一个来源。

## 一轮的阶段顺序

| 阶段 | 做什么 | 失败含义 |
|---|---|---|
| `entry` | 从大世界走到副本列表并选中目标副本 | 没找到入口或没选中副本 |
| `confirm` | 点「进入」，处理消耗确认弹窗，等加载 | 没进副本 |
| `advance` | 进副本后前进接敌 | 进了副本但没找到敌人（地形/朝向不对） |
| 战斗 | 交给 `AutoCombat` | 战斗脚本报错或被中断 |
| `settle` | 等战斗结束标志（可选） | 超时仍未出现结算画面 |
| `locate` | 找出口/奖励点 | 没走到奖励点 |
| `reward` | 领奖 | 没点到领取按钮 |
| `exit` | 退出或「再次挑战」 | 没回到可再来一轮的状态 |

任一阶段失败走 `recover`（通常是「回大世界」），累计失败达 `max_failures` 就停止。
失败计数是**累计**而非连续：偶发失败若总量过大说明配置或环境有问题，
该停下来让用户看，而不是用无限重试掩盖。

`advance` 和 `locate` 是可选阶段，不配就跳过。

## 步骤类型

配置里每个阶段是一个步骤数组。类型可以显式写 `type`，也由出现的键推断。

| 类型 | 必填 | 作用 |
|---|---|---|
| `ocr` | `text` + `roi` | OCR 命中后点击文字框中心 |
| `template` | `path` + `roi` | 模板匹配后点击命中处 |
| `node` | `node` | 跑一个已有 pipeline 节点（可用裸字符串简写） |
| `key` | `key` | 按一下某个键 |
| `hold` | `key` + `duration` | 按住某个键固定时长（前进、转向） |
| `wait` | `wait` | 干等 |
| `click_rect` | `roi` | 直接点矩形中心（不经识别） |
| `search` | `until` + `probe` | 探测循环：反复做 probe 直到 until 命中 |

通用可选字段：`desc`（日志里显示什么）、`required`（false 表示找不到就跳过）、
`post_wait`（执行后额外等待）、`role`（界面选项的挂钩点）、
识别类还有 `timeout` / `interval` / `threshold` / `click`。

### 为什么需要 hold 和 search

副本流程里有两段无法用「点某个坐标」表达的动作：

- **进副本要走过去才开打**：敌人在场地另一头，站着不动不会触发战斗。
- **打完要找出口才能领奖**：出口位置随地形和镜头朝向变化，没有固定坐标。

`search` 让配置只描述「怎么小步探一下」和「看到什么算到了」，
具体位置来自识别结果，不来自猜测的坐标：

```json
{
    "type": "search",
    "until": { "ocr": ["领取奖励"], "roi": [392, 264, 496, 84] },
    "probe": [
        { "hold": "w", "duration": 1.2 },
        { "key": "f", "post_wait": 0.5 }
    ],
    "sweep": [{ "hold": "d", "duration": 0.5 }],
    "sweep_every": 5,
    "timeout": 120
}
```

语义与约束：

- 先看一眼再动手。目标可能已经满足（战斗一结束奖励弹窗就自己弹出来了），
  这时候不该先莽一步走开。
- `until` 必须是识别类步骤且**不点击**——它只回答「到了没」。要执行的交互放
  `on_found`，或者像上例那样放进 `probe`（边走边按 F）。显式写 `click: true` 会被拒绝。
- `sweep` 是连续 `sweep_every` 轮没命中时的纠偏动作，防撞墙卡死。
- 除了 `timeout`，还有一道轮数硬上限（`MAX_SEARCH_ROUNDS = 300`）。
  时间上限依赖时钟真的在走，而「等待被卡住」是真实故障，
  没有这道上限时角色会一直按着前进键跑下去。
- `search` 不能嵌套 `search`：循环套循环之后超时与失败语义无法预期。

`hold` 的抬手写在 `finally` 里。战斗/刷本最糟的 bug 是角色卡住一直往前走，
这类问题在日志里看不出来，只能在游戏里看到人物发疯。

## 轮次衔接：loop_mode

| 值 | 行为 | 代价 |
|---|---|---|
| `reenter`（默认） | 每轮都完整走 `entry` + `confirm` | 每轮多花走路和选副本的时间 |
| `again` | `exit` 点「再次挑战」原地重开，下一轮直接从 `advance` 开始 | 依赖结算界面有这个按钮 |

`again` 模式**只在上一轮成功时**才跳过 `entry`。一旦某轮失败，
当前停在哪个界面就不确定了，必须回到完整流程重新定位——
否则会在错误的界面上继续乱点。

## 清怪即收兵

副本里打完怪队伍 UI 还在，`CombatSession` 的脱战判据（离开队伍界面）永远不成立。
不看血条就只能每轮干等满 `duration`。

所以 `SessionConfig` 增加了 `stop_when_no_enemy`：见过敌人之后血条持续消失
`no_enemy_grace` 秒就结束会话。

- 默认**关闭**。普通「自动战斗」任务里用户可能只是想让脚本挂着出招，
  不该替他决定何时收手；刷本入口默认打开。
- 要求「先见过敌人」才生效。否则开局那几帧还没识别到血条就会被判成打完了，
  战斗直接空转结束。
- 宽限期给到 4 秒：血条会因为镜头转动、技能特效遮挡短暂消失，判太快会在敌人
  还活着时收兵，下一轮进副本就会带着残怪。

`duration` 仍然保留，作为兜底上限。

## ROI 与分辨率

配置里的 ROI 一律按 **1280x720** 标定，与仓库其它资源一致。
识别前统一用内核的 `scale_roi` 换算到当前截图尺寸——真机截图不一定正好是
720p（窗口大小、云游戏缩放都会变），不换算的话每个 ROI 都会偏到左上角，
而日志只会写「没识别到」，根本看不出是缩放问题。

两类 ROI 的精度要求不同：

- **识别类（`ocr` / `template`）**：ROI 只是「在哪一片里找这行字」，点击落在识别到
  的文字框中心。可以留宽一些，窗口比例略有差异也不会点空。
- **`click_rect`**：不经识别、点下去就是那个坐标，必须准。
  兔子洞配置里唯一的 `click_rect` 是难度按钮，所以它默认不点
  （界面 `difficulty` 选 0 表示沿用游戏记住的上次难度）。

### 核对标定

```bash
python tools/calibrate_dungeon.py <截图路径> [配置名]
```

把配置里所有 ROI 画到截图上，输出 `<截图名>.calibrate.png`：
绿框=识别范围，红框=直接点击的坐标。控制台按编号列出每个框对应的步骤。
截图不是 16:9 时会警告——那种情况下框会整体偏移，需要用与实际运行相同比例的窗口重截。

不需要 OCR 引擎，也不需要游戏在跑。

## 兔子洞配置

`rabbit_hole.json` 对应的实机流程：

| 步骤 | 界面 | 判据 |
|---|---|---|
| 传送 | 地图「兔子洞 / 异象界域」面板 | 走 `map_teleport`，传送点 id `rabbithole` |
| 进入 | 大世界，角色面朝兔子雕像 | 按住 W 前进，直到出现「兔子洞」F 提示，按 F |
| 选副本 | 异象界域选择界面 | 左侧列表 OCR 副本名（默认「钟表把戏」） |
| 难度 | 底部 I~V 圆形按钮 | `difficulty_rects` 五档；默认不点 |
| 开始 | 右下「进入」 | OCR「进入」 |
| 开战 | 副本内，怪在场地另一头 | 按住 W 前进，直到 `PinkPawHeist_CheckMonsterOnce` 命中 |
| 领奖 | 「领取奖励」弹窗 | 边走边按 F 直到弹窗出现，点左侧「领取」（40 本性像素） |
| 下一轮 | 结算「获得道具」 | 点「再次挑战」（`loop_mode: again`） |

开战判据复用粉爪已验证的敌人血条颜色节点（`PinkPawHeist_CheckMonsterOnce`），
不新造阈值——它是仓库里唯一已在实机验证过的直接战斗信号。

`reward` 阶段的 ROI 刻意**不覆盖**右侧的「双倍领取」（80 本性像素）：
OCR 文本「领取」会同时命中两个按钮，靠 ROI 把范围限制在弹窗左半是唯一可靠的区分方式。
`verify_dungeon.py` 有断言守着这条边界。

## 界面选项

| 选项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| 副本配置 | select | 兔子洞（异象界域） | 选「自定义配置文件」会展开一个输入框填 `resource/base/dungeon/<名字>.json` |
| 副本名称 | select | 钟表把戏 | 覆盖 `role: "stage"` 步骤的 OCR 文本；6 个兔子洞副本 + 「用配置文件里的」 |
| 难度档位 | select | 不改 | 「不改」沿用游戏记住的档位；I~V 点 `difficulty_rects` 对应按钮 |
| 刷取轮数 | select | 1 | 1/3/5/10/一直刷 |
| 先传送到副本入口 | switch | 开 | 关掉表示已站在入口附近 |
| 战斗预设 | select | 用配置文件里的 | 副本内战斗脚本 |
| 输出规则触发日志 | switch | 关 | 排查战斗脚本为何不生效 |

界面选项通过步骤的 `role` 标签定位要改哪一步，而不是按下标——
顺序会随配置调整，按下标覆盖会在用户插一步之后静默改错对象。
选项挂不上时（配置里没有对应 `role`）会打印提示，不会静默无效。

### 参数为什么走载体节点而不是 custom_action_param

实机上出现过这个错误：

```
[DungeonFarm] 收到参数: '{"log_decisions":true}'
[DungeonFarm][ERROR] 未指定副本配置。请提供 config（内联）、dungeon（内置配置名）...
```

七个界面选项原本都覆盖 `DungeonFarmMain` 的 `custom_action_param`，而 GUI 合并多个
option 的 `pipeline_override` 时，**这个字段是整体替换而不是逐键合并**：最后一个
选项（日志开关）把前面六个连同 pipeline 里写死的 `dungeon` 默认值一起冲掉，
于是 Python 只收到 `{"log_decisions": true}`，任务在第一步就退出。

注意这个坑和「有没有默认值」无关——默认值也在同一个字段里，一起被替换掉了。
把选项从 `input` 改成 `select` 同样救不了它，因为问题出在合并粒度，不在占位符替换。

修法是让每个选项覆盖**各自独立的载体节点**的 `attach`，字段路径不同就不存在互相
覆盖。这与 `PinkPawHeist_AutoResizeGameWindowConfig` 是同一套机制（`attach` +
`get_node_data`），已经上线验证过：

```jsonc
// pipeline/Dungeon/DungeonFarm.json
"DungeonFarm_RoundsOption": {
    "desc": "选项载体：刷取轮数（rounds）",
    "enabled": false,
    "attach": {}          // 空 = 用户没有显式选择
}

// tasks/DungeonFarm.json
{
    "name": "R3",
    "label": "3",
    "pipeline_override": {
        "DungeonFarm_RoundsOption": { "attach": { "rounds": 3 } }
    }
}
```

Python 侧把七个载体节点的 `attach` 合并成参数字典，再与
`DungeonFarmMain.custom_action_param` 合并：

| 来源 | 角色 | 优先级 |
|---|---|---|
| 载体节点 `attach` | 用户在界面上的显式选择 | 高 |
| `custom_action_param` | 资源自带默认值 / 内联调用 | 低（兜底） |

合并时 `null` 和空字符串一律跳过——输入框占位符替换失败时下发的正是 `null`，
跳过它才能回落到 `dungeon: "rabbit_hole"`，而不是拿着 `None` 去当配置名。
`false` 和 `0` 是有效值，不能被当成空值丢掉（`difficulty: 0` 表示「不改难度」）。

`verify_dungeon.py` 第 10、11 组守着这套约定：选项若改回覆盖 `DungeonFarmMain`、
或载体节点没在 pipeline 里定义、或空值没被过滤，都会直接验证失败。

排查同类问题先看这两行日志，它们在任何配置解析之前就打出来：

```
[DungeonFarm] 收到参数: {"dungeon": "rabbit_hole"}
[DungeonFarm] 界面选项: {difficulty=0, dungeon='rabbit_hole', log_decisions=False, rounds=1, ...}
```

第一行是 `custom_action_param`，第二行是载体节点合出来的。哪一行缺东西，
问题就在对应的那一段。

## 验证

```bash
python tools/verify_dungeon.py     # 刷本流程离线验证
python tools/verify_combat.py      # 战斗引擎（改了 runtime.py 就要跑）
python tools/check_locale_keys.py DungeonFarm.json
```

`verify_dungeon.py` 重点验证的不是「正常情况能跑通」，而是**出错时不会在游戏里乱点**：

- 配置缺 roi / 缺战斗脚本 / 未标定 → 拒绝运行，且一步都不执行
- 传送失败 → 不进副本；`advance` 失败 → 不进战斗；`locate` 失败 → 不领奖
- `hold` 期间异常 → 必须抬手
- `search` 超时 → 不执行 `on_found`
- 时钟不推进 → 轮数上限兜住，不会无限前进
- 兔子洞配置引用的传送点、pipeline 节点必须真实存在
- 「领取」的 ROI 不能碰到「双倍领取」
- 界面选项必须走载体节点，空值不得当成有效参数（见上文「参数为什么走载体节点」）

## 未完成的部分

| 项 | 说明 |
|---|---|
| 实机验证 | 全部验证都是离线的。`rabbit_hole.json` 的 ROI 来自界面截图标定，需要实机跑一轮核对 |
| 副本列表滚动 | 左侧列表默认只认可见的项。副本多到需要滚动时要自行加滚动步骤 |
| 本性像素耗尽 | 像素不够时会以领奖失败的形式暴露，没有单独的「余额不足」判定 |
| 战斗失败处理 | 角色阵亡、挑战失败的界面未知，目前一律走 `recover` 回大世界 |
| 双倍领取 | 配置默认单倍。要双倍需自行把 `reward` 的 ROI 改到弹窗右半 |
