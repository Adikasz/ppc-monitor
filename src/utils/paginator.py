"""Lapozható Discord embed lista (gombokkal).

A Discord embed description max. 4096 karakter. Egy hosszú listát (pl. egy
ügyfél 895 kampánya) ezért NEM lehet egyetlen embedbe tenni — a korábbi
megoldás egyszerűen levágta a végét, így 895-ből csak ~35 sor látszott.

Ez a modul a listát oldalakra bontja, és `Előző` / `Következő` gombokkal
lapozhatóvá teszi. A gombok csak annak működnek, aki a parancsot indította
(`owner_id`), a többi felhasználó ephemeral figyelmeztetést kap.

Használat:
    view = Paginator(lines, title="📊 Ügyfél — kampányok", owner_id=interaction.user.id)
    await interaction.followup.send(embed=view.current_embed(), view=view)

Egyoldalas lista esetén a `view` nem tartalmaz gombot (`is_paginated` False),
így nem jelenik meg felesleges vezérlő.
"""
from __future__ import annotations

import discord

# Biztonsági margó a 4096-os Discord embed description limit alá.
_MAX_CHARS_PER_PAGE = 3800
# Egy oldalon ennyi tételnél többet akkor sem mutatunk, ha karakterben elférne
# (olvashatóság — a végtelen görgetés helyett inkább több oldal).
_MAX_ITEMS_PER_PAGE = 20
# A Discord a view-t 15 perc után amúgy is elengedi.
_TIMEOUT_S = 600.0


def paginate_lines(
    lines: list[str],
    *,
    max_chars: int = _MAX_CHARS_PER_PAGE,
    max_items: int = _MAX_ITEMS_PER_PAGE,
) -> list[str]:
    """Sorok listáját oldalakra (összefűzött description-ökre) bontja.

    Egy oldal addig gyűlik, amíg bele nem ütközik a karakter- vagy a
    tétel-limitbe. Egyetlen sor sem vész el: ha egy sor önmagában hosszabb a
    limitnél, saját (csonkolt) oldalt kap.
    """
    if not lines:
        return []

    pages: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in lines:
        # Önmagában túl hosszú sor: külön oldalra, csonkolva.
        if len(line) > max_chars:
            if current:
                pages.append("\n".join(current))
                current, current_len = [], 0
            pages.append(line[:max_chars])
            continue

        # +1 a sorok közti "\n"
        projected = current_len + len(line) + (1 if current else 0)
        if current and (projected > max_chars or len(current) >= max_items):
            pages.append("\n".join(current))
            current, current_len = [line], len(line)
        else:
            current.append(line)
            current_len = projected

    if current:
        pages.append("\n".join(current))
    return pages


class Paginator(discord.ui.View):
    """Gombokkal lapozható embed-lista.

    Paraméterek:
        lines      — a megjelenítendő sorok (egy tétel = egy sor/blokk)
        title      — az embed címe (az oldalszám a footerbe kerül)
        owner_id   — csak ez a Discord user tudja lapozni
        color      — embed szín
        total_label — footer felirat a tételszámhoz (pl. "kampány")
    """

    def __init__(
        self,
        lines: list[str],
        *,
        title: str,
        owner_id: int,
        color: discord.Color | None = None,
        total_label: str = "tétel",
    ) -> None:
        super().__init__(timeout=_TIMEOUT_S)
        self._pages = paginate_lines(lines) or ["—"]
        self._title = title
        self._owner_id = owner_id
        self._color = color or discord.Color.blue()
        self._total = len(lines)
        self._total_label = total_label
        self._index = 0

        # Egyoldalas lista: ne jelenjen meg lapozó vezérlő.
        if not self.is_paginated:
            self.clear_items()
        else:
            self._sync_buttons()

    @property
    def is_paginated(self) -> bool:
        return len(self._pages) > 1

    def current_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self._title,
            description=self._pages[self._index],
            color=self._color,
        )
        if self.is_paginated:
            embed.set_footer(
                text=f"{self._index + 1}/{len(self._pages)}. oldal · "
                     f"összesen {self._total} {self._total_label}"
            )
        else:
            embed.set_footer(text=f"Összesen: {self._total} {self._total_label}")
        return embed

    def _sync_buttons(self) -> None:
        """Szélső oldalakon letiltja a nem értelmes irányt."""
        self.prev_button.disabled = self._index == 0
        self.next_button.disabled = self._index >= len(self._pages) - 1

    async def _guard(self, interaction: discord.Interaction) -> bool:
        """Csak a parancs indítója lapozhat."""
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "Ezt a listát csak az tudja lapozni, aki lekérte. "
                "Futtasd le te is a parancsot.",
                ephemeral=True,
            )
            return False
        return True

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="◀ Előző", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self._guard(interaction):
            return
        self._index = max(0, self._index - 1)
        await self._show(interaction)

    @discord.ui.button(label="Következő ▶", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self._guard(interaction):
            return
        self._index = min(len(self._pages) - 1, self._index + 1)
        await self._show(interaction)

    async def on_timeout(self) -> None:
        """Időtúllépés után a gombok inaktívak (a lista olvasható marad)."""
        for item in self.children:
            item.disabled = True
