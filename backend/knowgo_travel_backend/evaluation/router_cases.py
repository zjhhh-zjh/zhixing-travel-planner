"""意图路由评测数据集

每条用例：(消息, 最近对话历史, 期望 planning, 期望命中的技能任一, 用例说明)
来源：把开发期踩过的路由 bug 沉淀为回归用例（对照 app/agents/routers/chat_router.py）：
- 需求收集中的短回答（"杭州"/"2人"）曾被误判闲聊 → history 上下文续接规则（③ 组）
- 纯问候/闲聊误入规划通道 → planning 必须为 False（① 组）
- 明确规划/查询请求 → planning=True 且预选对应技能（② 组）
"""
CASES: list[dict] = [
    # ===== ① 闲聊/问候：planning 必须为 False，skills 必须为空 =====
    {"msg": "你好呀", "history": [], "planning": False, "skills": [], "note": "纯问候"},
    {"msg": "今天天气不错啊", "history": [], "planning": False, "skills": [], "note": "日常闲聊"},
    {"msg": "你会做什么？", "history": [], "planning": False, "skills": [], "note": "询问能力"},
    # ===== ②a 明确规划：planning=True 且预选规划技能 =====
    {"msg": "帮我规划一次杭州3日游，2个大人", "history": [],
     "planning": True, "skills": ["itinerary_planning"], "note": "明确规划请求"},
    # ===== ② 单项查询：技能照常执行，但不进入规划流程（实测路由器语义：planning=False）=====
    {"msg": "查一下明天杭州的天气", "history": [],
     "planning": False, "skills": ["weather_query"], "note": "天气单项查询"},
    {"msg": "帮我查上海到杭州的高铁票", "history": [],
     "planning": False, "skills": ["train_query"], "note": "高铁单项查询"},
    {"msg": "杭州有哪些酒店推荐", "history": [],
     "planning": False, "skills": ["hotel_query"], "note": "酒店单项查询"},
    # ===== ③ 需求收集中的回答（历史 bug 高发区）：有 history 续接必须 planning=True =====
    {"msg": "杭州", "history": ["助手：请问您想去哪里旅游？"],
     "planning": True, "skills": [], "note": "答目的地（曾误判闲聊）"},
    {"msg": "2个大人1个小孩", "history": ["助手：请问几个人出行？"],
     "planning": True, "skills": [], "note": "答人数"},
    {"msg": "预算3000左右", "history": ["助手：请问您的预算是多少？"],
     "planning": True, "skills": [], "note": "答预算"},
    {"msg": "高铁吧", "history": ["助手：您想坐什么交通？", "用户：高铁或自驾犹豫"],
     "planning": True, "skills": [], "note": "选交通方式"},
    {"msg": "特色美食", "history": ["助手：您对哪种餐饮类型感兴趣？"],
     "planning": True, "skills": [], "note": "选餐饮类型"},
    # ===== ④ 表单清单场景：一次性完整需求 → planning=True + 规划技能 =====
    {"msg": "我想去苏州旅行，出发地福州，9月20日出发，3天2人，预算人均3000，坐高铁，喜欢文化探索",
     "history": [], "planning": True, "skills": ["itinerary_planning"], "note": "表单式完整需求"},
]