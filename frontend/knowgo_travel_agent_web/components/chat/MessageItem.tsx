'use client';

import { memo, useEffect, useRef, useCallback, useState } from 'react';
import { useDispatch } from 'react-redux';
import type { AppDispatch } from '@/store';
import type { ChatMessage, SkillUiFrame } from '@/lib/types';
import { escapeHtml } from '@/lib/utils';
import {
  fetchPlanThunk,
  streamingTargets,
  submitTrainSkillThunk,
} from '@/store/slices/chatSlice';
import { showToast } from '@/store/slices/uiSlice';
import { UI_SKILL_REGISTRY, type UiSkillProps } from '../skills/train-multi-segment/skillRegistry';
import type { TrainMultiSegmentSkillData } from '../skills/train-multi-segment/types';
import LoadingDots from '../common/LoadingDots';

/** 注册表取组件的统一形态（各 Skill 组件的 props 结构一致，仅 data/payload 类型不同） */
type AnySkillComponent = React.ComponentType<
  UiSkillProps<unknown, unknown> & { data: unknown }
>;

/**
 * [MERGE] 聊天流内嵌 UI Skill 块：按帧里的 skill_name 查注册表取组件渲染。
 * 未知 skill_name 直接不渲染（后端加新技能而前端未发布时不影响对话）。
 */
function SkillBlock({ frame }: { frame: SkillUiFrame }) {
  const dispatch = useDispatch<AppDispatch>();
  const [submitting, setSubmitting] = useState(false);
  // [MERGE] 修复 167：已提交态以消息里落库的账本为准——切会话（历史重取、消息 id 会重生成）
  // 或刷新后，卡片仍为锁定态且预览能复原各段选中项；本轮刚提交的由本地 done 立即接管，
  // 两者只需其一为真即视为已提交
  const persisted = frame.submitted;
  const [done, setDone] = useState(() => Boolean(persisted));

  // [MERGE] 提交走 store 的 SSE thunk：按帧里的 skill_name 路由到对应提交端点，
  // 后端落库后直接续跑后续编排（住宿→餐饮→行程），回复以新的助手气泡接着出现在对话里；
  // 此前只回 JSON，提交完流程就停住
  const handleSubmit = useCallback(
    async (payload: unknown) => {
      if (submitting) return;
      setSubmitting(true);
      try {
        const res = await dispatch(
          submitTrainSkillThunk({ skillName: frame.skill_name, payload }),
        ).unwrap();
        if (res.ok) setDone(true); // 成功：卡片转入"已提交"态并继续留在对话里
        // 失败保持展开供重选（仅提示错误）
        else if (res.msg) dispatch(showToast({ message: res.msg, type: 'error' }));
      } finally {
        setSubmitting(false);
      }
    },
    [dispatch, submitting, frame.skill_name],
  );

  const key = frame.skill_name as keyof typeof UI_SKILL_REGISTRY;
  if (!(key in UI_SKILL_REGISTRY)) return null;
  const SkillComp = UI_SKILL_REGISTRY[key] as unknown as AnySkillComponent;
  // [MERGE] 提交成功后不再卸载卡片：保留已提交状态在对话流里展示（用户要求，视觉更连贯）；
  // 交由外层 pointer-events 锁 + 组件 submitted 标记共同禁用交互，避免重复提交

  return (
    <div className={`mt-3${submitting || done ? ' pointer-events-none' : ''}`}>
      <SkillComp
        data={frame.data as TrainMultiSegmentSkillData}
        onSubmit={handleSubmit}
        submitting={submitting}
        submitted={done}
        initialSelections={persisted?.selections}
      />
    </div>
  );
}

interface MessageItemProps {
  message: ChatMessage;
  /** 是否为当前流式渲染中的助手消息（由 ChatMain 判定） */
  isStreamingTarget: boolean;
}

/**
 * 单条消息（用户 / AI 气泡）
 * - 原版信纸边框装饰：助手消息左侧金色标线 + 左上圆角 2px
 * - 消息入场 slideIn 滑入动画（transform 硬件加速 + will-change）
 * - 流式期间：content 置空并注册到 streamingTargets 注册表，
 *   SSE thunk 直接向该元素 TextNode 增量追加文本，绕开 React 重绘
 * - 文本渲染与原生逻辑一致：escapeHtml（转义 + \n 转 <br>）
 */
function MessageItem({ message, isStreamingTarget }: MessageItemProps) {
  const contentElRef = useRef<HTMLDivElement | null>(null);
  const dispatch = useDispatch<AppDispatch>();
  const isUser = message.role === 'user';

  // [MERGE] 点击查看行程清单：GET /api/v1/itinerary-plans/{planId} → PlanModal
  const handleViewPlan = useCallback(() => {
    if (message.planId) dispatch(fetchPlanThunk(message.planId));
  }, [dispatch, message.planId]);

  // 注册 / 注销流式目标 DOM（仅在流式消息挂载时注册）
  useEffect(() => {
    if (isStreamingTarget && contentElRef.current) {
      streamingTargets.set(message.id, contentElRef.current);
    }
    return () => {
      if (streamingTargets.get(message.id) === contentElRef.current) {
        streamingTargets.delete(message.id);
      }
    };
  }, [isStreamingTarget, message.id]);

  // 加载占位：只渲染三点动画
  if (message.kind === 'loading') {
    return (
      <div className="animate-slide-in flex max-w-[800px] gap-4 will-change-transform">
        <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-primary text-sm text-white">
          <i className="fa-solid fa-robot" />
        </div>
        <div className="relative rounded-xl border border-black/5 bg-white px-6 py-4 text-[15px] leading-[1.8] shadow-bubble">
          <LoadingDots />
        </div>
      </div>
    );
  }

  return (
    <div
      className={`animate-slide-in flex max-w-[800px] gap-4 will-change-transform ${
        isUser ? 'flex-row-reverse self-end' : ''
      }`}
    >
      {/* 头像 */}
      <div
        className={`flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full text-sm text-white ${
          isUser ? 'bg-accent' : 'bg-primary'
        }`}
      >
        <i className={`fa-solid ${isUser ? 'fa-user' : 'fa-robot'}`} />
      </div>

      {/* 气泡内容 */}
      <div
        className={`relative rounded-xl px-6 py-4 text-[15px] leading-[1.8] shadow-bubble ${
          isUser
            ? 'rounded-tr-[2px] bg-primary text-white'
            : 'rounded-tl-[2px] border border-black/5 bg-white text-text-main before:absolute before:bottom-[15px] before:left-0 before:top-[15px] before:w-[3px] before:rounded-[0_2px_2px_0] before:bg-accent before:opacity-50 before:content-[""]'
        }`}
      >
        {/* 流式直写目标：文本增量追加到这里；卡片等交互组件挂在它外面，
            避免流结束时 el.textContent='' 把 React 管理的节点一起清掉 */}
        <div ref={contentElRef}>
          {/* 流式期间不传 children，React 不会清理由 TextNode 追加的真实 DOM 内容 */}
          {isStreamingTarget ? null : (
            <span dangerouslySetInnerHTML={{ __html: escapeHtml(message.content) }} />
          )}
        </div>

        {/* [MERGE] 等待波浪动画：流式气泡处于等待态（模型思考 / 分片间歇静默）时展示。
            必须渲染在正文容器外——流式直写 el.textContent = '' 不会误清 React 管理的节点 */}
        {isStreamingTarget && message.waiting && <LoadingDots />}

        {/* [MERGE] 行程清单入口三态：planId=可点击查看；planPending=生成中（点开即加载态）；两者皆无=不渲染。
            注意不设 !isStreamingTarget 条件——pending 帧在正文流式期间即到达，按钮需提前显示 */}
        {(message.planId || message.planPending) && (
          <button
            type="button"
            onClick={handleViewPlan}
            disabled={!message.planId}
            className={`mt-3 flex items-center gap-1.5 rounded-sm border px-3 py-1.5 text-[13px] font-semibold transition-colors duration-200 ${
              message.planId
                ? 'cursor-pointer border-accent bg-transparent text-accent hover:bg-accent hover:text-white'
                : 'cursor-progress border-accent/40 bg-accent/5 text-accent/70'
            }`}
          >
            {message.planId ? (
              <>
                <i className="fa-regular fa-map" /> 查看行程清单
              </>
            ) : (
              <>
                <i className="fa-solid fa-spinner fa-spin" /> 行程清单生成中，可先看正文
              </>
            )}
          </button>
        )}

        {/* [MERGE] 内嵌 UI Skill（如多段高铁选择卡片）：流结束后才渲染，
            提交成功后卡片保留并锁定（已提交态） */}
        {message.skillUi && !isStreamingTarget && <SkillBlock frame={message.skillUi} />}
      </div>
    </div>
  );
}

export default memo(MessageItem);
