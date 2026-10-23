"""List/import MDF materials or check their actual optical model without solving."""
from __future__ import annotations

import argparse
import copy
import importlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ah53runner.materials import (freeze_material_databases, import_material_databases,
                                 list_material_names, material_definition_digest,
                                 material_roles, material_spec, resolve_materials,
                                 sha256_file, verify_material_databases)
from ah53runner.planning import load_config, plan

DEMO_MATERIALS = {
    'AH53_TEST_Silicon': 'Si (Silicon) - Palik',
    'AH53_TEST_Silica': 'SiO2 (Glass) - Palik',
    'AH53_TEST_Gold': 'Au (Gold) - CRC',
}
DEMO_FIT_SETTINGS = {
    # Fixed demonstration choices checked at 643 nm (all) and 780 nm (Si),
    # using a 200 nm full source band. More coefficients can introduce
    # narrow nonpassive wiggles; a smaller fit tolerance is not always better.
    'AH53_TEST_Silicon': {'tolerance': .01, 'max coefficients': 6},
    'AH53_TEST_Silica': {'tolerance': 1e-4, 'max coefficients': 20},
    'AH53_TEST_Gold': {'tolerance': .001, 'max coefficients': 6},
}


def export_demo(session, destination):
    """Copy installed builtins into a clearly named, user-local validation MDF."""
    path = Path(destination).expanduser().resolve()
    if path.suffix.lower() != '.mdf':
        raise ValueError('--output must end in .mdf for export-demo.')
    manifest = path.with_suffix('.manifest.json')
    if path.exists() or manifest.exists():
        raise ValueError(f'Demo export refuses to replace existing files: {path}. Choose a new output path.')
    names = set(list_material_names(session))
    records = []
    for alias, original in DEMO_MATERIALS.items():
        if original not in names:
            raise ValueError(f'Installed builtin material is unavailable: {original}')
        if alias in names:
            raise ValueError(f'Demo material alias already exists in this session: {alias}')
        copied = session.copymaterial(original)
        session.setmaterial(copied, 'name', alias)
        settings = dict(DEMO_FIT_SETTINGS[alias], **{'make fit passive': 1,
                                                  'improve numerical stability': 1})
        for property_name, value in settings.items():
            session.setmaterial(alias, property_name, value)
        records.append({'name': alias, 'copied_from_installed_builtin': original,
                        'definition_sha256': material_definition_digest(session, alias),
                        'fit_settings': settings})
    path.parent.mkdir(parents=True, exist_ok=True)
    session.exportmaterialdb(str(path), list(DEMO_MATERIALS))
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError('Lumerical did not create the requested demo MDF.')
    record = {'schema_version': '1.0', 'purpose': 'LOCAL VALIDATION DEMO; not a supplied user material library',
              'lumerical_version': str(session.version()), 'path': str(path),
              'sha256': sha256_file(path), 'materials': records,
              'note': 'Only copies in this temporary session were changed; check the fitted band before solving.'}
    manifest.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf8')
    return record


def _model_signature(model):
    """Avoid rechecking the same optical setup at every film thickness."""
    value = {'roles': {role: material_spec(model, role) for role in material_roles(model)},
             'material_databases': model.get('material_databases', []),
             'wavelength_nm': model['wavelength_nm'],
             'source_bandwidth_nm': model.get('sim', {}).get('source_bandwidth_nm', 200),
             'material_fit': model.get('material_fit', {})}
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def check_models(session, config):
    """Check all distinct active material/wavelength combinations in the plan."""
    cases = plan(config)
    if not cases:
        cases = [{'id': 'base_model', 'model': config['base_model']}]
    results, by_signature = [], {}
    for case in cases:
        model = case['model']
        verify_material_databases(model)
        signature = _model_signature(model)
        if signature in by_signature:
            by_signature[signature]['case_ids'].append(case['id'])
            continue
        session.newproject()
        resolved = resolve_materials(session, model)
        item = {'case_ids': [case['id']], 'resolution': resolved}
        by_signature[signature] = item
        results.append(item)
    for case in cases:
        verify_material_databases(case['model'])
    return {'schema_version': '1.0', 'status': 'PASS',
            'lumerical_version': str(session.version()),
            'solver_run_performed': False, 'cases_checked': len(cases),
            'unique_optical_setups': len(results), 'checks': results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('list', 'check', 'export-demo'))
    parser.add_argument('--config', help='Runner JSON; relative MDF paths are resolved against this file.')
    parser.add_argument('--api-path', help='Directory containing the installed lumapi.py; overrides config.')
    parser.add_argument('--database', action='append', default=[], help='List command only: an MDF path; repeat for multiple files.')
    parser.add_argument('--filter', default='', help='List command: case-insensitive material name filter.')
    parser.add_argument('--output', help='Optional JSON report path, or required .mdf destination for export-demo.')
    args = parser.parse_args(argv)
    config = None
    if args.config:
        config, unused_path = load_config(args.config)
    if args.command == 'check' and config is None:
        parser.error('check requires --config.')
    if args.database and args.command != 'list':
        parser.error('--database is supported only by list; put MDF paths in the config for check.')
    if args.command == 'export-demo' and not args.output:
        parser.error('export-demo requires --output materials/ah53_demo.mdf.')
    api_path = Path(args.api_path or (config or {}).get('lumerical_api_python', '')).expanduser().resolve()
    if not (api_path/'lumapi.py').is_file():
        raise ValueError('Provide --api-path or lumerical_api_python pointing to the installed lumapi.py directory.')
    sys.path.insert(0, str(api_path))
    lumapi = importlib.import_module('lumapi')
    with lumapi.FDTD(hide=True) as session:
        session.newproject()
        if args.command == 'export-demo':
            result = export_demo(session, args.output)
            print('EXPORTED LOCAL DEMO:', result['path'])
            print('Names:', ', '.join(DEMO_MATERIALS))
            return 0
        if args.command == 'check':
            result = check_models(session, config)
        else:
            model = copy.deepcopy((config or {}).get('base_model',
                        {'geometry_kind': 'planar_interface', 'n_substrate': 1,
                         'wavelength_nm': 643, 'sim': {}}))
            if args.database:
                if model.get('material_databases'):
                    raise ValueError('Use either config material_databases or --database, not both.')
                model['material_databases'] = freeze_material_databases(args.database, Path.cwd())
            imports = import_material_databases(session, model)
            names = [name for name in list_material_names(session) if args.filter.lower() in name.lower()]
            verify_material_databases(model)
            result = {'schema_version': '1.0', 'lumerical_version': str(session.version()),
                      'solver_run_performed': False, 'imports': imports,
                      'material_names': names,
                      'note': 'Exact names in the session after import, including installed builtins; list does not certify optical support.'}
    if args.output:
        destination = Path(args.output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')
        print('MATERIAL REPORT:', destination)
    if args.command == 'list':
        for name in result['material_names']:
            print(name)
        print(f'{len(result["material_names"])} names; no electromagnetic solve performed.')
    else:
        print(f'PASS: {result["cases_checked"]} cases, {result["unique_optical_setups"]} distinct optical setups; no electromagnetic solve performed.')
        for item in result['checks']:
            resolved = item['resolution']
            print(f'Wavelength {resolved["target_wavelength_nm"]:g} nm; fit band {resolved["fit_wavelength_range_nm"]} nm')
            for role, material in resolved['roles'].items():
                print(f'  {role}: {material.get("name", "constant")} n={material["n"]:.9g} k={material["k"]:.9g}')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError) as error:
        print('MATERIAL CHECK REFUSED:', error, file=sys.stderr)
        raise SystemExit(2)
