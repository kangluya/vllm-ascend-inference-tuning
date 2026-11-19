# 故障监控与恢复

本文件供 vLLM 多机部署服务使用：部署验收通过后或直接接入已有部署时，按本文件初始化故障监控（第 1-3 章），故障事件到达当前 Pi 后按本文件执行复核、恢复和验收（第 4-5 章），用户要求停止服务时按第 6 章执行，Pi 会话结束或崩溃后按第 7 章恢复监控接管。监控进程只负责探测和 HTTP 上报，不执行恢复动作；Agent 负责监控初始化、复核、判断、恢复、验收和停止。接入 Boom Gateway 时，现有 Docker、进程、NPU、readiness、日志和业务探针全部保留，Gateway 只作为额外的摘流状态来源。

## 1. 监控初始化

### 1.1 选择初始化入口

根据部署所处阶段选择入口，两种入口最终都执行第 1.3 节的通用初始化步骤。

- **新部署完成后接入监控**：本次部署已经按部署流程完成验收，直接执行第 1.3 节。
- **为已有部署接入监控**：先执行第 1.2 节，核对并补齐恢复所需的部署事实；前置步骤完成后，再执行第 1.3 节。

### 1.2 为已有部署补齐前置条件

用户要求为已有部署接入故障监控时，需要用户提供逐字部署命令清单（每条命令及其所在节点）、各节点 SSH 访问方式和服务形态说明（普通多机或 PD 分离、各节点角色）。

1. 实机核对：逐节点通过 SSH 检查容器、进程、端口和 NPU 状态，把用户提供的命令清单与实机状态互证；不一致时以实机为准，并向用户澄清是清单过时还是实机存在异常；
2. 命令落盘：核对过的命令逐字写入部署目录的 deployment_report.json与commands.log；部署报告标注"接入已有部署"，记录命令来源和核对结果，后续恢复以此记录为准；
3. 接入验收：逐节点完成 NPU 健康检查，逐服务完成进程身份、readiness、通信和业务验收；接入时发现既有异常，默认先按第 5 章处理并通过验收后再监控。

### 1.3 两种入口共用的初始化步骤

执行本节前必须满足以下任一条件：本次新部署已经通过部署验收，或者已有部署已经完成第 1.2 节的核对、记录和接入验收。满足条件后按以下顺序初始化监控：

存在多个能够独立停止、重建和验收的模型服务部署时，对每个模型服务部署分别执行以下步骤。每个模型服务部署使用独立的 `deployment_key`、部署目录、monitor-manifest.json 和 systemd 单元；同一模型服务部署内部必须共同恢复的 Master/Worker 或 Prefill/Decode/Proxy/Mooncake 作为服务组件继续放在同一份清单的 `services` 数组中。多个模型服务部署可以运行在同一台控制宿主机并共享监控脚本、Pi 通知地址和 Gateway 管理凭据，但 monitor-state.json、events、deployment_report.json 和 commands.log 必须按 deployment_key 隔离。

1. 生成 monitor-manifest.json，包含 deployment_key、control_host、监控进程可送达的 agent_notify endpoint（字段规则见第 3.2 节）、探测周期，以及每个服务的节点、容器、recovery_group、基于实机进程身份生成的必填容器内进程检查命令、readiness、NPU 和可选日志/业务探针命令；需要接入 Gateway 时，额外配置第 3.2 节的可选 `gateway` 映射，不删除或关闭原有服务探针；端口、设备、挂载、环境变量和完整部署命令继续记录在 deployment_report.json 与 commands.log；monitor-manifest.json 的字段契约见本文件第 3 章；
2. 调用 Pi Extension 的 vllm_fault_register_deployment 注册 deployment_key 到当前 Pi 会话；从控制宿主机侧请求 agent_notify 端点的健康检查接口（同机时即本地请求；不同机时通过 SSH 在控制宿主机上请求）确认路由已生效，`deployment_keys` 已包含本 deployment_key 才算确认成功；确认失败时停止流程，不得启动监控单元；
3. 配置 Gateway 增强时，从控制宿主机验证 Gateway 管理地址可达、管理员认证有效，并通过 `/dashboard/api/admin/models` 确认每个 `gateway_deployment_id` 唯一存在且映射到正确的入口服务；认证信息只放入 systemd credential 或权限受限的环境文件，不写入清单；验证失败时保留原有探针配置并停止 Gateway 增强的接入，不得带着错误映射启动；
4. 将 Skill 中的 `scripts/vllm_fault_monitor.py` 通过 SSH 复制到控制宿主机的共享路径 `/data/boom/deployment/vllm_fault_monitor.py`，并设置为可执行；只有路由确认成功且脚本复制完成后，才能注册并启动本次部署的 systemd 监控单元；
5. 监控进程启动后自行将 PID、PGID、启动时间、命令摘要、deployment_key 和清单路径写入 monitor-state.json；Agent 只读取并校验这些字段；
6. 监控进程通过 HTTP 向 Pi Extension 的 /v1/fault-events 上报事件。

## 2. systemd 监控单元

为每个 deployment_key 注册一个独立的 systemd 单元，单元名固定为 `vllm-fault-monitor-<deployment_key>.service`，只用于管理本次部署的运行周期，不配置开机自启软链。监控脚本可以共享；monitor-manifest.json、monitor-state.json、events、deployment_report.json 和 commands.log 必须位于该 deployment_key 的独立部署目录中。

~~~ini
[Service]
Type=simple
ExecStart=/usr/bin/python3 /data/boom/deployment/vllm_fault_monitor.py --manifest /data/boom/deployments/<deployment_key>/monitor-manifest.json
Restart=on-failure
RestartSec=5
KillMode=control-group
~~~

示例中的 `<deployment_key>` 必须替换为当前模型服务部署的实际值。Agent 必须先完成 deployment_key 路由登记再启动 systemd 单元。监控进程启动后自行写入本模型服务部署目录内的 monitor-state.json，其中包括 deployment_key、清单绝对路径、PID/PGID、启动时间和命令身份。Agent 只读取这些字段。用户主动停止时先停止该单元，防止 systemd 自动重新拉起监控进程，再使用 monitor-state.json 校验并精确处理当前进程。

配置 Gateway 增强时，推荐给上述单元增加受保护的 systemd credential：

~~~ini
[Service]
LoadCredential=boom_gateway_admin_api_key:/etc/boom/credentials/gateway-admin-api-key
Environment=VLLM_GATEWAY_ADMIN_USER_ID=admin
~~~

脚本会从 systemd 提供的 `$CREDENTIALS_DIRECTORY/boom_gateway_admin_api_key` 读取管理员 API key。也可以使用 `VLLM_GATEWAY_ADMIN_API_KEY_FILE` 指向权限受限的凭据文件；仅在运行环境本身已提供安全注入时使用 `VLLM_GATEWAY_ADMIN_API_KEY`。这些变量都不得写入 monitor-manifest.json、incident 或普通日志。

## 3. 监控清单契约（monitor-manifest.json）

monitor-manifest.json 是部署完成后生成的部署事实输入，供监控进程持续探测和故障恢复 Agent 复核使用，不包含密码、私钥、Token 或其他凭据。

### 3.1 必需字段示例

~~~json
{
  "schema_version": 1,
  "deployment_key": "deployment-20261029-001",
  "deployment_dir": "/data/boom/deployments/deployment-20261029-001",
  "control_host": {
    "ssh_target": "control-host",
    "deployment_dir": "/data/boom/deployments/deployment-20261029-001"
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
        "-c",
        "pgrep -af '<verified-process-pattern>'"
      ],
      "readiness_command": [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        "5",
        "--output",
        "/dev/null",
        "--write-out",
        "\\nhttp=%{http_code}\\n",
        "http://127.0.0.1:8000/health"
      ],
      "npu_command": [
        "sh",
        "-c",
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

Gateway 增强是可选项。需要接入时，在上述清单顶层增加：

~~~json
{
  "gateway": {
    "enabled": true,
    "base_url": "http://gateway-host:3000",
    "poll_interval_seconds": 5,
    "request_timeout_seconds": 5,
    "deployments": [
      {
        "gateway_deployment_id": "deployment-d1",
        "service_id": "decode-0",
        "recovery_group": "decode-group"
      }
    ]
  }
}
~~~

- schema_version：清单格式版本；
- deployment_key：本次部署的稳定标识，用于把事件路由到当前 Pi 会话；
- deployment_dir：控制宿主机上当前模型服务部署的独立目录；多个可独立恢复的模型服务部署不得共用该目录，避免 monitor-state.json 和 events 相互覆盖；
- control_host.ssh_target：Agent 从 Pi 容器通过 SSH 读取事件和部署资料时使用的目标；
- agent_notify.endpoint：监控进程向 Pi Extension 上报事件的 HTTP 地址，必须是监控进程可送达的地址：监控与 Pi 同机时为本机回环；不同机时，由 Pi 所在机器通过 SSH 反向隧道把 Extension 的监听端口落到控制宿主机回环（endpoint 仍为回环形态），或在防火墙白名单限制下直连 Pi 所在机器。Extension 的监听地址和端口由环境变量 VLLM_FAULT_NOTIFY_HOST（默认 127.0.0.1）和 VLLM_FAULT_NOTIFY_PORT（默认 8765）控制；通知接口无认证，除上述形态外不得暴露到网络；
- probe_timeout_seconds：单次 SSH 或检查命令的最长等待时间；
- `probe_interval_seconds` 控制全量巡检周期。Docker 事件触发对应服务的额外检查；
- `gateway` 是可选增强配置；省略或将 `gateway.enabled` 设为 `false` 时，监控完全按原有内部探针运行；启用后也不会停止任何现有探针；
- `gateway.base_url` 是控制宿主机可访问的 Boom Gateway origin，只允许不含路径、用户名、密码、查询参数和 fragment 的绝对 HTTP(S) 地址；
- `gateway.poll_interval_seconds` 控制 `/dashboard/api/admin/models` 的轮询周期，`gateway.request_timeout_seconds` 控制单次管理请求等待时间，两者都必须大于 0；
- `gateway.deployments` 使用 `gateway_deployment_id` 将 Gateway deployment 精确映射到现有 `service_id`；配置的 `recovery_group` 必须与该服务自身恢复组一致。不得只用 `model_name` 映射；
- Gateway 管理员凭据只从 `VLLM_GATEWAY_ADMIN_API_KEY`、`VLLM_GATEWAY_ADMIN_API_KEY_FILE` 或 systemd credential `boom_gateway_admin_api_key` 读取；管理员 ID 使用 `VLLM_GATEWAY_ADMIN_USER_ID`，默认 `admin`；
- services：本次部署中需要持续观察的服务；
- service_id：服务稳定标识；
- role：服务角色，例如 master、worker、prefill、decode、proxy 或 mooncake；
- host：服务所在目标宿主机的 SSH 目标；
- container：精确容器名；
- recovery_group：最小恢复单元的稳定标识；同一 rank 组或角色组应使用相同值；单服务使用自身 service_id；
- `process_check_command` 是必填的容器内进程检查命令数组，以 `docker exec <container>` 或 `docker top <container>` 开头。监控进程通过 SSH 在目标宿主机执行该命令。将 `<verified-process-pattern>` 替换为本次部署实机核实的模型路径和参数。使用 `pgrep -f` 时，搜索表达式会出现在探针自身的命令行中，必须把其中一个有辨识度的字符改写为等价字符类以避免自匹配，例如将真实进程特征 `w8a8` 写成 `w8a[8]`：该正则仍能匹配真实的 `w8a8`，但不会匹配探针命令行中的字面量 `w8a[8]`。使用 `docker exec` 检查时，未找到匹配进程应返回 `1`，且 stderr 为空；
- `readiness_command` 只对自身监听 API 端口的服务配置，使用部署验收通过的命令；
- npu_command：NPU 健康检查命令；只检查该服务实际占用的卡，不检查同节点其他服务占用的卡，避免其他服务的卡故障误报本服务；卡号和数量取自该服务部署时的设备分配（如 --device 挂载的 davinci 卡号）；示例形态（服务占用 0、1 两张卡）：`test "$(npu-smi info | grep -cE '^\| +(0|1) .*OK')" -eq 2`；
- log_check_command：可选的日志检查命令；命令退出码为 0 表示最近窗口没有匹配错误，非 0 表示发现日志错误或检查失败；不包含 KV connector 专用判断；
- `business_probe_command` 在部署的入口服务上配置，使用本次验收通过的最小业务请求。普通多机直连 Master 的 OpenAI API，PD 分离经 Proxy 检查完整链路。非流式探针直接调用 `curl` 时，保留 JSON 正文供监控识别 `error`。
- Agent 为 `readiness_command` 或 `business_probe_command` 生成直接调用 `curl` 的命令时，必须在命令数组中加入 `"--write-out", "\\nhttp=%{http_code}\\n"`，在标准输出的独立末行打印 HTTP 状态码。
- depends_on：服务依赖，值可以是依赖服务的 service_id、container 或 recovery_group；上游事件处于 processing 时，下游软信号只记录、不重复通知 Agent；
- recovery_scope：Agent 选择恢复范围时使用的部署事实。

### 3.3 清单变更与安全边界

清单生成后，部署拓扑、容器、端口、设备、Gateway deployment 映射或原始启动命令发生变化时必须重新生成清单并重启监控。监控进程只读清单，不在运行期间修改清单。命令字段只保存经过部署验收或已部署完成的服务提供的确认的检查命令。命令结果、凭据和完整日志写入部署运行目录或脱敏日志，不写入清单；Gateway 返回的 `api_base` 写入事件前会移除用户信息、查询参数和 fragment。Pi Extension 只在本机回环端口接收事件，并继续校验已登记的 deployment_key、incident_id 和事件路径；不得把该端口发布到外部网络。

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

`signals` 列出已确认信号。Agent 结合 `observation`、可用的 `last_observation` 和 `evidence` 复核。若有 `probe_results`，可查看失败原因和 HTTP 状态码。收到 `probe_execution_error` 时，查看检查项、`kind`、确认次数、退出码和 stderr，先复核检查命令或 SSH、Docker 链路。确认错误出在本次生成的命令时，按第 5.1.2 节修正并复跑。遇到 HTTP `4xx` 时，优先复核地址、认证、请求参数或限流，取得服务故障证据后再决定是否重启。监控事件是事实证据，不是最终故障结论。单次探测不足以支持高风险恢复时，先补充检查。

收到 `gateway_auto_disabled` 时，只能确认 Gateway 中对应 deployment 已满足 `enabled=false && auto_disabled=true` 并退出路由；`auto_disable_cause=unknown` 表示当前 `models` 接口不能区分主动健康检查和真实请求失败计数。Agent 先结合事件中已有的本地探针证据复核，证据不足时再按需读取 Gateway 请求日志，不得把自动摘流本身直接解释为某个容器、NPU 或软件根因。Gateway 已完成摘流时不重复隔离入口；Agent 只恢复并直接验收服务，不调用 Gateway 管理接口强制上线，Gateway 按现有 `auto_recovery_enabled` 和恢复阈值决定重新加入路由。

Gateway 登录、超时、响应格式、deployment 缺失或映射异常连续三轮时，以 `probe_execution_error` 上报，表示 Gateway 观察链路持续不可用，不表示推理 deployment 已故障。此类事件和同恢复组的服务故障事件分开记录。

事件文件由监控进程创建，并可能在 Agent 处理期间继续追加观测证据。Agent 必须遵守以下写入边界：

- 将事件中由监控进程生成的标识、信号、观测和证据字段视为只读，不得删除或覆盖；
- Agent开始处理时将 `status` 改为 `processing`，并只补充`diagnosis`（故障判断及依据：结论、指向本事件 `evidence` 条目的证据引用、排除的假设）、`recovery`（恢复执行记录：选择的恢复策略与范围、尝试次数、实际执行的动作、SSH/Docker 操作及结果、是否使用备用节点）和 `validation`（验收记录：readiness、通信、模型身份和真实 completion 的逐项验收结果）；
- 每次写回前重新读取最新事件，合并并保留监控进程已经追加的字段和证据；
- 完成验收后将 `status` 改为 `recovered`、`recovered_degraded`、`failed` 或 `waiting_for_user`。仍需用户处理的事项写入 `diagnosis`；`failed` 和 `waiting_for_user` 表示故障尚未解决，同组同类事件继续追加证据；状态为 `recovered` 或 `recovered_degraded` 后，该类故障再次出现时会新建事件。

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
| 命令、脚本、配置、镜像或模型错误 | 命令参数互斥、脚本缺陷、挂载或端口配置错误、镜像 digest 不一致或模型加载失败 | 能按第 5.1.2 节就地修正时，先修正再恢复 | 正确修正方案无法核实或需要变更用户确认的部署方案时，停止自动重试并报告 |
| 资源耗尽 | OOM 证据、显存/内存分配失败或句柄耗尽的日志报错；资源水位本身无信号，依据后果和日志判定 | 可清理本次残留后按原配置重试一次 | 不得自动缩小并行规模；仍不足则迁移或报告失败 |

同一未解决事件中，同一恢复组、同一故障类型下的同一恢复方案最多自动尝试 2 次。每次尝试都必须在 `recovery` 中记录方案、尝试次数、实际动作和验收结果；第 2 次仍未通过验收，或动作执行后服务从未就绪时，不得继续重复该方案，必须按上表升级到重建、恢复组处理、迁移或其他适用路径。没有有依据且可自主执行的后续方案时，将事件置为 `waiting_for_user` 并报告证据和结论。若先修正了 Agent 生成的命令、脚本或配置，修正后的动作属于新的恢复方案，重新按该方案计数。

Agent 不能因为恢复失败而缩卡、改单机、删除 rank 或修改原拓扑来伪造成功；只有满足第 5.3 节前置判定和执行约束的降级恢复可以改变拓扑，且终态必须为 recovered_degraded。

### 5.1 原地恢复

执行恢复前，Agent 必须通过实机检查同时确认以下条件。

- SSH 和 Docker 可用；
- NPU 设备健康且身份未改变；
- 镜像、模型、挂载、端口和网络仍可用；
- 故障不属于持续的节点、网络或 NPU 故障；
- 不改变 TP、DP、PP、EP、rank 和服务拓扑。

#### 5.1.1 可按原配置直接重启或重建

确认已验收的配置和启动命令仍然适用，且故障出在运行中的服务组件时，Agent 按 `deployment_report.json` 和 `commands.log` 记录的镜像、设备、挂载、环境和启动参数重启或重建受影响的服务，再按第 4 章验收。`monitor-manifest.json` 仅提供监控所需的服务与检查命令。

#### 5.1.2 需先修正再恢复

确认命令、脚本或配置本身有误时，先根据用户给定的部署要求、已验收的部署记录或实机复核确定修正内容。Agent 可以在原部署约束内自行修正本次部署中由其编写或生成的错误项，或还原已验收的配置。无法核实正确修法，或者修正需要改变用户指定的值或部署约束时，保留证据并置为 `waiting_for_user`。确认第三方软件缺陷时按第 5.4 节处理。

- 仅影响监控时，复跑并验证受影响的检查。修改监控清单或常驻监控脚本后，重启对应的监控单元并确认探测正常。
- 影响推理服务时，在原节点用已验证的方式使修正生效。必要时重启或重建受影响的服务或恢复组，并按第 4 章验收。

将修正内容和验证结果写入事件。若修正改变服务启动命令或部署配置，验收后更新 `deployment_report.json`，将实际执行的修正命令逐字追加到 `commands.log`，并按第 3.3 节同步监控清单。后续恢复使用验收通过的修正记录。

### 5.2 备用节点迁移

执行备用节点迁移前，Agent 必须通过实机检查确认存在以下至少一种情况：

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

成功降级后，Agent 必须完成：

- 将事件终态改为 recovered_degraded，禁止使用 recovered；
- 降级命令逐字追加到 commands.log，作为该降级形态后续恢复的重放命令；
- 按本文件第 3 章的清单变更与安全边界重新生成 monitor-manifest.json，并重启 systemd 监控单元；
- 部署报告记录原拓扑、降级拓扑和容量预期，并通知用户：硬件修复后按原始部署命令恢复原拓扑，重新走部署验收并重新生成清单。

### 5.4 软件缺陷的处理

诊断结论指向第三方软件缺陷（vLLM-Ascend、CANN、驱动或依赖组件的缺陷，排除配置、资源和硬件原因）时：

- 满足第 5.1 节的原地恢复条件时，先重启服务并验收。
- 重启验收结束后立即检索社区已有的对应 issue、修复版本、补丁或可行绕过方案。无法执行重启时直接检索。检索范围包括 vLLM-Ascend 与 CANN 的 issue 跟踪、版本发布说明和社区讨论。
- 检索结论写入 `diagnosis`，并向用户报告：对应的 issue 或缺陷报告、可用的修复或绕过方案、建议的处理路径；
- 是否应用修复由用户裁决：应用补丁、升级版本或调整参数属于部署变更，按部署流程重新部署并按第 3.3 节重新生成清单；Agent 不在恢复动作内直接修改软件或其参数；

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
