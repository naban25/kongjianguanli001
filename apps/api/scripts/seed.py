"""Seed script — create default org, project and admin user.

Usage:
    .venv/bin/python -m scripts.seed
"""
from __future__ import annotations

import sys
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import bcrypt as _bcrypt

# Ensure apps/api is on path
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session

from app.core.database import SessionLocal, engine
from app.models import (
    AppUser,
    Building,
    Charge,
    ChargeStatus,
    ChargeType,
    ImportJobStatus,
    Lease,
    LeaseStatus,
    Organization,
    Payment,
    PaymentStatus,
    Project,
    Space,
    SpaceStatus,
    Tenant,
    TenantStatus,
    AppUserStatus,
)

pwd_ctx = None  # kept for reference; use bcrypt directly
def _hash_pw(pw: str) -> str:
    return _bcrypt.hashpw(pw.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def seed() -> None:
    db: Session = SessionLocal()
    try:
        # --- Organization ---
        org = db.query(Organization).filter(Organization.name == "Demo Organization").first()
        if not org:
            org = Organization(
                id=uuid4(),
                name="Demo Organization",
                status="active",
            )
            db.add(org)
            db.flush()
            print(f"[seed] Created organization: {org.id}")

        # --- Project ---
        project = db.query(Project).filter(Project.code == "DEMO-P01").first()
        if not project:
            project = Project(
                id=uuid4(),
                organization_id=org.id,
                name="Demo Tower A",
                code="DEMO-P01",
                status="active",
                timezone="Asia/Shanghai",
            )
            db.add(project)
            db.flush()
            print(f"[seed] Created project: {project.id}")

        # --- Admin user ---
        admin = db.query(AppUser).filter(AppUser.username == "admin").first()
        if not admin:
            admin_password = "admin12345"
            admin = AppUser(
                id=uuid4(),
                username="admin",
                email="admin@example.com",
                password_hash=_hash_pw(admin_password),
                display_name="System Administrator",
                is_system_admin=True,
                status="active",
            )
            db.add(admin)
            db.flush()
            print(f"[seed] Created admin user 'admin' / '{admin_password}'")

        # --- Building ---
        building = db.query(Building).filter(Building.project_id == project.id).first()
        if not building:
            building = Building(
                id=uuid4(),
                organization_id=org.id,
                project_id=project.id,
                name="Tower A",
                address="123 Demo Street",
                status="active",
            )
            db.add(building)
            db.flush()
            print(f"[seed] Created building: {building.id}")

        # --- Demo spaces ---
        existing_spaces = db.query(Space).filter(Space.project_id == project.id).count()
        if existing_spaces == 0:
            demo_spaces = []
            floors = [1, 2, 3]
            for floor in floors:
                for i in range(1, 6):
                    code = f"A{floor:02d}-{i:02d}"
                    demo_spaces.append(
                        Space(
                            id=uuid4(),
                            organization_id=org.id,
                            project_id=project.id,
                            building_id=building.id,
                            space_code=code,
                            original_space_code=code,
                            floor=floor,
                            space_type="office",
                            area_sqm=Decimal(str(20 + i * 5)),
                            workstation_count=4 + i,
                            status=SpaceStatus.available if i > 2 else SpaceStatus.leased,
                        )
                    )
            db.add_all(demo_spaces)
            db.flush()
            print(f"[seed] Created {len(demo_spaces)} demo spaces")

        # --- Demo tenants ---
        existing_tenants = db.query(Tenant).filter(Tenant.project_id == project.id).count()
        if existing_tenants == 0:
            tenants = [
                ("Acme Inc.", "enterprise", "Zhang Wei"),
                ("Tech Startup Co.", "enterprise", "Li Na"),
                ("Creative Studio", "enterprise", "Wang Fang"),
            ]
            for name, ttype, contact in tenants:
                t = Tenant(
                    id=uuid4(),
                    organization_id=org.id,
                    project_id=project.id,
                    tenant_name=name,
                    tenant_type=ttype,
                    contact_name=contact,
                    status=TenantStatus.active,
                )
                db.add(t)
                print(f"[seed] Created tenant: {name}")
            db.flush()

        # --- Demo leases (link first two tenants to first two leased spaces) ---
        existing_leases = db.query(Lease).filter(Lease.project_id == project.id).count()
        if existing_leases == 0:
            spaces_q = db.query(Space).filter(Space.project_id == project.id, Space.status == SpaceStatus.leased).all()
            tenants_q = db.query(Tenant).filter(Tenant.project_id == project.id).all()
            if spaces_q and tenants_q:
                for sp, tn in zip(spaces_q, tenants_q[: len(spaces_q)]):
                    lease = Lease(
                        id=uuid4(),
                        organization_id=org.id,
                        project_id=project.id,
                        lease_code=f"L-{sp.space_code}",
                        tenant_id=tn.id,
                        space_id=sp.id,
                        contract_signed_date=datetime(2025, 1, 15).date(),
                        start_date=datetime(2025, 2, 1).date(),
                        end_date=datetime(2027, 1, 31).date(),
                        rent_amount=Decimal("15000.00"),
                        deposit_amount=Decimal("45000.00"),
                        payment_cycle="monthly",
                        status=LeaseStatus.active,
                    )
                    db.add(lease)
                    print(f"[seed] Created lease for {tn.tenant_name} in {sp.space_code}")

        db.commit()
        print("\n✅ Seed data created successfully.")
        print("   Admin login:  admin / admin12345")
        print("   Organization: Demo Organization")
        print("   Project:      Demo Tower A (DEMO-P01)")

    except Exception as exc:
        db.rollback()
        print(f"[seed] ERROR: {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()
