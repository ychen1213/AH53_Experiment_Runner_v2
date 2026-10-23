"""2D XY geometries. Every length in nm unless a property says otherwise.

The optical solver uses X for period, Y for height, Z for invariant stripes.
Planarized fill and rectilinear conformal approximations are geometrical,
NOT deposition physics. Do not mistake 2D sidewall slopes for 3D LER.
"""
from __future__ import annotations
import copy
from dataclasses import dataclass, asdict
from typing import List, Optional
from .materials import material_spec

@dataclass
class Shape:
    name: str
    material: str
    index_n: Optional[float]
    x1_nm: float
    x2_nm: float
    y1_nm: float
    y2_nm: float
    # Vertices for a nonvertical ridge; leave empty for rectangles.
    vertices_nm: Optional[list] = None
    # Preserve the material role above; database selectors are separate.
    material_name: Optional[str] = None


def _shape_material(model, role):
    spec = material_spec(model, role)
    return (spec.get('n'), spec.get('name'))


def _unique_sorted(vs, tol=1e-6):
    out = []
    for x in sorted(vs):
        if not out or x - out[-1] > tol:
            out.append(x)
    return out


def _top_at(x, boundaries, heights):
    for j in range(len(heights)):
        if boundaries[j] - 1e-6 <= x <= boundaries[j+1] + 1e-6:
            return heights[j]
    raise ValueError(f'No topography segment at x={x}')


def build_geometry(cfg: dict) -> tuple[list[Shape], float, dict]:
    c = copy.deepcopy(cfg)
    L = float(c['period_nm'])
    kind = c.get('geometry_kind', 'ah53')
    if kind == 'planar_interface':
        n_sub, substrate_name = _shape_material(c, 'substrate')
        if not L > 0:
            raise ValueError('Planar interface requires positive period and substrate index.')
        if c.get('films'):
            raise ValueError('Planar-interface benchmark must not contain films.')
        shapes = [Shape('substrate', 'substrate', n_sub,
                        -L/2-500, L/2+500, -1600, 0,
                        material_name=substrate_name)]
        return shapes, 0.0, {'geometry_kind': kind, 'period_nm': L,
                            'interface_y_nm': 0.0, 'shape_count': 1,
                            'top_nm': 0.0, 'film_history': [],
                            'coordinate': 'XY; incoming -Y; invariant Z'}
    if kind != 'ah53':
        raise ValueError(f'Unsupported geometry_kind: {kind}')
    p = float(c['child_pitch_nm'])
    cd = float(c['ridge_cd_nm'])
    h = float(c['ridge_height_nm'])
    n_sub, substrate_name = _shape_material(c, 'substrate')
    n_ridge, ridge_name = _shape_material(c, 'ridge')
    offsets = c.get('ridge_center_offsets_nm', [0.0,0.0,0.0])
    shift = float(c.get('ridge_shift_nm',0.0))
    left_inset = float(c.get('ridge_top_left_inset_nm',0.0))
    right_inset = float(c.get('ridge_top_right_inset_nm',0.0))
    films = c.get('films', [])
    if L <= 0 or p <= 0 or cd <= 0 or h <= 0 or cd >= p:
        raise ValueError('Require period,pitch,CD,height positive and CD < child pitch.')
    if len(offsets) != 3:
        raise ValueError('ridge_center_offsets_nm must contain three values.')
    if left_inset < 0 or right_inset < 0 or left_inset+right_inset >= cd:
        raise ValueError('Top insets must be non-negative and sum to less than CD.')
    if (left_inset > 0 or right_inset > 0) and films:
        raise ValueError('First version: nonvertical sidewalls + films not supported together. Separate experiments.')
    centers = [(-1+i)*p + shift + float(offsets[i]) for i in range(3)]
    lows = [xc-cd/2 for xc in centers]
    highs = [xc+cd/2 for xc in centers]
    if not all(-L/2 < lo < hi < L/2 for lo,hi in zip(lows,highs)):
        raise ValueError('Ridges cross mother-period boundary; wrap support is not implemented.')
    if not (highs[0] < lows[1] and highs[1] < lows[2]):
        raise ValueError('Overlapping or out-of-order ridges.')
    out = [Shape('substrate','substrate',n_sub, -L/2-500, L/2+500, -1600,0,
                 material_name=substrate_name)]
    for i,(lo,hi) in enumerate(zip(lows,highs),start=1):
        vertices = None
        if left_inset or right_inset:
            vertices = [[lo,0],[hi,0],[hi-right_inset,h],[lo+left_inset,h]]
        out.append(Shape(f'ridge_{i}','ridge',n_ridge,lo,hi,0,h,vertices,ridge_name))
    edges = [-L/2]
    tops = []
    for lo,hi in zip(lows,highs):
        edges.extend([lo,hi])
    edges.append(L/2)
    for j in range(len(edges)-1):
        xc = (edges[j]+edges[j+1])*0.5
        tops.append(h if any(lo<xc<hi for lo,hi in zip(lows,highs)) else 0.0)
    original_film_objects=0
    history=[]
    for fid,film in enumerate(films,1):
        mode = str(film.get('mode','none')).lower()
        t = float(film.get('thickness_nm',0))
        n, film_name = _shape_material(c, f'film_{fid}')
        if mode not in ('none','conformal','planarized') or t<0:
            raise ValueError(f'Invalid film configuration #{fid}: {film}')
        if mode == 'none':
            continue
        material = f'film_{fid}'
        old_edges,old_tops = edges[:],tops[:]
        if mode == 'planarized':
            new_y=max(old_tops)+t
            # Even t=0 *fills* the grooves; not a bare-Mark configuration.
            for j,old_y in enumerate(old_tops):
                if new_y > old_y + 1e-9:
                    original_film_objects+=1
                    out.append(Shape(f'film_{fid}_seg_{original_film_objects}',material,n,
                                     edges[j],edges[j+1],old_y,new_y,material_name=film_name))
            tops=[new_y]*len(tops)
        else:
            if t == 0:
                # Zero-thickness conformal is a true no-op.
                continue
            candidate = old_edges[:]
            for x in old_edges[1:-1]:
                for delta in (-t,t):
                    shifted=x+delta
                    while shifted < -L/2: shifted+=L
                    while shifted > L/2: shifted-=L
                    candidate.append(shifted)
            edges=_unique_sorted(candidate)
            new_tops=[]
            for j in range(len(edges)-1):
                xl,xr=edges[j],edges[j+1]
                xc=(xl+xr)*0.5
                old_y=_top_at(xc,old_edges,old_tops)
                yh=old_y
                for rep in (-1,0,1):
                    for k0,old_h in enumerate(old_tops):
                        seg_l=old_edges[k0]+rep*L
                        seg_r=old_edges[k0+1]+rep*L
                        if xc-t<seg_r and xc+t>seg_l:
                            yh=max(yh,old_h)
                new_y=yh+t
                new_tops.append(new_y)
                if new_y>old_y+1e-9:
                    original_film_objects+=1
                    out.append(Shape(f'film_{fid}_seg_{original_film_objects}',material,n,xl,xr,old_y,new_y,
                                     material_name=film_name))
            tops=new_tops
        history.append({'film_index':fid,'mode':mode,'thickness_nm':t,'n':n,
                        'material_name':film_name,'resulting_top_nm':max(tops)})
    final_top = max(tops)
    return out,final_top,{'period_nm':L,'ridge_centers_nm':centers,'film_history':history,
                          'shape_count':len(out),'top_nm':final_top,
                          'coordinate':'XY; incoming -Y; invariant Z'}


def shapes_to_json(shapes):
    return [asdict(s) for s in shapes]
