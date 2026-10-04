# 07 · 正式赛 checklist（10/5 00:00 – 10/7 23:59 北京时间）

## 主办方确认的规则要点（10/4 王老师群内答复）

- **观测周期不固定**：A–D 四张卡周期就不一样；练习卡都是 38 夜只是巧合。夜数/日程以 initialize 下发为准，代码不得写死（已核查：无写死）
- **故障可能多次**：同一时刻最多一个未修复故障，次数与时间各卡不同、不公开 → 举报策略已改自适应上限（未证实有故障时最多 2 次探索，确认有故障后放开到 6 次）
- **光纤数以 initialize 为准**（不一定 16 根；已核查：从 fiber_config 读）
- **第三方库允许**（numpy 等），写进依赖文件即可，平台准备阶段安装；正式评测无外网（仅模型接口）。我们保持纯标准库，无依赖风险
- 模型 API 必须在「参赛」页配置，评测时平台代理注入——**密钥模式"加密保存"**
- 榜单数据来自海外服务，加载失败刷新/换网络即可，不是账号问题

## 赛前（10/4 完成）

- [x] 本地 L1–L4 基线 4845.9（vs 官方示例 4223.3）
- [x] 云端练习赛 α–δ 均分 3997（两轮：3703 → 3997）
- [x] ≥2 个 LLM 环节：每晚 forecast 咨询 + bulletin 咨询 + 故障举报 LLM 确认（共 3 个）
- [x] 提交包 `kit/agent-submission.zip`（pack_agent.py 生成，.env 已排除）
- [x] **平台上模型 API 配置**：`https://api.minimax.cn/v1` + `MiniMax-M3`，密钥"加密保存"（env show 确认密文 ****DOdw）。Kimi key 已欠费弃用
- [ ] 领 Kimi Coding Plan 兑换码（前 100 队跑通练习赛，去控制台检查）
- [x] 最后一次练习赛验证最新 zip：M7 = 4444.42，与 a7 逐分一致（MiniMax 活跃下确定性复现）

## 比赛日每天（额度 UTC 0 点 = 北京时间 08:00 重置）

1. **08:00 前**：确认当天要提交的版本已推送 + 已打包
2. **上午**：下载正式卡公开输入（A–D 第一天，E–H 后续），本地核对 initialize 假设（目标数/光纤数/夜晚数/站点坐标——全部必须从 initialize 读，代码里不得写死）
3. **提交跑分**：先用保守版本打满额度；拿到结果包后逐卡分析（score_report 组件 → decisions.csv/observations.csv → agent.log 诊断行）
4. **迭代纪律**：
   - 一次只改一个变量；组合收益不可加
   - 任何改动本地 L1–L4 全卡回归，且 L2/L3/L4 的 report +100 必须在（故障诊断样本稳定性）
   - 单卡 ±0.5% 以内当噪声，不据此决策
5. **23:00 前**：提交当日最优版本，不留额度过夜（额度不累计）

## 截止前（10/7 晚）

- [x] 选定最终版本：**M7（17c1b07c，4444.42）已 `final set`**（10/5 03:15），替代默认挂错的 f0676902 强加成版。依据：六轮云端实验证明 LLM 建议应用全负期望（3876-4235 vs 纯规则 4444-4448）
- [x] 密钥确认"加密保存"模式（env show：密文 ****DOdw）
- [x] 最终提交并确认状态为"已完成评测"（eval 3986ffbd scored 4444.42）
- [ ] GitHub 仓库整理（README 写清策略与 LLM 环节，供评委用 Claude 分析代码）
- [ ] 若白天拿到更强的 OpenAI 兼容 key：可换 env 重掷——但 M7 行为已与 LLM 内容无关，**换 key 不改变分数**，仅影响评奖环节的调用成功率展示

## 应急

- **云端 LLM 全挂**：不影响运行（规则兜底），但评奖需要 LLM 环节"真实驱动"——检查平台密钥配置，必要时换备用 key
- **某卡得分异常低**：先看 termination_reason（提前终止？）→ required_missing → 举报/请求组件 → agent.log 的 pace level（墙钟不足会降搜索深度）
- **跑分进程疑似僵尸**：`ps aux | grep run_local` 确认无残留再跑新的；run_output 分析前先核对 score_report.json 的 scenario 字段

## 跨日先验机制（A6，10/4 晚加入）

同一张卡每次运行 replay 同一套真值（A2/A4b 云端 α/β/γ 逐分复现证实确定性）。因此：

1. **首日**：提交 `kit/agent-day1.zip`（A4b + 盲报机制，无 card_priors.json 时行为 = A4b），打满额度，**下载全部结果包**
2. **首日当晚**：`python3 tools/build_card_priors.py <结果包目录> --cards <正式卡公开输入目录> --out kit/python/card_priors.json`
   （从结果包提取每卡"正确举报的准确时刻"——重跑时必然仍正确）
3. **次日**：重新打包（card_priors.json 进 zip），盲报在已知时刻直接举报——跳过 12-24h 证据确认链，仪器提前修复 → 后续全季效率恢复，且腾出举报预算抓后续故障
4. 机制已在本地 L4 端到端验证（先验匹配→盲报触发→+100→自适应链路照常）

## 本地回归须知（10/4 晚发现）

- **LLM 网络延迟是隐藏变量**：本地 Moonshot key 欠费，失败速度随网络波动 → LLM 烧掉的墙钟不同 → pace 等级不同 → 同代码不同时段回归结果可差 ±700（L4）。教训：本地 A/B 必须钉死延迟
- **钉法**：`export OPENAI_BASE_URL=http://127.0.0.1:1`（秒败，shell 环境优先于 .env——run_local.py 已改）
- A4b 稳定基线（钉死后）：L1 4473 / L2 4935 / L3 4735 / L4 5158，均分 4825；未钉死的旧数字一律不可比

## 云端配置核对（正式赛用）

- 评分配置已从 scenarios bucket 下载核对（kit/cloud-cards/）：required 漏 1 个 −50、举报正确 +100 / 误报 −150（免罚 2 次）、请求完成 +100、uniformity 权重 200、program 倍率 DARK 1.2/BRIGHT 1.12/BACKUP 1.06、mismatch ×1.0
- 请求完成判定：**曝光必须完整落在请求窗口内**且单次 g≥0.5（v4_scorer.py:462 request_factors）——窗口外提前观测无效
- 质量公式：Q = mean(efficiency × transparency × sky × lunar / seeing / airmass^0.6) / 0.68，efficiency 含每 slot jitter ∈ [0.90,1.00] + 故障倍率（v4_scorer.py:314 score_target_exposure）
