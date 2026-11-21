# 询测材料（合成格面）

两个封存试验，均为纯合成离散格面。读数统一取**不开任何出口**时该格的蓄水
（水位被顶到 `ceiling`，故读数 = `ceiling − 格高`），由 `slabwater.solver` 的
minimax 引擎在封存时重算，输入里谎报读数会被 `readings_mismatch` 拒绝。

## T001 — 一行五格的小展台（题述示例）

- 格面 `1×5`，上限 `ceiling=3`；左右两端各一个门槛 0 的出口 `L=(0,0)`、`R=(0,4)`，最多开 1 个。
- 两份切面：`A=[0,0,0,2,0]`、`B=[0,2,0,0,0]`。
- 复测点：第二格 `P1=(0,1)`、第四格 `P2=(0,3)`、中格 `PM=(0,2)`，费用均为 1。

封存读数（蓄水）：

| 切面 | P1 | P2 | PM |
| --- | --- | --- | --- |
| A | 3 | 1 | 3 |
| B | 1 | 3 | 3 |

工单见 `T001_orders.json`：

1. **T001-O1**（中格蓄水 ≤ 0，预算 1）：根节点测 `P1`，读 1→只剩 B→开 `R`；
   读 3→只剩 A→开 `L`。最小最坏费用 1。
2. **T001-O1MID**（同样约束，`allowed_points=["PM"]`，预算 100）：两版在中格都读 3，
   任何测点序列都分不开，而 A 只能开 `L`、B 只能开 `R`，无共同可行出口 →
   `no_distinguishing_point`，附不可区分对证据；**花多少都不能保证**。
3. **T001-O3**（放宽为中格 ≤ 2，预算 0）：两版都可直接开 `L`（中格蓄水 1 ≤ 2），
   叶子在根上，费用 0，**无需测量**。

## T002 — 至少两层、第二测点随首答变化（2×3）

- 格面 `2×3`，上限 `ceiling=6`，六个候选切面 `S0..S5`；四角各一个门槛 0 出口
  `O0..O3`，最多开 1 个；约束为底排两角 `(1,0)`、`(1,2)` 蓄水都为 0。
- 复测点 `Q00=(0,0)`、`Q01=(0,1)`、`Q02=(0,2)`，费用均为 1。

封存读数（蓄水）：

| 切面 | Q00 | Q01 | Q02 | 单独可行的出口 |
| --- | --- | --- | --- | --- |
| S0 | 6 | 2 | 4 | O0, O2 |
| S1 | 2 | 2 | 5 | O2 |
| S2 | 2 | 4 | 2 | O3 |
| S3 | 5 | 2 | 2 | O2 |
| S4 | 6 | 2 | 5 | O1, O3 |
| S5 | 5 | 2 | 5 | O1, O3 |

工单 `T002-D2`（预算 2）的最优询测单：

```
测 Q00
├─ 读 2 → 剩 {S1,S2}：测 Q01
│        ├─ 读 2 → S1 → 开 O2
│        └─ 读 4 → S2 → 开 O3
├─ 读 5 → 剩 {S3,S5}：测 Q02
│        ├─ 读 2 → S3 → 开 O2
│        └─ 读 5 → S5 → 开 O1
└─ 读 6 → 剩 {S0,S4}：测 Q02
         ├─ 读 4 → S0 → 开 O0
         └─ 读 5 → S4 → 开 O1
```

- 深度恰为 2：没有任何根测点能一次分成“有共同可行出口”的组（测试
  `test_no_single_measurement_suffices` 穷举断言）。
- **第二测点随首答变化**：答 2 走 `Q01`，答 5/6 走 `Q02`。
- 同一工单预算降到 1（`T002-D2B1`）：方案客观存在但最小最坏费用 2 > 1 →
  `insufficient_budget`，并给出 `achievable_cost=2`、`shortfall=1`，与
  “缺少能区分的点”明确区分。

## 命令行复跑

```bash
python3 -m slabwater survey-demo /tmp/survey materials/survey/T001_experiment.json materials/survey/T001_orders.json
python3 -m slabwater survey-demo /tmp/survey materials/survey/T002_experiment.json materials/survey/T002_orders.json

# 单份读数回执查询（资料不足时保留全部可能；越界读数 out_of_draft=true，退出码 2）
python3 -m slabwater receipt /tmp/survey T001 --read P1=3

# 逐步会话：open / resume / record / fork
python3 -m slabwater session /tmp/survey T002 /tmp/order.json --action open
```
