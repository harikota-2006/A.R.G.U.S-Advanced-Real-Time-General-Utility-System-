"""Compile, import and exercise ARGUS without using personal data or live APIs."""
from __future__ import annotations
import importlib
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import unittest

def main():
    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix='argus-verification-') as directory:
        scratch = Path(directory)
        os.environ['ARGUS_DB_PATH'] = str(scratch / 'verification.db')
        os.environ['ARGUS_DATA_DIR'] = str(scratch / 'data')
        for key in ('GROQ_API_KEY', 'GEMINI_API_KEY', 'ELEVENLABS_API_KEY', 'TAVILY_API_KEY', 'NTFY_RX_URL', 'NTFY_TX_URL'):
            os.environ[key] = ''
        sys.path.insert(0, str(root))
        source_files = sorted(root.glob('*.py'))
        for path in source_files:
            py_compile.compile(str(path), cfile=str(scratch / (path.stem + '.pyc')), doraise=True)
            if path.stem != 'verify':
                importlib.import_module(path.stem)
        print(f'Compiled {len(source_files)} Python modules; imported {len(source_files)-1} without launch side effects.', flush=True)
        for path in sorted((root / 'tests').glob('*.py')):
            py_compile.compile(str(path), cfile=str(scratch / (path.stem + '.pyc')), doraise=True)
        suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern='test_*.py')
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful():
            return 1
        try:
            for path in sorted((root/'static').glob('*.js')):
                subprocess.run(['node', '--check', str(path)], check=True)
            for path in sorted((root/'tests').glob('test_*.cjs')):
                subprocess.run(['node', str(path)], check=True)
        except FileNotFoundError:
            print('Node.js not installed; browser JS checks were not run.')
        return 0

if __name__ == '__main__':
    raise SystemExit(main())
