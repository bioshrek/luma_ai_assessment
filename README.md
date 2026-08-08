# rdp — 机器人示教数据管线

把四个**互相不兼容**的机器人示教数据集，摄取进一套统一 schema、跑质检、导出训练子集。
全流程崩溃可恢复、重跑幂等。

```bash
uv sync
uv run rdp run --source pusht
uv run rdp export --budget 20000 --strategy balanced --seed 7 --out exports/subset.jsonl
uv run rdp report
```

---

## 1. 文档

这份作业的大部分判断写在文档里，所以文档放在最前面。

| 文件                                                       | 语言 | 是什么                                                                      |
| ---------------------------------------------------------- | ---- | --------------------------------------------------------------------------- |
| [docs/design_answers_zh.md](docs/design_answers_zh.md)     | 中文 | **作业要求说明的全部 8 道题，逐条回答**。先看这一份                         |
| [docs/ai/index.md](docs/ai/index.md)                       | 中文 | AI 协作记录：15 场会话的**原始导出**渲染件，主题一列是 prompt 原文          |
| [docs/ai/rejected.md](docs/ai/rejected.md)                 | 中文 | **被否决的 AI 建议**，以及否决的理由                                        |
| [docs/technical_design.md](docs/technical_design.md)       | 英文 | 设计权威。§2 schema、§3 QC、§5 恢复、§6 导出、§10 扩展、附录 A 真实数据形状 |
| [docs/implementation_plan.md](docs/implementation_plan.md) | 英文 | M0–M8 里程碑、验收命令、Definition of Done                                  |
| [docs/adr/](docs/adr/)                                     | 英文 | 19 条架构决策记录（000–018），每一条对应一次真实的意见分歧                  |
| [reports/qc_stats.md](reports/qc_stats.md)                 | 英文 | `config/qc.yaml` 里每个阈值背后的**实测分布**                               |
| [docs/assessment.md](docs/assessment.md)                   | 中文 | 原始作业要求（归档件，未改动）                                              |

文档语言的划分是一条**明确的决定**而不是没翻完：**对外交付文档用中文，工程记录用英文**。
理由、边界、以及为什么不做任何一份平行译本，见
[ADR 018](docs/adr/018-delivery-docs-in-chinese.md)。简短版本：同一个主张有两份拷贝，
就是文档和代码对不上的那个失败模式本身。

---

## 2. 这个项目难在哪

难点不是吞吐量，是**四个数据源对「一个 action 是什么」的定义根本不一致**：

| 源  | 数据集                              | 格式                   | action 语义                                             |
| --- | ----------------------------------- | ---------------------- | ------------------------------------------------------- |
| A   | `lerobot/pusht`                     | Parquet + MP4          | 任务空间绝对 xy，**单位是像素**                         |
| B   | `lerobot/aloha_sim_insertion_human` | Parquet + MP4          | 关节绝对角度 (rad) + 夹爪，14 维双臂                    |
| C   | OXE / RLDS `berkeley_autolab_ur5`   | 嵌套 TFRecord          | 末端**增量**位姿 (m/rad) + 绝对夹爪 + 控制标志位        |
| D   | EPIC-KITCHENS-100                   | CSV + JSON + IMU + MP4 | **episode 级符号标签** `(verb, noun)`，根本没有逐帧动作 |

所以本项目的核心主张是一句话：

> **统一结构，绝不统一数值。**

把四种 action 压成一个定长向量，看起来"干净"，实际上把这个项目唯一有价值的东西——语义——
全部销毁了。像素和弧度不能同列，增量和绝对不能同列，逐帧连续量和 episode 级符号标签
不能同列。详见 [docs/design_answers_zh.md §2](docs/design_answers_zh.md#2-统一-schema-的设计与取舍)。

---

## 3. 评审关注点索引

作业要求说明的每一项，结论在这里，展开在一个链接之外。

| 关注点              | 一句话结论                                                                                                                  | 展开                                                                                                                                      |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| pipeline 架构       | DDD + Clean Architecture 四层，依赖只向内；四个 port 就是全部的扩展面                                                       | [答辩 §1](docs/design_answers_zh.md#1-pipeline-架构与各环节职责) · [design §8](docs/technical_design.md)                                  |
| 统一 schema 取舍    | 统一结构不统一数值；单位/空间/增量与否挂在 **channel** 上而不是 spec 上，源 C 单独就否掉了所有 spec 级捷径                  | [答辩 §2](docs/design_answers_zh.md#2-统一-schema-的设计与取舍) · [design §2](docs/technical_design.md)                                   |
| checkpoint 设计     | 状态存在**进程之外**的 SQLite；每个 episode 的每一次阶段推进是一个独立事务；幂等键 `(source_id, upstream_id, content_hash)` | [答辩 §3](docs/design_answers_zh.md#3-checkpoint-设计存什么存哪如何幂等) · [ADR 007](docs/adr/007-m2-resume-leases-and-crash-criteria.md) |
| 断点恢复怎么测的    | 8 个崩溃点的注入矩阵（进程内）+ 真实 `kill -9`（进程外）；恢复后 DB 与 parquet 必须与不中断的基线逐字节相同                 | [答辩 §4](docs/design_answers_zh.md#4-断点恢复是怎么测的) · [scripts/demo_crash_resume.sh](scripts/demo_crash_resume.sh)                  |
| 采样策略            | `balanced`：按 embodiment 的**开方配额**再夹逼，组内质量优先、任务间轮转；种子是 uid 的带键摘要而非洗牌                     | [答辩 §5](docs/design_answers_zh.md#5-训练子集采样策略) · [ADR 016](docs/adr/016-balanced-curation-quotas-and-seed.md)                    |
| 生产化还差什么      | 现在是单进程顺序管线；差的是编排、对象存储、并发租约与真正的指标出口——每一项都已留好 port                                   | [答辩 §6](docs/design_answers_zh.md#6-要上生产还需要做什么)                                                                               |
| 500 数据集 / 5 亿帧 | 目录布局与 ID 空间现在就是可扩展的；换掉的是三个实现，不是四个接口                                                          | [答辩 §7](docs/design_answers_zh.md#7-架构扩展500-个数据集5-亿帧按帧随机读) · [design §10](docs/technical_design.md)                      |
| 已知局限            | 默认**不下视频**、3 条规则从未被触发过、FAIL 率为 0——都如实记录，没有一条被粉饰                                             | [答辩 §8](docs/design_answers_zh.md#8-已知局限)                                                                                           |
| AI 使用记录         | 15 场会话、50 轮对话、1936 次工具调用的**原始导出**，逐字渲染                                                               | [docs/ai/index.md](docs/ai/index.md)                                                                                                      |

---

## 4. 环境准备

只需要 `uv`，没有 Makefile —— 命令只有一份拼写，不会有第二处慢慢变旧。

```bash
# 1. 安装 uv（macOS / Linux）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 安装依赖。uv 会按 pyproject.toml 自动装好 Python 3.12 并建立 .venv
uv sync

# 3. 确认工具链
uv run rdp --help
uv run rdp sources
```

关于 Python 版本：`requires-python = ">=3.12,<3.13"`，这是**故意钉死的**，不要"升级"。
上界是因为 M0 阶段验证源 C 时需要 TensorFlow/TFDS，而 TF ≥ 2.16 支持 3.12 但不支持 3.13+；
下界是因为代码用了 3.12 的泛型语法。最终实现没有依赖 TensorFlow
（见 [ADR 001](docs/adr/001-rlds-reader-no-tensorflow.md)：TFRecord 用 130 行标准库直接读），
但这个 pin 保留着，因为 spike 还要能跑。

四个源全部从公网直接拉取，**不需要任何凭据**。`config/sources.yaml` 是提交进仓库的，
里面没有绝对路径也没有密钥；任何机器相关的东西放 `config/sources.local.yaml`（已 gitignore）。

---

## 5. 跑起来

```bash
# 摄取。--source 是必填的，没有"跑全部"模式
uv run rdp run --source pusht
uv run rdp run --source aloha_sim_insertion
uv run rdp run --source berkeley_ur5
uv run rdp run --source epic100

# 导出训练子集（balanced 是默认策略）
uv run rdp export --budget 20000 --strategy balanced --seed 7 --out exports/subset.jsonl

# 报告：本次 run + 累计视图
uv run rdp report
uv run rdp report --format md          # 纯 stdout，可重定向
uv run rdp report --cumulative         # 只看 catalog，不看单次 run

# config/qc.yaml 里每个阈值背后的实测分布
uv run rdp stats --out reports/qc_stats.md
```

全量摄取后 `store/` 实测占用约 920 MB：`raw/` 910 MB（逐字保存的原件，其中源 C 的 12 个
episode 就占了 ~660 MB，因为 RLDS 把相机帧内联在 TFRecord 里），`normalized/` 8.2 MB，
`catalog.sqlite` 4.1 MB。只想看流程的话，`uv run rdp run --source pusht` 一条就够，
它是四个源里最小的一个。

`--budget 50000`（作业里给的例子）在这个语料上**不起作用**：合格帧总共 41 418 帧，
预算根本没有约束力，所有策略都会产出同一个文件。要看出采样策略的差别，用 20000 或更小。

### 跑出来的结果

四个源，202 个 episode 全部提交，0 失败：

| 源                    | embodiment       | episodes |     frames |    PASS | REVIEW |  FAIL |
| --------------------- | ---------------- | -------: | ---------: | ------: | -----: | ----: |
| `pusht`               | `pusht_planar`   |       80 |      9 775 |      80 |      0 |     0 |
| `aloha_sim_insertion` | `aloha_bimanual` |       50 |     25 000 |      50 |      0 |     0 |
| `berkeley_ur5`        | `ur5_single_arm` |       12 |      1 140 |       7 |      5 |     0 |
| `epic100`             | `human_ego`      |       60 |      5 980 |      59 |      1 |     0 |
| **合计**              |                  |  **202** | **41 895** | **196** |  **6** | **0** |

导出（`balanced`，预算 20 000 帧，seed 7）选中 128 个 episode、正好 20 000 帧，
按 40% / 29% / 23% / 8% 分布在四个 embodiment 上。作为对照，`sequential` 会把
100% 的预算全花在 `aloha_bimanual` 上。

FAIL 率是 0，这一点没有被"修饰"过。为了证明规则有效而人为制造一个 FAIL，
是被明确拒绝的做法——理由写在 [ADR 014](docs/adr/014-qc-thresholds-from-measured-distributions.md)。

---

## 6. 复现两个验收场景

这两个场景是整个项目的**最高约束**：任何设计只要让它们更难满足，那个设计就是错的，
无论看起来多干净。

### 场景一：跑到一半 `kill -9`，重启后从断点继续

```bash
bash scripts/demo_crash_resume.sh
```

脚本做的事：用 `.demo/` 下的一次性 store 摄取真实数据 → 由**外部进程**发 `SIGKILL`
（不是异常、不是信号处理器能拦住的东西）→ 重启 → 断言恢复后的结果与不中断的基线一致。

自动化版本在测试里，覆盖 8 个崩溃点（`fetch.before/after`、
`normalize.after_write_before_commit`、`qc.mid_rule`、`commit.after_file_before_db` 等）：

```bash
uv run pytest tests/acceptance -q
```

两者都需要：注入矩阵测**穷尽性**，真实 `kill -9` 测**真实性**。

### 场景二：再跑一次，识别出没有新数据，不重复摄取

```bash
uv run rdp run --source pusht     # 第一次
uv run rdp run --source pusht     # 第二次：一个字节都不写
uv run rdp report
```

第二次的报告里 `skipped` 等于 episode 总数，`normalized` 为 0。幂等键是
`(source_id, upstream_id, content_hash)`——前两项决定"是哪个 episode"，
第三项决定"我们手上这份还是不是上游那份"。

---

## 7. 质量与一致性检查

```bash
uv run pytest                                        # 275 个测试：174 unit / 75 integration / 26 acceptance
uv run pytest --cov=src/rdp/domain --cov-fail-under=90
uv run ruff check . && uv run mypy src/rdp && uv run lint-imports
uv run python scripts/check_report_consistency.py    # 报告里每个数字都用另一套 SQL 重新算一遍
uv run python scripts/render_ai_sessions.py --check  # docs/ai/ 是否与原始导出同步
```

`lint-imports` 从第一天就在跑：**没有被强制的分层规则活不过一周**。
`check_report_consistency.py` 的 SQL 是**故意用另一种写法**拼的，然后去 diff 已渲染的
markdown——不是 diff 中间对象，因为读者读的是 markdown。

---

## 8. 仓库结构

```
src/rdp/
  domain/          # 实体、值对象、纯领域服务。零 IO，不 import sqlite3/pyarrow/上层任何东西
  application/     # 用例编排 + port 协议（Protocol）。只依赖 domain
  infrastructure/  # 源适配器、SQLite 仓储、parquet 存储、原子文件写、配置加载
  interfaces/      # typer CLI + presenter

config/            # sources.yaml（源）/ embodiments.yaml（每个本体的 channel 语义）/ qc.yaml（规则集）
store/             # raw/ 是权威且不可变；normalized/ 是派生且可丢弃；catalog.sqlite 是状态
tests/             # unit / integration / acceptance / fakes / fixtures（四个 mini fixture 共 ~505 KB）
scripts/           # demo_crash_resume.sh、check_report_consistency.py、make_fixtures.py、render_ai_sessions.py
docs/              # 见 §1
```

箭头只向内。加一个新数据源 = 一个 `SourcePort` 实现 + 一条配置，`domain/` 和 `application/`
零改动——这不是设想，是 M3 实测过的：加源 B **一行 Python 都没写**，加源 C 只往 `domain/`
加了一行。

---

## 9. AI 协作记录

[docs/ai/index.md](docs/ai/index.md) 是全部 15 场会话的**原始导出**渲染件：
50 轮对话、1936 次工具调用、150 个被改动的文件、7 小时 22 分墙钟时间。

- 会话主题一列是每场对话**第一条 prompt 的首行原文**，未翻译、未改写。
- 渲染脚本 [scripts/render_ai_sessions.py](scripts/render_ai_sessions.py) 只做格式化和路径脱敏，
  不重建任何内容；`--check` 校验渲染件与原始导出同步。
- 哪些 AI 建议被**否决**了、为什么否决，见 [docs/ai/rejected.md](docs/ai/rejected.md)。

值得单独一提的是：几乎每个里程碑的关键修正都来自**真实数据打脸**，而不是来自更周密的思考。
M4 在所有 fixture 门禁全绿之后，第一次跑真实服务器就查出四个缺陷，其中一个已经潜伏两个里程碑、
吃掉了 50 个 aloha episode 里的 35 个。教训写在
[ADR 012](docs/adr/012-epic-imu-is-two-streams.md) / [ADR 013](docs/adr/013-lerobot-global-index-and-qc-history.md)：
**fixture 要做数据「结构」的等比例模型，而不是数据「体积」的缩小版。**
