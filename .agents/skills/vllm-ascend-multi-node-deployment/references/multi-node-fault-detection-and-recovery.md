# multi-node fault detection and recovery

本文件定义多机 vLLM-Ascend 服务部署完成并验收 `ready` 之后的故障巡检、卡死检测、
分级恢复和恢复后验收，适用于 **multi-node deployment** 和 **multi-node
PD-disaggregated deployment**；单机部署不使用本文件。SSH/Docker 宿主机边界、精确
清理、最小业务请求和报告记录遵循 [common.md](common.md)；各角色启动命令和启动后
检查遵循对应模式文档（[multi-node-deployment.md](multi-node-deployment.md) 第 3、
4 节，[multi-node-PD-disaggregated-deployment.md](multi-node-PD-disaggregated-deployment.md)
第 5、6 节）；本文件不重复这些内容。

本文件是响应式流程：用户报告服务异常（不可用、请求失败、超时、hang）或提出“巡检”
“故障恢复”“健康检查”时执行，不作为常驻监控。所有巡检和恢复命令在目标节点宿主机上
执行，SSH 规则和 Agent 所在节点例外规则同 common.md。

巡检和恢复以部署记录（`/data/boom/deployment/deployment_report.json`、
`commands.log`）中的节点、角色、容器名和 NPU 分配为唯一事实来源；无部署记录时，先
向用户确认节点、角色、容器名和 NPU 分配，再执行巡检。除第 4.2 节迁移外，恢复一律
重新执行部署记录中的原命令，不修改部署参数。

## 执行流程（总览）

1. **巡检**：按第 1 节采集信号并判定故障类型；
2. **卡死检测**：疑似卡死时按第 2 节采样判定；
3. **分级恢复**：按第 3 节分级决策表执行恢复动作，涉及整组或迁移时按第 4 节；
4. **恢复后验收与记录**：按第 5 节验收，按第 7 节写故障处理记录。

## 1. 巡检

巡检范围默认为本次部署的全部角色容器（普通多机为全部 Master/Worker；PD 为全部
Prefill/Decode/Proxy，池化组合含 Mooncake Master）；用户指明异常范围时先检该范围，
未定位到故障再扩展到全部。

### 1.1 信号表

| 故障类型 | 判定命令（宿主机执行） | 判定条件 |
|---|---|---|
| 软件故障 | `docker ps -a --format '{{.Names}}\t{{.Status}}'` | 本次部署任一容器非 running |
| 软件故障 | `docker inspect <container> --format '{{.State.Status}} {{.State.ExitCode}}'` | 状态非 running 或退出码非 0 |
| 芯片故障 | `npu-smi info` | 本次部署使用的卡 Health 列非 OK |
| 芯片故障 | `npu-smi info -i <id> -c 0 -t health` | 输出健康状态异常 |
| 卡死 | 容器 running，最小业务请求超时或无响应 | 按第 2 节确认 |
| 网络断连 | `ping -c 1 -W 2 <对端 network_ip>`（同 common.md 第 1 节） | 节点间 ping 不通 |
| 网络断连 | `/usr/local/Ascend/driver/tools/hccn_tool -i <id> -link -g` | 链路状态 down |

`<id>` 为该卡在 `npu-smi info` 输出中的卡编号，与部署记录的 NPU 分配一致。

### 1.2 巡检顺序

1. 全部角色容器状态（软件故障信号）；
2. 各节点本次部署使用的 NPU 健康（芯片故障信号）；
3. 公共入口最小业务请求（普通多机为 Master API 端口，PD 为 Proxy 端口；请求格式同
   common.md 第 4 节，60 秒超时视为无响应）——成功返回排除卡死，超时或无响应触发
   第 2 节；
4. 节点间连通（网络断连信号）；
5. 对异常角色读宿主机日志 `/data/boom/deployment/<log-filename>`，记录 ERROR、
   Traceback 和 connector / KV 传输相关报错原文。

巡检结果记录：节点、角色、容器、故障类型、信号原文；作为第 3 节的输入并写入第 7 节
故障处理记录。

## 2. 卡死检测

触发条件：第 1 节判定疑似卡死，或用户报告服务 hang。判定对象为疑似卡死角色所在的
单个节点。

对该节点以 60 秒间隔连续采样 6 轮（得到 5 个相邻区间），每轮记录：

1. 每张本次部署使用的 NPU 的 AI Core 利用率和 HBM 使用量：

```bash
npu-smi info -i <id> -c 0 -t usages
```

2. 每张卡的 RoCE 收发包计数之和（取输出中 `roce_tx_all_pkt_num` 与
   `roce_rx_all_pkt_num` 之和）：

```bash
/usr/local/Ascend/driver/tools/hccn_tool -i <id> -stat -g
```

3. 服务进程 CPU 时间总和：先取该节点服务进程 PID，再累加每个 PID 的 CPU 时间：

```bash
npu-smi info | grep python | awk '{print $5}'
awk '{print $14+$15}' /proc/<PID>/stat
```

CPU 时间单位为 clock tick，100 tick = 1 秒。

5 个区间全部同时满足下表 4 项时判定卡死：

| 指标 | 卡死条件 |
|---|---|
| AI Core 利用率 | < 5% |
| HBM 使用量 | 无增长 |
| RoCE 收发包计数 | 每分钟增量 < 100 |
| 进程 CPU 时间 | 每分钟增量 < 5 秒（500 tick） |

判定卡死后按第 3 节 L1 恢复。任一区间不满足时判定未卡死：能按第 1 节其余故障类型
归类的按对应级别恢复，不能归类时向用户报告巡检信号。

## 3. 分级恢复

进入恢复前，先对故障节点本次部署使用的全部 NPU 执行芯片健康检查
（`npu-smi info -i <id> -c 0 -t health`）：任一卡异常走 L2，全部正常走 L1；L1 每次
重建前重复该检查，芯片转为异常时改走 L2。

| 级别 | 适用故障 | 恢复动作 | 失败升级 |
|---|---|---|---|
| L1 | 软件故障、卡死 | 原节点精确清理后按原命令重建 | 重建 3 次失败升 L4 |
| L2 | 芯片故障 | 停占用容器后复位芯片 | 复位 3 次失败升 L3 |
| L3 | 芯片不可复位 | 保留现场后迁移到备用节点 | 无备用节点或迁移失败，报告降级 |
| L4 | L1 反复失败、网络恢复后 | 按部署顺序整组清理重建 | 整组 3 次失败保留现场报告 |

网络断连不直接进入恢复：先向用户报告故障节点和信号（物理网络修复不在本 skill 范围）；
网络恢复后重新巡检，服务仍异常时按 L4 整组恢复。

### 3.1 L1：软件故障和卡死

1. 按 common.md 第 3 节精确清理该角色容器（逆依赖顺序，按容器名精确匹配）；
2. 重新执行部署记录中该角色的原启动命令；
3. 按第 5 节验收。

同一角色最多重建 3 次，仍失败升级 L4。

### 3.2 L2：芯片故障

1. 按 common.md 第 3 节精确清理占用故障卡的角色容器；PD 场景同一节点可能有多个角色，
   只清理占用该卡的角色；
2. 用 `npu-smi info` 确认该卡已无进程；
3. 在宿主机以 root 执行复位：

```bash
npu-smi set -t reset -i <id> -c 0
```

4. 复查 `npu-smi info -i <id> -c 0 -t health`：恢复正常则按 L1 重建该角色并按第 5 节
   验收；仍异常则重复复位，最多 3 次；
5. 3 次复位后仍异常，升级 L3。

### 3.3 L3：芯片不可复位

1. 保留现场：

```bash
npu-smi set -t collect-log -i <id>
msnpureport -f
```

2. 集群中存在不在本次部署节点列表中、且用户确认可用于迁移的健康节点时，按第 4.2 节
   迁移；迁移最多 3 次；
3. 无备用节点或迁移 3 次失败时报告降级：故障芯片清单、受影响角色、服务现状（哪些
   角色仍可用、公共入口是否可用）和现场文件清单，停止恢复。

### 3.4 L4：整组恢复

1. 按 common.md 第 3 节对本次部署全部角色容器精确清理（逆依赖顺序）；
2. 按第 4.1 节顺序、用部署记录中的原命令重建全部角色；
3. 按第 5 节验收。

整组恢复不修改任何部署参数（不换卡、不换端口、不换 connector）。最多 3 次，仍失败时
保留现场并报告。

## 4. 恢复顺序与迁移联动

### 4.1 整组恢复顺序

整组恢复顺序等于部署启动顺序（common.md 第 3 节）：

```text
multi-node deployment：Master → 全部 Worker（并行）
multi-node PD-disaggregated deployment：Mooncake Master（仅池化组合）→ 全部 Prefill/Decode → Proxy
```

### 4.2 迁移（L3）

1. 备用节点先通过 common.md 第 1、2 节预检：SSH 登录、节点间连通、模型路径（仅迁移
   加载模型的角色时需要）、镜像可用、Ascend 挂载源；并确认迁移要用的 NPU 全部健康
   （`npu-smi info -i <id> -c 0 -t health`）且无进程占用；
2. 按对应模式文档为新节点重新生成迁移角色的启动命令：环境变量（`HCCL_IF_IP`、
   `*_SOCKET_IFNAME`、`ASCEND_RT_VISIBLE_DEVICES` 等）、挂载和设备按新节点展开；
3. 按下方联动清单表更新配置，重建迁移角色和表中受影响的容器；
4. 按第 5 节验收；迁移改变了公共入口（Master API 或 Proxy）地址时，同步更新部署报告
   中的入口地址。

迁移联动清单：

| 迁移对象 | 联动更新 | 需重建的容器 |
|---|---|---|
| 普通多机 Worker | 无 | 该 Worker |
| 普通多机 Master | 全部 Worker 的 `--data-parallel-address` 改为新节点 IP | Master + 全部 Worker |
| PD Prefill 或 Decode 节点 | Proxy 的 `--prefiller-hosts` / `--decoder-hosts` 更新该节点地址；该节点是角色 DP rank 0 时，该角色其余节点的 `--data-parallel-address` 一并更新 | 迁移角色 + Proxy（rank 0 迁移时为该角色全部节点 + Proxy） |
| PD Mooncake Master（仅池化组合） | `mooncake.json` 的 `master_server_address` 改为新节点 IP:端口 | Mooncake Master + 全部 Prefill/Decode |
| Proxy | 无 | Proxy |

## 5. 恢复后验收

每次恢复动作（L1 重建、L2 复位后重建、L3 迁移、L4 整组）完成后执行：

1. 对应模式文档的启动后检查：普通多机为 [multi-node-deployment.md](multi-node-deployment.md)
   第 4 节，PD 为 [multi-node-PD-disaggregated-deployment.md](multi-node-PD-disaggregated-deployment.md)
   第 6 节；
2. common.md 第 4 节最小业务请求（PD 的公共入口是 Proxy）。

全部通过判定恢复成功，写第 7 节故障处理记录（`result=recovered`）；失败时按第 3 节
升级或重试。

## 6. 故障演练（可选）

用户要求验证恢复能力时执行，只演练 L1：

1. 选定一个 Worker 或 Decode 节点，在宿主机注入进程故障：

```bash
npu-smi info | grep python | awk '{print $5}'
kill -9 <PID>
```

2. 触发第 1 节巡检，确认识别为软件故障；
3. 按 L1 恢复并按第 5 节验收；
4. 写第 7 节故障处理记录，`fault_type` 记为 `drill`。

演练不执行芯片复位、迁移和整组恢复。

## 7. 故障处理记录

每次故障处理完成后，在部署目录写入 `fault-recovery-<timestamp>.json`
（`<timestamp>` 格式 `YYYYMMDD-HHMM`，同模式文档日志时间戳规则），至少记录：

- `time`：处理时间；
- `scope`：节点、角色、容器名；
- `fault_type`：`software` / `chip` / `hang` / `network` / `drill`；
- `signals`：巡检信号摘要（判定命令与关键输出）；
- `level`：L1–L4；
- `actions`：动作序列，含每次清理、重建、复位、迁移的结果；
- `result`：`recovered` / `degraded` / `failed`；
- `evidence`：现场保留文件清单（执行 L3 时）。

凭据不写入记录。
