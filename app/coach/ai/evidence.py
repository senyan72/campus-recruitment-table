"""证据与优势规则引擎。"""

from __future__ import annotations

import re
from typing import Any


_BULLET = re.compile(r"^\s*[-•*\d+\.]+\s*(.+)$")


def extract_experience_assets(text: str, *, source: str = "resume") -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        m = _BULLET.match(line)
        body = (m.group(1) if m else line).strip()
        if len(body) < 8:
            continue
        if len(body) > 240:
            body = body[:240]
        assets.append(
            {
                "id": f"exp_{i+1}",
                "source": source,
                "text": body,
                "kind": "experience",
                "status": "pending",
            }
        )
        if len(assets) >= 20:
            break
    return assets


def mine_strengths(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    strengths: list[dict[str, Any]] = []
    keywords = {
        "协作": ["协作", "沟通", "团队", "跨部门"],
        "交付": ["上线", "交付", "完成", "落地"],
        "分析": ["分析", "数据", "调研", "复盘"],
        "技术": ["开发", "实现", "算法", "系统", "接口"],
        "领导": ["负责", "带领", "组织", "协调"],
    }
    for asset in assets:
        text = str(asset.get("text") or "")
        for name, keys in keywords.items():
            if any(k in text for k in keys):
                strengths.append(
                    {
                        "id": f"str_{len(strengths)+1}",
                        "name": name,
                        "evidence": text,
                        "behavior": f"在经历中体现与「{name}」相关的具体行动",
                        "capability": name,
                        "job_signal": f"适合需要{name}能力的校招岗位",
                        "credibility": "中",
                        "missing_evidence": []
                        if len(text) > 20
                        else ["缺少可核验结果或职责边界"],
                        "fact_ref": asset.get("id"),
                    }
                )
                break
        if len(strengths) >= 8:
            break
    if not strengths and assets:
        strengths.append(
            {
                "id": "str_1",
                "name": "待澄清优势",
                "evidence": assets[0].get("text"),
                "behavior": "信息不足，需追问具体行动",
                "capability": "未知",
                "job_signal": "信息不足",
                "credibility": "低",
                "missing_evidence": ["行动", "结果", "个人贡献"],
                "fact_ref": assets[0].get("id"),
            }
        )
    return strengths
