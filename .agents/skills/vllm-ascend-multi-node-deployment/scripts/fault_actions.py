"""Deterministic remote actions used by the vLLM deployment monitor."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class RemoteExecutor:
    """Execute a pre-approved argv on a target host through non-interactive SSH."""

    def __init__(self, *, timeout_seconds: int = 30, dry_run: bool = False) -> None:
        self.timeout_seconds = timeout_seconds
        self.dry_run = dry_run

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
        if self.dry_run:
            return CommandResult(0, "DRY_RUN " + " ".join(command), "")
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = (exc.stderr or "") + "command timed out"
            return CommandResult(124, str(stdout), str(stderr))
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _commands(service: Mapping[str, Any], action: str) -> list[list[str]]:
    recovery = service.get("recovery_commands", {})
    commands = recovery.get(action, [])
    if not isinstance(commands, list) or any(not isinstance(command, list) for command in commands):
        raise ValueError(f"recovery_commands.{action} must be a list of argv lists")
    return [[str(part) for part in command] for command in commands]


def execute_service_action(
    executor: RemoteExecutor, service: Mapping[str, Any], action: str
) -> list[CommandResult]:
    """Run the exact command argv recorded for one service and action."""

    host = str(service["host"])
    commands = _commands(service, action)
    if not commands:
        raise ValueError(f"no deterministic {action} command for {service.get('container')}")
    results: list[CommandResult] = []
    for command in commands:
        result = executor.run(host, command)
        results.append(result)
        if not result.ok:
            break
    return results


def execute_group_action(
    executor: RemoteExecutor,
    services: Iterable[Mapping[str, Any]],
    action: str,
) -> list[CommandResult]:
    results: list[CommandResult] = []
    for service in services:
        service_results = execute_service_action(executor, service, action)
        results.extend(service_results)
        if any(not result.ok for result in service_results):
            break
    return results
