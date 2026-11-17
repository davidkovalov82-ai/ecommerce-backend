"""HTTP обработчици за публични маршрути на Cart в Commerce.

След K6.3 микросервисът Cart е самостоятелен собственик на потребителските
колички. Commerce запазва публичните си маршрути (/cart, /cart/items/{product_id})
и проксира заявките към Cart.
"""

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.modules.cart.client import CartClient, CartServiceUnavailableError
from app.modules.cart.dependencies import CartClientDep
from app.modules.cart.schemas import CartResponse

router = APIRouter(prefix="/cart", tags=["cart"])


async def _forward(
    request: Request,
    path: str,
    method: str,
    cart_client: CartClient,
    body: bytes | None = None,
) -> Response:
    """Пренасочва публична заявка към самостоятелния Cart микросервис."""
    headers = dict(request.headers)
    client_ip = request.client.host if request.client else None
    if client_ip:
        headers["x-forwarded-for"] = client_ip

    try:
        resp = cart_client.forward_request(
            method=method,
            path=path,
            content=body,
            headers=headers,
            params=dict(request.query_params),
        )
    except CartServiceUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "SERVICE_UNAVAILABLE",
                "message": "Cart service is unavailable",
            },
        )

    resp_headers = dict(resp.headers)
    resp_headers.pop("content-length", None)
    resp_headers.pop("transfer-encoding", None)

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=resp.headers.get("content-type"),
    )


@router.get("", response_model=CartResponse)
async def get_cart(
    request: Request,
    cart_client: CartClientDep,
) -> Response:
    """Извличане на потребителската количка (прокси към Cart)."""
    return await _forward(request, "/cart", "GET", cart_client)


@router.put("/items/{product_id}", response_model=CartResponse)
async def update_cart_item(
    product_id: int,
    request: Request,
    cart_client: CartClientDep,
) -> Response:
    """Задаване на количество за продукт в количката (прокси към Cart)."""
    body = await request.body()
    return await _forward(
        request, f"/cart/items/{product_id}", "PUT", cart_client, body=body
    )


@router.delete("/items/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_cart_item(
    product_id: int,
    request: Request,
    cart_client: CartClientDep,
) -> Response:
    """Премахване на продукт от количката (прокси към Cart)."""
    return await _forward(request, f"/cart/items/{product_id}", "DELETE", cart_client)
