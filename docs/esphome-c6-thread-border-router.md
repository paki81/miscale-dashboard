# Migrazione `display-posta` su ESP32-C6 + OpenThread Border Router

Config completa: [`display-posta-c6.yaml`](display-posta-c6.yaml) — identica alla versione S3
(OLED, sensori HA, BLE proxy, ingest bilancia, OTA, script) più il border router Thread.

> Il componente `openthread: border_router` è **sperimentale** (ESPHome recente, ESP-IDF ≥ 5.5.5).
> Se la compilazione rifiuta `border_router`, aggiorna l'add-on ESPHome o usa il canale dev.

## Cosa cambia

| Elemento | ESP32-S3 | ESP32-C6 |
|---|---|---|
| `board` | `esp32-s3-devkitc-1` | `esp32-c6-devkitc-1` |
| I2C SDA/SCL | GPIO17 / GPIO18 | **GPIO6 / GPIO7** |
| Display RST | GPIO21 | **GPIO5** |
| `network: enable_ipv6` | — | **obbligatorio** (backbone Wi-Fi ↔ Thread) |
| `openthread` | — | `border_router` + `tlv` |
| Radio 802.15.4 | assente | integrata |

Pin evitati su C6: GPIO8/9 (boot strapping), GPIO12/13 (USB nativo), GPIO16/17 (UART0/console).
Se la scheda non è la devkitc-1, ricontrolla il pinout sulla serigrafia.

## Nuovo secret

In ESPHome → **Secrets**:

```yaml
thread_tlv: "<TLV della rete Thread>"
```

Il TLV si copia da Home Assistant: **Impostazioni → Dispositivi e servizi → Thread** →
rete preferita → copia il dataset/TLV. Così la C6 si aggancia alla rete esistente come
secondo border router. Se non hai ancora una rete Thread, ometti `tlv`/`force_dataset`
e il BR ne crea una nuova (poi la imposti come preferita in HA e ricommissioni i device).

## Cablaggio OLED

| Segnale | Pin C6 |
|---|---|
| SDA | GPIO6 |
| SCL | GPIO7 |
| RST | GPIO5 |
| VCC/GND | 3V3 / GND |

## Flash

Il **primo flash va via USB** (la scheda è nuova, niente OTA). In ESPHome:
Install → "Plug into this computer" / web.esphome.io. Da quel momento OTA funziona
come prima (stesso `name: display-posta` → le entità HA non cambiano).

Durante il passaggio tieni acceso **un solo** dei due device (stesso nome/API key):
quando la C6 è verificata, spegni l'S3.

## Verifica

1. **Display/sensori**: lo schermo fa il boot test e mostra POSTA come prima.
2. **Ingest bilancia**: `curl -s http://192.168.188.163:8800/api/debug/packets` dopo
   una rilevazione → pacchetti presenti e log `200` (non `401`).
3. **Border router**: in HA → Impostazioni → Thread compare il nuovo BR; i device
   Matter-over-Thread della rete preferita lo usano in automatico.

## Coesistenza BLE ↔ Thread (nota)

BLE e 802.15.4 condividono la radio 2.4 GHz. Se l'ingest della bilancia diventa
irregolare, prima leva `active: true` da `bluetooth_proxy` (riduce il duty cycle BLE,
l'advertise listener resta attivo); se non basta, valuta BR su una seconda C6.
