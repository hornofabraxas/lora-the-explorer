from lora_explorer.main import browser_host


def test_browser_host_maps_wildcard_to_loopback():
    # 0.0.0.0 / :: are bind wildcards a browser can't connect to (Windows
    # especially) — the advertised/opened URL must use loopback instead.
    assert browser_host("0.0.0.0") == "127.0.0.1"
    assert browser_host("::") == "127.0.0.1"
    assert browser_host("") == "127.0.0.1"


def test_browser_host_preserves_explicit_host():
    # An operator who bound a specific interface meant that address; keep it.
    assert browser_host("127.0.0.1") == "127.0.0.1"
    assert browser_host("192.168.1.50") == "192.168.1.50"
    assert browser_host("lora.local") == "lora.local"
