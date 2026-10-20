'use client';

import { memo, useEffect, useRef, useState } from 'react';

/** 行程地图标记点（PlanModal 由 plan 预计算，保证引用稳定） */
export interface PlanMapPoint {
  toponym: string;
  lng: number;
  lat: number;
}

/**
 * 每日路线地图 [MERGE]：腾讯位置服务 JavaScript API GL
 * - 一天一张图：按景点顺序画驾车路线（后端存压缩 polyline），无路线段回退直线
 * - 数据坐标来自高德 POI 补全（GCJ-02），与腾讯地图同坐标系，直接渲染
 * - 脚本按需加载 + 单例缓存；销毁时释放地图实例
 * - Key 未开启 JSAPI / 域名未白名单 / 网络失败 → 显示降级提示
 */

/** TMap GL 最小类型集（仅覆盖本组件用到的 API 面） */
interface TMapLatLng {
  lat: number;
  lng: number;
}
interface TMapBounds {
  extend(ll: TMapLatLng): void;
}
interface TMapMap {
  fitBounds(bounds: TMapBounds, opts?: { padding?: number }): void;
  destroy(): void;
}
interface TMapNS {
  Map: new (el: HTMLElement, opts: { center: TMapLatLng; zoom?: number }) => TMapMap;
  LatLng: new (lat: number, lng: number) => TMapLatLng;
  LatLngBounds: new () => TMapBounds;
  MultiMarker: new (opts: { map: TMapMap; geometries: { id: string; position: TMapLatLng; content?: string }[] }) => unknown;
  MultiPolyline: new (opts: { map: TMapMap; geometries: { id: string; paths: TMapLatLng[] }[] }) => unknown;
}

declare global {
  interface Window {
    TMap?: TMapNS;
  }
}

let tmapLoader: Promise<TMapNS> | null = null;

/** 动态注入 GL 脚本（单例 Promise，避免重复加载） */
function loadTMap(): Promise<TMapNS> {
  if (window.TMap) return Promise.resolve(window.TMap);
  if (!tmapLoader) {
    const key = process.env.NEXT_PUBLIC_QQ_MAP_KEY;
    if (!key) return Promise.reject(new Error('未配置 NEXT_PUBLIC_QQ_MAP_KEY'));
    tmapLoader = new Promise<TMapNS>((resolve, reject) => {
      const script = document.createElement('script');
      script.src = `https://map.qq.com/api/gljs?v=1.exp&key=${key}`;
      script.async = true;
      script.onload = () =>
        window.TMap ? resolve(window.TMap) : reject(new Error('TMap 全局对象缺失'));
      script.onerror = () => reject(new Error('腾讯地图脚本加载失败，请检查网络'));
      document.head.appendChild(script);
    });
  }
  return tmapLoader;
}

/** 每段路线平铺坐标 [lat,lng,...] → LatLng 数组 [MERGE] */
function toPathPoints(flat: number[], LatLngCtor: new (lat: number, lng: number) => TMapLatLng): TMapLatLng[] {
  const pts: TMapLatLng[] = [];
  for (let i = 0; i + 1 < flat.length; i += 2) {
    pts.push(new LatLngCtor(flat[i], flat[i + 1]));
  }
  return pts;
}

function DayRouteMap({ label, spots, legs }: { label: string; spots: PlanMapPoint[]; legs?: number[][] }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<TMapMap | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (spots.length === 0) return;
    let cancelled = false;

    loadTMap()
      .then((TMap) => {
        if (cancelled || !containerRef.current) return;
        const first = new TMap.LatLng(spots[0].lat, spots[0].lng);
        const map = new TMap.Map(containerRef.current, { center: first, zoom: 12 });
        mapRef.current = map;

        const bounds = new TMap.LatLngBounds();
        const allPaths: TMapLatLng[][] = [];

        // 路线：有平铺坐标的段画真实路线，否则回退直线
        spots.forEach((s, i) => {
          const ll = new TMap.LatLng(s.lat, s.lng);
          bounds.extend(ll);
          if (i > 0) {
            const prev = spots[i - 1];
            const flat = legs?.[i - 1];
            if (flat && flat.length > 3) {
              allPaths.push(toPathPoints(flat, TMap.LatLng));
            } else {
              allPaths.push([new TMap.LatLng(prev.lat, prev.lng), ll]);
            }
          }
        });

        new TMap.MultiMarker({
          map,
          geometries: spots.map((s, i) => ({
            id: String(i),
            position: new TMap.LatLng(s.lat, s.lng),
            content: `${i + 1}.${s.toponym}`,
          })),
        });
        new TMap.MultiPolyline({
          map,
          geometries: allPaths.map((paths, i) => ({ id: String(i), paths })),
        });

        try {
          map.fitBounds(bounds, { padding: 40 });
        } catch {
          /* fitBounds 兼容性兜底：保留默认 zoom */
        }
        setReady(true);
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setError(
            e instanceof Error
              ? `${e.message}（请确认 Key 已开启 WebGL JS API 并将 localhost 加入域名白名单）`
              : '地图初始化失败',
          );
        }
      });

    return () => {
      cancelled = true;
      mapRef.current?.destroy();
      mapRef.current = null;
    };
  }, [spots, legs]);

  if (spots.length === 0) return null;

  return (
    <div className="relative mb-4 h-[220px] overflow-hidden rounded-sm border border-black/5 bg-sand">
      <div ref={containerRef} className="h-full w-full" />
      <span className="absolute left-2 top-2 z-10 rounded-full bg-white/90 px-2.5 py-0.5 text-xs font-medium text-text-serif shadow-sm">
        <i className="fa-solid fa-route mr-1 text-primary" />
        {label}
      </span>
      {!ready && !error && (
        <div className="absolute inset-0 flex items-center justify-center text-sm text-text-light">
          <i className="fa-solid fa-spinner fa-spin mr-2 text-primary" />
          路线图加载中...
        </div>
      )}
      {error && (
        <div className="absolute inset-0 flex items-center justify-center bg-white/80 px-6 text-center text-[13px] text-text-light">
          {error}
        </div>
      )}
    </div>
  );
}

export default memo(DayRouteMap);
