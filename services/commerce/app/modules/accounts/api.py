from fastapi import APIRouter, HTTPException, Query, Request, status

from app.config import settings
from app.modules.accounts.dependencies import (
    AccountServiceDep,
    AdminUserDep,
    CurrentUserDep,
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
from app.modules.accounts.service import (
    AccountBlocked,
    AccountClosed,
    EmailAlreadyExists,
    InvalidAccountState,
    InvalidCredentials,
    InvalidCurrentPassword,
    InvalidResetToken,
    RateLimitExceeded,
    SamePasswordError,
    SelfModerationForbidden,
    TechnicalAccountProtected,
    UserNotFound,
)
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
    except AccountBlocked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "ACCOUNT_BLOCKED",
                "message": "Account is blocked",
            },
        )
    except AccountClosed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "ACCOUNT_CLOSED",
                "message": "Account is closed",
            },
        )

    access_token = create_access_token(
        user_id=user.id,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expire_minutes=settings.jwt_access_token_expire_minutes,
        token_version=user.token_version,
    )
    return TokenResponse(access_token=access_token, token_type="bearer")


@auth_router.post(
    "/password-reset/request",
    response_model=PasswordResetRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_password_reset(
    body: PasswordResetRequest,
    request: Request,
    account_service: AccountServiceDep,
) -> PasswordResetRequestResponse:
    """HTTP обработчик за заявяване на линк/токен за възстановяване на парола.

    Връща еднакъв статус 202 и тяло за съществуващи, несъществуващи и неактивни акаунти,
    за да предотврати enumeration атаки.
    """
    client_ip = request.client.host if request.client else None
    try:
        account_service.request_password_reset(email=body.email, client_ip=client_ip)
    except RateLimitExceeded:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "Too many password reset requests. Please try again later.",
            },
        )

    return PasswordResetRequestResponse(
        message="If the account exists and is eligible, password reset instructions have been sent."
    )


@auth_router.post(
    "/password-reset/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
)
def confirm_password_reset(
    body: PasswordResetConfirmRequest,
    account_service: AccountServiceDep,
) -> None:
    """HTTP обработчик за потвърждаване на нова парола с еднократен токен."""
    try:
        account_service.confirm_password_reset(
            token=body.token,
            new_password=body.new_password,
        )
    except InvalidResetToken:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_RESET_TOKEN",
                "message": "Invalid or expired password reset token",
            },
        )


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


@router.patch("/me", response_model=UserResponse)
def update_me(
    request: UserUpdateRequest,
    current_user: CurrentUserDep,
    account_service: AccountServiceDep,
) -> UserResponse:
    """HTTP обработчик за редактиране на профила (display_name) на текущия потребител."""
    try:
        user = account_service.update_user_profile(
            user_id=current_user.id,
            display_name=request.display_name,
        )
        return UserResponse.model_validate(user)
    except UserNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "USER_NOT_FOUND",
                "message": "User not found",
            },
        )


@router.post(
    "/me/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
)
def change_password(
    request: ChangePasswordRequest,
    current_user: CurrentUserDep,
    account_service: AccountServiceDep,
) -> None:
    """HTTP обработчик за сигурна смяна на парола на текущия потребител."""
    try:
        account_service.change_password(
            user_id=current_user.id,
            current_password=request.current_password,
            new_password=request.new_password,
        )
    except InvalidCurrentPassword:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_CURRENT_PASSWORD",
                "message": "Current password is incorrect",
            },
        )
    except SamePasswordError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "SAME_PASSWORD",
                "message": "New password must be different from current password",
            },
        )


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


@router.post("/{user_id}/block", response_model=ModerationActionResponse)
def block_user(
    user_id: int,
    current_admin: AdminUserDep,
    account_service: AccountServiceDep,
    request: ModerationRequest | None = None,
) -> ModerationActionResponse:
    """Администраторски обработчик за блокиране на потребител."""
    reason = request.reason if request else None
    try:
        user = account_service.block_user(
            admin_id=current_admin.id,
            target_user_id=user_id,
            reason=reason,
        )
        return ModerationActionResponse(
            id=user.id,
            email=user.email,
            status=user.status,
            message="Account has been blocked",
        )
    except SelfModerationForbidden:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "SELF_MODERATION_FORBIDDEN",
                "message": "Cannot moderate your own account",
            },
        )
    except TechnicalAccountProtected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "TECHNICAL_ACCOUNT_PROTECTED",
                "message": "Cannot moderate technical system account",
            },
        )
    except UserNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "USER_NOT_FOUND",
                "message": f"User with id {user_id} not found",
            },
        )
    except InvalidAccountState as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_ACCOUNT_STATE",
                "message": str(e),
            },
        )


@router.post("/{user_id}/unblock", response_model=ModerationActionResponse)
def unblock_user(
    user_id: int,
    current_admin: AdminUserDep,
    account_service: AccountServiceDep,
    request: ModerationRequest | None = None,
) -> ModerationActionResponse:
    """Администраторски обработчик за отблокиране на потребител."""
    reason = request.reason if request else None
    try:
        user = account_service.unblock_user(
            admin_id=current_admin.id,
            target_user_id=user_id,
            reason=reason,
        )
        return ModerationActionResponse(
            id=user.id,
            email=user.email,
            status=user.status,
            message="Account has been unblocked",
        )
    except TechnicalAccountProtected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "TECHNICAL_ACCOUNT_PROTECTED",
                "message": "Cannot moderate technical system account",
            },
        )
    except UserNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "USER_NOT_FOUND",
                "message": f"User with id {user_id} not found",
            },
        )
    except InvalidAccountState as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_ACCOUNT_STATE",
                "message": str(e),
            },
        )


@router.post("/{user_id}/close", response_model=ModerationActionResponse)
def close_user(
    user_id: int,
    current_admin: AdminUserDep,
    account_service: AccountServiceDep,
    request: ModerationRequest | None = None,
) -> ModerationActionResponse:
    """Администраторски обработчик за окончателно закриване на потребителски акаунт."""
    reason = request.reason if request else None
    try:
        user = account_service.close_user(
            admin_id=current_admin.id,
            target_user_id=user_id,
            reason=reason,
        )
        return ModerationActionResponse(
            id=user.id,
            email=user.email,
            status=user.status,
            message="Account has been closed",
        )
    except SelfModerationForbidden:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "SELF_MODERATION_FORBIDDEN",
                "message": "Cannot moderate your own account",
            },
        )
    except TechnicalAccountProtected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "TECHNICAL_ACCOUNT_PROTECTED",
                "message": "Cannot moderate technical system account",
            },
        )
    except UserNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "USER_NOT_FOUND",
                "message": f"User with id {user_id} not found",
            },
        )
    except InvalidAccountState as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_ACCOUNT_STATE",
                "message": str(e),
            },
        )
