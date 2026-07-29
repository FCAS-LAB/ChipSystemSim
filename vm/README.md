# 仿真机与跨节点代码快照

此目录保存原生 LEGOSim + SimBricks Docker Swarm 实验所用的 VM 相关代码副本，
以便将项目作为独立仓库归档。运行时的权威源文件仍位于仓库根目录的 `real/` 和
`scripts/`；修改实现后，应同步更新本目录中的对应副本。

## `node-local/`：节点内运行时

- `entrypoint.sh`：容器入口；启动 coordinator 或 transport 服务。
- `worker.py`、`process_proxy.py`：启动并监管每个上游 phase-1 模拟器进程。
- `simbricks_worker_supervisor.py`：按确定顺序启动本地 router 与 SimBricks
  BaseIf 连接，并处理 Swarm DNS/监听端口的启动竞争。
- `fifo_broker.py`、`fifo_client.py`：本地 PipeComm/FIFO 适配组件。
- `simbricks_pipe_gateway.cc`、`simbricks_pipe_protocol.h`：C++ BaseIf 网关及其
  帧协议；负责相邻 Swarm 节点间的数据承载。

## `inter-node/`：跨节点编排和仿真机管理

- `simbricks_pipe_router.py`：每个 worker slot 的本地路由器。节点内 pipe
  在内存队列完成；跨 slot pipe 经 C++ BaseIf 网关转发，并输出 `pipe-metric`。
- `generate_*`：从工作负载坐标、节点数和放置策略生成 placement、topology、
  routing 与 Swarm stack。
- `run_remote_functional_matrix.py`：通过 SSH 在 Swarm manager 执行有界功能矩阵。
- `registry_vmnet_proxy.py`：让 VMware VMnet 网络内的节点访问宿主机 Docker registry。
- `provision_vmware_*.ps1`、`rescue_vmware_fresh_node_network.ps1`：创建 Ubuntu
  基础 VM、复制并加入 Swarm 节点，以及网络故障恢复。

这些脚本涉及 VMware Workstation、VMnet 私网、Docker registry 和 SSH 密码文件；
真实镜像、虚拟磁盘、seed ISO 与凭据均不应提交到 Git。
