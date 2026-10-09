# GOSIM 智能体巡天黑客松 · 参赛项目（survey26）

GOSIM "智能体巡天" 黑客松（survey26）：提交一个自主决策的智能体，模拟操作光谱巡天望远镜完成一整季巡天——决定每次曝光的时刻、指向（alt/az）、时长、观测程序（DARK/BRIGHT/BACKUP）与光纤↔目标指派，与真实天气、隐藏事件、900 秒墙钟时限对抗。

- 比赛平台：<https://create.gosim.org/survey26/platform/>（报名 / 提交 / 榜单）
- 参赛文档（完整版）：`kit/docs/participant-guide.zh.md`
- 官方示例源码仓库：<https://github.com/gosimfoundation/hackathon-survey26>
- 平台最后核查的正式赛提交窗口（UTC+8）：2026 年 10 月 5 日 00:00 – 10 月 8 日 09:00；隐藏卡 E–H 由主办方评测。

## 参赛版本与后续迭代

本仓库公开参赛实现、实验工具与开发记录。基于官方 Rust pro 示例持续改进，核心代码位于 [`kit/rust-pro/`](kit/rust-pro/)。

- **正式赛最终提交**：`rustpro-rr6`，平台版本 `48c46ca4-661f-4229-b3fb-7ce966197848`。2026 年 10 月 8 日核查时已锁定。
- **当前 main 分支**：包含后续 robust1 / robust2 改进，不等同于正式赛最终提交。包括异步情报可靠重试、光纤与曝光约束适配，以及模型鉴权失败或余额不足后的停用保护。
- **实验性故障检测**：`PRO_SCALE_INDEPENDENT=1` 开启，默认关闭。尝试在缺少健康基线时利用独立观测证据识别故障。

本地验证：12 项 Rust 测试通过；原始 L1–L4 回归分数不变；两套受控场景中故障场景得分提升，误报数量未增加。这些结果不代表隐藏卡 E–H 收益，真实模型完整复测尚未完成。

验证数据见 [`docs/validation/robust2-local.json`](docs/validation/robust2-local.json)，完整实验记录见 [`docs/05-开发计划.md`](docs/05-开发计划.md)。

## 策略架构与 LLM 环节

基座是官方 pro 算法的 Rust 实现（`examples/rust-pro`，与 `python-pro` 同算法同常量）。
确定性规划器做出每一次观测决策：

1. 候选目标按增量收益排序：`gain = weight × (reach(T)×程序倍率 − 已有最好因子)`，
   必观测目标与限时请求加奖励项，剩余夜数少的目标加 urgency 因子；
2. 候选视场 = 最佳锚点目标 × 每根光纤 + 剩余科学价值最密集的天区补丁；
3. 每次决策在"指向 + 光纤指派 + 时长 + 程序"四元空间搜索，
   胜者最大化 `总增益 − λ×T`（λ = 时间价格，按本卡时间稀缺度自适应，
   **我们修正为按真实光纤填充率标定**——指向次数而非光纤秒才是稀缺资源）；
4. 程序声明由饱和命中拟合出的天光带水平驱动；
5. 从自己的曝光结果在线学习：天空质量尺度、天光带、指向偏移（Hard 卡）、仪器故障信号。

### LLM 驱动的环节（评奖判定参考）

模型（平台代理注入，OpenAI 兼容接口）在每夜开始时后台并行调用两次、付费故障举报前调用一次，
另有情报解码调用按需触发，**建议经规则验证后真实参与决策**（非摆设）：

| 调用 | 环节归属 | 输入 → 输出 |
|---|---|---|
| `night_plan` | 自然语言理解 + 任务规划 | 今晚预报/简报的自然语言文本 → 坏夜判断、需回避的天区扇区（规划器真实回避） |
| `fault_review` | 数据解析 + 行动决策 | 自身逐小时观测质量表 → 仪器故障概率估计（驱动当晚举报倾向） |
| `confirm_report` | 行动决策 | 举报证据链 → 付费举报的最终确认/否决（报对 +100 / 误报 −150 的守门员） |
| `intel` | 自然语言理解 + 数据解析 | 请求 reason 里的交接班日志（凯撒密码/摩斯码/唱名编码）→ 真实山脊线高度、停机维护窗口（规划器据此修正地形遮挡模型、跳过停机时段） |

六环节（自然语言理解 / 数据解析 / 任务规划 / 行动决策 / 工具调用 / 自适应）中
**四个由 LLM 驱动**，满足"至少两个"的评奖门槛。模型调用全部在后台线程，
夜初等待有 `model_wait_budget` 上限；模型不可用时规则兜底、行为保持确定。

## 目录结构

```
docs/        需求文档与情报（01 协议需求 / 02 计分 / 03 任务卡与赛程 / 04 讲座纪要 / 05 开发计划 / 06 竞品调研 / 07 正式赛 checklist）
kit/
  rust-pro/  ★ 当前出战版本的开发主战场（官方 pro 算法 Rust 实现 + 我们的调优与结构性修正）
  python-pro/ 同算法 Python 参考（用于交叉验证移植保真度；平台 CPU 预算下会触发搜索降级，不作参赛版）
  python/    自研 M19 血统（archive：线上 25712.65，已被 rust-pro 取代）
  rust/      M19 的 Rust 移植（archive：线上 25597.00，验证过逐分一致，留作地基）
  typescript/ rust/   官方基础示例
  runner/    本地裁判引擎（与线上一致），verify_engine.py / run_local.py
  local-cards/L1–L4  本地练习卡（含天气真值，可离线复现得分）
  local-cards/L1-short{1,2,7,-f4}  短赛季/少光纤边界回归卡（自建）
  docs/      官方参赛指南中英全文
legacy/      旧 v3 协议时代的策略代码存档（仅供参考思路，协议已不兼容）
```

## 快速开始

```bash
cd kit/runner
python3 verify_engine.py                 # 1. 确认本地引擎状态全 OK
cp ../rust-pro/.env.example ../rust-pro/.env   # 2. 配置模型 API（OpenAI 兼容）
cd ../rust-pro && cargo build --release --locked   # 3. 构建出战 agent
cd ../runner && python3 run_local.py --card ../local-cards/L1 --agent "./target/release/rust-pro" --agent-cwd ../rust-pro --inherit-env
# 产物在 runner/run_output/（trace、decisions.csv、observations.csv、score_report.json）

# 4. 打包上传（排除 target/ 与 .env）：
cd ../rust-pro && zip -r ../agent.zip . -x "target/*" ".env" ".cargo-home/*"
```

调参：所有旋钮都是 `PRO_<名字>` 环境变量（见 `kit/rust-pro/src/planner.rs` 的 `Params::load`），
本地扫参用 `run_local.py --inherit-env` 传入；上线配置写进 `observer.project.json` 的 `environment`。

## 开发约定

- 所有日志写 stderr，stdout 只输出协议 JSON（违反 → `agent_error` 终止）。
- 密钥只进 `.env`（已 gitignore），绝不提交；提交平台用 zip 或公开仓库链接。
- **E–H 泛化铁律**：不得按卡名/卡特征硬编码分支；夜数、光纤数、目标、计分参数全部运行时读取。
- 候选改动流程：本地 L1–L4 回归不判负 → 线上 A/B → 打赢当前 final 分才 `final set`。

## 来源与许可

官方赛题、任务卡、文档、示例和裁判引擎来自 [GOSIM 2026 Agentic Observer Challenge](https://github.com/gosimfoundation/hackathon-survey26)，适用 **CC BY-NC 4.0**，详见 [`kit/LICENSE.md`](kit/LICENSE.md)。第三方依赖保留各自许可。本仓库的公开可见性不改变上述许可；对参赛者新增代码，本次公开未另行授予许可证。
