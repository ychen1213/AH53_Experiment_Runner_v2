"""Pure geometric fixtures: no CAD, solver, or physics-validation claims."""
from copy import deepcopy
import unittest

from ah53runner.lsf_geometry_checks import compare_snapshots, evaluate_snapshot


def rectangle(name, x1, x2, y1, y2, index, *, inferred=True):
    return {'name': name, 'path': '::model::' + name, 'type': 'Rectangle',
            'type_inferred': inferred, 'properties': {
                'x min': x1 * 1e-9, 'x max': x2 * 1e-9,
                'y min': y1 * 1e-9, 'y max': y2 * 1e-9,
                'z min': -0.5e-6, 'z max': 0.5e-6,
                'material': '<Object defined dielectric>', 'index': str(index),
                'enabled': 1, 'rotation 1': 0, 'rotation 2': 0, 'rotation 3': 0}}


def base():
    objects = [rectangle('substrate', -8500, 8500, -1600, 0, 1.5)]
    for i, center in enumerate((-3200, 0, 3200), 1):
        objects.append(rectangle('ridge_' + str(i), center - 800, center + 800, 0, 300, 2))
    objects.append({'name': 'FDTD', 'path': '::model::FDTD', 'type': 'FDTD',
                    'properties': {'x min': -8e-6, 'x max': 8e-6,
                                   'y min': -1.3e-6, 'y max': 2.3e-6,
                                   'dimension': '2D'}})
    for name, y in [('source', 1.35e-6), ('R', 1.63e-6), ('INC1', 1e-6), ('INC2', 1.1e-6)]:
        objects.append({'name': name, 'path': '::model::' + name,
                        'type': 'Source' if name == 'source' else 'Monitor',
                        'properties': {'x min': -8e-6, 'x max': 8e-6,
                                       'y min': y, 'y max': y, 'y': y,
                                       'z min': 0., 'z max': 0.}})
    parameters = {'P_nm': 16000, 'pitch_nm': 3200, 'CD_nm': 1600, 'h_nm': 300,
                  'n_sub': 1.5, 'n_ridge': 2, 'film_count': 0,
                  'film_mode': [1, 2, 1], 'film_t_nm': [100, 150, 75],
                  'film_n': [1.46, 1.8, 1.6]}
    return {'objects': objects, 'unverified': []}, parameters


def planarized(thickness=100):
    snapshot, parameters = base()
    parameters.update(film_count=1, film_mode=[2, 0, 0], film_t_nm=[thickness, 0, 0])
    # Explicitly list groove/ridge intervals, independent of the checker formula.
    spans = [(-8000, -4000, 0), (-4000, -2400, 300), (-2400, -800, 0),
             (-800, 800, 300), (800, 2400, 0), (2400, 4000, 300), (4000, 8000, 0)]
    number = 0
    for x1, x2, old in spans:
        if 300 + thickness > old:
            number += 1
            snapshot['objects'].append(rectangle('film_seg_' + str(number), x1, x2, old,
                                                  300 + thickness, 1.46))
    return snapshot, parameters


def conformal_100():
    snapshot, parameters = base()
    parameters.update(film_count=1, film_mode=[1, 0, 0], film_t_nm=[100, 0, 0])
    # Independent hand construction: 100 nm cap, 100 nm side strips, 100 nm floor.
    regions = [(-8000, -4100, 0, 100), (-4100, -4000, 0, 400),
               (-4000, -2400, 300, 400), (-2400, -2300, 0, 400),
               (-2300, -900, 0, 100), (-900, -800, 0, 400),
               (-800, 800, 300, 400), (800, 900, 0, 400),
               (900, 2300, 0, 100), (2300, 2400, 0, 400),
               (2400, 4000, 300, 400), (4000, 4100, 0, 400),
               (4100, 8000, 0, 100)]
    for i, (x1, x2, y1, y2) in enumerate(regions, 1):
        snapshot['objects'].append(rectangle('film_seg_' + str(i), x1, x2, y1, y2, 1.46))
    return snapshot, parameters


def checks(snapshot, parameters, profile='ah53_xy'):
    return {entry['code']: entry for entry in evaluate_snapshot(snapshot, parameters, profile)}


class GeometrySnapshotChecks(unittest.TestCase):
    def test_bare_structure_passes_known_geometry_without_monitor_volume_errors(self):
        snapshot, parameters = base()
        result = checks(snapshot, parameters)
        self.assertFalse([entry for entry in result.values() if entry['status'] == 'FAIL'])
        self.assertEqual(result['ah53.stack_top']['details']['expected_top_nm'], 300)
        self.assertEqual(result['ah53.film_cross_section']['status'], 'PASS')
        self.assertEqual(result['ah53.fdtd_contains_sources_monitors']['status'], 'PASS')
        self.assertEqual(result['generic.rectangle_dimensions']['status'], 'PASS')

    def test_zero_thickness_modes_differ_and_disabled_selected_film_fails(self):
        filled, params = planarized(0)
        params['__selected_parameter__'] = 'film_t_nm(1)'
        self.assertEqual(checks(filled, params)['ah53.film_cross_section']['status'], 'PASS')
        bare, bare_params = base()
        bare_params.update(film_count=1, film_mode=[1, 0, 0], film_t_nm=[0, 0, 0])
        self.assertEqual(checks(bare, bare_params)['ah53.film_cross_section']['status'], 'PASS')
        wrong_mode = deepcopy(params)
        wrong_mode['film_mode'][0] = 1
        self.assertEqual(checks(filled, wrong_mode)['ah53.film_cross_section']['status'], 'FAIL')
        params['film_count'] = 0
        self.assertEqual(checks(filled, params)['ah53.selected_film_enabled']['status'], 'FAIL')

    def test_conformal_sidewalls_and_gaps_checked_even_when_top_is_correct(self):
        snapshot, params = conformal_100()
        result = checks(snapshot, params)
        self.assertEqual(result['ah53.film_cross_section']['status'], 'PASS')
        self.assertEqual(result['ah53.disjoint_material_regions']['status'], 'PASS')
        # Remove one sidewall while keeping every ridge cap and the same top.
        snapshot['objects'] = [item for item in snapshot['objects'] if item['name'] != 'film_seg_2']
        result = checks(snapshot, params)
        self.assertEqual(result['ah53.stack_top']['status'], 'PASS')
        self.assertEqual(result['ah53.film_cross_section']['status'], 'FAIL')
        self.assertGreater(result['ah53.film_cross_section']['details']['mismatch_area_nm2'], 0)

    def test_multilayer_planarized_stack_keeps_upper_layer_material_and_location(self):
        snapshot, params = planarized(100)
        params.update(film_count=2, film_mode=[2, 2, 0], film_t_nm=[100, 80, 0])
        snapshot['objects'].append(rectangle('film_seg_8', -8000, 8000, 400, 480, 1.8))
        result = checks(snapshot, params)
        self.assertEqual(result['ah53.film_cross_section']['status'], 'PASS')
        self.assertAlmostEqual(result['ah53.stack_top']['details']['actual_top_nm'], 480)
        snapshot['objects'][-1]['properties']['y min'] = 350e-9
        result = checks(snapshot, params)
        self.assertEqual(result['ah53.stack_top']['status'], 'PASS')
        self.assertEqual(result['ah53.film_cross_section']['status'], 'FAIL')
        self.assertEqual(result['ah53.disjoint_material_regions']['status'], 'FAIL')

    def test_conformal_then_planarized_uses_the_previous_nonflat_surface(self):
        snapshot, params = conformal_100()
        params.update(film_count=2, film_mode=[1, 2, 0], film_t_nm=[100, 80, 0])
        # Fill above each of the independently listed first-layer top segments.
        first_layer = [item for item in snapshot['objects'] if item['name'].startswith('film_seg_')]
        for i, item in enumerate(first_layer, 14):
            p = item['properties']
            snapshot['objects'].append(rectangle('film_seg_' + str(i),
                p['x min'] * 1e9, p['x max'] * 1e9, p['y max'] * 1e9, 480, 1.8))
        self.assertEqual(checks(snapshot, params)['ah53.film_cross_section']['status'], 'PASS')

    def test_geometry_material_and_clearance_failures_are_distinct(self):
        snapshot, params = planarized(800)
        self.assertEqual(checks(snapshot, params)['ah53.source_monitor_clearance']['status'], 'FAIL')
        snapshot, params = base()
        next(item for item in snapshot['objects'] if item['name'] == 'ridge_1')['properties']['index'] = '1.9'
        next(item for item in snapshot['objects'] if item['name'] == 'ridge_2')['properties']['x max'] += 20e-9
        monitor = next(item for item in snapshot['objects'] if item['name'] == 'R')
        monitor['properties'].update({'y min': 3e-6, 'y max': 3e-6, 'y': 3e-6})
        result = checks(snapshot, params)
        self.assertEqual(result['ah53.material.ridge_1']['status'], 'FAIL')
        self.assertEqual(result['ah53.geometry.ridge_2']['status'], 'FAIL')
        self.assertEqual(result['ah53.fdtd_contains_sources_monitors']['status'], 'FAIL')

    def test_generic_overlap_warns_and_inferred_zero_dimension_is_not_strict_failure(self):
        a = rectangle('same', 0, 100, 0, 100, 1.5)
        b = rectangle('same', 50, 150, 50, 150, 2)
        result = checks({'objects': [a, b]}, {}, 'generic')
        self.assertEqual(result['generic.bounding_box_overlap']['status'], 'WARN')
        self.assertEqual(result['generic.duplicate_names']['status'], 'WARN')
        b['properties']['z max'] = b['properties']['z min']
        result = checks({'objects': [a, b]}, {}, 'generic')
        self.assertEqual(result['generic.rectangle_dimensions']['status'], 'WARN')
        b['type_inferred'] = False
        self.assertEqual(checks({'objects': [a, b]}, {}, 'generic')['generic.rectangle_dimensions']['status'], 'FAIL')

    def test_nonfinite_missing_and_invalid_parameters_never_pass_silently(self):
        snapshot, params = base()
        snapshot['objects'][0]['properties']['x min'] = float('nan')
        result = checks(snapshot, params)
        self.assertEqual(result['generic.finite_coordinates']['status'], 'FAIL')
        self.assertEqual(result['ah53.geometry.substrate']['status'], 'UNVERIFIED')
        for mutation in ({'film_count': 1.5}, {'film_count': 4}, {'CD_nm': 4000},
                         {'film_count': 1, 'film_mode': [0.5]},
                         {'film_count': 1, 'film_t_nm': [-100]}, {'n_sub': -1}):
            with self.subTest(mutation=mutation):
                current = dict(params, **mutation)
                self.assertEqual(checks(base()[0], current)['ah53.parameters']['status'], 'FAIL')

    def test_snapshot_comparison_reports_geometry_change_and_preserves_duplicate_paths(self):
        snapshot, _ = planarized(100)
        self.assertEqual(compare_snapshots(snapshot, deepcopy(snapshot)), [])
        changed = deepcopy(snapshot)
        changed['objects'][-1]['properties']['y max'] += 100e-9
        difference = compare_snapshots(snapshot, changed)
        self.assertEqual(len(difference), 1)
        self.assertEqual(difference[0]['property'], 'y max')
        duplicate = deepcopy(changed['objects'][0])
        changed['objects'].append(duplicate)
        self.assertTrue(any(item['kind'] == 'added' and item['occurrence'] == 2
                            for item in compare_snapshots(snapshot, changed)))


if __name__ == '__main__':
    unittest.main()
