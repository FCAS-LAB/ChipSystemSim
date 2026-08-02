# Dockerfile 入口说明

## 当前维护的入口

| 文件 | 用途 |
| --- | --- |
| `Dockerfile.real-mlp-dp` | 当前 DinD MLP-DP scale-out 实验的运行镜像。以一个已构建 LEGOSim/SimBricks 基础镜像为输入，重新编译 MLP-DP 并复制运行时 Python 组件。 |
| `Dockerfile.real-local-source` | 从本地已授权的 LEGOSim 源码组装离线基础镜像。需要在构建上下文提供 `upstream/LEGOSIM_MICRO/`。 |
| `Dockerfile.real-native-mlp-simbricks` | 原始 LEGOSim MLP 进程图的兼容构建路径；用于原始 MLP 功能研究，不是 MLP-DP 的默认入口。 |
| `Dockerfile.real-eight-functional` | 八个上游 benchmark 的有界功能验证镜像，不产生自然完成性能结论。 |

## 构建当前 MLP-DP 镜像

```bash
docker build -t chipsystemsim:mlp-dp-current \
  --build-arg BASE_IMAGE=chipsystemsim:native-mlp-simbricks-v30 \
  -f docker/Dockerfile.real-mlp-dp .
```

`BASE_IMAGE` 必须包含相同 ABI 的 LEGOSim、GPGPU-Sim、Sniper、SimBricks socket
transport 和 `simbricks-pipe-gateway`。它不是公开通用基础镜像；在另一台机器上应先按
根 README 的上游依赖说明构建或导入一个兼容基础层。

## 历史诊断 Dockerfile

目录中其余已版本化的 Dockerfile 是开发期的 GDB、单 benchmark、MNSIM 或协议诊断配方。
它们保留用于定位旧问题，新的实验脚本与 README 均不依赖它们。不要以带有 `debug`、`gdb`、
`asan`、`bounded` 或 `single_stage` 的 Dockerfile 报告正式 MLP-DP 性能数据。
