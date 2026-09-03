"""Standalone, disposable-seam deployment evidence package."""

from .controller import DeploymentBlocked, DeploymentController, GateBRequired
from .health import HealthVerifier
from .model import CandidateComposition, HealthReport, PriorRuntimeSnapshot, SlotIdentity, TransactionRecord, TransactionState

__all__ = [
    "CandidateComposition", "DeploymentBlocked", "DeploymentController",
    "GateBRequired", "HealthReport", "HealthVerifier", "PriorRuntimeSnapshot",
    "SlotIdentity", "TransactionRecord", "TransactionState",
]
