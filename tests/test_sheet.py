"""CaseSheetTool 与 sheet_plans 切片。"""

from pathlib import Path

import pytest

from work_agent.graph.helpers.sheet_plans import apply_column_mapping
from work_agent.tools.mock.sheet import LocalCaseSheetTool
from work_agent.tools.models import SheetTable


def test_read_csv(tmp_path: Path):
    p = tmp_path / "cases.csv"
    p.write_text(
        "用例名,版本,备注\n"
        "CaseA_235T_nmimo,27B,a\n"
        "HF_20B_PUSCH_001,26A,b\n"
        "HF_20B_PUSCH_002,26A,c\n",
        encoding="utf-8",
    )
    table = LocalCaseSheetTool().read(str(p))
    assert table.headers[0] == "用例名"
    assert len(table.rows) == 3
    assert table.rows[0][0] == "CaseA_235T_nmimo"


def test_apply_column_mapping_row_limit_and_spoken_env():
    table = SheetTable(
        headers=["case", "ver", "env"],
        rows=[
            ["CaseA_235T_nmimo", "27B", "7.223.10.11"],
            ["HF_20B_PUSCH_001", "27B", "7.223.10.11"],
            ["HF_20B_PUSCH_002", "26A", "7.223.10.11"],
            ["HF_20B_PUSCH_003", "26A", "7.223.10.11"],
        ],
    )
    plans = apply_column_mapping(
        table,
        case_name_col=0,
        version_col=1,
        env_col=2,
        row_limit=3,
        spoken_env="7.223.1.9",
        spoken_version="",
    )
    # 口头 env 覆盖表内；前 3 条 → 27B 两条 + 26A 一条
    assert len(plans) == 2
    by_ver = {p["version"]: p for p in plans}
    assert by_ver["27B"]["case_names"] == ["CaseA_235T_nmimo", "HF_20B_PUSCH_001"]
    assert by_ver["26A"]["case_names"] == ["HF_20B_PUSCH_002"]
    assert all(p["env"] == "7.223.1.9" for p in plans)
    assert all(p.get("logic_constraint") == "" for p in plans)


def test_apply_column_mapping_spoken_constraint():
    table = SheetTable(
        headers=["case", "ver"],
        rows=[
            ["HF_20B_PUSCH_001", "27B"],
            ["HF_20B_PUSCH_002", "27B"],
        ],
    )
    plans = apply_column_mapping(
        table,
        case_name_col=0,
        version_col=1,
        env_col=None,
        row_limit=None,
        spoken_env="3BBL_86_1BBL86",
        spoken_version="",
        spoken_constraint="85+86",
    )
    assert len(plans) == 1
    assert plans[0]["env"] == "3BBL_86_1BBL86"
    assert plans[0]["logic_constraint"] == "85+86"
    assert plans[0]["case_names"] == ["HF_20B_PUSCH_001", "HF_20B_PUSCH_002"]


def test_read_missing_file():
    with pytest.raises(FileNotFoundError):
        LocalCaseSheetTool().read("D:/no_such_sheet_xyz.csv")
