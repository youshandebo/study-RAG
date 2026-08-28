'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com


/** Markdown + KaTeX 统一渲染器（所有卡片共用）。
 * 安全：rehype-sanitize 在 KaTeX 之前过滤原始 HTML（script/iframe/事件属性），
 * 链接协议默认白名单 http/https/mailto，javascript:/data: 伪协议被剥除。
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

export default function Markdown({ text, className = '' }: { text: string; className?: string }) {
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
          // 外链强制新窗口 + nofollow，防钓鱼跳转
          a: (props) => {
            const href = String(props.href ?? '');
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
        {sanitizeLatex(text)}
      </ReactMarkdown>
    </div>
  );
}
