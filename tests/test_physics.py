import numpy as np
import unittest
from ah53runner.solver import proj_fourier
from ah53runner.report import _unwrapped_with_gaps

class TestPhysics(unittest.TestCase):
    def test_fourier_analytic_synthetic(self):
        # Pure synthetic mathematical test, not a Meep/Lumerical result.
        P=16e-6
        x=np.linspace(-P/2,P/2,20001)
        for order in (-5,-1,0,1,5):
            field=(.3+.4j)*np.exp(1j*2*np.pi*order*x/P)
            self.assertLess(abs(proj_fourier(x,field,P,order)-(.3+.4j)),1e-9)
            other=order+1
            self.assertLess(abs(proj_fourier(x,field,P,other)),1e-9)
    def test_phase_unwrap(self):
        raw=np.deg2rad([169,178,-177,-166])
        out=_unwrapped_with_gaps(raw)
        np.testing.assert_allclose(out,[169,178,183,194],atol=1e-8)
    def test_fourier_cell_center_periodic_samples(self):
        # A periodic monitor can omit the duplicated end point. Its full-cell
        # Fourier amplitude must still be independent of the sample origin.
        period=16e-6
        x=(np.arange(128)+0.5)*period/128-period/2
        expected=0.2-0.3j
        field=expected*np.exp(2j*np.pi*5*x/period)
        self.assertLess(abs(proj_fourier(x,field,period,5)-expected),1e-12)
        self.assertLess(abs(proj_fourier(x,field,period,1)),1e-12)

if __name__=='__main__':unittest.main()
