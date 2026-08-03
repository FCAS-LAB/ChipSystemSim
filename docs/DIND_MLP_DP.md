# 单机 DinD MLP-DP 复现实验

本文档描述当前默认实验：在一台 Linux 宿主机中建立多个独立 DinD daemon，组成内部 Docker
Swarm，并运行通过 SimBricks BaseIf 传输 PipeComm 的 LEGOSim MLP-DP 工作负载。

## 1. 实验边界

每个 DinD 容器是一个逻辑节点，不是一台物理服务器。该方法保留 Docker overlay、Swarm
placement、LEGOSim phase-1 进程、PipeComm、SimBricks BaseIf 和 socket transport 的实际
执行路径；但所有节点共享同一宿主机内核、磁盘、NUMA 拓扑和物理 NIC。

因此结果必须标注为 **single-host DinD**。`netem delay` 用于控制节点间链路延迟，不能代替
真实多机的 NIC、交换机或拥塞行为。

## 2. 当前资源与工作负载合同

每个逻辑节点固定配置如下：

| 项目 | 值 |
| --- | --- |
| CPU 配额 | 8 vCPU，绑定到独立宿主 CPU 集合 |
| 内存限制 | 16 GiB，`memory-swap` 同为 16 GiB |
| 应用拓扑 | 1 CPU rank + 2 GPU worker，三者静态同置 |
| MLP-DP | 32,768 全局样本，3 次迭代 |
| 网络 | 跨节点单向 `netem delay 1ms`，无 rate 限速 |

节点数增加时，总资源与 rank group 数线性增加：

| 节点数 | 总 vCPU / 内存 | CPU rank / GPU worker |
| ---: | --- | --- |
| 1 | 8 / 16 GiB | 1 / 2 |
| 2 | 16 / 32 GiB | 2 / 4 |
| 4 | 32 / 64 GiB | 4 / 8 |
| 8 | 64 / 128 GiB | 8 / 16 |

全局样本数不变，每个 rank group 处理其中一部分样本。梯度通过确定性二叉树规约并广播模型；
浮点规约顺序会随节点数变化，因此使用误差阈值而非位级相等判定正确性。

GPU worker 保留 GPGPU-Sim/PipeComm 端点，但局部梯度算术采用兼容 host 路径。它适合研究
端到端通信、同步和关键路径，不应解释为 GPU 周期级性能。

## 3. 前置条件

1. Linux x86-64，Docker Engine 27+；当前用户能够运行 `docker`，并允许特权 DinD 容器。
2. 运行 8 节点点需要至少 64 个可用逻辑 CPU、128 GiB 可用内存和足够临时磁盘空间。
3. 外层 Docker 中已有 `chipsystemsim:dind-netem` 和兼容的 MLP-DP runtime image。
4. runtime image 的基础层必须包含 ABI 匹配的 LEGOSim、Sniper、GPGPU-Sim、已打补丁的
   SimBricks socket transport 及 `simbricks-pipe-gateway` 编译依赖。
5. 构建 `Dockerfile.real-mlp-dp` 时，构建上下文必须有 `third_party/simbricks`；它是上游
   依赖，不随本仓库提交。准备方式见 `docs/external-overlays.md`。

构建 DinD 与运行镜像：

```bash
cd ChipSystemSim
docker build -t chipsystemsim:dind-netem -f docker/Dockerfile.dind-netem .
docker build -t chipsystemsim:mlp-dp-steady-v2 \
  --build-arg BASE_IMAGE=chipsystemsim:native-mlp-simbricks-v30 \
  -f docker/Dockerfile.real-mlp-dp .
```

`BASE_IMAGE` 不是公开通用镜像。若新主机没有该基础层，应先在已授权的 LEGOSim/SimBricks
源码环境完成基础镜像构建，或从可信内部制品库导入与本提交匹配的基础镜像。

## 4. 一键运行 1/2/4/8 节点矩阵

```bash
RUN=/mnt/large-disk/chipsystemsim-results/mlp-dp-$(date +%Y%m%d-%H%M%S)

scripts/run_dind_mlp_dp_per_node_scaleout.sh \
  --image chipsystemsim:mlp-dp-steady-v2 \
  --output-root "$RUN"
```

该脚本会：

1. 检查最大节点数所需的宿主 CPU 数；
2. 从外层运行镜像创建临时 `docker save` archive（也可提供 `--image-archive`）；
3. 对 1、2、4、8 节点顺序调用 `run_dind_mlp_dp_steady_once.sh`；
4. 将每个 DinD 节点限制为固定 8 vCPU/16 GiB 并绑定独立 CPU 集；
5. 每点自然完成后仅清理该点创建的 DinD 容器、bridge 和内层 image store；
6. 在 `$RUN/summary.csv` 输出统一的稳态指标表。

脚本自动删除自己创建的临时 image archive；若传入 `--image-archive`，该文件始终由调用者
负责保留或删除。

只运行部分点，例如 2、4、8：

```bash
scripts/run_dind_mlp_dp_per_node_scaleout.sh \
  --image chipsystemsim:mlp-dp-steady-v2 \
  --output-root /mnt/large-disk/mlp-dp-rerun \
  --nodes "2 4 8"
```

## 5. 单点运行与结果汇总

单点运行器的参数顺序为 `NODES OUTPUT_ROOT IMAGE_ARCHIVE IMAGE`。它默认读取
`LEGOSIM_PER_NODE_CPUS=8` 与 `LEGOSIM_PER_NODE_MEMORY_GIB=16`，并允许显式覆盖以进行独立
敏感性实验。

```bash
docker save -o /mnt/large-disk/mlp-runtime.tar chipsystemsim:mlp-dp-steady-v2

scripts/run_dind_mlp_dp_steady_once.sh 4 \
  /mnt/large-disk/mlp-dp-single-n4 \
  /mnt/large-disk/mlp-runtime.tar \
  chipsystemsim:mlp-dp-steady-v2

python3 scripts/summarize_steady_measurement.py \
  /mnt/large-disk/mlp-dp-single-n4/measurement/transport.log \
  --coordinator-timing /mnt/large-disk/mlp-dp-single-n4/measurement/coordinator_timing.txt \
  --output /mnt/large-disk/mlp-dp-single-n4/measurement/steady_summary.json
```

矩阵汇总器接受每个点的结果根目录：

```bash
python3 scripts/collect_steady_matrix.py --output summary.csv \
  /path/to/mlp-dp-steady-1-* /path/to/mlp-dp-steady-2-* \
  /path/to/mlp-dp-steady-4-* /path/to/mlp-dp-steady-8-*
```

## 6. 成功判据与指标

一个点满足以下条件才可报告：

1. `measurement/coordinator_timing.txt` 含 `exit=0`；
2. `measurement/coordinator.log` 含 `**** End of Simulation ****`；
3. `measurement/transport.log` 含 PipeComm `pipe-metric` 记录；
4. `measurement/steady_summary.json` 由汇总脚本成功写出。

`summary.csv` 的主要列：

| 列 | 含义 |
| --- | --- |
| `coordinator_wall_seconds` | coordinator 从启动到自然结束的总仿真墙钟时长；不含 DinD 预配置。 |
| `steady_wall_seconds` | PipeComm 定界后的应用稳态时间。 |
| `cross_node_sync_wall_union_seconds` | 跨节点读阻塞区间的墙钟并集。 |
| `cross_node_sync_overhead_percent` | 上述同步时间 / 稳态时间，范围 0–100%。 |
| `cross_node_read_events_in_window` / `...bytes...` | 工作区间中的跨节点接收事件与字节数。 |

不要用所有 rank 的 `cross_sync_wait_ns` 累加值除以总时间；各 rank 可以并行等待，累计值可能超过
100%。真实多机部署还必须用 NTP/PTP 校准时钟，或改为由 coordinator 记录等待区间。

## 7. 清理与安全范围

运行器只清理它以 `chipsystemsim-steady-n*` 前缀创建的 DinD 容器和显式传入的
`dind-storage` 目录。不要对其他项目的 Docker 容器、网络、镜像或卷执行批量清理。

若运行因宿主关机或网络中断而留下某个点，可用该点的精确 `--prefix` 与 `--dind-data-root` 调用
`scripts/provision_dind_swarm.sh --cleanup`，然后删除该点结果目录后重跑。
