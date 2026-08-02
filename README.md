# ChipSystemSim：基于 SimBricks BaseIf 的 LEGOSim 分布式运行时

ChipSystemSim 将 LEGOSim phase-1 进程间的 `PipeComm` 通信扩展到 Docker
Swarm 的不同节点：同节点 pipe 由本地 router 排队，跨节点 pipe 经过
`simbricks-pipe-gateway`、SimBricks `BaseIf` 与 `dist/sockets/net_sockets` 送达
目标节点。它用于验证真实 LEGOSim 进程图在受控多节点环境中的启动、通信、放置和同步。

## 重要边界

本项目**不是**把 LEGOSim CPU/GPU 映射为 SimBricks 的 PCIe、NIC、交换机或 ns-3
设备模型。SimBricks 在这里是跨节点 `PipeComm` 的 BaseIf 传输层；LEGOSim 的 Sniper、
GPGPU-Sim、MNSIM 与 DSA 仍是原进程图中的模拟器。因而它与 SimBricks+gem5 的完整硬件
接口集成不同，结果应表述为“LEGOSim PipeComm over SimBricks BaseIf”。

`MLP-DP` 是额外提供的确定性数据并行工作负载，用来研究一个共同任务的扩展行为；它不等同于
上游原始 MLP benchmark。其 GPU 梯度算术使用 GPGPU-Sim 兼容 host 路径，仍经 GPU
PipeComm 端点传输梯度。因此可报告端到端通信、同步与计算关键路径，但不能将其解释成 GPU
微体系结构周期性能。

## 当前推荐实验：单机 DinD MLP-DP

推荐在一台 Linux 服务器中以 DinD 创建 1、2、4、8 个独立 Docker daemon，并在内部
Swarm 中运行同一个 MLP-DP 任务。正式 scale-out 配置为：

- 每节点 4 vCPU、16 GiB 内存、1 个 CPU data rank、2 个 GPU worker；
- 全局 batch 固定为 32,768，训练迭代固定为 40；
- 节点数改变 data-parallel 分片数，因此总 CPU/GPU worker 数随节点数增加；
- 多节点使用纯 `netem delay 1ms`，不附加未声明的带宽限速；
- 1、2、4 节点可并行运行；8 节点应在前者结束并关闭其 DinD 后单独运行。

完整构建、运行、清理、成功判据和指标定义见
[docs/DIND_MLP_DP.md](docs/DIND_MLP_DP.md)。

## 已实现的运行时改动

1. `real/simbricks_pipe_router.py`：本地队列和跨 worker BaseIf 转发，并为每次
   PipeComm 记录机器可读指标。
2. `real/simbricks_pipe_gateway.cc`：将 router 请求转换为 SimBricks BaseIf 通道；
   `net_sockets` 负责跨 Swarm 节点 TCP 连接。
3. `real/simbricks_worker_supervisor.py` 与 `real/process_proxy.py`：处理 Swarm DNS、
   listener/connector 启动顺序、BaseIf 就绪重试，以及 coordinator 到远端 phase-1
   进程的标准输入/输出转发。
4. `scripts/generate_mlp_dp_matrix.py`：以固定全局 batch 生成 1/2/4/8 节点配置，并把
   同一 rank 的 CPU 与两个 GPU worker 放在同一逻辑节点。
5. `scripts/run_dind_mlp_dp.sh`：只在 transport 全部就绪后启动 coordinator，收集自然
   完成证据、通信指标，以及跨节点读等待的墙钟并集时间。

## 目录与入口

- `real/`：LEGOSim/SimBricks 分布式运行时、router、gateway、配置生成器和 MLP-DP 源码。
- `docker/`：镜像入口说明见 [docker/README.md](docker/README.md)；新的 MLP-DP
  实验使用 `Dockerfile.real-mlp-dp`。
- `scripts/`：DinD provisioning、运行器与结果汇总脚本。
- `docs/`：方法、指标与限制说明。
- `vm/`：原先 VMware 多机部署的节点内/节点间代码快照。
- `platform/`：早期 Python/JSON-TCP 合成适配器，仅作历史对照，不能作为原生结果。

## 构建前置条件

- Linux x86-64、Docker Engine 27+、当前用户可访问 Docker socket。
- 一个兼容基础镜像：其中必须已包含 LEGOSim、Sniper、GPGPU-Sim、已构建的 SimBricks
  socket transport 和 `simbricks-pipe-gateway`。已验证环境使用
  `chipsystemsim:native-mlp-simbricks-v30`。
- 从源码构建基础层时，按各自许可证准备 LEGOSim、SimBricks 与 benchmark 源码；本仓库不
  重新发布这些上游项目。SimBricks 使用固定 revision 并应用
  `patches/simbricks/0001-legosim-low-rate-baseif.patch`。

构建当前 MLP-DP 运行镜像：

```bash
docker build -t chipsystemsim:mlp-dp-current \
  --build-arg BASE_IMAGE=chipsystemsim:native-mlp-simbricks-v30 \
  -f docker/Dockerfile.real-mlp-dp .
```

不要提交 Docker image tar、DinD 数据目录、VM 磁盘、密码、私钥、registry token 或完整
原始服务日志。`results/` 默认应只保存经审查的 CSV、配置和必要的最小证据。

## 历史路径

原始八工作负载的有界功能验证、VMware 脚本及旧 Dockerfile 仍保留，便于复现过去的
兼容性实验；它们不是当前 MLP-DP 性能实验的默认入口。尤其不要将有界功能窗口的观测时间
当作自然完成 benchmark 的总运行时间。
