"""Agent UI Skill API：聊天流内嵌组件（前端 Skill）的回提交口

- POST /api/v1/skills/train_multi_segment/submit  多段高铁选择提交（校验 → 落库 → SSE 续跑）
- POST /api/v1/skills/hotel_multi_segment/submit  多段住宿选择提交（校验 → 落库 → SSE 续跑）
- POST /api/v1/skills/flight_multi_segment/submit 多段航班选择提交（校验 → 落库 → SSE 续跑）
- POST /api/v1/skills/driving_multi_segment/submit 多段自驾路线确认提交（校验 → 落库 → SSE 续跑）

为什么单独开路由：前端卡片是"一次选完 N 段 → 一次提交"的交互，走的是独立 HTTP 提交
（不是对话消息），提交成功后由代码把段账本写入状态机并推进到下一步。

为什么返回 SSE 而不是 JSON：只落状态不跑编排会让流程停住——卡片下发后旧 SSE 通道已关闭，
前端只做"卡片收起"，没有任何人驱动住宿步那一轮（用户实证：选完高铁后停在原处、无后续输出）。
故提交成功后用一条合成发言复用 run_chat_turn 续跑，事件流原样下发；
前端沿用既有事件管道渲染，住宿/餐饮/行程各步与原来"逐段确认"时完全一致。
"""
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.api.v1.messages import _SSE_HEADERS, _sse  # 复用同一套 SSE 帧序列化与响应头
from app.core.events import ROLE_ASSISTANT, ROLE_DONE, ROLE_ERROR, make_event
from app.models.base import get_db
from app.models.user import User
from app.services.chat_contract_service import (  # 卡片提交态落库
    mark_skill_ui_submitted,
    run_chat_turn,
)
from app.services.conversation_service import get_owned_conversation
from app.capabilities.driving_multi_segment.driving_flow import apply_driving_selection
from app.capabilities.driving_multi_segment.driving_skill_schema import (
    DRIVING_MULTI_SEGMENT_SKILL,
    DrivingSubmitPayload,
    DrivingSubmitResult,
)
from app.capabilities.flight_multi_segment.flight_flow import apply_flight_selection
from app.capabilities.flight_multi_segment.flight_skill_schema import (
    FLIGHT_MULTI_SEGMENT_SKILL,
    FlightSubmitPayload,
    FlightSubmitResult,
)
from app.capabilities.hotel_multi_segment.hotel_flow import apply_hotel_selection
from app.capabilities.hotel_multi_segment.hotel_skill_schema import (
    HOTEL_MULTI_SEGMENT_SKILL,
    HotelMultiSegmentSubmitPayload,
    HotelMultiSegmentSubmitResult,
)
from app.capabilities.train_multi_segment.train_flow import apply_train_selection
from app.capabilities.train_multi_segment.train_skill_schema import (
    TRAIN_MULTI_SEGMENT_SKILL,
    TrainSubmitPayload,
    TrainSubmitResult,
)
from app.utils.logger import app_logger

router = APIRouter(prefix="/skills", tags=["Agent Skill"])

class TrainSubmitRequest(BaseModel):
    """提交请求体：会话 id + 前端 Skill 一次性选择结果"""

    conversation_id: str = Field(min_length=1, description="会话 id（= LangGraph thread_id）")
    payload: TrainSubmitPayload = Field(description="前端 onSubmit 抛出的选择结果")


class HotelSubmitRequest(BaseModel):
    """住宿提交请求体：会话 id + 前端 HotelMultiSegment 的 onSubmit 结果"""

    conversation_id: str = Field(min_length=1, description="会话 id（= LangGraph thread_id）")
    payload: HotelMultiSegmentSubmitPayload = Field(description="前端 onSubmit 抛出的选择结果")


class FlightSubmitRequest(BaseModel):
    """航班提交请求体：会话 id + 前端 FlightMultiSegment 的 onSubmit 结果 """

    conversation_id: str = Field(min_length=1, description="会话 id（= LangGraph thread_id）")
    payload: FlightSubmitPayload = Field(description="前端 onSubmit 抛出的选择结果")


class DrivingSubmitRequest(BaseModel):
    """自驾提交请求体：会话 id + 前端 DrivingMultiSegment 的 onSubmit 结果 """

    conversation_id: str = Field(min_length=1, description="会话 id（= LangGraph thread_id）")
    payload: DrivingSubmitPayload = Field(description="前端 onSubmit 抛出的确认结果")


def _resume_content(result: TrainSubmitResult) -> str:
    """续跑用的合成发言（用户口吻）：住宿步提示词据此知道车次已定，直接进入酒店候选轮"""
    legs = "；".join(result.texts) if result.texts else f"{len(result.accepted)} 段车次"
    return f"我已按卡片选定全部车次：{legs}。请继续为我推荐住宿，并推进后续行程安排。"


def _hotel_resume_content(result: HotelMultiSegmentSubmitResult) -> str:
    """住宿续跑用的合成发言（用户口吻）

    口径：餐饮偏好是表单必填项且已在落库时写入 selected_food_types，故住宿是本流程**最后一个
    人工选择项**——合成发言必须要求"直接生成行程报告"，不能写"请继续为我安排餐饮"：
    实测模型会顺着这句话去讲餐饮候选（甚至模仿住宿卡片话术），结果行程正文一个字都不出。
    """
    stays = "；".join(result.texts) if result.texts else f"{len(result.accepted)} 站住宿"
    return f"我已按卡片选定全部住宿：{stays}。住宿是最后一项待确认内容，请直接为我生成完整行程报告。"


def _flight_resume_content(result: FlightSubmitResult) -> str:
    """航班续跑用的合成发言（用户口吻） 住宿步据此知道航班已定，直接进入酒店候选轮"""
    legs = "；".join(result.texts) if result.texts else f"{len(result.accepted)} 段航班"
    return f"我已按卡片选定全部航班：{legs}。请继续为我推荐住宿，并推进后续行程安排。"


def _driving_resume_content(result: DrivingSubmitResult) -> str:
    """自驾续跑用的合成发言（用户口吻） 住宿步据此知道路线已确认，直接进入酒店候选轮"""
    legs = "；".join(result.texts) if result.texts else f"{len(result.accepted)} 段自驾"
    return f"我已确认全部自驾路线：{legs}。请继续为我推荐住宿，并推进后续行程安排。"


def _confirmed_content(label: str, texts: list[str]) -> str:
    """ 提交成功后**立刻回显用户已选定的内容**（代码生成，各技能同款格式）

    用户要求：选完高铁/住宿后，对话里要能看到自己选好的车次、酒店（或品牌），随后继续下一步。
    因此这条帧必须在续跑之前下发——它是"已选结果"的确定性回执（results.texts 由服务端
    依据真实候选回查拼出），而不是模型自由发挥；无内容时只回一句确认，绝不留空。
    """
    if not texts:
        return f"✅ 已确认您选择的{label}。\n"
    items = "\n".join(f"{index}. {text}" for index, text in enumerate(texts, 1))
    return f"✅ 已确认您选择的{label}：\n{items}\n"

@router.post("/train_multi_segment/submit")
async def submit_train_multi_segment(
        req: TrainSubmitRequest,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
):
    """多段高铁提交：基础合法性校验（**不做任何列车时间校验**）→ 落库推进 → SSE 续跑住宿步"""
    conv = await get_owned_conversation(db, req.conversation_id, user.id)
    if not conv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    async def event_generator():
        # ★ ① 校验 + 落库推进（第 9 章 capability）
        result = await apply_train_selection(req.conversation_id, req.payload)
        if not result.ok:
            # 校验未过：只在流内如实告知（前端渲染为一条助手气泡，卡片保持展开供用户重选）
            yield _sse(make_event(ROLE_ERROR, result.msg))
            yield _sse(make_event(ROLE_DONE, "finish", code=400))
            return
        # 提交态 + 选中账本写回卡片帧消息（切会话/刷新后卡片仍为已提交锁定态）
        # ★ ② 回填卡片提交态（切会话回来仍是锁定态）
        await mark_skill_ui_submitted(db, conv.id, TRAIN_MULTI_SEGMENT_SKILL, req.payload)
        app_logger.info(f"🚄 卡片提交成功，SSE 续跑后续步骤：已确认 {len(result.texts)} 段")
        # 先在对话里回显用户已选定的车次（再续跑下一步），界面才能"看到选了什么"
        # ★ ③ 先回显已选结果：服务端的权威回执
        yield _sse(make_event(ROLE_ASSISTANT, _confirmed_content("车次（按行程段顺序）", result.texts)))
        # ★ ④ 续跑：合成发言 + 三个续跑参数，复用整条编排链
        stream = await run_chat_turn(
            db, str(user.id), req.conversation_id, _resume_content(result),
            force_planning=True,  # 续跑必须成立：不走概率性意图路由，由代码指定进规划状态机
            persist_user=False,   # 合成发言不落库：历史里不该冒出用户没打过的句子
            resume_stage="accommodation_planning",  # 反馈文案：车次已确认 → 正在规划住宿
        )
        async for event in stream:
            yield _sse(event)

    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.post("/hotel_multi_segment/submit")
async def submit_hotel_multi_segment(
        req: HotelSubmitRequest,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
):
    """多段住宿提交：基础合法性校验（**不做任何日期校验**）→ 落库推进 → SSE 续跑餐饮/行程步"""
    conv = await get_owned_conversation(db, req.conversation_id, user.id)
    if not conv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    async def event_generator():
        result = await apply_hotel_selection(req.conversation_id, req.payload)
        if not result.ok:
            # 校验未过：只在流内如实告知（前端渲染为一条助手气泡，卡片保持展开供用户重选）
            yield _sse(make_event(ROLE_ERROR, result.msg))
            yield _sse(make_event(ROLE_DONE, "finish", code=400))
            return
        # 提交态 + 选中账本写回卡片帧消息（切会话/刷新后卡片仍为已提交锁定态）
        await mark_skill_ui_submitted(db, conv.id, HOTEL_MULTI_SEGMENT_SKILL, req.payload)
        app_logger.info(f"🏨 住宿卡片提交成功，SSE 续跑后续步骤：已确认 {len(result.texts)} 站")
        # 先在对话里回显用户已选定的住宿（门店或品牌，按站顺序），再续跑下一步
        yield _sse(make_event(ROLE_ASSISTANT, _confirmed_content("住宿（按行程站顺序）", result.texts)))
        stream = await run_chat_turn(
            db, str(user.id), req.conversation_id, _hotel_resume_content(result),
            force_planning=True,  # 续跑必须成立：不走概率性意图路由，由代码指定进规划状态机
            persist_user=False,   # 合成发言不落库：历史里不该冒出用户没打过的句子
            # 反馈文案按落库后实际推进到的步骤取（住宿已确认 → 正在生成行程报告）
            resume_stage=result.next_step or None,
        )
        async for event in stream:
            yield _sse(event)

    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.post("/flight_multi_segment/submit")
async def submit_flight_multi_segment(
        req: FlightSubmitRequest,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
):
    """多段航班提交：基础合法性校验（**不做任何航班时间校验**）→ 落库推进 → SSE 续跑住宿步"""
    conv = await get_owned_conversation(db, req.conversation_id, user.id)
    if not conv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    async def event_generator():
        result = await apply_flight_selection(req.conversation_id, req.payload)
        if not result.ok:
            # 校验未过：只在流内如实告知（前端渲染为一条助手气泡，卡片保持展开供用户重选）
            yield _sse(make_event(ROLE_ERROR, result.msg))
            yield _sse(make_event(ROLE_DONE, "finish", code=400))
            return
        # 提交态 + 选中账本写回卡片帧消息（切会话/刷新后卡片仍为已提交锁定态）
        await mark_skill_ui_submitted(db, conv.id, FLIGHT_MULTI_SEGMENT_SKILL, req.payload)
        app_logger.info(f"✈️ 航班卡片提交成功，SSE 续跑后续步骤：已确认 {len(result.texts)} 段")
        # 先在对话里回显用户已选定的航班（按行程段顺序），再续跑下一步
        yield _sse(make_event(ROLE_ASSISTANT, _confirmed_content("航班（按行程段顺序）", result.texts)))
        stream = await run_chat_turn(
            db, str(user.id), req.conversation_id, _flight_resume_content(result),
            force_planning=True,  # 续跑必须成立：不走概率性意图路由，由代码指定进规划状态机
            persist_user=False,   # 合成发言不落库：历史里不该冒出用户没打过的句子
            resume_stage="accommodation_planning",  # 反馈文案：航班已确认 → 正在规划住宿
        )
        async for event in stream:
            yield _sse(event)

    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.post("/driving_multi_segment/submit")
async def submit_driving_multi_segment(
        req: DrivingSubmitRequest,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
):
    """多段自驾提交：基础合法性校验（**不做任何时间/路线校验**）→ 落库推进 → SSE 续跑住宿步"""
    conv = await get_owned_conversation(db, req.conversation_id, user.id)
    if not conv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    async def event_generator():
        result = await apply_driving_selection(req.conversation_id, req.payload)
        if not result.ok:
            # 校验未过：只在流内如实告知（前端渲染为一条助手气泡，卡片保持展开供用户重试）
            yield _sse(make_event(ROLE_ERROR, result.msg))
            yield _sse(make_event(ROLE_DONE, "finish", code=400))
            return
        # 提交态 + 选中账本写回卡片帧消息（切会话/刷新后卡片仍为已提交锁定态）
        await mark_skill_ui_submitted(db, conv.id, DRIVING_MULTI_SEGMENT_SKILL, req.payload)
        app_logger.info(f"🚗 自驾卡片提交成功，SSE 续跑后续步骤：已确认 {len(result.texts)} 段")
        # 先在对话里回显用户已确认的自驾路线（按行程段顺序），再续跑下一步
        yield _sse(make_event(ROLE_ASSISTANT, _confirmed_content("自驾路线（按行程段顺序）", result.texts)))
        stream = await run_chat_turn(
            db, str(user.id), req.conversation_id, _driving_resume_content(result),
            force_planning=True,  # 续跑必须成立：不走概率性意图路由，由代码指定进规划状态机
            persist_user=False,   # 合成发言不落库：历史里不该冒出用户没打过的句子
            resume_stage="accommodation_planning",  # 反馈文案：自驾已确认 → 正在规划住宿
        )
        async for event in stream:
            yield _sse(event)

    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=_SSE_HEADERS
    )