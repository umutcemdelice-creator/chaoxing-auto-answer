# -*- coding: utf-8 -*-
"""纯函数测试：不联网、不开浏览器。

    python -m pytest tests -q
    # 或
    python tests/test_answer_engine.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import answer_engine as engine
import main as bot


def test_normalize_choice():
    cases = {
        "A": "A",
        "答案为：A": "A",
        "正确答案是 C": "C",
        "（B）": "B",
        "选D": "D",
        "A、C": "AC",
        "A,C,D": "ACD",
        "ACD": "ACD",
        "答案是 A 和 C": "AC",
        "B. 因为……": "B",
        "D\n解析：略": "D",
    }
    for raw, want in cases.items():
        assert engine.normalize_choice(raw) == want, (raw, engine.normalize_choice(raw))


def test_normalize_text():
    cases = {
        "北京": "北京",
        "答案：北京": "北京",
        '"光合作用"': "光合作用",
        "1. 水\n2. 二氧化碳": "水,二氧化碳",
    }
    for raw, want in cases.items():
        assert engine.normalize_text(raw) == want, (raw, engine.normalize_text(raw))


def test_normalize_judge():
    cases = {
        "A": "A", "B": "B", "对": "A", "正确": "A",
        "错": "B", "错误": "B", "√": "A", "×": "B", "选A": "A",
    }
    for raw, want in cases.items():
        assert engine.normalize_judge(raw) == want, (raw, engine.normalize_judge(raw))


def test_detect_type():
    cases = {
        "1.（单选题）下列哪个是……": "single",
        "2、以下哪些说法正确（多选题）": "multi",
        "3. 判断题：地球是圆的": "judge",
        "4.（填空题）首都是____": "fill",
        "5.（简答题）请论述……": "essay",
        "6. 没有标注题型": "unknown",
        "7.（不定项选择题）": "multi",
    }
    for text, want in cases.items():
        assert bot.detect_type(text) == want, (text, bot.detect_type(text))


def test_split_blank():
    assert bot.split_blank("水,二氧化碳") == ["水", "二氧化碳"]
    assert bot.split_blank("水，二氧化碳、氧气") == ["水", "二氧化碳", "氧气"]
    assert bot.split_blank("") == []


def test_cache_key_stable():
    assert engine._cache_key(" 题目  A ", "choice") == engine._cache_key("题目A", "choice")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} 通过")
    print("全部测试通过")
