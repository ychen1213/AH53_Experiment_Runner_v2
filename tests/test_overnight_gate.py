"""Control-flow tests only; no numerical result files or solver calls."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ah53runner.artifacts import ArtifactError
from tools import run_validated_overnight as gate


class TestOvernightDispatch(unittest.TestCase):
    def dispatch(self, returncodes):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)
            (output/'old_failed').mkdir()
            (output/'old_failed'/'status.json').write_text(json.dumps({'status':'FAILED'}))
            cases=[{'id':'old_failed'}, {'id':'new_pending'}]
            with patch.object(gate,'load_config',return_value=({},output/'config.json')), \
                 patch.object(gate,'plan',return_value=cases), \
                 patch.object(gate,'output_path',return_value=output), \
                 patch.object(gate,'solver_fingerprint',return_value='fixed'), \
                 patch.object(gate,'load_artifacts',side_effect=ArtifactError('No complete result')), \
                 patch.object(gate.subprocess,'run',side_effect=[SimpleNamespace(returncode=c) for c in returncodes]) as run:
                result=gate.run_overnight(output/'config.json','fixed')
                return result,[call.args[0] for call in run.call_args_list]

    def test_failed_retry_does_not_skip_new_pending_work(self):
        result,commands=self.dispatch([1,0,0])
        self.assertEqual(result,1)
        self.assertEqual(len(commands),3)
        self.assertIn('--retry-failed',commands[0])
        self.assertNotIn('--retry-failed',commands[1])
        self.assertEqual(commands[2][2],'report')

    def test_interrupt_stops_pending_dispatch_but_keeps_report(self):
        result,commands=self.dispatch([130,0])
        self.assertEqual(result,130)
        self.assertEqual(len(commands),2)
        self.assertIn('--retry-failed',commands[0])
        self.assertEqual(commands[1][2],'report')
