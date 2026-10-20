'use client';

/**
 * 多段高铁行程 UI Skill（skill_name = train_multi_segment）
 *
 * 交互规则（严格遵守；段数**动态**，示例为「石家庄→北京→上海→石家庄」3 段闭环）：
 *   1. 后端一次性【并行】拉取全部段班次，前端不做查询。
 *   2. 组件按 payload.segments.length = N 自动生成子步骤（**禁止硬编码段数**）：
 *        第 1 步  选第 1 段车次 → 选中后【下一步】点亮
 *        ……
 *        第 N 步  选第 N 段车次 → 【下一步】文案变【预览全部行程】
 *        第 N+1 步 预览汇总 → 仅此页提供【提交确认】
 *   3. 支持【上一步】回退修改前面已选中的车次。
 *   4. 取消逐段单独确认，只在预览页做一次全局提交。
 *   5. 当前子步骤未选车次时【下一步】置灰不可点；**该段 train_list 为空（本次实时无候选）时例外：
 *      视为"可跳过"，只提示不阻塞，【下一步】【预览全部行程】保持可用**。
 *   6. **不做任何列车时间冲突校验**：不比对到站/发车先后，界面也不出现冲突提示。
 *   7. 提交时只回传「有候选且已选中」的段，空段整条不进 segments（带空 option_id 会被后端
 *      以 MISSING_OPTION_ID 拒绝）；后端对空段不判"漏选"，放行时以占位文案 + 估算费用计入。
 */

import { memo, useCallback, useMemo, useState } from 'react';
import {
  TRAIN_MULTI_SEGMENT_SKILL,
  type TrainMultiSegmentSkillData,
  type TrainMultiSegmentSubmitPayload,
  type TrainOption,
  type TrainSegment,
  type TrainSelectionMap,
  type TrainStage,
} from './types';

export interface TrainMultiSegmentProps {
  /** 后端 Skill 出参（并行查询后的结构化数据） */
  data: TrainMultiSegmentSkillData;
  /** 全局提交回调：仅在预览页点击【提交确认】时触发一次 */
  onSubmit: (payload: TrainMultiSegmentSubmitPayload) => void;
  /** 提交中标记（由上层控制，用于置灰提交按钮） */
  submitting?: boolean;
  /** [MERGE] 已提交标记：卡片提交成功后仍保留在对话流中展示，此时锁定交互（上一步/提交均禁用） */
  submitted?: boolean;
  /** [MERGE] 已提交前的选中账本（切回会话时复原预览） */
  initialSelections?: Record<string, string>;
}

// ==================== 取数工具 ====================

/** 按 option_id（身份）在段内取趟次；车次号可重复，不能用 trainNo 查 */
function findTrain(segment: TrainSegment, optionId?: string): TrainOption | undefined {
  if (!optionId) return undefined;
  return segment.train_list.find((item) => item.option_id === optionId);
}

// ==================== 子组件：进度条 ====================

/** 进度条节点：按 segments 动态生成 N 个段节点 + 1 个预览节点（总步数 = N+1） */
function SkillProgress({
  segments,
  activeIndex,
}: {
  segments: TrainSegment[];
  /** 当前激活节点下标；段选择阶段为段下标，预览阶段为 segments.length */
  activeIndex: number;
}) {
  const nodes = useMemo(
    () => [
      ...segments.map((segment, index) => {
        // 空段（本次实时无候选）标记为可跳过：指示器按"已完成"呈现，不滞留成待办
        const skippable = segment.train_list.length === 0;
        return {
          key: segment.segment_id,
          label: `第 ${index + 1} 段`,
          sub: skippable
            ? `${segment.from}→${segment.to} · 可跳过`
            : `${segment.from}→${segment.to}`,
          skippable,
        };
      }),
      { key: 'preview', label: '预览确认', sub: '提交前最终核对', skippable: false },
    ],
    [segments],
  );

  return (
    <ol className="mb-5 flex items-center gap-1">
      {nodes.map((node, index) => {
        // 可跳过段一律按"已完成/可跳过"呈现，不再滞留为待办；预览节点不参与跳过判定
        const done = index < activeIndex || node.skippable;
        const active = index === activeIndex && !node.skippable;
        return (
          <li key={node.key} className="flex min-w-0 flex-1 items-center gap-1">
            <div className="flex min-w-0 flex-col items-center gap-1">
              <span
                className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-xs font-semibold transition-colors ${
                  node.skippable
                    ? 'border-amber-300 bg-amber-50 text-amber-600'
                    : done
                      ? 'border-primary bg-primary text-white'
                      : active
                        ? 'border-accent bg-accent/15 text-accent'
                        : 'border-black/10 bg-white/70 text-text-light'
                }`}
              >
                {node.skippable ? (
                  <i className="fa-solid fa-forward-step" />
                ) : done ? (
                  <i className="fa-solid fa-check" />
                ) : (
                  index + 1
                )}
              </span>
              <span
                className={`truncate text-[11px] ${
                  node.skippable
                    ? 'text-amber-600'
                    : active
                      ? 'font-semibold text-accent'
                      : 'text-text-light'
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

// ==================== 子组件：车次卡片 ====================

/** 余票状态配色：无票/候补走红黄，有票走主色 */
function ticketStatusClass(status: string): string {
  if (status.includes('无票')) return 'bg-red-50 text-red-500 border-red-200';
  if (status.includes('候补')) return 'bg-amber-50 text-amber-600 border-amber-200';
  return 'bg-primary/10 text-primary border-primary/20';
}

interface TrainCardProps {
  train: TrainOption;
  /** 是否已选中（选中高亮） */
  selected: boolean;
  onPick: (optionId: string) => void;
}

function TrainCard({ train, selected, onPick }: TrainCardProps) {
  // [MERGE] 选中身份用 option_id：车次号在同段可重复（如两趟 G1308），用 trainNo 会同时勾选
  return (
    <button
      type="button"
      onClick={() => onPick(train.option_id)}
      aria-pressed={selected}
      className={`w-full rounded-md border p-4 text-left transition-all duration-200 ${
        selected
          ? 'border-primary bg-primary/5 shadow-icon'
          : 'border-black/10 bg-white/70 hover:border-primary/50 hover:bg-white'
      }`}
    >
      {/* 第一行：车次号 + 余票状态 */}
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="font-serif text-lg font-bold text-text-serif">
          {train.trainNo}
          {selected && <i className="fa-solid fa-circle-check ml-2 text-sm text-primary" />}
        </span>
        <span className={`rounded-full border px-2 py-0.5 text-[11px] ${ticketStatusClass(train.ticketStatus)}`}>
          {train.ticketStatus}
        </span>
      </div>

      {/* 第二行：站点与时刻 */}
      <div className="flex items-center gap-3 text-sm text-text-main">
        <span className="shrink-0 text-right">
          <span className="block font-semibold">{train.startTime}</span>
          <span className="block text-[11px] text-text-light">{train.startStation}</span>
        </span>
        <span className="flex min-w-0 flex-1 flex-col items-center">
          <span className="text-[11px] text-text-light">{train.duration}</span>
          <span className="flex w-full items-center gap-1 text-text-light/60">
            <span className="h-px flex-1 bg-current" />
            <i className="fa-solid fa-train-subway text-[10px]" />
            <span className="h-px flex-1 bg-current" />
          </span>
        </span>
        <span className="shrink-0">
          <span className="block font-semibold">{train.endTime}</span>
          <span className="block text-[11px] text-text-light">{train.endStation}</span>
        </span>
      </div>

      {/* 第三行：座位与票价 */}
      <div className="mt-2 flex items-center justify-between text-xs text-text-light">
        <span>{train.seatType}</span>
        <span className="font-semibold text-accent">¥{train.price}</span>
      </div>
    </button>
  );
}

// ==================== 主组件 ====================

function TrainMultiSegment({
  data,
  onSubmit,
  submitting = false,
  submitted = false,
  initialSelections,
}: TrainMultiSegmentProps) {
  const segments = data.segments;

  /** 状态机当前阶段（交互规则的唯一流向控制；段数由 segments.length 决定） */
  const [stage, setStage] = useState<TrainStage>(submitted ? 'submit' : 'select');
  /** 当前段下标 0 … N-1（预览阶段保持在末段，便于【上一步】回到末段改选） */
  const [stepIndex, setStepIndex] = useState(0);
  /** 选择账本：segment_id → option_id */
  const [selections, setSelections] = useState<TrainSelectionMap>(initialSelections ?? {});

  const isPreview = stage === 'preview' || stage === 'submit';
  const isLastStep = stepIndex >= segments.length - 1;
  const currentSegment = segments[stepIndex];
  const currentSelection = currentSegment
    ? selections[currentSegment.segment_id]
    : undefined;
  /** 该段本次实时无候选（查询超时/失败偶发，不是"该段没车"）：可直接跳过，绝不代用户瞎选 */
  const currentEmpty = Boolean(currentSegment) && currentSegment.train_list.length === 0;

  /** 全部「有候选」的段已选 + 未提交 → 才允许提交（空段不参与，由后端占位放行） */
  const allSelected = segments.every(
    (segment) =>
      segment.train_list.length === 0 || Boolean(selections[segment.segment_id]),
  );
  const canSubmit = allSelected && !submitting && stage !== 'submit' && !submitted;

  /** 选中车次：只写账本（值为 option_id，阶段恒为 select，无需额外状态跃迁） */
  const handlePick = useCallback((segmentId: string, optionId: string) => {
    setSelections((prev) => ({ ...prev, [segmentId]: optionId }));
  }, []);

  /** 【下一步】：本步未选且**有候选**才返回（按钮已置灰）；空段可直接跳过；第 N 段之后进入预览页 */
  const handleNext = useCallback(() => {
    if (!currentSelection && !currentEmpty) return;
    if (isLastStep) {
      setStage('preview');
      return;
    }
    setStepIndex((prev) => Math.min(prev + 1, segments.length - 1));
  }, [currentSelection, currentEmpty, isLastStep, segments.length]);

  /** 【上一步】：从预览回到末段改选；段内回退一段（第 1 步时按钮置灰） */
  const handlePrev = useCallback(() => {
    if (isPreview) {
      setStage('select');
      return;
    }
    setStepIndex((prev) => Math.max(0, prev - 1));
  }, [isPreview]);

  /** 【提交确认】：仅预览页可用，一次性抛出选中段（空段整条剔除，不带空 option_id 给后端） */
  const handleSubmit = useCallback(() => {
    if (!canSubmit) return;
    setStage('submit');
    onSubmit({
      skill_name: TRAIN_MULTI_SEGMENT_SKILL,
      // [MERGE] 空段（本次实时无候选）整条不进 segments：带空 option_id 会被后端
      // 以 MISSING_OPTION_ID 拒绝；后端本身对空段不判"漏选"（有候选却没选才判 INCOMPLETE），
      // 放行时以占位文案 + 估算费用计入
      segments: segments
        .filter(
          (segment) =>
            segment.train_list.length > 0 && Boolean(selections[segment.segment_id]),
        )
        .map((segment) => {
          const picked = findTrain(segment, selections[segment.segment_id]);
          return {
            segment_id: segment.segment_id,
            option_id: selections[segment.segment_id] ?? '',
            trainNo: picked?.trainNo ?? '',
          };
        }),
    });
  }, [canSubmit, onSubmit, segments, selections]);

  /** 预览页合计票价（仅汇总已返回的 price，不做任何重新查询） */
  const pickedTotal = useMemo(
    () =>
      segments.reduce(
        (sum, segment) =>
          sum + (findTrain(segment, selections[segment.segment_id])?.price ?? 0),
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
            <i className="fa-solid fa-train mr-2 text-accent" />
            多段高铁行程
          </h3>
          <p className="mt-1 text-xs text-text-light">
            共 {segments.length} 段 · 分 {segments.length + 1} 步完成选择 ·
            后端已一次性并行查询全部段班次
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
              /* 本次实时无候选（查询超时/失败偶发）：只提示、不阻塞——可直接跳过，费用按估算计入 */
              <p className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-700">
                该段暂未查询到实时班次，可直接跳过，费用将按估算计入。
              </p>
            ) : (
              currentSegment.train_list.map((train) => (
                <TrainCard
                  key={train.option_id}
                  train={train}
                  selected={currentSelection === train.option_id}
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
            const train = findTrain(segment, selections[segment.segment_id]);
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
                {train ? (
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-text-main">
                    <span className="font-serif font-bold text-text-serif">
                      {train.trainNo}
                    </span>
                    <span>
                      {train.startStation} {train.startTime} → {train.endStation}{' '}
                      {train.endTime}
                    </span>
                    <span className="text-xs text-text-light">历时 {train.duration}</span>
                    <span className="ml-auto text-xs text-text-light">{train.seatType}</span>
                    <span className="text-xs font-semibold text-accent">¥{train.price}</span>
                  </div>
                ) : segment.train_list.length === 0 ? (
                  /* 空段：非错误——按"可跳过"处理（提交时不回传该段，后端以占位文案放行） */
                  <p className="text-xs text-amber-600">
                    该段暂未查询到实时班次，已跳过，费用将按估算计入
                  </p>
                ) : (
                  <p className="text-xs text-red-500">
                    该段尚未选择车次，请返回上一步补选
                  </p>
                )}
              </div>
            );
          })}

          {/* 合计票价（仅汇总后端已返回的 price） */}
          <div className="flex items-center justify-between rounded-md bg-primary/5 px-4 py-3 text-sm">
            <span className="text-text-serif">合计票价（单人）</span>
            <span className="font-semibold text-accent">¥{pickedTotal}</span>
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
            disabled={!currentSelection && !currentEmpty}
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

export default memo(TrainMultiSegment);