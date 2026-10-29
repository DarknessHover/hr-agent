"""HR 端招聘 Agent 原型应用包。

分层：
    cli      —— 标准化项目级命令路由
    logging  —— 统一日志、证据等级，按状态机写入 log/<flow>/
    device   —— 设备能力层（adbd 封装，固定 Redmi 14C）
    resume   —— 页面识别、简历字段抽取、多屏拼接
    resume_collector —— 两部状态机共用的完整简历采集流程
    reply_ui —— 2号状态机页面识别与未读卡片绑定
    reply_store —— 2号状态机专属会话、消息与动作仓储
    reply_main —— 2号状态机控制入口与常用语发送编排
    review   —— Agent 审核输出预算、评语非空校验与长度建议
    store    —— 1号兼容 candidate 落盘
    main     —— 有限循环状态机与 CLI
"""
