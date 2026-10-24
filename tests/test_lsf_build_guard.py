"""Offline checks: build prefixes never infer safety by deleting solver calls."""
import hashlib
from pathlib import Path
import unittest

from ah53runner.lsf_build_guard import build_only_script
from ah53runner.lsf_config import expand_bundle, inspect_lsf, make_bundle


ROOT = Path(__file__).resolve().parents[1]
LOCAL_AH53_SOURCE = ROOT / '02_AH53_XY_FDTD_2023R2_1.lsf'
BOUNDARY = '\nif (run_solver==0) {?"built";} else {run("FDTD");}\n'
MARKER = '\n# AH53_BUILD_ONLY_END\n'


class BuildOnlyGuard(unittest.TestCase):
    @unittest.skipUnless(LOCAL_AH53_SOURCE.is_file(), 'Optional local AH53 source is not distributed with the public repository')
    def test_current_ah53_full_source_preserves_exact_build_prefix(self):
        source = LOCAL_AH53_SOURCE.read_text(encoding='utf8')
        result = build_only_script(source)
        self.assertEqual(result['boundary_mode'], 'top_level_run_solver_branch')
        self.assertEqual(result['script'], source[:source.index('if (run_solver==0)')])
        self.assertIn('addfdtd;', result['script'])
        self.assertIn('save("AH53_XY_FDTD_2023R2_1.fsp");', result['script'])
        self.assertNotIn('run("FDTD")', result['script'])
        self.assertEqual(result['relative_save_paths'], ['AH53_XY_FDTD_2023R2_1.fsp'])

    @unittest.skipUnless(LOCAL_AH53_SOURCE.is_file(), 'Optional local AH53 source is not distributed with the public repository')
    def test_actual_expanded_three_point_scripts_keep_overrides_and_arrays(self):
        source = LOCAL_AH53_SOURCE.read_text(encoding='utf8')
        parameters = {entry['label']: entry['id'] for entry in inspect_lsf(source)['parameters']}
        bundle = make_bundle(source, 'AH53.lsf', parameters['film_t_nm(1)'], [0, 100, 200],
                             {parameters['film_count']: 1})
        for case in expand_bundle(bundle):
            with self.subTest(thickness=case['value']):
                result = build_only_script(case['script'])
                self.assertIn(f'film_t_nm(1) = {case["value"]};', result['script'])
                self.assertIn('film_count = 1;', result['script'])
                self.assertIn('film_mode = [1, 2, 1];', result['script'])

    def test_explicit_marker_preserves_prefix_and_records_exact_provenance(self):
        prefix = 'h=100;\naddrect;\nset("y span",h);'
        source = prefix + MARKER + 'run;\n'
        result = build_only_script(source)
        self.assertEqual(result['script'], prefix + '\n')
        self.assertEqual(result['boundary_mode'], 'explicit_build_only_end')
        self.assertEqual(result['excluded_from_line'], 4)
        self.assertEqual(result['full_script_sha256'], hashlib.sha256(source.encode()).hexdigest())
        self.assertTrue(result['no_solver_calls'])

    def test_nested_solver_calls_and_other_execution_paths_are_rejected(self):
        for command in ('run;', 'runparallel;', 'runsweep("s");', 'runjobs;',
                        'runanalysis;', 'mesh;', 'eval("run;");', 'feval("run.lsf");',
                        'execute("run.lsf");', 'load("project.fsp");',
                        'system("anything");', 'runsetup;', 'matlab("run;");'):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    build_only_script('run_solver=0;\nif(0){' + command + '}\naddrect;' + BOUNDARY)

    def test_unknown_script_calls_are_not_confused_with_arrays(self):
        for invocation in ('external_script;', 'external_script();', 'external_script(1);'):
            with self.subTest(invocation=invocation):
                with self.assertRaises(ValueError):
                    build_only_script('h=100;\n' + invocation + MARKER + 'run;')
        result = build_only_script('values=[1,2];\nx=values(1);\naddrect;' + MARKER + 'run;')
        self.assertEqual(result['indexed_variables'], ['values'])

    def test_prefix_does_not_accept_function_or_group_setup_scripts(self):
        for prefix in ('function f(){run;}\naddrect;', 'addstructuregroup;',
                       'addanalysisgroup;', 'addgroup;set("script","run;");',
                       'addrect;setnamed("object","setup script","run;");',
                       'property="script";addrect;set(property,"run;");'):
            with self.subTest(prefix=prefix):
                with self.assertRaises(ValueError):
                    build_only_script(prefix + MARKER + 'run;')

    def test_save_must_have_an_explicit_in_case_relative_path(self):
        for path in ('"D:/outside/model.fsp"', '"../model.fsp"', '"/tmp/model.fsp"',
                     '"folder/../../model.fsp"', 'filename', '"model"+".fsp"'):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    build_only_script('filename="x.fsp";addrect;save(' + path + ');' + MARKER)
        result = build_only_script('addrect;save("model.fsp");' + MARKER)
        self.assertEqual(result['relative_save_paths'], ['model.fsp'])
        with self.assertRaises(ValueError):
            build_only_script('cd("folder");addrect;' + MARKER)

    def test_material_import_requires_a_relative_literal_dependency(self):
        result = build_only_script('importmaterial("materials/local.mdf");addrect;' + MARKER)
        self.assertEqual(result['required_relative_inputs'], ['materials/local.mdf'])
        with self.assertRaises(ValueError):
            build_only_script('importmaterial("D:/materials/local.mdf");addrect;' + MARKER)

    def test_marker_must_be_a_top_level_complete_statement_boundary(self):
        invalid = ('h=' + MARKER + '100;\n',
                   'if(1){addrect;' + MARKER + '}\n',
                   'text="line\n# AH53_BUILD_ONLY_END\nline";\naddrect;',
                   'addrect;' + MARKER + 'addrect;' + MARKER,
                   'if(1)' + MARKER + '{addrect;}',
                   'if(1){addrect;}' + MARKER + 'else{addcircle;}')
        for source in invalid:
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    build_only_script(source)

    def test_no_boundary_and_ambiguous_automatic_boundaries_are_rejected(self):
        for source in ('addrect;run;', 'run_solver=0;addrect;' + BOUNDARY + BOUNDARY,
                       'run_solver=0;if(1){' + BOUNDARY + '}',
                       'run_solver=0;addrect;\nif(run_solver==0){run;}'):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    build_only_script(source)

    def test_comments_and_strings_do_not_execute_their_contents(self):
        result = build_only_script('text="run; eval; system";\n# run;\naddrect;' + MARKER + 'run;')
        self.assertIn('text="run; eval; system";', result['script'])

    def test_clear_invalidates_earlier_array_definitions(self):
        with self.assertRaises(ValueError):
            build_only_script('values=[1,2];clear;x=values(1);addrect;' + MARKER)


if __name__ == '__main__':
    unittest.main()
