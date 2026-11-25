"""Connection management, pooling, parameter binding, transactions, timeouts, and cancellation."""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from oq.runtime.contract import (
    Adapter,
    AdapterError,
    AdapterErrorCode,
    Connection,
    ConnectionConfig,
    IsolationLevel,
    SecretProvider,
    Transaction,
    TransactionOptions,
)

logger = logging.getLogger("oq.runtime.connection")


class ParameterBinder:
    """Safe parameter binding and validation preventing SQL injection and unsafe identifiers."""

    @staticmethod
    def validate_identifier(name: str) -> str:
        """Sanitize and validate SQL identifiers (table/column names).

        Raises ValueError if identifier contains illegal characters.
        """
        if not name or not isinstance(name, str):
            raise ValueError("Identifier must be a non-empty string.")

        # Check for dangerous patterns / SQL injection attempts in identifiers
        if (
            ";" in name
            or "--" in name
            or "/*" in name
            or "*/" in name
            or "'" in name
            or '"' in name
            or " " in name
        ):
            raise ValueError(f"Invalid or unsafe identifier name: '{name}'")

        return name

    @staticmethod
    def bind_parameters(params: dict[str, Any]) -> dict[str, Any]:
        """Validate and prepare parameters for query execution."""
        bound = {}
        for k, v in params.items():
            ParameterBinder.validate_identifier(k)
            bound[k] = v
        return bound


class ConnectionPool:
    """Connection pooling implementation with max limits, timeouts, and health checks."""

    def __init__(
        self,
        adapter: Adapter,
        config: ConnectionConfig,
        secrets: SecretProvider,
        min_size: int = 1,
        max_size: int = 10,
        idle_timeout: float = 300.0,
    ) -> None:
        self.adapter = adapter
        self.config = config
        self.secrets = secrets
        self.min_size = min_size
        self.max_size = max_size
        self.idle_timeout = idle_timeout

        self._pool: list[Connection] = []
        self._in_use: list[Connection] = []
        self._closed = False

        # Pre-warm min connections
        for _ in range(self.min_size):
            try:
                conn = self.adapter.connect(self.config, secrets=self.secrets)
                self._pool.append(conn)
            except Exception as e:
                logger.warning(f"Failed to create pre-warmed connection: {e}")

    def acquire(self, timeout: float = 30.0) -> Connection:
        """Acquire a connection from pool or create a new one if limit not reached."""
        if self._closed:
            raise AdapterError(
                code=AdapterErrorCode.CONNECTION,
                message="Connection pool is closed.",
            )

        start_time = time.time()
        while time.time() - start_time < timeout:
            # Check available idle connections
            while self._pool:
                conn = self._pool.pop()
                if not conn.is_closed and conn.ping():
                    self._in_use.append(conn)
                    return conn
                else:
                    with contextlib.suppress(Exception):
                        self.adapter.close(conn)

            # If under max_size, create new connection
            if len(self._in_use) < self.max_size:
                try:
                    conn = self.adapter.connect(self.config, secrets=self.secrets)
                    self._in_use.append(conn)
                    return conn
                except Exception as e:
                    raise AdapterError(
                        code=AdapterErrorCode.CONNECTION,
                        message=f"Failed to connect: {e}",
                        cause=e,
                    ) from e

            time.sleep(0.05)

        raise AdapterError(
            code=AdapterErrorCode.TIMEOUT,
            message=f"Timeout waiting for connection from pool after {timeout} seconds.",
        )

    def release(self, conn: Connection) -> None:
        """Release a connection back to the pool."""
        if conn in self._in_use:
            self._in_use.remove(conn)

        if (
            not self._closed
            and not conn.is_closed
            and len(self._pool) < self.max_size
            and conn.ping()
        ):
            self._pool.append(conn)
            return

        # Close if pool full or connection unhealthy/closed
        with contextlib.suppress(Exception):
            self.adapter.close(conn)

    def close_all(self) -> None:
        """Close all pooled connections."""
        self._closed = True
        for conn in self._pool + self._in_use:
            with contextlib.suppress(Exception):
                self.adapter.close(conn)
        self._pool.clear()
        self._in_use.clear()


class ConnectionManager:
    """Manages adapter registration, database connection profiles, and active pools."""

    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}
        self._profiles: dict[str, ConnectionConfig] = {}
        self._pools: dict[str, ConnectionPool] = {}
        self._secrets = SecretProvider()

    def register_adapter(self, adapter: Adapter) -> None:
        """Register a database adapter."""
        self._adapters[adapter.id] = adapter

    def get_adapter(self, adapter_id: str) -> Adapter:
        """Retrieve a registered adapter."""
        if adapter_id not in self._adapters:
            raise AdapterError(
                code=AdapterErrorCode.UNSUPPORTED,
                message=f"Adapter '{adapter_id}' is not registered.",
            )
        return self._adapters[adapter_id]

    def add_profile(self, name: str, config: ConnectionConfig) -> None:
        """Add a named database connection profile."""
        self._profiles[name] = config

    def get_profile(self, name: str) -> ConnectionConfig:
        """Get connection configuration by profile name."""
        if name not in self._profiles:
            raise ValueError(f"Profile '{name}' not found.")
        return self._profiles[name]

    def get_pool(self, profile_name: str, secrets: SecretProvider | None = None) -> ConnectionPool:
        """Get or create connection pool for profile."""
        if profile_name not in self._pools:
            config = self.get_profile(profile_name)
            adapter = self.get_adapter(config.adapter)
            sec = secrets or self._secrets
            self._pools[profile_name] = ConnectionPool(adapter, config, sec)
        return self._pools[profile_name]

    def close(self) -> None:
        """Close all managed pools and resources."""
        for pool in self._pools.values():
            pool.close_all()
        self._pools.clear()


class TransactionManager:
    """Manages transactional scope, nested transactions / savepoints, and isolation levels."""

    def __init__(self, adapter: Adapter, connection: Connection) -> None:
        self.adapter = adapter
        self.connection = connection
        self._tx_stack: list[Transaction] = []

    def begin(
        self,
        isolation: IsolationLevel = IsolationLevel.READ_COMMITTED,
        read_only: bool = False,
    ) -> Transaction:
        """Begin transaction with given options."""
        opts = TransactionOptions(isolation=isolation, read_only=read_only)
        tx = self.adapter.begin(self.connection, opts)
        self._tx_stack.append(tx)
        return tx

    def commit(self) -> None:
        """Commit the active transaction."""
        if not self._tx_stack:
            raise AdapterError(
                code=AdapterErrorCode.SYNTAX,
                message="No active transaction to commit.",
            )
        tx = self._tx_stack.pop()
        self.adapter.commit(tx)

    def rollback(self) -> None:
        """Roll back the active transaction."""
        if not self._tx_stack:
            return
        tx = self._tx_stack.pop()
        self.adapter.rollback(tx)

    def __enter__(self) -> TransactionManager:
        self.begin()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
