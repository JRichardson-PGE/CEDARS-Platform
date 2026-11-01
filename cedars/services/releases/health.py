"""The release notes' health check (cedars/core/health.py, BR-12.1 and BR-12.5): the release the site runs has its notes, every limit
change in effect is what the site enforces, and the guides were revised for the releases that changed their part of CEDARS. Quiet
until the first note is published."""

from cedars.core import health

from . import service


def check() -> health.Result:
    result = health.Result("releases.notes", "Release notes", "Release notes, limits and guides match the site", link="/releases/")
    if not service.published().exists():
        result.summary = "No release notes published yet."
        return result
    running = service.running()
    if service.note_for_release(running) is None:
        result.items.append(
            health.Item(f"release:{running}", f"Release {running} is running with no published release note", detail="Publish its notes.")
        )
    for note, change, now in service.limit_mismatches():
        result.items.append(
            health.Item(
                f"limit:{change.limit}:{note.pk}",
                f"{change.limit}: announced as {change.limit_value} from {note.effective_on:%Y-%m-%d}, but the site applies {now}",
                detail="Change the setting, or publish a note withdrawing the change.",
                link=f"/releases/{note.pk}/",
            )
        )
    for g in service.guide_revisions():
        if g.behind:
            result.items.append(
                health.Item(
                    f"guide:{g.slug}:{g.behind[-1]}",
                    f"The {g.title} guide was last revised for {g.release or 'no release'}; "
                    f"{', '.join(g.behind)} changed its part of CEDARS",
                    detail="Revise the guide (manage.py help_revisions --update) in the next release.",
                    link=f"/help/{g.slug}/",
                )
            )
    if result.items:
        result.status = health.ATTENTION
        result.summary = f"{len(result.items)} thing{'s' if len(result.items) != 1 else ''} to put right."
    else:
        result.summary = f"Release {running} has its notes; announced limits and the guides match."
    return result
