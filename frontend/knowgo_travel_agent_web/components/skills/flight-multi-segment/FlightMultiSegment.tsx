'use client';

/**
 * 多段航班行程 UI Skill（skill_name = flight_multi_segment）[MERGE] 修复 155
 *
 * 交互规则（与多段高铁 TrainMultiSegment 同构；段数**动态**）：
 *   1. 后端一次性【并行】拉取全部段航班，前端不做查询。
 *   2. 组件按 payload.segments.length = N 自动生成子步骤（**禁止硬编码段数**）：
 *        第 1 步  选第 1 段航班 → 选中后【下一步】点亮
 *        ……
 *        第 N 步  选第 N 段航班 → 【下一步】文案变【预览全部行程】
 *        第 N+1 步 预览汇总 → 仅此页提供【提交确认】
 *   3. 支持【上一步】回退修改前面已选中的航班。
 *   4. 只在预览页做一次全局提交（只回传用户实际选中的段，空段不提交、后端自动占位），
 *      提交后按钮变"已提交"态。
 *   5. 有候选的段未选航班时【下一步】置灰不可点；空段（flight_list 为空）可直接跳过
 *      进入下一段，直到预览页。
 *   6. **不做任何航班时间冲突校验**：不比对到达/起飞先后，界面也不出现冲突提示。
 */

import { memo, useCallback, useMemo, useState } from 'react';
import {
  FLIGHT_MULTI_SEGMENT_SKILL,
  type FlightMultiSegmentSkillData,
  type FlightMultiSegmentSubmitPayload,
  type FlightOption,
  type FlightSegment,
  type FlightSelectionMap,
  type FlightStage,
} from './types';

// [MERGE] 修复 155：Skill 注册键在本文件再导出一次（唯一真源仍在 ./types），
// 便于阅读组件时一眼看到 skill_name，也满足"组件文件内可见 FLIGHT_MULTI_SEGMENT_SKILL"
export { FLIGHT_MULTI_SEGMENT_SKILL };

export interface FlightMultiSegmentProps {
  /** 后端 Skill 出参（并行查询后的结构化数据） */
  data: FlightMultiSegmentSkillData;
  /** 全局提交回调：仅在预览页点击【提交确认】时触发一次 */
  onSubmit: (payload: FlightMultiSegmentSubmitPayload) => void;
  /** 提交中标记（由上层控制，用于置灰提交按钮） */
  submitting?: boolean;
  /** [MERGE] 已提交标记：卡片提交成功后仍保留在对话流中展示，此时锁定交互（上一步/提交均禁用） */
  submitted?: boolean;
  /** [MERGE] 已提交前的选中账本（切回会话时复原预览） */
  initialSelections?: Record<string, string>;
}

// ==================== 取数工具 ====================

/** 按 option_id（身份）在段内取航班；航班号可重复，不能用 flight_no 查 */
function findFlight(segment: FlightSegment, optionId?: string): FlightOption | undefined {
  if (!optionId) return undefined;
  return segment.flight_list.find((item) => item.option_id === optionId);
}

/** 价格展示：数据源未返回价格（price<=0）时显示"价格待定"，绝不展示 ¥0 造成误导 */
function priceText(price: number): string {
  return price > 0 ? `¥${price}` : '价格待定';
}

// ==================== 子组件：进度条 ====================

/** 进度条节点：按 segments 动态生成 N 个段节点 + 1 个预览节点（总步数 = N+1） */
function SkillProgress({
  segments,
  activeIndex,
}: {
  segments: FlightSegment[];
  /** 当前激活节点下标；段选择阶段为段下标，预览阶段为 segments.length */
  activeIndex: number;
}) {
  const nodes = useMemo(
    () => [
      ...segments.map((segment, index) => ({
        key: segment.segment_id,
        label: `第 ${index + 1} 段`,
        sub: `${segment.from}→${segment.to}`,
      })),
      { key: 'preview', label: '预览确认', sub: '提交前最终核对' },
    ],
    [segments],
  );

  return (
    <ol className="mb-5 flex items-center gap-1">
      {nodes.map((node, index) => {
        const done = index < activeIndex;
        const active = index === activeIndex;
        return (
          <li key={node.key} className="flex min-w-0 flex-1 items-center gap-1">
            <div className="flex min-w-0 flex-col items-center gap-1">
              <span
                className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-xs font-semibold transition-colors ${
                  done
                    ? 'border-primary bg-primary text-white'
                    : active
                      ? 'border-accent bg-accent/15 text-accent'
                      : 'border-black/10 bg-white/70 text-text-light'
                }`}
              >
                {done ? <i className="fa-solid fa-check" /> : index + 1}
              </span>
              <span
                className={`truncate text-[11px] ${
                  active ? 'font-semibold text-accent' : 'text-text-light'
                }`}
              >
                {node.label}
              </span>
              <span className="truncate text-[10px] text-text-light/80">{node.sub}</span>
            </div>
            {index < nodes.length - 1 && (
              <span
                className={`mb-7 h-px flex-1 ${done ? 'bg-primary/40' : 'bg-black/10'}`}
              />
            )}
          </li>
        );
      })}
    </ol>
  );
}

// ==================== 子组件：航班卡片 ====================

interface FlightCardProps {
  flight: FlightOption;
  /** 是否已选中（选中高亮） */
  selected: boolean;
  onPick: (optionId: string) => void;
}

function FlightCard({ flight, selected, onPick }: FlightCardProps) {
  // [MERGE] 选中身份用 option_id：航班号可能不唯一，用 flight_no 会取错航班
  return (
    <button
      type="button"
      onClick={() => onPick(flight.option_id)}
      aria-pressed={selected}
      className={`w-full rounded-md border p-4 text-left transition-all duration-200 ${
        selected
          ? 'border-primary bg-primary/5 shadow-icon'
          : 'border-black/10 bg-white/70 hover:border-primary/50 hover:bg-white'
      }`}
    >
      {/* 第一行：航班号 + 选中标记 */}
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="font-serif text-lg font-bold text-text-serif">
          {flight.flight_no}
          {selected && <i className="fa-solid fa-circle-check ml-2 text-sm text-primary" />}
        </span>
        <span className="text-xs font-semibold text-accent">{priceText(flight.price)}</span>
      </div>

      {/* 第二行：机场与时刻 */}
      <div className="flex items-center gap-3 text-sm text-text-main">
        <span className="shrink-0 text-right">
          <span className="block font-semibold">{flight.dep_time}</span>
          <span className="block text-[11px] text-text-light">{flight.dep_airport}</span>
        </span>
        <span className="flex min-w-0 flex-1 flex-col items-center">
          <span className="text-[11px] text-text-light">{flight.duration || '—'}</span>
          <span className="flex w-full items-center gap-1 text-text-light/60">
            <span className="h-px flex-1 bg-current" />
            <i className="fa-solid fa-plane text-[10px]" />
            <span className="h-px flex-1 bg-current" />
          </span>
        </span>
        <span className="shrink-0">
          <span className="block font-semibold">{flight.arr_time}</span>
          <span className="block text-[11px] text-text-light">{flight.arr_airport}</span>
        </span>
      </div>
    </button>
  );
}

// ==================== 主组件 ====================

function FlightMultiSegment({
  data,
  onSubmit,
  submitting = false,
  submitted = false,
  initialSelections,
}: FlightMultiSegmentProps) {
  const segments = data.segments;

  /** 状态机当前阶段（交互规则的唯一流向控制；段数由 segments.length 决定） */
  const [stage, setStage] = useState<FlightStage>(submitted ? 'submit' : 'select');
  /** 当前段下标 0 … N-1（预览阶段保持在末段，便于【上一步】回到末段改选） */
  const [stepIndex, setStepIndex] = useState(0);
  /** 选择账本：segment_id → option_id */
  const [selections, setSelections] = useState<FlightSelectionMap>(initialSelections ?? {});

  const isPreview = stage === 'preview' || stage === 'submit';
  const isLastStep = stepIndex >= segments.length - 1;
  const currentSegment = segments[stepIndex];
  const currentSelection = currentSegment
    ? selections[currentSegment.segment_id]
    : undefined;
  /** 该段后端未返回任何航班（查询降级）：可跳过该段继续，但绝不代用户瞎选 */
  const currentEmpty = Boolean(currentSegment) && currentSegment.flight_list.length === 0;
  /** 【下一步】禁用条件：仅"该段有候选但未选"时置灰；空段不禁用（点击直接跳过） */
  const nextDisabled =
    Boolean(currentSegment) && currentSegment.flight_list.length > 0 && !currentSelection;

  /** 全部「有候选的段」已选 + 未提交 → 才允许提交（空段无需选择） */
  const allPickableSelected = segments.every(
    (segment) =>
      segment.flight_list.length === 0 || Boolean(selections[segment.segment_id]),
  );
  const canSubmit = allPickableSelected && !submitting && stage !== 'submit' && !submitted;

  /** 选中航班：只写账本（值为 option_id，阶段恒为 select，无需额外状态跃迁） */
  const handlePick = useCallback((segmentId: string, optionId: string) => {
    setSelections((prev) => ({ ...prev, [segmentId]: optionId }));
  }, []);

  /** 【下一步】：空段直接跳过；有候选的段未选则返回（按钮已置灰）；第 N 段之后进入预览页 */
  const handleNext = useCallback(() => {
    if (!currentEmpty && !currentSelection) return;
    if (isLastStep) {
      setStage('preview');
      return;
    }
    setStepIndex((prev) => Math.min(prev + 1, segments.length - 1));
  }, [currentEmpty, currentSelection, isLastStep, segments.length]);

  /** 【上一步】：从预览回到末段改选；段内回退一段（第 1 步时按钮置灰） */
  const handlePrev = useCallback(() => {
    if (isPreview) {
      setStage('select');
      return;
    }
    setStepIndex((prev) => Math.max(0, prev - 1));
  }, [isPreview]);

  /** 【提交确认】：仅预览页可用，只回传用户实际选中的段（option_id 为身份）；空段不提交 */
  const handleSubmit = useCallback(() => {
    if (!canSubmit) return;
    setStage('submit');
    onSubmit({
      skill_name: FLIGHT_MULTI_SEGMENT_SKILL,
      segments: segments.flatMap((segment) => {
        const optionId = selections[segment.segment_id];
        // 空段：不传缺 option_id 的项（后端会为该段自动占位，传空串会报 MISSING_OPTION_ID）
        if (!optionId) return [];
        return [
          {
            segment_id: segment.segment_id,
            option_id: optionId,
            flight_no: findFlight(segment, optionId)?.flight_no ?? '',
          },
        ];
      }),
    });
  }, [canSubmit, onSubmit, segments, selections]);

  /** 预览页合计票价（仅汇总已返回的 price；全部未返回则为 0，界面显示"价格待定"） */
  const pickedTotal = useMemo(
    () =>
      segments.reduce(
        (sum, segment) =>
          sum + (findFlight(segment, selections[segment.segment_id])?.price ?? 0),
        0,
      ),
    [segments, selections],
  );

  return (
    <section className="glass-card w-full rounded-lg bg-white/85 p-6">
      {/* 标题区：Skill 标识 + 并行查询说明 */}
      <header className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h3 className="font-serif text-xl text-text-serif">
            <i className="fa-solid fa-plane mr-2 text-accent" />
            多段航班行程
          </h3>
          <p className="mt-1 text-xs text-text-light">
            共 {segments.length} 段 · 分 {segments.length + 1} 步完成选择 ·
            后端已一次性并行查询全部段航班
          </p>
        </div>
        <span className="shrink-0 rounded-full bg-primary/10 px-3 py-1 text-[11px] text-primary">
          {data.skill_name}
        </span>
      </header>

      {/* 进度条：段选择阶段指向当前段；预览阶段指向预览节点（末位） */}
      <SkillProgress
        segments={segments}
        activeIndex={isPreview ? segments.length : stepIndex}
      />

      {/* 主体一：逐段选择（第 1 ~ N 步，N 由 segments.length 决定） */}
      {!isPreview && (
        <div>
          <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
            <span className="rounded-full bg-accent/10 px-3 py-0.5 text-xs font-semibold text-accent">
              Step {stepIndex + 1} / {segments.length}
            </span>
            <span className="font-semibold text-text-serif">
              {currentSegment.from} → {currentSegment.to}
            </span>
            <span className="text-xs text-text-light">{currentSegment.date}</span>
          </div>
          <div className="flex flex-col gap-3">
            {currentEmpty ? (
              /* 该段无候选航班：如实告知可调整或改交通方式（可跳过继续），不提供任何替代航班 */
              <p className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-700">
                该日期暂无航班数据，可调整出发日期或改选其他交通方式
              </p>
            ) : (
              currentSegment.flight_list.map((flight) => (
                <FlightCard
                  key={flight.option_id}
                  flight={flight}
                  selected={currentSelection === flight.option_id}
                  onPick={(optionId) => handlePick(currentSegment.segment_id, optionId)}
                />
              ))
            )}
          </div>
        </div>
      )}

      {/* 主体二：预览汇总（第 N+1 步，唯一提供【提交确认】的页面） */}
      {isPreview && (
        <div className="flex flex-col gap-3">
          {segments.map((segment, index) => {
            const flight = findFlight(segment, selections[segment.segment_id]);
            return (
              <div
                key={segment.segment_id}
                className="rounded-md border border-black/10 bg-white/70 p-4"
              >
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-sm font-semibold text-text-serif">
                    第 {index + 1} 段 · {segment.from} → {segment.to}
                  </span>
                  <span className="text-xs text-text-light">{segment.date}</span>
                </div>
                {flight ? (
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-text-main">
                    <span className="font-serif font-bold text-text-serif">
                      {flight.flight_no}
                    </span>
                    <span>
                      {flight.dep_airport} {flight.dep_time} → {flight.arr_airport}{' '}
                      {flight.arr_time}
                    </span>
                    <span className="text-xs text-text-light">
                      历时 {flight.duration || '—'}
                    </span>
                    <span className="ml-auto text-xs font-semibold text-accent">
                      {priceText(flight.price)}
                    </span>
                  </div>
                ) : segment.flight_list.length === 0 ? (
                  /* 空段：提交时不会回传，后端自动占位 */
                  <p className="text-xs text-text-light">未查询到航班，未选择</p>
                ) : (
                  <p className="text-xs text-red-500">
                    该段尚未选择航班，请返回上一步补选
                  </p>
                )}
              </div>
            );
          })}

          {/* 合计票价（仅汇总后端已返回的 price） */}
          <div className="flex items-center justify-between rounded-md bg-primary/5 px-4 py-3 text-sm">
            <span className="text-text-serif">合计票价（单人）</span>
            <span className="font-semibold text-accent">{priceText(pickedTotal)}</span>
          </div>
        </div>
      )}

      {/* 底部操作区：上一步 / 下一步·预览全部行程 / 提交确认 */}
      <footer className="mt-6 flex items-center justify-between gap-3 border-t border-black/5 pt-4">
        <button
          type="button"
          onClick={handlePrev}
          disabled={submitting || submitted || (!isPreview && stepIndex <= 0)}
          className="rounded-sm border border-black/15 px-5 py-2 text-sm text-text-main transition-colors duration-200 hover:border-primary hover:text-primary disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-black/15 disabled:hover:text-text-main"
        >
          <i className="fa-solid fa-chevron-left mr-1.5" />
          上一步
        </button>

        {isPreview ? (
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canSubmit}
            className="rounded-sm bg-accent px-6 py-2 text-sm font-semibold text-white transition-colors duration-200 hover:bg-accent-hover disabled:cursor-not-allowed disabled:bg-[#ccc]"
          >
            {stage === 'submit' ? (
              <>
                <i className="fa-solid fa-check mr-1.5" /> 已提交
              </>
            ) : submitting ? (
              <>
                <i className="fa-solid fa-spinner fa-spin mr-1.5" /> 提交中...
              </>
            ) : (
              <>
                <i className="fa-solid fa-paper-plane mr-1.5" /> 提交确认
              </>
            )}
          </button>
        ) : (
          <button
            type="button"
            onClick={handleNext}
            disabled={nextDisabled}
            className="rounded-sm bg-primary px-6 py-2 text-sm font-semibold text-white transition-colors duration-200 hover:bg-primary-light disabled:cursor-not-allowed disabled:bg-[#ccc]"
          >
            {isLastStep ? '预览全部行程' : '下一步'}
            <i className="fa-solid fa-chevron-right ml-1.5" />
          </button>
        )}
      </footer>
    </section>
  );
}

export default memo(FlightMultiSegment);