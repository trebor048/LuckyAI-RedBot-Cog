from .roaster import RoasterCog


async def setup(bot):
    await bot.add_cog(RoasterCog(bot))
