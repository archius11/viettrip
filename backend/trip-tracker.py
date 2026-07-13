#!/usr/bin/env python3
"""
Мини-бэкенд трека поездки (без внешних зависимостей, только стандартная библиотека).

Каждая точка трека имеет короткий стабильный id (p1, p2, …) и источник src:
  gps    — живой пинг с телефона (/api/ping)
  manual — поставлена вручную по карте (/api/point)
  photo  — координата фотографии, перенесённая в трек (/api/import-photos)
На id можно ссылаться (в т.ч. вербально: «убери p137») — они не переиспользуются.

У точки есть необязательное поле edge — как рисовать отрезок ОТ ПРЕДЫДУЩЕЙ точки к ней:
  auto (по умолчанию, поля нет) — вести по дорогам, но если роутер выдал крюк втрое
       длиннее прямой, фронт сам рисует прямую (точка не на дороге)
  road     — всегда по дорогам   |   straight — всегда прямая (бездорожье, паром, тропа)

Маршруты:
  GET    /api/trail          -> {"points":[{id,lat,lng,t,src,acc?,photo?,edge?}...], "updated":ts}  (публично)
  GET    /api/health         -> {"ok":true}
  POST   /api/ping           -> точка живого трека. Тело {lat,lng,acc?}. Заголовок X-Trip-Key
  POST   /api/point          -> точка вручную. Тело {lat,lng,after?:"pN"|before?:"pN"|t?,edge?}.
                                С якорем (after/before) время считает сервер — середина
                                промежутка с соседом, точка гарантированно встаёт между ними
  PATCH  /api/point?id=pN    -> подвинуть/переставить во времени/сменить edge. Тело {lat?,lng?,t?,edge?}
  DELETE /api/point?id=pN    -> удалить любую точку трека
  POST   /api/import-photos  -> перенести координаты всех фото в трек точками src=photo.
                                Идемпотентно: уже перенесённые (по photo=<id фото>) пропускаются
  DELETE /api/trail          -> очистить трек
Всё, кроме GET, требует заголовка X-Trip-Key: <секрет>.

(Фото-маршруты /api/photo[s] — ниже в обработчике.)
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
PHOTO_ID_RE    = re.compile(r"^[0-9]+_[0-9a-f]+$")   # id фото (исторический формат)
POINT_ID_RE    = re.compile(r"^p[0-9]+$")            # id точки трека — короткий, произносимый
EDGE_MODES     = ("auto", "road", "straight")        # отрезок от предыдущей точки к этой

_lock = threading.Lock()


def _normalize(d):
    """Привести трек к текущей модели: у каждой точки есть id (pN) и src.
    Мигрирует записи старого формата (без id; ручные помечались m:1) при первой
    загрузке. d['seq'] — счётчик выданных id, монотонный: id удалённой точки
    не переиспользуется, ссылки на неё не «переезжают» на чужую точку.
    Возвращает True, если что-то поменяли (тогда файл надо сохранить)."""
    seq = int(d.get("seq") or 0)
    changed = False
    for p in d["points"]:
        if "m" in p or not p.get("src"):
            p["src"] = "manual" if p.pop("m", None) else p.get("src") or "gps"
            changed = True
        if not POINT_ID_RE.match(str(p.get("id", ""))):
            seq += 1
            p["id"] = "p%d" % seq
            changed = True
    if seq != d.get("seq"):
        d["seq"] = seq
        changed = True
    return changed


def _next_id(d):
    d["seq"] = int(d.get("seq") or 0) + 1
    return "p%d" % d["seq"]


def _load():
    d = None
    try:
        with open(DATA, "r", encoding="utf-8") as f:
            j = json.load(f)
            if isinstance(j, dict) and isinstance(j.get("points"), list):
                d = j
    except Exception:
        pass
    if d is None:
        return {"points": [], "updated": 0, "seq": 0}
    if _normalize(d):
        _save(d)               # только после закрытия файла: os.replace поверх открытого — ошибка на Windows
    return d


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


def _between(a, b, step):
    """Время точки, вставляемой рядом с якорем: середина промежутка до соседа b.
    Соседа нет (якорь крайний) → отступаем на step секунд. Промежуток может быть
    любым (хоть 1 с) — время дробное, места между двумя точками хватит всегда."""
    if b is None:
        return round(a + step, 3)
    mid = round((a + b) / 2.0, 3)
    return int(mid) if float(mid).is_integer() else mid


def _insert_point(d, pt):
    """Вставить точку в d['points'] по времени (трек держим отсортированным по t).
    Живые пинги идут «сейчас» и садятся в конец; ручные точки могут быть в прошлом —
    их место находим бинарным поиском. Фронт всё равно сортирует по t, порядок на
    сервере — только гигиена и предсказуемый MAX_POINTS."""
    pts = d["points"]
    t = pt["t"]
    lo, hi = 0, len(pts)
    while lo < hi:
        mid = (lo + hi) // 2
        if pts[mid].get("t", 0) <= t:
            lo = mid + 1
        else:
            hi = mid
    pts.insert(lo, pt)
    if len(pts) > MAX_POINTS:
        d["points"] = pts[-MAX_POINTS:]


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

    def do_PATCH(self):
        if self._path() == "/api/point":
            return self._point_patch()
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        p = self._path()
        if p == "/api/photo":
            return self._photo_delete()
        if p == "/api/point":
            return self._point_delete()
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
        if p == "/api/point":
            return self._point_post()
        if p == "/api/import-photos":
            return self._import_photos()
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
        with _lock:
            d = _load()
            pts = d["points"]
            pt = {"id": None, "lat": round(lat, 6), "lng": round(lng, 6),
                  "t": now, "src": "gps"}
            if acc is not None:
                pt["acc"] = round(acc, 1)
            if pts:
                last = pts[-1]
                close = _dist_m((last["lat"], last["lng"]), (lat, lng)) < MIN_DIST_M
                soon = (now - last.get("t", 0)) < MIN_DT_S
                if close and soon:
                    pt["id"] = last["id"]   # обновляем последнюю (свежее время) — id держим прежний
                    pts[-1] = pt
                    d["updated"] = now
                    _save(d)
                    return self._send(200, {"ok": True, "id": pt["id"],
                                            "points": len(pts), "dedup": True})
            pt["id"] = _next_id(d)
            pts.append(pt)
            if len(pts) > MAX_POINTS:
                d["points"] = pts[-MAX_POINTS:]
            d["updated"] = now
            _save(d)
            return self._send(200, {"ok": True, "id": pt["id"], "points": len(d["points"])})

    def _body(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n < 0 or n > MAX_BODY:
            raise ValueError("len")
        if n == 0:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    @staticmethod
    def _coords(body):
        lat = float(body["lat"]); lng = float(body["lng"])
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError("range")
        return round(lat, 6), round(lng, 6)

    @staticmethod
    def _stamp(t):
        t = float(t)
        if not (0 < t < 4102444800):            # 1970 < t < 2100 — отсекаем мусор
            raise ValueError("t")
        return int(t) if t.is_integer() else round(t, 3)   # дробные секунды — для вставок между соседями

    @staticmethod
    def _edge(body):
        e = body.get("edge")
        if e is None:
            return None
        if e not in EDGE_MODES:
            raise ValueError("edge")
        return e

    @staticmethod
    def _anchor(body, key):
        a = body.get(key)
        if a is None:
            return None
        if not POINT_ID_RE.match(str(a)):
            raise ValueError(key)
        return a

    def _point_post(self):
        """Ручная точка трека: {lat, lng, after?:"pN" | before?:"pN" | t?, edge?}.

        Якорь (after/before) — главный способ: точка встаёт прямо за/перед указанной,
        а время ей считает сервер (середина промежутка с соседом). Так понятно,
        продолжением какой точки она является, и она не улетает в конец трека из-за
        неверно набранного времени. Без якоря — по времени t (по умолчанию «сейчас»).
        src=manual."""
        if not self._authed():
            return self._send(401, {"error": "bad key"})
        try:
            body = self._body()
            lat, lng = self._coords(body)
            after = self._anchor(body, "after")
            before = self._anchor(body, "before")
            edge = self._edge(body)
            t = self._stamp(body["t"]) if body.get("t") is not None else None
        except Exception:
            return self._send(400, {"error": "bad body"})

        with _lock:
            d = _load()
            pts = d["points"]
            idx = None
            if after or before:
                aid = after or before
                i = next((k for k, p in enumerate(pts) if p.get("id") == aid), None)
                if i is None:
                    return self._send(404, {"error": "anchor not found"})
                if after:
                    nb = pts[i + 1].get("t") if i + 1 < len(pts) else None
                    idx, t = i + 1, _between(pts[i].get("t", 0), nb, 60)
                else:
                    nb = pts[i - 1].get("t") if i > 0 else None
                    idx, t = i, _between(pts[i].get("t", 0), nb, -60)
            if t is None:
                t = int(time.time())
            pt = {"id": _next_id(d), "lat": lat, "lng": lng, "t": t, "src": "manual"}
            if edge and edge != "auto":
                pt["edge"] = edge
            if idx is None:
                _insert_point(d, pt)            # без якоря — по времени
            else:
                pts.insert(idx, pt)             # с якорем — ровно туда, куда просили
                if len(pts) > MAX_POINTS:
                    d["points"] = pts[-MAX_POINTS:]
            d["updated"] = int(time.time())
            _save(d)
            return self._send(200, {"ok": True, "id": pt["id"], "point": pt,
                                    "points": len(d["points"])})

    def _point_patch(self):
        """Правка точки по id: {lat?,lng?,t?,edge?}. Двигаем по карте, переставляем во
        времени (порядок трека = порядок по t, поэтому при смене t переставляем точку)
        и/или меняем режим отрезка от предыдущей точки (auto|road|straight)."""
        if not self._authed():
            return self._send(401, {"error": "bad key"})
        pid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
        if not POINT_ID_RE.match(pid):
            return self._send(400, {"error": "bad id"})
        try:
            body = self._body()
            move = ("lat" in body or "lng" in body)
            lat, lng = self._coords(body) if move else (None, None)
            t = self._stamp(body["t"]) if body.get("t") is not None else None
            edge = self._edge(body)
            if not move and t is None and edge is None:
                raise ValueError("empty")
        except Exception:
            return self._send(400, {"error": "bad body"})

        with _lock:
            d = _load()
            pt = next((p for p in d["points"] if p.get("id") == pid), None)
            if pt is None:
                return self._send(404, {"error": "not found"})
            if move:
                pt["lat"] = lat; pt["lng"] = lng
                pt.pop("acc", None)             # координата уже не «замер GPS с точностью N м»
            if edge is not None:
                if edge == "auto":
                    pt.pop("edge", None)        # авто = поля нет
                else:
                    pt["edge"] = edge
            if t is not None and t != pt.get("t"):
                pt["t"] = t
                d["points"].remove(pt)
                _insert_point(d, pt)
            d["updated"] = int(time.time())
            _save(d)
            return self._send(200, {"ok": True, "point": pt})

    def _point_delete(self):
        """Удалить любую точку трека по id (id не переиспользуется — ссылки не съедут)."""
        if not self._authed():
            return self._send(401, {"error": "bad key"})
        pid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
        if not POINT_ID_RE.match(pid):
            return self._send(400, {"error": "bad id"})
        with _lock:
            d = _load()
            keep = [p for p in d["points"] if p.get("id") != pid]
            if len(keep) == len(d["points"]):
                return self._send(404, {"error": "not found"})
            d["points"] = keep
            d["updated"] = int(time.time())
            _save(d)
        return self._send(200, {"ok": True, "points": len(keep)})

    def _import_photos(self):
        """Разово перенести координаты фото в трек точками src=photo (поездка кончилась,
        новых фото не будет). Идемпотентно: фото, уже перенесённое (есть точка с
        photo=<id фото>), пропускаем. После импорта путь строится только по трек-точкам,
        и каждую координату можно двигать/удалять по её id независимо от самого фото."""
        if not self._authed():
            return self._send(401, {"error": "bad key"})
        with _lock:
            d = _load()
            photos = _load_photos()["photos"]
            done = {p["photo"] for p in d["points"] if p.get("photo")}
            added = []
            for ph in photos:
                if not ph.get("id") or ph["id"] in done:
                    continue
                try:
                    lat, lng = self._coords(ph)
                    t = self._stamp(ph.get("t") or 0)
                except Exception:
                    continue                    # фото без валидных координат/времени — не точка трека
                pt = {"id": _next_id(d), "lat": lat, "lng": lng, "t": t,
                      "src": "photo", "photo": ph["id"]}
                _insert_point(d, pt)
                added.append(pt["id"])
            if added:
                d["updated"] = int(time.time())
                _save(d)
        return self._send(200, {"ok": True, "added": len(added), "ids": added,
                                "skipped": len(photos) - len(added),
                                "points": len(d["points"])})

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
        if not PHOTO_ID_RE.match(pid):
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
