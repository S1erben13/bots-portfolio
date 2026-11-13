import secrets
from datetime import date, datetime
from html import escape

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from booking.db import connect
from booking.service import BookingError


STATUS = {"confirmed": "Подтверждена", "cancelled": "Отменена", "attended": "Пришёл", "absent": "Не пришёл"}


def create_app(service):
    app = FastAPI(title="Онлайн-запись", docs_url=None, redoc_url=None)
    basic = HTTPBasic()
    csrf = secrets.token_urlsafe(32)

    def auth(credentials: HTTPBasicCredentials = Depends(basic)):
        config = service.config
        valid_user = secrets.compare_digest(credentials.username.encode(), config.admin_user.encode())
        valid_password = secrets.compare_digest(credentials.password.encode(), config.admin_password.encode())
        if not config.admin_password or not (valid_user and valid_password):
            raise HTTPException(401, "Требуется авторизация", headers={"WWW-Authenticate": "Basic"})

    def form(action, fields, label="Сохранить"):
        return (f'<form method="post" action="/admin/{action}">'
                f'<input type="hidden" name="csrf" value="{csrf}">{fields}<button>{label}</button></form>')

    def hidden(name, value):
        return f'<input type="hidden" name="{name}" value="{escape(str(value), quote=True)}">'

    def field(name, value="", kind="text", extra=""):
        return f'<input name="{name}" type="{kind}" value="{escape(str(value), quote=True)}" {extra}>'

    def page(body):
        return HTMLResponse('''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Онлайн-запись — админка</title>
<style>body{font:16px system-ui;max-width:1200px;margin:32px auto;padding:0 16px;color:#16312e;background:#f6f8f7}
table{border-collapse:collapse;width:100%;background:white}td,th{border:1px solid #ccd8d4;padding:10px;text-align:left}
input,select,button{font:inherit;margin:4px;padding:7px;max-width:95%}button{cursor:pointer}form{display:inline-block}
h1,h2{color:#174d42}.scroll{overflow:auto}small{display:block}a{color:#126b55}</style>
<h1>Онлайн-запись</h1><nav><a href="/admin">Записи</a> · <a href="/admin/catalog">Услуги, мастера и расписание</a></nav>''' + body + '</html>', headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/admin", dependencies=[Depends(auth)])
    def index(day: date | None = None, days: int = Query(1, ge=1, le=7)):
        day = day or service.clock().date()
        body = f'<h2>Записи с {day} ({days} дн.)</h2><form><input name="day" type="date" value="{day}"><select name="days"><option value="1">День</option><option value="7">Неделя</option></select><button>Показать</button></form>'
        body += '<div class="scroll"><table><tr><th>№ / время</th><th>Услуга / мастер</th><th>Клиент</th><th>Статус</th><th>Действия</th></tr>'
        for row in service.bookings(day=day, days=days):
            body += f"<tr><td>{row['id']}<br>{escape(row['starts'].replace('T', ' '))}</td><td>{escape(row['service'])}<br>{escape(row['master'])}</td><td>{escape(row['name'])}<br>{escape(row['phone'])}</td><td>{STATUS[row['status']]}</td><td>"
            if row["status"] == "confirmed":
                for status in ("cancelled", "attended", "absent"):
                    body += form("booking", hidden("id", row["id"]) + hidden("status", status), STATUS[status])
            body += "</td></tr>"
        return page(body + "</table></div>")

    @app.get("/admin/catalog", dependencies=[Depends(auth)])
    def catalog():
        body = '<h2>Услуги</h2><p>Длительность кратна 15 минутам. Удаление архивирует запись и сохраняет историю.</p>'
        for row in service.services():
            fields = hidden("id", row["id"]) + field("name", row["name"], extra='required maxlength="100"')
            fields += field("duration", row["duration"], "number", 'min="15" max="480" step="15" required')
            body += "<div>" + form("service", fields) + form("archive", hidden("kind", "services") + hidden("id", row["id"]), "Удалить") + "</div>"
        body += form("service", field("name", extra='placeholder="Новая услуга" required maxlength="100"') + field("duration", 30, "number", 'min="15" max="480" step="15" required'), "Добавить")
        body += '<h2>Мастера</h2><p>Все мастера оказывают все услуги. Воскресенье в демо — выходной. Для другого графика измените строки ниже.</p>'
        with connect(service.config.db_path) as conn:
            for master in service.masters():
                mid = master["id"]
                body += '<h3>' + escape(master["name"]) + '</h3>'
                body += form("master", hidden("id", mid) + field("name", master["name"], extra='required maxlength="100"'))
                body += form("archive", hidden("kind", "masters") + hidden("id", mid), "Удалить мастера")
                body += '<small>Рабочее время и перерыв: начало / конец. Шаг 15 минут. Пустой перерыв — работа без перерыва.</small>'
                for weekday, label in enumerate(("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")):
                    work = conn.execute("SELECT * FROM schedule WHERE master_id=? AND weekday=?", (mid, weekday)).fetchone()
                    label += " (рабочий)" if work else " (выходной)"
                    fields = hidden("master_id", mid) + hidden("weekday", weekday) + label
                    fields += field("start", work["start"] if work else "09:00", "time", 'step="900" required aria-label="Начало работы"')
                    fields += field("end", work["end"] if work else "18:00", "time", 'step="900" required aria-label="Конец работы"')
                    fields += field("break_start", work["break_start"] or "" if work else "", "time", 'step="900" aria-label="Начало перерыва"')
                    fields += field("break_end", work["break_end"] or "" if work else "", "time", 'step="900" aria-label="Конец перерыва"')
                    body += '<div>' + form("schedule", fields, "Рабочий день")
                    body += form("dayoff_week", hidden("master_id", mid) + hidden("weekday", weekday), "Выходной") + '</div>'
                body += '<p>Выходные по конкретным датам</p>'
                for row in conn.execute("SELECT day FROM days_off WHERE master_id=? ORDER BY day", (mid,)):
                    body += form("dayoff_remove", hidden("master_id", mid) + hidden("day", row["day"]), row["day"] + " — убрать")
                body += form("dayoff", hidden("master_id", mid) + field("day", kind="date", extra="required"), "Добавить выходной")
        body += '<h3>Новый мастер</h3>' + form("master", field("name", extra='required maxlength="100"'), "Добавить")
        body += '<p>После добавления мастера задайте рабочие дни. Изменения графика не отменяют существующие записи.</p>'
        return page(body)

    @app.post("/admin/{action}", dependencies=[Depends(auth)])
    async def mutate(action: str, request: Request):
        data = await request.form()
        if not secrets.compare_digest(str(data.get("csrf", "")).encode(), csrf.encode()):
            raise HTTPException(403, "Обновите страницу и повторите действие")
        try:
            if action == "booking":
                bid = int(data["id"])
                status = str(data["status"])
                if status == "cancelled":
                    service.cancel(bid)
                elif status in ("attended", "absent"):
                    with connect(service.config.db_path) as conn:
                        cursor = conn.execute("UPDATE bookings SET status=? WHERE id=? AND status='confirmed'", (status, bid))
                        if not cursor.rowcount:
                            raise BookingError("Запись не найдена или уже изменена.")
                else:
                    raise BookingError("Неизвестный статус.")
                return RedirectResponse("/admin", status_code=303)
            with connect(service.config.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if action in ("service", "master"):
                    name = str(data["name"]).strip()
                    if not 2 <= len(name) <= 100:
                        raise BookingError("Название должно содержать 2–100 символов.")
                    table = "services" if action == "service" else "masters"
                    values = [name]
                    columns = "name"
                    assignments = "name=?"
                    if action == "service":
                        duration = int(data["duration"])
                        if duration < 15 or duration > 480 or duration % 15:
                            raise BookingError("Длительность: 15–480 минут с шагом 15.")
                        values.append(duration)
                        columns += ",duration"
                        assignments += ",duration=?"
                    if data.get("id"):
                        values.append(int(data["id"]))
                        conn.execute(f"UPDATE {table} SET {assignments} WHERE id=? AND active=1", values)
                    else:
                        placeholders = ",".join("?" for _ in values)
                        conn.execute(f"INSERT INTO {table}({columns}) VALUES ({placeholders})", values)
                elif action == "archive":
                    table = str(data["kind"])
                    if table not in ("services", "masters"):
                        raise BookingError("Неизвестный каталог.")
                    conn.execute(f"UPDATE {table} SET active=0 WHERE id=?", (int(data["id"]),))
                elif action in ("schedule", "dayoff_week", "dayoff", "dayoff_remove"):
                    mid = int(data["master_id"])
                    if not conn.execute("SELECT 1 FROM masters WHERE id=? AND active=1", (mid,)).fetchone():
                        raise BookingError("Мастер не найден.")
                    if action in ("dayoff", "dayoff_remove"):
                        day = str(date.fromisoformat(str(data["day"])))
                        if action == "dayoff":
                            conn.execute("INSERT OR IGNORE INTO days_off VALUES (?,?)", (mid, day))
                        else:
                            conn.execute("DELETE FROM days_off WHERE master_id=? AND day=?", (mid, day))
                    else:
                        weekday = int(data["weekday"])
                        if weekday not in range(7):
                            raise BookingError("Неверный день недели.")
                        if action == "dayoff_week":
                            conn.execute("DELETE FROM schedule WHERE master_id=? AND weekday=?", (mid, weekday))
                        else:
                            start = str(data["start"])
                            end = str(data["end"])
                            bs = str(data.get("break_start", ""))
                            be = str(data.get("break_end", ""))
                            for value in (start, end, bs, be):
                                if value:
                                    parsed = datetime.strptime(value, "%H:%M")
                                    if parsed.minute % 15 or parsed.strftime("%H:%M") != value:
                                        raise BookingError("Время должно иметь шаг 15 минут.")
                            if not start < end or bool(bs) != bool(be):
                                raise BookingError("Некорректное время работы или перерыва.")
                            if bs and not start <= bs < be <= end:
                                raise BookingError("Перерыв должен быть внутри рабочего дня.")
                            conn.execute("INSERT OR REPLACE INTO schedule VALUES (?,?,?,?,?,?)", (mid, weekday, start, end, bs or None, be or None))
                else:
                    raise HTTPException(404, "Действие не найдено")
        except (ValueError, KeyError) as exc:
            return HTMLResponse(escape(str(exc)) + '<p><a href="/admin">Назад</a></p>', status_code=400)
        return RedirectResponse("/admin/catalog", status_code=303)

    return app
