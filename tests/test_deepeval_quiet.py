import os

import pytest
from conftest import DEEPEVAL_IMPORTED_BEFORE_SWITCHES
from deepeval.confident.api import is_confident


def test_deepeval_is_switched_off_before_import_and_kept_out_of_pytest(
    pytestconfig: pytest.Config,
) -> None:
    assert not DEEPEVAL_IMPORTED_BEFORE_SWITCHES
    assert os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] == "1"
    assert os.environ["DEEPEVAL_DISABLE_DOTENV"] == "1"
    assert os.environ["DEEPEVAL_FILE_SYSTEM"] == "READ_ONLY"
    assert os.environ["DEEPEVAL_DISABLE_LEGACY_KEYFILE"] == "1"
    assert "CONFIDENT_API_KEY" not in os.environ
    for plugin in ("deepeval", "asyncio", "repeat", "rerunfailures", "xdist"):
        assert not pytestconfig.pluginmanager.has_plugin(plugin)
    assert not is_confident()
