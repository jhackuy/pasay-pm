from pydantic import BaseModel, ConfigDict


class AuthInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    is_active: bool
