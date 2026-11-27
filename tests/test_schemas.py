"""schemas/*.schema.json parse, cover exactly the contract names, and agree with core.contracts."""
import json

import pytest

from core import contracts

SCHEMA_FILES = sorted(contracts.SCHEMAS_DIR.glob("*.json"))


def test_schemas_dir_exists_in_kit(root):
    assert contracts.SCHEMAS_DIR.is_dir()
    assert contracts.SCHEMAS_DIR == root / "schemas"


def test_schema_files_match_contract_names():
    names = {p.name for p in SCHEMA_FILES}
    assert names == {f"{c}.schema.json" for c in contracts.CONTRACT_NAMES}


@pytest.mark.parametrize("path", SCHEMA_FILES, ids=[p.name for p in SCHEMA_FILES])
def test_schema_parses_as_object_schema(path):
    with open(path, encoding="utf-8") as fh:
        schema = json.load(fh)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert isinstance(schema["properties"], dict)
    assert isinstance(schema["required"], list)


@pytest.mark.parametrize("name", contracts.CONTRACT_NAMES)
def test_load_schema_and_field_tables_agree(name):
    schema = contracts.load_schema(name)
    fields = contracts.CONTRACT_FIELDS[name]
    assert set(schema["properties"]) == set(fields), f"{name}: property set differs from field table"
    required = {k for k, (_, req) in fields.items() if req}
    assert set(schema["required"]) == required, f"{name}: required set differs from field table"
    assert schema["properties"]["schema_version"] == {"const": contracts.SCHEMA_VERSION}


def test_enums_match_schema():
    assert contracts.load_schema("brain")["properties"]["decision"]["enum"] == list(contracts.BRAIN_DECISIONS)
    assert contracts.load_schema("worker")["properties"]["status"]["enum"] == list(contracts.WORKER_STATUSES)
    assert contracts.load_schema("reviewer")["properties"]["verdict"]["enum"] == list(contracts.REVIEWER_VERDICTS)
    assert contracts.load_schema("job")["properties"]["mode"]["enum"] == list(contracts.JOB_MODES)
    assert contracts.load_schema("state")["properties"]["phase"]["enum"] == list(contracts.PHASES)


def test_load_schema_rejects_unknown_name():
    with pytest.raises(contracts.ContractError):
        contracts.load_schema("nope")
