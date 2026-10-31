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
import signal
import shlex
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

LOGGER = logging.getLogger("vllm_fault_monitor")
TERMINAL_STATES = {"recovered", "failed", "waiting_for_user"}
MAX_EVIDENCE = 2000
MAX_INCIDENT_EVIDENCE = 100
SOFT_SIGNAL_CONFIRMATIONS = 3
HARD_SIGNALS = {
    "ssh_unreachable",
    "container_exited",
    "docker_runtime_error",
    "process_missing",
    "npu_unhealthy",
}
SOFT_SIGNALS = {
    "container_unhealthy",
    "readiness_failed",
    "service_log_error",
    "business_failed",
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
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
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


def _service_probe(
    executor: RemoteExecutor,
    service: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    service_id = str(service.get("service_id") or service.get("container") or "unknown")
    host = _host(service)
    container = str(service.get("container") or "")
    observation: dict[str, Any] = {
        "service_id": service_id,
        "host": host,
        "container": container,
        "ssh_reachable": True,
        "container_status": "unknown",
        "container_health": None,
        "process_present": None,
        "readiness_ok": None,
        "npu_healthy": None,
        "log_error": None,
        "business_ok": None,
    }
    evidence: list[str] = []

    if not host or not container:
        observation["probe_error"] = "manifest missing host or container"
        return observation, ["manifest_missing_host_or_container"]

    inspect = executor.run(
        host,
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}}\t{{.State.OOMKilled}}\t{{.State.ExitCode}}\t{{if .State.Health}}{{.State.Health.Status}}{{end}}",
            container,
        ],
    )
    if inspect.returncode == 255:
        observation["ssh_reachable"] = False
        observation["container_status"] = "unknown"
        evidence.append("ssh_unreachable: " + truncate(inspect.stderr))
        return observation, evidence

    if inspect.returncode != 0:
        missing = "no such object" in (inspect.stderr + inspect.stdout).lower()
        observation["container_status"] = "not_found" if missing else "docker_runtime_error"
        evidence.append("docker_inspect: " + truncate(inspect.stderr or inspect.stdout))
    else:
        parts = inspect.stdout.strip().split("\t")
        observation["container_status"] = parts[0] if parts else "unknown"
        observation["oom_killed"] = len(parts) > 1 and parts[1].lower() == "true"
        observation["exit_code"] = parts[2] if len(parts) > 2 else ""
        observation["container_health"] = parts[3] if len(parts) > 3 and parts[3] else None
        evidence.append("docker_inspect: " + truncate(inspect.stdout))

    process_command = _command(service.get("process_check_command"))
    if process_command is not None:
        process = executor.run(host, process_command)
        observation["process_present"] = process.ok
        evidence.append("process: " + truncate(process.stdout or process.stderr))
    else:
        evidence.append("process: missing process_check_command")

    readiness_command = _command(service.get("readiness_command"))
    if readiness_command is not None:
        readiness = executor.run(host, readiness_command)
        observation["readiness_ok"] = readiness.ok
        evidence.append("readiness: " + truncate(readiness.stdout or readiness.stderr))

    npu_command = _command(service.get("npu_command"))
    if npu_command is not None:
        npu = executor.run(host, npu_command)
        observation["npu_healthy"] = npu_health_ok(npu.stdout, npu.returncode)
        evidence.append("npu: " + truncate(npu.stdout or npu.stderr))

    log_command = _command(service.get("log_check_command"))
    if log_command is not None:
        log_check = executor.run(host, log_command)
        observation["log_error"] = not log_check.ok
        evidence.append("log_check: " + truncate(log_check.stdout or log_check.stderr))

    business_command = _command(service.get("business_probe_command"))
    if business_command is not None:
        business = executor.run(host, business_command)
        observation["business_ok"] = business.ok
        evidence.append("business_probe: " + truncate(business.stdout or business.stderr))

    return observation, evidence


def _signals(observation: Mapping[str, Any]) -> list[str]:
    signals: list[str] = []
    if observation.get("ssh_reachable") is False:
        signals.append("ssh_unreachable")
    if observation.get("container_status") in {"exited", "created", "dead", "not_found"}:
        signals.append("container_exited")
    if observation.get("container_status") == "docker_runtime_error":
        signals.append("docker_runtime_error")
    if observation.get("container_health") == "unhealthy":
        signals.append("container_unhealthy")
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
    return signals


def _incident_path(events_dir: Path, incident_id: str) -> Path:
    return events_dir / f"{incident_id}.json"


def _find_open_incident(
    events_dir: Path,
    recovery_group: str,
) -> Path | None:
    if not events_dir.exists():
        return None
    for path in events_dir.glob("incident-*.json"):
        item = read_json(path, {})
        if not isinstance(item, dict):
            continue
        if item.get("status") in TERMINAL_STATES:
            continue
        item_group = item.get("recovery_group") or item.get("service")
        if item_group != recovery_group:
            continue
        return path
    return None


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
        existing_evidence.extend(
            {
                "kind": evidence_kind,
                "summary": truncate(item),
                **({"suppressed_by": suppressed_by} if suppressed_by else {}),
            }
            for item in evidence
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
    event: Mapping[str, Any] | None = None,
    confirmation_count: int = 1,
    first_failed_at: str | None = None,
) -> dict[str, Any]:
    incident_id = "incident-" + uuid.uuid4().hex
    signals = _signals(observation)
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
        "signals": signals,
        "evidence": [
            {"kind": "monitor_probe", "summary": truncate(item)}
            for item in evidence[-MAX_INCIDENT_EVIDENCE:]
        ],
        "evidence_dropped": max(0, len(evidence) - MAX_INCIDENT_EVIDENCE),
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

    def probe(self, service: Mapping[str, Any], trigger: Mapping[str, Any] | None) -> None:
        observation, evidence = _service_probe(self.executor, service)
        observed_signals = _signals(observation)
        signals, confirmation_count, first_failed_at = self.confirm_signals(
            service, observed_signals
        )
        if not signals:
            return
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
        existing = _find_open_incident(self.events_dir, recovery_group)
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
            trigger,
            confirmation_count,
            first_failed_at,
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
        try:
            while not self.stop_event.is_set():
                try:
                    event = self.probe_events.get(timeout=interval)
                    self.handle_runtime_event(event)
                except queue.Empty:
                    self.probe_all()
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
