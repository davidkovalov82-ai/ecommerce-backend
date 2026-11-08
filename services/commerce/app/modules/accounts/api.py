from fastapi import APIRouter, HTTPException, Query, status

from app.config import settings
from app.modules.accounts.dependencies import AccountServiceDep, CurrentUserDep
from app.modules.accounts.schemas import (
    TokenResponse,
    UserLoginRequest,
    UserRegisterRequest,
    UserResponse,
)
from app.modules.accounts.service import EmailAlreadyExists, InvalidCredentials
from app.modules.accounts.tokens import create_access_token
from app.modules.catalog.dependencies import CatalogServiceDep
from app.modules.catalog.schemas import ProductListResponse, ProductResponse

router = APIRouter(prefix="/users", tags=["accounts"])
auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post("/login", response_model=TokenResponse)
def login(
    request: UserLoginRequest,
    account_service: AccountServiceDep,
) -> TokenResponse:
    """HTTP обработчик за вход на потребител и издаване на JWT токен."""
    try:
        user = account_service.authenticate_user(
            email=request.email,
            password=request.password,
        )
    except InvalidCredentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "INVALID_CREDENTIALS",
                "message": "Invalid email or password",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(
        user_id=user.id,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expire_minutes=settings.jwt_access_token_expire_minutes,
    )
    return TokenResponse(access_token=access_token, token_type="bearer")


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(
    request: UserRegisterRequest,
    account_service: AccountServiceDep,
) -> UserResponse:
    """HTTP обработчик за регистрация на нов потребител (винаги customer)."""
    try:
        user = account_service.register_user(
            email=request.email,
            password=request.password,
        )
        return UserResponse.model_validate(user)
    except EmailAlreadyExists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "EMAIL_ALREADY_EXISTS",
                "message": "User with this email already exists",
            },
        )


@router.get("/me", response_model=UserResponse)
def get_me(
    current_user: CurrentUserDep,
) -> UserResponse:
    """HTTP обработчик за извличане на данните на текущия автентикиран потребител."""
    return UserResponse.model_validate(current_user)


@router.get("/me/products", response_model=ProductListResponse)
def get_my_products(
    current_user: CurrentUserDep,
    catalog_service: CatalogServiceDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ProductListResponse:
    """HTTP обработчик за получаване на собствените продукти на потребителя (вкл. неактивни)."""
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
