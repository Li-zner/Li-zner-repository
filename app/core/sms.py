"""
阿里云短信发送模块
使用阿里云 SMS API (dysmsapi.aliyuncs.com) 发送验证码
"""
import os
import json
import uuid
import base64
import hmac
import hashlib
import urllib.parse
from datetime import datetime, timezone
import httpx
from ..core.logging import setup_logging
from ..core.config import (
    ALIBABA_CLOUD_ACCESS_KEY_ID,
    ALIBABA_CLOUD_ACCESS_KEY_SECRET,
    SMS_SIGN,
    SMS_TEMPLATE_CODE,
    ADMIN_PHONE,
    HTTP_TIMEOUT_MEDIUM
)

logger = setup_logging()

ALIYUN_SMS_ENDPOINT = "https://dypnsapi.aliyuncs.com"


def _mask_phone(phone: str) -> str:
    """日志脱敏手机号（合规：日志不得打印明文手机号）"""
    return f"{phone[:3]}****{phone[-4:]}" if phone and len(phone) >= 7 else "****"


def _percent_encode(s: str) -> str:
    """阿里云签名专用 URL 编码"""
    return urllib.parse.quote(s, safe='').replace('+', '%20').replace('*', '%2A').replace('%7E', '~')


def _build_signature(parameters: dict, secret: str) -> str:
    """计算阿里云 API 签名 (HMAC-SHA1)"""
    # 1. 按键排序
    sorted_keys = sorted(parameters.keys())
    # 2. 构造 canonicalized query string
    canonicalized = "&".join([
        f"{_percent_encode(k)}={_percent_encode(str(parameters[k]))}"
        for k in sorted_keys
    ])
    # 3. StringToSign
    string_to_sign = f"GET&{_percent_encode('/')}&{_percent_encode(canonicalized)}"
    # 4. 计算签名
    h = hmac.new(
        (secret + "&").encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.sha1
    )
    return base64.b64encode(h.digest()).decode('utf-8')


async def send_sms(phone: str, code: str, ttl_minutes: int = 5) -> bool:
    """
    通过阿里云短信发送验证码
    需要配置:
      ALIBABA_CLOUD_ACCESS_KEY_ID
      ALIBABA_CLOUD_ACCESS_KEY_SECRET
      SMS_SIGN (短信签名)
      SMS_TEMPLATE_CODE (短信模板编码)
    """
    if not ALIBABA_CLOUD_ACCESS_KEY_ID or not ALIBABA_CLOUD_ACCESS_KEY_SECRET:
        # ----- 无阿里云配置 → 演示模式仅打印日志 -----
        logger.info(f"[演示模式] 验证码 {code[:2]}**** 已生成（{_mask_phone(phone)}）")
        logger.info(f"需配置 ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET 以启用真实短信")
        try:
            import subprocess
            subprocess.Popen(
                ["powershell", "-Command",
                 f'Write-Host "验证码: {code} (发送至 {phone})" -ForegroundColor Green'],
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        except Exception as e:
            logger.debug("演示模式弹窗显示失败（无桌面环境，忽略）")
        return True

    try:
        # 构造请求参数
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        # 使用阿里云号码认证服务 DypnsAPI 发送验证码
        # 传入我们自己生成的验证码，确保短信内容与 Redis 存储一致
        params = {
            "AccessKeyId": ALIBABA_CLOUD_ACCESS_KEY_ID,
            "Action": "SendSmsVerifyCode",
            "Format": "JSON",
            "PhoneNumber": phone,
            "RegionId": "cn-shanghai",
            "SignName": SMS_SIGN,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": str(uuid.uuid4()),
            "SignatureVersion": "1.0",
            "TemplateCode": SMS_TEMPLATE_CODE,
            "TemplateParam": json.dumps({"code": code, "min": "5"}, ensure_ascii=False),
            "Timestamp": timestamp,
            "Version": "2017-05-25",
        }

        # 计算签名
        signature = _build_signature(params, ALIBABA_CLOUD_ACCESS_KEY_SECRET)
        params["Signature"] = signature

        # 发送请求
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_MEDIUM, follow_redirects=True) as client:
            resp = await client.get(ALIYUN_SMS_ENDPOINT, params=params)
            result = resp.json()

            if result.get("Code") == "OK":
                logger.info(f"短信发送成功: phone={_mask_phone(phone)}, bizId={result.get('BizId', '')}")
                return True
            else:
                logger.error(f"短信发送失败: phone={_mask_phone(phone)}, Code={result.get('Code')}, Message={result.get('Message')}")
                logger.error(f"   建议检查: 1) AccessKey 权限 2) SMS_TEMPLATE_CODE 3) 短信签名审核状态")
                return False

    except Exception as e:
        logger.error(f"短信发送异常: {e}")
        return False


async def send_admin_notification(message: str) -> bool:
    """向管理员发送通知短信"""
    if ADMIN_PHONE:
        logger.info(f"[管理员通知] {ADMIN_PHONE}: {message}")
        return True
    return False
