/** 常用模型服务商一键填充模板（base_url + 默认模型名） */
export type SectionKey = 'llm' | 'embedding' | 'asr' | 'vlm';
export type AnySectionKey = SectionKey | 'media';

export interface Preset {
  name: string;
  base_url: string;
  model: string;
  provider?: string;
}

export const SECTION_META: Record<
  SectionKey,
  { title: string; short: string; desc: string; gradient: string }
> = {
  llm: {
    title: '大语言模型（LLM）',
    short: 'LLM',
    desc: '对话与解题的主脑，回答课堂提问、推导解题步骤。支持 OpenAI 兼容协议或 Anthropic 协议，保存后即刻热生效。',
    gradient: 'from-zinc-800 to-zinc-700',
  },
  embedding: {
    title: '嵌入模型（向量化）',
    short: '嵌入',
    desc: '把课堂切片与提问映射为向量驱动检索，兼容任意 OpenAI /embeddings 端点。更换模型后请重新上传素材重建向量。',
    gradient: 'from-zinc-700 to-zinc-600',
  },
  asr: {
    title: '语音转文字模型（ASR）',
    short: '语音',
    desc: '课堂录音转录为带时间戳的切片。兼容 /audio/transcriptions 端点；未配置时回落本地 faster-whisper 或演示转录。',
    gradient: 'from-amber-700 to-amber-600',
  },
  vlm: {
    title: '多模态识图模型（VLM）',
    short: '识图',
    desc: '板书与题目照片的手写公式识别。支持 Qwen-VL、GPT-4o、GLM-4V 等视觉端点，Anthropic 协议自动适配。',
    gradient: 'from-zinc-700 to-zinc-600',
  },
};

export const PRESETS: Record<SectionKey, Preset[]> = {
  llm: [
    { name: 'OpenAI', base_url: 'https://api.openai.com/v1', model: 'gpt-4o' },
    { name: 'DeepSeek', base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
    { name: '通义千问', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-max' },
    { name: '智谱 GLM', base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-plus' },
    { name: 'Kimi', base_url: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-8k' },
    { name: 'SiliconFlow', base_url: 'https://api.siliconflow.cn/v1', model: 'deepseek-ai/DeepSeek-V3' },
    { name: 'Claude', base_url: 'https://api.anthropic.com', model: 'claude-3-5-sonnet-latest', provider: 'anthropic' },
  ],
  embedding: [
    { name: 'OpenAI', base_url: 'https://api.openai.com/v1', model: 'text-embedding-3-small' },
    { name: '智谱', base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'embedding-3' },
    { name: 'SiliconFlow', base_url: 'https://api.siliconflow.cn/v1', model: 'BAAI/bge-m3' },
    { name: 'Jina', base_url: 'https://api.jina.ai/v1', model: 'jina-embeddings-v3' },
  ],
  asr: [
    { name: 'OpenAI Whisper', base_url: 'https://api.openai.com/v1', model: 'whisper-1' },
    { name: 'SiliconFlow', base_url: 'https://api.siliconflow.cn/v1', model: 'FunAudioLLM/SenseVoiceSmall' },
    { name: 'Groq', base_url: 'https://api.groq.com/openai/v1', model: 'whisper-large-v3' },
  ],
  vlm: [
    { name: '通义 Qwen-VL', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-vl-max' },
    { name: 'OpenAI GPT-4o', base_url: 'https://api.openai.com/v1', model: 'gpt-4o' },
    { name: '智谱 GLM-4V', base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4v-plus' },
    { name: 'SiliconFlow', base_url: 'https://api.siliconflow.cn/v1', model: 'Qwen/Qwen2.5-VL-32B-Instruct' },
  ],
};
