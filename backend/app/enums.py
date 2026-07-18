from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    USER = "user"


class OrganizationStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    EXPIRED = "expired"


class TaskStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    CLASSIFYING = "classifying"
    NEEDS_ATTENTION = "needs_attention"
    PAUSED = "paused"
    REVIEW = "review"
    EXPORTED = "exported"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ImageCategory(StrEnum):
    BLOOD_ROUTINE = "blood_routine"
    CT = "ct"
    MRI = "mri"
    PATHOLOGY = "pathology"
    OTHER_REPORT = "other_report"
    NON_REPORT = "non_report"
    UNCLASSIFIED = "unclassified"


class ConsentType(StrEnum):
    TASK_PRIVACY = "task_privacy"
    AI_PROCESSING = "ai_processing"
    EXPORT_PRIVACY = "export_privacy"
