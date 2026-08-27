# Sentinel-3 SLSTR FRP Hotspot Watch v3

Diese Version filtert bereits **serverseitig im Copernicus Data Space OData-Katalog** auf das Überwachungspolygon.

Dadurch werden nicht mehr bis zu 1000 globale `SL_2_FRP___`-Produkte geladen und erst lokal aussortiert.

## Kerneigenschaften

- `Collection = SENTINEL-3`
- `productType = SL_2_FRP___`
- räumlicher OData-Filter `OData.CSC.Intersects(...)`
- 14-Tage-Suchfenster
- NRT (`MAR_O_NR`) wird gegenüber NTC (`O_NT`) bevorzugt, wenn beide dieselbe Aufnahme repräsentieren
- nur räumlich relevante Produkte werden heruntergeladen
- echte FRP-Pixel werden aus NetCDF gelesen
- Pixel werden nochmals exakt gegen das Polygon geprüft
- Deduplizierung über `s3_seen.json`
- Cursor über `s3_cursor.json`
- keine Interpolation oder Schätzung

## GitHub Secrets

Unter:

`Settings → Secrets and variables → Actions`

müssen vorhanden sein:

- `CDSE_USERNAME`
- `CDSE_PASSWORD`

## Workflow

`.github/workflows/sentinel3-frp-watch-v3.yml`

läuft zweimal pro Stunde (`:13` und `:43`) und kann manuell gestartet werden.

## Ausgaben

- `data/s3_status.json`
- `data/s3_events.jsonl`
- `data/s3_seen.json`
- `data/s3_cursor.json`

Beim ersten v3-Lauf wird nur das neueste relevante Produkt verarbeitet, um keinen historischen Alarmsturm zu erzeugen. Danach werden alle neuen räumlich relevanten Produkte chronologisch verarbeitet.
