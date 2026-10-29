# 监控清单

`monitor-manifest.json` 是部署验收后由 Skill 生成、由监控进程只读使用的运行清单。它不是
自然语言计划，也不是最终部署报告；它必须包含监控进程执行确定性检查和恢复所需的事实。

最小结构如下：

```json
{
  "probe_interval_seconds": 30,
  "confirmation_failures": 3,
  "startup_grace_seconds": 600,
  "command_timeout_seconds": 30,
  "max_attempts_per_scope": 3,
  "stop_intent_path": "/data/boom/deployment/stop-intent.json",
  "fault_record_dir": "/data/boom/deployment",
  "services": [
    {
      "host": "user@node-a",
      "role": "master",
      "role_group": "ordinary-multi-node",
      "container": "node-a",
      "rank": 0,
      "process_match": "vllm serve",
      "readiness_command": ["curl", "--fail", "http://127.0.0.1:8899/health"],
      "business_probe_command": ["curl", "--fail", "http://127.0.0.1:8899/health"],
      "log_probe_command": ["docker", "logs", "--since", "2m", "node-a"],
      "npu_devices": [0, 1],
      "npu_probe_command": ["npu-smi", "info", "-i", "<device_id>", "-c", "0", "-t", "health"],
      "recovery_commands": {
        "restart_process": [["docker", "exec", "node-a", "bash", "-lc", "<fixed-command>"]],
        "recreate_container": [["docker", "rm", "-f", "node-a"], ["docker", "run", "..."]],
        "rebuild_role_group": [["docker", "exec", "node-a", "bash", "-lc", "<fixed-command>"]],
        "stop": [["docker", "stop", "node-a"]]
      }
    }
  ]
}
```

`recovery_commands` 必须由部署阶段根据已经验收的命令展开，不由监控进程拼接。清单中不得
保存密码、私钥、token 或其他凭据。清单生成后保持不变；节点、容器、设备、端口或恢复命令
发生变化时，必须停止监控并重新部署。

`npu_probe_command` 的退出码只能说明 `npu-smi` 命令是否执行成功，不能单独证明设备健康。
监控进程会解析输出中的 `Health: OK`、`Health: Normal` 或 `Health: Healthy`；没有明确的
健康字段、出现其他状态或命令超时时，按 NPU 不健康处理。使用 `<device_id>` 时，监控进程
会对 `npu_devices` 中的每张设备分别执行检查，所有设备都健康才通过。
