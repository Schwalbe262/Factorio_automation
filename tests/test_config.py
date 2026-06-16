import os
from unittest.mock import patch

from factorio_ai.config import load_config


def test_scheduler_mode_enables_slurm_even_when_example_config_is_false():
    with patch.dict(os.environ, {"FACTORIO_AI_SLURM_MODE": "scheduler"}, clear=False):
        cfg = load_config()

    assert cfg.slurm_enabled is True
