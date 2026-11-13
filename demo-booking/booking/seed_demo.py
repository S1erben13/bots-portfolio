from booking.config import settings
from booking.db import connect, init_db


def seed_demo(config=settings):
    init_db(config.db_path)
    with connect(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM services LIMIT 1").fetchone():
            return
        conn.executemany("INSERT INTO services(id,name,duration) VALUES (?,?,?)", [
            (1, "Стрижка", 45), (2, "Укладка", 30),
            (3, "Окрашивание", 120), (4, "Уход", 60),
        ])
        conn.executemany("INSERT INTO masters(id,name) VALUES (?,?)", [(1, "Анна"), (2, "Мария")])
        for mid in (1, 2):
            for weekday in range(6):
                conn.execute("INSERT INTO schedule VALUES (?,?,?,?,?,?)", (mid, weekday, "09:00", "18:00", "13:00", "14:00"))


if __name__ == "__main__":
    seed_demo()
    print("Демо-данные созданы. Повторный запуск безопасен.")
