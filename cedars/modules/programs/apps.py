from django.apps import AppConfig


class ProgramsConfig(AppConfig):
    """The Programs module: the program catalog and one page per program (BR-13), over the program registry. Changes go through
    registry.service; budgets, claims and monthly reports fill their parts of a program's page as those modules arrive."""

    name = "cedars.modules.programs"
    label = "programs"
    verbose_name = "Programs"
