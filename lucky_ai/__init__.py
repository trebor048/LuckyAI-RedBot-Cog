from .core.cog import LuckyAICog

__version__ = "1.0.0"
__author__ = "Lucky AI Team"
__red_end_user_data_statement__ = (
    "This cog stores Discord message IDs, user IDs, and message content in a local SQLite database "
    "for AI processing features (roasts, TLDRs, Q&A). Message content is only stored from channels "
    "explicitly configured by server admins. Users can opt out of being roasted via `[p]loptout out`."
)

async def setup(bot):
    await bot.add_cog(LuckyAICog(bot))
