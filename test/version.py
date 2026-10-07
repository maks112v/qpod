#!/usr/bin/env python3
"""Exercise local build numbering and production bumps without vendor firmware or compilers."""
import hashlib, json, pathlib, subprocess, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'tools'))
from build import ROOT, VERSION, version_tag

assert version_tag('8.7', 'ipod') == 'V8.7I'
assert version_tag('8.7', 'stock', 1) == 'V001s'
assert version_tag('8.7', 'ipod', 36) == 'V010i'
assert version_tag('8.7', 'ipod', 46655) == 'VZZZi'
assert len(version_tag('8.10', 'ipod')) == 5
assert version_tag('8.10', 'ipod') != version_tag('8.11', 'ipod')
for number in (0, 46656):
    try:
        version_tag('8.7', 'ipod', number)
    except ValueError:
        pass
    else:
        raise AssertionError('Accepted out-of-range build number')

with tempfile.TemporaryDirectory(prefix='q2-version-') as tmp:
    root = pathlib.Path(tmp)
    (root/'tools').mkdir()
    source = (ROOT/'tools/build.py').read_text()
    start, end = source.index('def build('), source.index("if __name__ == '__main__':")
    # Substitute only the expensive firmware builder; run the real CLI and prod child process.
    stub = '''def build(zip_path, out, logo, ipod=False, dev=False, build_number=None):
    if str(zip_path) == 'fail': raise ValueError('Simulated build failure')
    out.mkdir(parents=True, exist_ok=True)
    (out/'manifest.json').write_text(json.dumps(dict(release_version=VERSION,
        version=version_tag(VERSION, 'ipod' if ipod else 'stock', build_number),
        dev=dev, build_number=build_number, source_sha256=sha((ROOT/'tools/build.py').read_bytes()))))

'''
    (root/'tools/build.py').write_text(source[:start] + stub + source[end:])
    def run(mode, name, fail=False):
        result = subprocess.run([sys.executable, str(root/'tools/build.py'),
            'fail' if fail else 'stock.zip', '--ipod', '--out', str(root/name), mode], capture_output=True, text=True)
        assert result.returncode == (2 if fail else 0), result.stderr
        return None if fail else json.loads((root/name/'manifest.json').read_text())
    first, second = run('--dev', 'first'), run('--dev', 'second')
    assert first['build_number'] == 1 and second['build_number'] == 2
    assert first['version'] != second['version'] and second['release_version'] == VERSION
    run('--dev', 'failed-dev', True)
    assert run('--dev', 'after-failure')['build_number'] == 4
    before = (root/'tools/build.py').read_text()
    run('--prod', 'failed-prod', True)
    assert (root/'tools/build.py').read_text() == before
    prod = run('--prod', 'prod')
    major, minor = map(int, VERSION.split('.'))
    assert prod['release_version'] == f'{major}.{minor + 1}'
    assert prod['dev'] is False and prod['build_number'] is None
    assert prod['source_sha256'] == hashlib.sha256((root/'tools/build.py').read_bytes()).hexdigest()
    assert run('--dev', 'after-prod')['build_number'] == 5
    processes = [subprocess.Popen([sys.executable, str(root/'tools/build.py'), 'stock.zip',
        '--ipod', '--out', str(root/f'parallel-{i}'), '--dev'], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True) for i in range(2)]
    for process in processes:
        _, errors = process.communicate()
        assert process.returncode == 0, errors
    assert {json.loads((root/f'parallel-{i}'/'manifest.json').read_text())['build_number']
            for i in range(2)} == {6, 7}
    (root/'.build-number').write_text('46655\n')
    run('--dev', 'overflow', True)
    assert (root/'.build-number').read_text() == '46655\n'
print('Development numbering, failure handling, production bump and tag capacity passed.')
