"""SWE-Bench adapter — runs mini-agent over SWE-Bench instances and emits
``predictions.json`` files compatible with the official ``sb-cli`` evaluator.

Public entry points:
    - :func:`mini_agent.benchmarks.swebench.dataset.load_swebench_dataset`
    - :func:`mini_agent.benchmarks.swebench.batch_runner.run_batch`
    - ``python -m mini_agent.benchmarks.swebench.cli ...``
"""

from mini_agent.benchmarks.swebench.dataset import (
    SWEBenchInstance,
    load_swebench_dataset,
)

__all__ = ["SWEBenchInstance", "load_swebench_dataset"]
