# 监控清单契约

本文件定义部署完成后生成的 monitor-manifest.json。它是监控进程和故障恢复 Agent 的部署事实输入，不包含密码、私钥、Token 或其他凭据。

## 必需字段

~~~json
{
  "schema_version": 1,
  "deployment_key": "deployment-20261029-001",
  "deployment_dir": "/data/boom/deployment",
  "control_host": {
    "ssh_target": "control-host",
    "deployment_dir": "/data/boom/deployment"
  },
  "agent_notify": {
    "transport": "http",
    "endpoint": "http://127.0.0.1:18765/v1/fault-events"
  },
  "probe_timeout_seconds": 20,
  "probe_interval_seconds": 30,
  "services": [
    {
      "service_id": "decode-0",
      "role": "decode",
      "host": "npu-node-a",
      "container": "decode-0",
      "recovery_group": "decode-group",
      "expected_process": "python -m vllm.entrypoints.openai.api_server",
      "process_check_command": [
        "docker",
        "exec",
        "decode-0",
        "pgrep",
        "-af",
        "python -m vllm.entrypoints.openai.api_server"
      ],
      "readiness_command": [
        "curl",
        "--fail",
        "--silent",
        "http://127.0.0.1:8000/health"
      ],
      "npu_command": [
        "npu-smi",
        "info"
      ],
      "log_check_command": [
        "sh",
        "-lc",
        "! docker logs --since 30s decode-0 2>&1 | grep -E 'ERROR|Traceback|HCCL error'"
      ],
      "depends_on": [],
      "recovery_scope": "service"
    }
  ]
}
~~~

## 字段规则

- schema_version：清单格式版本；
- deployment_key：本次部署的稳定标识，用于把事件路由到当前 Pi 会话；
- deployment_dir：控制宿主机上的部署目录；
- control_host.ssh_target：Agent 从 Pi 容器通过 SSH 读取事件和部署资料时使用的目标；
- agent_notify.endpoint：仅供本机访问的 Pi Extension HTTP 接口；
- probe_timeout_seconds：单次 SSH 或检查命令的最长等待时间；
- probe_interval_seconds：没有事件推送时的兜底探测周期；
- services：本次部署中需要持续观察的服务；
- service_id：服务稳定标识；
- role：服务角色，例如 master、worker、prefill、decode、proxy 或 mooncake；
- host：服务所在目标宿主机的 SSH 目标；
- container：精确容器名；
- recovery_group：最小恢复单元的稳定标识；同一 rank 组或角色组应使用相同值；单服务使用自身 service_id；
- expected_process：预期进程命令摘要，仅用于记录和 Agent 复核；
- process_check_command：必填的容器内进程检查命令数组，必须以 `docker exec <container>` 或 `docker top <container>` 开头，由监控进程通过 SSH 执行；命令退出码为 0 才表示进程存在；
- readiness_command：服务 readiness 检查命令；
- npu_command：NPU 健康检查命令；
- log_check_command：可选的日志检查命令；命令退出码为 0 表示最近窗口没有匹配错误，非 0 表示发现日志错误或检查失败；不包含 KV connector 专用判断；
- business_probe_command：可选的最小业务探针命令；命令退出码为 0 表示探针成功，非 0 表示业务探针失败；
- depends_on：服务依赖；
- recovery_scope：Agent 选择恢复范围时使用的部署事实。

清单生成后，部署拓扑、容器、端口、设备或原始启动命令发生变化时必须重新生成清单并重启监控。监控进程只读清单，不在运行期间修改清单。

命令字段只保存经过部署阶段确认的检查命令。命令结果、凭据和完整日志写入部署运行目录或脱敏日志，不写入清单。

通知接口不使用 Token。Pi Extension 只在本机回环端口接收事件，并继续校验已登记的 deployment_key、incident_id 和事件路径；不得把该端口发布到外部网络。


## 进程管理器要求

Agent 为每个 deployment_key 注册一个独立的 systemd 单元，单元名固定为 `vllm-fault-monitor-<deployment_key>.service`。单元使用本次部署的监控清单路径启动监控进程，至少具备以下行为：

~~~ini
[Service]
Type=simple
ExecStart=/usr/bin/python3 <skill_dir>/scripts/vllm_fault_monitor.py --manifest /data/boom/deployment/monitor-manifest.json
Restart=on-failure
RestartSec=5
KillMode=control-group
~~~

监控进程启动后自行写入 monitor-state.json，其中包括 deployment_key、清单绝对路径、PID/PGID、启动时间和命令身份。Agent 只读取这些字段。用户主动停止时先停止该单元，防止 systemd 自动重新拉起监控进程，再使用 monitor-state.json 校验并精确处理当前进程。
