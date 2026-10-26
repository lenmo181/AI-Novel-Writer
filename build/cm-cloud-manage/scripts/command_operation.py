"""operation 领域命令登记（V1.8）。

operation 控制面同时承载写场景的确认/执行与 S22 统一任务状态查询：
- `operation confirm`：接受宿主签名事件、人工 TTY，或发布配置开放的计划绑定对话确认（控制面，非业务场景）
- `operation execute`：校验并消费一次性 confirm-token 后执行冻结计划（控制面，非业务场景）
- `operation status` ：S22 业务场景，从台账恢复并查询既有任务，不接受 Agent 传下游 taskId
"""

DOMAIN = "operation"

ACTIONS = {
    "confirm": {"scenarios": (), "summary": "按 operationRef 确认冻结计划（支持宿主签名、人工 TTY 或已开放的对话确认）"},
    "execute": {"scenarios": (), "summary": "校验并消费一次性确认凭据后执行冻结计划"},
    "status": {"scenarios": ("S22",), "summary": "按 operationRef 查询既有任务状态；未知状态只输出 UNKNOWN"},
}
