# 单机 DinD MLP-DP 实验方法

本方法在一台 Linux 宿主机的外层 Docker Engine 中启动多个特权
`docker:27-dind` 容器。每个 DinD 容器都有独立的内层 Docker daemon，随后加入一个
内层 Swarm。它保留 Docker overlay、Swarm placement、LEGOSim phase-1 进程、
PipeComm、SimBricks BaseIf 和 `net_sockets` 的真实运行路径；但所有逻辑节点仍共享一台
物理主机的内核、NUMA、磁盘和 NIC。因此应报告为 **single-host DinD**，不能外推为真实
多服务器网络性能。

## 1. MLP-DP 的含义

每个点运行一个共同的确定性 MLP 数据并行训练任务，而不是每个节点重复运行一份 benchmark。

- 一个 CPU data rank 与两个 GPU worker 构成一个 rank group；三者由生成器静态同置。
- rank 0 按固定顺序规约所有梯度并广播模型，所以节点数改变不会改变数学规约顺序。
- GPU worker 保留 GPGPU-Sim/PipeComm 端点；由于该 CUDA kernel 的 GPGPU-Sim launch
  队列不稳定，局部梯度算术在兼容 host 路径中执行。这是端到端通信与同步实验，**不是** GPU
  周期性能模型。

## 2. 两种资源合同

### 推荐：scale-out

这是当前正式脚本 `run_dind_mlp_dp_scaleout.sh` 的配置。每个逻辑节点固定 4 vCPU、16 GiB、
1 个 CPU rank 和 2 个 GPU worker；总资源随节点数增加。它回答“增加机器是否缩短同一全局
batch 的关键路径”。

| 节点数 | CPU rank | GPU worker | 外层 DinD 总配额 |
| ---: | ---: | ---: | ---: |
| 1 | 1 | 2 | 4 vCPU、16 GiB |
| 2 | 2 | 4 | 8 vCPU、32 GiB |
| 4 | 4 | 8 | 16 vCPU、64 GiB |
| 8 | 8 | 16 | 32 vCPU、128 GiB |

默认全局 batch 为 32,768、迭代为 40、跨节点单向延迟为 1 ms。1/2/4 节点点位可并行执行；
8 节点仅在它们都自然结束、对应 DinD 已停止后执行，避免资源竞争。

### 可选：固定总资源对照

若研究“切分同一总模拟资源”的开销，固定 CPU rank=8、GPU worker=16、总外层配额=8 vCPU/
32 GiB，并按节点数分别指定 `--ranks-per-node 8 4 2 1`。这不保证加速：节点数增加只会增加
跨节点通信。不要把固定总资源的结果与 scale-out 结果混在同一加速比图中。

## 3. 前置条件

1. Linux x86-64，Docker Engine 27+，当前用户可运行 `docker`；外层 Docker 支持
   `--privileged`。
2. `docker:27-dind` 已被拉取，或可从内网镜像仓库获取。
3. 外层 Docker daemon 中已有当前 MLP-DP 镜像。每个内层 daemon 必须导入同一 image ID。
4. 构建镜像的基础层已包含 LEGOSim、Sniper、GPGPU-Sim、SimBricks socket transport 和
   `simbricks-pipe-gateway`。上游源码/许可证要求见根 README。
5. 为镜像归档预留至少一个未压缩镜像大小的 `/tmp` 空间；每个 DinD 还会保存一份导入镜像。

构建当前运行镜像：

```bash
cd ChipSystemSim
docker build -t chipsystemsim:mlp-dp-current \
  --build-arg BASE_IMAGE=chipsystemsim:native-mlp-simbricks-v30 \
  -f docker/Dockerfile.real-mlp-dp .
```

## 4. 一键 scale-out 矩阵

```bash
cd ChipSystemSim
chmod +x scripts/run_dind_mlp_dp_scaleout.sh

scripts/run_dind_mlp_dp_scaleout.sh \
  --image chipsystemsim:mlp-dp-current \
  --output-root results/mlp-dp-scaleout-$(date +%Y%m%d-%H%M%S) \
  --iterations 40 --global-samples 32768 --delay 1ms
```

脚本严格执行如下顺序：

1. 生成固定 batch、固定迭代且一 rank group/节点的 1/2/4/8 配置；
2. 顺序准备独立的 1、2、4 节点 Swarm（避免外层子网冲突）；
3. 并行运行 1、2、4 节点 coordinator；
4. 检查三个运行均自然完成，然后停止其全部 DinD 容器；
5. 准备、运行并停止 8 节点 DinD；
6. 写出 `summary.csv`。

`provision_dind_swarm.sh` 创建一份临时 image archive，再用 `--image-load-jobs`（默认 4）
并发导入内层 daemon；它缩短准备时间，不是工作负载的一部分，也不计入总仿真时长。

## 5. 手工运行一个点

适用于调试或只运行一个节点数。以下为 4 节点 scale-out 点：

```bash
IMAGE=chipsystemsim:mlp-dp-current
RUN=results/manual-n4-$(date +%Y%m%d-%H%M%S)
PREFIX=chipsystemsim-manual-n4

python3 scripts/generate_mlp_dp_matrix.py \
  --output-root "$RUN/config" --image "$IMAGE" --nodes 4 \
  --iterations 40 --global-samples 32768 --ranks-per-node 1

scripts/provision_dind_swarm.sh \
  --nodes 4 --image "$IMAGE" --total-cpus 16 --total-memory 64g \
  --delay 1ms --rate none --image-load-jobs 4 --prefix "$PREFIX"

scripts/run_dind_mlp_dp.sh \
  --nodes 4 --prefix "$PREFIX" \
  --config-dir "$RUN/config/mlp-dp-nodes4" \
  --stack mlp_dp_manual_n4 --output-dir "$RUN/nodes4" \
  --timeout-seconds 2400

docker ps -q --filter "name=^/${PREFIX}-" | xargs -r docker stop -t 30
```

`--rate none` 表示只注入 `netem delay 1ms`。若要研究带宽受限网络，显式指定例如
`--rate 10gbit`，并将其视为另一组实验。

## 6. 输出和成功判据

每个 `nodesN/` 目录包含：

- `coordinator_timing.txt`：Docker 记录的 coordinator 开始、结束和 exit code；
- `coordinator.log`：必须含 `End of Simulation`；
- `transport.log`：所有 router/transport 服务的原始日志；
- `metrics.txt`：PipeComm 记录、字节数与同步指标。

只有满足以下条件才可报告该点：

1. `coordinator_timing.txt` 的 `exit=0`；
2. `coordinator.log` 含 `**** End of Simulation ****`；
3. `metrics.txt` 存在且包含 `cross_sync_wall_union_ns`；
4. 所有点使用相同镜像 tag/ID、batch、迭代、延迟和资源合同。

汇总：

```bash
python3 scripts/summarize_dind_mlp_dp.py \
  --input-root "$RUN" --output "$RUN/summary.csv" --nodes 1 2 4 8
```

`summary.csv` 中的关键列：

- `total_simulation_seconds`：coordinator 启动至自然结束的总墙钟时长；
- `cross_node_sync_wall_seconds`：所有跨节点 `PipeComm` 读阻塞区间的墙钟并集；
- `cross_node_sync_overhead_percent`：上列除以总仿真时长，范围为 0–100%；
- `cross_node_bytes`、`cross_node_records`：跨节点 PipeComm 流量诊断。

不要用 `cross_sync_wait_ns` 除以总时长：它是全部并行 rank 的累计等待，可能超过 100%。
墙钟并集指标依赖 router 的 Unix 时间戳；DinD 同宿主满足此条件，真实多机时必须先保证 NTP/PTP
时钟同步，或改为在 coordinator 上显式记录等待区间。

## 7. 清理

停止但保留容器和镜像：

```bash
docker ps -q --filter 'name=^/chipsystemsim-' | xargs -r docker stop -t 30
```

删除某一实验的 DinD 容器和外层 bridge（结果目录不会删除）：

```bash
scripts/provision_dind_swarm.sh --nodes 4 --prefix chipsystemsim-manual-n4 --cleanup
```

不要删除不属于本实验的 Docker 容器、镜像或网络。
