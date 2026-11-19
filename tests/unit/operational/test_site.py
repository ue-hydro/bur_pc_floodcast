from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from floodcast.operational.site import DEPTH_CLASSES, build_site

from .make_demo_data import write_demo_operational_dir

EXTERNAL = re.compile(r'(?:src|href)="(https?://[^"]+)"')
ALLOWED_EXTERNAL = (
    "https://www.ipma.pt",
    "https://www.prociv.gov.pt",
    "https://snirh.apambiente.pt",
    # partner institutions (logo links)
    "https://www.uevora.pt",
    "https://www.med.uevora.pt",
    "https://www.create.uevora.pt",
    "https://github.com/",
)


def _build(tmp_path, **kwargs):
    op = tmp_path / "op"
    op.mkdir(exist_ok=True)
    if kwargs.pop("demo", True):
        write_demo_operational_dir(op, **kwargs)
    out = tmp_path / "site"
    files = build_site(op, out, site_title="Floodcast Alcácer do Sal")
    return out, files


def test_no_data_renders_starting_page(tmp_path):
    out, files = _build(tmp_path, demo=False)
    index = (out / "index.html").read_text()
    assert "O sistema está a iniciar" in index and "The system is starting" in index
    for name in ("history.html", "about.html", "assets/style.css", "assets/app.js"):
        assert (out / name).is_file()
    assert "112" in index and "prociv.gov.pt" in index
    assert out / "index.html" in files


def test_level_none(tmp_path):
    out, _ = _build(tmp_path, level="none", hydrology=False, hydraulics=False)
    index = (out / "index.html").read_text()
    assert "lvl-none" in index and "Sem alerta" in index
    assert "Neste momento a previsão não indica cheias" in index
    assert 'id="map"' not in index
    assert 'id="hydrology"' not in index
    assert "não foi necessária a simulação hidráulica" in index.lower()


def test_warning_with_hydrology_and_hydraulics(tmp_path):
    out, _ = _build(tmp_path, level="warning")
    index = (out / "index.html").read_text()
    assert "lvl-warning" in index and "Aviso" in index
    assert 'id="hydrology"' in index and "<polygon" in index and "polyline" in index
    assert 'id="precip"' in index
    assert 'id="map"' in index and 'id="map-data"' in index
    mapdata = json.loads(re.search(r'id="map-data">(.*?)</script>', index, re.S).group(1))
    scenario = mapdata["scenarios"][0]
    assert scenario["id"] == "p90" and scenario["west"] < scenario["east"]
    image = out / scenario["image"]
    assert image.is_file() and image.read_bytes().startswith(b"\x89PNG")
    for _low, _high, colour in DEPTH_CLASSES:
        assert colour in index
    assert "Probabilidade de exceder" in index
    assert (out / "api" / "latest.json").is_file()
    assert (out / "api" / "cycles").is_dir()


def test_failed_cycle_is_unknown_not_safe(tmp_path):
    out, _ = _build(tmp_path, failed=True)
    index = (out / "index.html").read_text()
    assert "lvl-unknown" in index and "Sem dados fiáveis" in index
    assert "Não assuma que não há risco" in index
    assert "IFS acquisition: 3 members incomplete" in index
    assert "lvl-none" not in index.split("<main")[1].split("</main>")[0].split("Etapas")[0]


def test_required_content_and_privacy(tmp_path):
    out, _ = _build(tmp_path, level="warning")
    for page in ("index.html", "history.html", "about.html"):
        text = (out / page).read_text()
        assert "não é um serviço oficial de avisos" in text.lower()
        assert "not an official warning service" in text.lower()
        assert "112" in text
        assert "ECMWF" in text and "CC-BY-4.0" in text
        assert "SUMMA" in text and "mizuRoute" in text and "FLUXOS" in text
        assert 'data-updated=' in text and 'id="stale"' in text
        assert 'lang="pt"' in text and 'lang="en"' in text
        assert "<iframe" not in text and "analytics" not in text.lower()
        for url in EXTERNAL.findall(text):
            assert url.startswith(ALLOWED_EXTERNAL), url
    index = (out / "index.html").read_text()
    assert "ainda não está calibrado" in index
    assert "uncalibrated" not in index  # human text, not a raw flag
    for asset in ("style.css", "app.js"):
        text = (out / "assets" / asset).read_text()
        assert "document.cookie" not in text
        assert "fonts.googleapis" not in text and "@import" not in text


def test_calibrated_hides_uncalibrated_notice(tmp_path):
    out, _ = _build(tmp_path, level="none", hydrology=False, hydraulics=False, uncalibrated=False)
    assert "ainda não está calibrado" not in (out / "index.html").read_text()


def test_staleness_attribute_carries_update_time(tmp_path):
    old = datetime.now(UTC) - timedelta(hours=30)
    out, _ = _build(tmp_path, level="none", hydrology=False, hydraulics=False, updated_at=old)
    index = (out / "index.html").read_text()
    assert f'data-updated="{old.strftime("%Y-%m-%dT%H:%M:%SZ")}"' in index
    assert "mais de 18 horas" in index and "more than 18 hours" in index


def test_history_and_cycle_pages(tmp_path):
    out, _ = _build(tmp_path, level="warning", previous_cycles=2)
    history = (out / "history.html").read_text()
    assert history.count("<tr>") == 1 + 3  # header + 3 cycles
    older = out / "cycles" / "2026111712" / "index.html"
    assert older.is_file()
    text = older.read_text()
    assert "../../assets/style.css" in text and "Está a ver um ciclo anterior" in text
    assert 'href="cycles/2026111712/index.html"' in history


def test_single_language_has_no_toggle(tmp_path):
    op = tmp_path / "op"
    write_demo_operational_dir(op, level="none", hydrology=False, hydraulics=False)
    out = tmp_path / "site"
    build_site(op, out, languages=("en",))
    index = (out / "index.html").read_text()
    assert "langtoggle" not in index.split("<main")[0].split("<body")[1]
    assert 'lang="pt"' not in index.split("<main")[1]
    assert "No alert" in index


def test_atomic_replacement_and_no_leftovers(tmp_path):
    op = tmp_path / "op"
    write_demo_operational_dir(op, level="none", hydrology=False, hydraulics=False)
    out = tmp_path / "site"
    out.mkdir()
    (out / "stale.txt").write_text("old")
    build_site(op, out)
    assert not (out / "stale.txt").exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []
    with pytest.raises(ValueError):
        build_site(op, tmp_path / "bad", languages=("fr",))
    assert not (tmp_path / "bad").exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_html_escapes_untrusted_text(tmp_path):
    op = tmp_path / "op"
    write_demo_operational_dir(op, level="none", hydrology=False, hydraulics=False)
    latest = json.loads((op / "latest.json").read_text())
    latest["message"] = "<script>alert(1)</script>"
    (op / "latest.json").write_text(json.dumps(latest))
    status_path = op / latest["path"]
    status = json.loads(status_path.read_text())
    status["message"] = "<script>alert(1)</script>"
    status_path.write_text(json.dumps(status))
    out = tmp_path / "site"
    build_site(op, out)
    assert "<script>alert(1)</script>" not in (out / "index.html").read_text()
