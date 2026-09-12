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
表示改善）、是否回到阈值内、不可比原因、以及检修后仍超限的时段。

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

## 测试

```bash
python -m pytest tests/ -q
```
