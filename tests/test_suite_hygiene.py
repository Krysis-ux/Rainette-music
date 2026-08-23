"""Guards on the test suite itself.

A test that has stopped running does not fail -- it just quietly stops being
evidence, and the suite still reports green. That is the worst possible failure
mode for a safety net, so the few ways it can happen are checked here.

This file exists because it happened: a new `TestBackgroundPlayback` class was
added to a file that already had one, and Python's second definition shadowed
the first. Two real tests left the run and the total went *up*, so nothing
looked wrong.
"""

from __future__ import annotations

import ast
import unittest
from collections import Counter
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def _test_files():
    return sorted(TESTS_DIR.glob("test_*.py"))


class SuiteHygieneTests(unittest.TestCase):
    def test_no_module_defines_the_same_name_twice(self):
        """A redefinition shadows the first, taking its tests out of the run."""
        for path in _test_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            top_level = [
                node.name for node in tree.body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            for name, count in Counter(top_level).items():
                with self.subTest(module=path.name, name=name):
                    self.assertEqual(
                        count, 1,
                        f"{path.name} defines {name!r} {count} times. The later "
                        f"definition shadows the earlier one, so its tests "
                        f"silently stop running while the suite stays green.",
                    )

    def test_no_class_defines_the_same_test_twice(self):
        """Same trap, one level down: a duplicated method name inside a class."""
        for path in _test_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                methods = [
                    child.name for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name.startswith("test")
                ]
                for name, count in Counter(methods).items():
                    with self.subTest(module=path.name, cls=node.name, name=name):
                        self.assertEqual(
                            count, 1,
                            f"{path.name}::{node.name} defines {name!r} {count} "
                            f"times; only the last one runs.",
                        )


if __name__ == "__main__":
    unittest.main()
