#!/usr/bin/env python3
"""Exercise the real agent against a local fake model; no API key/network service needed.

A decode is held across four night boundaries, then returns an invalid schema.
The next two replies succeed. Every note must be delivered in order, including retry.
"""
import argparse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'kit/runner'))
from challenge.v4_workflow import V4Workflow, PROTOCOL_VERSION


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--agent', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    batches = []
    first_started = threading.Event()
    release_first = threading.Event()
    completed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            system = request['messages'][0]['content']
            if 'logbook decoder' in system:
                notes = json.loads(request['messages'][1]['content'])
                batches.append([note['request_id'] for note in notes])
                if len(batches) == 1:
                    first_started.set()
                    release_first.wait(20)
                    answer = {'error': 'simulated malformed model output'}
                else:
                    answer = {'terrain': [], 'maintenance': [], 'notes': 'No conditions stated.'}
                    completed.set()
            elif 'bad_night' in system:
                answer = {'bad_night': False, 'avoid_directions': [], 'reason': 'clear'}
            else:
                answer = {'fault_likely': 0, 'reason': 'healthy'}
            data = json.dumps({'choices': [{'message': {'content': json.dumps(answer)}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    init = V4Workflow(ROOT / 'kit/local-cards/L1').initialize_payload(900)
    init['targets']['rows'] = init['targets']['rows'][:100]
    nights = init['survey']['nights']
    notes = [{'request_id': f'note-{i}', 'reason': f'Unrelated synthetic log note {i}',
              'issued_at_utc': init['survey']['start_utc'], 'target_ids': []} for i in range(10)]
    env = dict(os.environ, OPENAI_API_KEY='local-test-only',
               OPENAI_BASE_URL=f'http://127.0.0.1:{server.server_port}/v1', OPENAI_MODEL='fake',
               PRO_INTEL='1', PRO_INTEL_BATCH_SIZE='6', PRO_MODEL_WAIT_MAX='0.1', PRO_FIXED_LEVEL='3')
    for key in ['PRO_MODEL_DISABLED', 'OBSERVER_MODEL_DISABLED']:
        env.pop(key, None)
    with (args.out / 'agent.log').open('w') as log:
        proc = subprocess.Popen([str(args.agent.resolve())], cwd=ROOT / 'kit/rust-pro', env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        sequence = 0

        def send(kind, payload):
            nonlocal sequence
            sequence += 1
            proc.stdin.write(json.dumps(dict(protocol_version=PROTOCOL_VERSION, message_type=kind,
                                            decision_sequence=sequence, payload=payload)) + '\n')
            proc.stdin.flush()
            if kind == 'decision_request':
                if not selector.select(15):
                    raise RuntimeError('agent response timeout')
                return json.loads(proc.stdout.readline())

        def step(night, offset=0):
            start = datetime.fromisoformat(nights[night]['observing_start_utc'].replace('Z', '+00:00'))
            now = (start + timedelta(seconds=offset)).isoformat().replace('+00:00', 'Z')
            return send('decision_request', dict(now_utc=now, survey_end_utc=init['survey']['end_utc'],
                        new_messages=[], latest_bulletin={'notices': []}, active_requests=notes,
                        last_result={}, wallclock={'remaining_seconds': 900}))

        try:
            send('initialize', init)
            step(0)
            assert first_started.wait(15), 'first decode was not submitted'
            for night in range(1, 5):
                step(night)
            release_first.set()
            time.sleep(0.2)
            # First reply has invalid schema: retry the original batch on the next night.
            step(5)
            got_retry = completed.wait(12)
            if got_retry:
                time.sleep(0.1)
                completed.clear()
                step(6)
                completed.wait(12)
                time.sleep(0.1)
                step(6, 60)  # collect last result
            expected = [[f'note-{i}' for i in range(6)]] * 2 + [[f'note-{i}' for i in range(6, 10)]]
            result = dict(passed=batches == expected, batches=batches, expected=expected)
            (args.out / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result), flush=True)
            return 0 if result['passed'] else 1
        finally:
            release_first.set()
            proc.terminate()
            proc.wait(timeout=5)
            selector.close()
            server.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
