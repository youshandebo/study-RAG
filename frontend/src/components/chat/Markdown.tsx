'use client';

/** Markdown + KaTeX 统一渲染器（所有卡片共用） */
import ReactMarkdown from 'react-markdown';
import remarkMath from 'remark-math';
import remarkGfm from 'remark-gfm';
import rehypeKatex from 'rehype-katex';
import { sanitizeLatex } from '@/lib/katex-parser';

export default function Markdown({ text, className = '' }: { text: string; className?: string }) {
  return (
    <div className={`prose-tutor leading-relaxed text-[14.5px] ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[
          [
            rehypeKatex,
            {
              throwOnError: false,
              errorColor: '#b5432f',
              strict: false,
              trust: true,
            },
          ],
        ]}
      >
        {sanitizeLatex(text)}
      </ReactMarkdown>
    </div>
  );
}
