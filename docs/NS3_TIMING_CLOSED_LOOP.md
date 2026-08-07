# LEGOSim–ns-3 时序闭环

本文档说明本项目中功能数据传输、时序仿真以及主机 wall-clock 度量的边界。它适用于单台物理机上的 Docker DinD/Swarm 部署；多个 DinD worker 是逻辑 LEGOSim 节点，不应被表述为已完成真实物理多机测量。

## 两条独立路径

```text
功能数据面（不参与 cycle）
simlet PipeComm
  -> worker-local router
  -> SimBricks BaseIf + net_sockets（仅跨逻辑 worker）
  -> worker-local router
  -> PipeComm payload

时序闭环（不传输 PipeComm payload）
Phase 1 InterChiplet WRITE/READ
  -> bench.txt
  -> ns3-phase2
  -> delayInfo.txt
  -> 下一轮 Phase 1 的 NetworkDelayStruct / SYNC
```

`PipeComm` 的 socket 阻塞时长是功能部署的主机 wall-clock 现象，不能换算或注入为模拟 cycle。`ns3-phase2` 不读取 PipeComm 内容，只读取 LEGOSim 已定义的六列 `bench.txt` 轨迹：

```text
src_cycle dst_cycle src_node dst_node flit_count descriptor
```

它以 ns-3 point-to-point 链路和 IPv4 路由重放该轨迹，并写回 LEGOSim 原生可读取的格式：

```text
src_cycle src_node dst_node descriptor delay_count delay_0 delay_1 [...]
```

普通 `WRITE/READ` 事务输出两个延迟：源端发送完成和目的端包到达。带 `BARRIER`、`LOCK` 或 `UNLOCK` 描述符的事务输出四个延迟：前向发送完成、前向到达、确认发送完成和确认到达；这与 `NetworkDelayStruct::getEndCycle` 的既有协议相对应。

## ns-3 模型和默认参数

实现位于 [`real/ns3_phase2.cc`](../real/ns3_phase2.cc)。它读取已有的 Graphviz mesh 拓扑，以一条双向 point-to-point 链路对应每个 `--` 边；IPv4 全局路由负责多跳转发，设备队列因此会影响后续包的到达时间。

当前默认值如下，均会写进 `ns3_phase2_summary.json`：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `cycle_ns` | 1 | 一个 Phase-2/InterChiplet cycle 的时长 |
| `flit_bytes` | 32 | MLP 配置中 `-F 4` 对应的 `4 × 64 bit` |
| 链路带宽 | 128 Gbps | 每条拓扑边的 ns-3 点到点速率 |
| 单跳传播延迟 | 1 ns | 每条拓扑边的传播时延 |
| UDP 分段上限 | 1400 B | 将一个 LEGOSim 多 flit 事务分段，保持队列竞争可见 |
| 设备发送队列 | 100,000 packets | 保持 PopNet 的无丢包时序语义；队列竞争只改变完成 cycle，不丢弃 LEGOSim 事务 |

这些是明确、可调整的网络模型参数，不是 Docker `tc netem` 的实际延迟。修改原始 MLP 生成命令时可传入 `--ns3-cycle-ns`、`--ns3-link-rate`、`--ns3-link-delay-ns` 与 `--ns3-queue-packets`；生成的 YAML 会记录实际值。

## 构建

`docker/Dockerfile.real-mlp-dp` 和 `docker/Dockerfile.real-native-mlp-simbricks` 都会从固定修订 `feb1f92eb9e71b129d5ae4aa09e4484c1554cc51` 获取 SimBricks 的 ns-3 分支，并只构建 adapter 所需的 `core,network,internet,point-to-point,applications` 模块。没有启用 ns-3 的 SimBricks NetIf/NIC 模块，因为 LEGOSim 没有可映射到该设备接口的 CPU/GPU/NIC 模型。

先准备与目标 Dockerfile ABI 兼容的 LEGOSim + SimBricks 基础镜像，再执行例如：

```bash
docker build -t chipsystemsim:native-mlp-ns3 \
  --build-arg BASE_IMAGE=chipsystemsim:native-mlp-simbricks-v30 \
  -f docker/Dockerfile.real-native-mlp-simbricks .
```

镜像构建需要访问固定的 ns-3 Git 仓库以及 PyTorch 官方 CPU wheel 索引，并以最多两个并行编译任务构建 ns-3。构建固定使用与 Ubuntu 22.04 / Python 3.10 兼容的 `torch==1.11.0+cpu` 与 `torchvision==0.12.0+cpu`；运行阶段不依赖外网。

`chipsystemsim:native-mlp-simbricks-v30` 是服务器保留的、已含原始 LEGOSim MLP 运行时的基底镜像。若改用其他基底，必须先验证其中同时包含 `/opt/legosim/artifact/MLP`、GPGPU-Sim、Sniper、MNSIM 与 SimBricks socket transport；不能用仅含 MLP-DP 工作负载的镜像替代原始 MLP 基底。

## 单机 DinD 运行和闭环检查

原始 MLP 的配置生成器会自动选择 ns-3 Phase 2：

```bash
python3 scripts/generate_native_mlp_dind_matrix.py \
  --output-root /mnt/large-disk/mlp-ns3-config \
  --image chipsystemsim:native-mlp-ns3 --nodes 1

scripts/provision_dind_swarm.sh \
  --nodes 1 --prefix chipsystemsim-ns3-1 \
  --image chipsystemsim:native-mlp-ns3 \
  --per-node-cpus 8 --per-node-memory 16GiB
scripts/run_dind_mlp_dp.sh \
  --nodes 1 --prefix chipsystemsim-ns3-1 \
  --config-dir /mnt/large-disk/mlp-ns3-config/mlp-nodes1 \
  --stack chipsystemsim_ns3_mlp_1 \
  --output-dir /mnt/large-disk/results/mlp-ns3-n1
```

对于“每个逻辑节点资源相同”的 1/2/4/8 节点扩展实验，使用新增的每节点模式，而不要使用默认的固定总资源模式。例如每节点 8 vCPU、16 GiB：

```bash
scripts/provision_dind_swarm.sh \
  --nodes 4 --prefix chipsystemsim-ns3-4 \
  --image chipsystemsim:native-mlp-ns3 \
  --per-node-cpus 8 --per-node-memory 16GiB
```

默认 `--total-cpus 8 --total-memory 32g` 仍保留，用于固定总资源的可比实验；两种资源模式不要在同一结果表中混合比较。

上述 `run_dind_mlp_dp.sh` 完成后必须同时检查：

```bash
cat /mnt/large-disk/results/mlp-ns3-n1/timing_feedback.txt
find /mnt/large-disk/results/mlp-ns3-n1/timing-artifacts/coordinator \
  -path '*proc_r*_p2_t*/phase2_*' -type f -print
```

成功的 ns-3 闭环至少应满足：

1. `timing_feedback=present`；
2. 最近一个 `proc_r*_p2_t*/phase2_input_bench.txt` 与同目录的 `phase2_delayInfo.txt` 都非空，且记录数一致；
3. `scripts/validate_ns3_timing_feedback.py` 已同时核验协调器日志在随后的 Phase 1 出现非零 `Load N delay records.`；
4. `phase2_metrics.csv` 和 `phase2_summary.json` 均存在。

不要只依据 `End of Simulation` 或 Phase-2 进程的零退出码声称时序闭环已生效；旧实验恰好暴露了这种不足：PopNet 启动成功，但下一轮仍加载零条 `delayInfo`。

## 指标与时间域

`scripts/collect_timing_metrics.py` 将以下两类数据写入 `metrics.txt`，但不会相除或相互换算。

| 字段前缀 | 时间域 | 含义 |
| --- | --- | --- |
| `ns3_normal_*_cycles` | 模拟 cycle | 普通 `WRITE/READ` 事务中 LEGOSim Phase 1 由 ns-3 反馈推进的源端与目的端同步量；`ns3_normal_destination_sync_block_cycles` 是目的端在 `SYNC` 时实际被推进的等待量。特殊 barrier/lock 事务另以四段延迟保留，不能混入这个普通读写聚合。 |
| `same_logical_worker_*` | 主机 wall-clock ns | 同一逻辑 worker 内的 PipeComm router 操作。 |
| `cross_legosim_*` | 主机 wall-clock ns | 不同逻辑 LEGOSim worker 之间的功能 PipeComm 操作；在单机 DinD 中仍属于同一物理机。 |
| `cross_physical_host_*` | 主机 wall-clock ns | 仅当 routing 配置将两个 worker 标为不同物理 host 时计入。单机 DinD 正确结果为零。 |

其中，`ns3_payload_bytes` 是所有 Phase 2 trace（包含特殊控制事务）的总载荷字节数；`ns3_normal_payload_bytes` 仅为普通 `WRITE/READ` 事务。与时序推进有关的 `ns3_normal_*_cycles` 均只统计后者，避免把 barrier/lock 的四段协议时延错误混入普通数据读写。

对于 wall-clock 同步比，只有 `*_sync_wall_union_ns / coordinator wall-clock` 可以形成 0–100% 的并集占比；不要使用各 router 的 `sync_wait_ns` 求和除以总时长，因为并行 rank 的等待会重叠。

`scripts/summarize_dind_mlp_dp.py` 会将这些字段导出为 CSV 中的 `cross_legosim_*`、`cross_physical_host_*` 和 `ns3_normal_*` 列。单机 DinD 的 `cross_physical_host_*` 应为零；若非零，说明生成 routing 时显式把逻辑 worker 映射到了多个物理 host。

## 已验证范围与未完成项

在受限本机资源下，已验证 ns-3 adapter 的 C++ 编译、普通两段 delay、特殊四段 delay、以及同链路并发队列会产生不同的到达 cycle。尚未在本机完成 Docker DinD 的完整 MLP 自然结束运行：当前 WSL 发行版没有可用 Docker Engine。因此，在取得一份 `timing_feedback=present` 的 DinD 产物前，不应把任何历史 wall-clock 结果解释为已纳入 ns-3 通信 cycle 的性能结果。
