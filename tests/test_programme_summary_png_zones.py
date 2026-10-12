"""The programme summary PNG draws the planner's three zones (slim/9.3, I2):
it reads intensity_dist's zone1_min .. zone3_min and says the method. Until
the evaluation of 2382cfd1 no test covered the renaming; a PNG reading the old
z1z2_min keys draws "no data"."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")

import programme_summary_png as png  # noqa: E402


def test_intensity_panel_names_zones_and_method():
    img = Image.new("RGB", (400, 200))
    draw = ImageDraw.Draw(img)
    said = []
    real = draw.text
    draw.text = lambda xy, text, *a, **k: (said.append(text), real(xy, text, *a, **k))
    png._draw_intensity_dist(draw, 0, 0, 400, 200,
                             {"zone1_min": 800, "zone2_min": 100, "zone3_min": 100, "method": "time in zone"})
    assert "Intensity distribution (min, time in zone)" in said
    assert "zone 1 80%" in said and "zone 2 10%" in said and "zone 3 10%" in said
    assert "no data" not in said
