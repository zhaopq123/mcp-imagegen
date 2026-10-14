"""本地持久化（规格书 3.3、6.2；红线 C6/R5、E7、Q3 推荐解读）。

职责边界（设计方案 2.2）：
- 图片下载落盘 IMAGE_DIR（下载 read = min(PERSIST_TIMEOUT_IMAGE_SEC, deadline 剩余)，E7；
  V2 更名自 PERSISTTIMEOUTSEC，纯改名）；
- 视频下载落盘 VIDEO_DIR（T7c：下载 read = min(PERSIST_TIMEOUT_VIDEO_SEC=120,
  deadline 剩余)，persist_video 单入口，永不报错）；
- /files 完整 URL 生成：按请求 Host / X-Forwarded-Proto / X-Forwarded-Host
  推断（Q3 推荐解读，不新增配置变量）；
- 磁盘水位检查：超线只 WARN + meta 提示，**不自动删图**（C10；规格书
  十二节明确"不做自动删图"，删图是危险动作，一律人工）。

红线 C6：本模块**永不报错、永不阻塞返回**——任何内部异常就地吞掉，
降级为 (原 URL, persist_failed) + WARN 日志；额度已花，绝不能因转存
失败把整个请求变成 [error]。

不做：24h 过期清理（规格书十二"明确不做自动删图"，与设计方案占位描述
冲突时以规格书原文为准；/files URL 长期有效）。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

import httpx

from config import DISK_WATERMARK_RATIO
from infra import budget

logger = logging.getLogger("mcp_imagegen.persist")

# Content-Type → 扩展名（推断落盘文件名；未知类型回退 .png）
_CONTENT_TYPE_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",    # T7c：视频转存（endpoints 白名单同步扩展）
    "video/webm": ".webm",
}

PERSISTED = "persisted"
PERSIST_FAILED = "persist_failed"
SKIPPED = "skipped"  # 未请求 persist（persist=false）

_URL_SUFFIX_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789.")


def _ext_for(content_type: str, url: str) -> str:
    """扩展名：优先 Content-Type；否则 URL 路径后缀；默认 .png。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CONTENT_TYPE_EXT:
        return _CONTENT_TYPE_EXT[ct]
    tail = url.split("?")[0].rsplit("/", 1)[-1].lower()
    dot = tail.rfind(".")
    if dot > 0 and set(tail[dot:]) <= _URL_SUFFIX_CHARS and 1 <= len(tail) - dot <= 5:
        return tail[dot:]
    return ".png"


def build_base_url(headers: Optional[Mapping[str, str]], default_port: int) -> str:
    """Q3 推荐解读：按请求头推断对外完整地址。

    scheme = X-Forwarded-Proto（Caddy 前置场景）或 http；
    host   = X-Forwarded-Host 或 Host；缺失回退 localhost:<port>。
    """
    h = {str(k).lower(): str(v).strip() for k, v in (headers or {}).items()}
    scheme = h.get("x-forwarded-proto") or "http"
    host = h.get("x-forwarded-host") or h.get("host") or f"localhost:{default_port}"
    return f"{scheme}://{host}"


class PersistService:
    """下载转存（每图独立 try/except，永不抛异常；R5）。"""

    def __init__(self, cfg, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._cfg = cfg
        self._dir = Path(cfg.image_dir)
        self._video_dir = Path(cfg.video_dir)  # T7c：视频落盘目录
        self._transport = transport  # 测试注入（ScriptedTransport 等）
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # 生命周期（server 装配时调用）
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """创建下载专用 httpx client（连接层参数同 config，read 每次动态）。"""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # 权限/磁盘问题：不阻断启动，persist 时逐图降级
            logger.warning("[persist] IMAGE_DIR 创建失败（persist 将全部降级）: %s", exc)
        try:
            self._video_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("[persist] VIDEO_DIR 创建失败（视频 persist 将降级）: %s", exc)
        self._client = httpx.AsyncClient(
            transport=self._transport,
            limits=httpx.Limits(
                max_connections=self._cfg.httpx_max_connections,
                max_keepalive_connections=self._cfg.httpx_max_keepalive,
                keepalive_expiry=self._cfg.httpx_keepalive_sec,
            ),
            timeout=httpx.Timeout(
                self._cfg.httpx_connect_sec,
                write=self._cfg.httpx_write_sec,
                pool=self._cfg.httpx_pool_sec,
                read=self._cfg.persist_timeout_image_sec,  # 占位；每次请求覆盖 read
            ),
        )

    def reconfigure(self, cfg) -> None:
        """热更新：下载超时 / 连接参数改用新的 Config（已建连接下次请求覆盖）。"""
        self._cfg = cfg

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        """懒创建（server 正常路径 start() 已建；此处兜底，保证 persist 永不因
        生命周期疏漏失败，C6）。"""
        if self._client is None:
            await self.start()
        return self._client  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # 下载与落盘（C6：永不报错）
    # ------------------------------------------------------------------
    def _read_timeout(self, deadline: budget.Deadline) -> float:
        """E7：下载 read = min(30s, deadline 剩余)。"""
        return max(0.0, min(self._cfg.persist_timeout_image_sec, deadline.remaining()))

    async def persist_images(
        self,
        urls: Sequence[str],
        *,
        deadline: budget.Deadline,
        req_id: str,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Tuple[Tuple[str, str], ...]:
        """逐图转存，返回 (最终 URL, status) 序列；任何异常 → 原 URL + persist_failed。

        - deadline 耗尽 → 不再发起下载 IO（R3），该图降级原 URL；
        - 磁盘水位超线 → 照常落盘（图已花额度），WARN + 返回值由调用方在
          meta 注明（C10：只提示不删图）。
        """
        base_url = build_base_url(headers, self._cfg.port)
        results: list[Tuple[str, str]] = []
        for index, url in enumerate(urls, start=1):
            try:
                results.append(await self._persist_one(
                    url, deadline=deadline, req_id=req_id, index=index, base_url=base_url,
                ))
            except Exception as exc:  # C6：就地吞掉，绝不让异常冒泡
                logger.warning(
                    "[persist] req_id=%s 转存失败（降级原 URL）: %s: %s",
                    req_id, type(exc).__name__, exc,
                )
                results.append((url, PERSIST_FAILED))
        return tuple(results)

    async def _persist_one(
        self,
        url: str,
        *,
        deadline: budget.Deadline,
        req_id: str,
        index: int,
        base_url: str,
    ) -> Tuple[str, str]:
        deadline.check()  # R3：预算耗尽 → 不发起下载 IO（转 persist_failed 由外层兜）
        client = await self._ensure_client()
        timeout = httpx.Timeout(
            self._cfg.httpx_connect_sec,
            write=self._cfg.httpx_write_sec,
            pool=self._cfg.httpx_pool_sec,
            read=self._read_timeout(deadline),
        )
        resp = await client.get(url, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"下载返回 {resp.status_code}")
        content = resp.content
        if not content:
            raise RuntimeError("下载内容为空")
        ext = _ext_for(resp.headers.get("content-type", ""), url)
        name = f"{req_id}-{index:02d}-{secrets.token_hex(4)}{ext}"
        path = self._dir / name
        path.write_bytes(content)  # 本地写盘（小图，亚毫秒级；R2 禁线程故不做线程池）
        logger.info(
            "[persist] req_id=%s 落盘 %s (%d bytes) ← %s",
            req_id, name, len(content), url,
        )
        return (f"{base_url}/files/{name}", PERSISTED)

    # ------------------------------------------------------------------
    # 视频转存（T7c；120s 档、VIDEO_DIR、C6/R5 永不报错）
    # ------------------------------------------------------------------
    def _read_timeout_video(self, deadline: budget.Deadline) -> float:
        """视频下载 read = min(PERSIST_TIMEOUT_VIDEO_SEC=120, deadline 剩余)。"""
        return max(0.0, min(self._cfg.persist_timeout_video_sec, deadline.remaining()))

    async def persist_video(
        self,
        url: str,
        *,
        deadline: budget.Deadline,
        req_id: str,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Tuple[str, str]:
        """单个视频转存；任何异常/预算不足 → (原上游 URL, persist_failed)。

        - C6/R5：本方法**永不抛异常、永不阻塞返回**——额度已花，转存失败
          绝不能把整个请求变成 [error] 行；
        - 下载 read = min(120s, deadline 剩余)，计入 deadline（方案 2.2）；
        - 预算耗尽 → 不发起下载 IO（R3），直接降级原 URL；
        - 成功 → (/files 完整 URL, persisted)，与生图同一 URL 构造规则。
        """
        try:
            return await self._persist_video_one(
                url, deadline=deadline, req_id=req_id,
                base_url=build_base_url(headers, self._cfg.port),
            )
        except Exception as exc:  # C6：就地吞掉，降级原 URL
            logger.warning(
                "[persist] req_id=%s 视频转存失败（降级原 URL）: %s: %s",
                req_id, type(exc).__name__, exc,
            )
            return (url, PERSIST_FAILED)

    async def _persist_video_one(
        self,
        url: str,
        *,
        deadline: budget.Deadline,
        req_id: str,
        base_url: str,
    ) -> Tuple[str, str]:
        deadline.check()  # R3：预算耗尽 → 不发起下载 IO（降级由外层兜）
        self._video_dir.mkdir(parents=True, exist_ok=True)  # 与 IMAGE_DIR 同纪律
        client = await self._ensure_client()
        timeout = httpx.Timeout(
            self._cfg.httpx_connect_sec,
            write=self._cfg.httpx_write_sec,
            pool=self._cfg.httpx_pool_sec,
            read=self._read_timeout_video(deadline),
        )
        resp = await client.get(url, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"下载返回 {resp.status_code}")
        content = resp.content
        if not content:
            raise RuntimeError("下载内容为空")
        ext = _ext_for(resp.headers.get("content-type", ""), url)
        name = f"{req_id}-01-{secrets.token_hex(4)}{ext}"  # 命名纪律与图片一致
        path = self._video_dir / name
        path.write_bytes(content)  # 本地写盘（R2 禁线程故不做线程池）
        logger.info(
            "[persist] req_id=%s 视频落盘 %s (%d bytes) ← %s",
            req_id, name, len(content), url,
        )
        return (f"{base_url}/files/{name}", PERSISTED)

    # ------------------------------------------------------------------
    # 磁盘水位（C10：只 WARN + meta 提示，不删图）
    # ------------------------------------------------------------------
    def disk_usage_ratio(self) -> Optional[float]:
        """IMAGE_DIR/VIDEO_DIR 所在文件系统使用率（0.0–1.0）；不可得 → None。

        T7c：水位检查覆盖 images/ + videos/ 双目录（方案三节 persist 行）；
        典型部署两目录同一文件系统，取两者最大值（最保守判定）。
        """
        ratios: list[float] = []
        for directory in (self._dir, self._video_dir):
            try:
                usage = shutil.disk_usage(directory)
            except OSError:
                continue
            if usage.total <= 0:
                continue
            ratios.append(usage.used / usage.total)
        return max(ratios) if ratios else None

    def over_watermark(self) -> bool:
        ratio = self.disk_usage_ratio()
        return ratio is not None and ratio >= DISK_WATERMARK_RATIO
