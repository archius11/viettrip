# viettrip

Веб-страница трекинга поездки: интерактивная карта маршрута с точками трека и
фотографиями. Живёт на **https://viettrip.arturgaleev.ru**.

## Структура

```
frontend/
  index.html     # одностраничное приложение: Leaflet-карта, весь JS/CSS внутри,
                 # библиотеки (leaflet, markercluster) подключаются с CDN unpkg
  route.json     # статические данные маршрута (meta + track)

backend/
  trip-tracker.py  # мини-API на стандартной библиотеке Python (без зависимостей)
```

## Бэкенд

`backend/trip-tracker.py` — HTTP-сервер на `http.server`, слушает `127.0.0.1:$TRIP_PORT`
(за nginx). Внешних зависимостей нет, запускается любым Python 3.

### Маршруты

| Метод  | Путь          | Описание                                                        |
|--------|---------------|-----------------------------------------------------------------|
| GET    | `/api/health` | Проверка живости → `{"ok": true}`                               |
| GET    | `/api/trail`  | Трек → `{"points": [{lat,lng,t,acc?}...], "updated": ts}`       |
| GET    | `/api/photos` | Список фотографий с метаданными                                 |
| GET    | `/api/photo`  | Отдельное фото по `id`                                          |
| POST   | `/api/ping`   | Добавить точку трека. Тело `{lat,lng,acc?}`, заголовок `X-Trip-Key` |
| POST   | `/api/photo`  | Загрузить фото (JSON с base64 full+thumb), заголовок `X-Trip-Key`  |
| DELETE | `/api/trail`  | Очистить трек. Заголовок `X-Trip-Key`                           |
| DELETE | `/api/photo`  | Удалить фото по `id`. Заголовок `X-Trip-Key`                    |

Запись (`POST`/`DELETE`) требует заголовка `X-Trip-Key`, совпадающего с `$TRIP_KEY`.

### Переменные окружения

| Переменная          | По умолчанию                    | Назначение                          |
|---------------------|---------------------------------|-------------------------------------|
| `TRIP_PORT`         | `8791`                          | Порт прослушивания                  |
| `TRIP_KEY`          | *(пусто)*                       | Секрет для записи; пусто = запись запрещена |
| `TRIP_DATA`         | `/var/lib/viettrip/trail.json`  | Файл с точками трека                |
| `TRIP_PHOTOS`       | `/var/lib/viettrip/photos`      | Каталог с фотографиями              |
| `TRIP_PHOTOS_META`  | `/var/lib/viettrip/photos.json` | Метаданные фотографий               |

### Запуск

```bash
export TRIP_KEY="ваш-секрет"
python3 backend/trip-tracker.py
```

За nginx: проксировать `/api/` на `127.0.0.1:8791` и отдавать `frontend/` как
статику (плюс каталог `/photos/` из `$TRIP_PHOTOS`).

## Данные

Рантайм-файлы (`trail.json`, `photos.json`, каталог `photos/`) генерируются
бэкендом на сервере и намеренно исключены из репозитория через `.gitignore`.
