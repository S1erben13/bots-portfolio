import logging
import re
import sqlite3
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


def validate_lead(name: str, phone: str) -> tuple[str, str]:
    name = name.strip()
    if not 2 <= len(name) <= 100 or any(ord(c) < 32 for c in name):
        raise ValueError("Имя должно содержать от 2 до 100 символов")
    if not re.fullmatch(r"[+\d\s()\-]{10,30}", phone):
        raise ValueError("Укажите корректный телефон")
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if not 10 <= len(digits) <= 15:
        raise ValueError("Телефон должен содержать от 10 до 15 цифр")
    return name, "+" + digits


class Leads:
    def __init__(self, path: Path, client: httpx.AsyncClient, token: str = "", chat_id: str = ""):
        self.path, self.client, self.token, self.chat_id = path, client, token, chat_id
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                notified INTEGER NOT NULL DEFAULT 0)""")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    async def create(self, name: str, phone: str) -> dict:
        name, phone = validate_lead(name, phone)
        with self.connect() as db:
            lead_id = db.execute("INSERT INTO leads(name, phone) VALUES (?, ?)", (name, phone)).lastrowid
        notified = False
        if self.token and self.chat_id:
            try:
                response = await self.client.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                                  json={"chat_id": self.chat_id,
                                                        "text": f"Заявка №{lead_id}\nИмя: {name}\nТелефон: {phone}"})
                response.raise_for_status()
                notified = response.json().get("ok") is True
            except Exception as exc:
                logger.warning("Уведомление заявки №%s не доставлено: %s", lead_id, type(exc).__name__)
        else:
            logger.info("Заявка №%s сохранена; Telegram не настроен", lead_id)
        if notified:
            with self.connect() as db:
                db.execute("UPDATE leads SET notified=1 WHERE id=?", (lead_id,))
        return {"id": lead_id, "notified": notified, "message": "Заявка сохранена. Менеджер свяжется с вами."}
