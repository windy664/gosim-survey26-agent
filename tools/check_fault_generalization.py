#!/usr/bin/env python3
"""Controlled fault/weather experiments, using public practice geometry and the stock engine.

No online card truth is used. All scenarios are fixed before running either policy.
The generated truth is private to the test runner, never passed as an agent file.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import random
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CASES = ['healthy', 'cold_start', 'late_fault', 'repeat_fault', 'announced_weather', 'hidden_weather', 'earthquake']


def read_csv(path):
    with path.open() as f:
        r = csv.DictReader(f)
        return r.fieldnames, list(r)


def write_csv(path, fields, rows):
    with path.open('w') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def make_card(source, dest, case, seed):
    shutil.copytree(source, dest, dirs_exist_ok=True)
    fields, weather = read_csv(dest / 'truth/v4_weather_truth.csv')
    _, nights = read_csv(dest / 'public/v4_night_calendar.csv')
    starts = [dt(n['observing_start_utc']) for n in nights]
    end = dt(nights[-1]['observing_end_utc'])
    spans = []
    if case == 'cold_start': spans = [(starts[0], end)]
    if case == 'late_fault': spans = [(starts[3], end)]
    if case == 'repeat_fault': spans = [(starts[0], starts[3]), (starts[4], end)]
    event_fields, _ = read_csv(dest / 'truth/v4_events.csv')
    events = []
    multiplier = 0.04 if seed % 2 else 0.08
    for i, (start, stop) in enumerate(spans):
        row = dict.fromkeys(event_fields, '')
        row.update(event_id=f'synthetic-fault-{i}', event_type='instrument_fault',
                   actual_start_utc=iso(start), actual_end_utc=iso(stop), scope_type='ALL',
                   severity='1', force_close='false', zero_score='false', seeing_multiplier='1',
                   transparency_multiplier='1', sky_quality_multiplier='1',
                   instrument_efficiency_multiplier=str(multiplier))
        events.append(row)
    write_csv(dest / 'truth/v4_events.csv', event_fields, events)
    quake_fields, _ = read_csv(dest / 'truth/v4_earthquake_effects.csv')
    write_csv(dest / 'truth/v4_earthquake_effects.csv', quake_fields, [])
    rng = random.Random(seed)
    bulletins = []
    for row in weather:
        now = dt(row['timestamp_utc'])
        bad_weather = case in {'announced_weather', 'hidden_weather'} and now < starts[4]
        quake = case == 'earthquake' and now < starts[4]
        faulty = any(start <= now < stop for start, stop in spans)
        row.update(is_observable='true', seeing_arcsec='1.2',
                   transparency=str((0.04 if bad_weather else 0.9) * rng.uniform(0.97, 1.03)),
                   sky_quality='0.9', instrument_efficiency=str(0.95 * (multiplier if faulty or quake else 1.0)))
        notices = []
        if bad_weather and case == 'announced_weather': notices = [{'event_kind': 'haze', 'direction': 'ALL'}]
        if quake: notices = [{'event_kind': 'earthquake', 'direction': 'ALL'}]
        bulletins.append(dict(record_type='bulletin', initial=not bulletins, slot_id=row['slot_id'],
                              night_id=row['night_id'], issued_at_utc=row['timestamp_utc'], notices=notices))
    write_csv(dest / 'truth/v4_weather_truth.csv', fields, weather)
    (dest / 'public/v4_bulletins.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in bulletins))
    (dest / 'public/v4_forecasts.jsonl').write_text('')
    (dest / 'truth/v4_observation_requests.jsonl').write_text('')
    config_path = dest / 'config/v4_scenario.json'
    config = json.loads(config_path.read_text())
    config['name'] = f'synthetic-{case}-{seed}'
    config['task_card'] = dict(card_id=config['name'], scenario_slug=config['name'], phase='local-synthetic')
    config['stress'] = {'enabled': False}
    config_path.write_text(json.dumps(config, indent=2) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--agent', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--seed', type=int, default=41)
    ap.add_argument('--card', type=Path, default=ROOT / 'kit/local-cards/L1-short7')
    args = ap.parse_args()
    args.out = args.out.resolve()
    jobs = []
    for case in CASES:
        card = args.out / 'cards' / case
        make_card(args.card.resolve(), card, case, args.seed)
        for flag in ['0', '1']:
            jobs.append((case, flag, card))

    def run(job):
        case, flag, card = job
        out = args.out / ('independent' if flag == '1' else 'baseline') / case
        out.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PRO_MODEL_DISABLED='1', PRO_INTEL='0', PRO_FIXED_LEVEL='2',
                   PRO_SCALE_INDEPENDENT=flag, PRO_LAMBDA_FRAC='0.45', PRO_POOL='900', PRO_REFINE_ROUNDS='6')
        with (out / 'runner.log').open('w') as log:
            p = subprocess.run(['python3', str(ROOT / 'kit/runner/run_local.py'), '--card', str(card),
                '--agent', str(args.agent.resolve()), '--agent-cwd', str(ROOT / 'kit/rust-pro'),
                '--inherit-env', '--out', str(out), '--quiet'], env=env, stdout=log, stderr=log, timeout=1000)
        report = json.loads((out / 'score_report.json').read_text())
        log = (out / 'agent.log').read_text()
        result = dict(case=case, independent=flag == '1', exit=p.returncode, score=report['total'],
                      termination=report['termination'], correct=log.count('report correct'),
                      false=log.count('report false'), components=report['components'])
        print(json.dumps(result), flush=True)
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, jobs))
    (args.out / 'summary.json').write_text(json.dumps(results, indent=2) + '\n')
    return 0 if all(r['exit'] == 0 for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
