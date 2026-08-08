# 原始 LEGOSim MLP 的通信感知静态放置

此实验使用上游 `artifact/MLP/mlp.yml`，不是 MLP-DP。Phase 1 固定进程图为 4 个
GPGPU-Sim、1 个 Sniper CPU、1 个 DSA 和 1 个 MNSIM，共 7 个计算/功能 simlet。原始
PopNet Phase 2 已替换为 ns-3 轨迹回放器：它读取 Phase 1 产生的 `bench.txt`，写出
`delayInfo.txt`，供下一轮 Phase 1 的 `SYNC` 消费。

## 通信模型

源代码中 Sniper CPU `(5,5)` 将矩阵发送到 4 个 GPU `(0,1)`、`(0,2)`、`(1,1)`、`(1,2)`，
并读取乘法结果。这四组双向矩阵传输是最高权重边。CPU 与 DSA `(2,0)` 的转置请求/响应、
CPU 与 MNSIM `(0,3)` 的推理数据交换是次级边。ns-3 只处理时序轨迹，不传输这些 PipeComm
功能负载。

`real/generate_placement.py --placement-policy communication-aware` 使用固定顺序：

```text
CPU, GPU(0,1), GPU(0,2), GPU(1,1), GPU(1,2), DSA, MNSIM
```

随后按节点数等容量分区。因此：

| 节点 | 每节点进程槽位 | 关键同置结果 |
| ---: | ---: | --- |
| 1 | 7 | 全部本地 |
| 2 | 4 | CPU 与 3 个 GPU 同置 |
| 4 | 2 | CPU 与 1 个 GPU 同置 |
| 8 | 1（其中 1 个 worker 空闲） | 受一进程一节点约束，所有进程间边跨 worker |

该策略最小化高权重 CPU↔GPU 边的跨节点数量；它不保证总时间下降，且不改变原始 MLP 的计算
语义、进程数或通信顺序。

为让 7 个 Phase-1 simlet 在 2/4/8 个 worker 上形成确定的近均衡分箱，placement JSON 保留了
上游 PopNet 的**虚拟第 8 个槽位**。启用 ns-3 后该条目不会被 worker 启动；Phase 2 始终在
coordinator 本地执行。这个虚拟槽位只决定 Phase-1 分箱边界，例如 2 节点时实际 Phase-1 数为
4/3、4 节点时为 2/2/2/1、8 节点时为 1/1/1/1/1/1/1/0。

## 生成配置

```bash
cd ChipSystemSim
docker build --progress=plain \
  -t chipsystemsim:native-mlp-ns3-v4 \
  --build-arg BASE_IMAGE=chipsystemsim:native-mlp-ns3-v3 \
  -f docker/Dockerfile.real-native-mlp-simbricks-gpu-fix .

IMAGE=chipsystemsim:native-mlp-ns3-v4
python3 scripts/generate_native_mlp_dind_matrix.py \
  --output-root results/native-mlp-comm-aware-config \
  --image "$IMAGE" --nodes 1 2 4 8 --stream-output
```

之后创建受限 DinD 节点并执行配置。每个 logical node 固定为 8 vCPU、16 GiB；`dind-netem`
镜像提供 `tc`，以便在外层 DinD 接口上注入 1 ms 单向延迟：

```bash
docker build -t chipsystemsim:dind-netem -f docker/Dockerfile.dind-netem .

scripts/provision_dind_swarm.sh \
  --nodes 4 --prefix chipsystemsim-native-mlp-n4 \
  --image "$IMAGE" --dind-image chipsystemsim:dind-netem \
  --per-node-cpus 8 --per-node-memory 16GiB \
  --dind-data-root /mnt/large-disk/chipsystemsim-dind-n4

scripts/run_dind_mlp_dp.sh \
  --nodes 4 --prefix chipsystemsim-native-mlp-n4 \
  --config-dir results/native-mlp-comm-aware-config/mlp-nodes4 \
  --stack native-mlp-n4 --output-dir results/native-mlp-comm-aware/nodes4
```

`run_dind_mlp_dp.sh` 等待所有 transport 服务就绪后才启动 coordinator，并保留完整日志、
精确起止时间、PipeComm 指标和 Phase-2 产物。一个结果点只有同时存在自然结束标记和
`timing_feedback=present` 时才可用于报告；具体检查命令见
[ns-3 时序闭环说明](NS3_TIMING_CLOSED_LOOP.md)。
