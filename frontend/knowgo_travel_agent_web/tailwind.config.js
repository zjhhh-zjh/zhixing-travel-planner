/** @type {import('tailwindcss').Config} */
/**
 * 知行旅行「复古大地海洋」主题配置
 * 将 zhixing.html 内 :root 的全部 CSS 变量与 keyframes 动画迁移至此，
 * 禁止在任何组件中书写原生 CSS 变量。
 */
module.exports = {
  // JIT 模式（v3 默认开启）：仅生成使用到的样式，精简产物体积
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // —— 原 :root 配色 ——
        primary: {
          DEFAULT: '#1a4d54', // 深黛绿（主色）
          light: '#2c6e7a', // 浅黛绿（hover）
        },
        accent: {
          DEFAULT: '#c28e5c', // 古铜金（强调色）
          hover: '#a67545', // 古铜金 hover
        },
        sand: '#f5f2eb', // 米白纸张底色
        map: 'rgba(26, 77, 84, 0.08)', // 地图纹理色
        'text-main': '#2c3e50', // 主文字色
        'text-serif': '#1a4d54', // 衬线标题色
        'text-light': '#7f8c8d', // 次级文字色
      },
      fontFamily: {
        // 原 body 默认 Noto Sans SC
        sans: ['"Noto Sans SC"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        // 原 .auth-logo / .chat-info h2 / .welcome-title 等衬线标题
        serif: ['"Noto Serif SC"', 'ui-serif', 'Georgia', 'serif'],
        // 原 .auth-subtitle / .stamp-decoration 英文衬线
        display: ['"Playfair Display"', 'ui-serif', 'Georgia', 'serif'],
      },
      borderRadius: {
        // 原圆角层级
        sm: '8px',
        md: '16px',
        lg: '24px',
        xs: '4px',
      },
      boxShadow: {
        // 原玻璃拟态阴影
        glass: '0 8px 32px 0 rgba(26, 77, 84, 0.08)',
        bubble: '0 2px 8px rgba(0, 0, 0, 0.05)',
        icon: '0 4px 12px rgba(26, 77, 84, 0.2)',
        toast: '0 10px 30px rgba(0, 0, 0, 0.2)',
      },
      backdropBlur: {
        // 玻璃拟态模糊
        card: '20px',
        overlay: '8px',
      },
      // 原 keyframes 完整迁移（时长 / 延迟 / 缓动函数完全一致）
      keyframes: {
        // 飞机横穿屏幕：60s linear infinite
        flyAcross: {
          '0%': { transform: 'translate(-100px, 0)' },
          '100%': { transform: 'translate(calc(100vw + 200px), -100px)' },
        },
        // 消息入场滑入：0.3s ease
        slideIn: {
          from: { opacity: '0', transform: 'translateY(10px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        // 波浪加载：0.9s infinite ease-in-out（延迟在组件内联控制，错峰形成横向流动的波浪）
        waveFloat: {
          '0%, 100%': { transform: 'translateY(2px)', opacity: '0.35' },
          '50%': { transform: 'translateY(-4px)', opacity: '1' },
        },
      },
      animation: {
        'fly-across': 'flyAcross 60s linear infinite',
        'slide-in': 'slideIn 0.3s ease',
        'wave-float': 'waveFloat 0.9s infinite ease-in-out',
      },
      // 响应式断点：复刻原 @media (max-width: 900px)
      screens: {
        narrow: { max: '900px' },
      },
    },
  },
  plugins: [],
};
