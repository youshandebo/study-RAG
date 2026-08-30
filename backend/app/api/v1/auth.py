# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""会员账户体系：注册 / 登录 / 当前用户。JWT 无状态签名，角色 kind=user。

鉴权策略：注册用户数 > 0（或 MEMBERSHIP_MODE=1）即全站强制登录；
全新部署无用户时保持开放演示，注册第一个账号后自动切换为正式模式。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator

import app.db.relational as repo
from app.core.membership import plan_for
from app.core.security import hash_password, jwt_sign, jwt_verify, verify_password

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def strong_enough(cls, v: str) -> str:
        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("密码需同时包含字母和数字")
        return v


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class AuthUser:
    """请求级用户上下文；anonymous=True 表示开放演示模式。"""

    def __init__(self, user_id: str | None, email: str | None, tier: str | None,
                 anonymous: bool = False) -> None:
        self.id = user_id
        self.email = email
        self.tier = tier or ("guest" if anonymous else "free")
        self.anonymous = anonymous

    def to_public(self) -> dict:
        return {
            "anonymous": self.anonymous,
            "id": self.id,
            "email": self.email,
            "tier": self.tier,
            "plan": plan_for(self.tier),
        }


async def _resolve_user(payload: dict | None) -> AuthUser | None:
    if not payload or payload.get("kind") != "user":
        return None
    user = await repo.get_user_by_id(str(payload.get("sub", "")))
    if user is None:
        return None
    return AuthUser(user["id"], user["email"], user.get("tier"))


async def current_user_optional(
    authorization: str = Header(default=""),
    x_auth_token: str = Header(default=""),
) -> AuthUser:
    """开放演示模式下返回 anonymous 用户；正式模式 401 拒绝匿名。"""
    from app.core.membership import auth_required

    token = x_auth_token or (authorization[7:] if authorization.startswith("Bearer ") else "")
    payload = jwt_verify(token) if token else None
    user = await _resolve_user(payload)
    if user is not None:
        return user
    if await auth_required():
        raise HTTPException(status_code=401, detail="请先登录（未登录无法访问课程内容）")
    return AuthUser(None, None, "guest", anonymous=True)


def sign_user_token(user_id: str, tier: str, ttl: int = 30 * 24 * 3600) -> str:
    return jwt_sign({"sub": user_id, "tier": tier, "kind": "user"}, ttl)


async def _needs_setup() -> bool:
    """首次部署判定：没有任何注册用户且管理面板未设过密码。"""
    from app.core.runtime_config import get_runtime_config

    if await repo.count_users() > 0:
        return False
    return not (get_runtime_config().get("admin_password_hash") or __import__("os").getenv("ADMIN_PASSWORD", "").strip())


class SetupBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def strong_enough(cls, v: str) -> str:
        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("密码需同时包含字母和数字")
        return v


@router.get("/auth/setup/status")
async def setup_status():
    return {"needs_setup": await _needs_setup()}


@router.post("/auth/setup")
async def setup_initialize(body: SetupBody):
    """网页初始化向导：创建平台管理员账号（旗舰档）+ 设置管理面板口令。

    仅在首次部署（needs_setup）时可用；此后此端点永久 403。
    """
    if not await _needs_setup():
        raise HTTPException(status_code=403, detail="系统已初始化，请直接登录")
    email = body.email.lower()
    if await repo.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="该邮箱已存在，请直接登录")
    from app.core.runtime_config import set_admin_password

    user = await repo.create_user(email, hash_password(body.password), tier="max")
    set_admin_password(body.password)  # /admin 面板用同一口令
    return {
        "token": sign_user_token(user["id"], user["tier"]),
        "user": {"id": user["id"], "email": user["email"], "tier": user["tier"]},
    }


@router.post("/auth/register")
async def register(body: RegisterBody):
    email = body.email.lower()
    if await repo.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录")
    user = await repo.create_user(email, hash_password(body.password), tier="free")
    return {
        "token": sign_user_token(user["id"], user["tier"]),
        "user": {"id": user["id"], "email": user["email"], "tier": user["tier"]},
    }


@router.post("/auth/login")
async def login(body: LoginBody):
    email = body.email.lower()
    user = await repo.get_user_by_email(email)
    if user is None or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")
    return {
        "token": sign_user_token(user["id"], user["tier"]),
        "user": {"id": user["id"], "email": user["email"], "tier": user["tier"]},
    }


@router.get("/auth/me")
async def me(user: AuthUser = Depends(current_user_optional)):
    return user.to_public()
