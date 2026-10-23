"""Isolated worker: one case -> one lumapi process -> one result.npz."""
import argparse
import json
import sys
import traceback
from pathlib import Path
from ah53runner.solver import run_case
from ah53runner.artifacts import solver_fingerprint

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--case',required=True)
    p.add_argument('--api-dir',default='')
    p.add_argument('--visible',action='store_true')
    args=p.parse_args()
    casepath=Path(args.case).resolve()
    case=json.loads(casepath.read_text(encoding='utf8'))
    result_dir=casepath.parent
    try:
        if case.get('solver_fingerprint') != solver_fingerprint():
            raise RuntimeError('Solver source changed after planning; refusing a mislabeled execution.')
        result=run_case(case,result_dir,args.api_dir,visible=args.visible)
        print('SOLVER_COMPLETED:',json.dumps(result,ensure_ascii=False),flush=True)
    except BaseException:
        traceback.print_exc()
        sys.exit(1)
if __name__=='__main__': main()
