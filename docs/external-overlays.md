# 项目外依赖的可复用改动

ChipSystemSim 不复制上游 LEGOSIM_MICRO 或 `single_stage_simulator`。原生多机集成曾对
这两个相邻仓库做过三项实质源码修改；其余工作区差异为构建产物或 Windows/WSL 的文件模式
变化，不能作为源代码补丁使用。

| 上游目录 | 文件 | 作用 |
| --- | --- | --- |
| `../LEGOSIM_MICRO` | `interchiplet/srcs/interchiplet.cpp` | 子进程退出触发 `EINTR` 时重试 `poll`；分别处理 stdout/stderr EOF；增加 bridge 诊断日志。 |
| `../single_stage_simulator` | `benchmark/MoE/dram.cpp` | 兼容返回周期值的 `readSync`/`writeSync` API。 |
| `../single_stage_simulator` | `benchmark/MoE/moe.yml` | 为 DRAM 进程声明固定坐标，以便 placement/routing 生成器识别。 |

在三个仓库同级放置后执行：

```bash
cd ChipSystemSim
bash scripts/apply_external_overlays.sh
```

该脚本先执行 `git apply --check`，因此补丁已应用、上游版本不匹配或存在冲突时都会失败，
不会静默覆盖用户改动。也可以传入两个上游目录：

```bash
bash scripts/apply_external_overlays.sh /path/to/LEGOSIM_MICRO /path/to/single_stage_simulator
```
