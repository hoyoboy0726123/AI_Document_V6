from typing import Optional

from fastapi import HTTPException, status
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .. import models

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


_ALLOWED_ROLES = {"admin", "assistant"}


def get_user_by_username(db: Session, username: str) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.username == username).first()


def get_user_by_email(db: Session, email: str) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.email == email).first()


def create_user(
    db: Session,
    *,
    username: str,
    email: str,
    password: str,
    role: str = "assistant",
) -> models.User:
    if get_user_by_username(db, username):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already registered")
    if get_user_by_email(db, email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

    # Audit C1 縱深防禦：role 只允許白名單值，非法值一律降為 assistant，
    # 避免任何呼叫端（含未來新端點）不小心讓外部輸入指定成 admin。
    if role not in _ALLOWED_ROLES:
        role = "assistant"

    db_user = models.User(
        username=username,
        email=email,
        hashed_password=hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def authenticate_user(db: Session, username: str, password: str) -> Optional[models.User]:
    user = get_user_by_username(db, username)
    if not user:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user
