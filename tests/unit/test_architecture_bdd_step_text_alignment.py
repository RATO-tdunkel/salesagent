"""Guard: concrete BDD step text must align with the fields asserted in code.

These checks target a recurring class of false-positive BDD steps where the
step text promises validation for one field, but the body inspects a different
field instead.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

from tests.unit._architecture_helpers import iter_call_expressions

_BDD_STEPS_DIR = Path(__file__).resolve().parents[1] / "bdd" / "steps"
_INSPECT_SCRIPT = Path(__file__).resolve().parents[2] / ".claude" / "scripts" / "inspect_bdd_steps.py"


def _load_extract_bdd_steps():
    """Load the shared BDD inspection script and return extract_bdd_steps()."""
    spec = importlib.util.spec_from_file_location("inspect_bdd_steps", _INSPECT_SCRIPT)
    assert spec is not None and spec.loader is not None, f"Could not load {_INSPECT_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["inspect_bdd_steps"] = module
    spec.loader.exec_module(module)
    return module.extract_bdd_steps


def _iter_then_steps() -> list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    """Yield Then step nodes plus their extracted step text."""
    extract_bdd_steps = _load_extract_bdd_steps()

    text_by_location: dict[tuple[str, int], str] = {}
    for step in extract_bdd_steps(_BDD_STEPS_DIR):
        if step.step_type == "then":
            text_by_location[(str(Path(step.file_path).resolve()), step.line_number)] = step.step_text

    results = []
    for py_file in sorted(_BDD_STEPS_DIR.rglob("*.py")):
        if py_file.name.startswith("_"):
            continue
        source = py_file.read_text()
        tree = ast.parse(source, filename=str(py_file))
        resolved = str(py_file.resolve())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            step_text = text_by_location.get((resolved, node.lineno))
            if step_text is not None:
                results.append((py_file, node, step_text))
    return results


def _field_names_referenced(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Collect likely field names referenced in a function body."""
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


def _account_id_passed_to_call(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the ``account_id`` parameter is routed into a call whose RESULT is used.

    Recognizes helper-mediated inspection like ``acct = _wire_account(ctx, account_id)`` —
    the parameter is routed into a lookup that performs the ref-echo grade itself — WITHOUT
    matching an incidental mention (an f-string failure message), where ``account_id`` is
    nested inside a ``JoinedStr`` rather than being a direct call argument. Only ``ast.Load``
    uses count (reading the parameter, not rebinding it).

    Calls that are a bare expression STATEMENT (``logger.info(account_id)``,
    ``print(account_id)``, ``logger.info("%s", account_id)``, ``logger.info(..., extra=account_id)``)
    are skipped: their result is discarded, so passing ``account_id`` to a log/print line
    is not an inspection. Without this, the exemption fired on any call argument — a log
    line satisfied the guard (#1329). Deliberately narrower than "the Name appears
    anywhere in the body": that broader form silently weakened the guard repo-wide for a fix
    that only needed to cover by-id lookup calls (#1329 — guard-matcher scope).
    """

    def _is_account_id_load(node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and node.id == "account_id" and isinstance(node.ctx, ast.Load)

    # Calls whose value is discarded as a bare expression statement (log/print lines).
    bare_statement_calls = {
        id(stmt.value) for stmt in ast.walk(func) if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
    }

    for call in iter_call_expressions(func):
        if id(call) in bare_statement_calls:
            continue
        for arg in call.args:
            value = arg.value if isinstance(arg, ast.Starred) else arg
            if _is_account_id_load(value):
                return True
        if any(_is_account_id_load(kw.value) for kw in call.keywords):
            return True
    return False


class TestBddStepTextAlignment:
    """Structural guard: literal field names in Then steps must be referenced in code."""

    @pytest.mark.arch_guard
    def test_account_id_steps_reference_account_id(self):
        """Then steps mentioning account_id must inspect account_id somewhere in the body.

        "Inspect" is satisfied by a literal ``"account_id"`` string / ``.account_id``
        attribute (the by-key read) OR by the ``account_id`` step parameter being passed as an
        argument to a call (Load context) — e.g. a by-id lookup helper such as
        ``_wire_account(ctx, account_id)``, whose lookup performs the ref-echo grade itself.
        Requiring only the by-key form was a false positive for helper-mediated inspection: it
        forced a redundant inline ref-echo assertion at every such call site (#1329). The exemption is scoped to a call ARGUMENT (not any Name anywhere in the body,
        which an incidental f-string/log mention would satisfy — #1329, guard-matcher
        scope). A step that mentions account_id in its text but neither reads it by key nor
        routes it into a call is still flagged.
        """
        violations = []
        for py_file, func, step_text in _iter_then_steps():
            if "account_id" not in step_text:
                continue
            referenced = _field_names_referenced(func)
            if "account_id" not in referenced and not _account_id_passed_to_call(func):
                violations.append(
                    f"{py_file.relative_to(Path.cwd())}:{func.lineno} {func.name} — step mentions account_id"
                )

        assert not violations, (
            f"Found {len(violations)} Then step(s) mentioning account_id without referencing it in code:\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    @pytest.mark.arch_guard
    def test_literal_response_field_steps_reference_the_named_field(self):
        """Then steps about literal response fields must reference those field names in code."""
        violations = []
        pattern = re.compile(r'the response should (?:not )?contain "([^"{}/]+)" field')
        for py_file, func, step_text in _iter_then_steps():
            match = pattern.search(step_text)
            if match is None:
                continue
            field_name = match.group(1)
            referenced = _field_names_referenced(func)
            if field_name not in referenced:
                violations.append(
                    f"{py_file.relative_to(Path.cwd())}:{func.lineno} {func.name} — step claims response field '{field_name}'"
                )

        assert not violations, (
            f"Found {len(violations)} response-field Then step(s) that do not reference the named field:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


def _parse_single_func(src: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    raise AssertionError("no function in snippet")


class TestAccountIdExemptionSelfTest:
    """Meta-tests for the ``_account_id_passed_to_call`` exemption (#1329)."""

    @pytest.mark.parametrize(
        "body",
        [
            "logger.info(account_id)",
            "print(account_id)",
            'logger.info("acct %s", account_id)',
            'logger.info("acct", extra=account_id)',
        ],
    )
    def test_bare_log_or_print_of_account_id_is_not_inspection(self, body):
        """A bare log/print statement of account_id does NOT satisfy the exemption."""
        func = _parse_single_func(f"def then_x(ctx, account_id):\n    {body}\n")
        assert not _account_id_passed_to_call(func), f"bare statement wrongly counted as inspection: {body!r}"

    @pytest.mark.parametrize(
        "body",
        [
            "acct = _wire_account(ctx, account_id)",
            "assert account_id in _wire_account_ids(ctx, account_id)",
            "return _wire_account(ctx, account_id)",
        ],
    )
    def test_used_result_call_with_account_id_is_inspection(self, body):
        """Routing account_id into a call whose result is used IS inspection."""
        func = _parse_single_func(f"def then_x(ctx, account_id):\n    {body}\n")
        assert _account_id_passed_to_call(func), f"real inspection wrongly rejected: {body!r}"
