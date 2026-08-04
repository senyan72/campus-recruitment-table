"""汇总黑名单、企业性质清洗、招聘大类映射。"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlparse

from app.config import COMPANY_NATURES, SKIP_SHEET_KEYWORDS


def current_grad_batch(today: date | datetime | None = None) -> str:
    """当前校招目标应届届别：公历年份 + 1（例：2026 年 → 2027届）。"""
    if today is None:
        from app.timeutil import today as cn_today

        d = cn_today()
    elif isinstance(today, datetime):
        d = today.date()
    else:
        d = today
    return f"{d.year + 1}届"


# 四位年 2025届 / 两位缩写 25届、26应届生 → 统一成 20xx届
_GRAD_BATCH_RE = re.compile(
    r"(?P<y>20\d{2}|[2-3]\d)\s*届|(?P<y2>20\d{2}|[2-3]\d)\s*应届(?:生|毕业生)?",
    re.I,
)


def _norm_grad_year(raw: str | None) -> int | None:
    """两位届别按 20xx 解释（25→2025）；非法则 None。"""
    if not raw:
        return None
    try:
        y = int(raw)
    except ValueError:
        return None
    if 0 <= y < 100:
        y += 2000
    if 2000 <= y <= 2099:
        return y
    return None


def normalize_grad_batch(value: str | None) -> str | None:
    """规范化届别字段：2025届 / 25届 →「2025届」。"""
    s = (value or "").strip()
    if not s:
        return None
    m = re.match(r"^(20\d{2}|[2-3]\d)\s*届$", s)
    if m:
        y = _norm_grad_year(m.group(1))
        return f"{y}届" if y is not None else None
    return parse_grad_batch(s)


def parse_grad_batch(text: str | None) -> str | None:
    """从标题/JD 解析届别：2027届 / 25届 / 2026应届生 →「2027届」等。取文中最大年份。"""
    s = text or ""
    years: list[int] = []
    for m in _GRAD_BATCH_RE.finditer(s):
        y = _norm_grad_year(m.group("y") or m.group("y2"))
        if y is not None:
            years.append(y)
    if not years:
        return None
    return f"{max(years)}届"


# 标题裸年份（补充信号）：「2025环保工程师」「（2025）算法」
# 排除日期形态：2025-09 / 2025/09 / 2025.09 / 2025年9月
_BARE_TITLE_YEAR_RE = re.compile(
    r"(?:^|[（(【\s·,/|])(?P<y>20\d{2})(?!\s*[-/.年]\s*\d)(?=\s*[）)\]]|[^\d]|$)"
)


def extract_grad_batch(text: str | None, *, allow_bare_year: bool = False) -> str | None:
    """
    届别抽取：先走 parse_grad_batch（届/应届）；
    allow_bare_year=True 时，标题中的裸年份（如「2025环保工程师」）也视为届别线索。
    裸年份取文中最小年份，便于检出偏旧届。
    """
    parsed = parse_grad_batch(text)
    if parsed:
        return parsed
    if not allow_bare_year:
        return None
    years: list[int] = []
    for m in _BARE_TITLE_YEAR_RE.finditer(text or ""):
        try:
            years.append(int(m.group("y")))
        except ValueError:
            continue
    if not years:
        return None
    return f"{min(years)}届"


def resolve_grad_batch(
    *,
    title: str | None = None,
    jd_text: str | None = None,
    existing: str | None = None,
    today: date | datetime | None = None,
) -> str:
    """优先标题/JD 显式届别 → 已有字段 → 默认 current_grad_batch()。

    标题里的 25届/26届 等不得被目标年（如 2027届）覆盖。
    """
    for cand in (
        extract_grad_batch(title, allow_bare_year=True),
        parse_grad_batch(jd_text),
        normalize_grad_batch(existing),
    ):
        if cand and re.match(r"^20\d{2}届$", str(cand).strip()):
            return str(cand).strip()
    return current_grad_batch(today)


def grad_batch_year(batch: str | None) -> int | None:
    m = re.match(r"^(20\d{2})届$", (batch or "").strip())
    return int(m.group(1)) if m else None


def is_stale_grad_batch(
    batch: str | None,
    *,
    today: date | datetime | None = None,
    recruit_bucket: str | None = None,
    title: str | None = None,
) -> bool:
    """
    届别早于当前目标届 → 过旧。
    实习岗（日常实习 / 标题含实习）不视为过旧，避免误伤。
    """
    text = f"{recruit_bucket or ''} {title or ''}"
    if "实习" in text or (recruit_bucket or "") == "日常实习":
        return False
    y = grad_batch_year(batch)
    if y is None:
        return False
    target = grad_batch_year(current_grad_batch(today)) or 0
    return y < target


def prefer_current_grad_batch(
    title: str | None,
    jd_text: str | None = None,
    *,
    today: date | datetime | None = None,
) -> bool:
    """标题/JD 明确命中目标届（如 2027届 / 27届 / 2027应届）时优先保留。"""
    target = current_grad_batch(today)
    year = target.replace("届", "")
    short = year[-2:] if len(year) >= 2 else year
    blob = f"{title or ''} {jd_text or ''}"
    return bool(
        re.search(rf"(?:{year}|{short})\s*届|(?:{year}|{short})\s*应届", blob)
    )


def is_intern_hiring(
    *,
    title: str | None = None,
    jd_text: str | None = None,
    recruit_bucket: str | None = None,
    recruit_project: str | None = None,
) -> bool:
    """日常实习 / 实习生信号。"""
    if (recruit_bucket or "").strip() in ("应届生实习", "日常实习"):
        return True
    if map_recruit_bucket(recruit_project, title) in ("应届生实习", "日常实习"):
        return True
    blob = f"{recruit_project or ''} {title or ''} {(jd_text or '')[:800]}"
    return any(k in blob for k in ("日常实习", "实习生", "实习岗", "暑假实习", "寒假实习", "暑期实习"))


_SOCIAL_HIRING_RE = re.compile(
    r"(社会招聘|社招|社会人才|社会招聘正式|正式社招|非校园招聘)",
    re.I,
)


_LABELED_SOCIAL_RE = re.compile(
    r"(?:招聘类别|招聘类型|招聘项目|类型)\s*[:：]?\s*(社会招聘|社招)\b",
    re.I,
)
_LABELED_CAMPUS_RE = re.compile(
    r"(?:招聘类别|招聘类型|招聘项目|类型)\s*[:：]?\s*(校园招聘|校招|秋招|春招)\b",
    re.I,
)


def is_social_hiring(
    *,
    title: str | None = None,
    jd_text: str | None = None,
    recruit_project: str | None = None,
) -> bool:
    """
    社招 / 非校招正式招聘。

    优先级：recruit_project 或 JD「招聘类别：社会招聘」等标签字段 > 标题笼统校招词。
    标题含校招但正文招聘类别为社会招聘 → 仍判社招。
    """
    rp = (recruit_project or "").strip()
    if rp and _SOCIAL_HIRING_RE.search(rp):
        # project 自身写明社招（即使同字段无校招词）
        if not any(k in rp for k in ("校园招聘", "校招", "秋招", "春招")):
            return True
    jd = jd_text or ""
    if _LABELED_SOCIAL_RE.search(jd):
        # 标签字段明确社招，即使标题有校招/应届
        return True
    if _LABELED_CAMPUS_RE.search(jd):
        return False
    blob = f"{rp} {title or ''} {jd[:800]}"
    if any(k in blob for k in ("校园招聘", "校招", "秋招", "春招", "应届")):
        return False
    return bool(_SOCIAL_HIRING_RE.search(blob))


_CAMPUS_OR_INTERN_KEEP_RE = re.compile(
    r"(校园招聘|校招|校园|实习生招聘|实习招聘|日常实习|暑期实习|暑假实习|寒假实习|秋招|春招)"
)


def is_clear_social_hire_job(job: dict | None) -> bool:
    """
    「校园招聘识别」用：明确社招则应软删。

    覆盖 recruit_project / raw_category / title / jd 中的社会招聘信号。
    """
    src = job or {}
    title = str(src.get("title") or "") or None
    jd = str(src.get("jd_text") or "") or None
    project = str(src.get("recruit_project") or "") or None
    raw_cat = str(src.get("raw_category") or "")
    bucket = str(src.get("recruit_bucket") or "")

    if is_social_hiring(title=title, jd_text=jd, recruit_project=project):
        return True
    if _SOCIAL_HIRING_RE.search(raw_cat) and not _CAMPUS_OR_INTERN_KEEP_RE.search(raw_cat):
        return True
    blob = f"{project or ''} {bucket} {raw_cat} {title or ''} {(jd or '')[:800]}"
    if _SOCIAL_HIRING_RE.search(blob):
        # 正文/字段含社招，且无校招/实习保留信号 → 删
        if not _CAMPUS_OR_INTERN_KEEP_RE.search(blob) and "应届" not in blob:
            return True
        # 标签式「招聘类别：社会招聘」已在 is_social_hiring；此处再拦裸「社会招聘」强信号
        if re.search(r"社会招聘", blob) and not re.search(
            r"(校园招聘|实习生招聘|日常实习)", blob
        ):
            return True
    return False


def campus_recognition_action(job: dict | None) -> str:
    """返回 keep | delete（仅社招删；校招/实习/不明均保留）。"""
    return "delete" if is_clear_social_hire_job(job) else "keep"


def campus_publish_cutoff(today: date | datetime | None = None) -> date:
    """
    目标届招聘季「合理窗口」起点：date(目标届年份 - 2, 1, 1)。

    例：公历 2026 → 目标届 2027 → 窗口起点 2025-01-01。
    发布时间早于该日（如 2024-09-18）视为非当年校招信息。
    等价年份口径：publish_year <= 目标届年份 - 2 时，完整日期亦必早于上述起点。
    """
    target = current_grad_batch(today)
    if today is None:
        from app.timeutil import today as cn_today

        ty_base = cn_today().year + 1
    elif isinstance(today, datetime):
        ty_base = today.date().year + 1
    else:
        ty_base = today.year + 1
    ty = grad_batch_year(target) or ty_base
    return date(ty - 2, 1, 1)


def resolve_publish_date(
    *,
    open_at: str | None = None,
    published_at: str | None = None,
    jd_text: str | None = None,
) -> date | None:
    """优先 open_at / published_at，其次 JD 标签「发布时间」抽取。"""
    for raw in (open_at, published_at):
        d = parse_date_loose(raw)
        if d:
            return d
    if jd_text and ("发布时间" in jd_text or "发布日期" in jd_text or "开放时间" in jd_text):
        try:
            from app.collector.label_fields import extract_labeled_fields

            labels = extract_labeled_fields(jd_text)
            return parse_date_loose(labels.get("open_at"))
        except Exception:  # noqa: BLE001
            return None
    return None


def is_outdated_campus_publish(
    *,
    open_at: str | None = None,
    published_at: str | None = None,
    jd_text: str | None = None,
    today: date | datetime | None = None,
) -> tuple[bool, str]:
    """
    补充规则：校招发布时间明显偏旧 → 非当年校招信息。

    判定：解析到的发布日 < campus_publish_cutoff(today)
    （即早于「目标届年份-2」年 1 月 1 日）。
    与 lookback/stale_over_3m 并存，不替代。
    """
    pub = resolve_publish_date(open_at=open_at, published_at=published_at, jd_text=jd_text)
    if pub is None:
        return False, ""
    cutoff = campus_publish_cutoff(today)
    if pub < cutoff:
        target = current_grad_batch(today)
        return (
            True,
            f"发布时间 {pub.isoformat()} 早于目标届{target}招聘季窗口（{cutoff.isoformat()} 起），非当年校招信息",
        )
    return False, ""


def is_target_campus_or_intern(
    *,
    title: str | None = None,
    jd_text: str | None = None,
    recruit_bucket: str | None = None,
    recruit_project: str | None = None,
    graduation_batch: str | None = None,
    open_at: str | None = None,
    published_at: str | None = None,
    today: date | datetime | None = None,
) -> tuple[bool, str, str]:
    """
    正常 vs 异常口径：
    - 正常：日常实习/实习生，或校招且届别=目标届（公历年+1）
    - 异常：更旧届、社招、校招届别对不上目标届且无实习信号
    - 补充（叠加，不削弱既有规则）：
      1) 标题含届别/裸年份且非目标届 → wrong_grad_batch
      2) 发布时间早于目标届招聘季窗口 → not_current_campus
      实习岗不因旧发布时间误杀；仅当标题明确旧届（届/应届）时仍判异常

    返回 (是否正常, kind, reason)。
    kind 为空表示正常；异常为 wrong_grad_batch / not_target_hiring / not_current_campus。
    """
    target = current_grad_batch(today)
    ty = grad_batch_year(target) or 0

    intern = is_intern_hiring(
        title=title,
        jd_text=jd_text,
        recruit_bucket=recruit_bucket,
        recruit_project=recruit_project,
    )
    if intern:
        # 实习：默认正常；标题明确旧届校招（届/应届）仍异常；不因旧发布时间误杀
        title_batch = parse_grad_batch(title)  # 不用裸年份，避免误杀
        if title_batch and title_batch != target:
            y = grad_batch_year(title_batch)
            if y is not None and y < ty:
                return (
                    False,
                    "wrong_grad_batch",
                    f"实习岗标题含旧届{title_batch}，当前目标为{target}",
                )
        return True, "", ""

    if is_social_hiring(title=title, jd_text=jd_text, recruit_project=recruit_project):
        return False, "not_target_hiring", "社招/非目标招聘（非日常实习且非目标应届校招）"

    # 检查用：标题允许裸年份；JD 仅 届/应届，避免正文年份误伤
    parsed = (
        extract_grad_batch(title, allow_bare_year=True)
        or parse_grad_batch(jd_text)
        or (
            graduation_batch.strip()
            if graduation_batch and re.match(r"^20\d{2}届$", graduation_batch.strip())
            else None
        )
    )
    if parsed:
        if parsed == target:
            # 届别正确时仍检查过旧发布时间（叠加）
            old_pub, pub_reason = is_outdated_campus_publish(
                open_at=open_at,
                published_at=published_at,
                jd_text=jd_text,
                today=today,
            )
            if old_pub:
                return False, "not_current_campus", pub_reason
            return True, "", ""
        y = grad_batch_year(parsed)
        if y is not None and y < ty:
            return (
                False,
                "wrong_grad_batch",
                f"校招届别为{parsed}，当前目标为{target}",
            )
        return (
            False,
            "wrong_grad_batch",
            f"校招届别为{parsed}，与目标{target}不符",
        )

    # 无明确届别：再叠加发布时间偏旧检查
    old_pub, pub_reason = is_outdated_campus_publish(
        open_at=open_at,
        published_at=published_at,
        jd_text=jd_text,
        today=today,
    )
    if old_pub:
        return False, "not_current_campus", pub_reason

    # 无明确届别且发布时间不旧：按校招默认面向目标届 → 正常
    return True, "", ""


# 校招侧关键词
CAMPUS_KEYWORDS = (
    "校招",
    "校园招聘",
    "秋招",
    "春招",
    "正式批",
    "提前批",
    "管培生",
    "应届",
    "毕业生",
)

# 日常实习侧关键词
INTERN_KEYWORDS = (
    "实习",
    "日常实习",
    "暑假实习",
    "寒假实习",
    "暑期",
    "寒假",
    "同学享",
)

AGGREGATOR_HINTS = (
    "汇总",
    "合集",
    "信息差",
    "海投",
    "每日岗",
    "整理了",
    "N家",
    "家公司",
    "offer研习社",
    "校招汇总",
)


def should_skip_sheet(sheet_name: str) -> bool:
    name = (sheet_name or "").strip()
    for kw in SKIP_SHEET_KEYWORDS:
        if kw in name:
            return True
    return False


def clean_company_nature(value: str | None) -> str | None:
    if not value:
        return None
    s = str(value).strip().replace("\n", "")
    if not s:
        return None
    # 直接命中词表
    if s in COMPANY_NATURES:
        return "民企" if s == "民营企业" else ("外企" if s == "外资" else s)
    # 短词包含
    for nature in COMPANY_NATURES:
        if nature in s and len(s) <= 12:
            if nature == "民营企业":
                return "民企"
            if nature == "外资":
                return "外企"
            return nature
    # 非法（混入标题等）置空
    if len(s) > 10 or re.search(r"招聘|公告|启动|开启|投递", s):
        return None
    return None


def map_recruit_bucket(recruit_project: str | None, title: str | None = None) -> str | None:
    """
    招聘项目/标题 → 大类 bucket。
    明确社招（社会招聘/社招）优先，不得落成「校招」；标题校招词也不能覆盖社招 project。
    """
    rp = (recruit_project or "").strip()
    if rp and _SOCIAL_HIRING_RE.search(rp) and not any(
        k in rp for k in ("校园招聘", "校招", "秋招", "春招")
    ):
        return None
    text = f"{rp} {title or ''}"
    if not text.strip():
        return None
    # 实习优先于笼统「招聘」
    for kw in INTERN_KEYWORDS:
        if kw in text:
            if re.search(
                r"(应届(?:生)?实习|校招实习|校园实习|毕业实习|"
                r"(?:20\d{2}|[2-3]\d)\s*届[^\n]{0,20}实习|"
                r"实习[^\n]{0,20}(?:20\d{2}|[2-3]\d)\s*届)",
                text,
                re.I,
            ):
                return "应届生实习"
            return "日常实习"
    # 仅当 project 不是社招时，才用标题/项目中的校招词
    for kw in CAMPUS_KEYWORDS:
        if kw in text:
            return "校招"
    return None


RECRUIT_BUCKETS = ("校招", "应届生实习", "日常实习")


def normalize_recruit_bucket(
    value: str | None,
    *,
    recruit_project: str | None = None,
    title: str | None = None,
) -> str | None:
    """将历史/页面类型统一为 Viewer 使用的三类岗位类型。"""
    raw = (value or "").strip()
    if raw:
        mapped = map_recruit_bucket(raw, title)
        if mapped:
            return mapped
    return map_recruit_bucket(recruit_project, title)


def is_aggregator_text(text: str, extra_patterns: Iterable[str] | None = None) -> bool:
    s = text or ""
    patterns = list(AGGREGATOR_HINTS)
    if extra_patterns:
        patterns.extend(extra_patterns)
    for p in patterns:
        if p and p in s:
            return True
    if re.search(r"整理了\s*\d+\s*家", s):
        return True
    return False


def extract_job_tags(title: str | None) -> list[str]:
    """从标题抽粗标签（非 Boss 细类）。"""
    if not title:
        return []
    mapping = {
        "算法": ("算法", "机器学习", "深度学习", "AI"),
        "前端": ("前端", "Frontend", "Web前端"),
        "后端": ("后端", "服务端", "Java", "Go开发", "Python开发"),
        "客户端": ("客户端", "Android", "iOS", "鸿蒙"),
        "测试": ("测试", "QA", "质量保障"),
        "产品": ("产品经理", "产品实习", "产品岗"),
        "运营": ("运营", "用户增长"),
        "数据": ("数据开发", "数据分析", "数据科学", "数仓"),
        "硬件": ("硬件", "嵌入式", "芯片", "IC"),
        "管培": ("管培生", "管培"),
    }
    tags: list[str] = []
    for tag, kws in mapping.items():
        if any(k.lower() in title.lower() if k.isascii() else k in title for k in kws):
            tags.append(tag)
    return tags


def looks_like_url(value: str | None) -> bool:
    if not value:
        return False
    s = value.strip().lower()
    return s.startswith("http://") or s.startswith("https://")


# 可信校招 ATS / 网申宿主（有此类 hint 时可自动标 official 并进入采集）
TRUSTED_ATS_HOSTS = (
    "mokahr.com",
    "moka.com",
    "zhiye.com",
    "jobs.feishu.cn",
    "jobs.f.mioffice.cn",
    "italent.cn",
    "tms.beisen.com",
    "hotjob.cn",
    "wecruit.hotjob.cn",
)


def host_of_url(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url or "").netloc.lower()
    except Exception:
        return ""


def is_trusted_ats_url(url: str | None) -> bool:
    """是否为常见官方网申/ATS 链接（不含纯公众号）。"""
    if not looks_like_url(url):
        return False
    host = host_of_url(url or "")
    if not host:
        return False
    return any(h in host for h in TRUSTED_ATS_HOSTS)


def is_campus_apply_url(url: str | None) -> bool:
    """疑似校招网申/职位页（ATS 或路径含 campus/recruit 等）。"""
    if not looks_like_url(url):
        return False
    if is_trusted_ats_url(url):
        return True
    u = (url or "").lower()
    return any(
        p in u
        for p in (
            "/campus",
            "campus_",
            "/recruit",
            "/career",
            "/school",
            "/joinus",
            "job.bank",
            "/jobs",
        )
    )


def parse_url_list(raw: Any) -> list[str]:
    """解析 DB 中 JSON 列表或 Python list。"""
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if looks_like_url(str(x))]
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return [raw.strip()] if looks_like_url(raw) else []
        if isinstance(data, list):
            return [str(x).strip() for x in data if looks_like_url(str(x))]
    return []


def prefer_apply_urls(urls: list[str], *, limit: int = 8) -> list[str]:
    """优先详情/ATS，其次普通校招页；去重保序。"""
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for u in urls:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        low = u.lower()
        score = 0
        if is_trusted_ats_url(u):
            score += 100
        if any(k in low for k in ("detail", "jobadid", "/position/", "/job/", "campus_apply")):
            score += 50
        if is_campus_apply_url(u):
            score += 20
        if "mp.weixin.qq.com" in low:
            score -= 30
        if is_noise_nav_url(u):
            score -= 80
        scored.append((score, u))
    scored.sort(key=lambda x: -x[0])
    return [u for _, u in scored[:limit]]


# ---- 采集质量：噪声标题 / 详情链接 / 去重归一化 ----

NOISE_TITLE_EXACT = frozenset(
    {
        "提示",
        "个人中心",
        "登录",
        "注册",
        "首页",
        "微官网",
        "微官网招聘系统",
        "招聘系统",
        "招聘系统首页",
        "校园招聘系统",
        # Moka SPA 页脚 / 壳文案（勿当岗位名）
        "Moka，智能化招聘管理系统",
        "moka，智能化招聘管理系统",
        "智能化招聘管理系统",
        "欢迎登录",
        "请登录",
        "修改密码",
        "忘记密码",
        "退出",
        "退出登录",
        "返回",
        "更多",
        "用户协议",
        "隐私政策",
        "验证码",
        "我的简历",
        "投递记录",
        "消息中心",
        "帮助中心",
        # 门户壳 / ATS 空页标题
        "招聘详细",
        "招聘系统--招聘详细",
        "招聘系统-招聘详细",
        "招聘首页",
        "招聘门户",
        "招贤纳才",
        "search jobs",
        "jobs at apple",
        "careers",
        "career",
        "找到你的理想职位",
        "找到你的理想职位。",
        "not found",
        "404",
        "404 not found",
        "page not found",
        "security verification",
        "安全验证",
        "人机验证",
        # 导航 / 行程 / 指南（非岗位）
        "校招行程",
        "招聘行程",
        "宣讲行程",
        "宣讲会行程",
        "应聘指南",
        "了解我们",
        "关于我们",
        "职位搜索",
        "搜索职位",
        "登录/注册",
        "登录注册",
        "招聘人数",
        # 列表表头误当岗位名
        "职位名称",
        "岗位名称",
        "职位类别",
        "岗位类别",
        "职位类型",
        "岗位类型",
        "工作类型",
        "工作地点",
        "工作城市",
        "更新日期",
        "更新时间",
        "发布时间",
        "发布日期",
        # 关停 / 内推壳页（整标题）
        "当前网页已关停",
        "网页已关停",
        "网页已关闭",
        "页面已关停",
        "页面已关闭",
        "职位已关闭",
        "岗位已关闭",
        "招聘已关闭",
        "已下线",
        "停止招聘",
        "招聘已停止",
        "招聘已结束",
        "内部推荐",
        "仅限内推",
        "内推专用",
        "仅内部推荐",
        "内部推荐专用",
    }
)

# 未渲染模板 / Vue 占位，不得当岗位名
TEMPLATE_TOKEN_RE = re.compile(
    r"(\{\{|\}\}|item\.(postName|workPlace|positionName|jobName|name)\b|"
    r"\$\{[^}]+\}|<%[=-]?.+?%>)",
    re.I,
)

NOISE_TITLE_RE = re.compile(
    r"^(提示|个人中心|登录|注册|首页|微官网|招聘系统(首页)?|"
    r"校园招聘系统|欢迎登录|请登录|修改密码|忘记密码|"
    r"退出(登录)?|用户协议|隐私政策|我的简历|投递记录|消息中心|帮助中心|"
    r"招聘详细|招聘首页|招聘门户|招贤纳才|search\s*jobs|jobs\s+at\s+apple|"
    r"careers?|找到你的理想职位。?|"
    r"not\s*found|404(\s*not\s*found)?|page\s*not\s*found|"
    r"security\s*verification|安全验证|人机验证|"
    r"校招行程|招聘行程|宣讲行程|宣讲会行程|应聘指南|了解我们|关于我们|"
    r"职位搜索|搜索职位|登录[/／]?注册|"
    r".{0,8}微官网.{0,8}|.{0,40}招聘系统[-—–]{1,2}招聘详细|"
    r".{0,6}招聘系统(首页)?)$",
    re.I,
)

# hotjob/wecruit SPA 壳：IE 兼容提示被抽成「岗位名」
IE_BROWSER_TIP_TITLE_RE = re.compile(
    r"(兼容模式|旧版\s*IE|IE\s*浏览器|温馨提示|"
    r"You need to enable JavaScript|"
    r"(?:Chrome|Firefox|Edge|Safari).{0,20}(?:Chrome|Firefox|Edge|Safari))",
    re.I,
)

# 含行程/指南等导航语义（允许前后缀，如「2026校招行程」）
NOISE_NAV_TITLE_RE = re.compile(
    r"(校招行程|招聘行程|宣讲行程|宣讲会行程|应聘指南|了解我们|关于我们|"
    r"职位搜索|搜索职位)",
    re.I,
)

# 站点 chrome / 搜索结果页标题（非整岗位名）：Search Jobs - … - 招贤纳才
CHROME_TITLE_RE = re.compile(
    r"(^search\s*jobs\b|"
    r"\bjobs\s+at\s+apple\b|"
    r"招贤纳才|"
    r"招聘首页|"
    r"招聘门户|"
    r"招聘官网|"
    r"招聘网站|"
    r"职位列表|"
    r"找到你的理想职位|"
    r"^careers?\s*[-—–|]|"
    r"[-—–|]\s*careers?\s*$|"
    # 「中国五矿集团招聘官网」「××校园招聘官网」等门户壳
    r".{2,40}招聘官网$|"
    r".{2,40}招聘门户$|"
    r".{2,30}(集团|股份)?有限公司?招聘官网$)",
    re.I,
)

# 详情标题常见 chrome 后缀：Role - 招贤纳才(中国)
CHROME_TITLE_SUFFIX_RE = re.compile(
    r"\s*[-—–|]\s*(?:招贤纳才|Jobs\s+at\s+Apple|Careers?|招聘首页|招聘门户)"
    r"(?:\s*[(（][^)）]{0,20}[)）])?\s*$",
    re.I,
)

# JD 内岗位码行：CN-Store Leader 114438029
JOB_CODE_TITLE_RE = re.compile(
    r"^((?:[A-Z]{2}-)?[A-Za-z\u4e00-\u9fff][\w\s/&:,.+·-]{1,70}?)\s+(\d{6,})\b",
)

# base 地噪声（导航/筛选项，非真实城市）
NOISE_LOCATION_EXACT = frozenset(
    {
        "团队",
        "team",
        "teams",
        "首页",
        "更多",
        "筛选",
        "搜索",
        "结果",
        "排序",
        "地点",
        "工作地点",
        "location",
        "locations",
        "关键词",
        "精确搜索",
        "搜索结果",
        "全部",
        "不限",
        "招聘人数",
        "需求人数",
        "职位类别",
        "工作类型",
        "更新日期",
        "发布时间",
    }
)

# 岗位标题含关停/内推专用等 → 无效条目（近似文案）
CLOSED_OR_REFERRAL_TITLE_RE = re.compile(
    r"(当前网页已关停|网页已关停|网页已关闭|页面已关停|页面已关闭|"
    r"职位已关闭|岗位已关闭|招聘已关闭|职位已下线|岗位已下线|"
    r"停止招聘|招聘已停止|招聘已结束|"
    r"仅限内推|内推专用|仅内部推荐|内部推荐专用|"
    r"内部推荐|^已下线$)",
    re.I,
)

# 详情正文明确关停 / 仅内部推荐
CLOSED_OR_REFERRAL_JD_RE = re.compile(
    r"(职位已关闭|岗位已关闭|该职位已关闭|该岗位已关闭|"
    r"招聘已结束|已停止招聘|停止招聘|职位已下线|岗位已下线|"
    r"当前网页已关停|网页已关停|网页已关闭|页面已关停|页面已关闭|"
    r"仅内部推荐|仅限内推|内推专用|只接受内部推荐|仅接受内部推荐|"
    r"本职位仅限内部|仅对内部开放|仅限内部推荐)",
    re.I,
)

NOISE_NAV_URL_RE = re.compile(
    r"(login|logout|register|signin|signup|password|forgot|"
    r"/user(?:/|$)|/account(?:/|$)|/ucenter|/passport|"
    r"个人中心|登录|注册|/help(?:/|$)|/about(?:/|$)|/privacy|"
    r"getAboutUs|aboutUs|schedule|行程|twoColumnId=100301)",
    re.I,
)

JOB_DETAIL_URL_RE = re.compile(
    r"(job[_-]?detail|/details?/\d+|/jobs?/|jobadid|positionid|/position/[^/]+/detail|/position(?:/|$)|"
    r"/post(?:/|$)|campus_apply|/recruitment/detail|announcement|/notice(?:/|$)|"
    r"/career/.+/detail|/school/.+/detail)",
    re.I,
)

# 招聘站导航/壳页行，不应进入岗位介绍
CAREER_NAV_LINE_RE = re.compile(
    r"^(Overview|Summary|Work at Apple|Explore working(?:\s+at\s+Apple)?|"
    r"Life at Apple|Teams?|Apple Retail|Students?|"
    r"Related jobs|Similar jobs|Share this job|Apply(?:\s+now)?|"
    r"Search Jobs|Find your role|See all jobs|Browse jobs|"
    r"招贤纳才|招聘首页|招聘门户|职位列表|校园招聘|"
    r"团队|学生|零售|概览|相关职位|相似职位|立即申请)\s*$",
    re.I,
)

JOB_DETAIL_TEXT_RE = re.compile(
    r"(详情|岗位|职位|投递|申请|实习|校招|管培|工程师|分析师|"
    r"研究员|专员|经理|开发|运营|产品|设计|财务|法务|销售)",
    re.I,
)

# 标题里像「另一家公司」的实体（与种子名比对）
TITLE_COMPANY_ENTITY_RE = re.compile(
    r"([\u4e00-\u9fffA-Za-z0-9]{2,24}"
    r"(?:股份有限公司|有限责任公司|有限公司|集团股份|集团|基金|银行|证券|保险|"
    r"生物科技|生物|科技|制药|药业|电子|通信|汽车|地产|置业))"
)


def is_closed_or_referral_title(title: str | None) -> bool:
    """岗位名称像关停页 / 仅内推壳，不应作为有效岗位。"""
    t = (title or "").strip()
    if not t:
        return False
    compact = re.sub(r"\s+", "", t)
    if compact in NOISE_TITLE_EXACT and any(
        k in compact
        for k in (
            "关停",
            "关闭",
            "已下线",
            "停止招聘",
            "招聘已停止",
            "招聘已结束",
            "内部推荐",
            "内推",
        )
    ):
        return True
    return bool(CLOSED_OR_REFERRAL_TITLE_RE.search(compact) or CLOSED_OR_REFERRAL_TITLE_RE.search(t))


def is_closed_or_referral_jd(jd_text: str | None) -> bool:
    """详情正文明确职位已关闭或仅内部推荐。"""
    jd = (jd_text or "").strip()
    if not jd:
        return False
    # 长 JD 里偶发「欢迎内推」不算；仅命中明确关停/仅内推句式
    return bool(CLOSED_OR_REFERRAL_JD_RE.search(jd))


def is_invalid_closed_or_referral_job(
    *,
    title: str | None = None,
    jd_text: str | None = None,
) -> bool:
    """标题或正文命中关停/仅内推 → 不入库或应软删。"""
    return is_closed_or_referral_title(title) or is_closed_or_referral_jd(jd_text)


def strip_chrome_title_suffix(title: str | None) -> str | None:
    """去掉「岗位名 - 招贤纳才(中国)」类站点后缀，保留真实角色名。"""
    t = (title or "").strip()
    if not t:
        return None
    cleaned = CHROME_TITLE_SUFFIX_RE.sub("", t).strip(" -—–|\t")
    return cleaned or None


def is_chrome_shell_title(title: str | None) -> bool:
    """页面/搜索 chrome（Search Jobs、招贤纳才等），不能当岗位名称。"""
    t = (title or "").strip()
    if not t:
        return False
    compact = re.sub(r"\s+", "", t)
    if compact in NOISE_TITLE_EXACT or t.lower() in NOISE_TITLE_EXACT:
        # 仅 chrome 类 exact；关停等走 is_noise_title
        if compact in {
            "招贤纳才",
            "招聘首页",
            "招聘门户",
            "searchjobs",
            "jobsatapple",
            "careers",
            "career",
            "找到你的理想职位",
            "找到你的理想职位。",
        } or t.lower() in {
            "search jobs",
            "jobs at apple",
            "careers",
            "career",
        }:
            return True
    if CHROME_TITLE_RE.search(t) or CHROME_TITLE_RE.search(compact):
        return True
    if IE_BROWSER_TIP_TITLE_RE.search(t) or IE_BROWSER_TIP_TITLE_RE.search(compact):
        return True
    # 多段 chrome：Search Jobs - 中国内地 - 招贤纳才 (中国)
    parts = [p.strip() for p in re.split(r"\s*[-—–|]\s*", t) if p.strip()]
    if len(parts) >= 2:
        chrome_hits = sum(1 for p in parts if CHROME_TITLE_RE.search(p) or is_noise_title_exact(p))
        role_hits = sum(1 for p in parts if any(h in p for h in JOB_ROLE_HINTS))
        if chrome_hits >= 1 and role_hits == 0:
            return True
    return False


def is_noise_title_exact(title: str | None) -> bool:
    t = (title or "").strip()
    if not t:
        return True
    compact = re.sub(r"\s+", "", t)
    return (
        compact in NOISE_TITLE_EXACT
        or t in NOISE_TITLE_EXACT
        or compact.lower() in NOISE_TITLE_EXACT
        or t.lower() in NOISE_TITLE_EXACT
    )


def sanitize_job_title(title: str | None) -> str | None:
    """清洗岗位名：去 chrome 后缀；仍是壳标题则返回 None（软失败，勿入库）。"""
    t = strip_chrome_title_suffix(title)
    if not t or is_noise_title(t) or is_chrome_shell_title(t):
        return None
    return t


def strip_career_nav_boilerplate(jd_text: str | None) -> str | None:
    """去掉招聘站导航/概览壳行，保留该岗位正文。"""
    text = (jd_text or "").strip()
    if not text:
        return None
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        if CAREER_NAV_LINE_RE.match(line):
            continue
        if is_chrome_shell_title(line):
            continue
        out.append(raw.rstrip())
    cleaned = "\n".join(out).strip()
    return cleaned or None


def is_noise_location(location: str | None) -> bool:
    """导航/筛选项误当 base 地。"""
    loc = (location or "").strip()
    if not loc:
        return True
    compact = re.sub(r"\s+", "", loc)
    if compact.lower() in NOISE_LOCATION_EXACT or loc.lower() in NOISE_LOCATION_EXACT:
        return True
    if len(compact) <= 1:
        return True
    # 纯导航词
    if compact in ("团队", "首页", "更多", "筛选", "搜索"):
        return True
    return False


# 「公司名 - 实习/校招」门户页标题，不是具体岗位
COMPANY_CHANNEL_TITLE_RE = re.compile(
    r"^.+?\s*[-—–|｜]\s*"
    r"(?:实习生专项|实习招聘|实习生招聘|日常实习|暑期实习|校园招聘|社会招聘|"
    r"校招|社招|实习)\s*$",
    re.I,
)


def is_noise_title(title: str | None) -> bool:
    """导航/登录/门户壳页/站点 chrome/关停内推标题，不应作为岗位发布。"""
    raw = (title or "").strip()
    if not raw or len(raw) < 2:
        return True
    # 占位/截断垃圾：xx、xxx、--、……（超时空壳或解析失败常见）
    compact0 = re.sub(r"\s+", "", raw)
    if re.fullmatch(r"[xX×*]{2,8}|[-—_]{2,8}|[.。…‧]{2,8}|[?]{2,8}|[暂无未知]+", compact0):
        return True
    if compact0.lower() in {"n/a", "na", "null", "none", "undefined", "test", "demo"}:
        return True
    # 未渲染模板占位（{{item.postName}} 等）
    if TEMPLATE_TOKEN_RE.search(raw):
        return True
    # 「宁德新能源 (ATL) - 实习」类频道壳标题
    if COMPANY_CHANNEL_TITLE_RE.match(raw):
        return True
    # 「Role - 招贤纳才」可剥离后缀；纯 Search Jobs / 招贤纳才 仍判噪声
    stripped = strip_chrome_title_suffix(raw)
    if stripped and stripped != raw and not is_chrome_shell_title(stripped):
        t = stripped
    else:
        t = stripped or raw
        if is_chrome_shell_title(raw) or is_chrome_shell_title(t):
            return True
    compact = re.sub(r"\s+", "", t)
    compact_l = compact.lower()
    if (
        compact in NOISE_TITLE_EXACT
        or t in NOISE_TITLE_EXACT
        or compact_l in NOISE_TITLE_EXACT
        or t.lower() in NOISE_TITLE_EXACT
    ):
        return True
    if NOISE_TITLE_RE.match(compact) or NOISE_TITLE_RE.match(t):
        return True
    # hotjob SPA IE 提示等（含 emoji 浏览器清单）
    if IE_BROWSER_TIP_TITLE_RE.search(t) or IE_BROWSER_TIP_TITLE_RE.search(compact):
        return True
    # 校招行程 / 应聘指南等导航文案（含前后缀）
    if NOISE_NAV_TITLE_RE.search(compact) or NOISE_NAV_TITLE_RE.search(t):
        return True
    # 「某某公司招聘系统--招聘详细」类壳标题
    if re.search(r"招聘系统\s*[-—–]{1,2}\s*招聘详细", t) or compact.endswith("招聘详细"):
        if "岗" not in compact and "工程师" not in compact and "实习" not in compact:
            return True
    if is_closed_or_referral_title(t):
        return True
    # 极短且无招聘语义
    if len(compact) <= 4 and not any(k in compact for k in ("校招", "实习", "招聘", "管培")):
        if compact in ("首页", "提示", "公告", "通知", "详情", "更多"):
            return True
    return False


def is_noise_nav_url(url: str | None, link_text: str | None = None) -> bool:
    """登录/个人中心等导航链接。"""
    u = url or ""
    if NOISE_NAV_URL_RE.search(u):
        return True
    text = (link_text or "").strip()
    if text and is_noise_title(text):
        return True
    return False


def is_job_detail_link(url: str, link_text: str | None = None) -> bool:
    """是否像职位/公告详情（而非校招门户首页或导航）。"""
    if is_noise_nav_url(url, link_text):
        return False
    if JOB_DETAIL_URL_RE.search(url or ""):
        return True
    text = (link_text or "").strip()
    if text and JOB_DETAIL_TEXT_RE.search(text) and not is_noise_title(text):
        # 文本像岗位，且 URL 不是纯门户根路径
        low = (url or "").lower().rstrip("/")
        if low.endswith(("/campus", "/career", "/careers", "/recruit", "/school", "/joinus")):
            return False
        return True
    return False


_DETAIL_QUERY_KEYS = frozenset(
    {
        "jobid",
        "job_id",
        "jobadid",
        "positionid",
        "position_id",
        "postid",
        "post_id",
        "recruitmentid",
        "noticeid",
    }
)


def is_job_detail_url(url: str | None) -> bool:
    """Strict URL-only detail check; job-list text must not turn a portal into a detail URL."""
    if not looks_like_url(url):
        return False
    raw = (url or "").strip()
    low = raw.lower()
    if "__job=" in low or is_noise_nav_url(raw):
        return False
    parsed = urlparse(raw)
    params = {k.lower(): v for k, v in parse_qsl(parsed.query, keep_blank_values=True)}
    fragment = parsed.fragment or ""
    if "?" in fragment:
        frag_path, frag_query = fragment.split("?", 1)
        params.update({k.lower(): v for k, v in parse_qsl(frag_query, keep_blank_values=True)})
    else:
        frag_path = fragment
    if any(params.get(key) for key in _DETAIL_QUERY_KEYS):
        return True
    combined_path = f"{parsed.path}#{frag_path}".lower()
    if re.search(r"#/(?:job|position)/[^/?#]+", combined_path):
        return True
    if re.search(r"/(?:job|jobs|position|positions|post|posts)/[^/?#]+(?:/detail)?/?$", parsed.path, re.I):
        return True
    return bool(
        re.search(
            r"(job[_-]?detail|posdetail|campusxq|jobadid|positionid|"
            r"/position/[^/]+/detail|/recruitment/detail|/career/.+/detail)",
            raw,
            re.I,
        )
    )


def is_job_portal_listing_url(url: str | None) -> bool:
    """Return True for campus/intern list or portal pages without a concrete job identity."""
    if not looks_like_url(url) or is_job_detail_url(url):
        return False
    parsed = urlparse((url or "").strip())
    low_path = parsed.path.lower().rstrip("/")
    low_frag = (parsed.fragment or "").lower().split("?", 1)[0].rstrip("/")
    if any(
        low_path.endswith(suffix)
        for suffix in (
            "/campus",
            "/campus/jobs",
            "/intern",
            "/intern/jobs",
            "/career",
            "/careers",
            "/recruit",
            "/joinus",
            "/school",
        )
    ):
        return True
    if low_frag in ("/jobs", "/campus", "/intern", "/home"):
        return True
    low = (url or "").lower()
    return any(
        marker in low
        for marker in (
            "/web/index/campus",
            "companylist",
            "/campus-recruitment/",
            "/social-recruitment/",
        )
    )


def is_portal_shell_record(
    *,
    title: str | None,
    jd_text: str | None,
    source_url: str | None = None,
) -> bool:
    """门户首页/空壳：噪声标题或几乎无 JD（真实职位名列表行除外）。"""
    if is_noise_title(title):
        return True
    t = (title or "").strip()
    if any(
        k in t
        for k in (
            "微官网",
            "招聘系统",
            "欢迎登录",
            "Not Found",
            "Security Verification",
            "校园招聘入口",
        )
    ):
        return True
    jd = (jd_text or "").strip()
    if len(jd) < 40:
        # 列表表已抽出的真实岗名（编辑岗/研究岗等）不算门户壳
        if re.search(
            r"(岗|工程师|专员|经理|实习|管培|开发|运营|产品|研究|营销|编辑|设计|销售|顾问)",
            t,
        ):
            return False
        if is_company_plus_recruit_title(None, t) or re.search(r"校园招聘|校招入口", t):
            return True
        return True
    return False


def recover_title_from_jd(jd_text: str | None) -> str | None:
    """详情正文首行、岗位码行或「职位名称」标签中回填真实岗位名。"""
    text = strip_career_nav_boilerplate(jd_text) or (jd_text or "").strip()
    if not text:
        return None
    m = re.search(r"(?:职位名称|岗位名称|Role|Job\s*Title)\s*[:：]\s*([^\n，,]{2,80})", text, re.I)
    if m:
        cand = sanitize_job_title(m.group(1).strip())
        if cand:
            return cand
    # 优先岗位码整行：CN-Store Leader 114438029（含职位编号）
    for line in text.splitlines()[:30]:
        line = re.sub(r"\s+", " ", line).strip()
        if not line or len(line) > 100:
            continue
        cm = JOB_CODE_TITLE_RE.match(line)
        if cm:
            role = cm.group(1).strip()
            full = sanitize_job_title(f"{role} {cm.group(2).strip()}")
            if full and (
                re.match(r"^[A-Z]{2}-", role)
                or any(h.lower() in role.lower() for h in JOB_ROLE_HINTS)
                or re.search(r"(Leader|Manager|Specialist|工程师|经理|专家|实习)", role, re.I)
            ):
                return full
    for line in text.splitlines()[:12]:
        line = re.sub(r"\s+", " ", line).strip()
        if not line or len(line) < 2 or len(line) > 80:
            continue
        if CAREER_NAV_LINE_RE.match(line):
            continue
        if re.match(
            r"^(招聘类别|发布时间|工作地点|工作职责|任职资格|岗位职责|岗位要求|"
            r"职位描述|职位类别|职位类型|学历要求|薪资|福利|搜索结果|Summary|Overview)",
            line,
            re.I,
        ):
            continue
        if "：" in line or ":" in line:
            continue
        cand = sanitize_job_title(line)
        if cand:
            return cand
    return None


def recover_location_from_jd(jd_text: str | None) -> str | None:
    text = (jd_text or "").strip()
    if not text:
        return None
    m = re.search(
        r"(?:工作地点|工作地|工作城市|办公地点|办公地址|所在城市|"
        r"岗位地址|工作地址|base\s*地|上班地点)\s*[:：]?\s*"
        r"([^\n；;|｜]{2,60})",
        text,
        re.I,
    )
    if m:
        loc = re.split(
            r"(?=岗位职责|工作职责|职位职责|任职要求|任职资格|岗位要求|学历要求)",
            m.group(1),
            maxsplit=1,
        )[0].strip(" \t-—–:：，,")
        loc = re.sub(r"\s*[·•]\s*", "·", loc)
        if not is_noise_location(loc):
            return loc
    return None


def strip_recruit_channel_company_suffix(name: str | None) -> str:
    """
    去掉误拼进公司名的频道后缀。
    例：「宁德新能源 (ATL) - 实习」→「宁德新能源 (ATL)」；
    「××实习生专项」→「××」。
    """
    s = (name or "").strip()
    if not s:
        return ""
    prev = None
    while s and s != prev:
        prev = s
        s2 = re.sub(
            r"(?:\s*[-—–|｜]\s*|\s+)"
            r"(?:实习生专项|实习招聘|实习生招聘|日常实习|暑期实习|校园招聘|社会招聘|"
            r"校招|社招|实习)\s*$",
            "",
            s,
            flags=re.I,
        ).strip()
        s2 = re.sub(
            r"[（(]\s*(?:实习|实习生|校招|社招|校园招聘|社会招聘|实习生专项)\s*[）)]\s*$",
            "",
            s2,
            flags=re.I,
        ).strip()
        s2 = re.sub(r"(?:实习生专项|实习招聘)\s*$", "", s2, flags=re.I).strip()
        s = s2
    return s


def company_name_core(name: str | None) -> str:
    """去掉常见后缀，得到可比对的核心名。"""
    s = strip_recruit_channel_company_suffix(name)
    for suf in (
        "股份有限公司",
        "有限责任公司",
        "有限公司",
        "集团股份",
        "集团",
        "控股",
        "股份",
    ):
        if s.endswith(suf) and len(s) > len(suf) + 1:
            s = s[: -len(suf)]
    return s.strip()


def title_company_mismatch(company: str | None, title: str | None) -> bool:
    """
    标题中出现明显另一家公司实体，且与种子公司核心名对不上。
    例：种子「晨光生物」 vs 标题「景顺长城基金…」。
    """
    company = (company or "").strip()
    title = (title or "").strip()
    if not company or not title:
        return False
    core = company_name_core(company)
    if not core or len(core) < 2:
        return False
    if core in title or company in title:
        return False
    # 种子核心的前 2–4 字常足以匹配简称
    short = core[:4] if len(core) >= 4 else core
    if short and short in title:
        return False
    for ent in TITLE_COMPANY_ENTITY_RE.findall(title):
        ent_core = company_name_core(ent)
        if not ent_core or len(ent_core) < 2:
            continue
        if ent_core == core or core in ent_core or ent_core in core:
            continue
        # 实体与种子无共享前缀 → 严重不符
        if ent_core[:2] != core[:2]:
            return True
    return False


def normalize_job_title(title: str | None) -> str:
    """用于去重的标题归一化。"""
    t = (title or "").strip().lower()
    t = re.sub(r"\s+", "", t)
    for ch in "【】[]（）()·・|-_/\\,，.。:：;；!！?？\"'“”‘’":
        t = t.replace(ch, "")
    t = re.sub(r"20\d{2}届?", "", t)
    t = re.sub(r"(?<!\d)\d{2}届", "", t)
    return t


def normalize_url_for_dedupe(url: str | None) -> str:
    """去 fragment、尾斜杠与常见追踪参数。"""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    u = (url or "").strip()
    if not u:
        return ""
    try:
        p = urlparse(u)
    except Exception:
        return u.rstrip("/").lower()
    drop = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "from", "spm"}
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in drop]
    path = (p.path or "").rstrip("/") or ""
    cleaned = urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", urlencode(q), ""))
    return cleaned


def normalize_job_identity_url(url: str | None) -> str:
    """Normalize a URL while retaining only query fields that identify one job."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        original = urlparse(raw)
    except Exception:
        return normalize_url_for_dedupe(raw)

    # Moka stores the stable position identity in a hash route, e.g.
    # ``#/job/<uuid>``.  ``normalize_url_for_dedupe`` intentionally drops
    # fragments, so preserve only this well-known job fragment here.
    detail_fragment = ""
    fragment_match = re.search(
        r"(?:^|/)job/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:$|[/?#])",
        original.fragment or "",
        re.I,
    )
    if fragment_match:
        detail_fragment = f"/job/{fragment_match.group(1).lower()}"

    cleaned = normalize_url_for_dedupe(raw)
    if not cleaned:
        return ""
    p = urlparse(cleaned)
    identity_keys = {
        "__job",
        "id",
        "job",
        "job_id",
        "jobadid",
        "jobcode",
        "jobid",
        "position",
        "positionid",
        "post",
        "postid",
        "recruitmentid",
    }
    query = [
        (key, value)
        for key, value in parse_qsl(p.query, keep_blank_values=True)
        if key.lower() in identity_keys
    ]
    return urlunparse(
        (p.scheme, p.netloc, p.path, "", urlencode(query), detail_fragment)
    )


def portal_entry_title(company: str | None, recruit_project: str | None = None) -> str:
    """门户无具体岗位时的唯一高质量标题。"""
    name = (company or "未知企业").strip() or "未知企业"
    project = (recruit_project or "").strip()
    if project and project not in ("校园招聘",) and not is_noise_title(project):
        return f"{name} {project}"
    return f"{name} 校园招聘入口"


# ---- 岗位判别 / 时间窗 ----

DEFAULT_LOOKBACK_DAYS = 90
DEFAULT_COLLECT_MONTHS = 3
# 校园/实习列表翻页：按卡片「更新日期」继续翻，默认近 6 个月
DEFAULT_LIST_LOOKBACK_MONTHS = 6
# 本地岗位保留期：发布/更新参照日早于该天数则软删（与列表翻页窗独立）
DEFAULT_RETENTION_DAYS = 365

JOB_ROLE_HINTS = (
    "工程师",
    "开发",
    "算法",
    "产品",
    "运营",
    "设计",
    "分析师",
    "研究员",
    "专员",
    "经理",
    "管培",
    "实习生",
    "实习岗",
    "顾问",
    "会计",
    "法务",
    "销售",
    "测试",
    "前端",
    "后端",
    "客户端",
    "数据",
    "硬件",
    "嵌入式",
    "安全",
    "运维",
    "HR",
    "财务",
    "市场",
    "增长",
    "策略",
    "咨询",
    "专家",
    "Leader",
    "Manager",
    "Specialist",
    "Engineer",
    "Intern",
)

OPEN_HIRING_SIGNALS = (
    "在招",
    "热招",
    "火热招聘",
    "正在招聘",
    "投递中",
    "开放投递",
    "立即申请",
    "立即投递",
    "欢迎投递",
    "岗位开放",
    "招聘中",
    "网申开启",
    "启动校园招聘",
    "正式启动",
)

# 实习岗视为仍在招信号：即使发布时间偏旧也不因「超3个月」进异常
INTERNSHIP_OPEN_SIGNALS = (
    "实习",
    "实习生",
    "实习岗",
    "日常实习",
    "暑假实习",
    "暑期实习",
    "寒假实习",
    "春季实习",
    "秋季实习",
    "可转正实习",
    "实习生招聘",
)

GENERIC_COMPANY_TITLE_RE = re.compile(
    r"^[\s【\[]*(?P<name>.{2,40}?)[\s】\]]*"
    r"(?:的)?"
    r"(?:20\d{2}届?)?"
    r"(?:校园招聘|校招|春招|秋招|招聘|招聘公告|招聘启事|官网招聘)"
    r"(?:入口|门户|系统|首页|公告|启事)?[\s】\]]*$",
    re.I,
)


def resolve_lookback_days(
    *,
    lookback_days: int | None = None,
    collect_months: int | None = None,
) -> int:
    """近 N 天时间窗；优先 lookback_days，否则 collect_months*30，默认 90。"""
    if lookback_days is not None and int(lookback_days) > 0:
        return int(lookback_days)
    if collect_months is not None and int(collect_months) > 0:
        return int(collect_months) * 30
    return DEFAULT_LOOKBACK_DAYS


def resolve_list_lookback_months(list_collect_months: int | None = None) -> int:
    """列表翻页时间窗（月）；默认 DEFAULT_LIST_LOOKBACK_MONTHS。"""
    if list_collect_months is not None and int(list_collect_months) > 0:
        return int(list_collect_months)
    return DEFAULT_LIST_LOOKBACK_MONTHS


def list_date_cutoff(*, months: int | None = None, today=None):
    """列表翻页截止日期（含当日：date >= cutoff 视为窗内）。"""
    from datetime import date, timedelta

    from app.timeutil import today as cn_today

    if today is None:
        today = cn_today()
    elif isinstance(today, datetime):
        today = today.date()
    m = resolve_list_lookback_months(months)
    return today - timedelta(days=max(1, int(m) * 30))


def resolve_retention_days(retention_days: int | None = None) -> int:
    """岗位保留天数；默认 DEFAULT_RETENTION_DAYS（365）。"""
    if retention_days is not None and int(retention_days) > 0:
        return int(retention_days)
    return DEFAULT_RETENTION_DAYS


def retention_cutoff(*, retention_days: int | None = None, today=None):
    """
    保留截止日：参照日 < cutoff 则超期。
    cutoff = today - retention_days（例：today=2026-08-02、365 天 → 2025-08-02）。
    """
    from datetime import timedelta

    from app.timeutil import today as cn_today

    if today is None:
        d = cn_today()
    elif isinstance(today, datetime):
        d = today.date()
    else:
        d = today
    days = resolve_retention_days(retention_days)
    return d - timedelta(days=days)


def job_retention_age_date(job: dict[str, Any] | None) -> date | None:
    """
    岗位「发布/更新」参照日：取 open_at、updated_at、created_at 中可解析的最晚日期。
    无任一可解析日期时返回 None（不因缺日期误删）。
    """
    if not job:
        return None
    dates: list[date] = []
    for key in ("open_at", "updated_at", "created_at"):
        d = parse_date_loose(job.get(key) if isinstance(job.get(key), str) else None)
        if d is None and job.get(key) is not None and not isinstance(job.get(key), str):
            d = parse_date_loose(str(job.get(key)))
        if d:
            dates.append(d)
    return max(dates) if dates else None


def is_past_retention(
    job: dict[str, Any] | None,
    *,
    retention_days: int | None = None,
    today: date | datetime | None = None,
) -> bool:
    """参照日严格早于 retention_cutoff → 超期（应软删）。"""
    age = job_retention_age_date(job)
    if age is None:
        return False
    return age < retention_cutoff(retention_days=retention_days, today=today)


def parse_date_loose(value: str | None):
    """解析常见日期字符串 → date；失败返回 None。"""
    from datetime import date, datetime

    s = (value or "").strip()
    if not s:
        return None
    s = s.replace("年", "-").replace("月", "-").replace("日", "")
    s = re.sub(r"[T\s].*$", "", s)
    s = s.strip(" ./")
    for cand in (s[:10], s[:7], s):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m", "%Y/%m"):
            try:
                return datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
    m = re.search(r"(20\d{2})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
        try:
            return date(y, mo, d)
        except ValueError:
            try:
                return date(y, mo, 1)
            except ValueError:
                return None
    return None


def has_internship_signal(*texts: str | None) -> bool:
    """标题/正文/招聘项目是否指向实习岗。"""
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return False
    return any(sig in blob for sig in INTERNSHIP_OPEN_SIGNALS)


def has_open_hiring_signal(*texts: str | None) -> bool:
    blob = "\n".join(t for t in texts if t)
    if any(sig in blob for sig in OPEN_HIRING_SIGNALS):
        return True
    # 实习岗位纳入正常在招口径
    return has_internship_signal(blob)


def is_deadline_passed(deadline: str | None, *, today=None) -> bool:
    from app.timeutil import today as cn_today

    d = parse_date_loose(deadline)
    if not d:
        return False
    if today is None:
        today = cn_today()
    elif isinstance(today, datetime):
        today = today.date()
    return d < today


def is_stale_outside_lookback(
    *,
    published_at: str | None = None,
    open_at: str | None = None,
    updated_hint: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    today=None,
) -> bool:
    """页面发布时间/开放时间早于时间窗。"""
    from datetime import timedelta

    from app.timeutil import today as cn_today

    if today is None:
        today = cn_today()
    elif isinstance(today, datetime):
        today = today.date()
    cutoff = today - timedelta(days=max(1, int(lookback_days)))
    for raw in (published_at, open_at, updated_hint):
        d = parse_date_loose(raw)
        if d and d < cutoff:
            return True
    return False


def classify_freshness(
    *,
    deadline: str | None = None,
    published_at: str | None = None,
    open_at: str | None = None,
    updated_hint: str | None = None,
    title: str | None = None,
    jd_text: str | None = None,
    recruit_project: str | None = None,
    recruit_bucket: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    today=None,
) -> tuple[str, str]:
    """
    新鲜度分类（异常口径以时间为主，不以是否列出具体岗位为准）：
    - ok：近 lookback_days 内有更新/发布，或明确仍在招 / 实习岗
    - deadline_passed：截止日已过 → 应丢弃/软删
    - stale_anomaly：最新信息发布时间早于时间窗且无在招信号 → **判为异常**（进审核，不自动当正常岗）
    """
    if is_deadline_passed(deadline, today=today):
        return "deadline_passed", "截止日期已过"
    # 实习岗：始终视为正常（除非截止日已过）
    if has_internship_signal(title, jd_text, recruit_project, recruit_bucket) or (
        (recruit_bucket or "").strip() == "日常实习"
    ):
        return "ok", "实习岗位纳入正常"
    open_sig = has_open_hiring_signal(title, jd_text, deadline, recruit_project)
    if is_stale_outside_lookback(
        published_at=published_at,
        open_at=open_at,
        updated_hint=updated_hint,
        lookback_days=lookback_days,
        today=today,
    ):
        if open_sig:
            return "ok", "超窗但仍在招"
        return (
            "stale_anomaly",
            f"最新信息发布时间超过近{lookback_days}天（约{max(1, lookback_days // 30)}个月），判为异常",
        )
    return "ok", "ok"


def keep_job_by_freshness(
    *,
    deadline: str | None = None,
    published_at: str | None = None,
    open_at: str | None = None,
    title: str | None = None,
    jd_text: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    today=None,
) -> tuple[bool, str]:
    """
    兼容旧接口：仅 ok 为 True。
    stale_anomaly / deadline_passed → False（调用方若需区分异常请用 classify_freshness）。
    """
    status, reason = classify_freshness(
        deadline=deadline,
        published_at=published_at,
        open_at=open_at,
        title=title,
        jd_text=jd_text,
        lookback_days=lookback_days,
        today=today,
    )
    return status == "ok", reason


def is_company_plus_recruit_title(company: str | None, title: str | None) -> bool:
    """
    「标题≈公司名+校园招聘」类门户噪声（非真实岗位名）。
    例：晨光生物校园招聘 / 晨光生物 校园招聘入口
    """
    company = (company or "").strip()
    title = (title or "").strip()
    if not title:
        return False
    compact_t = re.sub(r"\s+", "", title)
    # 显式入口后缀
    if re.search(r"(校园招聘|校招)(入口|门户|系统|首页)?$", compact_t):
        if not company:
            return True
        core = company_name_core(company)
        if core and (core in compact_t or company.replace(" ", "") in compact_t):
            return True
        # 无公司也可：纯「xx校园招聘」且无岗位角色词
        if not any(h in compact_t for h in JOB_ROLE_HINTS):
            m = GENERIC_COMPANY_TITLE_RE.match(title) or GENERIC_COMPANY_TITLE_RE.match(compact_t)
            if m:
                return True
    if company:
        core = company_name_core(company)
        variants = {company, core, company.replace(" ", "")}
        for v in list(variants):
            if v:
                variants.add(re.sub(r"\s+", "", v))
        for v in variants:
            if not v or len(v) < 2:
                continue
            for suffix in (
                "校园招聘",
                "校招",
                "春招",
                "秋招",
                "招聘",
                "校园招聘入口",
                "校招入口",
                "招聘公告",
            ):
                if compact_t in (f"{v}{suffix}", f"{v}的{suffix}"):
                    return True
    m = GENERIC_COMPANY_TITLE_RE.match(title) or GENERIC_COMPANY_TITLE_RE.match(compact_t)
    if m and not any(h in compact_t for h in JOB_ROLE_HINTS):
        return True
    return False


def is_real_job_title(title: str | None, company: str | None = None) -> bool:
    """是否像真实职位名（而非门户/公司名壳）。"""
    t = (title or "").strip()
    if not t or is_noise_title(t):
        return False
    if is_company_plus_recruit_title(company, t):
        return False
    if company:
        c = re.sub(r"\s+", "", company)
        tc = re.sub(r"\s+", "", t)
        if tc == c or tc == company_name_core(company):
            return False
    if any(h in t for h in JOB_ROLE_HINTS):
        return True
    if any(k in t for k in ("实习", "校招", "管培", "届", "岗")) and len(t) >= 4:
        # 含招聘语义但不是「公司名+校园招聘」
        if not is_company_plus_recruit_title(company, t):
            return True
    return False


def is_job_posting(
    *,
    title: str | None,
    source_url: str | None = None,
    jd_text: str | None = None,
    company: str | None = None,
    link_text: str | None = None,
) -> tuple[bool, float, str]:
    """
    判别是否为真实岗位条目。返回 (通过?, 置信度0~1, 原因)。
    置信度低时调用方应进异常队列，不自动发布。
    """
    t = (title or link_text or "").strip()
    url = source_url or ""
    jd = (jd_text or "").strip()

    if not t:
        return False, 0.05, "无岗位标题"
    if is_noise_title(t):
        reason = (
            "噪声标题（关停/内推壳页）"
            if is_closed_or_referral_title(t)
            else "噪声标题（导航/登录/门户壳页）"
        )
        return False, 0.05, reason
    if is_closed_or_referral_jd(jd):
        return False, 0.05, "详情表明职位已关闭或仅内部推荐"
    if is_noise_nav_url(url, t):
        return False, 0.08, "导航/登录类链接"
    if company and title_company_mismatch(company, t):
        return False, 0.12, "标题公司名与种子严重不符"
    if is_company_plus_recruit_title(company, t):
        # 门户入口：有长 JD 时略抬，但仍不算真实岗位
        conf = 0.25 if len(jd) >= 80 else 0.15
        return False, conf, "标题像公司名+校园招聘（非具体岗位）"

    score = 0.2
    reasons: list[str] = []

    if is_real_job_title(t, company):
        score += 0.35
        reasons.append("标题像职位名")
    else:
        score += 0.05

    if is_job_detail_link(url, t):
        score += 0.2
        reasons.append("URL像详情")
    elif JOB_DETAIL_URL_RE.search(url):
        score += 0.15

    if jd:
        if len(jd) >= 80:
            score += 0.2
            reasons.append("有JD正文")
        elif len(jd) >= 40:
            score += 0.1
        if any(k in jd for k in ("岗位职责", "任职要求", "职位描述", "工作内容", "岗位要求")):
            score += 0.1
            reasons.append("JD含职责/要求")
    else:
        score -= 0.15
        reasons.append("无JD")

    if any(h in t for h in JOB_ROLE_HINTS):
        score += 0.05

    score = max(0.0, min(score, 1.0))
    # 硬门槛：无真实职位名或无实质 JD → 不通过
    if not is_real_job_title(t, company):
        return False, score, "缺少真实职位名"
    if len(jd) < 40:
        return False, min(score, 0.45), "无岗位介绍/JD"
    ok = score >= 0.55
    reason = "；".join(reasons) if reasons else ("ok" if ok else "置信不足")
    if not ok:
        reason = f"岗位置信不足({score:.2f})：{reason}"
    return ok, score, reason


def company_name_key(name: str | None) -> str:
    """企业名归一化，用于判断是否同一主体（去后缀/空白）。"""
    s = (name or "").strip().lower()
    for ch in (
        " ",
        "\u3000",
        "（",
        "）",
        "(",
        ")",
        "股份有限公司",
        "有限责任公司",
        "有限公司",
        "集团股份",
        "集团",
    ):
        s = s.replace(ch.lower() if ch.isascii() else ch, "")
    return s


def companies_same(a: str | None, b: str | None) -> bool:
    ka, kb = company_name_key(a), company_name_key(b)
    if not ka or not kb:
        return False
    return ka == kb or ka in kb or kb in ka


def format_group_label(seed_company_name: str | None) -> str:
    """集团展示名：去掉尾部「有限公司」等，保留「…集团」。"""
    s = (seed_company_name or "").strip()
    if not s:
        return ""
    for suf in ("有限责任公司", "股份有限公司", "有限公司"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
            break
    return s or (seed_company_name or "").strip()


def resolve_group_and_company(
    *,
    seed_company_name: str | None,
    page_company: str | None,
) -> tuple[str, str]:
    """
    集团 / 公司判定：
    - 在种子公司（集团门户）招聘站上识别到**其他**招聘单位 → 种子为集团，页面单位为子公司；
    - 页面未写单位或与种子为同一主体 → 集团为空，公司用页面名或种子名。
    返回 (group_name, company)。
    """
    seed = strip_recruit_channel_company_suffix(seed_company_name)
    page = strip_recruit_channel_company_suffix(page_company)
    if not page:
        return "", seed or "未知企业"
    if not seed:
        return "", page
    if companies_same(seed, page):
        return "", page
    return format_group_label(seed), page
