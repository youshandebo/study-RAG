// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
import { NextResponse } from 'next/server';

/** 服务端轻量 API 代理：处理跨域与 Key 转发（按需启用） */
const UPSTREAM = process.env.BACKEND_ORIGIN ?? 'http://localhost:8000';

async function proxy(req: Request, ctx: { params: { path?: string[] } }): Promise<Response> {
  const url = new URL(req.url);
  const tail = `/${(ctx.params.path ?? []).join('/')}`;
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
