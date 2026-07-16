
from datetime import datetime, timedelta, timezone
from typing import Optional
import secrets

from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from ..core.config import settings
from .. import models, schemas
from ..database import get_db
from ..services import users as user_service

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

def verify_password(plain_password, hashed_password):
    return user_service.verify_password(plain_password, hashed_password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def get_current_user(db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = schemas.TokenData(username=username)
    except JWTError:
        raise credentials_exception
    user = user_service.get_user_by_username(db, token_data.username)
    if user is None:
        raise credentials_exception
    return user

def get_current_admin_user(current_user: models.User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The user doesn't have enough privileges"
        )
    return current_user


# ==================== Refresh Token Functions ====================

def create_refresh_token(db: Session, user: models.User, device_info: Optional[str] = None, ip_address: Optional[str] = None) -> models.RefreshToken:
    """
    Create a new refresh token for the user.

    Args:
        db: Database session
        user: User object
        device_info: Optional device information (e.g., "Chrome/Windows")
        ip_address: Optional IP address of the client

    Returns:
        RefreshToken object with token string

    Security:
        - Token is cryptographically random (urlsafe_base64)
        - Stored in database for revocation capability
        - Expires after REFRESH_TOKEN_EXPIRE_DAYS
    """
    # Generate cryptographically secure random token
    token_string = secrets.token_urlsafe(48)  # 64 chars, URL-safe

    # Calculate expiration time
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    # Create refresh token record
    refresh_token = models.RefreshToken(
        token=token_string,
        user_id=user.id,
        expires_at=expires_at,
        device_info=device_info,
        ip_address=ip_address,
        is_revoked=False
    )

    db.add(refresh_token)
    db.commit()
    db.refresh(refresh_token)

    return refresh_token


def verify_refresh_token(db: Session, token_string: str) -> models.User:
    """
    Verify a refresh token and return the associated user.

    Args:
        db: Database session
        token_string: The refresh token string

    Returns:
        User object if token is valid

    Raises:
        HTTPException(401): If token is invalid, expired, or revoked

    Security:
        - Validates token exists in database
        - Checks expiration time
        - Checks revoked status
        - Validates user is still active
    """
    # Find token in database
    refresh_token = db.query(models.RefreshToken).filter(
        models.RefreshToken.token == token_string
    ).first()

    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
            headers={"WWW-Authenticate": "Bearer"}
        )

    # Check if token is revoked
    if refresh_token.is_revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
            headers={"WWW-Authenticate": "Bearer"}
        )

    # Check if token is expired
    # Note: SQLite returns naive datetime, so we need to make it timezone-aware for comparison
    expires_at_aware = refresh_token.expires_at.replace(tzinfo=timezone.utc) if refresh_token.expires_at.tzinfo is None else refresh_token.expires_at
    if expires_at_aware < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has expired",
            headers={"WWW-Authenticate": "Bearer"}
        )

    # Get associated user
    user = db.query(models.User).filter(models.User.id == refresh_token.user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"}
        )

    # Check if user is active
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is disabled",
            headers={"WWW-Authenticate": "Bearer"}
        )

    return user


def rotate_refresh_token(db: Session, token_string: str) -> models.User:
    """原子輪替 refresh token（audit M：雙花競態 + 重用偵測）。

    舊流程是 verify → revoke → create 三步無鎖：兩個並發請求帶同一 token，
    可能都在對方 revoke 前通過 verify → 各拿到一組新 token（一票多花）。
    這裡改為單一原子 UPDATE 搶佔：
        UPDATE refresh_tokens SET is_revoked=1 WHERE token=:t AND is_revoked=0
    rowcount==1 者勝出；並發的第二個請求 rowcount==0 → 走「已撤銷被重用」分支。

    重用偵測：token 存在但已撤銷卻又被拿來用 → 視為 token 可能外洩，
    立刻撤銷該使用者「所有」refresh token（強制全裝置重新登入）。
    """
    # 原子搶佔（先標記撤銷，勝者才能換發）
    claimed = (
        db.query(models.RefreshToken)
        .filter(
            models.RefreshToken.token == token_string,
            models.RefreshToken.is_revoked == False,  # noqa: E712
        )
        .update({models.RefreshToken.is_revoked: True}, synchronize_session=False)
    )
    db.commit()

    row = (
        db.query(models.RefreshToken)
        .filter(models.RefreshToken.token == token_string)
        .first()
    )

    if claimed != 1:
        if row is not None:
            # token 存在但已被撤銷 → 重用！撤銷全家（reuse detection）
            revoke_all_user_tokens(db, row.user_id)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Refresh token reuse detected; all sessions revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 勝出者：檢查過期與使用者狀態（過期就不換發，token 已撤銷無妨）
    expires_at_aware = row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
    if expires_at_aware < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.query(models.User).filter(models.User.id == row.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or disabled",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def revoke_refresh_token(db: Session, token_string: str) -> bool:
    """
    Revoke a refresh token (e.g., on logout).

    Args:
        db: Database session
        token_string: The refresh token string to revoke

    Returns:
        True if token was found and revoked, False otherwise
    """
    refresh_token = db.query(models.RefreshToken).filter(
        models.RefreshToken.token == token_string
    ).first()

    if refresh_token:
        refresh_token.is_revoked = True
        db.commit()
        return True

    return False


def revoke_all_user_tokens(db: Session, user_id: str) -> int:
    """
    Revoke all refresh tokens for a user (e.g., on password change or account compromise).

    Args:
        db: Database session
        user_id: The user's ID

    Returns:
        Number of tokens revoked
    """
    count = db.query(models.RefreshToken).filter(
        models.RefreshToken.user_id == user_id,
        models.RefreshToken.is_revoked == False
    ).update({"is_revoked": True})

    db.commit()
    return count


def cleanup_expired_tokens(db: Session) -> int:
    """
    Delete expired refresh tokens from database (should be run periodically).

    Args:
        db: Database session

    Returns:
        Number of tokens deleted
    """
    deleted = db.query(models.RefreshToken).filter(
        models.RefreshToken.expires_at < datetime.now(timezone.utc)
    ).delete()

    db.commit()
    return deleted
