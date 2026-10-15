"""配置存储：把 Provider / 访问令牌 / 管理员凭据 / 运行时参数落到一个 JSON 文件。

设计约束
--------
- **单写者模型**：所有写操作都在事件循环内串行发出，写盘用「临时文件 +
  ``os.replace``」原子替换——读进程永远不会看到半个 JSON。
- **禁线程（R2）**：不使用任何锁；一致性靠「事件循环内无抢占」+ 原子替换
  双重保证。多台实例不要共享同一个 store 文件（没有做跨进程协商）。
- **密钥不落全新FRAME ork üch holmaydi**：``auth.token`` 支持 ``env:VAR`` 引用，
  真正敏感的部署可以只放引用。明文也允许——文件在本机、权限 0600，
  且面板上默认掩码显示。
- **gitignore 友好**：默认文件名 ``store.json``，已在 .gitignore 中排除。

首次启动
--------
没有任何配置时生成器观 rubicon 一个空 store（零 provider）。管理员密码未
设置 → 面板强制走初始化向导；此时 MCP 侧会自动签发第一个访问令牌（随机
值打印到启动日志），保证「起来就能用」而不必先进面板。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Callable, Optional

from . import schema

logger = logging.getLogger("mcp_imagegen.store")

STORE_VERSION = 1
DEFAULT_FILENAME = "store.json"

_PBKDF2_ITERATIONS = 120_000


class StoreError(Exception):
    """存储层错误（读写失败 / 内容非法）。"""


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# 口令哈希（PBKDF2-HMAC-SHA256；不引第三方）
# ---------------------------------------------------------------------------
def hash_password(password: str, *, salt: Optional[str] = None,
                  iterations: int = _PBKDF2_ITERATIONS) -> dict:
    raw_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), raw_salt.encode("utf-8"), iterations)
    return {
        "algo": "pbkdf2-sha256",
        "iterations": iterations,
        "salt": raw_salt,
        "hash": digest.hex(),
    }


def verify_password(password: str, record: Optional[dict]) -> bool:
    if not record or not isinstance(record, dict):
        return False
    try:
        expected = record["hash"]
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            str(record["salt"]).encode("utf-8"),
            int(record.get("iterations") or _PBKDF2_ITERATIONS),
        ).hex()
    except Exception:  # noqa: BLE001 —— 凭据结构损坏一律按失败处理
        return False
    return hmac.compare_digest(candidate, str(expected))


def new_token(nbytes: int = 24) -> str:
    """生成一个随机访问令牌（URL 安全，便于直接贴进配置文件）。"""
    return secrets.token_urlsafe(nbytes)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class Store:
    """JSON 配置仓库：内存对象 + 原子落盘。"""

    def __init__(self, path: str, clock: Callable[[], float] = _now) -> None:
        self._path = path
        self._clock = clock
        self.version: int = STORE_VERSION
        self.admin: Optional[dict] = None
        self.tokens: list[schema.TokenRecord] = []
        self.providers: list[schema.ProviderConfig] = []
        self.settings: dict = {}
        self.loaded = False

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    @property
    def path(self) -> str:
        return self._path

    def load(self) -> "Store":
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as exc:
            raise StoreError(f"配置文件读取失败 {self._path}: {exc}") from exc
        self._apply(raw)
        self.loaded = True
        return self

    def _apply(self, raw: dict) -> None:
        if not isinstance(raw, dict):
            raise StoreError("配置文件根节点必须是对象")
        self.version = int(raw.get("version") or STORE_VERSION)
        self.admin = raw.get("admin") or None
        self.tokens = [
            schema.TokenRecord.from_dict(item)
            for item in (raw.get("tokens") or [])
        ]
        self.providers = [
            schema.ProviderConfig.from_dict(item)
            for item in (raw.get("providers") or [])
        ]
        self.settings = dict(raw.get("settings") or {})

    def snapshot(self) -> dict:
        return {
            "version": STORE_VERSION,
            "admin": self.admin,
            "tokens": [t.to_dict() for t in self.tokens],
            "providers": [p.to_dict() for p in self.providers],
            "settings": self.settings,
        }

    def save(self) -> None:
        """原子落盘（临时文件 + replace；权限收紧到 0600）。"""
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        tmp = f"{self._path}.tmp.{secrets.token_hex(4)}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.snapshot(), fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
            try:
                os.chmod(self._path, 0o600)
            except OSError:  # Windows 无 POSIX 权限语义
                pass
        except OSError as exc:
            raise StoreError(f"配置文件写入失败 {self._path}: {exc}") from exc
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # 管理员
    # ------------------------------------------------------------------
    @property
    def admin_ready(self) -> bool:
        return bool(self.admin and self.admin.get("hash"))

    def set_admin_password(self, password: str) -> None:
        # 长度下限可由 ADMIN_MIN_PASSWORD_LEN 覆盖（默认 8）。
        # 为什么可配：管理面板常部署在内网/局域网，运维可能只想用短口令
        # 便于输入；但**默认仍是 8**，想要弱口令必须显式声明，不静默降级。
        # 上限 128 是防御性上限，避免把超长字符串丢进哈希。
        try:
            floor = int(os.environ.get("ADMIN_MIN_PASSWORD_LEN", "8"))
        except (TypeError, ValueError):
            floor = 8
        floor = max(4, min(floor, 128))
        if not isinstance(password, str) or len(password) < floor:
            raise StoreError(f"管理员密码至少 {floor} 位")
        if len(password) > 128:
            raise StoreError("管理员密码过长（上限 128 位）")
        self.admin = hash_password(password)
        self.save()

    def check_admin_password(self, password: str) -> bool:
        return verify_password(password, self.admin)

    # ------------------------------------------------------------------
    # Provider CRUD
    # ------------------------------------------------------------------
    def get_provider(self, pid: str) -> Optional[schema.ProviderConfig]:
        for item in self.providers:
            if item.id == pid:
                return item
        return None

    def list_providers(self) -> list[schema.ProviderConfig]:
        return list(self.providers)

    def upsert_provider(self, spec: schema.ProviderConfig) -> schema.ProviderConfig:
        for index, item in enumerate(self.providers):
            if item.id == spec.id:
                self.providers[index] = spec
                break
        else:
            self.providers.append(spec)
        self.save()
        return spec

    def delete_provider(self, pid: str) -> bool:
        before = len(self.providers)
        self.providers = [p for p in self.providers if p.id != pid]
        removed = len(self.providers) != before
        if removed:
            # 令牌若绑定到已删除的 provider → 解绑（否则该令牌彻底不可用）
            for tok in self.tokens:
                if tok.default_provider == pid:
                    tok.default_provider = ""
            self.save()
        return removed

    # ------------------------------------------------------------------
    # Token CRUD
    # ------------------------------------------------------------------
    def get_token(self, tid: str) -> Optional[schema.TokenRecord]:
        for item in self.tokens:
            if item.id == tid:
                return item
        return None

    def find_token_by_secret(self, secret: str) -> Optional[schema.TokenRecord]:
        if not secret:
            return None
        for item in self.tokens:
            if item.token and hmac.compare_digest(item.token, secret):
                return item
        return None

    def upsert_token(self, record: schema.TokenRecord) -> schema.TokenRecord:
        for index, item in enumerate(self.tokens):
            if item.id == record.id:
                self.tokens[index] = record
                break
        else:
            self.tokens.append(record)
        self.save()
        return record

    def delete_token(self, tid: str) -> bool:
        before = len(self.tokens)
        self.tokens = [t for t in self.tokens if t.id != tid]
        removed = len(self.tokens) != before
        if removed:
            self.save()
        return removed

    def bootstrap_token(self, name: str = "default") -> Optional[schema.TokenRecord]:
        """没有任何令牌时签发第一个（返回 None 表示已有令牌，无需签发）。"""
        if self.tokens:
            return None
        record = schema.TokenRecord(
            id=name, name=name, token=new_token(),
            created_at=self._clock(),
        )
        self.tokens.append(record)
        self.save()
        logger.warning(
            "[store] 未配置访问令牌，已自动生成 %r：%s "
            "（请妥善保存，它不会再次以明文展示在日志里）", name, record.token)
        return record

    def touch_token(self, record: schema.TokenRecord) -> None:
        """记录使用（惰性落盘，避免每个请求写一次文件）。"""
        record.use_count += 1
        record.last_used_at = self._clock()

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def set_settings(self, patch: dict) -> dict:
        for key, value in dict(patch or {}).items():
            if value is None:
                self.settings.pop(key, None)
            else:
                self.settings[key] = value
        self.save()
        return dict(self.settings)

    def has_any_capability(self, kind: str) -> bool:
        return any(p.has(kind) for p in self.providers)
