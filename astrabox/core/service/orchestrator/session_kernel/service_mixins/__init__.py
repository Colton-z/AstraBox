"""Mixin package for :class:`SessionKernelService`.

``_helpers`` holds shared module-scope constants, functions, the
``_ResumeCursorTracker`` class, and the pure terminal-frame-proof predicates
that both the read-rendering and recovery surfaces import — the anti-cycle
keystone that lets the facade decompose into mixins without any mixin importing
another mixin or the facade module.
"""
