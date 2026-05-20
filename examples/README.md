# Examples

These are small copyable starting points. They are not required for normal use;
the CLI can create the same state through `berth pull`, `berth run`, and the
admin API.

- `single-node-config.toml`: local single-host daemon settings.
- `behind-caddy-config.toml`: public listener behind a TLS-terminating reverse proxy.
- `service-profile-qwen.json`: reusable vLLM launch definition.
- `service-route-chat.json`: public `model="chat"` route to that profile.

Apply the profile and route:

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
