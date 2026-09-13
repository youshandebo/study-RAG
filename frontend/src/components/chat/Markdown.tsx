'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** Markdown + KaTeX 统一渲染器（所有卡片共用）。
 * 安全：rehype-sanitize 在 KaTeX 之前过滤原始 HTML（script/iframe/事件属性），
 * 链接协议默认白名单 http/https/mailto，javascript:/data: 伪协议被剥除。
 *
 * 流式防闪烁（streaming 模式）：未闭合的 $$ 块级公式暂不渲染。
 * SSE 流式期间，若 Markdown 解析器收到半个公式（如 `$$\frac{1}{` 但分母与
 * 闭合标记尚未到达），会先显示原始字符、闭合后跳变为渲染公式——整条气泡
 * 剧烈闪烁。缓冲未闭合尾段后，公式只会在完整到达时一次性出现。
 *
 * 引用角标（onCite）：回答文本中的 `[1]` `[2]` 渲染为可点击角标，
 * 点击回调携带编号——由调用方负责联动证据抽屉（打开 + 高亮对应条目）。
 */
import ReactMarkdown from 'react-markdown';
import remarkMath from 'remark-math';
import remarkGfm from 'remark-gfm';
import rehypeKatex from 'rehype-katex';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import { sanitizeLatex } from '@/lib/katex-parser';

// 在默认白名单上放开 KaTeX 渲染所需的输出结构（sanitize 先于 katex 执行，
// 放开项仅用于承载 katex 生成的 svg/span；用户原始 HTML 仍被严格过滤）
const sanitizeSchema = {
  ...defaultSchema,
  tagNames: [...(defaultSchema.tagNames ?? []), 'svg', 'path', 'g', 'line', 'rect', 'circle', 'annotation', 'semantics', 'mrow', 'mi', 'mo', 'mn', 'msup', 'msub', 'mfrac', 'msqrt', 'mroot', 'mtext', 'mspace'],
  attributes: {
    ...defaultSchema.attributes,
    '*': [...(defaultSchema.attributes?.['*'] ?? []), 'class', 'style'],
    svg: ['xmlns', 'width', 'height', 'viewbox', 'preserveaspectratio'],
    path: ['d', 'fill', 'stroke', 'stroke-width', 'stroke-linecap'],
    line: ['x1', 'y1', 'x2', 'y2', 'stroke', 'stroke-width'],
    rect: ['x', 'y', 'width', 'height', 'rx', 'fill'],
    circle: ['cx', 'cy', 'r', 'fill'],
    annotation: ['encoding'],
    span: [...(defaultSchema.attributes?.span ?? []), 'aria-hidden'],
    a: [...(defaultSchema.attributes?.a ?? []), 'rel', 'target'],
  },
  protocols: {
    ...defaultSchema.protocols,
    href: ['http', 'https', 'mailto'], // 显式拦截 javascript: / data:
  },
};

/** 流式期间隐藏未闭合的 $$ 块级公式尾段（防止半公式跳变闪烁）。
 * 原理：`$$` 出现次数为奇数 = 最后一块未闭合；从最后一个 `$$` 起截断，
 * 等闭合标记到达后自然渲染。已闭合的部分不受影响。
 */
function hideIncompleteBlockMath(text: string): string {
  const parts = text.split('$$');
  if (parts.length % 2 === 0) {
    // 偶数段 => `$$` 奇数次 => 末尾有未闭合块
    return parts.slice(0, -1).join('$$');
  }
  return text;
}

/** 把独立出现的 [1]~[9] 转成可被 a 组件拦截的链接标记。
 * 负向断言 (?!\()：不碰已经是 markdown 链接文本的 `[1](...)`；
 * (?<![\w\[]) 前向断言：不碰章节号 `5.3[1]` 或嵌套 `[[1]]` 里的片段。
 */
function linkCitations(text: string): string {
  return text.replace(/(?<![\w\[])\[([1-9])\](?!\()/g, '[$1](#cite-$1)');
}

export default function Markdown({
  text,
  className = '',
  streaming = false,
  onCite,
}: {
  text: string;
  className?: string;
  /** 流式渲染中：隐藏未闭合的 $$ 尾段，防公式闪烁跳变 */
  streaming?: boolean;
  /** 点击引用角标 [N] 的回调（联动证据抽屉），缺省时角标渲染为普通文本样式 */
  onCite?: (n: number) => void;
}) {
  const prepared = linkCitations(
    streaming ? hideIncompleteBlockMath(sanitizeLatex(text)) : sanitizeLatex(text),
  );
  return (
    <div className={`prose-tutor leading-relaxed text-[14.5px] ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[
          [rehypeSanitize, sanitizeSchema],
          [
            rehypeKatex,
            {
              throwOnError: false,
              errorColor: '#dc2626',
              strict: false,
              trust: false, // 关闭 \href/\includegraphics 等信任内容
            },
          ],
        ]}
        components={{
          a: (props) => {
            const href = String(props.href ?? '');
            // 引用角标：[1] → #cite-1，渲染为可点击角标按钮
            if (href.startsWith('#cite-')) {
              const n = Number.parseInt(href.slice(6), 10);
              return (
                <button
                  type="button"
                  onClick={(e) => {
                    e.preventDefault();
                    onCite?.(n);
                  }}
                  title={onCite ? `查看第 ${n} 条课堂证据` : undefined}
                  className={`mx-0.5 inline-flex h-[18px] min-w-[18px] -translate-y-[3px] items-center justify-center rounded px-1 align-middle font-mono text-[10.5px] font-bold leading-none transition ${
                    onCite
                      ? 'cursor-pointer bg-chalk-soft text-chalk hover:bg-chalk hover:text-white'
                      : 'bg-paper-deep text-ink-faint'
                  }`}
                >
                  {n}
                </button>
              );
            }
            // 外链强制新窗口 + nofollow，防钓鱼跳转
            if (/^\s*javascript:/i.test(href) || /^\s*data:/i.test(href)) {
              return <span className="text-ink-faint">{String(props.children ?? '')}(链接已移除)</span>;
            }
            return <a {...props} target="_blank" rel="noopener noreferrer nofollow" />;
          },
          img: (props) => {
            const src = String(props.src ?? '');
            if (/^\s*data:/i.test(src)) return null; // 拒绝内联图片
            // eslint-disable-next-line @next/next/no-img-element
            return <img {...props} loading="lazy" alt={props.alt ?? ''} />;
          },
        }}
      >
        {prepared}
      </ReactMarkdown>
    </div>
  );
}
