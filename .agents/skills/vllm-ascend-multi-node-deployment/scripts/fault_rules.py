"""Deterministic fault classification for the vLLM deployment monitor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class FaultDecision:
    fault_type: str
    scope: str
    action: str
    recoverable: bool
    recovery_window: str
    reason: str


def classify_observation(observation: Mapping[str, Any]) -> FaultDecision | None:
    """Map a normalized observation to one deterministic recovery decision."""

    if observation.get("intentional_stop"):
        return None

    if observation.get("ssh_reachable") is False:
        if observation.get("network_failures", 0) < observation.get(
            "confirmation_failures", 3
        ):
            return None
        return FaultDecision(
            "network_unreachable",
            "node",
            "wait_or_migrate",
            True,
            "short transient: 1-5 minutes; migration: 10-30 minutes",
            "target host or data-plane access is unavailable",
        )

    if observation.get("npu_healthy") is False:
        return FaultDecision(
            "npu_unhealthy",
            "node",
            "migrate",
            True,
            "10-30 minutes with a prepared backup node",
            "one or more assigned NPUs are unhealthy",
        )

    if observation.get("docker_runtime_error"):
        return FaultDecision(
            "node_runtime_unhealthy",
            "node",
            "migrate",
            True,
            "10-30 minutes with a prepared backup node",
            "Docker or the container runtime did not answer a container probe",
        )

    container_status = observation.get("container_status")
    if container_status and container_status != "running":
        return FaultDecision(
            "container_exit",
            "container",
            "recreate_container",
            True,
            "1-10 minutes plus model loading",
            f"container status is {container_status}",
        )

    if observation.get("process_present") is False:
        return FaultDecision(
            "process_exit",
            "service",
            "restart_process",
            True,
            "30 seconds-5 minutes plus model loading",
            "the expected service process is absent",
        )

    if observation.get("connector_error"):
        return FaultDecision(
            "connector_failed",
            "role_group",
            "rebuild_role_group",
            True,
            "2-15 minutes plus model loading",
            "connector or KV initialization/transport failed",
        )

    if observation.get("readiness_ok") is False:
        if observation.get("grace_active"):
            return None
        if observation.get("readiness_failures", 0) < observation.get(
            "confirmation_failures", 3
        ):
            return None
        return FaultDecision(
            "readiness_lost",
            "service",
            "recreate_container",
            True,
            "30 seconds-5 minutes",
            "readiness or the service port is unavailable",
        )

    if observation.get("business_probe_ok") is False:
        if observation.get("grace_active"):
            return None
        if observation.get("business_failures", 0) < observation.get(
            "confirmation_failures", 3
        ):
            return None
        return FaultDecision(
            "business_failed",
            "service",
            "restart_process",
            True,
            "1-10 minutes including confirmation",
            "the controlled business probe failed after confirmation",
        )

    return None
