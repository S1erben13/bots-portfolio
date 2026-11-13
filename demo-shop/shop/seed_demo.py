from shop.config import Settings
from shop.db import connect, init_db


def seed_demo(config):
    init_db(config.db_path)
    with connect(config.db_path) as conn:
        conn.executemany("INSERT OR IGNORE INTO products(id,name,description,price,file) VALUES (?,?,?,?,?)", [
            (1, "Набор файлов для бригады видеонаблюдения v1.1", "ДЕМО: ZIP с README, не настоящий набор.", 79000, "cctv-kit-demo.zip"),
            (2, "Чек-лист приёмки видеонаблюдения", "ДЕМО: текстовая заглушка.", 19000, "checklist-demo.txt")])


if __name__ == "__main__":
    seed_demo(Settings())
