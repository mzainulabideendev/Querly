"""Studio configuration and secure local storage paths."""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "QuerlyStudio"

STUDIO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKSPACE = STUDIO_ROOT / "workspace"


def data_dir() -> Path:
    override = os.environ.get("QUERLY_STUDIO_DATA_DIR")
    if override:
        path = Path(override).expanduser().resolve()
    else:
        base = os.environ.get("APPDATA")
        if base:
            path = Path(base) / APP_DIR_NAME
        else:
            path = Path.home() / ".querly-studio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def connections_file() -> Path:
    return data_dir() / "connections.json"


def workspace_dir() -> Path:
    override = os.environ.get("QUERLY_STUDIO_WORKSPACE")
    path = Path(override).expanduser().resolve() if override else DEFAULT_WORKSPACE
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_default_workspace() -> Path:
    root = workspace_dir()
    examples = {
        "welcome.qrl": SAMPLE_SCHEMA,
        "queries.qrl": SAMPLE_QUERIES,
        "bandwidth.qrl": SAMPLE_BANDWIDTH,
    }
    for name, content in examples.items():
        path = root / name
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    return root


SAMPLE_SCHEMA = """-- Querly Studio sample schema
-- Run this file against a SQLite connection to create the table.

table users {
  id: integer @primary @auto
  name: text @required
  email: email @unique
  age: integer?
  created_at: timestamp @default(now())
}

index idx_users_name (name)
unique (name, email)
"""

SAMPLE_QUERIES = """-- Both spellings compile to the same IR.

query adults {
  from users
  select id, name, age
  where age is greater than 18
  order by name asc
  limit 50
}

query search($term: text) {
  from users
  select id, name, email
  where name like $term
  order by name asc
}

insert into users (name, email, age) values ("Ada Lovelace", "ada@example.com", 36) returning id, name
"""

SAMPLE_BANDWIDTH = """create table accounts with:
  id as required number primary key auto
  account as required text
  plan as optional text
  monthly_gb as required number
  used_gb as required number

get all accounts where used_gb is greater than monthly_gb sort by account ascending
"""
