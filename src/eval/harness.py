"""Compatibility entrypoint: python -m src.eval.harness --fail-under .8."""

from .run import check_case, evaluate, main

__all__ = ["check_case", "evaluate", "main"]


if __name__ == "__main__":
    main()
