# 原始 LEGOSim MLP 的通信感知静态放置

此实验使用上游 `artifact/MLP/mlp.yml`，不是 MLP-DP。固定进程图为 4 个 GPGPU-Sim、
1 个 Sniper CPU、1 个 DSA、1 个 MNSIM 和 1 个 PopNet，共 8 个进程。

## 通信模型

源代码中 Sniper CPU `(5,5)` 将矩阵发送到 4 个 GPU `(0,1)`、`(0,2)`、`(1,1)`、`(1,2)`，
并读取乘法结果。这四组双向矩阵传输是最高权重边。CPU 与 DSA `(2,0)` 的转置请求/响应、
CPU 与 MNSIM `(0,3)` 的推理数据交换是次级边。PopNet 是 phase-2 协调组件。

`real/generate_placement.py --placement-policy communication-aware` 使用固定顺序：

```text
CPU, GPU(0,1), GPU(0,2), GPU(1,1), GPU(1,2), DSA, MNSIM, PopNet
```

随后按节点数等容量分区。因此：

| 节点 | 每节点进程槽位 | 关键同置结果 |
| ---: | ---: | --- |
| 1 | 8 | 全部本地 |
| 2 | 4 | CPU 与 3 个 GPU 同置 |
| 4 | 2 | CPU 与 1 个 GPU 同置 |
| 8 | 1 | 受一进程一节点约束，全部边跨节点 |

该策略最小化高权重 CPU↔GPU 边的跨节点数量；它不保证总时间下降，且不改变原始 MLP 的计算
语义、进程数或通信顺序。

## 生成配置

```bash
cd ChipSystemSim
docker build --progress=plain \
  -t chipsystemsim:native-mlp-simbricks-v20 \
  -f docker/Dockerfile.real-native-mlp-simbricks .

IMAGE=chipsystemsim:native-mlp-simbricks-v20
python3 scripts/generate_native_mlp_dind_matrix.py \
  --output-root results/native-mlp-comm-aware-config \
  --image "$IMAGE" --nodes 1 2 4 8 --stream-output
```

之后按 [`DIND_MLP_DP.md`](DIND_MLP_DP.md) 创建受限 DinD 节点，但将执行命令替换为：

```bash
./scripts/run_dind_legosim.sh \
  --nodes 4 --prefix chipsystemsim-native-mlp-n4 \
  --config-dir results/native-mlp-comm-aware-config/mlp-nodes4 \
  --stack native-mlp-n4 --output-dir results/native-mlp-comm-aware/nodes4
```

`run_dind_legosim.sh` 等待所有 transport 服务就绪后才启动 coordinator，并保留完整日志、
精确起止时间及 PipeComm 指标。原始 MLP 的自然结束耗时可能远长于 MLP-DP；首次运行应先采用
有界通信轮次验证，确认无死锁后再进行完整自然结束测量。
