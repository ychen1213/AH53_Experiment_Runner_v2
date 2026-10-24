"""Isolated CAD-only preflight worker. This entry never starts a solver."""
import argparse
import json
from pathlib import Path
import sys
import traceback

from ah53runner.lsf_preflight import run_preflight_case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, type=Path)
    parser.add_argument('--api-dir', default='')
    parser.add_argument('--visible', action='store_true')
    args = parser.parse_args()
    try:
        case = json.loads(args.case.read_text(encoding='utf8'))
        result = run_preflight_case(case, args.case.parent, args.api_dir, args.visible)
        print('PRECHECK_NATIVE_COMPLETE:', json.dumps({key: result[key] for key in
              ('build_completed', 'fdtd_solver_executed', 'geometry_passed', 'lumerical_version')}, ensure_ascii=False), flush=True)
        # The supervisor validates construction artifacts. Geometry failures
        # remain inspectable and are reported separately by the preflight report.
        return 0
    except BaseException:
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
