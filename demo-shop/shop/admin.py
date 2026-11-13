import asyncio
import csv
import io
import secrets
from datetime import date, datetime, time, timedelta, timezone
from html import escape

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from shop.db import connect
from shop.payments import rub
from shop.service import ShopError


def create_app(shop):
    app = FastAPI(title="Магазин цифровых товаров", docs_url=None, redoc_url=None, openapi_url=None)
    basic = HTTPBasic()
    csrf = secrets.token_urlsafe(32)

    def auth(credentials: HTTPBasicCredentials = Depends(basic)):
        valid_user = secrets.compare_digest(credentials.username.encode(), shop.config.admin_user.encode())
        valid_password = secrets.compare_digest(credentials.password.encode(), shop.config.admin_password.encode())
        if not shop.config.admin_password or not (valid_user and valid_password):
            raise HTTPException(401, "Требуется авторизация", headers={"WWW-Authenticate": "Basic"})

    @app.exception_handler(ShopError)
    async def invalid(request, exc):
        return HTMLResponse(escape(str(exc)), status_code=400)

    def field(name, value="", kind="text"):
        return f'<input name="{name}" type="{kind}" value="{escape(str(value), quote=True)}" aria-label="{name}">'

    def form(action, fields, label="Сохранить"):
        return f'<form method="post" action="/admin/{action}">{field("csrf", csrf, "hidden")}{fields}<button>{label}</button></form>'

    def page(body):
        return HTMLResponse('''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Магазин — админка</title>
<style>body{font:16px system-ui;max-width:1200px;margin:32px auto;padding:0 16px;color:#16312e;background:#f6f8f7}
table{border-collapse:collapse;background:white;width:100%}td,th{padding:10px;border:1px solid #ccd8d4;text-align:left}
input,button,select{font:inherit;padding:7px;margin:4px;max-width:95%}a{color:#126b55}form{display:inline-block}.scroll{overflow:auto}</style>
<h1>Цифровой магазин</h1><nav><a href="/admin">Заказы</a> · <a href="/admin/catalog">Товары</a> · <a href="/admin/export">CSV</a></nav>'''
                            + body + '</html>', headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/d/{token}")
    def download(token: str):
        try:
            path = shop.download(token)
        except ShopError:
            raise HTTPException(404, "Ссылка недействительна") from None
        return FileResponse(path, filename=path.name, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.post("/hooks/yookassa")
    async def webhook(request: Request):
        try:
            body = await request.json()
            await shop.webhook(body)
        except ValueError:
            raise HTTPException(400, "Платёж не подтверждён") from None
        return {"ok": True}

    @app.get("/admin", dependencies=[Depends(auth)])
    def index(status: str = "", start: date | None = None, end: date | None = None):
        start = start or datetime.now(timezone.utc).date().replace(day=1)
        end = end or datetime.now(timezone.utc).date()
        first = datetime.combine(start, time.min, timezone.utc).timestamp()
        last = datetime.combine(end + timedelta(days=1), time.min, timezone.utc).timestamp()
        total = shop.revenue(first, last)
        body = f'<h2>Выручка: {rub(total)} ₽</h2><p>Период включительно, UTC</p><form>{field("start", start, "date")}{field("end", end, "date")}{field("status", status)}<button>Показать</button></form>'
        body += '<p>Статусы: new, pending, paid, delivered, canceled. Пустое поле — все.</p><div class="scroll"><table><tr><th>№</th><th>Покупатель</th><th>Товар</th><th>Сумма</th><th>Статус</th><th>Действие</th></tr>'
        for row in shop.orders(status=status):
            action = form("redeliver", field("id", row["id"], "hidden"), "Выдать снова") if row["status"] in {"paid", "delivered"} else ""
            body += f'<tr><td>{row["id"]}</td><td>{row["user_id"]}</td><td>{escape(row["name"])}</td><td>{rub(row["amount"])}</td><td>{row["status"]}</td><td>{action}</td></tr>'
        return page(body + '</table></div>')

    @app.get("/admin/catalog", dependencies=[Depends(auth)])
    def catalog():
        body = '<h2>Товары</h2><p>Цена в копейках, файл относительно FILES_ROOT. Активен: 1 или 0.</p>'
        for row in shop.products(active=False) + [dict(id="", name="", description="", price=100, file="", active=1)]:
            fields = field("id", row["id"], "hidden")
            fields += ''.join(field(k, row[k], "number" if k in {"price", "active"} else "text") for k in ("name", "description", "price", "file", "active"))
            body += '<div>' + form("product", fields, "Сохранить" if row["id"] else "Добавить") + '</div>'
        return page(body)

    @app.get("/admin/export", dependencies=[Depends(auth)])
    def export(status: str = ""):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["id", "user_id", "product", "amount_kopecks", "status", "created_at", "paid_at"])
        for row in shop.orders(status=status):
            # Защита от формул при открытии CSV в Excel.
            name = row["name"]
            if name.lstrip().startswith(("=", "+", "-", "@")) or name.startswith(("\t", "\r", "\n")):
                name = "'" + name
            writer.writerow([row["id"], row["user_id"], name, row["amount"], row["status"], row["created_at"], row["paid_at"]])
        return Response("\ufeff" + stream.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="orders.csv"', "Cache-Control": "no-store"})

    @app.post("/admin/{action}", dependencies=[Depends(auth)])
    async def mutate(action: str, request: Request):
        data = await request.form()
        if not secrets.compare_digest(str(data.get("csrf", "")).encode(), csrf.encode()):
            raise HTTPException(403, "Обновите страницу")
        try:
            if action == "redeliver":
                await shop.redeliver(int(data["id"]))
                return RedirectResponse("/admin", status_code=303)
            if action != "product":
                raise HTTPException(404, "Действие не найдено")
            name, description = str(data["name"]).strip(), str(data["description"]).strip()
            price, active = int(data["price"]), int(data["active"])
            filename = str(data["file"])
            if not 2 <= len(name) <= 128 or len(description) > 2000 or not 0 < price <= 100000000 or active not in {0, 1}:
                raise ShopError("Проверьте поля товара")
            shop.file_path(filename)
            def save():
                with connect(shop.config.db_path) as conn:
                    if data.get("id"):
                        changed = conn.execute("UPDATE products SET name=?,description=?,price=?,file=?,active=? WHERE id=?", (name, description, price, filename, active, int(data["id"]))).rowcount
                        if not changed:
                            raise ShopError("Товар не найден")
                    else:
                        conn.execute("INSERT INTO products(name,description,price,file,active) VALUES (?,?,?,?,?)", (name, description, price, filename, active))
            await asyncio.to_thread(save)
        except (ValueError, KeyError):
            raise HTTPException(400, "Проверьте поля формы") from None
        return RedirectResponse("/admin/catalog", status_code=303)

    return app
