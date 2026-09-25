"""Checks safety invariants of main.py that tests cannot express.

Invariants:
  1. Filesystem-destroying calls appear only in the functions that verify
     each step first (_delete_file, _remove_dir) and in HashCache.save,
     which removes only its own temporary file.
  2. Unsafe deserialization and shell execution are never imported or
     called (pickle, marshal, shelve, eval, exec, os.system, shell=True).

Usage:
  python tools/check_invariants.py [path/to/main.py]

Exit code 0 if all invariants hold, 1 otherwise.
"""

from __future__ import annotations

import ast
import sys

DESTRUCTIVE = {
    'os.remove',
    'os.unlink',
    'os.rmdir',
    'os.removedirs',
    'shutil.rmtree',
    'shutil.move',
    'os.rename',
    'os.replace',
}
ALLOWED_DESTRUCTIVE = {
    '_delete_file': {'os.remove'},
    '_remove_dir': {'os.rmdir'},
    'HashCache.save': {'os.unlink', 'os.replace'},
}
FORBIDDEN_MODULES = {'pickle', 'marshal', 'shelve', 'dill', 'cloudpickle'}
FORBIDDEN_CALLS = {'eval', 'exec', 'os.system', 'os.popen'}


def _dotted(node: ast.AST) -> str:
    """Returns 'a.b.c' for a Name/Attribute chain, '' otherwise."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return '.'.join(reversed(parts))
    return ''


class _Checker(ast.NodeVisitor):
    """Collects invariant violations with line numbers."""

    def __init__(self) -> None:
        self.scope: list[str] = []
        self.errors: list[str] = []

    def _qualname(self) -> str:
        return '.'.join(self.scope) or '<module>'

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split('.')[0] in FORBIDDEN_MODULES:
                self.errors.append(
                    f'{node.lineno}: forbidden import {alias.name}'
                )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = (node.module or '').split('.')[0]
        if module in FORBIDDEN_MODULES:
            self.errors.append(f'{node.lineno}: forbidden import {module}')
        if module in {'os', 'shutil'}:
            names = {f'{module}.{a.name}' for a in node.names}
            if names & DESTRUCTIVE:
                self.errors.append(
                    f'{node.lineno}: import destructive functions via '
                    f'module attribute only (os.remove, not remove)'
                )

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func)
        where = self._qualname()
        if name in FORBIDDEN_CALLS:
            self.errors.append(f'{node.lineno}: forbidden call {name}()')
        for keyword in node.keywords:
            if (
                keyword.arg == 'shell'
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                self.errors.append(f'{node.lineno}: shell=True')
        if name in DESTRUCTIVE and name not in ALLOWED_DESTRUCTIVE.get(
            where, set()
        ):
            self.errors.append(
                f'{node.lineno}: {name}() in {where}; allowed only in '
                f'{sorted(ALLOWED_DESTRUCTIVE)}'
            )
        self.generic_visit(node)


def check(path: str) -> list[str]:
    """Returns a list of violations in the given file."""
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename=path)
    checker = _Checker()
    checker.visit(tree)
    return checker.errors


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else 'main.py'
    errors = check(path)
    for error in errors:
        print(f'{path}:{error}', file=sys.stderr)
    print(f'{path}: {len(errors)} invariant violation(s)')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
