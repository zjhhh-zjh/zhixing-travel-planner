'use client';

/**
 * 多段住宿选择 UI Skill（skill_name = hotel_multi_segment）
 *
 * 交互规则（与 train_multi_segment 高铁 Skill **完全同构**；段数**动态**）：
 *   1. 后端一次性【并行】拉取全部住宿段候选，前端不做查询。
 *   2. 组件按 payload.segments.length = N 自动生成子步骤（**禁止硬编码段数**）：
 *        第 1 步  选第 1 段住宿 → 选中后【下一步】点亮
 *        ……
 *        第 N 步  选第 N 段住宿 → 【下一步】文案变【预览全部住宿】
 *        第 N+1 步 预览汇总 → 仅此页提供【提交确认】
 *   3. 支持【上一步】回退修改前面任意一段的选择。
 *   4. 取消逐段确认，只在预览页做一次全局提交。
 *   5. 当前子步骤未选中酒店/品牌时【下一步】置灰不可点。
 *   6. **不做任何日期/时间校验**：不比对入住离店先后、不校验与行程是否冲突。
 *
 * 每段独立渲染（段间可混合，如 h1 门店 + h2 品牌）：
 *   - mode='hotel'：门店单选卡片，展示酒店名称、总价、每晚单价、评分、地址；
 *   - mode='brand'：品牌单选卡片，顶部提示「实时酒店报价暂不可用，仅可选择品牌」。
 */

import { memo, useCallback, useMemo, useState } from 'react';
import {
  HOTEL_MULTI_SEGMENT_SKILL,
  type HotelBrand,
  type HotelMultiSegmentSkillData,
  type HotelMultiSegmentSubmitPayload,
  type HotelOption,
  type HotelSegment,
  type HotelSelectionMap,
  type HotelStage,
} from '../train-multi-segment/types';

export interface HotelMultiSegmentProps {
  /** 后端 Skill 出参（并行查询后的结构化数据） */
  data: HotelMultiSegmentSkillData;
  /** 全局提交回调：仅在预览页点击【提交确认】时触发一次 */
  onSubmit: (payload: HotelMultiSegmentSubmitPayload) => void;
  /** 提交中标记（由上层控制，用于置灰提交按钮） */
  submitting?: boolean;
  /** [MERGE] 已提交标记：卡片提交成功后仍保留在对话流中展示，此时锁定交互（上一步/提交均禁用） */
  submitted?: boolean;
  /** [MERGE] 已提交前的选中账本（切回会话时复原预览） */
  initialSelections?: Record<string, string>;
}

// ==================== 取数工具 ====================

/** 按选中 id 在该段取门店；段内 id 形如 h1-1，**段间不共号**，故必须限定在段内查 */
function findHotel(segment: HotelSegment, selectedId?: string): HotelOption | undefined {
  if (!selectedId) return undefined;
  return segment.hotel_list.find((item) => item.hotel_id === selectedId);
}

/** 按选中 id 在该段取品牌 */
function findBrand(segment: HotelSegment, selectedId?: string): HotelBrand | undefined {
  if (!selectedId) return undefined;
  return segment.brand_list.find((item) => item.brand_id === selectedId);
}

/** 选中项的展示名（门店名或品牌名），供预览页与提交流程复用 */
function pickedName(segment: HotelSegment, selectedId?: string): string {
  if (segment.mode === 'hotel') return findHotel(segment, selectedId)?.hotelName ?? '';
  return findBrand(segment, selectedId)?.brandName ?? '';
}

/** 该段是否无任何候选（后端整段降级时的兜底展示，绝不给用户瞎选） */
function isEmptySegment(segment: HotelSegment): boolean {
  return segment.mode === 'hotel' ? segment.hotel_list.length === 0 : segment.brand_list.length === 0;
}

// ==================== 子组件：进度条 ====================

/** 进度条节点：按 segments 动态生成 N 个住宿段节点 + 1 个预览节点（总步数 = N+1） */
function SkillProgress({
  segments,
  activeIndex,
}: {
  segments: HotelSegment[];
  /** 当前激活节点下标；段选择阶段为段下标，预览阶段为 segments.length */
  activeIndex: number;
}) {
  const nodes = useMemo(
    () => [
      ...segments.map((segment, index) => ({
        key: segment.segment_id,
        label: `第 ${index + 1} 站`,
        sub: segment.city,
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
              <span className={`mb-7 h-px flex-1 ${done ? 'bg-primary/40' : 'bg-black/10'}`} />
            )}
          </li>
        );
      })}
    </ol>
  );
}

// ==================== 子组件：候选卡片（门店 / 品牌两种形态） ====================

interface CardShellProps {
  /** 是否已选中（选中高亮） */
  selected: boolean;
  onPick: () => void;
  children: React.ReactNode;
}

/** 单选卡外壳：门店与品牌共用同一套选中态样式，保证两种模式视觉一致 */
function CardShell({ selected, onPick, children }: CardShellProps) {
  return (
    <button
      type="button"
      onClick={onPick}
      aria-pressed={selected}
      className={`w-full rounded-md border p-4 text-left transition-all duration-200 ${
        selected
          ? 'border-primary bg-primary/5 shadow-icon'
          : 'border-black/10 bg-white/70 hover:border-primary/50 hover:bg-white'
      }`}
    >
      {children}
    </button>
  );
}

/** 门店卡（mode='hotel'）：名称 + 总价 + 每晚单价 + 评分 + 地址 */
function HotelCard({
  hotel,
  selected,
  onPick,
}: {
  hotel: HotelOption;
  selected: boolean;
  onPick: () => void;
}) {
  // 选中身份用 hotel_id（段内唯一，形如 h1-1），与后端提交校验口径一致
  return (
    <CardShell selected={selected} onPick={onPick}>
      {/* 第一行：门店名 + 评分 */}
      <div className="mb-2 flex items-start justify-between gap-2">
        <span className="font-serif text-base font-bold leading-snug text-text-serif">
          {hotel.hotelName}
          {selected && <i className="fa-solid fa-circle-check ml-2 text-sm text-primary" />}
        </span>
        <span className="shrink-0 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[11px] text-amber-600">
          <i className="fa-solid fa-star mr-1" />
          {hotel.starRating}
        </span>
      </div>

      {/* 第二行：地址 */}
      <p className="mb-2 text-xs text-text-light">
        <i className="fa-solid fa-location-dot mr-1" />
        {hotel.address || '地址未返回'}
      </p>

      {/* 第三行：总价 + 每晚单价 */}
      <div className="flex items-end justify-between text-xs text-text-light">
        <span>
          每晚 <span className="font-semibold text-text-main">¥{hotel.pricePerNight}</span>
        </span>
        <span>
          总价{' '}
          <span className="font-serif text-lg font-bold text-accent">
            ¥{hotel.totalPrice}
          </span>
        </span>
      </div>
    </CardShell>
  );
}

/** 品牌卡（mode='brand'）：只展示品牌名（无门店、无实时报价，绝不编造价格） */
function BrandCard({
  brand,
  priceLevel,
  selected,
  onPick,
}: {
  brand: HotelBrand;
  priceLevel: string;
  selected: boolean;
  onPick: () => void;
}) {
  return (
    <CardShell selected={selected} onPick={onPick}>
      <div className="flex items-center justify-between gap-2">
        <span className="font-serif text-base font-bold text-text-serif">
          {brand.brandName}
          {selected && <i className="fa-solid fa-circle-check ml-2 text-sm text-primary" />}
        </span>
        <span className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] text-primary">
          {priceLevel}
        </span>
      </div>
      <p className="mt-1 text-xs text-text-light">
        具体门店与价格到店/官网再定，同档位择优
      </p>
    </CardShell>
  );
}

// ==================== 主组件 ====================

function HotelMultiSegment({
  data,
  onSubmit,
  submitting = false,
  submitted = false,
  initialSelections,
}: HotelMultiSegmentProps) {
  const segments = data.segments;

  /** 状态机当前阶段（交互规则唯一流向控制；段数由 segments.length 决定） */
  const [stage, setStage] = useState<HotelStage>(submitted ? 'submit' : 'select');
  /** 当前段下标 0 … N-1（预览阶段保持在末段，便于【上一步】回到末段改选） */
  const [stepIndex, setStepIndex] = useState(0);
  /** 选择账本：segment_id → 选中的 hotel_id / brand_id */
  const [selections, setSelections] = useState<HotelSelectionMap>(initialSelections ?? {});

  const isPreview = stage === 'preview' || stage === 'submit';
  const isLastStep = stepIndex >= segments.length - 1;
  const currentSegment = segments[stepIndex];
  const currentSelection = currentSegment ? selections[currentSegment.segment_id] : undefined;
  /** 该段后端没有任何候选：提示不可继续，但绝不代用户瞎选 */
  const currentEmpty = Boolean(currentSegment) && isEmptySegment(currentSegment);

  /** 全部段已选 + 未提交 → 才允许提交 */
  const allSelected = segments.every((segment) => Boolean(selections[segment.segment_id]));
  const canSubmit = allSelected && !submitting && stage !== 'submit' && !submitted;

  /** 选中候选：只写账本（值为该段候选 id，阶段恒为 select） */
  const handlePick = useCallback((segmentId: string, candidateId: string) => {
    setSelections((prev) => ({ ...prev, [segmentId]: candidateId }));
  }, []);

  /** 【下一步】：本步未选则返回（按钮已置灰）；第 N 段之后进入预览页 */
  const handleNext = useCallback(() => {
    if (!currentSelection) return;
    if (isLastStep) {
      setStage('preview');
      return;
    }
    setStepIndex((prev) => Math.min(prev + 1, segments.length - 1));
  }, [currentSelection, isLastStep, segments.length]);

  /** 【上一步】：从预览回到末段改选；段内回退一段（第 1 步时按钮置灰） */
  const handlePrev = useCallback(() => {
    if (isPreview) {
      setStage('select');
      return;
    }
    setStepIndex((prev) => Math.max(0, prev - 1));
  }, [isPreview]);

  /** 【提交确认】：仅预览页可用，一次性抛出全部段的 mode + 选中 id（hotel_id / brand_id） */
  const handleSubmit = useCallback(() => {
    if (!canSubmit) return;
    setStage('submit');
    onSubmit({
      skill_name: HOTEL_MULTI_SEGMENT_SKILL,
      segments: segments.map((segment) => {
        const picked = selections[segment.segment_id] ?? '';
        return {
          segment_id: segment.segment_id,
          mode: segment.mode,
          hotel_id: segment.mode === 'hotel' ? picked : '',
          brand_id: segment.mode === 'brand' ? picked : '',
        };
      }),
    });
  }, [canSubmit, onSubmit, segments, selections]);

  /** 预览页合计（仅统计 mode='hotel' 已选段的实时总价；品牌段无报价，不计入也不臆造） */
  const pickedTotal = useMemo(
    () =>
      segments.reduce(
        (sum, segment) => sum + (findHotel(segment, selections[segment.segment_id])?.totalPrice ?? 0),
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
            <i className="fa-solid fa-hotel mr-2 text-accent" />
            多段住宿选择
          </h3>
          <p className="mt-1 text-xs text-text-light">
            共 {segments.length} 段住宿 · 分 {segments.length + 1} 步完成选择 ·
            后端已一次性并行查询全部城市候选
          </p>
        </div>
        <span className="shrink-0 rounded-full bg-primary/10 px-3 py-1 text-[11px] text-primary">
          {data.skill_name}
        </span>
      </header>

      {/* 进度条：段选择阶段指向当前段；预览阶段指向预览节点（末位） */}
      <SkillProgress segments={segments} activeIndex={isPreview ? segments.length : stepIndex} />

      {/* 主体一：逐段选择（第 1 ~ N 步，N 由 segments.length 决定） */}
      {!isPreview && (
        <div>
          {/* 该段住宿信息：城市 / 入住离店日期 / 档位 / 人数 */}
          <div className="mb-3 rounded-md bg-primary/5 px-4 py-3">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
              <span className="rounded-full bg-accent/10 px-3 py-0.5 text-xs font-semibold text-accent">
                Step {stepIndex + 1} / {segments.length}
              </span>
              <span className="font-semibold text-text-serif">
                {currentSegment.city}
                <span className="ml-2 text-xs font-normal text-text-light">
                  {currentSegment.mode === 'hotel' ? '门店候选' : '品牌候选'}
                </span>
              </span>
              <span className="text-xs text-text-light">
                <i className="fa-solid fa-calendar-days mr-1" />
                {currentSegment.checkInDate} 入住 · {currentSegment.checkOutDate} 离店
              </span>
              <span className="text-xs text-text-light">
                {currentSegment.priceLevel} · {currentSegment.guestCount} 人
              </span>
            </div>
          </div>

          {/* 明确标注本步候选所属城市：用户必须一眼看出"这几家酒店是哪个地方的" */}
          <h4 className="mb-3 font-serif text-base text-text-serif">
            第 {stepIndex + 1} 站 · {currentSegment.city} 的
            {currentSegment.mode === 'hotel' ? '酒店门店候选' : '酒店品牌候选'}
          </h4>

          {/* 品牌模式提示：实时报价不可用，只能选品牌 */}
          {currentSegment.mode === 'brand' && (
            <p className="mb-3 rounded-md border border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-700">
              <i className="fa-solid fa-triangle-exclamation mr-1" />
              实时酒店报价暂不可用，仅可选择品牌
            </p>
          )}

          <div className="flex flex-col gap-3">
            {currentEmpty ? (
              /* 该段无任何候选（整段降级）：如实告知，不提供任何替代候选 */
              <p className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-700">
                该段暂未查询到住宿候选，请返回上一步或稍后在对话中重新查询。
              </p>
            ) : currentSegment.mode === 'hotel' ? (
              currentSegment.hotel_list.map((hotel) => (
                <HotelCard
                  key={hotel.hotel_id}
                  hotel={hotel}
                  selected={currentSelection === hotel.hotel_id}
                  onPick={() => handlePick(currentSegment.segment_id, hotel.hotel_id)}
                />
              ))
            ) : (
              currentSegment.brand_list.map((brand) => (
                <BrandCard
                  key={brand.brand_id}
                  brand={brand}
                  priceLevel={currentSegment.priceLevel}
                  selected={currentSelection === brand.brand_id}
                  onPick={() => handlePick(currentSegment.segment_id, brand.brand_id)}
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
            const selectedId = selections[segment.segment_id];
            const hotel = findHotel(segment, selectedId);
            const brand = findBrand(segment, selectedId);
            return (
              <div
                key={segment.segment_id}
                className="rounded-md border border-black/10 bg-white/70 p-4"
              >
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <span className="text-sm font-semibold text-text-serif">
                    第 {index + 1} 站 · {segment.city}
                  </span>
                  <span className="text-xs text-text-light">
                    {segment.checkInDate} 至 {segment.checkOutDate} · {segment.priceLevel}
                  </span>
                </div>
                {hotel ? (
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-text-main">
                    <span className="font-serif font-bold text-text-serif">{hotel.hotelName}</span>
                    <span className="text-xs text-text-light">★ {hotel.starRating}</span>
                    <span className="text-xs text-text-light">{hotel.address}</span>
                    <span className="ml-auto text-xs text-text-light">每晚 ¥{hotel.pricePerNight}</span>
                    <span className="text-xs font-semibold text-accent">总价 ¥{hotel.totalPrice}</span>
                  </div>
                ) : brand ? (
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-text-main">
                    <span className="font-serif font-bold text-text-serif">{brand.brandName}</span>
                    <span className="text-xs text-text-light">
                      （该城市实时报价不可用，仅选定品牌，具体门店到店择优选同档位）
                    </span>
                  </div>
                ) : (
                  <p className="text-xs text-red-500">该段尚未选择住宿，请返回上一步补选</p>
                )}
              </div>
            );
          })}

          {/* 合计：仅统计门店模式已选段（品牌段无报价，不计入） */}
          {pickedTotal > 0 && (
            <div className="flex items-center justify-between rounded-md bg-primary/5 px-4 py-3 text-sm">
              <span className="text-text-serif">
                合计住宿总价
                <span className="ml-2 text-[11px] text-text-light">
                  （仅统计已给出实时报价的门店段）
                </span>
              </span>
              <span className="font-semibold text-accent">¥{pickedTotal}</span>
            </div>
          )}
        </div>
      )}

      {/* 底部操作区：上一步 / 下一步·预览全部住宿 / 提交确认 */}
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
            disabled={!currentSelection}
            className="rounded-sm bg-primary px-6 py-2 text-sm font-semibold text-white transition-colors duration-200 hover:bg-primary-light disabled:cursor-not-allowed disabled:bg-[#ccc]"
          >
            {isLastStep ? '预览全部住宿' : '下一步'}
            <i className="fa-solid fa-chevron-right ml-1.5" />
          </button>
        )}
      </footer>
    </section>
  );
}

export default memo(HotelMultiSegment);