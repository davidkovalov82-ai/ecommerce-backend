from fastapi import APIRouter, HTTPException, status

from app.modules.accounts.dependencies import AccountServiceDep
from app.modules.accounts.schemas import UserRegisterRequest, UserResponse
from app.modules.accounts.service import EmailAlreadyExists

router = APIRouter(prefix="/users", tags=["accounts"])


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
