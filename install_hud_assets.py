"""Verify the bundled rendering libraries and optionally repair missing or damaged files."""
from __future__ import annotations
import argparse
import hashlib
from pathlib import Path
import tempfile
from urllib.request import Request, urlopen

VENDOR = Path(__file__).resolve().parent / 'static' / 'vendor'
ASSETS = {
    'three.min.js': ('three@0.128.0/build/three.min.js', '9274bbcec8d96168626c732b5d31c775aa8cfb7eaa0599bec0c175908a2c1ce2'),
    'three-LICENSE.txt': ('three@0.128.0/LICENSE', '7dddf7c5b8fd10ee654db8857d75d104b5557889aa5a91fc4ca545ea7c07062f'),
    'chart.umd.min.js': ('chart.js@4.4.8/dist/chart.umd.min.js', 'c40877e88de4df7201532014e14fb707f0f07a196a4ec63e070544b80184fb00'),
    'chartjs-LICENSE.txt': ('chart.js@4.4.8/LICENSE.md', '41a84aa2caba645f966a18d9c2056b73e6d3a81d80bc0046bc0011a2634d4cce'),
}

def verify(directory: Path, repair: bool = False):
    for name, (source, digest) in ASSETS.items():
        target = directory / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            continue
        if not repair:
            raise ValueError(f'{name} missing or changed. Restore it from the ZIP or run this command with --repair.')
        request = Request('https://cdn.jsdelivr.net/npm/' + source)
        with urlopen(request, timeout=30) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f'Unexpected SHA-256 digest for {name}.')
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as file:
            temp = Path(file.name)
            file.write(data)
        try:
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=VENDOR)
    parser.add_argument('--repair', action='store_true', help='Fetch only missing or damaged pinned files')
    args = parser.parse_args()
    try:
        verify(args.directory, args.repair)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'{exc}\n')
    print('Verified Three.js, Chart.js and both license files.')
