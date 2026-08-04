"""统一时钟：Asia/Shanghai + CAMPUS_JOBS_NOW。"""

from datetime import date

from app.timeutil import ENV_NOW, CN_TZ, now, today, utc_now_iso


def test_campus_jobs_now_override(monkeypatch):
    monkeypatch.setenv(ENV_NOW, "2026-08-02 11:28:15")
    dt = now()
    assert dt.year == 2026 and dt.month == 8 and dt.day == 2
    assert dt.hour == 11 and dt.minute == 28 and dt.second == 15
    assert dt.utcoffset() == CN_TZ.utcoffset(dt)
    assert today() == date(2026, 8, 2)
    iso = utc_now_iso()
    assert "2026-08-02" in iso
    # UTC = 中国本地 - 8h → 03:28
    assert "03:28:15" in iso


def test_default_now_without_override(monkeypatch):
    monkeypatch.delenv(ENV_NOW, raising=False)
    dt = now()
    assert dt.tzinfo is not None
    assert abs((dt.date() - date.today()).days) <= 1
