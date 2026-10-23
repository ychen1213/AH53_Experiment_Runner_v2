"""Inspect completed physical benchmarks; this command never starts Lumerical."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ah53runner.planning import load_config
from ah53runner.validation import validate_results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT / 'configs' / 'physics_validation.json'))
    args = parser.parse_args()
    config, path = load_config(args.config)
    report, output = validate_results(config, path)
    print('PHYSICS VALIDATION:', report['status'])
    print('REPORT:', output / 'physics_validation.html')
    for case in report['cases']:
        failures = [c['name'] for c in case['checks'] if c['status'] == 'FAIL']
        print(case['status'], case['case_id'], '; '.join(failures))
    return 0 if report['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
