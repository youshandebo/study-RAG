"""生成 9 张「课堂板书」SVG：黑板底 + 粉笔字 + 手绘框线箭头。

排版核心为单 <text> + <tspan> 流式布局（宽度由渲染器精确计算），
支持 ^{上标} / _{下标} 行内标记。证据抽屉以 /static/boards/board_NN.svg 引用。
重新生成：python scripts/generate_boards.py
"""
from __future__ import annotations

import pathlib

W, H = 1280, 760
OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "boards"

FONT_CN = "'Xingkai SC','STXingkai','KaiTi','KaiTi SC','楷体','SimKai',serif"
FONT_MATH = "'Cambria Math','Latin Modern Math','Georgia','Times New Roman',serif"
CHALK = "#eef3ec"
CHALK_DIM = "#cfd8cd"
CHALK_YELLOW = "#f0dca0"
CHALK_PINK = "#eecfc4"


def _escape(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def el(tag: str, body: str, **attrs) -> str:
    a = "".join(f' {k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return f"<{tag}{a}>{body}</{tag}>" if body else f"<{tag}{a}/>"


def rich_tspans(text: str, base_size: float) -> str:
    """把 ^{...} 上标、_{...} 下标 解析成 tspan；其余字符原样转义。"""
    out: list[str] = []
    buf = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "^_" and i + 1 < len(text) and text[i + 1] == "{":
            if buf:
                out.append(_escape(buf))
                buf = ""
            depth, j = 1, i + 2
            while j < len(text) and depth:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            script = text[i + 2 : j - 1]
            up = ch == "^"
            out.append(el("tspan", _escape(script),
                          dy="-0.45em" if up else "0.35em",
                          font_size=f"{base_size * 0.62:.0f}"))
            # 基线复位（零宽占位）
            out.append(f'<tspan dy="{"0.45em" if up else "-0.35em"}">&#8203;</tspan>')
            i = j
        else:
            buf += ch
            i += 1
    if buf:
        out.append(_escape(buf))
    return "".join(out)


def line_text(x, y, segs) -> str:
    """一行 = 单个 <text> 多个分段 tspan，段宽由浏览器精确排版。"""
    parts = [f'<text x="{x}" y="{y}" fill="{CHALK}" opacity="0.95" '
             f'font-family="{FONT_CN}" font-size="30">']
    for seg in segs:
        text, opts = seg
        size = opts.get("size", 30)
        attrs = [f'font-size="{size}"']
        attrs.append(f'font-family="{FONT_MATH if opts.get("math") else FONT_CN}"')
        if opts.get("math"):
            attrs.append('font-style="italic"')
        if opts.get("fill"):
            attrs.append(f'fill="{opts["fill"]}"')
        if opts.get("weight"):
            attrs.append(f'font-weight="{opts["weight"]}"')
        parts.append(f'<tspan {" ".join(attrs)}>{rich_tspans(text, size)}</tspan>')
    parts.append("</text>")
    return "".join(parts)


def chalk_text(x, y, text, size=30, fill=CHALK, anchor="start", weight="normal", opacity=0.94):
    """独立文本元素：标题、页脚等。"""
    return el("text", _escape(text), x=x, y=y, font_size=size, font_family=FONT_CN,
              fill=fill, font_weight=weight, text_anchor=anchor, opacity=opacity)


def rough_line(x1, y1, x2, y2, color=CHALK, width=3, opacity=0.9):
    return el("path", "", d=f"M {x1} {y1} L {x2} {y2}", stroke=color, stroke_width=width,
              stroke_linecap="round", fill="none", opacity=opacity)


def rough_box(x, y, w, h, color=CHALK_YELLOW, width=3.5, rx=12, opacity=0.92):
    r = max(rx, 6)
    d = (
        f"M {x + r} {y} L {x + w - r} {y} Q {x + w} {y} {x + w} {y + r}"
        f" L {x + w} {y + h - r} Q {x + w} {y + h} {x + w - r} {y + h}"
        f" L {x + r} {y + h} Q {x} {y + h} {x} {y + h - r}"
        f" L {x} {y + r} Q {x} {y} {x + r} {y}"
    )
    return el("path", "", d=d, stroke=color, stroke_width=width, fill="none",
              stroke_linecap="round", opacity=opacity)


def arrow(x1, y1, x2, y2, color=CHALK, width=3.2):
    cx = (x1 + x2) / 2 + 14
    cy = min(y1, y2) - 26
    head = f"M {x2 - 16} {y2 - 4} L {x2} {y2} L {x2 - 13} {y2 + 12}"
    return (
        el("path", "", d=f"M {x1} {y1} Q {cx} {cy} {x2} {y2}", stroke=color,
           stroke_width=width, fill="none", stroke_linecap="round", opacity=0.9)
        + el("path", "", d=head, stroke=color, stroke_width=width, fill="none",
             stroke_linecap="round", stroke_linejoin="round", opacity=0.9)
    )


DEFS = (
    '<defs>'
    '<radialGradient id="glow" cx="42%" cy="36%" r="85%">'
    '<stop offset="0%" stop-color="#325247"/>'
    '<stop offset="70%" stop-color="#263e34"/>'
    '<stop offset="100%" stop-color="#1c2e27"/>'
    '</radialGradient>'
    '<filter id="chalk" x="-8%" y="-8%" width="116%" height="116%">'
    '<feTurbulence type="fractalNoise" baseFrequency="0.055" numOctaves="3" seed="11" result="n"/>'
    '<feDisplacementMap in="SourceGraphic" in2="n" scale="2.4"/>'
    '</filter>'
    '</defs>'
)


def board(title_no: int, topic: str, blocks: list[tuple], footer: str = "") -> str:
    parts: list[str] = [
        DEFS,
        el("rect", "", x=0, y=0, width=W, height=H, rx=22, fill="url(#glow)"),
        el("rect", "", x=10, y=10, width=W - 20, height=H - 20, rx=16, fill="none",
           stroke="#0f1d18", stroke_width=3, opacity=0.55),
        # 标题
        chalk_text(W // 2, 88, topic, size=44, weight="bold", anchor="middle", fill="#f6f1dd"),
        el("path", "", d=f"M {W//2 - 215} 110 Q {W//2} 126 {W//2 + 215} 106",
           stroke=CHALK_YELLOW, stroke_width=3, fill="none", stroke_linecap="round",
           opacity=0.75, filter="url(#chalk)"),
        chalk_text(62, 66, f"No.{title_no:02d}", size=19, opacity=0.45),
    ]

    for blk in blocks:
        kind = blk[0]
        if kind == "line":
            _, x, y, segs = blk
            parts.append(line_text(x, y, segs))
        elif kind == "boxed":
            _, x, y, w, h, segs = blk
            parts.append(rough_box(x, y, w, h))
            parts.append(line_text(x + 50, y + h / 2 + 12, segs))
        elif kind == "box":
            _, x, y, w, h = blk
            parts.append(rough_box(x, y, w, h))
        elif kind == "arrow":
            _, x1, y1, x2, y2 = blk
            parts.append(arrow(x1, y1, x2, y2))
        elif kind == "hr":
            _, x, y, w = blk
            parts.append(el("path", "", d=f"M {x} {y} Q {x + w/2} {y+7} {x + w} {y}",
                            stroke=CHALK, stroke_width=2.2, fill="none", opacity=0.42))

    foot = footer or "高等数学 · 反常积分专题"
    parts.append(chalk_text(60, H - 38, foot, size=19, opacity=0.42))
    parts.append(chalk_text(W - 60, H - 38, f"— {title_no:02d} —", size=19, anchor="end", opacity=0.42))

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'font-family="{FONT_CN}">\n' + "\n".join(parts) + "\n</svg>\n"
    )


def m(text: str, opts: dict | None = None):
    o = dict(opts or {})
    o.setdefault("math", True)
    return (text, o)


def t(text: str, opts: dict | None = None):
    return (text, dict(opts or {}))


BOARDS: dict[int, str] = {}

# ---- 01 定义与分类 ----------------------------------------------------------
BOARDS[1] = board(1, "反常积分 · 定义与分类", [
    ("line", 100, 200, [t("一、无穷限反常积分")]),
    ("line", 150, 275, [m("∫_{a}^{+∞}", {"size": 48}), m(" f(x) dx = lim_{t→+∞} ", {"size": 33}),
                        m("∫_{a}^{t}", {"size": 46}), m(" f(x) dx", {"size": 30})]),
    ("hr", 110, 320, 430),
    ("line", 100, 400, [t("二、瑕积分（无界函数）")]),
    ("line", 150, 475, [m("∫_{a}^{b}", {"size": 46}), m(" f(x) dx ，当 ", {"size": 30}),
                        m("x=c", {"size": 31}), t(" 为瑕点 ⇒ 拆分讨论")]),
    ("line", 120, 585, [t("判定总路线：", {"weight": "bold"}),
                        t("先找瑕点 → 再看无穷远处行为 → 判别法定结论")]),
    ("boxed", 120, 625, 900, 80,
     [t("口诀：", {"fill": CHALK_YELLOW, "weight": "bold"}),
      t("一找瑕点二比阶，三用 p 积分下结论", {"fill": CHALK_YELLOW})]),
])

# ---- 02 今日例题 ------------------------------------------------------------
BOARDS[2] = board(2, "今日例题 · 试判断敛散性", [
    ("box", 200, 145, 880, 195),
    ("line", 380, 262, [m("∫_{1}^{+∞}", {"size": 56}),
                        m(" √x ⁄ (x^{2} + ln x)", {"size": 52}),
                        m("  dx", {"size": 36})]),
    ("line", 300, 405, [t("审题：分子分母同时含幂与对数 —— 提示 "),
                        t("「抓大头」比阶法", {"fill": CHALK_YELLOW, "weight": "bold"})]),
    ("line", 240, 500, [t("检查区间端点：")]),
    ("line", 290, 572, [m("x = 1", {"size": 31}), t(" 代入得 "), m("√1 ⁄ (1 + ln 1) = 1", {"size": 31}),
                        t("，函数值有限，"), t("不是瑕点 ✓", {"fill": CHALK_PINK, "weight": "bold"})]),
    ("line", 340, 660, [t("只需专攻 "), m("x → +∞", {"size": 32}), t(" 方向的行为")]),
])

# ---- 03 抓大头 --------------------------------------------------------------
BOARDS[3] = board(3, "核心方法 · 抓大头（同阶无穷大比阶）", [
    ("line", 110, 205, [t("当 "), m("x → +∞", {"size": 34}), t(" 时，比较各项增长速度：")]),
    ("line", 180, 310, [m("x^{3}", {"size": 44}), m("  ≫  ", {"size": 40, "fill": CHALK_YELLOW}),
                        m("x^{2}", {"size": 44}), m("  ≫  ", {"size": 40, "fill": CHALK_YELLOW}),
                        m("√x", {"size": 44}), m("  ≫  ", {"size": 40, "fill": CHALK_YELLOW}),
                        m("ln x", {"size": 44})]),
    ("hr", 115, 360, 520),
    ("line", 115, 450, [t("本例分母：", {"weight": "bold"})]),
    ("line", 185, 530, [m("x^{2} + ln x  ∼  x^{2}", {"size": 44}),
                        t("   （ln x 可忽略）", {"opacity": 0.75})]),
    ("arrow", 815, 515, 935, 515),
    ("line", 185, 628, [m("∴ f(x) = √x ⁄ x^{2} ∼ 1 ⁄ x^{3/2}", {"size": 40, "fill": CHALK_YELLOW})]),
    ("line", 160, 700, [t("对数增长再慢，也慢不过任意正幂 —— 这就是「抓大头」", {"opacity": 0.68, "size": 26})]),
])

# ---- 04 p-积分判别法 --------------------------------------------------------
BOARDS[4] = board(4, "定理 · p-积分判别法", [
    ("box", 230, 140, 820, 190),
    ("line", 330, 255, [m("∫_{1}^{+∞}", {"size": 52}), m(" dx ⁄ x^{p}", {"size": 44}),
                        t("   当 ", {"size": 30}), m("p > 1", {"size": 40, "fill": CHALK_YELLOW}),
                        t(" 收敛", {"size": 32})]),
    ("line", 110, 420, [t("对照记忆：", {"weight": "bold"}), t("p 越大，曲线尾巴的面积缩得越快")]),
    ("line", 130, 490, [m("p = 3/2   ✓ 收敛", {"size": 33, "fill": "#c9ece2"})]),
    ("line", 130, 548, [m("p = 1     ✗ 发散（调和级数）", {"size": 33, "fill": CHALK_PINK})]),
    ("line", 130, 606, [m("p = 1/2   ✗ 发散", {"size": 33, "fill": CHALK_PINK})]),
    ("boxed", 130, 636, 780, 62,
     [t("口诀：p 大于一收敛，p 小等于一发散", {"fill": CHALK_YELLOW, "weight": "bold", "size": 28})]),
])

# ---- 05 完整推导链 ----------------------------------------------------------
BOARDS[5] = board(5, "例题完整推导链", [
    ("line", 120, 190, [t("① 抓大头化简被积函数", {"weight": "bold"})]),
    ("line", 170, 258, [m("√x ⁄ (x^{2} + ln x)  ∼  √x ⁄ x^{2}  =  1 ⁄ x^{3/2}", {"size": 35})]),
    ("line", 120, 335, [t("② 挂靠 p-积分判别法", {"weight": "bold"})]),
    ("line", 170, 402, [m("比拟 g(x) = 1 ⁄ x^{3/2} ，  p = 3/2 > 1", {"size": 33})]),
    ("line", 120, 480, [t("③ 得出结论", {"weight": "bold"})]),
    ("box", 165, 512, 600, 96),
    ("line", 230, 575, [m("原积分收敛 ✓", {"size": 42, "fill": CHALK_YELLOW, "weight": "bold"})]),
    ("line", 850, 240, [t("参照系：", {"weight": "bold", "size": 28})]),
    ("line", 850, 300, [m("∫_{1}^{+∞} x^{-3/2} dx = 2", {"size": 30})]),
    ("arrow", 1010, 330, 1010, 395),
    ("line", 850, 445, [t("收敛量级足够安全边际", {"size": 26, "opacity": 0.78})]),
    ("hr", 850, 490, 330),
    ("line", 850, 545, [t("非瑕点验证见第 02 板", {"size": 23, "opacity": 0.6})]),
])

# ---- 06 易错预警 ------------------------------------------------------------
BOARDS[6] = board(6, "⚠ 易错预警 · 抓大头 ≠ 乱丢项", [
    ("box", 110, 150, 1050, 125),
    ("line", 155, 222, [t("等价代换只能整体替换，", {"size": 33}),
                        t("不能在加减项里各抓各的大头！", {"size": 33, "fill": CHALK_PINK, "weight": "bold"})]),
    ("line", 130, 305, [t("经典反例：", {"weight": "bold"}), m("tan x − sin x ∼ ?", {"size": 34}),
                        t("   若误取 ", {"size": 27}),
                        m("x − x = 0", {"size": 34, "fill": CHALK_PINK}),
                        t("  ✗", {"size": 30, "fill": CHALK_PINK, "weight": "bold"})]),
    ("line", 180, 385, [t("正确答案首项是 "), m("½x^{3}", {"size": 32}), t("，须泰勒展开后整体处理")]),
    ("hr", 120, 432, 1010),
    ("line", 130, 505, [t("安全做法：", {"weight": "bold"}), t("① 通分成单一分式后再抓分子分母的大头")]),
    ("line", 195, 572, [t("② 加减结构优先提公因子或泰勒展开到首个非零项")]),
    ("line", 195, 639, [t("③ 替换完成后回代检验符号与阶数")]),
])

# ---- 07 瑕点辨析 ------------------------------------------------------------
BOARDS[7] = board(7, "辨析 · 什么才是瑕点", [
    ("line", 110, 195, [t("定义：若 ", {}), m("lim_{x→c⁺}", {"size": 32}), m(" f(x) = ∞", {"size": 32}),
                        t("，则 "), m("x = c", {"size": 31}), t(" 为瑕点")]),
    ("box", 110, 235, 1050, 130),
    ("line", 160, 285, [t("常见候选：区间端点 ／ 分母零点 ／ ln 的自变量趋于 0 处", {"size": 28})]),
    ("line", 160, 342, [t("注意：有限值 ≠ 无穷大，先代入验证再定性", {"size": 28, "fill": CHALK_YELLOW})]),
    ("line", 110, 455, [t("回到例题：", {"weight": "bold"}),
                        m("x = 1", {"size": 32}), t(" 代入 "),
                        m("√1 ⁄ (1² + ln 1) = 1 ⁄ 1 = 1", {"size": 32}), t("，有限 ✓")]),
    ("line", 170, 540, [t("∴ 下限 "), m("x = 1", {"size": 30}),
                        t(" 不是瑕点，别画蛇添足拆成瑕积分！", {"fill": CHALK_PINK})]),
    ("boxed", 130, 610, 820, 76,
     [t("自检清单：找零点 → 代入求值 → 结果有穷？⇒ 非瑕点", {"size": 27})]),
])

# ---- 08 变式训练 ------------------------------------------------------------
BOARDS[8] = board(8, "变式训练 · 放缩的艺术", [
    ("box", 160, 135, 950, 135),
    ("line", 225, 212, [t("变式：判断 "),
                        m("∫_{2}^{+∞}", {"size": 44}), m(" ln x ⁄ (x·√x) dx", {"size": 38}),
                        t(" 的敛散性", {"size": 29})]),
    ("line", 130, 335, [t("思路：对数增长慢于任意正幂 ⇒ 可以"),
                        t("「借」一个小幂来放缩", {"fill": CHALK_YELLOW, "weight": "bold"})]),
    ("line", 175, 440, [m("ln x ≤ x^{1/4}    ( x 充分大时 )", {"size": 40, "fill": CHALK_PINK})]),
    ("arrow", 720, 425, 850, 425),
    ("line", 175, 545, [m("∴ 原式 ≤ ∫_{2}^{+∞} dx ⁄ x^{5/4} ， p = 5/4 > 1", {"size": 37})]),
    ("line", 175, 643, [t("结论：", {"weight": "bold"}),
                        t("收敛 ✓", {"size": 37, "fill": CHALK_YELLOW, "weight": "bold"}),
                        t("     取四分之一放缩就出来了", {"opacity": 0.65, "size": 26})]),
    ("line", 130, 705, [t("火候：指数太小压不住 ln，太大浪费收敛速度", {"size": 22, "opacity": 0.62})]),
])

# ---- 09 课堂小结 ------------------------------------------------------------
BOARDS[9] = board(9, "课堂小结 · 一图流", [
    ("line", 130, 190, [t("识别瑕点 → 比阶化简 → 挂靠判别法 → 结论", {"size": 31, "fill": CHALK_YELLOW, "weight": "bold"})]),
    ("arrow", 350, 232, 520, 232),
    ("line", 545, 242, [m("p>1 ✓", {"size": 31, "fill": "#c9ece2"})]),
    ("arrow", 700, 232, 870, 232),
    ("line", 890, 242, [m("p≤1 ✗", {"size": 31, "fill": CHALK_PINK})]),
    ("hr", 130, 290, 1020),
    ("line", 130, 385, [t("三大纪律：", {"size": 32, "weight": "bold"})]),
    ("line", 170, 462, [t("① 先查瑕点，再谈无穷限")]),
    ("line", 170, 536, [t("② 抓大头只许乘除整体换，不许加减乱丢项")]),
    ("line", 170, 610, [t("③ 结论必须挂靠 p-积分或等价可积样本")]),
    ("line", 130, 690, [t("预告下一讲：条件收敛与绝对收敛（Dirichlet 判别法）",
                        {"fill": CHALK_YELLOW, "size": 25, "opacity": 0.85})]),
])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for no, svg in sorted(BOARDS.items()):
        path = OUT_DIR / f"board_{no:02d}.svg"
        path.write_text(svg, encoding="utf-8")
        print(f"wrote {path.name} ({path.stat().st_size/1024:.1f} KB)")


if __name__ == "__main__":
    main()
