"""Read-only geometry checks over captured CAD properties, with no solver API.

Coordinates in snapshots are SI metres. AH53 parameters ending in ``_nm``
remain nanometres. The caller must authenticate the source before selecting
``ah53_xy``; generic checks cannot infer a script's intended construction.
This is geometric evidence, never a mesh-convergence or electromagnetic test.
"""
from __future__ import annotations

import math
import re
from collections import Counter

LENGTH_TOLERANCE_M = 1e-12
_NM_TOL = LENGTH_TOLERANCE_M * 1e9
_AXES = ('x', 'y', 'z')
_COORDINATES = {axis + suffix for axis in _AXES for suffix in ('', ' min', ' max', ' span')}


def _check(code, status, message, **details):
    return {'code': code, 'status': status, 'message': message, 'details': details}


def _scalar(value):
    while isinstance(value, (list, tuple)) and len(value) == 1:
        value = value[0]
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _finite(value):
    value = _scalar(value)
    return value if value is not None and math.isfinite(value) else None


def _properties(item):
    value = item.get('properties', {})
    return value if isinstance(value, dict) else {}


def _name(item):
    return str(item.get('name') or str(item.get('path', '')).rsplit('::', 1)[-1])


def _identity(item):
    return str(item.get('path') or _name(item))


def _bounds(item, axis):
    props = _properties(item)
    low, high = _finite(props.get(axis + ' min')), _finite(props.get(axis + ' max'))
    if low is not None and high is not None:
        return low, high
    center, span = _finite(props.get(axis)), _finite(props.get(axis + ' span'))
    if center is not None and span is not None:
        return center - span / 2, center + span / 2
    return None


def _enabled(item):
    value = _finite(_properties(item).get('enabled', 1))
    return value is not None and value != 0


def _rectangle(item):
    return str(item.get('type', '')).casefold() in {'rectangle', 'rect', 'rectangular solid'}


def _overlap_pairs(items, axes):
    pairs = []
    for index, left in enumerate(items):
        if not _enabled(left):
            continue
        for right in items[index + 1:]:
            if not _enabled(right):
                continue
            overlaps = []
            for axis in axes:
                a, b = _bounds(left, axis), _bounds(right, axis)
                if a is None or b is None:
                    break
                overlaps.append(min(a[1], b[1]) - max(a[0], b[0]))
            if len(overlaps) == len(axes) and all(v > LENGTH_TOLERANCE_M for v in overlaps):
                pairs.append({'objects': [_identity(left), _identity(right)],
                              'overlap_m': dict(zip(axes, overlaps))})
    return pairs


def _generic(snapshot, objects):
    checks, invalid, unreadable, finite_count = [], [], [], 0
    for item in objects:
        for key, value in _properties(item).items():
            if key not in _COORDINATES:
                continue
            number = _scalar(value)
            record = {'object': _identity(item), 'property': key, 'value': repr(value)}
            if number is None:
                unreadable.append(record)
            elif not math.isfinite(number):
                invalid.append(record)
            else:
                finite_count += 1
    status = 'FAIL' if invalid else ('UNVERIFIED' if unreadable or finite_count == 0 else 'PASS')
    checks.append(_check('generic.finite_coordinates', status,
                         '检查已采集坐标是否为有限数；未读到的属性不按通过处理。',
                         checked_count=finite_count, nonfinite=invalid, unreadable=unreadable))
    names = Counter(_name(item) for item in objects)
    duplicates = {name: count for name, count in names.items() if count > 1}
    checks.append(_check('generic.duplicate_names', 'WARN' if duplicates else 'PASS',
                         '重名可能使按名称选择对象产生歧义；不同组内重名也会列出。', duplicates=duplicates))
    rectangles = [item for item in objects if _rectangle(item)]
    bad, inferred_bad, unavailable = [], [], []
    for item in rectangles:
        bounds = {axis: _bounds(item, axis) for axis in _AXES}
        if any(value is None for value in bounds.values()):
            unavailable.append(_identity(item))
            continue
        if any(value[1] <= value[0] for value in bounds.values()):
            record = {'object': _identity(item), 'bounds_m': bounds}
            (inferred_bad if item.get('type_inferred') else bad).append(record)
    status = 'FAIL' if bad else ('WARN' if inferred_bad else ('UNVERIFIED' if unavailable or not rectangles else 'PASS'))
    checks.append(_check('generic.rectangle_dimensions', status,
                         '仅检查矩形实体候选的三向尺寸；源、监视器和二维采样面的零厚度允许。',
                         invalid=bad, inferred_type_warnings=inferred_bad, unavailable=unavailable,
                         rectangle_count=len(rectangles)))
    if len(rectangles) <= 1000:
        overlaps = _overlap_pairs(rectangles, _AXES)
        checks.append(_check('generic.bounding_box_overlap', 'WARN' if overlaps else 'PASS',
                             '包围盒相交仅提示检查，材料优先级和有意重叠可能合法。',
                             overlaps=overlaps[:100], overlap_count=len(overlaps)))
    else:
        checks.append(_check('generic.bounding_box_overlap', 'UNVERIFIED',
                             '矩形候选超过 1000 个，未执行两两包围盒比较。'))
    unverified = snapshot.get('unverified', [])
    inferred = [_identity(item) for item in objects if item.get('type_inferred')]
    other = [_identity(item) for item in objects
             if str(item.get('type', '')).casefold() in {'', 'unknown'}]
    if unverified or inferred or other:
        checks.append(_check('generic.snapshot_limits', 'UNVERIFIED',
                             '部分属性未读取或对象类型由属性推断；不能据此宣称完整结构正确。',
                             unverified=unverified, inferred_types=inferred, unknown_types=other))
    return checks


def _close(a, b, tolerance=LENGTH_TOLERANCE_M):
    return a is not None and abs(a - b) <= tolerance


def _material(item, expected):
    props = _properties(item)
    material = props.get('material')
    index = _finite(props.get('index'))
    if material is None or index is None:
        return 'UNVERIFIED', {'material': material, 'index': props.get('index'), 'expected_n': expected}
    correct = str(material).strip().casefold() == '<object defined dielectric>' and _close(index, expected, 1e-9)
    return ('PASS' if correct else 'FAIL'), {'material': material, 'index': index, 'expected_n': expected}


def _coordinate_match(item, expected):
    missing, differences = [], []
    for axis, wanted in expected.items():
        actual = _bounds(item, axis)
        if actual is None:
            missing.append(axis)
        elif any(not _close(a, b) for a, b in zip(actual, wanted)):
            differences.append({'axis': axis, 'actual_m': actual, 'expected_m': wanted})
    return ('FAIL' if differences else ('UNVERIFIED' if missing else 'PASS')), {
        'object': _identity(item), 'missing_axes': missing, 'differences': differences}


def _unique(values):
    merged = []
    for value in sorted(values):
        if not merged or value - merged[-1] > _NM_TOL:
            merged.append(value)
    return merged


def _expected_films(period, pitch, width, height, modes, thicknesses, indices):
    """Construct an independent material partition using periodic interval sets.

    The formula is a max-envelope dilation of the preceding height function.
    No CAD object count, segment names, script interfaces, or captured top
    values are used to calculate the expected cross section.
    """
    half = period / 2
    ridges = [(center - width / 2, center + width / 2) for center in (-pitch, 0, pitch)]
    edges = _unique([-half, half, *[edge for pair in ridges for edge in pair]])
    surface = [(left, right, height if any(a < (left + right) / 2 < b for a, b in ridges) else 0.)
               for left, right in zip(edges, edges[1:])]
    regions = []
    for layer, (mode, thickness, index) in enumerate(zip(modes, thicknesses, indices), 1):
        if mode == 0 or (mode == 1 and thickness == 0):
            continue
        if mode == 2:
            top = max(segment[2] for segment in surface) + thickness
            new_surface = [(left, right, top) for left, right, _ in surface]
        else:
            # Changes can only occur at an old edge or an edge shifted by +/-t.
            # Modular wrapping is independent of the script's one-period branch.
            edges = _unique([-half, half, *[edge for s in surface for edge in s[:2]],
                             *[((edge + shift + half) % period) - half
                               for s in surface for edge in s[:2]
                               for shift in (-thickness, thickness)]])
            new_surface = []
            for left, right in zip(edges, edges[1:]):
                middle = (left + right) / 2
                if thickness >= period / 2:
                    top = max(segment[2] for segment in surface) + thickness
                else:
                    nearby = [level for lo, hi, level in surface
                              if any(middle + thickness > lo + repeat * period
                                     and middle - thickness < hi + repeat * period
                                     for repeat in (-1, 0, 1))]
                    top = max(nearby) + thickness
                new_surface.append((left, right, top))
        for left, right, top in new_surface:
            # A new interval never crosses an old surface edge.
            middle = (left + right) / 2
            old = next(level for lo, hi, level in surface if lo <= middle <= hi)
            if top - old > _NM_TOL:
                regions.append({'x1': left, 'x2': right, 'y1': old, 'y2': top,
                                'n': index, 'layer': layer})
        surface = new_surface
    return regions, max(segment[2] for segment in surface)


def _partition_difference(expected, actual, max_cells=200000):
    """Compare material occupancy in every cell of the combined XY partition."""
    edges = _unique([value for item in expected + actual for value in (item['x1'], item['x2'])])
    checked, mismatch_area, examples = 0, 0., []
    for left, right in zip(edges, edges[1:]):
        middle = (left + right) / 2
        wanted = [item for item in expected if item['x1'] < middle < item['x2']]
        found = [item for item in actual if item['x1'] < middle < item['x2']]
        vertical = _unique([value for item in wanted + found for value in (item['y1'], item['y2'])])
        for bottom, top in zip(vertical, vertical[1:]):
            checked += 1
            if checked > max_cells:
                return None, {'checked_cells': checked - 1, 'cell_limit': max_cells}
            y = (bottom + top) / 2
            a = [item['n'] for item in wanted if item['y1'] < y < item['y2']]
            b = [item['n'] for item in found if item['y1'] < y < item['y2']]
            equal = len(a) == len(b) and all(_close(x, z, 1e-9) for x, z in zip(sorted(a), sorted(b)))
            if not equal:
                mismatch_area += (right - left) * (top - bottom)
                if len(examples) < 20:
                    examples.append({'x_nm': [left, right], 'y_nm': [bottom, top],
                                     'expected_indices': a, 'actual_indices': b})
    return mismatch_area == 0, {'checked_cells': checked, 'mismatch_area_nm2': mismatch_area,
                               'mismatch_examples': examples}


def _ah53(snapshot, objects, parameters):
    checks = []
    required = ('P_nm', 'pitch_nm', 'CD_nm', 'h_nm', 'n_sub', 'n_ridge',
                'film_count', 'film_mode', 'film_t_nm', 'film_n')
    missing = [key for key in required if key not in parameters]
    if missing:
        return [_check('ah53.parameters', 'UNVERIFIED', '缺少独立几何预期所需的参数。', missing=missing)]
    numeric = {key: _finite(parameters[key]) for key in required[:7]}
    invalid = [key for key, value in numeric.items() if value is None]
    count = numeric.get('film_count')
    if count is not None and (count != int(count) or not 0 <= count <= 3):
        invalid.append('film_count')
    arrays = {key: parameters[key] for key in ('film_mode', 'film_t_nm', 'film_n')}
    for key, value in arrays.items():
        if not isinstance(value, (list, tuple)) or count is None or len(value) < count:
            invalid.append(key)
    if invalid:
        return [_check('ah53.parameters', 'FAIL', 'AH53 参数必须有限，膜层数为 0～3 的整数且数组足够长。', invalid=invalid)]
    period, pitch, width, height = (numeric[key] for key in required[:4])
    count = int(count)
    modes = [_finite(v) for v in arrays['film_mode'][:count]]
    thicknesses = [_finite(v) for v in arrays['film_t_nm'][:count]]
    indices = [_finite(v) for v in arrays['film_n'][:count]]
    if (any(value <= 0 for value in (period, pitch, width, height, numeric['n_sub'], numeric['n_ridge']))
            or width >= pitch or pitch + width / 2 >= period / 2):
        invalid.append('base_geometry_or_index')
    for i, (mode, thickness, index) in enumerate(zip(modes, thicknesses, indices), 1):
        if mode not in (0, 1, 2) or thickness is None or thickness < 0 or index is None or index <= 0:
            invalid.append(f'film_{i}')
    checks.append(_check('ah53.parameters', 'FAIL' if invalid else 'PASS',
                         '检查三条纹参数、启用膜层数、膜模式、厚度和常数折射率。', invalid=invalid))
    if invalid:
        return checks
    selected = str(parameters.get('__selected_parameter__', ''))
    selected_layer = re.fullmatch(r'film_(?:t_nm|n|mode)\((\d+)\)', selected)
    if selected_layer:
        slot = int(selected_layer.group(1))
        active = 1 <= slot <= count and modes[slot - 1] != 0
        checks.append(_check('ah53.selected_film_enabled', 'PASS' if active else 'FAIL',
                             '被扫描膜层必须在 film_count 范围内且 mode 不为 0。',
                             parameter=selected, layer=slot, film_count=count,
                             mode=modes[slot - 1] if 1 <= slot <= count else None))
    by_name = {}
    for item in objects:
        by_name.setdefault(_name(item), []).append(item)
    expected_names = ['substrate', 'ridge_1', 'ridge_2', 'ridge_3']
    bad_names = [name for name in expected_names if len(by_name.get(name, [])) != 1]
    checks.append(_check('ah53.base_objects', 'FAIL' if bad_names else 'PASS',
                         '应有一个基底和恰好三根指定条纹。', missing_or_duplicated=bad_names))
    geometry = [item for item in objects if _name(item) in expected_names
                or re.fullmatch(r'film_seg_\d+', _name(item))]
    unread = [name for name in expected_names if len(by_name.get(name, [])) != 1]
    if unread:
        return checks
    span = _finite(parameters.get('z_geometry_span', 1e-6))
    if span is None or span <= 0:
        checks.append(_check('ah53.z_span', 'FAIL', 'Z 向条纹跨度必须为正的有限长度。'))
        return checks
    for name in expected_names:
        item = by_name[name][0]
        substrate = name == 'substrate'
        center = (int(name[-1]) - 2) * pitch if not substrate else 0.
        expected = {'x': ((-period / 2 - 500) * 1e-9, (period / 2 + 500) * 1e-9)
                          if substrate else ((center - width / 2) * 1e-9, (center + width / 2) * 1e-9),
                    'y': (-1.6e-6, 0.) if substrate else (0., height * 1e-9),
                    'z': (-span / 2, span / 2)}
        status, details = _coordinate_match(item, expected)
        checks.append(_check('ah53.geometry.' + name, status, '核对独立计算的对象位置和尺寸。', **details))
        status, details = _material(item, numeric['n_sub' if substrate else 'n_ridge'])
        checks.append(_check('ah53.material.' + name, status,
                             '此源脚本使用对象定义的常数折射率；核对实际材料与 n。', **details))
    disabled = [_identity(item) for item in geometry if not _enabled(item)]
    unread_enabled = [_identity(item) for item in geometry if 'enabled' not in _properties(item)]
    rotations = [{'object': _identity(item), 'property': key, 'value': value}
                 for item in geometry for key, value in _properties(item).items()
                 if key in {'rotation 1', 'rotation 2', 'rotation 3', 'rotation1', 'rotation2', 'rotation3'}
                 and (_finite(value) is None or abs(_finite(value)) > 1e-9)]
    checks.append(_check('ah53.geometry_enabled_unrotated',
                         'FAIL' if disabled or rotations else ('UNVERIFIED' if unread_enabled else 'PASS'),
                         '建模预检应保留启用、未旋转的 AH53 实体。', disabled=disabled,
                         unread_enabled=unread_enabled, unexpected_rotations=rotations))
    overlaps = _overlap_pairs(geometry, ('x', 'y')) if len(geometry) <= 1000 else None
    checks.append(_check('ah53.disjoint_material_regions',
                         'UNVERIFIED' if overlaps is None else ('FAIL' if overlaps else 'PASS'),
                         '已知 AH53 分块应只共享边界，不应有正面积材料交叠。', overlaps=overlaps))
    expected_films, expected_top = _expected_films(period, pitch, width, height, modes, thicknesses, indices)
    actual_top_values = [_bounds(item, 'y')[1] * 1e9 for item in geometry
                         if _name(item) != 'substrate' and _enabled(item) and _bounds(item, 'y') is not None]
    missing_top = [_identity(item) for item in geometry if _bounds(item, 'y') is None]
    actual_top = max(actual_top_values) if actual_top_values else None
    checks.append(_check('ah53.stack_top',
                         'UNVERIFIED' if missing_top or actual_top is None else
                         ('PASS' if _close(actual_top, expected_top, _NM_TOL) else 'FAIL'),
                         '叠层顶部按启用膜层独立计算；平坦化 0 nm 填沟槽，包覆 0 nm 不添加材料。',
                         expected_top_nm=expected_top, actual_top_nm=actual_top, unavailable=missing_top))
    films = [item for item in geometry if _name(item).startswith('film_seg_')]
    actual_films, unavailable, material_mismatch, z_mismatch = [], [], [], []
    for item in films:
        if not _enabled(item):
            continue
        x, y = _bounds(item, 'x'), _bounds(item, 'y')
        props = _properties(item)
        index = _finite(props.get('index'))
        if x is None or y is None or index is None or 'material' not in props:
            unavailable.append(_identity(item))
            continue
        if str(props['material']).strip().casefold() != '<object defined dielectric>':
            material_mismatch.append(_identity(item))
        z_status, z_details = _coordinate_match(item, {'z': (-span / 2, span / 2)})
        if z_status != 'PASS':
            (unavailable if z_status == 'UNVERIFIED' else z_mismatch).append(_identity(item))
        actual_films.append({'x1': x[0] * 1e9, 'x2': x[1] * 1e9,
                             'y1': y[0] * 1e9, 'y2': y[1] * 1e9, 'n': index})
    if unavailable:
        status, comparison = 'UNVERIFIED', {'unavailable': unavailable}
    else:
        equal, comparison = _partition_difference(expected_films, actual_films)
        status = 'UNVERIFIED' if equal is None else ('PASS' if equal else 'FAIL')
    if material_mismatch or z_mismatch:
        status = 'FAIL'
    checks.append(_check('ah53.film_cross_section', status,
                         '独立比较全部膜层的 XY 材料分区、侧壁覆盖和空隙；不是只比较最高点。',
                         **comparison, wrong_material=material_mismatch, wrong_z_span=z_mismatch,
                         expected_region_count=len(expected_films), actual_region_count=len(actual_films),
                         method='periodic interval-envelope material partition',
                         approximation='square-corner conformal geometry; not deposition physics'))
    # The known XY script keeps all sources/monitors fixed above the structure.
    sampling_names = ('source', 'R', 'INC1', 'INC2')
    outside, below, unavailable = [], [], []
    fdtd = by_name.get('FDTD', [])
    fdtd_box = {axis: _bounds(fdtd[0], axis) for axis in ('x', 'y')} if len(fdtd) == 1 else {}
    if len(fdtd) != 1 or any(value is None for value in fdtd_box.values()):
        unavailable.append('FDTD')
    for name in sampling_names:
        matches = by_name.get(name, [])
        if len(matches) != 1:
            unavailable.append(name)
            continue
        item = matches[0]
        vertical = _bounds(item, 'y')
        y = _finite(_properties(item).get('y'))
        if vertical is None and y is not None:
            vertical = (y, y)
        if vertical is None:
            unavailable.append(name + '.y')
            continue
        if vertical[0] <= expected_top * 1e-9 + LENGTH_TOLERANCE_M:
            below.append({'object': name, 'y_min_m': vertical[0]})
        for axis, limits in fdtd_box.items():
            extent = vertical if axis == 'y' else _bounds(item, axis)
            if limits is None or extent is None:
                unavailable.append(name + '.' + axis)
            elif extent[0] < limits[0] - LENGTH_TOLERANCE_M or extent[1] > limits[1] + LENGTH_TOLERANCE_M:
                outside.append({'object': name, 'axis': axis, 'extent_m': extent, 'fdtd_m': limits})
    checks.append(_check('ah53.source_monitor_clearance',
                         'FAIL' if below else ('UNVERIFIED' if unavailable else 'PASS'),
                         '源和反射/入射监视器必须在预期结构顶部上方；这些位置不会自动随膜厚移动。',
                         below_or_touching=below, unavailable=unavailable))
    checks.append(_check('ah53.fdtd_contains_sources_monitors',
                         'FAIL' if outside else ('UNVERIFIED' if unavailable else 'PASS'),
                         '检查 XY 仿真区域包含源和监视器；基底跨出区域是设计行为，不作为错误。',
                         outside=outside, unavailable=unavailable))
    return checks


def evaluate_snapshot(snapshot, parameters, profile='generic'):
    """Return explicit PASS/FAIL/WARN/UNVERIFIED geometry evidence records."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('objects'), list):
        return [_check('snapshot.format', 'FAIL', 'snapshot.objects 必须是对象列表。')]
    objects = snapshot['objects']
    if any(not isinstance(item, dict) for item in objects):
        return [_check('snapshot.format', 'FAIL', '对象记录必须为字典。')]
    if not objects:
        return [_check('snapshot.empty', 'FAIL', '建模快照中没有对象。')]
    checks = _generic(snapshot, objects)
    if profile in {'ah53_xy', 'ah53-xy'}:
        if not isinstance(parameters, dict):
            checks.append(_check('ah53.parameters', 'FAIL', 'parameters 必须为字典。'))
        else:
            checks.extend(_ah53(snapshot, objects, parameters))
    elif profile != 'generic':
        checks.append(_check('snapshot.profile', 'UNVERIFIED', '未知检查配置，已仅执行通用检查。', profile=profile))
    else:
        checks.append(_check('generic.intended_geometry', 'UNVERIFIED',
                             '任意 LSF 的预期尺寸、材料与层关系需专用规则；通用检查不证明设计意图。'))
    return checks


def compare_snapshots(snapshot_a, snapshot_b):
    """List object additions/removals and property changes; [] means unchanged.

    Paths disambiguate groups; occurrence suffixes preserve duplicate objects.
    Numeric coordinates use the same absolute geometric tolerance as checks.
    """
    def indexed(snapshot):
        result, counts = {}, Counter()
        for item in snapshot.get('objects', []):
            path = _identity(item)
            counts[path] += 1
            result[(path, counts[path])] = item
        return result
    left, right = indexed(snapshot_a), indexed(snapshot_b)
    changes = []
    for key in sorted(left.keys() | right.keys()):
        path, occurrence = key
        common = {'path': path, 'occurrence': occurrence}
        if key not in left or key not in right:
            changes.append({**common, 'kind': 'added' if key in right else 'removed',
                            'property': None, 'before': left.get(key), 'after': right.get(key)})
            continue
        a = dict(_properties(left[key]), __type__=left[key].get('type'))
        b = dict(_properties(right[key]), __type__=right[key].get('type'))
        for prop in sorted(a.keys() | b.keys()):
            before, after = a.get(prop), b.get(prop)
            x, y = _finite(before), _finite(after)
            if x is not None and y is not None:
                equal = _close(x, y, LENGTH_TOLERANCE_M if prop in _COORDINATES else 1e-12)
            else:
                equal = before == after
            if not equal:
                changes.append({**common, 'kind': 'changed', 'property': prop,
                                'before': before, 'after': after})
    return changes
