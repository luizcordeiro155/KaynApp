"""Cog reservado para IA, imagens e KaynCodex.

Os comandos continuam registrados por `cogs.legacy_runtime` para preservar 100% do comportamento atual.
Use este arquivo para migrar comandos desse domínio aos poucos, sem mexer no `main.py`.
"""

from discord.ext import commands


class AICog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot


async def setup(bot: commands.Bot):
    await bot.add_cog(AICog(bot))
