# 故障监控与恢复

本文件供 vLLM 多机部署服务使用：部署验收通过后或直接接入已有部署时，按本文件初始化故障监控（第 1-3 章），故障事件到达当前 Pi 后按本文件执行复核、恢复和验收（第 4-5 章），用户要求停止服务时按第 6 章执行，Pi 会话结束或崩溃后按第 7 章恢复监控接管。监控进程只负责探测和 HTTP 上报，不执行恢复动作；Agent 负责监控初始化、复核、判断、恢复、验收和停止。

## 1. 监控初始化

监控初始化有两种入口，在部署首次验收通过后直接进入初始化；要监控已经部署成功的模型，先完成第 1.1 节的接入前置步骤，再进入同样的初始化。

### 1.1 接入已有部署的前置步骤

用户要求为已有部署接入故障监控时，需要用户提供逐字部署命令清单（每条命令及其所在节点）、各节点 SSH 访问方式和服务形态说明（普通多机或 PD 分离、各节点角色）。

1. 实机核对：逐节点通过 SSH 检查容器、进程、端口和 NPU 状态，把用户提供的命令清单与实机状态互证；不一致时以实机为准，并向用户澄清是清单过时还是实机存在异常；
2. 命令落盘：核对过的命令逐字写入部署目录的 deployment_report.json与commands.log；部署报告标注"接入已有部署"，记录命令来源和核对结果，后续恢复以此记录为准；
3. 接入验收：逐节点完成 NPU 健康检查，逐服务完成进程身份、readiness、通信和业务验收；接入时发现既有异常，默认先按第 5 章处理并通过验收后再监控。

部署首次验收通过或接入前置步骤完成后，继续执行以下初始化：

1. 生成 monitor-manifest.json，包含 deployment_key、control_host、监控进程可送达的 agent_notify endpoint（字段规则见第 3.2 节）、探测周期，以及每个服务的节点、容器、recovery_group、基于实机进程身份生成的必填容器内进程检查命令、readiness、NPU 和可选日志/业务探针命令；端口、设备、挂载、环境变量和完整部署命令继续记录在 deployment_report.json 与 commands.log；monitor-manifest.json 的字段契约见本文件第 3 章；
2. 调用 Pi Extension 的 vllm_fault_register_deployment 注册 deployment_key 到当前 Pi 会话；从控制宿主机侧请求 agent_notify 端点的健康检查接口（同机时即本地请求；不同机时通过 SSH 在控制宿主机上请求）确认路由已生效，`deployment_keys` 已包含本 deployment_key 才算确认成功；确认失败时停止流程，不得启动监控单元；
3. 将 Skill 中的 `scripts/vllm_fault_monitor.py` 通过 SSH 复制到控制宿主机的 `/data/boom/deployment/vllm_fault_monitor.py`，并设置为可执行；只有路由确认成功且脚本复制完成后，才能注册并启动本次部署的 systemd 监控单元；
4. 监控进程启动后自行将 PID、PGID、启动时间、命令摘要、deployment_key 和清单路径写入 monitor-state.json；Agent 只读取并校验这些字段；
5. 监控进程通过 HTTP 向 Pi Extension 的 /v1/fault-events 上报事件。

## 2. systemd 监控单元

为每个 deployment_key 注册一个独立的 systemd 单元，单元名固定为 `vllm-fault-monitor-<deployment_key>.service`，只用于管理本次部署的运行周期，不配置开机自启软链。

~~~ini
[Service]
Type=simple
ExecStart=/usr/bin/python3 /data/boom/deployment/vllm_fault_monitor.py --manifest /data/boom/deployment/monitor-manifest.json
Restart=on-failure
RestartSec=5
KillMode=control-group
~~~

Agent 必须先完成 deployment_key 路由登记再启动 systemd 单元。监控进程启动后自行写入 monitor-state.json，其中包括 deployment_key、清单绝对路径、PID/PGID、启动时间和命令身份。Agent 只读取这些字段。用户主动停止时先停止该单元，防止 systemd 自动重新拉起监控进程，再使用 monitor-state.json 校验并精确处理当前进程。

## 3. 监控清单契约（monitor-manifest.json）

monitor-manifest.json 是部署完成后生成的部署事实输入，供监控进程持续探测和故障恢复 Agent 复核使用，不包含密码、私钥、Token 或其他凭据。

### 3.1 必需字段示例

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
      "process_check_command": [
        "docker",
        "exec",
        "decode-0",
        "sh",
        "-lc",
        "pgrep -af '<verified-process-pattern>'"
      ],
      "readiness_command": [
        "curl",
        "--fail",
        "--silent",
        "http://127.0.0.1:8000/health"
      ],
      "npu_command": [
        "sh",
        "-lc",
        "test \"$(npu-smi info | grep -cE '^\\| +(0|1) .*OK')\" -eq 2"
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

### 3.2 字段规则

- schema_version：清单格式版本；
- deployment_key：本次部署的稳定标识，用于把事件路由到当前 Pi 会话；
- deployment_dir：控制宿主机上的部署目录；
- control_host.ssh_target：Agent 从 Pi 容器通过 SSH 读取事件和部署资料时使用的目标；
- agent_notify.endpoint：监控进程向 Pi Extension 上报事件的 HTTP 地址，必须是监控进程可送达的地址：监控与 Pi 同机时为本机回环；不同机时，由 Pi 所在机器通过 SSH 反向隧道把 Extension 的监听端口落到控制宿主机回环（endpoint 仍为回环形态），或在防火墙白名单限制下直连 Pi 所在机器。Extension 的监听地址和端口由环境变量 VLLM_FAULT_NOTIFY_HOST（默认 127.0.0.1）和 VLLM_FAULT_NOTIFY_PORT（默认 8765）控制；通知接口无认证，除上述形态外不得暴露到网络；
- probe_timeout_seconds：单次 SSH 或检查命令的最长等待时间；
- probe_interval_seconds：没有事件推送时的兜底探测周期；
- services：本次部署中需要持续观察的服务；
- service_id：服务稳定标识；
- role：服务角色，例如 master、worker、prefill、decode、proxy 或 mooncake；
- host：服务所在目标宿主机的 SSH 目标；
- container：精确容器名；
- recovery_group：最小恢复单元的稳定标识；同一 rank 组或角色组应使用相同值；单服务使用自身 service_id；
- process_check_command：必填的容器内进程检查命令数组，必须以 `docker exec <container>` 或 `docker top <container>` 开头，由监控进程通过 SSH 执行；命令必须根据部署完成后实际观察到的进程命令生成。<verified-process-pattern>替换为本次部署真实的模型路径和参数；
- readiness_command：服务 readiness 检查命令；只对自身监听 API 端口的服务配置，命令在部署验收时实际执行通过后才写入清单；
- npu_command：NPU 健康检查命令；只检查该服务实际占用的卡，不检查同节点其他服务占用的卡，避免其他服务的卡故障误报本服务；卡号和数量取自该服务部署时的设备分配（如 --device 挂载的 davinci 卡号）；示例形态（服务占用 0、1 两张卡）：`test "$(npu-smi info | grep -cE '^\| +(0|1) .*OK')" -eq 2`；
- log_check_command：可选的日志检查命令；命令退出码为 0 表示最近窗口没有匹配错误，非 0 表示发现日志错误或检查失败；不包含 KV connector 专用判断；
- business_probe_command：可选的最小业务探针命令；命令退出码为 0 表示探针成功，非 0 表示业务探针失败；每个部署需在入口服务上配置一条：PD 分离部署为 Proxy 服务，经公共入口发出，一条请求穿过 Prefill、KV 传输和 Decode 全链路；普通多机为 master 服务，直连引擎 OpenAI API 端口；命令从本次部署验收实际执行成功的最小业务请求派生；
- depends_on：服务依赖，值可以是依赖服务的 service_id、container 或 recovery_group；上游事件处于 processing 时，下游软信号只记录、不重复通知 Agent；
- recovery_scope：Agent 选择恢复范围时使用的部署事实。

### 3.3 清单变更与安全边界

清单生成后，部署拓扑、容器、端口、设备或原始启动命令发生变化时必须重新生成清单并重启监控。监控进程只读清单，不在运行期间修改清单。

命令字段只保存经过部署验收或已部署完成的服务提供的确认的检查命令。命令结果、凭据和完整日志写入部署运行目录或脱敏日志，不写入清单。

Pi Extension 只在本机回环端口接收事件，并继续校验已登记的 deployment_key、incident_id 和事件路径；不得把该端口发布到外部网络。

## 4. 故障事件处理

收到 Pi Extension 通知后：

1. 通过 control_host 的 SSH 读取 events/incident-<incident_id>.json；
2. 读取 monitor-manifest.json、deployment_report.json 和 commands.log；
3. 通过 SSH 重新检查服务、容器、进程、端口、NPU、日志和通信；
4. 判断故障类型和影响范围；
5. 选择恢复动作；
6. 通过 SSH/Docker 执行恢复；
7. 通过 readiness、通信、模型身份和真实 completion 验收；
8. 更新同一个 incident 文件的 diagnosis、recovery、validation 和 status。

事件状态：

~~~text
detected
  ↓
processing
  ↓
recovered / recovered_degraded / failed / waiting_for_user
~~~

监控事件是事实证据，不是最终故障结论。单次探测不足以支持高风险恢复时，先补充检查。

事件文件由监控进程和 Agent 共同维护，但写入范围不同：

- 监控进程首次发布事件时写入 `schema_version`、`incident_id`、`deployment_key`、`detected_at`、`service`、`node`、`recovery_group`、`affected_services`、`signals`、`evidence`、`evidence_dropped`、`confirmation_count`、`first_failed_at`、`observation`、`trigger`、`status` 和 `updated_at`，初始状态为 `detected`；同一 recovery_group 的后续探测只追加证据，不新建事件；
- Agent 开始处理时将 `status` 改为 `processing`，并补充三个字段：`diagnosis`（故障判断及依据：结论、指向本事件 `evidence` 条目的证据引用、排除的假设）、`recovery`（恢复执行记录：选择的恢复策略与范围、实际执行的动作、SSH/Docker 操作及结果、是否使用备用节点）和 `validation`（验收记录：readiness、通信、模型身份和真实 completion 的逐项验收结果）；
- Agent 完成验收后将 `status` 改为 `recovered`、`recovered_degraded`、`failed` 或 `waiting_for_user`，仍需用户处理的事项写入 `diagnosis`；`failed` 和 `waiting_for_user` 表示故障尚未解决：同组后续监控探测继续向该事件追加证据，不新建事件、不再次通知 Agent；仅当故障已解决，事件状态为 `recovered` 或 `recovered_degraded`后，同组再次出现故障才新建事件；
- 监控进程不覆盖 Agent 已写入的处理和验收字段。

容器退出、Docker 运行时错误、进程消失、SSH 不可达和 NPU 异常等硬信号单次确认即可上报；readiness、日志和业务探针等软信号需要连续三次探测失败后才上报，期间恢复则清零。

Agent 开始恢复前必须先将主故障事件状态更新为 `processing`。在该状态期间，依赖该服务或恢复组的其他服务如果只出现 readiness、日志或业务探针等软信号，监控进程只将证据追加到主事件，不新建事件或再次通知 Agent。主事件恢复完成后，监控重新探测依赖服务；依赖服务仍异常时，再生成新的故障事件。依赖服务自身出现容器退出、SSH 不可达或 NPU 异常等硬信号时，仍可以单独上报。

## 5. 故障类型和默认策略

下表为故障分类和默认恢复策略，作为 Agent 的决策参考。Agent 必须结合本次事件证据、部署拓扑、镜像行为和当前运行状态判断适用行；证据不足时先补充探测或报告无法确认，检测信号列是复核参考，不是自动匹配规则。

| 故障类型 | 检测信号和确认条件 | 默认恢复策略 | 升级条件 |
|---|---|---|---|
| 进程退出 | 容器仍在运行但预期 vLLM 进程不存在；PID 启动时间和命令身份不匹配 | 在原容器、原节点按原命令恢复进程 | 反复退出或容器环境损坏时重建容器 |
| 容器退出或 OOM | 容器状态为 exited、OOM 或非零退出；日志包含启动失败或 OOM | 先在原节点对仍存在且身份未变化的容器执行重启操作，等待进程、readiness、通信和业务验收；启动失败、再次退出、OOM 或容器配置不可信时，再按原镜像、挂载、设备和命令重建容器 | Docker、运行时或设备不可用时迁移节点 |
| readiness 丢失 | 对外 API 端口或服务控制端口连续不可用；进程仍存在 | 隔离入口后重启或重建对应服务 | 真实请求仍失败时恢复进程组或角色组 |
| 业务请求失败或卡死 | 清单配置的最小业务探针连续失败；真实用户流量失败由 Agent 结合日志、网关统计和真实 completion 复核 | 连续确认后按原拓扑恢复对应服务，不重放用户原始请求 | 仍无资源或通信进展时恢复角色组 |
| 分布式 rank 失效 | rank 数量不完整、通信端口断开、HCCL/TP/DP 错误；只有当前 vLLM-Ascend 版本、镜像和并行模式明确支持安全重加入时，才允许单 rank 恢复 | 默认按角色组恢复；不能只重启一个 rank 后报告成功 | 组重建失败或节点故障时迁移受影响组 |
| KV connector 或 Mooncake 异常 | connector 初始化、注册、KV 获取/发送或远端 engine 查询失败 | 重建受影响的 P/D 角色组或 KV 服务 | P/D 重新注册失败时迁移角色组 |
| Proxy 异常 | Proxy 进程退出、入口端口不可用或后端列表丢失 | 只重建 Proxy，保留健康的 P/D 和 KV 服务 | 真实请求仍失败时重新检查后端组 |
| 网络或 SSH 不可达 | SSH、数据面 IP、通信端口或网卡连续不可达 | 连续确认后隔离故障端点并等待或迁移 | 节点持续不可达时迁移 |
| NPU 或宿主机不健康 | npu-smi 健康异常、设备丢失、驱动或 HCCL 报错；SSH 失败、Docker/Ascend 运行时不可用或关键目录不可读 | 隔离故障设备或宿主机，不执行自动芯片 reset | 使用已预检的备用节点迁移；无备用节点时按本文件第 5.3 节自主执行降级恢复 |
| 配置、镜像或模型错误 | 参数互斥、挂载缺失、镜像 digest 不一致、模型加载失败、端口冲突 | 停止自动重试，保留证据并报告 | 用户修正部署计划后重新部署 |
| 资源耗尽 | OOM 证据、显存/内存分配失败或句柄耗尽的日志报错；资源水位本身无信号，依据后果和日志判定 | 可清理本次残留后按原配置重试一次 | 不得自动缩小并行规模；仍不足则迁移或报告失败 |

Agent 不能因为恢复失败而缩卡、改单机、删除 rank 或修改原拓扑来伪造成功；只有满足第 5.3 节前置判定和执行约束的降级恢复可以改变拓扑，且终态必须为 recovered_degraded。

### 5.1 原地恢复

优先原地恢复需要同时满足：

- SSH 和 Docker 可用；
- NPU 设备健康且身份未改变；
- 镜像、模型、挂载、端口和网络仍可用；
- 故障不属于持续的节点、网络或 NPU 故障；
- 不改变 TP、DP、PP、EP、rank 和服务拓扑。

原地恢复必须使用 deployment_report.json 和 commands.log 中确认的镜像、设备、挂载、环境和原始启动参数；monitor-manifest.json 只提供持续监控所需的服务与检查命令。

### 5.2 备用节点迁移

以下情况可以迁移：

- NPU 健康持续异常；
- 宿主机或关键网络持续不可达；
- Docker 或 Ascend 运行时损坏；
- 同一恢复范围多次失败；
- 原节点无法提供原拓扑所需设备。

备用节点必须在部署前或接入已有部署时通过 SSH、Docker、镜像 digest、Ascend 挂载、NPU 型号和数量、网络、模型路径、端口、日志目录和设备占用检查。

迁移后重新确认通信 IP、网卡、设备列表、rank 地址、Proxy 后端、KV 端口和 Mooncake 地址。模型、镜像、并行规模和服务角色保持不变。

### 5.3 降级恢复

降级恢复是硬件故障持续、且无预检备用节点和空闲健康卡时，通过缩小并行规模维持部分服务能力的最后恢复手段，由 Agent 自主执行：满足前置判定时推导并执行降级命令，重试超限后停止尝试并上报用户。

触发条件（须全部满足）：

- 硬件故障持续存在，原地恢复前置条件不满足；
- 无预检过的备用节点，无同节点空闲健康卡；
- 部署形态存在可丢弃的完整并行组。

前置判定：

- 形态合法性：只允许丢弃完整 DP 组或完整 PD 角色 worker；
- 组完整性：故障卡所在组整体丢弃，保留组全部卡健康（按卡位 NPU 探测复核）；
- 剩余形态构成完整模型副本或完整服务链路。

执行约束：

- 降级命令只能从 deployment_report.json 和 commands.log 中逐字记录的部署命令与部署事实推导，禁止凭记忆或猜测补参数；
- 降级命令相对原命令的每处变更（并行参数、设备列表、端口、Proxy 后端列表等）记录在事件文件中；
- 每次执行前清理上次失败残留，执行后通过完整验收：readiness、通信、入口真实 completion 和模型身份；
- 降级命令最多尝试 2 次，超限后停止尝试，事件状态置为 waiting_for_user 并上报用户。

成功后的义务：

- 事件终态为 recovered_degraded，禁止使用 recovered；
- 降级命令逐字追加到 commands.log，作为该降级形态后续恢复的重放命令；
- 按本文件第 3 章的清单变更与安全边界重新生成 monitor-manifest.json，并重启 systemd 监控单元；
- 部署报告记录原拓扑、降级拓扑和容量预期，并通知用户：硬件修复后按原始部署命令恢复原拓扑，重新走部署验收并重新生成清单。

## 6. 用户主动停止

用户直接向当前 Pi Agent 发出停止请求。Agent：

1. 停止对应的 systemd 监控单元，禁止监控进程自动拉起；
2. 读取 monitor-state.json；
3. 校验 deployment_key、清单路径、PID、PGID、启动时间和命令摘要；
4. 精确停止监控进程；
5. 按逆依赖顺序通过 SSH 停止服务；
6. 验证所有服务和容器已停止。

不使用宽范围 pkill、kill -9 或端口占用者推断进程归属。

## 7. 会话恢复

Pi会话崩溃或异常退出后，新开启的会话中用户要求恢复某部署的故障监控时：

1. 从用户处确认控制宿主机 SSH 目标和部署目录；
2. 通过 SSH 读取 monitor-state.json 和 monitor-manifest.json，确认 deployment_key 与清单路径；确认 systemd 监控单元仍在运行，已停止时按第 2 章重新启动；
3. 调用 Pi Extension 的 vllm_fault_register_deployment 将 deployment_key 重新登记到当前会话，并从控制宿主机侧请求 agent_notify 端点的 /health 确认 deployment_keys 已包含该 key；
4. 等待监控在下一个重试周期内把未送达的事件推送进当前会话；
5. 通过 SSH 对账 events/ 目录中全部未解决事件（status 为 detected、processing、waiting_for_user 或 failed），与已推送事件按 incident_id 去重：detected/processing 逐个按第 4 章流程处理；waiting_for_user/failed 只向用户提示存在待裁决或已放弃的事件，不重复进入恢复流程；
6. 无未解决事件时向用户报告当前监控状态干净。
   