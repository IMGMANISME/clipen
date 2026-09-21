"""Errors shared by independent product-domain state machines."""


class InvalidTransition(ValueError):
    """A requested state change violates a product state machine."""
