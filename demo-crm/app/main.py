import asyncio
import hashlib
import html
import json
import logging
import secrets
from contextlib import asynccontextmanager, suppress
from urllib.parse import parse_qs
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from app.config import Settings
from app.crm import AmoCRM, BitrixCRM, FakeCRM
from app.db import Database
from app.normalize import normalize
from app.worker import Notifier, Worker

logging.basicConfig(level=logging.INFO)
# HTTP-клиент не должен писать URL Telegram/Битрикс24 с токенами в INFO-лог.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)


def create_app(settings=None, start_worker=True):
    settings = settings or Settings.from_env()
    settings.validate()
    db = Database(settings.db_path)
    db.init()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            crm = {'fake': lambda: FakeCRM(db), 'amo': lambda: AmoCRM(settings, client),
                   'bitrix': lambda: BitrixCRM(settings, client)}[settings.crm_provider]()
            worker = Worker(db, crm, Notifier(settings, client), settings)
            app.state.worker = worker
            task = asyncio.create_task(worker.run()) if start_worker else None
            try:
                yield
            finally:
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(title='Заявки → CRM', lifespan=lifespan)
    app.state.db = db
    security = HTTPBasic()

    def auth(credentials: HTTPBasicCredentials = Depends(security)):
        user_ok = secrets.compare_digest(credentials.username.encode(), settings.admin_user.encode())
        password_ok = secrets.compare_digest(credentials.password.encode(), settings.admin_password.encode())
        if not (user_ok and password_ok):
            raise HTTPException(401, 'Неверный логин или пароль', headers={'WWW-Authenticate': 'Basic'})

    def csrf_token(id):
        import hmac
        return hmac.new(settings.admin_password.encode(), f'retry:{id}'.encode(), hashlib.sha256).hexdigest()

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'provider': settings.crm_provider}

    @app.post('/hooks/{source}', status_code=202)
    async def hook(source: str, request: Request):
        if source not in {'tilda', 'getcourse', 'generic'}:
            raise HTTPException(404, 'Неизвестный источник')
        secret = request.headers.get('X-Hook-Secret') or request.query_params.get('secret', '')
        if not secrets.compare_digest(secret.encode(), settings.hook_secret.encode()):
            raise HTTPException(403, 'Неверный секрет')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, 'Максимум 64 КиБ')
        content_type = request.headers.get('content-type', '').split(';')[0].strip().lower()
        try:
            if content_type == 'application/json':
                payload = json.loads(body)
            elif content_type == 'application/x-www-form-urlencoded':
                payload = {k: v[-1] for k, v in parse_qs(body.decode('utf-8'), keep_blank_values=True).items()}
            else:
                raise HTTPException(415, 'Нужен JSON или form-urlencoded')
            if not isinstance(payload, dict):
                raise ValueError('Нужен объект')
            lead = normalize(payload, source)
        except (ValueError, UnicodeError):
            raise HTTPException(422, 'Проверьте формат, телефон и email') from None
        key = request.headers.get('Idempotency-Key') or payload.get('event_id') or payload.get('request_id')
        if key is not None and (not isinstance(key, (str, int)) or len(str(key)) > 200):
            raise HTTPException(422, 'Некорректный ключ идемпотентности')
        # Без ключа одинаковые нормализованные заявки считаются повтором навсегда.
        key = str(key) if key else hashlib.sha256(json.dumps(lead, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        try:
            id, duplicate = db.enqueue(f'{source}:{key}', lead)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {'id': id, 'duplicate': duplicate, 'status': db.get(id)['status']}

    @app.get('/admin', response_class=HTMLResponse, dependencies=[Depends(auth)])
    async def admin():
        escape = lambda value: html.escape(str(value), quote=True)
        rows = []
        for job in db.latest():
            lead, state = job['lead'], job['state']
            id = job['id']
            retry = (f'<form method="post" action="/admin/retry/{id}"><input type="hidden" name="csrf" value="{csrf_token(id)}"><button>Повторить</button></form>') if job['status'] == 'failed' else ''
            cells = [id, lead['name'], lead['phone'], lead['email'], lead['source'], job['status'],
                     'доставлено' if state.get('crm_done') else 'ожидает',
                     'отправлено' if state.get('notified') else 'ожидает', job['attempts'], job['error']]
            rows.append('<tr>' + ''.join(f'<td>{escape(cell)}</td>' for cell in cells) + f'<td>{retry}</td></tr>')
        return '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Заявки → CRM</title><style>body{font:15px system-ui;margin:30px;background:#f6f8fb;color:#172333}table{border-collapse:collapse;background:white;width:100%}td,th{padding:10px;border:1px solid #d8dfeb;text-align:left}button{padding:8px;cursor:pointer}.scroll{overflow:auto}</style>
<h1>Заявки → CRM</h1><p>Последние 100 заявок. Обновите страницу для актуального статуса. Повтор доступен после ошибки.</p>
<div class="scroll"><table><tr>''' + ''.join(f'<th>{label}</th>' for label in ['№', 'Имя', 'Телефон', 'Email', 'Источник', 'Статус', 'CRM', 'Telegram', 'Попытки', 'Ошибка', 'Действие']) + '</tr>' + ''.join(rows) + '</table></div></html>'

    @app.post('/admin/retry/{id}', dependencies=[Depends(auth)])
    async def retry(id: int, request: Request):
        body = await request.body()
        if len(body) > 2048:
            raise HTTPException(413)
        token = parse_qs(body.decode()).get('csrf', [''])[0]
        if not secrets.compare_digest(token.encode(), csrf_token(id).encode()):
            raise HTTPException(403, 'Неверный CSRF-токен')
        if not db.get(id):
            raise HTTPException(404, 'Заявка не найдена')
        if not db.retry(id):
            raise HTTPException(409, 'Повтор доступен только после ошибки')
        return RedirectResponse('/admin', status_code=303)

    return app


app = create_app()
