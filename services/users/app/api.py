"""HTTP обработчици (FastAPI Routers) за регистрация, вход и състояние на Users."""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.dependencies import DbSession, UserServiceDep
from app.schemas import (
    HealthResponse,
    TokenResponse,
    UserLoginRequest,
    UserRegisterRequest,
    UserResponse,
)
from app.service import (
    AccountBlocked,
    AccountClosed,
    EmailAlreadyExists,
    InvalidCredentials,
)
from app.tokens import create_access_token

router = APIRouter(tags=["users"])
auth_router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/health", response_model=HealthResponse)
def health_check(session: DbSession) -> HealthResponse:
    """Проверка за работоспособността на сервиза и достъпа до базата данни."""
    try:
        session.execute(text("SELECT 1"))
        return HealthResponse(status="ok", database="ok")
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "DATABASE_UNAVAILABLE",
                "message": f"Database check failed: {exc}",
            },
        ) from exc


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    request: UserRegisterRequest,
    user_service: UserServiceDep,
) -> UserResponse:
    """HTTP обработчик за регистрация на нов потребител (винаги с роля customer)."""
    try:
        user = user_service.register_user(
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


@auth_router.post("/login", response_model=TokenResponse)
def login(
    request: UserLoginRequest,
    user_service: UserServiceDep,
) -> TokenResponse:
    """HTTP обработчик за вход на потребител и издаване на асиметрично подписан JWT токен."""
    try:
        user = user_service.authenticate_user(
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

    private_key = settings.get_private_key()
    access_token = create_access_token(
        user_id=user.id,
        role=user.role,
        email=user.email,
        token_version=user.token_version,
        private_key=private_key,
        algorithm=settings.jwt_algorithm,
        expire_minutes=settings.jwt_access_token_expire_minutes,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    return TokenResponse(access_token=access_token, token_type="bearer")


@auth_router.get("/public-key")
def get_public_key() -> dict[str, str]:
    """Връща публичния ключ на Users за локална верификация от други микросервизи."""
    return {
        "public_key": settings.get_public_key(),
        "algorithm": settings.jwt_algorithm,
        "issuer": settings.jwt_issuer,
        "audience": settings.jwt_audience,
    }
