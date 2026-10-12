"""Atomic GPU/port reservations across training and deployment (single API worker)."""

from threading import RLock

RESOURCE_LOCK = RLock()
