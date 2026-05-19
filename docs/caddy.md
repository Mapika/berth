# Caddy in front of serve-engine

Caddy terminates TLS for the public OpenAI endpoint and reverse-proxies
to the daemon's plain-HTTP listener on `127.0.0.1:11500`. The cluster
listener (`:11501`, mTLS WS) is *not* fronted by Caddy — it speaks its
own mutual-TLS direct to agents.

## Caddyfile

Minimal working example:

```caddyfile
serve.example.com {
    reverse_proxy 127.0.0.1:11500 {
        # Pass through the canonical client IP. The daemon's rate
        # limiter reads X-Forwarded-For when trust_proxy_headers is on.
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto https
    }

    # Optional: rate limit the public OpenAI endpoint at the edge.
    # Requires the caddy-ratelimit module — comment out if not built in.
    # rate_limit {
    #     zone openai {
    #         key {remote_host}
    #         events 60
    #         window 1m
    #     }
    #     match /v1/*
    # }
}
```

## On the daemon side

Set the corresponding `~/.serve/config.toml`:

```toml
[public]
host = "serve.example.com"
port = 11500
bind = "127.0.0.1"
scheme = "http"
trust_proxy_headers = true
forwarded_allow_ips = "127.0.0.1"
```

`scheme = "http"` tells the daemon to bind plain HTTP (Caddy handles
TLS). `trust_proxy_headers = true` opts into honouring
`X-Forwarded-For` and `X-Forwarded-Proto` from the configured proxy
addresses. `forwarded_allow_ips` should match where Caddy talks to
the daemon from — `127.0.0.1` when they share the box.

## Cluster listener

Caddy does **not** sit in front of `:11501`. The cluster listener
speaks mTLS end-to-end: agents present a client cert minted by the
leader's CA at enrollment time, and the leader validates the client
cert via `ssl_cert_reqs=CERT_OPTIONAL` plus a fingerprint lookup in
the nodes table. Putting Caddy in front would either break mTLS
verification or require pass-through TLS — neither buys us anything.

Open `:11501` directly in your VPS firewall:

```bash
sudo ufw allow 11501/tcp
```

## Verifying the path

After `systemctl restart serve-engine && systemctl restart caddy`:

```bash
# From the operator's laptop:
curl -i https://serve.example.com/healthz
# → HTTP/2 200, {"ok": true}

# From a would-be agent:
curl -k https://serve.example.com:11501/admin/ca.pem
# → the CA PEM the daemon serves on the cluster listener
```
