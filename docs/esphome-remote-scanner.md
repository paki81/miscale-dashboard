# Remote scanner with an ESP32 (ESPHome)

Normally the server listens to the Xiaomi Mi Body Composition Scale 2 (XMTZC05HM) over
Bluetooth itself, so it has to sit within BLE range of the scale. To keep the server elsewhere
(a NAS, a VM, a Proxmox LXC), put an ESP32 next to the scale and let it forward the scale's
advertisements over Wi-Fi to the server.

```text
Scale --BLE--> ESP32 (ESPHome) --HTTP POST--> server (POST /api/ingest)
```

The server runs the forwarded packets through the same pipeline as the built-in scanner
(parsing, MAC filter, deduplication, sessions, profiles, WebSocket), so nothing else changes.

## Server setup

Run the server without its own scanner and protect the endpoint with a token:

| Variable | Value |
| --- | --- |
| `MISCALE_SCANNER` | `off` |
| `MISCALE_INGEST_TOKEN` | a long random string |

When `MISCALE_INGEST_TOKEN` is set, requests to `/api/ingest` must carry it in the
`X-Ingest-Token` header. The dashboard itself has no login: this token protects only ingest,
not the rest of the API. Keep the server on a trusted network, or use an authenticated reverse
proxy and HTTPS before exposing it. Never expose `/api/ingest` to the internet without a token.

In your own Docker configuration you can drop the `/run/dbus` mount and the
`apparmor=unconfined` option, since nothing needs the host's BlueZ when the scanner is off.
The default `docker-compose.yml` is unchanged.

## The endpoint

`POST /api/ingest`

```json
{
  "data": "<26 hex characters = the 13 bytes of 0x181B service data>",
  "mac": "AA:BB:CC:DD:EE:FF",
  "rssi": -60
}
```

`mac` and `rssi` are optional (defaults: `"remote"` and `null`). If `MISCALE_SCALE_MAC` is
configured, send the scale's actual MAC: the usual MAC filter applies to remote packets too.

Responses: `200 {"ok": true}`, `401` for a missing or wrong token, `422` for malformed data.
A `200` acknowledges a valid request, not necessarily a saved measurement: the listener may
filter a MAC or skip a duplicate, and the session waits for stabilized weight.

## ESPHome configuration

Find your scale's MAC address first (a BLE scanner app will show it as `MIBFS`). Add these
sections to your existing ESP32 ESPHome configuration, replace both MAC addresses and the
server address, and define `miscale_token` in your ESPHome `secrets.yaml` to match
`MISCALE_INGEST_TOKEN` on the server.

```yaml
http_request:
  timeout: 2s
  verify_ssl: false

globals:
  - id: last_hex
    type: std::string
    restore_value: no
    initial_value: '""'

esp32_ble_tracker:
  on_ble_service_data_advertise:
    - service_uuid: 181B
      mac_address: AA:BB:CC:DD:EE:FF      # your scale's MAC
      then:
        - if:
            condition:
              lambda: |-
                if (x.size() != 13) return false;
                char hex[27] = {0};
                for (size_t i = 0; i < 13; i++) sprintf(hex + i * 2, "%02x", x[i]);
                if (id(last_hex) == hex) return false;   // same packet, skip
                id(last_hex) = hex;
                return true;
            then:
              - http_request.post:
                  url: http://<server-ip>:8800/api/ingest
                  request_headers:
                    Content-Type: application/json
                    X-Ingest-Token: !secret miscale_token
                  json:
                    data: !lambda 'return id(last_hex);'
                    mac: "AA:BB:CC:DD:EE:FF"
```

The example uses HTTP on a trusted local network. If using HTTPS, enable certificate
verification instead of `verify_ssl: false`.

If the ESP32 already runs `bluetooth_proxy`, keep your existing `esp32_ble_tracker` settings
and just add the `on_ble_service_data_advertise` trigger. Merge the other sections as needed;
do not duplicate top-level YAML keys.

## Tips

- Place the ESP32 within a few metres of the scale; walls reduce BLE range a lot.
- `/api/debug/packets` shows received packets and their `rssi` when provided. This minimal
  example does not send RSSI, so it appears as `null`.
- Give the server a fixed IP (or hostname) so the ESP32 can always reach it.
- Packets are not queued or retried: a weigh-in made while the server is unreachable is lost.
