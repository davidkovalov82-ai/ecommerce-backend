from typing import Any

# Временни данни в паметта за етап ECOM-01
_PRODUCTS: dict[int, dict[str, Any]] = {
    1: {
        "id": 1,
        "title": "Notebook",
        "price_minor": 1299,
        "currency": "EUR",
        "stock": 10,
        "active": True,
    }
}


def get_by_id(product_id: int) -> dict[str, Any] | None:
    """Извлича продукт по неговото ID от паметта."""
    return _PRODUCTS.get(product_id)
