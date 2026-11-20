"""The ARC adapter for TCKDB: build, write and optionally upload payloads from an ARC run.

The mapping from ARC's output to TCKDB payloads lives in this package. The upload
pipeline is ``tckdb_core``, the transport is ``tckdb-client``, and validation and
storage are TCKDB's.
"""

from tckdb_arc.adapter import TCKDBAdapter, UploadOutcome
from tckdb_arc.config import TCKDBConfig
from tckdb_arc.sweep import run_upload_sweep

__all__ = ["TCKDBAdapter", "TCKDBConfig", "UploadOutcome", "run_upload_sweep"]
