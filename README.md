# obsdelivery — 观测语义服务与十进制交付库

纯 Python 3.11 + 标准库（`sqlite3`、`fractions.Fraction`）。不依赖宿主浮点：
二进制位型解释、参考核对、系数取整、误差判定全部用精确有理数完成。

```
obsdelivery/
  semantics.py      位型解释 + 参考判定（纯函数，无存储）
  service.py        语义服务：观测接纳、参考版本、报告、规范观测产品
  delivery_algo.py  十进制表示选择（纯函数，无存储）
  contracts.py      独立读者合同校验与身份
  delivery.py       交付任务：产品 × 样品集 × 合同，全有或全无
  faults.py         故障码目录：NaN payload 掩码/值匹配、特异性过滤、原因并集
  stoppages.py      最少停机数求解（精确子集 DP）、分组见证、原因计数上下界
  notify.py         故障目录、缺测通知产品、停机重建的持久化服务
  public.py         公开产品/交付/通知/重建文档 + 独立读回校验
  storage.py        SQLite schema（不可变表 + 显式事务）
  demo.py           跑 materials/739 + J0/J1 + 三个停机对照
tests/              精确断言、失败原子性、规模测试
```

运行：

```
python3 -m obsdelivery.demo          # 人读输出
python3 -m obsdelivery.demo --json   # 结构化输出
python3 -m obsdelivery.demo --db /tmp/obs.sqlite
python3 -m pytest -q                  # 需 pytest；用 venv 安装即可
```

---

## 1. 位型语义

32 位格式：bit31 sign，bits30..23 exponent，bits22..0 fraction。

| 条件 | 精确值 |
|---|---|
| exp=0, frac=0 | 带符号零（`sign` 原样保留），值 0 |
| exp=0, frac>0 | `(-1)^sign · frac · 2^-149` |
| 1..254 | `(-1)^sign · (2^23+frac) · 2^(exp-150)` |
| 255, frac=0 | 带符号无穷 |
| 255, frac>0 | NaN，payload = fraction 原值 |

`interpret(bits)` 返回 `kind`（`FINITE/ZERO/INFINITY/NAN`）、`bits`、`sign`、
NaN `payload`，有限时返回精确 `Fraction` 值。零的符号与原件身份由 `bits`
与 `sign` 共同保留；负零的交付证据里明确注明“整数系数无法携带 −0，
身份由 original_bits/sign 保留”。

参考判定只看**原值**：

* 有限且 `|value − expected| ≤ tolerance` → `PASS`，否则 `OUTSIDE`；
* 非有限 → `NONFINITE`；无观测 → `MISSING`；
* 报告保存 `abs_difference` 精确差，永不使用交付舍入值。

## 2. 观测接纳

* 按 `(sample_id, revision)` 追加，最高 revision 生效；
* 同版同 bits → 重放（不写新状态）；同版异 bits → **整批拒绝**（`BatchRejected`），
  批次与全部成员以 `rejected` 记入审计，不触碰任何观测；
* 低 revision 合法 → 接纳但 `ignored_older`，不覆盖高版；
* 拒绝是按整个请求批次原子的，`s9` 等未被冲突的记录也不会写入。

## 3. 语义产品与报告

`report()` 把当时的参考版本 + 生效观测固化为不可变行（内容摘要自校验）。
`publish_semantic_product()` 生成独立于交付的规范观测产品，可由服务单独查询。
旧报告/旧产品在新观测、新参考出现后仍按原内容读回，摘要一致；同 id 异内容
直接冲突。参考的派生版本记录 `parent_version`，“复制旧完整版本、替换所列项、
保留未列项”可复现。

## 4. 交付：独立读者合同

合同字段：非空 `allowed_scales ⊆ {0..9}`、正 `max_abs_coefficient`、
非负有理 `max_abs_error`。读者值 = `coefficient / 10^scale`。

对每个允许 scale `s`：

```
coef  = nearest_integer(value · 10^s)      # 精确运算
tie   = 恰好 .5 时取偶系数                 # round_half_even
合法  ⇔  |coef| ≤ max_abs_coefficient  且  |coef/10^s − value| ≤ max_abs_error
```

**scale 选择策略**：按合同给定的 `allowed_scales` 顺序（通常升序）逐个精确
尝试，取第一个合法者。合法的较小 scale 给出更短系数；同一值在多个 scale
都合法时，先出现者胜出。这是有意的文档化策略，不宣称最优体积/精度。
关键点是：**每个**允许 scale 都被精确评估过，绝不会因为只试一个 scale 就拒绝。

**误差证据**：每行保存精确 `error = coefficient/10^scale − original`（带符号）、
`|error|`、全部 scale 的尝试记录（系数、误差、两个界是否满足）以及所选用
系数/scale。`public.verify_public_delivery` 无需数据库即可重算系数界、误差界、
reader 值与状态一致性。

**非有限不能伪造**：`NONFINITE`（NaN/±Inf）与 `MISSING` 没有有限十进制表示，
任一被请求记录无合法表示 → 整个任务拒绝，`deliveries`/`delivery_line` 零写入。
失败只在 `delivery_attempts` 留审计行，不占用成功 `delivery_id`；同一 id 在失败后
可用更合适的合同成功提交。

**OUTSIDE 不会被洗白**：交付行的 `delivered_status` 始终等于原语义 `status`，
即使所交付的十进制恰好等于 `expected`（例如 0.1f 交付成 `100/10^3` 仍是
`OUTSIDE`）。读回校验会检查 `status == delivered_status`。

**身份与重放**：成功 `delivery_id` 绑定 `(产品, 样品集合, 合同)`；同请求重放
返回原内容摘要（`created=False`），异请求抛 `DeliveryIdConflict`。交付行记录
`original_bits`、原精确值/状态、`coefficient`、`scale`、精确误差，独立读回时
核实际表示而非重新选择当前观测。新观测/参考只会生成新的语义产品和新的交付，
旧产品与旧交付保持可重开读。

## 5. 材料与 J0/J1 的实测结果

`demo.py` 实际读取 `materials/739/input.json` 并执行全部操作：

* `3dcccccd`：`13421773/134217728`，比 `1/10` 大 `1/671088640`，零容差 `OUTSIDE`；
* `80000000`：负零，`sign=1`，对 `0` 为 `PASS`；
* `7fc00001`：`NAN`，`payload=4194305`；`7f800000`：正无穷；
* `00000001`：`2^-149`，零容差 `OUTSIDE`；
* 更正后 `s1=1/8`（`3e000000`）且 `r2` 参考 `1/8` → `PASS`；旧报告仍保留
  `3dcccccd` 与 `1/671088640`。

追加请求 `{s1,s2}`、`allowed_scales=[2,3]`、`max_abs_coefficient=99`：

* **J0**（`max_abs_error=0`）整体拒绝：scale 2 → 系数 12、误差 `1/200`（超误差界）；
  scale 3 → 系数 125、误差 0（越系数界）。
* **J1**（`max_abs_error=1/200`）成功：`s1` → `coefficient=12, scale=2`
  （读者值 `3/25`，误差 `−1/200`）；`s2` → `coefficient=0, scale=2`，保留
  `80000000` 与 `sign=1`。J0 被拒不影响语义服务继续查询 `s1` 的 `PASS`。

## 6. 故障目录与缺测通知

**故障码目录**（设备型号 + 版本）是一组规则，每条限定 NaN payload 位：

```
(payload & mask) == value    → 解释为一组原因
```

规则校验 `value` 只设置 `mask` 内的位。一个缺测码（NaN payload）的解析是
**按特异性**而非首次命中：

* 收集所有匹配规则；
* 若匹配规则 `E` 的约束位是 `W` 的**真超集**（`mask_E & mask_W == mask_W` 且
  `mask_E != mask_W`）且在 `W` 约束位上取值一致（`value_E & mask_W == value`），
  则宽项 `W` 被 `E` 覆盖；
* 保留所有未被更具体匹配覆盖的匹配项；原因集合为保留项原因的**并集**；
* 两个掩码**不可比**（互不为子集）时两者可同时保留，原因取并集；
* 无任何匹配 → 显式 `UNKNOWN`，不带原因。

`notify.NotificationService.publish_notifications` 把原件位型 + 目录固化成
不可变通知产品。每行携带：固定 `revision`、`channel`、`reading`、原始
`original_bits`、`payload`、保留规则（`applicable_rules`）、原因集合与
`status`。有限值、无穷、无记录（`MISSING`）与不同 payload 的 NaN 通过
`bits`/`payload`/`kind` 保持可区分；非 NaN 位型不是缺测码，直接拒绝。
旧产品在新目录/新观测出现后仍按原内容读回。

## 7. 最少停机数重建

`stoppages.solve`（以及持久化的 `NotificationService.reconstruct`）：

* 每个通道登记所属表头与**整数闭区间**上报延迟 `[lo, hi]`；
* 通知的实际停机时刻窗口 = `[reading − hi, reading − lo]`，同一通道的不同通知
  可各自取不同延迟；
* 一次停机一个时刻、一个原因；同一表头最多为一次停机留一条通知（同表头的两条
  通知不能并入同一停机）；
* 假定每条通知恰由一次停机产生，求能解释**全部**通知的最少停机数。

一个通知组能构成一次停机当且仅当：表头两两不同、时间窗口交集非空（闭区间，
**恰在单个端点相交即可**）、原因集合交集非空。用精确的子集 DP（`2^n`，
`n ≤ 16`）求最小覆盖，**不枚举** `Bell(16)` 个集合划分、也不展开长整数区间。

在所有达到最小数的解释中给出：

* **一份分组见证**：每组的通知、共同原因、可行时刻区间与一个具体时刻；
* **各原因停机次数的上下界**：`max` = 共同原因含该原因的最多组数（全归给它）；
  `min` = 共同原因恰为 `{该原因}` 的组数（其余组可避开）。二者都只在保持最小
  基数的转移上计算。

**缺证处理**：若存在未解释的缺测码（`UNKNOWN`/无原因）或没有表头登记的通道，
不声称最小值：结果为 `INFEASIBLE`，在 `evidence_gaps` 中定位对应通知
（`HAS_UNKNOWN` / `UNREGISTERED`），保留该批未知结论。

`public.public_notifications` / `public_reconstruction` 导出含目录与登记信息的
独立文档；`verify_public_notifications` / `verify_public_reconstruction`
无需数据库即可重新解析每个 payload 并**重新求解**、逐项核对最小值、原因上下界
与见证分组（表头唯一、时间交集非空、原因适用、覆盖完整）。

## 8. 三个对照（demo 实测）

三条通知同一分钟、延迟均为零，原因依次 `{A,B}`、`{B,C}`、`{A,C}`，三个不同
表头：任意一对可合并，但三向交集为空 → **最少 2 次**。

对第三个 payload 新增更具体规则使其仅可能为 `B`（固定这个新产品）：三向交集
含 `B` → **一次原因 B 的停机**即可解释整批。

固定新产品，仅把前两个通道登记在同一表头：前两条不能再并入同一停机 →
**又变为 2 次**。

对照测试还覆盖：不可比掩码同时命中（原因并集）、两条时窗在单个端点相交
（可合并）、迟到更正后旧产品/旧重建仍按原内容读回。原有数值产品与十进制交付
入口（`SemanticsService.report`、`DeliveryService.submit`、J0/J1）保持可用。

## 9. 规模

测试覆盖 5000 观测（1000 样品 × 5 版）、100 个参考版本、单次 1000 样品的
语义产品与交付，以及独立读回校验；本地全部 < 2 秒。停机求解为 16 条通知的
子集 DP，最坏情况亦在秒级。不做概率统计、浮点求和或硬件异常模拟。
