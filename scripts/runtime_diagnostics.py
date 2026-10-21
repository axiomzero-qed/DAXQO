"""Report only runtime capabilities; never read provider or pairing credentials."""
import json
import os
from pathlib import Path
import subprocess

APP = Path('/root/phone-harness')
REPORT = Path('/sdcard/Download/daxqo-stage/runtime-diagnostic.json')

if __name__ == '__main__':
    binary = APP / 'runtime/codex-0.163.0/bin/codex'
    env = dict(os.environ)
    # This local CLI query makes no model requests.
    result = subprocess.run([str(binary), 'features', 'list'], env=env,
        capture_output=True, text=True, timeout=30)
    relevant = [line for line in result.stdout.splitlines()
                if any(term in line.lower() for term in ('agent', 'compact', 'collab'))]
    data = binary.read_bytes()
    keys = ('model_auto_compact_token_limit', 'model_context_window',
            'tool_output_token_limit', 'max_concurrent_threads_per_session', 'max_threads', 'max_depth')
    report = {'featuresExitCode': result.returncode, 'featureLines': relevant,
              'compiledConfigNames': {key: key.encode() in data for key in keys}}
    REPORT.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(REPORT), 'readModelKeys': False}))
