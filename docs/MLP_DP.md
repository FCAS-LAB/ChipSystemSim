# MLP-DP：固定 4 CPU + 8 GPU 的同步数据并行实验

`MLP-DP` 是独立于上游 7 进程 MLP 图的可验证数据并行工作负载。它用于回答一个明确问题：在保持同一全局计算任务、同一全局 batch、同一组逻辑资源时，将固定 rank 分布到 1、2 或 4 台 Swarm VM 是否改变训练结果或执行时间。

## 固定资源与放置

每个实验点始终有 4 个 CPU rank 和 8 个 GPU worker：

```text
rank 0: CPU (0,0) + GPU (0,1), (0,2)
rank 1: CPU (1,0) + GPU (1,1), (1,2)
rank 2: CPU (2,0) + GPU (2,1), (2,2)
rank 3: CPU (3,0) + GPU (3,1), (3,2)
```

- 1 节点：四个 rank 都位于 slot 0。
- 2 节点：rank 0、2 位于 slot 0；rank 1、3 位于 slot 1。
- 4 节点：每个 rank 位于一个独立 slot。

节点数不会改变 rank 数、GPU 数、样本数或 iteration 数；它只改变 PipeComm 是本地队列还是通过 SimBricks BaseIf 跨 VM 传输。

## 数学与同步语义

该 MLP 为 `4 -> 6(ReLU) -> 3(softmax)`，全局 batch 固定为 128 个确定性样本，训练固定为 100 次同步 SGD 更新。

1. 每个 GPU worker 固定处理 16 个样本，共 8 × 16 = 128。
2. 每个 CPU rank 汇总其两个 GPU 的局部梯度。
3. rank 0 依次接收 rank 1、2、3 的梯度，严格按该顺序求和。
4. rank 0 用全局 batch 大小 128 归一化并更新 51 个参数。
5. rank 0 将新模型广播给其余三个 rank。

固定数据生成、参数初始化和规约顺序意味着 1/2/4 节点应产生相同的参数向量；运行器以 `abs <= 1e-6` 或 `rel <= 1e-5` 验证相对单进程参考和相对同一重复的一节点基线的逐元素误差。

## 构建与执行

从工作区根目录构建并推送镜像：

```bash
cd /root/work2026
docker build --progress=plain \
  -t REGISTRY/chipsystemsim:mlp-dp-v1 \
  -f ChipSystemSim/docker/Dockerfile.real-mlp-dp .
docker push REGISTRY/chipsystemsim:mlp-dp-v1
```

生成 1/2/4 节点配置：

```bash
cd /root/work2026/ChipSystemSim
python3 scripts/generate_mlp_dp_matrix.py \
  --output-root results/mlp-dp-config \
  --image REGISTRY/chipsystemsim:mlp-dp-v1 \
  --nodes 1 2 4
```

在已标记 `chipsystemsim.node.0` 至 `chipsystemsim.node.3` 的 Swarm 中运行三次：

```bash
python3 scripts/run_mlp_dp_matrix.py \
  --manager MANAGER_IP \
  --password-file /secure/legosim-guest-password.txt \
  --source-root results/mlp-dp-config \
  --output-root results/mlp-dp-runs \
  --image REGISTRY/chipsystemsim:mlp-dp-v1 \
  --nodes 1 2 4 --repetitions 3 \
  --absolute-tolerance 1e-6 --relative-tolerance 1e-5
```

成功条件是每个 run 的四个 `MLP_DP_RESULT` 参数向量相同，并且 2/4 节点结果同时满足相对一节点基线和独立 Python 参考实现的误差阈值。运行器总是清理对应 Swarm stack，但会保留 stack YAML、服务日志和 `result.json`。
