"""微信 AUX 迁移服务。

本模块只依赖 Hub/冻结投影和显式注入的 fake/recorded provider：
不会调用旧 Flask API、不会启动浏览器、默认不会联网。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import struct
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock
from content_hub.services.audit import AuditService
from content_hub.validation.timestamps import utc_now_iso

MAX_COVER_BYTES = 8 * 1024 * 1024
MAX_COVER_BATCH = 10
EVIDENCE_RE = re.compile(r"[A-Za-z0-9_-]{3,180}\Z")
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
SECRET_RE = re.compile(
    r"(token|cookie|password|secret|api[_-]?key|profile[_-]?dir|"
    r"executable[_-]?path|credential|authorization)",
    re.I,
)
AGENT_METRIC_DICTIONARY = {
    "schema_version": "agent_metric_dictionary_v1",
    "updated_at": "2026-07-10",
    "principle": "指标是事实层的可复算观察辅助，不替代行业解释或行动判断。",
    "metrics": [
        {
            "metric_id": "account_score",
            "label": "账号分",
            "question_answered": "滚动15天内，哪个账号有更广、更持续、且经过第4至10名验证的搜索覆盖。",
            "unit": "相对基准分",
            "window": "滚动15天",
            "scope": "当前监控关键词与其搜索结果",
            "confidence_field": "account_score_hexagon.current.confidence",
            "do_not_infer": ["账号商业价值", "文章绝对质量", "真实用户转化"],
        },
        {
            "metric_id": "timeliness_score",
            "label": "时效分",
            "question_answered": "最近3天，哪个账号更集中地进入Top3，并出现新进Top3和新文冲榜。",
            "unit": "相对基准分",
            "window": "最近3天",
            "scope": "当前监控关键词与其搜索结果",
            "confidence_field": "timeliness_score_hexagon.current.confidence",
            "do_not_infer": ["长期经营能力", "未来持续表现"],
        },
        {
            "metric_id": "today_score",
            "label": "当天分",
            "question_answered": "今天在当前监控词下谁的搜索表现更强。",
            "unit": "相对基准分",
            "window": "当天",
            "scope": "当前监控关键词与其最新主快照",
            "confidence_field": "today_score_hexagon.current.confidence",
            "do_not_infer": ["账号长期水平", "真实全网热度"],
        },
        {
            "metric_id": "keyword_trend",
            "label": "趋势",
            "question_answered": "最近3天相对此前7天，关键词的阅读需求代理信号是在上升、平稳还是下降。",
            "unit": "方向信号",
            "window": "近3天对比此前7天",
            "scope": "关键词命中文章与关联词时间切片",
            "confidence_field": "keyword_read_delta.confidence_level",
            "do_not_infer": ["今天比昨天的真实搜索量", "全行业需求规模"],
        },
        {
            "metric_id": "steady_read",
            "label": "常态阅读",
            "question_answered": "在当前窗口和模型下，关键词的阅读基线估计是多少。",
            "unit": "估算阅读量",
            "window": "滚动15天",
            "scope": "当前监控关键词的命中文章",
            "confidence_field": "keyword_read_delta.confidence_level",
            "do_not_infer": ["微信真实总搜索量", "单篇文章真实阅读量"],
        },
        {
            "metric_id": "read_delta_15d",
            "label": "15日阅读增量",
            "question_answered": "有限时间切片下，关键词的阅读增量估算是多少。",
            "unit": "估算阅读量",
            "window": "滚动15天",
            "scope": "当前监控关键词的命中文章",
            "confidence_field": "keyword_read_delta.confidence_level",
            "required_quality_fields": [
                "coverage_ratio",
                "observed_share",
                "estimated_share",
                "snapshot_count",
            ],
            "do_not_infer": ["精确真实增量", "未被监控文章的阅读变化"],
        },
        {
            "metric_id": "external_heat",
            "label": "外部热度",
            "question_answered": "外部搜索渠道中是否存在辅助热度信号。",
            "unit": "月覆盖量或估算值",
            "window": "数据源抓取时点",
            "scope": "AIDSO WSO/DSO 已抓取关键词",
            "confidence_field": "wso_estimated",
            "do_not_infer": ["微信搜索今天上涨", "用户已经完成转化"],
        },
        {
            "metric_id": "article_interaction_proxy",
            "label": "文章互动代理",
            "question_answered": "文章当前可观测阅读、点赞和朋友在看表现如何。",
            "unit": "文章计数",
            "window": "文章被抓取时点",
            "scope": "已抓取到文章指标的监控样本",
            "confidence_field": "article metric availability",
            "do_not_infer": ["真实推荐曝光", "读者画像", "推荐流因果关系"],
        },
    ],
}


class AuxNotFound(LookupError):
    pass


class AuxEvidenceNotFound(AuxNotFound):
    pass


class AuxValidation(ValueError):
    pass


class AuxUpstreamError(RuntimeError):
    pass


class AuxIdempotencyConflict(AuxValidation):
    """同一幂等键被不同规范化输入重用。"""


class AuxCommandReplay(AuxUpstreamError):
    """重放已持久化的失败命令，不重新调用 provider。"""

    def __init__(self, payload: dict[str, Any], status_code: int) -> None:
        super().__init__(str(payload.get("error") or "command failed"))
        self.payload = payload
        self.status_code = status_code


class AidsoLoginRequired(AuxUpstreamError):
    pass


class AidsoProfileBusy(AuxUpstreamError):
    pass


class AidsoProvider(Protocol):
    kind: str

    def fetch(self, **params: Any) -> dict[str, Any]:
        ...


class ImageProvider(Protocol):
    """受控图片 provider；调用方负责只连接传入的公共解析地址。"""

    kind: str

    def fetch(self, url: str, *, resolved_addresses: tuple[str, ...]) -> Any:
        ...


class DisabledAidsoProvider:
    kind = "disabled"

    def fetch(self, **params: Any) -> dict[str, Any]:
        raise AuxUpstreamError("Aidso provider is disabled")


class RecordedAidsoProvider:
    kind = "recorded"

    def __init__(self, payload: dict[str, Any] | Callable[..., dict[str, Any]]) -> None:
        self.payload = payload
        self.calls = 0

    def fetch(self, **params: Any) -> dict[str, Any]:
        self.calls += 1
        value = self.payload(**params) if callable(self.payload) else dict(self.payload)
        if not isinstance(value, dict):
            raise AuxUpstreamError("recorded Aidso response is invalid")
        return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _implicit_command_key(operation: str, payload: Any) -> str:
    # An omitted key is an audit correlation id, not an idempotency contract.
    # Every legacy request must be allowed to execute again; only an explicit
    # caller key opts into replay/conflict semantics.
    return f"implicit:wechat-aux:{operation}:{uuid.uuid4().hex}"


def _safe_public(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SECRET_RE.search(str(key)) else _safe_public(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_public(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"(?i)(bearer\s+)[^\s]+", r"\1[REDACTED]", value)
    return value


def _now() -> str:
    return utc_now_iso()


def _bool_param(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise AuxValidation("boolean parameter is invalid")


def _int_param(value: Any, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise AuxValidation("integer parameter is invalid") from exc
    if result < 1:
        raise AuxValidation("timeout must be positive")
    return result


def _image_info(data: bytes, content_type: str) -> tuple[str, int | None, int | None]:
    if len(data) > MAX_COVER_BYTES:
        raise AuxUpstreamError("image response exceeds size limit")
    ctype = content_type.split(";", 1)[0].strip().lower()
    if ctype not in ALLOWED_IMAGE_TYPES:
        raise AuxUpstreamError("upstream response is not an image")
    kind = ""
    width = height = None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        kind = "image/png"
        width, height = struct.unpack(">II", data[16:24])
    elif data.startswith(b"\xff\xd8"):
        kind = "image/jpeg"
        pos = 2
        while pos + 9 < len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            pos += 2
            if marker in {0xD8, 0xD9}:
                continue
            if pos + 2 > len(data):
                break
            size = int.from_bytes(data[pos:pos + 2], "big")
            if marker in range(0xC0, 0xC4) and pos + 7 <= len(data):
                height = int.from_bytes(data[pos + 3:pos + 5], "big")
                width = int.from_bytes(data[pos + 5:pos + 7], "big")
                break
            pos += max(size, 2)
    elif data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        kind = "image/gif"
        width, height = struct.unpack("<HH", data[6:10])
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        kind = "image/webp"
        if data[12:16] == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
    if kind != ctype:
        raise AuxUpstreamError("image MIME does not match response bytes")
    return kind, width, height


def _resolved_public_addresses(host: str) -> tuple[str, ...]:
    lowered = host.rstrip(".").lower()
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".localhost"):
        raise AuxValidation("cover url host is not allowed")
    try:
        infos = socket.getaddrinfo(lowered, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise AuxUpstreamError("cover host could not be resolved") from exc
    addresses = tuple(sorted({item[4][0].split("%", 1)[0] for item in infos}))
    if not addresses:
        raise AuxUpstreamError("cover host could not be resolved")
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise AuxUpstreamError("cover host resolved to an invalid address") from exc
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise AuxValidation("cover url resolves to a non-public address")
    return addresses


def validate_cover_url(url: str) -> tuple[str, tuple[str, ...]]:
    text = str(url or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise AuxValidation("cover url is invalid")
    if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise AuxValidation("cover url is invalid")
    return text, _resolved_public_addresses(parsed.hostname)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuxNotFound(path.name) from exc


def _decode_json_object(value: str) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return result if isinstance(result, dict) else {}


def _command_replay(
    row: Any,
    expected_input: dict[str, Any],
) -> dict[str, Any] | None:
    actual_input = _decode_json_object(row["input_json"])
    if _json(actual_input) != _json(expected_input):
        raise AuxIdempotencyConflict("idempotency key already used with different input")
    status = str(row["status"] or "")
    if status == "succeeded":
        return _decode_json_object(row["output_json"])
    if status == "failed":
        error = _decode_json_object(row["error_json"])
        status_code = int(error.pop("_status_code", 502) or 502)
        raise AuxCommandReplay(error, status_code)
    raise AuxCommandReplay({"error": "command is still running"}, 409)


def _assert_no_symlink(path: Path, *, stop: Path | None = None) -> None:
    current = path.absolute()
    stop_abs = stop.absolute() if stop else None
    while True:
        if current.is_symlink():
            raise AuxValidation("freeze path contains a symlink")
        if stop_abs is not None and current == stop_abs:
            break
        if current.parent == current:
            break
        current = current.parent


@dataclass
class WechatAuxService:
    settings: Any
    aidso_provider: AidsoProvider | None = None
    image_provider: ImageProvider | None = None

    def __post_init__(self) -> None:
        if self.aidso_provider is None:
            self.aidso_provider = self._recorded_provider_from_env() or DisabledAidsoProvider()

    def _recorded_provider_from_env(self) -> AidsoProvider | None:
        path = os.getenv("HUB_WECHAT_AIDSO_RECORDED")
        if not path:
            return None
        try:
            payload = _read_json(Path(path).expanduser().absolute())
        except AuxNotFound:
            return None
        return RecordedAidsoProvider(payload) if isinstance(payload, dict) else None

    def _source_root(self) -> Path:
        return Path(getattr(self.settings, "wechat_source_root", self.settings.project_root)).absolute()

    def _artifact_file(self, kind: str, subject_id: str = "", root: Path | None = None) -> Path:
        base = root or self._source_root()
        mapping = {
            "manifest": base / "data/agent/manifest.json",
            "daily_brief": base / "data/agent/daily_brief.json",
            "metric_dictionary": base / "data/config/agent_metric_dictionary.json",
            "evidence": base / "data/agent/evidence" / f"{subject_id}.json",
            "penalty_signals": base / "normalized/penalty_signals.json",
            "account_aliases": base / "normalized/account_aliases.json",
        }
        if kind not in mapping:
            raise AuxValidation("unsupported AUX artifact")
        return mapping[kind]

    def import_frozen_artifacts(self, freeze_root: Path | str | None = None) -> int:
        root = Path(freeze_root or self._source_root()).expanduser().absolute()
        _assert_no_symlink(root)
        entries: list[tuple[str, str, Path]] = [
            ("manifest", "", self._artifact_file("manifest", root=root)),
            ("daily_brief", "", self._artifact_file("daily_brief", root=root)),
            ("metric_dictionary", "", self._artifact_file("metric_dictionary", root=root)),
            ("penalty_signals", "", self._artifact_file("penalty_signals", root=root)),
            ("account_aliases", "", self._artifact_file("account_aliases", root=root)),
        ]
        evidence_root = root / "data/agent/evidence"
        if evidence_root.exists():
            _assert_no_symlink(evidence_root, stop=root)
            for path in sorted(evidence_root.glob("*.json")):
                entries.append(("evidence", path.stem, path))
        loaded: list[tuple[str, str, str, str, str, str]] = []
        for kind, subject_id, path in entries:
            _assert_no_symlink(path, stop=root)
            if not path.is_file():
                continue
            raw = path.read_bytes()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AuxValidation(f"invalid frozen artifact: {path.name}") from exc
            if not isinstance(payload, dict):
                raise AuxValidation(f"frozen artifact must be an object: {path.name}")
            source_ref = path.relative_to(root).as_posix()
            model_version = str(payload.get("model_version") or payload.get("projection_version") or "")
            loaded.append((kind, subject_id, _json(payload), hashlib.sha256(raw).hexdigest(), source_ref, model_version))
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con, transaction(con):
                for kind, subject_id, payload, source_hash, source_ref, model_version in loaded:
                    con.execute(
                        """INSERT INTO wechat_aux_artifacts(
                           artifact_id,artifact_kind,subject_id,payload_json,source_hash,source_ref,model_version,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?)
                           ON CONFLICT(artifact_kind,subject_id,source_hash) DO UPDATE SET
                           payload_json=excluded.payload_json,source_ref=excluded.source_ref,
                           model_version=excluded.model_version,updated_at=excluded.updated_at""",
                        (
                            f"aux_artifact_{uuid.uuid4().hex}",
                            kind,
                            subject_id,
                            payload,
                            source_hash,
                            source_ref,
                            model_version,
                            _now(),
                        ),
                    )
        return len(loaded)

    def artifact(self, kind: str, subject_id: str = "") -> dict[str, Any]:
        if kind == "evidence" and not EVIDENCE_RE.fullmatch(subject_id):
            raise AuxValidation("invalid evidence id")
        if kind == "metric_dictionary":
            # This is a fixed agent-facing interpretation contract, not a
            # projection of Hub metric_definitions.
            return json.loads(json.dumps(AGENT_METRIC_DICTIONARY, ensure_ascii=False))
        with connect(self.settings, readonly=True) as con:
            row = con.execute(
                "SELECT payload_json FROM wechat_aux_artifacts WHERE artifact_kind=? AND subject_id=? ORDER BY updated_at DESC LIMIT 1",
                (kind, subject_id),
            ).fetchone()
        if row:
            try:
                value = json.loads(row["payload_json"])
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                pass
        path = self._artifact_file(kind, subject_id)
        _assert_no_symlink(path, stop=self._source_root())
        if not path.is_file():
            if kind == "evidence":
                raise AuxEvidenceNotFound(subject_id)
            raise AuxNotFound(subject_id or kind)
        value = _read_json(path)
        if not isinstance(value, dict):
            raise AuxNotFound(subject_id or kind)
        return value

    def _cover_response(self, provider: ImageProvider, url: str, addresses: tuple[str, ...]) -> Any:
        response = provider.fetch(url, resolved_addresses=addresses)
        connected = getattr(response, "connected_addresses", None)
        if connected is None:
            connected = getattr(response, "resolved_addresses", None)
        if connected is None or not set(str(item) for item in connected).issubset(set(addresses)):
            raise AuxUpstreamError("image provider did not prove DNS binding")
        if getattr(response, "status_code", 200) in range(300, 400):
            raise AuxUpstreamError("image redirects are not allowed")
        if getattr(response, "status_code", 200) >= 400:
            raise AuxUpstreamError("cover upstream returned an error")
        headers = getattr(response, "headers", {}) or {}
        content_length = int(headers.get("content-length", "0") or 0)
        if content_length > MAX_COVER_BYTES:
            raise AuxUpstreamError("image response exceeds size limit")
        chunks = getattr(response, "iter_content", None)
        if callable(chunks):
            parts: list[bytes] = []
            size = 0
            for chunk in chunks(64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_COVER_BYTES:
                    raise AuxUpstreamError("image response exceeds size limit")
                parts.append(bytes(chunk))
            body = b"".join(parts)
        else:
            body = bytes(getattr(response, "content", b""))
        ctype, width, height = _image_info(body, headers.get("content-type", ""))
        return body, ctype, width, height

    def cover_image(self, url: str) -> tuple[bytes, str, str]:
        safe_url, addresses = validate_cover_url(url)
        with connect(self.settings, readonly=True) as con:
            row = con.execute("SELECT * FROM wechat_aux_cover_cache WHERE source_url=?", (safe_url,)).fetchone()
        if row:
            path = (Path(self.settings.asset_store_path) / row["asset_path"]).absolute()
            root = Path(self.settings.asset_store_path).absolute()
            _assert_no_symlink(path, stop=root)
            if path.is_file() and root in path.parents:
                return path.read_bytes(), row["content_type"], row["asset_hash"]
        if self.image_provider is None:
            raise AuxUpstreamError("image provider is disabled")
        body, ctype, width, height = self._cover_response(self.image_provider, safe_url, addresses)
        digest = hashlib.sha256(body).hexdigest()
        root = Path(self.settings.asset_store_path).absolute()
        relative = f"wechat/cover/{digest}.{ctype.split('/', 1)[1]}"
        target = root / relative
        _assert_no_symlink(target, stop=root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.is_symlink():
            raise AuxValidation("asset path is a symlink")
        fd, temporary_name = tempfile.mkstemp(prefix=".cover-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con, transaction(con):
                con.execute(
                    """INSERT INTO wechat_aux_cover_cache(
                       source_url,asset_hash,asset_path,content_type,byte_size,width,height,fetched_at,source_ref
                       ) VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_url) DO UPDATE SET asset_hash=excluded.asset_hash,
                       asset_path=excluded.asset_path,content_type=excluded.content_type,
                       byte_size=excluded.byte_size,width=excluded.width,height=excluded.height,
                       fetched_at=excluded.fetched_at""",
                    (safe_url, digest, relative, ctype, len(body), width, height, _now(), f"provider:{self.image_provider.kind}"),
                )
        return body, ctype, digest

    def article_covers(self, raw_items: Any, *, idempotency_key: str = "", actor_id: str = "user") -> dict[str, Any]:
        if not isinstance(raw_items, list):
            raise AuxValidation("articles must be a list")
        if len(raw_items) > MAX_COVER_BATCH:
            raise AuxValidation(f"articles batch exceeds limit {MAX_COVER_BATCH}")
        items: list[dict[str, str]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise AuxValidation("article item must be an object")
            article_id = str(raw.get("article_id", "")).strip()
            if not article_id:
                raise AuxValidation("article_id is required")
            items.append({"article_id": article_id, "url": str(raw.get("url", "")).strip()})
        input_data = {"articles": items}
        key = (idempotency_key or "").strip() or _implicit_command_key("article-covers", input_data)
        by_id: dict[str, dict[str, Any]] = {}
        # Hub 事实优先：旧 article_id 通过 content_id / wechat_article identifier
        # 解析，payload_json 保留导入时的 cover/raw URL。
        with connect(self.settings, readonly=True) as con:
            rows = con.execute(
                "SELECT content_id,canonical_url,payload_json FROM contents WHERE content_type='external_article'"
            ).fetchall()
            for row in rows:
                payload = _decode_json_object(row["payload_json"])
                item = {"article_id": row["content_id"], "raw_url": row["canonical_url"], **payload}
                by_id[str(row["content_id"])] = item
            identifiers = con.execute(
                """SELECT i.external_id,c.content_id,c.canonical_url,c.payload_json
                   FROM content_identifiers i JOIN contents c ON c.content_id=i.content_id
                   WHERE i.namespace IN ('wechat_article','wechat_article_id')"""
            ).fetchall()
            for row in identifiers:
                payload = _decode_json_object(row["payload_json"])
                content_payload = _decode_json_object(row["payload_json"])
                by_id[str(row["external_id"])] = {
                    "article_id": row["external_id"],
                    "content_id": row["content_id"],
                    "raw_url": row["canonical_url"],
                    **content_payload,
                    **payload,
                }
        # 只有 Hub 没有对应事实时，才读取显式冻结 fallback；导入后可完全删除该目录。
        source = self._source_root() / "normalized/articles.json"
        _assert_no_symlink(source, stop=self._source_root())
        if source.is_file() and not source.is_symlink():
            articles = _read_json(source)
            if isinstance(articles, list):
                for item in articles:
                    if isinstance(item, dict) and str(item.get("article_id")) not in by_id:
                        by_id[str(item.get("article_id"))] = item
        results: list[dict[str, Any]] = []
        for item in items:
            article = by_id.get(item["article_id"])
            if article is None:
                results.append({"article_id": item["article_id"], "cover_url": None, "status": "missing_article"})
            elif str(article.get("cover_url") or "").strip():
                results.append({"article_id": item["article_id"], "cover_url": str(article["cover_url"]).strip(), "status": "cached"})
            elif item["url"] or str(article.get("raw_url") or "").strip():
                results.append({"article_id": item["article_id"], "cover_url": item["url"] or str(article.get("raw_url")).strip(), "status": "frozen"})
            else:
                results.append({"article_id": item["article_id"], "cover_url": None, "status": "no_url"})
        output = {"items": results, "count": len(results)}
        if not key:
            return output
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con, transaction(con):
                prior = con.execute(
                    "SELECT status,input_json,output_json,error_json FROM command_runs WHERE module_key='wechat-aux' AND idempotency_key=?",
                    (key,),
                ).fetchone()
                if prior:
                    replay = _command_replay(prior, input_data)
                    if replay is not None:
                        return replay
                command_id = f"cmd_wechat_aux_{uuid.uuid4().hex}"
                con.execute(
                    """INSERT INTO command_runs(
                       command_id,module_key,command_type,idempotency_key,actor_id,status,input_json,output_json,created_at,updated_at
                       ) VALUES(?,?,?,?,?,'succeeded',?,?,?,?)""",
                    (command_id, "wechat-aux", "article_covers", key, actor_id, _json(input_data), _json(output), _now(), _now()),
                )
                AuditService(con).record(
                    action="wechat_aux.article_covers",
                    subject_type="article_batch",
                    subject_id=key,
                    outcome="succeeded",
                    details={"count": len(results)},
                )
        return output

    def _aidso_params(self, params: dict[str, Any]) -> dict[str, Any]:
        keyword = str(params.get("keyword") or "").strip()
        if not keyword:
            raise AuxValidation("keyword is required")
        return {
            "keyword": keyword,
            "profile_dir": str(params.get("profile_dir") or ""),
            "headless": _bool_param(params.get("headless"), True),
            "auto_login": _bool_param(params.get("auto_login"), False),
            "wait_timeout_ms": _int_param(params.get("wait_timeout_ms"), 30000),
            "login_wait_timeout_ms": _int_param(params.get("login_wait_timeout_ms"), 300000),
            "channel": None if _bool_param(params.get("no_channel"), False) else str(params.get("channel") or "chrome"),
            "executable_path": str(params.get("executable_path") or ""),
        }

    def aidso_heat(self, params: dict[str, Any], *, idempotency_key: str | None = None, actor_id: str = "user", write: bool | None = None) -> dict[str, Any]:
        if write is None:
            write = bool((idempotency_key or "").strip())
        clean_params = self._aidso_params(params)
        lookup = hashlib.sha256(_json(clean_params).encode()).hexdigest()
        key = (idempotency_key or "").strip()
        if write and not key:
            key = _implicit_command_key("aidso-keyword-heat", clean_params)
        provider_kind = self.aidso_provider.kind
        input_data = _safe_public(clean_params)
        lock = writer_lock(self.settings.lock_path)
        with lock:
            with connect(self.settings, readonly=True) as con:
                if write:
                    prior = con.execute(
                        "SELECT status,input_json,output_json,error_json FROM command_runs WHERE module_key='wechat-aux' AND idempotency_key=?",
                        (key,),
                    ).fetchone()
                    if prior:
                        replay = _command_replay(prior, input_data)
                        if replay is not None:
                            return replay
                cached = con.execute(
                    "SELECT payload_json FROM wechat_aux_provider_results WHERE provider_kind=? AND operation='keyword_heat' AND lookup_key=?",
                    (provider_kind, lookup),
                ).fetchone()
            if cached:
                output = json.loads(cached["payload_json"])
                if write:
                    self._record_command_success(key, actor_id, clean_params, output, cached=True)
                return output
            command_id = self._create_running_command(key, actor_id, input_data) if write else None
            try:
                result = self.aidso_provider.fetch(**clean_params)
                output = _safe_public(result)
                self._store_provider_result(provider_kind, lookup, output, command_id)
                if write:
                    self._finish_command(command_id, key, clean_params["keyword"], output, actor_id, success=True)
                return output
            except Exception as exc:
                # provider 原始异常可能包含 Cookie/profile/绝对路径；持久化只留稳定类别。
                error, status_code = self._aidso_error(exc)
                if write:
                    self._finish_command(
                        command_id,
                        key,
                        clean_params["keyword"],
                        error,
                        actor_id,
                        success=False,
                        status_code=status_code,
                    )
                raise

    @staticmethod
    def _aidso_error(exc: Exception) -> tuple[dict[str, Any], int]:
        if isinstance(exc, AidsoLoginRequired):
            return {"error": "login required", "login_required": True}, 409
        if isinstance(exc, AidsoProfileBusy):
            return {"error": "profile busy", "profile_busy": True}, 409
        return {"error": "provider failed"}, 502

    def _create_running_command(self, key: str, actor_id: str, params: dict[str, Any]) -> str:
        command_id = f"cmd_wechat_aux_{uuid.uuid4().hex}"
        with connect(self.settings) as con, transaction(con):
            con.execute(
                """INSERT INTO command_runs(
                   command_id,module_key,command_type,idempotency_key,actor_id,status,input_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,'running',?,?,?)""",
                (command_id, "wechat-aux", "aidso_keyword_heat", key, actor_id, _json(_safe_public(params)), _now(), _now()),
            )
        return command_id

    def _store_provider_result(self, provider_kind: str, lookup: str, output: dict[str, Any], command_id: str | None) -> None:
        with connect(self.settings) as con, transaction(con):
            con.execute(
                """INSERT INTO wechat_aux_provider_results(
                   result_id,provider_kind,operation,lookup_key,payload_json,model_version,source_ref,captured_at,command_id
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(provider_kind,operation,lookup_key) DO NOTHING""",
                (
                    f"aux_result_{uuid.uuid4().hex}",
                    provider_kind,
                    "keyword_heat",
                    lookup,
                    _json(_safe_public(output)),
                    str(output.get("model_version") or ""),
                    "provider:recorded",
                    _now(),
                    command_id,
                ),
            )

    def _record_command_success(self, key: str, actor_id: str, params: dict[str, Any], output: dict[str, Any], *, cached: bool) -> None:
        command_id = self._create_running_command(key, actor_id, params)
        self._finish_command(command_id, key, params["keyword"], output, actor_id, success=True, cached=cached)

    def _finish_command(
        self,
        command_id: str | None,
        key: str,
        keyword: str,
        output: dict[str, Any],
        actor_id: str,
        *,
        success: bool,
        cached: bool = False,
        status_code: int = 502,
    ) -> None:
        if not command_id:
            return
        with connect(self.settings) as con, transaction(con):
            if success:
                con.execute(
                    "UPDATE command_runs SET status='succeeded',output_json=?,updated_at=? WHERE command_id=?",
                    (_json(_safe_public(output)), _now(), command_id),
                )
                details = {"provider": self.aidso_provider.kind, "cached": cached}
                AuditService(con).record(action="wechat_aux.aidso_keyword_heat", subject_type="keyword", subject_id=keyword, actor_id=actor_id, outcome="succeeded", details=details)
            else:
                con.execute(
                    "UPDATE command_runs SET status='failed',error_json=?,updated_at=? WHERE command_id=?",
                    (_json({**_safe_public(output), "_status_code": status_code}), _now(), command_id),
                )
                AuditService(con).record(
                    action="wechat_aux.aidso_keyword_heat",
                    subject_type="keyword",
                    subject_id=keyword,
                    actor_id=actor_id,
                    outcome="failed",
                    details=_safe_public(output),
                )
