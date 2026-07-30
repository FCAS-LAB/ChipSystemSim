# ChipSystemSim：LEGOSim、SimBricks 与 Docker Swarm 的分布式实验工程

本仓库将上游 **LEGOSim** 的异构工作负载进程图接入 **SimBricks BaseIf** 传输，
并用 Docker Swarm 将 phase-1 模拟器分布到 1、2、4 或 8 台 Linux 仿真机。
目标是验证原生 LEGOSim 进程、跨节点 PipeComm、SimBricks 通道和 Swarm 放置能否
共同工作；它不是一个已经过上游性能校准的完整系统模拟平台。

> 实验代码运行在 Linux 容器和 Ubuntu VM 中。Windows 仅用于 VMware Workstation
> 管理、镜像中继与运行 PowerShell 部署脚本，不能运行 Linux Docker 容器镜像。

## 项目完成了什么

原始 LEGOSim 假设所有 phase-1 模拟器与 `interchiplet` 位于同一主机，通过本地
FIFO 调用 `PipeComm`。本项目作出的关键改动如下：

1. **保留上游进程图。** coordinator 继续运行上游 `interchiplet` 与 phase-2
   PopNet；每一个原始 phase-1 模拟器由 Swarm transport 服务中的 `worker.py` /
   `process_proxy.py` 启动，而非用合成 CPU/GPU 角色替代。
2. **将 PipeComm 扩展到跨节点。** 本地 pipe 由 `simbricks_pipe_router.py` 的内存
   队列处理；跨 worker-slot pipe 交给 `simbricks_pipe_gateway.cc`，再经过 SimBricks
   BaseIf 与官方 `dist/sockets/net_sockets` 代理到达目标节点。
3. **消除分布式启动竞争。** `simbricks_worker_supervisor.py` 先建立监听端，再建立
   连接端，并对 Swarm DNS、TCP 监听和 BaseIf 就绪状态作有界重试。
4. **生成可复现实验配置。** `generate_placement.py`、`generate_simbricks_topology.py`、
   `generate_simbricks_routing.py` 和 `generate_swarm_stack.py` 从工作负载坐标及节点数
   生成 placement、拓扑、路由和 stack YAML；不满足节点标签时会失败，不会退化为单机。
5. **可观测性。** router 为每次完成的 PipeComm 输出 `pipe-metric` JSON，记录操作、
   字节数、源/目标 slot、是否跨节点、网关服务时间和读阻塞时间。
6. **VMware 多机复现。** 提供 Ubuntu 基础 VM、Swarm 节点复制/加入、VMnet registry
   中继和网络恢复脚本。相关代码副本见 [`vm/`](vm/README.md)。
7. **兼容八个工作负载。** 原生路径覆盖 MLP、DLRM、ResNet、BFS、FFT、PageRank、
   PDE、MoE。为 MoE 修复了与当前 InterChiplet API 不匹配的读写返回值，并补充 DRAM
   服务的放置参数。

## 架构

```text
                    coordinator（Swarm manager）
              interchiplet + phase-2 PopNet
                               |
                  Swarm overlay / 服务发现
                               |
     +-------------------------+-------------------------+
     |                                                   |
transport slot A                                   transport slot B
worker.py -> 原始 phase-1 模拟器                    worker.py -> 原始 phase-1 模拟器
     |                                                   |
PipeComm overlay -> 本地 router -> BaseIf gateway == SimBricks == BaseIf gateway
                    （本地 pipe 直接入队；跨节点 pipe 走上方通道）
```

`vm/node-local/` 保存节点内运行时快照；`vm/inter-node/` 保存跨节点路由、生成器、
VMware Swarm 和 registry 中继快照。它们是归档副本；权威源代码仍是 `real/` 与
`scripts/`。

## 目录

- `real/`：原生 LEGOSim + SimBricks 分布式运行时、PipeComm overlay、配置生成器与补丁。
- `docker/`：基础镜像、运行时 overlay 及八工作负载镜像的 Dockerfile。
- `scripts/`：远程矩阵执行、指标汇总、VMware/Swarm/registry 管理脚本。
- `vm/`：节点内和节点间仿真机代码快照及说明。
- `examples/`：保留的静态 Compose 样例。
- `platform/`：早期单机合成适配器；与原生路径分开，仅保留用于历史对照。
- `results/`：本地实验产物和日志，默认不提交，避免上传大文件与可能的环境信息。
- `third_party/simbricks/`：SimBricks 源码依赖；请按其自身许可证使用。

## 获取上游依赖

本仓库不复制整个 SimBricks 或 LEGOSim 上游仓库。构建原生镜像前，在工作区中准备固定
版本的 SimBricks，并应用本项目的低频 BaseIf 进度补丁：

```bash
mkdir -p third_party
git clone https://github.com/simbricks/simbricks.git third_party/simbricks
git -C third_party/simbricks checkout 6bc7fa5bf7ae4f69c54316c7ac8634af644ece1e
git -C third_party/simbricks apply ../../patches/simbricks/0001-legosim-low-rate-baseif.patch
```

该补丁将低频 PipeComm 请求的 BaseIf 进度报告阈值调整为 1，加入有限心跳，并使 socket
轮询线程短暂让出 CPU；否则单次请求/应答可能永远未达到上游默认批处理阈值。LEGOSim 与
八个 benchmark 源码也必须按各自许可证放在 Dockerfile 所引用的相邻工作区路径中。

## 环境要求

### 构建机与仿真机

- Ubuntu 22.04 或兼容 Linux，x86-64；Docker Engine 及 Docker Swarm。
- 至少 1 台 manager 和最多 7 台 worker；每台均可访问同一个 Docker registry。
- manager 与 worker 使用可互通的固定私网 IP；Swarm 所需 TCP 2377、TCP/UDP 7946、
  UDP 4789 必须在 VM 网络中可达。
- 每个节点需预先加入 Swarm 并标记 `chipsystemsim.node.0=true` 至
  `chipsystemsim.node.7=true`。本项目不会自动把未标记节点用于实验。
- Docker 构建阶段需要网络以安装依赖和获取基础镜像；运行阶段所有节点都必须能拉取
  完全相同的镜像 tag/digest。

### Windows + VMware Workstation（可选）

- VMware Workstation、`vmrun.exe`、`mkisofs.exe`、OVF Tool。
- Ubuntu 22.04 cloud OVA 及其 SHA256SUMS，Python 3 + Paramiko。
- 用于 VMnet 的宿主机 registry 中继。虚拟磁盘、seed ISO 和密码文件只能保留在本地，
  已被 `.gitignore` 排除。

## 首次部署示例：八工作负载的有界功能验证

以下示例验证所有原生进程图、跨节点 SimBricks 通道和 PipeComm 通信是否能启动。
它刻意使用 Sniper fast-forward 和 GPU 指令上限，因此**不是**完整 benchmark 到自然
结束的性能实验。

### 1. 准备 Swarm 节点

在 manager 上检查节点，再按实验规模给节点打标签：

```bash
docker node ls
docker node update --label-add chipsystemsim.node.0=true NODE_0
docker node update --label-add chipsystemsim.node.1=true NODE_1
# 继续至 chipsystemsim.node.7；一节点实验只需要 node.0。
```

在 Windows/VMware 环境，可先创建基础 VM，再按需要复制和配置 Swarm：

```powershell
Set-Location D:\root\work2026\ChipSystemSim
.\scripts\provision_vmware_base.ps1
.\scripts\provision_vmware_swarm.ps1 -NodeCount 8
```

#### 已版本化的八节点资源配置

[`vm/profiles/vmware-fresh-8x-1vcpu-1536mib.json`](vm/profiles/vmware-fresh-8x-1vcpu-1536mib.json)
记录当前已验证 VM 集合的可移植参数：8 台 `legosim-node-0-fresh` 至
`legosim-node-7-fresh`，每台 1 vCPU、1536 MiB 内存，使用 NAT `VMnet8` 和 e1000
网卡。该资源上限对所有 1/2/4/8 节点点位相同；节点数改变的是 Swarm placement，而不是
每台 VM 的资源。

先做只读校验：

```powershell
.\scripts\apply_vmware_resource_profile.ps1
```

只有在八台目标 VM 均已关机时，才可以应用该清单：

```powershell
.\scripts\apply_vmware_resource_profile.ps1 -Apply
```

脚本不会修改 MAC 地址、UUID、VMDK、NoCloud seed 或凭据；这些都是主机本地状态，不能
提交到 Git。`-Apply` 检测到虚拟机仍在运行会拒绝写入，而不会强制关机。

脚本需要本地 OVA、VMware 工具与密码文件；具体路径和默认 VMnet 地址可在脚本参数中
修改。启动 registry 中继后，所有 VM 应使用宿主机 VMnet 地址而不是 `localhost`。

### 2. 构建并发布相同镜像

八工作负载镜像的构建上下文是工作区根目录，因为 FFT/PageRank/PDE/MoE 源码位于
相邻的 `single_stage_simulator/benchmark/`：

```bash
cd /root/work2026
docker build --progress=plain \
  -t REGISTRY/chipsystemsim:native-eight-functional \
  -f ChipSystemSim/docker/Dockerfile.real-eight-functional .
docker push REGISTRY/chipsystemsim:native-eight-functional
```

该 Dockerfile 依赖已经构建好的、包含 LEGOSim、SimBricks 与运行时 overlay 的基础
镜像。若基础镜像 tag 不同，用 `--build-arg BASE_IMAGE=...` 指定。所有 Swarm 节点必须
能从同一 registry 拉取此镜像。

### 3. 生成配置并执行矩阵

先用 `real/generate_distributed_yaml.py` 为每个 `WORKLOAD-nodes{1,2,4,8}` 目录生成
`workload.yml`、`topology.json`、`routing.json` 和 `stack.yml`。有界功能验证需使用：

- `--sniper-cores 4 --sniper-maxthreads 16 --sniper-fast-forward`
- 对 GPU 工作负载增加 `--gpgpu-max-instructions 1000`
- `--stream-output`

配置准备好后，从 Linux 构建机运行远程矩阵驱动器：

```bash
cd /root/work2026/ChipSystemSim
python3 scripts/run_remote_functional_matrix.py \
  --manager 192.168.244.135 \
  --password-file /secure/legosim-guest-password.txt \
  --source-root results/functional-native-eight \
  --output-root results/native-eight-functional \
  --image REGISTRY/chipsystemsim:native-eight-functional \
  --workloads mlp dlrm resnet bfs fft pagerank pde moe \
  --nodes 1 2 4 8 --timeout-seconds 90 --observation-seconds 120
```

每一点只有在所有应有 phase-1 proxy 已启动且观测到首个 InterChiplet 命令时才记为
`functional-ok`。驱动器结束后会删除对应 Swarm stack；仍应手工执行
`docker stack ls` 和 `docker service ls` 进行最终清理检查。

### 4. 汇总指标

```bash
python3 scripts/summarize_native_matrix.py \
  --input-root results/native-eight-functional \
  --workloads mlp dlrm resnet bfs fft pagerank pde moe \
  --nodes 1 2 4 8
```

生成 `native_matrix_summary.csv` 与 `native_matrix_summary.json`。每行包括：

- `measurement_elapsed_seconds`：部署开始至固定观测窗口结束的时长，不含日志下载和
  stack 清理；
- `speedup_vs_1node`：同一工作负载的一节点时长除以当前点时长；
- `cross_write_service_ms`：路由器日志中捕获的跨节点 PipeComm 写入服务时间之和；
- `pipecomm_sync_ms`：路由器日志中捕获的 PipeComm 读阻塞时间之和。

router 日志以尾部采集，且有界窗口可能尚未触发某些工作负载的远程 pipe。因此 0 值仅
表示该采集窗口没有捕获事件，不表示完整程序不存在通信。

## 已验证实验

`results/native-eight-functional-long-20260729/` 保留了一次 8 工作负载 × 4 个节点数
的有界 VMware Swarm 验证。全部 32 个点为 `functional-ok`，观测时间为 120 秒；相应
汇总文件是 `native_matrix_summary.csv` 和 `native_matrix_summary.json`。

这是集成正确性和近似开销的证据，不是完整应用完成时间。完整性能研究至少应：固定
输入、配置、镜像 digest、网络与节点资源；多次重复每点；采集完整生命周期日志；并让
每个上游 benchmark 自然完成后再报告性能结论。

## 早期合成适配器

`platform/`、`docker/Dockerfile` 与 `scripts/run_experiments.sh` 是早期的单机 Docker
合成角色实验。它使用 Python 角色、TCP JSON RPC 和 ns-3 单包延迟助手，不能替代或
证明原生 LEGOSim + SimBricks 的结果。保留它们仅用于历史对照；新的分布式实验应使用
本文所述的 `real/` 原生路径。

## 安全与提交约定

- 禁止提交 VM 磁盘、OVA/ISO、私钥、密码、registry token 或宿主机日志。
- `results/` 默认被忽略；如需发表结果，导出已脱敏的 CSV/JSON 和实验配置，而非原始
  Docker 服务日志。
- `third_party/simbricks/` 需要遵守其上游许可证；LEGOSim 和 benchmark 源码没有被
  复制到本仓库，构建前须在工作区中按其各自许可证提供。

## MLP 固定总进程的有界通信轮次

`scripts/run_mlp_scalability.py` 用于 MLP 的可恢复、逐点运行。它在每个节点数下保留同一
逻辑进程图：phase-1 始终为 4 个 GPGPU-Sim、1 个 Sniper、1 个 DSA 和 1 个 MNSIM；
phase-2 始终为 1 个 PopNet。节点数仅改变 placement，不增加模拟进程总数。

为在资源有限的 VMware 节点上快速验证通信路径，`functional-matrix/mlp-nodes*-insn1000`
使用上游模拟器的 GPU 指令上限与 Sniper fast-forward。它仍运行原生 LEGOSim 进程、
PipeComm overlay、SimBricks BaseIf 和 Swarm 服务，但不是完整 MLP 自然结束的性能结果。

```bash
python3 scripts/run_mlp_scalability.py \
  --password-file /secure/legosim-guest-password.txt \
  --source-root results/functional-matrix \
  --source-suffix=-insn1000 \
  --output-root results/mlp-bounded-native \
  --image REGISTRY/chipsystemsim:native-mlp-fixed-eight-stagger-v1 \
  --nodes 1 2 4 8 --repetitions 1 \
  --timeout-seconds 180 --expected-pipecomm-events 13
```

一个 `communication-epoch-complete` 样本要求所有 7 个 phase-1 进程都已启动，并从原生
router 收集到指定数量的已完成 PipeComm 事件。样本在清理 Swarm stack 前会归档 coordinator
进程日志、bridge trace 和容器内的进程/套接字快照；因此 `timeout` 可以区分 GPU 模拟计算过长
与 PipeComm/网络故障，不能被解释为成功完成。

### 镜像一致性是实验前置条件

Swarm 的相同镜像 tag 不足以保证内容一致。开始一个多机点位前，应在所有参与节点校验相同
image ID（或更严格的 registry digest）：

```bash
IMAGE=REGISTRY/chipsystemsim:native-mlp-fixed-eight-stagger-v1
docker image inspect "$IMAGE" --format '{{.Id}}'
```

所有节点输出必须相同；若内网 registry 不可用，可从已验证的源节点通过 `docker save` / `docker
load` 复制镜像后重新校验。镜像、节点内存、vCPU 数、逻辑进程数量和 placement 必须一并记录。

### 解释指标

- `epoch_completion_seconds`：coordinator 启动至指定有界 PipeComm 轮次完成的墙钟时间。
- `communication_epoch_wall_seconds`：从首个 InterChiplet 命令到该轮次完成的时间；它包含
  本地等待和跨机同步，不能单独视为网络延迟。
- `pipe-metric.cross_node=true`：该条 PipeComm 操作跨越物理 Swarm 节点；应与读操作的
  `synchronization_wait_ns` 一起分析。
- `prewarm_elapsed_seconds`：transport/BaseIf 就绪的预热成本，应与轮次执行时间分开报告。

短有界轮次可能由启动和跨机协调开销主导，未必随着节点数增加而加速。只有完整输入、固定资源、
一致镜像、多次重复且应用自然结束的实验，才能据此报告总体加速比。
