"""写动作编排：把同一用户请求里的多个云端写命令冻结为一个计划、一次确认、按序执行。

- 输入：按用户语序排列的业务命令串（`<domain> <action> --旗标 值`），一条一个 `--step`；
- 准入：步骤只能命中**云端写命令**（`confirmation=TRUSTED_PLAN`）；读命令、下载（只写本地）
  与 `auth` 域命令一律拒绝，由调用方单独执行；
- 依赖：后续步骤可用 `?stepN.<产物名>` 声明"取第 N 步产出的受控引用"。冻结期只校验顺序、
  类型与参数结构，真实引用在执行期注入；
- 语义：一次确认 → 一次执行；按序推进；任一步失败即停，已完成步骤不回滚、不重做，
  未开始步骤不执行。
"""

import copy
import os
import re

from cmcloud_core import executor
from cmcloud_core import operation_ledger as ledger_module
from cmcloud_core import registry
from cmcloud_core import result as result_module
from cmcloud_workflows import common as workflow_common

_PLACEHOLDER_PATTERN = re.compile(r"\A\?step([1-9][0-9]*)\.([A-Za-z][A-Za-z0-9]*)\Z")

_REF_TOKEN_PATTERN = re.compile(r"\Acmr_([a-z]+)_[A-Za-z0-9._-]+\Z")

_STEP_TOKEN_PATTERN = re.compile(r"'([^']*)'|(\S+)")

# 数组产物是显式新增能力；不改变旧计划的标量占位符解释。
_TRANSFER_SCENES = frozenset(("S20", "S21"))
_ARRAY_TARGET_SCENES = frozenset(("S30", "S31"))

_ACTION_LABELS = {
    "files.upload": "上传文件到云盘",
    "files.mkdir": "新建目录",
    "files.copy": "复制到云盘目录",
    "share.create": "生成分享链接",
    "share.subscribe": "订阅外链",
    "share.save": "转存到云盘",
    "album.create": "新建相册",
    "album.add": "把图片加入相册",
    "album.update": "修改相册",
    "album.share": "生成相册分享链接",
}

_OBJECT_HINTS = {
    "files.mkdir": ("name", None),
    "files.upload": ("localFile", os.path.basename),
    "album.create": ("name", None),
    "album.update": ("name", None),
}

_REQUIRED_HINT = "只有写入云盘的动作可以编排，且每个步骤都要能独立形成计划"


def is_orchestration_plan(plan):
    return isinstance(plan, dict) and isinstance(plan.get("steps"), list)


def schema_payload():
    """编排命令自身的入参 Schema：步骤内容各自遵循其业务命令的 Schema。"""
    return {
        "commandId": registry.ORCHESTRATION_COMMAND_ID,
        "command": registry.ORCHESTRATION_COMMAND_ID,
        "schemaId": registry.ORCHESTRATION_SCENE.schema_id,
        "capability": registry.ORCHESTRATION_SCENE.capability_state,
        "disabledReason": registry.ORCHESTRATION_SCENE.disabled_reason,
        "requiresConfirmation": registry.ORCHESTRATION_SCENE.requires_confirmation,
        "maxSteps": None,
        "placeholderSyntax": "?step<N>.<产物名>",
        "arrayBinding": "转存后建册/加图：--fileRefs '[\"?step1.fileRefs\"]'，整体展开成功文件集合",
        "shared": False,
        "selectorFields": [],
        "scenes": [],
        "schema": {
            "type": "object",
            "properties": {
                "step": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 8192},
                    "minItems": 1,
                },
                "profile": {"type": "string", "minLength": 1},
                "format": {"const": "json"},
            },
            "required": ["step"],
            "additionalProperties": False,
        },
    }


def help_payload(action=None):
    """编排命令的发现入口：步骤怎么写、允许哪些命令、依赖怎么声明。"""
    if action is not None:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "orchestrate 没有子动作，用法见 help --topic orchestrate"
        )
    step_commands = list(
        dict.fromkeys(
            scene.command_id for scene in registry.all_commands() if scene.operation_type == registry.OPERATION_WRITE and scene.requires_confirmation
        )
    )
    return {
        "commandId": registry.ORCHESTRATION_COMMAND_ID,
        "summary": "把同一请求里的多个云端写动作冻结为一个计划、一次确认、按语序一次执行",
        "usage": [
            "orchestrate --step \"<domain> <action> --<schema-field> VALUE\" [--step ...]"
            " [--profile P] [--format json]"
        ],
        "fields": {
            "step": "可重复；每个值是一条业务命令串（<domain> <action> --旗标 值），按用户语序排列",
            "profile": "可选，缺省 default",
            "format": "固定 json",
        },
        "quoting": [
            "步骤串按空白切分；带空格的取值用单引号包住，如 --name '2026 归档'",
            "JSON 数组的双引号原样书写，如 --resources '[\"<ref>\"]'；不要用反斜杠转义双引号",
        ],
        "stepCommands": step_commands,
        "excludedCommands": [
            "只读查询与 files.download 单独执行；下载覆盖仍需确认，不进入云端写编排",
            "auth 域命令：不进编排",
        ],
        "dependency": {
            "syntax": "?step<N>.<产物名>",
            "example": "--destinationRef ?step1.folderRef",
            "rule": "只能引用更早的步骤；产物名取自该步骤回执里的引用字段（如 folderRef/fileRef/albumRef/linkRef）",
            "arrayExample": "--fileRefs '[\"?step1.fileRefs\"]'",
            "arrayRule": "仅允许转存(S20/S21)到建册加图/相簿加图(S30/S31)；数组必须只有该占位符，不能混入其他引用；原标量规则保持不变",
        },
        "nextStep": "cmcloud schema --command-id orchestrate --format json",
    }


def plan_steps(runtime, step_texts):
    """按语序冻结全部步骤；任一步骤不可冻结即整体失败，不产生业务副作用。

    不设步数上限：同一请求里的写动作有多少就编排多少，始终只出一次确认。
    """
    texts = list(step_texts or ())
    if not texts:
        raise result_module.ExecutionError(result_module.INVALID_ARGUMENT, "至少需要一个 --step")
    steps = []
    for index, text in enumerate(texts, 1):
        steps.append(_freeze_step(runtime, index, text, steps))
    summary = "共 %d 步：%s" % (len(steps), "；".join(step["label"] for step in steps))
    return {"summary": summary, "steps": steps}


def query_frozen_status(runtime, record):
    """编排恢复：可查询未确认的转存原任务，不重放任何写请求。"""
    status = record.get("status")
    if status == ledger_module.OPERATION_EXECUTING:
        last = record.get("lastResult") or {}
        if isinstance(last, dict) and last.get("remainingSteps"):
            return workflow_common.unknown(
                data=last, counts=last.get("counts"),
                message="原转存任务已确认成功，后续步骤尚未执行；请为剩余步骤重新计划",
            )
        recovered = _query_pending_transfer(runtime, record)
        if recovered is not None:
            return recovered
    data = {
        "operationRef": record.get("operationRef"),
        "operationStatus": status,
        "steps": record.get("steps") or [],
        "stepResults": record.get("lastResult"),
    }
    if status == ledger_module.OPERATION_PENDING:
        return workflow_common.Outcome(
            result_module.STATUS_CONFIRMATION_REQUIRED,
            data=data,
            next_action=result_module.NEXT_ACTION_OPERATION_CONFIRM,
        )
    if status == ledger_module.OPERATION_CONFIRMED:
        return workflow_common.Outcome(
            result_module.STATUS_RUNNING,
            data=data,
            next_action=result_module.NEXT_ACTION_OPERATION_EXECUTE,
        )
    if status == ledger_module.OPERATION_EXECUTED:
        return workflow_common.success(data=data)
    if status == ledger_module.OPERATION_PARTIAL:
        return workflow_common.partial(data=data)
    if status == ledger_module.OPERATION_REJECTED:
        return workflow_common.Outcome(
            result_module.STATUS_REJECTED,
            data=data,
            error=result_module.build_error(result_module.SCOPE_DENIED, "该操作已被拒绝"),
        )
    if status == ledger_module.OPERATION_FAILED:
        raise result_module.ExecutionError(result_module.FAILED, "该操作已失败", data=data)
    return workflow_common.running(data=data)


def _query_pending_transfer(runtime, record):
    """仅查询暂停步骤的原任务；不重放已完成步骤，不执行后续写入。"""
    last = record.get("lastResult") or {}
    results = last.get("steps") if isinstance(last, dict) else None
    if not isinstance(results, list) or not results:
        return None
    pending = results[-1]
    if pending.get("status") not in (result_module.STATUS_UNKNOWN, result_module.STATUS_RUNNING):
        return None
    steps = (record.get("plan") or {}).get("steps") or []
    step = next((item for item in steps if item.get("index") == pending.get("index")), None)
    task_ref = (pending.get("data") or {}).get("taskRef")
    if not step or step.get("scenarioId") not in ("S20", "S21") or not task_ref:
        return None
    spec = registry.get_scene(step["scenarioId"])
    previous_spec, previous_ref = runtime.spec, runtime.operation_ref
    try:
        runtime.spec, runtime.operation_ref = spec, record["operationRef"]
        executor._assert_capability(spec, runtime)
        outcome = registry.workflow_callable(spec, "query_status")(
            runtime, {"taskRef": task_ref, "plan": step.get("plan") or {}}
        )
        envelope = executor._envelope(spec, outcome, runtime)
    except result_module.ExecutionError as err:
        envelope = result_module.envelope_from_error(spec.command_id, err, runtime.trace_id)
    finally:
        runtime.spec, runtime.operation_ref = previous_spec, previous_ref
    results = list(results[:-1]) + [_step_result(step, envelope)]
    failure = envelope if envelope.status not in (result_module.STATUS_SUCCESS, result_module.STATUS_EMPTY) else None
    outcome = _orchestration_outcome(runtime, results, failure, len(steps))
    if failure is None and len(results) < len(steps):
        outcome = _outcome(
            result_module.STATUS_UNKNOWN, results,
            {"requested": len(steps), "completed": len(results), "failed": 0},
            result_module.build_error(
                result_module.UNKNOWN, "原转存任务已确认成功，后续步骤尚未执行；请为剩余步骤重新计划"
            ), None,
        )
        outcome.data["remainingSteps"] = [item["index"] for item in steps[len(results):]]
    operation_status = {
        result_module.STATUS_SUCCESS: ledger_module.OPERATION_EXECUTED,
        result_module.STATUS_PARTIAL: ledger_module.OPERATION_PARTIAL,
        result_module.STATUS_FAILED: ledger_module.OPERATION_FAILED,
    }.get(outcome.status, ledger_module.OPERATION_EXECUTING)
    last_result = dict(outcome.data)
    last_result["counts"] = outcome.counts
    runtime.ledger.update_operation(record["operationRef"], status=operation_status, lastResult=last_result)
    return outcome


def execute_frozen(runtime, plan):
    """按语序执行已确认的编排计划；返回编排级 Outcome。"""
    steps = plan["steps"]
    outputs = {}
    results = []
    failure = None
    for step in steps:
        spec = registry.get_scene(step["scenarioId"])
        runtime.spec = spec
        envelope = _run_step(runtime, step, outputs)
        outputs[str(step["index"])] = _collect_outputs(envelope, step["scenarioId"])
        results.append(_step_result(step, envelope))
        if envelope.status not in (result_module.STATUS_SUCCESS, result_module.STATUS_EMPTY):
            failure = envelope
            break
    return _orchestration_outcome(runtime, results, failure, len(steps))


def _run_step(runtime, step, outputs):
    spec = registry.get_scene(step["scenarioId"])
    try:
        input_data = _materialize(step, outputs)
        executor.validate_input(spec, input_data)
        executor._assert_capability(spec, runtime)
        executor._assert_publication_ready(spec, runtime)
        executor.preload_refs(spec, input_data, runtime)
        _validate_bound_images(runtime, step, input_data)
        frozen = step.get("plan") or registry.workflow_callable(spec, "prepare")(runtime, input_data)
        runtime.spec = spec
        outcome = registry.workflow_callable(spec, "execute")(runtime, frozen)
    except result_module.ExecutionError as err:
        return result_module.envelope_from_error(spec.command_id, err, runtime.trace_id)
    try:
        envelope = executor._envelope(spec, outcome, runtime)
    except result_module.ExecutionError as err:
        return result_module.envelope_from_error(spec.command_id, err, runtime.trace_id)
    return envelope


def _orchestration_outcome(runtime, results, failure, requested):
    if failure is None:
        return _outcome(
            result_module.STATUS_SUCCESS,
            results,
            {"requested": requested, "completed": len(results), "failed": 0},
            None,
            None,
        )
    completed = len(results) - 1
    if failure.status in (result_module.STATUS_UNKNOWN, result_module.STATUS_RUNNING):
        return _outcome(
            failure.status, results,
            {"requested": requested, "completed": completed, "failed": None},
            failure.to_dict().get("error"), _unwrap_failure_next_action(failure),
        )
    if completed <= 0:
        return _outcome(
            failure.status,
            results,
            {"requested": requested, "completed": 0, "failed": 1},
            failure.to_dict().get("error"),
            _unwrap_failure_next_action(failure),
        )
    error = failure.to_dict().get("error") or {
        "code": result_module.FAILED,
        "message": "部分步骤失败",
        "retryable": False,
    }
    return _outcome(
        result_module.STATUS_PARTIAL,
        results,
        {"requested": requested, "completed": completed, "failed": 1},
        {
            "code": error.get("code"),
            "message": "已完成 %d 项，第 %d 项失败" % (completed, completed + 1),
            "retryable": False,
        },
        _unwrap_failure_next_action(failure),
    )


def _unwrap_failure_next_action(envelope):
    next_action = envelope.to_dict().get("nextAction")
    if next_action == result_module.NEXT_ACTION_OPERATION_STATUS:
        return next_action
    if envelope.status == result_module.STATUS_AUTH_REQUIRED:
        return result_module.NEXT_ACTION_AUTH_LOGIN
    return None


def _outcome(status, results, counts, error, next_action):
    return workflow_common.Outcome(
        status,
        data={"steps": results, "orchestrated": True},
        counts=counts,
        error=error,
        next_action=next_action,
    )


def _step_result(step, envelope):
    payload = envelope.to_dict()
    return {
        "index": step["index"],
        "label": step["label"],
        "status": payload["status"],
        "data": payload["data"],
        "counts": payload["counts"],
        "error": payload["error"],
    }


def _freeze_step(runtime, index, text, frozen_steps):
    tokens = _split_step(text, index)
    if len(tokens) < 2:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "步骤 %d 必须写成 <domain> <action> --旗标 值" % index,
        )
    domain, action, rest = tokens[0], tokens[1], tokens[2:]
    route = registry.get_route(domain, action)
    if route is None:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "步骤 %d 不是已注册的云盘命令" % index,
        )
    from cmcloud_core import main

    options = main._parse_business_options(route, rest)
    if options["flags"]:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "步骤 %d 内不支持 profile/format 参数" % index,
        )
    business = options["business"]
    spec = registry.select_scene(route, business)
    if spec.operation_type != registry.OPERATION_WRITE or not spec.requires_confirmation:
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "步骤 %d（%s）不需要确认，请单独执行；%s"
            % (index, route.command, _REQUIRED_HINT),
        )
    binds = _collect_binds(business, index, frozen_steps, spec=spec)
    previous = runtime.spec
    runtime.spec = spec
    try:
        executor.validate_input(spec, _substitute_for_validation(business, binds))
        plan = None
        if not binds:
            executor._assert_capability(spec, runtime)
            executor._assert_publication_ready(spec, runtime)
            executor.preload_refs(spec, business, runtime)
            plan = registry.workflow_callable(spec, "prepare")(runtime, business)
    finally:
        runtime.spec = previous
    label = plan.get("summary") if isinstance(plan, dict) else None
    if not isinstance(label, str) or not label:
        label = _derived_label(spec, business, binds)
    return {
        "index": index,
        "commandId": spec.command_id,
        "scenarioId": spec.scenario_id,
        "label": label,
        "args": business,
        "binds": binds,
        "plan": plan,
    }


def _derived_label(spec, business, binds):
    label = _ACTION_LABELS.get(spec.command_id, "云端写操作")
    hint = _OBJECT_HINTS.get(spec.command_id)
    if hint:
        field, transform = hint
        value = business.get(field)
        if isinstance(value, str) and value:
            label = "%s %s" % (label, transform(value) if transform else value)
    if binds:
        label = "%s（目标来自步骤 %s）" % (
            label,
            "、".join(str(bind["fromStep"]) for bind in binds),
        )
    return label


def _split_step(text, index):
    """按空白切分命令串；整段用单引号包住的取值可以带空格，单引号本身被去掉。

    JSON 数组（如 `["cmr_…"]`）里的双引号属于取值本体，必须原样保留，故只认单引号包裹。
    """
    if not isinstance(text, str) or not text.strip():
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT, "步骤 %d 的命令串不能为空" % index
        )
    tokens = []
    for match in _STEP_TOKEN_PATTERN.finditer(text):
        tokens.append(match.group(1) if match.group(1) is not None else match.group(2))
    return tokens


def _collect_binds(business, index, frozen_steps, *, spec=None):
    binds = []
    for path, placeholder in _walk_placeholders(business, ""):
        from_step, field = placeholder
        if from_step >= index:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT,
                "步骤 %d 只能引用它之前的步骤" % index,
            )
        if from_step > len(frozen_steps):
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT,
                "步骤 %d 引用了不存在的步骤 %d" % (index, from_step),
            )
        bind = {"path": path, "fromStep": from_step, "fromField": field}
        if field == "fileRefs":
            source = frozen_steps[from_step - 1]
            source_args = source.get("args") or {}
            if (
                spec is None or spec.scenario_id not in _ARRAY_TARGET_SCENES
                or source.get("scenarioId") not in _TRANSFER_SCENES
                or path != "fileRefs[0]"
                or len(business.get("fileRefs", ())) != 1
                or ((source.get("plan") or {}).get("resolvedIds") or {}).get("catalogIds")
                or source_args.get("catalogRefs")
                or any(_ref_kind(token) == "catalog" for token in source_args.get("selectedResources", ()))
            ):
                raise result_module.ExecutionError(
                    result_module.INVALID_ARGUMENT,
                    "fileRefs 数组产物仅支持文件转存到建册/加图，且必须独占 fileRefs 数组",
                )
            bind.update(path="fileRefs", mode="array")
        binds.append(bind)
    return binds


def _walk_placeholders(node, path):
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(_walk_placeholders(value, "%s.%s" % (path, key) if path else key))
        return found
    if isinstance(node, list):
        for position, value in enumerate(node):
            found.extend(_walk_placeholders(value, "%s[%d]" % (path, position)))
        return found
    if isinstance(node, str):
        match = _PLACEHOLDER_PATTERN.match(node)
        if match:
            found.append((path, (int(match.group(1)), match.group(2))))
    return found


def _placeholder_kind(field):
    if field.endswith("Refs"):
        return field[:-4].lower()
    if field.endswith("Ref"):
        return field[:-3].lower()
    return field.lower()


def _substitute_for_validation(business, binds):
    """冻结期校验用替身：把占位符换成合法形状的假引用，只验参数结构，不验真实资源。"""
    replaced = copy.deepcopy(business)
    for bind in binds:
        value = "cmr_%s_orchestrationplaceholder" % _placeholder_kind(bind["fromField"])
        _assign(replaced, bind["path"], [value] if bind.get("mode") == "array" else value)
    return replaced


def _materialize(step, outputs):
    """执行期注入：把占位符换成前序步骤真实产出的受控引用。"""
    materialized = copy.deepcopy(step["args"])
    for bind in step["binds"]:
        produced = outputs.get(str(bind["fromStep"])) or {}
        candidates = list(dict.fromkeys(produced.get(bind["fromField"], ())))
        expected_kind = _placeholder_kind(bind["fromField"])
        if bind.get("mode") == "array":
            if (
                step.get("scenarioId") not in _ARRAY_TARGET_SCENES
                or bind["path"] != "fileRefs" or bind["fromField"] != "fileRefs"
                or not candidates or any(_ref_kind(token) != "file" for token in candidates)
            ):
                raise result_module.ExecutionError(
                    result_module.INVALID_ARGUMENT, "前序转存没有完整、有效的成功文件数组，不能继续建册或加图"
                )
            _assign(materialized, bind["path"], candidates)
            continue
        if not candidates:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT,
                "第 %d 步没有产出可用的%s引用，第 %d 步无法继续"
                % (bind["fromStep"], expected_kind, step["index"]),
            )
        same_kind = [token for token in candidates if _ref_kind(token) == expected_kind]
        if len(same_kind) == 1:
            # 引用类型由下游公共前置（x-ref-kind）与场景核验最终判定，这里只做消歧。
            selected = same_kind
        elif not same_kind and len(candidates) == 1:
            selected = candidates
        else:
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT,
                "第 %d 步产出的%s引用不唯一，第 %d 步无法确定目标"
                % (bind["fromStep"], expected_kind, step["index"]),
            )
        _assign(materialized, bind["path"], selected[0])
    return materialized


def _assign(node, path, value):
    target = node
    segments = _path_segments(path)
    for segment in segments[:-1]:
        target = target[segment]
    target[segments[-1]] = value


def _path_segments(path):
    segments = []
    for segment in path.split("."):
        name, _, remainder = segment.partition("[")
        if name:
            segments.append(name)
        while remainder:
            index, _, remainder = remainder.partition("]")
            remainder = remainder.lstrip("[")
            segments.append(int(index))
    return segments


def _validate_bound_images(runtime, step, input_data):
    """仅检查新数组绑定；旧单值计划及单命令保持原行为。"""
    if not any(bind.get("mode") == "array" for bind in step["binds"]):
        return
    unique_refs, seen_ids = [], set()
    for token in input_data["fileRefs"]:
        file_id = runtime.real_id(token, "file")
        if file_id in seen_ids:
            continue
        node = workflow_common.ensure_file(runtime, file_id)
        if node.get("fileId") != file_id:
            workflow_common.contract_mismatch(runtime, "转存图片详情与请求的文件不一致")
        category = node.get("category")
        if isinstance(category, bool) or category not in ("image", "1", 1):
            raise result_module.ExecutionError(
                result_module.INVALID_ARGUMENT, "转存产物包含非图片或类型未确认的文件，已停止建册或加图"
            )
        seen_ids.add(file_id)
        unique_refs.append(token)
    input_data["fileRefs"] = unique_refs


def _collect_outputs(envelope, scenario_id=None):
    """从步骤信封的业务 data 里收集受控引用，按字段名归类，供后续步骤绑定。"""
    found = {}

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and _ref_kind(value):
                    found.setdefault(key, []).append(value)
                else:
                    walk(value)
            return
        if isinstance(node, list):
            for item in node:
                walk(item)

    walk(envelope.to_dict().get("data") if hasattr(envelope, "to_dict") else None)
    if scenario_id in _TRANSFER_SCENES:
        found.pop("fileRefs", None)
        payload = envelope.to_dict()
        rows = (payload.get("data") or {}).get("results")
        counts = payload.get("counts") or {}
        if (
            payload.get("status") == result_module.STATUS_SUCCESS
            and isinstance(rows, list) and rows
            and all(isinstance(row, dict) and row.get("kind") == "content"
                    and row.get("status") == result_module.STATUS_SUCCESS
                    and _ref_kind(row.get("resultRef", "")) == "file" for row in rows)
            and counts.get("requested", len(rows)) == len(rows)
            and counts.get("succeeded", len(rows)) == len(rows)
            and counts.get("failed", 0) == 0
        ):
            found["fileRefs"] = list(dict.fromkeys(row["resultRef"] for row in rows))
    return found


def _ref_kind(token):
    if not isinstance(token, str):
        return None
    match = _REF_TOKEN_PATTERN.match(token)
    if not match:
        return None
    return match.group(1)
