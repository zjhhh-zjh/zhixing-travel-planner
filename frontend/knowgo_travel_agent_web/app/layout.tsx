import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';
import './globals.css';
import Providers from './providers';

export const metadata: Metadata = {
  title: '知行 ZhiXing - 您的智能旅伴',
  description: '知行旅行：规划路线、寻找隐秘的沙滩、预订最棒的日落景观位。',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
};

/**
 * 根布局：
 * - Google Fonts 外链（Noto Sans SC / Noto Serif SC / Playfair Display）
 * - Font Awesome 6.4.0 CDN 外链（不做本地打包）
 * - 全局 Redux Provider（自定义持久化层在 store 创建时同步 rehydrate）
 */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@300;400;500&family=Noto+Serif+SC:wght@500;700&family=Playfair+Display:wght@600;700&display=swap"
          rel="stylesheet"
        />
        <link
          rel="stylesheet"
          href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css"
        />
      </head>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
