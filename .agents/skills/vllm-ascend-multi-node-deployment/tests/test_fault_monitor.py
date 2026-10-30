import tempfile
import unittest
from pathlib import Path

from scripts.vllm_fault_monitor import (
    atomic_write_json,
    append_incident_evidence,
    build_incident,
    npu_health_ok,
    read_json,
    validate_manifest,
    _signals,
)


class FaultMonitorTests(unittest.TestCase):
    def test_npu_health_parser(self) -> None:
        self.assertTrue(npu_health_ok("Health : OK\n", 0))
        self.assertTrue(npu_health_ok("npu-smi info\n", 0))
        self.assertFalse(npu_health_ok("Health : Fault\n", 0))
        self.assertFalse(npu_health_ok("", 1))

    def test_incident_contains_detection_only(self) -> None:
        manifest = {"deployment_key": "deployment-1"}
        service = {
            "service_id": "decode-0",
            "host": "node-a",
            "container": "decode-0",
            "recovery_group": "decode-group",
        }
        observation = {
            "service_id": "decode-0",
            "host": "node-a",
            "container_status": "exited",
            "process_present": False,
            "readiness_ok": False,
        }
        incident = build_incident(
            manifest,
            service,
            observation,
            ["docker_inspect: exited"],
        )
        self.assertEqual(incident["status"], "detected")
        self.assertIn("container_exited", incident["signals"])
        self.assertIsNone(incident["diagnosis"])
        self.assertIsNone(incident["recovery"])
        self.assertEqual(incident["recovery_group"], "decode-group")

    def test_signals_include_configured_health_checks(self) -> None:
        signals = _signals(
            {
                "container_health": "unhealthy",
                "log_error": True,
                "business_ok": False,
            }
        )
        self.assertEqual(
            signals,
            ["container_unhealthy", "service_log_error", "business_failed"],
        )

    def test_process_check_command_must_target_container(self) -> None:
        validate_manifest(
            {
                "services": [
                    {
                        "service_id": "decode-0",
                        "container": "decode-0",
                        "process_check_command": [
                            "docker",
                            "exec",
                            "decode-0",
                            "pgrep",
                            "-af",
                            "vllm",
                        ],
                    }
                ]
            }
        )
        with self.assertRaises(ValueError):
            validate_manifest(
                {
                    "services": [
                        {
                            "service_id": "decode-0",
                            "container": "decode-0",
                            "process_check_command": ["pgrep", "-af", "vllm"],
                        }
                    ]
                }
            )

    def test_open_group_incident_collects_new_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "incident.json"
            first = build_incident(
                {"deployment_key": "deployment-1"},
                {
                    "service_id": "decode-0",
                    "container": "decode-0",
                    "recovery_group": "decode-group",
                },
                {"process_present": False},
                ["process missing"],
            )
            atomic_write_json(path, first)
            append_incident_evidence(
                path,
                {
                    "service_id": "decode-1",
                    "container": "decode-1",
                    "recovery_group": "decode-group",
                },
                {"container_status": "exited"},
                ["container exited"],
                ["container_exited"],
            )
            updated = read_json(path, {})
            self.assertEqual(updated["affected_services"], ["decode-0", "decode-1"])
            self.assertIn("container_exited", updated["signals"])

    def test_atomic_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_write_json(path, {"status": "running"})
            self.assertEqual(read_json(path, {}), {"status": "running"})
            self.assertFalse(path.with_name(".state.json.unknown.tmp").exists())


if __name__ == "__main__":
    unittest.main()

