'use client';

import { memo } from 'react';

/** 等高线地图背景 SVG（原版内联 data URL，无额外网络请求） */
const MAP_LINES_SVG =
  "data:image/svg+xml,%3Csvg width='100%25' height='100%25' viewBox='0 0 800 400' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' stroke='%231A4D54' stroke-width='1' stroke-opacity='0.08'%3E%3Cpath d='M0 300 C 200 400 400 200 800 350' /%3E%3Cpath d='M0 320 C 220 420 420 220 800 370' /%3E%3Cpath d='M0 340 C 240 440 440 240 800 390' /%3E%3Cpath d='M0 100 C 300 0 500 150 800 50' /%3E%3Cpath d='M0 120 C 320 20 520 170 800 70' /%3E%3Cpath d='M-100 200 C 200 100 600 300 900 200' stroke-dasharray='10,10' stroke-opacity='0.05' /%3E%3C/g%3E%3C/svg%3E";

/**
 * 全局装饰层（跨页面固定）：
 * 1. 等高线地图固定背景
 * 2. 飞行飞机 + 尾气动画（60s 横穿屏幕）
 * 动画使用 transform 硬件加速 + will-change 优化
 */
function DecorBackground() {
  return (
    <>
      {/* 等高线地图背景 */}
      <div
        aria-hidden
        className="pointer-events-none fixed left-0 top-0 z-0 h-full w-full bg-cover"
        style={{ backgroundImage: `url("${MAP_LINES_SVG}")` }}
      />

      {/* 飞行飞机（原版 .plane-container，left:-100px + flyAcross 动画） */}
      <div
        aria-hidden
        className="animate-fly-across fixed left-[-100px] top-[20%] z-0 opacity-80 will-change-transform"
      >
        {/* 尾气：渐变拖尾 */}
        <div className="absolute right-5 top-3 h-[2px] w-[100px] bg-gradient-to-r from-transparent to-accent/30" />
        {/* 纸飞机图标 */}
        <i className="fa-solid fa-plane rotate-15 text-2xl text-accent" />
      </div>
    </>
  );
}

export default memo(DecorBackground);
