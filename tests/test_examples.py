"""Every example's and doc snippet's SDK imports must resolve.

Examples and docs are the SDK's documentation, and a rename in
`multistack/` breaks them silently: nothing imports them, so they only
fail when someone runs one — which for `full_stack.py` means partway
through rebuilding a cluster. That happened, and `docs/storage.md` had
gone stale the same way. This is the guard.

The examples are not executed. They are destructive by design (installing
RKE2, deleting PVCs), so this reads their imports statically and checks
each one against the package as it exists now. It catches the whole class
of breakage a rename causes: a moved module, a renamed class, a symbol
dropped from a package's surface.
"""
from __future__ import annotations

import ast
import builtins
import importlib
import re
import textwrap
from pathlib import Path
from typing import List, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
EXAMPLE_FILES = sorted(EXAMPLES.rglob("*.py"))
# docs/ plus every markdown file at the repo root — README, CHANGELOG,
# CONTRIBUTING, SECURITY, CODE_OF_CONDUCT. README's composition snippet
# went stale in the same refactor that broke full_stack.py, and it is the
# first thing anyone reads; the community-health files are the second.
DOCS = sorted((ROOT / "docs").rglob("*.md")) + sorted(ROOT.glob("*.md"))

# Packages that ship from this repo. An example must import from one of
# them.
SDK_ROOTS = ("multistack",)


def skip_if_optional_extra(module_name: str, exc: ImportError, path: Path) -> None:
    """Skips when an import failed because an optional extra isn't installed.

    `pip install -e .` deliberately doesn't pull pyhelm3, so an example
    that uses the Helm layer can't import in that environment — and that is
    the extra working, not a broken example. Only a *third-party* module
    earns a skip: if what's missing is one of our own packages, the example
    really is broken and must still fail. `pip install -e ".[helm]"` runs
    these for real.
    """
    missing = getattr(exc, "name", "") or ""
    if missing.split(".")[0] in SDK_ROOTS:
        return
    pytest.skip(
        f"{rel(path)} needs `{missing}`, which is an optional extra and is "
        f"not installed (imported via {module_name})"
    )


def rel(path: Path) -> str:
    """Test id: path relative to the repo, since two examples can share a
    filename (examples/storage/install.py, examples/helm/install.py)."""
    return str(path.relative_to(ROOT))

# ```python fences only. A fence tagged anything else is prose or shell.
# The optional leading indent matches a fence nested in a list item, which
# is valid markdown — dedented below so it parses as Python.
PYTHON_FENCE = re.compile(r"^([ \t]*)```python\n(.*?)^[ \t]*```", re.S | re.M)


def python_snippets(path: Path) -> List[str]:
    return [
        textwrap.dedent(body)
        for _indent, body in PYTHON_FENCE.findall(path.read_text())
    ]


def sdk_imports(path: Path) -> List[Tuple[str, str]]:
    """Every `multistack` symbol the file imports, as (module, name).

    `name` is empty for a plain `import multistack.x`.
    """
    found: List[Tuple[str, str]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            # Relative imports have no module of ours to resolve.
            if node.module and node.module.split(".")[0] in SDK_ROOTS:
                found.extend((node.module, alias.name) for alias in node.names)
        elif isinstance(node, ast.Import):
            found.extend(
                (alias.name, "")
                for alias in node.names
                if alias.name.split(".")[0] in SDK_ROOTS
            )
    return found


def test_examples_are_present():
    # A glob that silently matches nothing would make every test below
    # pass without checking anything.
    assert EXAMPLE_FILES, f"no examples found under {EXAMPLES}"


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=rel)
def test_example_parses(path: Path):
    ast.parse(path.read_text())


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=rel)
def test_example_imports_resolve(path: Path):
    imports = sdk_imports(path)
    assert imports, f"{path.name} imports nothing from {' or '.join(SDK_ROOTS)}"

    for module_name, symbol in imports:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            skip_if_optional_extra(module_name, exc, path)
            pytest.fail(f"{path.name}: `{module_name}` does not import — {exc}")
        if symbol and not hasattr(module, symbol):
            pytest.fail(
                f"{path.name}: `from {module_name} import {symbol}` — "
                f"{module_name} has no {symbol}"
            )


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=rel)
def test_example_defines_every_name_it_uses(path: Path):
    """Catches a module-level NameError without running anything.

    The examples are deliberately not executed (see the module docstring),
    which left a gap: `ast.parse` accepts a file that reads an undefined
    name, and resolving imports does not touch it either. So
    `kubeconfig_path=KUBECONFIG` with no `KUBECONFIG = ...` passed every
    test and failed on the first real run.

    That is not hypothetical. 302e925 replaced a hardcoded lab path with
    `KUBECONFIG` across eleven examples and added the `os.environ` block
    to six of them, leaving five broken for four days.

    Every loaded name is checked, not just SCREAMING_CASE. The narrower
    version missed `examples/full_stack.py`, which used `os.environ` on
    line 60 and never imported `os` — the two `import os` lines in that
    file sit inside the embedded Job script, a string literal `ast.parse`
    never looks into. Lowercase names are exactly where that hides.

    Bindings are collected from the whole tree rather than per scope, so a
    name bound anywhere counts as defined everywhere. That is deliberately
    permissive: this test is here to catch a name defined *nowhere*, and a
    scope-accurate version would need a symbol table this does not
    justify.
    """
    tree = ast.parse(path.read_text())

    def names_in(target) -> set:
        """Name ids bound by an assignment target, unpacking included."""
        return {
            n.id for n in ast.walk(target)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        }

    bound = set()
    for node in ast.walk(tree):
        # Anything with a Store context: assignments, unpacking, for
        # targets, walrus, comprehension targets, `with ... as`.
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {a.asname or a.name.split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.arguments,)):
            bound |= {a.arg for a in (node.posonlyargs + node.args
                                      + node.kwonlyargs)}
            bound |= {a.arg for a in (node.vararg, node.kwarg) if a}
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound |= set(node.names)

    used = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    # __file__ and friends are module attributes, not builtins.
    module_globals = {"__file__", "__name__", "__doc__", "__package__"}
    undefined = sorted(used - bound - set(dir(builtins)) - module_globals)
    assert not undefined, (
        f"{path.name} reads {undefined} but never defines it — a module-level "
        f"NameError on the first real run"
    )


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=rel)
def test_examples_do_not_reach_into_driver_modules(path: Path):
    # An example is what the team copies. One that names a driver teaches
    # callers to defeat the interchangeability the spec exists to give,
    # and to import eagerly what the registry loads on demand.
    for module_name, symbol in sdk_imports(path):
        assert ".drivers." not in module_name, (
            f"{path.name} imports {module_name} directly — go through the "
            "capability's backend instead"
        )


# -- docs ------------------------------------------------------------------
def test_docs_are_present():
    assert DOCS, f"no docs found under {ROOT / 'docs'}"


@pytest.mark.parametrize("path", DOCS, ids=rel)
def test_doc_snippets_are_valid_python(path: Path):
    # A snippet tagged ```python that doesn't parse is either wrong or
    # mistagged; both mislead a reader who copies it.
    for i, snippet in enumerate(python_snippets(path), 1):
        try:
            ast.parse(snippet)
        except SyntaxError as exc:
            pytest.fail(f"{path.name} snippet {i} does not parse: {exc.msg}")


@pytest.mark.parametrize("path", DOCS, ids=rel)
def test_doc_snippet_imports_resolve(path: Path):
    """Docs went stale exactly as the examples did — `docs/storage.md`
    documented `LonghornStorage` and `LonghornBackend` after both were
    renamed. Snippets are fragments, so only their imports are checkable;
    that is also the part a rename breaks."""
    for i, snippet in enumerate(python_snippets(path), 1):
        for node in ast.walk(ast.parse(snippet)):
            names: List[Tuple[str, str]] = []
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] in SDK_ROOTS:
                    names = [(node.module, a.name) for a in node.names]
            elif isinstance(node, ast.Import):
                names = [
                    (a.name, "") for a in node.names
                    if a.name.split(".")[0] in SDK_ROOTS
                ]
            for module_name, symbol in names:
                try:
                    module = importlib.import_module(module_name)
                except ImportError as exc:
                    pytest.fail(
                        f"{path.name} snippet {i}: `{module_name}` does not "
                        f"import — {exc}"
                    )
                if symbol and not hasattr(module, symbol):
                    pytest.fail(
                        f"{path.name} snippet {i}: `from {module_name} import "
                        f"{symbol}` — {module_name} has no {symbol}"
                    )


# -- references that point somewhere ---------------------------------------
# Markdown links only. A backticked filename in a naming table is an
# illustration (`tests/inference/test_vllm.py`), not a claim that a file
# exists; a link is.
MD_LINK = re.compile(r"\]\(([^)#:]+\.(?:py|md|json|ya?ml))(?:#[^)]*)?\)")

# Paths named in Python docstrings and messages. Placeholders containing
# `<` are excluded by the character class, so `multistack/<capability>/`
# does not match.
PY_PATH = re.compile(r"\b(?:examples|docs|tests|multistack)/[A-Za-z0-9_/.-]+\.(?:py|md)")


@pytest.mark.parametrize("path", DOCS, ids=rel)
def test_markdown_links_resolve(path: Path):
    for target in MD_LINK.findall(path.read_text()):
        resolved = (path.parent / target).resolve()
        assert resolved.exists(), f"{rel(path)} links to missing {target}"


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=rel)
def test_paths_named_in_examples_exist(path: Path):
    """An example that tells you to run `python3 examples/longhorn/install.py`
    after that directory was renamed sends the reader nowhere. Caught
    exactly that."""
    for target in PY_PATH.findall(path.read_text()):
        assert (ROOT / target).exists(), f"{rel(path)} names missing {target}"
