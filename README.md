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

## 状态速览

- [x] 报名组队完成
- [x] 官方 v4 套件入库、需求文档整理（docs/01–05）
- [ ] 本地跑通 L1 基线分（讲座参考值 ≈4566）
- [ ] 协议健壮性 + Hard mode（state_resync / pointing_offset）处理
- [ ] 策略层：required 保障、均匀度、天气反推、program 判档
- [ ] ≥2 个 LLM 驱动环节（评奖硬门槛）
- [ ] 正式赛 A–D 评测迭代 → 选定最终版本

详见 `docs/05-开发计划.md`。
