import sys
from pathlib import Path

# Добавяне на корена на commerce към sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.modules.catalog.models import Product

PRODUCT_TEMPLATES = [
    ("Wireless Ergonomic Mouse", 4999, 35, True),
    ("Mechanical Gaming Keyboard RGB", 11999, 20, True),
    ("USB-C to HDMI Cable 2m", 1599, 85, True),
    ("Noise-Cancelling Over-Ear Headphones", 18900, 15, True),
    ("Ultra-Wide 34-inch Curved Monitor", 44900, 8, True),
    ("Aluminum Laptop Stand", 2999, 50, True),
    ("HD Webcam 1080p with Microphone", 5499, 25, True),
    ("Smartwatch Fitness Tracker", 7999, 40, True),
    ("Portable Bluetooth Speaker 20W", 4500, 60, True),
    ("GaN Fast Charger 65W 3-Port", 3299, 70, True),
    ("External SSD 1TB USB 3.2", 9999, 30, True),
    ("USB 3.0 7-Port Powered Hub", 2799, 45, True),
    ("Gel Memory Foam Mouse Pad", 1299, 110, True),
    ("LED Desk Lamp with Wireless Charging", 3999, 22, True),
    ("Cat 6 Ethernet Cable 5m", 899, 150, True),
    ("Condenser USB Microphone with Stand", 6899, 18, True),
    ("Mesh Ergonomic Office Chair", 24900, 12, True),
    ("Electric Standing Desk 140x70cm", 38900, 6, True),
    ("Compact Wireless Number Pad", 1899, 35, True),
    ("Dual Monitor Mount Gas Spring Arm", 5999, 28, True),
    ("Discontinued Vintage Mouse", 1999, 0, False),  # Неактивен за тест
    ("Out of Stock Mechanical Numpad", 2499, 0, False),  # Неактивен за тест
]


def generate_100_products() -> list[dict[str, object]]:
    products_data: list[dict[str, object]] = []

    categories = [
        "Pro",
        "Plus",
        "Ultra",
        "Mini",
        "Elite",
        "Studio",
        "Core",
        "Prime",
        "Max",
        "Air",
    ]

    count = 0
    while count < 100:
        for cat in categories:
            for base_title, base_price, base_stock, active in PRODUCT_TEMPLATES:
                count += 1
                title = f"{base_title} {cat} v{count}"
                # Лека вариация в цената и наличността
                price = int(base_price + (count * 15))
                stock = int(base_stock + (count % 25))
                # Около 5% от продуктите оставяме неактивни за лесно тестване на филтрирането
                is_active = False if count % 20 == 0 else active

                products_data.append(
                    {
                        "title": title,
                        "price_minor": price,
                        "currency": "EUR",
                        "stock": stock,
                        "active": is_active,
                    }
                )
                if count >= 100:
                    break
            if count >= 100:
                break

    return products_data


def seed(session: Session) -> int:
    """Вмъква 100 продукта в базата данни, ако вече не са добавени."""
    # Синхронизиране на sequence-а предварително, тъй като id=1 е вмъкнат от миграция
    session.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('products', 'id'), "
            "coalesce(max(id), 1), max(id) IS NOT NULL) FROM products;"
        )
    )
    session.commit()

    existing_count = session.scalar(select(func.count(Product.id))) or 0
    print(f"Текущ брой продукти в базата: {existing_count}")

    products = generate_100_products()
    added = 0
    for data in products:
        p = Product(**data)
        session.add(p)
        added += 1

    session.commit()

    # Синхронизиране на sequence-а в PostgreSQL
    session.execute(
        text(
            "SELECT setval(pg_get_serial_sequence('products', 'id'), "
            "coalesce(max(id), 1), max(id) IS NOT NULL) FROM products;"
        )
    )
    session.commit()

    total_count = session.scalar(select(func.count(Product.id))) or 0
    print(f"Успешно добавени нови продукти: {added}")
    print(f"Общ брой продукти в базата след seeding: {total_count}")
    return added


if __name__ == "__main__":
    with SessionLocal() as db:
        seed(db)
