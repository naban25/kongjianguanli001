"""All SQLAlchemy ORM models. Imported from Base so Alembic discovers them."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .core.database import Base


# ---------- Helpers ----------

def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _ts_created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)


def _ts_updated() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


# ---------- Enums ----------

class OrganizationStatus(StrEnum):
    active = "active"
    inactive = "inactive"


class ProjectStatus(StrEnum):
    active = "active"
    inactive = "inactive"
    archived = "archived"


class BuildingStatus(StrEnum):
    active = "active"
    inactive = "inactive"


# Space
class SpaceStatus(StrEnum):
    available = "available"   # 待租
    reserved = "reserved"     # 预留
    leased = "leased"         # 已租
    inactive = "inactive"     # 停用
    unknown = "unknown"       # 待确认


class SpaceType(StrEnum):
    office = "office"
    meeting = "meeting"
    storage = "storage"
    coworking = "coworking"
    retail = "retail"
    other = "other"


# Lease
class LeaseStatus(StrEnum):
    draft = "draft"
    pending = "pending"       # 待生效
    active = "active"         # 生效中
    expired = "expired"       # 已到期
    terminated = "terminated" # 已终止
    vacated = "vacated"       # 已退租
    renewed = "renewed"       # 已续签


class PaymentCycle(StrEnum):
    monthly = "monthly"
    quarterly = "quarterly"
    half_year = "half_year"
    yearly = "yearly"
    one_time = "one_time"
    custom = "custom"


# Charge
class ChargeType(StrEnum):
    rent = "rent"
    property_fee = "property_fee"
    water = "water"
    electricity = "electricity"
    internet = "internet"
    parking = "parking"
    deposit = "deposit"
    other = "other"


class ChargeStatus(StrEnum):
    not_due = "not_due"       # 未到期
    unpaid = "unpaid"         # 待缴
    partial = "partial"       # 部分缴纳
    paid = "paid"             # 已结清
    overdue = "overdue"       # 逾期
    cancelled = "cancelled"   # 已取消/冲销


class PaymentMethod(StrEnum):
    bank_transfer = "bank_transfer"
    cash = "cash"
    check = "check"
    online = "online"
    other = "other"


class PaymentStatus(StrEnum):
    pending = "pending"
    completed = "completed"
    refunded = "refunded"
    cancelled = "cancelled"


# Tenant
class TenantType(StrEnum):
    enterprise = "enterprise"
    individual = "individual"
    ngo = "ngo"
    other = "other"


class TenantStatus(StrEnum):
    active = "active"
    inactive = "inactive"
    blacklisted = "blacklisted"


# Users & RBAC
class AppUserStatus(StrEnum):
    active = "active"
    disabled = "disabled"
    invited = "invited"


class UserRole(StrEnum):
    system_admin = "system_admin"
    project_admin = "project_admin"
    operator = "operator"


# Import
class ImportJobStatus(StrEnum):
    uploaded = "uploaded"
    scanning = "scanning"
    parsing = "parsing"
    mapping_required = "mapping_required"
    preview_ready = "preview_ready"
    importing = "importing"
    completed = "completed"
    rejected = "rejected"
    failed = "failed"
    cancelled = "cancelled"


class ImportRowValidationStatus(StrEnum):
    pending = "pending"
    valid = "valid"
    warning = "warning"
    error = "error"
    skipped = "skipped"


# Field mapping
class MappingMethod(StrEnum):
    rule_based = "rule_based"
    ai_suggested = "ai_suggested"
    manual = "manual"
    template = "template"


# Data quality
class DataQualityIssueType(StrEnum):
    missing = "missing"
    duplicate = "duplicate"
    format = "format"
    reference = "reference"
    business_rule = "business_rule"
    status_conflict = "status_conflict"


class DataQualitySeverity(StrEnum):
    info = "info"
    warning = "warning"
    error = "error"
    critical = "critical"


class DataQualityStatus(StrEnum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    ignored = "ignored"


# Export
class ExportJobStatus(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    expired = "expired"


# ---------- Tables ----------


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[OrganizationStatus] = mapped_column(Enum(OrganizationStatus), default=OrganizationStatus.active)
    created_at: Mapped[datetime] = _ts_created()
    updated_at: Mapped[datetime] = _ts_updated()


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    status: Mapped[ProjectStatus] = mapped_column(Enum(ProjectStatus), default=ProjectStatus.active)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    created_at: Mapped[datetime] = _ts_created()
    updated_at: Mapped[datetime] = _ts_updated()

    organization = relationship("Organization")


class Building(Base):
    __tablename__ = "buildings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[BuildingStatus] = mapped_column(Enum(BuildingStatus), default=BuildingStatus.active)
    created_at: Mapped[datetime] = _ts_created()
    updated_at: Mapped[datetime] = _ts_updated()

    project = relationship("Project")


class Space(Base):
    """可出租空间/房间/工位。"""
    __tablename__ = "spaces"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    building_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("buildings.id"), index=True)
    space_code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    original_space_code: Mapped[Optional[str]] = mapped_column(String(128))
    floor: Mapped[Optional[int]] = mapped_column(Integer)
    space_type: Mapped[Optional[SpaceType]] = mapped_column(Enum(SpaceType))
    area_sqm: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    workstation_count: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[SpaceStatus] = mapped_column(Enum(SpaceStatus), default=SpaceStatus.unknown, index=True)
    source_import_row_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _ts_created()
    updated_at: Mapped[datetime] = _ts_updated()

    project = relationship("Project")
    building = relationship("Building")

    __table_args__ = (
        UniqueConstraint("project_id", "space_code", name="uq_spaces_project_code"),
    )


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    tenant_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    tenant_type: Mapped[Optional[TenantType]] = mapped_column(Enum(TenantType))
    registration_code: Mapped[Optional[str]] = mapped_column(String(128))
    contact_name: Mapped[Optional[str]] = mapped_column(String(128))
    contact_phone_encrypted: Mapped[Optional[str]] = mapped_column(String(255))
    industry: Mapped[Optional[str]] = mapped_column(String(128))
    status: Mapped[TenantStatus] = mapped_column(Enum(TenantStatus), default=TenantStatus.active)
    created_at: Mapped[datetime] = _ts_created()
    updated_at: Mapped[datetime] = _ts_updated()

    project = relationship("Project")


class Lease(Base):
    """租赁合同。"""
    __tablename__ = "leases"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    lease_code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    space_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("spaces.id"), nullable=False, index=True)
    contract_signed_date: Mapped[Optional[date]] = mapped_column(Date)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    rent_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    deposit_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    payment_cycle: Mapped[Optional[PaymentCycle]] = mapped_column(Enum(PaymentCycle))
    status: Mapped[LeaseStatus] = mapped_column(Enum(LeaseStatus), default=LeaseStatus.pending, index=True)
    source_import_row_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _ts_created()
    updated_at: Mapped[datetime] = _ts_updated()

    tenant = relationship("Tenant")
    space = relationship("Space")
    project = relationship("Project")


class Charge(Base):
    """应收费用台账。"""
    __tablename__ = "charges"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    lease_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("leases.id"), index=True)
    space_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("spaces.id"))
    charge_type: Mapped[ChargeType] = mapped_column(Enum(ChargeType), nullable=False, index=True)
    period_start: Mapped[Optional[date]] = mapped_column(Date)
    period_end: Mapped[Optional[date]] = mapped_column(Date)
    due_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    amount_due: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    status: Mapped[ChargeStatus] = mapped_column(Enum(ChargeStatus), default=ChargeStatus.not_due, index=True)
    source_import_row_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _ts_created()
    updated_at: Mapped[datetime] = _ts_updated()

    tenant = relationship("Tenant")
    lease = relationship("Lease")


class Payment(Base):
    """收款记录。"""
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    payment_method: Mapped[Optional[PaymentMethod]] = mapped_column(Enum(PaymentMethod))
    reference_no: Mapped[Optional[str]] = mapped_column(String(128))
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), default=PaymentStatus.completed, index=True)
    source_import_row_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _ts_created()
    updated_at: Mapped[datetime] = _ts_updated()

    tenant = relationship("Tenant")
    allocations = relationship("PaymentAllocation", back_populates="payment")


class PaymentAllocation(Base):
    """收款分配到具体费用。"""
    __tablename__ = "payment_allocations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    payment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("payments.id"), nullable=False, index=True)
    charge_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("charges.id"), nullable=False, index=True)
    allocated_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    created_at: Mapped[datetime] = _ts_created()

    payment = relationship("Payment", back_populates="allocations")
    charge = relationship("Charge")


class AppUser(Base):
    """系统用户。"""
    __tablename__ = "app_users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255))
    display_name: Mapped[Optional[str]] = mapped_column(String(128))
    is_system_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[AppUserStatus] = mapped_column(Enum(AppUserStatus), default=AppUserStatus.active, index=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts_created()
    updated_at: Mapped[datetime] = _ts_updated()


class UserProjectRole(Base):
    """用户-项目-角色关联。"""
    __tablename__ = "user_project_roles"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app_users.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False)
    created_at: Mapped[datetime] = _ts_created()

    user = relationship("AppUser")
    project = relationship("Project")

    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_user_project_role"),
    )


# ---------- Import pipeline ----------

class ImportJob(Base):
    __tablename__ = "import_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app_users.id"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    file_size: Mapped[int] = mapped_column(Integer)
    status: Mapped[ImportJobStatus] = mapped_column(Enum(ImportJobStatus), default=ImportJobStatus.uploaded, index=True)
    sheet_count: Mapped[int] = mapped_column(Integer, default=0)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    mapping_config: Mapped[Optional[dict]] = mapped_column(JSONB)
    error_summary: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _ts_created()
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    project = relationship("Project")
    uploader = relationship("AppUser")


class ImportSheet(Base):
    __tablename__ = "import_sheets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    import_job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("import_jobs.id"), nullable=False, index=True)
    sheet_name: Mapped[str] = mapped_column(String(255), nullable=False)
    header_row_index: Mapped[int] = mapped_column(Integer, default=0)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    column_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="parsed")
    created_at: Mapped[datetime] = _ts_created()

    import_job = relationship("ImportJob")


class ImportRow(Base):
    __tablename__ = "import_rows"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    import_job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("import_jobs.id"), nullable=False, index=True)
    import_sheet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("import_sheets.id"), nullable=False, index=True)
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_values: Mapped[dict] = mapped_column(JSONB, nullable=False)
    normalized_values: Mapped[Optional[dict]] = mapped_column(JSONB)
    validation_status: Mapped[ImportRowValidationStatus] = mapped_column(Enum(ImportRowValidationStatus), default=ImportRowValidationStatus.pending, index=True)
    created_at: Mapped[datetime] = _ts_created()


class FieldMapping(Base):
    __tablename__ = "field_mappings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    import_sheet_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("import_sheets.id"), index=True)
    source_field_name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_field_name: Mapped[Optional[str]] = mapped_column(String(255))
    target_entity: Mapped[Optional[str]] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(64), default="text")
    mapping_method: Mapped[MappingMethod] = mapped_column(Enum(MappingMethod), default=MappingMethod.rule_based)
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    confirmed_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("app_users.id"))
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts_created()


class DataQualityIssue(Base):
    __tablename__ = "data_quality_issues"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(index=True)
    issue_type: Mapped[DataQualityIssueType] = mapped_column(Enum(DataQualityIssueType), nullable=False, index=True)
    severity: Mapped[DataQualitySeverity] = mapped_column(Enum(DataQualitySeverity), default=DataQualitySeverity.warning)
    description: Mapped[str] = mapped_column(Text)
    suggested_action: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[DataQualityStatus] = mapped_column(Enum(DataQualityStatus), default=DataQualityStatus.open, index=True)
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("app_users.id"))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts_created()


# ---------- Audit ----------

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), index=True)
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), index=True)
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("app_users.id"), index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_id: Mapped[Optional[str]] = mapped_column(index=True)
    before_summary: Mapped[Optional[dict]] = mapped_column(JSONB)
    after_summary: Mapped[Optional[dict]] = mapped_column(JSONB)
    request_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts_created()


# ---------- AI / Agent ----------

class AIConversation(Base):
    __tablename__ = "ai_conversations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"))
    project_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app_users.id"), nullable=False, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = _ts_created()
    updated_at: Mapped[datetime] = _ts_updated()

    user = relationship("AppUser")


class AIMessage(Base):
    __tablename__ = "ai_messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ai_conversations.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | system | tool
    content: Mapped[Optional[str]] = mapped_column(Text)
    tool_name: Mapped[Optional[str]] = mapped_column(String(128))
    tool_args_safe: Mapped[Optional[dict]] = mapped_column(JSONB)  # sanitised tool args
    created_at: Mapped[datetime] = _ts_created()


class AIToolRun(Base):
    __tablename__ = "ai_tool_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("ai_conversations.id"), index=True)
    message_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("ai_messages.id"))
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    row_count: Mapped[Optional[int]] = mapped_column(Integer)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("app_users.id"))
    created_at: Mapped[datetime] = _ts_created()


# ---------- Export ----------

class ExportJob(Base):
    __tablename__ = "export_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("app_users.id"), index=True)
    filters: Mapped[Optional[dict]] = mapped_column(JSONB)
    selected_fields: Mapped[Optional[list]] = mapped_column(JSONB)
    status: Mapped[ExportJobStatus] = mapped_column(Enum(ExportJobStatus), default=ExportJobStatus.pending, index=True)
    storage_key: Mapped[Optional[str]] = mapped_column(String(512))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts_created()
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
