"""Empty conftest so pytest adds the project root to ``sys.path``.

This lets tests do ``from etherscan_client import detect_whales`` without
installing the project as a package.
"""