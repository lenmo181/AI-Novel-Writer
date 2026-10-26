"""S36 一次实时查询返回实例名称及专属目录，并刷新目录缓存，不列文件。"""

from cmcloud_workflows import common


def run(context, input_data):
    response = context.query_user_folder()
    data = common.payload_data(context, response)
    name = data.get("terminalAgentName")
    if name is not None and not isinstance(name, str):
        common.contract_mismatch(context, "data.terminalAgentName 必须是字符串或 null")
    context.cache_user_folder(data)
    dedicated_root = context.dedicated_root_public()
    # 实例名与展示名分开，避免把本地兜底误认为服务端实例名称。
    if name is None or not name.strip():
        display_name = context.agent_platform_name()
        return common.success(data={
            "terminalAgentName": None,
            "displayName": display_name,
            "nameSource": "unknown" if display_name == "其他" else "platform",
            "nameSourceText": "这是当前智能体平台的名称。",
            "dedicatedRoot": dedicated_root,
        })
    return common.success(data={
        "terminalAgentName": name,
        "displayName": name,
        "nameSource": "service",
        "nameSourceText": "这是查询到的实例名称。",
        "dedicatedRoot": dedicated_root,
    })
