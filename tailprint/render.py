"""Isolated document preparation process. The parent alone talks to the spooler."""

import json
import sys
from pathlib import Path


def main():
    source = Path(sys.argv[1])
    if sys.platform.startswith("linux") and sys.maxsize > 2**32:
        import resource

        # LibreOffice reserves considerably more virtual address space than resident RAM.
        mib = 6144 if source.suffix in {".doc", ".docx", ".xls", ".xlsx"} else 2500
        resource.setrlimit(resource.RLIMIT_AS, (mib * 1024**2, mib * 1024**2))
        resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024**2, 256 * 1024**2))
    from .documents import prepare, settings

    prepare(source, settings(json.loads(sys.argv[2])), source.parent / "sheets.pdf")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if not sys.stderr.isatty():
            sys.stderr.reconfigure(encoding="utf-8")
        print(str(exc)[:350] or "Не удалось подготовить документ.", file=sys.stderr)
        sys.exit(1)
