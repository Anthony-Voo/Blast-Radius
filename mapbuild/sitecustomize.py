import os

if os.environ.get("COVMAP_CHILD") and os.environ.get("COVMAP_PARENT") != str(os.getpid()):
    try:
        import covcore

        covcore.install_child()
    except Exception as e:  # pragma: no cover
        import sys

        print("covmap child install failed", repr(e), file=sys.stderr)
