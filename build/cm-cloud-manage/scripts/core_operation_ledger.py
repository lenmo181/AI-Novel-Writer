"""计划、确认、taskRef 与受控游标台账。

台账职责：
- 签发并解析 CLI 受控引用（*Ref）：绑定资源类型、真实 ID、profile、来源命令、授权范围、
  创建时间与完整性校验；跨 profile、过期、类型不符或未登记一律拒绝。
- 保存写场景的冻结计划、planHash、确认状态、逐步回执摘要、下游 taskRef 与最后可信结果。
- 保存受控分页引用（cursorRef）：绑定命令、profile、查询条件摘要、下游分页状态与完整性；
  对外不暴露 nextPageCursor/pageAfter/startNum/stopNum/bNum/eNum。

存储：受限目录中的两个固定 JSON 快照，整体 HMAC 签名，交替写入。
旧版 ledger.json 只用于首次迁移读取；写入不创建或删除临时文件。
"""

import base64
import binascii
import contextlib
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

# 在模块加载期绑定真实异常类，避免运行期引用被 mock 替换后的 subprocess 属性导致 TypeError。
_SUBPROCESS_ERRORS = (OSError, subprocess.SubprocessError)

# 台账写盘的瞬时失败只限"被占用/锁冲突"：这一类有界重试；权限被拒不重试、也不建议重试。
_TRANSIENT_WINERRORS = frozenset((32, 33))  # ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION
_TRANSIENT_ERRNOS = frozenset(
    value
    for value in (getattr(errno, "EBUSY", None), getattr(errno, "ETXTBSY", None))
    if value is not None
)
_WRITE_RETRY_BACKOFF_SECONDS = (0.05, 0.15)

from cmcloud_core import result as result_module

LEDGER_VERSION = 1
LEDGER_FILE_NAME = "ledger.json"
LEDGER_SLOT_FILE_NAMES = ("ledger.0.json", "ledger.1.json")
KEY_FILE_NAME = "ledger.key"
KEY_BYTES = 32
KEY_MIN_BYTES = 16
LOCK_FILE_NAME = "operation.lock"
SNAPSHOT_LOCK_FILE_NAME = "ledger.lock"
DEFAULT_LEDGER_DIR_NAME = ".cmcloud"
LEDGER_DIR_ENV = "CMCLOUD_LEDGER_DIR"

# 台账目录收紧的成本控制：收紧成功后写入标记，标记新鲜期内跳过重复收紧
# （目录断开继承后新条目自动继承 (OI)(CI)，无需每次重写）；同组目标合并为一次 icacls。
RESTRICTION_STAMP_FILE_NAME = ".restricted"
RESTRICTION_STAMP_MAX_AGE_SECONDS = 7 * 24 * 3600
_ICACLS_TIMEOUT_SECONDS = 100

REF_TTL_SECONDS = 12 * 3600
CURSOR_TTL_SECONDS = 3600
OPERATION_TTL_SECONDS = 1800
CONFIRM_TOKEN_PREFIX = "cmct_"

_OPERATION_TRANSACTION_LOCK = threading.RLock()


@contextlib.contextmanager
def _operation_transaction(root_dir, lock_file_name=LOCK_FILE_NAME):
    """跨线程、跨进程串行化确认签发和 token 消费。"""
    os.makedirs(root_dir, exist_ok=True)
    lock_path = os.path.join(root_dir, lock_file_name)
    with _OPERATION_TRANSACTION_LOCK, open(lock_path, "a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

OPERATION_PENDING = "PENDING_CONFIRMATION"
OPERATION_CONFIRMED = "CONFIRMED"
OPERATION_EXECUTING = "EXECUTING"
OPERATION_EXECUTED = "EXECUTED"
OPERATION_PARTIAL = "PARTIAL"
OPERATION_FAILED = "FAILED"
OPERATION_REJECTED = "REJECTED"
OPERATION_STATUSES = (
    OPERATION_PENDING,
    OPERATION_CONFIRMED,
    OPERATION_EXECUTING,
    OPERATION_EXECUTED,
    OPERATION_PARTIAL,
    OPERATION_FAILED,
    OPERATION_REJECTED,
)

STEP_OK = "OK"
STEP_FAILED = "FAILED"


class LedgerError(RuntimeError):
    """台账内部使用不当（编程错误）。"""


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(text):
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise LedgerError("台账时间字段非法")


def _default_root_dir():
    override = os.environ.get(LEDGER_DIR_ENV)
    if override:
        return os.path.abspath(override)
    # 台账按智能体隔离：不同智能体的计划、引用与游标互不可见，
    # 避免 A 智能体的待确认操作出现在 B 智能体的状态查询里。
    from cmcloud_auth import agent_scope

    agent_code, _source = agent_scope.resolve_agent_code()
    return os.path.join(os.path.expanduser("~"), DEFAULT_LEDGER_DIR_NAME, agent_code)


class LedgerStore:
    """受限本地台账；构造后所有读写都经过完整性校验。"""

    def __init__(self, root_dir=None, clock=None):
        self._root = os.path.abspath(root_dir) if root_dir else _default_root_dir()
        self._clock = clock or _utc_now
        self._path = os.path.join(self._root, LEDGER_FILE_NAME)
        self._slot_paths = tuple(os.path.join(self._root, name) for name in LEDGER_SLOT_FILE_NAMES)
        self._key = None
        self._document = None
        self._generation = 0

    # ---- 基础设施 ----

    @property
    def root_dir(self):
        return self._root

    @property
    def path(self):
        return self._path

    def now(self):
        return self._clock()

    def _load_key(self):
        if self._key is not None:
            return self._key
        key_path = os.path.join(self._root, KEY_FILE_NAME)
        try:
            if os.path.exists(key_path):
                key = _read_key(key_path)
            else:
                try:
                    key = _create_key(key_path, self._root)
                except FileExistsError:
                    # 首次并发：exists 判定与 O_EXCL 之间被另一进程抢先建好 key。
                    # 这不是错误，改为读取对方创建的密钥（O_EXCL 语义不变，绝不覆盖）。
                    _diagnostic("台账密钥已被其它进程创建，改为读取现有密钥")
                    key = _read_key(key_path)
        except result_module.ExecutionError:
            raise
        except OSError as err:
            raise result_module.ExecutionError(
                result_module.FAILED, "本地台账密钥不可读写: %s" % err.__class__.__name__
            )
        self._key = key
        return key

    def _sign(self, payload_bytes):
        return hmac.new(self._load_key(), payload_bytes, hashlib.sha256).hexdigest()

    def _empty_document(self):
        return {
            "version": LEDGER_VERSION,
            "refs": {},
            "cursors": {},
            "operations": {},
        }

    def _load(self):
        if self._document is not None:
            return self._document
        existing_slots = [path for path in self._slot_paths if os.path.exists(path)]
        candidates = []
        for path in existing_slots:
            try:
                payload, generation = self._read_snapshot(path)
            except result_module.ExecutionError:
                continue
            candidates.append((generation, payload))
        if candidates:
            self._generation, self._document = max(candidates, key=lambda item: item[0])
            return self._document
        if existing_slots and (len(existing_slots) > 1 or not os.path.exists(self._path)):
            raise result_module.ExecutionError(result_module.FAILED, "本地台账快照均不可用")
        if not os.path.exists(self._path):
            self._document = self._empty_document()
            return self._document
        self._document, self._generation = self._read_snapshot(self._path)
        return self._document

    def _read_snapshot(self, path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                wrapper = json.load(handle)
        except (OSError, ValueError) as err:
            raise result_module.ExecutionError(
                result_module.FAILED, "本地台账不可读取: %s" % err.__class__.__name__
            )
        if not isinstance(wrapper, dict) or "payload" not in wrapper or "sig" not in wrapper:
            raise result_module.ExecutionError(result_module.FAILED, "本地台账结构非法")
        generation = wrapper.get("generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise result_module.ExecutionError(result_module.FAILED, "本地台账版本非法")
        signed_value = (
            {"generation": generation, "payload": wrapper["payload"]}
            if "generation" in wrapper else wrapper["payload"]
        )
        signed_bytes = json.dumps(
            signed_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if not hmac.compare_digest(self._sign(signed_bytes), str(wrapper["sig"])):
            raise result_module.ExecutionError(
                result_module.FAILED, "本地台账完整性校验失败"
            )
        payload = wrapper["payload"]
        if not isinstance(payload, dict):
            raise result_module.ExecutionError(result_module.FAILED, "本地台账结构非法")
        if payload.get("version") != LEDGER_VERSION:
            raise result_module.ExecutionError(result_module.FAILED, "本地台账版本不受支持")
        for key in ("refs", "cursors", "operations"):
            if not isinstance(payload.get(key), dict):
                raise result_module.ExecutionError(result_module.FAILED, "本地台账字段缺失")
        return payload, generation

    def _flush(self):
        document = self._load()
        generation = self._generation + 1
        signed_value = {"generation": generation, "payload": document}
        signed_bytes = json.dumps(
            signed_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        wrapper = json.loads(signed_bytes)
        wrapper["sig"] = self._sign(signed_bytes)
        transient_attempt = 0
        permission_repaired = False
        while True:
            try:
                with _operation_transaction(self._root, SNAPSHOT_LOCK_FILE_NAME):
                    self._write_wrapper(wrapper, restrict=not permission_repaired)
                self._generation = generation
                return
            except PermissionError as err:
                if os.name != "nt" or permission_repaired or not self._repair_write_access(generation):
                    raise _write_error(err)
                permission_repaired = True
            except OSError as err:
                if not _is_transient_os_error(err) or transient_attempt == len(_WRITE_RETRY_BACKOFF_SECONDS):
                    raise _write_error(err)
                time.sleep(_WRITE_RETRY_BACKOFF_SECONDS[transient_attempt])
                transient_attempt += 1

    def _repair_write_access(self, generation):
        principal = _current_user_sid()
        if not principal:
            return False
        # 目录刚被恢复为可继承，收紧标记失效，下一次收紧重新跑全量。
        _invalidate_restriction_stamp(self._root)
        slot_path = self._slot_paths[generation % len(self._slot_paths)]
        _restore_inheritance(self._root, principal)
        for path in (os.path.join(self._root, SNAPSHOT_LOCK_FILE_NAME), slot_path):
            if os.path.exists(path):
                _restore_inheritance(path, principal)
        return True

    def _write_wrapper(self, wrapper, *, restrict=True):
        os.makedirs(self._root, exist_ok=True)
        if restrict:
            _restrict_directory(self._root)
        slot_path = self._slot_paths[wrapper["generation"] % len(self._slot_paths)]
        _write_snapshot(slot_path, wrapper)

    def _prune(self, bucket, ttl_seconds):
        now = self.now()
        stale = []
        for token, entry in bucket.items():
            expires_at = entry.get("expiresAt")
            if not isinstance(expires_at, str):
                stale.append(token)
                continue
            if _parse_iso(expires_at) <= now - timedelta(seconds=ttl_seconds):
                stale.append(token)
        for token in stale:
            bucket.pop(token, None)

    # ---- 受控引用 ----

    def issue_ref(
        self, *, kind, real_id, profile, source_command, scope,
        ttl_seconds=REF_TTL_SECONDS, parent_real_id=None, resource_path=None
    ):
        if not kind or not real_id or not profile or not source_command or not scope:
            raise LedgerError("签发引用缺少必要参数")
        document = self._load()
        self._prune(document["refs"], REF_TTL_SECONDS * 4)
        token = "cmr_%s_%s" % (kind, secrets.token_urlsafe(16))
        now = self.now()
        document["refs"][token] = {
            "kind": kind,
            "realId": real_id,
            "profile": profile,
            "sourceCommand": source_command,
            "scope": scope,
            "createdAt": _iso(now),
            "expiresAt": _iso(now + timedelta(seconds=ttl_seconds)),
        }
        if parent_real_id is not None:
            if not isinstance(parent_real_id, str) or not parent_real_id:
                raise LedgerError("引用所属资源 ID 非法")
            document["refs"][token]["parentRealId"] = parent_real_id
        if resource_path is not None:
            if not isinstance(resource_path, str) or not resource_path:
                raise LedgerError("引用资源路径非法")
            document["refs"][token]["resourcePath"] = resource_path
        self._flush()
        return token

    def resolve_ref(self, *, token, profile, expected_kind):
        if not isinstance(token, str) or not token:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "引用格式非法")
        document = self._load()
        entry = document["refs"].get(token)
        if entry is None:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "引用未登记或已被清理")
        if entry.get("profile") != profile:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "引用与当前 profile 不匹配")
        if expected_kind is not None and entry.get("kind") != expected_kind:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "引用类型与目标参数不匹配")
        if _parse_iso(entry.get("expiresAt")) <= self.now():
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "引用已过期")
        return dict(entry)

    # ---- 受控游标 ----

    def issue_cursor(self, *, command_id, profile, conditions_digest, upstream_state, complete):
        document = self._load()
        self._prune(document["cursors"], CURSOR_TTL_SECONDS * 4)
        token = "cmc_%s" % secrets.token_urlsafe(18)
        now = self.now()
        document["cursors"][token] = {
            "commandId": command_id,
            "profile": profile,
            "conditionsDigest": conditions_digest,
            "upstreamState": upstream_state,
            "complete": bool(complete),
            "createdAt": _iso(now),
            "expiresAt": _iso(now + timedelta(seconds=CURSOR_TTL_SECONDS)),
        }
        self._flush()
        return token

    def resolve_cursor(self, *, token, profile, command_id, conditions_digest):
        if not isinstance(token, str) or not token:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "游标格式非法")
        document = self._load()
        entry = document["cursors"].get(token)
        if entry is None:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "游标未登记或已被清理")
        if entry.get("profile") != profile:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "游标与当前 profile 不匹配")
        if entry.get("commandId") != command_id:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "游标不属于当前命令")
        if entry.get("conditionsDigest") != conditions_digest:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "查询条件已改变，游标不可复用")
        if _parse_iso(entry.get("expiresAt")) <= self.now():
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "游标已过期")
        return dict(entry)

    def drop_cursor(self, token):
        document = self._load()
        if document["cursors"].pop(token, None) is not None:
            self._flush()

    # ---- 写计划与任务 ----

    def create_operation(self, *, profile, scenario_id, command_id, normalized_input, resolved_ids, plan, plan_hash):
        document = self._load()
        self._prune(document["operations"], OPERATION_TTL_SECONDS * 4)
        operation_ref = "cmop_%s" % secrets.token_urlsafe(18)
        now = self.now()
        document["operations"][operation_ref] = {
            "operationRef": operation_ref,
            "profileRef": profile,
            "scenarioId": scenario_id,
            "commandId": command_id,
            "normalizedInput": normalized_input,
            "resolvedIds": resolved_ids,
            "plan": plan,
            "planSummary": plan.get("summary"),
            "planHash": plan_hash,
            "status": OPERATION_PENDING,
            "createdAt": _iso(now),
            "expiresAt": _iso(now + timedelta(seconds=OPERATION_TTL_SECONDS)),
            "confirmedAt": None,
            "confirmationTokenHash": None,
            "confirmationExpiresAt": None,
            "confirmationConsumedAt": None,
            "steps": [],
            "taskRef": None,
            "taskType": None,
            "queryCommandId": None,
            "lastResult": None,
        }
        self._flush()
        return operation_ref

    def confirm_operation(self, operation_ref, *, profile, plan_hash, confirmed_at=None):
        """确认冻结计划并签发与 operation/profile/planHash 绑定的一次性 token。"""
        with _operation_transaction(self._root):
            self._document = None
            document = self._load()
            entry = document["operations"].get(operation_ref)
            if entry is None:
                raise result_module.ExecutionError(
                    result_module.INVALID_ARGUMENT, "operationRef 未登记或已被清理"
                )
            if entry.get("profileRef") != profile:
                raise result_module.ExecutionError(
                    result_module.INVALID_ARGUMENT, "operationRef 与当前 profile 不匹配"
                )
            if entry.get("status") != OPERATION_PENDING:
                raise result_module.ExecutionError(
                    result_module.INVALID_ARGUMENT, "该 operationRef 不在待确认状态"
                )
            if entry.get("planHash") != plan_hash:
                raise result_module.ExecutionError(
                    result_module.INVALID_ARGUMENT, "确认事件与冻结计划不一致"
                )
            expires_at = entry.get("expiresAt")
            if _parse_iso(expires_at) <= self.now():
                raise result_module.ExecutionError(
                    result_module.PLAN_EXPIRED, "该计划的确认窗口已过期"
                )
            payload = {
                "operationRef": operation_ref,
                "profileRef": profile,
                "planHash": plan_hash,
                "expiresAt": expires_at,
                "nonce": secrets.token_urlsafe(12),
            }
            payload_bytes = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            encoded = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
            token = "%s%s.%s" % (CONFIRM_TOKEN_PREFIX, encoded, self._sign(payload_bytes))
            entry["status"] = OPERATION_CONFIRMED
            entry["confirmedAt"] = confirmed_at or _iso(self.now())
            entry["confirmationTokenHash"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
            entry["confirmationExpiresAt"] = expires_at
            entry["confirmationConsumedAt"] = None
            self._flush()
            return token

    def sign_confirmation_event(self, event):
        """供可信宿主桥接层签名用户确认事件；该能力不暴露为 CLI 命令。"""
        if not isinstance(event, dict):
            raise LedgerError("确认事件必须是对象")
        fields = {
            "operationRef": event.get("operationRef"),
            "profileRef": event.get("profileRef"),
            "planHash": event.get("planHash"),
            "confirmedAt": event.get("confirmedAt") or _iso(self.now()),
        }
        if not all(isinstance(value, str) and value for value in fields.values()):
            raise LedgerError("确认事件缺少绑定字段")
        payload = json.dumps(
            fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        fields["signature"] = self._sign(b"confirmation-event:" + payload)
        return fields

    def verify_confirmation_event(self, event, *, operation_ref, profile, plan_hash):
        """验证宿主确认事件签名及其与当前冻结计划的绑定。"""
        if not isinstance(event, dict):
            raise result_module.ExecutionError(
                result_module.CONFIRMATION_REQUIRED, "未取得可信宿主确认事件"
            )
        fields = {
            "operationRef": event.get("operationRef"),
            "profileRef": event.get("profileRef"),
            "planHash": event.get("planHash"),
            "confirmedAt": event.get("confirmedAt"),
        }
        if fields["operationRef"] != operation_ref:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT, "确认事件与当前操作不匹配"
            )
        if fields["profileRef"] != profile:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT, "确认事件与当前身份不匹配"
            )
        if fields["planHash"] != plan_hash:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT, "确认事件与冻结计划不一致"
            )
        if not isinstance(fields["confirmedAt"], str) or not fields["confirmedAt"]:
            raise result_module.ExecutionError(result_module.CONSENT_DENIED, "确认事件签名无效")
        payload = json.dumps(
            fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        expected = self._sign(b"confirmation-event:" + payload)
        supplied = event.get("signature")
        if not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
            raise result_module.ExecutionError(result_module.CONSENT_DENIED, "确认事件签名无效")
        return dict(fields)

    def consume_confirmation_token(self, operation_ref, *, profile, token):
        """验证并一次性消费 token，同时把 CONFIRMED 原子推进到 EXECUTING。"""
        if not isinstance(token, str) or not token.startswith(CONFIRM_TOKEN_PREFIX):
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "确认凭据格式非法")
        try:
            encoded, supplied_signature = token[len(CONFIRM_TOKEN_PREFIX):].rsplit(".", 1)
            padded = encoded + "=" * (-len(encoded) % 4)
            payload_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError):
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "确认凭据格式非法")
        if not isinstance(payload, dict) or not hmac.compare_digest(
            self._sign(payload_bytes), supplied_signature
        ):
            raise result_module.ExecutionError(result_module.CONSENT_DENIED, "确认凭据签名无效")
        if payload.get("operationRef") != operation_ref:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT, "确认凭据与当前操作不匹配"
            )
        if payload.get("profileRef") != profile:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT, "确认凭据与当前身份不匹配"
            )
        with _operation_transaction(self._root):
            self._document = None
            document = self._load()
            entry = document["operations"].get(operation_ref)
            if entry is None:
                raise result_module.ExecutionError(
                    result_module.INVALID_ARGUMENT, "operationRef 未登记或已被清理"
                )
            if payload.get("planHash") != entry.get("planHash"):
                raise result_module.ExecutionError(
                    result_module.INVALID_ARGUMENT, "确认凭据与冻结计划不匹配"
                )
            expires_at = payload.get("expiresAt")
            if expires_at != entry.get("confirmationExpiresAt") or _parse_iso(expires_at) <= self.now():
                raise result_module.ExecutionError(result_module.PLAN_EXPIRED, "确认凭据已过期")
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(str(entry.get("confirmationTokenHash") or ""), token_hash):
                raise result_module.ExecutionError(
                    result_module.INVALID_ARGUMENT, "确认凭据与当前计划不匹配"
                )
            if entry.get("confirmationConsumedAt") is not None:
                raise result_module.ExecutionError(result_module.CONSENT_DENIED, "确认凭据已使用")
            if entry.get("status") != OPERATION_CONFIRMED:
                raise result_module.ExecutionError(
                    result_module.CONFIRMATION_REQUIRED, "当前计划不处于可执行的已确认状态"
                )
            entry["confirmationConsumedAt"] = _iso(self.now())
            entry["status"] = OPERATION_EXECUTING
            self._flush()
            return dict(entry)

    def get_operation(self, operation_ref, profile=None):
        document = self._load()
        entry = document["operations"].get(operation_ref)
        if entry is None:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "operationRef 未登记或已被清理")
        if profile is not None and entry.get("profileRef") != profile:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "operationRef 与当前 profile 不匹配")
        return dict(entry)

    def update_operation(self, operation_ref, **fields):
        document = self._load()
        entry = document["operations"].get(operation_ref)
        if entry is None:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "operationRef 未登记或已被清理")
        for key, value in fields.items():
            if key not in entry:
                raise LedgerError("台账字段未定义: %s" % key)
            entry[key] = value
        if entry.get("status") not in OPERATION_STATUSES:
            raise LedgerError("台账状态非法: %s" % entry.get("status"))
        self._flush()
        return dict(entry)

    def append_step(self, operation_ref, *, name, status, summary, task_ref=None):
        document = self._load()
        entry = document["operations"].get(operation_ref)
        if entry is None:
            raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "operationRef 未登记或已被清理")
        if status not in (STEP_OK, STEP_FAILED):
            raise LedgerError("步骤状态非法: %s" % status)
        step = {
            "name": name,
            "status": status,
            "summary": summary,
            "at": _iso(self.now()),
        }
        if task_ref is not None:
            step["taskRef"] = task_ref
        entry["steps"].append(step)
        if status == STEP_FAILED:
            entry["status"] = OPERATION_FAILED
        self._flush()
        return dict(entry)

def _restriction_stamp_path(path):
    return os.path.join(path, RESTRICTION_STAMP_FILE_NAME)


def _restriction_is_fresh(path):
    """标记新鲜期内跳过重复收紧；标记缺失、过期或不可读时按未收紧处理。"""
    try:
        with open(_restriction_stamp_path(path), "r", encoding="utf-8") as handle:
            restricted_at = datetime.fromisoformat(handle.read().strip())
    except (OSError, ValueError):
        return False
    if restricted_at.tzinfo is None:
        return False
    return (_utc_now() - restricted_at) < timedelta(seconds=RESTRICTION_STAMP_MAX_AGE_SECONDS)


def _write_restriction_stamp(path):
    try:
        with open(_restriction_stamp_path(path), "w", encoding="utf-8") as handle:
            handle.write(_iso(_utc_now()))
    except OSError:
        pass


def _invalidate_restriction_stamp(path):
    try:
        os.remove(_restriction_stamp_path(path))
    except OSError:
        pass


def _run_icacls(targets, spec):
    """一次 icacls 调用收紧一组同质目标；返回是否成功完成，失败不抛出由调用方兜底。"""
    try:
        completed = subprocess.run(
            ["icacls", *targets, "/inheritance:r", "/grant:r", spec],
            check=False,
            capture_output=True,
            timeout=_ICACLS_TIMEOUT_SECONDS,
        )
    except _SUBPROCESS_ERRORS:
        return False
    return getattr(completed, "returncode", None) == 0


def _restrict_directory(path):
    """把台账目录收紧到当前用户：POSIX 用 chmod，Windows 用 DACL。

    Windows 上 `os.chmod` 只影响只读位、**不改 ACL**，只调 chmod 会让"目录已收紧"成为假象；
    因此 Windows 分支用 icacls 断开继承并只授权当前用户（对目录与已存在条目一起处理，
    新条目经 (OI)(CI) 继承）。失败不阻断台账可用性，但不会假装已收紧。

    收紧动作自身有把文件锁死的风险（继承被删、授权主体名解析不到），因此：
    - 授权主体优先用 SID（`*S-1-5-...`），避免 `%USERNAME%` 被覆盖或解析成空主体；
    - 已存在的文件只用读写打开验证；目录直接用本次真实台账写入验证，
      不创建再删除探测文件。权限被拒时在实际写入处恢复继承并重试。
    - 成本：icacls 单次调用在受限环境（沙箱/重定向文件系统）可能秒级，条目数增长会
      线性拖慢每次账本写入；因此收紧成功后写入标记，标记新鲜期内跳过重复收紧
      （目录断开继承后新条目自动继承 (OI)(CI)），且同组目标合并为一次 icacls 调用。
    """
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        return
    if _restriction_is_fresh(path):
        return
    principal = _current_user_sid()
    if not principal:
        return
    directories = []
    files = []
    for target in _restrict_targets(path):
        (directories if os.path.isdir(target) else files).append(target)
    all_ok = True
    for target in directories:
        all_ok = _run_icacls((target,), _grant_spec(target, principal)) and all_ok
    if files:
        # icacls 不接受多目标参数列表（第二个路径会被判非法参数）；用目录通配符一次
        # 处理全部子项（账本目录是扁平的，子项均按文件授权）。空匹配也返回 0。
        wildcard = os.path.join(path, "*")
        all_ok = _run_icacls((wildcard,), principal + ":F") and all_ok
    for target in files:
        writable, probe_error = _existing_file_writable(target)
        if writable or _is_transient_os_error(probe_error):
            continue
        _restore_inheritance(target, principal)
    if all_ok:
        _write_restriction_stamp(path)


def _current_user_sid():
    """受限授权主体：优先 SID（`*S-1-5-...`），退回 `DOMAIN\\账号` / 账号名。

    用 SID 是因为 `icacls /grant:r <名字>` 依赖名字解析：`%USERNAME%` 被覆盖或解析不到时
    会写成空主体（实测得到 `LIANGGUAN\\:(OI)(CI)(F)`），把当前用户挡在台账目录之外。
    """
    sid = _whoami_sid()
    if sid:
        return "*" + sid
    user = _current_user()
    if not user:
        return None
    try:
        domain = os.environ.get("USERDOMAIN")
    except Exception:
        domain = None
    if isinstance(domain, str) and domain.strip():
        return "%s\\%s" % (domain.strip(), user)
    return user


def _whoami_sid():
    """从 `whoami /user` 解析当前账号 SID；取不到返回 None。"""
    try:
        completed = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except _SUBPROCESS_ERRORS:
        return None
    if getattr(completed, "returncode", None) != 0:
        return None
    text = getattr(completed, "stdout", None)
    if not isinstance(text, str):
        return None
    match = re.search(r"S-1-[0-9-]+", text)
    return match.group(0) if match else None


def _restrict_targets(path):
    """收紧目标：目录本身 + 目录内已存在条目（新条目经 (OI)(CI) 继承）。"""
    targets = [path]
    try:
        targets.extend(os.path.join(path, name) for name in os.listdir(path))
    except OSError:
        pass
    return targets


def _existing_file_writable(target):
    """验证已有台账文件可写；不创建、移动或删除任何文件。"""
    try:
        descriptor = os.open(target, os.O_RDWR)
        os.close(descriptor)
        return True, None
    except OSError as err:
        return False, err


def _write_snapshot(path, wrapper):
    """覆盖一个固定槽；另一槽保留上次完整快照，不使用临时文件或删除操作。"""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(wrapper, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())


def _grant_spec(target, principal):
    """icacls 授权串：目录用 `(OI)(CI)F`（供后续子项继承），文件只能用 `F`。

    对文件套用 `(OI)(CI)F` 时 icacls 返回 0 但会**清空该文件的 DACL**（实测：之后连读取都
    被拒），是"台账目录跑过一次就再也写不进去"的成因；文件必须用普通 `F`。
    """
    return "%s:%s" % (principal, "(OI)(CI)F" if os.path.isdir(target) else "F")


def _restore_inheritance(target, principal):
    """收紧把目录锁死时的回退：恢复继承并补一条当前主体的显式授权。"""
    for args in (
        ["icacls", target, "/inheritance:e"],
        ["icacls", target, "/grant", _grant_spec(target, principal)],
    ):
        try:
            subprocess.run(args, check=False, capture_output=True, timeout=10)
        except _SUBPROCESS_ERRORS:
            pass
    _diagnostic("台账目录收紧后自身不可写，已恢复继承: %s" % os.path.basename(target))


def _read_key(key_path, attempts=5, delay_seconds=0.02):
    """读取已存在的台账密钥；长度不足时短暂重试（并发创建期密钥可能尚未写完）。"""
    for attempt in range(attempts):
        with open(key_path, "rb") as handle:
            key = handle.read()
        if len(key) >= KEY_MIN_BYTES:
            return key
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    raise result_module.ExecutionError(result_module.FAILED, "本地台账密钥不可用")


def _create_key(key_path, root_dir):
    """首次创建台账密钥；O_EXCL 保证不覆盖其它进程已创建的密钥。"""
    os.makedirs(root_dir, exist_ok=True)
    key = secrets.token_bytes(KEY_BYTES)
    _restrict_directory(root_dir)
    # Windows 文本模式会对随机密钥字节执行换行转换，必须显式使用二进制模式。
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(key_path, flags, 0o600)
    except PermissionError:
        if os.name != "nt":
            raise
        principal = _current_user_sid()
        if not principal:
            raise
        _restore_inheritance(root_dir, principal)
        descriptor = os.open(key_path, flags, 0o600)
    try:
        os.write(descriptor, key)
    finally:
        os.close(descriptor)
    return key


def _write_error(err):
    """台账写盘失败的统一载体：区分占用（可重试）与权限被拒（不可重试）。"""
    transient = _is_transient_os_error(err)
    message = (
        "本地台账被其它进程占用，重试后仍不可写入: %s" % err.__class__.__name__
        if transient
        else "本地台账不可写入: %s" % err.__class__.__name__
    )
    return result_module.ExecutionError(result_module.FAILED, message, retryable=transient)


def _is_transient_os_error(err):
    """占用/锁冲突属瞬时失败（可重试）；权限被拒属环境问题，不重试。"""
    if err is None:
        return False
    if getattr(err, "winerror", None) in _TRANSIENT_WINERRORS:
        return True
    return getattr(err, "errno", None) in _TRANSIENT_ERRNOS


def _diagnostic(message):
    """诊断只写 stderr，且不影响台账可用性；stdout 永远只有一个 JSON 文档。"""
    try:
        sys.stderr.write("[cmcloud-ledger] %s\n" % message)
        sys.stderr.flush()
    except Exception:
        pass


def _current_user():
    """当前 OS 账号名；取不到返回 None（此时不假装已收紧 ACL）。"""
    for name in ("USERNAME", "USER"):
        value = os.environ.get(name)
        if value and str(value).strip():
            return str(value).strip()
    try:
        import getpass

        return getpass.getuser() or None
    except Exception:
        return None


def plan_hash_of(plan):
    """计划摘要哈希：确认必须与冻结计划逐字节绑定。"""
    canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def conditions_digest_of(conditions):
    canonical = json.dumps(conditions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
