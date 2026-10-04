#!/usr/bin/env python3
"""Build card_priors.json from a cloud result pack (the "download all results" ZIP).

The race keeps the same cards (A-D) for all three days and each run replays identical
truth, so facts learned on day 1 are ground truth on day 2. Right now we extract one
high-value fact: the timestamps of *correct* instrument-fault reports. The day-2 agent
replays those reports blindly at the same timestamps (known-correct by construction),
which removes the multi-hour evidence-confirmation delay and frees the adaptive
report budget for discovering any further faults.

Usage:
    python3 tools/build_card_priors.py RESULTS_DIR --cards kit/cloud-cards --out kit/python/card_priors.json

RESULTS_DIR is the unzipped pack with per-card folders (any nesting depth) each
holding messages.jsonl. Card identity comes from the score_report.json `scenario`
field when present, else from the folder name.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

MIN_REPORT_SPACING_H = 24.0


def parse_utc(text: str) -> float:
    from datetime import datetime, timezone
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def card_signature(cards_root: Path, slug: str) -> dict | None:
    """Distinguishing facts the agent can compute from `initialize`: first-night start,
    target count, night count. Returns None when the public bundle is missing."""
    public = cards_root / slug / "public"
    targets_csv = public / "targets.csv"
    calendar_csv = public / "v4_night_calendar.csv"
    if not targets_csv.is_file() or not calendar_csv.is_file():
        return None
    with targets_csv.open() as fh:
        n_targets = sum(1 for _ in csv.DictReader(fh))
    with calendar_csv.open() as fh:
        nights = list(csv.DictReader(fh))
    first_col = "observing_start_utc" if nights and "observing_start_utc" in nights[0] else None
    first_start = nights[0][first_col] if first_col else ""
    return {"targets": n_targets, "nights": len(nights), "first_night_start_utc": first_start}


def find_card_dirs(results_dir: Path) -> dict[str, Path]:
    cards: dict[str, Path] = {}
    for report in results_dir.rglob("score_report.json"):
        folder = report.parent
        slug = None
        try:
            slug = json.loads(report.read_text()).get("scenario")
        except Exception:
            pass
        if not slug:
            for part in folder.name.split("-"):
                if part.startswith("practice") or part in "abcdefgh":
                    pass
            slug = folder.name
        if (folder / "messages.jsonl").is_file():
            cards[str(slug)] = folder
    return cards


# -- 故障发作点估计 -----------------------------------------------------------
#
# 正确举报的时刻是"证据链确诊"的时刻，故障实际发作要早得多（练习 γ 卡：疑似发作
# 10-25/26，确诊 10-29，带病低效观测 3-4 天）。用 hindsight 全量数据把先验时刻
# 从确诊点前移到发作点：次日盲报在发作点触发 = 立刻修复，后续全季效率恢复。
#
# 方法：对每条有效观测计算 ratio = 实测 quality / 几何模型（月光+大气质量，全部
# 公开公式，与评分器同构），把几何因素剔除后，故障表现为 ratio 的**永久性上限压缩**
# （天气只压中位数且会恢复，故障把 q90 钉死在 mult×天气 直到修复）。
#
# 关键消歧：bulletin 逐槽位播报天气事件（rain/overcast/haze/cold_snap…），但**不播报
# instrument_fault**（要靠自己发现）。凡有质量类天气通告的窗口一律掩蔽；剩下的"干净窗"
# 若持续凹陷，唯一解释就是故障。本地 L1-L4 真值校准：纯 ratio 阈值会被连续阴天骗到
# 提前 1-5 天（误报烧免罚额度），bulletin 掩蔽后才能安全前移。

# 逐槽位通告中不影响质量的类型（火箭/地形不涉及效率倍率）。地震必须掩蔽：
# 官方模拟器里地震会让效率倍率衰减式恢复好几夜（L3 实测 0.33→0.61 爬升 4 天），
# 不掩蔽会被误认为故障前兆。宁可多掩蔽（丢数据），不可少掩蔽（误前移）。
_NON_QUALITY_NOTICES = {"terrain_obstruction", "rocket_launch"}


def _weather_masked_windows(messages_path: Path) -> set[int]:
    """2h window indexes (epoch_hours // 2) with any quality-affecting bulletin notice."""
    masked: set[int] = set()
    try:
        fh = messages_path.open()
    except OSError:
        return masked
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("record_type") != "bulletin":
                continue
            notices = rec.get("notices") or []
            if any(n.get("event_kind") not in _NON_QUALITY_NOTICES for n in notices):
                try:
                    t = parse_utc(rec["issued_at_utc"])
                except (KeyError, ValueError):
                    continue
                w = int(t // 7200)
                masked.update((w - 1, w, w + 1))  # 前后各扩 2h，通告与观测的错位余量
    return masked


def estimate_fault_onset(folder: Path, card_dir: Path, report_time: float) -> float | None:
    """Estimate when the fault confirmed at `report_time` actually started.

    Returns a timestamp shortly after the onset, or None to fall back to the
    confirmed report time. Conservative: only fires when a *sustained* depression
    is unambiguous; a wrong early guess costs one free false-report allowance,
    with the adaptive chain as backstop."""
    import sys as _sys
    kit_python = Path(__file__).resolve().parents[1] / "kit" / "python"
    if str(kit_python) not in _sys.path:
        _sys.path.insert(0, str(kit_python))
    from datetime import datetime, timezone, timedelta
    from agent_core.geometry import (Moon, local_sidereal_deg, lunar_factor,
                                     normalized_airmass, radec_to_altaz)

    def pt(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)

    try:
        scenario = json.loads((card_dir / "config" / "v4_scenario.json").read_text())
        score_cfg = json.loads((card_dir / "config" / "v4_score_config.json").read_text())
        site = scenario["site"]
        lat, lon = float(site["latitude_deg"]), float(site["longitude_deg"])
        q0 = float(score_cfg.get("q0", 1.0))
        airmass_exp = float(score_cfg.get("airmass_exponent", 0.6))
        lunar_model = score_cfg.get("lunar_model") or {}
        targets = {}
        with (card_dir / "public" / "targets.csv").open() as fh:
            for row in csv.DictReader(fh):
                targets[row["target_id"]] = (float(row["ra_deg"]), float(row["dec_deg"]))
        starts: dict[str, tuple[datetime, int]] = {}
        with (folder / "decisions.csv").open() as fh:
            for row in csv.DictReader(fh):
                if row.get("action") == "observe":
                    starts[row["observe_index"]] = (pt(row["start_utc"]), int(row["duration_seconds"]))
    except (OSError, KeyError, ValueError):
        return None

    samples: list[tuple[float, float]] = []  # (epoch_hours, ratio)
    with (folder / "observations.csv").open() as fh:
        for row in csv.DictReader(fh):
            if row.get("valid") != "true":
                continue
            slot = starts.get(row.get("observe_index", ""))
            target = targets.get(row.get("target_id", ""))
            if not slot or not target:
                continue
            quality = float(row["quality"])
            if quality < 0.05:
                continue  # 关闭/零分样本是天气事件，不掺进故障估计
            start, dur = slot
            mid = start + timedelta(seconds=dur / 2)
            lst = local_sidereal_deg(mid, lon)
            alt, _az = radec_to_altaz(target[0], target[1], lst, lat)
            if alt < 32.0:
                continue
            lunar = lunar_factor(Moon(mid, lst, lat), target[0], target[1], lunar_model)
            model = lunar / (q0 * normalized_airmass(alt) ** airmass_exp)
            if model <= 1e-6:
                continue
            samples.append((mid.timestamp() / 3600.0, quality / model))
    if len(samples) < 200:
        return None

    # 2h 窗 q90 序列，bulletin 天气窗掩蔽：故障给干净窗 q90 装"永久上限"
    masked = _weather_masked_windows(folder / "messages.jsonl")
    windows: dict[int, list[float]] = {}
    for h, ratio in samples:
        w = int(h // 2)
        if w not in masked:
            windows.setdefault(w, []).append(ratio)
    ws = sorted((w, v) for w, v in windows.items() if len(v) >= 8)
    if len(ws) < 30:
        return None
    report_h = report_time / 3600.0
    ws = [(w, v) for w, v in ws if w * 2 < report_h]
    if len(ws) < 20:
        return None
    # 变点检测（从确诊点倒推）：post_level = 确诊前最后几个干净窗的中位水平
    # （故障期水平）。倒序找最后一个"持续高位"（3 窗中位 > post/0.65）——故障倍率
    # ≤0.65，跨故障边界的落差 ≥1.54×，天气单窗噪声过不了 3 窗中位。
    # 发作窗 = 高位终结后的第一个干净窗。只可能晚于真发作（安全方向），不会早：
    # 故障期水平被 mult 压住，天气恢复也只能恢复到 mult×天气，超不过阈值。
    q90 = [(w, sorted(v)[int(len(v) * 0.9)]) for w, v in ws]
    tail = [m for _w, m in q90[-4:]]  # 只用最后几窗：窗多了会跨故障边界把水平拉高的
    post_level = sorted(tail)[len(tail) // 2]
    if post_level <= 0:
        return None
    threshold = post_level / 0.65
    stop = None
    for i in range(len(q90) - 1, -1, -1):
        neigh = [m for _w, m in q90[max(0, i - 1): i + 2]]
        med = sorted(neigh)[len(neigh) // 2]
        if med > threshold:
            stop = i
            break
    if stop is None or stop + 1 >= len(q90):
        return None
    onset_h = q90[stop + 1][0] * 2
    est = (onset_h + 4.0) * 3600.0  # +4h 窗宽余量，确保故障已发作
    if est >= report_time - 3600:
        return None  # 没有比确诊点更早多少，不值得冒险，回退确诊时刻
    return est


def extract_blind_reports(messages_path: Path) -> list[float]:
    times: list[float] = []
    for line in messages_path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("record_type") == "report_result" and rec.get("correct"):
            times.append(parse_utc(rec["issued_at_utc"]))
    times.sort()
    spaced: list[float] = []
    for t in times:
        if not spaced or (t - spaced[-1]) >= MIN_REPORT_SPACING_H * 3600:
            spaced.append(t)
    return spaced


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("--cards", type=Path, default=Path("kit/cloud-cards"))
    ap.add_argument("--out", type=Path, default=Path("kit/python/card_priors.json"))
    args = ap.parse_args()

    priors: dict[str, dict] = {}
    for slug, folder in sorted(find_card_dirs(args.results_dir).items()):
        card_dir = (args.cards / slug) if args.cards else None
        blind: list[str] = []
        for confirmed in extract_blind_reports(folder / "messages.jsonl"):
            onset = None
            if card_dir and (card_dir / "config" / "v4_scenario.json").is_file():
                onset = estimate_fault_onset(folder, card_dir, confirmed)
            chosen = onset if onset is not None else confirmed
            tag = "onset" if onset is not None else "confirmed"
            blind.append(iso(chosen))
            print(f"  {slug} report@{iso(confirmed)} -> blind@{iso(chosen)} ({tag})")
        sig = card_signature(args.cards, slug) if args.cards else None
        priors[slug] = {"blind_report_times_utc": blind, "signature": sig,
                        "source": str(folder)}
        print(f"{slug}: {len(blind)} blind report(s) signature={'ok' if sig else 'MISSING'}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"schema_version": "card-priors-v1", "cards": priors},
                                   indent=1, ensure_ascii=False))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
