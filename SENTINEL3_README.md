# Sentinel-3 SLSTR FRP Hotspot Watch

GitHub-Actions-Überwachung für Copernicus Sentinel-3 SLSTR `SL_2_FRP___`.

## Funktionsweise

1. Öffentliche Copernicus Data Space OData-Suche nach `SENTINEL-3` / `SL_2_FRP___`.
2. GeoFootprint wird gegen das Überwachungspolygon geprüft.
3. Nur passende Produkte werden authentifiziert heruntergeladen.
4. FRP-NetCDF-Dateien werden aus dem Produkt gelesen.
5. Nur tatsächlich vorhandene positive FRP-Pixel innerhalb des Polygons werden übernommen.
6. `s3_seen.json` verhindert Doppelmeldungen.
7. `s3_cursor.json` merkt sich den letzten erfolgreich verarbeiteten Produktzeitpunkt.

## GitHub Secrets

Unter `Settings → Secrets and variables → Actions`:

- `CDSE_USERNAME`
- `CDSE_PASSWORD`

Das sind die Zugangsdaten deines kostenlosen Copernicus Data Space Ecosystem Kontos.

## Dateien

- `s3_watch.py`
- `s3-requirements.txt`
- `.github/workflows/sentinel3-frp-watch.yml`
- `data/s3_status.json`
- `data/s3_events.jsonl`
- `data/s3_seen.json`
- `data/s3_cursor.json`

## Zeitplan

Der Workflow läuft zweimal pro Stunde (`:11` und `:41`). Sentinel-3 ist ein polarumlaufendes System; ein 10-Minuten-Polling wie bei MTG bringt daher wenig.

## Datenprinzip

Keine Schätzungen und keine Interpolation. Wenn die reale Produktstruktur nicht zu den erwarteten FRP-Dateien/Variablen passt, wird dies in `s3_status.json` als Fehler protokolliert.
