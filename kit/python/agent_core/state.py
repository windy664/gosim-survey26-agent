"""Survey state: the target catalogue, learned sky-quality scale, and per-target
progress. Built once from `initialize`, then updated from every `decision_request`'s
messages and `last_result`. Holds no hidden data -- only what the public protocol
hands us, plus what we infer from our own hits (never from a file).

Mirrors the structure-of-parallel-arrays design (ids/ra/dec/flux/weight/required/...,
all indexed by the same integer i) used by this project's companion TypeScript example,
so both examples solve the same problem the same way and can be compared directly.
"""
from __future__ import annotations

import bisect
import math
from collections import deque
from typing import NamedTuple, Optional

from .geometry import FiberGrid, max_hour_angle_deg, parse_utc, wrap180
from .scoring import ScoringModel

ALT_MARGIN_DEG = 0.6
SKY_MEMORY_HOURS = 2.0
RECENT_SAMPLES = 60
EARLIER_SAMPLES = 60
SIDEREAL_DEG_PER_SECOND = 360.98564736629 / 86400.0


class PendingPrediction(NamedTuple):
    model: float          # lunar/airmass quality model used at planning time
    band_model: float      # model / 0.95, used for program-band back-estimation
    alt: float
    az: float
    clean: bool            # true when no all-sky notice / directional block applied at plan time


class FaultEvidence(NamedTuple):
    recent_median: float
    earlier_median: float
    drop: float
    recent_samples: int
    recent_nights: int
    earlier_samples: int
    dark_checks: int
    dark_matched: int


def _mod(a: float, n: float) -> float:
    m = a % n
    return m + n if m < 0 else m


class SurveyState:
    def __init__(self, init_payload: dict):
        site = init_payload["site"]
        survey = init_payload["survey"]
        instrument = init_payload["instrument"]
        limits = init_payload.get("limits", {})

        self.lat = float(site["latitude_deg"])
        self.lon = float(site["longitude_deg"])
        self.min_alt = float(site.get("minimum_altitude_deg", 30.0))
        self.sun_altitude_limit_deg = float(site.get("sun_altitude_limit_deg", -18.0))

        self.survey_start = parse_utc(survey["start_utc"])
        self.survey_end = parse_utc(survey["end_utc"])
        self.slot_seconds = int(survey.get("slot_seconds", 900))
        self.nights = [(parse_utc(n["observing_start_utc"]), parse_utc(n["observing_end_utc"]))
                       for n in survey.get("nights", [])]

        self.fiber_grid = FiberGrid(instrument)
        exposure = instrument.get("exposure", {})
        self.min_exposure = int(exposure.get("min_duration_seconds", 60))
        self.max_exposure = int(exposure.get("max_duration_seconds", 3600))

        self.scoring = ScoringModel(init_payload.get("scoring", {}), site)

        reporting = init_payload.get("scoring", {}).get("reporting", {})
        self.max_consecutive_reports = int(reporting.get("max_consecutive_reports", limits.get("max_consecutive_reports", 32)))
        self.false_report_free_allowance = int(reporting.get("false_report_free_allowance", 0))
        self.response_max_bytes = int(limits.get("response_max_bytes", 524288))

        # Parallel arrays, one slot per target, in catalogue order.
        self.ids: list[str] = []
        self.ra: list[float] = []
        self.dec: list[float] = []
        self.flux: list[float] = []
        self.weight: list[float] = []
        self.required: list[bool] = []
        self.index_of: dict[str, int] = {}

        columns = init_payload.get("targets", {}).get("columns", [])
        col = {name: idx for idx, name in enumerate(columns)}
        for row in init_payload.get("targets", {}).get("rows", []):
            target_id = str(row[col["target_id"]])
            self.index_of[target_id] = len(self.ids)
            self.ids.append(target_id)
            self.ra.append(float(row[col["ra_deg"]]))
            self.dec.append(float(row[col["dec_deg"]]))
            self.flux.append(float(row[col["feature_flux"]]))
            self.weight.append(float(row[col["science_weight"]]))
            self.required.append(bool(row[col["required"]]))

        n = len(self.ids)
        self.hmax = [max_hour_angle_deg(self.dec[i], self.lat, self.min_alt + ALT_MARGIN_DEG) for i in range(n)]
        self.factor = [0.0] * n
        self.misses = [0] * n
        self.attempts = [0] * n
        self.active = [i for i in range(n) if self.hmax[i] > 0.0]

        self._cells: dict[int, list[tuple[float, int]]] = {}
        self._build_index()
        self.first_night, self.last_night = self._build_windows()

        self.scale = 1.0
        self.prior_scale = 1.0
        self._samples: deque = deque(maxlen=24)           # (hours, ratio)
        self._all_ratios: deque = deque(maxlen=400)        # ratio
        self.clean_history: list[tuple[float, int, float]] = []  # (hours, night, ratio)
        self.pending_night = -1
        self._band_checks: deque = deque(maxlen=60)        # (program, matched, model)
        self.force_program: Optional[str] = None
        self.pending: dict[str, PendingPrediction] = {}
        self.pending_program = "BACKUP"
        self.pending_duration = 0
        self.blocked: list[tuple[float, float]] = []       # (az, alt) where a hit scored zero
        self.notices: set[str] = set()                      # "kind|direction"
        self.terrain: set[str] = set()
        self.extra_avoid: set[str] = set()
        self.duration_scale = 1.0
        self.fast_level = 0
        # 限时请求表：request_id -> {targets(下标集合), minimum, threshold, reward,
        # issued, deadline, completed(下标集合)}；进度以 payload.active_requests 快照为准
        self.requests: dict[str, dict] = {}

    # -- spatial index -------------------------------------------------------

    def _build_index(self) -> None:
        for i in self.active:
            key = math.floor(self.dec[i])
            self._cells.setdefault(key, []).append((self.ra[i], i))
        for band in self._cells.values():
            band.sort(key=lambda pair: pair[0])

    def neighbours(self, ra: float, dec: float, radius: float):
        """Indices within `radius` degrees of (ra, dec), using the 1-degree declination-band index."""
        found: list[int] = []
        cos_dec = max(0.05, math.cos(math.radians(min(89.0, abs(dec) + radius))))
        width = radius / cos_dec
        lo_key, hi_key = math.floor(dec - radius), math.floor(dec + radius)
        for key in range(lo_key, hi_key + 1):
            band = self._cells.get(key)
            if not band:
                continue
            spans: list[tuple[float, float]]
            lo, hi = ra - width, ra + width
            if lo < 0:
                spans = [(0.0, hi), (lo + 360.0, 360.0)]
            elif hi >= 360:
                spans = [(lo, 360.0), (0.0, hi - 360.0)]
            else:
                spans = [(lo, hi)]
            keys = [r for r, _ in band]
            for low, high in spans:
                start = bisect.bisect_left(keys, low)
                end = bisect.bisect_right(keys, high)
                for k in range(start, end):
                    found.append(band[k][1])
        return found

    def _build_windows(self):
        """First/last night index on which each target has >=20 minutes above the limit."""
        need = 20 * 60 * SIDEREAL_DEG_PER_SECOND
        spans = []
        for start, end in self.nights:
            from .geometry import local_sidereal_deg
            l0 = local_sidereal_deg(start, self.lon)
            span = (end - start).total_seconds() * SIDEREAL_DEG_PER_SECOND
            spans.append((l0, span))
        n = len(self.ra)
        first_night = [len(self.nights)] * n
        last_night = [-1] * n
        for i in self.active:
            h = self.hmax[i]
            for k, (l0, span) in enumerate(spans):
                if h >= 180.0:
                    overlap = span
                else:
                    a = _mod(self.ra[i] - h - l0, 360.0)
                    overlap = max(0.0, min(span, a + 2 * h) - a) + max(0.0, min(span, a - 360.0 + 2 * h))
                if overlap >= need:
                    if first_night[i] > k:
                        first_night[i] = k
                    last_night[i] = k
        return first_night, last_night

    # -- messages and results -------------------------------------------------

    def on_messages(self, messages: list[dict], latest_bulletin: Optional[dict]) -> None:
        for message in messages:
            if message.get("record_type") == "bulletin" and message.get("initial"):
                for notice in message.get("notices", []):
                    if notice.get("event_kind") == "terrain_obstruction":
                        self.terrain.add(notice.get("direction"))
            elif message.get("record_type") == "state_resync":
                self._resync(message.get("observed_target_ids", []), message.get("best_scores", []),
                             message.get("observation_requests") or [])
            elif message.get("record_type") == "observation_request":
                self._register_request(message)
            elif message.get("record_type") == "observation_request_result":
                request = self.requests.get(str(message.get("request_id")))
                if request is not None:
                    request["completed"] = {self.index_of[t] for t in message.get("completed_target_ids", [])
                                            if t in self.index_of}
        notices = (latest_bulletin or {}).get("notices", [])
        # terrain_obstruction 进 terrain（永久）；earthquake 只是余震预警，事件本身只有
        # 几分钟且质量系数中性——若留在 notices 里会让此后所有样本永远 "unclean"，故障证据冻结
        self.notices = {f"{n.get('event_kind')}|{n.get('direction')}" for n in notices
                        if n.get("event_kind") not in ("terrain_obstruction", "earthquake")}

    # -- observation requests (限时请求) -----------------------------------------

    def _register_request(self, record: dict) -> None:
        try:
            request_id = str(record["request_id"])
            targets = {self.index_of[t] for t in record.get("target_ids", []) if t in self.index_of}
            if not targets:
                return
            self.requests[request_id] = {
                "targets": targets,
                "minimum": int(record.get("minimum_completed", 1)),
                "threshold": float(record.get("completion_factor_threshold", self.scoring.required_threshold)),
                "reward": float(record.get("completion_reward", 0.0)),
                "issued": parse_utc(record["issued_at_utc"]),
                "deadline": parse_utc(record["deadline_utc"]),
                "completed": set(),
            }
        except (KeyError, TypeError, ValueError):
            return

    def update_requests(self, active: list, now) -> None:
        """Each decision: refresh progress from payload.active_requests (the engine's own
        in-window ledger view) and drop requests past their deadline."""
        for snapshot in active:
            request_id = str(snapshot.get("request_id"))
            if request_id not in self.requests:
                self._register_request(snapshot)
            request = self.requests.get(request_id)
            if request is not None:
                request["completed"] = {self.index_of[t] for t in snapshot.get("completed_target_ids", [])
                                        if t in self.index_of}
        for request_id in [rid for rid, req in self.requests.items() if now >= req["deadline"]]:
            del self.requests[request_id]

    def request_view(self, now) -> dict:
        """target index -> [bonus, threshold, deadline] for active requests still short of
        minimum_completed. bonus ≈ reward/minimum, scaled up as the window tightens."""
        view: dict[int, list] = {}
        for req in self.requests.values():
            remaining = req["minimum"] - len(req["completed"])
            window = (req["deadline"] - now).total_seconds()
            if remaining <= 0 or window <= 0:
                continue
            slack = window / max(1.0, remaining * 900.0)
            bonus = (req["reward"] / max(1, req["minimum"])) * min(10.0, 4.0 + 12.0 / max(1.0, slack))
            for i in req["targets"] - req["completed"]:
                entry = view.get(i)
                if entry is None:
                    view[i] = [bonus, req["threshold"], req["deadline"]]
                else:
                    entry[0] = max(entry[0], bonus)
                    entry[2] = min(entry[2], req["deadline"])
        return view

    def _resync(self, observed_ids: list, best_scores, requests: list) -> None:
        best: dict[str, float] = {}
        if best_scores and isinstance(best_scores[0], dict):
            for row in best_scores:
                best[row.get("target_id")] = float(row.get("best_score", 0.0))
        else:
            for target_id, score in zip(observed_ids, best_scores):
                best[target_id] = float(score)
        top_multiplier = max(self.scoring.program_multipliers.values()) if self.scoring.program_multipliers else 1.2
        for i in range(len(self.ids)):
            score = best.get(self.ids[i], 0.0)
            self.factor[i] = min(1.0, score / (self.weight[i] * top_multiplier)) if score > 0 and self.weight[i] > 0 else 0.0
        self.active = [i for i in range(len(self.ids)) if self.hmax[i] > 0.0]
        # 数据丢失后历史成败记录一并作废，目标才能被重新规划
        self.misses = [0] * len(self.ids)
        self.attempts = [0] * len(self.ids)
        self.pending.clear()
        for snapshot in requests:
            request_id = str(snapshot.get("request_id"))
            if request_id not in self.requests:
                self._register_request(snapshot)
            request = self.requests.get(request_id)
            if request is not None:
                request["completed"] = {self.index_of[t] for t in snapshot.get("completed_target_ids", [])
                                        if t in self.index_of}

    def site_closed(self) -> bool:
        for key in self.notices:
            kind, _, direction = key.partition("|")
            if kind in ("rain", "storm") and direction == "ALL":
                return True
        return False

    def all_sky_notice(self) -> bool:
        return any(key.partition("|")[2] == "ALL" for key in self.notices)

    def on_result(self, last_result: Optional[dict], hours: float) -> None:
        if not last_result or last_result.get("action") != "observe" or not self.pending:
            self.pending.clear()
            return
        hits = {h.get("target_id"): float(h.get("score", 0.0)) for h in last_result.get("hits", [])}
        any_positive = any(score > 0 for score in hits.values())
        scoring = self.scoring
        multipliers = scoring.program_multipliers
        mismatch = scoring.mismatch_multiplier
        declared_multiplier = multipliers.get(self.pending_program, 1.0)
        f0t0 = scoring.f0t0

        for target_id, prediction in self.pending.items():
            i = self.index_of.get(target_id)
            if i is None:
                continue
            if target_id not in hits:
                self.misses[i] += 1
                continue
            score = hits[target_id]
            if score <= 0.0:
                if any_positive:
                    self.blocked.append((prediction.az, prediction.alt))
                continue
            weight = self.weight[i] if self.weight[i] > 0 else 1e-9
            multiplier_seen = score / weight
            if prediction.clean:
                if abs(multiplier_seen - declared_multiplier) < 2e-4:
                    self._band_checks.append((self.pending_program, True, prediction.model))
                elif abs(multiplier_seen - mismatch) < 2e-4:
                    self._band_checks.append((self.pending_program, False, prediction.model))
            factor_if_match = score / (weight * declared_multiplier) if declared_multiplier > 0 else 0.0
            factor_if_miss = score / (weight * mismatch) if mismatch > 0 else 0.0
            ratio_match = (factor_if_match * f0t0) / (self.flux[i] * self.pending_duration * prediction.model) \
                if self.flux[i] > 0 and self.pending_duration > 0 and prediction.model > 0 else 0.0
            band = scoring.program_band(ratio_match * prediction.band_model)
            matched = band == self.pending_program
            estimate = factor_if_match if matched else factor_if_miss
            if self.required[i]:
                # 保守记账：倍率不确定时按已匹配（除数更大、factor 更小）处理，
                # 避免 required 目标被高估后永远不再补观测
                factor = min(factor_if_match, factor_if_miss)
            else:
                factor = estimate
            self.factor[i] = max(self.factor[i], min(1.0, factor))
            if self.required[i]:
                if self.factor[i] < scoring.required_threshold:
                    self.attempts[i] += 1
                else:
                    self.attempts[i] = 0  # 已达标：失败计数不再拖累后续冲高分
            # 天空质量样本始终用最大似然估计，保守记账不污染 scale / 故障判据
            if estimate < 0.97 and self.flux[i] > 0 and self.pending_duration > 0 and prediction.model > 0:
                ratio = (estimate * f0t0) / (self.flux[i] * self.pending_duration * prediction.model)
                self._samples.append((hours, ratio))
                self._all_ratios.append(ratio)
                if prediction.clean:
                    self.clean_history.append((hours, self.pending_night, ratio))
        self.pending.clear()
        self.update_scale(hours)

    def has_recent_sample(self, hours: float) -> bool:
        return any(when >= hours - SKY_MEMORY_HOURS for when, _ in self._samples)

    def update_scale(self, hours: float) -> None:
        if len(self._all_ratios) >= 8:
            ordered = sorted(self._all_ratios)
            self.prior_scale = ordered[len(ordered) // 2]
        recent = sorted(ratio for when, ratio in self._samples if when >= hours - SKY_MEMORY_HOURS)
        self.scale = max(0.05, recent[len(recent) // 2]) if len(recent) >= 4 else self.prior_scale

    # -- fault diagnostics ------------------------------------------------------

    def fault_evidence(self) -> Optional[FaultEvidence]:
        history = self.clean_history
        if len(history) < RECENT_SAMPLES + EARLIER_SAMPLES:
            return None
        recent = history[-RECENT_SAMPLES:]
        if recent[-1][0] - recent[0][0] < 4.0:
            # dense sampling makes RECENT_SAMPLES span too little time: widen "recent"
            # to a trailing 6-hour window so the median still compares now vs before
            cutoff = history[-1][0] - 6.0
            widened = [sample for sample in history if sample[0] >= cutoff]
            if len(widened) >= RECENT_SAMPLES:
                recent = widened
        earlier = history[:len(history) - len(recent)]
        span = recent[-1][0] - recent[0][0]
        nights = len({night for _, night, _ in recent})
        if span < 2.0 or nights < 1 or len(earlier) < EARLIER_SAMPLES // 2:
            return None
        recent_sorted = sorted(r for _, _, r in recent)
        earlier_sorted = sorted(r for _, _, r in earlier)
        recent_median = recent_sorted[len(recent_sorted) // 2]
        earlier_median = earlier_sorted[len(earlier_sorted) // 2]
        dark_line = self.scoring.program_bands["DARK"] * 1.3
        dark = [c for c in list(self._band_checks)[-16:]
                if c[0] == "DARK" and (c[2] * earlier_median) / 0.95 >= dark_line]
        return FaultEvidence(
            recent_median=round(recent_median, 3),
            earlier_median=round(earlier_median, 3),
            drop=round(recent_median / max(1e-9, earlier_median), 3),
            recent_samples=len(recent),
            recent_nights=nights,
            earlier_samples=len(earlier),
            dark_checks=len(dark),
            dark_matched=sum(1 for c in dark if c[1]),
        )

    def forget_quality_history(self) -> None:
        self.clean_history = []
        self._band_checks.clear()
        self._samples.clear()
        self._all_ratios.clear()
        self.prior_scale = 1.0

    # -- night lookup -------------------------------------------------------------

    def current_night(self, now):
        for index, (start, end) in enumerate(self.nights):
            if start <= now < end:
                return index, start, end
        return None

    def next_night_start(self, now):
        for start, _end in self.nights:
            if start > now:
                return start
        return None
