# 单机 DinD：MLP-DP 1/2/4/8 节点实验

本方案在一个 Linux 宿主 Docker Engine 中启动多个特权 DinD 容器；每个容器运行独立
Docker Engine 并加入同一个嵌套 Swarm。它适合验证原生 LEGOSim、PipeComm、SimBricks
BaseIf、Swarm placement 与人为控制的网络开销。

它不是八台物理机：所有虚拟节点共享宿主 CPU、内核、NUMA 和物理 NIC。因此报告必须标注
为“single-host DinD with netem”，不得解释为真实多服务器 NIC 延迟或 V100 性能。

## 固定资源合同

MLP-DP 固定为 8 个 CPU rank 和 16 个逻辑 GPGPU-Sim/PipeComm worker。全局 batch 仍为
128，100 次 SGD 更新，确定性规约顺序不变；每个 worker 处理 8 个样本。

| 节点数 | 每个 DinD 节点 | 总额 |
| --- | --- | --- |
| 1 | 8 CPU、32 GiB | 8 CPU、32 GiB |
| 2 | 4 CPU、16 GiB | 8 CPU、32 GiB |
| 4 | 2 CPU、8 GiB | 8 CPU、32 GiB |
| 8 | 1 CPU、4 GiB | 8 CPU、32 GiB |

## 创建一个点位

先在外层 Docker Engine 构建或导入完整的原生 MLP-DP 镜像；它必须包含
`/opt/legosim`、Sniper、GPGPU-Sim、PopNet 和 MLP-DP 二进制。然后运行：

```bash
./scripts/provision_dind_swarm.sh \
  --nodes 4 \
  --image chipsystemsim:mlp-dp-8ranks-v1 \
  --total-cpus 8 --total-memory 32g \
  --delay 1ms --rate 10gbit
```

脚本为每个点位重新创建节点，向所有内层 daemon 流式加载同一镜像 digest，并对每个
DinD 的 `eth0` 注入 1 ms 单向延迟和 10 Gbit/s 速率。只有跨 DinD 的 overlay 数据包经过
该接口；同一 DinD 内的消息不被 netem 延迟。

若服务器不能访问 Docker Hub，请先从内网镜像仓库导入 DinD 镜像，并显式指定：

```bash
--dind-image REGISTRY/docker:27-dind
```

完成一个点位后清理：

```bash
./scripts/provision_dind_swarm.sh --nodes 4 --cleanup
```

在正式结果中记录 `docker image inspect --format '{{.Id}}' IMAGE`、节点数、总资源、
netem 参数、宿主 CPU governor 与运行顺序。每个结果还必须通过八个 rank 的
`MLP_DP_RESULT` 与独立 Python 参考模型的逐元素误差阈值验证。
