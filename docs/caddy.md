# Caddy and HAProxy in front of berth

Recommended public deployments use HAProxy on external `:443` as a TCP
SNI router:

- `leader.example.com:443` is passed through to Caddy on
  `127.0.0.1:8443`; Caddy terminates public TLS and reverse-proxies to
  berth's plain-HTTP public listener on `127.0.0.1:11500`.
- `cluster.example.com:443` is passed through directly to berth's
  cluster listener on `127.0.0.1:11501`; HAProxy does not terminate TLS,
  so berth still sees the agent's mTLS client certificate.

## Caddyfile

Recommended 443/SNI example:

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
        # Caddy is behind HAProxy on :443; do not advertise loopback :8443
        # as an external HTTP/3 endpoint.
        -Alt-Svc
        Strict-Transport-Security "max-age=31536000"
    }
    reverse_proxy 127.0.0.1:11500 {
        header_up X-Forwarded-Proto https
    }
}
```

## HAProxy

```haproxy
frontend berth_https
    bind *:443
    tcp-request inspect-delay 5s
    tcp-request content accept if { req.ssl_hello_type 1 }
    use_backend berth_cluster if { req.ssl_sni -i cluster.example.com }
    use_backend berth_public if { req.ssl_sni -i leader.example.com }
    default_backend berth_public

backend berth_public
    server caddy_public 127.0.0.1:8443 check

backend berth_cluster
    server berth_cluster 127.0.0.1:11501 check
```

## On the daemon side

Set the corresponding `~/.berth/config.toml`:

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

Set `BERTH_LEADER_URL=https://cluster.example.com` in the berth
systemd unit so enrollment URIs advertise the external 443 endpoint
instead of the loopback cluster port.

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
