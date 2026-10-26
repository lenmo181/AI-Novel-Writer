"""share 领域命令登记（V1.8）。

只声明本领域下的 action、承载场景与帮助摘要；不复制 Schema、不调用 adapter、不保存业务状态。

V1.8 路由：
- `share subscribe`  = S18 订阅外链
- `share updates`    = S19 查询外链更新数量
- `share save`       = S20 转存订阅更新 / S21 转存选定外链资源（由 saveKind 判别）
- `share list`       = S23 查询订阅外链分享记录 / S24 查询订阅外链（由 listKind 判别）
- `share browse`     = S25 浏览外链

"""

DOMAIN = "share"

ACTIONS = {
    "create": {"scenarios": ("S16",), "summary": "为指定文件或目录创建公开外链"},
    "subscribe": {"scenarios": ("S18",), "summary": "订阅用户提供或已解析的他人分享链接"},
    "updates": {"scenarios": ("S19",), "summary": "查询某个已订阅外链的真实更新回执"},
    "save": {
        "scenarios": ("S20", "S21"),
        "summary": "转存外链资源到目标目录（saveKind 判别订阅更新/选定资源）",
    },
    "list": {
        "scenarios": ("S23", "S24"),
        "summary": "查询订阅外链分享记录或订阅外链；sent 不覆盖本人发出的全部公开链接",
    },
    "browse": {"scenarios": ("S25",), "summary": "浏览外链根目录或真实子目录中的文件和目录"},
}
