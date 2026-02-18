import discord
from discord.ext import commands
import yaml
import os
from pathlib import Path

def load_config(config_path='config.yaml'):
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

config = load_config()
intents = discord.Intents.default()
# Simulate main.py construction
print("Default voice_states:", intents.voice_states)

# Check if voice_states is explicitly disabled anywhere
# In main.py: super().__init__(..., intents=intents, ...)

# Let's check what a bot instance sees
bot = commands.Bot(command_prefix='/', intents=intents)
print("Bot intents voice_states:", bot.intents.voice_states)
print("Bot intents message_content:", bot.intents.message_content)
print("Bot intents members:", bot.intents.members)
