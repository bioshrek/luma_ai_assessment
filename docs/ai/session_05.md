# Session 5 — according to plan at `docs/plan.md`, help me understand dataset source D from a…

[← 回目录](index.md) · 由 `scripts/render_ai_sessions.py` 从 [session_5.json](../ai_chat_sessions/session_5.json) 生成，请勿手改。

| | |
| --- | --- |
| 开始时间 | 2026-08-08 08:30 UTC |
| 阶段 | 源 D |
| 轮次 | 4 |
| 模型 | claude-opus-5 |
| 工具调用 | 29 |
| 改动文件 | 1（23 处）|
| 输出 token | 72,676 |
| 墙钟时间 | 19m 45s |

---

## 第 1 轮 — 2026-08-08 08:30 UTC

> according to plan at `docs/plan.md`, help me understand dataset source D from a perspective of whom don't have prior knowledge of robotics, but with basic understanding of the dataset source A, B and C.

<sub>claude-opus-5 · 输出 3,360 tokens · 1m 00s</sub>

I'll read the plan document first.

<details>
<summary><b>2 次工具调用</b> — copilot_readFile x2</summary>

1. **`copilot_readFile`** — Read `docs/plan.md`, lines 1 to 400
2. **`copilot_readFile`** — Read `docs/plan.md`, lines 400 to 700

</details>

<details>
<summary><i>推理过程（2 段）</i></summary>

I need to explain source D to someone unfamiliar with robotics but who understands the previous sources. The document is in Chinese while the user asked in English, so I'm deciding whether to respond in Chinese to match the source material or English to match the user's request—I think responding in Chinese makes sense given the project documentation is in that language.

Actually, let me reconsider and respond in English since that's what the user asked in. I should focus on explaining what D is without robotics jargon: it's EPIC-KITCHENS-100, a dataset of 700 first-person videos of people cooking in their own kitchens with human annotations describing actions and their time intervals. Unlike sources A, B, and C which contain robot actions, states, images, and timestamps, D only has video footage plus text labels with time ranges—no action commands or state information. Including D is deliberate as a degradation test to show how a schema designed only for complete data breaks when encountering missing fields.

The key differences emerge in how each source handles episodes, provenance, and temporal data. For A/B/C, episodes come predefined from upstream, but for D you must define segments yourself since a 1652-second video isn't inherently an episode. The local copy was re-encoded to 512x288 at 30fps for another project, while the original is



</details>

## 简短版

A/B/C 都是**机器人自己录的**数据：每一帧都有"下达的指令"（action）和"实际的状态"（state）。D（EPIC-KITCHENS-100）是**人戴着头上的相机做饭的视频**，加一份人工写的时间标注。它一个数字信号都没有 —— 只有画面、时间区间和一句话描述。

一个非机器人的类比：

- **A/B/C** ≈ 汽车的行车黑匣子：每 0.02 秒记录一次方向盘转角、油门、车速。
- **D** ≈ 行车记录仪视频 + 事后有人看回放写下"00:14–00:23 左转"。你看得见发生了什么，但**控制量本身从未被记录过，也永远补不回来**。

---

## 逐项对照

| | A `pusht` | B `aloha` | C `OXE/UR5` | **D `EPIC-KITCHENS`** |
|---|---|---|---|---|
| 谁在动 | 仿真里的推杆 | 仿真双臂 | 真机单臂 | **真人的手** |
| action | 2 维 xy | 14 维关节角 | 7 维增量 + 3 维标志 | **无** |
| state | 有 | 有 | 有（语义不明） | **无** |
| 画面 | mp4 旁挂 | mp4 旁挂 | 内嵌在记录里 | mp4（本地已有） |
| 时间 | 真实时间戳 | 真实时间戳 | 无，靠频率合成 | **标注员写的秒数** |
| 一条 episode 是什么 | 上游给好的 | 上游给好的 | shard 里的第 N 条 | **需要我自己定义** |

---

## 为什么要专门收一个"什么都没有"的源

这是 D 存在的唯一理由：**它是降级路径的证据**。前三个源都"信号齐全"，schema 只跑在完整数据上，缺陷不会暴露。D 逼出三件事：

1. **NULL 不是 0。** 没有 action 时字段写 NULL、`has_action=False`、`ActionSpec.space="none"`，而不是塞一个 0 向量假装有。塞 0 的话，下游算统计量时会把一堆假零算进分布，且**无法区分"真的没动"和"根本没这个模态"**。
2. **SKIPPED 不是 FAIL，也不是 PASS。** 依赖 action 的质检规则（`ACTION_RANGE`、`GRIPPER_STUCK` 等）在 D 上应该输出"跳过 + 原因"。若输出 PASS，等于宣称"检查过了，没问题"——这是撒谎；若输出 FAIL，等于把"数据形态不同"污名化成"数据坏了"。
3. **数据源插件不该假设有网络。** A/B/C 都从 HuggingFace 拉，D 是纯本地文件，`fetch()` 是空操作。如果 `SourcePort` 这层抽象里偷偷混进了网络假设，D 会当场把它打出来。

---

## D 独有的两个坑（这两点最值得理解）

### 1. "一条 episode"在 D 里是被**发明**出来的，不是被读出来的

`P01_01.mp4` 长 1652 秒（≈5 万帧），是"一个人做了半小时饭"。这在语义上等价于机器人数据里的**一整个采集 session**，不是一条轨迹。真正对应"一条示教"的是标注里的 segment：

```json
{"segment": [0.14, 3.37], "verb_label_name": "open", "noun_label_name": "door"}
```

3 秒，一个原子动作。所以 `episode_uid = "epic100:P01_01#0042"`，`task = "open door"`（正好对上 C 的 `language_instruction`）。

**这是 D 最有教学价值的地方**：前三个源的 episode 边界是上游事实，D 的边界是**标注员的主观判断**。所以 `EpisodeBoundary.termination_source = "annotator"`、`success = None`（不是 `False` —— 没人说过这次开门成功了还是失败了，"不知道"和"否"是两回事）。同时它需要一条别的源不需要的规则 `SEGMENT_BOUNDS`：区间会越界、会重叠、会短到只剩几帧。

### 2. 这份视频是**改过的**，而且改动会污染帧号

本地这份是为别的项目重编码过的 512×288 / 30fps；官方原版是 1080p、部分 50 或 59.94 fps。

标注给的是**秒**，帧号得靠 `round(t × fps)` 算。fps 变了，帧号就全错。所以：

> **入库存秒（权威）+ 帧号（派生），并在 `provenance` 里写明帧号是 `derived_from_seconds@30fps`。**

只存帧号的话，哪天换回原始视频，全库的帧号静默失效且无从察觉。这就是 plan 里反复强调的"有损变换必须可追溯" —— D 是这条原则最干净的一个实例，因为它的有损变换是**已经发生过的、不由我控制的**。

附带代价：288p 分辨率不足以判断手部接触细节，所以本轮 D 只做结构性质检，不做视觉标签。

---

## 一句话总结

A、B、C 三个源回答的是"**不同机器人的动作语义怎么统一**"；D 回答的是"**当一个源连动作都没有时，这套 schema 是优雅降级还是当场崩溃**"。它量很小（几千帧）、不进训练子集、评审方没这份数据时会自动跳过 —— 它的价值全在**证明架构的边界行为是设计出来的，不是碰巧没遇到**。

---

## 第 2 轮 — 2026-08-08 08:42 UTC

> feedback:
> - we choose `EPIC-KITCHEN-100` for source D because I have some understanding of it. the local dataset is for reference only, which doesn't mean we should fetch the local version.
> - for source D, it's wired that there is no goal for each sample, just demonstration. I don't think there is no actions, but those actions are just categorised in spacial/temporal dimensions without further detailed data. I remembered that there are multiple tasks on the dataset, don't constraint yourself to just the termporal action detection tasks. If `EPIC-KITCHEN-100` is not the right fit for this project, consider other datasets.

<sub>claude-opus-5 · 输出 9,572 tokens · 2m 49s</sub>

<details>
<summary><i>推理过程（2 段）</i></summary>

The user is pointing out that EPIC-KITCHENS-100 was chosen mainly because they're familiar with it, and they're questioning whether it's actually the right fit since there's no explicit goal per sample and the actions are only categorized spatially and temporally without richer detail. They're noting that EPIC-KITCHENS-100 has multiple tasks beyond just temporal action detection, so I should explore what those other tasks are and consider whether a different dataset might be better suited for what we're trying to accomplish.

Looking at the available tasks in EPIC-KITCHENS-100, there's action recognition, temporal action detection, action anticipation, domain adaptation, and multi-instance retrieval. Beyond the base dataset, there are extensions like VISOR which provides pixel-level hand and object segmentation with relational information, and EPIC Fields which gives camera poses and 6-DoF head motion trajectories — that continuous ego-motion signal could be valuable. There are also hand-object interaction annotations available, though I'm seeing some fragmentation in what's provided across different versions of the dataset.

The key insight here is reframing the problem: actions do exist in this domain, but they're represented at a coarse symbolic level — verb and noun classes with temporal segments — rather than as continuous control signals. There's spatial information too through the segmentation masks and bounding boxes, so it's not a complete absence of action and state, just a different abstraction level than what I might initially expect.

Now I'm realizing that human video datasets like this are actually valuable for pretraining models in robot learning, and the action labels themselves function as task goals. EPIC-KITCHENS-100 includes free-text narrations and sentence-level captions, so language is definitely present. And EPIC Fields offers something genuinely continuous: 6-DoF camera poses per frame reconstructed in world coordinates via COLMAP, which maps nicely onto the schema with absolute end-effector pose in world frame, though the scale is arbitrary so it's not physically grounded.

This also introduces a third provenance category I hadn't fully considered: estimated or reconstructed signals (like SfM-derived poses) sit between measured and synthesized data. I could also extract sparse 2D hand positions from the VISOR hand masks as another signal to track.

EPIC-KITCHENS-100 still feels like the right choice, though there are some compelling alternatives worth noting. Ego4D has hand-object interactions and Ego-Exo4D adds actual 3D body and hand poses—real action-relevant signals. HOI4D is egocentric with 4D data including object and hand poses. ARCTIC and DexYCB offer hand-object interactions with motion capture data, giving genuine 3D hand joint angles—essentially treating humans like robots with continuous state. Something-Something v2 uses verb templates, and there are robotics datasets like RH20T and DROID, though those are robot-centric rather than human-centric.



</details>

Let me verify what EPIC-KITCHENS-100 actually ships beyond temporal action detection.

<details>
<summary><b>2 次工具调用</b> — copilot_fetchWebPage x1, vscode_fetchWebPage_internal x1</summary>

1. **`copilot_fetchWebPage`** — 
2. **`vscode_fetchWebPage_internal`** — Fetched 3 resources

</details>

<details>
<summary><i>推理过程（2 段）</i></summary>

Wait, I need to reconsider the dataset structure here. The Extended Sequences download actually includes gyroscope and accelerometer data—that's real IMU sensor information captured at high frequency, which means option D isn't actually "no signals" at all. Let me map out what EPIC-KITCHENS-100 really provides: the core dataset has 700 videos spanning 100 hours across 20 million frames, filmed in 45 different kitchens across 4 cities, with 89...

Actually, looking at the full ecosystem: there are 89.9K action segments and 20.5K unique narrations covering 97 verbs and 300 nouns. The dataset includes challenges for action recognition, detection, anticipation, unsupervised domain adaptation, and multi-instance retrieval. Beyond the RGB and optical flow frames, there's automatic hand-object bounding box detection and object masks. VISOR adds 271K manually annotated masks plus 9.9M densely interpolated ones across 257 object categories with 67K hand-object contact relations. EPIC-SOUNDS contributes 117.5K audio events in 44 classes. EPIC-Fields provides 3D camera calibration data for 671 videos using COLMAP registration. And HD-EPIC, the newest addition, includes 41 hours of digital twin reconstructions with gaze tracking, 3D object motion, hand segmentation masks, and "how"/"why" descriptions tied to recipes.

So the user's correction reframes everything—D isn't "no action" at all.  It operates at multiple levels: symbolic actions as discrete (verb, noun) classes at ~2-4 Hz, spatial grounding through hand bounding boxes and VISOR masks with contact relations, ego motion as continuous 6-DoF camera poses from structure-from-motion, and raw proprioceptive signals from the head-mounted GoPro's gyroscope and accelerometer. The narrations themselves function as the goal or instruction at the segment level, just like C's language_instruction, but there's no success label—nobody annotates whether the action actually succeeded. HD-EPIC does introduce recipe-level goals with explicit steps.

Now I'm realizing the current schema has a real problem: D's action space isn't "none" because the action is genuinely real, just discrete and symbolic rather than continuous. This exposes a gap in SignalSpec, which assumes fixed-width representations.

I'm considering a few approaches to fix this. The cleanest one is recognizing that D's action lives at the episode level, not per-frame—since an episode is a segment, the whole thing is one action. So I could mark the task at the episode level while keeping per-frame action as none, but that makes `has_action=False` confusing. A better path might be introducing explicit action representation levels: continuous control, discrete events, or label-only.

The real fix is adding a `level` field to `SignalSpec` that captures whether actions are per-frame continuous, per-frame discrete, episode-level labels, or absent entirely. D would be `episode_label`, which is a genuine schema improvement that catches the kind of forced error the assessment is looking for.

For the per-frame signals D does have—like IMU data with gyro and acceleration components—they map cleanly into the existing channel schema. I'm adding new role values like `head` for IMU, new space types for angular velocity and linear acceleration, units in rad/s and m/s², a sensor frame type, and marking them as physical and metrically convertible.

The camera pose from EPIC-Fields is a 6-DoF signal with quaternion and translation that needs `camera_pose_abs` as the space, world frame, and quaternion representation in COLMAP convention (qw, qx, qy, qz for world-to-camera). Crucially, it's not metrically convertible because SfM scale is arbitrary—this is the second instance of unitless data alongside the previous example.

Hand bounding boxes and VISOR masks are 2D image-space measurements in pixels, also not metrically convertible. Beyond the three provenance categories in the original plan, I'm realizing there's a critical fourth distinction: `measured` versus `synthesized` versus `estimated`. The EPIC-Fields poses come from COLMAP, which is a model output with 96% success and per-frame registration failures that produce NULLs. Hand bboxes come from the 100DOH detector, also estimated with confidence scores. This matters for quality control because you can't apply the same validation rules to estimated data as you would to measured data.

So `Provenance` needs to track `signal_origin` per signal, not just per timestamp. The user also clarified that the local copy is reference-only, so I should fetch from official sources instead — annotations from GitHub, EPIC-Fields JSON per video, and handle the large RGB frames and videos separately.

I can reframe the local copy as an optional `--local-mirror` for offline caching and as a provenance test case showing how to represent re-encoded derivatives, while keeping the canonical source as the official release. This satisfies both the schema requirements and the user's correction.

For the dataset choice, EPIC-KITCHENS-100 still works well with this richer framing, though I could offer alternatives like Ego-Exo4D if they want continuous human action with 3D body and hand pose from multi-view capture. suggest keeping EK-100 as the baseline dataset D, with the option to layer in EPIC-Fields, IMU, and VISOR data to create a richer signal source—or if they want genuine continuous state tracking from a human perspective, HOI4D or DexYCB would be the most practical fifth dataset to add. The user's feedback about considering other datasets signals this is a design decision that needs resolving, so I should update the plan document with this analysis and recommendation rather than just suggesting it. I'll present the analysis and recommendation clearly, then ask a focused question about scope before making those cascading edits. Let me also verify a couple of claims I'm less certain about — the IMU data, for instance, is only available in the extended sequences from the 2020 footage, not the original EK-55 videos, which is an important caveat about partial availability across the dataset.

This actually highlights a critical insight: capabilities must be tracked per-episode, not per-source, since different episodes within the same source can



</details>

你的两条反馈都成立，而且第二条直接推翻了我上一轮和 plan §1.1 的核心假设。先把事实核对清楚。

## 我查证的结果：EPIC-KITCHENS-100 远不止时序动作检测

官方生态实际提供（已从官网核实）：

| 层 | 内容 | 对本项目的意义 |
|---|---|---|
| **核心 EK-100** | 700 视频 / 100h / 20M 帧；89.9K action segments；97 动词 × 300 名词；20.5K 自然语言 narration | 符号级 action + 语言指令 |
| **下载包里的 IMU** | Extended Sequences 附带 **陀螺仪 + 加速度计** | **真实测量的逐帧物理信号**——我上轮说"一个数字信号都没有"是错的 |
| **EPIC-Fields** (NeurIPS'23) | 671 视频 / 18.7M 帧的 **6-DoF 相机外参 + 内参**（COLMAP） | **一条真正的连续位姿轨迹** |
| **VISOR** (NeurIPS'22) | 271K 人工掩膜 + 9.9M 稠密插值掩膜；**67K 手-物接触关系** | 空间级 grounding + 接触事件 |
| **EPIC-SOUNDS** | 117.5K 音频事件 / 44 类 | 一个前三源完全没有的模态 |
| **HD-EPIC** (CVPR'25) | 41h，含 recipe/步骤层级、注视、3D 物体运动、"how/why" 描述 | **有真正的层级目标** |

5 个官方 challenge：Action Recognition / Detection / Anticipation / UDA / Multi-Instance Retrieval，另加 VOS、HOS、audio 系列。

---

## 你的三点反馈，逐条回应

### 1. "本地副本只是参考，不代表要拉本地版"

接受，这是我的错。正确做法：**权威来源是官方发布**，本地那份降级为**可选镜像**：

- annotations 走 GitHub `epic-kitchens-100-annotations`（CSV/pickle，几十 MB，可直接进 CI）
- EPIC-Fields 位姿 / VISOR 稀疏标注 / hand-object bbox 各自独立小包
- 视频只在 `--with-video` 时按需取少量

本地那份 512×288 重编码副本仍然有价值，但**它的角色变了**：从"数据来源"变成"**镜像一致性的检验对象**"。当同一个 `episode_uid` 在官方元数据里是 50fps、在本地镜像里是 30fps 时，`provenance.transforms` 能不能把这个差异表达出来并让帧号重算——这才是它该证明的事，而且比"直接读本地"强得多。

### 2. "没有 goal 很奇怪" —— 一半对

- **有指令**：`verb + noun`（"open door"）与 20.5K 条自然语言 narration，就是任务描述，和 C 的 `language_instruction` 同一个槽位。HD-EPIC 更有 recipe → step 的层级目标。
- **没有成功标签**：确实没人标注"这次开门成功了"。所以 `success=None` 是对的——但原因不是"没有目标"，而是**目标已给定、结果未评判**。这在 schema 里是两件事，我上一版把它们混成了一件。

顺带：这恰好暴露 `EpisodeBoundary` 少了一个维度。A 的 success 由仿真器判定、C 由策略声明、D **压根不存在评判者**。`success=None` 至少要能区分"评判过但未知"和"体系里就没有评判"。

### 3. "action 不是没有，只是只有时空维度的分类，没有细粒度数据" —— 这句是关键，直接指出 schema 的洞

plan §2.2 的 `SignalSpec` 隐含假设：**action 是逐帧、定宽、数值向量**。D 的 action 不满足其中任何一条。写 `ActionSpec.space="none"` 不是"优雅降级"，是**把有信息说成没信息**。

修正方案：给 `SignalSpec` 加一个正交维度 —— **表示层级**，而不是继续在 `space` 枚举里塞值：

```python
SignalSpec = {
  "level": "per_frame_continuous"   # A/B/C
         | "per_frame_discrete"     # 逐帧离散（如接触状态）
         | "episode_label"          # D：整个 segment 一个 (verb, noun)
         | "absent",                # 真的没有
  ...
}
```

于是 D 是 `has_action=True, level="episode_label"`，而不是 `has_action=False`。质检规则的门控条件从 `required_capabilities` 变成 `required_capabilities + required_level` —— `ACTION_JERK` 要求 `per_frame_continuous`，在 D 上 `SKIPPED(reason=action_is_episode_label)`，理由是具体的，而不是笼统的"没有 action"。

---

## 修正后，D 反而变成四个源里最有价值的一个

因为它带来了三个前三源**结构上不可能出现**的问题：

**(a) 同一个 source 内部 capability 不齐。** IMU 只有 EK-100 扩展部分有（EK-55 那批老视频没有）；EPIC-Fields 只覆盖 671/700 视频、18.7M/20M 帧。这逼着验证一件 plan 里写了但从没被真正考验过的事：**`capabilities_json` 挂在 `episodes` 表上，是逐 episode 的，不是逐 source 的**。A/B/C 三个源内部完全齐整，永远测不出这一点。

**(b) 信号来源必须分"测量 / 估计 / 标注"三类，plan 现在只有两类。** §2.2f 的 `Provenance` 只区分 `real` / `synthesized`。但：

- IMU = **测量**（传感器实测）
- EPIC-Fields 位姿 = **估计**（COLMAP 重建，96% 成功率，未注册帧为 NULL，且**尺度任意**）
- VISOR 稠密掩膜 = **插值派生**（9.9M 由 271K 人工掩膜插出来）
- hand bbox = **检测器输出**（带置信度）

这个区分不是学究气，它直接决定质检语义：COLMAP 位姿里的一个跳变**大概率是重建失败，不是数据损坏**，判 FAIL 就是误伤。所以需要：

```python
"signal_origin": "measured" | "estimated" | "interpolated" | "annotated" | "synthesized"
```

并规定：`origin != "measured"` 的通道，数值类规则的 severity 自动降到 REVIEW。

**(c) 第二个 `metric_convertible=False` 的实例，而且比 pusht 更刁钻。** A 的像素是 2D 无尺度；EPIC-Fields 是**完整 6-DoF 位姿但尺度任意**（SfM 的固有性质），旋转部分用四元数 `quat_wxyz`、平移部分不可换算成米。这正好把 §2.2 新加的 `Channel.rotation` 和 `metric_convertible` 同时打在一个通道上——C 只暴露了 `rotation`，A 只暴露了 `metric_convertible`，只有 D 让两者相交。

---

## 建议：留下 EPIC-KITCHENS-100，但换个用法

**保留**，理由是上面三条在别的数据集里凑不齐，而且你熟悉它、能判断我写的对不对（这本身就是选型的合理依据）。

**改为分层取用**，按性价比排序：

| 层 | 体积 | 价值 | 建议 |
|---|---|---|---|
| annotations（segment + narration） | ~50 MB | 符号 action + 语言 | **必取** |
| EPIC-Fields 相机位姿 | 每视频一个 JSON | 连续轨迹 + `estimated` origin + 无尺度位姿 | **必取，这是 D 的核心价值** |
| IMU（gyro/accel） | 中等 | 唯一的 `measured` 逐帧信号 + 源内 capability 不齐 | **建议取**（只取几个视频） |
| VISOR 手-物接触关系 | 稀疏 JSON | 接触事件 ≈ 夹爪开合的人类对应物 | 取 or 不取，看工期 |
| 视频本体 | 巨大 | 只用于画面级质检 | 默认不取，`--with-video` 时取少量 |

---

## 如果要换：三个更贴近机器人的候选

万一你觉得"估计出来的相机位姿"还不够，这几个人类数据集有**真正的连续动作**：

1. **HOI4D** —— 第一人称，逐帧手部姿态 + 物体 6-DoF 位姿 + 动作分段。体量小、可控，最接近"人当机器人用"。
2. **DexYCB / ARCTIC / H2O** —— MoCap 级手-物交互，21 关节手部轨迹 = 一个真实的高 DoF 连续动作空间，直接落进 `role="joint"`。
3. **Ego-Exo4D** —— 3D 身体+手部姿态、相机位姿、**并且有专家对执行好坏的评价**（能填 `success`）。但体量大、门槛高。

不过要注意：这三个**都会让 D 变得和 A/B/C 更像**，从而削弱它作为"降级路径证据"的作用。EK-100 的"信号层级不齐、来源混杂、源内 capability 不一致"才是它不可替代的地方。

---

需要我现在动手改 `` 吗？涉及联动的有：§1.1 全部重写、§2.2a 加 `level`、§2.2e `Capabilities` 与逐 episode 语义、§2.2f `Provenance.signal_origin`、§3 质检规则的门控与 severity 降级、§8.4 不变量 3、§11 已知局限。告诉我按哪个取用范围写（"必取"两层 / 加 IMU / 全取），我一次改完。

---

## 第 3 轮 — 2026-08-08 08:48 UTC

> proceed with your finding and proposal

<sub>claude-opus-5 · 输出 57,521 tokens · 14m 41s</sub>

<details>
<summary><i>推理过程（2 段）</i></summary>

I'm going to update plan.md with the scope they approved: annotations and EPIC-Fields as required, IMU as recommended, and VISOR deferred. I need to revise the table row for option D, add the selection rationale bullet, and update the trade-offs section accordingly.

Then I'll work through the detailed changes: rewriting section 1.1 entirely, adding the `level` field to SignalSpec, documenting how option D drives new requirements, capturing D's per-episode capabilities, adding signal_origin to provenance, handling the success=None distinction for cases without an adjudicator, updating QC rules with level-based gating and severity downgrades, checking the SQLite schema, fixing the ubiquitous language for embodiment, updating invariants, and noting the known limitations. I'll also need to update adapter naming conventions and the M0 milestone. Let me pull up the exact text and make these edits carefully. 好的，现在我要确保 D 数据集能够通过可空字段和能力声明来展示架构的可插拔性，这样就不需要填充零值或抛异常。同时，由于它是本地文件源且没有网络依赖，能很好地验证数据源插件化的设计理念。

对于取舍部分，D 数据集默认不取视频，而是专注于标注层和位姿层的数据。

现在重写第 1.1 节的完整内容，然后处理第 2.2a 节的 SignalSpec，加入 `level` 字段来区分不同的信号粒度。

Writing SignalSpec schema...



</details>

Proceeding. Let me confirm exact text at the edit sites.

<details>
<summary><b>1 次工具调用</b> — copilot_readFile x1</summary>

1. **`copilot_readFile`** — Read `docs/plan.md`, lines 26 to 50

</details>

<details>
<summary><i>推理过程（2 段）</i></summary>

I'm planning two main edits: first, I'll update the table row for source D along with its selection rationale and trade-off section, then I'll do a full rewrite of section 1.1 covering the implementation details for the local EPIC-KITCHENS-100 dataset, including the verified directory structure.



</details>

<details>
<summary><b>1 次工具调用</b> — copilot_readFile x1</summary>

1. **`copilot_readFile`** — Read `docs/plan.md`, lines 48 to 106

</details>

<details>
<summary><i>推理过程（2 段）</i></summary>

I'm planning out the edits I need to make: updating a table row, revising a bullet point about selection rationale, editing a sentence in the trade-offs section, and completely rewriting section 1.1. I'll use multi_replace calls to apply these changes efficiently, splitting the large section 1.1 rewrite into two separate calls to be safe. Now I'm drafting the new content for section 1.1 about implementation details.

I need to correct two fundamental misconceptions from the previous version. First, the local 512×288/30fps copy isn't the authoritative source—it's a re-encoded derivative for another project, so treating it as canonical would lock the entire library's frame numbers to an unofficial fps. The real authority is the official release, with the local copy downgraded to an optional mirror that serves a better purpose: validating whether the provenance system can express and recalculate frame numbers when the same episode has different fps across copies. Second, the claim that D has no actions or state is wrong. D does have actions, just at a different representation level—they're symbolic labels combining verb-noun pairs with time intervals rather than per-frame continuous vectors. Plus the EPIC-KITCHENS ecosystem includes two genuine per-frame physical signals (IMU and camera pose), so writing `ActionSpec.space="none"` isn't elegant degradation, it's claiming information doesn't exist when it does.

Now I'm laying out the official data layers that are actually available, starting with the GitHub annotations package containing action segments, verb/noun vocabularies, narrations, and official splits, which is essential to include. 视频本体默认不取，除非用 `--with-video` 标志才会用本地镜像；标注数据包含叙述 ID、参与者、视频片段的时间戳、动作描述和对应的动词/名词分类。

关键是把 episode 的粒度定在单个 action segment 而不是整条视频——`P01_01` 虽然长达 1652 秒，但语义上对应一整个采集 session，真正的 episode 应该是几秒长的 segment，就像机器人数据里的一条示教轨迹；用官方的 `narration_id` 作为稳定的上游 ID 来构造 `episode_uid`。D 的 action 采用 `level="episode_label"` 而不是 `space="none"`，task 字段就是 verb 加 noun 的组合，比如"open door"。

由于 action 是 episode 级别的标签，frames.parquet 里根本没有逐帧的 action 列，那些依赖逐帧数值的规则（比如 `ACTION_RANGE`、`ACTION_JERK`、`GRIPPER_STUCK`）在 D 上会被标记为跳过，原因明确指出是因为 action 粒度是 episode 级别。而 D 的 state 信号则是真实的逐帧数据，这点在四个数据源里是独一无二的。

现在看一下 state 通道的具体构成，包括 IMU 的陀螺仪和加速度计读数，以及 EPIC-Fields 提供的相机位置和旋转估计。

这里的关键是 `cam_q` 用四元数表示旋转，坐标系是世界坐标系。这一组数据首次把之前分散的信息整合到一个向量里——摄像机旋转、度量可转换性的限制，还有 SfM 重建固有的尺度不确定性问题。现在要引入一个新的字段来区分数据的来源类型，包括测量值、估计值、插值和标注等。

这个区分很关键，因为它直接影响质检的严格程度：比如 COLMAP 位姿的跳变通常意味着重建失败而非数据错误，而未注册的帧位姿应该是空值而不是零或插值。因此对于非测量来源的数据，数值检查规则的严重程度要自动降级，并在报告中说明原因。

A/B/C 三个源的状态都是测量的，所以这条规则对它们没有实际影响，只有 D 能真正验证这个机制是否有效。

源内的能力集不一致是 D 最独特的特征——IMU 只在 EK-100 扩展部分存在，而 EPIC-Fields 虽然覆盖了大部分视频但逐帧可能缺失。这意味着同一个源下不同 episode 的能力集可能不同，而 A/B/C 内部完全齐整，永远无法暴露这个问题。既然数据库架构已经把能力集字段放在 episode 级别，D 就成了唯一能证明这个设计有实际用途的来源，需要添加一条验收断言来检查同一源下是否存在能力集不同的 episode。

本地镜像现在的角色是一致性验证而非数据来源。我在定义一个 provenance 结构，记录原始数据的来源、时间戳和帧索引的计算方式，以及任何本地转码镜像的信息——比如帧率、分辨率和与官方版本的差异说明。关键原则是秒数是权威的，帧号是从秒数派生的，因为官方视频混合了 50fps 和 59.94fps，而本地镜像统一是 30fps，同一个片段在两边的帧号会不同。

派生量必须附带它依赖的参数，否则无法判断数据是否过期。现在我在处理边界和目标的语义问题，区分不同系统中 `success` 字段的 `None` 值——有些表示评判机制存在但该条未知，有些表示体系里根本没有评判者——所以需要在 `EpisodeBoundary` 中添加字段来明确指定评判者的身份。

对于数据采样，我按参与者分散选择 5~8 个视频，确保既有 IMU 数据也有无 IMU 的情况来展示能力差异，每个视频取前 N 个片段，总量控制在几千到一万帧级别——目标是证明降级路径和来源混杂，而不是单纯堆积数据量。关于可用性和隐私，官方标注和 EPIC-Fields 数据可以公开下载（CC BY-NC 4.0 许可证，非商业用途），IMU 和视频文件体量较大，需要通过配置文件来管理访问。

本地镜像路径只在本地配置中出现，仓库里用占位符代替，任何数据层不可用时会自动标记并降级，而不是导致整个数据源失败——这套层级可用性机制和 episode 级别的能力声明是统一的。

现在我要开始编辑，但先确认一下"compose"字段的定义——计划中说是"pre"、"post"或"unknown"，但对于绝对旋转来说 compose 其实没有意义。



</details>

<details>
<summary><b>1 次工具调用</b> — copilot_multiReplaceString x1</summary>

1. **`copilot_multiReplaceString`** — Replacing 9 lines with 10 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 2 处修改，第 32-42 行

```
Now rewriting §1.1 entirely.

<details>
<summary><b>1 次工具调用</b> — copilot_replaceString x1</summary>

1. **`copilot_replaceString`** — Replacing 42 lines with 85 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 1 处修改，第 47-47 行

```
Now the schema changes in §2.2.

<details>
<summary><b>2 次工具调用</b> — copilot_multiReplaceString x2</summary>

1. **`copilot_multiReplaceString`** — Multi-Replace String in Files
2. **`copilot_multiReplaceString`** — Replacing 31 lines with 42 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 1 处修改，第 154-154 行

```

<details>
<summary><b>1 次工具调用</b> — copilot_replaceString x1</summary>

1. **`copilot_replaceString`** — Replacing 3 lines with 8 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 1 处修改，第 220-220 行

```

<details>
<summary><i>推理过程（2 段）</i></summary>

I'm updating section 2.2e to refine the Capabilities dictionary, adding per-episode notes and clarifying the distinction between RGB frames (including embedded ones from C) and independently decodable video files that the quality checks rely on.



</details>

<details>
<summary><b>1 次工具调用</b> — copilot_multiReplaceString x1</summary>

1. **`copilot_multiReplaceString`** — Replacing 32 lines with 44 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 3 处修改，第 282-314 行

```

<details>
<summary><b>3 次工具调用</b> — copilot_findTextInFiles x1, copilot_readFile x1, copilot_replaceString x1</summary>

1. **`copilot_findTextInFiles`** — Searched for regex `EpisodeBoundary = \{|success_adjudicator|signal_origin|has_camera_pose` (`**/docs/plan.md`), 10 results
2. **`copilot_readFile`** — Read `docs/plan.md`, lines 262 to 285
3. **`copilot_replaceString`** — Replacing 8 lines with 10 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 1 处修改，第 264-264 行

```

<details>
<summary><i>推理过程（2 段）</i></summary>

Looking at the QC rules for section 3, I need to handle several updates: keep the existing `SEGMENT_BOUNDS` rule (which is D-specific), add a new rule that gates on `level`, implement severity downgrade for non-measured origins, and potentially add a `POSE_TRACK_GAP` rule for handling NULL frames in EPIC-Fields. Let me check the current table structure to see exactly what's already defined.



</details>

Now the QC rules section.

<details>
<summary><b>1 次工具调用</b> — copilot_multiReplaceString x1</summary>

1. **`copilot_multiReplaceString`** — Replacing 3 lines with 6 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 2 处修改，第 393-412 行

```

<details>
<summary><b>3 次工具调用</b> — copilot_findTextInFiles x1, copilot_readFile x1, copilot_replaceString x1</summary>

1. **`copilot_findTextInFiles`** — Searched for regex `SEGMENT_BOUNDS|POSE_COVERAGE|required_level` (`**/docs/plan.md`), 4 results
2. **`copilot_readFile`** — Read `docs/plan.md`, lines 404 to 407
3. **`copilot_replaceString`** — Replacing 1 lines with 2 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 1 处修改，第 406-406 行

```

<details>
<summary><i>推理过程（2 段）</i></summary>

I need to verify that the origin severity bullet was actually added since the multi_replace operation had one failure. The successful replacements were the required_level intro and the 设计要点 bullet, but the table row replacement failed initially—though I've since corrected it. Let me check whether the bullet replacement for the non-physical aspect went through properly.

Now I'm working through the remaining sections: the 无损/有损边界 table needs D's annotation and mirror mentioned, the EpicKitchensAdapter description should be updated, and I need to handle the invariants, limitations, M0 section, and adapter details. For the invariants, I'm reworking invariant 3 to incorporate the level concept, and I should review the current EpicKitchensAdapter text which describes it as a local file source that reads JSON annotations and uses ffprobe for video headers, with fetch as a no-op to verify the port abstraction doesn't depend on networking.

For M0, I'm looking at a data accessibility spike that involves pulling one episode each from A/B/C and using ffprobe to read through one video from D, with a contingency to switch to HDF5 if TFDS isn't available. The known limitations section notes that source D depends on having a local 30 GB copy that won't be in git and reviewers without it will skip it automatically, plus that copy is a re-encoded 512×288 / 30fps version different from the official original, so visual conclusions can't be generalized. D is only for verifying the degradation path, has small sample size and no actions, so it doesn't enter the default training subset quota. I also need to add a row to the 无损/有损边界 table noting that annotation interval seconds are authoritative in the "必须无损" column.



</details>

<details>
<summary><b>1 次工具调用</b> — copilot_multiReplaceString x1</summary>

1. **`copilot_multiReplaceString`** — Replacing 7 lines with 14 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 6 处修改，第 255-761 行

```

<details>
<summary><i>推理过程（2 段）</i></summary>

I need to make a few updates to the documentation. First, I'll change the `upstream_id` reference in section 5 from D's current format to use `narration_id` instead. Then in section 8.1, I should refine the embodiment description — since it combines head camera and hand perspective, `human_ego` might be more accurate than just `human_hand`. Let me update the idempotency key section to reflect this change.

Updating the idempotency key definition...



</details>

<details>
<summary><b>1 次工具调用</b> — copilot_multiReplaceString x1</summary>

1. **`copilot_multiReplaceString`** — Replacing 3 lines with 4 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 3 处修改，第 465-739 行

```

<details>
<summary><b>1 次工具调用</b> — copilot_findTextInFiles x1</summary>

1. **`copilot_findTextInFiles`** — Searched for regex `本地已有副本|EPIC_KITCHENS_ROOT|无 action|human_hand|derived_from_seconds@30fps|1652` (`**/docs/plan.md`), 7 results

</details>

There's an appendix I haven't seen. Let me read it.

<details>
<summary><b>2 次工具调用</b> — copilot_readFile x1, copilot_multiReplaceString x1</summary>

1. **`copilot_readFile`** — Read `docs/plan.md`, lines 955 to 1023
2. **`copilot_multiReplaceString`** — Replacing 53 lines with 90 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 2 处修改，第 967-1006 行

```

<details>
<summary><b>1 次工具调用</b> — copilot_findTextInFiles x1</summary>

1. **`copilot_findTextInFiles`** — Searched for regex `epic|EPIC|D \u7684|human_` (`**/docs/plan.md`), 55 results

</details>

<details>
<summary><i>推理过程（2 段）</i></summary>

I'm verifying the document consistency — checking that section 6's export section correctly states D isn't in the default quota around line 490,



</details>

<details>
<summary><b>1 次工具调用</b> — copilot_readFile x1</summary>

1. **`copilot_readFile`** — Read `docs/plan.md`, lines 27 to 42

</details>

Done. `` is updated throughout.

## 核实到的事实（改动的依据）

EPIC-KITCHENS-100 官方生态远不止时序动作检测：5 个 challenge（Recognition / Detection / Anticipation / UDA / Multi-Instance Retrieval），下载包里附带**陀螺仪 + 加速度计**，另有 **EPIC-Fields**（671 视频 / 18.7M 帧的 6-DoF 相机外参）、**VISOR**（271K 手-物掩膜 + 67K 接触关系）、**EPIC-SOUNDS**、**HD-EPIC**。

## 结论：保留 EPIC-KITCHENS-100，但用法全改

**两个错误结论已作废并在文档里留痕**（写进 §1.1 开头与 §9 的 `rejected.md` 计划）：

1. ~~本地重编码副本 = 数据来源~~ → 官方发布为权威，本地件降级为**镜像**，新角色是"验证 fps 不同的两个副本能否被 provenance 表达并重算帧号"。
2. ~~D 无 action、无 state~~ → **action 存在，只是表示层级不同**；state 真实存在（IMU 6 维 + 相机位姿 7 维）。

## 由此逼出的 schema 改动

| 新增 | 位置 | 为什么只有 D 能暴露 |
|---|---|---|
| `SignalSpec.level`（`per_frame_continuous` / `per_frame_discrete` / `episode_label` / `absent`） | §2.2a | A/B/C 的 action 全是"逐帧定宽向量"，这个隐含假设从未被写下来也从未被检验 |
| `Channel.origin` + `Provenance.signal_origin`（`measured`/`estimated`/`interpolated`/`annotated`/`synthesized`） | §2.2a、§2.2f | A/B/C 的 state 全是 `measured`，"可信度"维度整个不存在 |
| `EpisodeBoundary.success_adjudicator` | §2.2g | C 的 `success=None` 是"未知"，D 是"没有评判者"——schema 里长得一样，语义相反 |
| `Capabilities.has_imu` / `has_camera_pose`，并明确**逐 episode** | §2.2e | IMU 只覆盖部分视频、位姿只覆盖 671/700，同源必然出现 capability 不同的两条 episode |
| 通道级 `camera_*_abs` / `imu_*` space、`frame="sensor"`、`rotation.compose=None` | §2.2a | 一个完整 6-DoF 位姿同时 `metric_convertible=false`（SfM 无绝对尺度） |

## 质检侧

- 门控从 `required_capabilities` 扩展为 **`+ required_level`**——否则 `ACTION_JERK` 会在 D 上以 KeyError 告终（第四个假阳性陷阱）。
- **`origin != "measured"` 的通道 severity 自动降级 FAIL → REVIEW**，由领域层强制（不变量 13）——COLMAP 的跳变是重建失败，不是数据损坏（第五个陷阱）。
- 新增 `POSE_COVERAGE`（D 专用）；不变量 3 重写为覆盖 `level`；新增不变量 13–15。

## 取用范围（§1.1 表格）

必取 annotations + EPIC-Fields，取少量 IMU（刻意挑 1 个有、1 个没有，制造 capability 不齐），VISOR / EPIC-SOUNDS / 官方视频本轮不取并登记进 §11 已知局限。另外补上了此前漏掉的 **CC BY-NC 4.0 非商业许可**约束——它会传染给任何混入 D 的导出子集。

---

## 第 4 轮 — 2026-08-08 09:04 UTC

> commit the changes

<sub>claude-opus-5 · 输出 2,223 tokens · 1m 15s</sub>

<details>
<summary><b>2 次工具调用</b> — run_in_terminal x2</summary>

1. **`run_in_terminal`** — $(info) Enable [shell integration](https://code.visualstudio.com/docs/terminal/shell-integration) to improve command detection

    ```sh
    cd . && git status --short && echo '---' && git --no-pager diff --stat
    ```
2. **`run_in_terminal`** — $(info) Enable [shell integration](https://code.visualstudio.com/docs/terminal/shell-integration) to improve command detection

    ```sh
    cd . && git --no-pager log --oneline -10 && echo '--- gitignore ---' && cat .gitignore 2>/dev/null || echo 'no .gitignore'
    ```

</details>

<details>
<summary><i>推理过程（2 段）</i></summary>

I'm deciding to just commit plan.md since that's what was requested, though adding a .gitignore for .DS_Store would be helpful. I'll use a conventional commit format with a Chinese message describing the refactoring of source D.



</details>

<details>
<summary><b>1 次工具调用</b> — run_in_terminal x1</summary>

1. **`run_in_terminal`** — $(info) Enable [shell integration](https://code.visualstudio.com/docs/terminal/shell-integration) to improve command detection

    ```sh
    cd . && git add docs/plan.md && git commit -m 'docs(plan): 重构来源 D —— 官方发布取代本地副本，action 是层级差异而非缺失' -m '核实 EPIC-KITCHENS-100 官方生态（5 个 challenge、IMU、EPIC-Fields、VISOR、
    EPIC-SOUNDS）后推翻两个错误结论：
    - 本地 512x288/30fps 重编码副本不是权威来源，降级为可选镜像，新角色是验证
      「同一 episode 在两副本里 fps 不同」时 provenance 能否表达并重算帧号
    - D 并非「无 action、无 state」：action 是 episode 级符号标签(verb+noun+区间)，
      另有 IMU(实测) 与 EPIC-Fields 相机位姿(SfM 估计，尺度任意) 两路逐帧信号' -m '由此新增的 schema 维度：
    - SignalSpec.level：表示层级，暴露「action 必是逐帧定宽向量」这一隐含假设
    - Channel.origin / Provenance.signal_origin：measured/estimated/interpolated/
      annotated/synthesized，A/B/C 全为 measured 故该维度此前不存在
    - EpisodeBoundary.success_adjudicator：区分「评判者存在但未知」与「无评判者」
    - Capabilities.has_imu / has_camera_pose，并明确 capability 逐 episode 而非逐 source' -m '质检与不变量：
    - 门控从 required_capabilities 扩展为 + required_level
    - origin != measured 的通道 severity 自动降级 FAIL -> REVIEW，由领域层强制
    - 新增 POSE_COVERAGE 规则；不变量 3 重写以覆盖 level，新增不变量 13-15
    - 补记 CC BY-NC 4.0 非商业许可约束与 VISOR/EPIC-SOUNDS 未取用的已知局限'
    ```

</details>

已提交：`1458419` — `docs(plan): 重构来源 D —— 官方发布取代本地副本，action 是层级差异而非缺失`（266 插入 / 101 删除）。

一点提醒：仓库里没有 `.gitignore`，`` 仍是未跟踪状态。要我加一个 `.gitignore` 吗（`.DS_Store`、`config/sources.local.yaml`、`store/`、`__pycache__/` 等——后两项 plan 里已经预设会用到）？
