import json
from typing import Protocol
import httpx


class CRM(Protocol):
    async def deliver(self, lead, state, checkpoint): ...


class RemoteError(Exception):
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


async def request(client, method, url, **kwargs):
    # Не включаем URL и тело ответа в ошибки: там могут быть секреты/персональные данные.
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TransportError:
        raise RemoteError('Транспортная ошибка CRM; результат запроса может быть неизвестен') from None
    if response.status_code >= 400:
        raise RemoteError(f'CRM HTTP {response.status_code}', response.status_code == 429 or response.status_code >= 500)
    if response.status_code == 204:
        return {}
    try:
        return response.json()
    except ValueError:
        raise RemoteError('Некорректный JSON CRM') from None


def description(lead):
    return f"Источник: {lead['source']}\n{lead['comment']}\nUTM: {json.dumps(lead['utm'], ensure_ascii=False)}"


class FakeCRM:
    def __init__(self, db):
        self.db = db

    async def deliver(self, lead, state, checkpoint=lambda state: None):
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            deal = c.execute('SELECT id,contact_id FROM deals WHERE job_key=?', (state['job_key'],)).fetchone()
            if not deal:
                contact = c.execute("SELECT id FROM contacts WHERE (phone<>'' AND phone=?) OR (email<>'' AND email=?) ORDER BY id LIMIT 1", (lead['phone'], lead['email'])).fetchone()
                contact_id = contact['id'] if contact else c.execute('INSERT INTO contacts(name,phone,email) VALUES(?,?,?)', (lead['name'], lead['phone'], lead['email'])).lastrowid
                deal_id = c.execute('INSERT INTO deals(job_key,contact_id,lead) VALUES(?,?,?)', (state['job_key'], contact_id, json.dumps(lead))).lastrowid
            else:
                deal_id, contact_id = deal['id'], deal['contact_id']
        state.update(contact_id=contact_id, deal_id=deal_id, note_done=True)
        checkpoint(state)
        return state


class AmoCRM:
    def __init__(self, settings, client):
        self.base = settings.amo_base_url.rstrip('/')
        self.client = client
        self.headers = {'Authorization': f'Bearer {settings.amo_token}'}

    async def call(self, method, path, **kwargs):
        return await request(self.client, method, self.base + path, headers=self.headers, **kwargs)

    async def find_contact(self, lead):
        for code, value in [('PHONE', lead['phone']), ('EMAIL', lead['email'])]:
            if not value:
                continue
            page = 1
            while True:
                data = await self.call('GET', '/api/v4/contacts', params={'query': value, 'limit': 250, 'page': page})
                for contact in data.get('_embedded', {}).get('contacts', []):
                    for field in contact.get('custom_fields_values', []):
                        if field.get('field_code') != code:
                            continue
                        for item in field.get('values', []):
                            actual = str(item.get('value', ''))
                            if code == 'PHONE':
                                from app.normalize import normalize_phone
                                try:
                                    actual = normalize_phone(actual)
                                except ValueError:
                                    continue
                            if actual.lower() == value.lower():
                                return contact['id']
                if not data.get('_links', {}).get('next'):
                    break
                page += 1
        return None

    async def deliver(self, lead, state, checkpoint=lambda state: None):
        if not state.get('deal_id'):
            contact_id = state.get('contact_id') or await self.find_contact(lead)
            contact = {'id': contact_id} if contact_id else {
                'name': lead['name'], 'custom_fields_values': [
                    {'field_code': code, 'values': [{'value': value, 'enum_code': 'WORK'}]}
                    for code, value in [('PHONE', lead['phone']), ('EMAIL', lead['email'])] if value]}
            result = await self.call('POST', '/api/v4/leads/complex', json=[{
                'name': f"Заявка: {lead['name']}", 'request_id': state['job_key'],
                '_embedded': {'contacts': [contact]}}])
            item = result[0]
            state.update(deal_id=item['id'], contact_id=contact_id or item['contact_id'])
            checkpoint(state)
        if not state.get('note_done'):
            await self.call('POST', f"/api/v4/leads/{state['deal_id']}/notes", json=[{
                'note_type': 'common', 'params': {'text': description(lead)}}])
            state['note_done'] = True
            checkpoint(state)
        return state


class BitrixCRM:
    def __init__(self, settings, client):
        self.base = settings.bitrix_webhook_url.rstrip('/')
        self.client = client

    async def call(self, method, payload):
        data = await request(self.client, 'POST', f'{self.base}/{method}.json', json=payload)
        if data.get('error'):
            code = str(data['error'])
            raise RemoteError('Ошибка API Битрикс24', code in {'QUERY_LIMIT_EXCEEDED', 'OPERATION_TIME_LIMIT', 'INTERNAL_SERVER_ERROR'})
        return data['result']

    async def deliver(self, lead, state, checkpoint=lambda state: None):
        if not state.get('contact_id'):
            contact_id = None
            for kind, value in [('PHONE', lead['phone']), ('EMAIL', lead['email'])]:
                if value:
                    result = await self.call('crm.duplicate.findbycomm', {'entity_type': 'CONTACT', 'type': kind, 'values': [value]})
                    if isinstance(result, dict) and result.get('CONTACT'):
                        contact_id = result['CONTACT'][0]
                        break
            if not contact_id:
                contact_id = await self.call('crm.contact.add', {'fields': {
                    'NAME': lead['name'], **{kind: [{'VALUE': value, 'VALUE_TYPE': 'WORK'}]
                    for kind, value in [('PHONE', lead['phone']), ('EMAIL', lead['email'])] if value}}})
            state['contact_id'] = contact_id
            checkpoint(state)
        if not state.get('deal_id'):
            state['deal_id'] = await self.call('crm.deal.add', {'fields': {
                'TITLE': f"Заявка: {lead['name']}", 'CONTACT_ID': state['contact_id'],
                'COMMENTS': description(lead), **{k.upper(): v for k, v in lead['utm'].items()
                    if k in {'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'}}}})
            state['note_done'] = True
            checkpoint(state)
        return state

