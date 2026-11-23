"""MariaDB adapter for Querly.

This module implements the adapter contract against a real MariaDB server
through the ``mariadb`` Python client. MariaDB is a MySQL fork; its protocol is
very close to MySQL's, but the two engines diverge in features such as JSON
support, sequence objects, and transactional DDL behaviour.

Nothing here claims MariaDB is MySQL. Where MariaDB's behaviour differs from
MySQL's, that difference is documented in the capability report and reflected in
compiled artifacts.
"""

from __future__ import annotations

from oq_adapters.mariadb.adapter import MariaDBAdapter

__version__ = "0.1.0.dev0"

__all__ = ["MariaDBAdapter", "__version__"]