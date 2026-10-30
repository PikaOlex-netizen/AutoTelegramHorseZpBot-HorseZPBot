from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


class PhoneLoginRequest(BaseModel):
    phone: str = Field(min_length=4, max_length=32)


class CodeLoginRequest(BaseModel):
    code: str = Field(min_length=3, max_length=16)


class PasswordLoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class ChatAssignmentRequest(BaseModel):
    pch_enabled: bool | None = None
    pb_enabled: bool | None = None
    enabled: bool | None = None
    gemini_enabled: bool | None = None
    keyword_reply_enabled: bool | None = None


class AddChatRequest(BaseModel):
    chat_id: int


class AccountCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)


class StartSchedulerRequest(BaseModel):
    text: str | None = Field(default=None, max_length=4096)


class TemplateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class FarmSettingsRequest(BaseModel):
    chat_id: int | None = None
    interval_hours: int = Field(default=23, ge=0)
    enabled: bool


class PSSettingsRequest(BaseModel):
    chat_id: int | None = None
    text: str | None = Field(default=None, max_length=4096)
    enabled: bool


class FeedSettingsRequest(BaseModel):
    username_to_feed: str | None = Field(default=None, max_length=128)
    text: str | None = Field(default=None, max_length=4096)
    enabled: bool


class ManualMessageRequest(BaseModel):
    chat_id: int
    text: str = Field(min_length=1, max_length=4096)
    message_type: str = Field(pattern="^(PCH|PB|PS|FARM|REPLY)$")


class SettingsRequest(BaseModel):
    delete_after_seconds: int = Field(ge=1, le=86400)
    max_delete_retries: int = Field(ge=0, le=10)
    pch_groups: int = Field(ge=1, le=100)
    pb_groups: int = Field(ge=1, le=100)
    group_delay_seconds: int = Field(ge=1, le=604800)
    pch_pb_delay_seconds: int = Field(ge=0, le=3600, default=0)
    telegram_device_model: str = Field(min_length=1, max_length=100)
    log_level: str = Field(pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    auto_reconnect: bool
    theme: str = Field(pattern="^(dark|light)$")


class GeminiSettingsRequest(BaseModel):
    enabled: bool
    model: str = Field(min_length=1, max_length=120)
    system_prompt: str = Field(min_length=1, max_length=10000)


class BotResponseItem(BaseModel):
    id: int
    chat_id: int
    chat_title: str
    bot_username: str
    bot_message_id: int
    pch_message_id: int | None
    response_text: str
    received_at: str


class ChatResponse(BaseModel):
    chat_id: int
    title: str
    username: str | None
    chat_type: str
    enabled: bool
    pch_enabled: bool
    pb_enabled: bool
    added_at: datetime
    updated_at: datetime
