"""Offline checks for preserving script logic and refusing ambiguous inputs."""
import unittest
from copy import deepcopy

from ah53runner.lsf_config import inspect_lsf, make_bundle, expand_bundle


def bundle_for(source, label, values=(0, 100, 200), fixed=None):
    parameters = {p['label']: p['id'] for p in inspect_lsf(source)['parameters']}
    return make_bundle(source, 'model.lsf', parameters[label], list(values),
                       {parameters[key]: value for key, value in (fixed or {}).items()})


class FullScriptParameters(unittest.TestCase):
    def test_override_runs_after_defaults_before_derived_geometry(self):
        source = 'clear;\nh=300;\nt=[100,150,75];\ncount=0;\ntop=h+t(1);\naddrect;\n'
        cases = expand_bundle(bundle_for(source, 't(1)', fixed={'count': 1}))
        self.assertEqual([c['value'] for c in cases], [0, 100, 200])
        for case in cases:
            generated = case['script']
            self.assertTrue(generated.startswith('clear;\nh=300;\nt=[100,150,75];\ncount=0;\n'))
            self.assertLess(generated.index('count = 1;'), generated.index('top=h+t(1);'))
            self.assertIn(f't(1) = {case["value"]};', generated)
            self.assertTrue(generated.endswith('top=h+t(1);\naddrect;\n'))

    def test_late_constant_after_derived_expression_is_not_an_input(self):
        source = 'h=100;\ntop=h*2;\nlate=50;\n'
        with self.assertRaisesRegex(ValueError, '不能暴露'):
            bundle_for(source, 'late')

    def test_reassignment_and_indexed_writes_are_rejected(self):
        for source, label in [('h=100;\nif (1) {h=200;}\n', 'h'),
                              ('t=[100,200];\nfor(i=1:2){t(i)=0;}\n', 't(1)')]:
            with self.assertRaisesRegex(ValueError, '不可修改'):
                bundle_for(source, label)

    def test_comments_and_strings_cannot_change_parameter_targets(self):
        source = '# h=200;\nh=100;\nmessage="h=500;";\n'
        generated = expand_bundle(bundle_for(source, 'h', [150]))[0]['script']
        self.assertIn('# h=200;', generated)
        self.assertIn('message="h=500;";', generated)
        self.assertIn('h=100;', generated)
        self.assertIn('h = 150;', generated)

    def test_source_or_anchor_edits_are_rejected(self):
        original = bundle_for('h=100;\naddrect;\n', 'h')
        altered = deepcopy(original)
        altered['source']['script'] = altered['source']['script'].replace('100', '200')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            expand_bundle(altered)
        altered = deepcopy(original)
        altered['inspection']['parameters'][0]['start'] += 1
        with self.assertRaisesRegex(ValueError, '锚点'):
            expand_bundle(altered)

    def test_old_converter_bundle_remains_usable_for_safe_inputs(self):
        legacy = bundle_for('h=100;\naddrect;\n', 'h')
        legacy['inspection']['inspector_version'] = '1.0'
        legacy['inspection'].pop('parameter_region')
        for parameter in legacy['inspection']['parameters']:
            parameter.pop('exposed')
            parameter.pop('exposure_reason')
        self.assertEqual(len(expand_bundle(legacy)), 3)

    def test_explicit_region_requires_literal_defaults(self):
        source = '# AH53_PARAMETERS_BEGIN\nh=100;\n# AH53_PARAMETERS_END\ntop=h*2;\n'
        self.assertIn('h = 150;', expand_bundle(bundle_for(source, 'h', [150]))[0]['script'])
        invalid = '# AH53_PARAMETERS_BEGIN\nh=100;\ntop=h*2;\n# AH53_PARAMETERS_END\n'
        with self.assertRaisesRegex(ValueError, '不能暴露'):
            bundle_for(invalid, 'h')

    def test_workspace_reset_after_defaults_cannot_silently_undo_a_sweep(self):
        for command in ('clear;', 'clearexcept("other");', 'clearall;',
                        'eval("h=200;");', 'feval("defaults.lsf");',
                        'execute("defaults.lsf");', 'load("defaults.fsp");',
                        'loaddata("defaults.ldf");', 'matlabload("defaults.mat");'):
            with self.subTest(command=command):
                source = 'h=100;\n' + command + '\naddrect;\nset("y span",h);\n'
                inspection = inspect_lsf(source)
                self.assertFalse(inspection['parameter_region']['valid'])
                with self.assertRaisesRegex(ValueError, '不能暴露'):
                    bundle_for(source, 'h', [150])

    def test_hidden_workspace_calls_in_branches_or_functions_are_rejected(self):
        for hidden in ('if(1){eval("h=200;");}',
                       'function reset_inputs(){clear;}',
                       'for(i=1:2){feval("defaults.lsf");}'):
            with self.subTest(hidden=hidden):
                with self.assertRaisesRegex(ValueError, '不能暴露'):
                    bundle_for('h=100;\n' + hidden + '\naddrect;\n', 'h')

    def test_cleanup_before_defaults_remains_valid(self):
        source = 'clear;\nswitchtolayout;\nselectall;\ndelete;\nh=100;\naddrect;\n'
        generated = expand_bundle(bundle_for(source, 'h', [150]))[0]['script']
        self.assertTrue(generated.startswith(source[:source.index('addrect;')]))
        self.assertLess(generated.index('h = 150;'), generated.index('addrect;'))

    def test_command_names_inside_comments_and_strings_do_not_block(self):
        source = 'h=100;\n# clear; eval("h=200;");\nmessage="matlabload and clear";\naddrect;\n'
        generated = expand_bundle(bundle_for(source, 'h', [150]))[0]['script']
        self.assertIn('h = 150;', generated)
        self.assertIn('message="matlabload and clear";', generated)

    def test_explicit_markers_do_not_bypass_earlier_actions_or_derivations(self):
        block = '# AH53_PARAMETERS_BEGIN\nh=100;\n# AH53_PARAMETERS_END\naddrect;\n'
        for prefix in ('addrect;\n', 'base=1;\nderived=base*2;\n',
                       'function helper(x){return x;}\n', 'eval("base=2;");\n',
                       'base=1;\nclear;\n'):
            with self.subTest(prefix=prefix):
                with self.assertRaisesRegex(ValueError, '不能暴露'):
                    bundle_for(prefix + block, 'h')

    def test_explicit_markers_allow_leading_cleanup_and_numeric_constants(self):
        source = ('clear;\nbase=5;\nconstants=[1,2];\n'
                  '# AH53_PARAMETERS_BEGIN\nh=100;\n# AH53_PARAMETERS_END\n'
                  'derived=base+h;\naddrect;\n')
        generated = expand_bundle(bundle_for(source, 'h', [150]))[0]['script']
        self.assertIn('base=5;\nconstants=[1,2];', generated)
        self.assertLess(generated.index('h = 150;'), generated.index('derived=base+h;'))

    def test_explicit_region_with_later_reset_is_rejected(self):
        source = ('clear;\n# AH53_PARAMETERS_BEGIN\nh=100;\n'
                  '# AH53_PARAMETERS_END\nclear;\naddrect;\n')
        with self.assertRaisesRegex(ValueError, '不能暴露'):
            bundle_for(source, 'h')


if __name__ == '__main__':
    unittest.main()
