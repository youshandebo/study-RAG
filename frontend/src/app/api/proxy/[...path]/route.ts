// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
import { NextResponse } from 'next/server';

/** 服务端轻量 API 代理：处理跨域与 Key 转发（按需启用） */
const UPSTREAM = process.env.BACKEND_ORIGIN ?? 'http://localhost:8000';

/**
 * 运维端点：后端把它们挂在**根路径**（不在 /api/v1 前缀下），按设计无鉴权，
 * 且后端只绑 127.0.0.1 回环——绝不允许经本代理从公网触达。
 *
 * 为什么显式点名，而不只靠下面的 `..` 闸：upstream 的前缀配置将来一变，
 * `..` 闸就可能失效；这道闸必须能独立兜住，而不是依赖另一道闸的正确性。
 */
const DENIED_TAILS = new Set(['/metrics', '/docs', '/redoc', '/openapi.json']);

/** tail 是否允许转发。失败关闭：任何可疑形态一律拒绝，不试图"洗干净再放行"。 */
function allowedTail(tail: string): boolean {
  let decoded: string;
  try {
    decoded = decodeURIComponent(tail);
  } catch {
    return false; // 畸形百分号编码
  }
  if (decoded.includes('\0')) return false;
  // `..` / `.` 段会被 fetch/undici 归一化，从而逃出 UPSTREAM 自带的 /api/v1
  // 前缀、打到后端根路径上的端点。业务路径永远不含这两种段，所以零误伤。
  for (const seg of decoded.split('/')) {
    if (seg === '..' || seg === '.') return false;
  }
  const normalized = `/${decoded.split('/').filter((s) => s !== '').join('/')}`;
  return !DENIED_TAILS.has(normalized);
}

async function proxy(req: Request, ctx: { params: { path?: string[] } }): Promise<Response> {
  const url = new URL(req.url);
  const tail = `/${(ctx.params.path ?? []).join('/')}`;
  if (!allowedTail(tail)) {
    // 用 404 而不是 403：不透露这些端点是否存在
    return NextResponse.json({ error: 'not found' }, { status: 404 });
  }
  const target = `${UPSTREAM}${tail}${url.search}`;
  const headers = new Headers(req.headers);
  headers.delete('host');
  try {
    const resp = await fetch(target, {
      method: req.method,
      headers,
      body: req.body,
      // @ts-expect-error -- Next fetch duplex flag
      duplex: 'half',
    });
    const body = resp.body;
    return new NextResponse(body, {
      status: resp.status,
      headers: { 'Content-Type': resp.headers.get('content-type') ?? 'application/json' },
    });
  } catch (e) {
    return NextResponse.json({ error: `代理失败: ${(e as Error).message}` }, { status: 502 });
  }
}

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
export { proxy as GET, proxy as POST, proxy as PATCH, proxy as DELETE };
