# Examples

`bfs-4nodes-config.json` defines a reduced-input BFS trace and its placement:
one CPU, two GPU workers, and one ns-3 timing service across four Docker nodes.

`bfs-4nodes-compose.json` is the matching ready-to-run Docker Compose file.
It embeds the configuration in `SIM_CONFIG`, so no host bind mount is needed.

Run it from the repository root after building the image:

```bash
docker build -t simbricks-legosim:latest -f docker/Dockerfile .
docker compose -f examples/bfs-4nodes-compose.json up --abort-on-container-exit
```
