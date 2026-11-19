import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.vllm_fault_monitor import (
    CommandResult,
    GATEWAY_API_KEY_ENV,
    GATEWAY_AUTO_DISABLED,
    GATEWAY_USER_ID_ENV,
    GatewayClient,
    Monitor,
    RemoteExecutor,
    atomic_write_json,
    append_incident_evidence,
    build_incident,
    npu_health_ok,
    read_json,
    validate_manifest,
    _find_open_incident,
    _service_probe,
    _signals,
)


class FaultProbeTests(unittest.TestCase):
    def service(self, **checks):
        return {
            "service_id": "decode-0",
            "host": "node-a",
            "container": "decode-0",
            "process_check_command": ["docker", "exec", "decode-0", "pgrep", "-af", "vllm"],
            **checks,
        }

    def test_direct_curl_checks_preserve_distinct_failure_types(self) -> None:
        command = ["curl", "-fsS", "-w", "http=%{http_code}", "http://localhost/health"]
        cases = [
            (CommandResult(22, "http=503", ""), "http_5xx", 503),
            (CommandResult(22, "http=401", "unauthorized"), "http_4xx", 401),
            (CommandResult(22, "", "HTTP error"), "http_error", None),
            (CommandResult(7, "http=000", "connection refused"), "http_connect_failed", None),
            (CommandResult(28, "http=200", "operation timed out"), "http_timeout", 200),
            (CommandResult(18, "http=200", "transfer closed early"), "http_partial_response", 200),
        ]
        for result, kind, http_status in cases:
            with self.subTest(kind=kind):
                executor = Mock(spec=RemoteExecutor)
                executor.run.side_effect = [
                    CommandResult(0, "running\tfalse\t0", ""),
                    CommandResult(0, "123 vllm", ""),
                    result,
                ]
                observation, _ = _service_probe(executor, self.service(readiness_command=command))
                details = observation["probe_results"]["readiness"]
                self.assertEqual(details["kind"], kind)
                self.assertEqual(details.get("http_status"), http_status)
                self.assertEqual(_signals(observation), ["readiness_failed"])

    def test_probe_errors_do_not_report_process_npu_or_business_failure(self) -> None:
        executor = Mock(spec=RemoteExecutor)
        executor.run.side_effect = [
            CommandResult(0, "running\tfalse\t0", ""),
            CommandResult(124, "", "command timed out", timed_out=True),
            CommandResult(255, "", "connection closed"),
            CommandResult(127, "", "curl: command not found"),
        ]
        service = self.service(
            npu_command=["npu-smi", "info"],
            business_probe_command=["curl", "-f", "-w", "http=%{http_code}", "http://localhost/"],
        )
        with self.assertLogs("vllm_fault_monitor", level="WARNING"):
            observation, _ = _service_probe(executor, service)
        self.assertIsNone(observation["process_present"])
        self.assertIsNone(observation["npu_healthy"])
        self.assertIsNone(observation["business_ok"])
        self.assertEqual(observation["probe_results"]["process"]["kind"], "command_timeout")
        self.assertEqual(observation["probe_results"]["npu"]["kind"], "ssh_failed")
        self.assertEqual(observation["probe_results"]["business_probe"]["kind"], "command_execution_failed")
        self.assertEqual(_signals(observation), ["probe_execution_error"])

    def test_business_probe_checks_json_error_and_preserves_curl_result(self) -> None:
        cases = [
            ('{"error":{"message":"generation failed"}}', True, 200, "business_response_error"),
            ('{"error":"generation failed"}', False, None, "business_response_error"),
            ('{"choices":[],"error":null}', True, 200, "ok"),
            ("data: [DONE]\n\n", False, None, "ok"),
            ("", True, 200, "ok"),
        ]
        for body, write_out, status, kind in cases:
            with self.subTest(body=body, write_out=write_out):
                command = ["curl", "-fsS"]
                if write_out:
                    command.extend(["--write-out", "\\nhttp=%{http_code}\\n"])
                command.append("http://localhost/v1/chat/completions")
                stdout = f"{body}\nhttp=200\n" if write_out else body
                executor = Mock(spec=RemoteExecutor)
                executor.run.side_effect = [
                    CommandResult(0, "running\tfalse\t0", ""),
                    CommandResult(0, "123 vllm", ""),
                    CommandResult(0, stdout, "curl progress"),
                ]
                observation, evidence = _service_probe(
                    executor, self.service(business_probe_command=command)
                )
                detail = observation["probe_results"]["business_probe"]
                self.assertEqual(detail["kind"], kind)
                self.assertEqual(detail.get("http_status"), status)
                self.assertEqual(detail["returncode"], 0)
                expected = ["business_failed"] if kind == "business_response_error" else []
                self.assertEqual(_signals(observation), expected)
                if expected:
                    self.assertIn("generation failed", evidence[-1])

    def test_probe_error_incident_does_not_block_service_fault_in_same_group(self) -> None:
        service = self.service(
            recovery_group="decode-group",
            readiness_command=["bash", "-c", "echo '"],
            business_probe_command=["curl", "--invalid-option", "http://localhost/"],
        )
        probe_errors = [
            CommandResult(2, "", "bash: -c: line 1: unexpected EOF while looking for matching `''"),
            CommandResult(2, "", "curl: option --invalid-option: is unknown"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor-manifest.json"
            atomic_write_json(path, {"deployment_key": "deployment-1", "services": [service]})
            monitor = Monitor(path)
            monitor.executor = Mock(spec=RemoteExecutor)
            monitor.executor.run.side_effect = [
                CommandResult(0, "running\tfalse\t0", ""),
                CommandResult(0, "123 vllm", ""),
                *probe_errors,
                CommandResult(0, "exited\tfalse\t1", ""),
                *probe_errors,
            ]
            monitor.probe(service, None)
            probe_path = monitor.report_queue.get_nowait()
            probe_event = read_json(probe_path, {})
            self.assertEqual(probe_event["signals"], ["probe_execution_error"])
            details = probe_event["observation"]["probe_results"]
            self.assertEqual(details["readiness"]["kind"], "shell_syntax_error")
            self.assertEqual(details["business_probe"]["kind"], "curl_execution_failed")
            probe_event["status"] = "processing"
            atomic_write_json(probe_path, probe_event)
            self.assertIsNone(monitor._processing_dependency_incident({"depends_on": ["decode-group"]}))

            monitor.probe(service, None)
            service_path = monitor.report_queue.get_nowait()
            self.assertNotEqual(service_path, probe_path)
            self.assertEqual(read_json(service_path, {})["signals"], ["container_exited"])
            self.assertEqual(read_json(probe_path, {})["signals"], ["probe_execution_error"])
            self.assertTrue(monitor.report_queue.empty())

    def test_unknown_probe_is_reported_after_three_periodic_checks(self) -> None:
        failures = [
            ("command_timeout", CommandResult(124, "", "command timed out", timed_out=True)),
            ("ssh_failed", CommandResult(255, "", "connection closed")),
            ("process_check_failed", CommandResult(1, "", "OCI runtime exec failed")),
        ]
        for kind, failure in failures:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "monitor-manifest.json"
                atomic_write_json(path, {"services": [self.service()]})
                monitor = Monitor(path)
                monitor.executor = Mock(spec=RemoteExecutor)
                monitor.executor.run.side_effect = [
                    CommandResult(0, "running\tfalse\t0", ""), failure
                ] * 6
                event = {"host": "node-a", "container": "decode-0"}
                monitor.probe_all()
                monitor.handle_runtime_event(event)
                monitor.handle_runtime_event(event)
                monitor.probe_all()
                self.assertTrue(monitor.report_queue.empty())

                monitor.probe_all()
                event_path = monitor.report_queue.get_nowait()
                incident = read_json(event_path, {})
                self.assertEqual(incident["signals"], ["probe_execution_error"])
                self.assertEqual(incident["confirmation_count"], 3)
                detail = incident["observation"]["probe_results"]["process"]
                self.assertEqual((detail["outcome"], detail["kind"]), ("unknown", kind))
                self.assertIn(failure.stderr, " ".join(incident["evidence"][0]["items"]))
                self.assertIn(
                    "process: inconclusive for at least 3 consecutive periodic checks",
                    incident["evidence"][0]["items"],
                )

                monitor.probe_all()
                self.assertTrue(monitor.report_queue.empty())
                self.assertEqual(read_json(event_path, {})["confirmation_count"], 4)

    def test_unknown_streaks_reset_on_conclusive_results_and_skipped_periodic_checks(self) -> None:
        unknown = {"outcome": "unknown", "kind": "command_timeout"}
        ok = {"outcome": "ok", "kind": "ok"}
        failed_process = {"probe_results": {"process": unknown}}
        failed_readiness = {"probe_results": {"readiness": unknown}}
        recovered_process = {"probe_results": {"process": ok}}
        rounds = [
            failed_process,
            failed_readiness,
            failed_process,
            failed_process,
            recovered_process,
            failed_process,
            failed_process,
            recovered_process,
            failed_process,
            failed_process,
            failed_process,
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor-manifest.json"
            atomic_write_json(path, {"services": [self.service()]})
            monitor = Monitor(path)
            event = {"host": "node-a", "container": "decode-0"}
            with patch(
                "scripts.vllm_fault_monitor._service_probe",
                side_effect=[(observation, []) for observation in rounds],
            ):
                for _ in range(7):
                    monitor.probe_all()
                    self.assertTrue(monitor.report_queue.empty())
                monitor.handle_runtime_event(event)
                for _ in range(2):
                    monitor.probe_all()
                    self.assertTrue(monitor.report_queue.empty())
                monitor.probe_all()
                incident = read_json(monitor.report_queue.get_nowait(), {})
            self.assertEqual(incident["signals"], ["probe_execution_error"])
            self.assertEqual(incident["confirmation_count"], 3)

    def test_inspect_ssh_failure_only_reports_the_host_fault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor-manifest.json"
            atomic_write_json(path, {"services": [self.service()]})
            monitor = Monitor(path)
            monitor.executor = Mock(spec=RemoteExecutor)
            monitor.executor.run.return_value = CommandResult(255, "", "connection closed")
            monitor.probe_all()
            event_path = monitor.report_queue.get_nowait()
            self.assertEqual(read_json(event_path, {})["signals"], ["ssh_unreachable"])
            monitor.probe_all()
            monitor.probe_all()
            self.assertTrue(monitor.report_queue.empty())
            self.assertEqual(list(monitor.events_dir.glob("incident-*.json")), [event_path])

    def test_inspect_timeout_is_not_a_docker_runtime_error(self) -> None:
        executor = Mock(spec=RemoteExecutor)
        executor.run.return_value = CommandResult(124, "", "command timed out", timed_out=True)
        with self.assertLogs("vllm_fault_monitor", level="WARNING"):
            observation, _ = _service_probe(executor, self.service())
        executor.run.assert_called_once()
        self.assertEqual(observation["container_status"], "unknown")
        self.assertEqual(_signals(observation), [])

    def test_restarting_container_is_reported_without_checking_process_absence(self) -> None:
        executor = Mock(spec=RemoteExecutor)
        executor.run.return_value = CommandResult(0, "restarting\tfalse\t1", "")
        observation, _ = _service_probe(executor, self.service())
        executor.run.assert_called_once()
        self.assertIsNone(observation["process_present"])
        self.assertEqual(_signals(observation), ["container_not_running"])

    def test_process_absence_requires_a_valid_process_check(self) -> None:
        for stderr, expected in [("", ["process_missing"]), ("OCI runtime exec failed", [])]:
            with self.subTest(stderr=stderr):
                executor = Mock(spec=RemoteExecutor)
                executor.run.side_effect = [
                    CommandResult(0, "running\tfalse\t0", ""),
                    CommandResult(1, "", stderr),
                ]
                observation, _ = _service_probe(executor, self.service())
                self.assertEqual(_signals(observation), expected)

    def test_oom_incident_only_publishes_confirmed_signals(self) -> None:
        service = self.service(readiness_command=["curl", "-f", "http://localhost/health"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor-manifest.json"
            atomic_write_json(path, {"deployment_key": "deployment-1", "services": [service]})
            monitor = Monitor(path)
            executor = Mock(spec=RemoteExecutor)
            executor.run.side_effect = [
                CommandResult(0, "exited\ttrue\t137", ""),
                CommandResult(7, "", "connection refused"),
            ] * 3
            monitor.executor = executor
            monitor.probe(service, {"Action": "oom"})
            event_path = monitor.report_queue.get_nowait()
            first = read_json(event_path, {})
            self.assertEqual(first["signals"], ["container_exited", "oom_killed"])
            self.assertFalse(first["observation"]["readiness_ok"])
            self.assertIsNone(first["observation"]["process_present"])
            monitor.probe(service, None)
            self.assertNotIn("readiness_failed", read_json(event_path, {})["signals"])
            monitor.probe(service, None)
            final = read_json(event_path, {})
            self.assertEqual(final["signals"], ["container_exited", "oom_killed", "readiness_failed"])
            self.assertEqual(final["confirmation_count"], 3)
            self.assertTrue(monitor.report_queue.empty())


class FaultMonitorTests(unittest.TestCase):
    def test_full_probe_runs_when_docker_events_keep_arriving(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor-manifest.json"
            atomic_write_json(path, {"services": [], "probe_interval_seconds": 30})
            monitor = Monitor(path)
            monitor.write_state = Mock()
            monitor.reporter = Mock()
            monitor.start_watchers = Mock()
            monitor.handle_runtime_event = Mock()
            clock = 0
            timeouts = []

            def next_event(timeout):
                nonlocal clock
                timeouts.append(timeout)
                self.assertLessEqual(len(timeouts), 3, "periodic sweep was delayed by events")
                clock += 10
                return {"container": "decode-0"}

            monitor.probe_events = Mock()
            monitor.probe_events.get.side_effect = next_event

            def full_probe():
                if clock >= 30:
                    monitor.stop_event.set()

            monitor.probe_all = Mock(side_effect=full_probe)
            with patch("scripts.vllm_fault_monitor.time.monotonic", side_effect=lambda: clock):
                self.assertEqual(monitor.run(), 0)
            self.assertEqual(timeouts, [30, 20, 10])
            self.assertEqual(monitor.handle_runtime_event.call_count, 3)
            self.assertEqual(monitor.probe_all.call_count, 2)

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
            signals=["container_exited"],
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

    def test_multiple_monitors_use_isolated_deployment_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            monitors = []
            for deployment_key, container in (
                ("dsv4-prod", "dsv4-0"),
                ("qwen-prod", "qwen-0"),
            ):
                deployment_dir = base / deployment_key
                deployment_dir.mkdir()
                manifest_path = deployment_dir / "monitor-manifest.json"
                atomic_write_json(
                    manifest_path,
                    {
                        "deployment_key": deployment_key,
                        "deployment_dir": str(deployment_dir),
                        "services": [
                            {
                                "service_id": container,
                                "host": "node-a",
                                "container": container,
                                "process_check_command": [
                                    "docker",
                                    "exec",
                                    container,
                                    "pgrep",
                                    "-af",
                                    "[v]llm",
                                ],
                            }
                        ],
                    },
                )
                monitors.append(Monitor(manifest_path))

            self.assertNotEqual(monitors[0].root, monitors[1].root)
            self.assertNotEqual(monitors[0].state_path, monitors[1].state_path)
            self.assertNotEqual(monitors[0].events_dir, monitors[1].events_dir)
            self.assertEqual(monitors[0].state_path.parent.name, "dsv4-prod")
            self.assertEqual(monitors[1].state_path.parent.name, "qwen-prod")

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
                signals=["process_missing"],
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
            signals=["process_missing"],
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


class GatewayIntegrationTests(unittest.TestCase):
    def service(self):
        return {
            "service_id": "proxy-0",
            "host": "node-a",
            "container": "proxy-0",
            "recovery_group": "pd-serving-group",
            "process_check_command": [
                "docker",
                "exec",
                "proxy-0",
                "pgrep",
                "-af",
                "proxy",
            ],
        }

    def make_monitor(self, directory: str) -> Monitor:
        path = Path(directory) / "monitor-manifest.json"
        atomic_write_json(
            path,
            {
                "deployment_key": "deployment-1",
                "services": [self.service()],
                "gateway": {
                    "enabled": True,
                    "base_url": "http://gateway:3000",
                    "poll_interval_seconds": 5,
                    "deployments": [
                        {
                            "gateway_deployment_id": "deployment-d1",
                            "service_id": "proxy-0",
                            "recovery_group": "pd-serving-group",
                        }
                    ],
                },
            },
        )
        return Monitor(path)

    def models_event(
        self,
        *,
        enabled: bool = False,
        auto_disabled: bool = True,
        updated_at: str = "2026-11-13 10:00:00+00",
    ):
        return {
            "models": [
                {
                    "deployment_id": "deployment-d1",
                    "model_name": "model-a",
                    "api_base": "http://node-a:8000/v1?token=must-not-be-recorded",
                    "enabled": enabled,
                    "auto_disabled": auto_disabled,
                    "updated_at": updated_at,
                }
            ]
        }

    def test_gateway_manifest_is_optional_and_rejects_embedded_credentials(self) -> None:
        validate_manifest({"services": [self.service()]})
        manifest = {
            "services": [self.service()],
            "gateway": {
                "base_url": "http://gateway:3000",
                "api_key": "secret",
                "deployments": [
                    {
                        "gateway_deployment_id": "deployment-d1",
                        "service_id": "proxy-0",
                    }
                ],
            },
        }
        with self.assertRaisesRegex(ValueError, "credentials"):
            validate_manifest(manifest)

    def test_start_watchers_keeps_docker_watcher_and_adds_gateway_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor = self.make_monitor(directory)
            with patch("scripts.vllm_fault_monitor.DockerEventWatcher") as docker_watcher, patch(
                "scripts.vllm_fault_monitor.GatewayWatcher"
            ) as gateway_watcher:
                monitor.start_watchers()
            docker_watcher.assert_called_once()
            gateway_watcher.assert_called_once()
            docker_watcher.return_value.start.assert_called_once()
            gateway_watcher.return_value.start.assert_called_once()

    def test_auto_disabled_creates_one_deduplicated_gateway_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor = self.make_monitor(directory)
            event = self.models_event()
            monitor.handle_gateway_models(event)
            path = monitor.report_queue.get_nowait()
            incident = read_json(path, {})
            self.assertEqual(incident["signals"], [GATEWAY_AUTO_DISABLED])
            self.assertEqual(incident["observation"]["auto_disable_cause"], "unknown")
            self.assertEqual(
                incident["observation"]["api_base"], "http://node-a:8000/v1"
            )
            self.assertEqual(len(incident["gateway_episodes"]), 1)
            self.assertEqual(incident["evidence"][0]["kind"], "gateway_models")

            monitor.handle_gateway_models(event)
            self.assertTrue(monitor.report_queue.empty())
            self.assertEqual(len(read_json(path, {})["evidence"]), 1)

    def test_gateway_signal_merges_into_existing_local_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor = self.make_monitor(directory)
            incident = build_incident(
                monitor.manifest,
                self.service(),
                {"container_status": "exited"},
                ["container exited"],
                signals=["container_exited"],
            )
            path = monitor.events_dir / f"{incident['incident_id']}.json"
            atomic_write_json(path, incident)

            monitor.handle_gateway_models(self.models_event())

            self.assertTrue(monitor.report_queue.empty())
            updated = read_json(path, {})
            self.assertEqual(
                updated["signals"], ["container_exited", GATEWAY_AUTO_DISABLED]
            )
            self.assertEqual(updated["evidence"][-1]["kind"], "gateway_models")

    def test_local_signal_merges_into_gateway_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor = self.make_monitor(directory)
            monitor.handle_gateway_models(self.models_event())
            path = monitor.report_queue.get_nowait()

            monitor._record_incident(
                self.service(),
                {"container_status": "exited"},
                ["container exited"],
                None,
                ["container_exited"],
                1,
                None,
            )

            self.assertTrue(monitor.report_queue.empty())
            self.assertEqual(
                read_json(path, {})["signals"],
                ["container_exited", GATEWAY_AUTO_DISABLED],
            )

    def test_rejoin_closes_episode_and_later_auto_disable_is_new_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor = self.make_monitor(directory)
            monitor.handle_gateway_models(self.models_event())
            first_path = monitor.report_queue.get_nowait()

            monitor.handle_gateway_models(
                self.models_event(
                    enabled=True,
                    auto_disabled=False,
                    updated_at="2026-11-13 10:05:00+00",
                )
            )
            first = read_json(first_path, {})
            self.assertIn("gateway_rejoined_at", first)
            self.assertEqual(
                set(first["gateway_rejoined_episodes"]),
                set(first["gateway_episodes"]),
            )
            first["status"] = "recovered"
            atomic_write_json(first_path, first)

            monitor.handle_gateway_models(
                self.models_event(updated_at="2026-11-13 11:00:00+00")
            )
            second_path = monitor.report_queue.get_nowait()
            self.assertNotEqual(first_path, second_path)

    def test_gateway_observer_error_requires_three_failures_and_stays_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor = self.make_monitor(directory)
            failure = {
                "kind": "gateway_auth_failed",
                "message": "Gateway returned HTTP 401",
            }
            monitor.handle_gateway_error(failure)
            monitor.handle_gateway_error(failure)
            self.assertTrue(monitor.report_queue.empty())
            monitor.handle_gateway_error(failure)
            probe_path = monitor.report_queue.get_nowait()
            probe_incident = read_json(probe_path, {})
            self.assertEqual(probe_incident["signals"], ["probe_execution_error"])
            self.assertEqual(probe_incident["confirmation_count"], 3)

            monitor.handle_gateway_models(self.models_event())
            service_path = monitor.report_queue.get_nowait()
            self.assertNotEqual(probe_path, service_path)
            self.assertEqual(read_json(service_path, {})["signals"], [GATEWAY_AUTO_DISABLED])

    def test_missing_gateway_deployment_requires_three_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor = self.make_monitor(directory)
            for _ in range(2):
                monitor.handle_gateway_models({"models": []})
                self.assertTrue(monitor.report_queue.empty())
            monitor.handle_gateway_models({"models": []})
            incident = read_json(monitor.report_queue.get_nowait(), {})
            self.assertEqual(incident["signals"], ["probe_execution_error"])
            detail = incident["observation"]["probe_results"]["gateway_models"]
            self.assertEqual(detail["kind"], "gateway_deployment_missing")

    def test_gateway_client_only_calls_login_and_models(self) -> None:
        class JsonResponse:
            status = 200

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(self.payload).encode("utf-8")

        client = GatewayClient("http://gateway:3000")
        client.opener = Mock()
        client.opener.open.side_effect = [
            JsonResponse({"ok": True}),
            JsonResponse({"models": [{"deployment_id": "deployment-d1"}]}),
        ]
        with patch.dict(
            os.environ,
            {
                GATEWAY_API_KEY_ENV: "secret",
                GATEWAY_USER_ID_ENV: "admin",
            },
            clear=False,
        ):
            models = client.list_models()

        self.assertEqual(models[0]["deployment_id"], "deployment-d1")
        requests = [call.args[0] for call in client.opener.open.call_args_list]
        self.assertEqual(
            [request.full_url for request in requests],
            [
                "http://gateway:3000/dashboard/api/auth/login",
                "http://gateway:3000/dashboard/api/admin/models",
            ],
        )
        self.assertEqual(
            json.loads(requests[0].data.decode("utf-8")),
            {"user_id": "admin", "api_key": "secret"},
        )

    def test_gateway_client_reauthenticates_once_after_401(self) -> None:
        class JsonResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(self.payload).encode("utf-8")

        client = GatewayClient("http://gateway:3000")
        client.opener = Mock()
        client.opener.open.side_effect = [
            JsonResponse({"ok": True}),
            urllib.error.HTTPError(
                "http://gateway:3000/dashboard/api/admin/models",
                401,
                "Unauthorized",
                {},
                None,
            ),
            JsonResponse({"ok": True}),
            JsonResponse({"models": []}),
        ]
        with patch.dict(os.environ, {GATEWAY_API_KEY_ENV: "secret"}, clear=False):
            self.assertEqual(client.list_models(), [])
        self.assertEqual(client.opener.open.call_count, 4)


if __name__ == "__main__":
    unittest.main()

