# Darehouse - Discord Bot
Fun and Games bot for my Discord Bot Infrastructure. \
Powered by [darehouse-pylib](https://github.com/mhaxanali/darehouse-pylib)

## Details
- Implements a custom logging module via `bot/logger.py`.
- Uses SQLite for data storage such as metadata and guild configs.
- Uses `config.json` to define cogs and `bot/bot.py` as bot entry point.
- Cog-based bot structure.
- Uses custom Game Engine published on PyPI: [darehouse](https://pypi.org/project/darehouse)

## Usage
### 1. Installation
Run the following commands to pull repository and install dependencies (requires Python 3.11 or greater):
```bash
git clone https://github.com/mhaxanali/darehouse.git
cd darehouse
python -m venv .venv
source .venv/bin/activate # for Linux / MacOS
./.venv/Scripts/Activate.ps1 # for Windows (Powershell)
pip install -r requirements.txt
```
Create `.env` using the template @ `.env.example` and fill in values.
### 2. Adding Prompts
To add Truth or Dare prompts put them in `bot/resources/data/` with truths in `bot/resources/data/truths.json` and dares in `bot/resources/data/dares.json`. Use JSON list syntax. \
These prompts will be added to the database on bot startup.
### 3. Starting the Bot
From the project directory, run:
```bash
python -m bot.bot
```
This will start the bot and notify of any errors.
### 4. Configuration
Use commands such as `+config help` or `+tod help` in Discord after starting the bot and inviting it to a server in order to view config settings and details.

## Author
[Muhammad Hasan](https://github.com/mhaxanali)

## License
[MIT License](https://github.com/mhaxanal/darehouse/blob/main/LICENSE)
