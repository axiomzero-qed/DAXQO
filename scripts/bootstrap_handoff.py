"""Run once through the already-paired prototype; never reads a model key."""
import json
from pathlib import Path

APP = Path('/root/phone-harness')
STAGE = Path('/sdcard/Download/daxqo-stage')

if __name__ == '__main__':
    capability = (APP / 'state' / 'capability').read_text(encoding='utf-8').strip()
    if not capability:
        raise SystemExit('The existing local workbench is not paired')
    STAGE.mkdir(parents=True, exist_ok=True)
    handoff = STAGE / 'gui-handoff.json'
    handoff.write_text(json.dumps({'url': 'http://127.0.0.1:18880/#token=' + capability}), encoding='utf-8')
    handoff.chmod(0o600)
    print(json.dumps({'ready': True, 'handoff': str(handoff)}))
