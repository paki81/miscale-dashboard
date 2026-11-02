# `display-posta` (ESP32-C6) come OpenThread Border Router

Config completa: [`display-posta-c6.yaml`](display-posta-c6.yaml) — la config attuale
(LCD ST7789V, BLE proxy + ingest bilancia, API HA, OTA, LED RGB) più il border
router Thread. L'ESP32-C6 ha la radio 802.15.4 integrata: serve solo il firmware.

> Il componente `openthread: border_router` è **sperimentale** e richiede un ESPHome
> recente (ESP-IDF ≥ 5.5.5). Se la compilazione rifiuta `border_router`, aggiorna
> l'add-on ESPHome o passa al canale dev.

## Cosa aggiungere alla config esistente

Solo due blocchi, tutto il resto resta identico:

```yaml
# IPv6 obbligatorio: il border router usa il Wi-Fi come backbone.
network:
  enable_ipv6: true

# OpenThread Border Router (SPERIMENTALE)
openthread:
  border_router:
  device_type: FTD
  tlv: !secret thread_tlv
  force_dataset: true
```

## Nuovo secret

In ESPHome → **Secrets**:

```yaml
thread_tlv: "<TLV della rete Thread>"
```

Il TLV si copia da Home Assistant: **Impostazioni → Dispositivi e servizi → Thread** →
rete preferita → copia il dataset/TLV. Così la C6 si aggancia alla rete esistente come
secondo border router. Se non hai ancora una rete Thread, ometti `tlv`/`force_dataset`:
il BR ne crea una nuova, poi la imposti come preferita in HA e ricommissioni i device.

## Flash

La scheda è la stessa: **Install → Wirelessly** come sempre (OTA su 192.168.4.150).
Stesso `name`, stessa API key, stesso IP — le entità HA non cambiano.

## Verifica

1. **Display/ingest**: alla prima rilevazione l'LCD mostra BILANCIA come prima e
   `curl -s http://192.168.188.163:8800/api/debug/packets` contiene pacchetti.
2. **Border router**: in HA → Impostazioni → Thread compare il nuovo BR; i device
   Matter-over-Thread della rete preferita lo usano in automatico.

## Coesistenza BLE ↔ Thread (nota)

BLE e 802.15.4 condividono la radio 2.4 GHz sulla C6. Il componente è nuovo:
se l'ingest della bilancia diventa irregolare o il BR cade, prima leva
`active: true` da `bluetooth_proxy` (riduce il duty cycle BLE, l'advertise
listener resta attivo); se non basta, valuta un BR su una seconda C6 (~5 €).
