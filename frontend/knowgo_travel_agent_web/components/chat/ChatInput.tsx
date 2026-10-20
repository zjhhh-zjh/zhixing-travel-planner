'use client';

import {
  memo,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
} from 'react';
import { useDispatch, useSelector } from 'react-redux';
import type { AppDispatch, RootState } from '@/store';
import { sendMessageThunk } from '@/store/slices/chatSlice';
import { debounce } from '@/lib/utils';

/** textarea 最大高度（原版 120px） */
const MAX_HEIGHT = 120;

/**
 * 底部输入框
 * - 自适应高度：输入防抖后按 scrollHeight 调整，上限 120px
 * - Enter 发送、Shift+Enter 换行
 * - 发送按钮禁用态（空内容 / 发送中）+ hover 变色
 */
function ChatInput() {
  const dispatch = useDispatch<AppDispatch>();
  const isLoading = useSelector((state: RootState) => state.chat.isLoading);
  const isStreaming = useSelector((state: RootState) => state.chat.isStreaming);
  const [value, setValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // [MERGE] 忙碌态（请求中 / 流式进行中，含卡片提交续跑那一轮）：输入框整体禁用。
  // 只禁发送不禁输入会让用户以为流程已结束而抢先输入下面这句话，甚至并发发出新一轮请求
  const busy = isLoading || isStreaming;

  // 自适应高度（防抖 100ms）：先复位高度再按内容重设
  const resizeTextarea = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, []);
  // 防抖实例只创建一次，避免每次渲染重置计时器
  const resizeDebouncedRef = useRef(debounce(resizeTextarea, 100));

  useEffect(() => {
    resizeDebouncedRef.current();
  }, [value]);

  const handleChange = useCallback((e: ChangeEvent<HTMLTextAreaElement>) => {
    setValue(e.target.value);
  }, []);

  // [MERGE] 表单门禁：当前会话一条消息都没有 = 旅行需求表单还在等提交
  // 判据与 ChatMain 渲染 WelcomeScreen/TripForm 的条件完全一致（messages 为空即表单页），
  // 因此不再依赖 currentConversationId + requirementSubmitted 组合——"规划新行程"新建空白会话、
  // 刷新后只恢复了会话 id 而消息未加载，这两个入口都会被一并锁死
  const formGate = useSelector((s: RootState) => s.chat.messages.length === 0);

  // 整体禁用态：忙碌（请求中/流式进行中）或表单未提交。只禁发送不禁输入会让用户
  // 以为流程已结束而抢先输入，且看起来"输入框可用"与实际不可发送自相矛盾
  const locked = busy || formGate;

  const handleSend = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || busy) return;
    // [MERGE] 表单门禁：仍在表单页（一条消息都没有）时不允许发送
    if (formGate) return;
    dispatch(sendMessageThunk(trimmed));
    setValue('');
    // 发送后立即复位高度（下一帧，等 React 更新完）
    requestAnimationFrame(() => {
      if (textareaRef.current) textareaRef.current.style.height = 'auto';
    });
  }, [dispatch, busy, value, formGate]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  const canSend = value.trim().length > 0 && !busy && !formGate;

  return (
    <div className="border-t border-black/5 bg-white/90 p-6 narrow:p-4">
      <div className="flex items-end rounded-xl border border-[#e0e0e0] bg-white p-2 pl-4 shadow-[0_4px_15px_rgba(0,0,0,0.03)] transition-colors duration-300 focus-within:border-primary">
        <textarea
          ref={textareaRef}
          rows={1}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          disabled={locked}
          placeholder={
            formGate
              ? '请先填写上方旅行需求并点击「开始规划」'
              : busy
                ? '正在为您规划行程，请稍候…'
                : '输入目的地，例如：马尔代夫的水上飞机体验...'
          }
          style={{ maxHeight: MAX_HEIGHT }}
          className="flex-1 resize-none border-none bg-transparent py-2.5 font-sans text-[15px] focus:outline-none disabled:cursor-not-allowed disabled:text-text-light"
        />
        <button
          type="button"
          onClick={handleSend}
          disabled={!canSend}
          title="发送"
          className="flex h-10 w-10 items-center justify-center rounded-[8px] bg-primary text-base text-accent transition-all duration-200 hover:bg-primary-light hover:text-white disabled:cursor-not-allowed disabled:bg-[#ccc]"
        >
          <i className="fa-regular fa-paper-plane" />
        </button>
      </div>
    </div>
  );
}

export default memo(ChatInput);
