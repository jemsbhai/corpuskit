"""Published distribution and application-branding invariants."""

from __future__ import annotations

import tomllib
from pathlib import Path

from corpuskit import __version__

_ROOT = Path(__file__).parents[2]


def test_distribution_metadata_preserves_the_corpuskit_product_contract() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["name"] == "corpuskit-app"
    assert project["version"] == __version__
    assert project["requires-python"] == ">=3.12,<3.13"
    assert "corpusgen==0.1.7" in project["dependencies"]
    assert project["urls"]["Repository"] == "https://github.com/jemsbhai/corpuskit"
    assert project["scripts"] == {
        "corpuskit-api": "corpuskit.api.cli:main",
        "corpuskit-continuity": "corpuskit.operations.continuity_cli:main",
        "corpuskit-db": "corpuskit.persistence.migration_cli:main",
        "corpuskit-dispatcher": "corpuskit.worker.dispatcher_cli:main",
        "corpuskit-maintenance": "corpuskit.operations.maintenance_cli:main",
        "corpuskit-phoible": "corpuskit.operations.phoible_cli:main",
        "corpuskit-worker": "corpuskit.worker.cli:main",
    }


def test_typed_marker_is_packaged_under_the_import_namespace() -> None:
    assert (_ROOT / "src" / "corpuskit" / "py.typed").is_file()
