# -*- coding: utf-8 -*-
"""
Bilibili 扫码登录（仅网络部分；Cookie 持久化由插件 KV 存储完成）。

使用 B站 Web 端 passport 二维码接口：
  - 生成二维码: https://passport.bilibili.com/x/passport-login/web/qrcode/generate
  - 轮询扫码:   https://passport.bilibili.com/x/passport-login/web/qrcode/poll

轮询 code 含义：
  86101 未扫码 / 86090 已扫码待确认 / 0 登录成功 / 86038 二维码已失效
登录成功时 SESSDATA、bili_jct、DedeUserID 位于响应头 Set-Cookie 中。
"""

import asyncio
import base64
import io
import re
import time
from typing import Awaitable, Callable, Optional

import aiohttp
import qrcode

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID")


def _headers(extra: Optional[dict] = None) -> dict:
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
    }
    if extra:
        headers.update(extra)
    return headers


class BiliLogin:
    """B站扫码登录。所有方法均为纯网络操作，不落盘。"""

    # ------------------------------------------------------------------
    async def create_qrcode(self) -> tuple[str, str]:
        """返回 (qrcode_key, 二维码PNG的base64)。"""
        async with aiohttp.ClientSession(headers=_headers()) as session:
            async with session.get(GEN_URL) as resp:
                resp.raise_for_status()
                body = await resp.json(content_type=None)
        if body.get("code") != 0:
            raise RuntimeError(f"生成二维码失败: {body.get('message')}")
        data = body["data"]
        url, key = data["url"], data["qrcode_key"]

        qr = qrcode.QRCode(version=None, box_size=8, border=2)
        qr.add_data(url)
        img = qr.make_image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return key, base64.b64encode(buf.getvalue()).decode("ascii")

    # ------------------------------------------------------------------
    async def poll_until_done(
        self,
        qrcode_key: str,
        timeout: int = 180,
        interval: int = 2,
        on_status: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Optional[dict]:
        """轮询扫码状态，成功返回 cookies dict（含 SESSDATA/bili_jct/DedeUserID）。

        失败（失效/超时/未知）返回 None。
        """
        deadline = time.time() + timeout
        last_state = ""

        async with aiohttp.ClientSession(headers=_headers()) as session:
            while time.time() < deadline:
                try:
                    async with session.get(
                        POLL_URL,
                        params={"qrcode_key": qrcode_key, "source": "main-fe-header"},
                    ) as resp:
                        body = await resp.json(content_type=None)
                        code = body.get("code")

                        if code == 86090:  # 已扫码，等待确认
                            if last_state != "scanned" and on_status:
                                last_state = "scanned"
                                await on_status("📱 已扫码，请在手机上确认登录。")
                        elif code == 0:
                            cookies = self._parse_cookies(resp)
                            if cookies.get("SESSDATA"):
                                return cookies
                        elif code == 86038:  # 二维码已失效
                            return None
                except Exception:
                    pass  # 网络抖动，继续轮询
                await asyncio.sleep(interval)
        return None

    @staticmethod
    def _parse_cookies(resp: aiohttp.ClientResponse) -> dict:
        cookies = {}
        for header_value in resp.headers.getall("Set-Cookie", []):
            first = header_value.split(";", 1)[0]
            if "=" not in first:
                continue
            name, value = first.split("=", 1)
            name = name.strip()
            if name in COOKIE_KEYS:
                cookies[name] = value.strip()
        return cookies

    # ------------------------------------------------------------------
    async def check(self, cookies: dict) -> tuple[bool, str, str]:
        """校验 Cookie，返回 (is_login, uname, mid)。"""
        async with aiohttp.ClientSession(headers=_headers()) as session:
            async with session.get(NAV_URL, cookies=cookies) as resp:
                body = await resp.json(content_type=None)
        data = body.get("data", {})
        if body.get("code") == 0 and data.get("isLogin"):
            return True, str(data.get("uname", "?")), str(data.get("mid", "?"))
        return False, "", ""
