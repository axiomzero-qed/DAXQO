"""Build a source-only, hash-addressed Linux update package."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent.parent
FLAT = ('server.py', 'supervisor.py', 'runtime_control.py', 'test_server.py',
        'test_supervisor.py', 'test_runtime_control.py', 'task_services.py', 'test_task_services.py',
        'models-deepseek.json', 'WORKSPACE_AGENTS.md')


def main():
    backend = ROOT / 'backend'
    output = ROOT / 'dist' / 'package' / 'backend'
    output.mkdir(parents=True, exist_ok=True)
    files = [backend / name for name in FLAT]
    files += sorted(path for path in (backend / 'static').rglob('*') if path.is_file())
    manifest = {'version': (ROOT / 'VERSION').read_text().strip(), 'files': {}}
    for source in files:
        if source.is_symlink(): raise RuntimeError('Package sources must be regular files')
        relative = source.relative_to(backend).as_posix()
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        manifest['files'][relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    target = ROOT / 'dist' / 'update-manifest.json'
    raw = (json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    target.write_bytes(raw)
    print(json.dumps({'version': manifest['version'], 'files': len(manifest['files']),
                      'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'manifest': str(target)}))


if __name__ == '__main__':
    main()
