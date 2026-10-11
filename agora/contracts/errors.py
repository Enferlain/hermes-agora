"""Contract-layer error hierarchy."""

from __future__ import annotations


class ContractError(Exception):
    """Base class for contract-layer failures."""


class ValidationError(ContractError):
    """A value or mapping failed contract validation.

    ``code`` is a ReasonCode string (INVALID_SCHEMA for shape failures,
    INVALID_REFERENCE for typed-reference failures); ``path`` is the dotted
    field path where the failure was detected.
    """

    def __init__(self, message: str, *, code: str = "INVALID_SCHEMA", path: str = "") -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.code = code
        self.path = path


class CanonicalizationError(ContractError):
    """A value cannot be canonically bound."""
