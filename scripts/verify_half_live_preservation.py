#!/usr/bin/env python3
"""Compare accepted and feature production publications in isolated interpreters.

Both runs use the same pinned woData/woStrategy sources. The feature run renders
half_live repeatedly at each checkpoint; entire snapshots, full track payloads,
traffic publications and pit groups must stay identical to the accepted run.
No production source or historical golden is changed by this verifier.
"""
from __future__ import annotations
import argparse
from collections import deque
from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime
from enum import Enum
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def encode(value):
    if is_dataclass(value):
        return {f.name: encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, deque)):
        return [encode(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((encode(v) for v in value), key=repr)
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    raise TypeError(type(value))


def child(repo, feature, destination):
    sys.path.insert(0, str(repo / 'tests'))
    from unittest.mock import patch
    import wodata.live.quali_models as data_module
    import wostrategy.analysis.live_qualifying as strategy_module
    import woplanner.quali.service as service_module
    from test_quali_service import driver, raw
    from test_quali_canonical_track_rate import sample, seed
    from test_quali_third_historical_evidence import calibrated_field, send, timing, withdraw_old
    from test_quali_real_consume_publication import fresh_tyres
    # The accepted service records the external wall monotonic callback clock.
    # Fix the clock equally in both isolated runs, rather than stripping any
    # production fields from comparison.
    service_module.monotonic = lambda: 120.0
    calculate = None
    if feature:
        from woplanner.quali.experimental_cutoff import calculate_half_live
        calculate = calculate_half_live
    records = []
    experimental_statuses = set()

    def production(service):
        return encode({
            'snapshot': service.snapshot,
            'track_payload': service.track_evolution_payload(),
            'traffic_finalisation': service.traffic_finalisation_records,
            'track_samples': service._track_samples,
            'pit_groups': service.pit_status_groups(),
        })

    def capture(name, service):
        before = production(service)
        if calculate:
            # Rendering must never invoke the estimator, including repeated paints.
            with patch('woplanner.quali.track_evolution_policy.build_track_evolution_diagnostic',
                       side_effect=AssertionError('R&D display triggered a new live fit')):
                for _ in range(3):
                    experimental_statuses.add(calculate(service.snapshot).status)
            assert production(service) == before, name + ': experimental display mutated production'
        records.append({'checkpoint': name, 'production': before})

    Service = service_module.QualiLiveService
    s = Service(track_rate_s_per_field_push=-.03)
    capture('empty', s)
    s.set_track_override(-.7)
    capture('empty_manual', s)
    s.reset()
    capture('empty_reset', s)
    for part in ('Q1', 'Q2', 'Q3'):
        for rate in (-.1, 0., .1):
            s = Service(eliminated_counts={part: 1}, track_rate_s_per_field_push=-.03)
            s.consume(raw([driver(1,90),driver(2,91),driver(3,92)],part=part))
            capture(f'{part}/{rate}/fallback',s)
            s.set_track_override(-.7)
            capture(f'{part}/{rate}/manual_fallback',s)
            s.set_track_override(None)
            seed(s,sample(1,90),sample(5,90+4*rate))
            s.consume(raw([driver(1,90),driver(2,91),driver(3,92)],part=part))
            capture(f'{part}/{rate}/limited',s)
            # Three drivers with >=3 observations establishes supported fit.
            seed(s,*(sample(i,90+i*rate,code=code) for code in ('AAA','BBB','CCC') for i in (1,5,9)))
            s.consume(raw([driver(1,90),driver(2,91),driver(3,92)],part=part))
            capture(f'{part}/{rate}/multi_driver_fit',s)
            s.set_track_override(-.7)
            capture(f'{part}/{rate}/manual_live',s)
            s.set_fp_track_rate(-.8)
            capture(f'{part}/{rate}/fallback_change_under_manual',s)
            s.set_track_override(None)
            capture(f'{part}/{rate}/clear_manual',s)
            s.finalise()
            capture(f'{part}/{rate}/finalised',s)
            s.reset()
            capture(f'{part}/{rate}/reset',s)
    s = Service(eliminated_counts={'Q1':0,'Q2':0},track_rate_s_per_field_push=-.03)
    for part, index in (('Q1',1),('Q2',5),('Q3',9)):
        if part != 'Q1':
            s.transition_to(part)
            capture(part+'/transition',s)
        seed(s,sample(index,90-.1*index,part=part))
        s.consume(raw([driver(1,90)],part=part))
        capture(part+'/cross_part_fit',s)
    # Real reducer ingestion, causal correction, timer publication and duplicates.
    for live in (False,True):
        for receipt in (112.,113.,114.,114.1):
            s = calibrated_field(live)
            prefix = f'real/{live}/{receipt}'
            capture(prefix+'/calibrated',s)
            send(s,'DriverList',{'1':{'Tla':'D1'}},109.,110.)
            timing(s,'1',{'NumberOfLaps':0,'InPit':False,'BestLapTime':{'Value':'10.000'}},109.,110.)
            fresh_tyres(s,0)
            timing(s,'1',{'NumberOfLaps':1,'LastLapTime':{'Value':'10.400'},'Sectors':{'0':{'Value':'3.000'}}},109.,110.)
            fresh_tyres(s,1)
            capture(prefix+'/first_lap',s)
            send(s,'TimingData',{'Lines':{'1':{'NumberOfLaps':2,'LastLapTime':{'Value':'10.300'},'Sectors':{'0':{'Value':'3.000'}}},'C':{'NumberOfLaps':1}}},110.,receipt)
            capture(prefix+'/atomic_fit_revoke',s)
            s.advance_traffic_clock(115.)
            capture(prefix+'/timer_finalised',s)
            s.consume(s.raw_snapshot)
            capture(prefix+'/duplicate',s)
            withdraw_old(s,116.)
            capture(prefix+'/late_correction',s)
    modules = {'woplanner':service_module.__file__,'wodata':data_module.__file__,'wostrategy':strategy_module.__file__}
    with gzip.open(destination, 'wt') as handle:
        json.dump({'modules':modules,'experimental_statuses':sorted(experimental_statuses),'records':records},handle,sort_keys=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accepted',type=Path)
    parser.add_argument('--feature',type=Path)
    parser.add_argument('--research',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--child',type=Path)
    parser.add_argument('--exercise-experimental',action='store_true')
    args=parser.parse_args()
    if args.child:
        child(args.child,args.exercise_experimental,args.output)
        return
    args.output.mkdir(parents=True,exist_ok=True)
    outputs={}
    for label,repo in (('accepted',args.accepted),('feature',args.feature)):
        dest=args.output/f'independent_{label}_publications.json.gz'
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',QT_QPA_PLATFORM='offscreen',MPLCONFIGDIR='/private/tmp/half-live-mpl-cache')
        env['PYTHONPATH']=os.pathsep.join(map(str,[repo/'src',args.research/'source/woData/src',args.research/'source/woStrategy/src']))
        command=[sys.executable,__file__,'--child',str(repo),'--output',str(dest)]
        if label=='feature': command.append('--exercise-experimental')
        subprocess.run(command,env=env,cwd=repo,check=True)
        with gzip.open(dest, 'rt') as handle:
            outputs[label]=json.load(handle)
    a,b=outputs['accepted'],outputs['feature']
    assert a['records']==b['records'],'Production output differs between accepted and feature'
    # Prove source resolution; editable installs elsewhere must not mask this comparison.
    for label,repo in (('accepted',args.accepted),('feature',args.feature)):
        for name,root in (('woplanner',repo),('wodata',args.research/'source/woData'),('wostrategy',args.research/'source/woStrategy')):
            assert Path(outputs[label]['modules'][name]).is_relative_to(root),outputs[label]['modules']
    digest=hashlib.sha256(json.dumps(a['records'],sort_keys=True).encode()).hexdigest()
    unchanged={}
    for relative in ('src/woplanner/quali/service.py','src/woplanner/quali/models.py','src/woplanner/quali/track_evolution.py','src/woplanner/quali/track_evolution_policy.py','src/woplanner/quali/projection.py','src/woplanner/quali/planning.py'):
        if (args.accepted/relative).exists():
            accepted=(args.accepted/relative).read_bytes()
            assert accepted==(args.feature/relative).read_bytes(),relative
            unchanged[relative]=hashlib.sha256(accepted).hexdigest()
    report={'external_monotonic_clock_s':120.0,'result':'PASS','checkpoints':len(a['records']),'exact_production_output_sha256':digest,'repeated_experimental_reads':3*len(a['records']),'experimental_statuses':b['experimental_statuses'],'accepted_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=args.accepted,text=True).strip(),'module_provenance':{k:v['modules'] for k,v in outputs.items()},'unchanged_core_source_sha256':unchanged,'coverage':['Q1/Q2/Q3','negative/zero/positive rates','manual with fallback/live','configured fallback changes','cross-part fit retention','finalise/reset','empty/unavailable','real archive/live ingestion','atomic new fit plus causal revocation','normal/grace/hard-lock/late receipts','timer publication','duplicate consume','late correction'],'compared':['entire session snapshot including driver projections and actual cutoffs','full track publication including diagnostic and as-of identity','traffic finalisation records','track samples','pit status groups']}
    (args.output/'independent_preservation_report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__': main()
