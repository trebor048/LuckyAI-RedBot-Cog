from .lucky_ai import LuckyAICog


async def setup(bot):
    await bot.add_cog(LuckyAICog(bot))
