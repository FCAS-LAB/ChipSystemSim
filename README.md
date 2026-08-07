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

`MLP-DP` 是项目提供的确定性数据并行工作负载，用于验证同一 MLP 任务的多节点划分、通信
和同步。GPU 端保留 GPGPU-Sim/PipeComm 端点；局部梯度算术走兼容 host 路径，因而结果不能
解释为 GPU 微体系结构周期性能。

## 推荐复现：固定每节点资源的 DinD 矩阵

当前推荐入口是
[`scripts/run_dind_mlp_dp_per_node_scaleout.sh`](scripts/run_dind_mlp_dp_per_node_scaleout.sh)。
它顺序运行 1、2、4、8 个 DinD Swarm 节点，并在每个点完成后清理该点创建的容器和内层镜像
存储。

| 项目 | 固定值 |
| --- | --- |
| 每个逻辑节点 | 8 vCPU、16 GiB、1 CPU rank、2 GPU worker |
| 节点数 | 1、2、4、8 |
| 总 CPU rank / GPU worker | 1/2、2/4、4/8、8/16 |
| 全局任务 | 同一 MLP-DP，32,768 全局样本、3 次迭代 |
| 跨节点网络 | `netem delay 1ms`，不注入带宽限制 |
| 放置 | 每个 rank 的 CPU 与两个 GPU worker 静态同置 |

因此，总 Docker CPU/内存随节点数线性增加，但**每节点**资源不变；全局样本数固定，由更多
rank group 数据并行划分。该实验考察增加节点与 rank group 后的端到端关键路径，而不是“只把
固定数量 rank 切到更多节点”的固定总资源对照。

完整前置条件、构建、运行、成功判据和指标定义见
[docs/DIND_MLP_DP.md](docs/DIND_MLP_DP.md)。最新已验证结果见
[docs/RESULTS_MLP_DP_PER_NODE_8VCPU.md](docs/RESULTS_MLP_DP_PER_NODE_8VCPU.md)。

## 快速开始

在 Linux x86-64 Docker 主机上执行。运行完整 8 节点矩阵至少需要 64 个可用逻辑 CPU、128 GiB
可用内存，以及容纳一份运行镜像和每点临时 DinD 存储的磁盘空间。

```bash
git clone git@github.com:LeenSed/ChipSystemSim.git
cd ChipSystemSim

# DinD 节点需要 tc/netem。
docker build -t chipsystemsim:dind-netem -f docker/Dockerfile.dind-netem .

# BASE_IMAGE 必须是本机已有、ABI 兼容的 LEGOSim + SimBricks 基础镜像。
docker build -t chipsystemsim:mlp-dp-steady-v2 \
  --build-arg BASE_IMAGE=chipsystemsim:native-mlp-simbricks-v30 \
  -f docker/Dockerfile.real-mlp-dp .

scripts/run_dind_mlp_dp_per_node_scaleout.sh \
  --image chipsystemsim:mlp-dp-steady-v2 \
  --output-root /mnt/large-disk/chipsystemsim-results/mlp-dp-$(date +%Y%m%d-%H%M%S)
```

脚本自动为内层 daemon 导出/导入同一镜像，最终写出 `summary.csv`。若已有镜像归档，传入
`--image-archive /path/to/runtime.tar` 可以避免再次 `docker save`。

`Dockerfile.real-mlp-dp` 不会从空白克隆下载 LEGOSim 或 SimBricks；它需要兼容基础镜像和
构建上下文中的 `third_party/simbricks`。上游依赖、固定补丁与离线基础镜像准备见
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

## 目录

- `real/`：原生 LEGOSim/SimBricks 分布式运行时、router、gateway 与 MLP-DP 源码。
- `docker/`：运行镜像与 DinD netem 镜像 Dockerfile。
- `scripts/`：DinD provisioning、矩阵运行、稳态指标与结果汇总脚本。
- `docs/`：复现实验、结果与限制说明。
- `platform/`：历史 Python/JSON-TCP 合成适配器；不得作为原生 LEGOSim 性能结果。

不要提交 Docker image archive、DinD 数据目录、私钥、密码、registry token 或完整服务日志。
`results/` 默认被忽略；只将经审查的参数、CSV 摘要或必要最小证据写入版本库。
