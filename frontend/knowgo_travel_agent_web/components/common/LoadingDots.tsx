'use client';

import { memo } from 'react';

/**
 * 波浪起伏延迟：周期 0.9s，5 个点按 T/5（0.18s）步进错峰，
 * 各点相位均匀铺满一整周期，形成横向流动的波浪
 */
const WAVE_DELAYS = ['-0.72s', '-0.54s', '-0.36s', '-0.18s', '0s'];

/**
 * AI 等待波浪加载动画
 * 5 个圆点按 0.9s 周期错峰上下起伏 + 明暗呼吸，
 * 等待模型响应时展示，提升等待感知
 */
function LoadingDots() {
  return (
    <div className="flex h-4 items-center gap-1.5 px-1">
      {WAVE_DELAYS.map((delay, index) => (
        <span
          key={index}
          className="h-1.5 w-1.5 animate-wave-float rounded-full bg-primary"
          style={{ animationDelay: delay }}
        />
      ))}
    </div>
  );
}

export default memo(LoadingDots);