"""手机号注册/登录/绑定 + 短信配置诊断

从 app/main.py 纯移动而来（2026-08-31 模块化，行为等价，零逻辑改动）。
含验证码防爆破三层（发送频率 / 校验错误次数锁定 / TTL）。
"""
import json
import secrets

import asyncpg
from pydantic import BaseModel, Field, field_validator
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

# 验证码校验原子脚本在 routes/sms_lua.py（逐字未改），单 IP 日额度上限与其
# 发送前预检在 services/sms_ip_quota.py：2026-09-22 外移，本文件贴 600 行门禁，
# 口径同 AUTH-1。

# 改名迁移 Lua：日额度键、文件归属反向索引和文件元数据在单个脚本内完成迁移。
# 旧键暂时保留，直到数据库事务确认成功后再清理，便于提交失败时可靠回滚。
_MIGRATE_IDENTITY_LUA = """
local src_user = ARGV[1]
local dst_user = ARGV[2]
local update_meta = ARGV[3] == '1'
local function merge_counter(src_key, dst_key)
    local src = redis.call('GET', src_key)
    if not src then return end
    local src_ttl = redis.call('TTL', src_key)
    local dst = redis.call('GET', dst_key)
    local src_num = tonumber(src)
    local dst_num = tonumber(dst)
    if not dst or not dst_num or (src_num and src_num > dst_num) then
        if src_ttl > 0 then redis.call('SET', dst_key, src, 'EX', src_ttl)
        else redis.call('SET', dst_key, src) end
    elseif src_ttl > 0 and redis.call('TTL', dst_key) < 0 then
        redis.call('EXPIRE', dst_key, src_ttl)
    end
end
merge_counter(KEYS[1], KEYS[2])
merge_counter(KEYS[3], KEYS[4])
local fids = redis.call('SMEMBERS', KEYS[5])
local migrated = 0
for _, fid in ipairs(fids) do
    redis.call('SADD', KEYS[6], fid)
    local meta_key = 'file:' .. fid .. ':meta'
    local raw = redis.call('GET', meta_key)
    if raw then
        local ok, meta = pcall(cjson.decode, raw)
        if update_meta and ok and type(meta) == 'table' and meta['uploaded_by'] == src_user then
            meta['uploaded_by'] = dst_user
            local ttl = redis.call('TTL', meta_key)
            local encoded = cjson.encode(meta)
            if ttl > 0 then redis.call('SET', meta_key, encoded, 'EX', ttl)
            else redis.call('SET', meta_key, encoded) end
            migrated = migrated + 1
        end
    end
end
local set_ttl = redis.call('TTL', KEYS[5])
if set_ttl > 0 then
    local dst_ttl = redis.call('TTL', KEYS[6])
    if dst_ttl < 0 or dst_ttl < set_ttl then redis.call('EXPIRE', KEYS[6], set_ttl) end
end
return migrated
"""

_MARK_IDENTITY_MIGRATION_LUA = """
redis.call('SET', KEYS[1], 'src:' .. ARGV[2], 'EX', tonumber(ARGV[3]))
redis.call('SET', KEYS[2], 'dst:' .. ARGV[1], 'EX', tonumber(ARGV[3]))
return 1
"""

_USER_RENAME_INTENT_TTL_SECONDS = 24 * 3600


from .sms_lua import (  # AUTH-1（09-20）：脚本外移，phone.py 曾破 600 行门禁
    SMS_IP_DAY_LUA, SMS_SEND_LUA, VERIFY_CODE_LUA,
)
from ..services.sms_ip_quota import (  # 09-22 审查 P2：IP 日额度预检与上限同处
    SMS_IP_DAY_LIMIT, assert_ip_day_headroom, ip_day_key,
)


class PhoneSendCodeRequest(BaseModel):
    """发送验证码请求"""
    phone: str = Field(..., pattern=r'^1\d{10}$')


class PhoneRegisterRequest(BaseModel):
    """手机号注册请求

    AUTH-5（09-20 审查）：三字段入库列无界且 profile 原样回吐，补齐与
    users.py 同口径上限；extra 按序列化后长度限。"""
    phone: str = Field(..., pattern=r'^1\d{10}$')
    code: str = Field(..., min_length=1, max_length=12)
    password: str = Field(default="", max_length=256)
    agree: bool = False
    display_name: str = Field(default="", max_length=128)
    email: str = Field(default="", max_length=254)
    extra: dict = {}

    @field_validator("extra")
    @classmethod
    def _cap_extra(cls, v: dict) -> dict:
        if len(json.dumps(v, ensure_ascii=False)) > 4096:
            raise ValueError("extra 过长")
        return v


class PhoneLoginRequest(BaseModel):
    """手机号验证码登录请求"""
    phone: str = Field(..., pattern=r'^1\d{10}$')
    code: str = Field(..., min_length=1, max_length=12)


class BindPhoneRequest(BaseModel):
    """绑定手机号请求"""
    phone: str = Field(..., pattern=r'^1\d{10}$')
    code: str = Field(..., min_length=1, max_length=12)


async def _check_sms_rate(phone: str):
    """发送频率限制：同一手机号 1 分钟 1 次、1 小时 5 次（单脚本原子，AUTH-1 收口）"""
    r = await get_redis()
    verdict = await r.eval(
        SMS_SEND_LUA, 2,
        f"phone_sms_min:{phone}",
        f"phone_sms_hour:{phone}",
        str(SMS_SEND_MIN_INTERVAL), str(SMS_SEND_HOUR_LIMIT), "3600",
    )
    if verdict == 0:
        raise HTTPException(429, "短信发送次数已达上限，请稍后再试")
    if verdict == -1:
        raise HTTPException(429, "发送太频繁，请 1 分钟后再试")


async def _check_sms_rate_ip(ip: str):
    """IP 维度日上限**落账**（2026-09-09 审查 P1）：换号绕过 per-phone 限流时的
    计费兜底；单脚本原子化同 AUTH-1。

    2026-09-22 审查 P2：调用点从"发送前"移到"短信确实发出去之后"——原来
    per-phone 分钟冷却抛 429、上游故障抛 503 时 IP 额度已经白扣，用户一条短信
    没收到却耗尽当日额度。防轰炸语义没丢：发送前先走 services.assert_ip_day_headroom
    只读预检（超额直接拒，不触发下发），落账仍是脚本内 INCR→超限 DECR 回滚，
    等价"先查后增"却把并发窗口收进脚本——预检与落账之间最多溢出"同时在飞"
    的条数，且溢出部分被回滚、不会累积。
    """
    r = await get_redis()
    verdict = await r.eval(
        SMS_IP_DAY_LUA, 1,
        ip_day_key(ip),
        str(SMS_IP_DAY_LIMIT), "86400",
    )
    if verdict == 0:
        raise HTTPException(429, "今日发送次数已达上限，请明日再试")


async def _verify_phone_code(phone: str, code: str):
    """原子校验验证码（含错误次数防爆破）；成功时消费验证码与错误计数"""
    r = await get_redis()
    status = await r.eval(
        VERIFY_CODE_LUA,
        3,
        f"phone_code_lock:{phone}",
        f"phone_code:{phone}",
        f"phone_code_attempts:{phone}",
        str(code),
        SMS_ATTEMPT_LIMIT,
        SMS_ATTEMPT_LOCK_SECONDS,
    )
    if status == "locked":
        raise HTTPException(429, "验证码错误次数过多，已锁定 15 分钟")
    if status == "expired":
        raise HTTPException(400, "验证码已过期，请重新发送")
    if status == "invalid":
        raise HTTPException(400, "验证码错误")
    if status != "ok":
        raise HTTPException(503, "验证码服务暂不可用，请稍后重试")


@router.post("/api/phone/send-code")
async def send_phone_code(payload: PhoneSendCodeRequest, request: Request):
    """发送手机验证码（阿里云短信；Pydantic 校验，A24）"""
    phone = payload.phone
    ip = _client_ip(request)
    # IP 维度兜底限流（2026-09-09 审查 P1：换号绕过 per-phone 限流的场景）。
    # 09-22 审查 P2：这里只做只读预检，额度实扣见下面发送成功分支
    await assert_ip_day_headroom(ip)
    # 发送频率限制（P0 #6 防短信轰炸：1 分钟 1 次 / 1 小时 5 次）
    await _check_sms_rate(phone)
    # 生成随机6位验证码（2026-09-12 清欠 P2：改 CSPRNG，与全文件 secrets 用法一致）
    code = str(secrets.randbelow(900000) + 100000)
    # 存入 Redis（有效期走配置常量，P2 #10）
    r = await get_redis()
    await r.setex(f"phone_code:{phone}", SMS_CODE_EXPIRE_SECONDS, code)
    # 通过阿里云短信发送
    from ..core.sms import send_sms
    sent = await send_sms(phone, code)
    if sent:
        # 短信确实出去了才扣 IP 日额度（2026-09-22 审查 P2，详见 _check_sms_rate_ip）
        await _check_sms_rate_ip(ip)
        # 日志脱敏（kefa 红线：日志禁明文手机号，2026-09-07 审查 P1）
        logger.info(f"验证码已发送: phone={_mask_phone(phone)}")
        return {"message": "验证码已发送", "phone": phone}
    # 发送失败（生产缺短信密钥 fail-closed / 上游故障）→ 明确 503，
    # 不谎报"已发送"让用户干等永不到达的验证码（2026-09-10 审查 P2）；
    # 同时清掉已写入的验证码，不在 Redis 留一枚用户收不到的有效码
    await r.delete(f"phone_code:{phone}")
    logger.warning(f"短信发送失败: phone={_mask_phone(phone)}")
    raise HTTPException(503, "短信服务暂不可用，请稍后重试")


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
    # 上次改名可能停在提交后的恢复窗口，先按数据库终态收敛 Redis 身份数据。
    await _recover_identity_migration(phone)
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
    async with pool.acquire(timeout=5) as conn:
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
    # 手机号即改名后的用户名，登录前先收敛可能遗留的改名迁移。
    await _recover_identity_migration(phone)
    # 校验验证码（含错误次数防爆破，P0 #6）
    await _verify_phone_code(phone, code)
    # 查找或自动创建用户（自动注册放事务内，P0 #12：INSERT 成功后仅签发 token 阶段失败会回滚）
    pool = await get_pool()
    is_new = False
    async with pool.acquire(timeout=5) as conn:
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


async def _cascade_rename_user(conn, old_username: str, new_username: str):
    """改名级联：user_id 引用表逐表迁移（无外键，同事务手动维护）。

    user_usage 以 username 为键（09-09 补：漏了它则改名后当日用量归零重计）。
    """
    for table in ("conversation_memories", "conversation_profiles",
                  "invoices", "payment_orders",
                  "transaction_logs", "user_profiles", "user_wallets",
                  "message_ratings", "audit_logs", "payment_attempts"):
        await conn.execute(
            f"UPDATE {table} SET user_id = $1 WHERE user_id = $2",
            new_username, old_username,
        )
    await conn.execute(
        "UPDATE user_usage SET username = $1 WHERE username = $2",
        new_username, old_username,
    )


def _identity_daily_keys(username: str) -> tuple[str, str]:
    """构造用户当日请求数/Tokens 两个 Redis 键，供改名迁移复用。"""
    from datetime import datetime, timezone as _tz
    today = datetime.now(_tz.utc).strftime("%Y-%m-%d")
    return f"daily_req:{username}:{today}", f"daily_token:{username}:{today}"


async def _mark_identity_migration(old_username: str, new_username: str) -> None:
    """原子写入双向迁移意图，任一路由可据用户名找到并恢复未完成迁移。"""
    r = await get_redis()
    await r.eval(
        _MARK_IDENTITY_MIGRATION_LUA,
        2,
        f"user_rename_intent:{old_username}",
        f"user_rename_intent:{new_username}",
        old_username,
        new_username,
        _USER_RENAME_INTENT_TTL_SECONDS,
    )


async def _migrate_identity_data(old_username: str, new_username: str,
                                 update_meta: bool = True) -> int:
    """原子迁移日额度、文件归属索引和文件元数据，旧键保留到事务确认。"""
    r = await get_redis()
    old_req, old_token = _identity_daily_keys(old_username)
    new_req, new_token = _identity_daily_keys(new_username)
    migrated = await r.eval(
        _MIGRATE_IDENTITY_LUA,
        6,
        old_req,
        new_req,
        old_token,
        new_token,
        f"file_owner:{old_username}",
        f"file_owner:{new_username}",
        old_username,
        new_username,
        "1" if update_meta else "0",
    )
    return int(migrated or 0)


async def _cleanup_identity_migration(old_username: str, new_username: str) -> None:
    """迁移确认后原子清理源键、反向索引和双向意图，避免旧身份继续生效。"""
    r = await get_redis()
    old_req, old_token = _identity_daily_keys(old_username)
    await r.delete(
        old_req,
        old_token,
        f"file_owner:{old_username}",
        f"user_rename_intent:{old_username}",
        f"user_rename_intent:{new_username}",
    )


async def _prepare_identity_migration(old_username: str, new_username: str) -> int:
    """数据库提交前执行 Redis 迁移；脚本报错时保留意图，交给恢复流程裁决。"""
    await _mark_identity_migration(old_username, new_username)
    return await _migrate_identity_data(old_username, new_username, update_meta=False)


async def _recover_identity_migration(username: str) -> None:
    """按数据库最终用户名裁决未完成迁移：已改名则完成，未改名则回滚。"""
    r = await get_redis()
    intent = await r.get(f"user_rename_intent:{username}")
    if not intent:
        return
    direction, _, other = str(intent).partition(":")
    if not other or direction not in ("src", "dst"):
        logger.error(f"用户名迁移意图损坏: user={username}")
        raise HTTPException(503, "账号迁移状态异常，请稍后重试")
    old_username, new_username = (
        (username, other) if direction == "src" else (other, username)
    )
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        rows = await conn.fetch(
            "SELECT username FROM users WHERE username = ANY($1::text[])",
            [old_username, new_username],
        )
    existing = {row["username"] for row in rows}
    if new_username in existing and old_username not in existing:
        await _migrate_identity_data(old_username, new_username)
        await _cleanup_identity_migration(old_username, new_username)
        logger.warning(f"已恢复数据库中已生效的改名迁移: {old_username} -> {new_username}")
        return
    if old_username in existing and new_username not in existing:
        await _migrate_identity_data(new_username, old_username)
        await _cleanup_identity_migration(new_username, old_username)
        logger.warning(f"已回滚数据库中未生效的改名迁移: {new_username} -> {old_username}")
        return
    logger.error(f"用户名迁移状态不明确: old={old_username}, new={new_username}")
    raise HTTPException(503, "账号迁移状态异常，请稍后重试")


async def _finalize_identity_migration(old_username: str, new_username: str) -> int:
    """数据库提交后再次合并迁移并清理旧键，覆盖提交窗口内的旧身份写入。"""
    migrated = await _migrate_identity_data(old_username, new_username)
    await _cleanup_identity_migration(old_username, new_username)
    return migrated


async def _apply_phone_binding(conn, phone: str, username: str,
                               new_username: str) -> tuple[bool, bool]:
    """在同一数据库事务中绑定手机号并准备 Redis 改名，返回改名与迁移状态。"""
    occupied = await conn.fetchval(
        "SELECT 1 FROM users WHERE phone = $1 AND username <> $2", phone, username
    )
    if occupied:
        raise HTTPException(400, "该手机号已被其他账号绑定")
    renamed = False
    redis_prepared = False
    taken = await conn.fetchval(
        "SELECT 1 FROM users WHERE username = $1 AND username <> $2",
        new_username, username,
    )
    try:
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
                    await _cascade_rename_user(conn, username, new_username)
                    await _prepare_identity_migration(username, new_username)
                    redis_prepared = True
    except asyncpg.UniqueViolationError as exc:
        # 唯一索引是并发绑定的最终防线；转成明确业务错误而不是 500。
        raise HTTPException(400, "该手机号已被其他账号绑定") from exc
    except Exception:
        # 提交结果不确定时只保留 Redis 迁移状态和双向意图，不能在这里猜测
        # 数据库是否已提交并删除意图；下一次登录/绑号按数据库终态收敛。
        logger.exception(
            f"改名事务异常，保留恢复意图: {username} -> {new_username}"
        )
        raise
    if "UPDATE 0" in result:
        raise HTTPException(400, "该账号已绑定其他手机号")
    return renamed, redis_prepared


@router.post("/api/user/bind-phone")
async def bind_phone(payload: BindPhoneRequest, current_user: dict = Depends(get_current_user)):
    """绑定手机号到当前账号：验验证码 → 写入 phone 并解除 GitHub 试用额度限制（A24）

    用户名策略（用户定稿 2026-09-09）：绑定成功且目标用户名（手机号本身，无前缀）
    未被占时，username 同步改绑并级联所有 user_id 引用表；
    目标用户名被占则仅绑定不改名。绑定后 GitHub 登录走 github_id 回访同一账号。
    """
    phone = payload.phone
    code = payload.code
    username = current_user["username"]
    await _recover_identity_migration(username)
    # 校验验证码（含错误次数防爆破，P0 #6）
    await _verify_phone_code(phone, code)
    new_username = phone
    pool = await get_pool()
    async with pool.acquire(timeout=5) as conn:
        renamed, redis_prepared = await _apply_phone_binding(
            conn, phone, username, new_username
        )
    # 改名后旧 JWT 的 sub 指向旧用户名，签发新 token 对；缓存键随用户名变化，新旧都失效
    final_username = new_username if renamed else username
    pair = create_token_pair(final_username)
    from ..core.quota import invalidate_user_cache
    await invalidate_user_cache(username)
    if renamed:
        await invalidate_user_cache(new_username)
        if redis_prepared:
            try:
                migrated = await _finalize_identity_migration(username, new_username)
                if migrated:
                    logger.info(f"已迁移文件归属 {migrated} 个: {username} -> {new_username}")
            except Exception as exc:
                # 不签发半迁移身份；意图仍在，用户可用手机号验证码登录触发恢复。
                logger.exception(
                    f"改名迁移收尾失败，等待后续自动恢复: {username} -> {new_username}"
                )
                raise HTTPException(
                    503, "账号迁移收尾失败，请使用手机号验证码登录"
                ) from exc
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
    return {
        "access_key_id": key_id_masked,
        "access_key_secret_configured": bool(ALIBABA_CLOUD_ACCESS_KEY_SECRET),
        "sms_sign": SMS_SIGN,
        "sms_template_code": SMS_TEMPLATE_CODE,
        "admin_phone": ADMIN_PHONE,
        "all_configured": bool(ALIBABA_CLOUD_ACCESS_KEY_ID and ALIBABA_CLOUD_ACCESS_KEY_SECRET and SMS_TEMPLATE_CODE != "SMS_000000")
    }
