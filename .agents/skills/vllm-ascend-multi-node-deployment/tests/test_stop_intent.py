import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.vllm_fault_monitor import write_json


class StopIntentTests(unittest.TestCase):
    def test_stop_intent_is_written_atomically_as_json(self) -> None:
        path = Path("/data/boom/deployment/stop-intent.json")
        with patch("scripts.vllm_fault_monitor.Path.write_text") as write_text:
            with patch("scripts.vllm_fault_monitor.os.replace") as replace:
                with patch("scripts.vllm_fault_monitor.Path.mkdir") as mkdir:
                    payload = {"status": "requested", "targets": [{"container": "node-a"}]}
                    write_json(path, payload)
                    mkdir.assert_called_once_with(parents=True, exist_ok=True)
                    write_text.assert_called_once()
                    replace.assert_called_once_with(path.with_suffix(path.suffix + ".tmp"), path)


if __name__ == "__main__":
    unittest.main()
