"""Run only the packaged local fixtures and export a bounded diagnostic report."""
import json
from pathlib import Path
import subprocess
import sys

if __name__ == '__main__':
    stage = Path('/sdcard/Download/daxqo-stage/backend')
    result = subprocess.run([sys.executable, '-m', 'unittest', '-v', 'test_server',
                             'test_supervisor', 'test_runtime_control'],
                            cwd=stage, capture_output=True, text=True, timeout=180)
    destination = Path('/sdcard/Download/daxqo-stage/test-diagnostic.json')
    destination.write_text(json.dumps({'exitCode': result.returncode,
        'stdout': result.stdout[-5000:], 'stderr': result.stderr[-24000:]},
        ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'exitCode': result.returncode, 'report': str(destination)}))
