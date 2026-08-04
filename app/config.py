"""本地配置加载（密钥不进 git）。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

APP_NAME = "campus-jobs"
DEFAULT_SYNC_INTERVAL_MINUTES = 30
APP_VERSION = "0.2.0"

# 合法企业性质词表（导入清洗用）
COMPANY_NATURES = frozenset(
    {
        "私企",
        "国企",
        "央企",
        "外企",
        "合资",
        "事业单位",
        "政府机关",
        "民营企业",
        "民企",
        "外资",
        "港澳台资",
        "其他",
    }
)

# 跳过的 xlsx sheet 关键词
SKIP_SHEET_KEYWORDS = ("表格说明", "内推码", "进度", "模板", "offer")

# 打包目录旁 config.json 可引导写入 AppData；永不从旁路文件导入 service_role
_SIDECAR_CLOUD_KEYS = ("supabase_url", "supabase_anon_key")


def app_data_dir() -> Path:
    """返回本机数据目录（配置、SQLite、密钥）。"""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def bundle_dir() -> Path:
    """可执行文件/分发包目录：冻结时为 exe 旁，开发时为项目根。"""
    override = (os.environ.get("CAMPUS_JOBS_BUNDLE_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return project_root()


def config_path() -> Path:
    return app_data_dir() / "config.json"


def sidecar_config_path() -> Path:
    """分发包内与 exe 同级的 config.json（同学端云同步开箱配置）。"""
    return bundle_dir() / "config.json"


def db_path() -> Path:
    return app_data_dir() / "campus_jobs.db"


def default_config() -> dict[str, Any]:
    return {
        "mode": "viewer",
        "supabase_url": "",
        "supabase_anon_key": "",
        "supabase_service_role_key": "",
        "sync_interval_minutes": DEFAULT_SYNC_INTERVAL_MINUTES,
        "rss_bridge_base": "",
        "wechat2rss_base": "",
        "seed_xlsx_path": "",
        # 多份种子表（春招/秋招等）；导入时与 seed_xlsx_path 合并去重
        "seed_xlsx_paths": [],
        "last_cloud_sync_at": None,
        "min_app_version": "0.2.0",
        # 深度采集时间窗：近 3 个月（90 天）；可改 lookback_days 或 collect_months
        "collect_months": 3,
        "lookback_days": 90,
        # 校园/实习列表翻页：卡片更新日期近 N 个月（与 collect_months 独立）
        "list_collect_months": 6,
        # 本地岗位保留天数：open_at/updated_at/created_at 最晚参照日早于此则软删
        "retention_days": 365,
        "deep_collect_batch_size": 10,
        "deep_collect_max_companies": 20,
        "deep_collect_concurrency": 1,
        "deep_collect_max_retries": 2,
        # 合规访问策略：公共网页抓取按域名串行，并至少间隔 1.5 秒；命中安全拦截即跳过企业。
        "collector_min_host_interval_seconds": 1.5,
        # 同一企业在一次深度采集中最多自动续采批数（每批仍受单企业上限约束）
        "deep_collect_max_company_batches": 10,
        "deep_collect_parse_supplement_limit": 5,
        "deep_collect_parse_supplement_timeout": 20,
        # 深采子进程按公开搜索摘要补全企业性质；无明确证据则保持空值
        "company_nature_lookup_enabled": True,
        "company_nature_lookup_timeout": 5,
        "company_nature_lookup_max_calls": 2,
        # 岗位审核「重新识别」独立限额；与深度采集单企业批量分开
        "reidentify_collect_batch": 15,
        "reidentify_detail_timeout": 45,
        # 单企业每轮批量/深度采集最多新入库岗位数（默认50；200岗约4轮）
        "per_company_collect_batch": 50,
        # 夜间自动复检（Admin 进程内）；校招+实习全量；0=不限公司数
        "nightly_enabled": True,
        "nightly_hour": 2,
        "nightly_minute": 0,
        "nightly_limit_companies": 0,
        # 详情页正文偏少时可选 OCR 兜底（需 pip install rapidocr-onnxruntime）
        "ocr_enabled": False,
    }


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        # utf-8-sig：兼容 Windows PowerShell Set-Content UTF8 带 BOM 的旁路 config
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _viewer_safe_from_sidecar(raw: dict[str, Any]) -> dict[str, Any]:
    """从旁路 config 提取同学端可用字段；强制清空 service_role。"""
    cfg = default_config()
    for key, value in raw.items():
        if key == "supabase_service_role_key":
            continue
        if key in cfg or str(key).startswith("_"):
            cfg[key] = value
    cfg["mode"] = "viewer"
    cfg["supabase_service_role_key"] = ""
    return cfg


def bootstrap_config_from_sidecar() -> bool:
    """若 AppData 尚无配置、但 exe 旁有 config.json，则写入本机（仅 anon）。"""
    path = config_path()
    if path.exists():
        return False
    side = sidecar_config_path()
    if not side.exists():
        return False
    raw = _read_json_dict(side)
    if not raw:
        return False
    save_config(_viewer_safe_from_sidecar(raw))
    return True


def load_config() -> dict[str, Any]:
    bootstrap_config_from_sidecar()
    path = config_path()
    cfg = default_config()
    if path.exists():
        data = _read_json_dict(path)
        if data:
            cfg.update({k: v for k, v in data.items() if k in cfg or str(k).startswith("_")})
    # 旁路 config：补全本机缺失的云端只读凭证（换机解压即用）
    side = sidecar_config_path()
    if side.exists() and side.resolve() != path.resolve():
        side_data = _read_json_dict(side)
        for key in _SIDECAR_CLOUD_KEYS:
            if not str(cfg.get(key) or "").strip() and str(side_data.get(key) or "").strip():
                cfg[key] = side_data[key]
        if not str(cfg.get("mode") or "").strip():
            cfg["mode"] = "viewer"
    # 环境变量可覆盖（便于开发）
    for env_key, cfg_key in (
        ("CAMPUS_JOBS_SUPABASE_URL", "supabase_url"),
        ("CAMPUS_JOBS_SUPABASE_ANON_KEY", "supabase_anon_key"),
        ("CAMPUS_JOBS_SUPABASE_SERVICE_ROLE_KEY", "supabase_service_role_key"),
        ("CAMPUS_JOBS_MODE", "mode"),
    ):
        val = os.environ.get(env_key)
        if val:
            cfg[cfg_key] = val
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    path = config_path()
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_seed_xlsx_paths(cfg: dict[str, Any] | None = None) -> list[str]:
    """合并 seed_xlsx_paths 与 seed_xlsx_path，去重并保留顺序。"""
    data = cfg if cfg is not None else load_config()
    paths: list[str] = []
    raw_list = data.get("seed_xlsx_paths") or []
    if isinstance(raw_list, str):
        raw_list = [p.strip() for p in raw_list.replace(";", "\n").splitlines() if p.strip()]
    if isinstance(raw_list, list):
        for p in raw_list:
            s = str(p or "").strip()
            if s:
                paths.append(s)
    single = str(data.get("seed_xlsx_path") or "").strip()
    if single and single not in paths:
        paths.append(single)
    # 去重（保留首次出现）
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        key = str(Path(p))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def example_config_path() -> Path:
    return project_root() / "data" / "config.example.json"
