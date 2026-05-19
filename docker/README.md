# Daemon-as-container

This directory contains a Dockerfile to run the berth daemon itself in a container. The daemon spawns engine containers (vLLM, SGLang) on the host's Docker — so the container must have access to the host Docker socket.

## Build

```bash
docker build -f docker/daemon.Dockerfile -t berth:dev .
```

## Run

```bash
docker run -d --name serve \
    --network host \
    -v ~/.serve:/root/.serve \
    -v /var/run/docker.sock:/var/run/docker.sock \
    berth:dev
```

**Why `--network host`?** The daemon binds to `127.0.0.1:<allocated>` ports for the engine containers it spawns. On the host network, the daemon container resolves those addresses transparently. On a bridge network, it would need its own engine network and address-by-name routing.

**Security note:** Mounting `/var/run/docker.sock` grants root-equivalent privileges to anything inside the container. Don't combine this with untrusted code.

## Versioned tags

The pinned engine images in `backends/backends.yaml` are pulled lazily on first use. To pre-pull them:

```bash
docker exec serve berth pull-engine vllm
docker exec serve berth pull-engine sglang
```
