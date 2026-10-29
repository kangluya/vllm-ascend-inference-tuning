"""Long-running deterministic monitor for deployed vLLM services.

The monitor consumes a deployment-specific manifest. It does not call an Agent or
an LLM and it never invents recovery commands.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from .fault_actions import RemoteExecutor, execute_group_action, execute_service_action
    from .fault_rules import FaultDecision, classify_observation
except ImportError:  # Running the file directly from the scripts directory.
    from fault_actions import RemoteExecutor, execute_group_action, execute_service_action
    from fault_rules import FaultDecision, classify_observation


LOGGER = logging.getLogger("vllm_fault_monitor")


def npu_health_ok(output: str) -> bool:
    """Return true only when npu-smi explicitly reports every health value as OK."""

    values = re.findall(r"\bHealth\s*[:：]\s*([A-Za-z]+)", output, flags=re.IGNORECASE)
    if not values:
        return False
    return all(value.lower() in {"ok", "normal", "healthy"} for value in values)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def find_service(manifest: Mapping[str, Any], container: str, host: str) -> Mapping[str, Any] | None:
    for service in manifest.get("services", []):
        if service.get("host") == host and service.get("container") == container:
            return service
    return None


def event_command(host: str, containers: list[str]) -> list[str]:
    filters = " ".join(f"--filter container={container}" for container in containers)
    return [
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
        f"docker events {filters} --format '{{{{json .}}}}'",
    ]


class DockerEventWatcher(threading.Thread):
    def __init__(self, host: str, containers: list[str], events: queue.Queue[dict[str, Any]]) -> None:
        super().__init__(daemon=True)
        self.host = host
        self.containers = containers
        self.events = events
        self.stop_requested = threading.Event()
        self.process: subprocess.Popen[str] | None = None

    def run(self) -> None:
        backoff_seconds = 1.0
        while not self.stop_requested.is_set():
            try:
                self.process = subprocess.Popen(
                    event_command(self.host, self.containers),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert self.process.stdout is not None
                for line in self.process.stdout:
                    if self.stop_requested.is_set():
                        return
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    backoff_seconds = 1.0
                    payload["host"] = self.host
                    payload["container"] = payload.get("Actor", {}).get("Attributes", {}).get(
                        "name", ""
                    )
                    self.events.put(payload)
                if self.stop_requested.is_set():
                    return
                self.events.put({"host": self.host, "error": "docker event stream ended"})
            except OSError as exc:
                self.events.put({"host": self.host, "error": str(exc)})
            finally:
                if self.process is not None:
                    try:
                        self.process.kill()
                    except OSError:
                        pass
                    self.process = None
            if self.stop_requested.wait(backoff_seconds):
                return
            backoff_seconds = min(backoff_seconds * 2, 30.0)

    def stop(self) -> None:
        self.stop_requested.set()
        if self.process is not None:
            self.process.kill()


class Monitor:
    def __init__(self, manifest_path: Path, *, dry_run: bool = False) -> None:
        self.manifest_path = manifest_path
        self.manifest = read_json(manifest_path, {})
        if not self.manifest.get("services"):
            raise ValueError("monitor manifest must contain services")
        self.root = manifest_path.parent
        self.state_path = self.root / "monitor-state.json"
        self.stop_intent_path = Path(
            self.manifest.get("stop_intent_path", self.root / "stop-intent.json")
        )
        self.fault_dir = Path(self.manifest.get("fault_record_dir", self.root))
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.watchers: list[DockerEventWatcher] = []
        self.executor = RemoteExecutor(
            timeout_seconds=int(self.manifest.get("command_timeout_seconds", 30)),
            dry_run=dry_run,
        )
        self.attempts: dict[str, int] = {}
        self.terminal_failures: set[str] = set()
        self.failure_counts: dict[str, dict[str, int]] = {}
        self.grace_until: dict[str, float] = {}
        self.pending_recoveries: dict[str, dict[str, Any]] = {}
        self.running = True

    def write_state(self, **updates: Any) -> None:
        state = read_json(self.state_path, {})
        state.update(updates)
        state["updated_at"] = utc_now()
        write_json(self.state_path, state)

    def start_watchers(self) -> None:
        by_host: dict[str, list[str]] = {}
        for service in self.manifest["services"]:
            by_host.setdefault(str(service["host"]), []).append(str(service["container"]))
        for host, containers in by_host.items():
            watcher = DockerEventWatcher(host, sorted(set(containers)), self.events)
            watcher.start()
            self.watchers.append(watcher)

    def stop_watchers(self) -> None:
        for watcher in self.watchers:
            watcher.stop()

    def stop_targets(self) -> list[Mapping[str, Any]]:
        intent = read_json(self.stop_intent_path, {})
        target_keys = {
            (str(item.get("host")), str(item.get("container")))
            for item in intent.get("targets", [])
        }
        return [
            service
            for service in self.manifest["services"]
            if (str(service.get("host")), str(service.get("container"))) in target_keys
        ]

    def stop_requested(self) -> bool:
        intent = read_json(self.stop_intent_path, {})
        if intent.get("status") != "requested" or not intent.get("targets"):
            return False
        expires_at = intent.get("expires_at")
        if expires_at:
            try:
                return datetime.fromisoformat(expires_at) > datetime.now(timezone.utc)
            except ValueError:
                return False
        return True

    def stop_targets_stopped(self) -> bool:
        targets = self.stop_targets()
        if not targets:
            return False
        observations = [self.probe_service(service) for service in targets]
        return all(
            observation.get("ssh_reachable") is True
            and observation.get("container_status") != "running"
            for observation in observations
        )

    @staticmethod
    def service_key(service: Mapping[str, Any]) -> str:
        return f"{service.get('host')}:{service.get('container')}"

    def is_active_stop_target(self, service: Mapping[str, Any]) -> bool:
        if not self.stop_requested():
            return False
        return service in self.stop_targets()

    def annotate_failures(self, service: Mapping[str, Any], observation: dict[str, Any]) -> None:
        key = self.service_key(service)
        counts = self.failure_counts.setdefault(key, {})
        if observation.get("readiness_ok") is False:
            counts["readiness"] = counts.get("readiness", 0) + 1
        else:
            counts.pop("readiness", None)
        if observation.get("business_probe_ok") is False:
            counts["business"] = counts.get("business", 0) + 1
        else:
            counts.pop("business", None)
        if observation.get("ssh_reachable") is False:
            counts["network"] = counts.get("network", 0) + 1
        else:
            counts.pop("network", None)
        observation["readiness_failures"] = counts.get("readiness", 0)
        observation["business_failures"] = counts.get("business", 0)
        observation["network_failures"] = counts.get("network", 0)
        observation["confirmation_failures"] = int(
            self.manifest.get("confirmation_failures", 3)
        )
        observation["grace_active"] = time.monotonic() < self.grace_until.get(key, 0.0)

    def consume_stop_intent(self) -> None:
        intent = read_json(self.stop_intent_path, {})
        intent["status"] = "consumed"
        intent["consumed_at"] = utc_now()
        write_json(self.stop_intent_path, intent)

    def reconcile_existing_stop_intent(self) -> bool:
        intent = read_json(self.stop_intent_path, {})
        if intent.get("status") != "requested" or not intent.get("targets"):
            return False
        if not self.stop_targets_stopped():
            return False
        self.consume_stop_intent()
        self.write_state(status="stopped", reason="preexisting_stop_intent")
        return True

    def services_for_event(self, event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        service = find_service(self.manifest, str(event.get("container")), str(event.get("host")))
        return [service] if service else []

    def probe_service(self, service: Mapping[str, Any]) -> dict[str, Any]:
        host = str(service["host"])
        container = str(service["container"])
        result = self.executor.run(host, ["docker", "inspect", "--format", "{{.State.Status}}", container])
        observation: dict[str, Any] = {
            "host": host,
            "container": container,
            "role": service.get("role"),
            "container_status": result.stdout.strip() if result.ok else None,
            "ssh_reachable": result.returncode != 255,
            "intentional_stop": self.is_active_stop_target(service),
        }
        if not result.ok:
            if result.returncode == 255:
                observation["ssh_reachable"] = False
            elif result.returncode == 124:
                observation["docker_runtime_error"] = True
            elif not any(
                marker in result.stderr.lower()
                for marker in ("no such container", "no such object")
            ):
                observation["docker_runtime_error"] = True
            else:
                observation["container_status"] = "not_found"
            observation["probe_error"] = result.stderr.strip()
            self.annotate_failures(service, observation)
            return observation

        process_match = service.get("process_match")
        if process_match:
            top = self.executor.run(host, ["docker", "top", container, "-eo", "pid,args"])
            observation["process_present"] = top.ok and str(process_match) in top.stdout
        readiness_command = service.get("readiness_command")
        if readiness_command:
            ready = self.executor.run(host, [str(part) for part in readiness_command])
            observation["readiness_ok"] = ready.ok
        business_command = service.get("business_probe_command")
        if business_command:
            business = self.executor.run(host, [str(part) for part in business_command])
            observation["business_probe_ok"] = business.ok
        log_command = service.get("log_probe_command")
        if log_command:
            logs = self.executor.run(host, [str(part) for part in log_command])
            text = (logs.stdout + "\n" + logs.stderr).lower()
            connector_terms = ("connector", "kv transfer", "mooncake", "ascendstore")
            error_terms = ("error", "failed", "failure", "cannot", "unable", "timeout", "exception")
            observation["connector_error"] = any(
                connector in text and error in text
                for connector in connector_terms
                for error in error_terms
            )
        npu_command = service.get("npu_probe_command")
        if npu_command:
            devices = service.get("npu_devices") or [None]
            probe_results = []
            for device in devices:
                command = [str(part) for part in npu_command]
                if device is not None:
                    command = [part.replace("<device_id>", str(device)) for part in command]
                probe_results.append(self.executor.run(host, command))
            observation["npu_healthy"] = all(
                result.ok and npu_health_ok(result.stdout) for result in probe_results
            )
        self.annotate_failures(service, observation)
        return observation

    def write_fault(self, service: Mapping[str, Any], decision: FaultDecision, result: str) -> None:
        record = {
            "time": utc_now(),
            "scope": {
                "node": service.get("host"),
                "role": service.get("role"),
                "container": service.get("container"),
                "rank": service.get("rank"),
            },
            "fault_type": decision.fault_type,
            "level": decision.scope,
            "action": decision.action,
            "expected_recovery_window": decision.recovery_window,
            "result": result,
            "reason": decision.reason,
        }
        self.fault_dir.mkdir(parents=True, exist_ok=True)
        filename = self.fault_dir / f"fault-recovery-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        write_json(filename, record)

    @staticmethod
    def observation_healthy(observation: Mapping[str, Any]) -> bool:
        if observation.get("container_status") != "running":
            return False
        for field in ("process_present", "readiness_ok", "business_probe_ok", "npu_healthy"):
            if field in observation and observation[field] is False:
                return False
        return True

    def finalize_pending_recovery(
        self, service: Mapping[str, Any], observation: Mapping[str, Any]
    ) -> bool:
        key = self.service_key(service)
        pending = self.pending_recoveries.get(key)
        if pending is None or not self.observation_healthy(observation):
            return False
        decision = pending["decision"]
        self.write_fault(service, decision, "recovered")
        self.pending_recoveries.pop(key, None)
        self.write_state(status="running", fault=None, recovery_validated=True)
        return True

    def recover(self, service: Mapping[str, Any], decision: FaultDecision) -> bool:
        if self.is_active_stop_target(service):
            return True
        base_key = f"{service.get('host')}:{service.get('container')}:{decision.fault_type}"
        escalation = {
            "restart_process": "recreate_container",
            "recreate_container": "rebuild_role_group",
            "rebuild_role_group": "migrate",
            "wait_or_migrate": "migrate",
        }
        key = f"{base_key}:{decision.action}"
        attempts = self.attempts.get(key, 0) + 1
        self.attempts[key] = attempts
        max_attempts = int(self.manifest.get("max_attempts_per_scope", 3))
        if attempts > max_attempts:
            next_action = escalation.get(decision.action)
            if next_action is None:
                if base_key not in self.terminal_failures:
                    self.terminal_failures.add(base_key)
                    LOGGER.error("recovery escalation exhausted for %s", base_key)
                    self.write_fault(service, decision, "failed_attempt_limit")
                return False
            decision = replace(
                decision,
                action=next_action,
                reason=f"{decision.action} exceeded {max_attempts} attempts",
            )
            key = f"{base_key}:{decision.action}"
            attempts = self.attempts.get(key, 0) + 1
            self.attempts[key] = attempts

        self.write_state(status="recovering", fault=decision.fault_type, attempts=attempts)
        affected_services: list[Mapping[str, Any]] = [service]
        try:
            if decision.action == "rebuild_role_group":
                group = service.get("role_group", service.get("role"))
                affected_services = [
                    item
                    for item in self.manifest["services"]
                    if item.get("role_group", item.get("role")) == group
                ]
                results = execute_group_action(
                    self.executor, affected_services, "rebuild_role_group"
                )
            else:
                results = execute_service_action(self.executor, service, decision.action)
        except (KeyError, ValueError) as exc:
            LOGGER.error("recovery action is not configured: %s", exc)
            self.write_fault(service, decision, "unconfigured")
            return False
        success = bool(results) and all(result.ok for result in results)
        if success:
            # A detached docker/process start has been accepted, but model loading
            # and readiness may still take minutes. Start the grace window now.
            grace_until = time.monotonic() + float(
                self.manifest.get("startup_grace_seconds", 600)
            )
            for affected_service in affected_services:
                affected_key = self.service_key(affected_service)
                self.grace_until[affected_key] = grace_until
                self.failure_counts.pop(affected_key, None)
                self.pending_recoveries[affected_key] = {"decision": decision}
            self.write_fault(service, decision, "recovery_started")
            self.write_state(status="starting", fault=decision.fault_type)
        else:
            self.write_fault(service, decision, "failed")
            self.write_state(status="failed", fault=None)
        return success

    def handle_event(self, event: Mapping[str, Any]) -> None:
        services = self.services_for_event(event)
        for service in services:
            observation = self.probe_service(service)
            observation["event"] = dict(event)
            if self.finalize_pending_recovery(service, observation):
                continue
            decision = classify_observation(observation)
            if decision is not None:
                self.recover(service, decision)

    def run_once(self) -> None:
        for service in self.manifest["services"]:
            observation = self.probe_service(service)
            if self.finalize_pending_recovery(service, observation):
                continue
            decision = classify_observation(observation)
            if decision is not None:
                self.recover(service, decision)

    def run(self, *, once: bool = False) -> None:
        if self.reconcile_existing_stop_intent():
            return
        self.write_state(status="running", pid=os.getpid())
        if once:
            self.run_once()
            return
        self.start_watchers()
        probe_interval = float(self.manifest.get("probe_interval_seconds", 30))
        try:
            while self.running:
                if self.stop_requested():
                    self.write_state(status="stopping")
                    if self.stop_targets_stopped():
                        self.stop_watchers()
                        self.consume_stop_intent()
                        self.write_state(status="stopped")
                        return
                try:
                    event = self.events.get(timeout=probe_interval)
                    if event.get("error"):
                        LOGGER.warning("event watcher error: %s", event)
                    else:
                        self.handle_event(event)
                except queue.Empty:
                    self.run_once()
        finally:
            self.stop_watchers()


def request_stop(manifest_path: Path) -> int:
    manifest = read_json(manifest_path, {})
    root = manifest_path.parent
    targets = [
        {"host": service["host"], "role": service.get("role"), "container": service["container"]}
        for service in manifest.get("services", [])
    ]
    requested_at = datetime.now(timezone.utc)
    intent = {
        "status": "requested",
        "requested_at": requested_at.isoformat(),
        "expires_at": (requested_at + timedelta(minutes=5)).isoformat(),
        "targets": targets,
    }
    stop_path = Path(manifest.get("stop_intent_path", root / "stop-intent.json"))
    write_json(stop_path, intent)
    executor = RemoteExecutor()
    ordered = list(reversed(manifest.get("services", [])))
    for service in ordered:
        try:
            results = execute_service_action(executor, service, "stop")
        except (KeyError, ValueError) as exc:
            LOGGER.error("stop action is not configured: %s", exc)
            return 2
        if any(not result.ok for result in results):
            return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    if args.stop:
        return request_stop(args.manifest)
    try:
        Monitor(args.manifest, dry_run=args.dry_run).run(once=args.once)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("monitor failed: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
