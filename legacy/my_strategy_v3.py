"""优化策略 — 基于 reference_strategy.py 的核心思路。

关键规则：
1. REQUIRED 天区告急（剩余窗口 ≤2）→ 最高优先级，漏一块 -1000
2. 覆盖均匀性 → 正式比赛占总分约 20%，优先拍完成少的分区
3. 不要等待 → 实测等待策略平均掉 1700 分
"""

from datetime import datetime

MISS_REQUIRED = 1000.0
FLEXIBLE_QUOTA = 4
REQUEST_REWARD = 140.0
REQUEST_MISS = 190.0
LAST_CHANCES = 2


def _utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def remaining_chances(snapshot, now):
    counts = {}
    weekly = (snapshot.get("weekly") or {}).get("tile_windows") or []
    tonight = (snapshot.get("night_start") or {}).get("tile_windows") or []
    for window in list(weekly) + list(tonight):
        end = _utc(window.get("window_end_utc"))
        if end is not None and end <= now:
            continue
        tile_id = window.get("tile_id")
        if tile_id:
            counts[tile_id] = counts.get(tile_id, 0) + 1
    return counts


def region_shortfall(snapshot):
    done = (snapshot.get("progress") or {}).get("flexible_completed_by_region") or {}
    return {region: max(0, FLEXIBLE_QUOTA - int(count or 0)) for region, count in done.items()}


def coverage_weight(snapshot):
    for key in ("score_config", "scoring", "competition"):
        block = snapshot.get(key)
        if isinstance(block, dict) and "coverage_bonus_weight" in block:
            try:
                return float(block["coverage_bonus_weight"])
            except (TypeError, ValueError):
                return 0.0
    try:
        return float(snapshot.get("coverage_bonus_weight") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _evenness_gain(done_by_region, region, n_regions):
    if region is None:
        return 0.0
    counts = list(done_by_region.values())
    total = sum(counts)
    squares = sum(v * v for v in counts)
    if total <= 0:
        return 0.0
    before = (total * total) / (n_regions * squares) if squares else 0.0
    x = done_by_region.get(region, 0)
    after = ((total + 1) ** 2) / (n_regions * (squares + 2 * x + 1))
    return after - before


def choose_action(candidates, snapshot, memory):
    if not candidates:
        return None

    now = _utc((snapshot.get("cursor") or {}).get("timestamp_utc")) or 0.0
    chances = remaining_chances(snapshot, now)
    shortfall = region_shortfall(snapshot)

    # ① REQUIRED 天区告急 — 漏一块 -1000，最高优先级
    at_risk = [
        (chances.get(c.get("tile_id"), 99), rank, c)
        for rank, c in enumerate(candidates)
        if (c.get("scheduling_class") or "").upper() == "REQUIRED"
        and chances.get(c.get("tile_id"), 99) <= LAST_CHANCES
    ]
    if at_risk:
        at_risk.sort(key=lambda item: (item[0], item[1]))
        chosen = at_risk[0][2]
        chosen["reason"] = f"required tile with only {at_risk[0][0]} window(s) left"
        return chosen

    # ② 覆盖均匀性 — 正式比赛占总分约 20%
    weight = coverage_weight(snapshot)
    if weight > 0.0:
        done_by_region = memory.setdefault("_coverage", {})
        science_so_far = float(memory.get("_science", 0.0))
        n_regions = max(1, len(done_by_region) or 8)
        best = None
        best_value = float("-inf")
        for candidate in candidates:
            seconds = max(1.0, float(candidate.get("nominal_exptime_seconds") or 900))
            value = float(candidate.get("estimated_total_gain") or 0.0)
            value += weight * science_so_far * _evenness_gain(done_by_region, candidate.get("region_id"), n_regions)
            value /= seconds
            if value > best_value:
                best_value, best = value, candidate
        if best is not None:
            region = best.get("region_id")
            done_by_region[region] = done_by_region.get(region, 0) + 1
            memory["_science"] = science_so_far + float(best.get("estimated_science_score") or 0.0)
            best["reason"] = "immediate gain plus coverage evenness"
            return best

    # ③ 默认：信任平台排序（最高每秒收益）
    best = candidates[0]
    best["reason"] = "platform ranking: highest estimated gain per second"
    return best
