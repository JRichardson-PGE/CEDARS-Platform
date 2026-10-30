# The pinned ReCET engine

`recet-commit` names the ReCET commit the platform is built and tested against (`JRichardson-PGE/ReCET`, public under MIT). The platform
imports the engine unchanged and never copies its code. To move the pin: change the commit, install it, re-export the field catalog
(`uv run python scripts/export_recet_dictionary.py data/dictionary/`) and run the tests; `tests/test_dictionary.py` checks the shipped
catalog is the pinned engine's.

Install it locally:

    uv pip install "recet @ git+https://github.com/JRichardson-PGE/ReCET@$(cat engine/recet-commit)"

CI installs it in every run. Without it installed locally, the tests that need the engine are skipped.

The web image carries the engine too (Jake, 2026-10-30): the Results, Compare and Edit inputs pages call its code unchanged. Build its
wheel first, then the image:

    scripts/build_engine_wheel.sh       # or RECET_SOURCE=<a ReCET checkout at the pinned commit>
    docker build -t cedars-platform .

Without a wheel in `engine/wheels/` the image builds without the engine and those pages say so. CI's docker job builds the wheel and
checks the image imports it. The wheel's own dependencies (pandas and the rest) are resolved when it is installed,
outside `uv.lock`.
