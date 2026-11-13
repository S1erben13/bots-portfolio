"""Пять синтетических вебхуков; не требует аккаунтов CRM."""

import os
import uuid

import httpx


def main():
    base_url = os.getenv("DEMO_URL", "http://localhost:8000").rstrip("/")
    secret = os.getenv("HOOK_SECRET", "demo-secret-change-me")
    run = uuid.uuid4().hex
    first = {"Name": "Анна Демо", "Phone": "8 (999) 123-45-67",
             "Email": "anna@example.test", "utm_source": "portfolio",
             "Comments": "Нужна консультация"}
    events = [
        ("tilda", "first", first, False),
        ("getcourse", "second", {"name": "Анна Демо", "phone": "+79991234567",
                                  "email": "anna@example.test", "comment": "Другой курс"}, True),
        ("generic", "third", {"name": "Борис Демо", "phone": "9997654321",
                                "source": "telegram", "comment": "Заявка из бота"}, False),
        ("generic", "fourth", {"name": "Вера Демо", "email": "vera@example.test",
                                 "source": "salebot", "utm_campaign": "demo"}, True),
        ("tilda", "first", first, False),
    ]
    with httpx.Client(timeout=20) as client:
        for source, key, payload, as_form in events:
            response = client.post(
                f"{base_url}/hooks/{source}",
                headers={"X-Hook-Secret": secret, "Idempotency-Key": f"{run}-{key}"},
                **({"data": payload} if as_form else {"json": payload}),
            )
            response.raise_for_status()
            print(source, key, response.json())
    print("Отправлено 5 запросов: 4 новые заявки, последний запрос — повтор первого.")
    print("Проверьте /admin после обработки очереди. У Анны один контакт и две сделки.")


if __name__ == "__main__":
    main()
