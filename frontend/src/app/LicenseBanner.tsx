'use client';
// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com

/** 控制台版权指纹：页面加载时输出彩色 ASCII Banner 与双重许可声明（客户端水印之一） */
import { useEffect } from 'react';

export default function LicenseBanner() {
  useEffect(() => {
    const styles = {
      brand: 'color:#2563eb;font:bold 14px monospace;',
      ink: 'color:#e4e4e7;background:#18181b;font:12px monospace;padding:4px 8px;border-radius:4px;',
      dim: 'color:#71717a;font:11px monospace;',
      gold: 'color:#b45309;font:bold 12px monospace;',
    };
    const art =
      String.raw`  ___    _____ _                _____            _             _ ` + '\n' +
      String.raw` |_ _|__|_   _| |__   ___ _ __ |_   _| __ __ _ __| |___ _ __   | |` + '\n' +
      String.raw`  \___ \ \ \ | '_ \ / _ \ '__|  | || '__/ _` + '`' + String.raw` / _` + '`' + String.raw` / -_) '_ \  | |` + '\n' +
      String.raw`  |___) / / | | | |  __/ |     | || | | (_| | (_| \__ \ | | | |_|` + '\n' +
      String.raw`  |____/___|_| |_|\___|_|      |_||_|  \__,_|\__,_|___/_| |_| \___` + '\n';
    // eslint-disable-next-line no-console
    console.log('%c AI Classroom Tutor \n' + '%c' + art, styles.brand, styles.ink);
    // eslint-disable-next-line no-console
    console.log(
      '%c⚖️ AGPL-3.0-or-Commercial 双重许可  |  闭源商用/OEM 需购买商业授权\n' +
        '%c📩 fennengxiong@qq.com  ·  © 2026 fennengxiong. 未经授权移除版权标识即构成侵权。',
      styles.gold,
      styles.dim,
    );
  }, []);
  return null;
}
