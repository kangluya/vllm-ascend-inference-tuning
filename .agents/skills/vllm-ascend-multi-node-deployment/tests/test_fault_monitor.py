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
    _find_open_incident,
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

    def test_signals_include_soft_observations(self) -> None:
        signals = _signals(
            {
                "readiness_ok": False,
                "log_error": True,
                "business_ok": False,
            }
        )
        self.assertEqual(
            signals,
            ["readiness_failed", "service_log_error", "business_failed"],
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
            self.assertEqual(len(updated["evidence"]), 2)
            self.assertEqual(updated["evidence"][1]["service"], "decode-1")
            self.assertEqual(updated["evidence"][1]["repeat_count"], 1)

    def _write_group_incident(
        self, directory: str, name: str, status: str, updated_at: str | None = None
    ) -> Path:
        incident = build_incident(
            {"deployment_key": "deployment-1"},
            {
                "service_id": "decode-0",
                "container": "decode-0",
                "recovery_group": "decode-group",
            },
            {"process_present": False},
            ["process missing"],
        )
        incident["status"] = status
        if updated_at is not None:
            incident["updated_at"] = updated_at
        path = Path(directory) / name
        atomic_write_json(path, incident)
        return path

    def test_unresolved_states_are_append_targets(self) -> None:
        for status in ("detected", "processing", "failed", "waiting_for_user"):
            with tempfile.TemporaryDirectory() as directory:
                path = self._write_group_incident(directory, "incident-a.json", status)
                self.assertEqual(
                    _find_open_incident(Path(directory), "decode-group"), path
                )

    def test_resolved_states_release_recovery_group(self) -> None:
        for status in ("recovered", "recovered_degraded"):
            with tempfile.TemporaryDirectory() as directory:
                self._write_group_incident(directory, "incident-a.json", status)
                self.assertIsNone(_find_open_incident(Path(directory), "decode-group"))

    def test_persistent_fault_merges_repeated_findings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_group_incident(
                directory, "incident-a.json", "waiting_for_user"
            )
            first_seen = read_json(path, {})["evidence"][0]["first_seen_at"]
            append_incident_evidence(
                path,
                {
                    "service_id": "decode-0",
                    "container": "decode-0",
                    "recovery_group": "decode-group",
                },
                {"process_present": False},
                ["process missing"],
                ["process_missing"],
            )
            updated = read_json(path, {})
            self.assertEqual(updated["status"], "waiting_for_user")
            self.assertEqual(len(updated["evidence"]), 1)
            entry = updated["evidence"][0]
            self.assertEqual(entry["repeat_count"], 2)
            self.assertEqual(entry["first_seen_at"], first_seen)
            self.assertGreaterEqual(entry["last_seen_at"], first_seen)

    def test_changed_findings_append_new_evidence_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_group_incident(directory, "incident-a.json", "detected")
            append_incident_evidence(
                path,
                {
                    "service_id": "decode-0",
                    "container": "decode-0",
                    "recovery_group": "decode-group",
                },
                {"process_present": False, "container_status": "exited"},
                ["process missing", "container exited"],
                ["process_missing", "container_exited"],
            )
            updated = read_json(path, {})
            self.assertEqual(len(updated["evidence"]), 2)
            entry = updated["evidence"][-1]
            self.assertEqual(entry["items"], ["process missing", "container exited"])
            self.assertEqual(entry["repeat_count"], 1)
            self.assertEqual(entry["first_seen_at"], entry["last_seen_at"])

    def test_interleaved_services_do_not_cross_merge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_group_incident(directory, "incident-a.json", "detected")
            for service_id in ("decode-1", "decode-0"):
                append_incident_evidence(
                    path,
                    {
                        "service_id": service_id,
                        "container": service_id,
                        "recovery_group": "decode-group",
                    },
                    {"process_present": False},
                    ["process missing"],
                    ["process_missing"],
                )
            updated = read_json(path, {})
            self.assertEqual(len(updated["evidence"]), 2)
            repeats = {
                entry["service"]: entry["repeat_count"]
                for entry in updated["evidence"]
            }
            self.assertEqual(repeats, {"decode-0": 2, "decode-1": 1})

    def test_stale_matching_entry_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_group_incident(directory, "incident-a.json", "detected")
            for findings, finding_signals in (
                (["container exited"], ["container_exited"]),
                (["process missing"], ["process_missing"]),
            ):
                append_incident_evidence(
                    path,
                    {
                        "service_id": "decode-0",
                        "container": "decode-0",
                        "recovery_group": "decode-group",
                    },
                    {"process_present": False},
                    findings,
                    finding_signals,
                )
            updated = read_json(path, {})
            self.assertEqual(len(updated["evidence"]), 3)
            self.assertEqual(updated["evidence"][-1]["repeat_count"], 1)

    def test_suppressed_dependency_entries_merge_by_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write_group_incident(directory, "incident-a.json", "detected")
            for _ in range(2):
                append_incident_evidence(
                    path,
                    {
                        "service_id": "decode-1",
                        "container": "decode-1",
                        "recovery_group": "decode-group",
                    },
                    {"readiness_ok": False},
                    ["readiness: connection refused"],
                    ["readiness_failed"],
                    suppressed_by="decode-0",
                )
            updated = read_json(path, {})
            self.assertEqual(len(updated["evidence"]), 2)
            entry = updated["evidence"][-1]
            self.assertEqual(entry["kind"], "suppressed_dependency")
            self.assertEqual(entry["suppressed_by"], "decode-0")
            self.assertEqual(entry["repeat_count"], 2)

    def test_newest_unresolved_incident_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._write_group_incident(
                directory, "incident-old.json", "failed", "2025-01-01T00:00:00Z"
            )
            newest = self._write_group_incident(
                directory, "incident-new.json", "waiting_for_user", "2025-01-02T00:00:00Z"
            )
            self.assertEqual(
                _find_open_incident(Path(directory), "decode-group"), newest
            )

    def test_atomic_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_write_json(path, {"status": "running"})
            self.assertEqual(read_json(path, {}), {"status": "running"})
            self.assertFalse(path.with_name(".state.json.unknown.tmp").exists())


if __name__ == "__main__":
    unittest.main()

