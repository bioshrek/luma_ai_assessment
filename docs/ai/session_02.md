# Session 2 — according to plan at `docs/plan.md`, help me understand dataset source A from a…

[← 回目录](index.md) · 由 `scripts/render_ai_sessions.py` 从 [session_2.json](../ai_chat_sessions/session_2.json) 生成，请勿手改。

| | |
| --- | --- |
| 开始时间 | 2026-08-08 03:41 UTC |
| 阶段 | 源 A |
| 轮次 | 10 |
| 模型 | claude-opus-5 |
| 工具调用 | 20 |
| 改动文件 | 1（22 处）|
| 输出 token | 41,256 |
| 墙钟时间 | 11m 45s |

---

## 第 1 轮 — 2026-08-08 03:41 UTC

> according to plan at `docs/plan.md`, help me understand dataset source A from a perspective who has no prior knowledge about robotics, but with knowledge of LLM computer use agent and temporal action detection.

<sub>claude-opus-5 · 输出 3,652 tokens · 1m 10s</sub>

I'll read the plan first to see what dataset source A refers to.

<details>
<summary><b>4 次工具调用</b> — copilot_readFile x2, copilot_fetchWebPage x1, vscode_fetchWebPage_internal x1</summary>

1. **`copilot_readFile`** — Read `docs/plan.md`, lines 1 to 400
2. **`copilot_fetchWebPage`** — 
3. **`copilot_readFile`** — Read `docs/plan.md`, lines 400 to 520
4. **`vscode_fetchWebPage_internal`** — Fetched https://huggingface.co/datasets/lerobot/pusht

</details>

<details>
<summary><i>推理过程（2 段）</i></summary>

I'm thinking about how to explain PushT to someone familiar with LLM computer-use agents—basically framing it as a mouse-drag agent where the model takes a 96x96 observation image and outputs target coordinates for where to move the pusher next in the 512x512 simulation space, similar to how a computer-use agent predicts click positions. Each episode corresponds to one recording, so the temporal action detection aspect maps naturally to predicting sequential pusher positions across a trajectory. The state represents the agent's current position while actions specify the target position that a PD controller tracks toward—similar to how recording mouse position differs from the intended click target. The dataset contains 206 episodes across ~25,650 frames at 10 fps (roughly 124 frames per episode), stored as parquet files with accompanying videos; success is measured by whether the T-block achieves >95% coverage of the target pose. I'm choosing this dataset because its 2D pixel-space absolute coordinates, lack of gripper/arm complexity, and multimodal task structure (many valid ways to push the block to goal) make it ideal for stress-testing the unified schema and validating which QC rules should be skipped.



</details>

## Source A = `lerobot/pusht` — what it actually is

**One-line translation into your world:** it's a recording of ~200 human "mouse-drag" sessions in a 2D game, stored as one Parquet table per session plus a matching screen-capture video. The "robot" is a circle you drag with the mouse; there is no arm, no joints, no gripper.

### The task
From the Diffusion Policy paper (Chi et al. 2023). A 2D physics sim: a T-shaped block sits on a plane, a circular "pusher" nudges it until the T lands on a green target outline. A human demonstrated it by moving the mouse; the sim runs at 10 Hz.

### The data model, in computer-use-agent terms

| pusht field | shape | Computer-use-agent analogue |
| --- | --- | --- |
| `observation.image` | 96×96×3 video frame | the screenshot the agent sees |
| `observation.state` | 2 floats | current cursor position (x, y) |
| `action` | 2 floats | **target** cursor position for the next step, i.e. `mouse.moveTo(x, y)` |
| `timestamp` / `frame_index` | scalar | wall-clock offset / step index inside the session |
| `episode_index` | scalar | session id |
| `next.reward` / `next.done` / `next.success` | scalar | RL leftovers: shaped score, episode end, task-completed flag |

So an *action* here is not a discrete tool call — it's a **dense, continuous, per-frame** signal: 10 (x, y) pairs per second, every single frame labelled. A low-level controller inside the sim then servos the pusher toward that target, which is why `state` lags `action` by a bit rather than equalling it.

### Scale
206 episodes, 25,650 frames total, 10 fps → ~125 frames ≈ **12.5 s per episode**. The whole low-dimensional part is 7.7 MB of Parquet; videos are separate MP4s (AV1, 96×96).

### On-disk layout (LeRobot v2.0 format)
```
meta/info.json                              # schema: fps, feature dtypes/shapes, path templates
data/chunk-000/episode_000000.parquet       # one file per episode, one row per frame
videos/chunk-000/observation.image/episode_000000.mp4
```
`meta/info.json` is the machine-readable schema — that's the thing `docs/plan.md` means by "靠 `meta/info.json` 驱动通道映射": the adapter reads feature names/shapes from it instead of hardcoding.

### How it compares to temporal action detection data
This is the axis where your TAD intuition needs to be inverted:

| | EPIC-KITCHENS (source D) | pusht (source A) |
| --- | --- | --- |
| video | untrimmed, 27 min | already trimmed, 12 s = exactly one attempt |
| annotation | **sparse intervals**: `[start, end, verb, noun]` | **dense per-frame** continuous vector |
| what a "sample" is | a segment you must *localize* inside the video | the whole file *is* the segment |
| label semantics | discrete class | 2D continuous control target |

That's precisely why the plan sets `episode = one annotation segment` for D — to make D's granularity match A/B/C. In TAD terms: robot episodes are pre-trimmed clips with a frame-level regression target; there is nothing to localize.

### Why the plan picked it (the parts that matter for schema design)
1. **Non-robot embodiment.** 2 dims, no arm, no gripper. It's the source that breaks any schema that assumes "N joints + a gripper". QC rules like `GRIPPER_STUCK` must resolve to `SKIPPED`, not `PASS`/`FAIL`.
2. **Absolute task-space coordinates.** `action` is *where to go*, not *how much to move* — like `moveTo(x,y)` vs `moveRel(dx,dy)`. Source C is the delta variant. A unified schema must record which one it is (`is_delta`, `space="cartesian_2d"`), otherwise a downstream consumer integrates deltas onto absolutes and gets garbage.
3. **Pixel units, not metres.** The values live in the sim's 512×512 coordinate space. Same problem as a click coordinate without a screen resolution: not convertible to physical units without a scene scale. Hence `metric_convertible=false` in `docs/plan.md` — converting anyway would be fabricating data.
4. **Real timestamps + 10 Hz**, versus B's 50 Hz and C's synthesized timestamps. It's the low-frequency end that motivates the √N sampling smoothing in the export step (frame count ≠ information content).

### Two gotchas worth knowing before you write the adapter
- `observation.state` and `action` are both 2D positions in the same space, so they're highly correlated by construction. That's the same trap `docs/plan.md` flags for ALOHA in `STATE_ACTION_ECHO` — a correlation-based check flags the entire dataset. The real bug signature is *bit-identical* values (a collection script that logged the readback instead of the command).
- The video is 96×96 and AV1-encoded. Pixel-level QC (black frames, exposure) is near-meaningless at that resolution; only structural checks (frame count vs Parquet row count, camera count) are worth running — consistent with the `--no-video` default.

---

## 第 2 轮 — 2026-08-08 03:58 UTC

> my understanding for source A, correct me if I'm wrong:
> - one episode has it's own task. and each task has a target position.
> - in each episode, 10 trajectories/frames are written every seconds, where action represents the target action, observation.state represents the current position. that means we don't record an "action" the pusher actually takes, but observe the block's current position.

<sub>claude-opus-5 · 输出 3,340 tokens · 1m 01s</sub>

<details>
<summary><i>推理过程（2 段）</i></summary>

I need to clarify something about the PushT dataset structure. There's actually only one task across all episodes—the goal is always to push the T-shaped block onto the fixed T-shaped target. What varies between episodes is the initial configuration of the block and agent positions, not the target itself. The task in LeRobot terms is a single natural language string that applies uniformly to all data points in this dataset.

Also, I should correct my understanding of what the state represents: `observation.state` contains only the pusher/agent's (x, y) position in 2D, not the block's pose. The block position is only observable through the image observations, not the low-dimensional state vector. And regarding actions—while the action represents a commanded target position, the actual executed motion is recoverable from the state sequence by looking at how the agent's position changed from one timestep to the next, so the realized motion is there, just as a derived quantity.

To clarify the action-state relationship: action is the command (desired target), and state is the measurement (what actually happened). It's like a computer-use agent where the action is `moveTo(x,y)` but the cursor ends up wherever the OS/physics actually resolved it—the agent might not reach the target if it collides with the block. Also, the 10 Hz refers to frames per second, not trajectories; one episode is one full trajectory, and each row in the dataset is one frame/timestep. The PD controller drives the agent toward the action target over one step, so the state at the next timestep moves partway toward the commanded action.



</details>

Two corrections, one of them important.

<details>
<summary><b>2 次工具调用</b> — copilot_fetchWebPage x1, vscode_fetchWebPage_internal x1</summary>

1. **`copilot_fetchWebPage`** — 
2. **`vscode_fetchWebPage_internal`** — Fetched https://raw.githubusercontent.com/huggingface/gym-pusht/main/gym_pusht/envs/pusht.py

</details>

<details>
<summary><i>推理过程（2 段）</i></summary>

I need to correct my understanding of the dataset structure. There's actually only one fixed goal pose at coordinates [256, 256, π/4] that remains constant across all episodes, while what changes is the starting configuration—the agent and block positions are randomized within specific ranges, and the block's angle varies uniformly. The task itself is represented as a single natural-language string with a task_index of 0 for all rows, which differs from how LeRobot typically structures tasks.

For the second part: `observation.state` contains only the agent's (pusher's) own position as a 2D vector, not the block's position—that's stored separately in the image. The action represents the target position the agent should move toward, and what actually happens is captured implicitly in the state difference between consecutive timesteps. The system runs a PD controller with specific gains over multiple physics substeps to drive the agent toward the action target, so the commanded action and achieved motion are distinct, especially when the block obstructs the pusher's path.



</details>

Point 1 is wrong, point 2 is half right — and the wrong half matters for your schema.

## 1. "Each episode has its own task with its own target position" — no

`meta/info.json` says `"total_tasks": 1`. All 206 episodes share one task string, and every row has `task_index = 0`. In the env, the goal is a hardcoded constant:

```python
self.goal_pose = np.array([256, 256, np.pi / 4])   # x, y, theta
```

What varies per episode is the **starting state**, sampled at reset: agent xy ∈ [50,450]², block xy ∈ [100,400]², block angle ∈ [−π, π]. So the diversity is in initial conditions, not in goals.

Also note the goal pose is **not a column in the dataset** — it's implicit in the env and visible only as the green outline drawn in the video. A dataset-level constant, not per-episode metadata. (For the plan: it belongs in `raw_extra` / source-level config, not in a per-episode field.)

Episodes terminate when coverage > 95%, so lengths vary (~125 frames on average) and `next.success` is true only on the final frame.

## 2. `observation.state` is the **pusher's** position, not the block's

This is the important correction. In `lerobot/pusht`, `observation.state` has `shape: [2]` — it's `agent_pos`, the circle's (x, y). The T-block's pose is **not in the low-dimensional data at all**; it exists only as pixels in the 96×96 video.

(The env *can* emit a 5-dim state `[agent_x, agent_y, block_x, block_y, block_angle]` under `obs_type="state"`, and a 16-dim keypoint variant — but this dataset was recorded with the pixels+agent_pos variant.)

Consequence for your project: pusht is a source where the **only** way to know whether the task is progressing is the video. Any structural-only QC pass (`--no-video`) can check the pusher's trajectory but is blind to whether the block moved at all. Worth stating explicitly as a known limitation.

## 3. "We don't record the action the pusher actually takes" — you do, derived

The commanded/achieved split is the core robotics convention, and both sides are present:

| | column | meaning |
| --- | --- | --- |
| commanded | `action[t]` | *intended* target position of the pusher (from the human's mouse) |
| achieved | `observation.state[t+1] − observation.state[t]` | what actually happened |

Between two recorded frames the sim runs 10 internal physics substeps (`dt=0.01`, `control_hz=10`) of a PD controller, `a = k_p·(action − pos) + k_v·(0 − vel)` with `k_p=100, k_v=20`. The pusher accelerates toward the target and generally **does not arrive** — especially when the T-block is in the way. That residual, `action[t] − state[t+1]`, *is* the contact/tracking error.

Your computer-use-agent analogue: `action` = `mouse.moveTo(x, y)` the agent issued; `state` = where the cursor actually ended up after the OS/physics had its say. You have both the intent and the outcome.

This also makes pusht one of the sources where `STATE_ACTION_ECHO` in `docs/plan.md` is genuinely applicable — `action` and `state` live in the same 2D pixel space with the same units, so a broken collection script logging the readback instead of the command would produce bit-identical columns. On a healthy episode the PD tracking error guarantees they differ.

---

## 第 3 轮 — 2026-08-08 04:27 UTC

> so the dataset is to capture human demonstration where the annoatator demonstrate the next action based on the current state(the block's position, angle and the target outline). And the goal is to teach the agent to figure out the next target position of the pusher based on the current state. right?

<sub>claude-opus-5 · 输出 1,757 tokens · 0m 34s</sub>

<details>
<summary><i>推理过程（2 段）</i></summary>

So the key distinction is that the human demonstrator sees the block position, angle, and target outline directly on screen, but the policy only has access to the 96×96 pixel image plus the agent's xy coordinates — it has to infer everything else from the visual input. The target outline is static in the image, so the policy learns to map observations to actions through behavior cloning from the teleoperated demonstrations.

There's a critical challenge here: PushT has multiple valid strategies (pushing from different angles), so a naive policy that averages the human's actions across demonstrations would collapse to a useless middle ground. That's precisely why the Diffusion Policy paper used this benchmark — to show how diffusion models handle multimodal behavior better than regression. Also



</details>

Correct in substance. Three refinements worth having in your head before you write the adapter/QC code.

## 1. What the human sees vs. what the policy sees

The human demonstrator sees the rendered 512×512 window: block, pusher, green target outline. The policy sees `observation.image` (96×96 RGB) + `observation.state` (pusher xy). Block position and angle are **not columns** — they only exist as pixels. So "based on the current state" is true for the human, but for the trained policy the block state must be inferred visually.

This is why it's a *visuomotor* benchmark rather than a state-based one, and why the plan's `--no-video` default is a real limitation for this source specifically: without the video you have the labels but not the observations.

## 2. This is behavior cloning — labels are recorded, not annotated

Worth naming the difference from your TAD world explicitly:

| | EPIC-KITCHENS | pusht |
| --- | --- | --- |
| how labels are produced | separate manual annotation pass over existing video | fall out of the recording itself — the teleoperator's mouse position *is* the label |
| label noise looks like | wrong class, imprecise boundaries, annotator disagreement | hesitation, idle frames, inefficient/failed strategies, sloppy demonstrations |

There is no annotation step and no inter-annotator agreement. The "label quality" problem becomes **demonstration quality** — which is exactly what `STATIC_EPISODE` (demonstrator paused / episode is mostly nothing) and `ACTION_JERK` (mouse jumped, teleop glitch) in `docs/plan.md` are trying to catch.

## 3. "Predict the next target position" is right, but naively regressing it fails

This is the whole reason PushT exists as a benchmark. The demonstrations are **multimodal**: from a given state, pushing around the left side of the T and pushing around the right side are both valid and both appear in the data. A model trained with MSE to predict *the* next action averages them and drives straight into the block. The Diffusion Policy paper used this dataset precisely to show that you need a distribution-modelling head (diffusion, VQ-BeT) rather than a point regressor.

Two consequences:
- Real policies predict an **action chunk** (a horizon of ~8–16 future targets) rather than one step, so they commit to a strategy instead of dithering between modes.
- The mapping isn't cleanly Markov — the human's next action depends on which strategy they're mid-way through, not just on the current frame.

Your computer-use-agent analogue: two different click paths can both accomplish the same UI goal; a model that averages the two coordinates clicks on empty space.

## Not affecting your understanding, but relevant to the pipeline

`next.reward` (coverage of the target zone) and `next.success` come from the **simulator**, not the human. That's free ground truth that real-robot sources (C) don't have — which is why `has_reward` is a capability flag and not an assumption.

---

## 第 4 轮 — 2026-08-08 04:37 UTC

> so the obversation.state is given by the simulator to the agent, right? and the agent is required to determine the coverage of the target zone and the termination state, right?

<sub>claude-opus-5 · 输出 1,781 tokens · 0m 31s</sub>

<details>
<summary><i>推理过程（2 段）</i></summary>

I need to correct two misconceptions here. First, `observation.state` is indeed what the simulator produces and feeds to the policy during inference—in this case, the pusher's xy coordinates. In a real robot, this would be proprioceptive data from the encoders, so it's really just the simulator's equivalent of a sensor reading. Second, I was wrong about the agent computing coverage and termination—those are actually handled by the environment itself. The agent's only job is to output an action.

The key distinction is that `next.reward`, `next.done`, and `next.success` come from the simulator's ground-truth geometry (using shapely to intersect the block and goal polygons), and they serve two purposes: scoring policies at evaluation time and providing training signals for RL algorithms. But for behavior cloning approaches like Diffusion Policy, these labels don't factor in at all—the policy just ignores them. The actual contract is simple: given an image and agent position, output an action. Nothing more.

There's an interesting contrast here with datasets like RT-1 and OXE-style formats, where `terminate_episode` is actually part of the action space itself, so the policy does predict termination as an output. That's a different design choice—worth noting that `terminate_episode` should be flagged as a non-physical channel and handled separately from motor commands.



</details>

First half yes, second half no.

## `observation.state`: yes, it's an input

The simulator produces it and hands it to the policy as input. In real robots the same field comes from joint encoders (proprioception) — pusht is the simulated equivalent. The policy's contract in this dataset is:

$$(\text{image}_{96\times96},\ \text{agent\_pos}_{2}) \longrightarrow \text{action}_{2}$$

That's it. Two floats in, two floats out.

## Coverage / termination: computed by the environment, never by the agent

`next.reward`, `next.done`, `next.success` come from the simulator's **privileged** access to ground truth that the agent never sees:

```python
coverage = goal_geom.intersection(block_geom).area / goal_geom.area   # shapely polygon overlap
reward   = clip(coverage / 0.95, 0, 1)
terminated = is_success = coverage > 0.95
```

The agent has no access to the block polygon — only pixels. It is not asked to predict any of this. These columns are used for:

- **evaluation**: rolling out a trained policy and scoring it
- **RL training signal**, if you were doing RL

A behavior-cloning policy (Diffusion Policy, ACT, VQ-BeT) **ignores these columns entirely** during training. They're supervision *about* the data, not part of the input→output mapping.

Your computer-use-agent analogue: it's the task-completion verifier watching the screen, not something the agent emits.

## The interesting contrast: source C *does* put termination in the action space

Look at your own appendix entry for `berkeley_autolab_ur5`:

```python
"action": {
   "world_vector":              float32[3],
   "rotation_delta":            float32[3],
   "gripper_closedness_action": float32[1],
   "terminate_episode":         float32[3],   # <- the policy outputs this
}
```

RT-1-style datasets make "I'm finished" a **channel the policy predicts** — the equivalent of an agent emitting a `task_complete()` tool call. pusht does not; the env decides.

This is a concrete schema consequence for `docs/plan.md`: `terminate_episode` is a non-physical channel sitting inside an otherwise physical action vector. It needs a distinct `role` (something like `"control_flag"`, not `arm`/`gripper`), and it must be **excluded from** `ACTION_RANGE` limit checks and `ACTION_JERK` statistics — a one-hot flag flipping 0→1 on the last step is a legitimate step change that would otherwise trip the jerk rule on every single episode.

Worth adding to appendix A's item 2 for source C, alongside the `is_delta` note.

---

## 第 5 轮 — 2026-08-08 04:46 UTC

> you mean the next.reward and next.success are not part of the training data? so after the trained agent is expected to stop by itself or by the simulator's termination signal?

<sub>claude-opus-5 · 输出 1,803 tokens · 0m 34s</sub>

<details>
<summary><i>推理过程（2 段）</i></summary>

Reward and success signals are included in the dataset file itself but don't factor into the behavioral cloning loss—they're only used during evaluation and for filtering the training data. At evaluation time in PushT, the environment controls when episodes end: either when coverage exceeds 0.95 or when hitting the time limit of 300 steps, so the policy itself has no stopping mechanism and just keeps generating actions until the environment terminates the rollout.

This points to a real challenge in robotics: without a simulator to compute coverage metrics, you need either human intervention, a fixed time budget, a learned success detector, or a termination action in the action space itself—similar to how computer-use agents emit a `done()` signal or rely on external verification. The reward and success flags are also valuable upstream for filtering out failed demonstrations and flagging successful episodes as high-quality training data, which aligns with their curation strategy.



</details>

## "Part of the training data" depends on the algorithm

They're **in the file**; whether they enter the loss depends on what you train:

| Method | Uses `action` | Uses `reward` / `success` |
| --- | --- | --- |
| Behavior cloning (Diffusion Policy, ACT, VQ-BeT) — what pusht is famous for | yes, as the regression target | no |
| Offline RL (IQL, CQL, Decision Transformer) | yes | **yes, essential** |
| Data curation / evaluation | — | yes, as a filter and a score |

So the honest statement is: BC ignores them, offline RL requires them, and the ingestion layer shouldn't decide on the downstream's behalf.

**This is a genuine issue with your plan.** Section 2.2d lists `reward / done 的具体数值` under 可有损, keeping only a bool-ized success flag. For pusht that's **irreversibly lossy** — `next.reward` is the block/goal polygon overlap ratio, and you cannot recompute it because the block pose isn't in the data. Discarding it silently forecloses every offline-RL consumer. Given the reward column is one float per frame (a few hundred KB across the whole source), the storage argument doesn't hold. I'd move dense reward to 必须无损 and keep only *`discount` / redundant `done` mirrors* as droppable.

## Who stops the episode at eval time

Neither the agent nor a "termination signal the agent reads" — the **rollout harness** stops it:

```python
obs, reward, terminated, truncated, info = env.step(action)
# terminated: coverage > 0.95, computed by the env
# truncated:  TimeLimit wrapper hit max steps
if terminated or truncated: break
```

The policy just keeps emitting 2-float actions forever; the loop around it decides when to quit. The policy has no "done" output at all.

## Why this matters beyond pusht

In simulation the env can compute ground-truth success. **On a real robot there is no such oracle** — nothing computes polygon overlap for you. So real deployments need one of:

1. a fixed time/step budget (crude, most common)
2. a human watching
3. a learned success detector / VLM verifier looking at the camera
4. **a termination channel in the action space** — which is exactly source C's `terminate_episode`, i.e. the policy declares itself done

Option 4 is the direct analogue of a computer-use agent emitting `task_complete()`, and option 3 is the analogue of an external LLM judge scoring the final screenshot.

Concretely for your four sources: A and B have simulator-computed success (`has_reward=True`), C encodes termination as a policy output, D has neither — the segment boundary came from a human annotator after the fact. Four different answers to "who decides it's over" across four sources is itself a good line for `docs/design.md`, and it argues for recording *where* the episode boundary came from, not just where it is — a natural extension of your `Provenance` value object.

---

## 第 6 轮 — 2026-08-08 04:52 UTC

> so how does it affect the unified schema design that whether the policy dertermine the termination?

<sub>claude-opus-5 · 输出 3,418 tokens · 0m 54s</sub>

<details>
<summary><i>推理过程（2 段）</i></summary>

The key issue is that "who determines termination" fundamentally changes where a column lives in the schema. In system C, `terminate_episode` is an action output—part of what the policy decides. But in systems A and B, `next.done` or `next.success` are environment observations—information about the episode state. This same concept ends up in completely different structural positions depending on the source, which breaks naive schema unification when you try to flatten everything into a single ActionSpec or reward column.

To handle this, the schema needs to extend the channel-level `role` field to distinguish non-physical control flags (like `role="control_flag"` with `is_physical=false`), and add an episode-level `termination_source` field that records whether the policy or environment ended the episode. This cascades into QC rules: action range checks become role-specific since a flag's valid range is {0,1} not physical limits; jerk detection must skip non-physical channels to avoid flagging every C episode; static episode checks shouldn't count the flag toward total travel; and state-action echo comparisons need to filter by physical channels only. There's also an opportunity for a new `TERMINATION_CONSISTENCY` rule that validates the flag behavior matches the declared source—if policy-side, the flag should be 0 everywhere except exactly once at the final step; if environment-side, `done`/`success` should only appear on the last frame.

The ambiguity in `action_dim` is critical: if C's raw action is 10-dimensional but only 7 are physical, and their plan already treats it as 7-dim, they've implicitly dropped the termination flag without recording that decision, which breaks policy training. Either keep the flag with a dedicated role or document the drop in the `transforms` provenance, and split `action_dim` into stored versus physical dimensions for proper statistics. The episode boundary semantics also matter—whether it came from an environment rule (coverage threshold, meaningful success), a teleoperator stopping recording (human decision), a policy flag with RLDS truncation semantics, or other sources each carry different ground-truth implications for the data.

RLDS distinguishes `is_terminal` from `is_last` to separate true termination from truncation, which is exactly the gym distinction—and collapsing them corrupts offline RL bootstrapping since truncated episodes need their final states bootstrapped while terminal ones must not. This is the classic offline RL bug with real technical consequences. If they truncate an episode to fit the frame budget, they're creating an artificial boundary that downstream readers might misinterpret, especially if the success flag gets dropped in the process.

The export record needs to mark when truncation happened, which connects directly to their existing plan. I'm sketching a concrete schema that captures the termination source (environment rule, policy flag, operator, annotator, or fixed length), the end reason (success, timeout, operator stop, annotation bound, or unknown), whether it was truncated in the gym sense, and whether it succeeded—plus channel-level metadata about whether the data is physical.



</details>

Four concrete consequences. This is not a cosmetic issue — it changes column classification, three QC rules, and the correctness of offline RL downstream.

## 1. The same concept sits in different structural positions

| Source | "is it over" lives in | Structural class |
| --- | --- | --- |
| A pusht | `next.done` / `next.success` | environment output — a *label* |
| B aloha | end of recording | nothing explicit at all |
| C ur5 | `action.terminate_episode[3]` | **policy output — inside the action vector** |
| D epic | annotation `segment[1]` | human judgement, post-hoc |

So a flattened `action` vector means "physical command" for A/B and "physical command + a control flag" for C. Any cross-source aggregation over `action` is then mixing units with a boolean. `ActionSpec.channels` needs a `role` value outside the physical set — `role="control_flag"`, or an explicit `is_physical: bool` — and `ActionSpec` needs `physical_dim` distinct from `dim`.

Note your appendix already says C is **7 维（3+3+1）**, which silently drops `terminate_episode`'s 3 columns. That's a defensible choice, but it is a *lossy* one and currently undocumented: data with the flag stripped can never train a policy that knows when to stop. It must land in `provenance.transforms` as `{"op": "drop_channels", "names": ["terminate_episode"], "reason": ...}`, or be kept with a non-physical role.

## 2. Three QC rules break on a non-physical channel

- `ACTION_RANGE`: limits for a one-hot flag are $\{0,1\}$, not `±0.1 m`. Per-role limits, not per-channel-index.
- `ACTION_JERK`: the flag going $0 \to 1$ on the final step is a step change of magnitude 1, orders of magnitude above the channel's p99.9. **Every single C episode would hit REVIEW.** Non-physical channels must be excluded from jerk statistics.
- `STATIC_EPISODE`: "action 总行程" must sum physical channels only, otherwise a flag flip contributes phantom motion.

Same shape of failure as your `STATE_ACTION_ECHO` trap — a rule that's correct in the abstract but produces 100% false positives on one source.

## 3. `terminated` vs `truncated` — the offline RL correctness bug

RLDS carries both `is_terminal` and `is_last`, and they are not synonyms:

- `is_terminal=True` → the episode genuinely ended (goal reached / unrecoverable). Value bootstrapping must stop: $V(s_T) = 0$.
- `is_last=True, is_terminal=False` → the episode was **cut short** by a step limit. The state is still a normal state; bootstrapping must continue: $V(s_T) \neq 0$.

Collapsing them into one `done` boolean corrupts every offline-RL algorithm trained on the export, silently. pusht has the same distinction (`terminated` from coverage vs `truncated` from `TimeLimit`) but the LeRobot dump only preserves `next.done`, so the information may already be gone — worth checking during M0 and recording as a known limitation if so.

## 4. Your exporter creates a fifth kind of boundary

Section 6.5 allows truncating the last episode to fit the frame budget. That manufactures an episode boundary that exists nowhere upstream — and if the truncation cuts off the frame carrying `success=True`, a successful demo silently becomes an unlabeled one. The export record needs `boundary_source: "exporter_truncation"` alongside `frame_start/frame_end`.

## Proposed addition to the domain model

A small value object, parallel to `Provenance` — "where did the episode boundary come from" is exactly the same kind of fact as "where did the timestamp come from":

```python
EpisodeBoundary = {
  "termination_source": "env_rule" | "policy_flag" | "operator" | "annotator" | "exporter",
  "end_reason": "success" | "truncated" | "operator_stop" | "annotation_bound" | "unknown",
  "is_truncated": bool,      # cut short; final state is NOT terminal
  "success": bool | None,    # None = unknown, not False
}
```

Per source: A = `env_rule / success` (or `truncated`), B = `operator / operator_stop`, C = `policy_flag` with `is_truncated` read from `is_last & ~is_terminal`, D = `annotator / annotation_bound, success=None`.

This also earns a ninth QC rule that costs almost nothing:

**`TERMINATION_CONSISTENCY`** — if `termination_source == policy_flag`, assert the flag is 0 on all frames and 1 exactly once, on the last; if `env_rule`, assert `done` appears only on the final frame. Catches episodes that were incorrectly concatenated or split during ingestion — a failure mode none of your current eight rules can see. Depends on a new capability `has_termination_signal`, so it `SKIPPED`s cleanly on B and D, which is another instance of the降级 ≠ 通过 argument you're already making.

Want me to write these changes into `docs/plan.md` — the `EpisodeBoundary` value object in §2.2, the rule in §3, and the C channel-drop note in appendix A?

---

## 第 7 轮 — 2026-08-08 04:56 UTC

> proceed

<sub>claude-opus-5 · 输出 18,744 tokens · 4m 37s</sub>

<details>
<summary><i>推理过程（2 段）</i></summary>

I need to make three specific edits to the plan document. First, I'm adding an EpisodeBoundary value object to section 2.2, possibly as a new subsection, and also updating ActionSpec with control flag and physical property attributes. Next, I'm moving the dense reward specification to the loss-free requirements section. Finally, I'm adding a termination consistency rule to section 3 along with a note about excluding non-physical channels from the action range and jerk constraints.

Beyond those core changes, I'm also updating the SQLite schema to include a boundary_json column, adding boundary_source to the export section, and incorporating several appendix updates including channel drop notes and reward correction items. I need to add a termination judgment row to the comparison table and include EpisodeBoundary in the ubiquitous language table at section 8.1. There's also a note about potential data loss in the LeRobot dump regarding terminated/truncated information that should go in the known limitations section.

I'm noticing the section 3 header currently says "目标 8 条" but the table already has 9 rows, and with the new termination rule it'll be 10 total. I'm updating the header to "目标 10 条（含 2 条来源专用）" to reflect this, and I need to update sections 8.5 and 8.7 which reference "8 条规则" to say 10 instead.

Now I'm carefully preparing the edits using exact string matching. Starting with the ActionSpec block in section 2.2a... Restructuring the table to move per-frame reward/done/terminal markers into the lossless column, replacing the current row 3's lossy cell with discount and redundant done mirrors, then adding a new row. After that, I'll add a subsection on episode boundaries and termination logic, update the rules table in §3 to include termination consistency checks, and refine the action range and jerk criteria to apply only to physical channels. I need to update all three occurrences of "8 条规则" to "10 条" across the document sections. I'm also adding `boundary.py` to the domain file list in section 8.3, and considering a new invariant for section 8.4 around termination signals.



</details>

Applying the changes to the plan now.

<details>
<summary><b>1 次工具调用</b> — copilot_multiReplaceString x1</summary>

1. **`copilot_multiReplaceString`** — Replacing 29 lines with 53 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 4 处修改，第 108-169 行

```

<details>
<summary><b>1 次工具调用</b> — copilot_multiReplaceString x1</summary>

1. **`copilot_multiReplaceString`** — Replacing 6 lines with 9 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 4 处修改，第 227-246 行

```

<details>
<summary><b>1 次工具调用</b> — copilot_multiReplaceString x1</summary>

1. **`copilot_multiReplaceString`** — Replacing 13 lines with 17 lines in `docs/plan.md`

</details>

> _Compacted conversation_

```

**文件改动**

- `docs/plan.md` — 7 处修改，第 267-575 行

```

<details>
<summary><b>4 次工具调用</b> — copilot_readFile x2, copilot_findTextInFiles x1, copilot_multiReplaceString x1</summary>

1. **`copilot_findTextInFiles`** — Searched for regex `next.reward|terminate_episode|7 维|episode.json|附录 A` (`**/docs/plan.md`), 14 results
2. **`copilot_readFile`** — Read `docs/plan.md`, lines 640 to 760
3. **`copilot_readFile`** — Read `docs/plan.md`, lines 760 to 794
4. **`copilot_multiReplaceString`** — Replacing 11 lines with 23 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 5 处修改，第 652-791 行

```

<details>
<summary><b>2 次工具调用</b> — copilot_findTextInFiles x1, copilot_replaceString x1</summary>

1. **`copilot_findTextInFiles`** — Searched for regex `8 条|8条|7 维` (`**/docs/plan.md`), 3 results
2. **`copilot_replaceString`** — Replacing 1 lines with 1 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 1 处修改，第 31-31 行

```

<details>
<summary><b>1 次工具调用</b> — copilot_readFile x1</summary>

1. **`copilot_readFile`** — Read `docs/plan.md`, lines 228 to 252

</details>

All edits applied. `` now carries the `EpisodeBoundary` design end to end:

- **§1 source table / Appendix A.C**: C is now `10 dim = 7 physical + 3 terminate_episode` control flags; dropping them is an explicit `drop_channels` transform, plus `is_last` vs `is_terminal` truncation semantics.
- **§2.2a/d/e/g**: `physical_dim` + `is_physical`, `control_flag` role, reward moved to 必须无损 (pusht coverage is irrecoverable), `has_termination_signal`, and the new §2.2g `EpisodeBoundary` value object.
- **§3**: new `TERMINATION_CONSISTENCY` rule (10 rules total), `ACTION_RANGE`/`ACTION_JERK`/`STATIC_EPISODE` scoped to physical channels, plus two new 设计要点 bullets.
- **§4**: `boundary_json` on `episodes`.
- **§6**: exporter truncation must write `termination_source="exporter"` / `is_truncated=true`; `boundary` + `physical_dim` added to JSONL fields.
- **§8**: `EpisodeBoundary` in the ubiquitous-language table, `domain/boundary.py` in the tree, two new invariants, M5 milestone updated to 10 rules.
- **§11 / Appendix A.A / A.D / comparison table**: upstream `terminated`/`truncated` loss risk, corrected reward note, `boundary` block in D's example, and a 终止判定 row (env rule / operator / policy flag / annotator).

---

## 第 8 轮 — 2026-08-08 06:25 UTC

> further discussion:
> - why not stored the source as it is, but writing adapters for reading it, instead of storing unified data?

<sub>claude-opus-5 · 输出 3,587 tokens · 1m 02s</sub>

<details>
<summary><i>推理过程（2 段）</i></summary>

The user is asking why we



</details>

<details>
<summary><b>1 次工具调用</b> — copilot_readFile x1</summary>

1. **`copilot_readFile`** — Read `docs/plan.md`, lines 196 to 228

</details>

<details>
<summary><i>推理过程（2 段）</i></summary>

I'm thinking through the core tension here—the question frames this as a binary choice between adapting data at read time versus materializing a normalized copy, but the plan actually does both. The real decision factors are read amplification costs, whether we need deterministic results, and where quality control happens. I should clarify that keeping raw data alongside adapters isn't either/or; it's a hybrid approach that gives us flexibility.

The case for materializing the normalized layer is strong: read-time adaptation means every downstream consumer pays the decode cost repeatedly, and with RLDS TFRecords requiring sequential scans and TensorFlow in-process, that's expensive at training scale. Materializing to Parquet with row-group indexing gives random access and amortizes the cost. There's also a dependency containment argument—TensorFlow on macOS with Python 3.11 is flagged as a major risk, so isolating that ugliness to a one-shot batch job and letting downstream consumers use only PyArrow is cleaner. And quality control needs a fixed artifact to validate against; a verdict like "episode X passes ACTION_JERK" only makes sense if it references a stable, materialized version. ss to frame data and consistent episode counts across sources. Mixing batches from multiple adapters requires uniform random access, which breaks down when each source has its own lazy read model. Errors in normalization get caught and recorded once in the catalog rather than surfacing mid-training. Plus, HuggingFace and OXE are remote, so pushing normalization to read time adds network latency to the hot path.

There are legitimate cases where read-time adapters make sense — mainly when dealing with massive datasets that get used sparingly, like normalizing 100 TB of OXE just to access 1%. But that's solvable by normalizing only the selected slice while keeping the catalog lazy, which the existing `list_episodes` function already supports. Storage doubling and drift from the source can be managed through provenance records and checksums, and re-normalization is idempotent. Early schema churn is expensive with materialization, but that's addressable through treating the normalized layer as disposable derived data, keeping the raw version intact, and versioning adapters in provenance so only stale data gets rebuilt.

The key distinction here is that the plan unifies *structure*, not values — it explicitly avoids semantic unification. The normalized layer is just re-encoding into one container format with per-channel metadata preserved, not a lossy fusion. Everything stays lossless because the raw data and transform are recorded, so regeneration from source is always possible. This is fundamentally the ELT versus virtual federation tradeoff, and the deciding factor comes down to read latency and consistency requirements.

I should probably add a section explaining why materializing a normalized layer makes sense here rather than just using adapters on



</details>

Good question — and worth pinning down precisely what's being traded, because the plan doesn't actually choose "adapters *or* unified storage." It has adapters (`SourceAdapter.normalize`), keeps `raw/` staging, and *also* persists the adapter's output to `normalized/`. So the real question is narrower:

> **should `normalize()` run once at ingest and be persisted, or run on every read?**

That's the classic ETL-vs-virtual-federation axis. The deciding factor is: **read amplification × per-read decode cost × whether anything needs to attest to an immutable artifact.**

## Why materialize here

**1. Read cost is wildly asymmetric across the four sources.**
RLDS/TFRecord is sequential-only and nested — "give me episode 47, frames 100–160" is an O(scan) operation, and it drags TensorFlow into the reader's process. Parquet with row groups makes that a seek. Training reads each episode hundreds of times; normalization runs once. Paying an RLDS decode per epoch to save disk is the wrong side of the trade.

**2. Dependency blast radius.** Your own §2.3 flags TFDS on macOS + py3.11 as the project's largest risk. If normalization is read-time, that risk becomes a permanent dependency of *every* consumer, forever. Materializing quarantines it in a one-shot batch job with its own environment; everything downstream needs only `pyarrow`.

**3. A QC verdict must point at something that can't change.** `episode_x: ACTION_JERK=REVIEW` is only meaningful if it refers to fixed bytes. With read-time normalization, the verdict describes *the output of a function* — bump the adapter, change the RLDS `action` dict flattening order, and every stored verdict is silently wrong with no way to detect it. Same for §5's resume state machine: `IngestionStage` needs a stable object identity to advance over. Persisted artifact + checksum makes the verdict falsifiable.

**4. "对外契约" requires a materialization.** Appendix A.C says the flattened channel order, once fixed, is a contract. Lazily computed, it isn't a contract — it's just current behavior of the current adapter version. The written `episode.json` + `ActionSpec` is what makes it one.

**5. Cross-source sampling needs a common *physical* layout, not just a logical one.** §6's frame-budget subset selection needs uniform random access and known per-episode frame counts across all four sources. Over four incompatible IO models, that's a distributed query; over uniform Parquet, it's an index lookup.

**6. Errors concentrate in normalization.** Batch → they surface once, land in the catalog, get fixed. Lazy → they surface mid-training-run at 3am, non-reproducibly.

## The honest counter-case

Read-time adapters win when the corpus is huge and read rarely — normalizing 100 TB of OXE to touch 1% of it is pure waste. Your design already hedges this correctly: `list_episodes()` is explicitly "只列，不下载", so the *catalog* is lazy and only the selected slice is materialized. That's the right split.

The two real costs you're accepting:
- **Storage duplication + drift risk** — the normalized copy can go stale against an HF revision. Mitigated by recording the source revision/commit and adapter version in `provenance`, so staleness is *detectable* and re-normalization is targeted.
- **Schema churn is expensive early** — every `ActionSpec` change costs a re-run. Mitigated because `normalized/` is **derived, disposable data**: `raw/` stays, so rebuilding is always possible.

## The framing that makes it defensible

The plan is *not* storing "unified data" in the sense the question implies. §2 explicitly refuses to unify values — no zero-padding, no fake common vector space. What's unified is the **container and the metadata contract**: same file layout, same per-channel `ActionSpec`, same `Capabilities`, same `Provenance`. Values stay in their native units with `metric_convertible=false`.

So the materialized layer is a **re-encoding, not a fusion**. It's lossless (anything not modeled goes to `raw_extra`), it's reversible (`raw/` is kept), and every transformation is recorded. That's why materializing doesn't contradict the "有损变换必须可追溯" principle — it's the mechanism that *implements* it.

If the deliverable were only a catalog + QC report with nothing training on the output, read-time adapters and no `normalized/` would be the better answer. It isn't — §6 exports a training subset.

Want me to write this as a §2.5 "为什么落一层 `normalized/`" in plan.md? It's a near-certain interview question, and the answer is currently implicit in the layout diagram rather than argued.

---

## 第 9 轮 — 2026-08-08 06:31 UTC

> proceed

<sub>claude-opus-5 · 输出 2,289 tokens · 0m 43s</sub>

<details>
<summary><b>1 次工具调用</b> — copilot_replaceString x1</summary>

1. **`copilot_replaceString`** — Replacing 5 lines with 29 lines in `docs/plan.md`

</details>

```

**文件改动**

- `docs/plan.md` — 1 处修改，第 225-225 行

```
Added §2.5 「为什么要落一层 `normalized/`，而不是"原样存源 + 读时用 adapter 转"」 right after the storage layout section — it reframes the question as ETL vs. virtual federation (adapters exist either way; the only question is whether `normalize()` output is persisted), gives the six reasons to materialize, concedes the case where lazy reads win and notes the lazy-catalog hedge already in `list_episodes()`, lists the two accepted costs with mitigations, and closes with the clarification that what's persisted is a re-encoding of *structure*, not a fusion of *values*.

---

## 第 10 轮 — 2026-08-08 06:32 UTC

> commit these changes

<sub>claude-opus-5 · 输出 885 tokens · 0m 39s</sub>

<details>
<summary><b>2 次工具调用</b> — run_in_terminal x2</summary>

1. **`run_in_terminal`** — $(info) Enable [shell integration](https://code.visualstudio.com/docs/terminal/shell-integration) to improve command detection

    ```sh
    cd . && git status --short && git --no-pager diff --stat
    ```
2. **`run_in_terminal`** — $(info) Enable [shell integration](https://code.visualstudio.com/docs/terminal/shell-integration) to improve command detection

    ```sh
    cd . && git add docs/plan.md && git commit -q -F - <<'EOF'
    docs(plan): model episode termination and justify the normalized layer

    - add EpisodeBoundary value object (§2.2g): termination_source / end_reason /
      is_truncated / success, wired into SQLite (boundary_json), export JSONL,
      ubiquitous language, domain layout and invariants
    - separate physical from non-physical action channels: ActionSpec gains
      physical_dim + per-channel is_physical and a control_flag role, so C's
      terminate_episode no longer trips ACTION_RANGE / ACTION_JERK on every episode
    - correct the lossless/lossy table: dense per-frame reward and the
      terminated/truncated distinction are lossless (pusht coverage is
      irrecoverable without block pose); dropping channels must be a recorded
      provenance transform
    - add TERMINATION_CONSISTENCY QC rule (8 -> 10 rules)
    - add §2.5 arguing why normalize() output is materialized instead of
      recomputed per read
    EOF
    git --no-pager log --oneline -1
    ```

</details>

Committed as `ef5d58a` (docs/plan.md only). `` is still untracked — left alone; tell me if you want it committed too.
