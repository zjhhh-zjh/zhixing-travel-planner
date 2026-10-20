'use client';

import { memo, useMemo, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import type { AppDispatch, RootState } from '@/store';
import { hidePlan } from '@/store/slices/uiSlice';
import type { ItineraryDay, ScenicSpot } from '@/lib/types';
import DayRouteMap, { type PlanMapPoint } from './PlanMap';

/** 稳定空数组引用，避免 DayRouteMap 因 props 变化重建地图 [MERGE] */
const EMPTY_SPOTS: PlanMapPoint[] = [];

/** 景点图片组：主图 + 副图（最多2张，加载失败自动隐藏）[MERGE] */
function SpotImage({ src, images, name }: { src: string; images: string[]; name: string }) {
  const [failed, setFailed] = useState(false);
  const subs = useMemo(() => images.filter((u) => u && u !== src).slice(0, 2), [images, src]);

  return (
    <div className="flex flex-shrink-0 gap-1">
      {src && !failed ? (
        <img
          src={src}
          alt={name}
          className="h-20 w-20 rounded-sm object-cover"
          loading="lazy"
          onError={() => setFailed(true)}
        />
      ) : (
        <div className="flex h-20 w-20 items-center justify-center rounded-sm bg-primary/10 text-2xl text-primary">
          <i className="fa-solid fa-camera-retro" />
        </div>
      )}
      {subs.length > 0 && (
        <div className="flex flex-col gap-1">
          {subs.map((u) => (
            <img
              key={u}
              src={u}
              alt={`${name}-副图`}
              className="h-[38px] w-[38px] rounded-sm object-cover"
              loading="lazy"
              onError={(e) => {
                e.currentTarget.style.display = 'none';
              }}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * 行程清单弹窗 [MERGE]（对应 knowgo_travel TravelItinerary 渲染契约）
 * - 数据源：GET /api/v1/itinerary-plans/{planId} 的 data（title + itinerary_list）
 * - 结构：天（day，含每日腾讯路线图）→ 时段（time）→ 景点（主图/副图/介绍）
 */
function PlanModal() {
  const dispatch = useDispatch<AppDispatch>();
  const { visible, loading, plan } = useSelector((state: RootState) => state.ui.planModal);

  // [MERGE] 每天有坐标的景点标记点（id → spots），顶层预计算，保证 DayRouteMap 依赖引用稳定
  const daySpots = useMemo<Record<string, PlanMapPoint[]>>(() => {
    const result: Record<string, PlanMapPoint[]> = {};
    (plan?.itinerary_list ?? []).forEach((d) => {
      result[d.id] = d.itinerary.flatMap((item) =>
        item.scenic_spots_list.flatMap((s) =>
          s.longitude != null && s.latitude != null
            ? [{ toponym: s.toponym, lng: s.longitude, lat: s.latitude }]
            : [],
        ),
      );
    });
    return result;
  }, [plan]);

  if (!visible) return null;

  /** 景点卡片 */
  const renderSpot = (spot: ScenicSpot) => (
    <div key={spot.toponym} className="flex gap-3 rounded-sm border border-black/5 bg-sand p-3">
      <SpotImage src={spot.toponym_image} images={spot.toponym_image_list} name={spot.toponym} />
      <div className="min-w-0">
        <h5 className="font-serif text-[15px] font-bold text-text-serif">
          <i className="fa-solid fa-location-dot mr-1 text-accent" />
          {spot.toponym}
        </h5>
        <p className="mt-1 text-[13px] leading-[1.7] text-text-light">{spot.toponym_desc}</p>
      </div>
    </div>
  );

  /** 时段块（含每日路线图） */
  const renderDay = (day: ItineraryDay) => (
    <section key={day.id} className="mb-5">
      <h4 className="mb-2.5 flex items-center gap-2 font-serif text-lg font-bold text-primary">
        <span className="flex h-7 w-7 items-center justify-center rounded-full bg-accent text-xs text-white">
          {day.day.replace(/\D/g, '') || '•'}
        </span>
        {day.day}
      </h4>
      {/* [MERGE] 每日腾讯路线图（有坐标时渲染） */}
      <DayRouteMap label={day.day} spots={daySpots[day.id] ?? EMPTY_SPOTS} legs={day.route_polylines} />
      <div className="flex flex-col gap-3">
        {day.itinerary.map((item, i) => (
          <div key={`${day.id}-${item.time}-${i}`}>
            <span className="mb-1.5 inline-block rounded-full bg-accent/10 px-3 py-0.5 text-xs font-semibold text-accent">
              {item.time}
            </span>
            <div className="flex flex-col gap-2">
              {item.scenic_spots_list.map(renderSpot)}
            </div>
          </div>
        ))}
      </div>
    </section>
  );

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-[rgba(26,77,84,0.3)] backdrop-blur-overlay"
      onClick={() => dispatch(hidePlan())}
    >
      <div
        className="glass-card flex max-h-[85vh] w-[640px] max-w-[92vw] flex-col overflow-hidden rounded-lg bg-white/95"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部：标题 + 关闭 */}
        <header className="flex items-center justify-between border-b border-black/5 px-6 py-4">
          <h3 className="flex items-center gap-2 font-serif text-xl text-text-serif">
            <i className="fa-solid fa-route text-accent" />
            {plan?.title || '行程清单'}
          </h3>
          <button
            type="button"
            onClick={() => dispatch(hidePlan())}
            title="关闭"
            className="cursor-pointer border-none bg-transparent p-1.5 text-text-light transition-colors hover:text-primary"
          >
            <i className="fa-solid fa-xmark text-lg" />
          </button>
        </header>

        {/* 内容区 [MERGE]：后端落库即增强后完整数据（修复 34），直接渲染；缺图景点有占位图标 */}
        <div className="thin-scroll flex-1 overflow-y-auto p-6">
          {loading ? (
            <div className="flex flex-col items-center gap-3 py-16 text-text-light">
              <i className="fa-solid fa-spinner fa-spin text-2xl text-primary" />
              <span className="text-sm">行程清单加载中...</span>
            </div>
          ) : plan?.itinerary_list?.length ? (
            plan.itinerary_list.map(renderDay)
          ) : (
            <div className="py-16 text-center text-sm text-text-light">
              行程清单内容为空
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default memo(PlanModal);
