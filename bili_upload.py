# -*- coding: utf-8 -*-
"""
Bilibili Web 端视频投稿（自实现，同步 requests 版）。

协议流程与当前 biliup 1.2.x (biliup/plugins/bili_webup.py, MIT) 保持一致：
  1. probe 探测线路（失败回退 upos bda2 线路）
  2. preupload 获取上传凭证 (endpoint/auth/upos_uri/biz_id/chunk_size)
  3. 分片 PUT 上传到 upos，完成后合并
  4. 可选：上传封面（自动裁剪 16:10）
  5. submit 提交稿件 (member.bilibili.com/x/vu/web/add)

本模块为同步代码（requests），请通过 asyncio.to_thread 在线程中调用。
"""

import base64
import io
import logging
import math
import os
import time
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter, Retry

logger = logging.getLogger("astrbot_plugin_bili_up.upload")

SESSION_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "Chrome/63.0.3239.108")

# 回退线路（probe 失败时使用）
_FALLBACK_LINE = {"os": "upos", "query": "upcdn=bda2&probe_version=20221109"}

# 封面 16:10 裁剪 + jpeg base64 上传
_COVER_DATA_PREFIX = "data:image/jpeg;base64,"


class BiliUploadError(RuntimeError):
    """携带 B站 返回信息的投稿异常。"""


def _make_session(cookies: dict) -> requests.Session:
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(total=5)))
    s.headers.update({
        "user-agent": SESSION_UA,
        "referer": "https://www.bilibili.com/",
        "connection": "keep-alive",
    })
    s.cookies.update(cookies)
    return s


def _json(resp: requests.Response, what: str = "API") -> dict:
    """解析 JSON 并统一报错。包含步骤上下文和响应体摘要。"""
    try:
        return resp.json()
    except Exception as e:
        body = resp.text[:300] if resp.text else "(空响应)"
        raise BiliUploadError(
            f"[{what}] 解析失败(HTTP {resp.status_code}): {e} | 响应: {body}"
        ) from e


def _bili_raise(ret: dict, what: str) -> None:
    if ret.get("code") != 0:
        msg = ret.get("message") or ret.get("msg") or str(ret)
        logger.error("%s 失败响应: %s", what, json.dumps(ret, ensure_ascii=False))
        raise BiliUploadError(f"{what}失败: {msg}")


# ---------------------------------------------------------------------------
# 上传流程
# ---------------------------------------------------------------------------
def _probe(session: requests.Session) -> dict:
    """探测最优上传线路；失败返回默认线路。"""
    try:
        ret = _json(session.get(
            "https://member.bilibili.com/preupload?r=probe", timeout=30,
        ), "线路探测")
        lines = ret.get("lines") or []
        if not lines:
            return dict(_FALLBACK_LINE)
        best, best_cost = None, None
        for line in lines:
            probe_url = line.get("probe_url")
            if not probe_url:
                continue
            method = "get" if (ret.get("probe") or {}).get("get") else "post"
            try:
                start = time.perf_counter()
                session.request(
                    method, f"https:{probe_url}",
                    data=b"" if method == "post" else None, timeout=10,
                )
                cost = time.perf_counter() - start
                if best_cost is None or cost < best_cost:
                    best, best_cost = line, cost
            except Exception:
                continue
        if best:
            return {"os": best.get("os") or "upos", "query": best.get("query", "")}
    except Exception as e:
        logger.warning("线路探测失败，使用默认线路: %s", e)
    return dict(_FALLBACK_LINE)


def _preupload(session: requests.Session, path: str, line: dict) -> dict:
    size = os.path.getsize(path)
    params = {
        "r": line.get("os", "upos"),
        "profile": "ugcupos/bup",
        "ssl": 0,
        "version": "2.8.12",
        "build": 2081200,
        "name": os.path.basename(path),
        "size": size,
    }
    ret = _json(session.get(
        f"https://member.bilibili.com/preupload?{line.get('query', '')}",
        params=params, timeout=30,
    ), "获取上传凭证")
    for key in ("auth", "endpoint", "upos_uri", "biz_id", "chunk_size"):
        if key not in ret:
            raise BiliUploadError(f"preupload 响应缺少字段: {key}")
    return ret


def _upload_upos(
    session: requests.Session,
    path: str,
    pre: dict,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """分片上传到 upos，返回分片信息 {"title","filename","desc"}。"""
    url = f"https:{pre['endpoint']}/{pre['upos_uri'].replace('upos://', '')}"
    headers = {"X-Upos-Auth": pre["auth"]}

    # 申请 upload_id
    init_ret = _json(session.post(f"{url}?uploads&output=json", headers=headers, timeout=30), "初始化上传")
    upload_id = init_ret.get("upload_id")
    if not upload_id:
        raise BiliUploadError(f"初始化上传失败: {init_ret}")

    total_size = os.path.getsize(path)
    chunk_size = int(pre["chunk_size"])
    chunks = math.ceil(total_size / chunk_size)
    parts = []

    with open(path, "rb") as f:
        for idx in range(chunks):
            data = f.read(chunk_size)
            params = {
                "uploadId": upload_id,
                "chunks": chunks,
                "total": total_size,
                "chunk": idx,
                "partNumber": idx + 1,
                "start": idx * chunk_size,
                "end": idx * chunk_size + len(data),
                "size": len(data),
            }
            last_err: Optional[Exception] = None
            for attempt in range(10):
                try:
                    session.put(url, params=params, data=data, headers=headers, timeout=600)
                    parts.append({"partNumber": idx + 1, "eTag": "etag"})
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(min(2 * (attempt + 1), 15))
            if last_err is not None:
                raise BiliUploadError(f"分片 {idx + 1}/{chunks} 上传失败: {last_err}")
            if progress_cb:
                progress_cb(idx + 1, chunks)

    # 合并分片
    merge_params = {
        "name": os.path.basename(path),
        "uploadId": upload_id,
        "biz_id": pre["biz_id"],
        "output": "json",
        "profile": "ugcupos/bup",
    }
    last_err = None
    for attempt in range(6):
        try:
            merge_ret = _json(session.post(
                url, params=merge_params, json={"parts": parts},
                headers=headers, timeout=60,
            ), "合并分片")
            if merge_ret.get("OK") == 1:
                filename = os.path.splitext(os.path.basename(pre["upos_uri"]))[0]
                return {
                    "title": os.path.splitext(os.path.basename(path))[0],
                    "filename": filename,
                    "desc": "",
                }
            last_err = BiliUploadError(f"合并分片失败: {merge_ret}")
        except Exception as e:
            last_err = e
        time.sleep(15)
    raise BiliUploadError(f"合并分片最终失败: {last_err}")


def _cover_up(session: requests.Session, cover_path: str, csrf: str) -> str:
    """上传封面（自动裁剪为 16:10），返回不带协议头的图片 URL。"""
    from PIL import Image

    with Image.open(cover_path) as im:
        im = im.convert("RGB")
        xsize, ysize = im.size
        if xsize / ysize > 1.6:
            delta = xsize - ysize * 1.6
            region = im.crop((delta / 2, 0, xsize - delta / 2, ysize))
        else:
            delta = ysize - xsize * 10 / 16
            region = im.crop((0, delta / 2, xsize, ysize - delta / 2))
        buffered = io.BytesIO()
        region.save(buffered, format="JPEG", quality=90)
        b64 = base64.b64encode(buffered.getvalue()).decode("ascii")

    ret = _json(session.post(
        url="https://member.bilibili.com/x/vu/web/cover/up",
        data={"cover": _COVER_DATA_PREFIX + b64, "csrf": csrf},
        timeout=60,
    ), "封面上传")
    url = (ret.get("data") or {}).get("url")
    if not url:
        raise BiliUploadError(f"封面上传失败: {ret}")
    return str(url).replace("http:", "")


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def bili_upload(
    cookies: dict,
    meta: dict,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """执行投稿（同步）。请在线程中调用。

    cookies: {"SESSDATA": ..., "bili_jct": ..., "DedeUserID": ...}
    meta: {
        "path": 视频文件路径,
        "title": 标题(<=80),
        "tid": 分区数字,
        "desc": 简介,
        "tags": 逗号分隔字符串,
        "copyright": 1原创/2转载,
        "source": 转载来源(可选),
        "cover": 封面本地路径(可选),
        "dynamic": 粉丝动态(可选),
    }
    返回 submit 完整响应 dict（含 data.bvid）。
    """
    if not os.path.exists(meta["path"]):
        raise FileNotFoundError(f"视频文件不存在: {meta['path']}")

    csrf = cookies.get("bili_jct", "")
    if not csrf:
        raise BiliUploadError("Cookie 缺少 bili_jct，请重新 /biliup绑定")

    session = _make_session(cookies)
    try:
        # 线路选择
        line = _probe(session)
        logger.info("上传线路: %s", line)
        # 获取上传凭证
        pre = _preupload(session, meta["path"], line)
        # 分片上传
        part = _upload_upos(session, meta["path"], pre, progress_cb)

        desc = (meta.get("desc") or meta["title"]).strip()
        payload = {
            "copyright": int(meta.get("copyright", 1)),
            "source": meta.get("source", ""),
            "tid": int(meta["tid"]),
            "cover": "",
            "title": meta["title"][:80],
            "desc_format_id": 0,
            "desc": desc,
            "desc_v2": [{"raw_text": desc, "biz_id": "", "type": 1}],
            "dynamic": meta.get("dynamic", ""),
            "subtitle": {"open": 0, "lan": ""},
            "tag": ",".join(t.strip() for t in str(meta.get("tags", "")).split(",") if t.strip()),
            "videos": [part],
            "dtime": None,
        }

        # 封面（可选）
        cover = meta.get("cover")
        if cover and os.path.exists(cover):
            payload["cover"] = _cover_up(session, cover, csrf)

        # 联合创作者（可选）
        cooperate_uids = meta.get("cooperate_uids")
        extra_fields_json = None
        if cooperate_uids:
            extra_fields_json = json.dumps({
                "cooperate_user": [
                    {"uid": uid, "role": 1, "sub_type": 0}
                    for uid in cooperate_uids
                ],
            })

        # 极验预检（与官方工具一致）
        try:
            session.get("https://member.bilibili.com/x/geetest/pre/add", timeout=10)
        except Exception:
            pass

        # 调试：记录关键 payload 字段（不含封面/联合创作者长文本）
        logger.info(
            "submit payload keys: copyright=%s tid=%s title_len=%s tag=%s videos=%s "
            "subtitle_open=%s dtime=%s cover_len=%s extra_fields=%s",
            payload["copyright"], payload["tid"], len(payload["title"]),
            payload["tag"], len(payload["videos"]),
            payload["subtitle"]["open"], payload.get("dtime"),
            len(payload.get("cover", "")), bool(extra_fields_json),
        )

        # 投稿（优先带联合创作者，失败则自动降级为普通投稿）
        def _do_submit(use_coop: bool) -> dict:
            p = dict(payload)
            if use_coop and extra_fields_json:
                p["extra_fields"] = extra_fields_json
            ret = _json(session.post(
                f"https://member.bilibili.com/x/vu/web/add?csrf={csrf}",
                json=p,
                timeout=60,
            ), "投稿提交")
            _bili_raise(ret, "投稿提交")
            return ret

        try:
            result = _do_submit(True)
        except BiliUploadError as e:
            if "参数错误" in str(e) and extra_fields_json:
                logger.warning("联合投稿失败，降级为普通投稿: %s", e)
                result = _do_submit(False)
                result["_coop_failed"] = True  # 标记供调用方提示
            else:
                raise
        return result
    finally:
        session.close()
