# rust-pro —— python-pro 的 Rust 移植版

[English README see README.md](README.md)

这是高分参考智能体 [`../python-pro`](../python-pro/README.zh.md) 的逐行 Rust 移植（GOSIM survey26 望远镜巡天赛题，
`participant-agent-protocol-v4`）。策略、常数、运算顺序和三个大模型环节都与 python-pro 相同，策略本身的说明见
[python-pro 的 README](../python-pro/README.zh.md)。运行时只用协议给智能体的信息（星表、公开评分配置、公报、预报、
自己的观测结果），从不读取卡片文件。

移植带来的是速度：每次决策的 CPU 时间约为 Python 版的 1/30，所以在平台的公平时钟下，即使是长卡，规划器也始终用
完整搜索（搜索力度 0）。

## 成绩（本地引擎，确定性的桩模型）

| 卡 | python-pro | **rust-pro** | 计费 CPU 时间（python-pro / rust-pro） |
|---|---:|---:|---:|
| 本地卡 L1 | 6,121.4 | **6,121.4** | 101–130 秒 / 5 秒 |
| 本地卡 L2 | 6,392.7 | **6,392.7** | 101–118 秒 / 5 秒 |
| 本地卡 L3 | 6,796.4 | **6,796.4** | 166–202 秒 / 6–7 秒 |
| 本地卡 L4 | 6,799.3 ± 0.1 | **6,789.3** | 170–201 秒 / 6–7 秒 |
| 套件 demo 卡 | 1,717.8 | **1,717.8** | 22–31 秒 / 1 秒 |

每张卡用 `run_local.py` 和正常的 900 秒公平时钟各跑三次；模型是本地的桩服务，按请求内容给出确定的回答（两个智能体
拿到相同的回答），所以几次运行之间只有节奏控制会带来差别。固定搜索力度（`PRO_FIXED_LEVEL=0`）时，两个智能体在五张卡上
发出的决策序列完全相同。L4 上 python-pro 为了不超出 CPU 预算，曾短暂降到较省的搜索力度 1，碰巧多得 10 分；rust-pro
不需要降级。使用真实模型（Kimi `k3`）时，单次运行之间的波动比这大得多（见 python-pro 的 README）。

## 目录结构（每个文件对应 python-pro 的一个模块）

```
src/main.rs        agent.py       入口：协议主循环、节奏控制、仪器故障报告、大模型环节的接线
src/planner.rs     planner.py     一次搜索同时决定指向、光纤、时长和项目；从观测结果中学习
src/skymath.rs     skymath.py     公开天球几何：恒星时、地平坐标、切平面投影、光纤网格、月亮
src/advisor.rs     advisor.py     大模型环节：夜间计划、故障复核、付费报告确认
src/llm_client.rs  llm_client.py  OpenAI 兼容客户端，调用在后台线程里跑
observer.project.json   平台清单（cargo build --release --locked；./target/release/rust-pro）
pack_agent.py      打包上传用的 ZIP（不会打包 target/ 和 .env）
.env.example       复制成 .env，本地运行前填好 API key
```

简要说明（详见 python-pro 的 README）：一次搜索以价值锚点和密度锚点为中心、再小步微调，最大化 `收益 − λ·T`；
项目档位按饱和命中拟合；用 `E = 质量水平 / 档位水平` 判断仪器故障，先用免费误报额度、再谨慎付费探测，地震公告后
12 小时内不探测、之后只在 E 再下一个台阶时探测；隐藏指向偏差按光纤间距缩放的网格、根据光纤命中/落空估计；必选
目标等到天空接近最好且不是预报坏天气的夜晚再尝试；观测请求按全有全无的价值计算。节奏按公平时钟控制
（`wallclock.remaining_real_cpu_seconds` 对比进程自己的 CPU 时间 `getrusage`，并留意实际时间上限）。大模型（默认
Kimi `k3`）每晚开始时问两次（夜间计划、故障复核），付费报告前再问一次，全部在后台线程里，从不阻塞决策。

## 配置（.env）

```
OPENAI_API_KEY=sk-...                            # 必填（也认 KIMI_API_KEY）
OPENAI_BASE_URL=https://api.kimi.com/coding/v1   # 默认；中国大陆以外账号用 https://api.kimi.ai/coding/v1
OPENAI_MODEL=k3                                  # 默认
```

没有 key 时，程序启动即退出并提示 `missing API key: set OPENAI_API_KEY`；`OBSERVER_MODEL_DISABLED=1`（选择「本次不提供模型」或 `survey26 eval start --no-model` 的评测由平台设置）：此时不需要 key，智能体只用规则运行，便于对比有无大模型的表现。在平台上，队伍变量就是程序的环境变量。
不发送 temperature（`k3` 只接受默认值）。HTTP 429/5xx 和网络错误会退避重试。

## 本地编译与运行

```bash
cargo build --release --locked
python3 ../_local/runner/run_local.py --inherit-env --card ../_local/cards/L1 --agent "./target/release/rust-pro" --agent-cwd .
python3 pack_agent.py --out ../rust-pro-agent.zip
```

每个常数都可以用与 python-pro 相同的 `PRO_<名字>` 环境变量覆盖（例如 `PRO_LAMBDA_FRAC=0.5`；`PRO_FIXED_LEVEL=0`
固定搜索力度）。平台在只读容器里编译，所以 `observer.project.json` 把 `CARGO_HOME` 指到 `/tmp`。

## 许可

任务卡、模拟数据、评测代码和示例项目采用 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) 许可；请引用
GOSIM 2026 Agentic Observer Hackathon（https://create.gosim.org/survey26/）。详见 `../LICENSE.md`。
