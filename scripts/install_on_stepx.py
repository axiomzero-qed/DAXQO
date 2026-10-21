"""One-time v0.1 migration; normal v0.2 updates use the runtime-control bridge."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

APP = Path('/root/phone-harness')
STAGE = Path('/sdcard/Download/daxqo-stage')
REPORT = STAGE / 'install-result.json'
FLAT = {'server.py', 'supervisor.py', 'runtime_control.py', 'test_server.py',
        'test_supervisor.py', 'test_runtime_control.py', 'models-deepseek.json', 'WORKSPACE_AGENTS.md'}


def report(value):
    temporary = REPORT.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(REPORT)


def identity(pid):
    proc = Path('/proc') / str(pid)
    fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
    args = [os.fsdecode(part) for part in (proc / 'cmdline').read_bytes().split(b'\0') if part]
    if len(args) < 2 or proc.stat().st_uid != os.getuid(): raise RuntimeError('Service identity mismatch')
    script = Path(args[1])
    if not script.is_absolute(): script = (proc / 'cwd').resolve() / script
    if script.resolve() != (APP / 'server.py').resolve(): raise RuntimeError('Not this application backend')
    return pid, int(fields[19])


def worker(expected_digest):
    result = {'version': '0.2.0', 'phase': 'validate', 'passed': False}
    cache = None
    try:
        if APP.is_symlink() or APP.stat().st_uid != os.getuid(): raise RuntimeError('Unsafe app directory')
        raw = (STAGE / 'update-manifest.json').read_bytes()
        if not secrets.compare_digest(hashlib.sha256(raw).hexdigest(), expected_digest):
            raise RuntimeError('Manifest digest mismatch')
        manifest = json.loads(raw)
        if manifest.get('version') != '0.2.0': raise RuntimeError('Unsupported version')
        cache = Path(tempfile.mkdtemp(prefix='.daxqo-install-', dir=APP / 'state'))
        cache.chmod(0o700)
        total = 0
        for relative, digest in manifest['files'].items():
            path = PurePosixPath(relative)
            if path.is_absolute() or path.as_posix() != relative or '\\' in relative or any(x in ('.', '..') for x in path.parts):
                raise RuntimeError('Unsafe package path')
            if relative not in FLAT and not relative.startswith('static/'):
                raise RuntimeError('Unknown package file')
            source = STAGE / 'backend' / relative
            for part in [source, *source.parents]:
                if part == STAGE.parent: break
                if part.is_symlink(): raise RuntimeError('Unsafe source link')
            content = source.read_bytes()
            total += len(content)
            if len(content) > 2 * 1024 * 1024 or total > 16 * 1024 * 1024:
                raise RuntimeError('Package is too large')
            if not secrets.compare_digest(hashlib.sha256(content).hexdigest(), digest):
                raise RuntimeError('Source digest mismatch')
            target = cache / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o600)
        result['phase'] = 'phone_tests'
        report(result)
        tests = subprocess.run([sys.executable, '-m', 'unittest', '-v', 'test_server',
                                'test_supervisor', 'test_runtime_control'], cwd=cache,
                               capture_output=True, text=True, timeout=180)
        result['tests'] = {'code': tests.returncode, 'summary': tests.stderr[-6000:]}
        if tests.returncode: raise RuntimeError('Phone Linux checks failed')
        capability = (APP / 'state' / 'capability').read_text().strip()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def status():
            request = urllib.request.Request('http://127.0.0.1:18880/api/status',
                headers={'Authorization': 'Bearer ' + capability})
            with opener.open(request, timeout=4) as response: return json.load(response)
        result['phase'] = 'waiting_for_bootstrap_task'
        report(result)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            initial = status()
            if not initial.get('activeTaskId'): break
            time.sleep(1)
        else: raise TimeoutError('A task is still active; update was not installed')
        original_identity = identity(initial['service']['pid'])
        backup = APP / 'state' / 'backups' / ('v0.1-before-0.2-' + time.strftime('%Y%m%d-%H%M%S'))
        backup.mkdir(mode=0o700, parents=True)
        with sqlite3.connect(APP / 'state' / 'harness.sqlite3') as source_db:
            with sqlite3.connect(backup / 'harness.sqlite3') as target_db: source_db.backup(target_db)
        (backup / 'harness.sqlite3').chmod(0o600)
        if status().get('activeTaskId'): raise RuntimeError('Task activity changed; retry upgrade when idle')
        for relative in manifest['files']:
            target = APP / relative
            if not target.resolve().is_relative_to(APP.resolve()) or target.is_symlink():
                raise RuntimeError('Unsafe installed target')
            if target.exists():
                saved = backup / 'source' / relative
                saved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                saved.write_bytes(target.read_bytes())
                saved.chmod(0o600)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.daxqo-update')
            temporary.write_bytes((cache / relative).read_bytes())
            temporary.chmod(0o600)
            temporary.replace(target)
        current = status()
        if current.get('activeTaskId') or identity(current['service']['pid']) != original_identity:
            raise RuntimeError('Backend changed before restart; manual review required')
        result.update(phase='restart_requested', passed=True, previous_backend_pid=original_identity[0],
                      backup=str(backup), files=len(manifest['files']))
        report(result)
        # The old backend owns this one-shot worker and will clean it up. Its
        # existing supervisor starts v0.2; v0.2 starts the independent runtime bridge.
        os.kill(original_identity[0], signal.SIGTERM)
        return 0
    except Exception as error:
        result.update(passed=False, error_type=type(error).__name__, error=str(error)[:240])
        report(result)
        return 1
    finally:
        if cache is not None:
            import shutil
            shutil.rmtree(cache, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest-sha', required=True)
    parser.add_argument('--launch', action='store_true')
    args = parser.parse_args()
    if len(args.manifest_sha) != 64 or any(ch not in '0123456789abcdef' for ch in args.manifest_sha):
        raise SystemExit('Invalid manifest digest')
    if args.launch:
        log_path = APP / 'state' / 'daxqo-install.log'
        with log_path.open('ab') as log:
            log_path.chmod(0o600)
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                '--manifest-sha', args.manifest_sha], stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True, close_fds=True)
        print(json.dumps({'launched': True, 'pid': child.pid, 'report': str(REPORT)}))
        return 0
    return worker(args.manifest_sha)


if __name__ == '__main__':
    raise SystemExit(main())
