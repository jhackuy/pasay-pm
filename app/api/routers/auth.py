from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.auth import AuthInfo

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("", response_model=AuthInfo, summary="Validate API key and return client info")
def verify_auth(user: User = Depends(get_current_user)) -> User:
    return user
