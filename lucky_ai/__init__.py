__version__ = "1.1.0"
__author__ = "Lucky AI Team"
__red_end_user_data_statement__ = (
    "This cog stores Discord message IDs, user IDs, command usage, and message content in a local SQLite database "
    "inside Red's cog data directory for AI processing features (roasts, TLDRs, Q&A, hot takes). Message content "
    "is only stored from channels explicitly configured by server admins. Users can opt out with `[p]loptout out`, "
    "and Red deletion requests are supported."
)

async def setup(bot):
    from .core.cog import LuckyAICog
    await bot.add_cog(LuckyAICog(bot))
