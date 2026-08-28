// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
/** LaTeX 公式与 Markdown 语法修复与清洗工具 */

/** 将模型输出中常见的坏 LaTeX 修复为 KaTeX 可渲染形式 */
export function sanitizeLatex(md: string): string {
  return (
    md
      // 统一定界符：\(...\) → $...$，\[...\] → $$...$$（KaTeX 原生识别 $ 系）
      .replace(/\\\[((?:.|\n)*?[^\\])\\\]/g, (_m, body: string) => `\n$$${body}$$\n`)
      .replace(/\\\((.*?)\\\)/g, (_m, body: string) => `$${body}$`)
      // 修复被过度转义的 \\frac 等（JSON 双反斜杠残留）
      .replace(/\\\\([a-zA-Z]+)/g, '\\$1')
      // 行内公式两侧多余空格收敛
      .replace(/\$\s+/g, '$')
      .replace(/\s+\$/g, '$')
      // 未闭合的单美元：行内只剩一个 $ 时降级为普通文本
      .replace(/(^|[^\$])\$([^$\n]*)$/gm, '$1$$$2')
      // 中文与 LaTeX 之间的硬空格
      .replace(/\\,+/g, '\\,')
  );
}

/** KaTeX 抛错时不中断渲染 */
export function katexThrowOnError(error: unknown, throwOnErrorFallback?: string): string {
  const msg = throwOnErrorFallback ?? String((error as Error)?.message ?? '公式渲染失败');
  return `<span class="katex-error" title="${encodeURIComponent(msg)}">${msg}</span>`;
}

/** 步骤文本快速判断是否含公式（用于列表渲染决策） */
export function hasFormula(text: string): boolean {
  return /\$[^$]+\$/.test(text);
}
