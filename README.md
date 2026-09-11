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

## 测试

```bash
python -m pytest tests/ -q
```
