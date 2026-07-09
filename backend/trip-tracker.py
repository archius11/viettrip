#!/usr/bin/env python3
"""
Мини-бэкенд трека поездки (без внешних зависимостей, только стандартная библиотека).

Маршруты:
  GET    /api/trail   -> {"points":[{lat,lng,t,acc?}...], "updated":ts}   (публично)
  GET    /api/health  -> {"ok":true}
  POST   /api/ping    -> добавить точку. Тело {lat,lng,acc?}. Заголовок X-Trip-Key: <секрет>
  DELETE /api/trail   -> очистить трек. Заголовок X-Trip-Key: <секрет>

Слушает 127.0.0.1:$TRIP_PORT (за nginx). Данные — JSON-файл $TRIP_DATA.
"""
import json, os, time, threading, base64, re
from math import radians, sin, cos, asin, sqrt
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST       = "127.0.0.1"
PORT       = int(os.environ.get("TRIP_PORT", "8791"))
KEY        = os.environ.get("TRIP_KEY", "")
DATA       = os.environ.get("TRIP_DATA", "/var/lib/viettrip/trail.json")
PHOTOS_DIR = os.environ.get("TRIP_PHOTOS", "/var/lib/viettrip/photos")
PHOTOS_META= os.environ.get("TRIP_PHOTOS_META", "/var/lib/viettrip/photos.json")
MAX_POINTS = 5000
MIN_DIST_M = 20.0     # не добавлять точку ближе 20 м к предыдущей,
MIN_DT_S   = 30.0     # ... если прошло меньше 30 с
MAX_BODY   = 4096
MAX_PHOTO_BODY = 20 * 1024 * 1024   # тело POST /api/photo (JSON с base64 full+thumb)
MAX_IMG_BYTES  = 8 * 1024 * 1024    # предел на один декодированный JPEG
MAX_PHOTOS     = 2000               # предел числа фото (защита диска)
ID_RE          = re.compile(r"^[0-9]+_[0-9a-f]+$")   # формат id, что мы генерим сами

_lock = threading.Lock()


def _load():
    try:
        with open(DATA, "r", encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("points"), list):
                return d
    except Exception:
        pass
    return {"points": [], "updated": 0}


def _save(d):
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, DATA)


def _dist_m(a, b):
    la1, lo1, la2, lo2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * asin(sqrt(h))


def _load_photos():
    try:
        with open(PHOTOS_META, "r", encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("photos"), list):
                return d
    except Exception:
        pass
    return {"photos": [], "updated": 0}


def _save_photos(d):
    os.makedirs(os.path.dirname(PHOTOS_META), exist_ok=True)
    tmp = PHOTOS_META + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, PHOTOS_META)


class Handler(BaseHTTPRequestHandler):
    server_version = "trip/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _path(self):
        return self.path.split("?", 1)[0]

    def _authed(self):
        return bool(KEY) and self.headers.get("X-Trip-Key", "") == KEY

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = self._path()
        if p == "/api/health":
            return self._send(200, {"ok": True})
        if p == "/api/trail":
            with _lock:
                d = _load()
            return self._send(200, d)
        if p == "/api/photos":
            with _lock:
                d = _load_photos()
            return self._send(200, d)
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        p = self._path()
        if p == "/api/photo":
            return self._photo_delete()
        if p != "/api/trail":
            return self._send(404, {"error": "not found"})
        if not self._authed():
            return self._send(401, {"error": "bad key"})
        with _lock:
            _save({"points": [], "updated": int(time.time())})
        return self._send(200, {"ok": True, "points": 0})

    def do_POST(self):
        p = self._path()
        if p == "/api/photo":
            return self._photo_post()
        if p != "/api/ping":
            return self._send(404, {"error": "not found"})
        if not self._authed():
            return self._send(401, {"error": "bad key"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > MAX_BODY:
                raise ValueError("len")
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            lat = float(body["lat"])
            lng = float(body["lng"])
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise ValueError("range")
            acc = body.get("acc", None)
            acc = float(acc) if acc is not None else None
        except Exception:
            return self._send(400, {"error": "bad body"})

        now = int(time.time())
        pt = {"lat": round(lat, 6), "lng": round(lng, 6), "t": now}
        if acc is not None:
            pt["acc"] = round(acc, 1)

        with _lock:
            d = _load()
            pts = d["points"]
            if pts:
                last = pts[-1]
                close = _dist_m((last["lat"], last["lng"]), (lat, lng)) < MIN_DIST_M
                soon = (now - last["t"]) < MIN_DT_S
                if close and soon:
                    pts[-1] = pt            # обновляем последнюю (свежее время), не плодим точки
                    d["updated"] = now
                    _save(d)
                    return self._send(200, {"ok": True, "points": len(pts), "dedup": True})
            pts.append(pt)
            if len(pts) > MAX_POINTS:
                d["points"] = pts[-MAX_POINTS:]
            d["updated"] = now
            _save(d)
            return self._send(200, {"ok": True, "points": len(d["points"])})

    def _photo_post(self):
        if not self._authed():
            return self._send(401, {"error": "bad key"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > MAX_PHOTO_BODY:
                raise ValueError("len")
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            lat = float(body["lat"]); lng = float(body["lng"])
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise ValueError("range")
            full  = base64.b64decode(body["full_b64"],  validate=True)
            thumb = base64.b64decode(body["thumb_b64"], validate=True)
            for b in (full, thumb):
                if len(b) < 4 or b[:3] != b"\xff\xd8\xff":   # JPEG magic
                    raise ValueError("not jpeg")
                if len(b) > MAX_IMG_BYTES:
                    raise ValueError("too big")
            t = body.get("t")
            t = int(t) if t is not None else int(time.time())
            w = int(body.get("w") or 0); h = int(body.get("h") or 0)
        except Exception:
            return self._send(400, {"error": "bad photo"})

        with _lock:
            meta = _load_photos()
            if len(meta["photos"]) >= MAX_PHOTOS:
                return self._send(507, {"error": "storage full"})
            pid = "%d_%s" % (int(time.time()), os.urandom(4).hex())
            os.makedirs(PHOTOS_DIR, exist_ok=True)
            with open(os.path.join(PHOTOS_DIR, pid + ".jpg"), "wb") as f:
                f.write(full)
            with open(os.path.join(PHOTOS_DIR, pid + "_t.jpg"), "wb") as f:
                f.write(thumb)
            rec = {"id": pid, "lat": round(lat, 6), "lng": round(lng, 6), "t": t,
                   "full": "/photos/" + pid + ".jpg", "thumb": "/photos/" + pid + "_t.jpg"}
            if w and h:
                rec["w"] = w; rec["h"] = h
            meta["photos"].append(rec)
            meta["updated"] = int(time.time())
            _save_photos(meta)
            return self._send(200, {"ok": True, "id": pid, "count": len(meta["photos"])})

    def _photo_delete(self):
        if not self._authed():
            return self._send(401, {"error": "bad key"})
        pid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
        if not ID_RE.match(pid):
            return self._send(400, {"error": "bad id"})
        with _lock:
            meta = _load_photos()
            keep = [p for p in meta["photos"] if p.get("id") != pid]
            if len(keep) == len(meta["photos"]):
                return self._send(404, {"error": "not found"})
            meta["photos"] = keep
            meta["updated"] = int(time.time())
            _save_photos(meta)
        for suf in (".jpg", "_t.jpg"):
            try:
                os.remove(os.path.join(PHOTOS_DIR, pid + suf))
            except OSError:
                pass
        return self._send(200, {"ok": True, "count": len(keep)})

    def log_message(self, *args):
        pass  # тишина в журнале


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
