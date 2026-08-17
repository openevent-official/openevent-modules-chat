from __future__ import annotations

from dataclasses import dataclass

from .errors import HistoryConflictError, TurnAlreadyExistsError, TurnNotFoundError
from .model import ParsedMessage, TurnAppend, TurnCancel, TurnEnd, TurnRef, TurnStart


@dataclass
class _TurnState:
    turn_ref: TurnRef
    start_seq: int
    reply_to_turns: tuple[TurnRef, ...]
    tail_seq: int
    terminal_kind: str | None = None
    terminal_seq: int | None = None

    @property
    def terminal(self) -> bool:
        return self.terminal_kind is not None


class ConversationState:
    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        self._turns: dict[TurnRef, _TurnState] = {}

    def has_turn(self, turn_ref: TurnRef) -> bool:
        return turn_ref in self._turns

    def require_turn(self, turn_ref: TurnRef) -> _TurnState:
        try:
            return self._turns[turn_ref]
        except KeyError:
            raise TurnNotFoundError(self.channel_id, turn_ref) from None

    def require_new_turn(self, turn_ref: TurnRef) -> None:
        if turn_ref in self._turns:
            raise TurnAlreadyExistsError(self.channel_id, turn_ref)

    def validate_replies(self, turn_ref: TurnRef, replies: tuple[TurnRef, ...]) -> None:
        if turn_ref in replies:
            raise HistoryConflictError(f"turn {turn_ref!r} replies to itself in channel {self.channel_id}")
        for reply in replies:
            if reply not in self._turns:
                raise TurnNotFoundError(self.channel_id, reply)

    def apply(self, message: ParsedMessage) -> None:
        event = message.payload
        turn_ref = message.turn_ref

        if isinstance(event, TurnStart):
            if turn_ref in self._turns:
                raise HistoryConflictError(f"duplicate start for turn {turn_ref!r} in channel {self.channel_id}")
            try:
                self.validate_replies(turn_ref, event.reply_to_turns)
            except TurnNotFoundError as exc:
                raise HistoryConflictError(str(exc)) from exc
            self._turns[turn_ref] = _TurnState(
                turn_ref=turn_ref,
                start_seq=message.seq,
                reply_to_turns=event.reply_to_turns,
                tail_seq=message.seq,
            )
            return

        try:
            state = self._turns[turn_ref]
        except KeyError:
            raise HistoryConflictError(f"event targets missing turn {turn_ref!r} in channel {self.channel_id}") from None

        if state.terminal:
            return

        if isinstance(event, TurnAppend):
            if event.pre_seq != state.tail_seq:
                raise HistoryConflictError(
                    f"turn {turn_ref!r} in channel {self.channel_id} expected pre_seq {state.tail_seq}, got {event.pre_seq}"
                )
            state.tail_seq = message.seq
            return

        if isinstance(event, TurnEnd):
            if message.principal != turn_ref.principal:
                raise HistoryConflictError(f"turn.end publisher does not own {turn_ref!r} in channel {self.channel_id}")
            if event.pre_seq != state.tail_seq:
                raise HistoryConflictError(
                    f"turn {turn_ref!r} in channel {self.channel_id} expected pre_seq {state.tail_seq}, got {event.pre_seq}"
                )
            state.terminal_kind = event.kind
            state.terminal_seq = message.seq
            return

        if isinstance(event, TurnCancel):
            state.terminal_kind = event.kind
            state.terminal_seq = message.seq
            return

        raise HistoryConflictError(f"unsupported event for turn {turn_ref!r}")

