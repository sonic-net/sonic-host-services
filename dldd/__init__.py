"""Device Local Diagnosis Daemon.

The package intentionally keeps platform integration at its edges.  Core rule
evaluation and orchestration code consumes typed values and small interfaces so
vendors can supply platform-specific DSE, source, action, and artifact hooks
without modifying the engine.
"""

__version__ = "0.1.0"
