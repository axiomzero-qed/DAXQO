"""The installer and both HTTP services must advertise the same release."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionTests(unittest.TestCase):
    def test_startup_components_agree_with_release_version(self):
        expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        for relative in ("backend/server.py", "backend/runtime_control.py", "scripts/install_linux.py"):
            with self.subTest(component=relative):
                tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
                values = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in node.targets)
                          and isinstance(node.value, ast.Constant)]
                self.assertEqual(values, [expected])


if __name__ == "__main__":
    unittest.main()
