import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("fetch_offline_inputs.py")
SPEC = importlib.util.spec_from_file_location("fetch_offline_inputs", SCRIPT)
fetch_offline_inputs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch_offline_inputs)


class Response:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size=-1):
        if self.offset >= len(self.data):
            return b""
        end = min(len(self.data), self.offset + (min(size, 3) if size >= 0 else 3))
        chunk = self.data[self.offset:end]
        self.offset = end
        return chunk


class Opener:
    def __init__(self, data):
        self.data = data
        self.calls = 0

    def open(self, url, timeout):
        self.calls += 1
        return Response(self.data)


class FetchOfflineInputsTests(unittest.TestCase):
    def test_download_verifies_before_atomic_publish_and_reuses_valid_cache(self):
        payload = b"pinned test payload"
        checksum = hashlib.sha256(payload).hexdigest()
        opener = Opener(payload)
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            target = fetch_offline_inputs.fetch_one(output, "input.tar", "https://fixed.invalid/input.tar", checksum, opener)
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(opener.calls, 1)
            self.assertEqual(list(output.iterdir()), [target])
            again = fetch_offline_inputs.fetch_one(output, "input.tar", "https://fixed.invalid/input.tar", checksum, opener)
            self.assertEqual(again, target)
            self.assertEqual(opener.calls, 1)

    def test_bad_download_is_never_published_and_partial_is_removed(self):
        opener = Opener(b"not the pinned archive")
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            with self.assertRaisesRegex(ValueError, "Checksum verification failed"):
                fetch_offline_inputs.fetch_one(output, "input.tar", "https://fixed.invalid/input.tar", "0" * 64, opener)
            self.assertEqual(list(output.iterdir()), [])

    def test_corrupt_cache_is_replaced_only_after_valid_download(self):
        payload = b"verified replacement"
        checksum = hashlib.sha256(payload).hexdigest()
        opener = Opener(payload)
        with tempfile.TemporaryDirectory() as td:
            output = Path(td)
            target = output / "input.tar"
            target.write_bytes(b"corrupt")
            fetch_offline_inputs.fetch_one(output, target.name, "https://fixed.invalid/input.tar", checksum, opener)
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(list(output.iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
