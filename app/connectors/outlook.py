"""
Microsoft Outlook connector — sends emails from the escalation risk analyzer.

Three backends (tried in priority order):

  1. **macOS Outlook app** (recommended, zero-setup):
     Uses AppleScript to send email through the locally installed and
     signed-in Outlook for Mac.  No Azure registration, no OAuth, no
     Conditional-Access issues.  Set OUTLOOK_SEND_VIA_APP=true in .env.

  2. **Power Automate webhook**:
     Set POWER_AUTOMATE_WEBHOOK_URL in .env.  Requires a Power Automate
     Premium license for the HTTP trigger connector.

  3. **MS Graph API** (legacy / fallback):
     Requires an Azure App Registration with Mail.Send delegated permission
     and user OAuth sign-in.  Blocked by Conditional-Access in many
     enterprise tenants.

When none is configured the connector disables itself and the UI falls
back to copy-to-clipboard / mailto mode.
"""
from __future__ import annotations

import asyncio
import html as html_mod
import logging
import platform
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AUTH_BASE = "https://login.microsoftonline.com"

_IS_MACOS = platform.system() == "Darwin"
_OUTLOOK_APP = Path("/Applications/Microsoft Outlook.app")


class OutlookConnector:
    """Manages email sending via macOS Outlook app, Power Automate, or MS Graph."""

    def __init__(self) -> None:
        self._app_enabled = (
            settings.outlook_send_via_app
            and _IS_MACOS
            and _OUTLOOK_APP.exists()
        )
        self._pa_enabled = bool(settings.power_automate_webhook_url)
        self._graph_enabled = bool(
            settings.ms_graph_client_id and settings.ms_graph_tenant_id
        )
        self._token: Optional[str] = None

        if self._app_enabled:
            logger.info(
                "Outlook connector enabled via macOS Outlook app (AppleScript)"
            )
        elif self._pa_enabled:
            logger.info("Outlook connector enabled via Power Automate webhook")
        elif self._graph_enabled:
            logger.info(
                "Outlook connector enabled via MS Graph (tenant: %s)",
                settings.ms_graph_tenant_id,
            )
        else:
            logger.info(
                "Outlook connector disabled — set OUTLOOK_SEND_VIA_APP=true, "
                "POWER_AUTOMATE_WEBHOOK_URL, or MS_GRAPH_CLIENT_ID"
            )

    @property
    def enabled(self) -> bool:
        return self._app_enabled or self._pa_enabled or self._graph_enabled

    @property
    def mode(self) -> str:
        if self._app_enabled:
            return "outlook_app"
        if self._pa_enabled:
            return "power_automate"
        if self._graph_enabled:
            return "ms_graph"
        return "disabled"

    @property
    def needs_oauth(self) -> bool:
        return self._graph_enabled and not self._app_enabled and not self._pa_enabled

    # ── macOS Outlook app backend (AppleScript) ──────────────────────

    @staticmethod
    def _escape_applescript(text: str) -> str:
        """Escape a string for safe embedding in AppleScript."""
        return text.replace("\\", "\\\\").replace('"', '\\"')

    async def _app_send_email(
        self,
        to: list[str],
        cc: list[str],
        subject: str,
        body_html: str,
        importance: str = "normal",
    ) -> dict[str, Any]:
        html_file = None
        try:
            html_file = tempfile.NamedTemporaryFile(
                suffix=".html", delete=False, mode="w", encoding="utf-8",
            )
            html_file.write(body_html)
            html_file.close()
            html_path = html_file.name

            to_block = "\n".join(
                f'make new to recipient at msg with properties '
                f'{{email address:{{address:"{self._escape_applescript(addr)}"}}}}'
                for addr in to if addr
            )
            cc_block = "\n".join(
                f'make new cc recipient at msg with properties '
                f'{{email address:{{address:"{self._escape_applescript(addr)}"}}}}'
                for addr in cc if addr
            )

            importance_map = {"high": "high", "low": "low"}
            importance_as = importance_map.get(importance, "normal")

            script = f'''
tell application "Microsoft Outlook"
    set htmlContent to (read POSIX file "{html_path}" as «class utf8»)
    set msg to make new outgoing message with properties {{subject:"{self._escape_applescript(subject)}", content:htmlContent, priority:{importance_as} priority}}
    {to_block}
    {cc_block}
    send msg
end tell
'''
            result = await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                err = result.stderr.strip()
                logger.error("AppleScript send failed: %s", err)
                raise RuntimeError(f"Outlook app send failed: {err}")

            logger.info("Email sent via Outlook app to %s (cc: %s)", to, cc)
            return {
                "status": "sent",
                "to": to,
                "cc": cc,
                "subject": subject,
                "method": "outlook_app",
            }
        finally:
            if html_file:
                Path(html_file.name).unlink(missing_ok=True)

    async def _app_create_draft(
        self,
        to: list[str],
        cc: list[str],
        subject: str,
        body_html: str,
    ) -> dict[str, Any]:
        html_file = None
        try:
            html_file = tempfile.NamedTemporaryFile(
                suffix=".html", delete=False, mode="w", encoding="utf-8",
            )
            html_file.write(body_html)
            html_file.close()
            html_path = html_file.name

            to_block = "\n".join(
                f'make new to recipient at msg with properties '
                f'{{email address:{{address:"{self._escape_applescript(addr)}"}}}}'
                for addr in to if addr
            )
            cc_block = "\n".join(
                f'make new cc recipient at msg with properties '
                f'{{email address:{{address:"{self._escape_applescript(addr)}"}}}}'
                for addr in cc if addr
            )

            script = f'''
tell application "Microsoft Outlook"
    set htmlContent to (read POSIX file "{html_path}" as «class utf8»)
    set msg to make new outgoing message with properties {{subject:"{self._escape_applescript(subject)}", content:htmlContent}}
    {to_block}
    {cc_block}
    open msg
end tell
'''
            result = await asyncio.to_thread(
                subprocess.run,
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                err = result.stderr.strip()
                logger.error("AppleScript draft failed: %s", err)
                raise RuntimeError(f"Outlook app draft failed: {err}")

            logger.info("Draft opened in Outlook app for %s", to)
            return {
                "status": "draft_created",
                "draft_id": None,
                "web_link": "",
                "note": "Draft opened in Outlook for Mac — review and send when ready.",
                "method": "outlook_app",
            }
        finally:
            if html_file:
                Path(html_file.name).unlink(missing_ok=True)

    # ── Power Automate backend ────────────────────────────────────────

    async def _pa_send_email(
        self,
        to: list[str],
        cc: list[str],
        subject: str,
        body_html: str,
        importance: str = "normal",
    ) -> dict[str, Any]:
        payload = {
            "to": ";".join(to),
            "cc": ";".join(cc) if cc else "",
            "subject": subject,
            "body": body_html,
            "importance": importance,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                settings.power_automate_webhook_url,
                json=payload,
            )
            if resp.status_code in (200, 202):
                logger.info(
                    "Email sent via Power Automate to %s (cc: %s)", to, cc
                )
                return {
                    "status": "sent", "to": to, "cc": cc, "subject": subject,
                    "method": "power_automate",
                }
            logger.error(
                "Power Automate webhook returned %s: %s",
                resp.status_code, resp.text[:500],
            )
            raise RuntimeError(
                f"Power Automate webhook failed (HTTP {resp.status_code})."
            )

    async def _pa_create_draft(
        self,
        to: list[str],
        cc: list[str],
        subject: str,
        body_html: str,
    ) -> dict[str, Any]:
        payload = {
            "to": settings.power_automate_sender_email or ";".join(to),
            "cc": "",
            "subject": f"[DRAFT] {subject}",
            "body": (
                '<div style="background:#fff3cd;padding:12px;border-radius:6px;'
                'margin-bottom:16px;color:#856404;font-size:0.9em">'
                "<strong>DRAFT:</strong> Forward to: "
                f"{', '.join(to)}"
                f"{'<br>CC: ' + ', '.join(cc) if cc else ''}"
                "</div>" + body_html
            ),
            "importance": "normal",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                settings.power_automate_webhook_url, json=payload,
            )
            if resp.status_code in (200, 202):
                return {
                    "status": "draft_created", "draft_id": None,
                    "web_link": "",
                    "note": "Draft sent to your inbox. Forward to recipients when ready.",
                    "method": "power_automate",
                }
            raise RuntimeError(
                f"Power Automate webhook failed (HTTP {resp.status_code})"
            )

    # ── MS Graph backend (legacy) ─────────────────────────────────────

    def get_auth_url(self, state: str = "") -> str:
        if not self._graph_enabled:
            raise RuntimeError("MS Graph is not configured")
        scopes = settings.ms_graph_scopes.replace(" ", "%20")
        return (
            f"{AUTH_BASE}/{settings.ms_graph_tenant_id}/oauth2/v2.0/authorize"
            f"?client_id={settings.ms_graph_client_id}"
            f"&response_type=code"
            f"&redirect_uri={settings.ms_graph_redirect_uri}"
            f"&scope={scopes}"
            f"&state={state}"
            f"&response_mode=query"
        )

    async def exchange_code(self, code: str) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{AUTH_BASE}/{settings.ms_graph_tenant_id}/oauth2/v2.0/token",
                data={
                    "client_id": settings.ms_graph_client_id,
                    "client_secret": settings.ms_graph_client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.ms_graph_redirect_uri,
                    "scope": settings.ms_graph_scopes,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            return {
                "access_token": data["access_token"],
                "expires_in": data.get("expires_in", 3600),
                "user": await self._get_me(data["access_token"]),
            }

    async def _get_me(self, token: str) -> dict[str, str]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GRAPH_BASE}/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                d = resp.json()
                return {
                    "display_name": d.get("displayName", ""),
                    "email": d.get("mail") or d.get("userPrincipalName", ""),
                }
            return {}

    def set_token(self, token: str) -> None:
        self._token = token

    async def _graph_send_email(
        self,
        to: list[str],
        cc: list[str],
        subject: str,
        body_html: str,
        importance: str = "normal",
        save_to_sent: bool = True,
        token: Optional[str] = None,
    ) -> dict[str, Any]:
        access_token = token or self._token
        if not access_token:
            raise PermissionError(
                "No Outlook access token. Complete OAuth sign-in first."
            )
        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [
                {"emailAddress": {"address": addr}} for addr in to if addr
            ],
            "importance": importance,
        }
        if cc:
            message["ccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in cc if addr
            ]
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GRAPH_BASE}/me/sendMail",
                json={"message": message, "saveToSentItems": save_to_sent},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 401:
                raise PermissionError("Outlook token expired. Re-authenticate.")
            if resp.status_code == 403:
                raise PermissionError("Insufficient permissions for Mail.Send.")
            resp.raise_for_status()
        logger.info("Email sent via MS Graph to %s (cc: %s)", to, cc)
        return {"status": "sent", "to": to, "cc": cc, "subject": subject,
                "method": "ms_graph"}

    async def _graph_create_draft(
        self,
        to: list[str],
        cc: list[str],
        subject: str,
        body_html: str,
        token: Optional[str] = None,
    ) -> dict[str, Any]:
        access_token = token or self._token
        if not access_token:
            raise PermissionError("No Outlook access token.")
        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [
                {"emailAddress": {"address": addr}} for addr in to if addr
            ],
        }
        if cc:
            message["ccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in cc if addr
            ]
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{GRAPH_BASE}/me/messages",
                json=message,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code in (401, 403):
                raise PermissionError("Outlook auth error.")
            resp.raise_for_status()
            data = resp.json()
        logger.info("Draft created via MS Graph (id: %s)", data.get("id", "?"))
        return {
            "status": "draft_created",
            "draft_id": data.get("id"),
            "web_link": data.get("webLink", ""),
            "method": "ms_graph",
        }

    # ── Public API (routes through the active backend) ────────────────

    async def send_email(
        self,
        to: list[str],
        cc: list[str],
        subject: str,
        body_html: str,
        body_text: str = "",
        importance: str = "normal",
        save_to_sent: bool = True,
        token: Optional[str] = None,
    ) -> dict[str, Any]:
        if self._app_enabled:
            return await self._app_send_email(
                to=to, cc=cc, subject=subject,
                body_html=body_html, importance=importance,
            )
        if self._pa_enabled:
            return await self._pa_send_email(
                to=to, cc=cc, subject=subject,
                body_html=body_html, importance=importance,
            )
        if self._graph_enabled:
            return await self._graph_send_email(
                to=to, cc=cc, subject=subject,
                body_html=body_html, importance=importance,
                save_to_sent=save_to_sent, token=token,
            )
        raise RuntimeError("No email backend configured.")

    async def create_draft(
        self,
        to: list[str],
        cc: list[str],
        subject: str,
        body_html: str,
        token: Optional[str] = None,
    ) -> dict[str, Any]:
        if self._app_enabled:
            return await self._app_create_draft(
                to=to, cc=cc, subject=subject, body_html=body_html,
            )
        if self._pa_enabled:
            return await self._pa_create_draft(
                to=to, cc=cc, subject=subject, body_html=body_html,
            )
        if self._graph_enabled:
            return await self._graph_create_draft(
                to=to, cc=cc, subject=subject,
                body_html=body_html, token=token,
            )
        raise RuntimeError("No email backend configured.")
