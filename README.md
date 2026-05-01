# 2GM Varme

AI-styrt varmepumpeautomatisering for Sensibo — med læring over tid.

## Hva det gjør

- Henter romtemperatur og AC-status fra Sensibo API
- Henter utetemperatur og varsler fra met.no
- Styrer vifte og temperatur optimalt for raskest mulig oppvarming
- Lærer over tid: jo flere sesjoner, jo bedre prediksjon
- PWA-grensesnitt — lagres som app på iPhone/Android

## Oppsett på Windows Server

### 1. Klon repoet
```
git clone https://github.com/ditt-repo/2gm-varme.git
cd 2gm-varme
```

### 2. Installer Python-pakker
```
pip install -r requirements.txt
```

### 3. Konfigurer .env
```
copy .env.example .env
```
Åpne `.env` i Notepad og lim inn Sensibo API-nøkkelen din.

### 4. Start server
```
python server.py
```

### 5. Åpne i nettleser
```
http://serverens-ip:8765
```

### 6. Legg til som app på iPhone
Safari → Del-knapp → "Legg til på Hjem-skjermen"

## Kjøre automatisk ved oppstart (Windows)

Lag en `.bat`-fil:
```bat
@echo off
cd C:\2gm-varme
python server.py
```
Legg den i Windows Oppgaveplanlegger med trigger "Ved systemstart".

## Filstruktur

```
2gm-varme/
├── index.html          # PWA-frontend
├── server.py           # Python-backend
├── requirements.txt    # Python-pakker
├── .env.example        # Konfigurasjonmal
├── .env                # Din konfigurasjon (IKKE commit!)
├── .gitignore          # Ekskluderer .env og loggfiler
├── heat_history.csv    # Læringsdata (genereres automatisk)
└── server.log          # Loggfil (genereres automatisk)
```

## Læringsmotor

Hver boost/auto-sesjon logger:
- Start- og slutttemperatur
- Utetemperatur
- Tid brukt
- Viftehastighet

Over tid bygges en modell som estimerer nøyaktig oppstartstid og optimale innstillinger basert på delta-temperatur og utetemperatur.

## API-endepunkter

| Metode | Endepunkt | Beskrivelse |
|--------|-----------|-------------|
| GET | `/api/status` | Nåværende status |
| GET | `/api/history` | All læringshistorikk |
| GET | `/api/predict?target=22` | Prediksjon for måltemperatur |
| POST | `/api/command` | Send kommando til pumpa |
| POST | `/api/log_session` | Logg en varmeøkt |
