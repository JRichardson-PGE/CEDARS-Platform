# The pinned ReCET engine

`recet-commit` names the ReCET commit the platform is built and tested against (ReCET is private: `JRichardson-PGE/ReCET`). The platform
imports the engine unchanged and never copies its code. To move the pin: change the commit, install it, re-export the field catalog
(`uv run python scripts/export_recet_dictionary.py data/dictionary/`) and run the tests; `tests/test_dictionary.py` checks the shipped
catalog is the pinned engine's.

Install it locally:

    uv pip install "recet @ git+https://github.com/JRichardson-PGE/ReCET@$(cat engine/recet-commit)"

CI installs it when the repository has the `RECET_READ_TOKEN` secret (a fine-grained token with read-only Contents on ReCET); without it
the tests that need the engine are skipped.
