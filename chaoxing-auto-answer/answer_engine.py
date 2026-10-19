"""
answer_engine.py —— 答案生成模块（DeepSeek API）

职责单一：给一道题干，返回可直接填进页面的答案。
  ask_choice() -> "A" / "ACD"    单选、多选
  ask_judge()  -> "A" / "B"      判断（对 / 错）
  ask_text()   -> "北京"         填空（多空用英文逗号分隔）

设计要点
--------
- Key 只从环境变量 DEEPSEEK_API_KEY 或项目根目录 .env 读取，代码里不含任何凭据。
- 非流式输出：批量答题不要打字机效果，一次性返回更快也更好重试。
- temperature=0，答案稳定可复现。
- 客户端单例 + 超时 + 指数退避重试。
- 内置答案缓存（内存 + answer_cache.json），同一道题不重复请求、不重复花钱。
- 归一化：把 "正确答案是 A" / "（B）" / "A、C" / "选D" / "对" / "√"
  统一压成 "A" / "AC" / "B" 这类干净结果。

单独测试：
    python answer_engine.py "1+1=？A 1 B 2 C 3 D 4"
    python answer_engine.py --kind text "中华人民共和国的首都是"
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from openai import OpenAI

log = logging.getLogger("answer_engine")

BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "answer_cache.json"


# --------------------------------------------------------------------------
# 配置（全部来自环境变量 / .env）
# --------------------------------------------------------------------------
def load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    """极简 .env 解析，省掉 python-dotenv 依赖。已存在的环境变量优先。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


load_dotenv()

BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

SYSTEM_PROMPT = (
    "你是答题引擎。只输出答案本身，绝不解释、不分析、不复述题目、不输出序号。"
)
PROMPT_CHOICE = (
    "下面是一道选择题或判断题。只输出选项字母（大写）。"
    "单选/判断只输出一个字母，例如 A；多选输出全部正确字母并用英文逗号分隔，例如 A,C,D。"
)
PROMPT_TEXT = (
    "下面是一道填空题。只输出应填入的内容本身，不要序号、不要引号、不要解释。"
    "有多个空时按顺序输出，空与空之间用英文逗号分隔。"
)

_client: OpenAI | None = None


def api_key() -> str:
    return os.getenv("DEEPSEEK_API_KEY", "").strip()


def get_client() -> OpenAI:
    """惰性创建客户端，全进程复用连接池。"""
    global _client
    if _client is None:
        key = api_key()
        if not key:
            raise RuntimeError(
                "未配置 DEEPSEEK_API_KEY：请设置环境变量，或把 .env.example 复制成 .env 后填入 Key"
            )
        # 重试由本模块自己做，SDK 层关掉，避免嵌套重试等待过久
        _client = OpenAI(api_key=key, base_url=BASE_URL, timeout=45.0, max_retries=0)
    return _client


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------
_cache: dict[str, str] | None = None


def _cache_bucket() -> dict[str, str]:
    global _cache
    if _cache is None:
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            _cache = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            _cache = {}
    return _cache


def _cache_key(question: str, kind: str) -> str:
    return kind + "::" + re.sub(r"\s+", "", question)


def save_cache() -> None:
    if _cache:
        try:
            CACHE_FILE.write_text(json.dumps(_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:  # 缓存写不进去不影响主流程
            log.debug("写入答案缓存失败：%s", exc)


# --------------------------------------------------------------------------
# 底层调用
# --------------------------------------------------------------------------
def _chat(messages: list[dict], *, temperature: float = 0.0, max_tokens: int = 512,
          retries: int = 3) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = get_client().chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return text
            last_exc = RuntimeError("模型返回空内容")
        except Exception as exc:  # noqa: BLE001 - 网络/限流/格式问题统一重试
            last_exc = exc
        wait = min(2 ** attempt, 8)
        if attempt < retries:
            log.warning("请求失败（第 %d/%d 次）：%s，%d 秒后重试", attempt, retries, last_exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"连续 {retries} 次请求失败：{last_exc}")


# --------------------------------------------------------------------------
# 答案归一化
# --------------------------------------------------------------------------
_PREFIX_RE = re.compile(r"^(?:正确)?答案|^答|^故|^所以|^选项|^应选|^选", re.IGNORECASE)
_WRAP_CHARS = " \t\r\n。.；;，,、:：\"'“”‘’（）()[]【】<>《》"


def _clean_first_line(raw: str) -> str:
    """取第一个非空行，去掉“答案：”“选 ”之类前缀和包裹标点。"""
    line = (raw or "").strip().replace("\r", "")
    for piece in (l.strip() for l in line.split("\n")):
        if piece:
            line = piece
            break
    line = _PREFIX_RE.sub("", line)
    line = re.sub(r"^(?:案|为|是)?\s*[:：]\s*", "", line)
    return line.strip(_WRAP_CHARS)


def _dedupe(text: str) -> str:
    out: list[str] = []
    for ch in text:
        if ch not in out:
            out.append(ch)
    return "".join(out)


def normalize_choice(raw: str) -> str:
    """把模型的自由文本压成 'A' 或 'ACD' 形式。"""
    upper = _clean_first_line(raw).upper()

    compact = re.sub(r"[\s,，、;；/和]+", "", upper)
    if re.fullmatch(r"[A-F]{1,6}", compact):
        return _dedupe(compact)

    letters = re.findall(r"(?<![A-Za-z])[A-F](?![A-Za-z])", upper)
    if letters:
        return _dedupe("".join(letters))

    return _dedupe(re.findall(r"[A-F]", (raw or "").upper()))


# 行首序号：1. / 1、 / （1） / 第1空：
_LINE_NUMBER_RE = re.compile(
    r"^(?:第\s*\d+\s*[空题]?\s*[:：、.．]?\s*"
    r"|[\(（]\s*\d{1,3}\s*[\)）]\s*"
    r"|\d{1,3}\s*[\.、．:：]\s*(?=\D))"
)
_EXPLAIN_RE = re.compile(r"^(?:解析|解释|分析|说明)\s*[:：]")


def normalize_text(raw: str) -> str:
    """填空题答案：去掉包裹引号、序号、解释尾巴。多空用英文逗号连接。"""
    text = (raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"^(?:正确)?答案\s*[:：是为]?\s*", "", text)
    text = text.strip().strip("\"'“”‘’ \t")

    cleaned: list[str] = []
    for line in text.splitlines():
        line = _LINE_NUMBER_RE.sub("", line.strip()).strip()
        if not line or _EXPLAIN_RE.match(line):
            continue
        cleaned.append(line.strip("\"'“”‘’"))
    if not cleaned:
        return text

    if len(cleaned) == 1:
        return cleaned[0]
    return re.sub(r"[,，、;；\s]+", ",", ",".join(cleaned)).strip(",")


JUDGE_TRUE = {"A", "T", "TRUE", "Y", "YES", "对", "正确", "√", "是", "1"}
JUDGE_FALSE = {"B", "F", "FALSE", "N", "NO", "错", "错误", "×", "x", "否", "0"}


def normalize_judge(raw: str) -> str:
    """判断题统一成 'A'(对) / 'B'(错)。"""
    text = _clean_first_line(raw).upper()
    if text[:1] in ("A", "B") and len(text) <= 8:
        return text[0]
    for token in JUDGE_TRUE:
        if text.startswith(token.upper()):
            return "A"
    for token in JUDGE_FALSE:
        if text.startswith(token.upper()):
            return "B"
    if re.search(r"对|正确|√", text):
        return "A"
    if re.search(r"错|错误|×", text):
        return "B"
    guess = normalize_choice(raw)
    return guess[0] if guess else ""


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------
def ask(question: str, kind: str = "choice", *, retries: int = 3,
        use_cache: bool = True, temperature: float = 0.0) -> str:
    """向模型提问并返回归一化答案。

    :param kind: choice = 选择题/判断题（返回字母），text = 填空题（返回文本）
    """
    question = (question or "").strip()
    if not question:
        return ""

    cache = _cache_bucket()
    key = _cache_key(question, kind)
    if use_cache and key in cache:
        log.debug("命中缓存：%s", question[:20])
        return cache[key]

    prompt = PROMPT_TEXT if kind == "text" else PROMPT_CHOICE
    raw = _chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{prompt}\n\n题目：{question}"},
        ],
        temperature=temperature,
        retries=retries,
    )
    answer = normalize_text(raw) if kind == "text" else normalize_choice(raw)
    if not answer:
        raise RuntimeError(f"无法从模型输出中解析答案：{raw!r}")

    if use_cache:
        cache[key] = answer
        save_cache()
    return answer


def ask_choice(question: str, **kwargs) -> str:
    """单选/多选：返回 'A' 或 'ACD'。"""
    return ask(question, "choice", **kwargs)


def ask_text(question: str, **kwargs) -> str:
    """填空：返回要填入的内容，多空用英文逗号分隔。"""
    return ask(question, "text", **kwargs)


def ask_judge(question: str, **kwargs) -> str:
    """判断：返回 'A'(对) / 'B'(错)。"""
    return normalize_judge(ask(question, "choice", **kwargs))


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = argv[1:]
    kind = "choice"
    if "--kind" in args:
        i = args.index("--kind")
        kind = args[i + 1]
        del args[i:i + 2]

    question = " ".join(args).strip() or input("请输入题目：").strip()
    try:
        print(ask(question, kind))
    except RuntimeError as exc:
        print(f"出错：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
