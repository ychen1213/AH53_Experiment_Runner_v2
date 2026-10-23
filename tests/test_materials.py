import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from ah53runner import materials
from ah53runner.planning import canonical_hash, load_config, plan

ROOT = Path(__file__).resolve().parents[1]
CLI_SPEC = importlib.util.spec_from_file_location('materials_cli_test_module', ROOT/'tools/materials.py')
CLI = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(CLI)


def definition(n=1.5, k=0.01, **changes):
    value = {'name': 'Glass', 'anisotropy': 'None', 'type': 'Sampled 3D data',
             'max coefficients': 10, 'tolerance': .001, 'make fit passive': 1,
             'specify fit range': 0, 'frequency min': materials.C0/2000e-9,
             'frequency max': materials.C0/200e-9,
             'sampled 3d data': np.array([[materials.C0/2000e-9, complex(n,k)**2],
                                         [materials.C0/200e-9, complex(n,k)**2]])}
    value.update(changes)
    return value


class MaterialSession:
    """Deterministic fake API, including silent MDF name replacement."""
    def __init__(self, definitions=None):
        self.original = copy.deepcopy(definitions or {'Glass': definition()})
        self.definitions = copy.deepcopy(self.original)
        self.libraries = {}
        self.calls = []
        self.fit_multiplier = 1.0
        self.axis_multiplier = {1: 1., 2: 1., 3: 1.}

    def newproject(self):
        self.definitions = copy.deepcopy(self.original)

    def getmaterial(self, name=None, prop=None):
        if name is None:
            return '\n'.join(self.definitions)
        if prop is None:
            return '\n'.join(self.definitions[name])
        return self.definitions[name][prop.lower()]

    def importmaterialdb(self, path):
        self.calls.append(('import', path))
        self.definitions.update(copy.deepcopy(self.libraries[path]))

    def getindex(self, name, frequencies, component=1):
        self.calls.append(('getindex', name, component))
        data = self.definitions[name]['sampled 3d data']
        return np.full(np.asarray(frequencies).shape, np.sqrt(data[0, 1])*self.axis_multiplier[component])

    def getfdtdindex(self, name, frequencies, fmin, fmax, component=1):
        self.calls.append(('getfdtdindex', name, component, fmin, fmax))
        return self.getindex(name, frequencies, component)*self.fit_multiplier

    def version(self):
        return 'TEST_ONLY'


class TestMaterials(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.model = {'geometry_kind': 'planar_interface', 'wavelength_nm': 643,
                      'substrate_material': 'Glass', 'sim': {}}

    def tearDown(self):
        self.temporary.cleanup()

    def library(self, name='one.mdf', content=b'library1'):
        path = self.directory/name
        path.write_bytes(content)
        return materials.freeze_material_databases([name], self.directory)[0]

    def test_named_and_numeric_are_mutually_exclusive(self):
        for key in ('n_substrate', 'k_substrate'):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'conflicts'):
                materials.material_spec(dict(self.model, **{key: 0}), 'substrate')

    def test_negligible_fit_loss_classification_preserves_weak_raw_absorption(self):
        tiny_fit = {'raw_k': 0., 'k': 1.52994823e-10}
        before = dict(tiny_fit)
        self.assertTrue(materials.is_negligible_fit_loss(tiny_fit))
        self.assertFalse(materials.is_effectively_absorbing(tiny_fit))
        self.assertEqual(tiny_fit, before)
        for material in ({'raw_k': 1e-15, 'k': 1e-15},
                         {'raw_k': 1e-15, 'k': 0},
                         {'raw_k': 0, 'k': 1.00001e-8},
                         {'k': 1e-15}):
            with self.subTest(material=material):
                self.assertTrue(materials.is_effectively_absorbing(material))
                self.assertFalse(materials.is_negligible_fit_loss(material))
        self.assertFalse(materials.is_effectively_absorbing({'raw_k': 0, 'k': 0}))
        self.assertFalse(materials.is_negligible_fit_loss({'raw_k': 0, 'k': 0}))
        self.assertTrue(materials.is_negligible_fit_loss({'raw_k': 0, 'k': 1e-8}))
        with self.assertRaises(ValueError):
            materials.is_effectively_absorbing({'raw_k': 0, 'k': float('nan')})

    def test_role_selectors_and_numeric_validation(self):
        self.assertEqual(materials.material_spec({'n_ridge': 2}, 'ridge'),
                         {'kind': 'constant', 'n': 2., 'k': 0.})
        for n in (True, float('nan'), -1, 0):
            with self.subTest(n=n), self.assertRaises(ValueError):
                materials.material_spec({'n_ridge': n}, 'ridge')
        with self.assertRaisesRegex(ValueError, 'named MDF'):
            materials.material_spec({'films': [{'n': 1.5, 'k': .1}]}, 'film_1')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            materials.material_spec({'films': [{'material': 'Glass', 'n': 1.5}]}, 'film_1')

    def test_active_roles_planarized_zero_is_real_material(self):
        model = {'n_substrate': 1.5, 'n_ridge': 2,
                 'films': [{'mode': 'planarized', 'thickness_nm': 0, 'material': 'Glass'},
                           {'mode': 'conformal', 'thickness_nm': 0, 'material': 'Unused'},
                           {'mode': 'none', 'thickness_nm': 20, 'material': 'Unused'}]}
        self.assertEqual(materials.material_roles(model), ['substrate', 'ridge', 'film_1'])
        self.assertTrue(materials.has_named_materials(model))

    def test_zero_planarized_layer_on_flat_surface_is_not_resolved(self):
        config, unused = load_config(ROOT/'configs/quick_check.json')
        model = config['base_model']
        model['films'] = [{'mode': 'planarized', 'thickness_nm': 100, 'n': 1.46},
                          {'mode': 'planarized', 'thickness_nm': 0, 'material': 'Missing unused name'}]
        self.assertEqual(materials.material_roles(model), ['substrate', 'ridge', 'film_1'])
        self.assertFalse(materials.has_named_materials(model))
        result = materials.resolve_materials(object(), model)
        self.assertEqual(set(result['roles']), {'substrate', 'ridge', 'film_1'})
        model['films'][1]['thickness_nm'] = 1
        self.assertIn('film_2', materials.material_roles(model))
        with self.assertRaisesRegex(ValueError, 'was not found'):
            materials.resolve_materials(MaterialSession(), model)

    def test_positive_fit_band(self):
        self.assertEqual(materials.fit_range_nm(self.model), (543, 743))
        self.assertEqual(materials.fit_range_nm(dict(self.model, sim={'source_bandwidth_nm': 40})), (623, 663))
        for wavelength, width in ((100, 200), (643, 0), (643, float('inf'))):
            with self.subTest(wavelength=wavelength, width=width), self.assertRaises(ValueError):
                materials.fit_range_nm({'wavelength_nm': wavelength, 'sim': {'source_bandwidth_nm': width}})

    def test_freeze_relative_path_and_detect_replaced_contents(self):
        record = self.library()
        model = dict(self.model, material_databases=[record])
        self.assertTrue(Path(record['path']).is_absolute())
        materials.verify_material_databases(model)
        Path(record['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed after planning'):
            materials.verify_material_databases(model)

    def test_missing_and_duplicate_library_rejected(self):
        with self.assertRaisesRegex(ValueError, 'existing .mdf'):
            materials.freeze_material_databases(['missing.mdf'], self.directory)
        first = self.library()
        self.library('two.mdf')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            materials.freeze_material_databases([first['path'], 'two.mdf'], self.directory)

    def test_frozen_records_cannot_be_unresolved_user_strings(self):
        with self.assertRaisesRegex(ValueError, 'Unfrozen'):
            materials.verify_material_databases({'material_databases': ['relative.mdf']})

    def test_config_freezes_before_case_digest_and_old_config_unchanged(self):
        source = json.loads((ROOT/'configs/quick_check.json').read_text(encoding='utf8'))
        path = self.directory/'config.json'
        path.write_text(json.dumps(source), encoding='utf8')
        before, unused = load_config(path)
        self.assertEqual(source, before)
        first_id = plan(before)[0]['id']
        self.library()
        source['material_databases'] = ['one.mdf']
        path.write_text(json.dumps(source), encoding='utf8')
        after, unused = load_config(path)
        self.assertNotEqual(first_id, plan(after)[0]['id'])
        old_hash = canonical_hash(after['base_model'])
        (self.directory/'one.mdf').write_bytes(b'new definition')
        with self.assertRaisesRegex(ValueError, 'changed after planning'):
            plan(after)
        fresh, unused = load_config(path)
        self.assertNotEqual(old_hash, canonical_hash(fresh['base_model']))

    def test_resolution_uses_fitted_response_and_explicit_band(self):
        session = MaterialSession()
        session.fit_multiplier = 1.001
        resolved = materials.resolve_materials(session, self.model)
        role = resolved['roles']['substrate']
        self.assertAlmostEqual(role['n'], 1.5015)
        self.assertAlmostEqual(role['k'], .01001)
        self.assertAlmostEqual(role['raw_n'], 1.5)
        self.assertEqual(resolved['fit_wavelength_range_nm'], [543, 743])
        calls = [call for call in session.calls if call[0] == 'getfdtdindex']
        self.assertEqual([call[2] for call in calls], [1, 2, 3])
        self.assertAlmostEqual(calls[0][3], materials.C0/743e-9)
        self.assertEqual(len(role['definition_sha256']), 64)
        self.assertEqual(role['validation']['spectrum_samples'], 1001)
        json.dumps(resolved, allow_nan=False)

    def test_import_new_alias_and_existing_builtin_override_provenance(self):
        record = self.library()
        session = MaterialSession()
        session.libraries[record['path']] = {'Glass': definition(n=1.7), 'New alias': definition()}
        result = materials.resolve_materials(session, dict(self.model, material_databases=[record]))
        self.assertAlmostEqual(result['roles']['substrate']['n'], 1.7)
        change = result['imports'][0]['selected_material_changes']['Glass']
        self.assertTrue(change['overrode_existing_definition'])
        self.assertNotEqual(change['before_sha256'], change['after_sha256'])
        self.assertEqual(result['imported_names'], ['New alias'])

    def test_two_overriding_libraries_rejected(self):
        records = [self.library(), self.library('two.mdf', b'library2')]
        session = MaterialSession()
        session.libraries[records[0]['path']] = {'Glass': definition(n=1.7)}
        session.libraries[records[1]['path']] = {'Glass': definition(n=1.8)}
        with self.assertRaisesRegex(ValueError, 'changed by both'):
            materials.resolve_materials(session, dict(self.model, material_databases=records))

    def test_identical_repeated_definition_is_harmless(self):
        records = [self.library(), self.library('two.mdf', b'library2')]
        session = MaterialSession()
        for record in records:
            session.libraries[record['path']] = {'Glass': definition(n=1.7)}
        result = materials.resolve_materials(session, dict(self.model, material_databases=records))
        self.assertEqual(result['imports'][1]['selected_material_changes'], {})

    def test_nonexistent_material_does_not_fall_back(self):
        with self.assertRaisesRegex(ValueError, 'was not found'):
            materials.resolve_materials(MaterialSession(), dict(self.model, substrate_material='Wrong'))

    def test_anisotropy_metadata_rejected(self):
        session = MaterialSession({'Glass': definition(anisotropy='Diagonal')})
        with self.assertRaisesRegex(ValueError, 'anisotropic'):
            materials.resolve_materials(session, self.model)

    def test_cartesian_disagreement_rejected(self):
        session = MaterialSession()
        session.axis_multiplier[3] = 1.01
        with self.assertRaisesRegex(ValueError, 'not isotropic'):
            materials.resolve_materials(session, self.model)

    def test_special_material_model_rejected(self):
        session = MaterialSession({'Glass': definition(type='Chi3 Raman Kerr')})
        with self.assertRaisesRegex(ValueError, 'supported linear bulk'):
            materials.resolve_materials(session, self.model)

    def test_fit_range_override_mismatch_rejected(self):
        session = MaterialSession({'Glass': definition(**{'specify fit range': 1})})
        with self.assertRaisesRegex(ValueError, 'overrides the fit range'):
            materials.resolve_materials(session, self.model)

    def test_sampled_data_extrapolation_rejected(self):
        session = MaterialSession({'Glass': definition(**{'sampled 3d data': np.array(
            [[materials.C0/650e-9, 2.25], [materials.C0/620e-9, 2.25]])})})
        with self.assertRaisesRegex(ValueError, 'does not cover'):
            materials.resolve_materials(session, self.model)

    def test_gain_and_nonfinite_response_rejected(self):
        for k in (-.01, float('nan')):
            session = MaterialSession({'Glass': definition(k=k)})
            with self.subTest(k=k), self.assertRaises(ValueError):
                materials.resolve_materials(session, self.model)

    def test_bad_fit_rejected_and_explicit_error_bound_recorded(self):
        session = MaterialSession()
        session.fit_multiplier = 1.1
        with self.assertRaisesRegex(ValueError, 'material-fit relative epsilon error'):
            materials.resolve_materials(session, self.model)
        result = materials.resolve_materials(session, dict(self.model, material_fit={'max_relative_permittivity_error': .25}))
        self.assertEqual(result['roles']['substrate']['validation']['relative_permittivity_error_limit'], .25)

    def test_constant_mode_requires_no_material_api(self):
        model = {'n_substrate': 1.5, 'n_ridge': 2, 'films': [], 'wavelength_nm': 643}
        result = materials.resolve_materials(object(), model)
        self.assertEqual(result['roles']['ridge']['n'], 2)
        self.assertEqual(result['material_checks']['unique_named_materials'], 0)

    def test_identical_named_roles_resolve_once(self):
        session = MaterialSession()
        model = dict(self.model, geometry_kind='ah53', ridge_material='Glass', films=[])
        result = materials.resolve_materials(session, model)
        self.assertEqual(result['roles']['substrate'], result['roles']['ridge'])
        self.assertEqual(sum(call[0] == 'getfdtdindex' for call in session.calls), 3)

    def test_cli_check_deduplicates_thickness_but_rechecks_changed_wavelength(self):
        config, unused = load_config(ROOT/'configs/quick_check.json')
        config['base_model']['substrate_material'] = 'Glass'
        del config['base_model']['n_substrate']
        session = MaterialSession()
        report = CLI.check_models(session, config)
        self.assertEqual(report['cases_checked'], 3)
        self.assertEqual(report['unique_optical_setups'], 1)
        config['experiments'] = [{'name': 'colors', 'parameter': 'wavelength_nm', 'values': [600, 700]}]
        report = CLI.check_models(session, config)
        self.assertEqual(report['unique_optical_setups'], 2)
        self.assertEqual([item['resolution']['target_wavelength_nm'] for item in report['checks']], [600, 700])

    def test_cli_demo_exports_only_renamed_copies_and_refuses_overwrite(self):
        class ExportSession(MaterialSession):
            def copymaterial(self, source):
                copied = source+' Copy'
                self.definitions[copied] = copy.deepcopy(self.definitions[source])
                return copied

            def setmaterial(self, name, key, value):
                if key == 'name':
                    self.definitions[value] = self.definitions.pop(name)
                    self.definitions[value]['name'] = value
                else:
                    self.definitions[name][key] = value

            def exportmaterialdb(self, path, names):
                self.exported_names = names
                Path(path).write_bytes(b'FAKE MDF FOR OFFLINE TEST ONLY')

        session = ExportSession({source: definition(name=source) for source in CLI.DEMO_MATERIALS.values()})
        path = self.directory/'demo.mdf'
        report = CLI.export_demo(session, path)
        self.assertEqual(session.exported_names, list(CLI.DEMO_MATERIALS))
        self.assertTrue(path.with_suffix('.manifest.json').is_file())
        self.assertEqual(report['sha256'], materials.sha256_file(path))
        for source in CLI.DEMO_MATERIALS.values():
            self.assertEqual(session.definitions[source]['tolerance'], .001)
        with self.assertRaisesRegex(ValueError, 'refuses to replace'):
            CLI.export_demo(session, path)


if __name__ == '__main__':
    unittest.main()
