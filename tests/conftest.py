import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src import allocator, excel_reader as er, sample_data as sd, tq_generator, validator
from src.models import RuleConfig


@pytest.fixture
def cfg():
    c = RuleConfig()
    c.solver.time_limit_s = 20
    return c


def run(df, cfg, previous=None, revision="T"):
    rr = er.read_master(sd.to_workbook_bytes(df), "t.xlsx", cfg)
    res = allocator.allocate(rr.signals, cfg, previous=previous, revision=revision)
    tq_generator.generate_tqs(res)
    return rr, res, validator.validate(res, cfg)
