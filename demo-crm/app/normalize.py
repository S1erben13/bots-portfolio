import re


def normalize_phone(value):
    digits = re.sub(r'\D', '', str(value or ''))
    if not digits:
        return ''
    if len(digits) == 10:
        digits = '7' + digits
    if len(digits) == 11 and digits[0] in '78':
        return '+7' + digits[1:]
    raise ValueError('Телефон должен содержать 10 цифр либо 11 с префиксом 7/8')


def normalize(payload, source):
    data = {str(k).lower(): v for k, v in payload.items()}
    def pick(*keys):
        for key in keys:
            value = data.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()[:4000]
        return ''
    phone = normalize_phone(pick('phone', 'telephone', 'телефон', 'formparams[phone]'))
    email = pick('email', 'e-mail', 'formparams[email]').lower()
    if email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise ValueError('Некорректный email')
    if not phone and not email:
        raise ValueError('Нужен телефон или email')
    utm = {str(k): str(v)[:1000] for k, v in data.get('utm', {}).items()} if isinstance(data.get('utm'), dict) else {}
    utm.update({k: str(v)[:1000] for k, v in data.items() if k.startswith('utm_')})
    return {'name': pick('name', 'имя', 'formparams[name]') or 'Без имени', 'phone': phone,
            'email': email, 'source': (pick('source') or source) if source == 'generic' else source, 'utm': utm,
            'comment': pick('comment', 'comments', 'message', 'комментарий')}

