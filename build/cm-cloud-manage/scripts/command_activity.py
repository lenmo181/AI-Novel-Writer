"""activity 领域命令登记（V1.8）。

只声明本领域下的 action、承载场景与帮助摘要；不复制 Schema、不调用 adapter、不保存业务状态。

V1.8 路由：
- `activity list`    = S07 浏览/播放动态 / S08 上传动态 / S09 分享转存动态 /
                       S10 圈子等来源转存动态 / S11 发现转存动态 / S12 不限来源转存动态
                       （由必填判别字段 eventKinds 唯一选择）
- `activity reading` = S13 阅读/听书记录
- `activity stats`   = S15 月度上传事件观察统计
"""

DOMAIN = "activity"

ACTIONS = {
    "list": {
        "scenarios": ("S07", "S08", "S09", "S10", "S11", "S12"),
        "summary": "查询动态记录（eventKinds 判别浏览/上传/分享转存/群体转存/发现转存/不限来源转存）",
    },
    "reading": {"scenarios": ("S13",), "summary": "返回平台最近 6 个月内最多 30 条阅读或听书记录"},
    "stats": {"scenarios": ("S15",), "summary": "按上游时间月份汇总观察到的上传事件；不筛图片类型或按指定时区转换"},
}
