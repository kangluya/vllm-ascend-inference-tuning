# 故障检测与恢复流程

本文件供 vLLM 多机部署 Skill 在收到故障事件后使用。监控进程只负责检测和 HTTP 上报，Agent 负责复核、判断、恢复和验收。

## 1. 事件处理流程

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
recovered / failed / waiting_for_user
~~~

监控事件是事实证据，不是最终故障结论。单次探测不足以支持高风险恢复时，先补充检查。

事件文件由监控进程和 Agent 共同维护，但写入范围不同：

- 监控进程首次发布事件时写入 `schema_version`、`incident_id`、`deployment_key`、`detected_at`、`service`、`node`、`recovery_group`、`affected_services`、`signals`、`evidence`、`evidence_dropped`、`confirmation_count`、`first_failed_at`、`observation`、`trigger`、`status` 和 `updated_at`，初始状态为 `detected`；同一 recovery_group 的后续探测只追加证据，不新建事件；
- Agent 开始处理时将 `status` 改为 `processing`，并补充 `diagnosis`、`recovery` 和 `validation`；
- Agent 完成验收后将状态改为 `recovered`、`failed` 或 `waiting_for_user`；
- 监控进程不覆盖 Agent 已写入的处理和验收字段。

容器退出、Docker 运行时错误、进程消失、SSH 不可达和 NPU 异常等硬信号单次确认即可上报；readiness、日志、业务探针和容器健康状态等软信号需要连续三次探测失败后才上报，期间恢复则清零。单个事件最多保留 100 条最新证据，超出部分通过 `evidence_dropped` 记录数量。

## 2. 故障类型和默认策略

| 故障类型 | 默认恢复策略 | 升级条件 |
|---|---|---|
| 进程退出 | 在原容器、原节点按原命令恢复进程 | 反复退出或容器环境损坏时重启容器 |
| 容器退出或 OOM | 在原节点按原镜像、挂载、设备和命令重建容器 | Docker、运行时或设备不可用时迁移节点 |
| readiness 丢失 | 隔离入口后重启或重建对应服务 | 真实请求仍失败时恢复进程组或角色组 |
| 业务请求失败或卡死 | 连续确认后按原拓扑恢复对应服务，不重放用户原始请求 | 仍无资源或通信进展时恢复角色组 |
| 分布式 rank 失效 | 默认按角色组恢复；不能只重启一个 rank 后报告成功 | 组重建失败或节点故障时迁移受影响组 |
| KV connector 或 Mooncake 异常 | 重建受影响的 P/D 角色组或 KV 服务 | P/D 重新注册失败时迁移角色组 |
| Proxy 异常 | 只重建 Proxy，保留健康的 P/D 和 KV 服务 | 真实请求仍失败时重新检查后端组 |
| 网络或 SSH 不可达 | 连续确认后隔离故障端点并等待或迁移 | 节点持续不可达时迁移 |
| NPU 或宿主机不健康 | 隔离故障设备或宿主机，不执行自动芯片 reset | 使用已预检的备用节点迁移 |
| 配置、镜像或模型错误 | 停止自动重试，保留证据并报告 | 用户修正部署计划后重新部署 |
| 资源耗尽 | 可清理本次残留后按原配置重试一次 | 不得自动缩小并行规模；仍不足则迁移或报告失败 |

Agent 不能因为恢复失败而缩卡、改单机、删除 rank 或修改原拓扑来伪造成功。

## 3. 原地恢复

优先原地恢复需要同时满足：

- SSH 和 Docker 可用；
- NPU 设备健康且身份未改变；
- 镜像、模型、挂载、端口和网络仍可用；
- 故障不属于持续的节点、网络或 NPU 故障；
- 不改变 TP、DP、PP、EP、rank 和服务拓扑。

恢复必须使用 deployment_report.json 和 commands.log 中确认的镜像、设备、挂载、环境和原始启动参数；monitor-manifest.json 只提供持续监控所需的服务与检查命令。

## 4. 备用节点迁移

以下情况可以迁移：

- NPU 健康持续异常；
- 宿主机或关键网络持续不可达；
- Docker 或 Ascend 运行时损坏；
- 同一恢复范围多次失败；
- 原节点无法提供原拓扑所需设备。

备用节点必须在部署前通过 SSH、Docker、镜像 digest、Ascend 挂载、NPU 型号和数量、网络、模型路径、端口、日志目录和设备占用检查。

迁移后重新确认通信 IP、网卡、设备列表、rank 地址、Proxy 后端、KV 端口和 Mooncake 地址。模型、镜像、并行规模和服务角色保持不变。

## 5. 用户主动停止

用户直接向当前 Pi Agent 发出停止请求。Agent：

1. 停止对应的 systemd 监控单元，禁止监控进程自动拉起；
2. 读取 monitor-state.json；
3. 校验 deployment_key、清单路径、PID、PGID、启动时间和命令摘要；
4. 精确停止监控进程；
5. 按逆依赖顺序通过 SSH 停止服务；
6. 验证所有服务和容器已停止。

不使用宽范围 pkill、kill -9 或端口占用者推断进程归属。

