from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .date_utils import SHANGHAI
from .enums import TaskStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserView(ORMModel):
    id: str
    email: str
    username: str | None
    display_name: str
    role: str
    active: bool
    must_change_password: bool


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9._-]+$")
    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip().lower()


class AdminUserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=500)
    new_password: str = Field(min_length=12, max_length=500)
    confirmation: str = Field(min_length=12, max_length=500)

    @model_validator(mode="after")
    def passwords_match(self):
        if self.new_password != self.confirmation:
            raise ValueError("两次输入的新密码不一致")
        return self


class ModelConfigurationUpdate(BaseModel):
    provider: str
    api_key: str | None = Field(default=None, min_length=8, max_length=1000)
    base_url: str | None = Field(default=None, max_length=500)
    model_name: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_provider(self):
        if self.provider not in {"openai_compatible", "minimax_token_plan"}:
            raise ValueError("不支持的模型接入方式")
        if self.provider == "openai_compatible":
            if not self.base_url or not self.model_name:
                raise ValueError("OpenAI兼容模式必须填写Base URL和模型名称")
            if not self.base_url.startswith("https://"):
                raise ValueError("生产使用的Base URL必须是HTTPS地址")
        return self


class KeywordCreate(BaseModel):
    keyword: str = Field(min_length=1, max_length=120)
    # The target is intentionally not capped at product level. Operational
    # pacing, page exhaustion and explicit platform-risk pauses remain the
    # stopping boundaries for very large tasks.
    target_count: int = Field(default=50, ge=1)

    @field_validator("keyword")
    @classmethod
    def clean_keyword(cls, value: str) -> str:
        return value.strip()


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    start_date: date
    end_date: date
    keywords: list[KeywordCreate] = Field(min_length=1, max_length=10)
    privacy_confirmed: bool
    ai_confirmed: bool = False

    @model_validator(mode="after")
    def validate_dates_and_consent(self):
        if self.end_date < self.start_date:
            raise ValueError("结束日期不能早于开始日期")
        if self.end_date > datetime.now(SHANGHAI).date():
            raise ValueError("结束日期不能晚于今天")
        if (self.end_date - self.start_date).days > 180:
            raise ValueError("单次任务时间范围不能超过180天")
        if not self.privacy_confirmed:
            raise ValueError("创建任务前必须确认信息处理提示")
        if len({item.keyword for item in self.keywords}) != len(self.keywords):
            raise ValueError("同一任务不能包含重复关键词")
        return self


class KeywordView(ORMModel):
    id: str
    keyword: str
    target_count: int
    collected_count: int
    scanned_count: int
    position: int
    completed: bool


class TaskView(ORMModel):
    id: str
    name: str
    status: str
    start_date: date
    end_date: date
    queue_position: int
    attention_reason: str | None
    progress_message: str | None
    privacy_confirmed: bool
    ai_confirmed: bool
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    review_expires_at: datetime | None
    keywords: list[KeywordView]


class TaskAction(BaseModel):
    action: str

    @field_validator("action")
    @classmethod
    def validate_action(cls, value: str) -> str:
        allowed = {"pause", "resume", "cancel", "move_up", "move_down"}
        if value not in allowed:
            raise ValueError(f"无效操作，可选：{', '.join(sorted(allowed))}")
        return value


class ImageUpdate(BaseModel):
    selected: bool | None = None


class BulkImageUpdate(BaseModel):
    # The browser submits very large review sets in bounded batches. Keep an
    # API-side ceiling per request without imposing a ceiling on the task.
    image_ids: list[str] = Field(default_factory=list, max_length=50_000)
    selected: bool | None = None
    invert: bool = False


class ImageView(BaseModel):
    id: str
    note_id: str
    task_id: str
    keyword: str
    platform_note_id: str
    note_title: str
    author: str
    source_url: str
    published_at: datetime | None
    ordinal: int
    selected: bool
    ai_is_report: bool | None
    ai_confidence: float | None
    ai_reason: str | None
    captured_at: datetime
    deleted_at: datetime | None
    image_url: str
    thumbnail_url: str


class ExportCreate(BaseModel):
    confirmed: bool

    @field_validator("confirmed")
    @classmethod
    def require_confirmation(cls, value: bool) -> bool:
        if not value:
            raise ValueError("导出前必须确认信息处理责任")
        return value


class AiClassifyCreate(BaseModel):
    confirmed: bool
    keyword_id: str | None = None

    @field_validator("confirmed")
    @classmethod
    def require_confirmation(cls, value: bool) -> bool:
        if not value:
            raise ValueError("必须确认允许将图片发送给已配置的视觉模型判断")
        return value


class BrowserClick(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class BrowserType(BaseModel):
    text: str = Field(max_length=100)


class BrowserKey(BaseModel):
    key: str = Field(max_length=30)


class BrowserScroll(BaseModel):
    delta_y: int = Field(ge=-3000, le=3000)


class DashboardView(BaseModel):
    queued: int
    running: int
    needs_attention: int
    awaiting_review: int
    selected_images: int
    expiring_soon: int
    recent_tasks: list[TaskView]


ACTIVE_TASK_STATUSES = {
    TaskStatus.QUEUED,
    TaskStatus.RUNNING,
    TaskStatus.CLASSIFYING,
    TaskStatus.NEEDS_ATTENTION,
    TaskStatus.PAUSED,
}
