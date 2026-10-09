from pydantic import BaseModel, ConfigDict


class GoogleAuthRequest(BaseModel):
    id_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: str | None
    telegram_chat_id: str | None
    onboarding_completed: bool


class TelegramUpdateRequest(BaseModel):
    telegram_chat_id: str

