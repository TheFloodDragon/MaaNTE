# 战斗模块（Combat）

独立的战斗内核，提供“通用战斗方式”与“自定义战斗方式（JSON 模板）”两类策略。
本页描述当前已实现的行为与边界；**尚未实机验收**，请以实际测试为准。

## 组成

| 位置 | 作用 |
| --- | --- |
| `agent/custom/action/Combat/models.py` | 会话、快照、动作意图/结果、待确认动作、声音事件 |
| `agent/custom/action/Combat/observer.py` | 截图并生成 `CombatSnapshot`；识别失败报告为“未知”而非“否” |
| `agent/custom/action/Combat/executor.py` | 唯一输入出口；每个动作前检查停止信号；`release_all()` 释放持有输入 |
| `agent/custom/action/Combat/policies.py` | `basic` / `balanced` / 自定义模板策略与会话阶段推进 |
| `agent/custom/action/Combat/templates.py` | 自定义模板校验（版本、动作白名单、步骤上限、循环语义） |
| `agent/custom/action/Combat/sound_events.py` | 复用 `SoundTrigger.Ear`，产出带会话 ID 与有效期的闪避事件 |
| `agent/custom/action/Combat/resources.py` | 开发/发行目录下解析模板与声音样本路径 |
| `agent/custom/action/Combat/actions.py` | `CombatInitAction` / `CombatStepAction` / `CombatFinalizeAction` |
| `agent/custom/action/Combat/recognitions.py` | `CombatStatusRecognition`（`in_combat` / `cleared` / `session_terminal`） |
| `assets/resource/base/pipeline/Combat/` | Pipeline 主循环与公共状态节点 |
| `assets/resource/base/combat/templates/` | 内置模板；用户模板放 `config/combat/templates/` |
| `assets/resource/tasks/Combat.json` | 任务与选项（限 `Win32-Front`） |
| `tests/combat/` | 无需 MaaFramework 的单元测试（`py -m unittest discover -s tests/combat`） |

## Pipeline 流程

```text
CombatMain (CombatInitAction)
  └─ CombatLoop (CombatStepAction：观察 → 决策 → 执行 一轮)
       ├─ CombatSessionEnded (CombatStatusRecognition: session_terminal) ─ CombatFinalize
       └─ CombatLoop
```

`CombatStepAction` 不以失败返回表示会话结束，退出循环由 `session_terminal` 识别分支决定，
否则会被路由到 `on_error` 而不是收尾节点。

## 行为契约

- 未知不等于否定：技能就绪、当前槽位、战斗状态均为三态（`True/False/None`）。
- 停止优先：每次输入前检查 `tasker.stopping`；蓄力按住分 50ms 小步检查并保证抬起。
- 发送输入不等于成功：`switch/skill/ultimate` 返回 `PENDING`，需要**更新的一帧**确认；
  到期未确认视为失败，`required` 步骤失败直接结束会话，不重试、不重放模板。
- 一个会话一份状态：模块级单例，`CombatFinalizeAction` 与异常路径都会 `release_all()` 并停止音频。
- 声音防御默认关闭；启动失败或监听线程退出时清空事件、提示用户，并在本场关闭声音防御。
- 会话终态：`ENDED`（脱战确认 / 模板完成 / 循环上限）、`FAILED`（入战超时、总时限、必需步骤失败）、`STOPPED`。

## 自定义模板

```json
{
  "version": 1,
  "id": "support_then_main",
  "type": "custom",
  "roles": { "support": 2, "main": 1 },
  "steps": [
    { "action": "switch", "role": "support", "required": true },
    { "action": "skill", "when": "skill_ready" },
    { "action": "switch", "role": "main", "required": true },
    { "action": "ultimate", "when": "ultimate_ready" },
    { "action": "normal_attack", "max_ms": 1200 }
  ],
  "on_finish": "reobserve",
  "max_cycles": 100
}
```

- 动作白名单：`switch` `normal_attack` `charged_attack` `skill` `ultimate` `dodge` `wait`
- 条件白名单：`always` `skill_ready` `ultimate_ready` `in_combat` `slot_alive`
- `on_finish`：`finish`（单轮后结束）或 `reobserve`（必须给正整数 `max_cycles`）
- 限制：最多 50 步、文件 ≤ 100KB、`max_cycles` ≤ 1000；模板名只允许 `[A-Za-z0-9_-]+\.json`

## 当前限制（提交时状态）

1. **观察器只实现了敌方血条检测**（`CombatEnemyHealthBar`，参数与粉爪 `CheckMonsterOnce` 相同）。
   当前槽位、存活、技能/终结技就绪均返回“未知”。因此：
   - `balanced` 目前不会释放技能/终结技，实际行为等同 `basic`；
   - 自定义模板中的 `switch` 步骤会因无法确认而在超时后失败，`required` 时会结束会话。
   这是有意的失败安全行为，识别补齐后无需改动策略层。
2. 首版仅支持 `Win32-Front`；后台控制器未验证。
3. 未做实机验收：入战/脱战判定阈值、确认超时（默认 2000ms）、声音阈值均需实测校准。
4. 粉爪清怪与旧“音频闪避”任务尚未接入本内核（按计划留待后续）。

## 本次同时修复的既有问题

- `pinkpaw_core1.py`：战斗/等待循环增加停止检查，停止后不再继续发起攻击。
- `pinkpaw_core3.py`：切人确认超时返回 `None`，不再伪装为目标键（现有调用方均不使用返回值，路线行为不变）。
- `SoundListener.py`：监听线程异常退出时清除运行标志，新增 `is_healthy()`。
- `SoundDodgeAction.py`：启动后与运行中检查监听器健康，失效时明确提示并结束。
