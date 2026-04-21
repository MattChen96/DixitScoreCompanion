"""
Authoritative in-memory game registry.

Structure: ``games[game_id] -> Game``. No persistence; cleared on process exit.
"""

from typing import Optional

from backend.models.game import Game

games: dict[str, Game] = {}


def get_game(game_id: str) -> Optional[Game]:
    return games.get(game_id)


def set_game(game_id: str, game: Game) -> None:
    games[game_id] = game


def delete_game(game_id: str) -> None:
    games.pop(game_id, None)
