"""Material plumbing and analytic physics tests; no fake solver evidence is saved."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from ah53runner.artifacts import ArtifactError, _material_evidence
from ah53runner.geometry import Shape, build_geometry, shapes_to_json
from ah53runner.materials import is_effectively_absorbing
from ah53runner.planning import load_config
from ah53runner.solver import add_rect, build_model
from ah53runner.validation import DEFAULT_TOLERANCES, check_common, check_planar, fresnel_at_reference

ROOT = Path(__file__).resolve().parents[1]


class RecordingSession:
    """In-memory API recorder, never a physical solver or saved FSP."""
    def __init__(self):
        self.objects=[]
        self.current=None
        self.global_monitor={}

    def add(self, kind):
        self.current={'kind':kind}
        self.objects.append(self.current)

    def addrect(self): self.add('rectangle')
    def addpoly(self): self.add('polygon')
    def addfdtd(self): self.add('fdtd')
    def addmesh(self): self.add('mesh')
    def addplane(self): self.add('source')
    def adddftmonitor(self): self.add('monitor')
    def set(self, key, value): self.current[key]=value
    def get(self, key): return self.current[key]
    def setglobalmonitor(self, key, value): self.global_monitor[key]=value


def planar_fixture(n, wavelength=643., depth_nm=600., raw_k=None):
    """Independent analytic half-space values, kept only in memory."""
    index=complex(n)
    coeff=fresnel_at_reference(index,wavelength,1.8,-1)
    reflected=abs((1-index)/(1+index))**2
    transmitted=index.real*abs(2/(1+index))**2*np.exp(-4*np.pi*index.imag*depth_nm/wavelength)
    jones=np.eye(2,dtype=complex)[None,:,:]*coeff
    substrate={'n':index.real,'k':index.imag,
               'raw_k':index.imag if raw_k is None else raw_k}
    q={'incident_spatial_phase_sign':-1,'absorption_present':is_effectively_absorbing(substrate)}
    for pol in ('s','p'):
        q.update({f'air_reference_reflected_{pol}':0.,f'total_reflected_{pol}':reflected,
                  f'total_transmitted_{pol}':transmitted,f'power_closure_{pol}':reflected+transmitted,
                  f'flux_decomposition_error_{pol}':0.,f'signed_reflection_flux_{pol}':reflected,
                  f'signed_transmission_flux_{pol}':-transmitted})
    return {'orders':np.array([0]),'r_E':jones,'r_power':jones.copy(),
            'theta_deg':np.array([0.]),'eta_s':np.array([reflected]),'eta_p':np.array([reflected]),
            'lambda_nm':np.array([wavelength]),'quality':q,
            'materials':{'roles':{'substrate':substrate}}}


class TestMaterialFDTD(unittest.TestCase):
    def setUp(self):
        self.base=load_config(ROOT/'configs'/'quick_check.json')[0]['base_model']

    def test_named_geometry_keeps_roles_without_fabricated_indices(self):
        model=copy.deepcopy(self.base)
        del model['n_substrate'],model['n_ridge']
        model.update(substrate_material='Test glass',ridge_material='Test silicon')
        model['films']=[{'mode':'planarized','thickness_nm':0,'material':'Test cap'}]
        shapes,top,meta=build_geometry(model)
        self.assertEqual(top,300)
        self.assertEqual({s.material for s in shapes},{'substrate','ridge','film_1'})
        self.assertTrue(all(s.index_n is None for s in shapes))
        self.assertEqual(shapes[0].material_name,'Test glass')
        self.assertTrue(all(s.material_name=='Test cap' for s in shapes if s.material=='film_1'))
        self.assertIn('"index_n": null',json.dumps(shapes_to_json(shapes)))

    def test_named_planar_geometry_and_selector_conflicts(self):
        model=copy.deepcopy(self.base)
        model.update(geometry_kind='planar_interface',films=[],substrate_material='Test glass')
        with self.assertRaisesRegex(ValueError,'conflicts'):
            build_geometry(model)
        del model['n_substrate']
        shapes,top,_=build_geometry(model)
        self.assertEqual(len(shapes),1)
        self.assertIsNone(shapes[0].index_n)
        self.assertEqual(shapes[0].material_name,'Test glass')

    def test_named_object_does_not_overwrite_database_index(self):
        session=RecordingSession()
        add_rect(session,Shape('substrate','substrate',None,-1,1,-1,0,material_name='My MDF material'),1e-6)
        self.assertEqual(session.current['material'],'My MDF material')
        self.assertNotIn('index',session.current)
        bad=RecordingSession()
        bad.get=lambda key:'Silently replaced material'
        with self.assertRaisesRegex(ValueError,'readback'):
            add_rect(bad,Shape('substrate','substrate',None,-1,1,-1,0,material_name='My MDF material'),1e-6)

    def test_constant_object_preserved_and_read_back(self):
        session=RecordingSession()
        add_rect(session,Shape('ridge','ridge',2.,-1,1,0,1),1e-6)
        self.assertEqual(session.current['material'],'<Object defined dielectric>')
        self.assertEqual(session.current['index'],2.)

    def test_source_and_material_fit_share_configured_band(self):
        model=copy.deepcopy(self.base)
        model['sim']['source_bandwidth_nm']=120.
        shapes,top,_=build_geometry(model)
        session=RecordingSession()
        coords=build_model(session,shapes,top,model,False)
        source=next(obj for obj in session.objects if obj.get('name')=='source')
        self.assertAlmostEqual(source['wavelength start'],(model['wavelength_nm']-60)*1e-9)
        self.assertAlmostEqual(source['wavelength stop'],(model['wavelength_nm']+60)*1e-9)
        self.assertEqual(coords['t'],model['sim']['transmission_y_um']*1e-6)

    def test_absorption_is_not_forced_to_lossless_closure(self):
        result=planar_fixture(1.5+.2j)
        checks={c['name']:c for c in check_common(result,DEFAULT_TOLERANCES)}
        self.assertNotIn('energy_closure_s',checks)
        self.assertEqual(checks['passive_flux_bound_s']['status'],'PASS')
        self.assertTrue(all(c['status']=='PASS' for c in checks.values()))
        result['quality']['power_closure_s']=1.1
        checks={c['name']:c for c in check_common(result,DEFAULT_TOLERANCES)}
        self.assertEqual(checks['passive_flux_bound_s']['status'],'FAIL')
        result['quality']['absorption_present']=False
        result['quality']['power_closure_s']=.7
        checks={c['name']:c for c in check_common(result,DEFAULT_TOLERANCES)}
        self.assertEqual(checks['energy_closure_s']['status'],'FAIL')

    def test_absorbing_planar_reference_checks_complex_r_and_internal_t(self):
        model=copy.deepcopy(self.base)
        model.pop('n_substrate')
        model.update(substrate_material='Test absorber',geometry_kind='planar_interface',films=[])
        model['sim']['transmission_y_um']=-.6
        result=planar_fixture(1.5+.2j)
        checks=check_planar({'model':model},result,DEFAULT_TOLERANCES)
        self.assertTrue(all(c['status']=='PASS' for c in checks))
        # Interface T substituted for attenuated internal T must fail.
        result['quality']['total_transmitted_p']=1.5*abs(2/(1+1.5+.2j))**2
        checks={c['name']:c for c in check_planar({'model':model},result,DEFAULT_TOLERANCES)}
        self.assertEqual(checks['fresnel_internal_plane_transmission_p']['status'],'FAIL')
        self.assertEqual(checks['fresnel_complex_p']['status'],'PASS')

    def test_tiny_fit_loss_in_raw_lossless_material_retains_strict_closure(self):
        result=planar_fixture(1.45667520+1.52995e-10j,raw_k=0.)
        self.assertFalse(result['quality']['absorption_present'])
        self.assertEqual(result['materials']['roles']['substrate']['k'],1.52995e-10)
        result['quality']['power_closure_s']=.95
        checks={item['name']:item for item in check_common(result,DEFAULT_TOLERANCES)}
        self.assertEqual(checks['energy_closure_s']['status'],'FAIL')
        self.assertNotIn('passive_flux_bound_s',checks)
        genuine=planar_fixture(1.45667520+1.52995e-10j)
        self.assertTrue(genuine['quality']['absorption_present'])

    def test_wrong_flux_direction_is_detected(self):
        result=planar_fixture(1.5)
        result['quality']['signed_transmission_flux_s']*=-1
        checks={c['name']:c for c in check_common(result,DEFAULT_TOLERANCES)}
        self.assertEqual(checks['outgoing_flux_direction_s']['status'],'FAIL')

    def test_legacy_constant_t_keeps_existing_checks_named_t_is_independent(self):
        model=copy.deepcopy(self.base)
        model.update(geometry_kind='planar_interface',films=[],n_substrate=1.5)
        model['sim']['transmission_y_um']=-.6
        result=planar_fixture(1.5)
        # Historical coarse-grid T; a math fixture only, never saved as a run.
        for pol in ('s','p'):
            result['quality'][f'total_transmitted_{pol}']=.949984
            result['quality'][f'signed_transmission_flux_{pol}']=-.949984
            result['quality'][f'power_closure_{pol}']=.04+.949984
        self.assertTrue(all(item['status']=='PASS' for item in check_common(result,DEFAULT_TOLERANCES)))
        constant_checks=check_planar({'model':model},result,DEFAULT_TOLERANCES)
        self.assertTrue(all(item['status']=='PASS' for item in constant_checks))
        self.assertFalse(any('internal_plane_transmission' in item['name'] for item in constant_checks))
        del model['n_substrate']
        model['substrate_material']='Test lossless glass'
        named_checks={item['name']:item for item in check_planar({'model':model},result,DEFAULT_TOLERANCES)}
        self.assertEqual(named_checks['fresnel_internal_plane_transmission_s']['status'],'FAIL')
        self.assertEqual(named_checks['fresnel_internal_plane_transmission_p']['status'],'FAIL')
        self.assertEqual(named_checks['fresnel_internal_plane_transmission_s']['limit'],.005)

    def test_changed_mdf_is_rejected_before_reusing_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test_only.mdf'
            path.write_bytes(b'not-a-real-library: hash test only')
            model=copy.deepcopy(self.base)
            model['material_databases']=[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]
            path.write_bytes(b'changed hash test input')
            with self.assertRaisesRegex(ValueError,'changed after planning'):
                _material_evidence(Path(directory),{'model':model},{})

    def test_named_materials_require_material_evidence(self):
        model=copy.deepcopy(self.base)
        model.pop('n_substrate')
        model['substrate_material']='Test glass'
        with self.assertRaisesRegex(ArtifactError,'missing material resolution'):
            _material_evidence(Path('.'),{'model':model},{})


if __name__=='__main__':
    unittest.main()
