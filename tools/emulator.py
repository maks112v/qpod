#!/usr/bin/env python3
"""Launch the visual Q2 emulator from a build directory or packaged release ZIP."""
import argparse
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from build import BASE, segments


def prepare_release(path, out):
    """Recover the emulator artifacts embedded in a packaged Q2 Pod release."""
    if not shutil.which('unsquashfs'):
        raise RuntimeError('unsquashfs is required; on macOS, run: brew install squashfs')
    with zipfile.ZipFile(path) as release:
        manifest = release.read('manifest.json')
        with tarfile.open(fileobj=io.BytesIO(release.read('update.tar'))) as update:
            rootfs = update.extractfile('recovery-update/rootfs.squashfs').read()

    out.mkdir()
    (out / 'manifest.json').write_bytes(manifest)
    (out / 'rootfs.squashfs').write_bytes(rootfs)
    demo = subprocess.check_output([
        'unsquashfs', '-cat', str(out / 'rootfs.squashfs'), 'release/bin/demo'
    ])
    (out / 'demo').write_bytes(demo)

    payload = next((demo[o:o + f] for _, (t, o, v, _, f, _, _, _) in segments(demo)
                    if t == 1 and v == BASE), None)
    if payload is None:
        raise ValueError('The release does not contain the Q2 Pod payload segment')
    (out / 'patch.bin').write_bytes(payload)

    expected = json.loads(manifest)
    if expected.get('variant') != 'ipod':
        raise ValueError('The visual emulator currently requires an iPod build')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=pathlib.Path,
                        help='build directory or Q2.Firmware.V*.zip release')
    parser.add_argument('--smoke-test', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix='q2-pod-emulator-') as temporary:
        source = args.source.resolve()
        if source.is_dir():
            build = source
        else:
            build = pathlib.Path(temporary) / 'build'
            prepare_release(source, build)

        manifest = json.loads((build / 'manifest.json').read_text())
        if manifest.get('variant') != 'ipod':
            parser.error('the visual emulator currently requires an iPod build')

        command = [sys.executable, str(ROOT / 'test' / 'patch.py'), str(build), '--emulator']
        if args.smoke_test:
            command.append('--smoke-test')
        env = os.environ.copy()
        greadelf = shutil.which('greadelf') or '/opt/homebrew/opt/binutils/bin/greadelf'
        readelf = shutil.which('readelf') or (greadelf if pathlib.Path(greadelf).exists() else None)
        if not readelf:
            parser.error('GNU readelf is required; on macOS, run: brew install binutils')
        if pathlib.Path(readelf).name != 'readelf':
            tools = pathlib.Path(temporary) / 'tools'
            tools.mkdir()
            (tools / 'readelf').symlink_to(readelf)
            env['PATH'] = f'{tools}:{env.get("PATH", "")}'
        raise SystemExit(subprocess.call(command, cwd=ROOT, env=env))


if __name__ == '__main__':
    main()
