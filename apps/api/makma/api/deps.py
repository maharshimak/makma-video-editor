"""Shared API dependencies: auth, current user, project access."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Project, User

ALGORITHM = "HS256"
DEV_EMAIL = "you@local"


def hash_password(password: str) -> str:
    from passlib.context import CryptContext

    return CryptContext(schemes=["bcrypt"], deprecated="auto").hash(password)


def verify_password(password: str, hashed: str) -> bool:
    from passlib.context import CryptContext

    if not hashed:
        return False
    return CryptContext(schemes=["bcrypt"], deprecated="auto").verify(password, hashed)


def create_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.access_token_hours),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


def _dev_user(db: Session) -> User:
    """A stable local account so the editor is usable before signing up.

    Only ever created when `dev_autologin` is on, which production disables.
    """
    user = db.query(User).filter(User.email == DEV_EMAIL).first()
    if user is None:
        user = User(email=DEV_EMAIL, name="Local User")
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    if token:
        user_id = decode_token(token)
        if user_id:
            user = db.get(User, user_id)
            if user is not None:
                return user
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired. Sign in again.",
        )

    # Belt and braces. Startup already refuses this combination, but a
    # misconfigured process must never hand out a shared account.
    if settings.dev_autologin and not settings.is_production:
        return _dev_user(db)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
    )


def get_project(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.owner_id != user.id:
        # Do not distinguish "not found" from "not yours".
        raise HTTPException(status_code=404, detail="Project not found.")
    return project
