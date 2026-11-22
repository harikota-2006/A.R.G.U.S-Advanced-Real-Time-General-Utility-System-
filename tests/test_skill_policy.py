"""Pure-computation capability boundary and worker output checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ARGUS_DB_PATH", str(Path(tempfile.gettempdir()) / "argus-skill-tests.db"))
os.environ.setdefault("ARGUS_DATA_DIR", str(Path(tempfile.gettempdir()) / "argus-skill-tests"))
from evolution_engine import validate_skill


class SkillPolicyTests(unittest.TestCase):
    def worker(self, code, params=None):
        return subprocess.run([sys.executable, "-I", "-S", str(ROOT / "skill_worker.py")], input=json.dumps({"code": code, "params": params or {}}), text=True, encoding="utf-8", capture_output=True, timeout=6)

    def test_approved_statistics_and_json_work(self):
        code = "import statistics\nimport json\ndef run(params):\n    return {'mean': statistics.mean(params['values']), 'parsed': json.loads('[1,2]')}"
        validate_skill(code)
        response = self.worker(code, {"values": [1, 2, 3]})
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(json.loads(response.stdout), {"mean": 2, "parsed": [1, 2]})

    def test_real_module_escape_and_unsafe_imports_rejected(self):
        for code in (
            "import statistics\ndef run(params):\n    return statistics.sys.modules['os'].getcwd()",
            "import statistics as s\ndef run(params):\n    return s.sys.modules['os'].getcwd()",
            "from statistics import sys\ndef run(params):\n    return sys.version",
            "import json\ndef run(params):\n    return json.codecs.open('file.txt').read()",
            "import os\ndef run(params):\n    return os.getcwd()",
            "def run(params):\n    return eval(params['code'])",
        ):
            with self.subTest(code=code), self.assertRaises(ValueError):
                validate_skill(code)

    def test_worker_facade_blocks_alias_escape_even_without_ast_validation(self):
        code = "import statistics\ndef run(params):\n    alias = statistics\n    return alias.sys.modules['os'].getcwd()"
        response = self.worker(code)
        self.assertNotEqual(response.returncode, 0)
        self.assertIn("AttributeError", response.stderr)

    def test_oversized_worker_result_is_rejected(self):
        response = self.worker("def run(params):\n    return 'x' * 1100000")
        self.assertNotEqual(response.returncode, 0)
        self.assertIn("Result exceeds 1 MB", response.stderr)


if __name__ == "__main__":
    unittest.main()
