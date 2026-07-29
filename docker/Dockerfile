FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libns3-dev pkg-config python3 python3-pip \
    ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY platform/ /app/platform/
# Ubuntu 22.04 packages ns-3 3.35 under a versioned include/library path.
RUN g++ -O2 -std=c++17 -I/usr/include/ns3.35 /app/platform/ns3_delay.cc \
    -o /usr/local/bin/ns3-delay \
    -lns3.35-core -lns3.35-network -lns3.35-point-to-point

ENTRYPOINT ["python3", "/app/platform/node.py"]
