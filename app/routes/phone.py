"""手机号注册/登录/绑定 + 短信配置诊断

从 app/main.py 纯移动而来（2026-08-31 模块化，行为等价，零逻辑改动）。
含验证码防爆破三层（发送频率 / 校验错误次数锁定 / TTL）。
"""
import json
import secrets

import asyncpg
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Request

from ..core.logging import setup_logging
from ..core.redis import get_redis
from ..core.db import get_pool
from ..core.config import (
    SMS_CODE_EXPIRE_SECONDS, SMS_SEND_MIN_INTERVAL, SMS_SEND_HOUR_LIMIT,
    SMS_ATTEMPT_LIMIT, SMS_ATTEMPT_LOCK_SECONDS, PASSWORD_MAX_BYTES,
)
from ..core.password import hash_password
from ..core.sms import _mask_phone
from ..middleware.auth import get_current_user, create_token_pair
from .auth import _client_ip

logger = setup_logging()

router = APIRouter()

# 单 IP 日发送上限（2026-09-09 审查 P1）：per-phone 限流可被换号绕过——
# 1000 个号 = 5000 条/小时计费短信 + 短信轰炸；本键兜底单 IP 日总量
SMS_IP_DAY_LIMIT = 20


class PhoneSendCodeRequest(BaseModel):
    """发送验证码请求"""
    phone: str = Field(..., pattern=r'^1\d{10}$')


class PhoneRegisterRequest(BaseModel):
    """手机号注册请求"""
    phone: str = Field(..., pattern=r'^1\d{10}$')
    code: str = Field(..., min_length=1)
    password: str = ""
    agree: bool = False
    display_name: str = ""
    email: str = ""
    extra: dict = {}


class PhoneLoginRequest(BaseModel):
    """手机号验证码登录请求"""
    phone: str = Field(..., pattern=r'^1\d{10}$')
    code: str = Field(..., min_length=1)


class BindPhoneRequest(BaseModel):
    """绑定手机号请求"""
    phone: str = Field(..., pattern=r'^1\d{10}$')
    code: str = Field(..., min_length=1)


# ---------- 验证码防爆破（P0 #6：防短信轰炸 / 暴力枚举）----------
async def _check_sms_rate(phone: str):
    """发送频率限制：同一手机号 1 分钟 1 次、1 小时 5 次（原子，防并发绕过）"""
    r = await get_redis()
    min_key = f"phone_sms_min:{phone}"
    hour_key = f"phone_sms_hour:{phone}"
    # 小时计数：原子 INCR（并发各取唯一值），首次置 TTL；超阈值回滚并拒绝
    hour = await r.incr(hour_key)
    if hour == 1:
        await r.expire(hour_key, 3600)
    if hour > SMS_SEND_HOUR_LIMIT:
        await r.decr(hour_key)
        raise HTTPException(429, "短信发送次数已达上限，请稍后再试")
    # 分钟冷却：SETNX 原子占位（原 GET→SETNX 两步并发可双双放行 → 短信轰炸，P1 修复）
    ok_min = await r.set(min_key, "1", nx=True, ex=SMS_SEND_MIN_INTERVAL)
    if not ok_min:
        await r.decr(hour_key)   # 回滚小时计数，避免无效占位累计
        raise HTTPException(429, "发送太频繁，请 1 分钟后再试")


async def _check_sms_rate_ip(ip: str):
    """IP 维度日上限（2026-09-09 审查 P1）：换号绕过 per-phone 限流时的计费兜底"""
    r = await get_redis()
    day_key = f"phone_sms_ip_day:{ip}"
    count = await r.incr(day_key)
    if count == 1:
        await r.expire(day_key, 86400)
    if count > SMS_IP_DAY_LIMIT:
        await r.decr(day_key)
        raise HTTPException(429, "今日发送次数已达上限，请明日再试")


async def _record_sms_attempt(phone: str, success: bool):
    """记录验证码校验结果：失败累计，达到上限锁定；成功清零"""
    r = await get_redis()
    attempt_key = f"phone_code_attempts:{phone}"
    if success:
        await r.delete(attempt_key)
        return
    count = await r.incr(attempt_key)
    if count == 1:
        await r.expire(attempt_key, SMS_ATTEMPT_LOCK_SECONDS)
    if count >= SMS_ATTEMPT_LIMIT:
        await r.setex(f"phone_code_lock:{phone}", SMS_ATTEMPT_LOCK_SECONDS, "1")
        await r.delete(attempt_key)


async def _verify_phone_code(phone: str, code: str):
    """校验验证码（含错误次数防爆破）；成功时删除验证码与错误计数"""
    r = await get_redis()
    if await r.get(f"phone_code_lock:{phone}"):
        raise HTTPException(429, "验证码错误次数过多，已锁定 15 分钟")
    stored = await r.get(f"phone_code:{phone}")
    if not stored:
        raise HTTPException(400, "验证码已过期，请重新发送")
    if stored != code:
        await _record_sms_attempt(phone, False)
        raise HTTPException(400, "验证码错误")
    await _record_sms_attempt(phone, True)
    await r.delete(f"phone_code:{phone}")


@router.post("/api/phone/send-code")
async def send_phone_code(payload: PhoneSendCodeRequest, request: Request):
    """发送手机验证码（阿里云短信；Pydantic 校验，A24）"""
    phone = payload.phone
    # IP 维度兜底限流（2026-09-09 审查 P1：换号绕过 per-phone 限流的场景）
    await _check_sms_rate_ip(_client_ip(request))
    # 发送频率限制（P0 #6 防短信轰炸：1 分钟 1 次 / 1 小时 5 次）
    await _check_sms_rate(phone)
    # 生成随机6位验证码
    import random
    code = str(random.randint(100000, 999999))
    # 存入 Redis（有效期走配置常量，P2 #10）
    r = await get_redis()
    await r.setex(f"phone_code:{phone}", SMS_CODE_EXPIRE_SECONDS, code)
    # 通过阿里云短信发送
    from ..core.sms import send_sms
    sent = await send_sms(phone, code)
    if sent:
        # 日志脱敏（kefa 红线：日志禁明文手机号，2026-09-07 审查 P1）
        logger.info(f"验证码已发送: phone={_mask_phone(phone)}")
        return {"message": "验证码已发送", "phone": phone}
    else:
        # 短信发送失败 → 仅在日志记录发送失败，不泄露验证码
        logger.warning(f"短信发送失败，降级到演示模式: phone={_mask_phone(phone)}")
        logger.info(f"[演示] 验证码已发送至演示日志（不返回客户端）")
        return {"message": "验证码已发送（演示模式）", "phone": phone}


@router.post("/api/phone/register")
async def phone_register(payload: PhoneRegisterRequest):
    """手机号注册（支持扩展字段；Pydantic 校验，A24）
    账号即手机号；不设置密码时不设可用密码（随机占位），登录走短信验证码。
    """
    phone = payload.phone
    code = payload.code
    password = payload.password
    agree = payload.agree
    display_name = payload.display_name
    email = payload.email
    extra = payload.extra

    if not agree:
        raise HTTPException(400, "请阅读并同意用户协议")
    # 默认密码随机化（2026-09-07 审查 P1）：原硬编码 123456789 + 手机号可作账号
    # 密码直登 → 知道手机号即可无短信登录所有未改密账号。随机密码不可登录，
    # 用户走验证码登录；如需密码登录在「设置」自行设置。
    if not password:
        password = secrets.token_urlsafe(12)
    if len(password) < 6:
        raise HTTPException(400, "密码至少6位")
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise HTTPException(400, f"密码长度不能超过 {PASSWORD_MAX_BYTES} 字节")  # C7：策略上限（SHA-256 预处理）
    # 校验验证码（含错误次数防爆破，P0 #6）
    await _verify_phone_code(phone, code)
    # 检查手机号是否已注册
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT username FROM users WHERE phone=$1", phone)
        if existing:
            raise HTTPException(400, "该手机号已注册")
        # 账号即手机号（2026-09-09 主人定稿：去掉 phone_ 前缀，直接用手机号做用户名；
        # 纯数字用户名与 GitHub 登录名不可能冲突——GitHub 用户名不允许纯数字）
        username = phone
        hashed = hash_password(password)
        async with conn.transaction():
            # 多租户：注册时自动创建个人租户（后续可做管理员分配）
            tenant = await conn.fetchrow(
                "INSERT INTO tenants (name) VALUES ($1) RETURNING id",
                f"租户_{username}",
            )
            # P3 修复：并发同号注册捕获唯一约束转 400（原先 500）
            try:
                await conn.execute(
                    "INSERT INTO users (username, hashed_password, phone, display_name, email, extra, role, tenant_id) "
                    "VALUES ($1, $2, $3, $4, $5, $6, 'user', $7)",
                    username, hashed, phone, display_name or "", email or "",
                    json.dumps(extra), tenant["id"],
                )
            except asyncpg.exceptions.UniqueViolationError:
                raise HTTPException(400, "该手机号已注册")
    # 验证码已由 _verify_phone_code 校验并删除
    pair = create_token_pair(username)
    # 审计：注册成功（含租户；手机号脱敏入审计与日志）
    from ..core.audit import audit
    await audit(username, "register", {"phone": phone, "tenant_id": tenant["id"]})
    logger.info(f"手机号注册成功: phone={_mask_phone(phone)}, username={username}, display_name={display_name or '(未设置)'}")
    # 默认密码已随机化，不存在"默认密码提示"（键保留维持 API 形状兼容）
    return {"access_token": pair["access_token"], "refresh_token": pair["refresh_token"], "token_type": "bearer", "username": username, "display_name": display_name or "", "is_new": True, "default_password_hint": False}


@router.post("/api/phone/login")
async def phone_login(payload: PhoneLoginRequest):
    """手机号验证码登录（自动注册；Pydantic 校验，A24）
    自动注册的账号不设可用密码（随机占位），登录一律走短信验证码。
    """
    phone = payload.phone
    code = payload.code
    # 校验验证码（含错误次数防爆破，P0 #6）
    await _verify_phone_code(phone, code)
    # 查找或自动创建用户（自动注册放事务内，P0 #12：INSERT 成功后仅签发 token 阶段失败会回滚）
    pool = await get_pool()
    is_new = False
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT username, hashed_password, is_active FROM users WHERE phone=$1", phone)
        if user and not user["is_active"]:
            # 禁用账号拒绝再签发 token（2026-09-07 审查 P2：原先仅 get_current_user 兜底）
            raise HTTPException(403, "账号已被禁用")
        if not user:
            username = phone
            # 自动注册默认密码随机化（与 phone_register 同一修复，2026-09-07 审查 P1）
            hashed = hash_password(secrets.token_urlsafe(12))
            # 自动注册：账号即手机号，默认密码 123456789（Bug #5：与 phone_register 一致同步建租户）
            # P3 修复（二次遍历）：并发同号注册的败者复用胜者账号签发 token。
            # try/except 必须包在事务外——唯一冲突会使事务中止，事务内再 SELECT 只会抛
            # "current transaction is aborted"；异常向外传播时事务整体回滚，不留孤儿租户。
            try:
                async with conn.transaction():
                    tenant = await conn.fetchrow(
                        "INSERT INTO tenants (name) VALUES ($1) RETURNING id",
                        f"租户_{username}",
                    )
                    await conn.execute(
                        "INSERT INTO users (username, hashed_password, phone, tenant_id) "
                        "VALUES ($1, $2, $3, $4)",
                        username, hashed, phone, tenant["id"]
                    )
            except asyncpg.exceptions.UniqueViolationError:
                winner = await conn.fetchrow("SELECT username FROM users WHERE phone=$1", phone)
                if not winner:
                    raise HTTPException(409, "注册冲突，请重试")
                username = winner["username"]
                logger.info(f"并发自动注册败者复用账号: phone={_mask_phone(phone)}, username={username}")
            else:
                is_new = True
                logger.info(f"手机号自动注册: phone={_mask_phone(phone)}, username={username}")
        else:
            username = user["username"]
    token = create_token_pair(username)
    return {"access_token": token["access_token"], "refresh_token": token["refresh_token"], "token_type": "bearer", "username": username, "is_new": is_new, "default_password_hint": is_new}


@router.post("/api/user/bind-phone")
async def bind_phone(payload: BindPhoneRequest, current_user: dict = Depends(get_current_user)):
    """绑定手机号到当前账号：验验证码 → 写入 phone 并解除 GitHub 试用额度限制（A24）

    用户名策略（用户定稿 2026-09-09）：绑定成功且目标用户名（手机号本身，无前缀）
    未被占时，username 同步改绑并级联所有 user_id 引用表（无外键，同事务手动维护）；
    目标用户名被占则仅绑定不改名。绑定后 GitHub 登录走 github_id 回访同一账号。
    """
    phone = payload.phone
    code = payload.code
    # 校验验证码（含错误次数防爆破，P0 #6）
    await _verify_phone_code(phone, code)
    username = current_user["username"]
    new_username = phone
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 该手机号不能已被其他账号绑定
        occupied = await conn.fetchval(
            "SELECT 1 FROM users WHERE phone = $1 AND username <> $2", phone, username
        )
        if occupied:
            raise HTTPException(400, "该手机号已被其他账号绑定")
        renamed = False
        taken = await conn.fetchval(
            "SELECT 1 FROM users WHERE username = $1 AND username <> $2",
            new_username, username,
        )
        # 绑定 + 解除限制 + 改名级联同一事务：任一步失败整体回滚，防账号与数据引用撕裂
        async with conn.transaction():
            if taken:
                result = await conn.execute(
                    "UPDATE users SET phone = $1, quota_limited = FALSE, used_requests = 0, "
                    "updated_at = CURRENT_TIMESTAMP WHERE username = $2 AND (phone IS NULL OR phone = $1)",
                    phone, username,
                )
            else:
                renamed = username != new_username
                result = await conn.execute(
                    "UPDATE users SET username = $1, phone = $2, quota_limited = FALSE, used_requests = 0, "
                    "updated_at = CURRENT_TIMESTAMP WHERE username = $3 AND (phone IS NULL OR phone = $2)",
                    new_username, phone, username,
                )
                if "UPDATE 1" in result and renamed:
                    for table in ("conversation_memories", "invoices", "payment_orders",
                                  "transaction_logs", "user_profiles", "user_wallets",
                                  "message_ratings", "audit_logs"):
                        await conn.execute(
                            f"UPDATE {table} SET user_id = $1 WHERE user_id = $2",
                            new_username, username,
                        )
                    # user_usage 以 username 为键（09-09 补：改名级联此前漏了它，
                    # 改名后当日用量计数归零重计）
                    await conn.execute(
                        "UPDATE user_usage SET username = $1 WHERE username = $2",
                        new_username, username,
                    )
        if "UPDATE 0" in result:
            raise HTTPException(400, "该账号已绑定其他手机号")
    # 改名后旧 JWT 的 sub 指向旧用户名，签发新 token 对；缓存键随用户名变化，新旧都失效
    final_username = new_username if renamed else username
    pair = create_token_pair(final_username)
    from ..core.quota import invalidate_user_cache
    await invalidate_user_cache(username)
    if renamed:
        await invalidate_user_cache(new_username)
    # 验证码已由 _verify_phone_code 校验并删除
    from ..core.audit import audit
    await audit(final_username, "phone_bind", {"phone": phone})
    logger.info(f"手机号绑定成功: username={username} -> {final_username}, phone={_mask_phone(phone)}, renamed={renamed}")
    return {
        "message": "手机号绑定成功，已解除限制" + ("，登录名已更新为手机号" if renamed else ""),
        "quota_limited": False,
        "username": final_username,
        "renamed": renamed,
        "access_token": pair["access_token"],
        "refresh_token": pair["refresh_token"],
        "token_type": "bearer",
    }


@router.get("/api/sms/check-config")
async def sms_check_config(current_user: dict = Depends(get_current_user)):
    """检查短信配置状态（管理员用）"""
    if current_user.get("role") != "admin":
        raise HTTPException(403, "仅管理员可查看")
    from ..core.config import (
        ALIBABA_CLOUD_ACCESS_KEY_ID, ALIBABA_CLOUD_ACCESS_KEY_SECRET,
        SMS_SIGN, SMS_TEMPLATE_CODE, ADMIN_PHONE
    )
    # 脱敏显示
    key_id = ALIBABA_CLOUD_ACCESS_KEY_ID
    key_id_masked = key_id[:4] + "****" + key_id[-4:] if len(key_id) > 8 else "未配置"
    secret = ALIBABA_CLOUD_ACCESS_KEY_SECRET
    secret_masked = secret[:2] + "****" + secret[-2:] if len(secret) > 4 else "未配置"
    return {
        "access_key_id": key_id_masked,
        "access_key_secret_configured": bool(ALIBABA_CLOUD_ACCESS_KEY_SECRET),
        "sms_sign": SMS_SIGN,
        "sms_template_code": SMS_TEMPLATE_CODE,
        "admin_phone": ADMIN_PHONE,
        "all_configured": bool(ALIBABA_CLOUD_ACCESS_KEY_ID and ALIBABA_CLOUD_ACCESS_KEY_SECRET and SMS_TEMPLATE_CODE != "SMS_000000")
    }
