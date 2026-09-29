"""Russian mobile-friendly text and opaque callback handles for MAX."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from tsr.contracts import ViewModel, content_hash


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
    for action in view.actions:
        label = resolve_message(action.label_key)
        if not 1 <= len(label) <= 128 or not 1 <= len(action.action_handle) <= 128:
            raise ValueError('invalid_max_button')
        # One action per row keeps Russian labels readable on a narrow screen.
        rows.append((MaxButton(label, action.action_handle),))
    if len(rows) > 30:
        raise ValueError('too_many_max_buttons')
    return MaxMessagePayload(view.view_id, content_hash(view), text, tuple(rows))
