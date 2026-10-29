from fastapi import APIRouter, HTTPException, status

from app.dependencies import DbSession
from app.modules.accounts import service
from app.modules.accounts.schemas import UserRegisterRequest, UserResponse
from app.modules.accounts.service import EmailAlreadyExists

router = APIRouter(prefix="/users", tags=["accounts"])


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(
    request: UserRegisterRequest,
    session: DbSession,
) -> UserResponse:
    """HTTP обработчик за регистрация на нов потребител (винаги customer)."""
    try:
        user = service.register_user(
            session=session,
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
