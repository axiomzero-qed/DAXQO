"""Prepare pinned Android/arm64 PRoot executables for the offline APK."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request

BASE = 'https://mirrors.ustc.edu.cn/termux/apt/termux-main/'
PACKAGES = {
    'proot': ('pool/main/p/proot/proot_5.1.107.101_aarch64.deb',
              'a28355e9be257e1f87f4e9294b3118470d4fd7a5442fed8b278f4443c83407d7'),
    'libtalloc': ('pool/main/libt/libtalloc/libtalloc_2.5.0_aarch64.deb',
                  '556591f43bb773ad8777e1a29522640866a55f95dab71914418b94a8c58ad5a7'),
    'libandroid-shmem': ('pool/main/liba/libandroid-shmem/libandroid-shmem_0.7_aarch64.deb',
                        '0da3a24d558b93c92bcf8d611e0826a99ff96e396b148e6cdf33b47c47c57ff6'),
}


def deb_data(data: bytes) -> bytes:
    if not data.startswith(b'!<arch>\n'):
        raise ValueError('Invalid Debian archive')
    offset = 8
    while offset + 60 <= len(data):
        header = data[offset:offset + 60]
        size = int(header[48:58])
        name = header[:16].decode('ascii').strip().rstrip('/')
        body = data[offset + 60:offset + 60 + size]
        if name.startswith('data.tar'):
            return body
        offset += 60 + size + size % 2
    raise ValueError('Debian package has no data archive')


def prepare(cache: Path, output: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    native = output / 'jniLibs' / 'arm64-v8a'
    notices = output / 'assets' / 'runtime' / 'licenses'
    native.mkdir(parents=True, exist_ok=True)
    notices.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    manifest = {'schema': 'daxqo.native-runtime/1', 'abi': 'arm64-v8a', 'packages': {}, 'files': {}}
    for package, (remote, expected) in PACKAGES.items():
        archive = cache / Path(remote).name
        if not archive.exists():
            temporary = archive.with_suffix('.download')
            with opener.open(BASE + remote, timeout=60) as response, temporary.open('wb') as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
            temporary.replace(archive)
        content = archive.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError('Pinned package checksum failed: ' + package)
        manifest['packages'][package] = {'url': BASE + remote, 'sha256': expected,
                                         'source': 'https://github.com/termux/termux-packages/tree/master/packages/' + package}
        with tarfile.open(fileobj=io.BytesIO(deb_data(content))) as tar:
            for entry in tar:
                name = entry.name.removeprefix('./')
                target = None
                if package == 'proot' and name.endswith('/bin/proot'):
                    target = 'libproot.so'
                elif package == 'proot' and name.endswith('/libexec/proot/loader'):
                    target = 'libproot_loader.so'
                elif package == 'libtalloc' and entry.isfile() and '/lib/libtalloc.so.' in name:
                    target = 'libtalloc.so'
                elif package == 'libandroid-shmem' and name.endswith('/lib/libandroid-shmem.so'):
                    target = 'libandroid-shmem.so'
                if target and entry.isfile():
                    data = tar.extractfile(entry).read()
                    if not data.startswith(b'\x7fELF'):
                        raise ValueError('Expected ELF: ' + name)
                    # Android installs lib*.so, not SONAMEs ending in .so.2.
                    data = data.replace(b'libtalloc.so.2\0', b'libtalloc.so\0\0\0')
                    (native / target).write_bytes(data)
                    manifest['files'][target] = {'sha256': hashlib.sha256(data).hexdigest(),
                                                  'sourceMember': name, 'bytes': len(data)}
                if entry.isfile() and ('/share/doc/' in name or '/share/licenses/' in name):
                    if entry.size < 2 * 1024 * 1024:
                        dest = notices / package / Path(name).name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(tar.extractfile(entry).read())
    required = {'libproot.so', 'libproot_loader.so', 'libtalloc.so', 'libandroid-shmem.so'}
    if not required.issubset(manifest['files']):
        raise ValueError('Native runtime closure is incomplete')
    (output / 'assets/runtime/native-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ready': True, 'nativeFiles': len(manifest['files']), 'output': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.cache_dir, args.output_dir)
