// Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
/** 音频时间戳与板书截图元数据定义 */

export interface AudioSnippetMeta {
  chunkId: string;
  audioId: string;
  timestampRange: [string, string];
  transcript: string;
  waveform: number[];
}

export interface BoardImageMeta {
  url: string;
  caption: string;
  boardIndex: number | null;
}

export interface EvidenceBundle {
  chunkId: string;
  audio: AudioSnippetMeta;
  board: BoardImageMeta;
  examPoint: string;
}

export interface IngestAsset {
  id: string;
  kind: 'audio' | 'board' | 'text';
  uri: string;
  filename: string;
  lectureDate: string;
  chunkCount: number;
  pitfalls: string[];
  createdAt: number;
}
