"""files 领域命令登记（V1.8，S01～S14 区间未调整）。

只声明本领域下的 action、承载场景与帮助摘要；不复制 Schema、不调用 adapter、不保存业务状态。
"""

DOMAIN = "files"

ACTIONS = {
    "upload": {"scenarios": ("S01",), "summary": "上传本地文件到当前账号可写的个人云目录"},
    "search": {"scenarios": ("S02",), "summary": "按目录、关键词、类型、后缀、时间或大小搜索文件"},
    "list": {"scenarios": ("S03",), "summary": "分页列出指定真实目录的直接子项"},
    "mkdir": {"scenarios": ("S04",), "summary": "在当前账号可写的个人云目录创建文件夹"},
    "download": {"scenarios": ("S05",), "summary": "把允许范围内的云端文件下载到本地目录"},
    "copy": {"scenarios": ("S06",), "summary": "把个人云资源复制到当前账号可写的目录"},
    "info": {"scenarios": ("S14",), "summary": "为动态结果或真实文件补充名称、大小、时间和位置"},
}
