# -*- coding: utf-8 -*-
"""
astrbot_plugin_bili_up —— AstrBot v4 插件：QQ 内扫码绑定 B站并交互式投稿视频。

兼容目标：AstrBot >= 4.0（Star 插件体系，依赖内置 Main 星提供 SessionWaiter 触发）。

命令（统一以 /biliup 为前缀；旧版 /b站xxx、/上传视频 仍可用作别名）：
  /biliup绑定      生成 B站登录二维码并轮询，成功后 Cookie 持久化（按会话隔离：每个群/每个私聊独立）
  /biliup状态      校验当前会话绑定的 B站账号是否有效
  /biliup解绑      删除当前会话的绑定（不影响其他会话）
  /biliup分区      查看常用分区(tid)列表
  /biliup上传视频  投稿流程：发视频 → 标题 → 原创/转载 → 分区 → 简介 → 标签 → 封面(可选) → 动态(可选) → 联合创作者(可选) → 回复【投稿】确认

会话隔离：B站凭证按 unified_msg_origin（群号/私聊对端）存储，
A 群上传的视频只会投稿到 A 群绑定的账号；B 群与私聊各自独立、互不通用。

建议私聊使用；群聊时流程期间同群其他消息会被会话等待器接管。
"""

import asyncio
import logging
import os
import re

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import File as FileComp
from astrbot.api.message_components import Image, Video
from astrbot.api.util import SessionController, session_waiter

from .bili_login import BiliLogin
from .bili_upload import BiliUploadError, bili_upload

module_logger = logging.getLogger("astrbot_plugin_bili_up")

CANCEL_WORDS = {"取消", "算了", "停止", "退出"}
CONFIRM_WORDS = {"投稿", "确认投稿", "上传"}

COMMON_TIDS = {
    "动画": 1, "MAD·AMV": 24, "音乐": 3, "原创音乐": 28, "翻唱": 31,
    "游戏": 4, "单机游戏": 17, "电子竞技": 65, "手机游戏": 172,
    "知识": 36, "科学科普": 201, "社科人文": 124, "校园学习": 208,
    "科技": 188, "数码": 95, "软件应用": 230,
    "生活": 160, "日常": 21, "美食": 211, "鬼畜": 119,
    "时尚": 155, "运动": 234, "汽车": 223, "影视": 181, "娱乐": 71,
}


class BiliUpPlugin(star.Star):
    """QQ 内扫码绑定 B站账号，并交互式投稿视频到 Bilibili。"""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context)
        cfg = config or {}
        try:
            self.recv_timeout = int(cfg.get("recv_timeout", 300))
            self.max_video_mb = int(cfg.get("max_video_mb", 8192))
            self.upload_timeout = int(cfg.get("upload_timeout", 7200))
        except (TypeError, ValueError):
            self.recv_timeout, self.max_video_mb, self.upload_timeout = 300, 8192, 7200

        self.login = BiliLogin()
        self._flows: dict[str, str] = {}  # umo -> 流程 owner uid（防并发）

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _uid(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:
            return "unknown"

    @staticmethod
    def _cookie_key(event: AstrMessageEvent) -> str:
        """Cookie 存储键：按会话（群/私聊）完全隔离。

        unified_msg_origin 唯一标识一个会话（如 aiocqhttp:GroupMessage:<群号>
        或 aiocqhttp:FriendMessage:<QQ号>），因此：
        - A 群与 B 群各自绑定各自的 B站账号；
        - 私聊与任意群聊也相互独立。
        """
        raw = str(event.unified_msg_origin)
        safe = re.sub(r"[^0-9A-Za-z_\-]", "_", raw)
        return f"cookies_{safe}"

    async def _load_cookies(self, event: AstrMessageEvent) -> dict | None:
        try:
            return await self.get_kv_data(self._cookie_key(event), None)
        except Exception as e:
            module_logger.warning("读取 KV 失败: %s", e)
            return None

    async def _save_cookies(self, event: AstrMessageEvent, cookies: dict) -> None:
        try:
            await self.put_kv_data(self._cookie_key(event), cookies)
        except Exception as e:
            module_logger.warning("写入 KV 失败: %s", e)
            raise

    async def _delete_cookies(self, event: AstrMessageEvent) -> None:
        try:
            await self.delete_kv_data(self._cookie_key(event))
        except Exception as e:
            module_logger.warning("删除 KV 失败: %s", e)

    async def _send(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(MessageEventResult().message(text))

    async def _send_qr(self, event: AstrMessageEvent, text: str, qr_b64: str) -> None:
        chain = MessageEventResult().message(text)
        chain.chain.append(Image.fromBase64(qr_b64))
        await event.send(chain)

    @staticmethod
    def _is_media(comp) -> bool:
        return isinstance(comp, (FileComp, Video))

    async def _media_to_path(self, comp) -> str:
        """将 File/Video/Image 组件转为本地文件路径（必要时自动下载）。"""
        if isinstance(comp, FileComp):
            path = await comp.get_file()
            if not path:
                raise ValueError("无法获取该文件（NapCat 未返回可用的下载地址）")
            return path
        return await comp.convert_to_file_path()

    def _cleanup_media(self, *paths) -> None:
        for p in paths:
            if not p:
                continue
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError as e:
                module_logger.debug("清理临时文件失败 %s: %s", p, e)

    # ------------------------------------------------------------------
    # /biliup绑定
    # ------------------------------------------------------------------
    @filter.command("biliup绑定", alias={"b站绑定", "biliup 绑定"})
    async def cmd_bind(self, event: AstrMessageEvent) -> None:
        event.should_call_llm(False)
        try:
            key, qr_b64 = await self.login.create_qrcode()
        except Exception as e:
            await self._send(event, f"❌ 生成二维码失败: {e}")
            event.stop_event()
            return

        await self._send_qr(
            event,
            "请使用【哔哩哔哩】App 扫码登录（3 分钟内有效）。\n"
            "注意：本会话（群/私聊）将独立绑定该 B站账号，与其他会话互不影响。",
            qr_b64,
        )

        async def on_status(text: str) -> None:
            await self._send(event, text)

        cookies = await self.login.poll_until_done(
            key, timeout=180, interval=2, on_status=on_status,
        )
        if cookies and cookies.get("SESSDATA"):
            try:
                await self._save_cookies(event, cookies)
            except Exception as e:
                await self._send(event, f"❌ 绑定信息保存失败: {e}")
                event.stop_event()
                return
            await self._send(
                event,
                "✅ B站绑定成功！\n发送 /biliup上传视频 开始投稿，/biliup状态 查看账号。",
            )
        else:
            await self._send(event, "❌ 登录超时或失败，请重新发送 /biliup绑定")
        event.stop_event()

    # ------------------------------------------------------------------
    # /biliup状态 /biliup解绑 /biliup分区
    # ------------------------------------------------------------------
    @filter.command("biliup状态", alias={"b站状态", "biliup 状态"})
    async def cmd_status(self, event: AstrMessageEvent) -> None:
        event.should_call_llm(False)
        cookies = await self._load_cookies(event)
        if not cookies:
            await self._send(event, "⚠️ 本会话尚未绑定，请发送 /biliup绑定")
            event.stop_event()
            return
        try:
            ok, uname, mid = await self.login.check(cookies)
        except Exception as e:
            await self._send(event, f"⚠️ 已保存 Cookie，但网络校验失败: {e}")
            event.stop_event()
            return
        if ok:
            await self._send(event, f"✅ 已绑定 B站账号：{uname}（mid={mid}）")
        else:
            await self._send(event, "❌ Cookie 已失效，请重新发送 /biliup绑定")
        event.stop_event()

    @filter.command("biliup解绑", alias={"b站解绑", "biliup 解绑"})
    async def cmd_unbind(self, event: AstrMessageEvent) -> None:
        event.should_call_llm(False)
        cookies = await self._load_cookies(event)
        await self._delete_cookies(event)
        if cookies:
            await self._send(event, "✅ 已解绑本会话并删除 Cookie（不影响其他会话的绑定）。")
        else:
            await self._send(event, "⚠️ 本会话当前没有绑定记录。")
        event.stop_event()

    @filter.command("biliup分区", alias={"b站分区", "biliup 分区"})
    async def cmd_tids(self, event: AstrMessageEvent) -> None:
        event.should_call_llm(False)
        lines = [f"{name}: {tid}" for name, tid in COMMON_TIDS.items()]
        await self._send(
            event,
            "常用分区(tid)：\n" + "\n".join(lines)
            + "\n\n也可直接输入数字 tid；完整列表以 bilibili 官网分区页为准。",
        )
        event.stop_event()

    # ------------------------------------------------------------------
    # /biliup上传视频 —— 交互式投稿主流程
    # ------------------------------------------------------------------
    @filter.command("biliup上传视频", alias={"上传视频", "biliup 上传视频"})
    async def cmd_upload(self, event: AstrMessageEvent) -> None:
        event.should_call_llm(False)
        umo = event.unified_msg_origin
        uid = self._uid(event)
        cookies = await self._load_cookies(event)
        if not cookies:
            await self._send(event, "⚠️ 本会话尚未绑定 B站，请先发送 /biliup绑定 扫码登录。")
            event.stop_event()
            return
        if umo in self._flows:
            await self._send(event, "⚠️ 当前会话已有投稿流程进行中，请先完成或回复【取消】。")
            event.stop_event()
            return

        self._flows[umo] = uid
        try:
            await self._run_upload_flow(event, umo, uid, cookies)
        except TimeoutError:
            await self._send(event, "⏱ 等待超时，本次投稿已结束。可重新发送 /biliup上传视频")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            module_logger.exception("投稿流程异常")
            try:
                await self._send(event, f"❌ 流程出错：{e}")
            except Exception:
                pass
        finally:
            self._flows.pop(umo, None)
        event.stop_event()

    # ------------------------------------------------------------------
    async def _run_upload_flow(
        self,
        event: AstrMessageEvent,
        umo: str,
        uid: str,
        cookies: dict,
    ) -> None:
        owner = uid
        ctx: dict = {
            "owner": owner,
            "step": "media",      # media/title/copyright/tid/desc/tags/cover/dynamic/confirm
            "meta": {"uid": uid},
            "cookies": cookies,
            "media_tries": 0,
            "done": False,
        }

        async def pump(
            controller: SessionController,
            ev: AstrMessageEvent,
        ) -> None:
            """每收到一条新消息调用一次。"""
            if ctx["done"]:
                controller.stop()
                return

            # 群聊：只接收发起者本人的消息；他人消息静默顺延等待
            if str(ev.get_sender_id()) != owner:
                controller.keep(self.recv_timeout, reset_timeout=True)
                return

            text = (ev.get_message_str() or "").strip()

            # 流程中不允许执行其他命令
            if text.startswith("/"):
                await self._send(ev, "⚠️ 投稿流程进行中，请按提示输入，或回复【取消】结束。")
                controller.keep(self.recv_timeout, reset_timeout=True)
                return

            if text in CANCEL_WORDS:
                await self._send(ev, "已取消，本次投稿未提交。")
                ctx["done"] = True
                controller.stop()
                return

            try:
                await self._pump_step(controller, ev, ctx, text)
            except Exception as e:
                module_logger.exception("pump 步骤异常")
                await self._send(ev, f"❌ 处理出错：{e}")
                ctx["done"] = True
                controller.stop()

        @session_waiter(self.recv_timeout)
        async def flow_waiter(
            controller: SessionController,
            ev: AstrMessageEvent,
        ) -> None:
            await pump(controller, ev)

        # 发送引导语并进入等待
        await self._send(
            event,
            "📤 请发送要投稿的视频。\n"
            "提示：建议以【文件】形式发送（勿选“作为短视频发送”），大文件请耐心等待上传完成。\n"
            "任意步骤可回复【取消】终止。",
        )
        await flow_waiter(event)

    # ------------------------------------------------------------------
    async def _pump_step(
        self,
        controller: SessionController,
        ev: AstrMessageEvent,
        ctx: dict,
        text: str,
    ) -> None:
        meta: dict = ctx["meta"]
        step: str = ctx["step"]

        if step == "media":
            comp = next((c for c in ev.get_messages() if self._is_media(c)), None)
            if comp is None:
                ctx["media_tries"] += 1
                if ctx["media_tries"] >= 3:
                    await self._send(ev, "❌ 多次未收到视频文件，流程已结束。可重新发送 /biliup上传视频")
                    ctx["done"] = True
                    controller.stop()
                    return
                await self._send(ev, "❌ 未检测到视频/文件消息段，请重新发送视频文件。")
                controller.keep(self.recv_timeout, reset_timeout=True)
                return

            # 下载可能耗时较长，先延长会话保活
            controller.keep(1800, reset_timeout=True)
            await self._send(ev, "⏳ 正在接收视频文件，请稍候…")
            try:
                path = await self._media_to_path(comp)
                size_mb = os.path.getsize(path) / 1024 / 1024
                if size_mb > self.max_video_mb:
                    self._cleanup_media(path)
                    raise ValueError(f"视频 {size_mb:.1f}MB 超过上限 {self.max_video_mb}MB")
            except Exception as e:
                await self._send(ev, f"❌ 视频接收失败：{e}\n请重新发送（或回复【取消】）。")
                controller.keep(self.recv_timeout, reset_timeout=True)
                return

            meta["path"] = path
            meta["size_mb"] = size_mb
            ctx["step"] = "title"
            await self._send(ev, f"✅ 视频已接收（{size_mb:.1f}MB）。\n【1/9】请输入视频标题（不超过 80 字）：")

        elif step == "title":
            meta["title"] = text[:80]
            ctx["step"] = "copyright"
            await self._send(ev, "【2/9】该稿件为 原创 还是 转载？")

        elif step == "copyright":
            if "转载" in text:
                meta["copyright"] = 2
                ctx["step"] = "source"
                await self._send(ev, "【2.5/9】转载需注明出处，请输入原视频链接（回复【跳过】留空）：")
            elif "原创" in text:
                meta["copyright"] = 1
                ctx["step"] = "tid"
                await self._send(ev, "【3/9】请输入分区（分区名或数字 tid，/biliup分区 可查看列表）：")
            else:
                await self._send(ev, "❌ 请回复【原创】或【转载】。")

        elif step == "source":
            meta["source"] = "" if text == "跳过" else text
            ctx["step"] = "tid"
            await self._send(ev, "【3/9】请输入分区（分区名或数字 tid，/biliup分区 可查看列表）：")

        elif step == "tid":
            tid = self._resolve_tid(text)
            if tid is None:
                await self._send(ev, "⚠️ 无法识别该分区，请输入列表中的分区名或纯数字 tid。")
                controller.keep(self.recv_timeout, reset_timeout=True)
                return
            meta["tid"] = tid
            ctx["step"] = "desc"
            await self._send(ev, "【4/9】请输入视频简介（回复【跳过】将使用标题作为简介）：")

        elif step == "desc":
            meta["desc"] = meta["title"] if text in ("跳过", "") else text[:2000]
            ctx["step"] = "tags"
            await self._send(ev, "【5/9】请输入标签，多个用逗号分隔（最多 12 个）：")

        elif step == "tags":
            tag_list = [t.strip() for t in re.split(r"[,，、|]", text) if t.strip()][:12]
            if not tag_list:
                await self._send(ev, "⚠️ 未识别到标签，请用逗号分隔多个标签。")
                controller.keep(self.recv_timeout, reset_timeout=True)
                return
            meta["tags"] = ",".join(tag_list)
            ctx["step"] = "cover"
            await self._send(ev, "【6/9】请发送封面图片，或回复【跳过】不设置封面：")

        elif step == "cover":
            if text == "跳过":
                ctx["step"] = "dynamic"
                await self._send(ev, "【7/9】请输入投稿附带的粉丝动态文案（回复【跳过】不设置）：")
                return
            img = next((c for c in ev.get_messages() if isinstance(c, Image)), None)
            if img is None:
                await self._send(ev, "⚠️ 未检测到图片消息。请发送封面图片，或回复【跳过】。")
                controller.keep(self.recv_timeout, reset_timeout=True)
                return
            controller.keep(1800, reset_timeout=True)
            try:
                cover_path = await self._media_to_path(img)
                meta["cover"] = cover_path
                await self._send(ev, "✅ 封面已接收。")
            except Exception as e:
                await self._send(ev, f"⚠️ 封面接收失败（{e}），已跳过封面。")
            ctx["step"] = "dynamic"
            await self._send(ev, "【7/9】请输入投稿附带的粉丝动态文案（回复【跳过】不设置）：")

        elif step == "dynamic":
            meta["dynamic"] = "" if text == "跳过" else text
            ctx["step"] = "cooperate"
            await self._send(ev, "【可选】请输入联合创作者 UID（多个用逗号分隔，回复【跳过】不设置联合投稿）：")

        elif step == "cooperate":
            if text == "跳过":
                meta["cooperate_uids"] = []
            else:
                uids = [int(t.strip()) for t in re.split(r"[,，\s]", text) if t.strip().isdigit()]
                if not uids:
                    await self._send(ev, "⚠️ 未识别到有效 UID，请输入纯数字 UID（多个用逗号分隔），或回复【跳过】。")
                    controller.keep(self.recv_timeout, reset_timeout=True)
                    return
                meta["cooperate_uids"] = uids
            ctx["step"] = "confirm"
            size_mb = meta.get("size_mb", 0)
            lines = [
                "📋 投稿信息确认：",
                f"标题：{meta.get('title')}",
                f"类型：{'原创' if meta.get('copyright') == 1 else '转载'}"
                + (f"（来源：{meta.get('source')}）" if meta.get("source") else ""),
                f"分区：tid={meta.get('tid')}",
                f"标签：{meta.get('tags')}",
                f"简介：{(meta.get('desc') or '')[:100]}"
                + ("…" if len(meta.get('desc') or '') > 100 else ""),
                f"封面：{'已设置' if meta.get('cover') else '无'}",
                f"动态：{meta.get('dynamic') or '无'}",
            ]
            if meta.get("cooperate_uids"):
                lines.append(f"联合创作者：{', '.join(str(u) for u in meta['cooperate_uids'])}")
            lines.extend([
                f"视频：{os.path.basename(meta.get('path', ''))}（{size_mb:.1f}MB）",
                "——————————————",
                "确认无误请回复【投稿】；回复其他内容取消。",
            ])
            summary = "\n".join(lines)
            await self._send(ev, summary)

        elif step == "confirm":
            if not any(w in text for w in CONFIRM_WORDS):
                await self._send(ev, "已取消，未投稿。")
                ctx["done"] = True
                controller.stop()
                return
            # 开始上传：长时间操作，延长会话保活
            controller.keep(self.upload_timeout, reset_timeout=True)
            loop = asyncio.get_running_loop()
            last_pct = {"v": 0}

            def on_progress(done: int, total: int) -> None:
                pct = int(done * 100 / total) if total else 100
                if pct - last_pct["v"] >= 10:
                    last_pct["v"] = pct
                    asyncio.run_coroutine_threadsafe(
                        self._send(ev, f"⏫ 上传进度：{pct}%（{done}/{total} 分片）"),
                        loop,
                    )

            await self._send(ev, "🚀 开始上传，大文件耗时较长，期间无需操作…")
            try:
                result = await asyncio.to_thread(
                    bili_upload, ctx["cookies"], meta, on_progress,
                )
            except Exception as e:
                module_logger.exception("投稿失败")
                await self._send(ev, f"❌ 投稿失败：{e}\n可检查 Cookie 是否失效或稍后重试。")
                ctx["done"] = True
                controller.stop()
                return

            bvid = ""
            if isinstance(result, dict):
                bvid = result.get("bvid") or ""
                data = result.get("data")
                if not bvid and isinstance(data, dict):
                    bvid = data.get("bvid") or ""
            coop_note = ""
            if isinstance(result, dict) and result.get("_coop_failed"):
                coop_note = "（联合投稿失败，已自动降级为普通投稿；可能账号未开通联合投稿权限）\n"
            if bvid:
                await self._send(
                    ev,
                    f"✅ 投稿成功！\n{coop_note}"
                    f"https://www.bilibili.com/video/{bvid}\n"
                    "稿件已进入审核流程，可在 B站创作中心查看进度。",
                )
            else:
                await self._send(ev, f"✅ 上传完成，返回：{result}")
            self._cleanup_media(meta.get("path"), meta.get("cover"))
            ctx["done"] = True
            controller.stop()

        # 默认：本步未终结会话，顺延等待窗口
        if not ctx["done"] and not controller.future.done():
            controller.keep(self.recv_timeout, reset_timeout=True)

    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_tid(raw: str) -> int | None:
        raw = raw.strip()
        if raw.isdigit():
            return int(raw)
        if raw in COMMON_TIDS:
            return COMMON_TIDS[raw]
        for name, tid in COMMON_TIDS.items():
            if raw in name or name in raw:
                return tid
        return None
