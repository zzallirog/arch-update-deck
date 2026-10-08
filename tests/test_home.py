"""Home by the access point's BSSID: read with nmcli or iw, never set; not knowing is bounded like the quiet wait.

No test reads the real Wi-Fi: every reader here is a scripted Shell, and the clock lives in the test's state directory.
"""

import json
import time

import pytest

from arch_updater import day, exits, home, watch
from test_auto import box, failed, ok  # noqa: F401 - box is a fixture

HOME_AP, OTHER_AP = "a4:2b:b0:11:22:33", "10:20:30:40:50:60"


def terse(bssid: str) -> str:
    return bssid.upper().replace(":", "\\:")


class Air:
    """A Shell that answers only the Wi-Fi readers, from a script."""

    def __init__(self, tools=("nmcli",), nmcli=None, iw_dev=None, links=None):
        self.tools, self.nmcli, self.iw_dev, self.links, self.calls = set(tools), nmcli, iw_dev, links or {}, []

    def has(self, tool):
        return tool in self.tools

    def capture(self, argv, timeout=30):
        self.calls.append(list(argv))
        if argv == home.NMCLI:
            return self.nmcli
        if argv == ["iw", "dev"]:
            return self.iw_dev
        return self.links.get(argv[2], failed("no such device", 237))


def on(*bssids, current=None):
    lines = [f"{'*' if bssid == current else ' '}:{terse(bssid)}" for bssid in bssids]
    return Air(nmcli=ok("\n".join(lines) + "\n"))


def test_nmcli_names_the_access_point_in_use_with_its_colons_unescaped() -> None:
    assert home.connected(on(OTHER_AP, HOME_AP, current=HOME_AP)) == [HOME_AP]
    assert home.connected(on(OTHER_AP, HOME_AP)) == [], "networks in range are not the one in use"


def test_without_networkmanager_iw_is_asked() -> None:
    air = Air(
        tools=("nmcli", "iw"),
        nmcli=failed("Error: NetworkManager is not running.", 8),
        iw_dev=ok("phy#0\n\tInterface wlan0\n\t\ttype managed\n"),
        links={"wlan0": ok(f"Connected to {HOME_AP} (on wlan0)\n\tSSID: anything\n")},
    )
    assert home.connected(air) == [HOME_AP]
    assert ["iw", "dev", "wlan0", "link"] in air.calls


def test_nothing_that_answers_is_none_and_not_connected_is_empty() -> None:
    assert home.connected(Air(tools=())) is None
    assert home.connected(Air(tools=("nmcli",), nmcli=failed("Error: NetworkManager is not running.", 8))) is None
    iw = Air(tools=("iw",), iw_dev=ok("\tInterface wlan0\n"), links={"wlan0": ok("Not connected.\n")})
    assert home.connected(iw) == []


@pytest.mark.parametrize(
    ("configured", "air", "where"),
    [
        ([HOME_AP.upper()], on(HOME_AP, current=HOME_AP), "home"),  # written in capitals, read in small letters
        (HOME_AP, on(HOME_AP, current=HOME_AP), "home"),  # one address as a plain string
        ([HOME_AP], on(OTHER_AP, current=OTHER_AP), "away"),
        ([HOME_AP], on(HOME_AP), "unknown"),  # in range, but not connected: a cable, a phone over USB
        ([HOME_AP], Air(tools=()), "unknown"),
        (["my router"], on(HOME_AP, current=HOME_AP), "unknown"),  # no address in it: not a reason to call anything home
        ([], on(HOME_AP, current=HOME_AP), "unknown"),
    ],
)
def test_where_the_machine_is(configured, air, where) -> None:
    assert home.locate({"home_bssid": configured}, air).where == where


def test_not_configured_is_no_check_at_all() -> None:
    air = Air()
    assert home.locate({"conditions": []}, air) is None and air.calls == [], "nothing is read for an owner who did not ask"


def test_the_unknown_clock_starts_once_and_any_reading_that_knew_clears_it(tmp_path) -> None:
    unknown, away = home.Place("unknown", "not connected to Wi-Fi"), home.Place("away", "elsewhere")
    home.track(unknown, tmp_path, now=1000.0)
    home.track(unknown, tmp_path, now=5000.0)
    assert json.loads((tmp_path / home.UNKNOWN_SINCE).read_text())["since"] == 1000.0, "the clock keeps its start"
    day_ = 86400
    assert not home.overdue(tmp_path, now=1000.0 + (home.MAX_UNKNOWN_DAYS - 1) * day_)
    assert home.overdue(tmp_path, now=1000.0 + home.MAX_UNKNOWN_DAYS * day_)
    home.track(away, tmp_path)
    assert not (tmp_path / home.UNKNOWN_SINCE).exists() and not home.overdue(tmp_path)


def scene(config, sh):
    return watch.Scene(sh, config, home.STATE_ROOT / "auto.json", home.STATE_ROOT / "keep", None)


def test_the_look_passes_at_home_and_holds_back_away_or_unknown_with_the_network_status() -> None:
    config = {"home_bssid": [HOME_AP], "conditions": []}
    (verdict,) = watch.probe_conditions(scene(config, on(HOME_AP, current=HOME_AP))).verdicts
    assert (verdict.probe, verdict.status) == ("home", "pass")
    for air in (on(OTHER_AP, current=OTHER_AP), Air(tools=())):
        (verdict,) = watch.probe_conditions(scene(config, air)).verdicts
        assert (verdict.status, verdict.case, verdict.code) == ("blocked", "CONDITION", exits.NETWORK_BLOCKED)


def test_not_knowing_for_long_enough_lets_the_run_go_but_away_never_does() -> None:
    """Unknown as not home for ever would stop the updates for ever; a known foreign access point stays a no."""
    config = {"home_bssid": [HOME_AP], "conditions": []}
    home.track(home.Place("unknown", "x"), now=time.time() - home.MAX_UNKNOWN_DAYS * 86400 - 60)
    (verdict,) = watch.probe_conditions(scene(config, Air(tools=()))).verdicts
    assert verdict.status == "pass" and f"{home.MAX_UNKNOWN_DAYS} days" in verdict.detail
    (verdict,) = watch.probe_conditions(scene(config, on(OTHER_AP, current=OTHER_AP))).verdicts
    assert verdict.status == "blocked", "the clock is for not knowing, not for being away"


def test_the_look_only_reads_the_clock() -> None:
    config = {"home_bssid": [HOME_AP], "conditions": []}
    watch.probe_conditions(scene(config, Air(tools=())))
    assert not (home.STATE_ROOT / home.UNKNOWN_SINCE).exists(), "only the timer's run keeps it"


class AwayMachine:
    """The test_auto machine, out of the house: nmcli says it is on someone else's access point."""

    def __init__(self, sh, bssid):
        self.sh, self.bssid = sh, bssid
        sh.tools.add("nmcli")
        plain = sh.capture

        def capture(argv, timeout=30):
            if argv == home.NMCLI:
                sh.calls.append(list(argv))
                return ok(f"*:{terse(self.bssid)}\n") if self.bssid else ok("")
            return plain(argv, timeout)

        sh.capture = capture


@pytest.fixture
def house(request):
    """test_auto's scripted machine (its `box` fixture), named apart so the import is not shadowed."""
    return request.getfixturevalue("box")


@pytest.fixture
def asked(house, monkeypatch):
    house.sh.tools |= {"kitty", "notify-send"}
    house.config(ask=True, home_bssid=[HOME_AP])
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    return house


def test_away_from_home_the_timer_opens_no_window_and_skips(asked) -> None:
    AwayMachine(asked.sh, OTHER_AP)
    assert day.scheduled(asked.sh) == exits.SKIPPED
    assert asked.sh.ran == [] and not (home.STATE_ROOT / home.UNKNOWN_SINCE).exists()


def test_at_home_the_timer_opens_the_window(asked) -> None:
    AwayMachine(asked.sh, HOME_AP)
    assert day.scheduled(asked.sh) == 70, "the window opened and nobody answered in it"
    assert asked.sh.ran[0][0] == "kitty"


def test_the_timer_starts_the_clock_when_it_cannot_tell(asked) -> None:
    AwayMachine(asked.sh, None)  # nmcli answers, no access point in use
    assert day.scheduled(asked.sh) == exits.SKIPPED and asked.sh.ran == []
    assert (home.STATE_ROOT / home.UNKNOWN_SINCE).is_file()


def test_unasked_away_nothing_runs_and_the_week_is_not_spent(house) -> None:
    house.config(home_bssid=[HOME_AP])
    AwayMachine(house.sh, OTHER_AP)
    assert day.scheduled(house.sh) == exits.SKIPPED
    assert house.sh.ran == [] and not (house.root / "last-auto.json").exists()


def test_by_hand_away_the_run_stops_before_any_package_manager(house) -> None:
    house.config(home_bssid=[HOME_AP])
    AwayMachine(house.sh, OTHER_AP)
    report = house.run()
    assert report["exit_code"] == exits.NETWORK_BLOCKED and house.sh.ran == []
    assert [(item["probe"], item["case"]) for item in report["trouble"]] == [("home", "CONDITION")]


def test_no_reader_is_needed_when_home_is_not_configured(house) -> None:
    house.sh.tools |= {"nmcli", "iw"}
    house.run()
    assert not any(call[0] in ("nmcli", "iw") for call in house.sh.calls)

