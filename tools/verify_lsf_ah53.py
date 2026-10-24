"""Compare native AH53 XY Jones exports against independently edited references.

This is a separate, explicitly AH53-specific analysis command. It opens saved
projects and MATLAB files using lumapi but NEVER starts a solver. No generic LSF
monitor names, phase conventions, or Jones definitions are inferred.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ah53runner.lsf_runtime import load_lsf_artifacts

FIELDS = ('Nall', 'theta_deg', 'rss', 'rsp', 'rps', 'rpp', 'rJones',
          'eta_s', 'eta_p', 'lam_selected', 'y_ref', 'Rs_mon', 'Rp_mon', 'sign_in')
MAT_NAME = 'AH53_XY_FDTD_2023R2_1_Jones.mat'
TOLERANCES = {'geometry_absolute_m': 1e-15, 'material_index_absolute': 1e-12,
              'array_absolute': 1e-7, 'array_relative': 1e-6,
              'wavelength_reference_plane_absolute_m': 1e-15}


def read_json(path):
    return Path(path).read_text(encoding='utf-8-sig')


def load_json(path):
    return json.loads(read_json(path))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def geometry_snapshot(session):
    session.selectall()
    names = [str(session.get('name', i)) for i in range(1, int(session.getnumber()) + 1)]
    objects = []
    for name in names:
        if name != 'substrate' and not name.startswith(('ridge_', 'film_seg_')):
            continue
        item = {'name': name}
        for prop in ('x min', 'x max', 'y min', 'y max', 'z min', 'z max', 'enabled'):
            item[prop] = float(np.asarray(session.getnamed(name, prop)).reshape(-1)[0])
        item['material'] = str(session.getnamed(name, 'material'))
        item['index'] = str(session.getnamed(name, 'index'))
        objects.append(item)
    return {'dimension': str(session.getnamed('FDTD', 'dimension')),
            'objects': sorted(objects, key=lambda item: item['name']),
            'source_wavelength_start': float(session.getnamed('source', 'wavelength start')),
            'source_wavelength_stop': float(session.getnamed('source', 'wavelength stop'))}


def read_native(session, folder):
    model, mat = folder / 'model.fsp', folder / MAT_NAME
    for path in (model, mat):
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f'Missing native artifact: {path}')
    session.load(model.as_posix())
    geometry = geometry_snapshot(session)
    # Loading the real native MATLAB export avoids silently using an FSP's
    # possibly unrelated script workspace or a Python recomputation of Jones.
    session.eval('clear;')
    session.matlabload(mat.as_posix())
    arrays = {key: np.asarray(session.getv(key)) for key in FIELDS}
    for key, value in arrays.items():
        if value.size == 0 or not np.isfinite(value).all():
            raise ValueError(f'{folder}: empty or nonfinite native field {key}')
    return geometry, arrays


def compare_geometry(actual, reference):
    differences = []
    maximum = 0.0
    if actual['dimension'] != reference['dimension']:
        differences.append('dimension')
    for key in ('source_wavelength_start', 'source_wavelength_stop'):
        if abs(actual[key] - reference[key]) > TOLERANCES['geometry_absolute_m']:
            differences.append(key)
    left = {item['name']: item for item in actual['objects']}
    right = {item['name']: item for item in reference['objects']}
    if left.keys() != right.keys():
        differences.append('object names/count')
    for name in left.keys() & right.keys():
        for key in ('x min', 'x max', 'y min', 'y max', 'z min', 'z max'):
            error = abs(left[name][key] - right[name][key])
            maximum = max(maximum, error)
            if error > TOLERANCES['geometry_absolute_m']:
                differences.append(f'{name}.{key}')
        for key in ('material', 'enabled'):
            if left[name][key] != right[name][key]:
                differences.append(f'{name}.{key}')
        try:
            index_error = abs(complex(left[name]['index']) - complex(right[name]['index']))
            equal = index_error <= TOLERANCES['material_index_absolute']
        except ValueError:
            equal = left[name]['index'] == right[name]['index']
        if not equal:
            differences.append(f'{name}.index')
    top = max((obj['y max'] for obj in actual['objects'] if obj['name'] != 'substrate'), default=0)
    return {'passed': not differences, 'differences': differences,
            'max_coordinate_error_m': maximum, 'object_count': len(left),
            'dimension': actual['dimension'], 'actual_top_nm': top * 1e9}


def compare_arrays(actual, reference):
    results = {}
    for field in FIELDS:
        left, right = actual[field], reference[field]
        if left.shape != right.shape:
            results[field] = {'passed': False, 'actual_shape': list(left.shape),
                              'reference_shape': list(right.shape)}
            continue
        delta = np.abs(left - right)
        maximum = float(delta.max())
        scale = float(np.linalg.norm(right))
        norm = float(np.linalg.norm(left - right))
        if field in ('Nall', 'sign_in'):
            passed = np.array_equal(left, right)
        elif field in ('lam_selected', 'y_ref'):
            passed = np.allclose(left, right, rtol=0,
                                atol=TOLERANCES['wavelength_reference_plane_absolute_m'])
        else:
            passed = np.allclose(left, right, rtol=TOLERANCES['array_relative'],
                                atol=TOLERANCES['array_absolute'])
        results[field] = {'passed': bool(passed), 'shape': list(left.shape),
                          'max_absolute_error': maximum,
                          'relative_l2_error': norm / scale if scale else None}
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--reference-dir', required=True, type=Path)
    parser.add_argument('--api-dir', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    output, references = args.output_dir.resolve(), args.reference_dir.resolve()
    sys.path.insert(0, str(args.api_dir.resolve()))
    import lumapi
    manifest = load_json(output / 'manifest.json')
    if [entry['value'] for entry in manifest['cases']] != [0, 100, 200]:
        raise ValueError('This minimum acceptance requires exactly 0, 100, 200 nm in order.')
    report = {'schema_version': 'ah53-native-comparison-1.0', 'passed': False,
              'scope': 'AH53 XY, first conformal film, film_count=1, three thicknesses',
              'method': 'Independent default-literal edits vs generated override block; both execute full native LSF through lumapi. No human GUI run claimed.',
              'tolerances': TOLERANCES, 'cases': [], 'api_dir': str(args.api_dir.resolve()),
              'unverified': ['Arbitrary 3D scripts', '2023 R2.1', 'External material databases in this three-point comparison',
                             'Mesh convergence or physical correctness of original Jones phase convention']}
    with lumapi.FDTD(hide=True) as session:
        report['analysis_lumerical_version'] = str(session.version())
        for entry in manifest['cases']:
            folder = output / entry['id']
            case = load_json(folder / 'case.json')
            artifacts = load_lsf_artifacts(folder, case)
            reference = references / str(entry['value'])
            completed = load_json(reference / 'manual_completed.json')
            if completed['status'] != 'DONE' or completed['script_sha256'] != sha256(reference / 'manual.lsf'):
                raise ValueError(f'Invalid independent reference provenance: {reference}')
            actual_geometry, actual_arrays = read_native(session, folder)
            reference_geometry, reference_arrays = read_native(session, reference)
            geometry = compare_geometry(actual_geometry, reference_geometry)
            fields = compare_arrays(actual_arrays, reference_arrays)
            expected_top = 300 + entry['value']
            geometry['expected_top_nm'] = expected_top
            geometry['expected_top_matches'] = abs(geometry['actual_top_nm'] - expected_top) <= 1e-6
            geometry['passed'] &= geometry['expected_top_matches'] and geometry['dimension'] == '2D'
            versions = (artifacts['script_result']['lumerical_version'], completed['version'],
                        report['analysis_lumerical_version'])
            passed = geometry['passed'] and all(item['passed'] for item in fields.values()) and len(set(versions)) == 1
            record = {'thickness_nm': entry['value'], 'case_id': entry['id'], 'passed': bool(passed),
                      'solver_fingerprint': case['solver_fingerprint'],
                      'source_sha256': case['source_sha256'], 'script_sha256': case['script_sha256'],
                      'geometry_and_materials': geometry, 'fields': fields,
                      'versions': versions, 'automatic_elapsed_s': load_json(folder / 'status.json')['elapsed_s'],
                      'manual_elapsed_s': completed['elapsed_s'],
                      'artifact_integrity_verified': True,
                      'automatic_files': artifacts['script_result']['files'],
                      'reference_files': [{'path': name, 'sha256': sha256(reference / name),
                                           'size_bytes': (reference / name).stat().st_size}
                                          for name in ('manual.lsf', 'model.fsp', MAT_NAME)]}
            report['cases'].append(record)
            evidence = args.report.resolve().parent / 'native_comparison_arrays'
            evidence.mkdir(parents=True, exist_ok=True)
            np.savez(evidence / f'automatic_{entry["value"]}.npz', **actual_arrays)
            np.savez(evidence / f'manual_{entry["value"]}.npz', **reference_arrays)
            (evidence / f'geometry_{entry["value"]}.json').write_text(json.dumps(
                {'automatic': actual_geometry, 'manual': reference_geometry}, indent=2), encoding='utf8')
            print('PASS' if passed else 'FAIL', entry['value'],
                  'nm; max |delta Jones| =', fields['rJones']['max_absolute_error'], flush=True)
    report['passed'] = all(item['passed'] for item in report['cases'])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf8')
    print('Comparison:', args.report.resolve(), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
