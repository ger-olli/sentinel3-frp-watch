
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import netCDF4 as nc
import numpy as np
import requests
from shapely.geometry import Point, Polygon, shape

POLYGON = Polygon([
    (21.30252, 44.83812),
    (21.21291, 44.79014),
    (20.99648, 44.89789),
    (21.10188, 44.96886),
])

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
session.headers.update({"User-Agent": "sentinel3-frp-watch/1.0"})

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default

def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")

def get_token():
    r = session.post(TOKEN_URL, data={
        "client_id": "cdse-public",
        "username": USERNAME,
        "password": PASSWORD,
        "grant_type": "password",
    }, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError("No access_token returned by CDSE identity service.")
    return data["access_token"]

def query_products():
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=3)

    # Public catalogue query; product type and recent time window.
    filt = (
        "Collection/Name eq 'SENTINEL-3' and "
        "Attributes/OData.CSC.StringAttribute/any(att:"
        "att/Name eq 'productType' and "
        "att/OData.CSC.StringAttribute/Value eq 'SL_2_FRP___') and "
        f"ContentDate/Start ge {start.isoformat().replace('+00:00','Z')}"
    )

    params = {
        "$filter": filt,
        "$orderby": "ContentDate/Start asc",
        "$top": "200",
        "$select": "Id,Name,ContentDate,GeoFootprint",
    }
    r = session.get(CATALOGUE, params=params, timeout=60)
    r.raise_for_status()
    return r.json().get("value", [])

def intersects_polygon(product):
    gf = product.get("GeoFootprint")
    if not gf:
        return False
    try:
        return shape(gf).intersects(POLYGON)
    except Exception:
        return False

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
    if TMP_DIR.exists():
        import shutil
        shutil.rmtree(TMP_DIR)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(TMP_ZIP, "r") as z:
        z.extractall(TMP_DIR)

def all_nc_files():
    return list(TMP_DIR.rglob("*.nc"))

def find_frp_files():
    # Official product commonly contains FRP_in.nc and FRP_an.nc.
    files = all_nc_files()
    preferred = [p for p in files if p.name in {"FRP_in.nc", "FRP_an.nc"}]
    return preferred if preferred else files

def pick_var(ds, names):
    low = {k.lower(): k for k in ds.variables.keys()}
    for name in names:
        if name.lower() in low:
            return ds.variables[low[name.lower()]]
    for k, v in ds.variables.items():
        lk = k.lower()
        if any(name.lower() in lk for name in names):
            return v
    return None

def as_float_array(var):
    if var is None:
        return None
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
            classv = pick_var(ds, ["classification", "hotspot_classification", "class"])
            timev = pick_var(ds, ["time", "time_stamp", "timestamp"])

            mappings.append({
                "file": path.name,
                "latitude": None if latv is None else latv.name,
                "longitude": None if lonv is None else lonv.name,
                "frp": None if frpv is None else frpv.name,
                "frp_uncertainty": None if uncv is None else uncv.name,
                "classification": None if classv is None else classv.name,
                "time": None if timev is None else timev.name,
            })

            if latv is None or lonv is None or frpv is None:
                continue

            lat = as_float_array(latv).reshape(-1)
            lon = as_float_array(lonv).reshape(-1)
            frp = as_float_array(frpv).reshape(-1)
            unc = None if uncv is None else as_float_array(uncv).reshape(-1)
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

                item = {
                    "latitude": float(la),
                    "longitude": float(lo),
                    "frp_mw": float(fv),
                    "frp_uncertainty_mw": None if unc is None or i >= len(unc) or not np.isfinite(unc[i]) else float(unc[i]),
                    "classification": None if cls is None or i >= len(cls) else str(cls[i]),
                    "source_file": path.name,
                    "source": "Sentinel-3 SLSTR SL_2_FRP",
                    "product_name": product_name,
                }
                hotspots.append(item)
        finally:
            ds.close()

    return hotspots, mappings

def cleanup():
    for p in [TMP_ZIP]:
        try:
            p.unlink()
        except OSError:
            pass
    if TMP_DIR.exists():
        import shutil
        shutil.rmtree(TMP_DIR)

def main():
    checked = datetime.now(timezone.utc).isoformat()
    seen = set(load_json(STATE_PATH, []))
    cursor = load_json(CURSOR_PATH, {"last_product_start": None})

    status = {
        "checked_at_utc": checked,
        "polygon": list(POLYGON.exterior.coords),
        "source": "Copernicus Sentinel-3 SLSTR SL_2_FRP",
        "processed_products": [],
        "new_hotspots": [],
        "errors": [],
    }

    try:
        products = [p for p in query_products() if intersects_polygon(p)]
        status["catalogue_products_intersecting_polygon"] = len(products)

        last = cursor.get("last_product_start")
        pending = []
        for p in products:
            start = (p.get("ContentDate") or {}).get("Start")
            if not start:
                continue
            if last is None or start > last:
                pending.append(p)

        # First run: only latest intersecting product, to avoid historical flood.
        if last is None and pending:
            pending = pending[-1:]

        status["pending_product_count"] = len(pending)
        token = get_token() if pending else None
        all_new = []
        last_success = last

        for product in pending:
            pr = {
                "id": product["Id"],
                "name": product["Name"],
                "content_start": product["ContentDate"]["Start"],
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
                    if key not in seen:
                        seen.add(key)
                        new_for_product.append(h)
                        all_new.append(h)

                pr["new_hotspot_count"] = len(new_for_product)
                last_success = product["ContentDate"]["Start"]
                save_json(CURSOR_PATH, {"last_product_start": last_success})
            except Exception as e:
                pr["error"] = str(e)
                status["errors"].append({"product": product["Name"], "error": str(e)})
                status["processed_products"].append(pr)
                cleanup()
                break

            status["processed_products"].append(pr)
            cleanup()

        status["last_product_start_before_run"] = last
        status["last_product_start_after_run"] = last_success
        status["new_hotspots"] = all_new
        status["new_hotspot_count"] = len(all_new)

        if all_new:
            with EVENTS_PATH.open("a", encoding="utf-8") as f:
                for h in all_new:
                    f.write(json.dumps({"detected_at_utc": checked, **h}) + "\n")

    except Exception as e:
        status["errors"].append({"general": str(e)})
        status["new_hotspot_count"] = 0
        cleanup()

    save_json(STATE_PATH, sorted(seen))
    save_json(STATUS_PATH, status)
    print(json.dumps(status, indent=2))

if __name__ == "__main__":
    main()
