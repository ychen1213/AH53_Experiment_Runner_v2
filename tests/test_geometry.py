import json
import unittest
from pathlib import Path
from ah53runner.geometry import build_geometry
from ah53runner.planning import load_config,plan
ROOT=Path(__file__).resolve().parents[1]

class TestGeometry(unittest.TestCase):
    def setUp(self):
        self.config,_=load_config(ROOT/'configs'/'quick_check.json')
        self.base=self.config['base_model']
    def test_ah53(self):
        model=json.loads(json.dumps(self.base))
        model['films']=[]
        shapes,top,meta=build_geometry(model)
        self.assertEqual(len(shapes),4)
        self.assertEqual(top,300)
        self.assertEqual(meta['ridge_centers_nm'],[-3200,0,3200])
    def test_planarized_zero_is_fill(self):
        shapes,top,meta=build_geometry(self.base)
        self.assertEqual(top,300)
        self.assertGreater(len(shapes),4)
        self.assertGreater(len([s for s in shapes if s.material=='film_1']),0)
    def test_conformal(self):
        model=json.loads(json.dumps(self.base))
        model['films'][0]['mode']='conformal';model['films'][0]['thickness_nm']=100
        shapes,top,meta=build_geometry(model)
        self.assertEqual(top,400)
        self.assertGreater(len(shapes),4)
    def test_stacked(self):
        model=json.loads(json.dumps(self.base))
        model['films']=[{'mode':'conformal','thickness_nm':80,'n':1.46},
                        {'mode':'planarized','thickness_nm':120,'n':1.8}]
        shapes,top,_=build_geometry(model)
        self.assertEqual(top,500)
    def test_ler_not_silently_claimed(self):
        model=json.loads(json.dumps(self.base))
        model['films']=[]
        model['ridge_top_left_inset_nm']=40
        shapes,top,_=build_geometry(model)
        self.assertIsNotNone(shapes[1].vertices_nm)
    def test_invalid_sw_film(self):
        model=json.loads(json.dumps(self.base))
        model['ridge_top_left_inset_nm']=40
        with self.assertRaises(ValueError):build_geometry(model)
    def test_plan(self):
        cases=plan(self.config)
        self.assertEqual(len(cases),3)
        self.assertNotEqual(cases[0]['digest'],cases[1]['digest'])
        self.assertEqual(sum(bool(c.get('capture_field')) for c in cases),3)

if __name__=='__main__':unittest.main()
