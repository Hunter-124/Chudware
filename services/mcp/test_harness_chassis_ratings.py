"""NASA spaceflight wire ratings are not generic chassis ampacities."""
import pytest

from services.harness import wire_data


@pytest.mark.parametrize("awg,vacuum,cabin", [
    (26, 3.4, 3.8), (24, 4.7, 5.4), (22, 6.5, 7.4),
    (20, 8.8, 10.0), (18, 11.6, 13.2), (16, 13.3, 15.0),
    (14, 18.0, 20.0), (12, 25.0, 29.0), (10, 34.8, 40.0),
])
def test_nasa_published_single_wire_rows(awg, vacuum, cabin):
    assert wire_data.nasa_single_wire_ampacity(
        awg, 200, pressure_psia=0, ambient_c=94) == vacuum
    assert wire_data.nasa_single_wire_ampacity(
        awg, 200, pressure_psia=4.3, ambient_c=22) == cabin


@pytest.mark.parametrize("rating,vacuum_factor,cabin_factor", [
    (200, 1, 1), (150, .65, .8), (135, .45, .75), (105, None, .65),
])
def test_nasa_insulation_derating(rating, vacuum_factor, cabin_factor):
    assert wire_data.nasa_single_wire_ampacity(
        20, rating, pressure_psia=14.7, ambient_c=22) == pytest.approx(10 * cabin_factor)
    if vacuum_factor is None:
        with pytest.raises(wire_data.NotTabulated):
            wire_data.nasa_single_wire_ampacity(20, rating, pressure_psia=0, ambient_c=94)
    else:
        assert wire_data.nasa_single_wire_ampacity(
            20, rating, pressure_psia=0, ambient_c=94) == pytest.approx(8.8 * vacuum_factor)


@pytest.mark.parametrize("count,factor", [(1, 1), (2, 27/28), (14, 15/28),
                                          (15, .5), (16, .5), (100, .5)])
def test_nasa_helical_bundle_factor(count, factor):
    assert wire_data.nasa_bundle_factor(count) == pytest.approx(factor)
    assert wire_data.nasa_harness_ampacity(
        20, 200, pressure_psia=14.7, ambient_c=22, n_wires=count) == pytest.approx(10 * factor)


@pytest.mark.parametrize("count", [0, -1, 1.5, True])
def test_nasa_rejects_invalid_bundle_count(count):
    with pytest.raises(wire_data.NotTabulated):
        wire_data.nasa_bundle_factor(count)


@pytest.mark.parametrize("awg,rating,pressure,ambient", [
    (23, 200, 14.7, 22), (28, 200, 14.7, 22),
    (20, 90, 14.7, 22), (20, 200, 14.7, 23),
    (20, 200, 4.29, 22), (20, 200, 4.3, 94),
    (20, 200, -1, 94), (20, 200, float('nan'), 22),
])
def test_nasa_rejects_untabulated_conditions(awg, rating, pressure, ambient):
    with pytest.raises(wire_data.NotTabulated):
        wire_data.nasa_single_wire_ampacity(
            awg, rating, pressure_psia=pressure, ambient_c=ambient)
