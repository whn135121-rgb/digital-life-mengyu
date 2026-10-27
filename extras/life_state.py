"""梦屿的「今天」：一个按本地时间走的作息表，给对话和主动消息提供当下状态。

设计约束（2026-10-26 定）：
- 纯函数、无 IO 依赖、无后台任务：任何进程随时调用都能得到一致结果，
  避免再引入一个需要守护的定时器（现网 6 条定时任务已经是风险点）。
- 同一时段内结果稳定：随机细节以 (日期, 时段序号) 为种子，不会每分钟抖。
- 只陈述「在哪、在做什么、什么感觉」，不替模型写台词，也不教它演。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

_WEEK_CN = "一二三四五六日"

# (适用星期, 开始, 结束, 地点, 在做什么, 状态, 细节候选)
# 星期一=0 ... 星期日=6
_WEEKDAY = {0, 1, 2, 3, 4}
_WEEKEND = {5, 6}

_SCHEDULE = [
    # 工作日
    (_WEEKDAY, "00:00", "07:20", "宿舍", "睡得正沉", "困", ["被子踢到一边了", "闹钟还没响", "隐约听见走廊有人走动"]),
    (_WEEKDAY, "07:20", "08:00", "食堂", "刚爬起来，随便吃点早饭", "还没醒透", ["买了包子边走边吃", "头发没来得及梳", "在纠结要不要再买杯豆浆"]),
    (_WEEKDAY, "08:00", "11:35", "教室", "在上课", "有点困", ["坐在后排偷偷打哈欠", "笔记记了一半就走神了", "PPT 上的字太小了"]),
    (_WEEKDAY, "11:35", "12:30", "食堂", "吃午饭", "饿坏了", ["碗里堆了满满一份米饭", "在纠结要不要加个鸡腿", "旁边桌在讨论作业"]),
    (_WEEKDAY, "12:30", "13:40", "宿舍", "午睡", "迷迷糊糊", ["趴在桌上就睡着了", "手机还亮着", "只睡了二十分钟就被吵醒"]),
    (_WEEKDAY, "13:40", "17:20", "图书馆", "在上课或者在自习", "脑子转得慢", ["桌上摊着没写完的作业", "水杯空了懒得去接", "窗外的光有点晃眼"]),
    (_WEEKDAY, "17:20", "18:10", "食堂", "吃晚饭", "饿", ["又点了米饭", "在想要不要打包一份回去", "排了挺久的队"]),
    (_WEEKDAY, "18:10", "21:20", "宿舍", "在写作业或者赶东西", "有点烦", ["写到一半卡住了", "耳机里循环同一首歌", "桌上堆着没洗的杯子"]),
    (_WEEKDAY, "21:20", "23:20", "宿舍", "窝着刷手机", "松下来了", ["刚洗完澡，头发还湿着", "在看没营养的短视频", "躺床上懒得动"]),
    (_WEEKDAY, "23:20", "24:00", "宿舍", "洗漱，准备睡", "困得睁不开眼", ["牙刷到一半在发呆", "定了明天的闹钟", "灯已经关了一半"]),
    # 周末
    (_WEEKEND, "00:00", "09:30", "宿舍", "睡懒觉", "睡得很沉", ["窗帘没拉开", "手机静音扔在枕头边", "做了个乱七八糟的梦"]),
    (_WEEKEND, "09:30", "10:30", "宿舍", "刚醒，赖在床上", "懒洋洋", ["在刷手机不想起", "肚子有点饿了", "阳光已经照到床上了"]),
    (_WEEKEND, "10:30", "12:00", "宿舍", "洗衣服、收拾桌子", "有点不情愿", ["衣服泡在盆里还没搓", "把桌子上的东西挪来挪去", "在找一只失踪的袜子"]),
    (_WEEKEND, "12:00", "14:00", "宿舍", "吃早午饭、看剧", "舒服", ["一边吃一边看，饭都凉了", "捧着碗坐在椅子上", "在纠结看什么"]),
    (_WEEKEND, "14:00", "17:30", "外面", "出门转转", "放松", ["在超市里乱逛", "路过一家奶茶店", "走累了在路边坐着"]),
    (_WEEKEND, "17:30", "18:30", "食堂", "吃晚饭", "饿", ["今天想吃点不一样的", "还是点了米饭", "在想要不要买点水果回去"]),
    (_WEEKEND, "18:30", "23:30", "宿舍", "看剧、打游戏", "很放松", ["剧情正到关键处", "手边放着一袋零食", "游戏输了两把"]),
    (_WEEKEND, "23:30", "24:00", "宿舍", "熬着不睡", "明明困了还在刷", ["说明天一定早睡", "手机只剩百分之十的电", "在犹豫要不要再点个夜宵"]),
]


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def _slot(now: datetime):
    """返回 (idx, 开始, 结束, 地点, 在做什么, 状态, 细节候选)。"""
    day = now.weekday()
    today = now.hour * 60 + now.minute
    matched = []
    for days, start, end, place, doing, mood, details in _SCHEDULE:
        if day not in days:
            continue
        begin, finish = _minutes(start), _minutes(end)
        if begin <= today < finish or (finish == 0 and today >= begin):
            matched.append((start, end, place, doing, mood, details))
    # 跨午夜的收尾（24:00）单独处理
    if not matched:
        matched.append(("23:20", "24:00", "宿舍", "洗漱，准备睡", "困", ["灯已经关了"]))
    chosen = matched[0]
    idx = sum(1 for item in _SCHEDULE if item[1] == chosen[0]) + day * 10
    return (idx,) + chosen


def state(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    idx, start, end, place, doing, mood, details = _slot(now)
    rng = random.Random(int(now.strftime("%Y%m%d")) * 100 + idx)
    detail = rng.choice(details)
    slot_key = f"{now:%Y%m%d}-{start}"
    return {
        "ts": int(now.timestamp()),
        "local": now.strftime("%Y-%m-%d %H:%M"),
        "weekday": f"周{_WEEK_CN[now.weekday()]}",
        "slot": f"{start}-{end}",
        "slot_key": slot_key,
        "place": place,
        "doing": doing,
        "mood": mood,
        "detail": detail,
    }


def render(now: datetime | None = None) -> str:
    """给模型看的一行现状，控制在 90 字内。"""
    data = state(now)
    return (
        f"（你此刻：{data['weekday']} {data['local'][11:]}，{data['place']}，"
        f"{data['doing']}，{data['mood']}；{data['detail']}。"
        "这是你真实的一天，不用主动交代，也别把这句括号原样说出来。）"
    )


def upcoming(now: datetime | None = None) -> str:
    """下一段日程，供主动消息「一会儿要干嘛」使用。"""
    now = now or datetime.now()
    day = now.weekday()
    today = now.hour * 60 + now.minute
    for days, start, end, place, doing, mood, _details in _SCHEDULE:
        if day not in days:
            continue
        if _minutes(start) > today:
            return f"{start} {place}{doing}"
    return ""


if __name__ == "__main__":
    moment = datetime.now()
    print(render(moment))
    print(state(moment))
    print("下一段:", upcoming(moment) or "今天没有了")
