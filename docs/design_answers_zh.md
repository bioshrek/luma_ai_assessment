# 设计答辩

作业要求在文档里说明的每一项，逐条回答。这里只放**结论和理由**；实现细节在
[docs/technical_design.md](technical_design.md)，每一次和最初设想不一致的地方在
[docs/adr/](adr/) 里都有一条对应的记录。

文档语言的划分见 [ADR 018](adr/018-delivery-docs-in-chinese.md)：对外交付文档用中文，
工程记录用英文。这是一个决定，不是没翻完。

**目录**

1. [pipeline 架构与各环节职责](#1-pipeline-架构与各环节职责)
2. [统一 schema 的设计与取舍](#2-统一-schema-的设计与取舍)
3. [checkpoint 设计：存什么、存哪、如何幂等](#3-checkpoint-设计存什么存哪如何幂等)
4. [断点恢复是怎么测的](#4-断点恢复是怎么测的)
5. [训练子集采样策略](#5-训练子集采样策略)
6. [要上生产还需要做什么](#6-要上生产还需要做什么)
7. [架构扩展：500 个数据集、5 亿帧、按帧随机读](#7-架构扩展500-个数据集5-亿帧按帧随机读)
8. [已知局限](#8-已知局限)

---

## 1. pipeline 架构与各环节职责

### 1.1 五个阶段

```
discover → fetch → normalize → qc → commit
```

| 阶段        | 职责                                                        | 产物                                  |
| ----------- | ----------------------------------------------------------- | ------------------------------------- |
| `discover`  | 列出上游有哪些 episode，算出幂等键，和 catalog 做差集       | 待处理清单（已存在且未变的直接跳过）  |
| `fetch`     | 把上游原件**逐字**落到 `store/raw/`，不做任何解释           | `store/raw/<source>/…` + staging 句柄 |
| `normalize` | 上游语义 → 统一 schema；这一层是**唯一**懂上游格式的地方    | `frames.parquet` + `episode.json`     |
| `qc`        | 对规范化后的 episode 跑规则集，产出 PASS/REVIEW/FAIL + 指标 | `qc_results` 行                       |
| `commit`    | 把 episode 落成 catalog 里的已提交状态                      | `episodes` 行更新                     |

`export` 和 `report` 不在这条链上：它们是**读侧**，只读 catalog 和 `normalized/`，
可以任意重放。这一点是刻意的——报告如果依赖某次 run 的内存状态，那它就只能在那次 run
里生成一次，事后无法复核。

### 1.2 分层：DDD + Clean Architecture

```
src/rdp/
  domain/          # 实体、值对象、纯领域服务。零 IO
  application/     # 用例编排 + port 协议。只依赖 domain
  infrastructure/  # 源适配器、SQLite 仓储、parquet 存储、原子写、ffprobe、配置加载
  interfaces/      # typer CLI + presenter
```

**箭头只向内。** `domain/` 不 import `sqlite3` / `pyarrow` / `requests`，也不 import
上面任何一层。这条规则从第一天起就由 `import-linter` 强制（`uv run lint-imports`）——
**没有被强制的分层规则活不过一周**，这不是格言，是这个仓库刻意做的一个赌注。

四个 port 就是全部的扩展面：

| port                               | 现在的实现             | 换掉它意味着          |
| ---------------------------------- | ---------------------- | --------------------- |
| `SourcePort`                       | 4 个源适配器           | 加一个新数据源        |
| `EpisodeRepository` + `UnitOfWork` | SQLite                 | 换成 Postgres         |
| `FrameStore` / `BlobStore`         | 本地文件系统 + parquet | 换成 S3 + Lance/chunk |
| `FaultInjector`                    | 生产实现是 no-op       | 崩溃恢复的可测性      |

最后一个值得解释：`FaultInjector` 是一个**为了可测性而进入生产代码的 port**。
它的生产实现什么都不做，测试实现在指定的检查点抛 `Crash`。理由是断点恢复这件事，
不从内部注入就只能靠"跑跑看有没有崩"，而"它没崩"不是一个断言。

### 1.3 加一个新源要付出什么

这不是设想，是 M3 实测的：

- **加源 B（ALOHA）：一行 Python 都没写。** 一条 `sources.yaml`、一条
  `embodiments.yaml`、一个 fixture，和源 A 共用 `LeRobotAdapter`。
- **加源 C（RLDS）：一个适配器 + 130 行标准库 TFRecord 读取 + fetcher 上一个流式方法。**
  `application/` 完全没动，`domain/` 只多了一行。

能做到这一点，是因为**上游的"怪癖"被挡在适配器层**，没有一条泄漏进领域模型。

### 1.4 一条贯穿全局的原则：绝不信任上游字段名

pusht 的两个 channel 上游叫 `motor_0` / `motor_1`，而它们**不是电机**——是任务空间的
像素坐标 xy。语义由我们自己的 `config/embodiments.yaml` 断言，在适配器层完成绑定。
一个错误的 channel 映射是这个项目里最阴险的 bug：它不会报错，只会安静地毒化下游，
所以适配器用**针对固定 fixture 的特征化测试 + golden file** 锁死，而不是靠严格 TDD。

---

## 2. 统一 schema 的设计与取舍

### 2.1 核心主张

> **统一结构，绝不统一数值。**

把四个源的 action 压成一个定长向量是被**明确拒绝**的做法。理由不是麻烦，是它会销毁
这个项目唯一值得做的东西：

- 像素（pusht）和弧度（ALOHA）不能同列；
- **增量**位姿（UR5）和**绝对**位姿（ALOHA）不能同列——把它们当成一个空间，
  下游做积分就会得到一个物理上无意义的量；
- 逐帧连续量（A/B/C）和 **episode 级符号标签**（EPIC 的 `(verb, noun)`）根本不是
  同一个数学对象。

所以统一的是**结构**：每个 episode 都有 `SignalSpec` / `Channel` / `Capabilities` /
`Provenance` / `EpisodeBoundary` 和一张 `frames.parquet`；不统一的是**数值和单位**，
它们连同各自的语义一起被原样保留。

### 2.2 语义挂在 channel 上，不挂在 spec 上

这是被源 C 单独逼出来的结论。`unit` / `space` / `is_delta` / `frame` / `origin` /
`is_physical` **全部是 per-channel 的**：

UR5 的一个 action 向量里同时有

- 3 维末端位置**增量**（m），
- 3 维姿态**增量**（rad），
- 1 维夹爪**绝对**指令，
- 1 维 `terminate_episode` **控制标志位**（根本不是物理量）。

任何"这个 spec 是 delta 的"或"这个 spec 单位是 m"的说法在这里都是错的。`is_physical`
的存在就是为了让下游能把那个标志位从"8 维动作"里摘出去，而不是把一个布尔量当成
第 8 个自由度去做归一化。

### 2.3 无损 / 有损 / 可丢弃的边界

| 必须无损                                                    | 允许有损                       | 允许丢弃                                   |
| ----------------------------------------------------------- | ------------------------------ | ------------------------------------------ |
| action / state 原始数值（单位换算可逆，换算因子记录在案）   | 视频（转码、抽帧、只留元数据） | 上游内部调试字段（如冗余的 `frame_index`） |
| 时间戳、episode 边界、帧序                                  | 图像分辨率                     | 上游内联的 padding / 空 step               |
| 原始任务语言指令                                            | 深度图（本轮不处理，记录存在） | 与轨迹无关的 license/readme 散文（留 URI） |
| **逐帧 reward** 与 `terminated`/`truncated` 的区分          | 帧序号（可由秒 + fps 重算）    | `discount` / `done` 的冗余镜像             |
| 本体/相机拓扑元数据、`terminate_episode` 这类控制标志位     | —                              | —                                          |
| EPIC 的**标注区间秒数**（它的权威时间轴）、`Channel.origin` | —                              | —                                          |

有两条值得单独说，因为它们是在实现过程中**推翻了最初设计**的：

**reward 从"允许有损"改成"必须无损"。** pusht 的 `next.reward` 是 T 形块与目标区域的
多边形重叠率，而数据集里**根本没有 T 形块的位姿**（`observation.state` 只有推杆的 xy）。
也就是说这个值一旦丢弃就永远无法重算。行为克隆确实不需要它，但离线 RL（IQL / CQL /
Decision Transformer）把它当成核心监督信号——**摄取层没有资格替下游做这个决定**。
成本论据也不成立：一帧一个 float，四个源加起来几百 KB。

**`natural_language_embedding` 可以丢，`natural_language_instruction` 不可以。**
前者是后者的 512 维导出物，可由文本重算；后者是原始事实。丢弃动作本身被记录在
`provenance.transforms` 里（`{"op": "drop_channels", …}`），而不是安静地消失。

### 2.4 没被建模的上游数据，按粒度保留而不是丢弃

- **episode 级** → `raw_extra`（JSON）
- **帧级** → `frames.parquet` 里的 `raw.<name>` 列，列表登记在 `raw_frame_columns`

这条区分是被源 C 逼出来的：C 几乎所有未建模字段都是 per-step 的（`discount`、
`is_first/is_last/is_terminal`、`language_instruction`、`language_embedding`）。
把逐帧数据塞进 episode 级的 JSON blob，既不可查询也不可用——"不丢弃我们还不理解的信息"
会恰好在最需要它的地方失去落点。

### 2.5 多时钟：一个 episode 可以有不止一条时间轴

源 D 逼出了 `stream_specs` / `streams`（不变式 17）：**一个信号如果它的时钟不是帧时钟，
它就有自己的 `streams/<id>.parquet`**。

EPIC 的陀螺仪和加速度计是**两条流、两个时钟**（见 [ADR 012](adr/012-epic-imu-is-two-streams.md)）——
在 `P28_101` 上两者最大相差 15 ms。想要一个 6 维 IMU 向量的消费者必须自己决定把哪一条
重采样到另一条上，并为这个决定负责。摄取层不替他决定，因为**任何一个重采样选择都是
不可逆的**。

### 2.6 优雅降级：capability 是 per-episode 的

`Capabilities` 挂在 **episode** 上而不是 source 上，因为源 D 的**同一个源内部**，
两个 episode 的能力就不一样：EPIC 的三个数据层（标注 / EPIC-Fields 相机位姿 / GoPro IMU）
来自三台服务器、三套发布节奏。某一层 404 只降级**那一层**对应的能力位——
**缺少相机位姿不是一次摄取失败，它就是 `has_camera_pose: false`**
（见 [ADR 011](adr/011-epic-layered-availability-and-origins.md)）。

配套的是 `Channel.origin`（信号来源）和一条 QC 严重度下调规则（不变式 13）：
一个由 SfM 估计出来的量，不该和一个直接测量的量适用同一档告警。

### 2.7 三条绝不违反的纪律

1. **绝不零填充。** 缺失就是 `NULL` + `raw_extra`。
2. **绝不猜单位。**
3. **绝不猜角色。**

一个看起来很合理的编造，比一个诚实的空缺有害得多——因为空缺会被下游发现，
编造不会。

### 2.8 为什么要物化 `normalized/`，而不是"存原样、读时转换"

因为**读时转换会把上游格式的知识泄漏到每一个消费者身上**。物化一层之后，
`normalized/` 提供的是一份统一的元数据契约（一种文件布局、channel 级的 spec、
`Capabilities`、`Provenance`）。这一层是**重编码，不是融合**：无损（未建模的都进
`raw_extra`）、可逆（`raw/` 保留）、全程记录。

推论是 `raw/` **权威且不可变**，`normalized/` **派生且可丢弃**。所以 schema 变更是一次
**定向重规范化，永远不是一个 migration 脚本**。

---

## 3. checkpoint 设计：存什么、存哪、如何幂等

### 3.1 存哪：进程之外

状态存在 SQLite（`store/catalog.sqlite`）里，**不在进程内存里**。
这不是实现细节，是从验收场景倒推出来的唯一解：`kill -9` 之后进程的一切都没了，
能被恢复的只有已经落盘的东西。

推论一条：**每个 episode 的每一次阶段推进都是它自己的一个事务**——绝不"跑完了在最后
统一写一次"。一次 run 中途被杀，已经推进过的 episode 必须已经在库里。

### 3.2 存什么：一个阶段状态机

```
DISCOVERED → FETCHED → NORMALIZED → QC_DONE → COMMITTED
                                                  ↘ FAILED
```

`episode_state` 表记录每个 episode 当前的阶段、`attempt` 次数、`last_error`，
以及租约字段 `lease_owner` / `lease_expires_at`。

### 3.3 幂等键

```
(source_id, upstream_id, content_hash)
```

前两项决定**"是哪个 episode"**，第三项决定**"我们手上这份还是不是上游那份"**。

`content_hash` 的定义必须显式给出，而且 **parquet 的文件字节不合格**：压缩器、
row-group 划分、writer 版本都会改变文件字节，逻辑上完全相同的内容会算出不同的哈希。
定义是：按 spec 声明的 channel 顺序，把每列的值转成 float64 小端原始字节拼接，
前置一个按键排序的元数据 JSON（列名、dtype、行数），再对整体做 sha256。
**哈希覆盖的是逻辑内容，不是容器。**

### 3.4 源 C 是唯一没有稳定上游 ID 的源

RLDS 的一个 TFRecord shard 里有**很多** episode，一个 episode 的唯一身份就是它在
shard 里的下标。对 shard 文件做哈希，会让里面每个 episode 拿到同一个哈希；而**上游一旦
重新分片**，所有下标全部平移，每个 `upstream_id` 都失效——第二次运行会把整个旧语料
当成新数据。这直接砸在"第二次运行识别出没有新数据"这条验收标准上，所以它不是细节：

```
upstream_id  = f"{split}/{shard_basename}#{index_in_shard}"
content_hash = sha256(规范化后 episode 的规范字节)     # 不是 shard 文件的哈希
sources 表增加 shard_layout_revision 列                # 重新分片被识别为"陈旧"，不是"新增"
```

代价必须写清楚：C 的 `content_hash` 只能在规范化**之后**才算得出来，所以"靠哈希提前
跳过下载"对 C 不成立——跳过靠 `upstream_id`，`content_hash` 用于事后校验和陈旧检测。
详见 [ADR 009](adr/009-rlds-identity-clock-and-padding.md)。

### 3.5 "陈旧"是一个统一谓词，而且不只看上游

```
stale ⟺ 记录的 (content_hash, schema_version, adapter_version, ruleset_version) ≠ 当前元组
```

"上游改了数据"和"我们改了 schema / 适配器 / 阈值"共用同一套检测和重跑路径。命中之后
定向地、幂等地重跑相应阶段：schema/适配器变更从 normalize 重跑，只改了规则集就只重跑 QC。

**所以 schema 迭代不需要任何一次性 migration 脚本**——它就是又一轮增量摄取。

### 3.6 崩溃安全的三条铁律

1. 所有产物先写 `*.tmp`，`fsync` 文件，`fsync` 目录，再 `os.replace()` 原子改名。
2. **文件先、状态后。** 中间崩溃留下的是"上一个阶段"的状态，重跑会覆盖同一个 tmp 文件——
   幂等是结构性的，不是靠检查得来的。
3. 启动时**无条件**跑一次恢复扫描（`RecoverIncomplete`，**不放在 flag 后面**，因为一个
   "崩溃之后要记得加"的 flag 是不会被记得的）：把没有 `finished_at` 的 `runs` 行收尾为
   `INTERRUPTED`、清理孤儿 `*.tmp`、释放可回收的租约、验证 `NORMALIZED` / `QC_DONE`
   的 parquet 能否打开，打不开就降级回 `FETCHED`。

恢复**刻意不做**的事：它绝不因为"某个阶段当时正在执行"就把状态回滚。由铁律 2，
被记录下来的阶段一定是**最后一个持久完成的**阶段，所以没有什么需要撤销，只有租约要释放。

### 3.7 一次崩溃的代价是多少

**最多是当时正在执行的那一个阶段，且永远不会是 catalog 已记录为完成的阶段。**
八个检查点里有六个代价为零；`fetch.after` 的代价是重做一次 `fetch`，
`normalize.after_write_before_commit` 的代价是重做一次 `normalize`——因为这两个恰好就是
铁律 2 **故意打开**的窗口。重做这一个单位的工作是正确的：另一个选择是去信任一个
从来没有任何事务为它背书的产物。

还有一条：**恢复永远不重新 fetch。** 一个被记录为 `FETCHED` 的 episode，按定义它的原始
字节已经落盘了，所以 `IngestEpisodes` 从 `(ref, staging_dir, revision)` 重建
`RawEpisode` 句柄，而不是再调一次 `fetch`。这也是 `REDO_NORMALIZE` 回退到 `FETCHED`
而不是 `DISCOVERED` 的原因。

### 3.8 租约

一个租约可回收，当且仅当 **TTL 已过**，**或者**它指向的是我们自己的 worker slot。
单个 SQLite catalog 同时只允许一个写者（`BEGIN IMMEDIATE`），`lease_owner` 指的是一个
**槽位**而不是一个进程，所以启动时发现自己槽位上还挂着租约，只可能意味着它的上一个
持有者死了。TTL 是那个**换到 Postgres 之后依然成立**的谓词——到那时每个 worker 有独立
owner id，上面那个假设就不再成立了。详见 [ADR 007](adr/007-m2-resume-leases-and-crash-criteria.md)。

---

## 4. 断点恢复是怎么测的

### 4.1 模拟了什么故障

八个检查点，覆盖每一个阶段边界和**阶段内部**的两个窗口：

```
fetch.before      fetch.after      normalize.before   normalize.after_write_before_commit
qc.before         qc.mid_rule      qc.after_episode_n commit.after_file_before_db
```

其中两个是关键：

- `normalize.after_write_before_commit`——parquet 已经落盘，但 DB 还不知道；
- `commit.after_file_before_db`——同一个窗口在提交阶段的版本。

这正是铁律 2（文件先、状态后）打开的窗口，也是最容易出错的地方，所以它们被单独列成
检查点而不是混在"阶段之间"里。

### 4.2 三套机制，各自证明另外两套证明不了的事

| 机制                                 | 证明什么                      | 证明不了什么           |
| ------------------------------------ | ----------------------------- | ---------------------- |
| `tests/acceptance/test_resume.py`    | **穷尽性**：8 个点全覆盖      | 进程真的被杀掉时会怎样 |
| `tests/acceptance/test_cli_crash.py` | 真实 CLI 子进程 `os._exit(1)` | 外部信号               |
| `scripts/demo_crash_resume.sh`       | **真实性**：外部 `kill -9`    | 覆盖全部检查点         |

`test_resume.py` 抛的是 `Crash(BaseException)` 而不是 `Exception`，**故意的**：
每个 episode 外面包着 `except Exception` 的失败处理，如果崩溃是一个 `Exception`，
它会被安静地转成一行 `FAILED` 记录，测试就测了个寂寞。

`test_cli_crash.py` 用 `FAULT_INJECT=<checkpoint>[:<occurrence>]` 环境变量驱动真实 CLI
子进程走到检查点后 `os._exit(1)`——**没有栈展开、没有 `finally`、没有缓冲区刷新**。
一个抛出的异常永远证明不了这件事。

`demo_crash_resume.sh` 由**外部进程**发 `SIGKILL`：没有 handler 能拦、没有 `finally`
能缓冲、没有缓冲区能幸存。用 `.demo/` 下的一次性 store，摄取**真实**数据源
（这样这一刀才落在真正的工作中间，而不是一个还没来得及被打断就跑完的 fixture 上）。

### 4.3 恢复后的行为是怎么断言的

**"它没崩"不是一个断言。** 每次崩溃-恢复之后，与一次不中断的基线逐项比对：

1. `FakeSource.fetch` / `normalize` 的调用计数——记在**磁盘上的计数文件**里
   （记在内存里的话，崩溃时就一起没了）。恢复之后这些计数**必须不增加**。
2. `episodes` 和 `qc_results` 两张表**逐字段**相同。
3. `normalized/` 下**每个文件的 sha256** 相同。

### 4.4 场景二"没有新数据"是怎么断言的

用强形式，而不是"episode 数没变"：

- `skipped_already_processed == n`，
- 其他每一个计数器都为 0，
- 行数不变，
- **每一行的 `updated_at` 逐字节相同**。

最后一条只有在跳过路径**一个字节都不写**的时候才成立。如果实现里有一句
"顺便更新一下 last_seen"，这条断言立刻会红——这正是它存在的意义。

### 4.5 还测了什么

- **上游新增 1 个 episode**：只有那一个被 fetch 和 normalize。
- **陈旧的两个方向**：改 `ruleset_version` → 只重跑 QC；改 `adapter_version` →
  重新规范化但**不**重新 fetch。

### 4.6 一个不舒服但必须承认的事

崩溃恢复的正确性靠的是**注入**，而注入点是代码里的一行 `fault.checkpoint("qc.mid_rule")`。
也就是说：**没有被埋检查点的地方，我们没有证据。** 这是这套方法的真实边界，
写在这里而不是留给读者去发现。

---

## 5. 训练子集采样策略

```bash
uv run rdp export --budget 20000 --strategy balanced --seed 7 --out exports/subset.jsonl
```

### 5.1 先说一个测量上的教训

**作业给的 50 000 帧预算，在这个语料上根本不起作用。** 合格帧总共 41 418 帧，
所以 `--budget 50000` 时所有东西都装得下，任何策略都产出同一个文件。M6 的全部证据
都是在 **20 000** 帧下取的——在那里 `sequential` 把 100% 的预算全花在
`aloha_bimanual` 上，而 `balanced` 把它铺成 40 / 29 / 23 / 8 的四个 embodiment。

一个不能区分策略优劣的预算，不能用来论证策略。

### 5.2 分层：按 embodiment，不按 source

训练关心的是**本体和动作空间的覆盖度**；source 只是一个存储事实。

本轮 source 和 embodiment 恰好一一对应，所以组内按 source 轮转退化成了恒等操作。
保留它是因为一旦有两个源共享一个本体（比如两个 UR5 数据集），没有它其中一个源就会
吃掉这个本体的全部配额。

### 5.3 组间配额：开方平滑 + 夹逼

$$w_i = \frac{\sqrt{N_i}}{\sum_j \sqrt{N_j}}$$

$N_i$ 是第 $i$ 个 embodiment 组的合格帧数。然后施加下限 5%、上限 40%。

理由：**严格按帧数正比分配**，会让 ALOHA 的 50 Hz 数据淹没 pusht 的 10 Hz
（帧数是 5 倍，但信息量不是 5 倍）；**均匀分配**又浪费了大源的多样性。开方是标准折中
（多语言 NLP 语料采样用的是同一个技术）。实测：ALOHA 的份额从正比的 60.4% →
开方后 43.8% → 夹逼后 40.0%。

两个结构性发现：

1. **下限和上限不能一趟施加完。** 钉死一个组会改变其余组要凑的总和，所以夹逼是
   **迭代的**；而当两个界对所有组都无法同时成立时，两者一起让位给 `1/n`。
2. **欠额组释放出来的余量，重分配时刻意不再受上限约束。** `ur5_single_arm` 只有
   738 合格帧，配额却是 1 606。上限的存在是为了防止一个本体挤掉别人；当没有人再想要
   这份预算时，还去守着它就等于**为了让表格好看而丢弃真实的训练帧**。

详见 [ADR 016](adr/016-balanced-curation-quotas-and-seed.md)。

### 5.4 组内：质量优先，任务间轮转

1. 默认只从 `qc_verdict == PASS` 的 episode 里选（`--include-review` 可以放宽）。
2. 组内先按 QC 质量排序（没有命中 REVIEW 的排在命中过的前面）。
3. 再按 `(source, task)` 轮转，避免一个任务吃掉整组配额。

### 5.5 预算是上界，不是目标——而且不提供截断选项

**只打包完整的 episode；装不下的整个跳过，绝不截短。**

算一下这笔账：不截断的话，缺口一定小于任何一个被留下的 episode；截断的代价是
**制造出上游根本不存在的 episode 边界**——最坏情况是把携带 `success=True` 的最后几帧
切掉，安静地把一次成功的示教变成无标注数据；而且被切掉的恰恰是信息量最高的尾部
（抓取/放置发生的那一刻）。为了省下那点边角，引入一整套假边界的词汇和下游特判，
不是一笔划算的买卖。

如果预算比最短的合格 episode 还小，导出会**直接报错**，而不是降级成截断。

### 5.6 种子是摘要，不是洗牌

`--seed` 的作用是按 `blake2b(seed:episode_uid)` 给每个 `(source, task)` 桶排序，
**而不是**去 seed 一个 RNG。这样结果是 episode 身份的纯函数，不可能依赖迭代顺序。
不给 seed 时按 episode uid 排序，同样可复现。

`exports` 表记录策略、种子、两个过滤条件和每组的配额-实取统计——
**没有这些就无法重放一次导出**。

### 5.7 输出格式

JSONL，每行一个 episode：

```
source_id, embodiment, action_space, action_dim, physical_dim, episode_uid,
frame_start, frame_end, n_frames, fps, capabilities, boundary, task,
frames_path, key_stats(每个物理 channel 的 mean/std/min/max), qc_verdict, qc_rules_hit
```

`frame_start/frame_end` 永远是完整的 `[0, n_frames)`——字段保留是为了让消费者不必
再查一次元数据就能定位帧范围。

---

## 6. 要上生产还需要做什么

现在的东西是：**单机、单进程、顺序执行**的管线，吞吐量本轮不是目标。
下面按"缺什么"排列，每一条都注明它对应的现有接缝。

### 6.1 编排：for 循环 → 持久化执行引擎

首选 **Temporal**。它的核心抽象——durable execution、activity 自动重试、心跳、超时——
和本设计的 `IngestionStage` 状态机是**同构**的。一个 episode 一个 workflow，
`fetch / normalize / qc / commit` 是四个 activity；崩溃恢复从"手写恢复扫描 + 租约"
升级成平台保证。现在的 `lease_owner` / `lease_expires_at` / `attempt` 三个字段，
就是 Temporal worker 心跳和重试策略的手工版本——**迁移是概念替换，不是概念叠加**。

三条纪律：

1. workflow 载荷只带 `EpisodeUid` 和 URI，**帧数据永远不经过编排器**；
2. **catalog 仍然是数据状态的唯一真相**，Temporal 只拥有执行状态——workflow history
   可以被裁剪，`episodes` 表不可以是错的；
3. activity 是 at-least-once 语义，所以幂等性依然靠现有的
   `(source_id, upstream_id, content_hash)` 和原子写，**绝不依赖编排器去重**。

Airflow 不适合 per-episode 粒度（几百万个 DAG run 不是它的设计点）。

**接缝：** application 层的用例函数**原封不动**变成 activity——调度器本来就在
application 层之外。

### 6.2 元数据层：SQLite → 托管 Postgres

瓶颈**不是行数，是写并发**：SQLite 单写者，几百个 worker 推进状态机会全部串行在一把锁上。

`qc_results` 是真正会变大的表（1.7M episode × 10 规则 × 若干次 run ≈ 10⁸ 行），
按 `run_id` 或按月分区，老分区归档成对象存储里的 parquet，报表用 DuckDB/Athena 直查。

**帧级索引不进 Postgres**：`global_frame_id → (shard, row_offset)` 是一个 5 亿行的
静态映射，它属于数据文件旁边的伴生索引，塞进 OLTP 引擎是选错了引擎。

**接缝：** 换 `EpisodeRepository` / `UnitOfWork` 实现；领域层从来不写 SQL。

### 6.3 存储与 IO

见 [§7](#7-架构扩展500-个数据集5-亿帧按帧随机读)。

### 6.4 可观测性：把 run report 长成指标系统，而不是另建一个

单机版里已经有三个可观测性原语，上云不是重建它们，是给它们挂 exporter：

| 单机原语                                | 云上形态                | 接缝                             |
| --------------------------------------- | ----------------------- | -------------------------------- |
| `runs` 表 + `rdp report`（纯 SQL 聚合） | Prometheus + Grafana    | 给 `RunReporter` port 加一个实现 |
| `qc_results`（数值指标，不只是布尔）    | 数据质量看板 + 异常告警 | 报表直查只读副本 / 归档 parquet  |
| 状态机 + `attempt` / `last_error`       | OpenTelemetry trace     | 不需要新埋点                     |

**数据质量指标比系统指标更值钱**：系统指标告诉你管线还活着，质量指标才告诉你它没在
安静地生产垃圾。而且**变点告警优于绝对阈值**——某条规则命中率突然跳变，
通常意味着上游换了 revision 或者适配器长了 bug，而不是数据集体变差了。

这里有一条自我约束：指标的含义必须和 run report **逐字一致**。
**如果看板的数字和 `rdp report` 对不上，那是一次事故。**

### 6.5 人工复核看板

`REVIEW` 这个判定的存在就是为了人。**本轮（4 个源、约 200 个 episode）不做 web UI
是正确的取舍**——REVIEW 队列就是一条 SQL。到 500 个数据集时它会变成每月 10⁴–10⁵ 条的
持续工作流，没有看板就没人去消费它，队列会单调增长。

那时要建的是一个**薄**看板：队列页、episode 详情页、以及**恰好一个写操作**——
人工覆盖，记录 `human_verdict + reason` 和审计轨迹。

关键设计：**人工判定与机器判定并存，不覆盖它。** 覆盖记录是免费的标注数据，
可以拿来回归 QC 阈值（"规则说 REVIEW、人说 PASS"占比高，说明阈值过紧）。
这个反馈回路是 QC 系统能自我改进的前提，也是看板在"消费队列"之外的第二个价值。

**接缝：** 看板只是 `interfaces/` 里的第二个 presenter，调用**同一批**用例。

### 6.6 云上运维

- **一切 IaC**（Terraform/Pulumi），环境可复现可销毁；优先托管服务，只自运维 worker 池。
- **worker 池跑 spot/抢占式实例**：规范化和 QC 是无状态批处理，**被抢占等价于 kill -9**——
  本设计的验收场景（幂等 + 租约 + 断点恢复）就是 spot 实例的日常。单机设计的这份投入
  在云上直接兑现。
- 写路径用对象存储版本的铁律：**先写对象，后提交元数据**。S3 没有 rename，
  原子性靠"同 key 覆盖是原子的 + catalog 里存在即已提交"。`content_hash` 定期和
  S3 ETag / Inventory 对账。
- 发布与迁移：worker 蓝绿/滚动；schema 迁移走 expand–migrate–contract（先加、双写窗口、
  最后收缩）；适配器/规则集升级走统一陈旧谓词做定向重算，**摄取不停**。
- **合规变成机器强制**：500 个数据集时，license 字段（D 的 CC BY-NC）不能再靠文档提醒——
  导出层加一道硬校验，让非商用数据无法混进商用子集。

### 6.7 QC 执行形态

从"逐 episode 串行"升级为"批量向量化 activity + 采样/分层复检"：新源或新发布的规则先
全量跑一遍，稳定后降为采样，命中率异常时再升回全量。**重算 5 亿帧是不现实的**——
统一陈旧谓词（`ruleset_version`）把重算限制在受影响的分片上。

**接缝：** 变的是 application 层的规则执行器，`QCRule` 这些纯函数本身不动。

---

## 7. 架构扩展：500 个数据集、5 亿帧、按帧随机读

### 7.1 先算账，再选技术

| 量           | 本轮（4 个源） | 500 个数据集             | 含义                                             |
| ------------ | -------------- | ------------------------ | ------------------------------------------------ |
| episode      | ~200           | ~170 万（按每个 300 帧） | `episodes` 仍是"小表"，单个 Postgres 够用        |
| 低维信号     | < 1 GB         | ~0.2–0.5 TB parquet      | 对象存储很便宜，难点是**读模式**不是容量         |
| 视频/图像    | 默认不下       | 几十到几百 TB            | 成本大头，需要分层生命周期管理                   |
| `qc_results` | ~2K 行         | ~10⁸ 行                  | 真正的大表：分区 + 归档                          |
| 摄取形态     | 一次性批处理   | 持续流入 + 按需定向重算  | 从"跑一次"变成**长期服务**，可维护性成为一等公民 |

### 7.2 按帧随机读是"格式 × 缓存"的联合问题

"5 亿帧随机访问"打中的是对象存储最弱的地方（每次 GET 毫秒级延迟、按请求计费、
按前缀限速），所以两端都要动。

**格式侧（降低每次读的放大率）**

- 按 `(embodiment, shard)` 重组为定长 chunk：Lance 式列存（原生 `take(row_ids)`）、
  行组严格对齐的 parquet、或 WebDataset tar。全局帧索引带版本号与数据一起发布。
  训练侧读路径是 **mmap + row-group 级随机读**，不是整文件反序列化。
- **先和训练侧对齐真正需要的是什么**：绝大多数训练需要的是**足够的打散**，
  而不是任意寻址。**shard 级 shuffle + shard 内缓冲 shuffle**（WebDataset 模式）
  把随机 IO 变成顺序 IO，吞吐量差一个数量级。基于索引的 `take()` 留给真正需要任意寻址
  的场景（比如重放一次 QC 命中的帧区间）。
- **不要对视频做随机 seek 解码**：预切片或预抽帧成 chunk，关键帧索引预先建好。
  如果训练消费图像，直接物化成 JPEG chunk——用存储换延迟，这是最常见的工程答案。

**基础设施侧（把延迟藏起来）**

- 训练节点本地 NVMe 缓存 + 分布式缓存层（Alluxio / JuiceFS / Mountpoint-S3 /
  FSx for Lustre over S3）；冷数据（`raw/`、被取代的旧 normalized 版本）按生命周期策略
  下沉到低频/归档层。
- **导出的训练子集本身就是热集的定义**：`exports` 表加子集清单可以驱动缓存预热，
  让预取在训练开始前完成——**策展层和 IO 层在这里闭环**。
- 容量按"每 GPU 每秒消费帧数 × 节点数"估；S3 有按前缀的请求限速，所以 shard 命名必须
  打散前缀（云上随机读的日常坑）。

### 7.3 每一条扩展答案都落在一个已经存在的接缝上

| 扩展动作                          | 变什么                                       | 不变什么                    |
| --------------------------------- | -------------------------------------------- | --------------------------- |
| SQLite → Postgres                 | `EpisodeRepository` / `UnitOfWork` 实现      | 领域状态机、事务边界语义    |
| 本地 FS → S3 + Lance/chunk        | `FrameStore` / `BlobStore` 实现              | 列名契约、canonical schema  |
| for 循环 → Temporal               | application 之外的调度外壳                   | 用例编排逻辑、幂等键        |
| 逐 episode QC → 批量向量化 + 采样 | application 层的规则执行器                   | `QCRule` 纯函数本身         |
| run report → Prometheus/Grafana   | 给 `RunReporter` 加一个指标实现              | `IngestionRun` 的统计词汇表 |
| CLI → CLI + web 看板              | `interfaces/` 里加一个 presenter             | 全部用例和领域模型          |
| 单机崩溃恢复 → spot 实例常态化    | **什么都不用变**（同一套机制，不同的触发源） | 幂等 + 租约 + 原子写        |

### 7.4 迁移路径

绞杀者模式，每一步独立上线、独立回滚：

① `FrameStore` 指向对象存储（风险最低，先解决成本）
→ ② 仓储换 Postgres（解决写并发）
→ ③ 用例包成 Temporal activity（解决调度和长期运行）
→ ④ 加看板（解决人工复核吞吐）

**顺序可以互换**，因为这四个接缝互相解耦。

### 7.5 结论

**不变的是：** canonical schema、能力声明、幂等键设计、阶段状态机。

这恰恰是这个设计的价值所在。代码里的"可扩展"不长成一个预先搭好的分布式系统的样子，
它长成的样子是：**每一个以后会变的决定，都被放在一个可以被独立替换的地方。**
规模变化换掉的只是执行引擎和存储介质，领域模型一行都不用改。

---

## 8. 已知局限

这一节是刻意写长的。**没有一条被粉饰过。**

### 8.1 默认不下载视频——以及它的代价

`config/sources.yaml` 里 `with_video: false`。

**代价，具体地说：**

1. **A/B/C 的像素级 QC 全部降级。** 只能校验视频**元数据**（帧数、时长、fps），
   不能校验画面内容。
2. **`VIDEO_FRAME_MISMATCH` 这条规则在整个语料上从未评估过一个 episode。**
   这不是规则写错了，是它的前置能力位 `has_video` 在当前配置下处处为 false。
   我们把这件事**如实记录**（`rdp report` 里它显示为 0 次评估），而不是为了让表格
   好看去伪造一次运行。
3. **源 D 的官方视频是几百 GB**，本地镜像是**重编码过的** 512×288 / 30fps 版本，
   与官方原件（1080p @ 50/59.94fps）不同。所以任何视觉结论都不可外推，
   而且帧序号必须按镜像的 fps 重算。

**为什么接受这个代价：** 本轮的核心问题是 schema 语义和崩溃恢复，不是像素质量；
而视频会把语料从不到 1 GB 推到几百 GB，把迭代周期从分钟推到小时。这是一个**日程取舍，
不是架构限制**——`--with-video` 开关和 `has_video` 能力位都已经在位。

### 8.2 11 条规则里有 3 条从未评估过任何 episode

| 规则                   | 为什么从未触发                  |
| ---------------------- | ------------------------------- |
| `TS_MONOTONIC`         | 没有一个源提供真实时钟          |
| `FPS_DRIFT`            | 同上                            |
| `VIDEO_FRAME_MISMATCH` | 语料跑在 `with_video: false` 下 |

前两条的根因是：**pusht 的时间戳是我们自己合成的**
（[ADR 005](adr/005-pusht-timestamps-are-synthesized.md)），
**RLDS 根本没有时间戳字段**，所以 C 的 `timestamp_source` 是 `synthesized@5Hz`。
推论：**在 C 上算出来的任何速度或加加速度，单位是"每步"而不是"每秒"**，
C 的 QC 阈值也是照这个设的。

这三条留着而不是删掉，是因为它们描述的失效模式是真实的，只是这批数据碰不到。
但**"我们有 11 条规则"这句话，诚实的版本是"其中 8 条见过数据"**。

### 8.3 语料的 FAIL 率是 0%

196 PASS / 6 REVIEW / 0 FAIL。

**为了证明规则有效而人为制造一个 FAIL，是被明确拒绝的做法。** M5 的两个发现更值得看：

1. **一个阈值可以是不可达的。** `ACTION_JERK` 的第一版拿 `max` 和 `p99.9` 比，
   而这个比值在整个语料上从未超过 1.57——一条 5× 的规则**在任何输入上都不可能触发**。
   见 [ADR 014](adr/014-qc-thresholds-from-measured-distributions.md)。
2. **一条规则必须检查 channel 声明的量程是否**符合**它的假设。** `STATIC_EPISODE` 的
   位移检验把夹爪的 `[-1, 1]` 和相机四元数的 `[-1, 1]` 当成了距离，误标了 17 个健康
   episode。

`config/qc.yaml` 里**每一个阈值都注明了它是从哪个实测数字推出来的**，
分布本身在 `reports/qc_stats.md`（`uv run rdp stats` 生成）。

### 8.4 action 空间没有做跨本体统一

这是**故意的**（见 [§2](#2-统一-schema-的设计与取舍)）。下游如果需要单一向量输入，
必须自己加投影层。

### 8.5 单机单进程

吞吐量本轮不是目标。多进程被明确拒绝：它换走的是崩溃恢复的清晰性。

### 8.6 源 C 只取了 OXE 的一个小子集

12 个 episode，不代表 OXE 的全部多样性。原因是 RLDS 把相机帧内联在 TFRecord 里，
而 `raw/` 是逐字保存的：12 个 episode 已经是 ~660 MB，80 个就是 ~4.4 GB。

### 8.7 源 D 只取了三层中的三层，但不是全部图层

VISOR（手-物掩码与接触关系）和 EPIC-SOUNDS（音频事件）本轮**没有取**。
所以 D 的接触/抓取语义还不能和机器人的夹爪 channel 对齐。
这是日程取舍不是架构限制——`layers` 配置就是那个接缝。

### 8.8 源 D 的相机位姿是 SfM 估计，尺度任意

`metric_convertible=false`，只覆盖 671/700 个视频，而且可能有逐帧空洞。
**任何基于它的距离或速度结论都是相对量。**

### 8.9 源 D 的 license 是 CC BY-NC 4.0（非商用）

和 A/B/C 不同。**导出子集只要包含 D，整个子集都受这个约束。**
`sources.license` 字段和导出的每一行都携带这个信息。见 [§6.6](#66-云上运维)：
到规模化时这条必须变成机器强制。

### 8.10 A 和 B 的 `terminated` / `truncated` 区分已经在上游丢失

M0 实测（[ADR 002](adr/002-lerobot-v3-layout-and-lost-termination.md)）：
LeRobot v3.0 的导出只保留了 `next.done`（pusht 另有 `next.success`/`next.reward`，
aloha 除了 `next.done` 什么都没有），所以 `EpisodeBoundary.is_truncated` 对 A/B 都是 `None`。

**做 value bootstrapping 的消费者，在 A/B 的 episode 上不能假设 $V(s_T)=0$。**

旁证是：50 个 aloha episode**全部恰好 500 帧**，这说明终止来自一个固定步数上限,
也就是说它们**大概**都是被截断的。但"大概"被记在 `raw_extra` 里，
**不会被提升成一个字段**。只有源 C 诚实地携带了这个区分。

### 8.11 源 D 的标注帧序号和视频官方 fps 不一致

对 42% 的语料成立（M0 实测，[ADR 004](adr/004-epic-frame-fps-and-imu-units.md)）。
**秒是权威的**；`raw.frame_index` 按**官方** fps 重新推导，这样位姿层才能不重采样直接
join（[ADR 010](adr/010-epic-two-frame-numberings.md)）。

代价：任何针对第三方 EPIC 产物（它们按抽帧 fps 编号）的 join，必须走
`raw_extra.epic.extraction_numbering`，**不能走 `raw.frame_index`**——
两者每 10 秒视频最多漂移一帧。

### 8.12 源 D 的 action 是 episode 级标签，不能直接做行为克隆

所以它被排除在本轮默认的训练子集配额之外。它的价值在于验证三条路径：
**表示层降级、信号来源混合、源内能力不均**。

### 8.13 崩溃恢复的证据边界

见 [§4.6](#46-一个不舒服但必须承认的事)：没有被埋检查点的地方，我们没有证据。
