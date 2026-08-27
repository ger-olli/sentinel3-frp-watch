
import json
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import netCDF4 as nc
import numpy as np
import requests
from shapely.geometry import Point, Polygon

POLYGON_COORDS = [
    (21.30252, 44.83812),
    (21.21291, 44.79014),
    (20.99648, 44.89789),
    (21.10188, 44.96886),
]
POLYGON = Polygon(POLYGON_COORDS)

USERNAME = os.environ.get("CDSE_USERNAME")
PASSWORD = os.environ.get("CDSE_PASSWORD")
if not USERNAME or not PASSWORD:
    print("ERROR: CDSE_USERNAME/CDSE_PASSWORD missing", file=sys.stderr)
    sys.exit(2)

CATALOGUE = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
DOWNLOAD_BASE = "https://download.dataspace.copernicus.eu/odata/v1/Products"

STATE_PATH = Path("data/s3_seen.json")
CURSOR_PATH = Path("data/s3_cursor.json")
STATUS_PATH = Path("data/s3_status.json")
EVENTS_PATH = Path("data/s3_events.jsonl")
TMP_ZIP = Path("data/s3_product.zip")
TMP_DIR = Path("data/s3_product")

session = requests.Session()
session.headers.update({"User-Agent": "sentinel3-frp-watch/3.0"})

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default

def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")

def polygon_wkt():
    pts = POLYGON_COORDS + [POLYGON_COORDS[0]]
    coords = ", ".join(f"{lon} {lat}" for lon, lat in pts)
    return f"POLYGON(({coords}))"

def get_token():
    r = session.post(TOKEN_URL, data={
        "client_id": "cdse-public",
        "username": USERNAME,
        "password": PASSWORD,
        "grant_type": "password",
    }, timeout=60)
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("No access_token returned by CDSE identity service.")
    return token

def query_products(days=14):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    spatial = f"OData.CSC.Intersects(area=geography'SRID=4326;{polygon_wkt()}')"

    filt = (
        "Collection/Name eq 'SENTINEL-3' and "
        "Attributes/OData.CSC.StringAttribute/any(att:"
        "att/Name eq 'productType' and "
        "att/OData.CSC.StringAttribute/Value eq 'SL_2_FRP___') and "
        f"ContentDate/Start ge {start.isoformat().replace('+00:00','Z')} and "
        + spatial
    )

    params = {
        "$filter": filt,
        "$orderby": "ContentDate/Start asc",
        "$top": "200",
        "$select": "Id,Name,ContentDate,GeoFootprint,S3Path",
    }

    r = session.get(CATALOGUE, params=params, timeout=90)
    r.raise_for_status()
    return r.json().get("value", []), r.url

def prefer_nrt(products):
    # Same acquisition can exist as both NRT (MAR_O_NR) and NTC (O_NT).
    # Prefer NRT when both exist, while keeping a fallback if no NRT version exists.
    grouped = {}
    for p in products:
        name = p.get("Name", "")
        start = (p.get("ContentDate") or {}).get("Start")
        if not start:
            continue

        # Acquisition identity based on platform + first sensing timestamp embedded in the filename.
        parts = name.split("_")
        platform = name[:3]
        sensing = ""
        for part in parts:
            if len(part) >= 15 and part[:8].isdigit() and "T" in part:
                sensing = part[:15]
                break
        key = (platform, sensing or start)

        score = 2 if "MAR_O_NR" in name else (1 if "_O_NT_" in name else 0)
        prev = grouped.get(key)
        if prev is None or score > prev[0]:
            grouped[key] = (score, p)

    selected = [v[1] for v in grouped.values()]
    selected.sort(key=lambda p: (p.get("ContentDate") or {}).get("Start", ""))
    return selected

def download_product(product_id, token):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{DOWNLOAD_BASE}({product_id})/$value"
    with session.get(url, headers=headers, stream=True, timeout=300, allow_redirects=True) as r:
        r.raise_for_status()
        with TMP_ZIP.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

def unzip_product():
    cleanup_dir_only()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(TMP_ZIP, "r") as z:
        z.extractall(TMP_DIR)

def cleanup_dir_only():
    if TMP_DIR.exists():
        import shutil
        shutil.rmtree(TMP_DIR)

def cleanup():
    try:
        TMP_ZIP.unlink()
    except OSError:
        pass
    cleanup_dir_only()

def find_frp_files():
    files = list(TMP_DIR.rglob("*.nc"))
    preferred = [p for p in files if p.name in {"FRP_in.nc", "FRP_an.nc"}]
    return preferred if preferred else files

def pick_var(ds, names):
    lower = {k.lower(): k for k in ds.variables}
    for name in names:
        if name.lower() in lower:
            return ds.variables[lower[name.lower()]]
    for k, v in ds.variables.items():
        lk = k.lower()
        if any(n.lower() in lk for n in names):
            return v
    return None

def to_float_array(var):
    arr = np.array(var[:])
    if np.ma.isMaskedArray(arr):
        arr = arr.filled(np.nan)
    return arr.astype(float)

def extract_hotspots(product_name):
    hotspots = []
    mappings = []

    for path in find_frp_files():
        try:
            ds = nc.Dataset(path)
        except Exception:
            continue

        try:
            latv = pick_var(ds, ["latitude", "lat"])
            lonv = pick_var(ds, ["longitude", "lon"])
            frpv = pick_var(ds, ["FRP", "fire_radiative_power"])
            uncv = pick_var(ds, ["FRP_uncertainty", "frp_uncertainty", "uncertainty"])
            confv = pick_var(ds, ["confidence", "fire_confidence"])
            classv = pick_var(ds, ["classification", "hotspot_classification", "class"])

            mapping = {
                "file": path.name,
                "latitude": None if latv is None else latv.name,
                "longitude": None if lonv is None else lonv.name,
                "frp": None if frpv is None else frpv.name,
                "frp_uncertainty": None if uncv is None else uncv.name,
                "confidence": None if confv is None else confv.name,
                "classification": None if classv is None else classv.name,
            }
            mappings.append(mapping)

            if latv is None or lonv is None or frpv is None:
                continue

            lat = to_float_array(latv).reshape(-1)
            lon = to_float_array(lonv).reshape(-1)
            frp = to_float_array(frpv).reshape(-1)
            unc = None if uncv is None else to_float_array(uncv).reshape(-1)
            conf = None if confv is None else to_float_array(confv).reshape(-1)
            cls = None if classv is None else np.array(classv[:]).reshape(-1)

            n = min(len(lat), len(lon), len(frp))
            for i in range(n):
                la, lo, fv = lat[i], lon[i], frp[i]
                if not (np.isfinite(la) and np.isfinite(lo) and np.isfinite(fv)):
                    continue
                if fv <= 0:
                    continue

                pt = Point(float(lo), float(la))
                if not (POLYGON.contains(pt) or POLYGON.touches(pt)):
                    continue

                hotspots.append({
                    "latitude": float(la),
                    "longitude": float(lo),
                    "frp_mw": float(fv),
                    "frp_uncertainty_mw": None if unc is None or i >= len(unc) or not np.isfinite(unc[i]) else float(unc[i]),
                    "confidence": None if conf is None or i >= len(conf) or not np.isfinite(conf[i]) else float(conf[i]),
                    "classification": None if cls is None or i >= len(cls) else str(cls[i]),
                    "source_file": path.name,
                    "source": "Sentinel-3 SLSTR SL_2_FRP",
                    "product_name": product_name,
                })
        finally:
            ds.close()

    return hotspots, mappings

def main():
    checked = datetime.now(timezone.utc).isoformat()
    seen = set(load_json(STATE_PATH, []))
    cursor = load_json(CURSOR_PATH, {"last_product_start": None})

    status = {
        "checked_at_utc": checked,
        "polygon": list(POLYGON.exterior.coords),
        "source": "Copernicus Sentinel-3 SLSTR SL_2_FRP",
        "query_window_days": 14,
        "processed_products": [],
        "new_hotspots": [],
        "errors": [],
    }

    try:
        products, request_url = query_products(days=14)
        status["catalogue_request_url"] = request_url
        status["catalogue_products_spatially_filtered"] = len(products)

        selected = prefer_nrt(products)
        status["catalogue_products_after_nrt_dedup"] = len(selected)

        last = cursor.get("last_product_start")
        pending = []
        for p in selected:
            start = (p.get("ContentDate") or {}).get("Start")
            if start and (last is None or start > last):
                pending.append(p)

        # First run processes only newest relevant product to avoid a historic flood.
        if last is None and pending:
            pending = pending[-1:]

        status["last_product_start_before_run"] = last
        status["pending_product_count"] = len(pending)

        token = get_token() if pending else None
        all_new = []
        last_success = last

        for product in pending:
            pr = {
                "id": product.get("Id"),
                "name": product.get("Name"),
                "content_start": (product.get("ContentDate") or {}).get("Start"),
                "s3path": product.get("S3Path"),
            }

            try:
                download_product(product["Id"], token)
                unzip_product()
                hotspots, mappings = extract_hotspots(product["Name"])

                pr["inside_polygon"] = len(hotspots)
                pr["dataset_mapping"] = mappings

                new_for_product = []
                for h in hotspots:
                    key = "|".join([
                        product["Id"],
                        f"{h['latitude']:.6f}",
                        f"{h['longitude']:.6f}",
                        f"{h['frp_mw']:.6f}",
                        h["source_file"],
                    ])
                    h["_key"] = key
                    h["content_start"] = pr["content_start"]

                    if key not in seen:
                        seen.add(key)
                        new_for_product.append(h)
                        all_new.append(h)

                pr["new_hotspot_count"] = len(new_for_product)

                last_success = pr["content_start"]
                save_json(CURSOR_PATH, {"last_product_start": last_success})

            except Exception as e:
                pr["error"] = str(e)
                status["errors"].append({
                    "product": pr["name"],
                    "error": str(e),
                })
                status["processed_products"].append(pr)
                cleanup()
                break

            status["processed_products"].append(pr)
            cleanup()

        status["last_product_start_after_run"] = last_success
        status["new_hotspots"] = all_new
        status["new_hotspot_count"] = len(all_new)

        if all_new:
            with EVENTS_PATH.open("a", encoding="utf-8") as f:
                for h in all_new:
                    f.write(json.dumps({
                        "detected_at_utc": checked,
                        **h
                    }) + "\n")

    except Exception as e:
        status["errors"].append({"general": str(e)})
        status["new_hotspot_count"] = 0
        cleanup()

    save_json(STATE_PATH, sorted(seen))
    save_json(STATUS_PATH, status)
    print(json.dumps(status, indent=2))

if __name__ == "__main__":
    main()
