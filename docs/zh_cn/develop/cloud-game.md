# 云异环启动任务

把云异环客户端拉起来、等它真正进入游戏，然后交给后续每日任务。配合前端调度器即可实现「定时自动跑每日」。

- 代码位置：`agent/custom/action/CloudGame/`
- 自定义动作：`CloudGameLaunch`
- Pipeline 节点：`CloudGameLaunchMain`（`assets/resource/base/pipeline/CloudGame/CloudGameLaunch.json`）
- 任务定义：`assets/resource/tasks/CloudGameLaunch.json`
- 串联预设：`assets/resource/tasks/preset/CloudDaily.json`
- 离线验证：`tools/verify_cloudgame.py`

## 与已有云异环兼容改动的关系

`CloudGame-Front` 控制器与 `utils/pienv.py` 由既有改动提供，本任务不修改它们。两者分工：

| | 既有兼容改动 | 本任务 |
|---|---|---|
| 解决的问题 | 已有任务在云端不出错 | 把客户端拉起来并进入游戏 |
| 前提 | 用户已手动开好客户端并进入游戏 | 无，从关闭状态开始 |
| 手段 | `pienv` 判断控制器，跳过本地窗口操作 | 探测安装路径、启动进程、等待就绪 |

## 分层结构

每层可独立测试，均不接触账号凭据。

```
locator.py      安装路径探测（注册表 / 常见目录 / 用户指定）+ 白名单校验
launcher.py     启动进程 + 等窗口出现 + 最小化时恢复窗口
launcher_ui.py  启动器界面：OCR 文本锚点、阶段判断、剩余时长解析
ocr_bridge.py   把一帧截图变成 launcher_ui 的 LauncherScreen
enter.py        进入流程状态机：点开始游戏 → 处理确认弹窗 → 排队等待 → 进游戏
ready.py        只等待、不点击的旧路径（auto_enter 关闭时使用）
action.py       CustomAction 入口：参数解析与各层串联
```

`enter.py` 只接受一个 `ocr_screen(image) -> LauncherScreen` 回调，真实 OCR 实现放在 `ocr_bridge.py`。这样状态机可以完全离线测试，不需要加载 OCR 模型。

## 关键设计

### 安全：可执行文件白名单

这是仓库里第一个能启动外部进程的功能，因此把「能启动什么」限制在最小集合。`locator.is_trusted_launcher` 要求同时满足三条：

1. 文件真实存在；
2. 后缀是 `.exe`；
3. 文件名落在 `TRUSTED_LAUNCHER_NAMES` 白名单内（`ntecloudlauncher.exe`、`ntecloudgame.exe`）。

白名单不是形式主义。云异环安装目录里就躺着一个 `uninst.exe`——朴素的「扫目录找 exe」做法会把卸载程序当启动器拉起。实测该文件被正确拒绝。

启动时不使用 `shell=True`，参数以列表传递，避免命令注入。

### 显式配置无效时直接失败，不静默回退

用户填了 `launcher_path` 但路径非法（比如填成 `cmd.exe`）时，任务**报错退出**，而不是悄悄回退到自动探测并启动另一个客户端。

第一版实现就是静默回退的，结果是：用户配错了路径，任务却「成功」了，启动的是别的东西，而且永远不会知道自己配错。现在的行为是明确报错并给出可操作提示。

只有在用户**完全没填**时才走自动探测。

### 探测优先级

| 顺序 | 来源 | 说明 |
|---|---|---|
| 1 | 用户指定 | 填 exe 全路径，或填安装目录（会在目录及 `NTECloud/` 下找） |
| 2 | 注册表 | `Uninstall` 键的 `InstallLocation` / `DisplayIcon`，本机实测可命中 |
| 3 | 常见目录 | 逐盘扫 `NevernessToEvernessCloudGame`，注册表被清理时兜底 |

`DisplayIcon` 可能带 `,0` 图标索引，解析时会裁掉——但仅当逗号后是纯数字时才裁，避免误伤含逗号的目录名。

### 判断「已进入游戏」：只认已验证的游戏内节点

判定「是否已经在游戏里」只依赖已验证的游戏内公共节点，不拿云客户端自身的界面来充当依据：

- `InWorld` —— 大世界（Esc 菜单按钮 + 任务菜单按钮同时命中）
- `InMiniWorld` —— 小世界（粉爪、主线故事等）

只要其中之一命中，说明登录、排队、串流、加载全部走完了——这些画面里不可能出现游戏内 HUD 按钮。

### 进入流程：客户端不会自己进游戏

最初的实现从「窗口出现」直接跳到「等 `InWorld` 命中」，中间**没有任何点击**。这在实机上永远进不了游戏：

1. 启动后停在启动器主页，要点右下角「开始游戏」；
2. 随后弹出「本次游戏将使用您的免费时长或计费时长」确认框，左按钮「退出启动」**带 30 秒倒计时**，超时自动放弃启动；
3. 之后才是排队 / 串流加载 / 进入游戏。

所以缺了这一段，任务只会干等到 `ready_timeout` 再报「未能进入游戏」，而真因是没人点按钮。

`enter.py` 是补上的状态机。每轮截一帧、OCR 判断阶段、再决定动作——**先识别再动作，不预设顺序**，因为这几个阶段的出现次序会随「不再提醒」是否勾选、是否需要排队而变化：

```
  已在游戏内 ────────────────────────────────► 成功
  确认弹窗   ──点「进入游戏」──┐
  启动器主页 ──点「开始游戏」──┤
  都不是（排队/加载/登录中）──┴──► 继续轮询，直到进游戏或超时
```

三条硬性约束：

- **确认弹窗优先于主页**。弹窗盖在主页上时两者的文本会同时出现在 OCR 结果里；判成主页就会去点被遮住的「开始游戏」，白等 30 秒后启动被放弃。
- **时长为 0 立即停**，但「读不到时长」≠「时长为 0」。值那一条 OCR 偶尔会漏，若把 `None` 当成 `0`，任务会以「剩余时长为 0」中止，而账号里其实还有时长。
- **排队超上限放弃并报错**。定时调度里静默占着云游戏时长是最糟的失败方式。

### 启动器界面靠 OCR，不引入未验证的模板

启动器界面没有模板图，但 OCR 读得很干净（实测分数普遍 0.98~1.00），所以这一层全部走 OCR 文本匹配，不写任何未经验证的模板与 ROI。

坐标不写死：每次都从当前帧的 OCR 结果里取 box，因此窗口尺寸变化也不受影响。标定依据是 `debug/cloudgame/023714_restored.png`（1280x720 实机截图）。

两个实测得来的注意点：

- 文本匹配一律用**包含**而非相等。「付费时长：」前的图标会被 OCR 读成 emoji（实机得到 `😄付费时长：`），相等匹配必然失配。
- `JOCR(only_rec=True)` 是**跳过检测、把整个 ROI 当作一行识别**，对整屏用会返回一条低分垃圾（实测 `"口"` / 0.19）。要拿到画面里的多条文本必须用 `only_rec=False`。

### 点击坐标用截图的坐标空间，不必手动换算

云异环窗口尺寸不固定（实测出现过 1280x720 与 1600x900），而 `cached_image` 始终是 1280x720——MaaFramework 会把截图缩放到目标短边。那么点击该传哪套坐标？

已从 MaaFramework 源码确认：传**截图的坐标空间**，框架内部换算到设备原始坐标。`ControllerAgent::preproc_touch_point` 的逻辑是

```cpp
double scale_width  = image_raw_width_  / image_target_width_;
int    proced_x     = round(p.x * scale_width);
```

`handle_click`、`handle_touch_down`、`handle_touch_move`、`handle_swipe` 全部先过这个函数。唯一例外是控制单元带 `MaaControllerFeature_NoScalingTouchPoints` 时不缩放，而 `Win32ControlUnitMgr::get_features()` 只会返回 `UseMouseDownAndUpInsteadOfClick` 与 `UseKeyboardDownAndUpInsteadOfClick`，**从不设置** 该标志。

所以直接拿 OCR 结果里的 box 中心去点是正确的，窗口多大都不用管。这也是坐标不写死、每帧从 OCR 现取的另一个理由。

顺带一提，`UseMouseDownAndUpInsteadOfClick` 意味着对报告该特性的鼠标方式，`post_click` 在框架内部本来就会拆成 touch_down + 50ms + touch_up。本模块用的三段式与之等价，只是多了一次显式 move 来触发 hover。

### controller 句柄不能缓存

这条对所有 CustomAction 都适用，值得单独记。在 AgentServer 上下文里探测得到：

```
controller._handle = 2498503850400
两次取 context.tasker.controller: 同一对象=False 同一句柄=False
```

**每次访问 `context.tasker.controller` 都返回新对象、新句柄。** 把它取出来跨调用缓存，输入 API 会抛 `OSError: exception: access violation reading 0xFFFFFFFFFFFFFFFF`。

这个 bug 的症状极具迷惑性：

- 截图**全程正常**（截图那条路每次都重新取 controller）
- 只有点击失败，而且**偶发成功**（刚取到的句柄有时还没失效）
- 日志里满屏 access violation，看不出是哪个调用炸的

排除过的错误方向：maafw 版本不一致（系统与 `.venv` 都是 5.10.4）、符号缺失（`MaaControllerPostClickV2` 能正常解析）、argtypes 未声明（已声明且类型正确）、测试工具的采集线程与 agent 并发（关掉后现象不变）。

正确写法是每次现取：

```python
def _controller_of(context):
    return getattr(getattr(context, "tasker", None), "controller", None)
```

点击本身用 `post_touch_move` → `post_touch_down` → `post_touch_up`，与仓库通用工具 `Common.utils.click_rect` 一致。

### 窗口最小化必须先恢复

窗口最小化时 `post_screencap().wait()` **仍然返回成功**，但截到的是全白画面（实测 mean=255 / std=0），尺寸还会变成异常值（实测 3317x720 而非 1280x720）。识别拿着白图必然全不命中，表现为「一直等到超时」，日志里却看不出任何异常。

所以任何识别之前都先查 `IsIconic` 并 `ShowWindow(SW_RESTORE)`。三月七工具箱在云游戏截图前做同样的事（`_ensure_window_not_minimized_for_frame_capture`），原因一致：最小化后画面停止渲染。

### 连续命中确认

加载过程中可能出现单帧误命中，因此要求连续命中 `confirm_hits` 次（默认 2 次）才放行。命中的节点中途变化会重置计数。

识别抛异常时**不放行**，记录日志后继续等。在加载页误放行会让后续任务全部错乱，代价远大于多等一会儿。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `launcher_path` | 空 | 留空自动探测；填错会报错而非回退 |
| `window_timeout` | 120s | 等客户端窗口出现 |
| `ready_timeout` | 600s | 从主页到进入游戏的全程上限，含登录、点按钮、排队与加载 |
| `confirm_hits` | 2 | 连续命中次数 |
| `wait_in_game` | 是 | 关掉则只启动不等进入游戏 |
| `auto_enter` | 是 | 自动点「开始游戏」并处理 30 秒确认弹窗。关掉则退回「只等待」的旧行为 |
| `max_queue_time` | 1800s | 排队 / 加载超过此时长即放弃并报错 |
| `stop_when_no_playtime` | 是 | 明确读到免费与付费时长均为 0 时立即停止 |

`ready_timeout` 默认从 300s 提到 600s：现在它要覆盖点按钮、确认弹窗与排队，300s 在排队时明显不够。

## 定时自动跑每日

调度**不在本仓库实现**。MFAAvalonia / MXU 前端已有调度器，仓库提供可被调度的任务与预设，职责这样切更干净。

`CloudDaily` 预设把启动放在首位，后接每日三件套：

```
CloudGameLaunch → Furniture → WithdrawMoney → ClaimRewards
```

顺序有强约束：启动任务必须在首位，否则每日任务会在尚未进入游戏时就开始跑。`tools/verify_cloudgame.py` 对此有专门断言。

前端设定定时后，到点执行该预设即可。

## 登录方式

本任务**不碰账号凭据**，不读取、不存储、不传递密码。登录由云客户端自己完成，需要你先在客户端里保存登录状态。

进入游戏这一步已经自动化（点「开始游戏」+ 处理确认弹窗），但**登录本身仍需客户端自己保持登录态**。如果客户端每次都停在登录页，任务会走到总超时并提示先手动登录一次。要做点击式自动登录，需要补登录页的模板与 ROI。

顺带一提：确认弹窗上的「不再提醒」勾选后可让后续启动跳过该弹窗，但代码不依赖它——新账号或重装后弹窗仍会出现。

### 云客户端自身的相关配置

排查时翻到的，供参考。代码既不读也不写这些文件。

`<安装目录>\NTECloud\UserData\Config\Config.ini`

| 键 | 含义 |
|---|---|
| `[Setting] autoLogin` | 自动登录。为 1 时客户端自己完成登录，本任务因此不必碰凭据 |
| `[Setting] noBillingReminder0_<UID>` | 确认弹窗的「不再提醒」，按 UID 存。置 1 可跳过该弹窗 |

`<安装目录>\NTECloud\Config\Config.ini` 里 `LaunchCmdLine=/launcher`，即客户端主程序 `NTECloudGame.exe` 由启动器带 `/launcher` 拉起。

启动器 `NTECloudLauncher.exe` 的二进制里只有两个命令行参数：`/launcher` 与 `/directly`。`/directly` 的上下文紧邻 `[UPDATER] NeedUpdate` / `NeedCheckFirst` 与 "Patcher file info not found"，指向更新器的跳过检查，**不是**「直接进入游戏」。

结论：没有可用的命令行开关能绕过主页那次点击。进游戏必须点，因此必须有可交互的输入桌面。

## 验证

```bash
python tools/verify_cloudgame.py
```

397 项断言，六组：

| 组 | 内容 |
|---|---|
| 1 | 探测层：白名单防线、来源优先级、目录输入、错误提示 |
| 2 | 启动层：拒绝即失败、不静默替换、已运行不重复拉起、停止响应 |
| 3 | 就绪层：连续命中确认、识别异常不放行、超时提示 |
| 4 | 接线层：任务定义、Pipeline、五语言、注册、预设交叉核对 |
| 5 | 启动器界面：文本锚点、阶段判断、时长解析边界 |
| 6 | 进入流程：状态机、点击冷却、排队上限、时长为 0 即停 |

第 1 组的白名单用例直接跑本机真实路径，包括「同目录的 `uninst.exe` 必须被拒」。第 5、6 组的界面文本与 box 全部照实机 OCR 结果填写，不是编造的字符串。

```bash
python tools/mutate_cloudgame.py
```

变异测试植入 12 处缺陷，全部被捕获：弹窗判断失效、主页不排除弹窗、去掉点击冷却、把读不到时长当成 0、时长为 0 仍继续点、排队计时从入口起算、不先判断是否已在游戏内、识别异常当作命中、文本改用相等匹配、改用 `post_click`、点击省略 move、缓存 controller 跨调用复用。

其中「把读不到时长当成 0」最初**没被抓到**——原用例里连时长标签都没有，函数在 `anchor is None` 处就提前返回了，走不到真正的配对逻辑。补上「标签在、值读不到」的用例后才覆盖到。这类盲区只有变异测试能暴露。

### replay：真实截图 + 真实 OCR

```bash
python tools/replay_cloudgame.py
```

`verify_cloudgame.py` 第 5、6 组用的是**手抄**的 OCR 文本（照实机输出把 `TextHit` 打进代码）。那能验判断逻辑，但验不到两件事：OCR 模型的真实输出是否还和抄的一致（换模型、换缩放都可能漂移），以及当初有没有抄错。

replay 读 `debug/cloudgame/` 下的真实 PNG，跑真实 OCR 模型，把结果喂给真实的 `launcher_ui`，再断言判断结果。交叉印证已成立：`023714_restored.png` 读出免费 683 分钟＝11 小时 23 分，与手抄 fixture 完全一致。

`mutate_cloudgame.py` 会对界面判断类的变异同时跑 verify 与 replay，两边都必须抓到。

**当前覆盖缺口**：`debug/cloudgame/` 下的帧全是启动器主页，所以 replay 抓不到这三类退化——弹窗判断失效、主页不排除弹窗、把读不到时长当成 0。原因不是 fixture 脱节，而是**没有能触发那些路径的真实帧**：从未采到弹窗画面，也从未采到「时长标签在、值缺失」的画面。这两种画面采到后，把对应条目移进 `mutate_cloudgame.py` 的 `REPLAY_SENSITIVE` 即可纳入交叉校验。

## 采集与标定工具

排队界面的精确识别需要真实截图。相关工具：

| 工具 | 用途 |
|---|---|
| `tools/capture_cloudgame.py` | 抓单帧或按帧差持续抓，存到 `debug/cloudgame/` |
| `tools/capture_cloudgame_stages.py` | 跨阶段采集，每张变化帧附 OCR 结果写入 `ocr.jsonl` |
| `tools/probe_cloudgame_ocr.py` | 对已有截图跑 OCR，看哪些文字可读、box 在哪 |

OCR 模型**不在仓库里**。跑这些工具前需要：

```bash
git submodule update --init assets/MaaCommonAssets
python -c "import sys; sys.path.insert(0,'tools/ci'); from configure import configure_all_models; configure_all_models()"
```

缺模型时 OCR 会静默返回 0 条，不会报错。

## 已知限制

| 限制 | 影响 | 补齐方式 |
|---|---|---|
| **排队界面未实机采集** | 排队中只能判定为「推进中」，看不出排队人数与预计时间；靠 `max_queue_time` 兜底 | 实机跑一次，日志里会打出当时画面的全部 OCR 文本，据此补精确识别 |
| 未登录页未实机采集 | 停在登录页时只会报总超时，不会明确说「需要登录」 | 同上 |
| 不做点击式登录 | 需客户端自己保持登录态 | 补登录页模板与 ROI |
| 点击链路未实机验证 | 开发机进程无输入桌面权限（见下），点击代码只过了离线断言 | 在可交互会话里跑一次 |

### 点击需要可交互的输入桌面

`CloudGame-Front` 用 `mouse: Seize`，写的是**真实**鼠标输入，需要一个可交互的桌面。远程会话断开或最小化时：

- `SetCursorPos` 返回 0，`GetCursorPos()` 冻在 `(0, 0)`
- `GetForegroundWindow()` 返回 0
- 但 `post_touch_*` 仍然**返回成功**

最后一条是诊断陷阱：**API 调用成功不等于输入送达**。判断是否真的点动只能看画面是否变化。开发机上实测过这个反差——日志里满屏「已点击」，界面却 5 分钟毫无变化。

`tools/live_cloudgame_enter.py` 因此在启动前先探一次输入桌面，不可用就直接退出（返回码 2）并说明原因，而不是跑满超时才发现点击全落空。确实只想采集画面时加 `--force`。

```bash
python tools/probe_input_desktop.py
```

这个脚本区分三种成因并给出结论，不把它们混为一谈：

| 现象 | 成因 | 解法 |
|---|---|---|
| 输入桌面是 `Winlogon` | 会话被锁 | 解锁会话 |
| `SetCursorPos` 失败且 `GetLastError=5` | 权限不足，运行身份与交互会话所有者不一致 | 用会话所有者的身份运行 |
| `WTSConnectState` 非 Active | 会话断开 | 重新连接 |
| 会话 Active、未锁屏、输入桌面为 `Default`，但 `GetForegroundWindow()==0` | 远程桌面客户端窗口未处于前台（最小化或失焦），远程会话里没有任何前台窗口 | 把客户端窗口恢复并保持前台 |

最后一行是开发机上实测到的情况。注意 RDP 会话与控制台会话本来就不是同一个，两者不同**不是**原因。

### 端到端实机验证

```bash
python tools/live_cloudgame_enter.py --timeout 300
```

它搭起完整链路，跑的是真实 pipeline 节点与真实注册的 CustomAction，与 MXU 里的执行路径一致：

```
Resource + Win32Controller + Tasker + AgentClient + agent 子进程
```

为什么不写旁路脚本：`InWorld` 是 `And(EscMenuButton, TasksMenuButton)` 的复合识别，在旁路里重实现这套逻辑，测到的是复现品而不是真实路径。

任务运行期间采集线程**只存帧、不跑 OCR**。tasker 正忙时主线程再调 `post_recognition` 会一直阻塞——实测采集线程就是这样卡死的：第一帧图写出来了，`ocr.jsonl` 却是空的。OCR 统一在任务结束后补跑。
