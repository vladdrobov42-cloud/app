import hashlib
import os
from datetime import date
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(Text, nullable=False)
    courier_type = Column(String(20), nullable=False)  # foot | company_car | own_car
    created_at = Column(DateTime, server_default=func.now())


class Shift(Base):
    __tablename__ = "shifts"
    __table_args__ = (UniqueConstraint("user_id", "work_date", name="uq_user_work_date"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    work_date = Column(Date, nullable=False, index=True)
    hours = Column(Float, nullable=False, default=0.0)
    deliveries = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


Base.metadata.create_all(bind=engine)

app = FastAPI(title="Courier Schedule API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://wordlist-25840.web.app",
                   "http://127.0.0.1:5500", 
                   ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=4, max_length=128)
    courier_type: str = Field(..., pattern="^(foot|company_car|own_car)$")


class LoginRequest(BaseModel):
    username: str
    password: str


class ShiftUpsertRequest(BaseModel):
    user_id: int
    work_date: date
    hours: float = Field(..., ge=0, le=24)
    deliveries: int = Field(..., ge=0)


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, expected_digest = stored_hash.split("$", 1)
    except ValueError:
        return False
    actual_digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
    return actual_digest == expected_digest


def calculate_pay(hours: float, deliveries: int, courier_type: str) -> dict:
    if courier_type == "foot":
        gross = (4.7 * hours) + (2.5 * deliveries)
        tax = gross * 0.14
        net = gross - tax
    elif courier_type == "company_car":
        gross = (4.7 * hours) + (1.0 * deliveries)
        tax = gross * 0.14
        net = gross - tax
    elif courier_type == "own_car":
        hourly_gross = 2.0 * hours
        tax = hourly_gross * 0.14
        net = (hourly_gross - tax) + (7.19 * deliveries)
    else:
        raise HTTPException(status_code=400, detail="Неизвестный тип курьера")

    return {
        "tax": round(tax, 2),
        "net": round(net, 2),
    }


@app.post("/api/auth/register")
def register_user(payload: RegisterRequest):
    username_clean = payload.username.strip().lower()
    if not username_clean:
        raise HTTPException(status_code=400, detail="Логин не может быть пустым")

    with SessionLocal() as db:
        existing = db.query(User).filter(User.username == username_clean).first()
        if existing:
            raise HTTPException(status_code=409, detail="Пользователь с таким логином уже существует")

        user = User(
            username=username_clean,
            password_hash=hash_password(payload.password),
            courier_type=payload.courier_type,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        return {
            "user_id": user.id,
            "username": user.username,
            "courier_type": user.courier_type,
        }


@app.post("/api/auth/login")
def login_user(payload: LoginRequest):
    username_clean = payload.username.strip().lower()
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username_clean).first()
        if not user or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Неверный логин или пароль")

        return {
            "user_id": user.id,
            "username": user.username,
            "courier_type": user.courier_type,
        }


@app.get("/api/shifts")
def get_shifts(
    user_id: int = Query(...),
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),  # например 2026-11
):
    year, mon = map(int, month.split("-"))
    start_date = date(year, mon, 1)
    if mon == 12:
        end_date = date(year + 1, 1, 1)
    else:
        end_date = date(year, mon + 1, 1)

    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")

        rows = (
            db.query(Shift)
            .filter(
                Shift.user_id == user_id,
                Shift.work_date >= start_date,
                Shift.work_date < end_date,
            )
            .order_by(Shift.work_date.asc())
            .all()
        )

        items = []
        total_days = 0
        total_hours = 0.0
        total_deliveries = 0
        total_net = 0.0

        for r in rows:
            if r.hours > 0 or r.deliveries > 0:
                calc = calculate_pay(r.hours, r.deliveries, user.courier_type)
                total_days += 1
                total_hours += r.hours
                total_deliveries += r.deliveries
                total_net += calc["net"]
                items.append({
                    "date": r.work_date.isoformat(),
                    "hours": r.hours,
                    "deliveries": r.deliveries,
                    "tax": calc["tax"],
                    "net": calc["net"],
                })

        return {
            "user_id": user.id,
            "username": user.username,
            "courier_type": user.courier_type,
            "month": month,
            "summary": {
                "days": total_days,
                "hours": round(total_hours, 2),
                "deliveries": total_deliveries,
                "net_pay": round(total_net, 2),
            },
            "shifts": items,
        }


@app.put("/api/shifts")
def upsert_shift(payload: ShiftUpsertRequest):
    with SessionLocal() as db:
        user = db.query(User).filter(User.id == payload.user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")

        if payload.hours == 0 and payload.deliveries == 0:
            db.query(Shift).filter(
                Shift.user_id == payload.user_id,
                Shift.work_date == payload.work_date,
            ).delete()
            db.commit()
            return {"status": "deleted", "date": payload.work_date.isoformat()}

        shift = (
            db.query(Shift)
            .filter(
                Shift.user_id == payload.user_id,
                Shift.work_date == payload.work_date,
            )
            .first()
        )
        if shift:
            shift.hours = payload.hours
            shift.deliveries = payload.deliveries
        else:
            shift = Shift(
                user_id=payload.user_id,
                work_date=payload.work_date,
                hours=payload.hours,
                deliveries=payload.deliveries,
            )
            db.add(shift)

        db.commit()
        calc = calculate_pay(payload.hours, payload.deliveries, user.courier_type)
        return {
            "status": "saved",
            "date": payload.work_date.isoformat(),
            "hours": payload.hours,
            "deliveries": payload.deliveries,
            "tax": calc["tax"],
            "net": calc["net"],
        }


@app.delete("/api/shifts")
def delete_shift(user_id: int = Query(...), work_date: date = Query(...)):
    with SessionLocal() as db:
        deleted = (
            db.query(Shift)
            .filter(Shift.user_id == user_id, Shift.work_date == work_date)
            .delete()
        )
        db.commit()
        return {"status": "deleted", "rows_affected": deleted}
