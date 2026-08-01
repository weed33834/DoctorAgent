"""DoctorAgent multi-device sync package.

Exports
-------
SyncMessage, SyncState, FileIndex, SecureSyncProtocol (from protocol)
DeviceDiscovery, UdpDiscovery (from discovery)
DeviceAuth (from auth)
SyncEngine, OfflineOperationQueue, SyncProgressReporter,
SyncStatistics (from engine)
Conflict, ConflictDetector, ConflictResolver, LastWriteWins, KeepBoth,
ManualResolve, CRDTMerge, HLC, VectorClock, CRDTDocument,
ThreeWayMerge, SemanticMerge, ConflictHistory (from conflict)
"""

from doctoragent.sync.auth import DeviceAuth
from doctoragent.sync.conflict import (
    HLC,
    Conflict,
    ConflictDetector,
    ConflictHistory,
    ConflictResolver,
    CRDTDocument,
    CRDTMerge,
    KeepBoth,
    LastWriteWins,
    ManualResolve,
    SemanticMerge,
    ThreeWayMerge,
    VectorClock,
)
from doctoragent.sync.discovery import DeviceDiscovery, UdpDiscovery
from doctoragent.sync.engine import (
    OfflineOperationQueue,
    SyncEngine,
    SyncProgressReporter,
    SyncStatistics,
)
from doctoragent.sync.protocol import FileIndex, SecureSyncProtocol, SyncMessage, SyncState

__all__ = [
    # protocol
    "SyncMessage",
    "SyncState",
    "FileIndex",
    "SecureSyncProtocol",
    # discovery
    "DeviceDiscovery",
    "UdpDiscovery",
    # auth
    "DeviceAuth",
    # engine
    "SyncEngine",
    "OfflineOperationQueue",
    "SyncProgressReporter",
    "SyncStatistics",
    # conflict
    "Conflict",
    "ConflictDetector",
    "ConflictResolver",
    "LastWriteWins",
    "KeepBoth",
    "ManualResolve",
    "CRDTMerge",
    "HLC",
    "VectorClock",
    "CRDTDocument",
    "ThreeWayMerge",
    "SemanticMerge",
    "ConflictHistory",
]
