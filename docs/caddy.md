# Caddy and HAProxy in front of berth

This is how I run a public berth leader. The goal is to put the public site
and the cluster endpoint behind the same `:443`, on one VPS, without losing
the agent's mTLS client certificate on the way in.

The trick is that HAProxy owns `:443` and routes by SNI at the TCP layer.
It never terminates TLS. Caddy sits behind it on loopback and terminates
public TLS only for the public site. The cluster traffic goes straight
through to berth so the daemon still sees the agent cert.

`scripts/setup-leader-vps.sh <base-domain>` writes all of the config below
for you. If you'd rather wire it up by hand, the pieces are here. See
[../README.md](../README.md) for the project itself and
[deploy.md](deploy.md) for the broader deployment story.

## How traffic flows

Two names point at the same box, both on `:443`:

- `leader.example.com:443` — HAProxy passes the TCP stream to Caddy on
  `127.0.0.1:8443`. Caddy terminates public TLS and reverse-proxies to
  berth's plain-HTTP public listener on `127.0.0.1:11500`.
- `cluster.example.com:443` — HAProxy passes the stream straight to berth's
  cluster listener on `127.0.0.1:11501`. No TLS termination in the middle,
  so berth still gets the agent's mTLS client certificate.

Anything that arrives with neither SNI is dropped at HAProxy.

## Caddyfile

This is `/etc/caddy/Caddyfile`. Replace `leader.example.com` and
`cluster.example.com` with your own names; `8443`, `11500` and `11501` are
the loopback ports.

```caddyfile
{
    auto_https disable_redirects
}

http://leader.example.com {
    redir https://leader.example.com{uri} permanent
}

http://cluster.example.com {
    respond 404
}

https://leader.example.com:8443 {
    bind 127.0.0.1
    header {
        # Caddy is behind HAProxy on :443; do not advertise loopback
        # :8443 as an external HTTP/3 endpoint.
        -Alt-Svc
        Strict-Transport-Security "max-age=31536000"
    }
    reverse_proxy 127.0.0.1:11500 {
        header_up X-Forwarded-Proto https
    }
}

# Catch-all on the same loopback listener: any request whose Host header does
# not match the public site above gets 421 Misdirected Request instead of
# Caddy's empty 200. tls internal uses Caddy's local CA, which is fine here —
# this site only sees connections that HAProxy already routed via the leader
# SNI but with a wrong Host header.
https://:8443 {
    bind 127.0.0.1
    tls internal
    respond 421
}
```

A few things worth knowing:

- `auto_https disable_redirects` keeps Caddy from standing up its own `:443`
  redirector. HAProxy owns `:443`, not Caddy.
- The `http://cluster.example.com` site answers `404`. Plain HTTP to the
  cluster name shouldn't reach berth's mTLS listener, so Caddy just turns it
  away.
- `-Alt-Svc` strips the header that would otherwise tell clients to come back
  on `:8443` over HTTP/3. That port is loopback-only; nothing external should
  ever see it.

## HAProxy

This is `/etc/haproxy/haproxy.cfg`. It's a pure TCP/SNI router — `mode tcp`
everywhere, no `mode http`.

```haproxy
global
    log /dev/log local0
    log /dev/log local1 notice
    chroot /var/lib/haproxy
    stats socket /run/haproxy/admin.sock mode 660 level admin
    stats timeout 30s
    user haproxy
    group haproxy
    daemon

defaults
    log global
    mode tcp
    option tcplog
    timeout connect 5s
    timeout client  1h
    timeout server  1h

frontend berth_https
    bind *:443
    tcp-request inspect-delay 5s
    tcp-request content accept if { req.ssl_hello_type 1 }
    # Drop connections that don't carry one of our two expected SNI values.
    # No default_backend: anything not matched here has already been rejected.
    tcp-request content reject if !{ req.ssl_sni -i cluster.example.com leader.example.com }
    use_backend berth_cluster if { req.ssl_sni -i cluster.example.com }
    use_backend berth_public if { req.ssl_sni -i leader.example.com }

backend berth_public
    server caddy_public 127.0.0.1:8443 check

backend berth_cluster
    server berth_cluster 127.0.0.1:11501 check
```

The `reject if !{...}` line is the important one. There's no `default_backend`:
a connection has to present `cluster.example.com` or `leader.example.com` in
its SNI, or HAProxy closes it. The long `timeout client`/`timeout server` of
`1h` is for the cluster's long-lived mTLS WebSocket — agents hold that
connection open.

## On the daemon side

berth has to know it's behind a proxy: bind on loopback, speak plain HTTP on
the public listener, and trust the forwarded headers Caddy adds. Put this in
`~/.berth/config.toml`:

```toml
[server]
leader_only = true

[public]
host = "leader.example.com"
port = 11500
bind = "127.0.0.1"
scheme = "http"
trust_proxy_headers = true
forwarded_allow_ips = "127.0.0.1"

[cluster]
host = "cluster.example.com"
port = 11501
bind = "127.0.0.1"
```

What these do:

- `[server] leader_only` runs the leader as control-plane only — no local
  Docker or NVIDIA required, every deploy targets a remote agent. Drop it if
  the leader box also has GPUs.
- `[public] scheme = "http"` plus `trust_proxy_headers` makes berth bind plain
  HTTP and honour the `X-Forwarded-Proto` Caddy sends. `forwarded_allow_ips`
  restricts that trust to the loopback proxy, so a client can't spoof the
  header.
- Both listeners bind `127.0.0.1` because HAProxy is the only thing that
  should reach them.

Then set `BERTH_LEADER_URL=https://cluster.example.com` in berth's systemd
unit. That's the URL berth puts in the enrollment URIs it hands out. Without
it, agents would be told to connect to the loopback cluster port instead of
the external `:443` endpoint. The leader installer and `berth deploy bootstrap
--sni-443` both set this for you.

## Verifying the path

After restarting berth, Caddy, and HAProxy:

```bash
# From the operator's laptop:
curl -i https://leader.example.com/healthz
# → HTTP/2 200, {"ok": true}

# From a would-be agent:
curl -k https://cluster.example.com/admin/ca.pem
# → the CA PEM the daemon serves on the cluster listener
```

The first request proves the public path: HAProxy → Caddy → berth on
`:11500`. The second proves the cluster path goes straight through to berth on
`:11501` — `-k` because you don't have the CA yet, which is exactly what that
endpoint hands you. If either one hangs, check that the SNI name you're
hitting matches one of the two HAProxy `use_backend` rules.
