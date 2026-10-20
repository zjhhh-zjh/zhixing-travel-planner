'use client';

/**
 * 多段自驾行程 UI Skill（skill_name = driving_multi_segment）[MERGE] 修复 156
 *
 * 交互规则（与多段高铁/航班同构；段数**动态**，但为**只读确认**）：
 *   1. 后端一次性【并行】拉取全部段自驾路线，前端不做查询。
 *   2. 每段只给**一条**高德默认策略的最优路线，**用户不能选路线策略**，只能整批【确认】。
 *   3. 组件按 payload.segments.length = N 自动生成子步骤（**禁止硬编码段数**）：
 *        第 1 步  核对第 1 段路线 → 该段 ok 时【下一步】点亮
 *        ……
 *        第 N 步  核对第 N 段路线 → 【下一步】文案变【预览全部行程】
 *        第 N+1 步 预览汇总 → 仅此页提供【提交确认】
 *   4. 支持【上一步】回退查看前面各段。
 *   5. 段"已就绪"判定 = ok === true：未就绪段禁止推进，并在卡片上说明原因（前端置灰）。
 *   6. **不做任何时间/路线校验**：不比对段时间先后、不校验路线可行性，界面也不出现冲突提示。
 */

import { memo, useCallback, useMemo, useState } from 'react';
import {
  DRIVING_MULTI_SEGMENT_SKILL,
  type DrivingMultiSegmentSkillData,
  type DrivingMultiSegmentSubmitPayload,
  type DrivingSegment,
  type DrivingStage,
} from './types';

// [MERGE] 修复 156：Skill 注册键在本文件再导出一次（唯一真源仍在 ./types），
// 便于阅读组件时一眼看到 skill_name，也满足"组件文件内可见 DRIVING_MULTI_SEGMENT_SKILL"
export { DRIVING_MULTI_SEGMENT_SKILL };

export interface DrivingMultiSegmentProps {
  /** 后端 Skill 出参（并行查询后的结构化数据） */
  data: DrivingMultiSegmentSkillData;
  /** 全局提交回调：仅在预览页点击【提交确认】时触发一次 */
  onSubmit: (payload: DrivingMultiSegmentSubmitPayload) => void;
  /** 提交中标记（由上层控制，用于置灰提交按钮） */
  submitting?: boolean;
  /** [MERGE] 已提交标记：卡片提交成功后仍保留在对话流中展示，此时锁定交互（上一步/提交均禁用） */
  submitted?: boolean;
  /** [MERGE] 已提交前的选中账本（切回会话时复原预览） */
  initialSelections?: Record<string, string>;
}

// ==================== 展示工具 ====================

/** 距离展示：未解析到（<=0）时显示"距离未知"，绝不展示 0km 造成误导 */
function distanceText(km: number): string {
  return km > 0 ? `${km} km` : '距离未知';
}

/** 耗时展示：未解析到时显示"—" */
function durationText(text: string): string {
  return text || '—';
}

/** 过路费展示：未解析到或免费（<=0）时显示"免费/未知" */
function tollsText(tolls: number): string {
  return tolls > 0 ? `¥${tolls}` : '免费/未知';
}

/** 未就绪原因（该段 ok=false：城市未收录坐标或高德未返回结果） */
const NOT_READY_REASON =
  '该段实时自驾路线暂时查询不到（城市未收录坐标或高德未返回结果），请返回上一步或稍后在对话中重新查询。';

// ==================== 子组件：进度条 ====================

/** 进度条节点：按 segments 动态生成 N 个段节点 + 1 个预览节点（总步数 = N+1） */
function SkillProgress({
  segments,
  activeIndex,
}: {
  segments: DrivingSegment[];
  /** 当前激活节点下标；段核对阶段为段下标，预览阶段为 segments.length */
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

// ==================== 子组件：路线信息卡（只读，无选项） ====================

function RouteCard({ segment }: { segment: DrivingSegment }) {
  return (
    <div className="rounded-md border border-black/10 bg-white/70 p-4">
      {/* 第一行：里程 + 耗时 */}
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="font-serif text-lg font-bold text-text-serif">
          <i className="fa-solid fa-car mr-2 text-sm text-accent" />
          {distanceText(segment.distance_km)}
        </span>
        <span className="text-xs font-semibold text-accent">
          预计 {durationText(segment.duration_text)}
        </span>
      </div>

      {/* 第二行：过路费 + 红绿灯 */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text-light">
        <span>
          <i className="fa-solid fa-money-bill-wave mr-1" />
          过路费 {tollsText(segment.tolls)}
        </span>
        {segment.traffic_lights > 0 && (
          <span>
            <i className="fa-solid fa-traffic-light mr-1" />
            红绿灯 {segment.traffic_lights} 个
          </span>
        )}
        <span className="ml-auto rounded-full bg-primary/10 px-2 py-0.5 text-[10px] text-primary">
          高德默认策略最优路线
        </span>
      </div>

      {/* 第三行：关键路段摘要（有则展示） */}
      {segment.summary && (
        <p className="mt-2 border-t border-black/5 pt-2 text-xs text-text-main">
          <span className="text-text-light">途经：</span>
          {segment.summary}
        </p>
      )}
    </div>
  );
}

// ==================== 主组件 ====================

function DrivingMultiSegment({
  data,
  onSubmit,
  submitting = false,
  submitted = false,
}: DrivingMultiSegmentProps) {
  const segments = data.segments;

  /** 状态机当前阶段（交互规则的唯一流向控制；段数由 segments.length 决定） */
  const [stage, setStage] = useState<DrivingStage>(submitted ? 'submit' : 'select');
  /** 当前段下标 0 … N-1（预览阶段保持在末段，便于【上一步】回到末段查看） */
  const [stepIndex, setStepIndex] = useState(0);

  const isPreview = stage === 'preview' || stage === 'submit';
  const isLastStep = stepIndex >= segments.length - 1;
  const currentSegment = segments[stepIndex];
  /** 该段后端未查到路线（ok=false）：提示不可继续，且绝不代用户瞎推进 */
  const currentNotReady = Boolean(currentSegment) && !currentSegment.ok;

  /** 全部段已就绪（ok）+ 未提交 → 才允许提交 */
  const allReady = segments.length > 0 && segments.every((segment) => segment.ok);
  const canSubmit = allReady && !submitting && stage !== 'submit' && !submitted;

  /** 【下一步】：本段未就绪则返回（按钮已置灰）；第 N 段之后进入预览页 */
  const handleNext = useCallback(() => {
    if (!currentSegment?.ok) return;
    if (isLastStep) {
      setStage('preview');
      return;
    }
    setStepIndex((prev) => Math.min(prev + 1, segments.length - 1));
  }, [currentSegment, isLastStep, segments.length]);

  /** 【上一步】：从预览回到末段查看；段内回退一段（第 1 步时按钮置灰） */
  const handlePrev = useCallback(() => {
    if (isPreview) {
      setStage('select');
      return;
    }
    setStepIndex((prev) => Math.max(0, prev - 1));
  }, [isPreview]);

  /** 【提交确认】：仅预览页可用，一次性抛出全部段的 segment_id（只读确认，无策略可选） */
  const handleSubmit = useCallback(() => {
    if (!canSubmit) return;
    setStage('submit');
    onSubmit({
      skill_name: DRIVING_MULTI_SEGMENT_SKILL,
      segments: segments.map((segment) => ({ segment_id: segment.segment_id })),
    });
  }, [canSubmit, onSubmit, segments]);

  /** 预览页合计：仅汇总后端已返回的数值（未解析到的按 0，不编造） */
  const totals = useMemo(
    () =>
      segments.reduce(
        (acc, segment) => ({
          km: acc.km + (segment.distance_km > 0 ? segment.distance_km : 0),
          tolls: acc.tolls + (segment.tolls > 0 ? segment.tolls : 0),
        }),
        { km: 0, tolls: 0 },
      ),
    [segments],
  );

  return (
    <section className="glass-card w-full rounded-lg bg-white/85 p-6">
      {/* 标题区：Skill 标识 + 并行查询说明 */}
      <header className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h3 className="font-serif text-xl text-text-serif">
            <i className="fa-solid fa-car-side mr-2 text-accent" />
            多段自驾行程
          </h3>
          <p className="mt-1 text-xs text-text-light">
            共 {segments.length} 段 · 分 {segments.length + 1} 步完成确认 ·
            后端已一次性并行查询全部段路线（每段默认策略最优路线，不可选策略）
          </p>
        </div>
        <span className="shrink-0 rounded-full bg-primary/10 px-3 py-1 text-[11px] text-primary">
          {data.skill_name}
        </span>
      </header>

      {/* 进度条：段核对阶段指向当前段；预览阶段指向预览节点（末位） */}
      <SkillProgress
        segments={segments}
        activeIndex={isPreview ? segments.length : stepIndex}
      />

      {/* 主体一：逐段核对（第 1 ~ N 步，N 由 segments.length 决定） */}
      {!isPreview && currentSegment && (
        <div>
          <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
            <span className="rounded-full bg-accent/10 px-3 py-0.5 text-xs font-semibold text-accent">
              Step {stepIndex + 1} / {segments.length}
            </span>
            <span className="font-semibold text-text-serif">
              {currentSegment.from} → {currentSegment.to}
            </span>
            {currentSegment.date && (
              <span className="text-xs text-text-light">{currentSegment.date}</span>
            )}
          </div>
          {currentNotReady ? (
            /* 该段查询失败降级：如实告知，不提供任何替代路线，禁止推进 */
            <p className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-700">
              {NOT_READY_REASON}
            </p>
          ) : (
            <RouteCard segment={currentSegment} />
          )}
        </div>
      )}

      {/* 主体二：预览汇总（第 N+1 步，唯一提供【提交确认】的页面） */}
      {isPreview && (
        <div className="flex flex-col gap-3">
          {segments.map((segment, index) => (
            <div
              key={segment.segment_id}
              className="rounded-md border border-black/10 bg-white/70 p-4"
            >
              <div className="mb-2 flex items-center justify-between">
                <span className="text-sm font-semibold text-text-serif">
                  第 {index + 1} 段 · {segment.from} → {segment.to}
                </span>
                {segment.date && (
                  <span className="text-xs text-text-light">{segment.date}</span>
                )}
              </div>
              {segment.ok ? (
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-text-main">
                  <span className="font-serif font-bold text-text-serif">
                    {distanceText(segment.distance_km)}
                  </span>
                  <span>预计 {durationText(segment.duration_text)}</span>
                  <span className="text-xs text-text-light">
                    过路费 {tollsText(segment.tolls)}
                  </span>
                  {segment.traffic_lights > 0 && (
                    <span className="text-xs text-text-light">
                      红绿灯 {segment.traffic_lights} 个
                    </span>
                  )}
                </div>
              ) : (
                <p className="text-xs text-red-500">
                  该段实时路线未查询到，请返回上一步查看原因
                </p>
              )}
            </div>
          ))}

          {/* 合计（仅汇总后端已返回的数值） */}
          <div className="flex items-center justify-between rounded-md bg-primary/5 px-4 py-3 text-sm">
            <span className="text-text-serif">合计（里程 / 过路费）</span>
            <span className="font-semibold text-accent">
              {totals.km > 0 ? `${Math.round(totals.km * 10) / 10} km` : '距离未知'} ·{' '}
              {tollsText(totals.tolls)}
            </span>
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
            disabled={!currentSegment?.ok}
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

export default memo(DrivingMultiSegment);