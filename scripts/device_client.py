"""Temporary USB development session. Local capability stays in process memory."""
import json
import getpass
import os
from pathlib import Path
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding='utf-8')
ADB = Path(os.environ.get('DAXQO_ADB', 'adb'))
SERIAL = os.environ.get('DAXQO_SERIAL')
HANDOFF = '/sdcard/Download/daxqo-stage/gui-handoff.json'


def adb(*args):
    result = subprocess.run([str(ADB), *(['-s', SERIAL] if SERIAL else []), *args], capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Device command failed')
    return result.stdout.decode('utf-8', errors='replace').strip()


def main():
    if os.environ.get('DAXQO_PAIRING_SOURCE') == 'android-debug':
        # Development APK only: read our own private pairing, never model keys.
        preferences = ET.fromstring(adb('exec-out', 'run-as', 'io.daxqo.app',
            'cat', 'shared_prefs/pairing_private.xml'))
        entry = preferences.find("string[@name='capability']")
        token = entry.text if entry is not None else None
        if not token or len(token) < 32:
            raise RuntimeError('DAXQO debug app is not paired')
    else:
        handoff = json.loads(adb('exec-out', 'cat', HANDOFF))
        token = handoff['url'].split('#token=', 1)[1]
        adb('shell', 'rm', '-f', HANDOFF)
    ports = {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        for port in (18880, 18882, 18883):
            forwarded = adb('forward', 'tcp:0', 'tcp:' + str(port))
            if not forwarded.isdigit(): raise RuntimeError('Invalid forwarding result')
            ports[port] = forwarded
        print(json.dumps({'ready': True, 'modelKeysRead': False}), flush=True)
        for line in sys.stdin:
            try:
                command = json.loads(line)
                if command.get('action') == 'exit': break
                if command.get('action') == 'pair-app':
                    uri = 'daxqo://connect#token=' + token
                    adb('shell', 'am start -a android.intent.action.VIEW -d ' + shlex.quote(uri) + ' -p io.daxqo.app')
                    result = {'pairing_intent_sent': True}
                else:
                    path = command.get('path', '/api/status')
                    if not path.startswith(('/api/', '/healthz')): raise ValueError('Invalid API path')
                    port = {'runtime': 18882, 'android': 18883}.get(command.get('target'), 18880)
                    payload = command.get('body')
                    if command.get('action') == 'configure-mail':
                        if path != '/api/remote/settings': raise ValueError('Invalid settings path')
                        payload = dict(payload or {})
                        payload['password'] = getpass.getpass('Mail authorization code (hidden): ')
                    if command.get('manifest'):
                        payload = json.loads(Path(command['manifest']).read_text(encoding='utf-8'))
                    binary = Path(command['uploadFile']).read_bytes() if command.get('uploadFile') else None
                    request = urllib.request.Request(
                        'http://127.0.0.1:' + ports[port] + path,
                        headers={'Host': '127.0.0.1:' + str(port), 'Authorization': 'Bearer ' + token,
                                 'Content-Type': 'application/octet-stream' if binary is not None else 'application/json'},
                        data=binary if binary is not None else (json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None))
                    try:
                        with opener.open(request, timeout=45) as response: result = json.load(response)
                    except urllib.error.HTTPError as error:
                        result = {'http_status': error.code, 'response': json.load(error)}
                    if command.get('save'):
                        destination = Path(command['save'])
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                        result = {'saved': str(destination), 'http_status': result.get('http_status', 200)}
                    elif command.get('compact') and isinstance(result.get('task'), dict):
                        task = result['task']
                        result = {'task': {key: value for key, value in task.items() if key != 'prompt'},
                                  **{key: value for key, value in result.items() if key not in ('task', 'events')}}
                print(json.dumps(result, ensure_ascii=False), flush=True)
            except Exception as error:
                print(json.dumps({'error_type': type(error).__name__}), flush=True)
    finally:
        for forwarded in ports.values():
            subprocess.run([str(ADB), *(['-s', SERIAL] if SERIAL else []), 'forward', '--remove', 'tcp:' + forwarded], capture_output=True)
        token = None
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
