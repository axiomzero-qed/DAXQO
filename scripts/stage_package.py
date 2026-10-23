"""Build a source-only, hash-addressed Linux update package."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent.parent
FLAT = ('server.py', 'supervisor.py', 'runtime_control.py', 'test_server.py',
        'test_supervisor.py', 'test_runtime_control.py', 'task_services.py', 'test_task_services.py',
        'android_mcp_server.py', 'test_android_mcp.py', 'phone_backends.py', 'test_phone_backends.py',
        'remote_protocol.py', 'test_remote_protocol.py', 'email_control.py', 'test_email_control.py',
        'test_remote_email_mcp.py',
        'models-deepseek.json', 'WORKSPACE_AGENTS.md')
SCRIPT_FLAT = ('remote_email_mcp.py',)


def main():
    backend = ROOT / 'backend'
    output = ROOT / 'dist' / 'package' / 'backend'
    output.mkdir(parents=True, exist_ok=True)
    files = [(backend / name, name) for name in FLAT]
    files += [(ROOT / 'scripts' / name, name) for name in SCRIPT_FLAT]
    files += [(path, path.relative_to(backend).as_posix()) for path in sorted((backend / 'static').rglob('*')) if path.is_file()]
    manifest = {'version': (ROOT / 'VERSION').read_text().strip(), 'files': {}}
    for source, relative in files:
        if source.is_symlink(): raise RuntimeError('Package sources must be regular files')
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
