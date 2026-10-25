"""One repeatable, offline R18 preparation/strategy/native historical Race check.

Prepared artifacts must already exist. No downloads, data producer reruns, user
state edits, or future R18 Race observations. A fresh output directory is required.
CONDITIONAL means local workflow passed; future live transport is not certified.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    planner = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=planner.parent/'woData')
    parser.add_argument('--event-config', type=Path, default=planner.parent/'woWeekend/configs/race_2026_R18.json')
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=512, help='Historical smoke budget only')
    args = parser.parse_args()
    output = args.output_root.resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    env.setdefault('QT_QPA_PLATFORM', 'offscreen')
    env.setdefault('MPLBACKEND', 'Agg')
    env.setdefault('MPLCONFIGDIR', '/tmp/r18-mpl')
    jobs = [
        ('r18-local-preflight', 'preflight_live_mc.py', ['--year','2026','--round','18',
            '--data-root',str(args.data_root.resolve()), '--event-config',str(args.event_config.resolve())]),
        ('r18-strategy-ui', 'preflight_r18_strategy.py', ['--data-root',str(args.data_root.resolve()),
            '--event-config',str(args.event_config.resolve()), '--output-root',str(output/'r18-strategy')]),
        ('historical-native-race', 'preflight_r18_race.py', ['--output-root',str(output/'historical-race'),
            '--samples',str(args.samples)]),
    ]
    evidence = {'event':'2026-R18', 'data_root':str(args.data_root.resolve()),
        'event_config':str(args.event_config.resolve()), 'checks':[],
        'not_validated':['future live transport/authentication', 'future R18 Race posterior',
                         'strategy rank uncertainty or calibration'],
        'status':'NOT READY'}
    for name, script, options in jobs:
        command = [sys.executable, str(planner/'scripts'/script), *options]
        result = subprocess.run(command, cwd=planner, env=env, text=True, capture_output=True)
        (output/(name+'.stdout.log')).write_text(result.stdout)
        (output/(name+'.stderr.log')).write_text(result.stderr)
        evidence['checks'].append({'name':name,'exit_code':result.returncode,
            'command':command, 'status':'passed' if result.returncode==0 else 'blocked'})
        (output/'readiness.json').write_text(json.dumps(evidence,indent=2))
        print(f'{name}: {evidence["checks"][-1]["status"]}',flush=True)
        if result.returncode:
            return result.returncode
    evidence['status'] = 'CONDITIONAL'
    (output/'readiness.json').write_text(json.dumps(evidence,indent=2))
    print(f'CONDITIONAL: local workflows passed; evidence {output}',flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
