"""Independent analytic checks only; no native or solver result files are made."""
import copy
from pathlib import Path
import unittest

import numpy as np

from ah53runner.planning import load_config
from tools.validate_rcwa_origin import born_reflection_s, hypothesis_factor, evaluate_origin

ROOT=Path(__file__).resolve().parents[1]


class TestRCWAOrigin(unittest.TestCase):
    def setUp(self):
        config,_=load_config(ROOT/'configs'/'rcwa_origin_validation.json')
        self.model=copy.deepcopy(config['base_model'])
        self.model.update(n_substrate=1.,n_ridge=1.0001,ridge_height_nm=100,films=[])

    def test_born_zero_order_matches_independent_uniform_weak_slab_leading_term(self):
        actual=born_reflection_s(self.model,[0])[0]
        k=2*np.pi/self.model['wavelength_nm'];h=self.model['ridge_height_nm']
        filling=3*self.model['ridge_cd_nm']/self.model['period_nm'];chi=self.model['n_ridge']**2-1
        expected=.5j*chi*filling*np.sin(k*h)*np.exp(1j*k*(3600-h))
        self.assertAlmostEqual(actual,expected,places=15)

    def test_cell_origin_is_exact_odd_even_sign_with_no_fit(self):
        orders=np.array([-5,-2,-1,0,1,2,5]);settings={'mesh_cells_x':800}
        half=hypothesis_factor(self.model,settings,orders,'half_cell')
        full=hypothesis_factor(self.model,settings,orders,'cell_origin_half_cell')
        np.testing.assert_allclose(full/half,(-1.)**orders,atol=1e-14)

    def test_unique_declared_hypothesis_is_identified_from_analytic_memory_controls(self):
        records=[];orders=np.arange(-24,25)
        for mesh in (800,1600):
            for n in (1.0001,1.00005):
                model=copy.deepcopy(self.model);model['n_ridge']=n
                settings={'mesh_cells_x':mesh,'name':str(mesh)}
                target=born_reflection_s(model,orders)
                raw=target/hypothesis_factor(model,settings,orders,'cell_origin_half_cell')
                electric=np.zeros((len(orders),2,2),complex);electric[:,0,0]=raw
                electric[:,1,1]=raw*np.sqrt(1-(orders*643/16000)**2)
                frozen={'case':{'id':f'memory_{mesh}_{n}','model':model},'settings':settings}
                records.append((frozen,{}, {'r_orders':orders,'r_E':electric}))
        result=evaluate_origin(records)
        self.assertEqual(result['cell_origin_half_cell']['status'],'PASS')
        self.assertEqual(result['half_cell']['status'],'FAIL')
        self.assertEqual(result['native']['status'],'FAIL')


if __name__=='__main__':unittest.main()
