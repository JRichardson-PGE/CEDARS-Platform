"""What a release note's notification says (the notifications service's concern hook, registered in apps.ready): published notes go
to everyone subscribed to release notes, whatever PAs they follow (a release note concerns no one PA)."""

from cedars.services.notifications.service import Concern

from .models import ReleaseNote


def _date(d) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def concern(event) -> Concern | None:
    note = ReleaseNote.objects.filter(pk=event.object_id).prefetch_related("changes").first()
    if note is None:
        return None
    link = f"/releases/{note.pk}/"
    if event.action == "release.withdrawn":
        return Concern(
            link=link,
            title=f"Withdrawn: {note.title}",
            body=(
                f"The changes announced for {_date(note.effective_on)} will not take effect as announced.\nReason: {note.withdrawn_reason}"
            ),
        )
    if event.action != "release.published":
        return None
    lines = [note.summary, ""]
    for c in note.changes.all():
        line = f"- {c.get_kind_display()}: {c.text}"
        if c.limit_value:
            line += f" New value: {c.limit_value}."
        if c.action:
            line += f" What to do: {c.action}"
        lines.append(line)
    if note.short_notice_reason:
        lines += ["", f"Why at short notice: {note.short_notice_reason}"]
    release = f" (CEDARS {note.version})" if note.version else ""
    return Concern(link=link, title=f"Coming on {_date(note.effective_on)}{release}: {note.title}", body="\n".join(lines))
