"""Private subprocess entry point; never prints raw content or parser errors."""
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .extractors import extract_file


def main():
    if len(sys.argv) != 3:
        return 2
    if os.name != "nt":
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (20 * 1024 * 1024,) * 2)
    try:
        extraction = extract_file(Path(sys.argv[1]))
        payload = json.dumps(asdict(extraction), ensure_ascii=False).encode("utf-8")
        if len(payload) > 20 * 1024 * 1024:
            return 1
        Path(sys.argv[2]).write_bytes(payload)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
