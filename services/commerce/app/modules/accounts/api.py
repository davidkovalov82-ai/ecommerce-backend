"""HTTP обработчици за акаунти и автентикация в Commerce.

След K6.1 микросервисът Users е единствен собственик на потребителските акаунти,
пароли, профили и модерация. Commerce запазва публичните си URL адреси и проксира
тези заявки към Users, запазвайки HTTP методите, статус кодовете и телата.

Маршрутът /users/me/products се обслужва директно от Catalog в Commerce.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.modules.accounts.dependencies import (
    CurrentUserDep,
    get_users_client,
)
from app.modules.accounts.schemas import (
    ChangePasswordRequest,
    ModerationActionResponse,
    ModerationRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    PasswordResetRequestResponse,
    TokenResponse,
    UserLoginRequest,
    UserRegisterRequest,
    UserResponse,
    UserUpdateRequest,
)
from app.modules.accounts.users_client import (
    UsersClient,
    UsersServiceUnavailableError,
)
from app.modules.catalog.dependencies import CatalogServiceDep
from app.modules.catalog.schemas import ProductListResponse, ProductResponse

router = APIRouter(prefix="/users", tags=["accounts"])
auth_router = APIRouter(prefix="/auth", tags=["auth"])


def _forward(
    request: Request,
    path: str,
    method: str,
    users_client: UsersClient,
    body: bytes | None = None,
) -> Response:
    """Помощна функция за пренасочване на заявка към Users сервиса."""
    headers = dict(request.headers)
    client_ip = request.client.host if request.client else None
    if client_ip:
        headers["x-forwarded-for"] = client_ip

    try:
        resp = users_client.forward_request(
            method=method,
            path=path,
            content=body,
            headers=headers,
            params=dict(request.query_params) if request.query_params else None,
        )
    except UsersServiceUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "USERS_SERVICE_UNAVAILABLE",
                "message": "Users service is unavailable",
            },
        )

    content_type = resp.headers.get("content-type", "application/json")
    hop_by_hop = {
        "content-length",
        "content-encoding",
        "transfer-encoding",
        "connection",
    }
    response_headers = {
        k: v for k, v in resp.headers.items() if k.lower() not in hop_by_hop
    }
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=response_headers,
        media_type=content_type,
    )


# --- Публични Auth маршрути ---


@auth_router.post("/login", response_model=TokenResponse)
async def login(
    body: UserLoginRequest,
    request: Request,
    users_client: Annotated[UsersClient, Depends(get_users_client)],
) -> Response:
    """Вход на потребител (проксира се към Users)."""
    return _forward(
        request,
        "/auth/login",
        "POST",
        users_client,
        body.model_dump_json().encode("utf-8"),
    )


@auth_router.post(
    "/password-reset/request",
    response_model=PasswordResetRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_password_reset(
    body: PasswordResetRequest,
    request: Request,
    users_client: Annotated[UsersClient, Depends(get_users_client)],
) -> Response:
    """Заявка за възстановяване на парола (проксира се към Users)."""
    return _forward(
        request,
        "/auth/password-reset/request",
        "POST",
        users_client,
        body.model_dump_json().encode("utf-8"),
    )


@auth_router.post(
    "/password-reset/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def confirm_password_reset(
    body: PasswordResetConfirmRequest,
    request: Request,
    users_client: Annotated[UsersClient, Depends(get_users_client)],
) -> Response:
    """Потвърждаване на възстановяване на парола (проксира се към Users)."""
    return _forward(
        request,
        "/auth/password-reset/confirm",
        "POST",
        users_client,
        body.model_dump_json().encode("utf-8"),
    )


# --- Публични Users маршрути ---


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: UserRegisterRequest,
    request: Request,
    users_client: Annotated[UsersClient, Depends(get_users_client)],
) -> Response:
    """Регистрация на нов потребител (проксира се към Users)."""
    return _forward(
        request, "/users", "POST", users_client, body.model_dump_json().encode("utf-8")
    )


@router.get("/me", response_model=UserResponse)
async def get_me(
    request: Request,
    users_client: Annotated[UsersClient, Depends(get_users_client)],
) -> Response:
    """Преглед на профил на текущия потребител (проксира се към Users)."""
    return _forward(request, "/users/me", "GET", users_client)


@router.patch("/me", response_model=UserResponse)
async def update_me(
    body: UserUpdateRequest,
    request: Request,
    users_client: Annotated[UsersClient, Depends(get_users_client)],
) -> Response:
    """Редакция на профил на текущия потребител (проксира се към Users)."""
    return _forward(
        request,
        "/users/me",
        "PATCH",
        users_client,
        body.model_dump_json().encode("utf-8"),
    )


@router.post("/me/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    users_client: Annotated[UsersClient, Depends(get_users_client)],
) -> Response:
    """Смяна на парола на текущия потребител (проксира се към Users)."""
    return _forward(
        request,
        "/users/me/change-password",
        "POST",
        users_client,
        body.model_dump_json().encode("utf-8"),
    )


@router.get("/me/products", response_model=ProductListResponse)
def get_my_products(
    current_user: CurrentUserDep,
    catalog_service: CatalogServiceDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ProductListResponse:
    """Извличане на собствените продукти на продавача (управлява се от Catalog в Commerce)."""
    products = catalog_service.list_products_by_seller(
        seller_id=current_user.id,
        limit=limit,
        offset=offset,
    )
    return ProductListResponse(
        items=[ProductResponse.model_validate(p) for p in products],
        limit=limit,
        offset=offset,
    )


# --- Административна модерация ---


@router.post("/{user_id}/block", response_model=ModerationActionResponse)
async def block_user(
    user_id: int,
    request: Request,
    users_client: Annotated[UsersClient, Depends(get_users_client)],
    body: ModerationRequest | None = None,
) -> Response:
    """Блокиране на потребител от администратор (проксира се към Users)."""
    content = body.model_dump_json().encode("utf-8") if body else None
    return _forward(request, f"/users/{user_id}/block", "POST", users_client, content)


@router.post("/{user_id}/unblock", response_model=ModerationActionResponse)
async def unblock_user(
    user_id: int,
    request: Request,
    users_client: Annotated[UsersClient, Depends(get_users_client)],
    body: ModerationRequest | None = None,
) -> Response:
    """Отблокиране на потребител от администратор (проксира се към Users)."""
    content = body.model_dump_json().encode("utf-8") if body else None
    return _forward(request, f"/users/{user_id}/unblock", "POST", users_client, content)


@router.post("/{user_id}/close", response_model=ModerationActionResponse)
async def close_user(
    user_id: int,
    request: Request,
    users_client: Annotated[UsersClient, Depends(get_users_client)],
    body: ModerationRequest | None = None,
) -> Response:
    """Окончателно закриване на потребител от администратор (проксира се към Users)."""
    content = body.model_dump_json().encode("utf-8") if body else None
    return _forward(request, f"/users/{user_id}/close", "POST", users_client, content)
