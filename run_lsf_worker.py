"""Isolated worker entry point for a complete native LSF case."""
import argparse
import json
from pathlib import Path
import sys
import traceback

from ah53runner.lsf_worker import run_lsf_case


def main():
    parser = argparse.ArgumentParser(description="Execute one full native LSF without adding solver calls.")
    parser.add_argument("--case", required=True)
    parser.add_argument("--api-dir", default="")
    parser.add_argument("--visible", action="store_true")
    args = parser.parse_args()
    try:
        case_path = Path(args.case).absolute()
        case = json.loads(case_path.read_text(encoding="utf8"))
        result = run_lsf_case(case, case_path.parent, args.api_dir, visible=args.visible)
        print("LSF_COMPLETED:", json.dumps({key: result[key] for key in
              ("execution_completed", "completion_kind", "lumerical_version", "files")},
              ensure_ascii=False), flush=True)
    except BaseException:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
