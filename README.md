# slabwater — 分块标高接缝、有开闸上限的蓄水核验与自适应询测

纯合成离散模型，仅用于可复现的规则核验，**不用于现场水利判断**。

实现为 Python 3.11 独立库（仅标准库 + SQLite），分四层：

| 模块 | 职责 |
| --- | --- |
| `slabwater/catalog.py` | 独立、只追加的分块标高目录；批次校验、冻结切面、逐格来源 |
| `slabwater/solver.py` | 水位/蓄水计算；按出口集合搜索可行计划 |
| `slabwater/publisher.py` | 发布、幂等复读、拒绝留证、核计划、重开 |
| `slabwater/experiment.py` | 按试验号封存有限切面、正费用复测点与逐点准确读数 |
| `slabwater/surveyor.py` | 最小最坏费用的自适应询测单、两类不可行诊断、可分叉会话 |

## 目录（Catalog）

固定 `H×W` 格面，每块给出 `tile_id`、`revision`、原点 `(row,col)`、整数矩形与高度。一次批次：

- 可同时给出多块，**按 `tile_id` 替换**；批次未提及的块沿用上一版。
- 批次整体校验，全部通过才追加一个新版本；失败则**不落任何版本**，旧产品不变。
- 校验项：矩形越界、`tile_id` 重复、同一 `(tile_id,revision)` 内容被改写（immutable）、
  切面**未完整覆盖**（`coverage_gap`）、**重合格高度不一致**（`overlap_mismatch`）、
  格高超过上限 `C`（`height_above_ceiling`）。
- 每版冻结 `heights`、`elevation_hash` 与逐格 `sources`（来自哪块、块的修订、行列局部坐标）。

历史版本可随时 `get_slice(version)` 读取，发布端据此独立取标高，互不影响。

## 水位模型（Solver）

四邻移动，整数高度。对已打开的出口集合：

1. 一格到某打开出口的**排水门槛** = 路线经过的所有格高与出口门槛 `sill` 的最大值。
2. 该格水位 = `min(C, 所有打开出口门槛的最小值)`；无任何出口可达时为 `C`。
3. 蓄水 = 水位 − 本格高（逐格一致计算）。

这不是最短路求和，而是**最小化最大瓶颈**（minimax）的 Dijkstra：扩展边时取
`max(当前瓶颈, 邻格高)`。因此**不能直接用最低出口门槛**——路径上的高堤仍会抬升水位
（见测试 `test_lowest_sill_alone_is_not_enough`）。

### 两种搜索策略

- `recompute`：对每个候选出口集合跑一次多源 minimax Dijkstra。直接、易证正确。
- `reuse`：每个出口只算一次逐格瓶颈图，集合内逐格取**最小**组合。候选集合多时更省。
  瓶颈图只对**某一冻结切面**有效。

`BottleneckCache` 以 `elevation_hash` 为键：切面一旦改变（新版本、标高修订），
下次查询立即**清空失效**并计数 `invalidations`。在 12×12、8 出口压力样例中
（最多开 8 个，共 256 个集合）：

| 策略 | Dijkstra 次数 | 耗时 | 结果 |
| --- | --- | --- | --- |
| reuse | 8（每出口一次） | 0.028s | 与 recompute 完全一致 |
| recompute | 256 | 0.119s | — |

选择建议：候选集合少、或只需核一个既定集合 → `recompute` 更直接；候选集合多且同一
切面反复查询 → `reuse`。任何标高修订后不要跨切面复用瓶颈图，交给缓存自动失效。

搜索按“集合大小升序、再按出口 id”确定性枚举，因此同输入必然得到同一产品（无需最小开闸数）。
`stop_on_first=True` 命中即返回；`False` 则遍历全部并在可行集合中取总蓄水最小者，同时保留全部尝试。

## 发布（Publisher）

请求需给候选出口、`opens`（最多开启数，未要求必须开满）、以及 `total_max` 与 `cell_max`
（若干指定格蓄水上限）。消费端只读**已冻结**的 `source_revision` 切面。

- **成功**：写正式 `plans` 行，产品含开启出口、逐格水位/蓄水、总量、可复查的
  `elevation_source`（版本、`elevation_hash`、块清单）与 `artifact_hash`。
- **拒绝**：不写正式计划；保存真实尝试的**穷尽证据**：每个候选集合的开口、总量、指定格蓄水
  与违反的约束（`total` / `cell`），以及 `exhaustive=true`。可直接下载核对。
- **错误**（未知版本、出口越界/不在边界、请求非法）：记错误结果，同样不写正式计划。
- **幂等**：同一申请 `id` + 同一输入 → 原样复读首次结果（成功返回同一 `plan_id` 与工件；
  失败返回同一证据）。同一 `id` 换输入 → `idempotency_conflict`，绝不静默覆盖。
- **版本隔离**：新标高版本不改写旧 `plans` 行；`get_plan`/`verify`/`reopen` 均按产品内
  冻结的版本与哈希核验。

### 读标高 / 核计划 / 发布 / 重开入口

```python
from slabwater import Catalog, Publisher

catalog = Catalog("catalog.db", height=1, width=5, ceiling=6)
publisher = Publisher("plans.db", catalog)

catalog.apply_batch([...tiles...])          # 追加切面版本
publisher.read_elevation(version=1)         # 读标高（含逐格来源）
publisher.publish(request, default_outlets=[...])  # 实际发布
publisher.verify(plan_id)                   # 核计划（重算并重搜索）
publisher.reopen(plan_id)                   # 重开已存工件
```

命令行：

```bash
python3 -m slabwater run materials/772/input.json --db-dir /tmp/run772
python3 -m slabwater verify /tmp/run772 <plan_id>
python3 -m slabwater reopen /tmp/run772 <plan_id>
```

## 询测（Experiment / Surveyor）

交接时只留下几份都通过接缝检查的测绘草案，现场铺的是哪一版要复测后才知道。
询测层在一个**试验号**下封存：有限个候选切面（≤16）、一组带**正整数费用**的复测点
（≤12）、候选出口（≤8），以及每个切面在每个点会给出的**准确读数**。

- 读数是**不开任何出口**时该格的蓄水（全部格面顶到 `ceiling`，读数 = `ceiling − 格高`），
  由 `solver` 引擎在封存时重算；输入声明的读数若与重算不符，整份封存被拒
  （`readings_mismatch`）。只接受合成格面，重复格面按 `duplicate_surface` 拒绝。
- 试验只追加：同一 `experiment_id` 重复封存同内容则原样复读，换内容报
  `experiment_id_conflict`，旧记录不动。
- 一份读数回执 `[(point_id, value), ...]` 可随时查 `possible_slices`：只剔除被读数
  排除的切面，资料不足时保留全部可能；读到任何切面都不会出现的值时返回空集
  （`out_of_draft`），会话据此**停止签发并保留原回执**。

### 询测单（自适应决策树）

`SurveyOrder` 给出沿用出口 `outlet_ids`、最多开几个 `opens`、指定格蓄水上界
`cell_max`（可选总量上界 `total_max`）与预算 `budget`。`plan_survey` 构造一棵决策树：

- 内部节点 = 在某点复测，按读数分叉；叶子 = 对当时**所有剩余切面都可行**的出口集合，
  不要求辨明具体切面。
- 节点费用 = 该点费用 + 各分叉费用的最大值；规划器最小化**最坏分支费用**，同费用按点 id
  确定性破并。可行性用与 `solver` 的 `reuse` 相同的逐出口瓶颈图组合，并在测试中与
  `recompute` 全量搜索逐集合对账。
- 两种失败严格区分：
  - `no_distinguishing_point`：预算无限也无法把候选分成有共同可行出口的组。证据是
    不可区分对（每个可用点上两版读数逐项相等、各自可行出口交集为空）或停滞候选集在
    每个点上的划分。
  - `insufficient_budget`：自适应方案存在，但最小最坏费用超过预算；返回
    `achievable_cost` 与 `shortfall` 作为可核查理由。
- 工单可用 `allowed_points` 限制只准测某些点（如“只会测中格”），不改变封存试验。

### 可逐步执行、可重开的会话

`SessionStore` 把每次询测持久化到 SQLite：`open` 后按 `next_step` 逐点 `record`；
可在另一进程 `resume`，也可 `fork` 出空回执的独立会话走**另一条读数分支**
（同一试验材料）。读到草案之外的值立即 `aborted`，不签任何出口，原回执逐条保留。

```python
from slabwater import ExperimentStore, SessionStore, SurveyOrder, plan_survey

experiments = ExperimentStore("experiments.db")
exp = experiments.seal(spec)                       # 封存（幂等复读）
exp.possible_slices([("P1", 3)])                  # 回执查询：保留全部仍可能的切面

order = SurveyOrder.from_dict(order_raw)
result = plan_survey(exp, order)                  # 最小最坏费用询测单
if not result.ok:
    result.reason                                 # no_distinguishing_point / insufficient_budget
    result.witnesses, result.diagnostic           # 可核查理由

sessions = SessionStore("sessions.db", experiments)
session = sessions.open_session(exp.experiment_id, order, result)
step = session.next_step(exp)                     # {'action': 'measure', 'point_id': ...}
session.record(exp, step["point_id"], value)      # 越界值 -> stopped，原回执保留
```

命令行：

```bash
python3 -m slabwater seal /tmp/survey materials/survey/T001_experiment.json
python3 -m slabwater receipt /tmp/survey T001 --read P1=3
python3 -m slabwater plan-order /tmp/survey T001 order.json
python3 -m slabwater session /tmp/survey T002 order.json --action open
python3 -m slabwater session /tmp/survey T002 order.json --action resume --session-id S-xxxx
python3 -m slabwater session /tmp/survey T002 order.json --action record --session-id S-xxxx --point Q00 --value 2
python3 -m slabwater survey-demo /tmp/survey materials/survey/T002_experiment.json materials/survey/T002_orders.json
```

## 样例核对（materials/772）

- 初版 `[0,4,1,4,0]`，双口全开：水位 `[1,4,4,4,3]`，蓄水 `[1,0,3,0,3]`，总 7，中格 3。
  A1（总量≤7、中格≤2）必拒：没有任何集合同时满足，证据穷尽列出（含双开的 `cell` 违例）。
- 降右侧堤后 `[0,4,1,2,0]`，双口全开：水位 `[1,4,3,3,3]`，蓄水 `[1,0,2,1,3]`，总 7、中格 2。
  A2（≤7、中格≤2）成功，开启 `L,R`。
- A3 只允许开 1 口：`L` 总 10、`R` 总 10，均超总量 7（`R` 虽满足中格≤2），故拒绝。
- `invalid_correction` 使重合格 `(0,2)` 出现 1 与 2，冲突 → 整版拒绝，版本仍为 `[1,2]`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：切面/接缝/覆盖/越界/不可变修订/原子批次/重开；水位规则与“最低门槛不充分”；
reuse 与 recompute 一致；修订使缓存失效；A1/A2/A3；幂等复读与冲突；版本隔离；
核计划能发现篡改；12×12、8 出口压力与穷尽拒绝证据。询测层（`test_survey.py`，材料在
`materials/survey/`）另覆盖：封存读数与回执查询、1×5 题述三场景、2×3 两层且第二测点
随首答变化、两类不可行诊断、minimax 费用与确定性破并、会话持久化/分叉/越界中止、
16/12/8 硬上限，以及询测可行性表与发布求解器的逐集合对账。
