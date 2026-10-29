import unittest

from scripts.fault_rules import classify_observation
from scripts.vllm_fault_monitor import npu_health_ok


class FaultRuleTests(unittest.TestCase):
    def test_container_exit_maps_to_container_recreate(self) -> None:
        decision = classify_observation({"container_status": "exited"})
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.fault_type, "container_exit")
        self.assertEqual(decision.action, "recreate_container")

    def test_npu_failure_precedes_process_failure(self) -> None:
        decision = classify_observation(
            {"npu_healthy": False, "process_present": False}
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.fault_type, "npu_unhealthy")

    def test_healthy_observation_has_no_decision(self) -> None:
        self.assertIsNone(
            classify_observation(
                {
                    "container_status": "running",
                    "process_present": True,
                    "readiness_ok": True,
                    "business_probe_ok": True,
                }
            )
        )

    def test_intentional_stop_has_no_recovery_decision(self) -> None:
        self.assertIsNone(classify_observation({"intentional_stop": True}))

    def test_readiness_failure_requires_consecutive_confirmation(self) -> None:
        observation = {"readiness_ok": False, "readiness_failures": 2}
        self.assertIsNone(classify_observation(observation))
        observation["readiness_failures"] = 3
        decision = classify_observation(observation)
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.fault_type, "readiness_lost")

    def test_missing_container_is_not_network_failure(self) -> None:
        decision = classify_observation(
            {"ssh_reachable": True, "container_status": "not_found"}
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.fault_type, "container_exit")

    def test_docker_runtime_error_maps_to_node_runtime(self) -> None:
        decision = classify_observation(
            {"ssh_reachable": True, "docker_runtime_error": True}
        )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.fault_type, "node_runtime_unhealthy")

    def test_npu_health_requires_explicit_health_values(self) -> None:
        self.assertTrue(npu_health_ok("Health : OK\n"))
        self.assertFalse(npu_health_ok("npu-smi completed successfully\n"))
        self.assertFalse(npu_health_ok("Health : Fault\n"))


if __name__ == "__main__":
    unittest.main()
