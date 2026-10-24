"""Inspect literal LSF inputs and package text-only parameter sweeps.

This module never imports lumapi, evaluates LSF, opens a project, or starts a
solver.  It is a conservative lexical reader, not a complete LSF parser or a
translator to the runner's geometry schema.  All source offsets are Python
string offsets (Unicode characters), with an exclusive end position.
"""

from __future__ import annotations

import hashlib
import math
import re
from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext
from typing import Any


SCHEMA_VERSION = "lsf-sweep-1.0"
BUNDLE_KIND = "lumerical_lsf_parameter_sweep"
MAX_CASES = 10000
_NUMBER_TEXT = r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
_NUMBER_RE = re.compile(_NUMBER_TEXT)
_SIGNED_NUMBER_RE = re.compile(r"[+-]?" + _NUMBER_TEXT + r"\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_ASSIGNMENT_OPERATORS = {"=", "+=", "-=", "*=", "/=", "^=", "++", "--"}
_PAIRS = {"(": ")", "[": "]", "{": "}"}
# These commands can erase/rebind inputs, or execute/load assignments which the
# lexical reader cannot inspect. Merely warning is insufficient for a sweep:
# the generated override could otherwise be silently discarded before modeling.
_UNTRACKED_WORKSPACE_COMMANDS = {
    "clear", "clearall", "clearexcept", "eval", "feval", "execute",
    "load", "loaddata", "matlabload",
}
_LEADING_CLEANUP_COMMANDS = {
    "clear", "newproject", "switchtolayout", "selectall", "delete", "deleteall",
}
_ANCHOR_FIELDS = (
    "variable", "index_1based", "assignment_start", "start", "end", "literal",
    "line", "editable", "value", "kind",
)


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int
    line: int
    depth: tuple[int, int, int]


def _sha256(script: str) -> str:
    try:
        return hashlib.sha256(script.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise ValueError("LSF 文本包含无法编码为 UTF-8 的字符。") from exc


def _decimal(value: Any, label: str, allow_text: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须是有限数值，不能是 true/false。")
    if isinstance(value, str):
        if not allow_text:
            raise ValueError(f"{label} 必须是数字，不能是字符串。")
        value = value.strip()
        if len(value) > 4096 or not _SIGNED_NUMBER_RE.fullmatch(value):
            raise ValueError(f"{label} 不是有效的十进制数值。")
    elif not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{label} 必须是有限数值。")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
        numeric = float(result)
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise ValueError(f"{label} 不是可表示的有限数值。") from exc
    if not result.is_finite() or not math.isfinite(numeric):
        raise ValueError(f"{label} 不允许 NaN、Infinity 或超出浮点范围的数值。")
    if result != 0 and numeric == 0:
        raise ValueError(f"{label} 太小，转换为数值时会下溢为零。")
    # Zero may carry an arbitrarily large exponent (0e-999999999); normalise it
    # before calculating a Decimal context precision for range generation.
    return Decimal(0) if result == 0 else result


def _number(value: Any, label: str, allow_text: bool = False) -> int | float:
    decimal = _decimal(value, label, allow_text)
    if decimal == decimal.to_integral_value():
        return int(decimal)
    return float(decimal)


def _values(values: Any) -> list[int | float]:
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("扫描值必须是非空数字列表。")
    if len(values) > MAX_CASES:
        raise ValueError(f"扫描点数不能超过 {MAX_CASES}。")
    result: list[int | float] = []
    seen: set[int | float] = set()
    for index, value in enumerate(values, 1):
        number = _number(value, f"第 {index} 个扫描值")
        if number in seen:
            raise ValueError(f"扫描值存在重复或数值精度导致的重合：{number}。")
        seen.add(number)
        result.append(number)
    return result


def parse_values(text: str) -> list[int | float]:
    """Parse comma/whitespace-separated finite values, without evaluation."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请输入扫描值，例如 0, 100, 200。")
    parts = [part for part in re.split(r"[,\s]+", text.strip()) if part]
    if not parts:
        raise ValueError("扫描值不能为空。")
    if len(parts) > MAX_CASES:
        raise ValueError(f"扫描点数不能超过 {MAX_CASES}。")
    return _values([
        _number(part, f"第 {index} 个扫描值", allow_text=True)
        for index, part in enumerate(parts, 1)
    ])


def range_values(
    start: Any,
    stop: Any,
    step: Any = None,
    count: Any = None,
) -> list[int | float]:
    """Create at most 10,000 points using decimal arithmetic.

    Supply exactly one of step/count. Step ranges include stop only when a
    whole number of steps reaches it. Count ranges include both endpoints
    when count > 1; count == 1 returns start. Descending ranges are supported.
    """
    if (step is None) == (count is None):
        raise ValueError("步长 step 和点数 count 必须且只能填写一个。")
    first = _decimal(start, "起点", allow_text=True)
    last = _decimal(stop, "终点", allow_text=True)
    increment = _decimal(step, "步长", allow_text=True) if step is not None else None
    decimals = [first, last] + ([increment] if increment is not None else [])
    exponents = [value.as_tuple().exponent for value in decimals]
    precision = max(
        80,
        max(len(value.as_tuple().digits) for value in decimals) + 20,
        max(value.adjusted() for value in decimals) - min(exponents) + 20,
    )
    with localcontext() as context:
        context.prec = precision
        if increment is not None:
            if increment == 0:
                raise ValueError("步长不能为零。")
            if (last - first) * increment < 0:
                raise ValueError("步长方向与起点、终点不一致。")
            number_of_points = int(
                ((last - first) / increment).to_integral_value(rounding=ROUND_FLOOR)
            ) + 1
            if number_of_points > MAX_CASES:
                raise ValueError(f"扫描点数不能超过 {MAX_CASES}。")
            raw = [first + increment * index for index in range(number_of_points)]
        else:
            requested = _decimal(count, "点数", allow_text=True)
            if requested != requested.to_integral_value() or not 1 <= requested <= MAX_CASES:
                raise ValueError(f"点数必须是 1～{MAX_CASES} 的整数。")
            number_of_points = int(requested)
            if number_of_points == 1:
                raw = [first]
            else:
                increment = (last - first) / (number_of_points - 1)
                raw = [first + increment * index for index in range(number_of_points)]
                raw[-1] = last
    return _values(raw)


def _lex(script: str) -> tuple[list[_Token], dict[int, int], list[str]]:
    tokens: list[_Token] = []
    matching: dict[int, int] = {}
    warnings: list[str] = []
    line_starts = [0] + [match.end() for match in re.finditer(r"\r\n|\r|\n", script)]
    position = 0
    depth = [0, 0, 0]  # braces, parentheses, brackets
    stack: list[tuple[str, int]] = []
    opening_index = {"{": 0, "(": 1, "[": 2}
    closing_index = {"}": 0, ")": 1, "]": 2}
    while position < len(script):
        char = script[position]
        if char.isspace() or char == "\ufeff":
            position += 1
            continue
        if char == "#":
            while position < len(script) and script[position] not in "\r\n":
                position += 1
            continue
        start = position
        line = bisect_right(line_starts, start)
        if char in {"'", '"'}:
            quote = char
            position += 1
            closed = False
            while position < len(script):
                if script[position] == "\\":
                    position += 2
                elif script[position] == quote:
                    position += 1
                    closed = True
                    break
                else:
                    position += 1
            position = min(position, len(script))
            if not closed:
                warnings.append(f"第 {line} 行开始的字符串未闭合；无法可靠定位参数。")
            kind = "string"
        else:
            number = _NUMBER_RE.match(script, position)
            identifier = _IDENTIFIER_RE.match(script, position)
            if number:
                position = number.end()
                kind = "number"
            elif identifier:
                position = identifier.end()
                kind = "identifier"
            else:
                operator = script[position:position + 2]
                position += 2 if operator in {
                    "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "^=",
                    "++", "--", "&&", "||", "~=",
                } else 1
                kind = "punctuation"
        token = _Token(kind, script[start:position], start, position, line, tuple(depth))
        token_index = len(tokens)
        tokens.append(token)
        if kind == "punctuation" and token.text in opening_index:
            stack.append((token.text, token_index))
            depth[opening_index[token.text]] += 1
        elif kind == "punctuation" and token.text in closing_index:
            if not stack or _PAIRS[stack[-1][0]] != token.text:
                warnings.append(f"第 {line} 行的 {token.text} 没有匹配的起始括号。")
            else:
                _, other = stack.pop()
                matching[other] = token_index
                matching[token_index] = other
            dimension = closing_index[token.text]
            depth[dimension] = max(0, depth[dimension] - 1)
    for opening, token_index in stack:
        warnings.append(f"第 {tokens[token_index].line} 行的 {opening} 未闭合。")
    return tokens, matching, warnings


def _statement_start(tokens: list[_Token], index: int) -> bool:
    return index == 0 or tokens[index - 1].text in {";", "{", "}"}


def _writes(tokens: list[_Token], matching: dict[int, int]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or (index and tokens[index - 1].text == "."):
            continue
        cursor = index + 1
        indexed = False
        while cursor < len(tokens):
            if tokens[cursor].text in {"(", "["} and cursor in matching:
                indexed = True
                cursor = matching[cursor] + 1
            elif tokens[cursor].text == "." and cursor + 1 < len(tokens):
                indexed = True
                cursor += 2
            else:
                break
        prefix_mutation = index > 0 and tokens[index - 1].text in {"++", "--"}
        if prefix_mutation or (cursor < len(tokens) and tokens[cursor].text in _ASSIGNMENT_OPERATORS):
            result.setdefault(token.text, []).append({
                "position": token.start,
                "line": token.line,
                "indexed": indexed,
            })
    return result


def _literal_number(
    script: str, tokens: list[_Token], index: int, end: int,
) -> tuple[dict[str, Any] | None, int]:
    if index >= end:
        return None, index
    start_index = index
    sign = ""
    if tokens[index].text in {"+", "-"}:
        sign = tokens[index].text
        index += 1
    if index >= end or tokens[index].kind != "number":
        return None, start_index
    value = _number(sign + tokens[index].text, "源脚本数字字面量", allow_text=True)
    start = tokens[start_index].start
    finish = tokens[index].end
    if "#" in script[start:finish]:
        # A comment between a unary sign and its operand is not part of a
        # contiguous numeric literal. Never remove it during replacement.
        return None, start_index
    return {
        "value": value,
        "start": start,
        "end": finish,
        "literal": script[start:finish],
        "line": tokens[start_index].line,
    }, index + 1


def _literal_rhs(
    script: str, tokens: list[_Token], start: int, end: int,
    matching: dict[int, int],
) -> tuple[list[dict[str, Any]] | None, bool]:
    if start >= end:
        return None, False
    if tokens[start].text != "[":
        number, cursor = _literal_number(script, tokens, start, end)
        return ([number], False) if number is not None and cursor == end else (None, False)
    if matching.get(start) != end - 1:
        return None, True
    cursor = start + 1
    values: list[dict[str, Any]] = []
    separators: set[str] = set()
    while cursor < end - 1:
        number, following = _literal_number(script, tokens, cursor, end - 1)
        if number is None:
            return None, True
        values.append(number)
        cursor = following
        if cursor == end - 1:
            break
        if tokens[cursor].text not in {",", ";"}:
            return None, True
        separators.add(tokens[cursor].text)
        cursor += 1
        if cursor == end - 1:
            return None, True
    # Comma vectors and single-column semicolon vectors are both one-dimensional.
    # A mixture of these separators is conservatively treated as a matrix.
    if not values or len(separators) > 1:
        return None, True
    return values, True


def _string(token: _Token) -> str | None:
    if token.kind != "string" or len(token.text) < 2:
        return None
    quote = token.text[0]
    body = token.text[1:-1]
    return body.replace("\\" + quote, quote).replace("\\\\", "\\")


def _arguments(
    tokens: list[_Token], opening: int, matching: dict[int, int],
) -> list[list[_Token]]:
    if opening not in matching:
        return []
    end = matching[opening]
    start = opening + 1
    cursor = start
    arguments: list[list[_Token]] = []
    while cursor < end:
        if tokens[cursor].text in _PAIRS and cursor in matching:
            cursor = matching[cursor] + 1
            continue
        if tokens[cursor].text == ",":
            arguments.append(tokens[start:cursor])
            start = cursor + 1
        cursor += 1
    arguments.append(tokens[start:end])
    return arguments


def _dimension(tokens: list[_Token], matching: dict[int, int]) -> tuple[str, list[str]]:
    found: set[str] = set()
    unresolved = False
    warnings: list[str] = []
    for index, token in enumerate(tokens[:-1]):
        if token.kind != "identifier" or token.text.lower() not in {"set", "setnamed"}:
            continue
        if tokens[index + 1].text != "(":
            continue
        args = _arguments(tokens, index + 1, matching)
        property_index = 0 if token.text.lower() == "set" else 1
        if len(args) <= property_index + 1 or len(args[property_index]) != 1:
            continue
        property_name = _string(args[property_index][0])
        if property_name is None or property_name.lower() != "dimension":
            continue
        value = args[property_index + 1]
        if len(value) != 1:
            unresolved = True
            continue
        if value[0].kind == "string":
            text = (_string(value[0]) or "").upper()
            if text in {"2D", "3D"}:
                found.add(text)
            else:
                unresolved = True
        elif value[0].kind == "number":
            try:
                numeric = _number(value[0].text, "dimension", allow_text=True)
            except ValueError:
                unresolved = True
                continue
            if numeric in {1, 2}:
                found.add("2D" if numeric == 1 else "3D")
                warnings.append("发现数字 dimension 设置，按 FDTD 的 1=2D、2=3D 显示；未验证该命令作用的对象。")
            else:
                unresolved = True
        else:
            unresolved = True
    if len(found) > 1:
        warnings.append("脚本包含不同的 2D/3D dimension 设置，最终维度取决于执行路径。")
        return "mixed", warnings
    if unresolved:
        warnings.append("部分 dimension 设置使用表达式或未知值，不能静态确定最终维度。")
        return "unknown", warnings
    return next(iter(found)) if found else "unknown", warnings


def _commands(tokens: list[_Token]) -> set[str]:
    names: set[str] = set()
    for index, token in enumerate(tokens[:-1]):
        if token.kind != "identifier":
            continue
        if tokens[index + 1].text == "(" or (
            tokens[index + 1].text == ";" and _statement_start(tokens, index)
        ):
            names.add(token.text.lower())
    return names


def _parameter_region(script, tokens, matching):
    """A literal default block ends before the first derived expression/action."""
    begins = list(re.finditer(r'(?m)^\s*#\s*AH53_PARAMETERS_BEGIN\s*$', script))
    ends = list(re.finditer(r'(?m)^\s*#\s*AH53_PARAMETERS_END\s*$', script))
    explicit = bool(begins or ends)
    if explicit:
        if len(begins) != 1 or len(ends) != 1 or begins[0].end() >= ends[0].start():
            return {'valid': False, 'reason': '参数区标记必须各出现一次且 BEGIN 位于 END 之前。'}
        start, limit = begins[0].end(), ends[0].start()
    else:
        start, limit = 0, len(script)
    # Explicit markers identify a block; they cannot make earlier model/derived
    # state safe. Permit only the same initial cleanup and numeric constants
    # which automatic discovery permits before reaching that block.
    if explicit:
        prefix_default = False
        prefix_cursor = 0
        while prefix_cursor < len(tokens) and tokens[prefix_cursor].start < start:
            token = tokens[prefix_cursor]
            if token.depth != (0, 0, 0) or not _statement_start(tokens, prefix_cursor):
                return {'valid': False, 'reason': '显式参数区之前存在无法确认的作用域或不完整语句。'}
            if (not prefix_default and token.text in _LEADING_CLEANUP_COMMANDS
                    and prefix_cursor + 1 < len(tokens)
                    and tokens[prefix_cursor + 1].text == ';'
                    and tokens[prefix_cursor + 1].end <= begins[0].start()):
                prefix_cursor += 2
                continue
            end = prefix_cursor + 1
            while end < len(tokens) and not (
                    tokens[end].depth == (0, 0, 0) and tokens[end].text in {';', '{'}):
                end += 1
            literal = None
            if (token.kind == 'identifier' and prefix_cursor + 1 < len(tokens)
                    and tokens[prefix_cursor + 1].text == '=' and end < len(tokens)
                    and tokens[end].text == ';' and tokens[end].end <= begins[0].start()):
                try:
                    literal, _ = _literal_rhs(script, tokens, prefix_cursor + 2, end, matching)
                except ValueError:
                    pass
            if literal is None:
                return {'valid': False, 'reason':
                        f'显式参数区之前第 {token.line} 行已有派生表达式、建模、函数定义或其他执行语句；'
                        '参数区必须位于这些语句之前，仅允许顶部清理和纯数值默认常量。'}
            prefix_default = True
            prefix_cursor = end + 1
    seen_default = False
    cursor = 0
    while cursor < len(tokens):
        token = tokens[cursor]
        if token.start < start:
            cursor += 1
            continue
        if token.start >= limit:
            break
        if token.depth != (0, 0, 0) or not _statement_start(tokens, cursor):
            return {'valid': False, 'reason': '参数区不在顶层完整语句边界。'}
        # Leading layout cleanup is allowed only before any default input.
        if (not explicit and not seen_default and token.text in _LEADING_CLEANUP_COMMANDS
                and cursor + 1 < len(tokens) and tokens[cursor + 1].text == ';'):
            cursor += 2
            continue
        end = cursor + 1
        while end < len(tokens) and not (tokens[end].depth == (0, 0, 0) and tokens[end].text in {';', '{'}):
            end += 1
        literal = None
        if (token.kind == 'identifier' and cursor + 1 < len(tokens)
                and tokens[cursor + 1].text == '=' and end < len(tokens)
                and tokens[end].text == ';' and tokens[end].end <= limit):
            try:
                literal, _ = _literal_rhs(script, tokens, cursor + 2, end, matching)
            except ValueError:
                pass
        if literal is None:
            if explicit:
                return {'valid': False, 'reason': f'显式参数区第 {token.line} 行包含表达式或执行语句；只接受数值默认赋值。'}
            limit = token.start
            break
        seen_default = True
        cursor = end + 1
    if not seen_default:
        return {'valid': False, 'reason': '没有找到连续的数值默认参数区；请整理参数区或添加 AH53_PARAMETERS_BEGIN/END 注释标记。'}
    return {'valid': True, 'start': start, 'insertion_offset': limit,
            'mode': 'explicit_markers' if explicit else 'leading_literal_defaults',
            'reason': '覆盖在默认赋值之后、首个派生计算/建模/检查之前执行。'}


def _untracked_after_override(tokens, insertion_offset):
    """Locate calls whose hidden workspace effects invalidate input tracking."""
    found = []
    for index, token in enumerate(tokens):
        if (token.start < insertion_offset or token.kind != 'identifier'
                or token.text.lower() not in _UNTRACKED_WORKSPACE_COMMANDS):
            continue
        following = tokens[index + 1].text if index + 1 < len(tokens) else None
        if following == '(' or (following in {';', None} and _statement_start(tokens, index)):
            found.append(f'{token.text}（第 {token.line} 行）')
    return found


def inspect_lsf(script: str) -> dict[str, Any]:
    """Find replaceable top-level literal scalars and numeric vector elements.

    ``id`` is an opaque, source-specific identifier. Labels use LSF's one-based
    array indexing, e.g. ``film_t_nm(1)``. Duplicate definitions, indexed writes,
    and any lexical structural error make affected candidates unselectable.
    Expressions, function/loop bodies, matrices, and hard-coded set() arguments
    are intentionally not offered as inputs. ``recommended`` only indicates an
    early input-block position; it does not establish a variable's physical role.
    """
    if not isinstance(script, str) or not script.strip():
        raise ValueError("LSF 文本不能为空。")
    digest = _sha256(script)
    tokens, matching, lexical_warnings = _lex(script)
    region = _parameter_region(script, tokens, matching)
    if region['valid']:
        untracked = _untracked_after_override(tokens, region['insertion_offset'])
        if untracked:
            region = {**region, 'valid': False, 'reason':
                      '参数覆盖点之后存在可能清除/重设变量或动态执行未知赋值的命令：'
                      + '、'.join(untracked)
                      + '。无法保证扫描值用于后续建模，禁止暴露；请先把输入清理移到默认赋值之前，'
                      '并将动态执行/载入改为可静态追踪的明确输入和语句。'}
    writes = _writes(tokens, matching)
    parameters: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    input_block_end = len(script)
    for index, token in enumerate(tokens):
        if token.depth != (0, 0, 0) or not _statement_start(tokens, index):
            continue
        is_assignment = (
            token.kind == "identifier" and index + 1 < len(tokens)
            and tokens[index + 1].text == "="
        )
        if not is_assignment and token.text not in {";", "}", "clear", "newproject"}:
            input_block_end = token.start
            break
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.depth != (0, 0, 0):
            continue
        if not _statement_start(tokens, index) or index + 1 >= len(tokens):
            continue
        if tokens[index + 1].text != "=":
            continue
        end = index + 2
        while end < len(tokens):
            if tokens[end].depth == (0, 0, 0) and tokens[end].text in {";", "{"}:
                break
            end += 1
        try:
            values, is_array = _literal_rhs(script, tokens, index + 2, end, matching)
        except ValueError as exc:
            skipped.append({"variable": token.text, "line": token.line, "reason": str(exc)})
            continue
        if values is None:
            skipped.append({
                "variable": token.text,
                "line": token.line,
                "reason": "不是简单数值标量或一维纯数值数组；表达式、矩阵和字符串不自动改写。",
            })
            continue
        other_writes = [entry for entry in writes.get(token.text, []) if entry["position"] != token.start]
        reason = ""
        if lexical_warnings:
            reason = "脚本存在字符串或括号结构问题，无法可靠确认作用域。"
        elif other_writes:
            lines = ", ".join(str(line) for line in sorted({entry["line"] for entry in other_writes}))
            if any(entry["indexed"] for entry in other_writes):
                reason = f"变量还有索引/成员修改或重新赋值（第 {lines} 行），保守禁止自动替换。"
            else:
                reason = f"变量还有其他赋值（第 {lines} 行），保守禁止自动替换。"
        for element, number in enumerate(values, 1):
            array_index = element if is_array else None
            parameter_id = f"{token.text}@{token.start}"
            label = token.text
            if is_array:
                parameter_id += f"[{element}]"
                label += f"({element})"
            parameters.append({
                "id": parameter_id,
                "label": label,
                "variable": token.text,
                "index_1based": array_index,
                "assignment_start": token.start,
                "kind": "array_element" if is_array else "scalar",
                **number,
                "editable": not bool(reason),
                "reason": reason,
                "recommended": token.start < input_block_end,
            })
    dimension, dimension_warnings = _dimension(tokens, matching)
    for parameter in parameters:
        exposed = (region['valid'] and region['start'] <= parameter['assignment_start']
                   and parameter['end'] <= region['insertion_offset'])
        if exposed and region.get('mode') == 'explicit_markers':
            # Earlier reads may already have captured the old value.
            exposed = not any(t.kind == 'identifier' and t.text == parameter['variable']
                              and t.start < region['start'] for t in tokens)
        parameter['exposed'] = bool(exposed)
        parameter['exposure_reason'] = '' if exposed else (
            region.get('reason', '') if not region['valid'] else
            '不在可覆盖的默认参数区内，或在参数区前已有同名使用；请把输入默认值集中到派生计算之前。')
    commands = _commands(tokens)
    warnings = list(lexical_warnings) + dimension_warnings
    warnings.append(
        "这里只做保守的文本参数识别，不执行 LSF，也不证明参数会影响目标结构；"
        "不会自动检查单位、材料、几何约束、参数依赖、求解设置或物理正确性。"
    )
    warnings.append(
        "原脚本的 2D/3D 设置、表达式和默认赋值均保留；扫描值通过明确的参数覆盖区写入。"
        "仅默认参数区内的数值输入可暴露，覆盖发生在派生计算和建模之前。"
    )
    if commands & {"run", "runsweep", "runjobs"}:
        warnings.append("原脚本包含求解命令。生成 JSON/展开 LSF 不会求解；以后在 Lumerical 执行展开脚本时，这些命令仍会生效。")
    if commands & {"save", "saveas", "matlabsave", "write", "exportdataset"}:
        warnings.append("原脚本包含保存/导出命令，原有文件名不会自动更改；日后运行多个案例可能覆盖同名输出。")
    if commands & {"delete", "deleteall", "newproject"}:
        warnings.append("原脚本包含删除对象或新建工程命令；转换过程不执行它们，日后执行展开脚本时会保留这些动作。")
    if "clear" in commands:
        warnings.append("原脚本包含 clear；参数覆盖区必须位于清理和默认赋值之后。")
    if commands & {"eval", "feval", "load", "loaddata", "matlabload"}:
        warnings.append("脚本包含动态执行或载入命令，静态检查不能识别其中隐含的变量改写或工程依赖。")
    scalars = {
        parameter["variable"]: parameter["value"]
        for parameter in parameters
        if parameter["index_1based"] is None and parameter["editable"]
    }
    if scalars.get("film_count") == 0 and any(p["variable"] == "film_t_nm" for p in parameters):
        warnings.append("发现 film_count=0 与 film_t_nm：原始设置未启用膜层，仅改变膜厚通常不会生成膜；请同时设置所需 film_count 和 film_mode。")
    if scalars.get("run_solver") == 1 and commands & {"run", "runsweep", "runjobs"}:
        warnings.append("发现 run_solver=1；原脚本的求解开关处于开启状态，转换工具不会擅自改为 0。")
    for index, token in enumerate(tokens[:-1]):
        if token.text.lower() != "find" or token.kind != "identifier" or tokens[index + 1].text != "(":
            continue
        args = _arguments(tokens, index + 1, matching)
        if "lam" in scalars and len(args) >= 2 and len(args[1]) == 1 and args[1][0].kind == "number":
            warnings.append(
                f"发现 lam 输入以及第 {token.line} 行 find(..., {args[1][0].text}) 的硬编码查找值；"
                "扫描 lam 不会自动改写该后处理常量，请核对波长选择逻辑。"
            )
    if not parameters:
        warnings.append("没有找到符合规则的顶层数字输入；需要先在 LSF 中把所需硬编码值提取为独立输入变量。")
    return {
        "inspector_version": "1.1",
        "script_sha256": digest,
        "dimension": dimension,
        "parameters": parameters,
        "parameter_region": region,
        "warnings": list(dict.fromkeys(warnings)),
        "skipped_assignments": skipped,
    }


def _selection(inspection: dict[str, Any], selected_id: Any, fixed_values: Any) -> tuple[dict[str, Any], dict[str, int | float]]:
    parameters = {parameter["id"]: parameter for parameter in inspection["parameters"]}
    if not isinstance(selected_id, str) or selected_id not in parameters:
        raise ValueError("扫描参数 ID 不存在；请重新读取原始 LSF 并选择参数。")
    selected = parameters[selected_id]
    if not selected["editable"]:
        raise ValueError(f"参数 {selected['label']} 不可修改：{selected['reason']}")
    if not selected.get('exposed'):
        raise ValueError(f"参数 {selected['label']} 不能暴露：{selected.get('exposure_reason', '')}")
    if fixed_values is None:
        fixed_values = {}
    if not isinstance(fixed_values, dict):
        raise ValueError("固定参数必须是参数 ID 到数字值的对象。")
    fixed: dict[str, int | float] = {}
    for parameter_id, value in fixed_values.items():
        if not isinstance(parameter_id, str) or parameter_id not in parameters:
            raise ValueError(f"固定参数 ID 不存在：{parameter_id!r}。")
        if parameter_id == selected_id:
            raise ValueError("同一个参数不能同时作为扫描参数和固定参数。")
        parameter = parameters[parameter_id]
        if not parameter["editable"]:
            raise ValueError(f"固定参数 {parameter['label']} 不可修改：{parameter['reason']}")
        if not parameter.get('exposed'):
            raise ValueError(f"固定参数 {parameter['label']} 不在可覆盖的默认参数区内。")
        fixed[parameter_id] = _number(value, f"固定参数 {parameter['label']}")
    return selected, fixed


def make_bundle(
    script: str,
    filename: str,
    selected_id: str,
    values: list[int | float],
    fixed_values: dict[str, int | float] | None = None,
    name: str = "lsf_scan",
) -> dict[str, Any]:
    """Build a self-contained, JSON-serializable text sweep bundle.

    fixed_values keys and selected_id must be IDs from inspect_lsf(script).
    Neither parameter labels nor legacy AH53 model paths are accepted as IDs.
    """
    if not isinstance(filename, str) or not filename.strip() or "\x00" in filename:
        raise ValueError("必须提供原始 LSF 文件名。")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("扫描名称不能为空。")
    inspection = inspect_lsf(script)
    _, fixed = _selection(inspection, selected_id, fixed_values)
    sweep_values = _values(values)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": BUNDLE_KIND,
        "name": name,
        "source": {
            "filename": filename,
            "script": script,
            "sha256": inspection["script_sha256"],
        },
        "sweep": {"parameter_id": selected_id, "values": sweep_values},
        "fixed_parameters": fixed,
        "inspection": inspection,
    }


def _verified_bundle(bundle: Any) -> tuple[str, dict[str, Any], dict[str, Any], list[int | float], dict[str, int | float]]:
    if not isinstance(bundle, dict):
        raise ValueError("扫描 JSON 的根节点必须是对象。")
    if bundle.get("schema_version") != SCHEMA_VERSION or bundle.get("kind") != BUNDLE_KIND:
        raise ValueError(f"只支持 {SCHEMA_VERSION} 的 LSF 扫描包；不能使用 AH53 2.0 几何配置。")
    if not isinstance(bundle.get("name"), str) or not bundle["name"].strip():
        raise ValueError("扫描名称不能为空。")
    source = bundle.get("source")
    sweep = bundle.get("sweep")
    stored = bundle.get("inspection")
    if not isinstance(source, dict) or not isinstance(sweep, dict) or not isinstance(stored, dict):
        raise ValueError("扫描包缺少 source、sweep 或 inspection 对象。")
    script = source.get("script")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("扫描包内的原始 LSF 文本为空。")
    filename = source.get("filename")
    if not isinstance(filename, str) or not filename.strip() or "\x00" in filename:
        raise ValueError("扫描包缺少有效的原始文件名。")
    actual_hash = _sha256(script)
    if source.get("sha256") != actual_hash or stored.get("script_sha256") != actual_hash:
        raise ValueError("原始 LSF 的 SHA-256 不匹配；请从修改后的源文件重新生成扫描 JSON。")
    inspection = inspect_lsf(script)
    recorded = stored.get("parameters")
    if not isinstance(recorded, list):
        raise ValueError("扫描包缺少原始参数字面量锚点。")
    recorded_map: dict[str, dict[str, Any]] = {}
    for parameter in recorded:
        if not isinstance(parameter, dict) or not isinstance(parameter.get("id"), str):
            raise ValueError("扫描包中的参数锚点格式无效。")
        parameter_id = parameter["id"]
        if parameter_id in recorded_map:
            raise ValueError("扫描包中的参数 ID 重复。")
        recorded_map[parameter_id] = parameter
    actual_parameters = {parameter["id"]: parameter for parameter in inspection["parameters"]}
    if set(recorded_map) != set(actual_parameters):
        raise ValueError("参数列表与原始 LSF 不一致；请重新生成扫描 JSON。")
    for parameter_id, actual in actual_parameters.items():
        recorded_parameter = recorded_map[parameter_id]
        for field in _ANCHOR_FIELDS:
            if field not in recorded_parameter or recorded_parameter[field] != actual[field]:
                raise ValueError(f"参数 {actual['label']} 的原始字面量锚点不匹配（{field}）。")
        if type(recorded_parameter.get("editable")) is not bool:
            raise ValueError("参数 editable 元数据必须是布尔值。")
        for field in ("start", "end", "assignment_start", "line"):
            if type(recorded_parameter.get(field)) is not int:
                raise ValueError(f"参数锚点 {field} 必须是整数。")
        array_index = recorded_parameter.get("index_1based")
        if array_index is not None and type(array_index) is not int:
            raise ValueError("参数数组索引必须是整数或 null。")
        _number(recorded_parameter.get("value"), "参数锚点原值")
        if script[actual["start"]:actual["end"]] != actual["literal"]:
            raise ValueError("参数锚点未能定位到原始数字字面量。")
    selected, fixed = _selection(inspection, sweep.get("parameter_id"), bundle.get("fixed_parameters", {}))
    values = _values(sweep.get("values"))
    return script, inspection, selected, values, fixed


def _literal_text(value: int | float) -> str:
    # repr produces only a decimal/scientific numeric literal after validation.
    return str(value) if isinstance(value, int) else repr(value)


def expand_bundle(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one-based case index, scanned value, and complete expanded LSF.

    Expansion re-inspects the embedded source and validates SHA-256 and all
    literal anchors. It does not trust offsets edited inside JSON. One explicit
    override block is inserted after numeric defaults and before derived/model
    statements; the entire original source, including defaults, is preserved.
    There are no file writes, subprocesses, imports of solver APIs, or LSF
    evaluation.
    """
    script, inspection, selected, values, fixed = _verified_bundle(bundle)
    parameters = {parameter["id"]: parameter for parameter in inspection["parameters"]}
    targets = [selected] + [parameters[parameter_id] for parameter_id in fixed]
    ordered = sorted(targets, key=lambda parameter: parameter["start"])
    insertion = inspection['parameter_region']['insertion_offset']
    cases: list[dict[str, Any]] = []
    for index, value in enumerate(values, 1):
        replacements = dict(fixed)
        replacements[selected["id"]] = value
        changes = []
        lines = ['# AH53_SWEEP_OVERRIDES_BEGIN', '# Defaults above remain unchanged. Case inputs:']
        for parameter in ordered:
            override = replacements[parameter['id']]
            lines.append(f"{parameter['label']} = {_literal_text(override)};")
            changes.append({'parameter': parameter['id'], 'label': parameter['label'],
                            'original_value': parameter['value'], 'value': override,
                            'kind': 'sweep' if parameter['id'] == selected['id'] else 'fixed'})
        lines.append('# AH53_SWEEP_OVERRIDES_END')
        expanded = script[:insertion] + '\n' + '\n'.join(lines) + '\n' + script[insertion:]
        cases.append({"index": index, "value": value, "script": expanded,
                      'changes': changes, 'override_insertion_offset': insertion})
    return cases
