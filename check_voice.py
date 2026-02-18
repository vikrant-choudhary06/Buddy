import discord
import nacl
print("Discord version:", discord.__version__)
print("PyNaCl version:", nacl.__version__)
try:
    discord.opus.load_opus(None)
    print("Opus loaded:", discord.opus.is_loaded())
except Exception as e:
    print("Opus load error:", e)
