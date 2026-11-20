"""APA gauge comparison: SNIRH table parsing, datum conversion and forecast-vs-measured stats."""

from datetime import datetime, timezone

from floodcast.operational import gauges

GAUGE = {"code": "23F/01H", "name": "Alcácer do Sal", "snirh_site": 1627743394, "snirh_parameter": 1843,
         "gauge_zero_m": -2.18, "lat": 38.370111, "lon": -8.506972}

HTML = """<table><tr><th>Data</th><th>Nível hidrométrico Instantâneo (m)</th></tr>
<tr><td>19/11/2026 22:00</td><td>3.19</td></tr>
<tr><td>19/11/2026 23:00</td><td>3.45</td></tr>
<tr><td>20/11/2026 00:00</td><td>3.40</td></tr>
<tr><td>25/11/2026 00:00</td><td>9.99</td></tr></table>"""


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body.encode("latin-1")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_observed_parses_and_converts_to_msl(monkeypatch):
    monkeypatch.setattr(gauges.urllib.request, "urlopen", lambda req, timeout=60: _Resp(HTML))
    t0 = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    t1 = datetime(2026, 11, 20, 6, tzinfo=timezone.utc)
    obs = gauges.fetch_observed(GAUGE, t0, t1)
    assert obs["times_utc"] == ["2026-11-19T22:00:00Z", "2026-11-19T23:00:00Z", "2026-11-20T00:00:00Z"]
    assert obs["level_m"] == [1.01, 1.27, 1.22]   # level above gauge zero + (-2.18 m)


def test_comparison_uses_common_hours_only():
    doc = {"predicted": {"times_utc": ["2026-11-19T22:00:00Z", "2026-11-19T23:00:00Z", "2026-11-20T00:00:00Z"],
                         "p50": [0.5, 0.7, None]},
           "observed": {"times_utc": ["2026-11-19T21:00:00Z", "2026-11-19T22:00:00Z", "2026-11-19T23:00:00Z",
                                      "2026-11-20T00:00:00Z"], "level_m": [0.0, 1.0, 1.2, 1.1]}}
    c = gauges.comparison(doc, "p50")
    assert c["n"] == 2 and c["bias_m"] == -0.5 and c["mae_m"] == 0.5
    assert c["last"]["time_utc"] == "2026-11-19T23:00:00Z"
    assert gauges.comparison({"predicted": {}, "observed": {}}) is None
