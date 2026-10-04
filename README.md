# GOSIM 智能体巡天黑客松 · 参赛项目（survey26）

GOSIM "智能体巡天" 黑客松（survey26）：提交一个自主决策的智能体，模拟操作光谱巡天望远镜完成一整季巡天——决定每次曝光的时刻、指向（alt/az）、时长、观测程序（DARK/BRIGHT/BACKUP）与光纤↔目标指派，与真实天气、隐藏事件、900 秒墙钟时限对抗。

- 比赛平台：<https://create.gosim.org/survey26/platform/>（报名 / 提交 / 榜单）
- 参赛文档（完整版）：`kit/docs/participant-guide.zh.md`
- 官方示例源码仓库：<https://github.com/gosimfoundation/hackathon-survey26>
- 比赛时间（UTC+8）：正式赛 10 月 5 日 00:00 – 10 月 7 日 23:59；隐藏卡 E–H 决赛评测在截止后由主办方运行；预计 10 月 10 日前出成绩。

## 目录结构

```
docs/        需求文档与情报（01 协议需求 / 02 计分 / 03 任务卡与赛程 / 04 讲座纪要 / 05 开发计划）
kit/         官方 v4 示例套件（2026-10-02 release，只读参考基线）
  python/    ★ 我们的开发主战场：Python 示例智能体（anchor-search planner + LLM night advice）
  typescript/ rust/   同算法的其他语言示例
  runner/    本地裁判引擎（与线上一致），verify_engine.py / run_local.py
  local-cards/L1–L4  本地练习卡（含天气真值，可离线复现得分）
  docs/      官方参赛指南中英全文
legacy/      旧 v3 协议时代的策略代码存档（my_strategy_v3.py，仅供参考思路，协议已不兼容）
```

## 快速开始

```bash
cd kit/runner
python3 verify_engine.py                 # 1. 确认本地引擎状态全 OK
cp ../python/.env.example ../python/.env # 2. 配置模型 API（Kimi: https://api.kimi.com/coding/v1，模型 kimi-for-coding 或 k3）
python3 run_local.py --card L1 --agent "python3 agent.py" --agent-cwd ../python   # 3. 本地跑 L1 卡
# 产物在 runner/run_output/（trace、decisions.csv、observation.csv、score report）

cd ../python
python3 pack_agent.py --out ../agent.zip # 4. 打包 → 平台「参赛」页上传（或传公开仓库链接）
```

## 开发约定

- **在 `kit/python/` 里开发自己的策略**（planner.py / state.py / scoring.py 是重点改动区）；`kit/` 其余部分尽量保持与官方一致，方便对照。
- 所有日志写 stderr，stdout 只输出协议 JSON（违反 → `agent_error` 终止）。
- 密钥只进 `.env`（已 gitignore），绝不提交；提交平台用 zip 或公开仓库链接。
- 每日评测额度以平台「参赛」页实时显示为准（UTC 0:00 / 北京 8:00 重置）；本地验证通过前不要浪费线上次数。

## 策略与 LLM 环节（供评委代码审查）

智能体 = 确定性锚点搜索规划器（`agent_core/planner.py`）+ 三个真实 LLM 调用环节
（`agent_core/llm_client.py`，OpenAI 兼容接口，平台代理注入密钥）：

1. **每晚开局的预报咨询**：读当晚 forecast 通告，输出避让方位与时长建议（JSON）
2. **每晚开局的公告核对**：读实时 bulletin 与近期命中率，独立输出同类建议；
   两组答案合并前经过实据校验——建议避让的方位必须在当晚通告中真实出现，
   凭空发明的方位直接丢弃（日志可见 `dropping unsupported avoid advice`）
3. **仪器故障举报的 LLM 确认**：规则检测器（质量中位数断裂 + 暗源对照）发现
   疑似故障后，由 LLM 复核证据再决定是否占用宝贵的举报额度

设计结论（六轮云端 A/B，练习卡均分）：LLM 数值建议**应用**到调度后是负期望
——曝光/举报证据链对序列扰动混沌敏感，连方向正确的避让都会打断故障检测
（单卡 −1300）。因此决赛版本（M7）中建议经解析、校验、追踪后**只记录不应用**，
调度由确定性规则与实测 scale 学习承担；LLM 调用、校验与故障确认否决路径全部
真实保留。详见 `docs/05-开发计划.md` M8–M10 节。

## 状态速览

- [x] 报名组队完成
- [x] 官方 v4 套件入库、需求文档整理（docs/01–07）
- [x] 本地 L1–L4 钉死基线 4558 / 4957 / 4852 / 4406
- [x] 协议健壮性 + Hard mode（state_resync / pointing_offset）处理
- [x] 策略层：required 临界曝光档、请求弱加成、自适应故障举报链（证据中位数断裂 + 暗源对照 + 举报额度 2/6 自适应）
- [x] ≥2 个 LLM 驱动环节（3 个真实调用点，见上节）
- [x] 练习赛六轮 A/B 定稿 **M7**（云端 4444.42 = α3893/β4427/γ4714/δ4744，MiniMax 活跃下确定性复现），已 `final set` 为最终版本（2026-10-05）

详见 `docs/05-开发计划.md`。
