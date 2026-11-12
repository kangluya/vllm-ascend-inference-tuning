#!/usr/bin/env python3
"""Resident vLLM fault detector and Pi HTTP notifier.

The monitor observes deployment facts from monitor-manifest.json. It records
facts and emits incident files; recovery decisions remain in the Pi Skill.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import signal
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

LOGGER = logging.getLogger("vllm_fault_monitor")
# Resolved states release the recovery group for a new incident; failed and
# waiting_for_user incidents keep an unresolved fault and stay append targets.
RESOLVED_STATES = {"recovered", "recovered_degraded"}
MAX_EVIDENCE = 2000
MAX_INCIDENT_EVIDENCE = 100
SOFT_SIGNAL_CONFIRMATIONS = 3
UNKNOWN_PROBE_CONFIRMATIONS = 3
PROBE_EXECUTION_ERROR = "probe_execution_error"
PROBE_EXECUTION_ERROR_KINDS = {
    "command_execution_failed",
    "curl_execution_failed",
    "shell_syntax_error",
}
PERSISTENT_UNKNOWN_KINDS = {"command_timeout", "ssh_failed", "process_check_failed"}
HARD_SIGNALS = {
    "ssh_unreachable",
    "container_exited",
    "oom_killed",
    "docker_runtime_error",
    "process_missing",
    "npu_unhealthy",
}
SOFT_SIGNALS = {
    "container_not_running",
    "readiness_failed",
    "service_log_error",
    "business_failed",
}
HTTP_STATUS_LINE = re.compile(r"^http=([1-5][0-9]{2})(?:\s|$)")
SHELL_SYNTAX_ERROR = re.compile(
    r"^(?:[^:\n]*/)?(?:sh|bash|dash|ash|ksh|zsh|[^:/\n]+\.sh): "
    r"(?:-c: )?(?:(?:line )?\d+: )?(?:syntax error\b|unexpected EOF while looking for matching\b)",
    re.IGNORECASE | re.MULTILINE,
)
CURL_FAILURES = {
    6: "http_dns_failed",
    7: "http_connect_failed",
    18: "http_partial_response",
    22: "http_error",
    28: "http_timeout",
    52: "http_empty_response",
    56: "http_receive_failed",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def process_start_time(pid: int) -> str:
    stat_path = Path(f"/proc/{pid}/stat")
    fields = stat_path.read_text(encoding="utf-8").split()
    if len(fields) <= 21:
        raise RuntimeError(f"invalid process stat for pid {pid}")
    return fields[21]


def truncate(value: str) -> str:
    value = value.strip()
    return value if len(value) <= MAX_EVIDENCE else value[:MAX_EVIDENCE] + "...<truncated>"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class RemoteExecutor:
    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def run(self, host: str, argv: Sequence[str]) -> CommandResult:
        remote = " ".join(shlex.quote(str(part)) for part in argv)
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            host,
            remote,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                124,
                str(exc.stdout or ""),
                str(exc.stderr or "") + " command timed out",
                timed_out=True,
            )
        except OSError as exc:
            return CommandResult(-1, "", str(exc))
        return CommandResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )


def npu_health_ok(output: str, returncode: int) -> bool:
    if returncode != 0:
        return False
    lowered = output.lower()
    bad_markers = ("fault", "unhealthy", "not ok", "no device", "error")
    return not any(marker in lowered for marker in bad_markers)


def _host(service: Mapping[str, Any]) -> str:
    return str(service.get("ssh_target") or service.get("host") or "")


def _command(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        return None
    return value


def _recovery_group(service: Mapping[str, Any]) -> str:
    service_id = str(service.get("service_id") or service.get("container") or "unknown")
    return str(service.get("recovery_group") or service_id)


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    services = manifest.get("services")
    if not isinstance(services, list):
        raise ValueError("manifest services must be a list")
    for service in services:
        if not isinstance(service, Mapping):
            raise ValueError("each service in manifest must be an object")
        service_id = str(service.get("service_id") or service.get("container") or "unknown")
        container = str(service.get("container") or "")
        command = _command(service.get("process_check_command"))
        if not container or command is None:
            raise ValueError(
                f"service {service_id} requires process_check_command and container"
            )
        if len(command) < 3 or command[0] != "docker":
            raise ValueError(
                f"service {service_id} process_check_command must be container-scoped"
            )
        if command[1] not in {"exec", "top"} or command[2] != container:
            raise ValueError(
                f"service {service_id} process_check_command must target {container}"
            )


def _probe_details(
    name: str, command: Sequence[str], result: CommandResult, elapsed_ms: int
) -> dict[str, Any]:
    outcome, kind = ("ok", "ok") if result.ok else ("failed", "check_failed")
    http_status = None
    if result.timed_out:
        outcome, kind = "unknown", "command_timeout"
    elif result.returncode == 255:
        outcome, kind = "unknown", "ssh_failed"
    elif result.returncode < 0 or result.returncode in {126, 127}:
        outcome, kind = "unknown", "command_execution_failed"
    elif not result.ok and SHELL_SYNTAX_ERROR.search(result.stderr):
        outcome, kind = "unknown", "shell_syntax_error"
    elif name in {"readiness", "business_probe"} and command[0].rsplit("/", 1)[-1] == "curl":
        body = result.stdout
        # Only interpret an explicit curl write-out marker, not a response body or log line.
        if any(
            option in {"-w", "--write-out"} and "http=%{http_code}" in fmt
            for option, fmt in zip(command, command[1:])
        ):
            before_status, _, last_line = body.rstrip().rpartition("\n")
            match = HTTP_STATUS_LINE.match(last_line)
            if match:
                http_status = int(match[1])
                body = before_status
        if result.returncode in {0, 22} and http_status is not None and http_status >= 400:
            outcome, kind = "failed", "http_5xx" if http_status >= 500 else "http_4xx"
        elif result.returncode in {1, 2, 3, 4, 23, 26, 27, 48, 77}:
            outcome, kind = "unknown", "curl_execution_failed"
        elif not result.ok:
            kind = CURL_FAILURES.get(result.returncode, "http_check_failed")
        elif name == "business_probe":
            try:
                response = json.loads(body)
            except (ValueError, RecursionError):
                response = None
            if isinstance(response, dict) and response.get("error") is not None:
                outcome, kind = "failed", "business_response_error"
    elif name == "process" and not result.ok:
        # A failed docker top cannot prove absence. For docker exec, only a clean
        # negative check (e.g. pgrep exiting 1) is evidence of a missing process.
        if command[1] != "exec" or result.returncode != 1 or result.stderr.strip():
            outcome, kind = "unknown", "process_check_failed"
        else:
            kind = "process_not_found"
    elif name == "npu" and not npu_health_ok(result.stdout, result.returncode):
        outcome, kind = "failed", "npu_check_failed"
    detail = {
        "outcome": outcome,
        "kind": kind,
        "returncode": result.returncode,
        "elapsed_ms": elapsed_ms,
    }
    if http_status is not None:
        detail["http_status"] = http_status
    return detail


def _service_probe(
    executor: RemoteExecutor,
    service: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    service_id = str(service.get("service_id") or service.get("container") or "unknown")
    host = _host(service)
    container = str(service.get("container") or "")
    probe_results: dict[str, dict[str, Any]] = {}
    observation: dict[str, Any] = {
        "service_id": service_id,
        "host": host,
        "container": container,
        "ssh_reachable": True,
        "container_status": "unknown",
        "process_present": None,
        "readiness_ok": None,
        "npu_healthy": None,
        "log_error": None,
        "business_ok": None,
        "probe_results": probe_results,
    }
    evidence: list[str] = []

    if not host or not container:
        observation["probe_error"] = "manifest missing host or container"
        return observation, ["manifest_missing_host_or_container"]

    def run_probe(name: str, command: Sequence[str]) -> CommandResult:
        start = time.monotonic()
        result = executor.run(host, command)
        detail = _probe_details(name, command, result, int((time.monotonic() - start) * 1000))
        probe_results[name] = detail
        status = f" http={detail['http_status']}" if "http_status" in detail else ""
        summary = f"{name}: {detail['kind']} rc={result.returncode}{status}"
        output = result.stdout or result.stderr
        if (
            detail["outcome"] != "ok"
            and detail["kind"] != "business_response_error"
            and result.stderr
        ):
            output = result.stderr
        evidence.append(f"{summary} {truncate(output)}".rstrip())
        if detail["outcome"] == "unknown":
            LOGGER.warning("inconclusive probe for %s (%s): %s", service_id, host, summary)
        return result

    inspect = run_probe(
        "docker_inspect",
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}}\t{{.State.OOMKilled}}\t{{.State.ExitCode}}\t{{if .State.Health}}{{.State.Health.Status}}{{end}}",
            container,
        ],
    )
    inspect_details = probe_results["docker_inspect"]
    if inspect_details["kind"] == "ssh_failed":
        observation["ssh_reachable"] = False
        return observation, evidence

    if inspect_details["outcome"] == "unknown":
        observation["ssh_reachable"] = None
    elif not inspect.ok:
        error = (inspect.stderr + inspect.stdout).lower()
        missing = "no such object" in error or "no such container" in error
        observation["container_status"] = "not_found" if missing else "docker_runtime_error"
    else:
        parts = inspect.stdout.strip().split("\t")
        observation["container_status"] = parts[0] if parts else "unknown"
        observation["oom_killed"] = len(parts) > 1 and parts[1].lower() == "true"
        observation["exit_code"] = parts[2] if len(parts) > 2 else ""

    for name, command_key, observation_key in (
        ("process", "process_check_command", "process_present"),
        ("readiness", "readiness_command", "readiness_ok"),
        ("npu", "npu_command", "npu_healthy"),
        ("log_check", "log_check_command", "log_error"),
        ("business_probe", "business_probe_command", "business_ok"),
    ):
        command = _command(service.get(command_key))
        if command is None or (name == "process" and observation["container_status"] != "running"):
            continue
        run_probe(name, command)
        outcome = probe_results[name]["outcome"]
        if outcome != "unknown":
            observation[observation_key] = outcome == ("failed" if name == "log_check" else "ok")

    return observation, evidence


def _signals(observation: Mapping[str, Any]) -> list[str]:
    signals: list[str] = []
    if observation.get("ssh_reachable") is False:
        signals.append("ssh_unreachable")
    if observation.get("container_status") in {"exited", "created", "dead", "not_found"}:
        signals.append("container_exited")
    if observation.get("container_status") in {"paused", "restarting"}:
        signals.append("container_not_running")
    if observation.get("oom_killed") is True:
        signals.append("oom_killed")
    if observation.get("container_status") == "docker_runtime_error":
        signals.append("docker_runtime_error")
    if observation.get("process_present") is False:
        signals.append("process_missing")
    if observation.get("readiness_ok") is False:
        signals.append("readiness_failed")
    if observation.get("npu_healthy") is False:
        signals.append("npu_unhealthy")
    if observation.get("log_error") is True:
        signals.append("service_log_error")
    if observation.get("business_ok") is False:
        signals.append("business_failed")
    if any(
        detail["kind"] in PROBE_EXECUTION_ERROR_KINDS
        for detail in observation.get("probe_results", {}).values()
    ):
        signals.append(PROBE_EXECUTION_ERROR)
    return signals


def _incident_path(events_dir: Path, incident_id: str) -> Path:
    return events_dir / f"{incident_id}.json"


def _find_open_incident(
    events_dir: Path,
    recovery_group: str,
    *,
    probe_only: bool = False,
) -> Path | None:
    # Unresolved incidents (anything but resolved states) are append targets:
    # the monitor never opens a duplicate incident or re-notifies the Agent
    # while a failed/waiting_for_user incident still represents the fault.
    if not events_dir.exists():
        return None
    newest: Path | None = None
    newest_updated = ""
    for path in events_dir.glob("incident-*.json"):
        item = read_json(path, {})
        if not isinstance(item, dict):
            continue
        if item.get("status") in RESOLVED_STATES:
            continue
        if (item.get("signals") == [PROBE_EXECUTION_ERROR]) != probe_only:
            continue
        item_group = item.get("recovery_group") or item.get("service")
        if item_group != recovery_group:
            continue
        updated = str(item.get("updated_at") or "")
        if updated >= newest_updated:
            newest = path
            newest_updated = updated
    return newest


def append_incident_evidence(
    path: Path,
    service: Mapping[str, Any],
    observation: Mapping[str, Any],
    evidence: Sequence[str],
    signals: Sequence[str],
    confirmation_count: int = 1,
    first_failed_at: str | None = None,
    suppressed_by: str | None = None,
) -> None:
    incident = read_json(path, {})
    if not isinstance(incident, dict):
        return
    service_id = str(service.get("service_id") or service.get("container") or "unknown")
    affected = incident.setdefault("affected_services", [])
    if isinstance(affected, list) and service_id not in affected:
        affected.append(service_id)
    if suppressed_by is None:
        existing_signals = incident.setdefault("signals", [])
        if isinstance(existing_signals, list):
            incident["signals"] = sorted(set(existing_signals).union(signals))
    else:
        suppressed_signals = incident.setdefault("suppressed_signals", {})
        if isinstance(suppressed_signals, dict):
            for signal in signals:
                suppressed_signals[signal] = int(suppressed_signals.get(signal, 0) or 0) + 1
    existing_evidence = incident.setdefault("evidence", [])
    if isinstance(existing_evidence, list):
        evidence_kind = "suppressed_dependency" if suppressed_by else "monitor_probe"
        now = utc_now()
        items = [truncate(item) for item in evidence]
        target = None
        for entry in reversed(existing_evidence):
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") != evidence_kind or entry.get("service") != service_id:
                continue
            previous = entry.get("items")
            same_items = (
                isinstance(previous, list)
                and previous
                and {str(value) for value in previous} == set(items)
            )
            if not same_items:
                break
            target = entry
            break
        if target is not None:
            target["repeat_count"] = int(target.get("repeat_count", 1) or 1) + 1
            target["last_seen_at"] = now
        else:
            existing_evidence.append(
                {
                    "kind": evidence_kind,
                    "service": service_id,
                    "items": items,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "repeat_count": 1,
                    **({"suppressed_by": suppressed_by} if suppressed_by else {}),
                }
            )
        if len(existing_evidence) > MAX_INCIDENT_EVIDENCE:
            dropped = len(existing_evidence) - MAX_INCIDENT_EVIDENCE
            incident["evidence_dropped"] = int(incident.get("evidence_dropped", 0) or 0) + dropped
            del existing_evidence[:-MAX_INCIDENT_EVIDENCE]
    if suppressed_by is not None:
        incident["suppressed_count"] = int(incident.get("suppressed_count", 0) or 0) + 1
    incident["confirmation_count"] = max(
        int(incident.get("confirmation_count", 1) or 1), confirmation_count
    )
    if first_failed_at is not None and not incident.get("first_failed_at"):
        incident["first_failed_at"] = first_failed_at
    incident["last_observation"] = dict(observation)
    incident["updated_at"] = utc_now()
    atomic_write_json(path, incident)


def build_incident(
    manifest: Mapping[str, Any],
    service: Mapping[str, Any],
    observation: Mapping[str, Any],
    evidence: Sequence[str],
    *,
    signals: Sequence[str],
    event: Mapping[str, Any] | None = None,
    confirmation_count: int = 1,
    first_failed_at: str | None = None,
) -> dict[str, Any]:
    incident_id = "incident-" + uuid.uuid4().hex
    detected_at = utc_now()
    return {
        "schema_version": 1,
        "incident_id": incident_id,
        "deployment_key": manifest.get("deployment_key"),
        "detected_at": detected_at,
        "service": service.get("service_id") or service.get("container"),
        "node": service.get("host"),
        "recovery_group": _recovery_group(service),
        "affected_services": [service.get("service_id") or service.get("container")],
        "signals": list(signals),
        "evidence": [
            {
                "kind": "monitor_probe",
                "service": service.get("service_id") or service.get("container"),
                "items": [truncate(item) for item in evidence],
                "first_seen_at": detected_at,
                "last_seen_at": detected_at,
                "repeat_count": 1,
            }
        ]
        if evidence
        else [],
        "evidence_dropped": 0,
        "observation": dict(observation),
        "trigger": dict(event or {}),
        "status": "detected",
        "confirmation_count": confirmation_count,
        "first_failed_at": first_failed_at or detected_at,
        "diagnosis": None,
        "recovery": None,
        "validation": None,
        "updated_at": utc_now(),
    }


class DockerEventWatcher(threading.Thread):
    def __init__(
        self,
        host: str,
        containers: Sequence[str],
        events: queue.Queue[dict[str, Any]],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name=f"docker-events-{host}", daemon=True)
        self.host = host
        self.containers = list(containers)
        self.events = events
        self.stop_event = stop_event

    def run(self) -> None:
        backoff = 1.0
        filters = " ".join(
            f"--filter {shlex.quote('container=' + container)}"
            for container in self.containers
        )
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            self.host,
            "docker events --format '{{json .}}' "
            "--filter type=container "
            "--filter event=die "
            "--filter event=oom "
            "--filter event=health_status "
            + filters,
        ]
        while not self.stop_event.is_set():
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    if self.stop_event.is_set():
                        break
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload["host"] = self.host
                    self.events.put(payload)
                if self.stop_event.is_set():
                    process.terminate()
                    break
                process.wait(timeout=2)
                backoff = 1.0
            except (OSError, subprocess.TimeoutExpired) as exc:
                LOGGER.warning("docker events watcher %s: %s", self.host, exc)
            if self.stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, 30.0)


class Monitor:
    def __init__(
        self,
        manifest_path: Path,
    ) -> None:
        self.manifest_path = manifest_path
        self.manifest = read_json(manifest_path, {})
        if not isinstance(self.manifest, dict):
            raise ValueError("manifest must be a JSON object")
        validate_manifest(self.manifest)
        self.root = Path(str(self.manifest.get("deployment_dir") or manifest_path.parent))
        self.events_dir = self.root / "events"
        self.state_path = self.root / "monitor-state.json"
        self.executor = RemoteExecutor(
            int(self.manifest.get("probe_timeout_seconds", 20))
        )
        self.stop_event = threading.Event()
        self.probe_events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.report_queue: queue.Queue[Path] = queue.Queue()
        self.watchers: list[DockerEventWatcher] = []
        self.soft_failure_counts: dict[tuple[str, str], int] = {}
        self.soft_failure_first_seen: dict[tuple[str, str], str] = {}
        self.unknown_probe_streaks: dict[str, dict[str, tuple[int, str]]] = {}
        self.reporter = threading.Thread(
            target=self._report_loop,
            name="pi-http-reporter",
            daemon=True,
        )

    @property
    def services(self) -> list[Mapping[str, Any]]:
        values = self.manifest.get("services", [])
        return [item for item in values if isinstance(item, dict)]

    def write_state(self, status: str, **extra: Any) -> None:
        state = read_json(self.state_path, {})
        if not isinstance(state, dict):
            state = {}
        state.update(
            {
                "schema_version": 1,
                "status": status,
                "monitor_pid": os.getpid(),
                "monitor_pgid": os.getpgrp(),
                "process_start_time": process_start_time(os.getpid()),
                "command": " ".join(shlex.quote(part) for part in os.sys.argv),
                "deployment_key": self.manifest.get("deployment_key"),
                "manifest_path": str(self.manifest_path.resolve()),
                "updated_at": utc_now(),
            }
        )
        state.update(extra)
        atomic_write_json(self.state_path, state)

    def start_watchers(self) -> None:
        by_host: dict[str, list[str]] = {}
        for service in self.services:
            host = _host(service)
            container = str(service.get("container") or "")
            if host and container:
                by_host.setdefault(host, []).append(container)
        for host, containers in by_host.items():
            watcher = DockerEventWatcher(
                host,
                sorted(set(containers)),
                self.probe_events,
                self.stop_event,
            )
            self.watchers.append(watcher)
            watcher.start()

    def stop_watchers(self) -> None:
        self.stop_event.set()
        for watcher in self.watchers:
            watcher.join(timeout=2)

    def find_service(self, host: str, container: str) -> Mapping[str, Any] | None:
        for service in self.services:
            if _host(service) == host and str(service.get("container")) == container:
                return service
        return None

    def _service_keys(self, service: Mapping[str, Any]) -> set[str]:
        return {
            str(value)
            for value in (
                service.get("service_id"),
                service.get("container"),
                service.get("recovery_group"),
            )
            if value
        }

    def _dependency_keys(self, service: Mapping[str, Any]) -> set[str]:
        pending = [str(value) for value in service.get("depends_on", []) if value]
        dependencies: set[str] = set()
        while pending:
            dependency = pending.pop()
            if dependency in dependencies:
                continue
            dependencies.add(dependency)
            target = next(
                (
                    candidate
                    for candidate in self.services
                    if dependency in self._service_keys(candidate)
                ),
                None,
            )
            if target is not None:
                dependencies.update(self._service_keys(target))
                pending.extend(str(value) for value in target.get("depends_on", []) if value)
        return dependencies

    def _processing_dependency_incident(
        self,
        service: Mapping[str, Any],
    ) -> tuple[Path, str] | None:
        dependencies = self._dependency_keys(service)
        if not dependencies or not self.events_dir.exists():
            return None
        for path in self.events_dir.glob("incident-*.json"):
            item = read_json(path, {})
            if not isinstance(item, dict) or item.get("status") != "processing":
                continue
            if item.get("signals") == [PROBE_EXECUTION_ERROR]:
                continue
            group = str(item.get("recovery_group") or item.get("service") or "")
            if group in dependencies:
                incident_id = str(item.get("incident_id") or path.stem)
                return path, incident_id
        return None

    def confirm_signals(
        self,
        service: Mapping[str, Any],
        signals: Sequence[str],
    ) -> tuple[list[str], int, str | None]:
        service_id = str(service.get("service_id") or service.get("container") or "unknown")
        current_soft = set(signals).intersection(SOFT_SIGNALS)
        confirmed: list[str] = [signal for signal in signals if signal in HARD_SIGNALS]
        first_failed_at: str | None = None
        confirmation_count = 1

        for signal in SOFT_SIGNALS:
            key = (service_id, signal)
            if signal not in current_soft:
                self.soft_failure_counts.pop(key, None)
                self.soft_failure_first_seen.pop(key, None)
                continue
            count = self.soft_failure_counts.get(key, 0) + 1
            self.soft_failure_counts[key] = count
            first_seen = self.soft_failure_first_seen.setdefault(key, utc_now())
            if count >= SOFT_SIGNAL_CONFIRMATIONS:
                confirmed.append(signal)
                confirmation_count = max(confirmation_count, count)
                first_failed_at = first_seen

        return sorted(set(confirmed)), confirmation_count, first_failed_at

    def _confirm_unknown_probes(
        self,
        service: Mapping[str, Any],
        probe_results: Mapping[str, Mapping[str, Any]],
        *,
        periodic: bool,
    ) -> dict[str, tuple[int, str]]:
        service_id = str(service.get("service_id") or service.get("container") or "unknown")
        previous = self.unknown_probe_streaks.get(service_id, {})
        if not periodic:
            for name, detail in probe_results.items():
                if detail["outcome"] != "unknown":
                    previous.pop(name, None)
            return {}

        # Rebuild each round so a skipped check breaks the consecutive streak.
        current: dict[str, tuple[int, str]] = {}
        now = utc_now()
        for name, detail in probe_results.items():
            kind = detail["kind"]
            if kind not in PERSISTENT_UNKNOWN_KINDS:
                continue
            if name == "docker_inspect" and kind == "ssh_failed":
                continue  # The first SSH failure already has a hard signal.
            count, first_seen = previous.get(name, (0, now))
            current[name] = (count + 1, first_seen)

        if current:
            self.unknown_probe_streaks[service_id] = current
        else:
            self.unknown_probe_streaks.pop(service_id, None)
        return {
            name: streak for name, streak in current.items()
            if streak[0] >= UNKNOWN_PROBE_CONFIRMATIONS
        }

    def probe(self, service: Mapping[str, Any], trigger: Mapping[str, Any] | None) -> None:
        observation, evidence = _service_probe(self.executor, service)
        observed_signals = _signals(observation)
        signals, confirmation_count, first_failed_at = self.confirm_signals(
            service, observed_signals
        )
        if signals:
            self._record_incident(
                service, observation, evidence, trigger, signals, confirmation_count, first_failed_at
            )
        unknown_probes = self._confirm_unknown_probes(
            service, observation["probe_results"], periodic=trigger is None
        )
        if PROBE_EXECUTION_ERROR in observed_signals or unknown_probes:
            for name in unknown_probes:
                evidence.append(
                    f"{name}: inconclusive for at least {UNKNOWN_PROBE_CONFIRMATIONS} "
                    "consecutive periodic checks"
                )
            self._record_incident(
                service,
                observation,
                evidence,
                trigger,
                [PROBE_EXECUTION_ERROR],
                max((count for count, _ in unknown_probes.values()), default=1),
                min((first_seen for _, first_seen in unknown_probes.values()), default=None),
            )

    def _record_incident(
        self,
        service: Mapping[str, Any],
        observation: Mapping[str, Any],
        evidence: Sequence[str],
        trigger: Mapping[str, Any] | None,
        signals: list[str],
        confirmation_count: int,
        first_failed_at: str | None,
    ) -> None:
        service_id = str(service.get("service_id") or service.get("container"))
        recovery_group = _recovery_group(service)
        if set(signals).issubset(SOFT_SIGNALS):
            dependency_incident = self._processing_dependency_incident(service)
            if dependency_incident is not None:
                append_incident_evidence(
                    dependency_incident[0],
                    service,
                    observation,
                    evidence,
                    signals,
                    confirmation_count,
                    first_failed_at,
                    suppressed_by=dependency_incident[1],
                )
                return
        existing = _find_open_incident(
            self.events_dir, recovery_group, probe_only=signals == [PROBE_EXECUTION_ERROR]
        )
        if existing is not None:
            append_incident_evidence(
                existing,
                service,
                observation,
                evidence,
                signals,
                confirmation_count,
                first_failed_at,
            )
            return
        incident = build_incident(
            self.manifest,
            service,
            observation,
            evidence,
            signals=signals,
            event=trigger,
            confirmation_count=confirmation_count,
            first_failed_at=first_failed_at,
        )
        path = _incident_path(self.events_dir, incident["incident_id"])
        atomic_write_json(path, incident)
        self.report_queue.put(path)
        LOGGER.warning("incident detected: %s %s", service_id, signals)

    def probe_all(self) -> None:
        for service in self.services:
            if not self.stop_event.is_set():
                self.probe(service, None)

    def handle_runtime_event(self, event: Mapping[str, Any]) -> None:
        service = self.find_service(
            str(event.get("host") or ""),
            str(event.get("container") or event.get("Actor", {}).get("Attributes", {}).get("name", "")),
        )
        if service is not None:
            self.probe(service, event)

    def _report_loop(self) -> None:
        notify = self.manifest.get("agent_notify", {})
        endpoint = notify.get("endpoint") if isinstance(notify, dict) else None
        if not endpoint:
            LOGGER.warning("agent_notify.endpoint is not configured")
        while not self.stop_event.is_set() or not self.report_queue.empty():
            try:
                path = self.report_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not endpoint:
                self.report_queue.task_done()
                continue
            payload = read_json(path, {})
            if not isinstance(payload, dict):
                self.report_queue.task_done()
                continue
            body = {
                "schema_version": 1,
                "deployment_key": self.manifest.get("deployment_key"),
                "incident_id": payload.get("incident_id"),
                "event_path": str(path.relative_to(self.root)).replace(os.sep, "/"),
                "summary": {
                    "service": payload.get("service"),
                    "node": payload.get("node"),
                    "signals": payload.get("signals", []),
                },
            }
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            request = urllib.request.Request(
                str(endpoint),
                data=data,
                headers=headers,
                method="POST",
            )
            delivered = False
            for attempt in range(5):
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        delivered = 200 <= response.status < 300
                    if delivered:
                        break
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    LOGGER.warning(
                        "Pi notification failed for %s (attempt %d): %s",
                        path,
                        attempt + 1,
                        exc,
                    )
                    if self.stop_event.wait(min(2**attempt, 30)):
                        break
            if not delivered:
                self.report_queue.put(path)
            self.report_queue.task_done()

    def queue_pending_incidents(self) -> None:
        if not self.events_dir.exists():
            return
        for path in sorted(self.events_dir.glob("incident-*.json")):
            item = read_json(path, {})
            if isinstance(item, dict) and item.get("status") in {"detected", "processing"}:
                self.report_queue.put(path)

    def run(self) -> int:
        self.write_state("starting")
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.reporter.start()
        self.queue_pending_incidents()
        self.start_watchers()
        self.probe_all()
        self.write_state("running")
        interval = float(self.manifest.get("probe_interval_seconds", 30))
        next_probe_at = time.monotonic() + interval
        try:
            while not self.stop_event.is_set():
                try:
                    event = self.probe_events.get(timeout=max(0, next_probe_at - time.monotonic()))
                except queue.Empty:
                    pass
                else:
                    self.handle_runtime_event(event)
                if time.monotonic() >= next_probe_at and not self.stop_event.is_set():
                    self.probe_all()
                    next_probe_at = time.monotonic() + interval
        finally:
            self.stop_watchers()
            self.reporter.join(timeout=5)
            self.write_state("stopped")
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("VLLM_MONITOR_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = parse_args()
    monitor = Monitor(args.manifest)

    def stop(_signum: int, _frame: Any) -> None:
        monitor.stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return monitor.run()


if __name__ == "__main__":
    raise SystemExit(main())
