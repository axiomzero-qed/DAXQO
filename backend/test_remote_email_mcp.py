import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from remote_email_mcp import serve_stdio


class RemoteEmailMCPStdioTests(unittest.TestCase):
    def test_non_object_json_gets_invalid_request_then_server_continues(self):
        source = io.StringIO('1\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n')
        output = io.StringIO()
        serve_stdio(object(), source, output)

        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0], {
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32600, "message": "Invalid request"},
        })
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 7, "result": {}})


if __name__ == "__main__":
    unittest.main()
