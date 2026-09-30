"""Russian mobile-friendly text and opaque callback handles for MAX."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from tsr.contracts import ViewModel, content_hash


# Pair only known, short controls. Product choices, files and destructive actions
# keep a full row, so similar names never become ambiguous on mobile.
_BUTTON_GROUPS = {
    "back": "navigation", "resume": "navigation",
    "materials": "resources", "help": "resources",
    "Мои кейсы": "cases", "Новый кейс с актуальным каталогом": "cases",
    "Да": "answer", "Нет": "answer",
    "confirm": "confirmation", "edit": "confirmation",
}


@dataclass(frozen=True)
class MaxButton:
    text: str
    payload: str


@dataclass(frozen=True)
class MaxMessagePayload:
    view_id: UUID
    source_hash: str
    text: str
    buttons: tuple[tuple[MaxButton, ...], ...] = ()

    def as_api_dict(self) -> dict:
        body = {'text': self.text}
        if self.buttons:
            body['attachments'] = [{'type': 'inline_keyboard', 'payload': {
                'buttons': [[{'type': 'callback', 'text': b.text, 'payload': b.payload} for b in row] for row in self.buttons]}}]
        return body


def render_max_view(view: ViewModel) -> MaxMessagePayload:
    # Lazy import: adapter import does not load application/config or resource files.
    from tsr.application.presenter import resolve_message

    lines = [resolve_message(view.title_key)]
    lines.extend(resolve_message(section.text_key, section.parameters) for section in view.sections)
    text = '\n\n'.join(lines)
    if len(text) > 4000:
        raise ValueError('max_view_too_large')
    rows = []
    previous_group = None
    for action in view.actions:
        label = resolve_message(action.label_key)
        if not 1 <= len(label) <= 128 or not 1 <= len(action.action_handle) <= 128:
            raise ValueError('invalid_max_button')
        group = _BUTTON_GROUPS.get(action.label_key)
        button = MaxButton(label, action.action_handle)
        if group and group == previous_group and len(rows[-1]) == 1 and len(label) <= 20 and len(rows[-1][0].text) <= 20:
            rows[-1] += (button,)
            previous_group = None
        else:
            rows.append((button,))
            previous_group = group
    if len(rows) > 30:
        raise ValueError('too_many_max_buttons')
    return MaxMessagePayload(view.view_id, content_hash(view), text, tuple(rows))
