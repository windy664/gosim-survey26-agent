#!/usr/bin/env python3
"""Exercise public instrument variations with the unchanged local evaluation engine.

This checks protocol validity and completion, not competitive score or E-H performance.
The agent receives only normal protocol input; generated truth stays with the runner.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = [(1, 60, 240), (4, 180, 180), (9, 1000, 1400), (25, 60, 3600)]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--agent', type=Path, required=True)
    ap.add_argument('--baseline', type=Path)
    ap.add_argument('--card', type=Path, default=ROOT / 'kit/local-cards/L1-short2')
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    jobs = []
    binaries = [('candidate', args.agent.resolve())]
    if args.baseline:
        binaries.append(('baseline', args.baseline.resolve()))
    for fibers, minimum, maximum in VARIANTS:
        name = f'fibers{fibers}-exposure{minimum}-{maximum}'
        card = args.out / 'cards' / name
        shutil.copytree(args.card, card, dirs_exist_ok=True)
        config_path = card / 'config/v4_fiber_config.json'
        config = json.loads(config_path.read_text())
        config['field']['n_fibers'] = fibers
        config['exposure'].update(min_duration_seconds=minimum, max_duration_seconds=maximum)
        config_path.write_text(json.dumps(config, indent=2) + '\n')
        for label, binary in binaries:
            jobs.append((name, label, binary, card, fibers, minimum, maximum))

    def run(job):
        name, label, binary, card, fibers, minimum, maximum = job
        out = args.out / label / name
        out.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PRO_MODEL_DISABLED='1', PRO_FIXED_LEVEL='2',
                   PRO_INTEL='0', PRO_LAMBDA_FRAC='0.45', PRO_POOL='900', PRO_REFINE_ROUNDS='6')
        command = ['python3', str(ROOT / 'kit/runner/run_local.py'), '--card', str(card),
                   '--agent', str(binary), '--agent-cwd', str(ROOT / 'kit/rust-pro'),
                   '--inherit-env', '--out', str(out), '--quiet']
        with (out / 'runner.log').open('w') as log:
            proc = subprocess.run(command, env=env, stdout=log, stderr=log, timeout=1000)
        report_path = out / 'score_report.json'
        report = json.loads(report_path.read_text()) if report_path.exists() else {}
        termination = report.get('termination', {}).get('reason')
        result = dict(variant=name, agent=label, returncode=proc.returncode,
                      termination=termination, score=report.get('total'),
                      counts=report.get('counts', {}))
        print(json.dumps(result), flush=True)
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, jobs))
    (args.out / 'summary.json').write_text(json.dumps(results, indent=2) + '\n')
    return 0 if all(r['termination'] in {'survey_complete', 'agent_finished'} and r['returncode'] == 0
                    and r['counts'].get('observe_actions', 0) > 0
                    for r in results if r['agent'] == 'candidate') else 1


if __name__ == '__main__':
    raise SystemExit(main())
