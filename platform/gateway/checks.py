"""Deterministic checks on a released run: cheap, explainable flags that no prompt can talk
past. They don't block release (the release rule does that); they decide whether a
run is "clean" enough to be promoted without a person.
"""

import ast
import csv
import io

SUSPICIOUS_CALLS = {"__import__", "eval", "exec", "compile", "getattr", "setattr", "globals", "locals",
                    "open", "vars", "breakpoint"}
SUSPICIOUS_MODULES = {"os", "sys", "subprocess", "socket", "urllib", "http", "requests", "pickle", "importlib",
                      "ctypes", "builtins"}


def code_flags(code: str) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ["UDF does not parse"]
    flags = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in SUSPICIOUS_CALLS:
            flags.add(f"calls {node.func.id}()")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
            flags.add(f"uses dunder attribute {node.attr}")
        elif isinstance(node, ast.Name) and node.id in SUSPICIOUS_MODULES:
            flags.add(f"references module {node.id}")
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)) and \
                sum(isinstance(e, ast.Constant) and isinstance(e.value, (int, float)) for e in node.elts) > 20:
            flags.add("embeds a long list of numeric literals (possible hard-coded data)")
        elif isinstance(node, ast.Try) and any(h.type is None for h in node.handlers):
            flags.add("bare except: swallows every error")
    return sorted(flags)


def output_flags(csv_text: str, count_column: str | None) -> list[str]:
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    flags = []
    if len(rows) >= 5:
        for col in rows[0]:
            if col != count_column and len({r[col] for r in rows}) == 1:
                flags.append(f"column {col} has the same value in every row")
        if count_column and len({r[count_column] for r in rows}) == 1:
            flags.append(f"every row has the same {count_column}")
    if not rows:
        flags.append("released artifact is empty after suppression")
    return flags


def run(contract: dict, code: str, csv_text: str) -> list[str]:
    k_rule = contract["artifact"].get("min_group_size") or {}
    return code_flags(code) + output_flags(csv_text, k_rule.get("column"))
