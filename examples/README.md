# Examples

Small, copyable starting points. None of these are required — the CLI and the
admin API can build the same state through `berth pull`, `berth run`, and a
couple of POSTs. They're here so you don't have to write the JSON from scratch.

- `single-node-config.toml` — daemon settings for a local single-host box.
- `behind-caddy-config.toml` — the public listener when it sits behind a
  TLS-terminating reverse proxy (see [../docs/caddy.md](../docs/caddy.md)).
- `service-profile-qwen.json` — a reusable vLLM launch definition.
- `service-route-chat.json` — a public `model="chat"` route pointing at that
  profile.

To apply the profile and its route, point `$BERTH_URL` and `$BERTH_TOKEN` at your
daemon and POST the two files:

```bash
curl -k -X POST "$BERTH_URL/admin/service-profiles" \
  -H "Authorization: Bearer $BERTH_TOKEN" \
  -H "Content-Type: application/json" \
  --data @examples/service-profile-qwen.json

curl -k -X POST "$BERTH_URL/admin/routes" \
  -H "Authorization: Bearer $BERTH_TOKEN" \
  -H "Content-Type: application/json" \
  --data @examples/service-route-chat.json
```

After that, a request for `model="chat"` lands on the `qwen-vllm` profile. There's
more on profiles and routes in the [README](../README.md) and
[docs/multi-node.md](../docs/multi-node.md).
