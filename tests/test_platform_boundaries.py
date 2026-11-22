"""Release checks run in disposable processes with role-specific import boundaries.

A minimal laptop install may run this module with ARGUS_TEST_ROLE=laptop.
The checked contract hash was recorded from Source A's parsed multiline literal.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_SHA256 = '040f18786014e3169ae2dd75b0c234872c623e10f62d8aac2cf0c2a4b0316fe8'
DESKTOP = {'laptop_daemon', 'workstation_daemon', 'report_renderer'}
DESKTOP_ONLY = DESKTOP | {'pptx', 'docx', 'reportlab', 'pyautogui', 'win32api', 'win32com',
                           'win32gui', 'win32con', 'pythoncom', 'pywintypes', 'cv2', 'torch', 'psutil'}
PRODUCTION = sorted(ROOT.glob('*.py'))
HUB = {p.stem for p in PRODUCTION} - DESKTOP
IMPORT_PROBE = """
import importlib, json, sys
from pathlib import Path

modules, forbidden = json.loads(sys.argv[1]), set(json.loads(sys.argv[2]))
class Boundary:
    def find_spec(self, fullname, path, target=None):
        if fullname.partition('.')[0] in forbidden:
            raise AssertionError(f'Unexpected role-specific dependency: {fullname}')

def offline(event, args):
    if event in {'socket.connect','socket.bind','socket.getaddrinfo','subprocess.Popen','os.system'}:
        raise RuntimeError(f'Import attempted side effect: {event}')

sys.meta_path.insert(0, Boundary())
sys.addaudithook(offline)
for name in modules:
    loaded = importlib.import_module(name)
    if Path(loaded.__file__).resolve().parent != Path.cwd():
        raise AssertionError(f'{name} was loaded outside the isolated copy')
print(json.dumps(modules))
"""


class PlatformBoundaryTests(unittest.TestCase):
    def test_release_compiles_including_tests(self):
        files = PRODUCTION + sorted((ROOT/'tests').glob('*.py'))
        with tempfile.TemporaryDirectory() as directory:
            for index, source in enumerate(files):
                with self.subTest(path=source.relative_to(ROOT)):
                    py_compile.compile(str(source), cfile=str(Path(directory)/f'{index}.pyc'), doraise=True)

    def test_hub_sources_exclude_desktop_dependencies(self):
        issues = []
        for name in sorted(HUB):
            path = ROOT/(name+'.py')
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), str(path))
            if name == 'setup_config':
                # The shared setup CLI has an explicit Windows-only --laptop branch.
                for branch in ast.walk(tree):
                    if isinstance(branch, ast.If) and ast.unparse(branch.test)=='args.laptop':
                        branch.body.clear()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports = [entry.name for entry in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imports = [node.module or '']
                elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                    func = node.func
                    callee = func.id if isinstance(func, ast.Name) else getattr(func, 'attr', '')
                    imports = [node.args[0].value] if callee in {'import_module','__import__'} else []
                else:
                    continue
                for target in imports:
                    if isinstance(target, str) and target.split('.',1)[0] in DESKTOP_ONLY:
                        issues.append(f'{path.name}:{node.lineno} imports {target}')
        self.assertEqual(issues, [], 'Windows and document rendering imports must stay on the workstation')

    def probe(self, modules, blocked):
        with tempfile.TemporaryDirectory(prefix='argus-platform-') as directory:
            root = Path(directory)
            for source in PRODUCTION:
                shutil.copyfile(source, root/source.name)
            # StaticFiles checks are allowed without copying private state, vendor JS or configuration.
            (root/'static'/'icons').mkdir(parents=True)
            environ = dict(os.environ, ARGUS_DB_PATH=str(root/'profile.db'), ARGUS_DATA_DIR=str(root/'data'),
                           PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
            for key in list(environ):
                if key.startswith(('NTFY_', 'GROQ_', 'GEMINI_', 'ELEVENLABS_', 'TAVILY_')):
                    environ.pop(key)
            result = subprocess.run([sys.executable, '-B', '-c', IMPORT_PROBE, json.dumps(modules), json.dumps(sorted(blocked))],
                                    cwd=root, env=environ, capture_output=True, encoding='utf-8', timeout=75)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), modules)

    @unittest.skipIf(os.getenv('ARGUS_TEST_ROLE')=='laptop', 'Hub imports require the tablet dependency set')
    def test_hub_normal_entrypoint_isolated_from_workstation(self):
        self.probe(['server'] + sorted(HUB-{'server'}), DESKTOP_ONLY)

    def test_workstation_entrypoints_isolated_from_hub(self):
        # Renderer imports are lazy; the laptop entrypoint runs without the Android/web stack.
        blocked = (HUB-{'config'}) | {'fastapi','uvicorn','edge_tts','tavily','duckduckgo_search','cv2','torch'}
        self.probe(sorted(DESKTOP | {'config'}), blocked)

    def test_capability_contract_matches_source_a(self):
        tree = ast.parse((ROOT/'evolution_engine.py').read_text(encoding='utf-8-sig'))
        expressions = [node.value for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id=='ARGUS_CAPABILITY_CONTRACT' for target in node.targets)]
        self.assertEqual(len(expressions),1,'The release must declare the single original capability contract')
        literal = ast.literal_eval(expressions[0])
        self.assertEqual(hashlib.sha256(literal.encode('utf-8')).hexdigest(), CONTRACT_SHA256)


if __name__ == '__main__':
    unittest.main()
