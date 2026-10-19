"""
main.py —— 学习通（超星）作业自动作答

流程：登录 -> 进课程 -> 作业页 -> 逐份处理未完成作业 -> 逐题作答 -> 提交。
答案由 DeepSeek 生成（见 answer_engine.py），也可以换成你自己的答案来源。

首次使用务必先跑：
    python main.py --check-config      # 检查配置是否齐全（不启动浏览器）
    python main.py --dry-run           # 只填不提交，确认选择器还有效

凭据全部来自环境变量或项目根目录 .env（见 .env.example），代码里不含任何个人信息。

依赖：selenium>=4.20、openai>=1.30、本机安装 Edge 浏览器。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import answer_engine as engine

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "run.log"
FAILED_FILE = BASE_DIR / "failed_questions.txt"
log = logging.getLogger("main")

# ==========================================================================
# 一、页面选择器
# 学习通改版、或你的课程页面结构不同时，只需要改这一段。
# 改动方法见 README「选择器失效怎么办」。
# ==========================================================================
SEL_LOGIN_PHONE = "#phone"          # 手机号输入框
SEL_LOGIN_PWD = "#pwd"              # 密码输入框
SEL_LOGIN_BTN = "#loginBtn"         # 登录按钮
SEL_COURSE_FRAME = "frame_content"  # 首页课程列表所在 iframe
SEL_HOMEWORK_FRAME = "frame_content-zy"  # 课程页作业列表所在 iframe

# 作业列表的「未完成」筛选标签
SEL_UNFINISHED_TAB = "body > div.box > div > div > div.has-content > div.filter > span:nth-child(7)"
SEL_ASSIGNMENT_ITEM = "right-content"    # 作业条目 class

# 题目容器
SEL_QUESTION = "div.questionLi"
SEL_QUESTION_LEGACY = '//div[@class="padBom50 questionLi fontLabel singleQuesId"]'

# 提交按钮 / 二次确认
SEL_SUBMIT_BTN = "#submitFocus > a.completeBtn.fl"
SEL_SUBMIT_CONFIRM = "#popok"
SUBMIT_FALLBACKS = ("a.completeBtn", "#submitFocus a", "//a[contains(text(),'提交')]")

# ==========================================================================
# 二、配置
# ==========================================================================
# 必填项：(环境变量名, 说明)
REQUIRED_ENV = {
    "CHAOXING_USER": "学习通账号（手机号/超星号）",
    "CHAOXING_PASSWORD": "学习通密码",
    "CHAOXING_COURSE_ENTRY": "首页课程入口元素，形如 #first123456",
    "CHAOXING_COURSE_ID": "课程块 id，形如 c_123456789",
    "CHAOXING_COURSE_NAV": "课程页「作业」导航，形如 #nav_123456 > a > i",
    "DEEPSEEK_API_KEY": "DeepSeek API Key，形如 sk-xxxx",
}


@dataclass
class Config:
    user: str = ""
    password: str = ""
    login_url: str = "https://i.chaoxing.com/base"

    course_entry: str = ""
    course_id: str = ""
    course_nav: str = ""

    timeout: int = 20          # 常规显式等待秒数
    short_wait: int = 6        # 小控件等待秒数
    click_pause: float = 0.15  # 连续点击之间的微停顿
    max_assignments: int = 20  # 单次最多处理几份作业
    auto_submit: bool = True
    force_submit: bool = False
    dry_run: bool = False
    headless: bool = False


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config() -> Config:
    engine.load_dotenv()
    return Config(
        user=os.getenv("CHAOXING_USER", "").strip(),
        password=os.getenv("CHAOXING_PASSWORD", "").strip(),
        login_url=os.getenv("CHAOXING_LOGIN_URL", "https://i.chaoxing.com/base"),
        course_entry=os.getenv("CHAOXING_COURSE_ENTRY", "").strip(),
        course_id=os.getenv("CHAOXING_COURSE_ID", "").strip(),
        course_nav=os.getenv("CHAOXING_COURSE_NAV", "").strip(),
        timeout=int(os.getenv("CHAOXING_TIMEOUT", "20")),
        short_wait=int(os.getenv("CHAOXING_SHORT_WAIT", "6")),
        max_assignments=int(os.getenv("CHAOXING_MAX", "20")),
        headless=_env_bool("CHAOXING_HEADLESS"),
    )


def missing_env() -> list[str]:
    load_config()
    return [k for k in REQUIRED_ENV if not os.getenv(k, "").strip()]


def mask(secret: str) -> str:
    """日志/终端里打码，避免截图时把密码漏出去。"""
    if not secret:
        return "(未配置)"
    if len(secret) <= 4:
        return "*" * len(secret)
    return secret[:2] + "*" * (len(secret) - 4) + secret[-2:]


def check_config() -> int:
    load_config()
    print("配置检查（来源：环境变量 / .env）")
    print("-" * 60)
    bad = 0
    for key, hint in REQUIRED_ENV.items():
        value = os.getenv(key, "").strip()
        if value:
            shown = mask(value) if key in ("CHAOXING_USER", "CHAOXING_PASSWORD", "DEEPSEEK_API_KEY") else value
            print(f"[ OK ] {key:<24} = {shown}")
        else:
            bad += 1
            print(f"[缺失] {key:<24} —— {hint}")
    print("-" * 60)
    if bad:
        print(f"还缺 {bad} 项。把 .env.example 复制成 .env 填好即可（.env 已被 .gitignore 忽略）。")
        return 2
    print("配置齐全，可以跑 python main.py --dry-run 了。")
    return 0


# ==========================================================================
# 三、题型识别
# ==========================================================================
TYPE_KEYWORDS = (
    ("multi", ("多选题", "多项选择", "不定项")),
    ("judge", ("判断题",)),
    ("fill", ("填空题",)),
    ("single", ("单选题", "单项选择题", "单项选择")),
    ("essay", ("简答题", "论述题", "计算题", "名词解释", "分析题", "综合题")),
)


def detect_type(text: str) -> str:
    """按题干关键字判断题型；识别不了返回 unknown。"""
    flat = re.sub(r"\s+", "", text or "")
    for kind, words in TYPE_KEYWORDS:
        if any(w in flat for w in words):
            return kind
    return "unknown"


def split_blank(answer: str) -> list[str]:
    """填空题答案按空拆分。"""
    return [p.strip() for p in re.split(r"[,，、;；\n]+", answer or "") if p.strip()]


# ==========================================================================
# 四、机器人
# ==========================================================================
class ChaoxingBot:
    def __init__(self, cfg: Config, driver: webdriver.Edge):
        self.cfg = cfg
        self.driver = driver
        self.course_window: str | None = None
        self.stats = {"ok": 0, "skip": 0, "fail": 0}
        self.failed: list[str] = []
        self._fill_index = 0   # 填空题全局兜底序号
        self._judge_index = 0  # 判断题全局兜底序号

    # ---------------- 基础工具 ----------------
    def _wait(self, by: str, value: str, timeout: int | None = None,
              root: WebElement | None = None) -> WebElement:
        ctx = root if root is not None else self.driver
        return WebDriverWait(ctx, timeout or self.cfg.timeout).until(
            EC.presence_of_element_located((by, value))
        )

    def _wait_clickable(self, by: str, value: str, timeout: int | None = None) -> WebElement:
        return WebDriverWait(self.driver, timeout or self.cfg.timeout).until(
            EC.element_to_be_clickable((by, value))
        )

    def _click(self, by: str, value: str, timeout: int | None = None) -> WebElement:
        el = self._wait_clickable(by, value, timeout)
        self._click_el(el)
        return el

    def _click_el(self, el: WebElement) -> None:
        """点击；被遮挡时降级到 JS 点击，再不行点父级 li。"""
        try:
            el.click()
            return
        except WebDriverException:
            pass
        try:
            self.driver.execute_script("arguments[0].click();", el)
            return
        except WebDriverException:
            pass
        try:
            parent = el.find_element(By.XPATH, "./ancestor::li[1]")
            self.driver.execute_script("arguments[0].click();", parent)
        except WebDriverException as exc:
            log.debug("点击失败：%s", exc)

    def _switch_to_new_window(self, old: list[str], timeout: int | None = None) -> bool:
        deadline = time.time() + (timeout or self.cfg.timeout)
        while time.time() < deadline:
            fresh = [h for h in self.driver.window_handles if h not in old]
            if fresh:
                self.driver.switch_to.window(fresh[-1])
                return True
            time.sleep(0.25)
        return False

    # ---------------- 登录 / 进课程 ----------------
    def login(self) -> None:
        log.info("打开登录页")
        self.driver.get(self.cfg.login_url)
        try:
            phone = self._wait(By.CSS_SELECTOR, SEL_LOGIN_PHONE, timeout=self.cfg.short_wait)
        except TimeoutException:
            log.info("未检测到登录表单，可能已是登录状态，继续")
            return

        phone.clear()
        phone.send_keys(self.cfg.user)
        pwd = self._wait(By.CSS_SELECTOR, SEL_LOGIN_PWD)
        pwd.clear()
        pwd.send_keys(self.cfg.password)
        self._click(By.CSS_SELECTOR, SEL_LOGIN_BTN)
        self._wait(By.ID, SEL_COURSE_FRAME, timeout=self.cfg.timeout)
        time.sleep(1)
        log.info("登录完成")

    def open_course(self) -> None:
        self.driver.switch_to.default_content()
        self._click(By.CSS_SELECTOR, self.cfg.course_entry)
        self.driver.switch_to.frame(self._wait(By.ID, SEL_COURSE_FRAME))

        # 课程封面 / 课程名 / 整个课程块，按顺序尝试
        candidates = (
            f"#{self.cfg.course_id} div.course-cover a img",
            f"#{self.cfg.course_id} div.course-info h3 a span",
            f"#{self.cfg.course_id} a",
        )
        for sel in candidates:
            try:
                self._click(By.CSS_SELECTOR, sel, timeout=self.cfg.short_wait)
                break
            except (TimeoutException, NoSuchElementException):
                continue
        else:
            raise RuntimeError(f"没找到课程 {self.cfg.course_id}，请检查 CHAOXING_COURSE_ID")

        old = list(self.driver.window_handles)
        if not self._switch_to_new_window(old):
            log.warning("点击课程后没有新窗口，假定当前窗口即课程页")
        self.driver.switch_to.default_content()

        self.course_window = self.driver.current_window_handle
        self._click(By.CSS_SELECTOR, self.cfg.course_nav)
        time.sleep(0.5)
        log.info("已进入课程作业页")

    # ---------------- 作业列表 ----------------
    def _enter_list_frame(self) -> None:
        self.driver.switch_to.default_content()
        self.driver.switch_to.frame(self._wait(By.ID, SEL_HOMEWORK_FRAME))

    def list_unfinished(self) -> list[WebElement]:
        """每轮重新拉取，避免用到过期的元素。"""
        self._enter_list_frame()
        try:
            self._click(By.CSS_SELECTOR, SEL_UNFINISHED_TAB, timeout=self.cfg.short_wait)
        except TimeoutException:
            log.debug("未找到「未完成」筛选标签，按当前列表继续")
        try:
            self._wait(By.CLASS_NAME, SEL_ASSIGNMENT_ITEM, timeout=self.cfg.short_wait)
            time.sleep(0.4)
        except TimeoutException:
            return []
        return self.driver.find_elements(By.CLASS_NAME, SEL_ASSIGNMENT_ITEM)

    @staticmethod
    def _item_title(el: WebElement, index: int) -> str:
        try:
            text = (el.text or "").strip()
            if text:
                return text.splitlines()[0][:60]
            for attr in ("id", "data-id", "workid"):
                val = el.get_attribute(attr)
                if val:
                    return f"{attr}:{val}"
        except (StaleElementReferenceException, WebDriverException):
            pass
        return f"作业#{index}"

    def open_assignment(self, el: WebElement) -> None:
        old = list(self.driver.window_handles)
        self._click_el(el)
        if not self._switch_to_new_window(old, timeout=self.cfg.short_wait):
            log.warning("作业没有在新窗口打开，按同窗口处理")
        self.driver.switch_to.default_content()

    def close_assignment(self) -> None:
        try:
            if self.course_window and self.driver.current_window_handle != self.course_window:
                self.driver.close()
        except WebDriverException:
            pass
        handles = self.driver.window_handles
        if self.course_window in handles:
            self.driver.switch_to.window(self.course_window)
        elif handles:
            self.driver.switch_to.window(handles[0])
        try:
            self.driver.switch_to.default_content()
        except WebDriverException:
            pass

    # ---------------- 定位选项 ----------------
    def _find_questions(self) -> list[WebElement]:
        try:
            self._wait(By.CSS_SELECTOR, SEL_QUESTION, timeout=self.cfg.timeout)
        except TimeoutException:
            pass
        els = self.driver.find_elements(By.CSS_SELECTOR, SEL_QUESTION)
        if not els:
            els = self.driver.find_elements(By.XPATH, SEL_QUESTION_LEGACY)
        return els

    def _option_in_question(self, q: WebElement, letter: str) -> WebElement | None:
        """优先在题目内部找选项——最稳的方式，不依赖元素全局顺序。"""
        for xpath in (
            f'.//span[@data="{letter}"]',
            f'.//li[@data="{letter}"]',
            f'.//*[@data="{letter}"]',
        ):
            found = q.find_elements(By.XPATH, xpath)
            if found:
                return found[0]
        return None

    def _global_option(self, letter: str, ordinal: int) -> WebElement | None:
        """兜底：按全局顺序取第 ordinal 个同字母选项。"""
        for xpath in (f'//span[@data="{letter}"]', f'//*[@data="{letter}"]'):
            found = self.driver.find_elements(By.XPATH, xpath)
            if found and 0 <= ordinal < len(found):
                return found[ordinal]
        return None

    # ---------------- 作答 ----------------
    def _answer_choice(self, q: WebElement, text: str, ordinal: int) -> str:
        qtype = detect_type(text)
        if qtype in ("essay", "unknown"):
            log.warning("跳过暂不支持的题型：%s", text[:40].replace("\n", " "))
            return "skip"

        if qtype == "judge":
            answer = engine.ask_judge(text)
            if answer not in ("A", "B"):
                log.warning("判断题答案解析异常（%r）", answer)
                return "fail"
            ok = self._click_judge(q, answer)
            self._judge_index += 1
            return "ok" if ok else "fail"

        letters = engine.ask_choice(text)
        if qtype == "single" and len(letters) > 1:
            letters = letters[0]  # 单选题只保留第一个字母

        clicked = 0
        for letter in letters:
            el = self._option_in_question(q, letter) or self._global_option(letter, ordinal)
            if el is None:
                log.warning("找不到选项 %s：%s", letter, text[:40].replace("\n", " "))
                continue
            self._click_el(el)
            clicked += 1
            time.sleep(self.cfg.click_pause)
        return "ok" if clicked else "fail"

    def _click_judge(self, q: WebElement, answer: str) -> bool:
        label = "对选择" if answer == "A" else "错选择"
        local = q.find_elements(By.XPATH, f'.//div[@aria-label="{answer}{label}"]')
        if not local:
            local = q.find_elements(By.XPATH, f'.//div[contains(@aria-label,"{label}")]')
        if local:
            self._click_el(local[0])
            time.sleep(self.cfg.click_pause)
            return True

        # 兜底 1：按全局顺序取第 _judge_index 个
        for xpath in (
            f'//div[@aria-label="{answer}{label}"]',
            f'//div[contains(@aria-label,"{label}")]',
        ):
            found = self.driver.find_elements(By.XPATH, xpath)
            if found and 0 <= self._judge_index < len(found):
                self._click_el(found[self._judge_index])
                time.sleep(self.cfg.click_pause)
                return True

        # 兜底 2：有的页面判断题也渲染成 span[data]
        span = self._option_in_question(q, answer)
        if span is not None:
            self._click_el(span)
            return True
        log.warning("判断题选项定位失败（答案 %s）", answer)
        return False

    def _type_in_frame(self, frame_el: WebElement, text: str) -> bool:
        self.driver.switch_to.frame(frame_el)
        try:
            body = self._wait(By.CSS_SELECTOR, "body", timeout=self.cfg.short_wait)
            body.click()
            body.send_keys(Keys.CONTROL, "a")
            body.send_keys(Keys.DELETE)
            body.send_keys(text)
            return True
        except WebDriverException as exc:
            log.warning("填空写入失败：%s", exc)
            return False
        finally:
            try:
                self.driver.switch_to.default_content()
            except WebDriverException:
                pass

    def _answer_fill(self, q: WebElement, text: str) -> str:
        answer = engine.ask_text(text)
        if not answer:
            return "fail"

        frames = q.find_elements(By.XPATH, './/iframe[starts-with(@id,"ueditor_")]')
        if not frames:
            frames = q.find_elements(By.TAG_NAME, "iframe")

        if not frames:
            # 少数页面是 contenteditable div
            editable = q.find_elements(By.CSS_SELECTOR, '[contenteditable="true"]')
            if editable:
                try:
                    editable[0].click()
                    editable[0].send_keys(answer)
                    return "ok"
                except WebDriverException as exc:
                    log.warning("contenteditable 填空失败：%s", exc)
                    return "fail"
            # 最后兜底：全局 ueditor 按已填个数顺延
            all_frames = self.driver.find_elements(By.XPATH, '//iframe[starts-with(@id,"ueditor_")]')
            if 0 <= self._fill_index < len(all_frames):
                frames = [all_frames[self._fill_index]]
            else:
                log.warning("填空题找不到输入框：%s", text[:40].replace("\n", " "))
                return "fail"

        parts = split_blank(answer)
        if len(frames) > 1 and len(parts) >= len(frames):
            ok = False
            for frame_el, part in zip(frames, parts):
                ok = self._type_in_frame(frame_el, part) or ok
            self._fill_index += len(frames)
            return "ok" if ok else "fail"

        if len(frames) > 1:
            log.warning("该题有 %d 个空但只解析出 %d 段答案，只填第一个空", len(frames), len(parts))
        ok = self._type_in_frame(frames[0], answer)
        self._fill_index += 1
        return "ok" if ok else "fail"

    def _solve_question(self, q: WebElement, ordinal: int) -> str:
        text = q.text.strip()
        qtype = detect_type(text)
        if qtype == "fill":
            return self._answer_fill(q, text)
        if qtype in ("essay", "unknown"):
            log.warning("题型不支持（%s），跳过：%s", qtype, text[:40].replace("\n", " "))
            return "skip"
        return self._answer_choice(q, text, ordinal)

    def solve_assignment(self) -> dict:
        questions = self._find_questions()
        if not questions:
            log.warning("这份作业没找到题目，可能是空作业或选择器失效，不提交")
            return {"found": 0, "ok": 0, "skip": 0, "fail": 0}

        log.info("共 %d 道题", len(questions))
        local = {"ok": 0, "skip": 0, "fail": 0}
        for idx, q in enumerate(questions, 1):
            text = ""
            try:
                text = (q.text or "").strip()
                status = self._solve_question(q, idx - 1)
            except (RuntimeError, TimeoutException, StaleElementReferenceException,
                    NoSuchElementException, WebDriverException) as exc:
                log.error("第 %d 题处理失败：%s", idx, exc)
                status = "fail"

            local[status] += 1
            self.stats[status] += 1
            if status == "fail":
                self.failed.append(f"第 {idx} 题：{text[:200]}")
            log.info("第 %d/%d 题 -> %s", idx, len(questions), status)

        if local["fail"] == 0 and local["skip"] == 0:
            self._submit()
        elif local["fail"] == 0 and self.cfg.force_submit:
            log.warning("有 %d 道题被跳过，按 --force-submit 仍然提交", local["skip"])
            self._submit()
        elif local["fail"]:
            log.warning("有 %d 道题没答好，默认不提交（避免交白卷）", local["fail"])
        else:
            log.warning("有 %d 道题型不支持（如简答/论述），请手动补完再交", local["skip"])
        return {"found": len(questions), **local}

    # ---------------- 提交 ----------------
    def _submit(self) -> bool:
        if self.cfg.dry_run or not self.cfg.auto_submit:
            log.info("dry-run / no-submit：不提交，请人工检查")
            return False

        btn = None
        try:
            btn = self._wait_clickable(By.CSS_SELECTOR, SEL_SUBMIT_BTN, timeout=self.cfg.short_wait)
        except TimeoutException:
            for sel in SUBMIT_FALLBACKS:
                by = By.XPATH if sel.startswith("//") else By.CSS_SELECTOR
                try:
                    btn = self._wait_clickable(by, sel, timeout=3)
                    break
                except TimeoutException:
                    continue
        if btn is None:
            log.error("找不到提交按钮，请手动提交")
            return False

        self._click_el(btn)
        time.sleep(1)
        try:
            self._click_el(self._wait_clickable(By.CSS_SELECTOR, SEL_SUBMIT_CONFIRM,
                                                timeout=self.cfg.short_wait))
            log.info("已提交并确认")
            return True
        except TimeoutException:
            log.warning("没等到提交确认弹窗，可能已提交，请人工确认")
            return False

    # ---------------- 主循环 ----------------
    def run(self) -> None:
        self.login()
        self.open_course()

        for round_no in range(1, self.cfg.max_assignments + 1):
            items = self.list_unfinished()
            if not items:
                log.info("没有未完成的作业了")
                break

            title = self._item_title(items[0], 0)
            log.info("===== 第 %d 份：%s =====", round_no, title)

            try:
                self.open_assignment(items[0])
            except StaleElementReferenceException:
                items = self.list_unfinished()
                if not items:
                    break
                self.open_assignment(items[0])

            try:
                self.solve_assignment()
            except Exception as exc:  # noqa: BLE001 - 一份作业崩了不影响下一份
                log.exception("这份作业整体出错：%s", exc)
                self.failed.append(f"[{title}] 整体出错：{exc}")
            finally:
                self.close_assignment()

            if self.cfg.dry_run:
                log.info("dry-run：处理完第一份就停（浏览器保持打开，回车后关闭）")
                break

            # 确认它真的从「未完成」里消失了，否则说明没交上，别再重复交
            try:
                left = {self._item_title(e, i) for i, e in enumerate(self.list_unfinished())}
            except (WebDriverException, TimeoutException):
                continue
            if title in left:
                log.warning("这份作业仍在「未完成」里（没提交成功或题没答全），停止以免重复提交")
                break

        log.info("统计：成功 %d，跳过 %d，失败 %d",
                 self.stats["ok"], self.stats["skip"], self.stats["fail"])
        if self.failed:
            try:
                FAILED_FILE.write_text("\n\n".join(self.failed), encoding="utf-8")
                log.warning("有 %d 道题没答好，题目已写入 %s", len(self.failed), FAILED_FILE.name)
            except OSError as exc:
                log.warning("写失败清单出错：%s", exc)


# ==========================================================================
# 五、入口
# ==========================================================================
def build_driver(cfg: Config) -> webdriver.Edge:
    options = EdgeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    if cfg.headless:
        options.add_argument("--headless=new")
    driver = webdriver.Edge(options=options)
    driver.implicitly_wait(0)  # 只用显式等待，避免隐式等待拖慢查找
    return driver


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S", handlers=handlers)
    for noisy in ("selenium", "urllib3", "httpx", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="学习通作业自动作答")
    p.add_argument("--check-config", action="store_true", help="只检查配置是否齐全，不启动浏览器")
    p.add_argument("--dry-run", action="store_true", help="只填不提交，用于验证选择器")
    p.add_argument("--no-submit", action="store_true", help="同 --dry-run")
    p.add_argument("--force-submit", action="store_true", help="有跳过的题也照样提交")
    p.add_argument("--max", type=int, default=None, help="最多处理几份作业")
    p.add_argument("--timeout", type=int, default=None, help="显式等待秒数")
    p.add_argument("--headless", action="store_true", help="无头模式（看不到浏览器）")
    p.add_argument("--verbose", "-v", action="store_true", help="调试日志")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    cfg = load_config()
    if args.check_config:
        return check_config()

    missing = missing_env()
    if missing:
        print("配置不完整，缺少：", ", ".join(missing))
        print("把 .env.example 复制成 .env 填好，或设置同名环境变量。")
        return 2

    if args.max:
        cfg.max_assignments = args.max
    if args.timeout:
        cfg.timeout = args.timeout
    cfg.headless = cfg.headless or args.headless
    cfg.dry_run = args.dry_run or args.no_submit
    cfg.force_submit = args.force_submit

    driver = build_driver(cfg)
    bot = ChaoxingBot(cfg, driver)
    try:
        bot.run()
    except KeyboardInterrupt:
        log.warning("手动中断")
    except Exception as exc:  # noqa: BLE001
        log.exception("运行出错：%s", exc)
        return 1
    finally:
        engine.save_cache()
        if cfg.dry_run and not cfg.headless:
            try:
                input("dry-run 结束，检查完浏览器后按回车关闭…")
            except (EOFError, KeyboardInterrupt):
                pass
        try:
            driver.quit()
        except WebDriverException:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
