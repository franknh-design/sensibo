# Sensibo Varme

Privat varmepumpeautomatisering med AI-læring over tid. Styrer Sensibo-tilkoblet varmepumpe basert på romtemperatur, utetemperatur og historiske data.

---

## Arkitektur

```
iPhone / PC
    ↓ HTTPS
varme.haugan.online
    ↓ Cloudflare Tunnel
Hetzner CPX22 (Helsinki)
    ├── server.py        ← Python-backend
    ├── index.html       ← PWA-frontend
    └── Sensibo API / met.no API
```

---

## Funksjoner

### Auto-styring
- Setter 30°C + høy vifte når romtemp faller under ønsket temp − hysteresis
- Setter ønsket temp + mid-høy vifte når temp er nådd
- Går til vifte-modus (stille) når rommet er for varmt
- Slår av pumpa etter justerbar tid i vifte-modus

### Læringsmotor
- Logger alle boost-sesjoner med utetemperatur, delta og tidsbruk
- Viser historikk og statistikk i appen
- Grunnlag for fase 2: progressiv eskalering basert på utetemperatur

### Timeplan
- Planlagte temperaturendringer per klokkeslett og ukedag
- Lagres på serveren — gjelder alle enheter

### PWA
- Fungerer som app på iPhone (Legg til på hjemskjerm)
- Alle innstillinger synkronisert via server — ingen localStorage
- Identisk visning på alle enheter

---

## Filer

```
/opt/sensibo/
├── server.py           # Python-backend (HTTP-server + Sensibo/met.no API)
├── index.html          # PWA-frontend (alle sider)
├── .env                # API-nøkkel (aldri i GitHub)
├── state.json          # Persistent lagring av innstillinger (autogenerert)
├── heat_history.csv    # Læringshistorikk (autogenerert)
└── server.log          # Loggfil (autogenerert)
```

---

## Innstillinger (via app)

| Innstilling | Standard | Beskrivelse |
|---|---|---|
| Ønsket temp | 23°C | Måltemperatur for auto-styring |
| Nedre hysteresis | 1.0°C | Temp må falle X°C under mål før oppvarming starter |
| Øvre hysteresis | 1.0°C | Temp må stige X°C over mål før vifte-modus starter |
| Hvilemodus etter | 2.0 t | Timer i vifte-modus før pumpa slås av |
| Normalvifte | Mid-Høy | Viftehastighet når ønsket temp er nådd |

---

## Auto-logikk

```
romtemp < ønsket - nedre_hysteresis  →  30°C + høy vifte (oppvarming)
romtemp > ønsket + øvre_hysteresis   →  vifte-modus + stille (steg 1)
  etter X timer i vifte-modus        →  pumpa AV (steg 2)
romtemp tilbake i normal sone        →  ønsket temp + mid-høy vifte
```

Viktig: Kjøling er permanent deaktivert. Pumpa kjører alltid i varmemodus.

---

## Sensibo-enheter

| UID | Modell | Rom | Brukes til |
|---|---|---|---|
| YRqRonRH | skyplus | Frank's device | AC-styring |
| FTKRATUUWC | motion_sensor | — | Romtemperatur (romsensor) |
| bCCMwLk4 | skyv2 | kjeller | Ikke i bruk |

---

## Drift på Hetzner

### Tjenester
```bash
systemctl status sensibo       # Python-backend
systemctl status cloudflared   # Cloudflare Tunnel
```

### Restart
```bash
systemctl restart sensibo
systemctl restart cloudflared
```

### Logg
```bash
tail -f /opt/sensibo/server.log
```

### Oppdatere filer
```bash
# Fra PC (PowerShell):
scp C:\sensibo\server.py root@204.168.206.182:/opt/sensibo/
scp C:\sensibo\index.html root@204.168.206.182:/opt/sensibo/

# Restart etter opplasting:
systemctl restart sensibo
```

---

## API-endepunkter

| Metode | Endepunkt | Beskrivelse |
|---|---|---|
| GET | `/api/status` | Full status — temp, AC, innstillinger, historikk |
| POST | `/api/command` | Send kommando til pumpa (manual, boost, power) |
| POST | `/api/settings` | Oppdater innstillinger (target_temp, auto_enabled, hysteresis osv.) |
| POST | `/api/log_session` | Logg en varmeøkt |

---

## Cloudflare Tunnel

- Tunnel navn: `varme`
- Tunnel ID: `fa282ef8-fee3-4abb-a06f-76a2dd7e3bf6`
- URL: `https://varme.haugan.online`
- Credentials: `/root/.cloudflared/fa282ef8-fee3-4abb-a06f-76a2dd7e3bf6.json`
- Config: `/root/.cloudflared/config.yml`

---

## Veikart

- [x] To-tilstands auto-styring (oppvarming / holder)
- [x] Vifte-modus og hvilemodus ved for høy temp
- [x] Persistent lagring av alle innstillinger
- [x] Synkronisering mellom alle enheter via server
- [x] Timeplan med ukedager
- [x] Læringshistorikk
- [x] Autostart via systemd
- [x] HTTPS via Cloudflare Tunnel
- [ ] Timeplan-motor på server (automatisk utførelse)
- [ ] Gradert styring — bruk hele spekteret av temp/vifte
- [ ] Progressiv eskalering basert på utetemperatur (fase 2 læring)
- [ ] Fiks dobbel logging i server.log
