# ChipSystemSim：LEGOSim PipeComm over SimBricks BaseIf

> 当前原生路径已新增 **Phase 1 `bench.txt` → ns-3 → `delayInfo.txt` → 下一轮 `SYNC`** 的时序闭环；功能 PipeComm/SimBricks BaseIf 仍保持独立。完整边界、参数和验证条件见 [docs/NS3_TIMING_CLOSED_LOOP.md](docs/NS3_TIMING_CLOSED_LOOP.md)。

本项目在开源 **LEGOSim** 和 **SimBricks** 的基础上，将 LEGOSim phase-1 进程的
`PipeComm` 跨节点消息接入 SimBricks `BaseIf`/`net_sockets`，并以单机 DinD +
Docker Swarm 部署可复现的多逻辑节点 MLP 数据并行实验。

## 这是什么，以及不是什么

同一逻辑节点内的 `PipeComm` 由本地 router 排队；跨节点消息经
`simbricks-pipe-gateway`、SimBricks `BaseIf` 和 socket transport 到达目标节点。
LEGOSim 的 Sniper、GPGPU-Sim、MNSIM 与 DSA 仍按原进程图运行。

这**不是**把 LEGOSim CPU/GPU 映射为 SimBricks PCIe、NIC、交换机或 ns-3 设备模型。
因此应称为 **LEGOSim PipeComm over SimBricks BaseIf**：SimBricks 在此承担跨节点通信
传输层，不是完整硬件接口级联仿真。

当前主实验使用**上游原始 MLP**：Phase 1 保留 1 个 Sniper CPU、4 个 GPGPU-Sim、1 个 DSA
和 1 个 MNSIM；原始 PopNet Phase 2 被 ns-3 轨迹回放器替换。每轮 Phase 1 先输出
`bench.txt`，ns-3 写回 `delayInfo.txt`，下一轮 Phase 1 再将其读入 `SYNC`。因此，GPU 端仍是
GPGPU-Sim/PipeComm 端点，网络模拟 cycle 来自 LEGOSim 的原生时序接口，而非 PipeComm 的
墙钟阻塞。历史 `MLP-DP` 工作负载仅用于旧的功能性实验，不是本 README 的主复现路径。

除原始 MLP 外，仓库还提供了固定 32 GPU rank 的 **Matmul-DP/block-GEMM** 时序微基准。它用于
通过受控反事实测量“跨 LEGOSim 通信使应用关键路径额外增加了多少模拟 cycle”，而不是用
PipeComm 的 host wall-clock `READ` 等待替代该结论。已导出的 1/2/4/8 节点数据、ns-3 参数和
复现步骤见 [Matmul-DP 反事实结果记录](docs/RESULTS_MATMUL_DP_COUNTERFACTUAL_20260809.md)。

## 推荐复现：原始 MLP + ns-3 时序闭环

采用单机 DinD + Docker Swarm 可以复现 1、2、4、8 个**逻辑** LEGOSim worker 的部署。每一
点均使用通信感知静态放置；同一 DinD worker 内为本地 PipeComm，不同 worker 通过
SimBricks BaseIf/socket transport 路由。ns-3 仅处理 `bench.txt` 时序轨迹，不传输功能 payload。

| 项目 | 固定值 |
| --- | --- |
| 每个逻辑节点 | 8 vCPU、16 GiB |
| 节点数 | 1、2、4、8 |
| Phase 1 进程图 | 1 Sniper CPU、4 GPGPU-Sim、1 DSA、1 MNSIM |
| Phase 2 | ns-3：`bench.txt → delayInfo.txt → 下一轮 SYNC` |
| 外层逻辑网络 | `netem delay 1ms`，不注入带宽限制 |
| ns-3 链路 | 128 Gbps、1 ns/跳、32 B flit、100,000 packet 队列 |
| 放置 | communication-aware；CPU 优先与高权重 GPU 边同置 |

因此，总 Docker CPU/内存随逻辑节点数线性增加，但**每节点**资源不变。原始 MLP 的进程图和
通信语义不变；节点数只改变这些 simlet 的静态部署位置。详细进程放置见
[docs/ORIGINAL_MLP_STATIC_SCHEDULING.md](docs/ORIGINAL_MLP_STATIC_SCHEDULING.md)。

功能路径、时序闭环、成功判据和指标定义见
[docs/NS3_TIMING_CLOSED_LOOP.md](docs/NS3_TIMING_CLOSED_LOOP.md)；已验证的 1/2/4/8
logical-worker 结果及其边界见
[docs/RESULTS_NATIVE_MLP_NS3_DIND_20260808.md](docs/RESULTS_NATIVE_MLP_NS3_DIND_20260808.md)。

## 快速开始

在 Linux x86-64 Docker 主机上执行。运行完整 8 节点矩阵至少需要 64 个可用逻辑 CPU、128 GiB
可用内存，以及容纳一份运行镜像和每点临时 DinD 存储的磁盘空间。

```bash
git clone git@github.com:LeenSed/ChipSystemSim.git
cd ChipSystemSim

# DinD 节点需要 tc/netem。
docker build -t chipsystemsim:dind-netem -f docker/Dockerfile.dind-netem .

# BASE_IMAGE 必须是本机已有、ABI 兼容且含原始 MLP 运行时的镜像。
docker build -t chipsystemsim:native-mlp-ns3-v4 \
  --build-arg BASE_IMAGE=chipsystemsim:native-mlp-ns3-v3 \
  -f docker/Dockerfile.real-native-mlp-simbricks-gpu-fix .

RUN=/mnt/large-disk/chipsystemsim-results/native-mlp-ns3-$(date +%Y%m%d-%H%M%S)
IMAGE=chipsystemsim:native-mlp-ns3-v4

python3 scripts/generate_native_mlp_dind_matrix.py \
  --output-root "$RUN/config" --image "$IMAGE" --nodes 1 2 4 8 --stream-output

scripts/provision_dind_swarm.sh \
  --nodes 4 --prefix native-mlp-n4 --image "$IMAGE" \
  --dind-image chipsystemsim:dind-netem \
  --per-node-cpus 8 --per-node-memory 16GiB \
  --dind-data-root "$RUN/dind-n4"

scripts/run_dind_mlp_dp.sh \
  --nodes 4 --prefix native-mlp-n4 \
  --config-dir "$RUN/config/mlp-nodes4" --stack native-mlp-n4 \
  --output-dir "$RUN/nodes4" --timeout-seconds 7200
```

上述示例是单个 4 节点点；更换 `--nodes`、`--prefix`、配置目录和数据根即可运行 1、2、8 节点。
`provision_dind_swarm.sh` 会为内层 daemon 导出/导入同一镜像；若已有镜像归档，传入
`--image-archive /path/to/runtime.tar` 可以避免再次 `docker save`。成功后必须检查
`timing_feedback.txt`、`coordinator.log` 的自然结束标记和 `metrics.txt`；不要只看容器退出码。

`Dockerfile.real-native-mlp-simbricks-gpu-fix` 不会从空白克隆下载完整 LEGOSim 运行时；它需要
兼容的原始 MLP 基础镜像，并在其上应用 GPGPU-Sim 运行时修复。基础镜像的上游依赖、固定补丁
与离线准备见
[docs/external-overlays.md](docs/external-overlays.md) 和 [docker/README.md](docker/README.md)。

## 结果口径

- **总仿真时间**：`coordinator_timing.txt` 的 coordinator 墙钟时长；包含该次 coordinator/
  worker 的启动和回收，但不含外层 DinD 集群预配置。
- **稳态仿真时间**：由 `transport.log` 中 PipeComm 外部时间戳界定的应用工作区间，排除
  DinD、Swarm 和 worker 初始化。
- **同步时间**：工作区间中所有跨节点 PipeComm 读阻塞区间的墙钟并集。
- **同步占比**：同步时间 / 稳态仿真时间；使用并集而不是各 rank 等待时间之和，故范围为
  0–100%。

单节点不存在跨 rank 启动/完成屏障，稳态边界使用“首个工作 header 到最后一个 payload”近似，
结果 JSON 中会明确标记该规则。

对于 ns-3 cycle 域的跨 LEGOSim 同步开销，不应把上述 wall-clock 指标相除，而应运行配对的
本地化时序基线：保持 workload、放置与 PipeComm 功能路径不变，仅令跨 worker 的 ns-3
`delayInfo` 为零，并以 `Benchmark elapses` 的收敛 cycle 计算
`(T_actual - T_local_baseline) / T_actual`。实现和注意事项见
[ns-3 时序闭环](docs/NS3_TIMING_CLOSED_LOOP.md#跨-worker-反事实基线)。

## 目录

- `real/`：原生 LEGOSim/SimBricks 分布式运行时、router、gateway 与 MLP-DP 源码。
- `docker/`：运行镜像与 DinD netem 镜像 Dockerfile。
- `scripts/`：DinD provisioning、矩阵运行、稳态指标与结果汇总脚本。
- `docs/`：复现实验、结果与限制说明。
- `platform/`：历史 Python/JSON-TCP 合成适配器；不得作为原生 LEGOSim 性能结果。

不要提交 Docker image archive、DinD 数据目录、私钥、密码、registry token 或完整服务日志。
`results/` 默认被忽略；只将经审查的参数、CSV 摘要或必要最小证据写入版本库。
