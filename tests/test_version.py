"""Version reporting for frozen (PyInstaller) builds.

The regression these exist for: installing 0.4.2 over 0.4.1 on Windows left the
old `lora_the_explorer-0.4.1.dist-info` folder beside the new one inside
`_internal`, and importlib.metadata returns the first match in directory order
rather than the highest version, so the app reported 0.4.1 while running 0.4.2
code. The installer now clears the old payload; the build-time stamp this tests
makes the reported version independent of it either way.
"""
import lora_explorer


def test_stamp_is_ignored_outside_a_frozen_build(monkeypatch, tmp_path):
    monkeypatch.delattr("sys.frozen", raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    (tmp_path / "lora_explorer").mkdir()
    (tmp_path / "lora_explorer" / "_version.txt").write_text("9.9.9")

    assert lora_explorer._stamped_version() is None
    # A source checkout still answers from package metadata.
    assert lora_explorer._resolve_version() != "9.9.9"


def test_frozen_build_reads_the_stamp(monkeypatch, tmp_path):
    (tmp_path / "lora_explorer").mkdir()
    (tmp_path / "lora_explorer" / "_version.txt").write_text("0.4.3\n")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)

    assert lora_explorer._stamped_version() == "0.4.3"
    assert lora_explorer._resolve_version() == "0.4.3"


def test_frozen_build_without_a_stamp_falls_back_to_metadata(monkeypatch, tmp_path):
    """An older or hand-assembled bundle has no stamp; it must not report an
    empty version or crash."""
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)

    assert lora_explorer._stamped_version() is None
    assert lora_explorer._resolve_version()


def test_empty_stamp_is_treated_as_missing(monkeypatch, tmp_path):
    (tmp_path / "lora_explorer").mkdir()
    (tmp_path / "lora_explorer" / "_version.txt").write_text("   \n")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)

    assert lora_explorer._stamped_version() is None
    assert lora_explorer._resolve_version()


def test_corrupt_stamp_falls_back_instead_of_raising(monkeypatch, tmp_path):
    """This resolves while the root package is being imported, so a damaged
    stamp must not raise: that would take down every entry point."""
    (tmp_path / "lora_explorer").mkdir()
    (tmp_path / "lora_explorer" / "_version.txt").write_bytes(b"\xff\xfe\x00bad")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)

    assert lora_explorer._stamped_version() is None
    assert lora_explorer._resolve_version()


def test_stamp_directory_in_place_of_a_file_falls_back(monkeypatch, tmp_path):
    (tmp_path / "lora_explorer" / "_version.txt").mkdir(parents=True)
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)

    assert lora_explorer._stamped_version() is None
    assert lora_explorer._resolve_version()


def test_module_version_is_a_real_string():
    assert isinstance(lora_explorer.__version__, str)
    assert lora_explorer.__version__
