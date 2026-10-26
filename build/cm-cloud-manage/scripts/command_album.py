"""album 领域命令登记（V1.8）。

只声明本领域下的 action、承载场景与帮助摘要；不复制 Schema、不调用 adapter、不保存业务状态。

V1.8 路由：
- `album search`  = S26 搜索相簿/故事
- `album list`    = S27 相簿/故事列表（不接受对象引用）
- `album files`   = S28 相簿/故事图片明细（必须提供对象引用）
- `album create`  = S29 新建相簿 / S30 建册并加图 / S32 新建回忆故事（由 kind 与 fileRefs 判别）
- `album add`     = S31 向已有相簿加图
- `album update`  = S33 修改相簿 / S34 修改故事（由 kind 判别）
- `album share`   = S35 只生成相簿/故事分享链接（deliveryMode=link-only）

"""

DOMAIN = "album"

ACTIONS = {
    "search": {"scenarios": ("S26",), "summary": "按单个关键词搜索相簿或故事"},
    "list": {"scenarios": ("S27",), "summary": "列出相簿或故事对象，不查询图片明细"},
    "files": {"scenarios": ("S28",), "summary": "按对象引用查询相簿或故事的图片明细"},
    "create": {
        "scenarios": ("S29", "S30", "S32"),
        "summary": "创建相簿（kind=album，带 fileRefs 即建册并加图）或创建回忆故事（kind=story）",
    },
    "add": {"scenarios": ("S31",), "summary": "把指定真实图片加入已有相簿"},
    "update": {
        "scenarios": ("S33", "S34"),
        "summary": "修改相簿（kind=album）或修改故事（kind=story）",
    },
    "share": {
        "scenarios": ("S35",),
        "summary": "为相簿或故事生成分享链接；不发送、不报告送达",
    },
}
