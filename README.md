# 调节阀全行程诊断服务

对调节阀检修前后的全行程测试数据进行自动诊断：统一单位、对齐不同采样频率的时间轴、
切分开阀/停留/关阀区段，计算行程时间、死区、回差、过冲、稳态偏差，并定位
卡跳、反向迟滞、供压不足、信号断档与未完成行程。支持技术人员调整分段边界、
剔除无效点（每次修改生成新版本并保留原始点引用与理由），以及在条件兼容时
配对检修前后测试、量化指标改善。

## 运行

```bash
pip install -r requirements.txt
python scripts/generate_samples.py        # 生成 samples/ 下四组请求样例
uvicorn app.main:app --port 8000
```

数据库路径用环境变量 `VALVE_DB_PATH` 指定（默认 `./valve_diag.db`），SQLite 自动建表。

## 处理流程

1. **单位统一**：指令/阀位支持 `%`、`mA`(4–20)、物理行程（`mm/cm/in`，按量程归一）；
   压力支持 `kPa/MPa/bar/psi/kgf/cm2` 统一为 kPa。单位与量程量纲不符、数值越界
   属于**单位冲突**，直接阻断结论。
2. **时间对齐**：各通道采样频率可不同，取重叠区间，按最细通道中位间隔
   （限制在 0.02–1.0s）线性插值到公共网格。
3. **自动分段**：按指令窗口斜率（阈值 0.5%/s）分类升/降/稳，短于 1s 的稳定段
   并入运动段。每个边界都返回依据（观测斜率、稳定带、方向反转）。
4. **指标**：行程时间（指令到位后阀位进入稳定带的附加时间）、死区（反转点阀位
   未响应期间指令最大变化）、回差（准静态点开/关同指令阀位差最大值）、
   过冲、稳态偏差（停留段尾部）。
5. **异常定位**：卡跳（指令变化而阀位停滞后突跳）、反向迟滞（反转死区超限）、
   供压不足（运动段压力低于下限的时段）、断档（采样缺口/冻结，冻结只针对
   反馈信号）、未完成行程。
6. **判定**：存在阻断问题 → `no_conclusion`；有超限/异常 → `exceedances`；
   否则 `ok`。
7. **测量不确定度评估（蒙特卡洛）**：测试提交或 `/analyze` 请求可携带
   `uncertainty` 输入，声明各通道分辨率（均匀量化）、准确度、零点漂移、
   采样时间抖动（通道时钟偏差）与校准标准不确定度（正态，含覆盖范围）。
   按固定种子对单位统一后的原始点做误差扰动并重新对齐/重采样，复算
   行程时间、死区、回差、过冲、稳态偏差的经验区间（默认 95%，分位数法），
   结果记录样本数、种子、输入分量与同一组复算参数。**分辨率声明值为量化步进，
   均匀扰动半宽取步进的一半（resolution=0.2% → ±0.1%）**。指标 95% 区间**跨越
   判定阈值（含端点贴限）时符合性为 `indeterminate`，不得只按中心值通过**；
   此时顶层 `verdict` 无论原为 ok 还是 exceedances 都同步为 `indeterminate`
   （打印页顶部结论显示“符合性不确定”，原有超限/异常事件仍保留在清单中），
   区间整体越过限值时 `verdict` 为 `exceedances`，阻断（no_conclusion）优先级最高。
   未提供分量时沿用原中心值结果，不确定度明确标为 `not_evaluated`；
   分量单位冲突、校准范围不覆盖观测值时评估 `invalid` 并逐条列出原因；
   有效重采样不足（<30 次或 <80%）时总体判 `indeterminate` 并说明。
   人工移动边界/剔除点后另存版本，不确定度区间与复算参数随该版本独立保存，
   缺省沿用上一版本输入。配对比较给出指标**差值区间**（检修前−检修后），
   只有区间整体越过零点才标记 `improved`/`degraded`，跨零为 `indeterminate`。
   JSON 导出与打印页共用同一组区间和复算参数，报告不另行重算。

**阻断条件**（不得用于维修结论）：校准失效或未提供、单位冲突、关键区段不完整
（缺开/关/停留段，或运动段数据断档超过 20%）。

## 主要端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/valves` | 建立阀门档案 |
| POST | `/tests` | 提交测试（量程、三路带时标序列、工况、阈值、校准有效期） |
| POST | `/tests/{id}/analyze` | 执行分析，生成版本 |
| GET | `/tests/{id}/analyses` | 某测试的全部分析版本 |
| POST | `/analyses/{id}/adjust` | 移动边界 / 剔除无效点 → 新版本（保留原始点引用与理由） |
| POST | `/pairings` | 检修前后配对（方向/行程范围/负载兼容才比较） |
| GET | `/analyses/{id}/export` | 导出 JSON（含曲线、指标、版本） |
| GET | `/analyses/{id}/report` | 打印报告（HTML+SVG 曲线、阈值、版本） |
| GET | `/samples/{name}` | 请求样例（可直接 POST 到 `/tests`） |

配对兼容性：两侧都含开/关行程、指令行程跨度相差 ≤5%、负载条件（load/medium）
一致、同一台阀门，且两侧分析均无阻断问题。响应包含各指标改善量（delta 为正值
表示改善）、是否回到阈值内、不可比原因、检修后仍超限的时段，以及
`uncertainty_comparison`：每个指标的检修前/后区间、差值区间与
`improved/degraded/indeterminate/not_evaluated` 判定（仅差值区间整体越零
才标记明确改善或退化）。

`uncertainty` 输入示例（提交测试或 `/analyze` 请求体均可；缺省 `n_samples=120`、
固定 `seed=20260912`）：

```json
"uncertainty": {
  "channels": {
    "command":  {"resolution": {"kind":"resolution","value":0.1,"unit":"%"},
                 "accuracy":   {"kind":"accuracy","value":0.2,"unit":"%"},
                 "time_jitter_s": 0.01},
    "position": {"resolution": {"kind":"resolution","value":0.15,"unit":"%"},
                 "zero_drift": {"kind":"zero_drift","value":0.1,"unit":"%"},
                 "time_jitter_s": 0.02},
    "pressure": {"resolution": {"kind":"resolution","value":1.0,"unit":"kPa"}}
  },
  "calibration": {"value":0.2,"unit":"%","applies_to":["command","position"],
                  "range_min":0,"range_max":100,"range_unit":"%"},
  "n_samples": 120, "seed": 20260912, "interval_prob": 0.95
}
```

分量单位须与通道量纲一致（指令/阀位：`%`、`mA`、`mm/cm/in`；压力：
`kPa/MPa/bar/psi/kgf/cm2`；时钟抖动：`s`），冲突即评估无效并列原因。

## 样例

`samples/` 下四组样例（指令 20Hz / 阀位 10Hz / 压力 2Hz，各不相同）：

- `sample_stiction.json` — 卡涩（检修前）：粘滑、死区 ≈3.4%、回差 ≈4.4%
- `sample_stiction_post.json` — 检修后对照：可与前者配对，量化改善
- `sample_dropout.json` — 信号断档：阀位反馈有缺口与冻结
- `sample_normal.json` — 正常行程

快速体验：

```bash
curl -X POST localhost:8000/tests -H 'Content-Type: application/json' -d @samples/sample_stiction.json
curl -X POST localhost:8000/tests/1/analyze -H 'Content-Type: application/json' -d '{"author":"tech-01"}'
curl -X POST localhost:8000/tests -H 'Content-Type: application/json' -d @samples/sample_stiction_post.json
curl -X POST localhost:8000/tests/2/analyze -H 'Content-Type: application/json' -d '{}'
curl -X POST localhost:8000/pairings -H 'Content-Type: application/json' \
     -d '{"pre_analysis_id":1,"post_analysis_id":2}'
```

样例快速体验见各模块 `scripts/generate_*_samples.py` 与 `/samples/{name}` 端点。

## 阀杆推力签名（/stemthrust）

阀门走完全行程后，单路执行器压力无法区分卡涩来自执行机构还是阀体。本模块在
异频序列（指令、阀位、供气压力、一个或两个工作腔压力）上按阀位运动切分开/关
行程，再细分**启程、匀速、换向、离座、落座**五类区段，按
`净推力 = 压差×有效面积 ± 弹簧力`（开阀方向为正）计算推力签名，输出开/关
启动力、运行摩擦（匀速段中位力绝对值）、摩擦带（段内峰峰 + 开/关中位力之差）、
开阀离座力、落座力与落座裕量，并对每个异常区间标注嫌疑来源
（执行机构 / 阀体填料 / 阀座负载）。

- 单作用执行器需 A 腔压力 + 弹簧曲线（版本化，须覆盖试验行程，含覆盖裕度）；
  双作用执行器需 A、B 两腔压力。必需压力通道缺失、通道时间无重叠、参数量纲
  不符、校准缺失/失效、弹簧曲线不覆盖行程或关键相位无法圈定时，只给
  `no_conclusion` 与证据缺口。
- 复核人移动相位边界或剔除坏点必须填写理由，结果另存为不可覆盖的新版本。
- 检修前后比较（`POST /stemthrust/comparisons`）仅在执行器结构、阀杆方向、
  有效面积（±2%）、弹簧版本与负载条件（load/medium）兼容时定量比较；
  不兼容逐条给出不可比原因，含证据缺口的版本不参与数值比较。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/stemthrust/tests` | 提交推力签名测试（执行器类型/面积/弹簧曲线/异频序列） |
| POST | `/stemthrust/tests/{id}/analyze` | 执行分析，生成版本 |
| POST | `/stemthrust/analyses/{id}/adjust` | 移动相位边界 / 剔除坏点 → 新版本（须带理由） |
| POST | `/stemthrust/comparisons` | 检修前后比较（可按 analysis_ids 或 valve_tag） |
| GET | `/stemthrust/analyses/{id}/export` | 导出 JSON（区段、指标、采用区间、不可比原因） |
| GET | `/stemthrust/analyses/{id}/report` | 打印报告（阀位/压力/净推力 SVG、相位与来源嫌疑） |

## 单相液体流量曲线校核（/flowcurve）

定位器显示阀位已到，但阀芯冲蚀、结垢或装反会使实际流量偏离铭牌特性曲线。
本模块上传阀位、流量、阀前/阀后压力与温度的异频时序，以及介质密度、饱和蒸气压、
流量计量程、额定 Cv、口径、流向与设计特性（线性 / 等百分比 / 快开），逐稳态
平台校核实测 Cv-阀位曲线。

- 异频对齐后按窗口阀位速度把连续数据归为**有序稳态平台**（每平台 = 曲线上一个
  测点，窗口按采样步长扩展，1 Hz 全程斜坡不会被误判为平台）；平台中位值按
  IEC 60534-2-1（非阻塞液体）逐点换算
  `Cv = Q / (N1·√(ΔP/(ρ/ρ0)))`，N1=0.865（Q:m³/h、ΔP:bar，
  1 US gpm 水 / 1 psi → Cv≈1），支持 gpm、L/min 等流量与各压力单位换算。
- 按指定基准过原点最小二乘拟合容量系数 k，返回实测曲线、百分比残差带、
  单调性（Spearman 秩相关 + 逆序）、有效调节比（实测 Cv 最大/最小），并诊断
  **疑似堵塞（k 偏小）、冲蚀（k 偏大）、反装行程（镜像行程残差显著更小/
  Cv 随阀位下降）**。
- 逐点排除、单独解释且不进入拟合：压差不足、平台漂移（阀位峰峰或流量变异超限）、
  流量计超量程（读数顶到满量程轨）、气蚀/闪蒸（ΔP > FL²·(P1−Ff·Pv)）、
  物性缺项/超出参考温度适用窗、平台过短、人工停用。单位冲突、校准缺失/失效、
  通道无重叠、无平台或有效点不足时只给 `no_conclusion`。
- 复核人可移动平台边界或停用测点（必须填写理由），结果另存不可覆盖新版本，
  平台边界与测点均保留原始采样引用。
- 叠加比较（`POST /flowcurve/comparisons`）仅在**阀内件 trim、介质（名称/密度/
  蒸气压）、流向、流量计量程与单位一致**时定量叠加 Cv-阀位采用点并比较容量系数、
  残差与调节比趋势；不一致逐条给出不可比原因。
- 详情接口与打印页展示采用点（绿点）、排除点（红叉，仅展示诊断 Cv）、逐点
  换算参数（Q、ΔP、绝压、Ff、ΔPchoked）与排除缘由。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/flowcurve/tests` | 提交校核测次（五路异频序列 + 介质/阀门/流量计参数） |
| POST | `/flowcurve/tests/{id}/analyze` | 划分平台、逐点换算 Cv 并拟合，生成版本 |
| POST | `/flowcurve/analyses/{id}/adjust` | 移动平台边界 / 停用测点 → 新版本（须带理由） |
| POST | `/flowcurve/comparisons` | 多测次叠加比较（可按 analysis_ids 或 valve_tag） |
| GET | `/flowcurve/analyses/{id}/export` | 导出 JSON（测点、换算参数、曲线、排除缘由） |
| GET | `/flowcurve/analyses/{id}/report` | 打印报告（时序/平台、Cv-阀位曲线、采用点与排除缘由） |

样例见 `scripts/generate_flowcurve_samples.py`（铭牌一致、堵塞、冲蚀、反装、
低压差、超量程、气蚀、平台漂移、物性缺项、1 Hz 斜坡无平台）。

## 测试

```bash
python -m pytest tests/ -q
```
