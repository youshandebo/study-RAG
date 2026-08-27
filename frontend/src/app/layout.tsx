import type { Metadata } from 'next';
import 'katex/dist/katex.min.css';
import './globals.css';

export const metadata: Metadata = {
  title: '课堂原法助教 · Unified Omni-Chat Workspace',
  description: '拍照解题 · 老师原法 RAG · 音画溯源 · 苏格拉底伴学 · 多模型分屏比对',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
