import asyncio
import logging
import time
from app.crm import RemoteError, request

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings, client):
        self.settings = settings
        self.client = client

    async def send(self, lead, result):
        if not self.settings.telegram_token:
            logger.info('Демо-уведомление: источник=%s, сделка=%s', lead['source'], result['deal_id'])
            return
        data = await request(self.client, 'POST', f'https://api.telegram.org/bot{self.settings.telegram_token}/sendMessage', json={
            'chat_id': self.settings.telegram_chat_id,
            'text': f"Новая заявка: {lead['name']}\n{lead['phone']} {lead['email']}\nИсточник: {lead['source']}\nСделка: {result['deal_id']}\n{lead['comment']}"[:4000]})
        if not data.get('ok'):
            raise RemoteError('Telegram отклонил уведомление')


class Worker:
    def __init__(self, db, crm, notifier, settings):
        self.db, self.crm, self.notifier, self.settings = db, crm, notifier, settings

    async def run_once(self):
        job = self.db.claim()
        if not job:
            return False
        state = job['state']
        state['job_key'] = job['key']
        def checkpoint(value):
            self.db.save(job['id'], state=value)
        try:
            if not state.get('crm_done'):
                state = await self.crm.deliver(job['lead'], state, checkpoint)
                state['crm_done'] = True
                checkpoint(state)
            if not state.get('notified'):
                await self.notifier.send(job['lead'], state)
                state['notified'] = True
            self.db.save(job['id'], state=state, status='sent', error='', lease_until=0)
        except Exception as exc:
            retryable = getattr(exc, 'retryable', True)
            failed = not retryable or job['attempts'] >= self.settings.max_attempts
            # Не сохраняем произвольный текст исключения: HTTP URL может содержать токен.
            error = str(exc) if isinstance(exc, RemoteError) else f'Ошибка обработки: {type(exc).__name__}'
            self.db.save(job['id'], state=state, status='failed' if failed else 'pending', error=error,
                         next_at=time.time() + min(3600, self.settings.retry_base_seconds * 2 ** min(job['attempts'] - 1, 20)), lease_until=0)
        return True

    async def run(self):
        while True:
            try:
                worked = await self.run_once()
            except Exception:
                logger.error('Ошибка доступа к очереди; повтор после паузы')
                worked = False
            if not worked:
                await asyncio.sleep(self.settings.poll_seconds)
