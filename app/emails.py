"""Transactional email through Resend: OTP verification and password reset codes."""
from __future__ import annotations

import asyncio
import logging

import resend

from .config import settings

log = logging.getLogger("saferoad.email")
resend.api_key = settings.resend_api_key or None


def _html(title: str, code: str, intro: str) -> str:
    return f"""
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;color:#0f172a">
  <div style="font-weight:800;font-size:20px;margin-bottom:24px">SafeRoad</div>
  <h1 style="font-size:22px;margin:0 0 12px">{title}</h1>
  <p style="font-size:15px;line-height:1.5;color:#475569;margin:0 0 20px">{intro}</p>
  <div style="font-size:34px;letter-spacing:10px;font-weight:800;background:#f1f5f9;border-radius:12px;padding:18px 0;text-align:center">{code}</div>
  <p style="font-size:13px;color:#64748b;margin-top:20px">This code expires in 10 minutes. If you did not request it, you can ignore this email.</p>
</div>"""


async def send_code(to: str, code: str, purpose: str) -> bool:
    """Returns True if the email was handed to Resend. Never raises."""
    if purpose == "reset":
        subject, title, intro = f"{code} is your SafeRoad password reset code", "Reset your password", "Use this code to set a new password for your SafeRoad account."
    else:
        subject, title, intro = f"{code} is your SafeRoad verification code", "Verify your email", "Enter this code in SafeRoad to finish creating your account."
    if not settings.resend_api_key:
        log.warning("RESEND_API_KEY not set; code for %s is %s", to, code)
        return False
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: resend.Emails.send({"from": settings.email_from, "to": [to], "subject": subject, "html": _html(title, code, intro)}))
        return True
    except Exception as e:  # noqa: BLE001
        log.error("resend failed for %s: %s", to, str(e)[:200])
        return False
