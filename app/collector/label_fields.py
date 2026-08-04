"""详情页「标签：值」结构化抽取（主路径，不依赖 OCR）。"""

from __future__ import annotations

import re
from typing import Any

from app.collector.adapters.base import ParseResult
from app.collector.extract import classify_from_text, soup_from_html
from app.collector.filters import (
    is_noise_location,
    map_recruit_bucket,
    recover_title_from_jd,
    sanitize_job_title,
    strip_career_nav_boilerplate,
)

# 标签 → 内部字段（按长度降序匹配，避免短标签抢先）
_LABEL_TO_FIELD: list[tuple[str, str]] = [
    ("招聘类型", "recruit_type"),
    ("招聘类别", "recruit_type"),
    ("招聘项目", "recruit_type"),
    ("工作性质", "work_type"),
    ("工作类型", "work_type"),
    ("职位类型", "work_type"),
    ("岗位类型", "work_type"),
    ("岗位类别", "raw_category"),
    ("职能类别", "raw_category"),
    ("岗位发布时间", "open_at"),
    ("发布时间", "open_at"),
    ("发布日期", "open_at"),
    ("开放时间", "open_at"),
    ("更新日期", "open_at"),
    ("更新时间", "open_at"),
    ("截止时间", "deadline"),
    ("截止日期", "deadline"),
    ("投递截止", "deadline"),
    ("报名截止", "deadline"),
    ("工作地点", "work_location"),
    ("工作地", "work_location"),
    ("工作城市", "work_location"),
    ("办公地点", "work_location"),
    ("办公地址", "work_location"),
    ("所在城市", "work_location"),
    ("上班地点", "work_location"),
    ("岗位地址", "work_location"),
    ("工作地址", "work_location"),
    ("base地", "work_location"),
    ("职位名称", "title"),
    ("岗位名称", "title"),
    ("公司名称", "company"),
    ("招聘单位", "company"),
    ("用人单位", "company"),
    ("所属公司", "company"),
    ("学历要求", "education"),
    ("学历层次", "education"),
    ("薪资范围", "salary_range"),
    ("薪酬范围", "salary_range"),
    ("薪资待遇", "salary_range"),
    ("薪酬待遇", "salary_range"),
    ("薪资", "salary_range"),
    ("月薪", "salary_range"),
    ("年薪", "salary_range"),
    ("薪酬", "salary_range"),
    ("待遇", "salary_range"),
    ("招聘人数", "headcount"),
    ("需求人数", "headcount"),
    ("用人名额", "headcount"),
    ("招人人数", "headcount"),
    ("招聘名额", "headcount"),
    ("招若干人", "headcount"),
    ("专业要求", "jd_major"),
    ("专业需求", "jd_major"),
    ("工作职责", "jd_duties"),
    ("岗位职责", "jd_duties"),
    ("职位职责", "jd_duties"),
    ("主要职责", "jd_duties"),
    ("核心职责", "jd_duties"),
    ("工作任务", "jd_duties"),
    ("岗位使命", "jd_duties"),
    ("你将负责", "jd_duties"),
    ("你需要做", "jd_duties"),
    ("您将负责", "jd_duties"),
    ("您需要做", "jd_duties"),
    ("职位概述", "jd_duties"),
    ("职位描述", "jd_duties"),
    ("岗位描述", "jd_duties"),
    ("工作描述", "jd_duties"),
    ("岗位介绍", "jd_duties"),
    ("工作内容", "jd_duties"),
    ("职责描述", "jd_duties"),
    ("我们需要你", "jd_duties"),
    ("任职要求", "jd_requirements"),
    ("任职职责", "jd_requirements"),
    ("任职资格", "jd_requirements"),
    ("岗位要求", "jd_requirements"),
    ("能力要求", "jd_requirements"),
    ("资格要求", "jd_requirements"),
    ("岗位资格", "jd_requirements"),
    ("任职标准", "jd_requirements"),
    ("基本条件", "jd_requirements"),
    ("胜任条件", "jd_requirements"),
    ("申请资格", "jd_requirements"),
    ("职位要求", "jd_requirements"),
    ("你需要具备", "jd_requirements"),
    ("您需要具备", "jd_requirements"),
    ("我们希望你", "jd_requirements"),
    ("我们期望你", "jd_requirements"),
    ("任职条件", "jd_requirements"),
    # English / mixed CN-EN JD section headings (hotjob / Decathlon-style)
    ("Job Description", "jd_duties"),
    ("Job Responsibilities", "jd_duties"),
    ("Key Responsibilities", "jd_duties"),
    ("Role Responsibilities", "jd_duties"),
    ("Your Responsibilities", "jd_duties"),
    ("Responsibilities", "jd_duties"),
    ("Job Duties", "jd_duties"),
    ("Duties", "jd_duties"),
    ("What you'll do", "jd_duties"),
    ("What you’ll do", "jd_duties"),  # curly apostrophe
    ("What you will do", "jd_duties"),
    ("What you do", "jd_duties"),
    ("Job Requirements", "jd_requirements"),
    ("Essential Requirements", "jd_requirements"),
    ("Preferred Qualifications", "jd_requirements"),
    ("Requirements", "jd_requirements"),
    ("Qualifications", "jd_requirements"),
    ("Skills", "jd_requirements"),
    ("Who you are", "jd_requirements"),
    ("Who We're Looking For", "jd_requirements"),
    ("Who We’re Looking For", "jd_requirements"),
    ("What we're looking for", "jd_requirements"),
    ("What we’re looking for", "jd_requirements"),
    ("What we are looking for", "jd_requirements"),
    ("About you", "jd_requirements"),
    ("Must Have", "jd_requirements"),
    ("Nice to Have", "jd_requirements"),
    ("类型", "recruit_type"),  # 过宽；取值时再校验
]

_LABEL_ALT = "|".join(re.escape(k) for k, _ in sorted(_LABEL_TO_FIELD, key=lambda x: -len(x[0])))
_SECTION_NUMBER_PREFIX = (
    r"(?:(?:[一二三四五六七八九十]+|\d+)\s*[、.．)]\s*|"
    r"[（(](?:[一二三四五六七八九十]+|\d+)[）)]\s*)?"
)
_LABEL_RE = re.compile(
    rf"(?:^|\n|(?<=[。；;]))\s*{_SECTION_NUMBER_PREFIX}({_LABEL_ALT})"
    rf"(?:\s*[:：]\s*|\s*(?=\n|$))",
    re.MULTILINE | re.IGNORECASE,
)

_RECRUIT_TYPE_VALUES = re.compile(
    r"^(校园招聘|社会招聘|实习生招聘|校招|社招|应届生实习|日常实习|暑期实习|暑假实习|寒假实习|秋招|春招|全职|实习)"
)

_FIELD_BY_LABEL = {k.lower(): v for k, v in _LABEL_TO_FIELD}

_PORTAL_CHROME_RE = re.compile(
    r"(招聘系统|校园招聘|社会招聘|实习生招聘|欢迎登录|个人中心)",
)


def _is_noise_jd_section(value: str) -> bool:
    """过滤被空段落误吞的门户顶栏/壳标题。"""
    v = (value or "").strip()
    if not v:
        return True
    hits = len(_PORTAL_CHROME_RE.findall(v))
    if hits >= 2:
        return True
    if v.startswith("招聘系统") and len(v) < 80:
        return True
    return False


def _append_jd_section(existing: str, value: str) -> str:
    """合并同类型分段（如 Skills + 任职要求），避免重复粘贴。"""
    value = (value or "").strip()
    if not value:
        return existing
    if not existing:
        return value
    if value in existing or existing in value:
        return existing if len(existing) >= len(value) else value
    return f"{existing}\n{value}"


def html_to_visible_text(html: str | None) -> str:
    """HTML → 可见纯文本（供标签解析）。"""
    if not html:
        return ""
    soup = soup_from_html(html)
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_labeled_fields(text: str | None) -> dict[str, str]:
    """
    从纯文本抽取「标签：值」字段。
    多行段落（职责/要求）取至下一标签；单行字段取首行。
    标签存在但值为空时不写入（调用方视为缺省/空）。
    """
    raw = (text or "").strip()
    if not raw:
        return {}

    matches = list(_LABEL_RE.finditer(raw))
    if not matches:
        return {}

    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        label = m.group(1).strip()
        field = _FIELD_BY_LABEL.get(label.lower())
        if not field:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        value = raw[start:end].strip()
        # 缺内容 → 跳过，保持空（不写入占位）
        if not value:
            continue

        if field in ("jd_duties", "jd_requirements", "jd_major"):
            # 保留多行，去掉尾部空白行
            value = value.strip()
            if _is_noise_jd_section(value):
                continue
        else:
            # 单行字段：取第一行，去掉多余分隔
            value = re.split(r"[\n\r]", value, maxsplit=1)[0].strip()
            value = re.split(r"[|｜]", value, maxsplit=1)[0].strip()
            value = value.strip(" ，,;；")

        if not value:
            continue

        if field == "recruit_type" and label in ("类型", "Type", "type"):
            if not _RECRUIT_TYPE_VALUES.search(value):
                continue

        if field in ("work_location", "title", "company", "raw_category") and len(value) > 80:
            value = value[:80].strip()
        if field == "open_at":
            # 「2026-07-31 最新」/「2026年7月31日最新发布」→ 规范化日期
            value = re.sub(r"\s*最新(?:发布)?\s*$", "", value).strip()
            from app.collector.filters import parse_date_loose

            d = parse_date_loose(value)
            if d:
                value = d.isoformat()
            elif len(value) > 40:
                value = value[:40].strip()
        if field == "deadline" and len(value) > 40:
            value = value[:40].strip()
        if field == "education":
            value = normalize_education_requirement(value) or value
            if len(value) > 40:
                value = value[:40].strip()
        if field == "salary_range":
            value = normalize_salary_range(value) or ""
            if not value:
                continue
        if field == "headcount":
            value = normalize_headcount(value) or ""
            if not value:
                continue

        # 先出现的优先；职责/要求/专业段可追加多个英文小标题（Skills + Requirements）
        if field in ("jd_duties", "jd_requirements", "jd_major"):
            out[field] = _append_jd_section(out.get(field) or "", value)
        elif field not in out or not out[field]:
            out[field] = value

    return out


_BODY_SECTION_START_RE = re.compile(
    r"^(专业要求|专业需求|工作职责|岗位职责|职位职责|主要职责|核心职责|工作任务|岗位使命|"
    r"你将负责|你需要做|您将负责|您需要做|职位概述|岗位要求|任职要求|任职资格|任职职责|任职条件|"
    r"能力要求|资格要求|岗位资格|任职标准|基本条件|胜任条件|申请资格|职位要求|"
    r"岗位介绍|职位描述|岗位描述|工作描述|工作内容|职责描述|"
    r"我们需要你|我们希望你|我们期望你|你需要具备|您需要具备|"
    r"Job Description|Job Responsibilities|Key Responsibilities|Role Responsibilities|Your Responsibilities|"
    r"Responsibilities|Job Duties|Duties|"
    r"What you['’]?ll do|What you will do|What you do|"
    r"Job Requirements|Essential Requirements|Preferred Qualifications|"
    r"Requirements|Qualifications|Skills|Who you are|"
    r"Who We['’]?re Looking For|What we['’]?re looking for|What we are looking for|"
    r"About you|Must Have|Nice to Have)\s*[:：]?",
    re.I,
)

# 审核表「岗位要求 / 任职要求」列拆分：岗位要求列优先职责段（含中文「岗位要求」）
_JD_DUTY_SECTION_LABELS = frozenset(
    {
        "工作职责",
        "岗位职责",
        "职位职责",
        "主要职责",
        "核心职责",
        "工作任务",
        "岗位使命",
        "你将负责",
        "你需要做",
        "您将负责",
        "您需要做",
        "职位概述",
        "岗位要求",
        "职位描述",
        "岗位描述",
        "工作描述",
        "岗位介绍",
        "工作内容",
        "职责描述",
        "我们需要你",
        "job description",
        "responsibilities",
        "job responsibilities",
        "key responsibilities",
        "role responsibilities",
        "your responsibilities",
        "duties",
        "job duties",
        "what you'll do",
        "what you’ll do",
        "what you will do",
        "what you do",
    }
)
_JD_REQ_SECTION_LABELS = frozenset(
    {
        "任职要求",
        "任职资格",
        "任职职责",
        "任职条件",
        "能力要求",
        "资格要求",
        "岗位资格",
        "任职标准",
        "基本条件",
        "胜任条件",
        "申请资格",
        "职位要求",
        "你需要具备",
        "您需要具备",
        "我们希望你",
        "我们期望你",
        "requirements",
        "job requirements",
        "essential requirements",
        "qualifications",
        "preferred qualifications",
        "skills",
        "who you are",
        "who we're looking for",
        "who we’re looking for",
        "what we're looking for",
        "what we’re looking for",
        "what we are looking for",
        "about you",
        "must have",
        "nice to have",
    }
)
_JD_STOP_SECTION_LABELS = frozenset(
    {
        "福利待遇",
        "薪酬福利",
        "员工福利",
        "职位亮点",
        "公司介绍",
        "企业介绍",
        "关于我们",
        "申请方式",
        "投递方式",
        "联系方式",
        "工作地点",
        "办公地点",
        "薪资待遇",
        "其他信息",
    }
)
_JD_SECTION_LABEL_ALT = "|".join(
    re.escape(k)
    for k in sorted(
        _JD_DUTY_SECTION_LABELS | _JD_REQ_SECTION_LABELS | _JD_STOP_SECTION_LABELS,
        key=len,
        reverse=True,
    )
)
_JD_SECTION_SPLIT_RE = re.compile(
    rf"(?:^|\n|(?<=[。；;]))\s*{_SECTION_NUMBER_PREFIX}({_JD_SECTION_LABEL_ALT})"
    rf"(?:\s*[:：]\s*|\s*(?=\n|$))",
    re.MULTILINE | re.IGNORECASE,
)


def split_jd_sections(jd_text: str | None) -> tuple[str, str]:
    """
    从 JD 文本拆出（职责, 任职要求），支持中英/混排分段标题。

    职责：工作职责 / 岗位职责 / 职位描述 / Responsibilities / Duties / What you'll do …
    要求：任职要求 / 任职资格 / 我们期望你 / Requirements / Qualifications / Skills …
    显示侧将中文「岗位要求」归入职责列（与审核表一致）；标签抽取仍映射 jd_requirements。
    仅有一段描述正文时归入职责列，避免两列皆空。
    """
    raw = (jd_text or "").strip()
    if not raw:
        return "", ""

    duties = ""
    reqs = ""
    matches = list(_JD_SECTION_SPLIT_RE.finditer(raw))
    for i, m in enumerate(matches):
        label = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        value = raw[start:end].strip()
        if not value or _is_noise_jd_section(value):
            continue
        if label in _JD_DUTY_SECTION_LABELS:
            duties = _append_jd_section(duties, value)
        elif label in _JD_REQ_SECTION_LABELS:
            reqs = _append_jd_section(reqs, value)

    if not duties or not reqs:
        labels = extract_labeled_fields(raw)
        duties = duties or (labels.get("jd_duties") or "")
        reqs = reqs or (labels.get("jd_requirements") or "")

    # 无经典分段标题：整段实质 JD 落入职责列（职位描述 / 远景「我们需要你」等）
    if not duties and not reqs:
        fallback = _unlabeled_jd_body(raw)
        if fallback:
            duties = fallback
    elif not duties and reqs and len(reqs) < 40:
        # 仅抽到极短「要求」时仍尝试把正文主体作职责
        fallback = _unlabeled_jd_body(raw)
        if fallback and fallback != reqs:
            duties = fallback
    return duties, reqs


def _unlabeled_jd_body(raw: str) -> str:
    """去掉元信息行后，若仍有实质正文则作为单一描述块。"""
    lines: list[str] = []
    for ln in (raw or "").splitlines():
        s = ln.strip()
        if not s:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if _META_HEADER_LABEL_RE.match(s):
            continue
        if _BARE_META_VALUES and s in _BARE_META_VALUES:
            continue
        if _is_noise_jd_section(s):
            continue
        lines.append(s)
    body = "\n".join(lines).strip()
    if len(body) < 40:
        return ""
    if _is_noise_jd_section(body):
        return ""
    return body


_META_HEADER_LABEL_RE = re.compile(
    r"^(岗位名称|职位名称|招聘类别|招聘类型|招聘项目|工作性质|工作类型|职位类型|"
    r"岗位类型|岗位类别|职能类别|工作地点|工作城市|上班地点|岗位地址|工作地址|"
    r"base地|公司名称|招聘单位|用人单位|所属公司|学历要求|学历层次|"
    r"薪资范围|薪酬范围|薪资待遇|薪酬待遇|薪资|月薪|年薪|薪酬|待遇|"
    r"招聘人数|需求人数|用人名额|招人人数|招聘名额|招若干人|"
    r"截止时间|截止日期|投递截止|报名截止|岗位发布时间|发布时间|发布日期|开放时间|"
    r"更新日期|更新时间|类型)\s*[:：]?\s*(.*)$",
    re.I,
)

# 与结构化字段重复、常单独占一行的招聘元信息
_BARE_META_VALUES = frozenset(
    {
        "校园招聘",
        "社会招聘",
        "实习生招聘",
        "校招",
        "社招",
        "秋招",
        "春招",
        "全职",
        "实习",
        "日常实习",
        "暑期实习",
        "暑假实习",
        "寒假实习",
        "兼职",
    }
)

_DEGREE_ORDER = ("博士", "硕士", "本科", "大专")

# 中化等卡片标题副信号：研发工程师（硕士） / 管培生(博士)
_TITLE_DEGREE_RE = re.compile(
    r"[（(]\s*(博士研究生|硕士研究生|博士|硕士|本科|大专|专科)"
    r"(?:及以上|以上)?\s*[）)]"
)


def _norm_meta_token(value: str | None) -> str:
    return re.sub(r"\s+", "", (value or "").strip()).lower()


def normalize_education_requirement(value: str | None) -> str | None:
    """归一学历文案：优先 博士/硕士/本科/大专（可带「及以上」）。"""
    v = (value or "").strip()
    if not v or v in {"-", "—", "无", "不限"}:
        return None
    v = re.split(r"[;；|/｜]", v, maxsplit=1)[0].strip()
    has_plus = bool(re.search(r"及以上|以上|及更[高低]", v))
    for deg in _DEGREE_ORDER:
        if deg in v:
            return f"{deg}及以上" if has_plus else deg
    if "研究生" in v:
        return "硕士及以上" if has_plus else "硕士"
    if "专科" in v:
        return "大专及以上" if has_plus else "大专"
    if "学士" in v:
        return "本科及以上" if has_plus else "本科"
    return v[:40] if len(v) <= 40 else v[:40].strip()


_SALARY_NOISE = frozenset(
    {
        "工作职责",
        "岗位职责",
        "任职要求",
        "任职资格",
        "岗位要求",
        "专业要求",
        "招聘人数",
        "学历要求",
        "responsibilities",
        "requirements",
        "qualifications",
        "skills",
        "duties",
    }
)
_HEADCOUNT_NOISE = frozenset(
    {
        "工作职责",
        "岗位职责",
        "任职要求",
        "任职资格",
        "岗位要求",
        "专业要求",
        "薪资范围",
        "学历要求",
        "薪资",
        "responsibilities",
        "requirements",
        "qualifications",
        "skills",
        "duties",
    }
)


def normalize_salary_range(value: str | None) -> str | None:
    """薪资范围短文案；缺省/噪声返回 None（UI 显示「-」）。"""
    v = (value or "").strip()
    if not v or v in {"-", "—", "无"}:
        return None
    v = re.split(r"[\n\r]", v, maxsplit=1)[0].strip()
    v = re.split(r"[|｜]", v, maxsplit=1)[0].strip(" ，,;；")
    if not v or v in _SALARY_NOISE or len(v) > 40:
        return None
    # 勿把职责/日期整段误当薪资
    if _BODY_SECTION_START_RE.match(v):
        return None
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", v):
        return None
    return v


def normalize_headcount(value: str | None) -> str | None:
    """招聘人数短文案（若干/数字/N人）；缺省/噪声返回 None。"""
    v = (value or "").strip()
    if not v or v in {"-", "—", "无"}:
        return None
    v = re.split(r"[\n\r]", v, maxsplit=1)[0].strip()
    v = re.split(r"[|｜]", v, maxsplit=1)[0].strip(" ，,;；")
    if not v or v in _HEADCOUNT_NOISE or len(v) > 40:
        return None
    if _BODY_SECTION_START_RE.match(v):
        return None
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", v):
        return None
    # 「招若干人」整段 → 若干
    m = re.fullmatch(r"招?\s*(若干|若干人|[0-9]+(?:\s*[-~～到至]\s*[0-9]+)?\s*人?)", v)
    if m:
        got = m.group(1).strip()
        if got in ("若干", "若干人"):
            return "若干"
        return got
    return v


def extract_education_from_title(title: str | None) -> str | None:
    """从岗位名括号学历副信号抽取，如「研发工程师（硕士）」。"""
    m = _TITLE_DEGREE_RE.search(title or "")
    if not m:
        return None
    # 用整段括号内容归一，保留「及以上」
    return normalize_education_requirement(m.group(0).strip("（）()"))


def extract_education_requirement(
    text: str | None,
    *,
    labels: dict[str, str] | None = None,
    title: str | None = None,
) -> str | None:
    """
    从标签学历要求或任职要求/资格正文抽取学历；标题括号为次级信号。
    缺省返回 None（UI 可显示为「-」）。
    """
    lab = labels or {}
    if lab.get("education"):
        return normalize_education_requirement(lab["education"])

    raw = (text or "").strip()
    if not raw and not lab and not (title or "").strip():
        return None

    m = re.search(r"学历要求\s*[:：]\s*([^\n\r]+)", raw)
    if m:
        got = normalize_education_requirement(m.group(1))
        if got:
            return got

    req = (lab.get("jd_requirements") or "").strip()
    if not req:
        # 从全文截任职段
        sec = re.search(
            r"(?:任职要求|任职资格|任职职责)\s*[:：]?\s*(.+?)(?=\n\s*(?:工作职责|岗位职责|专业要求|福利|$))",
            raw,
            re.S | re.I,
        )
        req = (sec.group(1) if sec else raw).strip()

    for blob in (req, raw):
        if not blob:
            continue
        # 优先含「学历」的句子/行
        for line in re.split(r"[\n\r]+", blob):
            if "学历" in line or "学位" in line:
                got = normalize_education_requirement(line)
                if got:
                    return got
        got = normalize_education_requirement(blob)
        if got and any(d in blob for d in (*_DEGREE_ORDER, "研究生", "专科", "学士")):
            # 避免把整段任职要求误当学历：仅当出现学历词时
            if re.search(r"(博士|硕士|研究生|本科|大专|专科|学士)", blob):
                # 取首次命中的短形式
                for deg in _DEGREE_ORDER:
                    if deg in blob:
                        has_plus = bool(re.search(rf"{deg}.{{0,6}}(?:及以上|以上)", blob))
                        return f"{deg}及以上" if has_plus else deg
                if "研究生" in blob:
                    return "硕士"
    # 次级：标题（硕士）/（博士）
    return extract_education_from_title(title)


def summarize_jd_dedupe(old_jd: str | None, new_jd: str | None) -> str:
    """中文摘要：JD 去重剥掉了哪些页眉行。"""
    old = (old_jd or "").strip()
    new = (new_jd or "").strip()
    if old == new:
        return "无重复页眉可清理"
    old_lines = [ln.strip() for ln in old.splitlines() if ln.strip()]
    new_set = {ln.strip() for ln in new.splitlines() if ln.strip()}
    removed = [ln for ln in old_lines if ln not in new_set]
    if not removed:
        return f"已压缩空白（原文 {len(old)} → {len(new)} 字）"
    preview = "、".join(removed[:4])
    if len(removed) > 4:
        preview += f" 等共 {len(removed)} 行"
    else:
        preview = f"{preview}（{len(removed)} 行）"
    return f"已去掉重复页眉：{preview}"


def strip_redundant_jd_meta(
    jd_text: str | None,
    *,
    title: str | None = None,
    company: str | None = None,
    recruit_project: str | None = None,
    recruit_bucket: str | None = None,
    work_location: str | None = None,
    raw_category: str | None = None,
    education: str | None = None,
    salary_range: str | None = None,
    headcount: str | None = None,
    extra_values: list[str] | None = None,
) -> str | None:
    """
    去掉 JD 开头已结构化字段重复的页眉/元信息行，保留工作职责/任职要求正文。

    典型：岗位名、校园招聘、全职、工作地点：深圳、薪资范围、招聘人数 等与侧栏字段重复的行。
    """
    text = (jd_text or "").strip()
    if not text:
        return None

    dup_norms: set[str] = set()
    for raw in (
        title,
        company,
        recruit_project,
        recruit_bucket,
        work_location,
        raw_category,
        education,
        salary_range,
        headcount,
        *(extra_values or ()),
    ):
        n = _norm_meta_token(raw)
        if n and len(n) >= 2:
            dup_norms.add(n)
        # 「若干人」与字段值「若干」互通去重
        if raw and "若干" in str(raw):
            dup_norms.add(_norm_meta_token("若干"))
            dup_norms.add(_norm_meta_token("若干人"))
            dup_norms.add(_norm_meta_token("招若干人"))
    for bare in _BARE_META_VALUES:
        dup_norms.add(_norm_meta_token(bare))

    lines = text.splitlines()
    body_at: int | None = None
    for i, ln in enumerate(lines):
        if _BODY_SECTION_START_RE.match(ln.strip()):
            body_at = i
            break

    header = lines if body_at is None else lines[:body_at]
    body = [] if body_at is None else lines[body_at:]

    kept_header: list[str] = []
    for raw in header:
        line = raw.strip()
        if not line:
            continue
        m = _META_HEADER_LABEL_RE.match(line)
        if m:
            # 标签元信息行：整行丢弃（值已进结构化字段或可忽略）
            continue
        if _norm_meta_token(line) in dup_norms:
            continue
        # 纯地点/类型短行（无标签）也去重
        if line in _BARE_META_VALUES:
            continue
        kept_header.append(raw.rstrip())

    # 正文前若仍以岗位名独占一行，再剥一次
    while kept_header and _norm_meta_token(kept_header[0]) in dup_norms:
        kept_header.pop(0)

    merged = [*kept_header, *body]
    # 压缩多余空行
    out: list[str] = []
    for raw in merged:
        if not raw.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(raw.rstrip())
    cleaned = "\n".join(out).strip()
    return cleaned or None


def map_labeled_recruit(
    recruit_type: str | None,
    work_type: str | None = None,
    *,
    title: str | None = None,
) -> tuple[str | None, str | None]:
    """
    招聘类型 / 工作类型 → (recruit_project, recruit_bucket)。

    明确「招聘类别/类型」字段优先于标题笼统词：
    - 社会招聘/社招 → project=社会招聘，bucket 不得为「校招」
    - 校园招聘 → 校招
    """
    rt = (recruit_type or "").strip()
    wt = (work_type or "").strip()
    blob = f"{rt} {wt} {title or ''}"

    project: str | None = None
    bucket: str | None = None

    # 实习优先（工作类型或招聘类型）；社招类别下的「实习」仍走实习
    if any(k in blob for k in ("应届生实习", "日常实习", "暑期实习", "暑假实习", "寒假实习", "实习生", "实习岗")) or (
        "实习" in wt or (rt == "实习")
    ):
        # 招聘类别明确社招且工作类型不是实习 → 不走实习短路
        if not (
            any(k in rt for k in ("社会招聘", "社招"))
            and "实习" not in wt
            and rt not in ("实习",)
        ):
            project = next(
                (k for k in ("应届生实习", "日常实习", "暑期实习", "暑假实习", "寒假实习") if k in blob),
                None,
            ) or (rt if "实习" in rt else "日常实习")
            bucket = map_recruit_bucket(project, title) or "日常实习"
            return project, bucket

    # 招聘类别字段明确社招：优先于标题里的校招/应届等词
    if any(k in rt for k in ("社会招聘", "社招")):
        return "社会招聘", None

    if any(k in rt for k in ("实习生招聘", "应届生实习", "日常实习", "暑期实习", "暑假实习", "寒假实习")) or rt in (
        "实习",
        "实习生",
    ):
        project = rt if "实习" in rt else "日常实习"
        return project, map_recruit_bucket(project, title) or "日常实习"

    if any(k in rt for k in ("校园招聘", "校招", "秋招", "春招")):
        project = rt if rt else "校园招聘"
        if "秋招" in rt:
            project = "秋招"
        elif "春招" in rt:
            project = "春招"
        elif "校园" in rt or rt == "校招":
            project = "校园招聘"
        bucket = "校招"
        return project, bucket

    # 无明确招聘类别时，正文/标题出现社会招聘（且招聘类型字段未写校招）
    if "社会招聘" in blob or re.search(r"(?<![校园])社招", blob):
        if not any(k in rt for k in ("校园招聘", "校招", "秋招", "春招")):
            return "社会招聘", None

    if "全职" in wt or "全职" in rt:
        project = "校园招聘"
        bucket = "校招"
    elif rt:
        project, bucket, _ = classify_from_text(rt, title)
        project = project or rt
        # classify 可能把社招映射出校招 bucket，纠正
        if project and any(k in project for k in ("社会招聘", "社招")):
            return "社会招聘", None

    if not bucket and project and not any(k in (project or "") for k in ("社会招聘", "社招")):
        bucket = map_recruit_bucket(project, title) or map_recruit_bucket(wt, title)
    return project, bucket


def merge_jd_sections(
    existing: str | None,
    *,
    duties: str | None = None,
    requirements: str | None = None,
    major: str | None = None,
) -> str | None:
    """将专业要求 / 工作职责 / 任职要求并入 jd_text（分段标题）。"""
    major = (major or "").strip() or None
    duties = (duties or "").strip() or None
    requirements = (requirements or "").strip() or None
    existing = (existing or "").strip() or None

    parts: list[str] = []
    if major:
        block = major if major.startswith("专业要求") or major.startswith("专业需求") else f"专业要求：\n{major}"
        if not existing or major[:40] not in existing:
            parts.append(block)
    if duties:
        block = (
            duties
            if _jd_block_has_section_header(duties, kind="duties")
            else f"工作职责：\n{duties}"
        )
        if not existing or duties[:40] not in existing:
            parts.append(block)
    if requirements:
        block = (
            requirements
            if _jd_block_has_section_header(requirements, kind="requirements")
            else f"任职要求：\n{requirements}"
        )
        if not existing or requirements[:40] not in existing:
            parts.append(block)

    if not parts:
        return existing
    if not existing or len(existing) < 60:
        return "\n\n".join(parts)
    return existing.rstrip() + "\n\n" + "\n\n".join(parts)


def apply_labeled_fields(result: ParseResult, labels: dict[str, str]) -> ParseResult:
    """将标签抽取结果写入 ParseResult（仅填补空缺，不覆盖已有强字段）。"""
    if not labels:
        return result

    if labels.get("title"):
        labeled_title = sanitize_job_title(labels["title"])
        if labeled_title and (not result.title or is_noise_or_shell_title(result.title)):
            result.title = labeled_title

    if not result.work_location and labels.get("work_location"):
        loc = labels["work_location"]
        if not is_noise_location(loc):
            result.work_location = loc
    if is_noise_location(result.work_location):
        result.work_location = None

    if not result.deadline and labels.get("deadline"):
        result.deadline = labels["deadline"]

    if labels.get("raw_category") and not result.raw_category:
        result.raw_category = labels["raw_category"]

    open_at = labels.get("open_at")
    if open_at:
        extras = result.extras if isinstance(result.extras, dict) else {}
        if not extras.get("published_at"):
            extras["published_at"] = open_at
            result.extras = extras

    if labels.get("company"):
        extras = result.extras if isinstance(result.extras, dict) else {}
        if not extras.get("company"):
            extras["company"] = labels["company"]
            result.extras = extras

    project, bucket = map_labeled_recruit(
        labels.get("recruit_type"),
        labels.get("work_type"),
        title=result.title,
    )
    rt = (labels.get("recruit_type") or "").strip()
    # 招聘类别/类型标签为强信号：可覆盖适配器/标题误判的校园招聘
    explicit_label = bool(
        rt
        and any(
            k in rt
            for k in ("社会招聘", "社招", "校园招聘", "校招", "秋招", "春招", "实习")
        )
    )
    if project and (explicit_label or not result.recruit_project):
        result.recruit_project = project
    if explicit_label and project and any(k in project for k in ("社会招聘", "社招")):
        result.recruit_project = "社会招聘"
        # 不得保留误标的「校招」bucket
        result.recruit_bucket = None
    elif bucket and (explicit_label or not result.recruit_bucket):
        result.recruit_bucket = bucket
    # 实习工作类型可纠正 bucket
    if bucket in ("应届生实习", "日常实习"):
        result.recruit_bucket = bucket
        if not result.recruit_project or result.recruit_project in (
            "校园招聘",
            "全职",
            "社会招聘",
            "应届生实习",
            "日常实习",
        ):
            result.recruit_project = project or "实习生招聘"

    if labels.get("work_type") and not result.raw_category:
        result.raw_category = labels["work_type"]

    if labels.get("education") and not result.education:
        result.education = normalize_education_requirement(labels["education"])
    if labels.get("salary_range") and not result.salary_range:
        result.salary_range = normalize_salary_range(labels["salary_range"])
    if labels.get("headcount") and not result.headcount:
        result.headcount = normalize_headcount(labels["headcount"])

    result.jd_text = merge_jd_sections(
        result.jd_text,
        major=labels.get("jd_major"),
        duties=labels.get("jd_duties"),
        requirements=labels.get("jd_requirements"),
    )
    # 未抽到分段正文时，去掉通用解析残留的空「专业要求/工作职责/任职要求」行
    if not any(labels.get(k) for k in ("jd_major", "jd_duties", "jd_requirements")):
        result.jd_text = _strip_bare_section_headers(result.jd_text)

    extras = result.extras if isinstance(result.extras, dict) else {}
    extras["labeled_fields"] = {k: v for k, v in labels.items() if v}
    result.extras = extras
    return result


_BARE_SECTION_HEADER_RE = re.compile(
    r"^(专业要求|专业需求|工作职责|岗位职责|职位职责|主要职责|核心职责|工作任务|岗位使命|"
    r"你将负责|你需要做|您将负责|您需要做|职位概述|职位描述|岗位描述|工作描述|岗位介绍|"
    r"任职要求|任职资格|任职条件|岗位要求|能力要求|资格要求|岗位资格|任职标准|基本条件|"
    r"胜任条件|申请资格|职位要求|你需要具备|您需要具备|我们需要你|我们希望你|我们期望你|"
    r"Job Description|Job Responsibilities|Key Responsibilities|Responsibilities|Job Duties|Duties|"
    r"What you['’]?ll do|What you will do|"
    r"Job Requirements|Requirements|Qualifications|Skills|Who you are|"
    r"What we['’]?re looking for|About you|Must Have|Nice to Have)\s*[:：]?\s*$",
    re.I,
)


def _jd_block_has_section_header(block: str, *, kind: str) -> bool:
    """已带分段标题的块不再套中文默认标题。"""
    head = (block or "").lstrip()[:80]
    if kind == "duties":
        return bool(
            re.match(
                r"^(工作职责|岗位职责|职位职责|主要职责|核心职责|工作任务|岗位使命|"
                r"你将负责|你需要做|您将负责|您需要做|职位概述|职位描述|岗位描述|工作描述|岗位介绍|工作内容|"
                r"我们需要你|Job Description|Responsibilities|Job Responsibilities|"
                r"Key Responsibilities|Duties|Job Duties|What you['’]?ll do|What you will do)\s*[:：]?",
                head,
                re.I,
            )
        )
    return bool(
        re.match(
            r"^(任职要求|任职资格|任职职责|任职条件|岗位要求|能力要求|资格要求|岗位资格|任职标准|"
            r"基本条件|胜任条件|申请资格|职位要求|你需要具备|您需要具备|我们希望你|我们期望你|Requirements|Job Requirements|"
            r"Qualifications|Skills|Who you are|What we['’]?re looking for|About you)\s*[:：]?",
            head,
            re.I,
        )
    )


def _strip_bare_section_headers(jd: str | None) -> str | None:
    if not jd:
        return jd
    lines = [ln for ln in jd.splitlines() if not _BARE_SECTION_HEADER_RE.match(ln.strip())]
    cleaned = "\n".join(lines).strip()
    return cleaned or None


def enrich_with_label_extraction(
    result: ParseResult,
    html: str | None,
    *,
    extra_text: str | None = None,
    ocr_enabled: bool = False,
    source_url: str | None = None,
) -> ParseResult:
    """
    主路径：HTML/纯文本标签抽取；缺关键字段且开启 OCR 时尝试图片 OCR 兜底。
    """
    # 分块抽取再合并，避免「空的任职要求」跨块吞掉下一页顶栏噪声
    texts: list[str] = []
    visible = html_to_visible_text(html) if html else ""
    if visible:
        texts.append(visible)
    if extra_text:
        texts.append(extra_text)
    if result.jd_text:
        texts.append(result.jd_text)

    labels: dict[str, str] = {}
    for block in texts:
        for key, val in extract_labeled_fields(block).items():
            if val and (key not in labels or not labels[key]):
                labels[key] = val
    result = apply_labeled_fields(result, labels)
    combined = "\n".join(texts)

    needs_more = _needs_ocr_fallback(result, visible)
    extras = result.extras if isinstance(result.extras, dict) else {}

    if ocr_enabled and needs_more:
        from app.collector.ocr_fallback import try_ocr_from_html

        ocr_text, note = try_ocr_from_html(html or "", base_url=source_url or result.apply_url)
        extras["ocr_note"] = note
        if ocr_text:
            ocr_labels = extract_labeled_fields(ocr_text)
            result = apply_labeled_fields(result, ocr_labels)
            if ocr_text and (not result.jd_text or len(result.jd_text) < 80):
                result.jd_text = (result.jd_text or "") + ("\n" if result.jd_text else "") + ocr_text[:15000]
            extras["ocr_used"] = True
        result.extras = extras
    elif needs_more and not ocr_enabled:
        extras.setdefault(
            "ocr_note",
            "正文偏少或关键字段缺失；可在 config 开启 ocr_enabled 并安装 OCR 可选依赖后重采",
        )
        result.extras = extras

    # 标签抽取后若仍无 project，用全文再分类一次
    if not result.recruit_project:
        project, bucket, tags = classify_from_text(result.title, result.jd_text or combined[:2000])
        result.recruit_project = project
        result.recruit_bucket = result.recruit_bucket or bucket
        if tags and not result.job_tags:
            result.job_tags = tags

    # 详情大标题（h1）优先于壳 og:title
    from app.collector.portal_nav import apply_channel_to_result, extract_detail_heading_title

    heading = extract_detail_heading_title(html)
    if heading and (not result.title or is_noise_or_shell_title(result.title)):
        result.title = heading

    # 最终再清洗：壳标题尝试从 JD 回填，失败则清空（勿把 chrome 写进 JD）
    cleaned = sanitize_job_title(result.title)
    if result.title and not cleaned:
        extras = result.extras if isinstance(result.extras, dict) else {}
        extras["raw_page_title"] = result.title
        result.extras = extras
        recovered = recover_title_from_jd(result.jd_text)
        result.title = recovered
    elif cleaned:
        result.title = cleaned

    if result.jd_text:
        result.jd_text = strip_career_nav_boilerplate(result.jd_text) or result.jd_text
    if result.work_location and is_noise_location(result.work_location):
        result.work_location = None

    # 门户顶栏/URL 频道 → recruit_project/bucket（社招不会标成校招）
    result = apply_channel_to_result(result, source_url or result.apply_url, html)

    # 无显式发布/更新日期时，用北森等源码线索填补（不覆盖已有 published_at）
    from app.collector.page_mtime import apply_page_mtime_to_result

    apply_page_mtime_to_result(result, html)

    # 结构化字段填好后，剥掉 JD 页眉重复元信息，并补学历
    labeled = {}
    extras = result.extras if isinstance(result.extras, dict) else {}
    raw_labels = extras.get("labeled_fields")
    if isinstance(raw_labels, dict):
        labeled = {str(k): str(v) for k, v in raw_labels.items() if v}
    if not result.education:
        result.education = extract_education_requirement(
            result.jd_text, labels=labeled, title=result.title
        )
    if not result.salary_range:
        result.salary_range = normalize_salary_range(labeled.get("salary_range"))
    if not result.headcount:
        result.headcount = normalize_headcount(labeled.get("headcount"))
    if result.jd_text:
        company = str(extras.get("company") or "").strip() or None
        extra_vals = [
            labeled.get("recruit_type") or "",
            labeled.get("work_type") or "",
        ]
        result.jd_text = (
            strip_redundant_jd_meta(
                result.jd_text,
                title=result.title,
                company=company,
                recruit_project=result.recruit_project,
                recruit_bucket=result.recruit_bucket,
                work_location=result.work_location,
                raw_category=result.raw_category,
                education=result.education,
                salary_range=result.salary_range,
                headcount=result.headcount,
                extra_values=extra_vals,
            )
            or result.jd_text
        )
    return result


def is_noise_or_shell_title(title: str | None) -> bool:
    from app.collector.filters import is_chrome_shell_title, is_noise_title

    t = (title or "").strip()
    if not t or is_noise_title(t) or is_chrome_shell_title(t):
        return True
    compact = re.sub(r"\s+", "", t)
    return compact in {
        "校园招聘",
        "社会招聘",
        "实习生招聘",
        "招聘",
        "招聘详细",
        "招聘系统--招聘详细",
        "招贤纳才",
        "招聘首页",
        "SearchJobs",
    }


def _needs_ocr_fallback(result: ParseResult, visible_text: str) -> bool:
    """HTML 几乎无正文，或解析结果缺地点/截止/JD 等关键字段。"""
    visible_len = len((visible_text or "").strip())
    jd_len = len((result.jd_text or "").strip())
    if visible_len < 100 and jd_len < 80:
        return True
    missing_key = (
        not result.work_location
        and not result.deadline
        and jd_len < 80
    )
    return bool(missing_key and visible_len < 400)


def labeled_fields_summary(labels: dict[str, Any]) -> str:
    if not labels:
        return ""
    parts = [f"{k}={v}" for k, v in labels.items() if v]
    return "; ".join(parts[:12])
