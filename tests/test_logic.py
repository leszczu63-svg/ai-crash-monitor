"""Testy logiki: python -m pytest tests  (albo: python tests/test_logic.py)"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor import analyze_drawdown, analyze_h100, find_combo_episodes  # noqa: E402

D = dt.date.fromisoformat


def test_drawdown_crossing_and_rearm():
    pts = [("2024-01-01", 100), ("2024-01-02", 60), ("2024-01-03", 49), ("2024-01-04", 48),
           ("2024-01-05", 52), ("2024-01-06", 50), ("2024-01-07", 60), ("2024-01-08", 50)]
    a = analyze_drawdown(pts, 0.5, 0.8, 0.05)
    # 49 -> przekroczenie; 52 (−48%) nie uzbraja ponownie (margines 5 pp); 60 uzbraja; 50 -> nowe przekroczenie
    assert a["crossings"] == ["2024-01-03", "2024-01-08"]
    assert a["warn_crossings"] == ["2024-01-02"]
    assert a["ath"] == 100 and a["below"]


def test_drawdown_uses_running_ath():
    pts = [("2024-01-01", 100), ("2024-01-02", 55), ("2024-01-03", 200), ("2024-01-04", 99)]
    a = analyze_drawdown(pts, 0.5, 0.8, 0.05)
    assert a["crossings"] == ["2024-01-04"]


def test_h100_five_consecutive_days():
    s = {f"2026-01-{i:02d}": p for i, p in enumerate([1.2, 0.99, 1.0, 0.9, 0.95, 0.97, 0.96, 1.1], start=1)}
    r = analyze_h100(s, 1.0, 5, 1.5)
    assert r["crossings"] == ["2026-01-06"]
    assert r["qualifying_days"] == ["2026-01-06", "2026-01-07"]


def test_h100_gap_breaks_streak():
    s = {"2026-01-01": 0.9, "2026-01-02": 0.9, "2026-01-04": 0.9, "2026-01-05": 0.9, "2026-01-06": 0.9}
    assert analyze_h100(s, 1.0, 5, 1.5)["crossings"] == []


def test_combo_within_90_days():
    occ = {"a": ["2026-01-01"], "b": ["2026-02-15"], "c": ["2026-03-31"], "d": []}
    eps, active = find_combo_episodes(occ, 90, 3, D("2026-04-05"))
    assert len(eps) == 1 and eps[0]["start"] == "2026-03-31" and eps[0]["end"] == "2026-03-31"
    assert active == {"b", "c"}  # 5 kwietnia 'a' wypadło już z okna


def test_combo_91_days_is_too_long():
    occ = {"a": ["2026-01-01"], "b": ["2026-02-15"], "c": ["2026-04-01"]}  # 91 dni łącznie
    eps, _ = find_combo_episodes(occ, 90, 3, D("2026-04-05"))
    assert eps == []


def test_combo_deadline():
    occ = {"a": ["2026-01-01"], "b": ["2026-01-02"], "c": ["2026-01-10"]}
    assert find_combo_episodes(occ, 90, 3, D("2026-02-01"), deadline="2026-01-05")[0] == []
    assert len(find_combo_episodes(occ, 90, 3, D("2026-02-01"), deadline="2026-01-10")[0]) == 1


def test_combo_count_increase_recorded():
    occ = {"a": ["2026-01-01"], "b": ["2026-01-02"], "c": ["2026-01-03"], "d": ["2026-01-20"]}
    eps, _ = find_combo_episodes(occ, 90, 3, D("2026-01-25"))
    assert eps[0]["count_dates"] == {3: "2026-01-03", 4: "2026-01-20"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
