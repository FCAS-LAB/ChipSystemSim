# Matmul-DP 跨 LEGOSim 时序同步反事实结果（2026-08-09）

本记录给出固定 32 个 GPU block rank 的 Matmul-DP/Block-GEMM 实验中，跨 LEGOSim
通信对应用**模拟关键路径**的净影响。它区别于 PipeComm 的主机 wall-clock 阻塞指标。

## 定义

对每一个节点数，保持相同的 CPU/GPU 进程、32-rank 数据切分、静态放置、PipeComm 功能数据
传输、DinD 资源和 ns-3 拓扑。实际组使用正常 ns-3 `delayInfo.txt`；反事实组仅把源、目的
芯粒位于不同 LEGOSim worker 的事务写为零 ns-3 delay。最终由 InterChiplet 在自然结束后输出：

```text
Benchmark elapses <cycles> cycle.
```

因此报告的关键路径同步开销为：

\[
\Delta T_{\mathrm{cross}} = T_{\mathrm{actual}} - T_{\mathrm{local\ baseline}},
\qquad
O_{\mathrm{cross}} = \Delta T_{\mathrm{cross}} / T_{\mathrm{actual}}.
\]

这不包含发送端尚未完成计算造成的端到端 `READ` 等待，也不把 Docker/PipeComm 的 wall-clock
堵塞换算为 cycle。

## 配置

完整机器可读配置见
[`data/ns3_matmul_dp_fixed32_counterfactual_20260809.csv`](data/ns3_matmul_dp_fixed32_counterfactual_20260809.csv)。

| 项目 | 值 |
| --- | --- |
| 工作负载 | Matmul-DP / block GEMM，480×64×64，全局固定 32 GPU rank |
| Phase 1 | 1 个 Sniper CPU controller + 32 个 GPGPU-Sim block worker |
| 放置 | 连续 rank 均分到 1、2、4、8 个逻辑 LEGOSim worker |
| 每逻辑 worker | 8 vCPU、16 GiB |
| ns-3 拓扑 | 6×6 Mesh，36 个网络节点，IPv4 多跳 point-to-point 路由 |
| 时序参数 | 1 ns/cycle、32 B flit、128 Gbps、1 ns/跳、1400 B UDP 分段、100,000 packet DropTail 队列 |
| 功能运行网络 | 单机 DinD；跨 worker 外层 `tc netem delay 1ms`，不限速 |
| 闭环 | 两轮 `bench.txt → ns-3 → delayInfo.txt → 下一轮 SYNC`，每轮 160 条 trace |

Matmul-DP 本轮没有特殊 `BARRIER`、`LOCK` 或 `UNLOCK` trace。基线中每轮被本地化的跨 worker
普通事务数为 0、80、120、140；其余事务仍采用正常 ns-3 时序。

## 结果

完整原始 cycle 与汇总值见
[`data/matmul_dp_fixed32_counterfactual_20260809.csv`](data/matmul_dp_fixed32_counterfactual_20260809.csv)。

| 节点数 | 实际完成 cycle（3 次均值±标准差） | 本地化基线 cycle（1 次） | 关键路径增量 cycle | 同步开销占比 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 3,217,656 ± 1,116 | 3,217,350 | 0* | 0% |
| 2 | 3,217,592 ± 720 | 3,182,318 | 35,274 | 1.096% |
| 4 | 3,217,318 ± 487 | 3,166,470 | 50,848 | 1.580% |
| 8 | 3,217,635 ± 478 | 3,157,491 | 60,144 | 1.869% |

\* 1 节点没有跨 worker 事务。实际/基线的原始差为 306 cycles，小于实际组三次标准差
1,116 cycles，按定义报告为零而不是同步开销。

2、4、8 节点的差值显著大于实际组三次重复的波动，说明当前 6×6 Mesh 参数下，跨 LEGOSim
ns-3 时序会使该应用关键路径分别增加约 1.10%、1.58% 和 1.87%。反事实基线目前每点只运行
一次；若用于统计显著性检验，应为基线再增加独立重复并报告差值的置信区间。

## 复现

先构建包含反事实模式的 Matmul-DP 覆盖镜像。它只重编译 coordinator-local 的 ns-3 adapter，
不会替换 CPU/GPU benchmark 或 PipeComm 传输实现：

```bash
docker build -t chipsystemsim:native-matmul-dp-counterfactual-v1 \
  -f docker/Dockerfile.real-native-matmul-dp-counterfactual .

RUN=/mnt/large-disk/matmul-dp-fixed32-counterfactual-$(date +%Y%m%d)
scripts/run_matmul_dp_counterfactual_baseline.sh --output-root "$RUN"
```

该脚本会创建 8 个 DinD worker、以每 worker 8 vCPU/16 GiB 依次运行 1/2/4/8 节点基线，并
自动清理 DinD 容器。实际组需要使用相同的 `--gpu-ranks 32`、资源和 ns-3 参数生成，但不传
`--ns3-localize-cross-worker-network`；将实际组三次结果与基线的
`benchmark_completion_cycles` 代入上述公式即可。

通过 `timing_feedback.txt`、`matmul_dp_validation.txt`、`coordinator.log` 的自然结束标记以及
每轮 `phase2_summary.json` 中的 `counterfactual_localized_cross_worker_records` 共同验证结果。
