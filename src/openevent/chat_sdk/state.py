from __future__ import annotations

from dataclasses import dataclass

from .errors import HistoryConflictError, TurnAlreadyExistsError, TurnNotFoundError
from .model import ParsedMessage, TurnAppend, TurnCancel, TurnEnd, TurnRef, TurnSingle, TurnStart


@dataclass
class _TurnState:
    turn_ref: TurnRef
    creation_kind: str
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

    def has_turn(self, ref: TurnRef) -> bool:
        return ref in self._turns

    def require_turn(self, ref: TurnRef) -> _TurnState:
        try:
            return self._turns[ref]
        except KeyError:
            raise TurnNotFoundError(ref) from None

    def require_new_turn(self, ref: TurnRef) -> None:
        if ref in self._turns:
            raise TurnAlreadyExistsError(ref)

    def validate_replies(self, ref: TurnRef, replies: tuple[TurnRef, ...]) -> None:
        if ref in replies:
            raise HistoryConflictError(f"turn {ref!r} replies to itself")
        for reply in replies:
            if reply not in self._turns:
                raise TurnNotFoundError(reply)

    def apply(self, message: ParsedMessage) -> None:
        event = message.payload
        ref = message.turn_ref

        if isinstance(event, (TurnSingle, TurnStart)):
            if ref in self._turns:
                raise HistoryConflictError(f"duplicate creation for turn {ref!r}")
            try:
                self.validate_replies(ref, event.reply_to_turns)
            except (TurnNotFoundError, HistoryConflictError) as exc:
                raise HistoryConflictError(str(exc)) from exc
            self._turns[ref] = _TurnState(
                turn_ref=ref,
                creation_kind=event.kind,
                start_seq=message.seq,
                reply_to_turns=event.reply_to_turns,
                tail_seq=message.seq,
                terminal_kind=event.kind if isinstance(event, TurnSingle) else None,
                terminal_seq=message.seq if isinstance(event, TurnSingle) else None,
            )
            return

        try:
            state = self._turns[ref]
        except KeyError:
            raise HistoryConflictError(f"event targets missing turn {ref!r}") from None

        if state.terminal:
            return

        if isinstance(event, TurnAppend):
            if message.principal != ref.principal or event.pre_seq != state.tail_seq:
                raise HistoryConflictError(f"invalid append chain for turn {ref!r}")
            state.tail_seq = message.seq
            return

        if isinstance(event, TurnEnd):
            if message.principal != ref.principal or event.pre_seq != state.tail_seq:
                raise HistoryConflictError(f"invalid end chain for turn {ref!r}")
            state.terminal_kind = event.kind
            state.terminal_seq = message.seq
            return

        if isinstance(event, TurnCancel):
            state.terminal_kind = event.kind
            state.terminal_seq = message.seq
            return

        raise HistoryConflictError(f"unsupported event for turn {ref!r}")
