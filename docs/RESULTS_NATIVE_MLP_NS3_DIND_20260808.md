# 原始 MLP + ns-3 时序闭环：DinD 逻辑节点结果

本记录对应原始 LEGOSim MLP 的自然结束运行，目的是验证两条路径都实际工作：

1. 功能数据面：PipeComm 在不同 LEGOSim worker 间经 SimBricks BaseIf/socket transport 路由；
2. 时序闭环：每轮 Phase 1 的 `bench.txt` 经 ns-3 处理后写为 `delayInfo.txt`，并被下一轮
   Phase 1 的 `SYNC` 实际加载。

它是**单台物理服务器上的 Docker DinD/Swarm 实验**，不是物理多机测量。因而下表的
“跨 LEGOSim”意为跨 logical worker；`cross_physical_host_*` 为零是正确结果，不能被改写为
物理跨机通信数据。

## 固定配置

| 项目 | 值 |
| --- | --- |
| Phase 1 工作负载 | 上游 `artifact/MLP/mlp.yml`，自然结束（两轮） |
| Phase 1 组件 | 1 Sniper CPU、4 GPGPU-Sim、1 DSA、1 MNSIM |
| Phase 2 | `real/ns3_phase2.cc`，`bench.txt → delayInfo.txt` |
| 放置策略 | `communication-aware` |
| 每个 DinD worker | 8 vCPU、16 GiB |
| 逻辑节点数 | 1、2、4、8 |
| 外层网络 | 每个 DinD 外部接口 `tc netem delay 1ms`，不限速 |
| ns-3 cycle | 1 ns |
| ns-3 链路 | 128 Gbps、每跳 1 ns、32 B flit、100,000 packet 队列 |
| 物理放置 | 所有 worker 的 `worker_physical_slots` 均为 `0` |

8 节点点保留了 8 个受限 worker；原始 MLP Phase 1 只有 7 个 simlet，故第 8 个 worker 没有
分配 simlet。这不影响“8 个已创建并受资源限制的 Swarm 节点”的部署口径。

## 完成与闭环判据

所有四个点均满足：`exit=0`、协调器日志含 `**** End of Simulation ****`、
`timing_feedback=present`，且 Phase 2 的 `bench.txt`、`delayInfo.txt`、metrics 均为 104 条，
下一轮 Phase 1 实际加载 104 条延迟记录。

| logical workers | coordinator 墙钟时间 (s) | 最终仿真 cycle | ns-3 普通记录 | ns-3 网络延迟总 cycle | Phase-2 回写/下一轮加载 |
| ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 391.709 | 87,332,480 | 104 | 56,484 | 104 / 104 |
| 2 | 398.707 | 87,302,883 | 104 | 56,484 | 104 / 104 |
| 4 | 427.810 | 87,351,502 | 104 | 56,484 | 104 / 104 |
| 8 | 524.943 | 87,419,756 | 104 | 56,484 | 104 / 104 |

`ns3_normal_destination_sync_block_cycles` 分别为 557,438,495、556,478,811、
557,194,259、556,564,704。它是 ns-3 反馈给 LEGOSim 时序协议的**模拟 cycle**，不能与下表的
主机墙钟秒数相加或相除。

## 功能 PipeComm 部署指标

`*_sync_wall_union_seconds` 是所有读阻塞区间在主机墙钟上的并集；它仅描述功能数据面的部署
行为。尤其在本工作负载的锁步 PipeComm 协议下，该并集可接近 coordinator 生命周期，不能视作
ns-3 的网络延迟，也不能用它评价模拟 cycle 的网络性能。

| logical workers | 同 worker 记录 / 字节 | 跨 LEGOSim 记录 / 字节 | 跨 LEGOSim 阻塞并集 (s) | 并集 / coordinator 时间 | 跨物理主机记录 / 字节 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 416 / 1,436,032 | 0 / 0 | 0.000 | 0.00% | 0 / 0 |
| 2 | 324 / 1,083,168 | 92 / 352,864 | 393.700 | 98.74% | 0 / 0 |
| 4 | 128 / 470,848 | 288 / 965,184 | 416.415 | 97.34% | 0 / 0 |
| 8 | 0 / 0 | 416 / 1,436,032 | 519.086 | 98.88% | 0 / 0 |

## 正确解读

节点增多并没有使此固定的原始 MLP 进程图加速：更多 CPU/GPU/DSA/MNSIM 边被拆到不同 logical
worker，功能 PipeComm 的锁步等待随之增加。本结果证明“跨 worker 功能路由”和“ns-3 时序
回写”能够共同完成，而**不**证明该单机 DinD 部署具有物理多机的扩展性。

若要测量真实多机开销，下一步是把 worker 分别部署到不同物理主机、在 routing 中记录不同的
`worker_physical_slots`，并在保持同样的 `bench.txt → delayInfo.txt` 校验后报告
`cross_physical_host_*`。不要用当前容器边界替代物理网卡、交换机或 SimBricks NIC/NetIf 模型。
