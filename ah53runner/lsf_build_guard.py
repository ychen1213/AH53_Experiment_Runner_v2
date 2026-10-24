"""Conservatively extract an auditable, solver-free native LSF build prefix.

This module does not execute LSF or import lumapi. It preserves the original
prefix byte-for-byte as Unicode text; it never deletes individual ``run`` calls
or rewrites solver switches. Unsupported constructs require source cleanup or
an earlier explicit build boundary, rather than a guessed interpretation.
"""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath, PureWindowsPath
import re

from .lsf_config import _arguments, _lex, _statement_start, _string


# Deliberately finite: adding another native command requires reviewing whether
# it can run simulations, execute scripts, or read/write outside the case.
_MODEL_COMMANDS = {
    "clear", "newproject", "switchtolayout", "select", "selectall", "unselectall",
    "selectpartial", "delete", "deleteall", "copy", "groupscope", "addtogroup",
    "addgroup", "addrect", "addpoly", "addcircle", "addsphere", "addellipsoid",
    "addring", "addpyramid", "addcone", "addwaveguide", "addtriangle",
    "addfdtd", "addmesh", "addplane", "addgaussian", "adddipole", "addmode",
    "addpower", "addprofile", "adddftmonitor", "addindex", "addtime", "addmovie",
    "set", "setnamed", "setglobalsource", "setglobalmonitor", "setmaterial",
    "get", "getnamed", "getnumber", "getnamednumber", "getmaterial", "materialexists",
    "addmaterial", "deletematerial", "copymaterial", "importmaterial", "save",
}
_MATH_FUNCTIONS = {
    "abs", "acos", "acosh", "angle", "asin", "asinh", "atan", "atan2", "atanh",
    "ceil", "conj", "cos", "cosh", "cumprod", "cumsum", "diff", "exp", "find",
    "flip", "floor", "imag", "length", "linspace", "log", "log10", "logspace",
    "matrix", "max", "mean", "median", "min", "mod", "num2str", "ones", "pinch",
    "prod", "real", "reshape", "round", "sign", "sin", "sinh", "size", "sort",
    "sqrt", "std", "sum", "tan", "tanh", "transpose", "zeros",
}
_CONTROL = {"if", "else", "for", "while", "break", "continue"}
_CONSTANTS = {"pi", "c", "eps0", "mu0", "i", "true", "false"}
_FORBIDDEN = {
    "run", "runparallel", "runsweep", "runjobs", "runanalysis", "runsetup", "mesh",
    "eval", "feval", "execute", "system", "system2", "load", "loaddata",
    "matlabload", "matlab", "python", "appevalscript", "appopen", "addjob",
    "function", "return", "addstructuregroup", "addanalysisgroup", "adduserprop",
    "cd", "pwd", "write", "rm", "del", "mv", "cp", "mkdir", "rmdir",
    "clearexcept", "clearall", "setresource",
}
_PROPERTY_COMMANDS = {
    "set": 0, "setnamed": 1, "setglobalsource": 0, "setglobalmonitor": 0,
    "setmaterial": 1,
}
_MARKER = re.compile(r"(?m)^[ \t]*#[ \t]*AH53_BUILD_ONLY_END[ \t]*\r?$")


def _fail(message: str) -> None:
    raise ValueError("无法安全生成仅建模脚本：" + message)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line(script: str, offset: int) -> int:
    return len(re.findall(r"\r\n|\r|\n", script[:offset])) + 1


def _relative_file(value: str, command: str) -> str:
    relative = value.replace("\\", "/")
    windows, posix = PureWindowsPath(relative), PurePosixPath(relative)
    if (not relative or windows.drive or windows.root or posix.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or any(char in relative for char in '<>:"|?*')
            or any(ord(char) < 32 for char in relative)):
        _fail(f"{command} 只允许明确的案例目录内相对文件路径，不能使用绝对路径或 ..：{value!r}。")
    for part in relative.split("/"):
        if part.endswith((".", " ")) or re.fullmatch(
                r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part):
            _fail(f"{command} 的文件路径包含不支持的 Windows 名称：{value!r}。")
    return posix.as_posix()


def _boundary(script, tokens, matching):
    markers = list(_MARKER.finditer(script))
    if markers:
        if len(markers) != 1:
            _fail("AH53_BUILD_ONLY_END 必须且只能出现一次。")
        marker = markers[0]
        if any(token.kind == "string" and token.start <= marker.start() < token.end
               for token in tokens):
            _fail("AH53_BUILD_ONLY_END 位于字符串中，必须使用独占一行的真实注释。")
        prefix = script[:marker.start()]
        before, _, problems = _lex(prefix)
        if problems or not before or before[-1].text not in {";", "}"}:
            _fail("AH53_BUILD_ONLY_END 必须位于顶层完整语句之后，不能切断赋值、表达式或作用域。")
        after = next((token for token in tokens if token.start >= marker.end()), None)
        if after is not None and after.text == "else":
            _fail("AH53_BUILD_ONLY_END 不能放在同一 if/else 语句的两个分支之间。")
        return marker.start(), "explicit_build_only_end"
    candidates = []
    for index, token in enumerate(tokens):
        if (token.text != "if" or token.depth != (0, 0, 0)
                or not _statement_start(tokens, index)
                or index + 1 >= len(tokens) or tokens[index + 1].text != "("):
            continue
        closing = matching.get(index + 1)
        if closing is None:
            continue
        condition = [entry.text for entry in tokens[index + 2:closing]]
        if condition != ["run_solver", "==", "0"]:
            continue
        opening = closing + 1
        if opening >= len(tokens) or tokens[opening].text != "{":
            continue
        branch_end = matching.get(opening)
        if (branch_end is None or branch_end + 2 >= len(tokens)
                or tokens[branch_end + 1].text != "else"
                or tokens[branch_end + 2].text != "{"
                or branch_end + 2 not in matching):
            continue
        candidates.append(token.start)
    if len(candidates) != 1:
        _fail("未找到唯一的顶层 if (run_solver==0) {...} else {...} 边界；"
              "请在完整建模结束、任何求解或后处理之前添加独占行 # AH53_BUILD_ONLY_END。")
    return candidates[0], "top_level_run_solver_branch"


def _audit_prefix(prefix: str):
    tokens, matching, problems = _lex(prefix)
    if problems:
        _fail("建模前缀存在字符串或括号错误：" + "；".join(problems))
    if not tokens or tokens[-1].text not in {";", "}"}:
        _fail("建模边界前没有完整的顶层语句。")
    defined = set()
    commands, math_functions, arrays = set(), set(), set()
    output_paths, input_paths = set(), set()
    for index, token in enumerate(tokens):
        if token.kind != "identifier":
            continue
        name = token.text
        lower = name.lower()
        following = tokens[index + 1].text if index + 1 < len(tokens) else None
        previous = tokens[index - 1].text if index else None
        if lower in _FORBIDDEN:
            _fail(f"第 {token.line} 行的 {name} 不允许出现在仅建模前缀中。")
        # Struct/member access may hide a function handle or script property.
        # This intentionally supports ordinary numerical arrays only.
        if previous == "." or following == ".":
            _fail(f"第 {token.line} 行包含成员/结构访问 {name}，无法静态证明没有隐藏脚本调用。")
        if following == "=":
            if lower in _MODEL_COMMANDS | _MATH_FUNCTIONS | _CONTROL:
                _fail(f"第 {token.line} 行不能用变量覆盖内置命令/函数 {name}。")
            defined.add(name)
            continue
        if lower in _CONTROL:
            continue
        if following == "(":
            if lower in _MODEL_COMMANDS or lower in _MATH_FUNCTIONS:
                if lower in _MODEL_COMMANDS:
                    commands.add(lower)
                else:
                    math_functions.add(lower)
                args = _arguments(tokens, index + 1, matching)
                if lower in _PROPERTY_COMMANDS:
                    property_index = _PROPERTY_COMMANDS[lower]
                    if (len(args) <= property_index or len(args[property_index]) != 1
                            or args[property_index][0].kind != "string"):
                        _fail(f"第 {token.line} 行 {name} 的属性名必须是明确的字符串，不能动态设置隐藏脚本属性。")
                    prop = (_string(args[property_index][0]) or "").strip().lower()
                    if "script" in prop or "setup" in prop or prop in {"construction group", "analysis group"}:
                        _fail(f"第 {token.line} 行设置了可执行的组/脚本属性 {prop!r}。")
                if lower in {"save", "importmaterial"}:
                    if len(args) != 1 or len(args[0]) != 1 or args[0][0].kind != "string":
                        _fail(f"第 {token.line} 行 {name} 必须使用单一明确的相对路径字符串。")
                    relative = _relative_file(_string(args[0][0]) or "", name)
                    (output_paths if lower == "save" else input_paths).add(relative)
                if lower == "clear":
                    if any(args):
                        _fail(f"第 {token.line} 行只允许无参数 clear，不能使用动态清理表达式。")
                    defined.clear()
                continue
            if name in defined:
                arrays.add(name)
                continue
            _fail(f"第 {token.line} 行调用 {name}(...) 不在建模/数学白名单内，也不是已定义数组；"
                  "未知脚本调用不能用于无求解预检。")
        if lower in _MODEL_COMMANDS:
            if following != ";" or not _statement_start(tokens, index):
                _fail(f"第 {token.line} 行 {name} 不是可审计的完整建模命令。")
            if lower in _PROPERTY_COMMANDS or lower in {"save", "importmaterial"}:
                _fail(f"第 {token.line} 行 {name} 必须显式提供可检查的参数。")
            commands.add(lower)
            if lower == "clear":
                defined.clear()
            continue
        if name in defined or lower in _CONSTANTS:
            if following == ";" and _statement_start(tokens, index):
                _fail(f"第 {token.line} 行的裸名称 {name} 可能被当作脚本执行；请使用明确的赋值或 ? 输出表达式。")
            continue
        _fail(f"第 {token.line} 行包含未知命令或尚未定义的变量 {name}；请整理为可审计的建模语句。")
    return {"commands": sorted(commands), "math_functions": sorted(math_functions),
            "indexed_variables": sorted(arrays), "relative_save_paths": sorted(output_paths),
            "required_relative_inputs": sorted(input_paths)}


def build_only_script(full_case_script: str) -> dict:
    """Return an unchanged safe build prefix and its static audit manifest.

    Accepted boundaries are one top-level ``if (run_solver==0)``/``else`` block,
    or an explicit standalone ``# AH53_BUILD_ONLY_END`` comment. The entire
    suffix is excluded. Every prefix branch is inspected regardless of runtime
    conditions. Relative native ``save`` calls are preserved and must execute
    in a fresh case-specific directory supplied by the caller.
    """
    if not isinstance(full_case_script, str) or not full_case_script.strip():
        _fail("输入必须是非空的完整 LSF 文本。")
    tokens, matching, problems = _lex(full_case_script)
    if problems:
        _fail("完整 LSF 存在字符串或括号错误：" + "；".join(problems))
    offset, mode = _boundary(full_case_script, tokens, matching)
    prefix = full_case_script[:offset]
    audit = _audit_prefix(prefix)
    return {"script": prefix, "boundary_mode": mode, "boundary_offset": offset,
            "excluded_from_line": _line(full_case_script, offset),
            "full_script_sha256": _hash(full_case_script), "build_script_sha256": _hash(prefix),
            "excluded_suffix_sha256": _hash(full_case_script[offset:]),
            "no_solver_calls": True, **audit,
            "limitations": ["仅接受白名单建模命令、数学函数和此前已定义变量的数值索引。",
                            "相对 save 路径会原样保留；调用方必须使用独立案例目录。",
                            "静态门禁不证明几何或物理正确性；原生建模执行仍可能报错或提前退出。"]}
