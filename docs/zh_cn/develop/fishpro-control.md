# FishPro 控条引擎

新一代钓鱼控条实现，作为 `FishNew` 任务的可选引擎与现有实现并存。默认**不启用**，需在任务选项中显式切换。

- 代码位置：`agent/custom/action/AutoFish/fishpro/`
- 自定义动作：`auto_fish_pro`（`agent/custom/action/AutoFish/auto_fish_pro.py`）
- Pipeline 节点：`FishNewGamingPro`（`assets/resource/base/pipeline/Fish/FishNew.json`）
- 离线验证：`tools/verify_fishpro.py`
- 资产校验：`tools/check_fishpro_assets.py`

## 与现有实现的关系

| | `FishNewGaming`（旧） | `FishNewGamingPro`（新） |
|---|---|---|
| 自定义动作 | `auto_fish_without_cv` | `auto_fish_pro` |
| 默认状态 | 启用 | 关闭 |
| 控制量 | 二值方向 + 固定脉冲 | 连续动作强度 + 自适应脉冲 |
| 绿条识别 | 固定窄 ROI + 颜色点合并 | 形状过滤 + 候选打分 + 缝隙桥接 |
| 目标建模 | 中心/宽度 EWMA | 中心与宽度平滑 + 宽度多帧确认 + 速度估计 |
| 预测 | 单一前瞻 | 光标/绿条双前瞻 + 相对速度制动偏置 |
| 丢帧 | 超时即放弃 | 分级宽限 + 动作衰减 + 超限重置 |
| 学习 | 无 | 可选在线残差策略 |

切换引擎**不改动**旧节点的任何字段，旧实现行为保持不变。两个节点通过 `enabled` 互斥，同一时刻只有一个生效。

## 分层结构

每层可独立测试，控制算法不接触截图与按键。

```
config.py      参数定义与 custom_action_param 解析（逐字段钳制、非法回退）
vision.py      ROI 裁剪、绿条与光标识别、候选打分
thresholds.py  目标宽度自适应阈值（静默半径、安全边距）
state.py       平滑、速度估计、预测、派生观测量、丢帧判定
policy.py      规则控制层：强度曲线、静默滞回、恢复与降档
learning.py    在线残差策略、奖励函数、持久化与回放
executor.py    动作强度 → A/D 按键状态机
runtime.py     单次控条会话主循环、日志限频、调试帧
```

数据流：

```
截图 → vision（Detection）→ state（Observation）
      → policy（rule_action）+ learning（residual）
      → merge（final_action）→ executor（A/D 按键）
```

## 关键设计

### 识别：绿条缝隙桥接

光标压在绿条中段时，颜色掩码会被切成左右两块。若沿用「取最大连通域」，只会识别到半条，**导致中心与宽度同时算错**，控制目标随光标漂移。

`green_bridge_px`（默认 21，需大于光标宽度）先做水平闭运算把窄缝补回，再执行形状过滤。实测对比（绿条真实中心 200、宽 161）：

| 光标位置 | 未桥接（中心/宽） | 桥接后（中心/宽） |
|---|---|---|
| 200 | 242 / 78 | 200 / 161 |
| 180 | 232 / 98 | 200 / 161 |
| 240 | 179 / 118 | 200 / 161 |

### 状态：跳变抑制与宽度确认

- 光标单帧位移超过 `cursor_jump_max_px` 时，位置仍更新但**速度保持上一次平滑值**，避免识别误选污染速度。
- 绿条宽度突变需连续 `target_width_confirm_frames` 帧一致才采纳，避免遮挡造成的瞬时宽度错误直接进入控制。
- 速度经 EWMA 平滑并受 `cursor_max_velocity_px` 钳制。

### 规则：安全区优先当前位置

判定安全区时**以当前位置为主**，预测仅在接近边缘时作为补充依据。若要求「当前与预测同时安全」，条件过严会导致持续过度干预。

进入安全区即停手，并通过 `center_silence_active` + 边缘滞回维持静默，减少框内来回抖动。

### 规则：分段强度与动作上限

- 框内：上限收紧（`inside_action_cap`），避免强动作把光标顶出框；
- 框内贴边：放宽到 `inside_edge_action_cap` 并设下限，保证能拉回；
- 框外：全力恢复，按误差分级设动作下限；
- 框外但预测将进框：降档到 `outside_approach_action_cap` 提前刹车，避免冲过头。

朝目标运动时按相对速度主动制动，远离目标时加力。

### 执行：脉冲优先，长按受限

框内默认只用短脉冲（`inside_pulse_*`）。长按仅在同时满足「极度贴边 + 预测越界 + 正在远离 + 大误差」时才允许——框内长按是掉框的主因。

方向切换有 `switch_hold_time` 保护窗口，窗口内小误差不反向，防止抖动。

### 丢帧：分级宽限

| 阶段 | 行为 |
|---|---|
| 宽限内（`missing_*_grace_sec`） | 按上次观测继续输出，动作按 `missing_*_decay` 衰减；静默态保持静默 |
| 超出宽限 | 释放 A/D，重置平滑与节奏状态（保留学习进度） |
| 始终未识别到且超过 `lost_abort_ms` | 返回失败，交给 Pipeline 恢复 |
| 已见过控条后光标持续消失 `control_end_grace_ms` | 视为进入结算，返回成功 |

### 学习层（默认关闭）

13 维特征的线性残差策略，叠加在规则输出之上，残差受 `learning_residual_limit` 限幅。

- 奖励：框内为正、掉框重罚且按距离分级、恢复进框高奖励；
- 更新：优势（`reward - reward_ema`）驱动，含权重衰减与 ±5 裁剪；
- 产物：写入 `debug/fishpro/`（`policy.npz` + `samples.jsonl`），**不写源码目录**；
- 关闭时不读写任何学习产物。

奖励 EMA 持续为负且样本足够时，残差会被缩放抑制，避免学坏后持续干扰规则层。

## 使用

在 MXU 中选择 `钓鱼任务（新）` → `控条引擎`：

- **旧版（默认）**：使用 `FishNewGaming`，行为不变；
- **FishPro（新）**：使用 `FishNewGamingPro`，可再开启：
  - `开启在线学习`：启用残差学习并读写 `debug/fishpro/`；
  - `输出控条调试帧`：把带标注的画面写入 `debug/fishpro/`，仅用于调参。

## 参数覆盖

所有 `FishProConfig` 字段都可通过 `custom_action_param` 覆盖，非法值回退默认并钳制范围：

```jsonc
"custom_action_param": {
    "roi_px": [384, 21, 512, 58],
    "inside_action_cap": 0.16,
    "outside_approach_action_cap": 0.35,
    "control_end_grace_ms": 300,
    "lost_abort_ms": 1500,
    "session_timeout_ms": 60000,
    "learning_enabled": false,
    "debug_enabled": false
}
```

ROI 默认使用 1280×720 基准像素并经 `screen.map_rect` 归一化；设置 `use_ratio_roi: true` 可改用比例 ROI 兜底。

## 验证

```bash
python tools/verify_fishpro.py        # 离线算法验证，165 项检查
python tools/check_fishpro_assets.py  # Pipeline / 任务选项 / 5 语言文案
```

`verify_fishpro.py` 不依赖游戏与 MaaFramework 控制器，用合成图与假控制器覆盖七个方面：识别、状态、规则、执行、学习、闭环仿真、参数解析。

闭环仿真用简化控条物理（按键给加速度、光标带阻尼、绿条正弦漂移）统计留框率，当前基线：

| 场景 | 留框率 |
|---|---|
| 静止绿条 | 100.0% |
| 移动绿条 | 97.4% |
| 框外起步 | 97.5% |
| 零散丢帧 | 99.5% |
| 开启学习 | 100.0% |

并覆盖：控条正常结束、全程无识别失败退出、截图异常、任务停止、会话超时。**所有退出路径都断言 A/D 已释放。**

> 仿真物理是简化模型，留框率用于回归对比与防止算法退化，不等价于真机表现。真机效果仍需实测。

## 注意

- 控条循环高频截图，CPU 占用高于旧实现；
- 调试帧会明显增加磁盘占用，仅在调参时开启；
- 学习产物跨会话累积，删除 `debug/fishpro/` 即可重置。
