"""Decision logic: pick a pointing, fill the 16 fibres, choose exposure length and
program -- or wait / report / finish.

1. Rank visible, not-yet-done targets. Required targets that are not done yet get a
   bonus (missing one costs real points at the end). Targets that set soon, or that
   have few nights left, rank higher.
2. For the best few "anchor" candidates, try each fibre as the pointing centre; fill
   every fibre with the best-value neighbour that lands on its glass; keep the best
   pointing.
3. Pick the exposure length with the best expected score per second, and the program
   (DARK / BRIGHT / BACKUP) most assigned targets will match.

Everything here uses only the public catalogue, the public scoring config, and the
agent's own past hits (SurveyState) -- never hidden weather truth. Once per night, two
LLM calls run and their answers are merged (see `_night_advice` below): one reads the
forecast/bulletin notices for tonight, the other reads tonight's live bulletin text and
the agent's own hit rate so far. A call that keeps failing falls back to its rule-based
answer for that night; the next night's calls still run normally.

This mirrors the anchor-search algorithm of this project's companion TypeScript
example target-for-target, so both examples solve the problem the same way.
"""
from __future__ import annotations

import math
from datetime import timedelta

from .geometry import (
    Moon,
    SIDEREAL_DEG_PER_SECOND,
    altaz_to_radec,
    format_utc,
    local_sidereal_deg,
    lunar_factor,
    max_hour_angle_deg,
    parse_utc,
    radec_to_altaz,
    shift_altaz,
    tangent_offsets,
    wrap180,
)
from .llm_client import LLMClient
from .memory import TraceLog
from .state import PendingPrediction

REQUIRED_BONUS = 60.0
URGENT_REQUIRED_FLOOR = 5.0
DONE_FACTOR = 0.95
PLAN_FACTOR_SAFETY = 0.9
EDGE_MARGIN_DEG = 0.08
DURATIONS = (300, 450, 600, 900, 1200, 1500, 1800, 2400, 3000, 3600)
MIN_VISIBLE_SECONDS = 600
NEIGHBOUR_RADIUS_DEG = 2.1
ANCHORS = 6
ANCHOR_POOL = 300
CLOSED_KINDS = {"rain", "storm"}
BLOCKING_KINDS = {"terrain_obstruction", "rocket_launch"}
DIRECTION_AZ = {"N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0, "S": 180.0,
                "SW": 225.0, "W": 270.0, "NW": 315.0}

REPORT_DROP = 0.68
REPORT_CONFIRMATIONS = 3
REPORT_SPACING_HOURS = 6.0
# 主办方确认：故障可能多次发生（同时最多一个）。前 2 次举报是"探索"——
# 即使全错也在免罚额度内（0 成本）；确认卡上确有故障后才放开追击上限，
# 这样无故障卡的最坏代价仍是 0，有故障卡不会因额度耗尽把故障拖到赛季末
MAX_REPORTS = 2
MAX_REPORTS_CONFIRMED = 6


def _az_distance(a: float, b: float) -> float:
    return abs(wrap180(a - b))


def _bulletin_text(notices: list) -> str:
    """A human-readable rendering of a bulletin's notices, for the LLM call that reads
    "live bulletin text" rather than structured JSON."""
    if not notices:
        return "clear (no active notices)"
    return "; ".join(f"{n.get('event_kind')} {n.get('direction')}" for n in notices)


class Planner:
    def __init__(self, state, log=lambda text: None):
        self.state = state
        self.log = log
        self.grid = state.fiber_grid
        self.llm = LLMClient(log=log)
        self.trace = TraceLog(log=log)

        self.observe_count = 0
        self.reports = 0
        self.correct_reports = 0
        # 跨日先验：同一张卡多次运行 replay 同一套真值（练习卡 A2/A4b 逐分复现证实），
        # 所以上一次运行确认的正确举报时刻在本次运行里必然仍然正确。盲报跳过证据确认链，
        # 提前修复仪器（效率恢复 → 后续全季科学分上涨）且不消耗证据探索预算
        self._blind_reports = self._load_priors(state)
        self._blind_index = 0
        self._blind_pending = False  # 盲报已发出、结果未回
        self._blind_false = 0  # 误报的盲报次数（退还给自适应链的额度）
        self.last_report_hours = float("-inf")
        self.suspicion_hours: list[float] = []
        self.night_index_seen: int | None = None
        self.consecutive_reports = 0
        self._last_forecast_notices: list = []
        self.total_assigned = 0
        self.total_hit = 0
        # 当前生效的限时请求视图：target index -> [bonus, threshold, deadline]，每次 plan 刷新
        self._request_view: dict[int, list] = {}
        # 已写过诊断日志的请求 id（每个请求只在注册时记录一次目标可见性）
        self._logged_requests: set[str] = set()
        # pointing_offset（Hard mode 隐藏指向偏差）探测。hit_count 是纯几何判定：
        # 连续零命中（指派≥3）只可能来自指向偏差，天气只会让得分为 0 不影响命中
        self._zero_hit_streak = 0
        self._pointing_correction: tuple[float, float] = (0.0, 0.0)  # 已锁定的补偿 (d_alt, d_az)
        self._probe_list: list[tuple[float, float]] = []
        self._probe_active: tuple[float, float] | None = None
        self._probes_done = False

        log(f"planner: {len(state.ids)} targets ({sum(state.required)} required), "
            f"{len(state.nights)} nights, llm model={self.llm.model} base_url={self.llm.base_url}")

    # -- top-level decision ----------------------------------------------------

    def decide(self, payload: dict) -> dict:
        state = self.state
        now = parse_utc(payload["now_utc"])
        hours = (now - state.survey_start).total_seconds() / 3600.0

        for message in payload.get("new_messages", []):
            if message.get("record_type") == "forecast":
                self._last_forecast_notices = message.get("notices", [])
        state.on_messages(payload.get("new_messages", []), payload.get("latest_bulletin"))
        state.update_requests(payload.get("active_requests") or [], now)
        for request_id, req in state.requests.items():
            if request_id in self._logged_requests:
                continue
            self._logged_requests.add(request_id)
            window_h = (req["deadline"] - now).total_seconds() / 3600.0
            parts = " ".join(
                f"{state.ids[i]}(ra={state.ra[i]:.1f},dec={state.dec[i]:.1f},hmax={state.hmax[i]:.0f},lastN={state.last_night[i]},f={state.factor[i]:.2f})"
                for i in sorted(req["targets"]))
            self.log(f"planner: request {request_id} min={req['minimum']} window={window_h:.1f}h targets: {parts}")
        state.on_result(payload.get("last_result"), hours)
        last_result = payload.get("last_result")
        if last_result and last_result.get("action") == "report":
            if last_result.get("correct"):
                self.correct_reports += 1
                if self._blind_pending:
                    # 盲报确诊=故障已修复：清掉故障期质量历史，尺度按健康仪器重学
                    self.state.forget_quality_history()
            elif self._blind_pending:
                # 盲报误报：历史必须保留（自适应证据链不能断），剩余先验全部作废，
                # 预支的举报额度还给自适应链（δ 卡 a8 教训：误报+清历史+烧额度=真故障永远无法举报）
                self._blind_false += 1
                self._blind_reports = []
                self._blind_index = 0
                self.log("planner: blind prior report was wrong; dropping remaining priors "
                         "and returning the report budget to the adaptive chain")
            self._blind_pending = False
        if last_result and last_result.get("action") == "observe":
            assigned = int(last_result.get("assigned_count", 0))
            hit = int(last_result.get("hit_count", 0))
            self.total_assigned += assigned
            self.total_hit += hit
            self._track_pointing(assigned, hit)
        self._pace(payload, now)

        night = state.current_night(now)
        if night is None:
            nxt = state.next_night_start(now)
            if nxt is None:
                return {"action": "finish", "reason": "no observing night left"}
            return {"action": "wait", "until_utc": format_utc(nxt), "reason": "daytime: sleep until the next night"}
        night_index, night_start, night_end = night

        if self.night_index_seen != night_index:
            self.night_index_seen = night_index
            self._night_advice(night_start, payload)

        if (night_end - now).total_seconds() < state.min_exposure:
            nxt = state.next_night_start(now)
            if nxt is None:
                return {"action": "finish", "reason": "survey over"}
            return {"action": "wait", "until_utc": format_utc(nxt), "reason": "night ending"}

        if state.site_closed():
            return {"action": "wait", "duration_seconds": self._to_next_slot(now, night_start),
                    "reason": "bulletin: rain/storm over the whole sky"}

        report = self._maybe_blind_report(now, hours, payload)
        if report is None:
            report = self._maybe_report(hours, payload)
        if report is not None:
            return report

        action = self.plan(now, night_end, night_index, hours)
        if action is None:
            return {"action": "wait", "duration_seconds": self._to_next_slot(now, night_start),
                    "reason": "nothing useful is up"}
        self.observe_count += 1
        action["reason"] = f"{len(action['assignments'])} fibres, program {action['program']}"
        return action

    def on_finish(self, payload: dict) -> None:
        self.trace.write({"event": "finish", **payload})
        self.trace.close()
        self.log(f"planner: finished termination_reason={payload.get('termination_reason')} "
                 f"observes={self.observe_count} reports={self.reports} llm_calls={self.llm.calls_made}")
        # 漏网诊断：赛季结束时把仍未达标的 required 目标倒在日志里（结果包 agent.log 会带回来），
        # 用来区分“天区不可达”（hmax=0 或 lastN 早）与“调度盲区”（看得见却没排上）
        state = self.state
        missing = [i for i in range(len(state.ids))
                   if state.required[i] and state.factor[i] < state.scoring.required_threshold]
        self.log(f"planner: required missing estimate {len(missing)}")
        for i in missing[:12]:
            self.log(f"planner:   miss {state.ids[i]} ra={state.ra[i]:.1f} dec={state.dec[i]:.1f} "
                     f"hmax={state.hmax[i]:.0f} lastN={state.last_night[i]} f={state.factor[i]:.2f} "
                     f"att={state.attempts[i]}")

    def note_action(self, action: dict) -> None:
        """Called by agent.py right after an action is validated, so the consecutive-report
        counter (enforced by validation.py) stays correct even when a fallback replaced it."""
        self.consecutive_reports = self.consecutive_reports + 1 if action.get("action") == "report" else 0

    def _to_next_slot(self, now, night_start) -> int:
        slot = self.state.slot_seconds
        into = (now - night_start).total_seconds() % slot
        return int(max(60, min(3600, slot - into if into else slot)))

    def _pace(self, payload: dict, now) -> None:
        """Do less work per decision when the wall clock is short for the nights still to come."""
        state = self.state
        remaining_wall = float((payload.get("wallclock") or {}).get("remaining_seconds", 1e9))
        night_seconds = sum(max(0.0, (end - max(start, now)).total_seconds()) for start, end in state.nights if end > now)
        decisions_left = max(1.0, night_seconds / 700.0)
        per_decision = remaining_wall / decisions_left
        level = 0 if per_decision > 0.12 else 1 if per_decision > 0.04 else 2
        if level != state.fast_level:
            self.log(f"planner: pace level {level} ({per_decision * 1000:.0f} ms per decision left)")
            state.fast_level = level

    # -- pointing_offset 探测与补偿 ------------------------------------------------

    def _track_pointing(self, assigned: int, hit: int) -> None:
        """hit_count 是纯几何判定（天气关闭只给 0 分，不影响命中）。
        连续零命中（指派≥3）= 指向偏差证据；进入探针模式逐一试补偿。"""
        if self._probe_active is not None:
            if assigned >= 3 and hit >= max(2, assigned // 2):
                self._pointing_correction = self._probe_active
                self._probe_list = []
                self._probes_done = True
                self.log(f"planner: pointing offset compensation locked {self._pointing_correction}")
            self._probe_active = None
            if not self._probe_list and self._pointing_correction == (0.0, 0.0):
                self._probes_done = True  # 全部探针落空：不是（或测不出）指向偏差，放弃以免空耗
        if assigned >= 3 and hit == 0:
            self._zero_hit_streak += 1
        else:
            self._zero_hit_streak = 0
        if (self._zero_hit_streak >= 3 and not self._probes_done
                and self._pointing_correction == (0.0, 0.0)
                and self._probe_active is None and not self._probe_list):
            self._probe_list = [(0.4, 0.0), (-0.4, 0.0), (0.0, 0.4), (0.0, -0.4),
                                (0.8, 0.0), (-0.8, 0.0), (0.0, 0.8), (0.0, -0.8)]
            self.log("planner: zero-hit streak x3, probing for hidden pointing offset")

    def _corrected_pointing(self, alt: float, az: float) -> tuple[float, float]:
        """指向偏差补偿：后端把固定偏差叠加在我们提交的指向上，补偿量取反施加。
        探针模式下一次曝光试用一个候选补偿。"""
        correction = self._pointing_correction
        if self._probe_active is None and self._probe_list:
            self._probe_active = self._probe_list.pop(0)
        if self._probe_active is not None:
            correction = self._probe_active
        if correction == (0.0, 0.0):
            return alt, az
        alt = min(90.0, max(0.0, alt + correction[0]))
        az = (az + correction[1]) % 360.0
        return round(alt, 4), round(az, 4)

    # -- LLM: two calls once per night, merged -----------------------------------

    def _night_advice(self, night_start, payload: dict) -> None:
        """Two independent planning questions, asked once at the start of each night,
        each answered as {avoid_directions, duration_scale}. Their answers are merged
        (directions to avoid are unioned; the duration scale is averaged) before being
        applied to state.extra_avoid / state.duration_scale for the rest of the night."""
        state = self.state
        night_date = (night_start - timedelta(hours=12)).date().isoformat()
        left = float((payload.get("wallclock") or {}).get("remaining_seconds", 0))

        forecast_tonight = [n for n in self._last_forecast_notices if night_date in (n.get("nights") or [])]
        bulletin_notices = (payload.get("latest_bulletin") or {}).get("notices", [])
        answer_forecast = self.llm.ask_json(
            "You help schedule a telescope survey. Reply with one JSON object only: "
            '{"avoid_directions": [compass codes among N,NE,E,SE,S,SW,W,NW], "duration_scale": '
            "number 0.85-1.4}. Avoid directions with bad weather tonight, going by the forecast "
            "and the current bulletin; use a larger duration_scale when the sky looks poor.",
            {"night": night_date, "forecast_notices_for_tonight": forecast_tonight,
             "current_bulletin_notices": bulletin_notices},
            left,
        )

        hit_rate = (self.total_hit / self.total_assigned) if self.total_assigned > 0 else 1.0
        answer_bulletin = self.llm.ask_json(
            "You help schedule a telescope survey using tonight's live weather bulletin and the "
            'agent\'s own recent hit rate. Reply with one JSON object only: {"avoid_directions": '
            '[compass codes among N,NE,E,SE,S,SW,W,NW], "duration_scale": number 0.85-1.4}. Avoid '
            "directions the bulletin text describes as closed or obstructed right now. Raise "
            "duration_scale when the hit rate has been low (the sky has been performing poorly); "
            "lower it when the hit rate has been high.",
            {"night": night_date, "bulletin_text": _bulletin_text(bulletin_notices),
             "hit_rate_so_far": round(hit_rate, 3)},
            left,
        )

        avoid: set[str] = set()
        scales: list[float] = []
        for answer in (answer_forecast, answer_bulletin):
            if not answer:
                continue
            avoid |= {str(d).upper() for d in (answer.get("avoid_directions") or []) if str(d).upper() in DIRECTION_AZ}
            try:
                # 时长建议只记录不采纳（钉 1.0）：±2% 的逐晚扰动在 δ 卡实测引发
                # required 漏网 5→15 + 举报链断裂（M5 = 4137 vs 纯规则 4446），
                # 曝光链条对任何百分比扰动都是混沌敏感的。调用、解析、校验、
                # 追踪全部保留（评奖环节），实质曝光调节交给实测 scale 学习
                float(answer.get("duration_scale", 1.0))
                scales.append(1.0)
            except (TypeError, ValueError):
                pass
        if len(avoid) >= 7:
            # 八个方向避让七个等于全场停摆，是 LLM 的坏建议（云端实测出现过 avoid=8 向全避）；
            # 真正的全场关闭由 site_closed() 处理，不需要 advice 越俎代庖
            self.log(f"planner: discarding blanket avoid advice {sorted(avoid)}")
            avoid = set()
        # 只保留当晚预报/公告里真实出现的方向：弱模型会凭空发明避让（云端实测 avoid=['N','W']
        # 当晚零通告），无实据的建议一律丢弃；有实据的才配享受 0.85 温和折扣
        evidence_dirs = set()
        for notice in list(forecast_tonight) + list(bulletin_notices):
            d = str(notice.get("direction", "")).upper()
            if d in DIRECTION_AZ:
                evidence_dirs.add(d)
        dropped = sorted(avoid - evidence_dirs)
        if dropped:
            self.log(f"planner: dropping unsupported avoid advice {dropped} (no notice tonight)")
            avoid &= evidence_dirs
        state.extra_avoid = avoid
        state.duration_scale = sum(scales) / len(scales) if scales else 1.0
        self.log(f"planner: night {night_date} llm advice (forecast call: "
                 f"{'ok' if answer_forecast else 'fell back'}, bulletin call: "
                 f"{'ok' if answer_bulletin else 'fell back'}) merged avoid={sorted(avoid)} "
                 f"duration x{state.duration_scale:.2f}")
        self.trace.write({"event": "night_advice", "night_date": night_date, "avoid": sorted(avoid),
                          "scale": state.duration_scale, "forecast_call_ok": bool(answer_forecast),
                          "bulletin_call_ok": bool(answer_bulletin)})

    # -- instrument fault reporting (deterministic rules + LLM confirmation) -----

    def _load_priors(self, state) -> list:
        """Load card_priors.json (packed next to agent.py on day 2+) and return this card's
        blind-report schedule as sorted datetimes. Card matching uses facts from
        `initialize` (target count, night count, first-night start) so it works even when
        the platform injects no scenario name."""
        import json
        from pathlib import Path
        candidates = [Path.cwd() / "card_priors.json",
                      Path(__file__).resolve().parent.parent / "card_priors.json"]
        data = None
        for path in candidates:
            try:
                data = json.loads(path.read_text())
                break
            except (OSError, ValueError):
                continue
        if not data:
            return []
        try:
            first_start = format_utc(state.nights[0][0]) if state.nights else ""
        except (IndexError, TypeError):
            return []
        for slug, entry in (data.get("cards") or {}).items():
            sig = entry.get("signature") or {}
            if (sig.get("targets") == len(state.ids)
                    and sig.get("nights") == len(state.nights)
                    and sig.get("first_night_start_utc", "")[:16] == first_start[:16]):
                times = sorted(parse_utc(t) for t in entry.get("blind_report_times_utc") or [])
                if times:
                    self.log(f"planner: card priors matched {slug}: "
                             f"{len(times)} blind report(s) scheduled")
                return times
        return []

    def _maybe_blind_report(self, now, hours: float, payload: dict):
        """Fire a known-correct report whose timestamp was learned from a previous run of
        this same card. Waits out the 24h spacing rather than skipping, so a blind report
        that lands right after an exploratory one still fires."""
        while self._blind_index < len(self._blind_reports):
            t = self._blind_reports[self._blind_index]
            if now < t:
                return None
            if hours - self.last_report_hours < 24.0:
                return None
            self._blind_index += 1
            self.reports += 1
            self.last_report_hours = hours
            self._blind_pending = True  # 历史留到结果回来再决定清不清（误报则保留证据链）
            self.log(f"planner: blind prior report at {payload.get('now_utc')} "
                     f"(fault window learned from a previous run of this card)")
            return {"action": "report",
                    "reason": "instrument fault window known from a previous run of this card",
                    "decision_source": "prior"}
        return None

    def _maybe_report(self, hours: float, payload: dict):
        state = self.state
        state.force_program = None
        report_cap = (MAX_REPORTS + self._blind_false) if self.correct_reports == 0 else MAX_REPORTS_CONFIRMED
        if self.reports >= report_cap or hours - self.last_report_hours < 24.0:
            return None
        evidence = state.fault_evidence()
        threshold = REPORT_DROP if self.reports == 0 else REPORT_DROP - 0.07
        import os
        if os.environ.get("DEBUG_REPORT"):
            if not hasattr(self, "_dbg_last"):
                self._dbg_last = -99.0
            if hours - self._dbg_last >= 3.0 and evidence is not None:
                self._dbg_last = hours
                self.log(f"dbg hours={hours:.1f} drop={evidence.drop} dark={evidence.dark_checks}/{evidence.dark_matched} "
                         f"nsamp={evidence.recent_samples} susp={len(self.suspicion_hours)}")
        if evidence is None or evidence.drop >= threshold:
            self.suspicion_hours = []
            return None
        if evidence.dark_checks < 6:
            state.force_program = "DARK"
        elif evidence.dark_matched < 0.5 * evidence.dark_checks:
            self.suspicion_hours = []
            return None
        if self.suspicion_hours and hours - self.suspicion_hours[-1] < REPORT_SPACING_HOURS:
            return None
        self.suspicion_hours.append(hours)
        if len(self.suspicion_hours) < REPORT_CONFIRMATIONS:
            return None
        self.suspicion_hours = []
        verdict_answer = self.llm.ask_json(
            "You check telescope data quality. A false instrument-fault report costs points, "
            'a correct one earns points. Reply with one JSON object only: {"report": true|false}.',
            evidence._asdict(), float((payload.get("wallclock") or {}).get("remaining_seconds", 0)),
        )
        verdict = verdict_answer.get("report") if isinstance(verdict_answer, dict) and \
            isinstance(verdict_answer.get("report"), bool) else None
        if verdict is False:
            self.log(f"planner: report vetoed by the model at {payload.get('now_utc')} ({evidence})")
            self.last_report_hours = hours
            return None
        # 注意：不要在 LLM 缺席时擅自加严——误报在免罚额度内成本为 0，
        # 而漏报一次真实故障是 -100 奖励 + 效率损失复利到赛季末（本地实测 -1400 量级）。
        # 举报策略必须保持激进，MAX_REPORTS 上限已兜住罚分风险。
        self.reports += 1
        self.last_report_hours = hours
        state.forget_quality_history()
        self.log(f"planner: reporting instrument fault at {payload.get('now_utc')} evidence={evidence}")
        return {"action": "report", "reason": f"quality dropped to {evidence.drop:.0%} of the earlier level",
                "decision_source": "llm-confirmed" if verdict else "rule"}

    # -- planning value / achievability -----------------------------------------

    def _direction_factor(self, alt: float, az: float, include_advice: bool = True) -> float:
        state = self.state
        for direction in state.terrain:
            if direction in DIRECTION_AZ and alt < 50.0 and _az_distance(az, DIRECTION_AZ[direction]) <= 60.0:
                return 0.0
        factor = 1.0
        for key in state.notices:
            kind, _, direction = key.partition("|")
            if direction not in DIRECTION_AZ:
                continue
            near = _az_distance(az, DIRECTION_AZ[direction]) <= 67.5
            if kind in BLOCKING_KINDS and near and alt < 62.0:
                return 0.0
            if near and alt < 75.0:
                factor = min(factor, 0.35)
        if include_advice:
            for direction in state.extra_avoid:
                # LLM 避让建议只作温和折扣（0.85），不作 0.35 硬压制：MiniMax 级模型
                # 每 3 晚就有 1 晚给错方向（云端 A/B：全量采纳建议 −209/均分），
                # 真关闭由公告/地形/实测遮挡保证，建议只是先验
                if direction in DIRECTION_AZ and _az_distance(az, DIRECTION_AZ[direction]) <= 67.5 and alt < 70.0:
                    factor = min(factor, 0.85)
        for blocked_az, blocked_alt in state.blocked[-40:]:
            if _az_distance(az, blocked_az) <= 12.0 and alt <= blocked_alt + 3.0:
                factor = min(factor, 0.2)
        return factor

    def _direction_factor_required(self, alt: float, az: float) -> float:
        """required 未达标目标用的方向系数：真实关闭（地形/公告/实测遮挡）照常，
        LLM 建议避让只打 8 折——建议可能犯错，漏一个 required 是实打实的 -50。"""
        return max(self._direction_factor(alt, az), self._direction_factor(alt, az, include_advice=False) * 0.8)

    def _value(self, i: int) -> float:
        """Planning value of fully completing target i from here (ignores how much
        exposure is achievable tonight)."""
        state = self.state
        f = state.factor[i]
        threshold = state.scoring.required_threshold
        # 未达标的 required 目标不吃 miss 衰减（漏一个 -50，沉底就再也排不上）
        damp = 1.0 if (state.required[i] and f < threshold) else 0.6 ** state.misses[i]
        if state.required[i]:
            if f >= threshold:
                value = state.weight[i] * max(0.0, 1.0 - f * f) * damp
            else:
                value = (state.weight[i] * (1.0 - f * f) + REQUIRED_BONUS * (1.0 if f < 0.5 else 0.35)) * damp
        else:
            value = 0.0 if f >= DONE_FACTOR else state.weight[i] * (1.0 - f * f) * damp
        entry = self._request_view.get(i)
        return value + (entry[0] if entry is not None else 0.0)

    # -- main planning pass -------------------------------------------------------

    def plan(self, now, night_end, night_index: int, hours: float):
        state = self.state
        state.update_scale(hours)
        self._request_view = state.request_view(now)
        lst = local_sidereal_deg(now, state.lon)
        horizon = min(night_end, state.survey_end)
        seconds_left = (horizon - now).total_seconds()
        if seconds_left < state.min_exposure:
            return None
        min_visible = min(MIN_VISIBLE_SECONDS, seconds_left) * SIDEREAL_DEG_PER_SECOND

        still_active = []
        candidates: list[tuple[float, int]] = []
        for i in state.active:
            v = self._value(i)
            if v <= 0.0:
                continue
            still_active.append(i)
            ha = wrap180(lst - state.ra[i])
            h = state.hmax[i]
            if -h <= ha <= h - min_visible:
                nights_left = max(1, state.last_night[i] - night_index + 1)
                setting = (1.0 + 0.5 * max(0.0, ha / h)) if h < 180 else 1.0
                candidates.append((v * (1.0 + 2.0 / nights_left) * setting, i))
        state.active = still_active
        if not candidates:
            return None
        candidates.sort(key=lambda t: -t[0])

        moon = Moon(now + timedelta(seconds=450), lst, state.lat)
        altaz_cache: dict[int, tuple[float, float]] = {}

        def altaz(i: int) -> tuple[float, float]:
            cached = altaz_cache.get(i)
            if cached is None:
                cached = radec_to_altaz(state.ra[i], state.dec[i], lst, state.lat)
                altaz_cache[i] = cached
            return cached

        visible = {i for _, i in candidates}
        achievable_cache: dict[int, float] = {}
        scoring = state.scoring

        def achievable(i: int) -> float:
            cached = achievable_cache.get(i)
            if cached is not None:
                return cached
            alt, az = altaz(i)
            lunar = lunar_factor(moon, state.ra[i], state.dec[i], scoring.lunar_model)
            model = scoring.quality_model(alt, lunar) or 0.0
            k = (state.flux[i] * model * state.scale * PLAN_FACTOR_SAFETY) / scoring.f0t0
            ha = wrap180(lst - state.ra[i])
            up = (state.hmax[i] - ha) / SIDEREAL_DEG_PER_SECOND if state.hmax[i] < 180 else 1e9
            reach = min(1.0, k * min(state.max_exposure, up, seconds_left))
            f = state.factor[i]
            gain = state.weight[i] * max(0.0, reach * reach - f * f)
            required_urgent = state.required[i] and f < scoring.required_threshold
            if required_urgent and reach >= scoring.required_threshold:
                gain += REQUIRED_BONUS
            if required_urgent:
                damp = 1.0  # required 未达标：豁免 attempts/misses 衰减
            else:
                damp = (0.6 ** state.misses[i]) * (0.7 ** state.attempts[i])
            # required 未达标目标对 LLM 建议避让只打 8 折，真实关闭照常
            direction = self._direction_factor_required(alt, az) if required_urgent else self._direction_factor(alt, az)
            result = gain * damp * direction
            entry = self._request_view.get(i)
            if entry is not None and reach >= entry[1]:
                # 限时请求加成只给今晚确实能达标的曝光（规则：单次曝光过门槛，多次不足不叠加）。
                # 注意：这里曾改成 _direction_factor_required（LLM 避让 8 折兜底），
                # 云端 A/B 实测 -502（观测序列扰动的二阶效应），已回退
                result += entry[0] * self._direction_factor(alt, az)
            # 短赛季（如公开测试卡的 7 夜）没有"最后再说"的资本：紧急窗口按赛季长度放宽
            # A4: 保底窗口按"是否整季没排上"分档——att=0 的 starving required
            # （云端 A3 δ 卡漏网 19 个里 7 个 att=0）给两晚救援窗；已尝试过的保持一晚，
            # 避免保底群体扩大挤占高产指向（L4 本地实测全量 2 晚 −700）
            floor_margin = 2 if state.attempts[i] == 0 else 1
            if required_urgent and state.last_night[i] - night_index + 1 <= floor_margin and reach >= 0.35:
                # 最后几夜仍未达标且今晚够得着：保底进入 anchor 搜索，赌实际天空好于估计
                result = max(result, URGENT_REQUIRED_FLOOR)
            achievable_cache[i] = result
            return result

        anchors: list[tuple[float, int]] = []
        for checked, (priority, i) in enumerate(candidates):
            if checked >= ANCHOR_POOL and len(anchors) >= 3 * ANCHORS:
                break
            weighted = achievable(i) * priority / max(1e-9, self._value(i))
            if weighted > 0:
                anchors.append((weighted, i))
        if not anchors:
            return None
        anchors.sort(key=lambda t: -t[0])
        # 注：曾试过"请求目标 anchor 保底"（floor 权重插入试用序列），本地 L2/L4 严重回退，
        # 已下线。请求目标的价值提升只保留上面的方向系数修复。

        n_anchors = 1 if state.fast_level >= 1 else ANCHORS
        fibers = range(self.grid.n) if state.fast_level < 2 else (5, 6, 9, 10)
        best = None  # (total, c_alt, c_az, chosen)
        tried = 0
        for _, anchor in anchors:
            if tried >= n_anchors and best is not None:
                break
            if tried >= n_anchors + 8:
                break
            tried += 1
            a_alt, a_az = altaz(anchor)
            near = [j for j in state.neighbours(state.ra[anchor], state.dec[anchor], NEIGHBOUR_RADIUS_DEG) if j in visible]
            near_values = {j: achievable(j) for j in near}
            for fiber in fibers:
                d_north, d_east = self.grid.fiber_center(fiber)
                c_alt, c_az = shift_altaz(a_alt, a_az, -d_north, -d_east)
                if not (state.min_alt + 1.5 <= c_alt <= 89.0):
                    continue
                c_alt = round(c_alt, 4)
                c_az = round(c_az, 4) % 360.0
                chosen: dict[int, tuple[float, int, float]] = {}  # fiber -> (score, j, margin)
                for j, v in near_values.items():
                    if v <= 0.0:
                        continue
                    alt, az = altaz(j)
                    offsets = tangent_offsets(alt, az, c_alt, c_az)
                    if offsets is None:
                        continue
                    fib, margin = self.grid.classify(*offsets)
                    if fib is None:
                        continue
                    score = v * (1.0 if margin >= EDGE_MARGIN_DEG * (1 + 1.5 * state.misses[j]) else 0.4)
                    existing = chosen.get(fib)
                    if existing is None or score > existing[0]:
                        chosen[fib] = (score, j, margin)
                if not chosen:
                    continue
                total = sum(score for score, _, _ in chosen.values())
                if best is None or total > best[0]:
                    best = (total, c_alt, c_az, chosen)
        if best is None:
            return None
        _, c_alt, c_az, chosen = best
        return self._finish_plan(now, lst, c_alt, c_az, chosen, seconds_left, moon, altaz, hours, night_index)

    def _finish_plan(self, now, lst, c_alt, c_az, chosen, seconds_left, moon, altaz, hours, night_index):
        state = self.state
        scoring = state.scoring
        c_ra, c_dec = altaz_to_radec(c_alt, c_az, lst, state.lat)
        c_hmax = max_hour_angle_deg(c_dec, state.lat, state.min_alt + 0.3)
        c_ha = wrap180(lst - c_ra)

        info: dict[int, dict] = {}
        for fiber, (_, j, _margin) in chosen.items():
            alt, az = altaz(j)
            lunar = lunar_factor(moon, state.ra[j], state.dec[j], scoring.lunar_model)
            model = scoring.quality_model(alt, lunar) or 0.0
            ha = wrap180(lst - state.ra[j])
            up = (state.hmax[j] - ha) / SIDEREAL_DEG_PER_SECOND if state.hmax[j] < 180 else 1e9
            k = (state.flux[j] * model * state.scale * PLAN_FACTOR_SAFETY) / scoring.f0t0
            info[fiber] = {"i": j, "alt": alt, "az": az, "model": model, "up": up, "k": k}
        center_up = (c_hmax - c_ha) / SIDEREAL_DEG_PER_SECOND if c_hmax < 180 else 1e9

        # 只有完整落在请求窗口内的曝光才计入请求完成：时长不得超过最早的相关截止
        deadline_cap = None
        for item in info.values():
            entry = self._request_view.get(item["i"])
            if entry is not None:
                cap = (entry[2] - now).total_seconds()
                deadline_cap = cap if deadline_cap is None else min(deadline_cap, cap)
        if deadline_cap is not None and deadline_cap < state.min_exposure:
            deadline_cap = None

        # 候选时长 = 固定档位（×LLM 时长调节）+ 临界曝光（不×调节：物理达标线不打折）。
        # 临界档 = 刚好让每个指派目标跨 required 门槛 / 请求门槛 / 饱和 g=1 的秒数，向上对齐 30s。
        # 云端 A/B 实测四卡全正（均分 +850，required 漏网 64→29）：真实判定按单次曝光
        # max g 计，"刚好跨线"的档位让阶跃收益在 rate 竞争中显形
        durations: set[int] = set()
        for base in DURATIONS:
            d = round((base * state.duration_scale) / 30.0) * 30
            durations.add(int(max(state.min_exposure, min(state.max_exposure, d))))
        for item in info.values():
            if item["k"] <= 0.0:
                continue
            j = item["i"]
            critical = [1.0]
            if state.required[j] and state.factor[j] < scoring.required_threshold:
                critical.append(scoring.required_threshold)
            entry = self._request_view.get(j)
            if entry is not None:
                critical.append(entry[1])
            for g in critical:
                d = int(math.ceil(g / item["k"] / 30.0)) * 30
                durations.add(int(max(state.min_exposure, min(state.max_exposure, d))))

        best = None  # (rate, duration)
        for duration in sorted(durations):
            if duration > seconds_left or duration > center_up:
                continue
            if deadline_cap is not None and duration > deadline_cap:
                continue
            gain = 0.0
            for item in info.values():
                if item["up"] < duration:
                    continue
                reached = min(1.0, item["k"] * duration)
                f = state.factor[item["i"]]
                gain += state.weight[item["i"]] * max(0.0, reached * reached - f * f)
                if state.required[item["i"]] and f < scoring.required_threshold and reached >= 0.5:
                    gain += REQUIRED_BONUS
                entry = self._request_view.get(item["i"])
                if entry is not None and reached >= entry[1]:
                    gain += entry[0]
            rate = gain / duration
            if best is None or rate > best[0]:
                best = (rate, duration)
        if best is None:
            return None
        duration = best[1]
        if best[0] <= 0.0:
            if state.has_recent_sample(hours):
                return None
            fallback = next((d for d in (900, 600, 300)
                             if d <= seconds_left and d <= center_up
                             and (deadline_cap is None or d <= deadline_cap)), None)
            if fallback is None:
                return None
            duration = fallback

        assignments: dict[str, str] = {}
        for fiber, item in info.items():
            if item["up"] >= duration:
                assignments[str(fiber)] = state.ids[item["i"]]
        if not assignments:
            return None

        band_scale = state.scale / 0.95
        votes = {"DARK": 0.0, "BRIGHT": 0.0, "BACKUP": 0.0}
        for fiber, item in info.items():
            if str(fiber) not in assignments:
                continue
            band = scoring.program_band(item["model"] * band_scale)
            votes[band] += state.weight[item["i"]] * min(1.0, item["k"] * duration) + \
                (REQUIRED_BONUS * 0.02 if state.required[item["i"]] else 0.0)
        program, best_score = "BACKUP", float("-inf")
        for name in ("DARK", "BRIGHT", "BACKUP"):
            matched = votes[name] * scoring.program_multipliers.get(name, 1.0)
            mismatched = (votes["DARK"] + votes["BRIGHT"] + votes["BACKUP"] - votes[name]) * scoring.mismatch_multiplier
            score = matched + mismatched
            if score > best_score:
                best_score, program = score, name
        if state.force_program:
            program = state.force_program

        clean = not state.all_sky_notice()
        state.pending.clear()
        for fiber, item in info.items():
            if str(fiber) in assignments:
                state.pending[state.ids[item["i"]]] = PendingPrediction(
                    model=item["model"], band_model=item["model"] / 0.95, alt=item["alt"], az=item["az"],
                    clean=clean and self._direction_factor(item["alt"], item["az"]) >= 1.0,
                )
        state.pending_program = program
        state.pending_duration = duration
        state.pending_night = night_index

        out_alt, out_az = self._corrected_pointing(c_alt, c_az)
        return {
            "action": "observe",
            "pointing": {"alt_deg": out_alt, "az_deg": out_az},
            "assignments": assignments,
            "duration_seconds": duration,
            "program": program,
        }
