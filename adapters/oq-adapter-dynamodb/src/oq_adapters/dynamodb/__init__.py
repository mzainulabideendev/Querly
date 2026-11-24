"""Amazon DynamoDB adapter for Querly.

Data model: key-value. Driver: boto3>=1.34 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.dynamodb.adapter import DynamoDBAdapter

__version__ = "0.1.0.dev0"

__all__ = ["DynamoDBAdapter", "__version__"]
