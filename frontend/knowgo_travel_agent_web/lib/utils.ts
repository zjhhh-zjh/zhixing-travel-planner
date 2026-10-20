/**
 * 公共工具函数
 * - HTML 转义（防 XSS，映射表单例）
 * - 日期格式化（带计算结果缓存）
 * - 节流 / 防抖
 */

// 单例 HTML 转义映射表：模块加载时只创建一次，复用于全部转义调用
const HTML_ESCAPE_MAP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};
const HTML_ESCAPE_REGEX = /[&<>"']/g;

/**
 * HTML 转义，并把换行符转成 <br>
 * 与原 zhixing.html escapeHtml 行为完全一致。
 * 在 React 中通常直接渲染文本即可（React 默认转义），
 * 本函数用于任意需要拼接 innerHTML 的注入点，防止 XSS。
 */
export function escapeHtml(text: string): string {
  if (!text) return '';
  return (
    text
      .replace(HTML_ESCAPE_REGEX, (char) => HTML_ESCAPE_MAP[char])
      .replace(/\n/g, '<br>')
  );
}

/** 日期格式化结果缓存：同一原始字符串不重复计算 */
const timeCache = new Map<string, string>();

/**
 * 时间格式化：原版返回「M月D日」
 * 计算结果按原始字符串缓存，避免重复 new Date() 与拼接。
 */
export function formatTime(str?: string | null): string {
  if (!str) return '';
  const cached = timeCache.get(str);
  if (cached !== undefined) return cached;
  const d = new Date(str);
  if (Number.isNaN(d.getTime())) {
    timeCache.set(str, '');
    return '';
  }
  const result = `${d.getMonth() + 1}月${d.getDate()}日`;
  timeCache.set(str, result);
  return result;
}

/** 节流：保证 fn 在 wait 时间内最多执行一次 */
export function throttle<T extends (...args: never[]) => void>(
  fn: T,
  wait = 100,
): (...args: Parameters<T>) => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return (...args: Parameters<T>) => {
    if (timer) return;
    timer = setTimeout(() => {
      fn(...args);
      timer = null;
    }, wait);
  };
}

/** 防抖：连续触发时重置计时，只在停止触发 wait 后执行一次 */
export function debounce<T extends (...args: never[]) => void>(
  fn: T,
  wait = 100,
): (...args: Parameters<T>) => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return (...args: Parameters<T>) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      fn(...args);
      timer = null;
    }, wait);
  };
}

/** 简易类名拼接工具：过滤 falsy 值 */
export function cx(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(' ');
}
