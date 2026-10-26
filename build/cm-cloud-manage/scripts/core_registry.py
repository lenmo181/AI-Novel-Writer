"""唯一命令注册表：S01～S36（S17 已移除）的不可变 SceneSpec、命令路由与唯一性断言。

约定：
- 注册表恰好 35 个场景（S01～S36 中 S17 已移除）；scenario_id 唯一，command_id 唯一由路由保证。
- 一个公开命令路由（domain+action）可以承载多个场景（共享主命令），由判别字段
  （saveKind / listKind / kind / fileRefs 存在性）确定性选择，绝不依赖注册顺序。
- 进程启动时完成注册断言；任一重复、缺失、取值非法、选择器重叠时直接启动失败，不做
  “最后注册覆盖前者”。workflow 模块改为按场景懒加载：首次使用时导入并校验入口，
  冷启动不再全量导入全部模块。
- capability 初始状态保守：只有同时具备正式成功码、响应字段、范围与真实脱敏样本的
  场景才为 ENABLED；契约存在缺口的一律 DISABLED，且 disabled_reason 必须非空。
  命令仍可通过 capabilities/schema/help 被发现。
"""

import ast
import importlib
import os
from dataclasses import dataclass
from types import MappingProxyType

from cmcloud_validation import command_schemas

OPERATION_READ = "READ"
OPERATION_WRITE = "WRITE"
OPERATION_LOCAL_WRITE = "LOCAL_WRITE"
OPERATION_STATUS = "STATUS"
OPERATION_TYPES = (OPERATION_READ, OPERATION_WRITE, OPERATION_LOCAL_WRITE, OPERATION_STATUS)

CONFIRMATION_NONE = "NONE"
CONFIRMATION_TRUSTED_PLAN = "TRUSTED_PLAN"
CONFIRMATION_LOCAL_OVERWRITE = "LOCAL_OVERWRITE"
CONFIRMATION_VALUES = (CONFIRMATION_NONE, CONFIRMATION_TRUSTED_PLAN, CONFIRMATION_LOCAL_OVERWRITE)

CAPABILITY_ENABLED = "ENABLED"
CAPABILITY_DISABLED = "DISABLED"
CAPABILITY_VALUES = (CAPABILITY_ENABLED, CAPABILITY_DISABLED)

PRESENCE_ANY = "any"
PRESENCE_PRESENT = "present"
PRESENCE_ABSENT = "absent"
PRESENCE_VALUES = (PRESENCE_ANY, PRESENCE_PRESENT, PRESENCE_ABSENT)

LOGICAL_SCOPES = (
    "agent_identity",
    "dedicated_root",
    "authorized_personal_cloud",
    "outer_link",
    "share_record",
    "album_story",
    "activity",
    "local_filesystem",
    "operation_ledger",
)

RESOURCE_POLICIES = (
    "agent_identity_scope",
    "dedicated_root_only",
    "dedicated_root_descendants",
    "authorized_sources_into_dedicated_root",
    "authorized_sources_out_of_scope",
    "authorized_personal_cloud_read",
    "authorized_personal_cloud_write",
    "outer_link_assets",
    "share_records_scope",
    "album_owner_scope",
    "activity_scope",
    "local_filesystem_scope",
    "operation_ledger_scope",
)

SCENARIO_COUNT = 35

DOMAIN_HELP = MappingProxyType(
    {
        "agent": "智能体：实例名称与当前可操作专属根目录查询",
        "files": "文件：上传、搜索、列表、建目录、下载、复制、详情",
        "activity": "动态与阅读：听过动态、上传动态、转存动态、阅读记录、月度统计",
        "share": "分享与外链：创建公开外链、订阅、查询更新、转存、查询、浏览",
        "album": "相册与故事：搜索、列表、图片明细、创建、加图、更新、生成链接",
        "operation": "操作控制面：计划确认、执行与统一任务状态查询",
    }
)


@dataclass(frozen=True)
class Selector:
    """共享主命令的判别条件：字段等值约束与字段存在性约束。"""

    field: str
    value: str | None = None
    presence: str = PRESENCE_ANY

    def __post_init__(self):
        if self.presence not in PRESENCE_VALUES:
            raise RuntimeError("Selector presence 非法: %s" % self.presence)
        if self.value is not None and self.presence != PRESENCE_ANY:
            raise RuntimeError("Selector 不允许同时声明等值与存在性约束")
        if self.value is None and self.presence == PRESENCE_ANY:
            raise RuntimeError("Selector 必须至少声明一种约束")

    def matches(self, input_data):
        present = self.field in input_data
        if self.presence == PRESENCE_PRESENT and not present:
            return False
        if self.presence == PRESENCE_ABSENT and present:
            return False
        if self.value is not None:
            return present and input_data.get(self.field) == self.value
        return True

    def describe(self):
        if self.value is not None:
            return {"field": self.field, "equals": self.value}
        return {"field": self.field, "presence": self.presence}


@dataclass(frozen=True)
class SceneSpec:
    scenario_id: str
    command_id: str
    domain: str
    action: str
    operation_type: str
    schema_id: str
    workflow: str
    logical_scopes: tuple
    resource_policy: str
    confirmation: str
    capability_state: str
    disabled_reason: str
    selector: tuple = ()

    @property
    def workflow_module_name(self):
        return self.workflow.rsplit(".", 1)[0]

    @property
    def workflow_entry_name(self):
        return self.workflow.rsplit(".", 1)[1]

    @property
    def entry_point(self):
        return self.workflow_entry_name

    @property
    def is_read_like(self):
        return self.operation_type in (OPERATION_READ, OPERATION_STATUS)

    @property
    def is_write_like(self):
        return self.operation_type in (OPERATION_WRITE, OPERATION_LOCAL_WRITE)

    @property
    def requires_confirmation(self):
        """是否必须用户确认：只看 confirmation，不看是否写类。

        `LOCAL_WRITE`（如下载）只改本机文件，可以登记计划但不要求确认；
        `WRITE`（云端写）始终为 `TRUSTED_PLAN`，由 `_validate_spec` 强制。
        """
        return self.confirmation in (CONFIRMATION_TRUSTED_PLAN, CONFIRMATION_LOCAL_OVERWRITE)

    @property
    def enabled(self):
        return self.capability_state == CAPABILITY_ENABLED

    @property
    def command(self):
        return "%s %s" % (self.domain, self.action)

    def matches(self, input_data):
        return all(selector.matches(input_data) for selector in self.selector)

    def describe(self):
        schema = command_schemas.SCENARIO_SCHEMAS[self.scenario_id]
        required = schema.get("required") if isinstance(schema, dict) else None
        return {
            "scenarioId": self.scenario_id,
            "commandId": self.command_id,
            "command": self.command,
            "domain": self.domain,
            "action": self.action,
            "operationType": self.operation_type,
            "schemaId": self.schema_id,
            "workflow": self.workflow,
            "logicalScopes": list(self.logical_scopes),
            "resourcePolicy": self.resource_policy,
            "confirmation": self.confirmation,
            "requiresConfirmation": self.requires_confirmation,
            "capability": self.capability_state,
            "disabledReason": self.disabled_reason,
            "selector": [selector.describe() for selector in self.selector],
            "requiredFields": list(required) if isinstance(required, list) else [],
        }


@dataclass(frozen=True)
class CommandRoute:
    """公开 CLI 路由：一个 domain+action 唯一对应一个 command-id 与一组互斥场景。"""

    command_id: str
    domain: str
    action: str
    scenes: tuple

    @property
    def command(self):
        return "%s %s" % (self.domain, self.action)

    @property
    def selector_fields(self):
        fields = []
        for scene in self.scenes:
            for selector in scene.selector:
                if selector.field not in fields:
                    fields.append(selector.field)
        return tuple(fields)

    @property
    def shared(self):
        return len(self.scenes) > 1

    @property
    def scenario_ids(self):
        return tuple(scene.scenario_id for scene in self.scenes)

    def describe(self):
        return {
            "commandId": self.command_id,
            "command": self.command,
            "domain": self.domain,
            "action": self.action,
            "shared": self.shared,
            "selectorFields": list(self.selector_fields),
            "scenarioIds": list(self.scenario_ids),
        }


_ENABLED = CAPABILITY_ENABLED
_DISABLED = CAPABILITY_DISABLED
_NONE = CONFIRMATION_NONE
_PLAN = CONFIRMATION_TRUSTED_PLAN

_EQ = lambda field, value: Selector(field, value=value)  # noqa: E731
_HAS = lambda field: Selector(field, presence=PRESENCE_PRESENT)  # noqa: E731
_MISSING = lambda field: Selector(field, presence=PRESENCE_ABSENT)  # noqa: E731

# (scenario_id, command_id, domain, action, operation_type, logical_scopes,
#  resource_policy, confirmation, capability_state, disabled_reason, selector)
_SPEC_ROWS = (
    ("S01", "files.upload", "files", "upload", OPERATION_WRITE,
     ("authorized_personal_cloud", "local_filesystem"), "authorized_personal_cloud_write", _PLAN, _ENABLED, "", ()),
    ("S02", "files.search", "files", "search", OPERATION_READ,
     ("authorized_personal_cloud",), "authorized_personal_cloud_read", _NONE, _ENABLED, "", ()),
    ("S03", "files.list", "files", "list", OPERATION_READ,
     ("authorized_personal_cloud",), "authorized_personal_cloud_read", _NONE, _ENABLED, "", ()),
    ("S04", "files.mkdir", "files", "mkdir", OPERATION_WRITE,
     ("authorized_personal_cloud",), "authorized_personal_cloud_write", _PLAN, _ENABLED, "", ()),
    ("S05", "files.download", "files", "download", OPERATION_LOCAL_WRITE,
     ("authorized_personal_cloud", "local_filesystem"),
     "authorized_sources_out_of_scope", CONFIRMATION_LOCAL_OVERWRITE, _ENABLED, "", ()),
    ("S06", "files.copy", "files", "copy", OPERATION_WRITE,
     ("authorized_personal_cloud",), "authorized_personal_cloud_write",
     _PLAN, _ENABLED, "", ()),
    ("S07", "activity.list", "activity", "list", OPERATION_READ,
     ("activity",), "activity_scope", _NONE, _ENABLED, "",
     (_EQ("eventKinds", command_schemas.EVENT_KINDS_BROWSE),)),
    ("S08", "activity.list", "activity", "list", OPERATION_READ,
     ("activity",), "activity_scope", _NONE, _ENABLED, "",
     (_EQ("eventKinds", command_schemas.EVENT_KINDS_UPLOAD),)),
    ("S09", "activity.list", "activity", "list", OPERATION_READ,
     ("activity",), "activity_scope", _NONE, _ENABLED, "",
     (_EQ("eventKinds", command_schemas.EVENT_KINDS_SHARE_SAVE),)),
    ("S10", "activity.list", "activity", "list", OPERATION_READ,
     ("activity",), "activity_scope", _NONE, _ENABLED, "",
     (_EQ("eventKinds", command_schemas.EVENT_KINDS_GROUP_SAVE),)),
    ("S11", "activity.list", "activity", "list", OPERATION_READ,
     ("activity",), "activity_scope", _NONE, _ENABLED, "",
     (_EQ("eventKinds", command_schemas.EVENT_KINDS_DISCOVER_SAVE),)),
    ("S12", "activity.list", "activity", "list", OPERATION_READ,
     ("activity",), "activity_scope", _NONE, _ENABLED, "",
     (_EQ("eventKinds", command_schemas.EVENT_KINDS_ALL_SAVE),)),
    ("S13", "activity.reading", "activity", "reading", OPERATION_READ,
     ("activity",), "activity_scope", _NONE, _ENABLED, "", ()),
    ("S14", "files.info", "files", "info", OPERATION_READ,
     ("authorized_personal_cloud", "activity"), "authorized_personal_cloud_read",
     _NONE, _ENABLED, "", ()),
    ("S15", "activity.stats", "activity", "stats", OPERATION_READ,
     ("activity",), "activity_scope", _NONE, _ENABLED, "", ()),
    ("S16", "share.create", "share", "create", OPERATION_WRITE,
     ("authorized_personal_cloud",), "authorized_personal_cloud_write", _PLAN, _ENABLED, "", ()),
    # ---- 分享与外链 S18～S25 ----
    ("S18", "share.subscribe", "share", "subscribe", OPERATION_WRITE,
     ("outer_link",), "outer_link_assets", _PLAN, _ENABLED, "", ()),
    ("S19", "share.updates", "share", "updates", OPERATION_READ,
     ("outer_link",), "outer_link_assets", _NONE, _ENABLED, "", ()),
    ("S20", "share.save", "share", "save", OPERATION_WRITE,
     ("outer_link", "authorized_personal_cloud"), "outer_link_assets", _PLAN, _ENABLED, "",
     (_EQ("saveKind", command_schemas.SAVE_KIND_SUBSCRIPTION_UPDATES),)),
    ("S21", "share.save", "share", "save", OPERATION_WRITE,
     ("outer_link", "authorized_personal_cloud"), "outer_link_assets", _PLAN, _ENABLED, "",
     (_EQ("saveKind", command_schemas.SAVE_KIND_SELECTED_RESOURCES),)),
    ("S22", "operation.status", "operation", "status", OPERATION_STATUS,
     ("operation_ledger", "outer_link", "local_filesystem"), "operation_ledger_scope",
     _NONE, _ENABLED, "", ()),
    ("S23", "share.list", "share", "list", OPERATION_READ,
     ("share_record",), "share_records_scope", _NONE, _ENABLED, "",
     (_EQ("listKind", command_schemas.LIST_KIND_SENT),)),
    ("S24", "share.list", "share", "list", OPERATION_READ,
     ("share_record",), "share_records_scope", _NONE, _ENABLED, "",
     (_EQ("listKind", command_schemas.LIST_KIND_SUBSCRIPTIONS),)),
    ("S25", "share.browse", "share", "browse", OPERATION_READ,
     ("outer_link",), "outer_link_assets", _NONE, _ENABLED, "", ()),
    # ---- 相簿与故事 S26～S35 ----
    ("S26", "album.search", "album", "search", OPERATION_READ,
     ("album_story",), "album_owner_scope", _NONE, _ENABLED, "", ()),
    ("S27", "album.list", "album", "list", OPERATION_READ,
     ("album_story",), "album_owner_scope", _NONE, _ENABLED, "", ()),
    ("S28", "album.files", "album", "files", OPERATION_READ,
     ("album_story",), "album_owner_scope", _NONE, _ENABLED, "", ()),
    ("S29", "album.create", "album", "create", OPERATION_WRITE,
     ("album_story",), "album_owner_scope", _PLAN, _ENABLED, "",
     (_EQ("kind", command_schemas.ALBUM_KIND), _MISSING("fileRefs"))),
    ("S30", "album.create", "album", "create", OPERATION_WRITE,
     ("album_story",), "album_owner_scope", _PLAN, _ENABLED, "",
     (_EQ("kind", command_schemas.ALBUM_KIND), _HAS("fileRefs"))),
    ("S31", "album.add", "album", "add", OPERATION_WRITE,
     ("album_story",), "album_owner_scope", _PLAN, _ENABLED, "", ()),
    ("S32", "album.create", "album", "create", OPERATION_WRITE,
     ("album_story",), "album_owner_scope", _PLAN, _ENABLED, "",
     (_EQ("kind", command_schemas.STORY_KIND),)),
    ("S33", "album.update", "album", "update", OPERATION_WRITE,
     ("album_story",), "album_owner_scope", _PLAN, _ENABLED, "",
     (_EQ("kind", command_schemas.ALBUM_KIND),)),
    ("S34", "album.update", "album", "update", OPERATION_WRITE,
     ("album_story",), "album_owner_scope", _PLAN, _ENABLED, "",
     (_EQ("kind", command_schemas.STORY_KIND),)),
    ("S35", "album.share", "album", "share", OPERATION_WRITE,
     ("album_story",), "album_owner_scope", _PLAN, _ENABLED, "",
     (_EQ("deliveryMode", command_schemas.DELIVERY_MODE_LINK_ONLY),)),
    ("S36", "agent.info", "agent", "info", OPERATION_READ,
     ("agent_identity",), "agent_identity_scope", _NONE, _ENABLED, "", ()),
)


def _workflow_module(scenario_id):
    file_name = "s%s" % scenario_id[1:]
    return "workflow_%s_%s" % (file_name, _WORKFLOW_SHORT_NAMES[scenario_id])


_WORKFLOW_SHORT_NAMES = {
    "S01": "files_upload",
    "S02": "files_search",
    "S03": "files_list",
    "S04": "files_mkdir",
    "S05": "files_download",
    "S06": "files_copy",
    "S07": "activity_browse",
    "S08": "activity_uploads",
    "S09": "activity_share_saves",
    "S10": "activity_group_saves",
    "S11": "activity_discover_saves",
    "S12": "activity_all_saves",
    "S13": "activity_reading",
    "S14": "files_info",
    "S15": "activity_monthly_uploads",
    "S16": "share_create",
    "S18": "share_subscribe",
    "S19": "share_updates",
    "S20": "share_save_updates",
    "S21": "share_save_selected",
    "S22": "operation_status",
    "S23": "share_list_sent",
    "S24": "share_list_subscriptions",
    "S25": "share_browse",
    "S26": "album_search",
    "S27": "album_list",
    "S28": "album_files",
    "S29": "album_create",
    "S30": "album_create_with_files",
    "S31": "album_add",
    "S32": "story_create",
    "S33": "album_update",
    "S34": "story_update",
    "S35": "album_share",
    "S36": "agent_info",
}


def _build_spec(row):
    (
        scenario_id,
        command_id,
        domain,
        action,
        operation_type,
        scopes,
        resource_policy,
        confirmation,
        capability,
        disabled_reason,
        selector,
    ) = row
    module_name = _workflow_module(scenario_id)
    entry = "run" if operation_type in (OPERATION_READ, OPERATION_STATUS) else "prepare"
    return SceneSpec(
        scenario_id=scenario_id,
        command_id=command_id,
        domain=domain,
        action=action,
        operation_type=operation_type,
        schema_id=command_schemas.SCHEMA_IDS[scenario_id],
        workflow="%s.%s" % (module_name, entry),
        logical_scopes=tuple(scopes),
        resource_policy=resource_policy,
        confirmation=confirmation,
        capability_state=capability,
        disabled_reason=disabled_reason,
        selector=tuple(selector),
    )


def _validate_spec(spec):
    if spec.operation_type not in OPERATION_TYPES:
        raise RuntimeError("%s operation_type 非法: %s" % (spec.scenario_id, spec.operation_type))
    if spec.confirmation not in CONFIRMATION_VALUES:
        raise RuntimeError("%s confirmation 非法: %s" % (spec.scenario_id, spec.confirmation))
    if spec.capability_state not in CAPABILITY_VALUES:
        raise RuntimeError("%s capability_state 非法: %s" % (spec.scenario_id, spec.capability_state))
    if spec.resource_policy not in RESOURCE_POLICIES:
        raise RuntimeError("%s resource_policy 未登记: %s" % (spec.scenario_id, spec.resource_policy))
    if not spec.logical_scopes:
        raise RuntimeError("%s 必须至少登记一个 logical scope" % spec.scenario_id)
    for scope in spec.logical_scopes:
        if scope not in LOGICAL_SCOPES:
            raise RuntimeError("%s logical scope 未登记: %s" % (spec.scenario_id, scope))
    conditional_download = (spec.scenario_id == "S05" and spec.operation_type == OPERATION_LOCAL_WRITE
                            and spec.confirmation == CONFIRMATION_LOCAL_OVERWRITE)
    if spec.confirmation == CONFIRMATION_LOCAL_OVERWRITE and not conditional_download:
        raise RuntimeError("LOCAL_OVERWRITE 仅适用于 S05 本地下载")
    if spec.is_write_like and spec.confirmation != CONFIRMATION_TRUSTED_PLAN and not conditional_download:
        raise RuntimeError("%s 是写场景，必须使用 TRUSTED_PLAN 确认" % spec.scenario_id)
    if spec.is_read_like and spec.confirmation != CONFIRMATION_NONE:
        raise RuntimeError("%s 是读场景，不应要求确认" % spec.scenario_id)
    if spec.capability_state == CAPABILITY_DISABLED and not spec.disabled_reason:
        raise RuntimeError("%s 关闭时必须给出 disabled_reason" % spec.scenario_id)
    if spec.capability_state == CAPABILITY_ENABLED and spec.disabled_reason:
        raise RuntimeError("%s 已开启时不得保留 disabled_reason" % spec.scenario_id)
    if spec.schema_id != command_schemas.SCHEMA_IDS[spec.scenario_id]:
        raise RuntimeError("%s schema_id 与 command_schemas 不一致" % spec.scenario_id)
    if command_schemas.SCENARIO_COMMAND_IDS.get(spec.scenario_id) != spec.command_id:
        raise RuntimeError("%s command_id 与 command_schemas 路由表不一致" % spec.scenario_id)
    _assert_selector_matches_schema(spec)


def _assert_selector_matches_schema(spec):
    """判别字段必须与该场景 Schema 一致：等值约束须落在 const/enum 且必填；存在性约束须可判定。"""
    schema = command_schemas.SCENARIO_SCHEMAS[spec.scenario_id]
    properties = schema.get("properties") or {}
    required = tuple(schema.get("required") or ())
    for selector in spec.selector:
        declared = selector.field in properties
        if selector.value is None:
            if selector.presence == PRESENCE_PRESENT:
                if not declared or selector.field not in required:
                    raise RuntimeError(
                        "%s 的存在性判别字段必须是 Schema 中已声明的必填字段: %s"
                        % (spec.scenario_id, selector.field)
                    )
            elif selector.presence == PRESENCE_ABSENT:
                # 未声明的字段由 additionalProperties=false 拒绝，等价于“必须不出现”。
                if declared and selector.field in required:
                    raise RuntimeError(
                        "%s 不允许把必填字段声明为“不得出现”: %s" % (spec.scenario_id, selector.field)
                    )
            continue
        if not declared or selector.field not in required:
            raise RuntimeError(
                "%s 的等值判别字段必须是 Schema 中已声明的必填字段: %s" % (spec.scenario_id, selector.field)
            )
        declared_schema = properties[selector.field]
        allowed = None
        if "const" in declared_schema:
            allowed = (declared_schema["const"],)
        elif "enum" in declared_schema:
            allowed = tuple(declared_schema["enum"])
        if allowed is None or selector.value not in allowed:
            raise RuntimeError(
                "%s 的判别字段取值与 Schema 不一致: %s=%s" % (spec.scenario_id, selector.field, selector.value)
            )


def _selector_constraints(selectors):
    constraints = {}
    for selector in selectors:
        if selector.value is not None:
            constraints[selector.field] = ("eq", selector.value)
        else:
            constraints[selector.field] = (selector.presence, None)
    return constraints


def _selectors_can_both_apply(left, right):
    """判断两个场景的选择器是否存在可同时命中的输入。"""
    left_constraints = _selector_constraints(left)
    right_constraints = _selector_constraints(right)
    for field, (kind, value) in left_constraints.items():
        other = right_constraints.get(field)
        if other is None:
            continue
        other_kind, other_value = other
        if kind == "eq" and other_kind == "eq":
            if value != other_value:
                return False
            continue
        if kind == other_kind:
            continue
        if {kind, other_kind} & {"present", "absent"} == {"present", "absent"}:
            return False
        # eq 与 present/absent 冲突
        return False
    return True


def _assert_route_selector_integrity(scenes):
    if len(scenes) == 1:
        return
    for index, left in enumerate(scenes):
        for right in scenes[index + 1:]:
            if _selectors_can_both_apply(left.selector, right.selector):
                raise RuntimeError(
                    "共享主命令的选择器重叠，无法确定性选择场景: %s / %s"
                    % (left.scenario_id, right.scenario_id)
                )


def _assert_integrity(specs):
    if len(specs) != SCENARIO_COUNT:
        raise RuntimeError("命令注册表必须恰好 %d 条，当前 %d 条" % (SCENARIO_COUNT, len(specs)))
    seen_scenarios = set()
    expected_scenarios = set(command_schemas.SCENARIO_SCHEMAS)
    for spec in specs:
        if spec.scenario_id in seen_scenarios:
            raise RuntimeError("scenario_id 重复: %s" % spec.scenario_id)
        seen_scenarios.add(spec.scenario_id)
    missing = expected_scenarios - seen_scenarios
    if missing:
        raise RuntimeError("缺少场景注册: %s" % ", ".join(sorted(missing)))
    extra = seen_scenarios - expected_scenarios
    if extra:
        raise RuntimeError("注册了未定义场景: %s" % ", ".join(sorted(extra)))


def _load_workflow(spec):
    try:
        module = importlib.import_module(spec.workflow_module_name)
    except ImportError as err:
        raise RuntimeError("%s 的 workflow 不可导入: %s" % (spec.scenario_id, err))
    missing = []
    if spec.is_read_like and not hasattr(module, "run"):
        missing.append("run")
    if spec.is_write_like:
        for name in ("prepare", "execute"):
            if not hasattr(module, name):
                missing.append(name)
    if missing:
        raise RuntimeError("%s 的 workflow 缺少入口: %s" % (spec.scenario_id, ", ".join(missing)))
    return module


SCENES = tuple(_build_spec(row) for row in _SPEC_ROWS)

for _spec in SCENES:
    _validate_spec(_spec)
_assert_integrity(SCENES)

_BY_SCENARIO = {scene.scenario_id: scene for scene in SCENES}

# 写动作编排：把同一用户请求里的多个云端写命令冻结为**一个**计划、一次确认。
# 它不是业务场景（不进 SCENES、不计入 SCENARIO_COUNT），只是台账与状态机需要一个
# 稳定的 scene/command 标识，故单独登记一份规格；步骤仍各自走原场景的 prepare/execute。
ORCHESTRATION_SCENARIO_ID = "ORCH"
ORCHESTRATION_COMMAND_ID = "orchestrate"
ORCHESTRATION_SCENE = SceneSpec(
    scenario_id=ORCHESTRATION_SCENARIO_ID,
    command_id=ORCHESTRATION_COMMAND_ID,
    domain="orchestrate",
    action="orchestrate",
    operation_type=OPERATION_WRITE,
    schema_id="orchestrate",
    workflow="core_orchestrator.execute_frozen",
    logical_scopes=(),
    resource_policy="authorized_personal_cloud_write",
    confirmation=CONFIRMATION_TRUSTED_PLAN,
    capability_state=CAPABILITY_ENABLED,
    disabled_reason="",
)

# 按注册顺序构造路由（保持稳定输出顺序），并断言 domain+action 与 command-id 一一对应。
_ROUTE_ORDER = []
_GROUPED = {}
for _scene in SCENES:
    key = (_scene.domain, _scene.action)
    if key not in _GROUPED:
        _GROUPED[key] = []
        _ROUTE_ORDER.append(key)
    _GROUPED[key].append(_scene)

ROUTES = tuple(
    CommandRoute(
        command_id=_GROUPED[key][0].command_id,
        domain=key[0],
        action=key[1],
        scenes=tuple(_GROUPED[key]),
    )
    for key in _ROUTE_ORDER
)

for _route in ROUTES:
    expected = command_schemas.COMMAND_SCENARIOS.get(_route.command_id)
    if expected is None:
        raise RuntimeError("路由 %s 的 command-id 未在 command_schemas 登记: %s" % (_route.command, _route.command_id))
    if tuple(sorted(expected)) != tuple(sorted(_route.scenario_ids)):
        raise RuntimeError(
            "路由 %s 的场景集合与 command_schemas 不一致: %s vs %s"
            % (_route.command, _route.scenario_ids, expected)
        )
    _assert_route_selector_integrity(_route.scenes)

if len({route.command_id for route in ROUTES}) != len(ROUTES):
    raise RuntimeError("command-id 与 domain+action 路由不是一一对应")

_BY_ROUTE = {(route.domain, route.action): route for route in ROUTES}
_BY_COMMAND_ID = {route.command_id: route for route in ROUTES}

if set(command_schemas.COMMAND_SCENARIOS) != set(_BY_COMMAND_ID):
    raise RuntimeError("command_schemas 的 command 集合与注册表路由不一致")

# workflow 模块按场景懒加载（首次使用时导入并校验入口），冷启动不再全量导入全部模块。
_WORKFLOW_MODULES = {}

# 兼容旧调用点的显式别名：注册表对外只有一个命令清单。
COMMANDS = SCENES


def all_commands():
    """全部 35 个场景（按 scenario_id 升序注册顺序）。"""
    return SCENES


def all_routes():
    return ROUTES


def get_route(domain, action):
    """按 domain/action 查唯一路由；未注册返回 None。"""
    return _BY_ROUTE.get((domain, action))


# 旧名保留：语义等同 get_route（返回的是路由，不再直接是单场景）。
def get_command(domain, action):
    return get_route(domain, action)


def get_route_by_id(command_id):
    route = _BY_COMMAND_ID.get(command_id)
    if route is None:
        raise KeyError("未注册的 command-id: %s" % command_id)
    return route


def get_scene(scenario_id):
    if scenario_id == ORCHESTRATION_SCENARIO_ID:
        return ORCHESTRATION_SCENE
    scene = _BY_SCENARIO.get(scenario_id)
    if scene is None:
        raise KeyError("未注册的 scenario-id: %s" % scenario_id)
    return scene


def scenes_for_command(command_id):
    return get_route_by_id(command_id).scenes


def select_scene(route, input_data):
    """确定性选择场景：恰好命中一个才返回，否则抛 INVALID_ARGUMENT。"""
    from cmcloud_core import result as result_module

    payload = input_data if isinstance(input_data, dict) else {}
    matched = [scene for scene in route.scenes if scene.matches(payload)]
    if len(matched) == 1:
        return matched[0]
    if not matched:
        fields = "、".join(route.selector_fields) or "（无判别字段）"
        raise result_module.ExecutionError(
            result_module.INVALID_ARGUMENT,
            "命令 %s 需要可判别的输入才能确定场景，请检查判别字段: %s" % (route.command, fields),
        )
    raise RuntimeError(
        "命令 %s 的选择器命中多个场景: %s" % (route.command, ", ".join(scene.scenario_id for scene in matched))
    )


def get_scene_by_command_id(command_id, input_data=None):
    """单场景命令直接返回；共享主命令必须给出可判别的输入。"""
    route = get_route_by_id(command_id)
    if len(route.scenes) == 1:
        return route.scenes[0]
    if input_data is None:
        raise KeyError("command-id %s 承载多个场景，必须提供判别输入" % command_id)
    return select_scene(route, input_data)


# 旧名保留：单场景命令可用，共享主命令请改用 get_scene_by_command_id。
def get_command_by_id(command_id):
    route = get_route_by_id(command_id)
    if len(route.scenes) != 1:
        raise KeyError("command-id %s 承载多个场景，请使用 scenes_for_command/select_scene" % command_id)
    return route.scenes[0]


def get_command_by_scenario(scenario_id):
    return get_scene(scenario_id)


def commands_for_domain(domain):
    return tuple(route for route in ROUTES if route.domain == domain)


def scenes_for_domain(domain):
    return tuple(scene for scene in SCENES if scene.domain == domain)


def domains():
    seen = []
    for route in ROUTES:
        if route.domain not in seen:
            seen.append(route.domain)
    return tuple(seen)


def workflow_module(spec):
    module = _WORKFLOW_MODULES.get(spec.scenario_id)
    if module is None:
        module = _load_workflow(spec)
        _WORKFLOW_MODULES[spec.scenario_id] = module
    return module


def workflow_callable(spec, entry_name):
    module = workflow_module(spec)
    entry = getattr(module, entry_name, None)
    if entry is None:
        raise RuntimeError("%s 未导出入口 %s" % (spec.scenario_id, entry_name))
    if not callable(entry):
        raise RuntimeError("%s 的入口 %s 不可调用" % (spec.scenario_id, entry_name))
    return entry


_ASYNC_SUPPORT_CACHE = {}


def supports_async(spec):
    """场景是否支持异步状态查询。

    静态判定优先：只解析 workflow 源文件的顶层定义，不为查询单个属性导入全部
    workflow 模块（全量导入实测约 2.5s，占 capabilities 命令耗时的大头）。
    静态分析无法确定（源文件缺失/语法错误/动态赋值）时回退导入判定，保持语义一致。
    """
    cached = _ASYNC_SUPPORT_CACHE.get(spec.scenario_id)
    if cached is not None:
        return cached
    static = _static_defines_query_status(_workflow_module(spec.scenario_id))
    if static is None:
        module = workflow_module(spec)
        static = callable(getattr(module, "query_status", None))
    _ASYNC_SUPPORT_CACHE[spec.scenario_id] = static
    return static


def _static_defines_query_status(module_name):
    """源文件静态判定 query_status 是否存在；无法确定返回 None（回退导入判定）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), module_name + ".py")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
    except OSError:
        return None
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "query_status":
            return True
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "query_status" for target in node.targets
        ):
            return None
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "query_status"
        ):
            return None
    return False


def capability_table():
    return [scene.describe() for scene in SCENES]
