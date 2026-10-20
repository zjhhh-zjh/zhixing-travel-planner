"""用户意图单一入口：全项目"这句话是什么意思"的判定都来自这里。

为什么要收口：改造前这件事散在七处（直查门禁、两处表单快筛、回退守卫、
确认语、酒店接受语、交通改选），各写各的词表。同一个词在一处认、另一处不认
（"高铁"在交通清单剔除里有、在直查门禁里没有），不是巧合，是没有单一词汇源的必然结果。

本模块不做分类模型，就是一张词表加一次扫描——意图类别只有四种
（规划 / 单点查询 / 闲聊 / 调整），难的从来不是分类，是槽位与状态。
真要上模型，也应该在这里换实现，而不是再开第八处词表。
"""
from dataclasses import dataclass, field

# ===== 词表：全项目唯一来源，改词只改这里 =====

# 交通查询对象词。★ 裸词必须收：用户说的是"高铁""飞机"，不是"高铁票""飞机票"
TRAIN_OBJECTS = frozenset({
    "高铁票", "火车票", "车票", "动车票", "城际票", "班次", "车次", "余票", "余座",
    "票价", "出发时刻", "到达时刻", "高铁", "动车", "火车", "列车",
})
FLIGHT_OBJECTS = frozenset({"航班", "机票", "飞机票", "直飞", "班机", "飞机"})
# 天气查询对象词。★ 缺了这张表，"明天福州天气怎么样"会被当成闲聊交给裸模型乱答。
#   ★ 必须收"不含天气二字"的口语问法（多少度/冷不冷/带伞…），否则用户换个说法就漏判；
#     单字"冷/热/雨"一律不收——"冷门景点""热干面""雨季"会被误伤
WEATHER_OBJECTS = frozenset({
    "天气", "气温", "温度", "降雨", "下雨", "下雪", "台风", "空气质量",
    "多少度", "几度", "冷不冷", "热不热", "冷吗", "热吗", "凉快", "降温", "升温",
    "带伞", "伞", "湿度", "风力", "穿衣", "穿什么", "紫外线", "雾霾", "暴雨",
    "雷阵雨", "晴天", "阴天", "多云", "防晒",
})
# "天气之外的第二诉求"信号词：命中才把非天气部分交回模型补答，纯天气问句不加赘述。
# ★ 不能收裸"怎么"——"天气怎么样"是标准问法，收它会误判成有第二诉求
EXTRA_ASK_WORDS = frozenset({
    "适合", "好玩", "值得", "推荐", "要不要", "能不能", "建议",
    "安排", "路线", "门票", "美食", "景点", "必去", "逛街",
    "怎么玩", "怎么走", "怎么去", "怎么安排", "怎么办",
})
QUERY_VERBS = frozenset({"查", "搜", "问", "看看", "有没", "还有", "多少"})
# 出现这些词说明用户要的是"行程规划"而不是单点查询——对象词放宽后靠它挡回去
PLANNING_NOISE = frozenset({
    "规划", "行程", "攻略", "预算", "人数", "自由行", "安排", "旅行天数",
    # ★ 裸"玩/游玩/旅游/旅行"必须收：对象词放宽到"高铁"之后，
    #   "我想坐高铁去杭州玩，明天的"会凑齐"对象词 + 日期词"，全靠这几个词挡回规划通道
    "玩", "游玩", "怎么玩", "旅游", "旅行",
})
# 相对日期词与天数偏移（长词在前：匹配"大后天"不能被"后天"截胡）
DAY_OFFSET = (("大后天", 3), ("后天", 2), ("明天", 1), ("明日", 1), ("今天", 0), ("今日", 0))
DATE_WORDS = frozenset(w for w, _ in DAY_OFFSET)

# 自动确认表单（前端模板固定文案，去空白后比对）
AUTO_CONFIRM_WORDS = ("全程交给AI自动确认", "无需逐步询问")

# 纯确认语：整句削掉首尾空白与结尾标点后，恰好等于其中一个词才算
CONFIRM_WORDS = frozenset({
    "满意", "可以", "确认", "确定", "对的", "对", "是的", "是", "嗯",
    "好的", "好", "好嘞", "行", "中", "没问题", "就这样", "就这个", "OK", "ok", "Ok",
})
CONFIRM_TAIL = "。！!…,.，、"

# 回退意图：phrases = 单独出现即算；pairs = (左边任一) 与 (右边任一) 同时出现才算
BACK_INTENT = {
    "destination": {
        "phrases": ("重新选", "重选", "选别的", "选另一个", "选其他",
                    "别的地方", "不想去", "选错了", "换一下"),
        "pairs": ((("换", "改"), ("目的地", "城市", "地方")), (("不去",), ("了",))),
    },
    "transport": {
        "phrases": ("重新选", "不坐", "不乘", "换一个", "换一下"),
        "pairs": ((("换", "改", "坐", "乘", "还是"),
                   ("交通", "高铁", "动车", "火车", "飞机", "航班", "自驾", "驾车", "车")),),
    },
    "requirement": {
        "phrases": ("重新规划", "从头", "重新来", "重新开始", "全部重来", "重新选"),
        "pairs": (),
    },
}

# 住宿降级轮的"委托语"：用户把选择权交回给系统
HOTEL_ACCEPT_WORDS = frozenset({
    "按推荐来", "按你的推荐", "你决定", "你看着办", "听你的",
    "都可以", "都行", "随便", "随便选", "无所谓",
})


def _hit(text: str, words) -> bool:
    """这句话里出现过词表里的任意一个词"""
    return any(w in text for w in words)


@dataclass(frozen=True)
class UserIntent:
    """一句话的意图快照。一次扫描算完，下游读字段，不再各自 search"""

    text: str
    auto_confirm: bool = False          # 表单勾了"全程交给 AI 自动确认"
    is_confirm_reply: bool = False      # 整句是一句纯确认语
    train_object: bool = False          # 提到了车票/车次类对象
    flight_object: bool = False         # 提到了航班类对象
    weather_object: bool = False        # 提到了天气类对象
    extra_ask: bool = False             # 天气问句里还夹着"天气之外"的诉求
    query_verb: bool = False
    date_word: bool = False
    planning_noise: bool = False        # 像"规划行程"而不是单点查询
    hotel_accept: bool = False          # 住宿轮的委托语
    back: dict = field(default_factory=dict)   # key -> 是否表达了回退意图

    @property
    def transport_object(self) -> bool:
        """提到任何交通查询对象（车票或航班）"""
        return self.train_object or self.flight_object

    @property
    def looks_like_query(self) -> bool:
        """像"单点班次查询"：对象词 +（动词 或 日期词），且不含规划噪声

        ★ 这就是直查通道的门禁①②，现在只有这一处定义。
          放宽对象词的风险由 planning_noise 接住："明天坐高铁去上海旅游"不会被抢走
        """
        return (self.transport_object
                and (self.query_verb or self.date_word)
                and not self.planning_noise)

    def wants_back(self, key: str) -> bool:
        """用户这句话有没有表达『换 / 改 / 重新选』的意图"""
        return bool(self.back.get(key))


def detect_intent(text: str) -> UserIntent:
    """一次扫描产出意图快照。纯字符串判定，0ms，不调任何模型"""
    raw = str(text or "")
    flat = "".join(raw.split())          # 去掉全部空白，供表单快筛用
    core = raw.strip().rstrip(CONFIRM_TAIL).rstrip()
    back = {}
    for key, cfg in BACK_INTENT.items():
        # ★ 独立短语命中即算；否则要"动作词 × 对象词"共现
        back[key] = _hit(raw, cfg["phrases"]) or any(
            _hit(raw, left) and _hit(raw, right) for left, right in cfg["pairs"]
        )
    return UserIntent(
        text=raw,
        auto_confirm=_hit(flat, AUTO_CONFIRM_WORDS),
        is_confirm_reply=core in CONFIRM_WORDS,
        train_object=_hit(raw, TRAIN_OBJECTS),
        flight_object=_hit(raw, FLIGHT_OBJECTS),
        query_verb=_hit(raw, QUERY_VERBS),
        weather_object=_hit(raw, WEATHER_OBJECTS),
        extra_ask=_hit(raw, EXTRA_ASK_WORDS),
        date_word=_hit(raw, DATE_WORDS),
        planning_noise=_hit(raw, PLANNING_NOISE),
        hotel_accept=_hit(raw, HOTEL_ACCEPT_WORDS),
        back=back,
    )