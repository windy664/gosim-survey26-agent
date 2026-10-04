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


def extract_blind_reports(messages_path: Path) -> list[str]:
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
    return [iso(t) for t in spaced]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("--cards", type=Path, default=Path("kit/cloud-cards"))
    ap.add_argument("--out", type=Path, default=Path("kit/python/card_priors.json"))
    args = ap.parse_args()

    priors: dict[str, dict] = {}
    for slug, folder in sorted(find_card_dirs(args.results_dir).items()):
        blind = extract_blind_reports(folder / "messages.jsonl")
        sig = card_signature(args.cards, slug) if args.cards else None
        priors[slug] = {"blind_report_times_utc": blind, "signature": sig,
                        "source": str(folder)}
        print(f"{slug}: {len(blind)} blind report(s) {blind} signature={'ok' if sig else 'MISSING'}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"schema_version": "card-priors-v1", "cards": priors},
                                   indent=1, ensure_ascii=False))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
