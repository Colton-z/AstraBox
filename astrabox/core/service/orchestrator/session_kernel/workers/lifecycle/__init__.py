"""``SessionLifecycleWorker`` and its lifecycle leaf modules.

``worker.py`` holds the worker entry class (``__init__``, ``run_once``,
``_execute_command``, and the two thin dispatch adapters), composed from the
sibling helper modules (``retry.py``, ``assistant_workspace.py``,
``projection.py``, ``create.py``, ``commands.py``, ``recover.py``,
``startup.py``) as mixins.
"""
