"""Compatibility exports for shared Access test builders."""

from tests.builders.access import (
    create_neutral_test_user,
    create_provisioned_test_user,
)

__all__ = ["create_neutral_test_user", "create_provisioned_test_user"]
