import pytest

from dealsource.httpcache import CachedHttp, cache_url
from dealsource.models import GeoSpec
from dealsource.sources.census_cbp import CensusCBPSource, CensusError, parse_rows, store_stats


def make_source(conn, server, api_key=None):
    return CensusCBPSource(CachedHttp(conn, server.client(), "dealsource-test"), api_key)


def test_fetch_parses_totals_and_size_classes(conn, cbp_server):
    stats = list(
        make_source(conn, cbp_server).fetch(["332300"], [GeoSpec.parse("state:13,37")], 2022)
    )
    by_state = {s.geo_code: s for s in stats}
    ga = by_state["13"]
    assert (ga.geo_name, ga.establishments, ga.employees, ga.annual_payroll_usd_k) == (
        "Georgia",
        412,
        9870,
        512340,
    )
    assert ga.naics_label == "Architectural and structural metals manufacturing"
    assert ga.size_classes == {
        "212": {"label": "Establishments with 1 to 4 employees", "establishments": 150},
        "220": {"label": "Establishments with 5 to 9 employees", "establishments": 90},
    }
    assert by_state["37"].establishments == 505
    assert by_state["37"].size_classes == {}


def test_second_fetch_is_served_from_cache(conn, cbp_server):
    args = (["332300", "332700"], [GeoSpec.parse("state:13,37"), GeoSpec.parse("us")], 2022)
    first = make_source(conn, cbp_server)
    list(first.fetch(*args))
    assert (first.requests_made, first.cache_hits) == (4, 0)
    second = make_source(conn, cbp_server)
    list(second.fetch(*args))
    assert (second.requests_made, second.cache_hits) == (0, 4)
    assert len(cbp_server.calls) == 4


def test_no_data_response_is_skipped(conn, cbp_server):
    assert list(make_source(conn, cbp_server).fetch(["999999"], [GeoSpec.parse("us")], 2022)) == []


def test_api_key_is_sent_but_never_cached(conn, cbp_server):
    list(
        make_source(conn, cbp_server, api_key="SECRET123").fetch(
            ["332700"], [GeoSpec.parse("us")], 2022
        )
    )
    assert cbp_server.calls[0].url.params["key"] == "SECRET123"
    urls = [r[0] for r in conn.execute("SELECT url FROM http_cache")]
    assert urls and all("SECRET123" not in u and "key=" not in u for u in urls)


def test_missing_or_bad_key_redirect_is_a_clear_error_and_not_cached(conn, cbp_server):
    with pytest.raises(CensusError, match="CENSUS_API_KEY"):
        list(
            make_source(conn, cbp_server, api_key="BAD").fetch(
                ["332700"], [GeoSpec.parse("us")], 2022
            )
        )
    assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0


def test_unsupported_year(conn, cbp_server):
    with pytest.raises(CensusError, match="not supported"):
        list(make_source(conn, cbp_server).fetch(["332700"], [GeoSpec.parse("us")], 2012))


def test_store_stats_upserts(conn, cbp_server):
    stats = list(
        make_source(conn, cbp_server).fetch(["332300"], [GeoSpec.parse("state:13,37")], 2022)
    )
    assert store_stats(conn, stats) == 2
    store_stats(conn, stats)
    assert conn.execute("SELECT COUNT(*) FROM market_stats").fetchone()[0] == 2
    assert (
        conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0] == 0
    )  # never creates companies


def test_parse_rows_keeps_only_all_legal_forms():
    rows = [
        ["NAME", "NAICS2017", "LFO", "ESTAB", "EMP", "PAYANN", "state"],
        ["Georgia", "332300", "001", "412", "9870", "512340", "13"],
        ["Georgia", "332300", "002", "40", "300", "9000", "13"],
    ]
    (stat,) = parse_rows(rows, year=2022, naics_field="NAICS2017", geo_level="state")
    assert stat.establishments == 412


def test_parse_rows_rejects_unexpected_shape():
    with pytest.raises(CensusError):
        parse_rows(
            [["NAME", "state"], ["Georgia", "13"]],
            year=2022,
            naics_field="NAICS2017",
            geo_level="state",
        )


@pytest.mark.parametrize(
    ("spec", "level", "codes", "within"),
    [
        ("us", "us", ("1",), None),
        ("state:13,37", "state", ("13", "37"), None),
        ("state:*", "state", ("*",), None),
        ("county:*/state:13", "county", ("*",), "13"),
    ],
)
def test_geo_spec_parse(spec, level, codes, within):
    g = GeoSpec.parse(spec)
    assert (g.level, g.codes, g.within_state) == (level, codes, within)


@pytest.mark.parametrize("spec", ["county:*", "metro:1", "state:13/state:1"])
def test_geo_spec_rejects_bad_input(spec):
    with pytest.raises(ValueError):
        GeoSpec.parse(spec)


def test_cache_url_is_order_independent_and_drops_secrets():
    assert cache_url("https://x.test/a", {"b": "2", "a": "1", "key": "s"}) == cache_url(
        "https://x.test/a", {"a": "1", "b": "2"}
    )
