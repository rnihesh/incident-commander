"""Fault injection (R3, KTD12): the training classes bad deploy, config change and flag flip,
and the held-out classes (KTD15) expiry and undeclared coupling, written after the freeze.

``inject_fault`` writes a root change, 3-6 timing-matched decoys, an open Incident and its
Symptom nodes into a session world graph. The root is created by exactly the same code path
as the decoys (same delay, author and summary distributions), all new changes sit on distinct
version chains, ids follow the generator's ``chg-NNNNN`` scheme in valid_from order, and the
rows are written in a seeded shuffled order. The answer (root change id + nonce) goes only to
the SQLite store, never into any graph (R26).

Layout: ``common`` (answer types, context, the shared change path, topology reads, row
writers), ``training`` (``inject_fault``), ``heldout``, ``commit_reveal``,
``judge`` (grading and target catalog) and ``storm`` (``inject_storm``). Everything callers
import is re-exported here, so ``from ic.world.faults import X`` keeps working.
"""

from __future__ import annotations

from ic.world.faults.commit_reveal import commitment, commitment_preimage, reveal
from ic.world.faults.common import (
    TRAINING_CLASSES,
    WINDOW_S,
    FaultAnswer,
    InjectedFault,
    InjectionError,
)
from ic.world.faults.common import _impact_services as _impact_services  # routes_judge, tests
from ic.world.faults.common import _max_id as _max_id  # tests
from ic.world.faults.heldout import FAULT_CLASSES, HELDOUT_CLASSES
from ic.world.faults.judge import (
    ALERT_PATH_HOPS,
    ALERT_PATH_MIN_CALLERS,
    CHANGE_KIND_CLASS,
    Comparison,
    alert_path_check,
    claimed_class,
    compare,
    reference_time,
    target_catalog,
)
from ic.world.faults.storm import (
    JOINT_CLASS,
    JOINT_SIGNATURES,
    STORM_FAULT_CLASSES,
    STORM_PAGES,
    STORM_REACH_DEPTH,
    STORM_SPREAD_S,
    STORM_TAIL_S,
    InjectedStorm,
    StormAnswer,
    inject_storm,
)
from ic.world.faults.training import inject_fault

__all__ = [
    "ALERT_PATH_HOPS",
    "ALERT_PATH_MIN_CALLERS",
    "CHANGE_KIND_CLASS",
    "FAULT_CLASSES",
    "HELDOUT_CLASSES",
    "JOINT_CLASS",
    "JOINT_SIGNATURES",
    "STORM_FAULT_CLASSES",
    "STORM_PAGES",
    "STORM_REACH_DEPTH",
    "STORM_SPREAD_S",
    "STORM_TAIL_S",
    "TRAINING_CLASSES",
    "WINDOW_S",
    "Comparison",
    "FaultAnswer",
    "InjectedFault",
    "InjectedStorm",
    "InjectionError",
    "StormAnswer",
    "alert_path_check",
    "claimed_class",
    "commitment",
    "commitment_preimage",
    "compare",
    "inject_fault",
    "inject_storm",
    "reference_time",
    "reveal",
    "target_catalog",
]
