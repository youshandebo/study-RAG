# Copyright (C) 2026 fennengxiong. AGPL-3.0-or-Commercial. Commercial: fennengxiong@qq.com
"""微型多章节基准夹具：为 rerank 门控参数标定构造可复现的对抗候选池。

存在的理由
----------
内置演示语料只有 4 条切片、1 条 canonical、且全在同一章节，导致标定必然
退化（chapter 通道把同章切片全部票成正样本，硬对抗负样本物理不存在）。
但真实标定的价值恰恰在于「跨章节同名概念的定版切片不得被误提权」——
这个场景必须有数据支撑，不能靠合成 logit 假装。

本夹具用 15 条切片 / 3 条 canonical 构造出跨章节的**同词异章**冲突：
    Lecture A（基础优化）  梯度下降 / 学习率衰减
    Lecture B（深层网络）  梯度消失爆炸 / 残差连接
    Lecture C（生成模型）  模式崩溃 / 判别器收敛

三者共享「梯度」这个领域高频词，但没有一条切片真正回答别人的问题。
这正是用户所说的"负样本长尾抬升"的物理载体。

关键约束：夹具**不得**放进 corpus.py
-----------------------------------
corpus.py 是随 `seed_demo_corpus` 灌入生产知识库的演示种子。基准夹具属于
测试资产，混进去会污染真实部署的检索结果。因此它独立成模块，只在
fixture 生效期间注入，用完由测试自行清理。

召回深度的物理限制
------------------
检索器带考点硬过滤（`retriever.py` 的 `exam_point` 早退分支）：查询一旦
携带 exam_point，跨考点切片在打分前就被丢弃，对抗样本根本进不了候选池。
因此本夹具的查询**只给 course_id 与自然语言 query，不给 exam_point**，
让粗排依语义自行召回错配章节——这与真实学生提问的行为一致（学生不会
先声明考点），也才谈得上检验 tau 的拦截力。
"""
from __future__ import annotations

from app.services.rag.chunker import Chunk

FIXTURE_COURSE = "fixture-dl-101"

# 授课日期固定为相对今天的偏移量，保证时间衰减项不影响跨章节比较
_LECTURE_OFFSET_DAYS = {"a": 30, "b": 20, "c": 10}


def _date_of(lec: str) -> str:
    from datetime import date, timedelta

    return (date.today() - timedelta(days=_LECTURE_OFFSET_DAYS[lec])).isoformat()


def _mk(
    lec: str,
    seq: int,
    text: str,
    exam_point: str,
    chapter: str,
    is_canonical: bool = False,
    difficulty: int = 3,
) -> Chunk:
    return Chunk(
        id=f"fx-{lec}-c{seq:03d}",
        audio_id=f"fx-lec-{lec}",
        start=f"{seq:02d}:00",
        end=f"{seq:02d}:50",
        text=text,
        board_index=seq,
        board_caption=f"夹具板书 {lec.upper()}-{seq:02d}",
        exam_point=exam_point,
        difficulty=difficulty,
        subject="机器学习",
        course_id=FIXTURE_COURSE,
        chapter=chapter,
        lecture_date=_date_of(lec),
        is_canonical=is_canonical,
        method_version=1,
    )


# ------------------------------------------------------------ 三条讲座 --

# Lecture A —— 基础优化：梯度下降与学习率
_LEC_A_CHAPTER = "2.1 梯度下降与学习率"
_LEC_A_POINT = "梯度更新步长控制"

# Lecture B —— 深层网络训练：梯度消失/爆炸与梯度截断
_LEC_B_CHAPTER = "6.2 梯度消失与梯度爆炸"
_LEC_B_POINT = "梯度截断阈值选择"

# Lecture C —— 生成模型：模式崩溃与判别器收敛
_LEC_C_CHAPTER = "9.4 生成对抗网络训练"
_LEC_C_POINT = "判别器收敛判据"


FIXTURE_CHUNKS: list[Chunk] = [
    # ---------------- Lecture A：口语讲授切片（非定版）----------------
    _mk("a", 1,
        "梯度下降的时候步子迈多大？步长就是学习率，太大容易跨过最低点来回震荡，"
        "太小又收敛得慢。所以我们一般配合学习率衰减，前期大步走，后期步长收一收。",
        _LEC_A_POINT, _LEC_A_CHAPTER, difficulty=2),
    _mk("a", 2,
        "注意这里讨论的是凸优化场景下的步长选择。非凸情形下学习率调度还涉及"
        "动量、二阶矩估计这些机制，那是后面章节的内容。",
        _LEC_A_POINT, _LEC_A_CHAPTER, difficulty=3),
    _mk("a", 3,
        "我给大家总结一个经验：学习率设成 1e-3 起步，观察 loss 曲线，"
        "如果前几十步就上下抖动，那就是步子太大了。",
        _LEC_A_POINT, _LEC_A_CHAPTER, difficulty=2),
    # Lecture A 的定版：教师权威解法（潜在对抗负样本）
    _mk("a", 4,
        "【定版】学习率设置四步法：一、按任务规模选初值（大模型 1e-4，"
        "小模型 1e-3）；二、用 warmup 让前 5% 步数线性升温；三、进入"
        "平台期后按 0.5 倍衰减；四、loss 出现 NaN 立即回退到上一检查点。",
        _LEC_A_POINT, _LEC_A_CHAPTER, is_canonical=True, difficulty=3),

    # ---------------- Lecture B：口语讲授切片 ------------------------
    _mk("b", 1,
        "深层网络里梯度为什么会消失？因为反向传播是连乘，每一层的导数都小于 1，"
        "乘上几十层就趋近于零了。梯度爆炸则反过来，初始权重太大就会出现。",
        _LEC_B_POINT, _LEC_B_CHAPTER, difficulty=3),
    _mk("b", 2,
        "梯度更新过大导致发散，工程上最直接的手段就是梯度截断，"
        "把整个梯度的范数裁剪到阈值以内，超过就等比例缩小。",
        _LEC_B_POINT, _LEC_B_CHAPTER, difficulty=3),
    _mk("b", 3,
        "残差连接其实是让梯度有一条恒等映射的捷径可以回流，"
        "所以即使网络很深，梯度也不会衰减到无法训练。",
        "残差连接作用", "6.3 残差与归一化", difficulty=3),
    _mk("b", 4,
        "梯度消失还有一个常见诱因是激活函数选择不当，比如 Sigmoid 的"
        "导数最大只有 0.25，叠几层就没信号了。换成 ReLU 系列会好很多。",
        _LEC_B_POINT, _LEC_B_CHAPTER, difficulty=3),
    # Lecture B 的定版：正确答案（应被 beta 提权）
    _mk("b", 5,
        "【定版】梯度截断标准流程：一、先算全局梯度范数 L2；二、若超过"
        "阈值 max_norm 则全部梯度按 max_norm/L2 等比例缩放；三、阈值经验值"
        "取 1.0~5.0，RNN 类任务取小值更稳；四、截断后仍需监控是否频繁触发，"
        "频繁触发说明学习率或初始化本身有问题。",
        _LEC_B_POINT, _LEC_B_CHAPTER, is_canonical=True, difficulty=4),

    # ---------------- Lecture C：口语讲授切片 ------------------------
    _mk("c", 1,
        "模式崩溃的表现是生成器不管输入什么噪声都吐同一张图，"
        "根因通常是判别器太强，生成器梯度被压制。",
        _LEC_C_POINT, _LEC_C_CHAPTER, difficulty=4),
    _mk("c", 2,
        "判别器什么时候算收敛？理论上要到最优判别器，即输出为真实分布与"
        "生成分布密度比的一半，但实践中判别器 loss 降到 0.69 附近"
        "就是一个典型信号。",
        _LEC_C_POINT, _LEC_C_CHAPTER, difficulty=4),
    _mk("c", 3,
        "生成器损失和判别器损失要交替看，只看一边很容易被误导。"
        "两个 loss 都在震荡且不收敛，往往是学习率没调好。",
        _LEC_C_POINT, _LEC_C_CHAPTER, difficulty=3),
    # Lecture C 的定版
    _mk("c", 4,
        "【定版】判别器收敛判据三条件同时满足：一、判别器对真实样本与生成"
        "样本的平均输出接近 0.5；二、判别器 loss 稳定在 ln2≈0.693 附近"
        "不再下降；三、生成样本的视觉多样性与真实分布相当。",
        _LEC_C_POINT, _LEC_C_CHAPTER, is_canonical=True, difficulty=4),
]


# --------------------------------------------------------------- 查询 --

# 刻意不填 exam_point：检索器的考点硬过滤会在打分前丢掉跨考点切片，
# 对抗样本将无法进入候选池。学生提问也不会预先声明考点。
FIXTURE_QUERIES: list[dict] = [
    {
        "query_id": "fx001",
        "query": "梯度更新过大导致发散怎么办？",
        # 正确答案在 Lecture B（梯度截断），Lecture A 的定版是错配的权威
        "course_id": FIXTURE_COURSE,
        "expect_relevant_chunk": "fx-b-c005",
        "expect_adversarial_chunk": "fx-a-c004",
    },
    {
        "query_id": "fx002",
        "query": "梯度裁剪的阈值一般怎么定？",
        "course_id": FIXTURE_COURSE,
        "expect_relevant_chunk": "fx-b-c005",
        "expect_adversarial_chunk": "fx-a-c004",
    },
    {
        "query_id": "fx003",
        "query": "判别器 loss 降到多少算收敛？",
        "course_id": FIXTURE_COURSE,
        "expect_relevant_chunk": "fx-c-c004",
        "expect_adversarial_chunk": "fx-b-c005",
    },
    {
        "query_id": "fx004",
        "query": "怎么判断判别器已经训练到位了？",
        "course_id": FIXTURE_COURSE,
        "expect_relevant_chunk": "fx-c-c004",
        "expect_adversarial_chunk": "fx-a-c004",
    },
    {
        "query_id": "fx005",
        "query": "学习率设多大的时候需要配合衰减策略？",
        "course_id": FIXTURE_COURSE,
        "expect_relevant_chunk": "fx-a-c004",
        "expect_adversarial_chunk": "fx-b-c005",
    },
]


def fixture_canonical_ids() -> set[str]:
    return {c.id for c in FIXTURE_CHUNKS if c.is_canonical}
