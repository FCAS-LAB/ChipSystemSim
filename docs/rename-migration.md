# LegoSimbricks 到 ChipSystemSim 的迁移说明

本仓库已改名为 ChipSystemSim，并统一了以下项目命名空间：

- 本地源目录：`D:\root\work2026\ChipSystemSim`；
- Docker 镜像仓库名：`chipsystemsim`；
- Swarm node label：`chipsystemsim.node.N=true`；
- Swarm stack 和 overlay network 前缀：`chipsystemsim`；
- Docker 内项目 overlay 目录：`/opt/chipsystemsim-distributed`；
- VMware 资产根目录默认值：`E:\ChipSystemSimVMs`；
- Git remote：`LeenSed/ChipSystemSim.git` 与 `FCAS-LAB/ChipSystemSim.git`。

## 迁移现有 VM 集合

现有 VMware 磁盘不会自动移动。关闭所有使用旧资产根目录的 VM 后执行：

```powershell
.\scripts\migrate_vmware_root.ps1
```

随后重新打开 VMX 文件并重新设置 Swarm 标签：

```bash
docker node update --label-rm legosim.node.0 NODE_0
docker node update --label-add chipsystemsim.node.0=true NODE_0
```

对每个节点序号重复上述操作。`legosim` 来宾用户名、`/opt/legosim` 上游安装路径和
`LEGOSIM_*` 环境变量是 LEGOSim 的兼容接口，刻意保留，不能机械改名。

## 重建镜像

旧的 `legosim-real:*` 镜像 tag 不会自动变更。按 README 中的新 tag 重新构建并推送镜像，
然后在每个 Swarm 节点确认相同 image ID 或 digest。这样不会把旧镜像误当作新项目镜像。
