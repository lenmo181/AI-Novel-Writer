# S05 下载文件：把允许范围内的云端文件下载到本地目录。
# 流程：资源/本地目录核验 -> 可信确认 -> 获取下载地址 -> 独占创建或覆盖下载 -> 完整性校验。
# CLI: cmcloud files download --format json

import os
import secrets

from cmcloud_core import result as result_module
from cmcloud_adapters import file_batch_get_download_url, file_download, file_get, file_get_download_url
from cmcloud_workflows import common

BATCH_LIMIT = 100
CONFLICT_POLICIES = ("fail", "overwrite", "rename")
INTEGRITY_POLICIES = ("size", "sha256")


def prepare(context, input_data):
    local_directory = common.local_path(context, input_data["localDirectory"], must_be_dir=True)
    conflict_policy = input_data.get("conflictPolicy") or "fail"
    integrity_policy = input_data.get("integrityPolicy") or "size"
    if conflict_policy not in CONFLICT_POLICIES:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "conflictPolicy 取值非法")
    if integrity_policy not in INTEGRITY_POLICIES:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "integrityPolicy 取值非法")
    targets = []
    preflight_failures = []
    for item_index, token in enumerate(input_data["resources"]):
        ref_error = common.preloaded_ref_error(context, "resources", item_index)
        if ref_error is not None:
            preflight_failures.append(_failed_item(item_index, ref_error))
            continue
        file_id = context.real_id(token, "file")
        try:
            node = common.ensure_file(context, file_id)
        except result_module.ExecutionError as err:
            if _is_request_level_error(err):
                raise
            preflight_failures.append(_failed_item(item_index, err))
            continue
        name = common.field_text(context, node, "name")
        # 下游文件名不可信：先按本机规则校验，再确认拼接结果仍在所选目录内。
        local_path = common.ensure_within_directory(
            local_directory, os.path.join(local_directory, common.safe_local_file_name(name))
        )
        targets.append(
            {
                "fileRef": token,
                "fileId": file_id,
                "itemIndex": item_index,
                "name": name,
                "localPath": local_path,
            }
        )
    if not targets:
        counts = {
            "requested": len(input_data["resources"]),
            "succeeded": 0,
            "failed": len(preflight_failures),
        }
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "没有可下载的有效文件",
            data={"items": preflight_failures, "localDirectory": local_directory},
            counts=counts,
        )
    return {
        "summary": "下载 %d 个云端文件到本地目录 %s" % (len(targets), local_directory),
        "resolvedIds": {"targets": targets},
        "requested": len(input_data["resources"]),
        "preflightFailures": preflight_failures,
        "localDirectory": local_directory,
        "conflictPolicy": conflict_policy,
        "integrityPolicy": integrity_policy,
    }


def execute(context, plan):
    targets = plan["resolvedIds"]["targets"]
    urls = _download_urls(context, targets)
    succeeded = 0
    items = list(plan.get("preflightFailures") or ())
    failed = len(items)
    for target in targets:
        entry = urls.get(target["fileId"])
        reason = None
        downloaded = None
        if entry:
            try:
                downloaded = _download_one(context, entry, target, plan)
            except result_module.ExecutionError as err:
                if err.code == result_module.SCOPE_DENIED:
                    raise
                reason = err.code
            except OSError:
                reason = result_module.FAILED
        else:
            reason = result_module.FAILED
        if downloaded is None:
            failed += 1
            items.append(
                {
                    "itemIndex": target.get("itemIndex"),
                    "fileRef": context.issue_ref("file", target["fileId"], common.SCOPE_AUTHORIZED_PERSONAL),
                    "name": target["name"],
                    "status": "FAILED",
                    "errorCode": reason or result_module.FAILED,
                }
            )
            continue
        succeeded += 1
        items.append(
            {
                "itemIndex": target.get("itemIndex"),
                "fileRef": context.issue_ref("file", target["fileId"], common.SCOPE_AUTHORIZED_PERSONAL),
                "name": target["name"],
                "localPath": os.path.basename(downloaded),
                "size": entry.get("size"),
                "status": "DOWNLOADED",
            }
        )
    items.sort(key=lambda item: item.get("itemIndex", 0))
    counts = {
        "requested": plan.get("requested", len(targets) + len(plan.get("preflightFailures") or ())),
        "succeeded": succeeded,
        "failed": failed,
    }
    data = {"items": items, "localDirectory": plan["localDirectory"]}
    if failed and succeeded:
        return common.partial(data=data, counts=counts)
    if failed:
        raise result_module.ExecutionError(
            result_module.FAILED, "全部下载项均失败", data=data, counts=counts
        )
    return common.success(data=data, counts=counts)


def _is_request_level_error(error):
    return error.code in (
        result_module.AUTH_REQUIRED,
        result_module.AUTH_EXPIRED,
        result_module.SCOPE_DENIED,
        result_module.UPSTREAM_CONTRACT_MISMATCH,
    )


def _failed_item(item_index, error):
    return {
        "itemIndex": item_index,
        "name": "第%d项" % (item_index + 1),
        "status": "FAILED",
        "errorCode": error.code,
        "reason": common.batch_resource_failure_reason(error),
    }


def _download_urls(context, targets):
    entries = {}
    if len(targets) == 1:
        target = targets[0]
        payload = common.guard_upstream(context, file_get_download_url.call, file_id=target["fileId"])
        data = common.payload_data(context, payload)
        entries[target["fileId"]] = {
            "url": common.field_text(context, data, "url"),
            "size": data.get("size"),
        }
        return entries
    ids = [target["fileId"] for target in targets]
    for index in range(0, len(ids), BATCH_LIMIT):
        chunk = ids[index : index + BATCH_LIMIT]
        payload = common.guard_upstream(context, file_batch_get_download_url.call, file_ids=chunk)
        data = common.payload_data(context, payload)
        items = data.get("items")
        if not isinstance(items, list):
            common.contract_mismatch(context, "批量下载地址响应缺少 items 列表")
        for item in items:
            if not isinstance(item, dict):
                common.contract_mismatch(context, "批量下载地址项不是对象")
            file_id = item.get("fileId")
            err_code = item.get("errCode")
            if err_code not in (None, "0000") or not isinstance(item.get("url"), str):
                continue
            entries[file_id] = {"url": item["url"], "size": item.get("size")}
    return entries


def _download_one(context, entry, target, plan):
    # 计划冻结后目录可能被替换（符号链接/目录联接），写盘前再校验一次包含关系。
    final_path = common.ensure_within_directory(plan["localDirectory"], target["localPath"])
    final_path = _resolve_conflict(plan["conflictPolicy"], final_path)
    if plan["conflictPolicy"] != "overwrite":
        return _download_without_overwrite(context, entry, target, plan, final_path)
    temp_path = "%s.%s.tmp" % (final_path, secrets.token_hex(8))
    written = 0
    try:
        stream = common.guard_upstream(
            context, file_download.call, download_url=entry["url"]
        )
        try:
            with open(temp_path, "wb") as handle:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
        finally:
            _close(stream)
        _verify_size(context, entry, written, plan["integrityPolicy"], target)
        os.replace(temp_path, final_path)
    except BaseException:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise
    context.note_step("download", "已下载 %s" % target["name"])
    return final_path


def _download_without_overwrite(context, entry, target, plan, final_path):
    """独占创建目标，成功路径不生成或删除临时硬链接。

    文件在下载期间可见；只有通过长度校验后才返回成功。失败时把未完成文件
    改名留证，不把残缺内容保留在请求的目标路径，也不触发宿主的删除确认。
    """
    created = False
    written = 0
    stream = common.guard_upstream(context, file_download.call, download_url=entry["url"])
    try:
        try:
            with open(final_path, "xb") as handle:
                created = True
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            raise result_module.ExecutionError(result_module.CONFLICT, "下载期间出现同名文件，未覆盖")
        _verify_size(context, entry, written, plan["integrityPolicy"], target)
    except BaseException:
        if created:
            quarantine = "%s.incomplete-%s" % (final_path, secrets.token_hex(8))
            try:
                os.rename(final_path, quarantine)
            except OSError:
                # 留在原路径也不能伪报成功；不以删除文件掩盖本次失败。
                pass
        raise
    finally:
        _close(stream)
    context.note_step("download", "已下载 %s" % target["name"])
    return final_path


def _close(stream):
    close = getattr(stream, "close", None)
    if callable(close):
        close()


def _verify_size(context, entry, written, integrity_policy, target):
    expected = entry.get("size")
    if expected is None:
        if integrity_policy == "size":
            common.contract_mismatch(context, "下载地址未返回 size，无法按长度校验")
        return
    if isinstance(expected, bool) or not isinstance(expected, int):
        common.contract_mismatch(context, "下载地址返回的 size 类型非法")
    if expected != written:
        raise result_module.ExecutionError(
            result_module.FAILED, "本地落盘长度与云端声明不一致", retryable=True
        )


def _resolve_conflict(policy, final_path):
    if not os.path.exists(final_path):
        return final_path
    if policy == "overwrite":
        return final_path
    if policy == "rename":
        base, extension = os.path.splitext(final_path)
        for index in range(1, 1000):
            candidate = "%s(%d)%s" % (base, index, extension)
            if not os.path.exists(candidate):
                return candidate
        raise result_module.ExecutionError(result_module.CONFLICT, "同目录同名文件过多，无法自动改名")
    raise result_module.ExecutionError(result_module.CONFLICT, "本地已存在同名文件")
