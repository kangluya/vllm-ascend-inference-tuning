# vLLM 在线推理服务故障检测与 Agent 恢复方案

本文面向已经完成部署验收的 vLLM 在线推理服务，覆盖单机、普通多机和 Prefill/Decode（PD）分离部署。

在线服务需要同时解决两个问题：

1. 服务持续运行时，能够独立、连续地发现容器、进程、端口、NPU、网络和业务异常；
2. 发现异常后，能够结合本次部署的拓扑、启动参数、日志和模型行为，判断影响范围并选择恢复方式。

因此，方案分为两个相互配合的部分：

- **故障检测部分**：持续观察服务，保存客观证据，生成故障事件，不负责最终判断和恢复；
- **Agent 恢复部分**：接收故障事件，读取部署事实和运行证据，使用故障恢复 Skill 进行分析、执行恢复和验收。

故障检测部分保证服务状态不会因为 Agent 暂时繁忙而无人观察；Agent 恢复部分保留对日志、拓扑、镜像行为和业务影响的综合判断能力。两部分通过故障事件连接，互不替代。

## 1. 总体流程和职责边界

完整流程如下：

~~~text
在线 vLLM 服务
  ↓
故障检测
  ├── 监听运行时事件
  ├── 执行必要的状态检查
  ├── 保存检测证据
  └── 生成故障事件
          ↓
事件传递
  ├── 通过本地 HTTP 通知 Pi Extension
  └── 由 Pi Extension 调用 pi.sendMessage()
          ↓
当前交互式 Pi Agent
  ↓
故障恢复 Skill
  ├── 重新检查故障
  ├── 判断故障类型和影响范围
  ├── 选择恢复方式
  ├── 通过 SSH/Docker 执行恢复
  └── 完成 readiness、通信和真实请求验收
~~~

故障检测部分只描述观察到的事实，不决定重启、重建或迁移。Agent 恢复部分必须读取本次部署的实际清单并重新核对关键证据，不能依赖单次旧探测结果。

## 2. 故障检测

### 2.1 常驻监控进程和异常退出保护

故障检测由部署控制宿主机上的常驻监控进程持续执行。它在首次部署验收完成后启动，在服务运行期间保持运行。部署验收通过后，Agent 按 SKILL.md 的执行流程注册宿主机进程管理器单元并启动监控进程；监控进程启动后自行将 monitor_pid、monitor_pgid、process_start_time、命令摘要、deployment_key 和清单路径写入 monitor-state.json，Agent 只读取和校验这些字段。

监控进程由宿主机的 systemd 或等效进程管理器托管：

- 监控进程异常退出、被 OOM 杀掉或宿主机重启后，进程管理器自动重新启动；
- 监控进程重新启动后读取部署清单，检查自身状态，并重新探测所有服务；
- 仍处于 detected 或 processing 的事件继续保留并重新上报；
- 进程管理器负责拉起监控进程，不负责故障分类和服务恢复。

用户主动停止时，Agent 先停止对应的 systemd 单元，禁止监控进程被自动拉起；确认单元已停止后，再根据 monitor-state.json 中记录的 deployment_key、清单路径、PID/PGID、启动时间和命令身份校验当前进程。

### 2.2 检测信号

故障监控进程接收或检查以下信号：

- **容器生命周期异常**：容器退出、OOM、启动失败或健康状态变为异常；
- **服务进程异常**：预期服务进程消失、进程命令身份不匹配或服务进程异常退出；
- **服务可用性异常**：API、RPC、控制端口或 readiness 持续不可用；
- **节点和网络异常**：宿主机不可连接、数据面地址不可达、通信端口中断或网络接口异常；
- **NPU 和驱动异常**：设备丢失、设备健康状态异常、驱动报错或通信后端报错；
- **服务日志异常**：vLLM、HCCL、TP/DP、Mooncake 或 Proxy 等组件出现明确的错误信息；
- **业务请求异常**：真实请求持续失败、超时、空响应或返回结果异常；没有真实流量时，通过最小业务请求确认服务链路；
- **分布式通信组异常**：rank 数量不完整、通信组无法建立或已有通信组中断。

### 2.3 检测结果

监控进程发现异常后：

1. 记录节点、服务、容器、时间和原始信号；
2. 保存必要的命令输出、日志摘要和状态结果；
3. 对同一 recovery_group 的未结束事件去重，并向已有事件追加新的服务、信号和证据；
4. 没有未结束事件时生成一个故障事件；已有事件时只更新该事件；
5. 仅将新生成的事件放入待上报队列；
6. 继续检测其他服务。

容器退出、Docker 运行时错误、进程消失、SSH 不可达和 NPU 异常等硬信号单次确认即可上报；readiness、日志、业务探针和容器健康状态等软信号需要连续三次探测失败后才上报，期间恢复则清零。单个事件只保留有限数量的最新证据，并记录被丢弃的旧证据数量。

监控进程不判断最终故障类型，也不执行恢复动作。事件必须先写入临时文件，再通过原子改名发布；检测进程重启后继续处理仍处于待处理状态的事件。

## 3. 故障事件传递给当前 Pi Agent

### 3.1 HTTP 通知链路

Pi Extension 在当前 Pi 会话中提供本地 HTTP 接口：

~~~text
GET  /health
POST /v1/fault-events
~~~

检测部分将故障事件提交给 POST 接口，Pi Extension 校验事件后调用官方的 pi.sendMessage，将通知送入当前交互式 Agent 会话。

这条链路只负责传递事件：

~~~text
检测部分
  → HTTP
  → Pi Extension
  → pi.sendMessage()
  → 当前 Pi Agent
~~~

Pi Extension 不判断故障，也不执行 SSH 或 Docker。

### 3.2 部署关联和通知配置

每次部署生成一个稳定的 deployment_key，例如：

~~~text
deployment-20261029-001
~~~

部署清单只保存本机通知地址，不包含认证配置：

~~~json
{
  "deployment_key": "deployment-20261029-001",
  "control_host": {
    "ssh_target": "<control-host>",
    "deployment_dir": "/data/boom/deployment"
  },
  "agent_notify": {
    "transport": "http",
    "endpoint": "http://127.0.0.1:18765/v1/fault-events"
  }
}
~~~

Pi Extension 将 deployment_key 关联到当前 Pi 会话。检测部分上报事件时只携带 deployment_key，不需要知道 Pi 的进程 ID、窗口或内部对象。

事件通知包含：

~~~json
{
  "schema_version": 1,
  "deployment_key": "deployment-20261029-001",
  "incident_id": "incident-001",
  "event_path": "events/incident-001.json",
  "summary": {
    "service": "decode-0",
    "node": "node-a",
    "signals": [
      "process_missing",
      "readiness_failed"
    ]
  }
}
~~~

Pi Extension 校验 deployment_key、事件编号和事件路径后，向当前 Agent 发送故障通知。事件被接受后返回 202；Pi 不可用或事件重复时，检测部分保留事件并重试。

通知接口不使用 Token。监控进程和 Pi Extension 位于同一台控制宿主机，接口只允许通过本机回环地址访问；不得将该端口发布到外部网络。

Agent 收到通知后，通过 control_host 的 SSH 连接读取事件文件、部署清单、部署报告和相关日志。HTTP 消息只负责唤醒当前 Agent，不负责传输完整部署目录。

### 3.3 Pi 在 Docker 中运行

如果 Pi 运行在宿主机上，Extension 绑定 `127.0.0.1`。如果 Pi 运行在 Docker 容器中：

- Pi Extension 在容器内监听 0.0.0.0:8765；
- 宿主机只发布本地端口 127.0.0.1:18765:8765；
- Agent 通过 SSH 连接 control_host 读取 /data/boom/deployment；
- 检测部分通过宿主机本地 HTTP 端口提交事件。

## 4. Agent 分析和恢复故障

Agent 收到事件后，按照故障恢复 Skill 执行：

~~~text
通过 SSH 从 control_host 读取故障事件
  ↓
通过 SSH 读取部署清单、部署报告和相关日志
  ↓
通过 SSH 重新检查故障
  ↓
核对容器、进程、端口、设备、启动参数和服务依赖
  ↓
根据第 5 节故障类型和恢复策略判断影响范围
  ↓
选择进程恢复、容器重启、角色组重建或备用节点迁移
  ↓
通过 SSH/Docker 执行恢复
  ↓
执行 readiness、通信、模型身份和真实 completion 验收
  ↓
更新故障事件状态并报告结果
~~~

Agent 可以根据实际日志和镜像行为调整恢复顺序，不受固定升级链限制。Agent 必须：

- 只操作本次部署清单中的节点、容器和服务；
- 验证镜像、设备、容器身份和原始启动参数；
- 不使用宽范围 pkill、kill -9 或无归属校验的 docker rm；
- 不通过缩卡、改单机或删除角色来伪造恢复成功；
- 恢复完成后通过真实验收确认服务可用。

故障分类表是 Agent 的决策参考，不是检测部分中的自动匹配代码。证据不足时，Agent 先请求补充探测或报告无法确认。

## 5. 统一故障分类、检测信号和恢复策略

下表为故障分类和默认恢复策略，作为 Agent 的决策参考。Agent 必须结合本次事件证据、部署拓扑、镜像行为和当前运行状态判断是否适用；证据不足时先补充探测或报告无法确认。表中的时间是镜像和模型已经位于目标节点、SSH 和 Docker 可用、恢复动作可以直接执行时的典型自动处理时间，包括恢复动作和基本验收，不包括首次拉取镜像、下载模型、物理网络维修、硬件更换和人工修正配置的时间。大模型重新加载时间应在部署验收时测得，并叠加到相应估算中。

| 故障类型 | 检测信号和确认条件 | 默认恢复范围和策略 | 典型自动恢复时间 | 升级条件 |
|---|---|---|---|---|
| 进程退出 | 容器仍在运行但预期 vLLM 进程不存在；PID 启动时间和命令身份不匹配 | 单进程：使用原命令在原容器、原节点恢复进程 | 30 秒–5 分钟，另加实际模型加载时间 | 进程反复退出、容器环境损坏或原地恢复失败，升级为容器重建 |
| 容器退出或 OOM | docker inspect 状态为 exited、OOM 或非零退出；日志包含启动失败或 OOM | 容器：在原节点按原镜像、挂载、设备和命令重建 | 1–10 分钟，另加实际模型加载时间 | 达到重试上限、Docker/运行时异常或设备不可用，升级为节点迁移 |
| readiness 丢失 | API、RPC、控制端口连续不可用；进程仍存在 | 单服务或容器：先隔离入口，再重启或重建对应服务 | 30 秒–5 分钟 | 端口恢复但真实请求仍失败，按进程组或角色组重建 |
| 业务请求失败或卡死 | 清单配置的最小业务探针连续失败；真实用户流量失败由 Agent 结合日志、网关统计和真实 completion 复核 | 使用 readiness、端口、进程状态和受控的最小业务探针连续确认服务异常后，按原命令重启对应进程或容器，保持原拓扑；不重放用户原始请求 | 1–10 分钟，包含确认和验收时间 | 采样仍无 CPU、NPU、显存和网络进展，升级为角色组重建 |
| 分布式 rank 失效 | rank 数量不完整、通信端口断开、HCCL/TP/DP 错误；只有当前 vLLM-Ascend 版本、镜像和并行模式明确支持安全重加入时，才允许单 rank 恢复 | 默认按角色组恢复：普通多机重建 Master 和全部 Worker，PD 分离重建受影响的 Prefill 或 Decode 组；不能只确认单个 rank 重新运行或端口恢复 | 5–20 分钟，另加实际模型加载时间 | 组重建失败或承载该组的节点故障时，使用备用节点按原拓扑重建受影响通信组；健康节点保持不变，必须通过完整通信检查和真实 completion 才能报告成功 |
| KV connector 或 Mooncake 异常 | connector 初始化、注册、KV 获取/发送或远端 engine 查询失败 | 角色组或单服务：按受影响的 P/D 角色组重建；Mooncake 服务单独故障时先重建该服务 | 2–15 分钟，另加实际模型加载时间 | P/D 重新注册失败，迁移受影响角色组 |
| Proxy 异常 | Proxy 进程退出、入口端口不可用或后端列表丢失 | 单服务：只重建 Proxy，保留健康的 P/D 和 KV 服务 | 10 秒–2 分钟 | Proxy 恢复后真实请求仍失败，重新检查后端角色组 |
| 网络或 SSH 不可达 | SSH、数据面 IP、通信端口或网卡连续不可达 | 节点或整体部署：等待短暂抖动，同时隔离故障端点 | 不固定；短暂抖动通常 1–5 分钟，迁移通常 10–30 分钟 | 节点或链路持续不可用，迁移到通过预检的备用节点 |
| NPU 不健康 | npu-smi 健康异常、设备丢失、驱动或 HCCL 报错 | 节点或整体部署：隔离使用故障设备的服务，不执行自动芯片 reset | 有备用节点时通常 10–30 分钟；无备用节点时无法自动恢复 | 有等价备用节点则迁移；没有则报告降级或失败 |
| 宿主机不健康 | SSH 失败、Docker/Ascend 运行时不可用或关键目录不可读 | 节点或整体部署：不在故障宿主机上无限重试 | 有备用节点时通常 10–30 分钟；无备用节点时无法自动恢复 | 迁移该节点上的服务或报告失败 |
| 配置、镜像或模型错误 | 参数互斥、挂载缺失、镜像 digest 不一致、模型加载失败、端口冲突 | 不恢复：停止自动重试并保留完整证据 | 不自动恢复 | 由用户修正部署计划后重新部署 |
| 资源耗尽 | 显存、KV Cache、磁盘、端口或设备资源不足 | 单服务或容器：不自动缩小 TP/DP/PP/EP；可清理本次残留后重试一次 | 清理重试通常 1–10 分钟；迁移通常 10–30 分钟 | 仍不足则使用已声明的等价备用节点或报告失败 |

### 5.1 当前支持的恢复范围

| 范围 | 说明 |
|---|---|
| 进程原地恢复 | Agent 在原宿主机、原容器或原服务实例中恢复 vLLM 进程 |
| 容器原地重建 | Agent 在原宿主机使用原镜像、原设备和原命令重新创建容器 |
| 角色组重建 | Agent 同时重建一个分布式通信组，例如全部 Worker、Prefill 或 Decode |
| 备用节点迁移 | Agent 将受影响服务迁移到预先声明且通过预检的备用宿主机 |
| 恢复后验证 | Agent 通过 readiness、通信检查和真实 completion 判断是否恢复 |

### 5.2 原地恢复

Agent 只有在确认以下条件后才选择原地恢复：

1. SSH 和 Docker 仍然可用；
2. 所需 NPU 健康，设备数量和设备身份未改变；
3. 镜像 digest、模型目录、挂载、端口和网络仍可用；
4. 故障不属于持续的宿主机、网络或 NPU 故障；
5. 恢复不改变 TP、DP、PP、EP、rank 和服务拓扑。

原地恢复必须使用部署记录中的原镜像、原挂载、原设备、原环境变量和原始启动参数。Agent 不能因为恢复失败而自行缩小并行规模或改单机部署。

### 5.3 备用节点迁移

Agent 在以下情况考虑备用节点迁移：

- NPU 健康持续异常；
- 宿主机或关键网络持续不可达；
- Docker 或 Ascend 运行时损坏；
- 同一恢复范围已经多次失败；
- 原节点无法提供原拓扑所需的设备数量。

备用节点必须在部署阶段声明并通过 SSH 权限、Docker、镜像 digest、Ascend 挂载、等价 NPU 型号和数量、网络、模型路径、端口、日志目录及设备占用检查。迁移不能通过缩卡或改单机拓扑来伪装成恢复成功。迁移后，Agent 需要重新确认通信 IP、网卡、设备列表、rank 地址、Proxy 后端、KV 端口和 Mooncake 地址；模型、镜像、并行规模和服务角色保持不变。

## 6. Agent 直接停止服务

用户向 Agent 发出停止本次部署的请求时，停止流程固定为：

~~~text
用户请求停止服务
  ↓
Agent 停止对应的 systemd 单元
  ↓
确认单元不再自动拉起
  ↓
读取 monitor-state.json，校验 deployment_key、清单路径、PID/PGID、启动时间和命令
  ↓
必要时精确停止仍存活的监控进程，并确认其退出
  ↓
Agent 按逆依赖顺序通过 SSH 停止目标服务
  ↓
Agent 验证容器和服务已经全部停止并汇报结果
~~~

监控进程不提供单独的 shutdown 接口。Agent 只能使用部署启动时记录的 deployment_key、清单路径、PID/PGID、启动时间和命令摘要精确停止本次监控进程。禁止使用宽范围 pkill、kill -9 或通过端口占用者猜测监控进程。

监控进程先退出后，后续容器退出事件不会再触发新的故障事件。HTTP 上报层随监控进程一起退出，服务停止完成后不再有残留运行组件。

如果 Agent 在监控进程退出后、服务停止前异常中断，服务可能暂时处于无人监控状态。下一次 Agent 恢复会话必须先读取部署报告和容器状态，完成未结束的停止操作或重新启动监控；不得把用户已经明确请求的停止当作故障恢复。

## 7. 文件目录设计与生成数据

### 7.1. 文件目录设计

SKILL.md 是多机部署与故障检测恢复的统一入口，负责模式选择、部署流程、监控启动、故障事件处理和恢复验收。

~~~text
.agents/skills/vllm-ascend-multi-node-deployment/
├── SKILL.md
├── references/
│   ├── common.md
│   ├── multi-node-deployment.md
│   ├── multi-node-PD-disaggregated-deployment.md
│   ├── monitor-manifest.md
│   └── fault-recovery.md
└── scripts/
    └── vllm_fault_monitor.py

.pi/extensions/
└── vllm-fault-events.ts
~~~

其中：

- SKILL.md：统一描述多机部署和故障检测恢复，并引用两个故障恢复参考文件；
- monitor-manifest.md：定义监控清单、检查命令、control_host 和 HTTP 通知配置；
- fault-recovery.md：定义故障检测、事件传递、Agent 分析、恢复策略和验收流程。
- vllm_fault_monitor.py：负责检测故障、保存事件和通过 HTTP 通知 Pi Extension；
- vllm-fault-events.ts：负责 HTTP 接收和 pi.sendMessage，不执行恢复；
- common.md、multi-node-deployment.md 和 multi-node-PD-disaggregated-deployment.md：保留多机部署公共规则和部署模式说明。

### 7.2. 故障记录和状态

运行数据放在部署目录中：

~~~text
/data/boom/deployment/
├── deployment_report.json
├── commands.log
├── monitor-manifest.json
├── monitor-state.json
└── events/
    └── incident-<incident_id>.json
~~~

其中：

- monitor-manifest.json：保存 deployment_key、节点、服务、容器、recovery_group、进程检查、readiness、NPU、日志和业务探针命令、control_host SSH 配置引用及 HTTP 通知配置；
- monitor-state.json：保存监控进程当前状态、deployment_key、清单路径、PID/PGID、启动时间和命令身份；
- events/incident-<incident_id>.json：保存单个故障从发现到恢复完成的完整记录。

故障处理分为三个状态：

- detected：监控进程已经发现异常并写入事件；
- processing：监控进程已将事件交给 Agent，Agent 正在分析或执行恢复；
- recovered、failed 或 waiting_for_user：Agent 完成验收、判断无法恢复或需要用户处理。

监控进程只负责写入 incident 文件的检测部分和最小运行状态。Agent 在同一个 incident 文件中补充恢复记录，至少包括：

- 事件时间、incident ID、节点和服务；
- Agent 采用的故障判断及证据引用；
- 选择的恢复范围和实际执行动作；
- 执行的 SSH/Docker 操作及结果；
- 是否使用备用节点；
- readiness、通信、模型身份和真实 completion 验收结果；
- 最终状态和仍需用户处理的事项。

Pi 不可用时，监控进程保留 detected 事件并报告待处理状态，不自行执行恢复。这样不会因为 Agent 暂时离线而丢失故障，也不会让没有上下文的代码擅自重启或迁移服务。



